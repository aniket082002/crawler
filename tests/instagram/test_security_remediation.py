"""Regression tests for the Phase 1 security-audit remediation:
M2 redirects, L1 cursors, L2 no query tokens, L3 request secrets, L4 shared
redaction, L5 hard limits, L6 response size, L7 app secret format, I4.
Fake credentials and fake transports only; no network."""
import copy
import dataclasses
import io
import json
import logging
import pickle

import pytest

from tests.instagram.conftest import FAKE_FB_TOKEN, FAKE_SECRET, FakeTransport, SleepRecorder, response
from webcrawler.instagram import client as C
from webcrawler.instagram.client import (
    GraphApiClient, Paginator, PreparedRequest, RawResponse, paging_call, validate_cursor,
    validate_field_expression,
)
from webcrawler.instagram.config import ConfigError, Secret, load_config
from webcrawler.instagram.errors import (
    ErrorCategory, InvalidResponseError, RedirectRejectedError, ResponseTooLargeError, classify_error,
)
from webcrawler.instagram.redaction import REDACTED, Redactor, default_redactor, get_logger

PLAIN_TOKEN = "plain-token-without-known-prefix-0001"  # not caught by any shape pattern


def make_client(transport, **kw):
    kw.setdefault("sleep", SleepRecorder())
    kw.setdefault("redactor", Redactor())
    kw.setdefault("access_token", FAKE_FB_TOKEN)
    return GraphApiClient(base_url="https://graph.facebook.com", api_version="v27.0",
                          transport=transport, **kw)


# =========================== M2: redirects are never followed ===========================
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirect_rejected_not_followed_not_retried(status):
    evil = "https://evil.example/steal"
    sleep = SleepRecorder()
    t = FakeTransport(response(status, {}, headers={"Location": evil}), response(200, {"id": "x"}))
    with pytest.raises(RedirectRejectedError) as exc:
        make_client(t, sleep=sleep, max_retries=5).get("me")
    assert exc.value.category is ErrorCategory.REDIRECT_REJECTED and not exc.value.retryable
    assert len(t.requests) == 1 and sleep.calls == []           # no retry, no second request
    assert all(r.host == "graph.facebook.com" for r in t.requests)  # destination never requested
    assert evil not in str(exc.value)


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_redirect_never_forwards_authorization(status):
    """The client never builds a credential-carrying request for the redirect
    target: the transport sees exactly one request, to the approved host, and
    no request object anywhere points at the Location host. (A request object
    pointing at another host cannot even be created - see test_final_remediation
    R4 tests.)"""
    t = FakeTransport(response(status, {}, headers={"Location": "https://evil.example/"}),
                      response(200, {"id": "should never be requested"}))
    with pytest.raises(RedirectRejectedError):
        make_client(t, max_retries=5).get("me")
    assert len(t.requests) == 1 and len(t.script) == 1   # second response never consumed
    assert {r.host for r in t.requests} == {"graph.facebook.com"}
    assert not any("evil.example" in r.url for r in t.requests)


@pytest.mark.parametrize("final_url", [
    "https://evil.example/v27.0/me",
    "http://graph.facebook.com/v27.0/me",
    "https://graph.instagram.com/v27.0/me",
    "https://graph.facebook.com/v27.0/other",
])
def test_transport_reported_redirect_rejected(final_url):
    t = FakeTransport(RawResponse(200, {}, b'{"id": "1"}', final_url=final_url))
    with pytest.raises(RedirectRejectedError):
        make_client(t).get("me")


def test_same_final_url_is_accepted():
    t = FakeTransport(RawResponse(200, {}, b'{"id": "1"}', final_url="https://graph.facebook.com/v27.0/me?x=1"))
    assert make_client(t).get("me").data == {"id": "1"}


def test_classify_error_treats_3xx_as_redirect():
    assert classify_error(302, {}).category is ErrorCategory.REDIRECT_REJECTED


def test_transport_contract_documented():
    """DOCUMENTATION CHECK ONLY: this proves the contract is written down, not
    that any transport obeys it. No real transport exists in Phase 1; the
    future transport must be tested against these requirements."""
    doc = C.__doc__
    for phrase in ("NEVER follow redirects", "ssl.create_default_context()", "certificate AND hostname",
                   "exact host", "max_response_bytes", "transport_headers()"):
        assert phrase in doc


