"""Exceptions and Graph API error classification for the Instagram module.

Every message stored on these exceptions is redacted before it is stored
(see redaction.py), so printing or logging an exception can never reveal an
access token or app secret.

Classification follows Meta's documented error codes:
  https://developers.facebook.com/docs/graph-api/guides/error-handling
  https://developers.facebook.com/docs/graph-api/overview/rate-limiting
"""
from __future__ import annotations

import enum
from typing import Mapping

from webcrawler.instagram.redaction import Redactor, default_redactor

MAX_MESSAGE_CHARS = 500


class ErrorCategory(str, enum.Enum):
    AUTHENTICATION = "authentication"    # token expired/invalid: re-authenticate
    PERMISSION = "permission"            # permission/feature not granted
    RATE_LIMIT = "rate_limit"            # throttled: wait, then retry
    TRANSIENT = "transient"              # temporary server or network problem
    NOT_FOUND = "not_found"              # object missing or not visible to this token
    INVALID_REQUEST = "invalid_request"  # bad parameter/field: fix the request
    INVALID_RESPONSE = "invalid_response"  # response body was not usable JSON
    REDIRECT_REJECTED = "redirect_rejected"    # 3xx: redirects are never followed
    RESPONSE_TOO_LARGE = "response_too_large"  # body exceeded the configured limit
    TRANSPORT_FAILURE = "transport_failure"    # unexpected error inside the transport
    UNKNOWN = "unknown"


RETRYABLE_CATEGORIES = frozenset({ErrorCategory.RATE_LIMIT, ErrorCategory.TRANSIENT})

# Graph API error codes (error.code).
AUTH_CODES = frozenset({102, 190})          # 190: access token expired/invalid
PERMISSION_CODES = frozenset({3, 10}) | frozenset(range(200, 300))
# 4 app, 17 user, 32 Page, 613 custom, 80001 Pages BUC, 80002 Instagram BUC.
RATE_LIMIT_CODES = frozenset({4, 17, 32, 613, 80001, 80002})
TRANSIENT_CODES = frozenset({1, 2})         # unknown error / service unavailable
INVALID_PARAMETER_CODE = 100
NOT_FOUND_SUBCODES = frozenset({33})        # object does not exist or no permission


# --- exception hierarchy ----------------------------------------------------
class InstagramError(Exception):
    """Base class for every error raised by webcrawler.instagram."""


class ConfigError(InstagramError):
    """Configuration is missing or invalid. Lists variable NAMES only."""

    def __init__(self, message: str, missing: tuple[str, ...] = (), invalid: tuple[str, ...] = ()):
        super().__init__(message)
        self.missing = tuple(missing)
        self.invalid = tuple(invalid)


class TargetValidationError(InstagramError, ValueError):
    """The username / profile URL entered is not acceptable."""


class TransportError(InstagramError):
    """Raised by a transport for network-level failures (retryable)."""


class ApiError(InstagramError):
    category = ErrorCategory.UNKNOWN

    def __init__(self, message: str, *, category: ErrorCategory | None = None,
                 http_status: int | None = None, code: int | None = None,
                 subcode: int | None = None, error_type: str | None = None,
                 fbtrace_id: str | None = None, retry_after: float | None = None):
        super().__init__(message)
        if category is not None:
            self.category = category
        self.message = message
        self.http_status = http_status
        self.code = code
        self.subcode = subcode
        self.error_type = error_type
        self.fbtrace_id = fbtrace_id
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        return self.category in RETRYABLE_CATEGORIES

    def to_safe_dict(self) -> dict:
        """JSON-safe description for logs and reports (message already redacted)."""
        return {
            "category": self.category.value,
            "message": self.message,
            "http_status": self.http_status,
            "code": self.code,
            "subcode": self.subcode,
            "type": self.error_type,
            "fbtrace_id": self.fbtrace_id,
            "retryable": self.retryable,
        }

    def __repr__(self) -> str:
        return (f"{type(self).__name__}(category={self.category.value!r}, "
                f"code={self.code!r}, subcode={self.subcode!r}, "
                f"http_status={self.http_status!r}, message={self.message!r})")


class AuthenticationError(ApiError):
    category = ErrorCategory.AUTHENTICATION


class PermissionDeniedError(ApiError):
    category = ErrorCategory.PERMISSION


class RateLimitError(ApiError):
    category = ErrorCategory.RATE_LIMIT


class TransientError(ApiError):
    category = ErrorCategory.TRANSIENT


class NotFoundError(ApiError):
    category = ErrorCategory.NOT_FOUND


class InvalidRequestError(ApiError):
    category = ErrorCategory.INVALID_REQUEST


class InvalidResponseError(ApiError):
    category = ErrorCategory.INVALID_RESPONSE


