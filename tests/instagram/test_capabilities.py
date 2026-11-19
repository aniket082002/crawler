"""Authentication capability model: modes, permissions, eligibility, token expiry."""
import datetime as dt

import pytest

from webcrawler.instagram import config as C
from webcrawler.instagram.config import (
    AccountType, AuthContext, AuthMode, Capability, FieldStatus, TokenMetadata,
    evaluate_all, evaluate_capability,
)

NOW = dt.datetime(2026, 11, 18, 12, 0, tzinfo=dt.timezone.utc)

IG_ALL = frozenset({C.IG_BUSINESS_BASIC, C.IG_BUSINESS_MANAGE_COMMENTS, C.IG_BUSINESS_MANAGE_INSIGHTS})
FB_ALL = frozenset({C.FB_INSTAGRAM_BASIC, C.FB_INSTAGRAM_MANAGE_COMMENTS, C.FB_INSTAGRAM_MANAGE_INSIGHTS,
                    C.FB_PAGES_SHOW_LIST, C.FB_PAGES_READ_ENGAGEMENT})


def ctx(mode, perms=None, **kw):
    perms = (IG_ALL if mode is AuthMode.INSTAGRAM_LOGIN else FB_ALL) if perms is None else frozenset(perms)
    kw.setdefault("account_type", AccountType.BUSINESS)
    kw.setdefault("facebook_page_linked", True if mode is AuthMode.FACEBOOK_LOGIN else None)
    return AuthContext(mode=mode, authenticated=True, granted_permissions=perms, username="myshop",
                       ig_user_id="17841400000000000", **kw)


def status(cap, context, own=True):
    return evaluate_capability(cap, context, target_is_own=own, now=NOW).status


def test_every_capability_has_a_rule_in_both_modes():
    for mode in AuthMode:
        assert set(C.CAPABILITY_RULES[mode]) == set(Capability)


def test_modes_do_not_expose_identical_capabilities():
    ig, fb = C.CAPABILITY_RULES[AuthMode.INSTAGRAM_LOGIN], C.CAPABILITY_RULES[AuthMode.FACEBOOK_LOGIN]
    differ = {cap for cap in Capability if ig[cap].supported != fb[cap].supported}
    assert {Capability.BUSINESS_DISCOVERY, Capability.HASHTAG_SEARCH, Capability.MEDIA_PRODUCT_TYPE} <= differ


def test_permission_names_are_mode_specific():
    ig_perms = set().union(*(r.permissions for r in C.CAPABILITY_RULES[AuthMode.INSTAGRAM_LOGIN].values()))
    fb_perms = set().union(*(r.permissions for r in C.CAPABILITY_RULES[AuthMode.FACEBOOK_LOGIN].values()))
    assert ig_perms and fb_perms and not ig_perms & fb_perms
    assert all(p.startswith("instagram_business_") for p in ig_perms)


# --- Instagram Login ---------------------------------------------------------------------------
def test_instagram_login_own_account_full_permissions():
    d = evaluate_all(ctx(AuthMode.INSTAGRAM_LOGIN), target_is_own=True, now=NOW)
    for cap in (Capability.OWN_PROFILE, Capability.OWN_MEDIA, Capability.COMMENTS,
                Capability.MEDIA_INSIGHTS, Capability.ACCOUNT_INSIGHTS, Capability.TAGGED_MEDIA,
                Capability.STORIES):
        assert d[cap].status is FieldStatus.AVAILABLE, cap
    for cap in (Capability.BUSINESS_DISCOVERY, Capability.HASHTAG_SEARCH, Capability.MEDIA_PRODUCT_TYPE,
                Capability.CONTACT_INFORMATION, Capability.FOLLOWER_LISTS):
        assert d[cap].status is FieldStatus.NOT_AVAILABLE, cap


def test_instagram_login_cannot_reach_other_accounts():
    d = evaluate_all(ctx(AuthMode.INSTAGRAM_LOGIN), target_is_own=False, now=NOW)
    assert d[Capability.OWN_PROFILE].status is FieldStatus.NOT_ACCESSIBLE
    assert d[Capability.COMMENTS].status is FieldStatus.NOT_ACCESSIBLE
    assert d[Capability.BUSINESS_DISCOVERY].status is FieldStatus.NOT_AVAILABLE
    assert not any(dec.allowed for dec in d.values())