# =========================== L1: paging cursors ===========================
@pytest.mark.parametrize("cursor", [
    "QVFIUmFkY2xTd3pKV0s0bGRtV2N5M0JBZAm5qdWQ", "QVFIUmFk=", "QVFIUmFk==", "abc-DEF_123", "c1",
    "x" * C.MAX_CURSOR_LENGTH,
])
def test_valid_cursors(cursor):
    assert validate_cursor(cursor) == cursor


@pytest.mark.parametrize("cursor", [
    "", None, 123, "x" * (C.MAX_CURSOR_LENGTH + 1), "abc===", "=abc", "ab=c", "a b", "a\n",
    "x){id},access_token,owner{username", "abc)", "abc&access_token=1", "abc%3D", "../x", "a/b", "a+b",
])
def test_invalid_cursors(cursor):
    with pytest.raises(ValueError):
        validate_cursor(cursor)


def test_cursor_error_does_not_echo_value():
    with pytest.raises(ValueError) as exc:
        validate_cursor("evil){access_token}")
    assert "evil" not in str(exc.value)


def _page(after, next_=True, ids=(1,)):
    body = {"data": [{"id": i} for i in ids], "paging": {"cursors": {"after": after}}}
    if next_:
        body["paging"]["next"] = "https://graph.facebook.com/next"
    return body


def test_malicious_cursor_from_api_stops_pagination():
    t = FakeTransport(response(200, _page("x){id},access_token,owner{username")), response(200, _page("c2")))
    p = make_client(t).paginate("me/media")
    got = []
    with pytest.raises(InvalidResponseError):
        for item in p:
            got.append(item)
    assert got == [{"id": 1}] and len(t.requests) == 1 and p.stop_reason == "invalid_cursor"


@pytest.mark.parametrize("bad", ["a" * (C.MAX_CURSOR_LENGTH + 1), 12345, ["c"], "a b"])
def test_oversized_or_malformed_cursor_from_api_rejected(bad):
    t = FakeTransport(response(200, _page(bad)))
    with pytest.raises(InvalidResponseError):
        list(make_client(t).paginate("me/media"))


def test_padded_cursor_is_used():
    t = FakeTransport(response(200, _page("QVFIUmFk==")), response(200, {"data": [{"id": 2}]}))
    assert [i["id"] for i in make_client(t).paginate("me/media")] == [1, 2]
    assert "after=QVFIUmFk==" in t.requests[1].url


def test_nested_business_discovery_paging_with_padding():
    def bd(ids, after):
        media = {"data": [{"id": i} for i in ids]}
        if after:
            media["paging"] = {"cursors": {"after": after}}
        return {"business_discovery": {"media": media}}

    t = FakeTransport(response(200, bd([1], "QVFIUmFk==")), response(200, bd([2], None)))
    client = make_client(t)

    def factory(after):
        media = f"media{paging_call('after', after)}{{id}}" if after else "media{id}"
        return "17841400000000000", None, [f"business_discovery.username(nike){{{media}}}"]

    p = Paginator(client, request_factory=factory, container=lambda b: b["business_discovery"]["media"],
                  require_next=False)
    assert [i["id"] for i in p] == [1, 2]
    assert "media.after(QVFIUmFk==){id}" in t.requests[1].url


def test_pagination_cannot_switch_endpoint():
    t = FakeTransport(response(200, _page("c1")), response(200, _page("c2")))
    paths = iter(["me/media", "me/accounts"])
    p = Paginator(make_client(t), request_factory=lambda after: (next(paths), None, ["id"]))
    with pytest.raises(ValueError, match="same endpoint|endpoint of the first page"):
        list(p)
    assert len(t.requests) == 1


@pytest.mark.parametrize("expr", [
    "media.after(x){id},access_token,owner{username){id}}",   # cut-off injection: unbalanced
    "media.after(bad cursor){id}", "media.after(a===){id}", "id,access_token", "id,client_secret",
    "media{id", "media{id}}", "id)", "x" * (C.MAX_FIELD_EXPRESSION_LENGTH + 1), "id=1",
])
def test_unsafe_field_expressions_rejected(expr):
    with pytest.raises(ValueError):
        validate_field_expression(expr)