class RedirectRejectedError(ApiError):
    """A redirect was returned (or followed by a transport). Never retried:
    following it could send the Authorization header to another host."""
    category = ErrorCategory.REDIRECT_REJECTED


class ResponseTooLargeError(ApiError):
    category = ErrorCategory.RESPONSE_TOO_LARGE


class TransportFailureError(ApiError):
    """The transport raised an unexpected (non-network) exception, e.g. a
    ValueError from an HTTP library. Only the exception TYPE is kept: the
    original message can contain request headers, so it is discarded. Not
    retried: an unexpected failure is not expected to go away on its own."""
    category = ErrorCategory.TRANSPORT_FAILURE


ERROR_CLASSES = {
    ErrorCategory.AUTHENTICATION: AuthenticationError,
    ErrorCategory.PERMISSION: PermissionDeniedError,
    ErrorCategory.RATE_LIMIT: RateLimitError,
    ErrorCategory.TRANSIENT: TransientError,
    ErrorCategory.NOT_FOUND: NotFoundError,
    ErrorCategory.INVALID_REQUEST: InvalidRequestError,
    ErrorCategory.INVALID_RESPONSE: InvalidResponseError,
    ErrorCategory.REDIRECT_REJECTED: RedirectRejectedError,
    ErrorCategory.RESPONSE_TOO_LARGE: ResponseTooLargeError,
    ErrorCategory.TRANSPORT_FAILURE: TransportFailureError,
    ErrorCategory.UNKNOWN: ApiError,
}


# --- classification ---------------------------------------------------------
def _as_int(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _category_for(http_status: int | None, code: int | None, subcode: int | None,
                  is_transient: bool) -> ErrorCategory:
    # Graph error codes are more specific than HTTP status codes, so check them first.
    if code is not None:
        if code in AUTH_CODES:
            return ErrorCategory.AUTHENTICATION
        if code in RATE_LIMIT_CODES:
            return ErrorCategory.RATE_LIMIT
        if code in PERMISSION_CODES:
            return ErrorCategory.PERMISSION
        if code in TRANSIENT_CODES:
            return ErrorCategory.TRANSIENT
        if code == INVALID_PARAMETER_CODE:
            if subcode in NOT_FOUND_SUBCODES:
                return ErrorCategory.NOT_FOUND
            return ErrorCategory.INVALID_REQUEST
    if is_transient:
        return ErrorCategory.TRANSIENT
    if http_status is not None and 300 <= http_status < 400:
        return ErrorCategory.REDIRECT_REJECTED
    if http_status is not None:
        if http_status == 429:
            return ErrorCategory.RATE_LIMIT
        if http_status >= 500:
            return ErrorCategory.TRANSIENT
        if http_status == 401:
            return ErrorCategory.AUTHENTICATION
        if http_status == 403:
            return ErrorCategory.PERMISSION
        if http_status == 404:
            return ErrorCategory.NOT_FOUND
        if 400 <= http_status < 500:
            return ErrorCategory.INVALID_REQUEST
    return ErrorCategory.UNKNOWN


def _retry_after_header(headers: Mapping[str, str] | None) -> float | None:
    if not headers:
        return None
    for key, value in headers.items():
        if key.lower() == "retry-after":
            try:
                seconds = float(str(value).strip())
            except ValueError:
                return None  # HTTP-date form: ignore, fall back to backoff
            return seconds if seconds >= 0 else None
    return None


def classify_error(http_status: int | None, body, *, headers: Mapping[str, str] | None = None,
                   regain_access_minutes: float | None = None,
                   redactor: Redactor | None = None) -> ApiError:
    """Turn an HTTP status + decoded JSON body into a typed, redacted ApiError."""
    redactor = redactor or default_redactor
    err = body.get("error") if isinstance(body, dict) else None
    if not isinstance(err, dict):
        err = {}
    code = _as_int(err.get("code"))
    subcode = _as_int(err.get("error_subcode"))
    is_transient = err.get("is_transient") is True
    category = _category_for(http_status, code, subcode, is_transient)

    raw_message = err.get("message") or (
        f"HTTP {http_status}" if http_status is not None else "Unknown API error")
    message = redactor.redact(str(raw_message))[:MAX_MESSAGE_CHARS]
    error_type = err.get("type")
    trace = err.get("fbtrace_id")

    retry_after = _retry_after_header(headers)
    if retry_after is None and category is ErrorCategory.RATE_LIMIT and regain_access_minutes:
        retry_after = float(regain_access_minutes) * 60.0

    cls = ERROR_CLASSES[category]
    return cls(
        message,
        http_status=http_status,
        code=code,
        subcode=subcode,
        error_type=redactor.redact(str(error_type))[:100] if error_type else None,
        fbtrace_id=redactor.redact(str(trace))[:100] if trace else None,
        retry_after=retry_after,
    )