def test_instagram_login_missing_insights_permission():
    d = evaluate_capability(Capability.MEDIA_INSIGHTS,
                            ctx(AuthMode.INSTAGRAM_LOGIN, {C.IG_BUSINESS_BASIC}), target_is_own=True, now=NOW)
    assert d.status is FieldStatus.PERMISSION_NOT_GRANTED
    assert d.missing_permissions == (C.IG_BUSINESS_MANAGE_INSIGHTS,)
    assert C.IG_BUSINESS_MANAGE_INSIGHTS in d.reason


def test_unverified_rules_are_flagged():
    d = evaluate_capability(Capability.STORIES, ctx(AuthMode.INSTAGRAM_LOGIN), target_is_own=True, now=NOW)
    assert d.allowed and d.verified is False
    assert evaluate_capability(Capability.STORIES, ctx(AuthMode.FACEBOOK_LOGIN),
                               target_is_own=True, now=NOW).verified is True


# --- Facebook Login ---------------------------------------------------------------------------------
def test_facebook_login_business_discovery_for_other_account():
    d = evaluate_all(ctx(AuthMode.FACEBOOK_LOGIN), target_is_own=False, now=NOW)
    assert d[Capability.BUSINESS_DISCOVERY].status is FieldStatus.AVAILABLE
    for cap in (Capability.OWN_PROFILE, Capability.OWN_MEDIA, Capability.COMMENTS, Capability.MEDIA_INSIGHTS,
                Capability.ACCOUNT_INSIGHTS, Capability.STORIES, Capability.TAGGED_MEDIA):
        assert d[cap].status is FieldStatus.NOT_ACCESSIBLE, cap
    assert d[Capability.CONTACT_INFORMATION].status is FieldStatus.NOT_AVAILABLE


def test_facebook_login_business_discovery_needs_manage_insights():
    perms = FB_ALL - {C.FB_INSTAGRAM_MANAGE_INSIGHTS}
    d = evaluate_capability(Capability.BUSINESS_DISCOVERY, ctx(AuthMode.FACEBOOK_LOGIN, perms),
                            target_is_own=False, now=NOW)
    assert d.status is FieldStatus.PERMISSION_NOT_GRANTED
    assert d.missing_permissions == (C.FB_INSTAGRAM_MANAGE_INSIGHTS,)


def test_hashtag_search_needs_reviewed_feature():
    c = ctx(AuthMode.FACEBOOK_LOGIN)
    d = evaluate_capability(Capability.HASHTAG_SEARCH, c, target_is_own=False, now=NOW)
    assert d.status is FieldStatus.PERMISSION_NOT_GRANTED
    assert d.missing_features == (C.FEATURE_PUBLIC_CONTENT_ACCESS,) and d.missing_permissions == ()
    c2 = ctx(AuthMode.FACEBOOK_LOGIN, granted_features=frozenset({C.FEATURE_PUBLIC_CONTENT_ACCESS}))
    assert status(Capability.HASHTAG_SEARCH, c2, own=False) is FieldStatus.AVAILABLE


def test_facebook_login_requires_linked_page():
    c = ctx(AuthMode.FACEBOOK_LOGIN, facebook_page_linked=False)
    d = evaluate_capability(Capability.OWN_PROFILE, c, target_is_own=True, now=NOW)
    assert d.status is FieldStatus.NOT_ACCESSIBLE and "Facebook Page" in d.reason
    # unknown link state is not treated as a failure
    assert status(Capability.OWN_PROFILE, ctx(AuthMode.FACEBOOK_LOGIN, facebook_page_linked=None)) \
        is FieldStatus.AVAILABLE


def test_instagram_login_does_not_need_a_page():
    c = ctx(AuthMode.INSTAGRAM_LOGIN, facebook_page_linked=False)
    assert status(Capability.OWN_PROFILE, c) is FieldStatus.AVAILABLE


# --- shared rules -----------------------------------------------------------------------------------
@pytest.mark.parametrize("mode", list(AuthMode))
def test_unauthenticated_requires_authentication(mode):
    d = evaluate_all(AuthContext.unauthenticated(mode), target_is_own=True, now=NOW)
    for cap, dec in d.items():
        rule = C.CAPABILITY_RULES[mode][cap]
        expected = FieldStatus.AUTHENTICATION_REQUIRED if rule.supported else FieldStatus.NOT_AVAILABLE
        assert dec.status is expected, cap