def test_after_param_validated():
    client = make_client(FakeTransport())
    with pytest.raises(ValueError):
        client.build_request("me/media", {"after": "x&access_token=1"})
    assert "after=QVF==" in client.build_request("me/media", {"after": "QVF=="}).url


def test_paging_call_direction():
    assert paging_call("before", "abc") == ".before(abc)"
    with pytest.raises(ValueError):
        paging_call("sideways", "abc")


# =========================== L2: tokens never in URLs ===========================
def test_token_placement_option_removed():
    with pytest.raises(TypeError):
        make_client(FakeTransport(), token_placement="query")


@pytest.mark.parametrize("name", ["access_token", "client_secret", "fb_exchange_token", "input_token",
                                  "appsecret_proof", "refresh_token", "code"])
def test_credential_query_params_rejected(name):
    with pytest.raises(ValueError):
        make_client(FakeTransport()).build_request("me", {name: "x"})


def test_token_never_in_url_logs_or_errors(caplog):
    t = FakeTransport(response(500, {}), response(400, {"error": {"code": 190, "message": "expired"}}))
    with caplog.at_level(logging.DEBUG, logger="webcrawler.instagram"):
        with pytest.raises(Exception) as exc:
            make_client(t, access_token=PLAIN_TOKEN, backoff_base=0).get("me", fields=["id"])
    for req in t.requests:
        assert PLAIN_TOKEN not in req.url and "access_token" not in req.url
    assert PLAIN_TOKEN not in caplog.text + str(exc.value) + repr(exc.value)


# =========================== L3: prepared request secrets ===========================
def test_prepared_request_serializations_hide_token():
    req = make_client(FakeTransport(), access_token=PLAIN_TOKEN).build_request("me")
    views = [repr(req), str(req), req.url, req.safe_url, str(dict(req.headers)),
             str(dataclasses.asdict(req)), str(vars(req)), str(copy.deepcopy(req)), repr(req.auth)]
    for text in views:
        assert PLAIN_TOKEN not in text
    with pytest.raises(TypeError):
        json.dumps(dataclasses.asdict(req))          # fails closed instead of leaking
    with pytest.raises(TypeError):
        pickle.dumps(req)


def test_prepared_request_headers_are_read_only_and_credential_free():
    req = make_client(FakeTransport()).build_request("me")
    assert "Authorization" not in req.headers
    with pytest.raises(TypeError):
        req.headers["Authorization"] = "x"
    with pytest.raises(dataclasses.FrozenInstanceError):
        req.url = "https://evil.example/"


def test_transport_headers_returns_fresh_copy():
    req = make_client(FakeTransport()).build_request("me")
    h = req.transport_headers()
    h["Authorization"] = "tampered"
    assert req.transport_headers()["Authorization"] == f"Bearer {FAKE_FB_TOKEN}"


def test_secret_copy_and_asdict_keep_value_hidden():
    s = Secret(PLAIN_TOKEN)
    assert copy.copy(s) is s and copy.deepcopy(s) is s
    cfg = load_config({"IG_AUTH_MODE": "facebook", "FB_LOGIN_ACCESS_TOKEN": PLAIN_TOKEN})
    assert PLAIN_TOKEN not in str(dataclasses.asdict(cfg))


# =========================== L4: shared logging redaction ===========================
def test_custom_redactor_client_token_redacted_in_logs():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        make_client(FakeTransport(), access_token=PLAIN_TOKEN, redactor=Redactor(share=False))
        assert default_redactor.redact(PLAIN_TOKEN) == REDACTED
        get_logger("webcrawler.instagram.l4").error("token %s", PLAIN_TOKEN)
    finally:
        root.removeHandler(handler)
    assert PLAIN_TOKEN not in stream.getvalue()


# =========================== L5: hard limits ===========================
@pytest.mark.parametrize("kwargs", [
    {"max_retries": C.MAX_RETRIES_LIMIT}, {"max_retries": 0}, {"max_backoff": C.MAX_BACKOFF_LIMIT},
    {"max_backoff": 0}, {"timeout": C.MAX_TIMEOUT}, {"backoff_base": 0},
    {"max_response_bytes": C.MAX_RESPONSE_BYTES_LIMIT}, {"max_response_bytes": 1},
    {"usage_threshold": 100}, {"usage_cooldown": 0},
])
def test_boundary_values_accepted(kwargs):
    make_client(FakeTransport(), **kwargs)


