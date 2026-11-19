"""API client foundation: request building, retries, rate limits, safe errors.
All responses come from a FakeTransport; nothing is sent anywhere."""
import json
import logging

import pytest

from tests.instagram.conftest import (
    FAKE_FB_TOKEN, FAKE_IG_TOKEN, FakeTransport, SleepRecorder, response,
)
from webcrawler.instagram.client import (
    GraphApiClient, PreparedRequest, parse_usage_headers,
)
from webcrawler.instagram.config import InstagramConfig, AuthMode, Secret
from webcrawler.instagram.errors import (
    AuthenticationError, ErrorCategory, InvalidResponseError, NotFoundError,
    PermissionDeniedError, RateLimitError, TransientError, TransportError,
)
from webcrawler.instagram.redaction import Redactor


def make_client(transport, token=FAKE_FB_TOKEN, base="https://graph.facebook.com", **kw):
    kw.setdefault("sleep", SleepRecorder())
    kw.setdefault("redactor", Redactor())
    return GraphApiClient(base_url=base, api_version="v27.0", access_token=token,
                          transport=transport, **kw)


# --- construction ---------------------------------------------------------------------
@pytest.mark.parametrize("base", [
    "http://graph.facebook.com", "https://graph.facebook.com.evil.example",
    "https://evil.example", "https://graph.instagram.com:8443", "graph.facebook.com", "",
])
def test_only_official_graph_hosts_allowed(base):
    with pytest.raises(ValueError):
        make_client(FakeTransport(), base=base)


@pytest.mark.parametrize("kwargs", [
    {"transport": None}, {"transport": object()}, {"timeout": 0}, {"max_retries": -1},
    {"access_token": 12345},
])
def test_invalid_construction(kwargs):
    base = {"transport": FakeTransport()}
    base.update(kwargs)
    t = base.pop("transport")
    token = base.pop("access_token", FAKE_FB_TOKEN)
    with pytest.raises(ValueError):
        make_client(t, token=token, **base)


def test_invalid_api_version():
    with pytest.raises(ValueError):
        GraphApiClient(base_url="https://graph.facebook.com", api_version="27",
                       access_token=FAKE_FB_TOKEN, transport=FakeTransport())


def test_from_config_uses_mode_host():
    cfg = InstagramConfig(AuthMode.INSTAGRAM_LOGIN, "v27.0", Secret(FAKE_IG_TOKEN))
    client = GraphApiClient.from_config(cfg, FakeTransport(), sleep=SleepRecorder())
    assert client.base_url == "https://graph.instagram.com"
    cfg = InstagramConfig(AuthMode.FACEBOOK_LOGIN, "v26.0", Secret(FAKE_FB_TOKEN))
    client = GraphApiClient.from_config(cfg, FakeTransport(), sleep=SleepRecorder())
    assert client.base_url == "https://graph.facebook.com" and client.api_version == "v26.0"


# --- request construction ---------------------------------------------------------------
def test_build_get_request_with_header_token():
    client = make_client(FakeTransport())
    req = client.build_request("me", {"limit": 25, "include": True}, ["id", "username"])
    assert req.method == "GET"
    assert req.url == "https://graph.facebook.com/v27.0/me?limit=25&include=true&fields=id,username"
    # the token is only produced for the transport, at send time
    assert req.transport_headers()["Authorization"] == f"Bearer {FAKE_FB_TOKEN}"
    assert "Authorization" not in req.headers
    assert FAKE_FB_TOKEN not in req.url
    assert FAKE_FB_TOKEN not in repr(req) + str(req) + req.safe_url


def test_nested_field_expression_allowed():
    client = make_client(FakeTransport())
    expr = "business_discovery.username(nike){followers_count,media{caption}}"
    req = client.build_request("17841400000000000", fields=[expr])
    assert "fields=business_discovery.username(nike){followers_count,media{caption}}" in req.url


def test_no_token_means_no_authorization_header():
    req = make_client(FakeTransport(), token=None).build_request("me")
    assert "Authorization" not in req.transport_headers()
    assert req.auth is None


@pytest.mark.parametrize("path", [
    "", "../me", "me/../../x", "https://evil.example/me", "me?access_token=x", "me#x", "me edges", "/",
    "me/%2e%2e",
])
def test_invalid_paths_rejected(path):
    with pytest.raises(ValueError):
        make_client(FakeTransport()).build_request(path)


def test_leading_slash_is_tolerated():
    assert make_client(FakeTransport()).build_request("/me/media").url.endswith("/v27.0/me/media")


@pytest.mark.parametrize("params", [
    {"access_token": "x"}, {"client_secret": "x"}, {"ACCESS_TOKEN": "x"}, {"fields": "id"},
    {"bad-name": 1}, {"x": object()}, {"x": "line\nbreak"}, {"appsecret_proof": "x"},
])
def test_unsafe_params_rejected(params):
    with pytest.raises(ValueError):
        make_client(FakeTransport()).build_request("me", params)


