"""Graph API client foundation (Phase 1).

PHASE 1 CONTAINS NO NETWORK CODE. The client builds GET requests, retries,
classifies errors, reads rate-limit headers and paginates, but it sends
nothing by itself: every request goes through an injected ``transport``
object. Tests use fake transports. A real HTTPS transport and the endpoint
calls belong to a later phase that needs separate approval.

TRANSPORT CONTRACT (mandatory for any future real transport)
------------------------------------------------------------
A transport implements ``send(request, timeout) -> RawResponse`` and MUST:
  1. HTTPS only, with certificate AND hostname verification using
     ``ssl.create_default_context()``. Never disable verification and never
     load a custom trust store unless a later phase approves it.
  2. Connect only to the exact host in ``request.url``. The client guarantees
     that host is graph.instagram.com or graph.facebook.com; the transport
     must re-check it (``request.host``) before connecting.
  3. NEVER follow redirects. Return any 3xx response as a RawResponse. (Python's
     default urllib redirect handler copies the Authorization header to the
     redirect target, so it must not be used.) If an underlying library could
     have changed the URL, report it in ``RawResponse.final_url``.
  4. Read the body in a bounded, streaming way: stop after
     ``request.max_response_bytes + 1`` bytes and set ``truncated=True`` (or
     raise TransportError). Never load an unbounded body into memory first.
  5. Enforce ``timeout`` for connect and read.
  6. Obtain credentials only via ``request.transport_headers()`` at send time,
     and never log, store or echo them.
  7. Raise TransportError (or TimeoutError/OSError) for network failures.

The client enforces everything it can on its side: approved hosts only,
Authorization header only (tokens are never put in URLs), redirects rejected
(never followed or retried), oversized bodies rejected, bounded retries,
backoff and pagination, validated paging cursors, redacted errors and logs.

References:
  https://developers.facebook.com/docs/graph-api/results          (pagination)
  https://developers.facebook.com/docs/graph-api/overview/rate-limiting
  https://developers.facebook.com/docs/graph-api/guides/error-handling
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable, Iterator, Mapping, Protocol, Sequence
from urllib.parse import urlencode, urlsplit

from webcrawler.instagram.config import (
    ALLOWED_GRAPH_HOSTS, API_VERSION_RE, InstagramConfig, Secret, access_token_problem,
)
from webcrawler.instagram.errors import (
    ApiError, InvalidResponseError, RedirectRejectedError, ResponseTooLargeError,
    TransientError, TransportError, TransportFailureError, classify_error,
)
from webcrawler.instagram.redaction import (
    SENSITIVE_KEYS, SENSITIVE_PARAM_NAMES, Redactor, default_redactor, get_logger,
)

logger = get_logger("webcrawler.instagram.client")

USER_AGENT = "WebCrawlerBot/1.0 (instagram-graph-client)"

# --- defaults and HARD LIMITS (finding L5) --------------------------------------
# Values above a hard limit are rejected with ValueError (not silently
# clamped), so a misconfiguration is visible instead of hidden.
DEFAULT_TIMEOUT = 30.0
MAX_TIMEOUT = 300.0                    # seconds per request
DEFAULT_MAX_RETRIES = 3
MAX_RETRIES_LIMIT = 10                 # retries per request (attempts = retries + 1)
DEFAULT_BACKOFF_BASE = 2.0
DEFAULT_MAX_BACKOFF = 60.0
MAX_BACKOFF_LIMIT = 900.0              # longest single wait (15 minutes)
DEFAULT_USAGE_THRESHOLD = 90.0         # percent of a rate-limit budget
DEFAULT_USAGE_COOLDOWN = 60.0          # pause once the threshold is reached
DEFAULT_MAX_PAGES = 50
MAX_PAGES_LIMIT = 1000                 # pages per pagination run
MAX_ITEMS_LIMIT = 100_000              # items per pagination run
MAX_PAGE_SIZE = 500                    # "limit" parameter per page
DEFAULT_MAX_RESPONSE_BYTES = 20 * 1024 * 1024   # 20 MiB
MAX_RESPONSE_BYTES_LIMIT = 50 * 1024 * 1024     # 50 MiB
MAX_FIELD_EXPRESSION_LENGTH = 4096

# --- validation patterns ----------------------------------------------------------
PATH_RE = re.compile(r"^[A-Za-z0-9_]+(?:/[A-Za-z0-9_]+)*$")
PARAM_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
FIELD_RE = re.compile(r"^[A-Za-z0-9_.(){},:]+$")
FIELD_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Paging cursors (finding L1): base64/base64url characters with up to two '='
# padding characters at the end, at most MAX_CURSOR_LENGTH long.
MAX_CURSOR_LENGTH = 1024
CURSOR_RE = re.compile(r"[A-Za-z0-9_\-]+={0,2}")
_PAGING_CALL_RE = re.compile(r"\.(after|before)\(([^()]*)\)")


def validate_cursor(cursor: Any) -> str:
    """Return the cursor if it is a well-formed paging cursor, else raise ValueError.
    The error message never echoes the cursor value."""
    if not isinstance(cursor, str) or not cursor:
        raise ValueError("Paging cursor must be a non-empty string.")
    if len(cursor) > MAX_CURSOR_LENGTH:
        raise ValueError(f"Paging cursor is longer than {MAX_CURSOR_LENGTH} characters.")
    if not CURSOR_RE.fullmatch(cursor):
        raise ValueError("Paging cursor contains characters that are not allowed.")
    return cursor


def paging_call(direction: str, cursor: str) -> str:
    """Build a nested paging call such as ``.after(<cursor>)`` for field
    expressions (e.g. Business Discovery media paging), validating the cursor."""
    if direction not in ("after", "before"):
        raise ValueError("direction must be 'after' or 'before'.")
    return f".{direction}({validate_cursor(cursor)})"


def _balanced(expr: str) -> bool:
    pairs = {")": "(", "}": "{"}
    stack: list[str] = []
    for ch in expr:
        if ch in "({":
            stack.append(ch)
        elif ch in ")}":
            if not stack or stack.pop() != pairs[ch]:
                return False
    return not stack


def validate_field_expression(expr: Any) -> str:
    """Validate one ``fields`` expression: allowed characters only, balanced
    brackets, valid cursors inside .after()/.before(), and no secret-looking
    field names."""
    if not isinstance(expr, str) or not expr or len(expr) > MAX_FIELD_EXPRESSION_LENGTH:
        raise ValueError("Invalid field expression.")

    def check_call(match):
        validate_cursor(match.group(2))
        return f".{match.group(1)}(cursor)"

    stripped = _PAGING_CALL_RE.sub(check_call, expr)
    if not FIELD_RE.fullmatch(stripped) or not _balanced(stripped):
        raise ValueError("Invalid field expression.")
    for name in FIELD_IDENTIFIER_RE.findall(stripped):
        if name.lower() in SENSITIVE_KEYS:
            raise ValueError("Field expression requests a credential-like field.")
    return expr


def final_url_matches(request_url: str, final_url: Any) -> bool:
    """True only if a transport-reported final URL is the request's own
    endpoint (finding R5): scheme https, exactly the same hostname (letter case
    is ignored, as in DNS, but a trailing dot or any other spelling is not),
    the default HTTPS port only (none, or an explicit 443), no userinfo, and
    the same path. The query string is not compared."""
    try:
        req = urlsplit(str(request_url))
        fin = urlsplit(str(final_url))
        fin_port = fin.port
    except ValueError:
        return False
    if fin.scheme.lower() != "https" or fin.username is not None or fin.password is not None:
        return False
    if fin_port not in (None, 443):
        return False
    if (fin.hostname or "") != (req.hostname or "") or not fin.hostname:
        return False
    return fin.path == req.path


# --- transport contract ------------------------------------------------------------
@dataclass(frozen=True)
class PreparedRequest:
    """A request ready for a transport.

    ``url`` and ``headers`` never contain credentials. The token is held only
    as a Secret in ``auth`` (hidden from repr; Secret prints [REDACTED]) and is
    turned into an Authorization header only by ``transport_headers()``, at
    send time. ``dataclasses.asdict()`` therefore shows Secret([REDACTED]).

    Every instance validates itself on creation (finding R4), including
    instances made by ``dataclasses.replace()``: GET only; an https URL whose
    host is exactly an approved Graph API host (no port, userinfo or
    fragment); credential-free, CR/LF-free headers; a valid token; a bounded
    response-size limit. So a request carrying a bearer token can never point
    at another host.
    """
    method: str
    url: str
    header_items: tuple = ()
    auth: Secret | None = field(default=None, repr=False, compare=False)
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES

    def __post_init__(self):
        if self.method != "GET":
            raise ValueError("Only GET requests are supported.")
        if not isinstance(self.url, str):
            raise ValueError("Request URL must be a string.")
        try:
            parts = urlsplit(self.url)
        except ValueError:
            raise ValueError("Request URL is not valid.") from None
        if parts.scheme != "https" or parts.netloc not in ALLOWED_GRAPH_HOSTS or parts.fragment:
            raise ValueError("Request URL must be https:// on an approved Graph API host "
                             "(no port, credentials or fragment).")
        if not isinstance(self.header_items, tuple):
            raise ValueError("header_items must be a tuple of (name, value) pairs.")
        for item in self.header_items:
            if (not isinstance(item, tuple) or len(item) != 2
                    or not all(isinstance(x, str) for x in item)):
                raise ValueError("header_items must be a tuple of (name, value) pairs.")
            name, value = item
            if name.strip().lower() in ("authorization", "proxy-authorization", "cookie"):
                raise ValueError("Credentials must not be passed in header_items.")
            if any(ord(ch) < 32 or ord(ch) == 127 for ch in name + value):
                raise ValueError("Header names and values must not contain control characters.")
        if self.auth is not None:
            if not isinstance(self.auth, Secret):
                raise ValueError("auth must be a Secret or None.")
            if access_token_problem(self.auth):
                raise ValueError("auth does not hold a valid access token.")
        if (isinstance(self.max_response_bytes, bool) or not isinstance(self.max_response_bytes, int)
                or not 1 <= self.max_response_bytes <= MAX_RESPONSE_BYTES_LIMIT):
            raise ValueError("max_response_bytes is out of range.")

    @property
    def headers(self) -> Mapping[str, str]:
        """Read-only, credential-free headers."""
        return MappingProxyType(dict(self.header_items))

    @property
    def host(self) -> str:
        return (urlsplit(self.url).hostname or "").lower()

    @property
    def safe_url(self) -> str:
        return self.url  # credentials are never placed in the URL

    def transport_headers(self) -> dict[str, str]:
        """Headers INCLUDING Authorization, for a transport's send() only.
        Never log, store or return this value."""
        headers = dict(self.header_items)
        if self.auth:
            headers["Authorization"] = f"Bearer {self.auth.reveal()}"
        return headers

    def __repr__(self) -> str:
        return (f"PreparedRequest(method={self.method!r}, url={self.url!r}, "
                f"auth={'set' if self.auth else 'none'})")

    __str__ = __repr__


@dataclass(frozen=True)
class RawResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes
    final_url: str | None = None   # set by a transport if the URL could have changed
    truncated: bool = False        # set when reading stopped at the size limit


class Transport(Protocol):
    """See the TRANSPORT CONTRACT in this module's docstring."""

    def send(self, request: PreparedRequest, timeout: float) -> RawResponse: ...