@pytest.mark.parametrize("kwargs", [
    {"max_retries": C.MAX_RETRIES_LIMIT + 1}, {"max_retries": 10 ** 6}, {"max_retries": -1},
    {"max_retries": 2.5}, {"max_retries": True}, {"max_backoff": C.MAX_BACKOFF_LIMIT + 1},
    {"max_backoff": float("inf")}, {"max_backoff": float("nan")}, {"max_backoff": -1},
    {"timeout": 0}, {"timeout": C.MAX_TIMEOUT + 1}, {"timeout": "30"},
    {"backoff_base": C.MAX_BACKOFF_LIMIT + 1}, {"usage_threshold": 0}, {"usage_threshold": 101},
    {"usage_cooldown": C.MAX_BACKOFF_LIMIT + 1}, {"max_response_bytes": 0},
    {"max_response_bytes": C.MAX_RESPONSE_BYTES_LIMIT + 1}, {"max_response_bytes": 1.5},
])
def test_excessive_or_invalid_values_rejected(kwargs):
    with pytest.raises(ValueError):
        make_client(FakeTransport(), **kwargs)


@pytest.mark.parametrize("kwargs", [
    {"max_pages": C.MAX_PAGES_LIMIT + 1}, {"max_pages": 0}, {"max_pages": 10 ** 9}, {"max_pages": 1.5},
    {"max_items": C.MAX_ITEMS_LIMIT + 1}, {"max_items": -1}, {"page_size": C.MAX_PAGE_SIZE + 1},
    {"page_size": 0}, {"page_size": True},
])
def test_pagination_limits_enforced(kwargs):
    with pytest.raises(ValueError):
        Paginator(make_client(FakeTransport()), path="me/media", **kwargs)


def test_pagination_limit_boundaries_accepted():
    Paginator(make_client(FakeTransport()), path="me/media", max_pages=C.MAX_PAGES_LIMIT,
              max_items=C.MAX_ITEMS_LIMIT, page_size=C.MAX_PAGE_SIZE)


def test_total_attempts_bounded_even_at_limit():
    t = FakeTransport(*[response(503, {}) for _ in range(C.MAX_RETRIES_LIMIT + 5)])
    with pytest.raises(Exception):
        make_client(t, max_retries=C.MAX_RETRIES_LIMIT, backoff_base=0).get("me")
    assert len(t.requests) == C.MAX_RETRIES_LIMIT + 1


def test_retry_waits_never_exceed_max_backoff():
    sleep = SleepRecorder()
    t = FakeTransport(*[response(503, {}) for _ in range(4)], response(200, {}))
    make_client(t, sleep=sleep, max_retries=4, backoff_base=500, max_backoff=30).get("me")
    assert sleep.calls == [30.0, 30.0, 30.0, 30.0]


@pytest.mark.parametrize("headers", [
    {"X-App-Usage": json.dumps({"call_count": 99, "total_cputime": 5, "total_time": 5})},
    {"X-Business-Use-Case-Usage": json.dumps({"1784": [{"type": "instagram", "call_count": 95,
                                                        "estimated_time_to_regain_access": 60}]})},
])
def test_rate_limit_cooldown_path_capped_at_max_backoff(headers):
    """Exercises the cooldown path for real: a SUCCESSFUL response reporting
    high usage triggers a pause; usage_cooldown=900 and a 60-minute regain hint
    must both be capped to max_backoff=30."""
    sleep = SleepRecorder()
    t = FakeTransport(response(200, {"id": "1"}, headers=headers))
    client = make_client(t, sleep=sleep, usage_threshold=90, usage_cooldown=900, max_backoff=30)
    assert client.get("me").data == {"id": "1"}
    assert sleep.calls == [30.0]
    assert client.last_usage.max_percent >= 90