@pytest.mark.parametrize("fields", [["id&access_token=x"], ["id username"], [123], ["id\n"]])
def test_invalid_fields_rejected(fields):
    with pytest.raises(ValueError):
        make_client(FakeTransport()).build_request("me", fields=fields)


def test_none_params_are_dropped():
    assert make_client(FakeTransport()).build_request("me", {"after": None}).url.endswith("/me")


# --- success, timeout --------------------------------------------------------------------
def test_successful_get_and_timeout_passed():
    t = FakeTransport(response(200, {"id": "1", "username": "acct"}))
    client = make_client(t, timeout=12.5)
    r = client.get("me", fields=["id", "username"])
    assert r.data == {"id": "1", "username": "acct"} and r.status == 200 and r.attempts == 1
    assert t.timeouts == [12.5]
    assert FAKE_FB_TOKEN not in r.safe_url


# --- retries ---------------------------------------------------------------------------------
def test_retry_on_transient_then_success():
    sleep = SleepRecorder()
    t = FakeTransport(response(500, {"error": {"code": 2, "message": "Service unavailable"}}),
                      TransportError("connection reset"),
                      TimeoutError("timed out"),
                      response(200, {"id": "1"}))
    r = make_client(t, sleep=sleep, max_retries=3, backoff_base=1.0).get("me")
    assert r.data == {"id": "1"} and r.attempts == 4
    assert sleep.calls == [1.0, 2.0, 4.0]       # exponential backoff


def test_retries_exhausted_raises_last_error():
    sleep = SleepRecorder()
    t = FakeTransport(*[response(503, {}) for _ in range(3)])
    with pytest.raises(TransientError):
        make_client(t, sleep=sleep, max_retries=2, backoff_base=1.0).get("me")
    assert len(t.requests) == 3 and sleep.calls == [1.0, 2.0]


def test_backoff_capped():
    sleep = SleepRecorder()
    t = FakeTransport(*[response(500, {}) for _ in range(5)], response(200, {}))
    make_client(t, sleep=sleep, max_retries=5, backoff_base=10.0, max_backoff=25.0).get("me")
    assert sleep.calls == [10.0, 20.0, 25.0, 25.0, 25.0]


@pytest.mark.parametrize("payload, exc", [
    ({"error": {"code": 190, "error_subcode": 463, "message": "expired"}}, AuthenticationError),
    ({"error": {"code": 10, "message": "no permission"}}, PermissionDeniedError),
    ({"error": {"code": 100, "error_subcode": 33, "message": "missing"}}, NotFoundError),
])
def test_non_retryable_errors_fail_immediately(payload, exc):
    sleep = SleepRecorder()
    t = FakeTransport(response(400, payload))
    with pytest.raises(exc):
        make_client(t, sleep=sleep).get("me")
    assert len(t.requests) == 1 and sleep.calls == []


def test_token_expiry_is_authentication_error():
    t = FakeTransport(response(400, {"error": {"code": 190, "error_subcode": 463,
                                               "message": "Error validating access token: Session has expired"}}))
    with pytest.raises(AuthenticationError) as exc:
        make_client(t).get("me")
    assert exc.value.category is ErrorCategory.AUTHENTICATION and not exc.value.retryable


def test_os_error_from_transport_is_transient():
    t = FakeTransport(OSError("network unreachable"), response(200, {}))
    assert make_client(t, backoff_base=0).get("me").attempts == 2


# --- rate limits --------------------------------------------------------------------------------
def test_rate_limit_retried_after_header_delay():
    sleep = SleepRecorder()
    t = FakeTransport(response(429, {}, headers={"Retry-After": "7"}), response(200, {"ok": True}))
    make_client(t, sleep=sleep).get("me")
    assert sleep.calls == [7.0]


def test_rate_limit_with_long_regain_time_stops_instead_of_waiting():
    sleep = SleepRecorder()
    buc = json.dumps({"1784": [{"type": "instagram", "call_count": 100, "total_cputime": 20,
                                "total_time": 30, "estimated_time_to_regain_access": 30}]})
    t = FakeTransport(response(400, {"error": {"code": 80002, "message": "too many calls"}},
                               headers={"X-Business-Use-Case-Usage": buc}))
    with pytest.raises(RateLimitError) as exc:
        make_client(t, sleep=sleep, max_backoff=60).get("me")
    assert exc.value.retry_after == 1800.0
    assert sleep.calls == [] and len(t.requests) == 1


def test_rate_limit_code_without_hint_uses_backoff():
    sleep = SleepRecorder()
    t = FakeTransport(response(400, {"error": {"code": 4, "message": "app limit"}}), response(200, {}))
    make_client(t, sleep=sleep, backoff_base=3.0).get("me")
    assert sleep.calls == [3.0]


