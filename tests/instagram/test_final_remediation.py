"""Regression tests for the independent review findings R1-R7 and the
protected-logger requirement. Fake credentials and fake transports only;
network access is blocked by conftest.py."""
import ast
import dataclasses
import http.client
import io
import json
import logging
import pathlib
import subprocess
import sys
import traceback

import pytest

from tests.instagram.conftest import FAKE_FB_TOKEN, FAKE_SECRET, FakeTransport, SleepRecorder, response
from webcrawler.instagram.client import (
    GraphApiClient, PreparedRequest, RawResponse, final_url_matches, parse_usage_headers,
)
from webcrawler.instagram.config import ConfigError, Secret, access_token_problem, load_config
from webcrawler.instagram.errors import (
    ErrorCategory, InvalidRequestError, InvalidResponseError, RedirectRejectedError, TransientError,
    TransportFailureError,
)
from webcrawler.instagram.redaction import (
    REDACTED, RedactingFilter, Redactor, default_redactor, get_logger, protect_handlers,
)

PROJECT = pathlib.Path(__file__).resolve().parents[2]
PKG = PROJECT / "webcrawler" / "instagram"
PLAIN_TOKEN = "plain-fake-token-without-prefix-0042"   # no shape pattern matches it
URL = "https://graph.facebook.com/v27.0/me"


def make_client(transport, **kw):
    kw.setdefault("sleep", SleepRecorder())
    kw.setdefault("redactor", Redactor(share=False))
    kw.setdefault("access_token", PLAIN_TOKEN)
    return GraphApiClient(base_url="https://graph.facebook.com", api_version="v27.0",
                          transport=transport, **kw)


def full_text(exc: BaseException) -> str:
    """Everything a crash would print: message, repr and the whole traceback chain."""
    return str(exc) + repr(exc) + "".join(traceback.format_exception(exc))


# ============================== R1: transport exceptions ==============================
class CustomTransportBug(Exception):
    pass


@pytest.mark.parametrize("exc", [
    ValueError(f"Invalid header value b'Bearer {PLAIN_TOKEN}'"),
    KeyError(f"Authorization: Bearer {PLAIN_TOKEN}"),
    RuntimeError(f"unexpected state token={PLAIN_TOKEN}"),
    TypeError(f"bad header {PLAIN_TOKEN}"),
    UnicodeEncodeError("latin-1", PLAIN_TOKEN, 0, 1, f"cannot encode {PLAIN_TOKEN}"),
    http.client.InvalidURL(f"invalid url with {PLAIN_TOKEN}"),
    CustomTransportBug(f"{PLAIN_TOKEN}"),
])
def test_unexpected_transport_exception_is_sanitized(exc):
    sleep = SleepRecorder()
    t = FakeTransport(exc, response(200, {"id": "never"}))
    with pytest.raises(TransportFailureError) as info:
        make_client(t, sleep=sleep, max_retries=5).get("me")
    err = info.value
    assert err.category is ErrorCategory.TRANSPORT_FAILURE and not err.retryable
    assert type(exc).__name__ in err.message          # useful type kept
    assert PLAIN_TOKEN not in full_text(err)          # message, repr and traceback are clean
    assert err.__cause__ is None and err.__context__ is None and err.__suppress_context__ is False
    assert len(t.requests) == 1 and sleep.calls == []  # not retried


@pytest.mark.parametrize("exc", [
    OSError(f"connection reset while sending {PLAIN_TOKEN}"),
    TimeoutError(f"timed out {PLAIN_TOKEN}"),
    http.client.RemoteDisconnected(f"closed {PLAIN_TOKEN}"),   # an OSError subclass
])
def test_network_exceptions_stay_retryable_and_sanitized(exc):
    sleep = SleepRecorder()
    t = FakeTransport(exc, exc, response(200, {"id": "1"}))
    assert make_client(t, sleep=sleep, max_retries=2, backoff_base=1).get("me").data == {"id": "1"}
    assert sleep.calls == [1.0, 2.0]
    t2 = FakeTransport(exc, exc)
    with pytest.raises(TransientError) as info:
        make_client(t2, max_retries=1, backoff_base=0).get("me")
    assert PLAIN_TOKEN not in full_text(info.value)
    assert info.value.__context__ is None and info.value.__cause__ is None