def test_rate_limit_cooldown_below_cap_uses_configured_value():
    sleep = SleepRecorder()
    t = FakeTransport(response(200, {}, headers={"X-App-Usage": json.dumps({"call_count": 91})}))
    make_client(t, sleep=sleep, usage_threshold=90, usage_cooldown=12, max_backoff=30).get("me")
    assert sleep.calls == [12.0]


def test_documented_limits():
    assert (C.MAX_RETRIES_LIMIT, C.MAX_BACKOFF_LIMIT, C.MAX_PAGES_LIMIT) == (10, 900.0, 1000)


# =========================== L6: response size ===========================
# SCOPE NOTE: these tests prove the CLIENT rejects bodies over the limit, or
# flagged truncated, before decoding. They do NOT prove bounded streaming: a
# FakeTransport already holds the whole body in memory. Stopping the read at
# max_response_bytes + 1 bytes is a requirement on the future real transport
# (see the TRANSPORT CONTRACT in client.py) and must be tested there.
@pytest.mark.parametrize("extra, ok", [(-1, True), (0, True), (1, False)])
def test_response_size_below_at_above_limit(extra, ok):
    body = b'{"id": "' + b"a" * 50 + b'"}'
    limit = len(body) - extra
    t = FakeTransport(RawResponse(200, {}, body))
    client = make_client(t, max_response_bytes=limit)
    if ok:
        assert client.get("me").data["id"] == "a" * 50
    else:
        with pytest.raises(ResponseTooLargeError) as exc:
            client.get("me")
        assert not exc.value.retryable and len(t.requests) == 1


def test_truncated_flag_rejected_even_if_small():
    t = FakeTransport(RawResponse(200, {}, b'{"id": "1"}', truncated=True))
    with pytest.raises(ResponseTooLargeError):
        make_client(t).get("me")


def test_oversized_error_body_rejected_before_parsing():
    t = FakeTransport(RawResponse(500, {}, b"x" * 200))
    with pytest.raises(ResponseTooLargeError):
        make_client(t, max_response_bytes=100).get("me")
    assert len(t.requests) == 1  # not retried


def test_limit_is_passed_to_transport():
    t = FakeTransport(response(200, {}))
    make_client(t, max_response_bytes=12345).get("me")
    assert t.requests[0].max_response_bytes == 12345


# =========================== L7: app secret format ===========================
@pytest.mark.parametrize("secret", ["nike", "password", "0f0f0f0f", "0f" * 15 + "0",   # too short
                                    "0f" * 16 + "0",                                     # 33 chars
                                    "g" * 32, "0f0f0f0f-0f0f-0f0f-0f0f-0f0f0f0f", "0f" * 15 + "  "])
def test_invalid_app_secrets_rejected(secret):
    with pytest.raises(ConfigError) as exc:
        load_config({"IG_AUTH_MODE": "instagram", "IG_LOGIN_ACCESS_TOKEN": PLAIN_TOKEN,
                     "IG_LOGIN_APP_SECRET": secret})
    assert exc.value.invalid == ("IG_LOGIN_APP_SECRET",)
    assert secret.strip() not in str(exc.value) + repr(exc.value)


@pytest.mark.parametrize("secret", [FAKE_SECRET, FAKE_SECRET.upper()])
def test_valid_app_secret_format(secret):
    cfg = load_config({"IG_AUTH_MODE": "facebook", "FB_LOGIN_ACCESS_TOKEN": PLAIN_TOKEN,
                       "FB_LOGIN_APP_SECRET": secret})
    assert cfg.app_secret.reveal() == secret


def test_short_invalid_secret_does_not_corrupt_redaction():
    with pytest.raises(ConfigError):
        load_config({"IG_AUTH_MODE": "instagram", "IG_LOGIN_ACCESS_TOKEN": PLAIN_TOKEN,
                     "IG_LOGIN_APP_SECRET": "nike"})
    assert default_redactor.redact("Just do it - nike") == "Just do it - nike"


# =========================== I4: constant-time secret comparison ===========================
def test_secret_equality_uses_compare_digest(monkeypatch):
    import hmac
    calls = []
    real = hmac.compare_digest
    monkeypatch.setattr(hmac, "compare_digest", lambda a, b: calls.append(1) or real(a, b))
    assert Secret("abcdefgh") == Secret("abcdefgh") and Secret("abcdefgh") != Secret("abcdefgX")
    assert calls
