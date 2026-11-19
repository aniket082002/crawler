"""Secret redaction for everything the Instagram module logs, raises or writes.

Two layers:
  1. exact values registered with a Redactor (the configured access token and
     app secret are registered automatically by config.load_config and by
     the API client);
  2. patterns that catch secrets by shape or by name, even if they were never
     registered: ``access_token=...`` (also URL-encoded ``access_token%3D...``)
     parameters, ``Authorization: Bearer ...`` headers, JSON
     ``"access_token": "..."`` pairs, and Meta token prefixes (EAA... for
     Facebook tokens, IGAA.../IGQV... for Instagram tokens).

Shared protection: every secret added to ANY Redactor is also added to the
process-wide ``default_redactor`` (unless share=False), so log redaction,
which uses the default redactor, always knows every secret in use.

Logging (audit finding M1): Python applies a logger's filters only to records
created on that same logger, never to records propagated up from child
loggers. So protection is applied in two places:
  * at the SOURCE: package modules get loggers from ``get_logger()``, which
    attaches the redacting filter to that exact logger. A record is redacted
    when it is created, before it propagates to any handler (root included);
  * at the OUTPUT: ``protect_handlers()`` attaches the filter to the handlers
    that actually write log output, covering records from any logger.
Both are idempotent (no duplicate filters) and neither changes global logging
configuration on import.

Standard library only.
"""
from __future__ import annotations

import logging
import re
import threading
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

REDACTED = "[REDACTED]"
# Shorter values would redact ordinary words. Real access tokens are much
# longer (config requires >= 20 chars) and app secrets are 32 hex chars.
MIN_SECRET_LENGTH = 8

PACKAGE_LOGGER_NAME = "webcrawler.instagram"

# Query-string / form parameter names whose values are always secret.
SENSITIVE_PARAM_NAMES = frozenset({
    "access_token", "client_secret", "app_secret", "fb_exchange_token",
    "input_token", "appsecret_proof", "refresh_token", "code", "password",
})
# Mapping keys whose values are always secret (redact_obj). "code" is left out
# here: in JSON it is normally a Graph API error code, not an OAuth code.
SENSITIVE_KEYS = (SENSITIVE_PARAM_NAMES - {"code"}) | frozenset({
    "token", "authorization", "secret", "api_key", "x-api-key",
})

_NAMES = "access_token|client_secret|app_secret|fb_exchange_token|input_token|appsecret_proof|refresh_token"
_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    # access_token=VALUE (or URL-encoded access_token%3DVALUE) in URLs, bodies, log text
    # (the name may follow a word boundary or an encoded "?" / "&": %3F, %26)
    (re.compile(rf"(?i)(?:\b|(?<=%3F)|(?<=%26))({_NAMES})(=|%3D)((?:[^&\s\"'<>%]|%[0-9A-Fa-f]{{2}})+)"),
     rf"\1\2{REDACTED}"),
    # "access_token": "VALUE" in JSON text
    (re.compile(rf"(?i)(\"(?:{_NAMES}|token)\"\s*:\s*\")([^\"]*)(\")"), rf"\1{REDACTED}\3"),
    # Authorization: Bearer VALUE / OAuth VALUE
    (re.compile(r"(?i)\b(authorization\b[\"']?\s*[:=]\s*[\"']?)(bearer|oauth)\s+[^\s\"',;]+"),
     rf"\1\2 {REDACTED}"),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-~+/]{16,}=*"), rf"\1 {REDACTED}"),
    # Token shapes: Facebook (EAA...) and Instagram (IGAA..., IGQV...) access tokens
    (re.compile(r"\bEAA[A-Za-z0-9]{20,}"), REDACTED),
    (re.compile(r"\bIG(?:AA|QV)[A-Za-z0-9_\-]{20,}"), REDACTED),
)


class Redactor:
    """Replaces registered secret values and secret-shaped text with [REDACTED].

    share=True (default): every secret added here is also added to the
    process-wide default_redactor used by log redaction (finding L4)."""

    def __init__(self, secrets: Iterable[str] = (), *, share: bool = True):
        self._lock = threading.Lock()
        self._secrets: tuple[str, ...] = ()
        self._share = share
        for s in secrets:
            self.add_secret(s)

    def add_secret(self, value: Any) -> None:
        if hasattr(value, "reveal"):  # config.Secret
            value = value.reveal()
        if not isinstance(value, str):
            return
        value = value.strip()
        if len(value) < MIN_SECRET_LENGTH:
            return
        with self._lock:
            if value not in self._secrets:
                # longest first, so a secret containing another is replaced whole
                self._secrets = tuple(sorted(self._secrets + (value,), key=len, reverse=True))
        shared = globals().get("default_redactor")
        if self._share and shared is not None and shared is not self:
            shared.add_secret(value)

    def clear(self) -> None:
        with self._lock:
            self._secrets = ()

    @property
    def secret_count(self) -> int:
        return len(self._secrets)

    def redact(self, text: Any) -> str:
        if text is None:
            return ""
        text = str(text)
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, REDACTED)
        for pattern, replacement in _PATTERNS:
            text = pattern.sub(replacement, text)
        return text

    def redact_url(self, url: str) -> str:
        """Redact secret query parameters by name, drop userinfo and fragment,
        then apply redact()."""
        try:
            parts = urlsplit(str(url))
        except ValueError:
            return self.redact(url)
        query = parts.query
        if query:
            pairs = parse_qsl(query, keep_blank_values=True)
            safe = [(k, REDACTED if k.lower() in SENSITIVE_PARAM_NAMES else v) for k, v in pairs]
            query = urlencode(safe, safe="[](){},.:")
        netloc = parts.hostname or ""
        try:
            port = parts.port
        except ValueError:
            port = None
        if port:
            netloc += f":{port}"
        if parts.scheme or netloc:
            url = urlunsplit((parts.scheme, netloc, parts.path, query, ""))
        else:
            url = urlunsplit(("", "", parts.path, query, ""))
        return self.redact(url)

    def redact_obj(self, obj: Any, _depth: int = 0) -> Any:
        """Recursively redact a JSON-like object (new object; input untouched)."""
        if _depth > 50:
            return REDACTED
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                key = self.redact(k) if isinstance(k, str) else k
                if isinstance(k, str) and k.lower() in SENSITIVE_KEYS:
                    out[key] = REDACTED
                else:
                    out[key] = self.redact_obj(v, _depth + 1)
            return out
        if isinstance(obj, (list, tuple)):
            return [self.redact_obj(v, _depth + 1) for v in obj]
        if isinstance(obj, str):
            return self.redact(obj)
        return obj

    def contains_secret(self, text: Any) -> bool:
        """True if text still holds a registered secret or a secret-shaped value."""
        text = "" if text is None else str(text)
        return self.redact(text) != text


