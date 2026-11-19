"""Graph API error classification (from fake error bodies)."""
import pytest

from tests.instagram.conftest import FAKE_FB_TOKEN
from webcrawler.instagram.errors import (
    ApiError, AuthenticationError, ErrorCategory, InvalidRequestError, NotFoundError,
    PermissionDeniedError, RateLimitError, TransientError, classify_error,
)
from webcrawler.instagram.redaction import REDACTED, Redactor


def body(code=None, subcode=None, message="msg", **extra):
    err = {"message": message, "type": "OAuthException", "fbtrace_id": "TRACE123", **extra}
    if code is not None:
        err["code"] = code
    if subcode is not None:
        err["error_subcode"] = subcode
    return {"error": err}


@pytest.mark.parametrize("status, payload, category, cls", [
    (400, body(190, 463), ErrorCategory.AUTHENTICATION, AuthenticationError),
    (400, body(190, 460), ErrorCategory.AUTHENTICATION, AuthenticationError),
    (400, body(102), ErrorCategory.AUTHENTICATION, AuthenticationError),
    (403, body(10), ErrorCategory.PERMISSION, PermissionDeniedError),
    (400, body(200), ErrorCategory.PERMISSION, PermissionDeniedError),
    (400, body(299), ErrorCategory.PERMISSION, PermissionDeniedError),
    (400, body(3), ErrorCategory.PERMISSION, PermissionDeniedError),
    (400, body(4), ErrorCategory.RATE_LIMIT, RateLimitError),
    (400, body(17), ErrorCategory.RATE_LIMIT, RateLimitError),
    (400, body(32), ErrorCategory.RATE_LIMIT, RateLimitError),
    (400, body(613), ErrorCategory.RATE_LIMIT, RateLimitError),
    (400, body(80002), ErrorCategory.RATE_LIMIT, RateLimitError),
    (400, body(80001), ErrorCategory.RATE_LIMIT, RateLimitError),
    (500, body(1), ErrorCategory.TRANSIENT, TransientError),
    (503, body(2), ErrorCategory.TRANSIENT, TransientError),
    (400, body(100, 33), ErrorCategory.NOT_FOUND, NotFoundError),
    (400, body(100), ErrorCategory.INVALID_REQUEST, InvalidRequestError),
    (400, body(100, 2207013), ErrorCategory.INVALID_REQUEST, InvalidRequestError),
    (400, body(9999, is_transient=True), ErrorCategory.TRANSIENT, TransientError),
    (429, {}, ErrorCategory.RATE_LIMIT, RateLimitError),
    (502, None, ErrorCategory.TRANSIENT, TransientError),
    (401, {}, ErrorCategory.AUTHENTICATION, AuthenticationError),
    (403, {}, ErrorCategory.PERMISSION, PermissionDeniedError),
    (404, "not json", ErrorCategory.NOT_FOUND, NotFoundError),
    (418, {}, ErrorCategory.INVALID_REQUEST, InvalidRequestError),
    (200, body(), ErrorCategory.UNKNOWN, ApiError),
])
def test_classification(status, payload, category, cls):
    err = classify_error(status, payload)
    assert err.category is category
    assert type(err) is cls


def test_graph_code_beats_http_status():
    # Graph returns many errors as HTTP 400; the code decides the category.
    assert classify_error(500, body(190)).category is ErrorCategory.AUTHENTICATION
    assert classify_error(400, body(4)).category is ErrorCategory.RATE_LIMIT


@pytest.mark.parametrize("category, retryable", [
    (ErrorCategory.RATE_LIMIT, True), (ErrorCategory.TRANSIENT, True),
    (ErrorCategory.AUTHENTICATION, False), (ErrorCategory.PERMISSION, False),
    (ErrorCategory.NOT_FOUND, False), (ErrorCategory.INVALID_REQUEST, False),
    (ErrorCategory.UNKNOWN, False),
])
def test_retryable_flag(category, retryable):
    assert ApiError("x", category=category).retryable is retryable


def test_fields_extracted_and_safe_dict():
    err = classify_error(400, body(190, 463, message="Session has expired"))
    assert (err.code, err.subcode, err.http_status, err.fbtrace_id) == (190, 463, 400, "TRACE123")
    d = err.to_safe_dict()
    assert d == {"category": "authentication", "message": "Session has expired", "http_status": 400,
                 "code": 190, "subcode": 463, "type": "OAuthException", "fbtrace_id": "TRACE123",
                 "retryable": False}


def test_string_codes_and_garbage_codes():
    assert classify_error(400, body("190")).code == 190
    err = classify_error(400, {"error": {"code": True, "error_subcode": [1]}})
    assert err.code is None and err.subcode is None


def test_message_redacted_and_truncated():
    r = Redactor()
    msg = f"Invalid token access_token={FAKE_FB_TOKEN} " + "x" * 2000
    err = classify_error(400, body(190, message=msg), redactor=r)
    assert FAKE_FB_TOKEN not in err.message and REDACTED in err.message
    assert len(err.message) <= 500
    assert FAKE_FB_TOKEN not in str(err) + repr(err) + str(err.to_safe_dict())


def test_retry_after_header_and_regain_minutes():
    err = classify_error(429, {}, headers={"Retry-After": "12"})
    assert err.retry_after == 12.0
    err = classify_error(400, body(80002), regain_access_minutes=3)
    assert err.retry_after == 180.0
    err = classify_error(400, body(80002), headers={"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"})
    assert err.retry_after is None
    # regain minutes are only used for rate limits
    assert classify_error(400, body(190), regain_access_minutes=3).retry_after is None


def test_non_dict_error_member():
    err = classify_error(400, {"error": "just a string"})
    assert err.category is ErrorCategory.INVALID_REQUEST
    assert err.message == "HTTP 400"
