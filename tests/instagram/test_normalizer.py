"""Normalizer: explicit statuses, provenance metadata, no fabrication."""
import datetime as dt
import json
import math

import pytest

from tests.instagram.conftest import FAKE_FB_TOKEN, FAKE_IG_TOKEN
from webcrawler.instagram.config import (
    AccountType, AuthContext, AuthMode, Capability, FieldStatus, evaluate_capability,
)
from webcrawler.instagram.normalizer import (
    Provenance, REASON_NOT_RETURNED, REASON_NULL_RETURNED, TRUNCATED_MARKER, authentication_required,
    normalize_record, normalize_records, normalize_unavailable, not_accessible, not_available,
    permission_not_granted, sanitize_endpoint, state_from_decision, status_counts, to_json,
)
from webcrawler.instagram.redaction import REDACTED, Redactor

FIXED = "2026-11-18T12:00:00Z"
PROFILE_FIELDS = ["id", "username", "name", "biography", "followers_count", "website", "account_type"]


def prov(**kw):
    base = dict(source_endpoint="/v27.0/17841400000000000", api_version="v27.0", auth_mode="facebook",
                target_username="nike", ig_account_id="17841400000000000")
    base.update(kw)
    return Provenance(**base)


def norm(raw, fields=PROFILE_FIELDS, **kw):
    kw.setdefault("redactor", Redactor())
    kw.setdefault("clock", lambda: FIXED)
    return normalize_record("profile", raw, fields, prov(), **kw)


def test_available_and_missing_fields():
    rec = norm({"id": "1", "username": "nike", "followers_count": 300000000})
    f, s = rec["fields"], rec["field_status"]
    assert f["username"] == "nike" and f["followers_count"] == 300000000
    assert s["username"] == {"status": "Available"}
    assert f["biography"] == "Not available"
    assert s["biography"] == {"status": "Not available", "reason": REASON_NOT_RETURNED}
    assert list(f) == PROFILE_FIELDS  # expected field order kept


def test_explicit_null_is_not_available_not_fabricated():
    rec = norm({"id": "1", "website": None})
    assert rec["fields"]["website"] == "Not available"
    assert rec["field_status"]["website"]["reason"] == REASON_NULL_RETURNED


def test_falsy_real_values_are_kept():
    rec = norm({"id": "1", "followers_count": 0, "biography": "", "name": False})
    assert rec["fields"]["followers_count"] == 0
    assert rec["fields"]["biography"] == ""
    assert rec["fields"]["name"] is False
    assert all(rec["field_status"][k]["status"] == "Available" for k in ("followers_count", "biography", "name"))


def test_missing_states_explain_why():
    rec = norm({"id": "1", "username": "nike"}, missing_states={
        "biography": not_accessible("Business Discovery returns public fields only."),
        "followers_count": permission_not_granted("Not granted: instagram_manage_insights."),
        "website": authentication_required(),
    })
    f, s = rec["fields"], rec["field_status"]
    assert f["biography"] == "Not accessible" and "public fields" in s["biography"]["reason"]
    assert f["followers_count"] == "Permission not granted"
    assert f["website"] == "Authentication required"
    assert f["name"] == "Not available"   # no state given -> default


def test_real_value_wins_over_predicted_status():
    rec = norm({"biography": "Just do it."}, missing_states={"biography": not_accessible("predicted")})
    assert rec["fields"]["biography"] == "Just do it."
    assert rec["field_status"]["biography"] == {"status": "Available"}


def test_default_missing_state():
    rec = norm({}, default_missing=not_accessible("Target is not a professional account."))
    assert set(rec["fields"].values()) == {"Not accessible"}


def test_status_label_and_real_text_distinguishable():
    rec = norm({"biography": "Not available"})  # someone's real bio text
    assert rec["fields"]["biography"] == "Not available"
    assert rec["field_status"]["biography"]["status"] == "Available"


def test_metadata_provenance():
    rec = norm({"id": "1"})
    assert rec["record_type"] == "profile"
    assert rec["metadata"] == {
        "source_endpoint": "/v27.0/17841400000000000", "api_version": "v27.0", "auth_mode": "facebook",
        "extracted_at": FIXED, "ig_account_id": "17841400000000000", "target_username": "nike",
        "schema_version": 1,
    }


def test_missing_account_id_is_marked_not_fabricated():
    rec = normalize_record("profile", {}, ["id"], prov(ig_account_id=None), redactor=Redactor(),
                           clock=lambda: FIXED)
    assert rec["metadata"]["ig_account_id"] == "Not available"


def test_default_clock_is_utc_iso():
    rec = normalize_record("profile", {}, ["id"], prov(), redactor=Redactor())
    ts = rec["metadata"]["extracted_at"]
    assert ts.endswith("Z")
    parsed = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    assert abs((dt.datetime.now(dt.timezone.utc) - parsed).total_seconds()) < 60


