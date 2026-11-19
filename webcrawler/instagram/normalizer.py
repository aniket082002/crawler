"""Normalize Instagram API objects into JSON-compatible records.

Rules:
  * Never fabricate: a value comes from the API response or not at all.
  * Every expected field gets an explicit status. A field that is missing
    is written as its status label ("Not available", "Not accessible",
    "Permission not granted", "Authentication required"), and
    ``field_status`` records the status and the reason, so a label can be
    told apart from real text.
  * A value the API actually returned always wins over a predicted status.
  * Every record carries provenance metadata: source endpoint (path only,
    never a query string or token), API version, auth mode, UTC extraction
    timestamp, Instagram account ID when known, and target username.
  * All strings pass through the redactor before they are stored. A value
    that was changed on the way (secret-like text masked, data nested too
    deeply cut off, NaN/Infinity removed) is never reported as plain
    "Available": its status is "Available (redacted)" or "Available
    (modified)", field_status lists the modifications, and
    metadata["modified_fields"] names those fields. The original API data
    is never written anywhere unredacted.

Record layout:
    {
      "record_type": "profile",
      "fields":       {"username": "nike", "biography": "Not accessible", ...},
      "field_status": {"username": {"status": "Available"},
                       "biography": {"status": "Not accessible", "reason": "..."}},
      "metadata":     {"source_endpoint": "/v27.0/me", "api_version": "v27.0", ...}
    }
"""
from __future__ import annotations

import datetime as _dt
import json
import math
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

from webcrawler.instagram.config import (
    API_VERSION_RE, AuthMode, CapabilityDecision, FieldStatus, parse_auth_mode,
)
from webcrawler.instagram.redaction import REDACTED, SENSITIVE_KEYS, Redactor, default_redactor

SCHEMA_VERSION = 1
MAX_DEPTH = 50

# Standard reason codes for missing fields.
REASON_NOT_RETURNED = "not_returned_by_api"
REASON_NULL_RETURNED = "null_returned_by_api"
REASON_NO_DATA = "no_data_returned"
REASON_REDACTED = "security_redaction_applied"
REASON_MODIFIED = "value_modified_for_output"

# Modifications applied to a returned value (listed in field_status).
MOD_REDACTED = "redacted"                       # secret-like text masked as [REDACTED]
MOD_NESTING_TRUNCATED = "nesting_truncated"     # data nested deeper than MAX_DEPTH cut off
MOD_NON_FINITE_REMOVED = "non_finite_number_removed"  # NaN/Infinity replaced by null
TRUNCATED_MARKER = "[TRUNCATED: nested too deeply]"

AVAILABLE_STATUSES = frozenset({FieldStatus.AVAILABLE, FieldStatus.AVAILABLE_REDACTED,
                                FieldStatus.AVAILABLE_MODIFIED})


@dataclass(frozen=True)
class FieldState:
    status: FieldStatus
    reason: str = ""
    modifications: tuple = ()

    def __post_init__(self):
        if not isinstance(self.status, FieldStatus):
            raise TypeError("status must be a FieldStatus")

    def to_dict(self) -> dict:
        d = {"status": self.status.value}
        if self.reason:
            d["reason"] = self.reason
        if self.modifications:
            d["modifications"] = list(self.modifications)
        return d


AVAILABLE = FieldState(FieldStatus.AVAILABLE)


def not_available(reason: str = REASON_NOT_RETURNED) -> FieldState:
    return FieldState(FieldStatus.NOT_AVAILABLE, reason)


def not_accessible(reason: str) -> FieldState:
    return FieldState(FieldStatus.NOT_ACCESSIBLE, reason)


def permission_not_granted(reason: str) -> FieldState:
    return FieldState(FieldStatus.PERMISSION_NOT_GRANTED, reason)


def authentication_required(reason: str = "No authenticated session.") -> FieldState:
    return FieldState(FieldStatus.AUTHENTICATION_REQUIRED, reason)


def state_from_decision(decision: CapabilityDecision) -> FieldState:
    """Missing-field state for a capability that is not allowed."""
    if decision.allowed:
        raise ValueError("The capability is allowed; there is no missing-field state.")
    return FieldState(decision.status, decision.reason)


def utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def sanitize_endpoint(endpoint: str) -> str:
    """Keep only the path of an endpoint (never host query strings or tokens)."""
    text = str(endpoint or "").strip()
    if not text:
        return ""
    path = urlsplit(text).path if "://" in text else text.split("?", 1)[0].split("#", 1)[0]
    return "/" + path.lstrip("/")


@dataclass(frozen=True)
class Provenance:
    source_endpoint: str
    api_version: str
    auth_mode: AuthMode | str
    target_username: str
    ig_account_id: str | None = None
    extracted_at: str | None = None   # filled with the current UTC time if None

    def to_dict(self, redactor: Redactor, clock: Callable[[], str] = utc_now) -> dict:
        if not API_VERSION_RE.fullmatch(str(self.api_version or "")):
            raise ValueError("api_version must look like 'v27.0'.")
        mode = parse_auth_mode(self.auth_mode).value
        account = self.ig_account_id
        return {
            "source_endpoint": redactor.redact(sanitize_endpoint(self.source_endpoint)),
            "api_version": self.api_version,
            "auth_mode": mode,
            "extracted_at": self.extracted_at or clock(),
            "ig_account_id": redactor.redact(str(account)) if account else FieldStatus.NOT_AVAILABLE.value,
            "target_username": redactor.redact(self.target_username),
        }