def test_sanitized_errors_do_not_reach_logs(caplog):
    t = FakeTransport(ValueError(f"Invalid header value b'Bearer {PLAIN_TOKEN}'"))
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(TransportFailureError):
            make_client(t).get("me")
    assert PLAIN_TOKEN not in caplog.text
    assert "transport_failure" in caplog.text


def test_keyboard_interrupt_is_not_swallowed():
    t = FakeTransport(KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        make_client(t).get("me")


def test_unexpected_failure_inside_pagination_is_sanitized():
    t = FakeTransport(response(200, {"data": [{"id": 1}], "paging": {"next": "x", "cursors": {"after": "c1"}}}),
                      ValueError(f"boom {PLAIN_TOKEN}"))
    got = []
    with pytest.raises(TransportFailureError) as info:
        for item in make_client(t).paginate("me/media"):
            got.append(item)
    assert got == [{"id": 1}] and PLAIN_TOKEN not in full_text(info.value)


# ============================== R2: direct token validation ==============================
@pytest.mark.parametrize("token", [
    "fake-token-0000000000\r\nX-Injected: 1", "fake-token-0000000000\nX: 1", "fake-token-0000000000\r",
    "fake token with spaces 0000", "fake-token-0000000000\t", "fake-token-0000000000\x00",
    "fake-token-0000000000\x7f", "fake-token-00000000ü0", "fake-token-0000000000 ",
    "short-token", "x" * 4097, Secret(""), Secret("fake-token-0000000000\r\nX: 1"),
])
def test_direct_constructor_rejects_invalid_tokens_before_transport(token):
    t = FakeTransport(response(200, {}))
    with pytest.raises(ValueError) as info:
        GraphApiClient(base_url="https://graph.facebook.com", api_version="v27.0", access_token=token,
                       transport=t, sleep=SleepRecorder())
    assert t.requests == []
    raw = token.reveal() if isinstance(token, Secret) else token
    if raw:
        assert raw not in str(info.value)


@pytest.mark.parametrize("token", [PLAIN_TOKEN, FAKE_FB_TOKEN, "x" * 20, "a|b-c_d.e~f=g+h/i:j" + "0" * 5,
                                   "x" * 4096, Secret(PLAIN_TOKEN)])
def test_direct_constructor_accepts_valid_tokens(token):
    make_client(FakeTransport(), access_token=token)


def test_config_and_client_share_the_same_token_rules():
    bad = "fake-token-00000000ü0"           # non-ASCII is now rejected by both
    assert access_token_problem(bad)
    with pytest.raises(ConfigError) as info:
        load_config({"IG_AUTH_MODE": "facebook", "FB_LOGIN_ACCESS_TOKEN": bad})
    assert info.value.invalid == ("FB_LOGIN_ACCESS_TOKEN",)
    with pytest.raises(ValueError):
        make_client(FakeTransport(), access_token=bad)
    assert access_token_problem(PLAIN_TOKEN) is None


def test_access_token_problem_never_echoes_value():
    for bad in ("fake-token-0000000000\r\nX", "short-tok"):
        assert "fake-token" not in access_token_problem(bad) and "short-tok" not in access_token_problem(bad)


# ============================== R3: deep JSON nesting ==============================
DEEP = 100_000


@pytest.mark.parametrize("body", [
    b"[" * DEEP + b"]" * DEEP,
    b'{"a":' + b"[" * DEEP + b"]" * DEEP + b"}",
    b'{"data":' + b'{"x":' * DEEP + b"1" + b"}" * DEEP + b"}",
    b"[" * DEEP,                                   # deep AND malformed (never closed)
])
def test_deeply_nested_success_body_is_invalid_response(body):
    t = FakeTransport(RawResponse(200, {}, body))
    with pytest.raises(InvalidResponseError) as info:
        make_client(t).get("me")
    assert info.value.category is ErrorCategory.INVALID_RESPONSE and len(t.requests) == 1


def test_deeply_nested_error_body_is_still_classified():
    t = FakeTransport(RawResponse(400, {}, b"[" * DEEP + b"]" * DEEP))
    with pytest.raises(InvalidRequestError):          # classified from the HTTP status
        make_client(t).get("me")


def test_deeply_nested_rate_limit_header_ignored():
    deep = "[" * DEEP + "]" * DEEP
    assert parse_usage_headers({"X-App-Usage": deep}).max_percent is None
    t = FakeTransport(RawResponse(200, {"X-Business-Use-Case-Usage": deep}, b'{"id": "1"}'))
    assert make_client(t).get("me").data == {"id": "1"}


def test_normal_nesting_still_decodes():
    body = json.dumps({"data": [[[[{"id": "1"}]]]]}).encode()
    assert make_client(FakeTransport(RawResponse(200, {}, body))).get("me").data["data"][0][0][0][0] == {"id": "1"}


# ============================== R4: PreparedRequest host validation ==============================
def _req(**over):
    base = dict(method="GET", url=URL, header_items=(("Accept", "application/json"),),
                auth=Secret(PLAIN_TOKEN), max_response_bytes=1000)
    base.update(over)
    return PreparedRequest(**base)


@pytest.mark.parametrize("url", [
    "https://evil.example/v27.0/me", "http://graph.facebook.com/v27.0/me",
    "https://graph.facebook.com:8443/v27.0/me", "https://graph.facebook.com:443/v27.0/me",
    "https://user:pw@graph.facebook.com/v27.0/me", "https://graph.facebook.com.evil.example/x",
    "https://GRAPH.FACEBOOK.COM/v27.0/me", "https://graph.facebook.com./v27.0/me",
    "https://graph.facebook.com/v27.0/me#frag", "//graph.facebook.com/v27.0/me", "", 123,
])
def test_direct_construction_rejects_unapproved_urls(url):
    with pytest.raises(ValueError):
        _req(url=url)


@pytest.mark.parametrize("url", ["https://graph.facebook.com/v27.0/me",
                                 "https://graph.instagram.com/v27.0/me?fields=id"])
def test_direct_construction_accepts_approved_urls(url):
    assert _req(url=url).host in ("graph.facebook.com", "graph.instagram.com")


@pytest.mark.parametrize("change", [
    {"url": "https://evil.example/steal"}, {"url": "http://graph.facebook.com/v27.0/me"},
    {"method": "POST"}, {"header_items": (("Authorization", "Bearer other"),)},
    {"header_items": (("X-Ok", "a\r\nAuthorization: Bearer x"),)}, {"header_items": [("Accept", "x")]},
    {"header_items": (("Cookie", "s=1"),)}, {"auth": "raw-string-token-0000000000"},
    {"auth": Secret("bad token\r\n00000000000")}, {"max_response_bytes": 0},
    {"max_response_bytes": 10 ** 12},
])
def test_dataclasses_replace_cannot_produce_unsafe_request(change):
    req = make_client(FakeTransport()).build_request("me")
    with pytest.raises(ValueError):
        dataclasses.replace(req, **change)


def test_safe_replace_still_works_and_keeps_host():
    req = make_client(FakeTransport()).build_request("me")
    r2 = dataclasses.replace(req, url="https://graph.facebook.com/v27.0/me/media")
    assert r2.host == "graph.facebook.com" and r2.auth == req.auth


def test_client_built_requests_pass_validation():
    for base in ("https://graph.facebook.com", "https://graph.instagram.com"):
        c = GraphApiClient(base_url=base, api_version="v27.0", access_token=PLAIN_TOKEN,
                           transport=FakeTransport(), sleep=SleepRecorder())
        assert c.build_request("me/media", {"limit": 5}, ["id"]).host == base[len("https://"):]


# ============================== R5: final URL validation ==============================
@pytest.mark.parametrize("final, ok", [
    ("https://graph.facebook.com/v27.0/me", True),
    ("https://graph.facebook.com:443/v27.0/me", True),          # explicit default port
    ("https://graph.facebook.com/v27.0/me?after=x", True),      # query not compared
    ("https://GRAPH.Facebook.COM/v27.0/me", True),              # DNS names are case-insensitive
    ("HTTPS://graph.facebook.com/v27.0/me", True),
    ("https://graph.facebook.com:8443/v27.0/me", False),        # non-default port
    ("https://graph.facebook.com:80/v27.0/me", False),
    ("https://graph.facebook.com:0/v27.0/me", False),
    ("https://graph.facebook.com:abc/v27.0/me", False),         # unparsable port
    ("https://graph.facebook.com./v27.0/me", False),            # trailing dot: other spelling
    ("https://GRAPH.FACEBOOK.COM./v27.0/me", False),
    ("http://graph.facebook.com/v27.0/me", False),
    ("https://user@graph.facebook.com/v27.0/me", False),
    ("https://evil.example/v27.0/me", False),
    ("https://graph.facebook.com/v27.0/other", False),
    ("", False), (None, False),
])
def test_final_url_matches(final, ok):
    assert final_url_matches(URL, final) is ok


@pytest.mark.parametrize("final", ["https://graph.facebook.com:8443/v27.0/me",
                                   "https://graph.facebook.com./v27.0/me"])
def test_client_rejects_final_url_on_other_port_or_spelling(final):
    t = FakeTransport(RawResponse(200, {}, b'{"id": "1"}', final_url=final))
    with pytest.raises(RedirectRejectedError):
        make_client(t).get("me")


@pytest.mark.parametrize("final", ["https://graph.facebook.com:443/v27.0/me",
                                   "https://Graph.Facebook.com/v27.0/me"])
def test_client_accepts_equivalent_final_url(final):
    t = FakeTransport(RawResponse(200, {}, b'{"id": "1"}', final_url=final))
    assert make_client(t).get("me").data == {"id": "1"}


# ============================== R6: logging extra fields ==============================
@pytest.fixture
def extra_capture():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(name)s|%(levelname)s|%(lineno)d|%(message)s|%(custom)s"))
    log = get_logger("webcrawler.instagram.r6test")
    log.handlers[:] = [handler]
    log.propagate = False
    log.setLevel(logging.DEBUG)
    yield log, stream, handler
    log.handlers[:] = []
    log.propagate = True


class Holder:
    def __init__(self, text):
        self.text = text

    def __str__(self):
        return self.text


@pytest.mark.parametrize("custom", [
    PLAIN_TOKEN, f"prefix {PLAIN_TOKEN} suffix", {"access_token": "anything", "note": PLAIN_TOKEN},
    [f"x {PLAIN_TOKEN}"], (PLAIN_TOKEN,), Holder(f"obj {PLAIN_TOKEN}"), f"Authorization: Bearer {FAKE_FB_TOKEN}",
])
def test_extra_fields_redacted_through_formatter(extra_capture, custom):
    log, stream, _ = extra_capture
    default_redactor.add_secret(PLAIN_TOKEN)
    log.error("hello", extra={"custom": custom})
    out = stream.getvalue()
    assert PLAIN_TOKEN not in out and FAKE_FB_TOKEN not in out and REDACTED in out
    assert out.startswith("webcrawler.instagram.r6test|ERROR|")   # standard fields intact


@pytest.mark.parametrize("custom, expected", [("harmless", "harmless"), (42, "42"), (None, "None"),
                                              (3.5, "3.5"), (True, "True")])
def test_extra_fields_without_secrets_unchanged(extra_capture, custom, expected):
    log, stream, _ = extra_capture
    log.info("x", extra={"custom": custom})
    assert stream.getvalue().strip().endswith(f"|x|{expected}")


def test_extra_object_without_secret_keeps_identity():
    obj = Holder("nothing secret here")
    rec = logging.LogRecord("x", logging.INFO, __file__, 1, "m", (), None)
    rec.custom = obj
    RedactingFilter(Redactor(share=False)).filter(rec)
    assert rec.custom is obj


def test_standard_record_fields_untouched():
    default_redactor.add_secret(PLAIN_TOKEN)
    rec = logging.LogRecord("webcrawler.instagram.x", logging.WARNING, "/p/file.py", 7, "m %s", ("a",), None)
    before = {k: getattr(rec, k) for k in ("name", "levelno", "levelname", "pathname", "lineno", "created",
                                           "process", "thread", "funcName")}
    rec.custom = PLAIN_TOKEN
    RedactingFilter().filter(rec)
    assert {k: getattr(rec, k) for k in before} == before
    assert rec.getMessage() == "m a" and rec.custom == REDACTED


def test_protected_output_handler_redacts_extra_from_plain_logger():
    default_redactor.add_secret(PLAIN_TOKEN)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s|%(custom)s"))
    protect_handlers(handler)
    plain = logging.getLogger("isolated_r6_plain")
    plain.handlers[:] = [handler]
    plain.propagate = False
    try:
        plain.error("m", extra={"custom": PLAIN_TOKEN})
    finally:
        plain.handlers[:] = []
        plain.propagate = True
    assert PLAIN_TOKEN not in stream.getvalue()


# ============================== R7: shared redactor via load_config ==============================
def test_load_config_with_unshared_redactor_registers_shared_secrets():
    private = Redactor(share=False)
    load_config({"IG_AUTH_MODE": "facebook", "FB_LOGIN_ACCESS_TOKEN": PLAIN_TOKEN,
                 "FB_LOGIN_APP_SECRET": FAKE_SECRET}, redactor=private)
    for r in (private, default_redactor):
        assert r.redact(PLAIN_TOKEN) == REDACTED and r.redact(FAKE_SECRET) == REDACTED


def test_load_config_secret_masked_in_package_logs_without_a_client(caplog):
    load_config({"IG_AUTH_MODE": "instagram", "IG_LOGIN_ACCESS_TOKEN": PLAIN_TOKEN},
                redactor=Redactor(share=False))
    with caplog.at_level(logging.INFO):
        get_logger("webcrawler.instagram.r7").info("token %s", PLAIN_TOKEN)
    assert PLAIN_TOKEN not in caplog.text


# ============================== protected logger usage ==============================
def _package_modules():
    return sorted(PKG.glob("*.py"))


@pytest.mark.parametrize("path", _package_modules(), ids=lambda p: p.name)
def test_package_modules_only_use_protected_loggers(path):
    """Package code must get loggers from redaction.get_logger(); it must not
    create loggers with logging.getLogger(), log through the root logger
    (logging.info(...) etc.), or print()."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    root_calls = {"debug", "info", "warning", "warn", "error", "exception", "critical", "log", "basicConfig"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name) and f.id == "print":
                pytest.fail(f"{path.name}:{node.lineno} uses print()")
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id == "logging":
                if f.attr in root_calls:
                    pytest.fail(f"{path.name}:{node.lineno} logs through the root logger (logging.{f.attr})")
                if f.attr == "getLogger" and path.name != "redaction.py":
                    pytest.fail(f"{path.name}:{node.lineno} creates an unprotected logger")
            if isinstance(f, ast.Name) and f.id == "getLogger":
                pytest.fail(f"{path.name}:{node.lineno} creates an unprotected logger")
        if isinstance(node, ast.ImportFrom) and node.module == "logging":
            if any(a.name == "getLogger" for a in node.names):
                pytest.fail(f"{path.name} imports getLogger directly")


def test_every_package_logger_has_redaction_after_import():
    """Fresh interpreter: import every package module, then every logger in the
    package namespace must carry a RedactingFilter."""
    mods = ", ".join(f"webcrawler.instagram.{p.stem}" for p in _package_modules() if p.stem != "__init__")
    code = (f"import sys, logging; sys.path.insert(0, {str(PROJECT)!r}); import {mods}; "
            "from webcrawler.instagram.redaction import RedactingFilter; "
            "lg=[(n, l) for n, l in logging.Logger.manager.loggerDict.items() "
            "if n.startswith('webcrawler.instagram') and isinstance(l, logging.Logger)]; "
            "bad=[n for n, l in lg if not any(isinstance(f, RedactingFilter) for f in l.filters)]; "
            "print(len(lg), bad)")
    out = subprocess.run([sys.executable, "-I", "-B", "-c", code], capture_output=True, text=True,
                         timeout=60, cwd=str(PROJECT))
    assert out.returncode == 0, out.stderr
    count, bad = out.stdout.strip().split(" ", 1)
    assert int(count) >= 1 and bad == "[]"