def test_explicit_extracted_at_is_kept():
    p = prov(extracted_at="2026-01-01T00:00:00Z")
    assert normalize_record("x", {}, [], p, redactor=Redactor())["metadata"]["extracted_at"] == "2026-01-01T00:00:00Z"


@pytest.mark.parametrize("mode", ["instagram", "facebook", AuthMode.INSTAGRAM_LOGIN])
def test_auth_mode_recorded(mode):
    rec = normalize_record("x", {}, [], prov(auth_mode=mode), redactor=Redactor(), clock=lambda: FIXED)
    assert rec["metadata"]["auth_mode"] in ("instagram", "facebook")


def test_invalid_provenance_rejected():
    with pytest.raises(ValueError):
        normalize_record("x", {}, [], prov(api_version="27"), redactor=Redactor())
    with pytest.raises(Exception):
        normalize_record("x", {}, [], prov(auth_mode="myspace"), redactor=Redactor())


@pytest.mark.parametrize("endpoint, expected", [
    ("/v27.0/me/media", "/v27.0/me/media"),
    ("v27.0/me", "/v27.0/me"),
    (f"https://graph.facebook.com/v27.0/me?fields=id&access_token={FAKE_FB_TOKEN}", "/v27.0/me"),
    (f"/v27.0/me?access_token={FAKE_FB_TOKEN}#x", "/v27.0/me"),
    ("", ""),
])
def test_source_endpoint_is_path_only(endpoint, expected):
    assert sanitize_endpoint(endpoint) == expected
    rec = normalize_record("x", {}, [], prov(source_endpoint=endpoint), redactor=Redactor(), clock=lambda: FIXED)
    assert FAKE_FB_TOKEN not in json.dumps(rec)


def test_unrequested_fields_kept_and_listed():
    rec = norm({"id": "1", "media_count": 5, "extra_null": None})
    assert rec["fields"]["media_count"] == 5
    assert rec["field_status"]["extra_null"]["status"] == "Not available"
    assert rec["metadata"]["unrequested_fields"] == ["extra_null", "media_count"]
    rec2 = norm({"id": "1", "media_count": 5}, include_unexpected=False)
    assert "media_count" not in rec2["fields"]


def test_nested_values_json_compatible_and_redacted():
    r = Redactor([FAKE_IG_TOKEN])
    raw = {"id": "1", "children": {"data": [{"id": "c1", "media_url": "https://cdn.example/x.jpg"}]},
           "when": dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc), "ratio": math.nan,
           "tags": {"b", "a"}, "biography": f"leak {FAKE_IG_TOKEN}",
           "debug": {"access_token": "whatever", "note": f"access_token={FAKE_FB_TOKEN}"}}
    rec = normalize_record("media", raw, ["id"], prov(), redactor=r, clock=lambda: FIXED)
    text = to_json(rec)
    assert FAKE_IG_TOKEN not in text and FAKE_FB_TOKEN not in text
    f = rec["fields"]
    assert f["children"]["data"][0]["id"] == "c1"
    assert f["when"] == "2026-01-01T00:00:00+00:00"
    assert f["ratio"] is None
    assert f["tags"] == ["a", "b"]
    assert f["debug"]["access_token"] == REDACTED
    assert f["biography"] == f"leak {REDACTED}"


def test_sensitive_top_level_field_never_stored():
    rec = norm({"id": "1", "access_token": FAKE_FB_TOKEN})
    assert rec["fields"]["access_token"] == REDACTED
    assert FAKE_FB_TOKEN not in to_json(rec)


def test_to_json_strict_and_round_trips():
    rec = norm({"id": "1", "username": "nike"})
    assert json.loads(to_json(rec)) == rec


def test_normalize_unavailable_marks_every_field():
    rec = normalize_unavailable("insights", ["reach", "views"], prov(),
                                not_accessible("Insights are only available for your own account."),
                                redactor=Redactor(), clock=lambda: FIXED)
    assert rec["fields"] == {"reach": "Not accessible", "views": "Not accessible"}
    with pytest.raises(ValueError):
        normalize_unavailable("insights", ["reach"], prov(), not_available().__class__(FieldStatus.AVAILABLE))


def test_state_from_capability_decision():
    other = AuthContext(mode=AuthMode.FACEBOOK_LOGIN, authenticated=True, account_type=AccountType.BUSINESS,
                        granted_permissions=frozenset({"instagram_basic", "pages_show_list",
                                                       "pages_read_engagement", "instagram_manage_insights"}),
                        facebook_page_linked=True, username="myshop")
    d = evaluate_capability(Capability.MEDIA_INSIGHTS, other, target_is_own=False)
    state = state_from_decision(d)
    assert state.status is FieldStatus.NOT_ACCESSIBLE
    rec = normalize_unavailable("insights", ["reach"], prov(), state, redactor=Redactor(), clock=lambda: FIXED)
    assert rec["fields"]["reach"] == "Not accessible"
    allowed = evaluate_capability(Capability.BUSINESS_DISCOVERY, other, target_is_own=False)
    with pytest.raises(ValueError):
        state_from_decision(allowed)