# --- rate-limit usage ----------------------------------------------------------------
@dataclass(frozen=True)
class RateLimitUsage:
    app_usage: dict | None = None             # X-App-Usage
    business_usage: dict | None = None        # X-Business-Use-Case-Usage
    max_percent: float | None = None
    regain_access_minutes: float | None = None

    @property
    def present(self) -> bool:
        return self.app_usage is not None or self.business_usage is not None


def _header(headers: Mapping[str, str] | None, name: str) -> str | None:
    if not headers:
        return None
    lname = name.lower()
    for key, value in headers.items():
        if str(key).lower() == lname:
            return value
    return None


def _json_header(headers, name) -> Any:
    value = _header(headers, name)
    if value is None:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError, RecursionError):
        return None


def _num(value) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def parse_usage_headers(headers: Mapping[str, str] | None) -> RateLimitUsage:
    """Read X-App-Usage and X-Business-Use-Case-Usage. Malformed headers are ignored."""
    percents: list[float] = []
    regain: list[float] = []

    app = _json_header(headers, "X-App-Usage")
    if not isinstance(app, dict):
        app = None
    else:
        percents += [p for p in (_num(app.get(k)) for k in ("call_count", "total_cputime", "total_time"))
                     if p is not None]

    buc = _json_header(headers, "X-Business-Use-Case-Usage")
    if not isinstance(buc, dict):
        buc = None
    else:
        for entries in buc.values():
            for entry in entries if isinstance(entries, list) else []:
                if not isinstance(entry, dict):
                    continue
                percents += [p for p in (_num(entry.get(k)) for k in
                                         ("call_count", "total_cputime", "total_time")) if p is not None]
                minutes = _num(entry.get("estimated_time_to_regain_access"))
                if minutes is not None and minutes > 0:
                    regain.append(minutes)

    return RateLimitUsage(app, buc, max(percents) if percents else None,
                          max(regain) if regain else None)