class RedactingFilter(logging.Filter):
    """Logging filter: formats the message, redacts it, and freezes the result
    (args cleared) so later handlers/formatters cannot reintroduce secrets."""

    def __init__(self, redactor: Redactor | None = None):
        super().__init__()
        self._redactor = redactor

    @property
    def redactor(self) -> Redactor:
        return self._redactor or default_redactor

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # bad format args: keep the raw template only
            message = str(record.msg)
        record.msg = self.redactor.redact(message)
        record.args = ()
        if record.exc_info:
            text = logging.Formatter().formatException(record.exc_info)
            record.exc_text = self.redactor.redact(text)
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = self.redactor.redact(record.exc_text)
        if record.stack_info:
            record.stack_info = self.redactor.redact(record.stack_info)
        self._redact_extra(record)
        return True

    def _redact_extra(self, record: logging.LogRecord) -> None:
        """Redact attributes added via ``extra=`` (finding R6), which a
        formatter such as "%(custom)s" would print. Standard LogRecord fields
        are left alone. Strings are redacted; dicts/lists keep their shape
        (redact_obj); numbers/bools/None are kept; any other object is kept
        unless its text form contains a secret, in which case it is replaced
        by the redacted text."""
        for key in list(vars(record)):
            if key in _STANDARD_RECORD_ATTRS:
                continue
            value = getattr(record, key)
            if isinstance(value, str):
                setattr(record, key, self.redactor.redact(value))
            elif value is None or isinstance(value, (bool, int, float)):
                continue
            elif isinstance(value, (dict, list, tuple)):
                setattr(record, key, self.redactor.redact_obj(value))
            else:
                try:
                    text = str(value)
                except Exception:
                    text = ""
                if self.redactor.contains_secret(text):
                    setattr(record, key, self.redactor.redact(text))


# Attributes every LogRecord has (plus ones formatters add); anything else on a
# record came from ``extra=`` or a custom record factory.
_STANDARD_RECORD_ATTRS = frozenset(
    vars(logging.LogRecord("x", logging.INFO, __file__, 1, "x", (), None))
) | {"message", "asctime", "taskName"}


# Process-wide redactor: every Redactor shares its secrets with it.
default_redactor = Redactor(share=False)

# One filter instance for package loggers (identity check prevents duplicates).
_PACKAGE_FILTER = RedactingFilter()


def register_secret(value: Any) -> None:
    default_redactor.add_secret(value)


def redact(text: Any) -> str:
    return default_redactor.redact(text)


def _has_redacting_filter(filterer: logging.Filterer) -> bool:
    return any(isinstance(f, RedactingFilter) for f in filterer.filters)


def get_logger(name: str = PACKAGE_LOGGER_NAME) -> logging.Logger:
    """Logger for Instagram package code, with redaction at the source.

    Only names inside the package namespace are allowed, so this never
    changes loggers that belong to the website crawler or other code."""
    if name != PACKAGE_LOGGER_NAME and not name.startswith(PACKAGE_LOGGER_NAME + "."):
        raise ValueError(f"Logger name must be within '{PACKAGE_LOGGER_NAME}'.")
    logger = logging.getLogger(name)
    if not _has_redacting_filter(logger):
        logger.addFilter(_PACKAGE_FILTER)
    return logger


def protect_handlers(*handlers: logging.Handler, redactor: Redactor | None = None) -> int:
    """Attach redaction to output handlers (covers records from every logger,
    including propagated child-logger records). Idempotent: a handler that
    already has a RedactingFilter is left as it is. Returns how many handlers
    were newly protected.

    Intended for a future Instagram entry point (launcher) to call on its own
    handlers; nothing in the package calls it on import."""
    added = 0
    for handler in handlers:
        if not isinstance(handler, logging.Handler):
            raise TypeError("protect_handlers() expects logging.Handler objects.")
        if not _has_redacting_filter(handler):
            handler.addFilter(RedactingFilter(redactor))
            added += 1
    return added