def test_field_state_requires_status_enum():
    from webcrawler.instagram.normalizer import FieldState
    with pytest.raises(TypeError):
        FieldState("Not available")


def test_normalize_records_and_status_counts():
    recs = normalize_records("media", [{"id": "1", "caption": "a"}, {"id": "2"}], ["id", "caption"], prov(),
                             redactor=Redactor(), clock=lambda: FIXED)
    assert [r["fields"]["id"] for r in recs] == ["1", "2"]
    assert status_counts(recs[1]) == {"Available": 1, "Not available": 1}


@pytest.mark.parametrize("raw", ["string", [1, 2], 5])
def test_raw_must_be_mapping(raw):
    with pytest.raises(TypeError):
        norm(raw)


def test_record_type_required():
    with pytest.raises(ValueError):
        normalize_record("", {}, [], prov(), redactor=Redactor())


def test_duplicate_expected_fields_collapsed():
    rec = norm({"id": "1"}, fields=["id", "id", "username"])
    assert list(rec["fields"]) == ["id", "username"]


def test_deep_nesting_is_bounded():
    deep = current = {}
    for _ in range(200):
        current["n"] = {}
        current = current["n"]
    rec = norm({"id": "1", "deep": deep})
    text = to_json(rec)  # cut off instead of recursing forever
    # I2: marked as truncated, not mislabelled as a redacted secret
    assert TRUNCATED_MARKER in text and REDACTED not in text
    assert rec["field_status"]["deep"] == {"status": "Available (modified)",
                                           "reason": "value_modified_for_output",
                                           "modifications": ["nesting_truncated"]}
    assert rec["metadata"]["modified_fields"] == ["deep"]


# --- I1: redacted / modified values are never reported as plain "Available" -------------------
def test_redacted_caption_reported_as_redacted():
    caption = "Promo code EAAAAAAAAAAAAAAAAAAAAAAAAAAAAA today"   # genuine text shaped like a token
    rec = norm({"id": "1", "biography": caption})
    assert rec["fields"]["biography"] == "Promo code [REDACTED] today"
    assert rec["field_status"]["biography"] == {"status": "Available (redacted)",
                                                "reason": "security_redaction_applied",
                                                "modifications": ["redacted"]}
    assert rec["field_status"]["id"] == {"status": "Available"}
    assert rec["metadata"]["modified_fields"] == ["biography"]


def test_redaction_inside_nested_value_qualifies_whole_field():
    rec = norm({"id": "1", "children": {"data": [{"caption": f"x access_token={FAKE_FB_TOKEN}"}]}})
    assert rec["field_status"]["children"]["status"] == "Available (redacted)"
    assert FAKE_FB_TOKEN not in to_json(rec)


def test_sensitive_nested_key_qualifies_field():
    rec = norm({"id": "1", "debug": {"access_token": "whatever"}})
    assert rec["fields"]["debug"] == {"access_token": REDACTED}
    assert rec["field_status"]["debug"]["status"] == "Available (redacted)"


def test_non_finite_number_reported_as_modified():
    rec = norm({"id": "1", "ratio": math.inf})
    assert rec["fields"]["ratio"] is None
    assert rec["field_status"]["ratio"]["status"] == "Available (modified)"
    assert rec["field_status"]["ratio"]["modifications"] == ["non_finite_number_removed"]


def test_redaction_takes_precedence_over_other_modifications():
    rec = norm({"id": "1", "mix": {"a": math.nan, "b": f"t {FAKE_IG_TOKEN}"}}, redactor=Redactor([FAKE_IG_TOKEN]))
    st = rec["field_status"]["mix"]
    assert st["status"] == "Available (redacted)"
    assert st["modifications"] == ["non_finite_number_removed", "redacted"]


def test_unmodified_values_have_no_qualification():
    rec = norm({"id": "1", "username": "nike", "followers_count": 5})
    assert all(v == {"status": "Available"} for k, v in rec["field_status"].items()
               if k in ("id", "username", "followers_count"))
    assert "modified_fields" not in rec["metadata"]


@pytest.mark.parametrize("status", [FieldStatus.AVAILABLE, FieldStatus.AVAILABLE_REDACTED,
                                    FieldStatus.AVAILABLE_MODIFIED])
def test_missing_field_state_can_never_claim_availability(status):
    from webcrawler.instagram.normalizer import FieldState
    with pytest.raises(ValueError):
        norm({}, missing_states={"biography": FieldState(status)})
    with pytest.raises(ValueError):
        norm({}, default_missing=FieldState(status))
    with pytest.raises(ValueError):
        normalize_unavailable("x", ["a"], prov(), FieldState(status), redactor=Redactor())