# --- client ----------------------------------------------------------------------------
@dataclass(frozen=True)
class ApiResponse:
    status: int
    data: dict
    usage: RateLimitUsage
    safe_url: str
    attempts: int


def _check_param_value(name: str, value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
            raise ValueError(f"Parameter '{name}' contains control characters.")
        return value
    raise ValueError(f"Parameter '{name}' must be a string, number or boolean.")


def _check_range(name: str, value, low, high, *, low_inclusive=True) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number.")
    ok_low = value >= low if low_inclusive else value > low
    if not ok_low or value > high or value != value:  # value != value: NaN
        bound = "[" if low_inclusive else "("
        raise ValueError(f"{name} must be in {bound}{low}, {high}].")
    return value


class GraphApiClient:
    def __init__(self, *, base_url: str, api_version: str, access_token, transport: Transport,
                 timeout: float = DEFAULT_TIMEOUT, max_retries: int = DEFAULT_MAX_RETRIES,
                 backoff_base: float = DEFAULT_BACKOFF_BASE, max_backoff: float = DEFAULT_MAX_BACKOFF,
                 usage_threshold: float = DEFAULT_USAGE_THRESHOLD,
                 usage_cooldown: float = DEFAULT_USAGE_COOLDOWN,
                 max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
                 sleep: Callable[[float], None] = time.sleep, redactor: Redactor | None = None):
        if transport is None or not callable(getattr(transport, "send", None)):
            raise ValueError("A transport with a send() method is required.")
        base = str(base_url).rstrip("/")
        host = base[len("https://"):] if base.startswith("https://") else None
        if host not in ALLOWED_GRAPH_HOSTS:
            raise ValueError("base_url must be https://graph.instagram.com or https://graph.facebook.com.")
        if not API_VERSION_RE.fullmatch(str(api_version)):
            raise ValueError("api_version must look like 'v27.0'.")

        _check_range("timeout", timeout, 0, MAX_TIMEOUT, low_inclusive=False)
        if isinstance(max_retries, bool) or not isinstance(max_retries, int):
            raise ValueError("max_retries must be an integer.")
        _check_range("max_retries", max_retries, 0, MAX_RETRIES_LIMIT)
        _check_range("max_backoff", max_backoff, 0, MAX_BACKOFF_LIMIT)
        # Every actual wait is additionally capped at max_backoff when it is used.
        _check_range("backoff_base", backoff_base, 0, MAX_BACKOFF_LIMIT)
        _check_range("usage_threshold", usage_threshold, 0, 100, low_inclusive=False)
        _check_range("usage_cooldown", usage_cooldown, 0, MAX_BACKOFF_LIMIT)
        if isinstance(max_response_bytes, bool) or not isinstance(max_response_bytes, int):
            raise ValueError("max_response_bytes must be an integer.")
        _check_range("max_response_bytes", max_response_bytes, 1, MAX_RESPONSE_BYTES_LIMIT)

        if isinstance(access_token, str):
            access_token = Secret(access_token) if access_token else None
        if access_token is not None and not isinstance(access_token, Secret):
            raise ValueError("access_token must be a string or Secret.")
        if access_token is not None:
            # Same validation as load_config() (finding R2). Register first so
            # the value is masked even if a caller logs this rejection.
            default_redactor.add_secret(access_token)
            problem = access_token_problem(access_token)
            if problem:
                raise ValueError(f"access_token is not valid: {problem}.")
        self._token: Secret | None = access_token
        self.redactor = redactor or default_redactor
        if self._token:
            # Registered with this redactor AND the shared logging redactor (L4).
            self.redactor.add_secret(self._token)
            default_redactor.add_secret(self._token)

        self.base_url = base
        self.host = host
        self.api_version = str(api_version)
        self.transport = transport
        self.timeout = float(timeout)
        self.max_retries = int(max_retries)
        self.backoff_base = float(backoff_base)
        self.max_backoff = float(max_backoff)
        self.usage_threshold = float(usage_threshold)
        self.usage_cooldown = float(usage_cooldown)
        self.max_response_bytes = int(max_response_bytes)
        self.sleep = sleep
        self.last_usage = RateLimitUsage()
        self.requests_sent = 0

    @classmethod
    def from_config(cls, config: InstagramConfig, transport: Transport, **kwargs) -> "GraphApiClient":
        return cls(base_url=config.graph_base_url, api_version=config.api_version,
                   access_token=config.access_token, transport=transport, **kwargs)

    def __repr__(self) -> str:
        return (f"GraphApiClient(base_url={self.base_url!r}, api_version={self.api_version!r}, "
                f"token={'set' if self._token else 'missing'})")

    # -- request construction --
    def build_request(self, path: str, params: Mapping[str, Any] | None = None,
                      fields: Sequence[str] | None = None) -> PreparedRequest:
        path = str(path or "").strip().strip("/")
        if not PATH_RE.fullmatch(path):
            raise ValueError("Invalid Graph API path: only node IDs and edge names are allowed.")

        query: list[tuple[str, str]] = []
        for name, value in (params or {}).items():
            if not PARAM_NAME_RE.fullmatch(str(name)):
                raise ValueError(f"Invalid parameter name: {name!r}")
            if name.lower() in SENSITIVE_PARAM_NAMES or name.lower() == "fields":
                raise ValueError(f"Parameter '{name}' is not allowed here (credentials are never "
                                 "sent in URLs; use the fields argument for fields).")
            if value is None:
                continue
            if name in ("after", "before"):
                validate_cursor(value)
            query.append((name, _check_param_value(name, value)))
        if fields:
            for f in fields:
                validate_field_expression(f)
            query.append(("fields", ",".join(fields)))

        url = f"{self.base_url}/{self.api_version}/{path}"
        if query:
            url += "?" + urlencode(query, safe="(){},.:=")
        headers = (("Accept", "application/json"), ("User-Agent", USER_AGENT))
        return PreparedRequest("GET", url, headers, self._token, self.max_response_bytes)

    # -- sending --
    def _retry_delay(self, error: ApiError, attempt: int) -> float | None:
        if error.retry_after is not None:
            return None if error.retry_after > self.max_backoff else max(0.0, error.retry_after)
        return min(self.max_backoff, self.backoff_base * (2 ** (attempt - 1)))

    def _check_raw(self, request: PreparedRequest, raw: RawResponse) -> ApiError | None:
        """Checks that must pass before a response body is even decoded."""
        if raw.final_url is not None:
            if not final_url_matches(request.url, raw.final_url):
                return RedirectRejectedError(
                    "The transport reported a different final URL; redirects are never followed.",
                    http_status=raw.status)
        if 300 <= raw.status < 400:
            return RedirectRejectedError(
                f"Redirect response (HTTP {raw.status}) rejected; redirects are never followed.",
                http_status=raw.status)
        body = raw.body or b""
        if raw.truncated or len(body) > request.max_response_bytes:
            return ResponseTooLargeError(
                f"Response body exceeded the {request.max_response_bytes}-byte limit.",
                http_status=raw.status)
        return None

    @staticmethod
    def _decode(body: bytes):
        """Decoded JSON, or None if the body is not valid JSON. Absurdly deep
        nesting makes the decoder raise RecursionError (finding R3); that is
        treated as an invalid response instead of crashing the run."""
        try:
            return json.loads((body or b"").decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            return None

    def _after_success(self, usage: RateLimitUsage) -> None:
        self.last_usage = usage
        if usage.max_percent is not None and usage.max_percent >= self.usage_threshold:
            pause = self.usage_cooldown
            if usage.regain_access_minutes:
                pause = max(pause, usage.regain_access_minutes * 60.0)
            pause = min(pause, self.max_backoff)
            logger.warning("Rate-limit usage at %.0f%%; pausing %.0fs.", usage.max_percent, pause)
            self.sleep(pause)

    def get(self, path: str, params: Mapping[str, Any] | None = None,
            fields: Sequence[str] | None = None) -> ApiResponse:
        request = self.build_request(path, params, fields)
        if request.host != self.host:  # defence in depth; build_request guarantees this
            raise ValueError("Request host is not the configured Graph API host.")
        attempt = 0
        while True:
            attempt += 1
            self.requests_sent += 1
            # Finding R1: an exception from the transport can quote request
            # headers (e.g. http.client's "Invalid header value b'Bearer ...'").
            # Its message, traceback and chain are therefore discarded; only the
            # exception TYPE name is kept. The replacement error is raised
            # outside the except block, so it carries no __context__/__cause__.
            failure: str | None = None
            failure_type = ""
            try:
                raw = self.transport.send(request, self.timeout)
            except (TransportError, TimeoutError, OSError) as exc:
                failure, failure_type = "network", type(exc).__name__
            except Exception as exc:  # noqa: BLE001 - any other transport error
                failure, failure_type = "unexpected", type(exc).__name__
            if failure == "network":
                error: ApiError = TransientError(
                    f"Network error ({failure_type}); details withheld because they may "
                    "contain request data.")
            elif failure == "unexpected":
                error = TransportFailureError(
                    f"The transport failed unexpectedly ({failure_type}); details withheld "
                    "because they may contain request data.")
            else:
                error = self._check_raw(request, raw)
                if error is None:
                    body = self._decode(raw.body)
                    usage = parse_usage_headers(raw.headers)
                    ok_status = 200 <= raw.status < 300
                    if ok_status and isinstance(body, dict) and "error" not in body:
                        self._after_success(usage)
                        return ApiResponse(raw.status, body, usage, request.safe_url, attempt)
                    if ok_status and not isinstance(body, dict):
                        error = InvalidResponseError("The API response was not a JSON object.",
                                                     http_status=raw.status)
                    else:
                        error = classify_error(raw.status, body, headers=raw.headers,
                                               regain_access_minutes=usage.regain_access_minutes,
                                               redactor=self.redactor)
                    self.last_usage = usage

            if not error.retryable or attempt > self.max_retries:
                logger.warning("Request failed (%s, attempt %d): %s %s", error.category.value,
                               attempt, request.safe_url, error.message)
                raise error
            delay = self._retry_delay(error, attempt)
            if delay is None:
                logger.warning("Rate limited for longer than %.0fs; stopping: %s",
                               self.max_backoff, request.safe_url)
                raise error
            logger.info("Retrying in %.1fs (%s, attempt %d): %s", delay, error.category.value,
                        attempt, request.safe_url)
            self.sleep(delay)

    def paginate(self, path: str, params: Mapping[str, Any] | None = None,
                 fields: Sequence[str] | None = None, **kwargs) -> "Paginator":
        return Paginator(self, path=path, params=params, fields=fields, **kwargs)


# --- pagination ------------------------------------------------------------------------
RequestFactory = Callable[[str | None], tuple[str, Mapping[str, Any] | None, Sequence[str] | None]]


class Paginator:
    """Iterates the items of a cursor-paginated edge.

    Default: GET path?after=<cursor>, items in body["data"], cursor in
    body["paging"]["cursors"]["after"]. Iteration stops when there is no
    next page, a cursor repeats, a page/item limit is reached, or (for edges
    without paging.next) a page comes back empty. ``stop_reason`` says which.

    Safety: the ``paging.next`` URL is never followed; every cursor is
    validated (validate_cursor) before it is used, and a malformed cursor
    raises InvalidResponseError; every page must use the same endpoint path
    as the first page.

    For nested pagination (e.g. Business Discovery puts the cursor inside the
    fields expression) pass ``request_factory(after) -> (path, params, fields)``
    (build the nested call with ``paging_call("after", after)``) and
    ``container(body) -> dict`` returning the object holding data/paging.
    """

    def __init__(self, client: GraphApiClient, *, path: str | None = None,
                 params: Mapping[str, Any] | None = None, fields: Sequence[str] | None = None,
                 request_factory: RequestFactory | None = None,
                 container: Callable[[dict], Any] | None = None, items_key: str = "data",
                 page_size: int | None = None, max_pages: int = DEFAULT_MAX_PAGES,
                 max_items: int | None = None, require_next: bool = True):
        if request_factory is None and not path:
            raise ValueError("Either path or request_factory is required.")
        if isinstance(max_pages, bool) or not isinstance(max_pages, int):
            raise ValueError("max_pages must be an integer.")
        _check_range("max_pages", max_pages, 1, MAX_PAGES_LIMIT)
        if max_items is not None:
            if isinstance(max_items, bool) or not isinstance(max_items, int):
                raise ValueError("max_items must be an integer.")
            _check_range("max_items", max_items, 0, MAX_ITEMS_LIMIT)
        if page_size is not None:
            if isinstance(page_size, bool) or not isinstance(page_size, int):
                raise ValueError("page_size must be an integer.")
            _check_range("page_size", page_size, 1, MAX_PAGE_SIZE)
        self.client = client
        self.path, self.params, self.fields = path, dict(params or {}), fields
        self.request_factory = request_factory
        self.container = container
        self.items_key = items_key
        self.page_size = page_size
        self.max_pages = max_pages
        self.max_items = max_items
        self.require_next = require_next
        self.pages_fetched = 0
        self.items_yielded = 0
        self.stop_reason: str | None = None
        self._endpoint: str | None = None

    def _request_for(self, after: str | None):
        if self.request_factory is not None:
            path, params, fields = self.request_factory(after)
        else:
            params = dict(self.params)
            if self.page_size is not None:
                params["limit"] = self.page_size
            if after:
                params["after"] = after
            path, fields = self.path, self.fields
        endpoint = str(path or "").strip().strip("/")
        if self._endpoint is None:
            self._endpoint = endpoint
        elif endpoint != self._endpoint:
            raise ValueError("Pagination must stay on the endpoint of the first page.")
        return path, params, fields

    def __iter__(self) -> Iterator[Any]:
        after: str | None = None
        seen: set[str] = set()
        while True:
            if self.max_items is not None and self.items_yielded >= self.max_items:
                self.stop_reason = "max_items"
                return
            if self.pages_fetched >= self.max_pages:
                self.stop_reason = "max_pages"
                return
            path, params, fields = self._request_for(after)
            response = self.client.get(path, params, fields)
            self.pages_fetched += 1

            page = self.container(response.data) if self.container else response.data
            if not isinstance(page, dict):
                self.stop_reason = "no_page_container"
                return
            items = page.get(self.items_key)
            items = items if isinstance(items, list) else []
            for item in items:
                if self.max_items is not None and self.items_yielded >= self.max_items:
                    self.stop_reason = "max_items"
                    return
                self.items_yielded += 1
                yield item

            paging = page.get("paging") if isinstance(page.get("paging"), dict) else {}
            cursors = paging.get("cursors") if isinstance(paging.get("cursors"), dict) else {}
            next_after = cursors.get("after")
            if self.require_next and not paging.get("next"):
                self.stop_reason = "end_of_results"
                return
            if next_after is None or next_after == "":
                self.stop_reason = "end_of_results"
                return
            try:
                validate_cursor(next_after)
            except ValueError as exc:
                self.stop_reason = "invalid_cursor"
                raise InvalidResponseError(f"Invalid paging cursor in API response: {exc}") from None
            if not self.require_next and not items:
                self.stop_reason = "empty_page"
                return
            if next_after in seen or next_after == after:
                self.stop_reason = "repeated_cursor"
                return
            seen.add(next_after)
            after = next_after

    def summary(self) -> dict:
        return {"pages_fetched": self.pages_fetched, "items": self.items_yielded,
                "stop_reason": self.stop_reason}