def _convert(value: Any, redactor: Redactor, mods: set, depth: int = 0) -> Any:
    """JSON-compatible copy of value. Every change made to the content is
    recorded in ``mods`` so the field status can say the value is not verbatim."""
    if depth > MAX_DEPTH:
        mods.add(MOD_NESTING_TRUNCATED)
        return TRUNCATED_MARKER
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        mods.add(MOD_NON_FINITE_REMOVED)
        return None
    if isinstance(value, str):
        out = redactor.redact(value)
        if out != value:
            mods.add(MOD_REDACTED)
        return out
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        out = {}
        for k, v in value.items():
            key = _convert(str(k), redactor, mods, depth)
            if str(k).lower() in SENSITIVE_KEYS:
                mods.add(MOD_REDACTED)
                out[key] = REDACTED
            else:
                out[key] = _convert(v, redactor, mods, depth + 1)
        return out
    if isinstance(value, (list, tuple, set, frozenset)):
        items = sorted(value, key=str) if isinstance(value, (set, frozenset)) else value
        return [_convert(v, redactor, mods, depth + 1) for v in items]
    return _convert(str(value), redactor, mods, depth)


def to_json_value(value: Any, redactor: Redactor, _depth: int = 0) -> Any:
    """Convert to a JSON-compatible value, redacting strings and secret keys."""
    return _convert(value, redactor, set(), _depth)


def converted_state(mods: set) -> FieldState:
    """Field state for a value the API returned, qualified if it was modified."""
    if not mods:
        return AVAILABLE
    if MOD_REDACTED in mods:
        return FieldState(FieldStatus.AVAILABLE_REDACTED, REASON_REDACTED, tuple(sorted(mods)))
    return FieldState(FieldStatus.AVAILABLE_MODIFIED, REASON_MODIFIED, tuple(sorted(mods)))


def normalize_record(record_type: str, raw: Mapping[str, Any] | None, expected_fields: Sequence[str],
                     provenance: Provenance, *, missing_states: Mapping[str, FieldState] | None = None,
                     default_missing: FieldState | None = None, include_unexpected: bool = True,
                     redactor: Redactor | None = None, clock: Callable[[], str] = utc_now) -> dict:
    """Normalize one API object.

    expected_fields  the fields this record type should have (output columns).
    missing_states   why specific fields are missing (e.g. from a capability
                     decision). Used ONLY if the API did not return a value.
    default_missing  state for missing fields without a specific state
                     (default: Not available / not_returned_by_api).
    """
    redactor = redactor or default_redactor
    if not record_type or not isinstance(record_type, str):
        raise ValueError("record_type is required.")
    if raw is not None and not isinstance(raw, Mapping):
        raise TypeError("raw must be a mapping (a decoded JSON object) or None.")
    raw = raw or {}
    missing_states = missing_states or {}
    default_missing = default_missing or not_available(REASON_NOT_RETURNED)
    # A missing field can never be declared "available": that would let a
    # caller report data that the API did not return.
    for state in (default_missing, *missing_states.values()):
        if state.status in AVAILABLE_STATUSES:
            raise ValueError("A missing-field state must not be an 'Available' status.")

    fields: dict[str, Any] = {}
    status: dict[str, dict] = {}

    modified: list[str] = []

    def put(name: str, state: FieldState, value: Any = None):
        key = redactor.redact(str(name))
        if state.status is FieldStatus.AVAILABLE:
            mods: set = set()
            if str(name).lower() in SENSITIVE_KEYS:
                mods.add(MOD_REDACTED)
                fields[key] = REDACTED
            else:
                fields[key] = _convert(value, redactor, mods)
            state = converted_state(mods)
            if mods:
                modified.append(key)
        else:
            fields[key] = state.status.value
        status[key] = FieldState(state.status, redactor.redact(state.reason),
                                 state.modifications).to_dict()

    seen = set()
    for name in expected_fields:
        if name in seen:
            continue
        seen.add(name)
        if name in raw and raw[name] is not None:
            put(name, AVAILABLE, raw[name])
        elif name in missing_states:
            put(name, missing_states[name])
        elif name in raw:  # explicit null from the API
            put(name, not_available(REASON_NULL_RETURNED))
        else:
            put(name, default_missing)

    unexpected = []
    if include_unexpected:
        for name in raw:
            if name in seen:
                continue
            unexpected.append(name)
            if raw[name] is None:
                put(name, not_available(REASON_NULL_RETURNED))
            else:
                put(name, AVAILABLE, raw[name])

    metadata = provenance.to_dict(redactor, clock)
    metadata["schema_version"] = SCHEMA_VERSION
    if unexpected:
        metadata["unrequested_fields"] = sorted(redactor.redact(str(n)) for n in unexpected)
    if modified:
        metadata["modified_fields"] = sorted(modified)
    return {"record_type": record_type, "fields": fields, "field_status": status,
            "metadata": metadata}


def normalize_unavailable(record_type: str, expected_fields: Sequence[str], provenance: Provenance,
                          state: FieldState, **kwargs) -> dict:
    """A record where nothing could be obtained, every field marked with ``state``."""
    if state.status in AVAILABLE_STATUSES:
        raise ValueError("state must describe why the data is missing.")
    return normalize_record(record_type, None, expected_fields, provenance,
                            default_missing=state, include_unexpected=False, **kwargs)


def normalize_records(record_type: str, raws: Iterable[Mapping[str, Any]], expected_fields: Sequence[str],
                      provenance: Provenance, **kwargs) -> list[dict]:
    return [normalize_record(record_type, raw, expected_fields, provenance, **kwargs) for raw in raws]


def status_counts(record: Mapping[str, Any]) -> dict:
    """How many fields of a record have each status (for summaries)."""
    counts: dict[str, int] = {}
    for entry in (record.get("field_status") or {}).values():
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
    return counts


def to_json(record: Any, **kwargs) -> str:
    """Strict JSON (NaN/Infinity are rejected) of a normalized record."""
    return json.dumps(record, ensure_ascii=False, allow_nan=False, **kwargs)
