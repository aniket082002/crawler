"""Shared record structure: statuses, field records, records, timestamps."""
import dataclasses
import datetime as dt
import json

import pytest

from webcrawler.social_common import schema as S
from webcrawler.social_common.schema import (
    FieldRecord, FieldStatus, Record, available, make_dedup_key, not_available, unavailable,
)

STAMP = "2026-11-13T04:35:25Z"
MISSING_STATUSES = [s for s in FieldStatus if s is not FieldStatus.AVAILABLE]


# --- statuses ---------------------------------------------------------------------
def test_status_labels_are_the_agreed_seven():
    assert [s.value for s in FieldStatus] == [
        "Available", "Not available", "Blocked", "Login required", "Restricted", "Not supported",
        "Parsing failed"]


# --- field records ------------------------------------------------------------------
def test_available_field():
    record = available("nike", "https://acme.example/contact", STAMP)
    assert record.available and record.display() == "nike"
    assert record.to_dict() == {"value": "nike", "status": "Available",
                                "source": "https://acme.example/contact", "extracted_at": STAMP, "reason": ""}


@pytest.mark.parametrize("value", ["", 0, False, [], {}, 0.0])
def test_empty_but_real_values_are_available(value):
    assert available(value).available


def test_available_field_needs_a_value():
    with pytest.raises(ValueError):
        available(None)


@pytest.mark.parametrize("status", MISSING_STATUSES)
def test_missing_field_has_no_value_and_shows_its_status(status):
    record = unavailable(status, "the page did not contain it", "https://acme.example/", STAMP)
    assert not record.available and record.value is None
    assert record.display() == status.value
    assert record.to_dict()["reason"] == "the page did not contain it"


@pytest.mark.parametrize("status", MISSING_STATUSES)
@pytest.mark.parametrize("reason", ["", "   "])
def test_missing_field_needs_a_reason(status, reason):
    with pytest.raises(ValueError):
        FieldRecord(None, status, reason=reason)


@pytest.mark.parametrize("status", MISSING_STATUSES)
def test_missing_field_can_never_carry_a_value(status):
    """Nothing can be reported as data while being marked unavailable."""
    with pytest.raises(ValueError):
        FieldRecord("made up", status, reason="because")


def test_helpers():
    assert not_available("gone").status is FieldStatus.NOT_AVAILABLE
    with pytest.raises(ValueError):
        unavailable(FieldStatus.AVAILABLE, "x")


def test_status_must_be_a_field_status():
    with pytest.raises(TypeError):
        FieldRecord("x", "Available")


@pytest.mark.parametrize("bad", [object(), {1: "a"}, float("nan"), float("inf"), {"a": object()},
                                 b"bytes", {1, 2}, [object()], dt.datetime(2026, 1, 1)])
def test_values_must_be_json_compatible(bad):
    with pytest.raises(ValueError):
        available(bad)


def test_deeply_nested_values_refused():
    value = "x"
    for _ in range(S.MAX_VALUE_DEPTH + 2):
        value = [value]
    with pytest.raises(ValueError):
        available(value)
    assert not S.is_json_value(value)


@pytest.mark.parametrize("name", ["source", "extracted_at", "reason"])
def test_text_attributes_must_be_strings(name):
    kwargs = {"source": "s", "extracted_at": "", "reason": ""}
    kwargs[name] = 5
    with pytest.raises(TypeError):
        FieldRecord("x", FieldStatus.AVAILABLE, **kwargs)


@pytest.mark.parametrize("bad", ["yesterday", "2026-13-45T00:00:00Z", "2026-11-13", "=HYPERLINK(1)", "12:30"])
def test_extracted_at_must_be_a_timestamp(bad):
    with pytest.raises(ValueError):
        available("x", extracted_at=bad)


def test_field_record_is_immutable():
    record = available("x")
    with pytest.raises(dataclasses.FrozenInstanceError):
        record.value = "y"


def test_tuples_are_written_as_lists():
    record = available(("a", ("b",)), extracted_at=STAMP)
    assert record.to_dict()["value"] == ["a", ["b"]] and record.display() == ["a", ["b"]]


def test_to_dict_returns_a_copy():
    data = {"k": ["v"]}
    record = available(data)
    record.to_dict()["value"]["k"].append("changed")
    assert record.to_dict()["value"] == {"k": ["v"]}


