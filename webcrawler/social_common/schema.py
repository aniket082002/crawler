"""Shared record structure for the social data modules.

Every extracted value is a FieldRecord:

    value         the extracted value (JSON-compatible), or None
    status        one of the FieldStatus labels below
    source        where the value came from (a page URL, a file, a field name)
    extracted_at  when it was extracted or observed (UTC, ISO 8601), or ""
    reason        why the value is missing; required unless status is Available

Rules enforced here, so no module can break them by accident:
  * a field that is not Available never carries a value (nothing is made up),
    and always says why;
  * an Available field always has a value (an empty string is a real value;
    None is not);
  * values are plain JSON data, timestamps are real ISO 8601 date-times.

A Record groups the fields of one thing (a profile, a post, a discovered
link) with its type, the module that produced it, the platform and a
deduplication key. Module-specific data that is not a field (for example the
list of pages a link was found on) goes into ``extra``.

Standard library only; no I/O.
"""
from __future__ import annotations

import datetime as _dt
import enum
import math
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

SCHEMA_VERSION = 1
MAX_VALUE_DEPTH = 20

NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_TIMESTAMP_SHAPE_RE = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")
_UTC = _dt.timezone.utc


class FieldStatus(str, enum.Enum):
    """The label is exactly what is written to the exports."""
    AVAILABLE = "Available"
    NOT_AVAILABLE = "Not available"        # the source does not contain it
    BLOCKED = "Blocked"                    # access was refused or not permitted
    LOGIN_REQUIRED = "Login required"      # only visible after signing in
    RESTRICTED = "Restricted"              # limited by the platform (rate limit, age gate, ...)
    NOT_SUPPORTED = "Not supported"        # this module cannot obtain it by design
    PARSING_FAILED = "Parsing failed"      # the data was there but could not be read


# --- JSON-compatible values ---------------------------------------------------
def is_json_value(value: Any, _depth: int = 0) -> bool:
    """True for None, bool, int, finite float, str, and lists/dicts of those
    (string keys only), nested at most MAX_VALUE_DEPTH levels."""
    if _depth > MAX_VALUE_DEPTH:
        return False
    if value is None or isinstance(value, (bool, str, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, (list, tuple)):
        return all(is_json_value(v, _depth + 1) for v in value)
    if isinstance(value, Mapping):
        return all(isinstance(k, str) and is_json_value(v, _depth + 1) for k, v in value.items())
    return False


def to_plain(value: Any) -> Any:
    """Deep copy of a JSON-compatible value using only list/dict/scalars."""
    if isinstance(value, (list, tuple)):
        return [to_plain(v) for v in value]
    if isinstance(value, Mapping):
        return {k: to_plain(v) for k, v in value.items()}
    return value


# --- timestamps ---------------------------------------------------------------
def parse_timestamp(text: Any) -> _dt.datetime | None:
    """Parse an ISO 8601 date-time (e.g. the crawler's
    ``2026-11-13T04:35:25+00:00``, or a ``Z`` suffix) into an aware UTC
    datetime. A value without a UTC offset is taken as UTC, which is what the
    crawler writes. Returns None for anything that is not a date-time."""
    if not isinstance(text, str):
        return None
    s = text.strip()
    if not s or len(s) > 40 or not _TIMESTAMP_SHAPE_RE.match(s):
        return None
    if s[-1] in "Zz":
        s = s[:-1] + "+00:00"
    try:
        parsed = _dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_UTC)
    try:
        return parsed.astimezone(_UTC)
    except (OverflowError, ValueError):
        return None