def test_high_usage_triggers_cooldown_after_success():
    sleep = SleepRecorder()
    app = json.dumps({"call_count": 95, "total_cputime": 10, "total_time": 12})
    t = FakeTransport(response(200, {"id": "1"}, headers={"x-app-usage": app}))
    client = make_client(t, sleep=sleep, usage_threshold=90, usage_cooldown=30, max_backoff=60)
    client.get("me")
    assert sleep.calls == [30]
    assert client.last_usage.max_percent == 95


def test_low_usage_no_cooldown():
    sleep = SleepRecorder()
    app = json.dumps({"call_count": 10, "total_cputime": 5, "total_time": 5})
    make_client(FakeTransport(response(200, {}, headers={"X-App-Usage": app})), sleep=sleep).get("me")
    assert sleep.calls == []


def test_parse_usage_headers():
    buc = {"111": [{"type": "instagram", "call_count": 40, "total_cputime": 85, "total_time": 12,
                    "estimated_time_to_regain_access": 0}],
           "222": [{"type": "pages", "call_count": 5, "estimated_time_to_regain_access": 4}]}
    u = parse_usage_headers({"X-Business-Use-Case-Usage": json.dumps(buc),
                             "X-App-Usage": json.dumps({"call_count": 50})})
    assert u.present and u.max_percent == 85 and u.regain_access_minutes == 4


@pytest.mark.parametrize("headers", [
    None, {}, {"X-App-Usage": "not json"}, {"X-App-Usage": "[1,2]"},
    {"X-Business-Use-Case-Usage": json.dumps({"1": "bad"})},
    {"X-Business-Use-Case-Usage": json.dumps({"1": [None, {"call_count": "high"}]})},
])
def test_malformed_usage_headers_ignored(headers):
    u = parse_usage_headers(headers)
    assert u.max_percent is None and u.regain_access_minutes is None


# --- bad responses -----------------------------------------------------------------------------
@pytest.mark.parametrize("raw", [b"not json", b"[1,2,3]", b"\xff\xfe", b""])
def test_invalid_json_success_body(raw):
    t = FakeTransport(response(200, raw=raw))
    with pytest.raises(InvalidResponseError):
        make_client(t).get("me")
    assert len(t.requests) == 1


def test_error_object_inside_200_is_an_error():
    t = FakeTransport(response(200, {"error": {"code": 190, "message": "expired"}}))
    with pytest.raises(AuthenticationError):
        make_client(t).get("me")


# --- safe error reporting --------------------------------------------------------------------------
def test_errors_and_logs_never_contain_the_token(caplog):
    leaky = f"Invalid OAuth access token {FAKE_FB_TOKEN} (access_token={FAKE_FB_TOKEN})"
    t = FakeTransport(TransportError(f"failed to reach host with token {FAKE_FB_TOKEN}"),
                      response(400, {"error": {"code": 190, "message": leaky, "fbtrace_id": "T"}}))
    with caplog.at_level(logging.DEBUG, logger="webcrawler.instagram.client"):
        with pytest.raises(AuthenticationError) as exc:
            make_client(t, backoff_base=0).get("me")
    err = exc.value
    for text in (str(err), repr(err), json.dumps(err.to_safe_dict()), caplog.text):
        assert FAKE_FB_TOKEN not in text
    assert "[REDACTED]" in err.message


def test_unregistered_token_shape_still_redacted_in_errors():
    other = "EAA" + "Z" * 40  # a token the client was never given
    t = FakeTransport(response(400, {"error": {"code": 190, "message": f"bad token {other}"}}))
    with pytest.raises(AuthenticationError) as exc:
        make_client(t).get("me")
    assert other not in str(exc.value)


def test_client_repr_hides_token():
    client = make_client(FakeTransport(), token=Secret(FAKE_IG_TOKEN), base="https://graph.instagram.com")
    assert FAKE_IG_TOKEN not in repr(client)
    assert "token=set" in repr(client)


def test_prepared_request_is_what_transport_receives():
    t = FakeTransport(response(200, {}))
    make_client(t).get("me", fields=["id"])
    (req,) = t.requests
    assert isinstance(req, PreparedRequest)
    assert req.url.startswith("https://graph.facebook.com/v27.0/me")


@pytest.mark.parametrize("version", ["v27.0\n", "v27.0 "])
def test_api_version_with_trailing_characters_rejected(version):
    with pytest.raises(ValueError):
        GraphApiClient(base_url="https://graph.facebook.com", api_version=version,
                       access_token=FAKE_FB_TOKEN, transport=FakeTransport())


@pytest.mark.parametrize("path", ["me\n", "me/media\n"])
def test_path_with_trailing_newline_rejected(path):
    # strip() would remove it; the client must still never accept it unstripped mid-path
    req = make_client(FakeTransport()).build_request(path)
    assert "\n" not in req.url