# --- timestamps ------------------------------------------------------------------------
@pytest.mark.parametrize("text, expected", [
    ("2026-11-13T04:35:25+00:00", "2026-11-13T04:35:25Z"),     # exactly what the crawler writes
    ("2026-11-13T04:35:25Z", "2026-11-13T04:35:25Z"),
    ("2026-11-13T04:35:25z", "2026-11-13T04:35:25Z"),
    ("2026-11-13T10:05:25+05:30", "2026-11-13T04:35:25Z"),     # converted to UTC
    ("2026-11-12T23:35:25-05:00", "2026-11-13T04:35:25Z"),
    ("2026-11-13T04:35:25", "2026-11-13T04:35:25Z"),           # no offset: taken as UTC
    ("2026-11-13 04:35:25", "2026-11-13T04:35:25Z"),
    ("2026-11-13T04:35:25.987654+00:00", "2026-11-13T04:35:25Z"),
    ("  2026-11-13T04:35:25+00:00  ", "2026-11-13T04:35:25Z"),
    ("2026-11-13T04:35", "2026-11-13T04:35:00Z"),
])
def test_normalize_timestamp(text, expected):
    assert S.normalize_timestamp(text) == expected
    assert S.parse_timestamp(text).tzinfo is not None


@pytest.mark.parametrize("bad", [None, 5, 5.5, "", "   ", "not a date", "2026-11-13", "13/11/2026 04:35",
                                 "2026-02-30T00:00:00", "2026-11-13T25:00:00", "x" * 100,
                                 "0001-01-01T00:00:00+14:00", ["2026-11-13T04:35:25Z"], "=1+1"])
def test_not_a_timestamp(bad):
    assert S.parse_timestamp(bad) is None and S.normalize_timestamp(bad) is None


def test_format_and_now():
    naive = dt.datetime(2026, 11, 13, 4, 35, 25)
    assert S.format_timestamp(naive) == "2026-11-13T04:35:25Z"
    ist = dt.timezone(dt.timedelta(hours=5, minutes=30))
    assert S.format_timestamp(dt.datetime(2026, 11, 13, 10, 5, 25, tzinfo=ist)) == "2026-11-13T04:35:25Z"
    now = S.parse_timestamp(S.utc_now())
    assert abs((dt.datetime.now(dt.timezone.utc) - now).total_seconds()) < 60


def test_normalised_timestamps_sort_in_time_order():
    stamps = [S.normalize_timestamp(t) for t in ("2026-11-13T10:05:25+05:30", "2026-11-12T23:00:00-08:00",
                                                 "2026-11-13T04:35:24Z")]
    assert sorted(stamps) == ["2026-11-13T04:35:24Z", "2026-11-13T04:35:25Z", "2026-11-13T07:00:00Z"]


# --- records -----------------------------------------------------------------------------
def make_record(**overrides):
    kwargs = dict(record_type="discovered_profile", module="social_discovery", platform="instagram",
                  dedup_key="instagram:profile:nike",
                  fields={"username": available("nike", "https://acme.example/", STAMP),
                          "biography": unavailable(FieldStatus.NOT_SUPPORTED, "not read by this module")},
                  extra={"sources": [{"source_page_url": "https://acme.example/"}]})
    kwargs.update(overrides)
    return Record(**kwargs)


def test_record_to_dict():
    data = make_record().to_dict()
    assert list(data) == ["record_type", "module", "platform", "dedup_key", "fields", "sources"]
    assert data["fields"]["username"]["value"] == "nike"
    assert data["fields"]["biography"] == {"value": None, "status": "Not supported", "source": "",
                                           "extracted_at": "", "reason": "not read by this module"}
    assert data["sources"] == [{"source_page_url": "https://acme.example/"}]
    assert json.loads(json.dumps(data, allow_nan=False)) == data      # strict JSON


def test_record_without_extra():
    assert "sources" not in make_record(extra={}).to_dict()


@pytest.mark.parametrize("overrides", [
    {"record_type": ""}, {"record_type": "Discovered Profile"}, {"module": "social-discovery"},
    {"platform": "Instagram"}, {"platform": None}, {"dedup_key": ""}, {"dedup_key": 5},
    {"fields": {"User Name": available("x")}}, {"fields": {"": available("x")}},
    {"fields": {"username": "nike"}}, {"fields": ["username"]},
    {"extra": {"fields": 1}}, {"extra": {"record_type": "x"}}, {"extra": {"k": object()}},
    {"extra": {1: "x"}}, {"extra": ["x"]},
])
def test_record_validation(overrides):
    with pytest.raises((ValueError, TypeError)):
        make_record(**overrides)


def test_make_dedup_key():
    assert make_dedup_key("instagram", "profile", "nike") == "instagram:profile:nike"
    assert make_dedup_key("instagram", "profile", "opositive.io") == "instagram:profile:opositive.io"


@pytest.mark.parametrize("args", [("Instagram", "profile", "nike"), ("instagram", "Profile", "nike"),
                                  ("", "profile", "nike"), ("instagram", "profile", ""),
                                  ("instagram", "profile", "ni ke"), ("instagram", "profile", "nike\n"),
                                  ("instagram", "profile", None)])
def test_make_dedup_key_rejects_bad_parts(args):
    with pytest.raises(ValueError):
        make_dedup_key(*args)