def format_timestamp(moment: _dt.datetime) -> str:
    """Aware (or naive-UTC) datetime -> ``YYYY-MM-DDTHH:MM:SSZ``."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_UTC)
    return moment.astimezone(_UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_timestamp(text: Any) -> str | None:
    """ISO 8601 text -> ``YYYY-MM-DDTHH:MM:SSZ``, or None if it is not a timestamp."""
    parsed = parse_timestamp(text)
    return format_timestamp(parsed) if parsed is not None else None


def utc_now() -> str:
    return format_timestamp(_dt.datetime.now(_UTC))


# --- field records --------------------------------------------------------------
@dataclass(frozen=True)
class FieldRecord:
    value: Any
    status: FieldStatus
    source: str = ""
    extracted_at: str = ""
    reason: str = ""

    def __post_init__(self):
        if not isinstance(self.status, FieldStatus):
            raise TypeError("status must be a FieldStatus")
        for name in ("source", "extracted_at", "reason"):
            if not isinstance(getattr(self, name), str):
                raise TypeError(f"{name} must be a string")
        if self.status is FieldStatus.AVAILABLE:
            if self.value is None:
                raise ValueError("An available field needs a value.")
        else:
            if self.value is not None:
                raise ValueError("A field that is not available must not carry a value.")
            if not self.reason.strip():
                raise ValueError("A field that is not available needs a reason.")
        if not is_json_value(self.value):
            raise ValueError("Field values must be JSON-compatible.")
        if self.extracted_at and parse_timestamp(self.extracted_at) is None:
            raise ValueError("extracted_at must be an ISO 8601 date-time.")

    @property
    def available(self) -> bool:
        return self.status is FieldStatus.AVAILABLE

    def display(self) -> Any:
        """The value, or the status label when there is no value (for flat exports)."""
        return to_plain(self.value) if self.available else self.status.value

    def to_dict(self) -> dict:
        return {"value": to_plain(self.value), "status": self.status.value, "source": self.source,
                "extracted_at": self.extracted_at, "reason": self.reason}


def available(value: Any, source: str = "", extracted_at: str = "") -> FieldRecord:
    return FieldRecord(value, FieldStatus.AVAILABLE, source, extracted_at)


def unavailable(status: FieldStatus, reason: str, source: str = "", extracted_at: str = "") -> FieldRecord:
    if status is FieldStatus.AVAILABLE:
        raise ValueError("unavailable() needs a status other than Available.")
    return FieldRecord(None, status, source, extracted_at, reason)


def not_available(reason: str, source: str = "", extracted_at: str = "") -> FieldRecord:
    return unavailable(FieldStatus.NOT_AVAILABLE, reason, source, extracted_at)


# --- records ----------------------------------------------------------------------
RESERVED_RECORD_KEYS = frozenset({"record_type", "module", "platform", "dedup_key", "fields"})


def make_dedup_key(platform: str, kind: str, identifier: str) -> str:
    """``platform:kind:identifier``, e.g. ``instagram:profile:nike``."""
    for label, text in (("platform", platform), ("kind", kind)):
        if not isinstance(text, str) or not NAME_RE.fullmatch(text):
            raise ValueError(f"{label} must be a short lowercase name.")
    if (not isinstance(identifier, str) or not identifier
            or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in identifier)):
        raise ValueError("identifier must be a non-empty string without whitespace.")
    return f"{platform}:{kind}:{identifier}"


@dataclass(frozen=True)
class Record:
    record_type: str
    module: str
    platform: str
    dedup_key: str
    fields: Mapping[str, FieldRecord]
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        for label in ("record_type", "module", "platform"):
            text = getattr(self, label)
            if not isinstance(text, str) or not NAME_RE.fullmatch(text):
                raise ValueError(f"{label} must be a short lowercase name.")
        if not isinstance(self.dedup_key, str) or not self.dedup_key:
            raise ValueError("dedup_key is required.")
        if not isinstance(self.fields, Mapping):
            raise TypeError("fields must be a mapping of name -> FieldRecord.")
        for name, record in self.fields.items():
            if not isinstance(name, str) or not NAME_RE.fullmatch(name):
                raise ValueError("Field names must be short lowercase names.")
            if not isinstance(record, FieldRecord):
                raise TypeError(f"Field '{name}' must be a FieldRecord.")
        if not isinstance(self.extra, Mapping):
            raise TypeError("extra must be a mapping.")
        for key in self.extra:
            if not isinstance(key, str) or key in RESERVED_RECORD_KEYS:
                raise ValueError("extra keys must be strings and must not reuse record keys.")
        if not is_json_value(self.extra):
            raise ValueError("extra must be JSON-compatible.")

    def to_dict(self) -> dict:
        out = {"record_type": self.record_type, "module": self.module, "platform": self.platform,
               "dedup_key": self.dedup_key,
               "fields": {name: record.to_dict() for name, record in self.fields.items()}}
        out.update(to_plain(self.extra))
        return out