@pytest.mark.parametrize("mode", list(AuthMode))
def test_expired_token_requires_authentication(mode):
    token = TokenMetadata(expires_at=NOW - dt.timedelta(seconds=1))
    d = evaluate_capability(Capability.OWN_PROFILE, ctx(mode, token=token), target_is_own=True, now=NOW)
    assert d.status is FieldStatus.AUTHENTICATION_REQUIRED and "expired" in d.reason


@pytest.mark.parametrize("mode", list(AuthMode))
def test_personal_account_not_accessible(mode):
    d = evaluate_capability(Capability.OWN_MEDIA, ctx(mode, account_type=AccountType.PERSONAL),
                            target_is_own=True, now=NOW)
    assert d.status is FieldStatus.NOT_ACCESSIBLE and "professional" in d.reason


def test_creator_account_eligible():
    assert status(Capability.OWN_MEDIA, ctx(AuthMode.INSTAGRAM_LOGIN, account_type=AccountType.CREATOR)) \
        is FieldStatus.AVAILABLE


def test_no_permissions_at_all():
    d = evaluate_capability(Capability.OWN_PROFILE, ctx(AuthMode.FACEBOOK_LOGIN, set()),
                            target_is_own=True, now=NOW)
    assert d.status is FieldStatus.PERMISSION_NOT_GRANTED
    assert set(d.missing_permissions) == {C.FB_INSTAGRAM_BASIC, C.FB_PAGES_SHOW_LIST, C.FB_PAGES_READ_ENGAGEMENT}


def test_decision_order_not_available_beats_everything():
    d = evaluate_capability(Capability.CONTACT_INFORMATION, AuthContext.unauthenticated("facebook"),
                            target_is_own=False, now=NOW)
    assert d.status is FieldStatus.NOT_AVAILABLE


def test_decision_to_dict_is_json_safe():
    import json
    d = evaluate_capability(Capability.COMMENTS, ctx(AuthMode.INSTAGRAM_LOGIN, {C.IG_BUSINESS_BASIC}),
                            target_is_own=True, now=NOW)
    out = json.loads(json.dumps(d.to_dict()))
    assert out["status"] == "Permission not granted" and out["capability"] == "comments"
    assert out["doc"].startswith("https://developers.facebook.com/")


def test_every_rule_has_official_doc_link():
    for mode in AuthMode:
        for cap, rule in C.CAPABILITY_RULES[mode].items():
            assert rule.doc.startswith("https://developers.facebook.com/"), (mode, cap)


# --- token metadata, account type, own target -------------------------------------------------------
def test_token_metadata_expiry():
    t = TokenMetadata(expires_at=NOW + dt.timedelta(days=5), long_lived=True)
    assert not t.is_expired(NOW)
    assert t.seconds_remaining(NOW) == 5 * 86400
    assert t.expires_within(dt.timedelta(days=7), NOW) and not t.expires_within(dt.timedelta(days=1), NOW)
    assert t.to_dict()["expires_at"] == "2026-11-23T12:00:00Z"


def test_token_metadata_naive_datetimes_are_utc():
    t = TokenMetadata(expires_at=dt.datetime(2026, 11, 18, 11, 0))
    assert t.is_expired(NOW)


def test_token_without_expiry_and_data_access_expiry():
    assert not TokenMetadata().is_expired(NOW) and TokenMetadata().seconds_remaining(NOW) is None
    t = TokenMetadata(data_access_expires_at=NOW - dt.timedelta(days=1))
    assert t.is_expired(NOW)


@pytest.mark.parametrize("raw, expected", [
    ("BUSINESS", AccountType.BUSINESS), ("Media_Creator", AccountType.CREATOR),
    ("creator", AccountType.CREATOR), ("PERSONAL", AccountType.PERSONAL), (None, AccountType.UNKNOWN),
    ("other", AccountType.UNKNOWN),
])
def test_parse_account_type(raw, expected):
    assert C.parse_account_type(raw) is expected


def test_is_own_target_case_insensitive():
    c = ctx(AuthMode.INSTAGRAM_LOGIN)
    assert c.is_own_target("MyShop") and not c.is_own_target("nike") and not c.is_own_target(None)
    assert not AuthContext.unauthenticated("instagram").is_own_target("myshop")
