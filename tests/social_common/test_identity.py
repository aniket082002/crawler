"""Instagram usernames, profile URLs and link classification (format checks only)."""
import pytest

from webcrawler.social_common import identity as I
from webcrawler.social_common.identity import (
    IdentityError, InstagramProfileRef, LinkKind, RejectReason, classify_instagram_link,
    looks_like_instagram_link, normalize_username, parse_profile_reference, username_problem,
)

IG = "https://www.instagram.com"

PROFILE_LINKS = [
    (f"{IG}/nike/", "nike"),
    ("https://instagram.com/nike", "nike"),
    ("http://www.instagram.com/nike/", "nike"),
    ("https://m.instagram.com/nike/", "nike"),
    ("https://WWW.INSTAGRAM.COM/Nike/", "nike"),                 # mixed case
    (f"{IG}/NIKE", "nike"),
    ("HTTPS://Instagram.com/NiKe/", "nike"),
    (f"{IG}/nike/?hl=en", "nike"),                               # query string ignored
    (f"{IG}/nike?igshid=abc123&utm_source=qr", "nike"),
    (f"{IG}/nike/#", "nike"),
    (f"{IG}//nike//", "nike"),
    ("//www.instagram.com/nike/", "nike"),                       # protocol-relative
    ("www.instagram.com/nike", "nike"),                          # no scheme
    ("instagram.com/nike/", "nike"),
    ("https://www.instagram.com./nike/", "nike"),                # trailing-dot host
    (f"  {IG}/nike/  ", "nike"),
    (f"{IG}/opositive.io", "opositive.io"),                      # period inside a username
    (f"{IG}/nike_run.club/", "nike_run.club"),
    (f"{IG}/_under_score_/", "_under_score_"),
    (f"{IG}/12345/", "12345"),
    (f"{IG}/" + "a" * 30, "a" * 30),
]

REJECTED_LINKS = [
    # posts
    (f"{IG}/p/CxYz123/", RejectReason.POST_URL),
    (f"{IG}/p/CxYz123", RejectReason.POST_URL),
    (f"{IG}/P/CxYz123/", RejectReason.POST_URL),
    (f"{IG}/tv/Bxyz/", RejectReason.POST_URL),
    (f"{IG}/nike/p/CxYz123/", RejectReason.POST_URL),            # newer post URL form
    # reels
    (f"{IG}/reel/Cabc999/", RejectReason.REEL_URL),
    (f"{IG}/reels/Cabc999/", RejectReason.REEL_URL),
    (f"{IG}/reels/", RejectReason.REEL_URL),
    (f"{IG}/nike/reel/Cabc999/", RejectReason.REEL_URL),
    # stories
    (f"{IG}/stories/nike/3141592653/", RejectReason.STORY_URL),
    (f"{IG}/stories/highlights/17890000/", RejectReason.STORY_URL),
    (f"{IG}/s/aGlnaGxpZ2h0OjE3", RejectReason.STORY_URL),
    # explore
    (f"{IG}/explore/", RejectReason.EXPLORE_URL),
    (f"{IG}/explore/tags/running/", RejectReason.EXPLORE_URL),
    (f"{IG}/explore/locations/212988663/new-york/", RejectReason.EXPLORE_URL),
    # other Instagram pages
    (f"{IG}/accounts/login/", RejectReason.NON_PROFILE_PAGE),
    (f"{IG}/accounts/login/?next=/nike/", RejectReason.NON_PROFILE_PAGE),
    (f"{IG}/direct/inbox/", RejectReason.NON_PROFILE_PAGE),
    (f"{IG}/about/us/", RejectReason.NON_PROFILE_PAGE),
    (f"{IG}/legal/terms/", RejectReason.NON_PROFILE_PAGE),
    (f"{IG}/developer/", RejectReason.NON_PROFILE_PAGE),
    (f"{IG}/challenge/", RejectReason.NON_PROFILE_PAGE),
    (f"{IG}/share/BAxyz", RejectReason.NON_PROFILE_PAGE),
    (f"{IG}/invites/contact/?i=abc", RejectReason.NON_PROFILE_PAGE),
    (f"{IG}/directory/profiles/", RejectReason.NON_PROFILE_PAGE),
    (f"{IG}/help", RejectReason.NON_PROFILE_PAGE),
    # profile tabs and deeper paths
    (f"{IG}/nike/tagged/", RejectReason.PROFILE_SUBPAGE),
    (f"{IG}/nike/reels/", RejectReason.PROFILE_SUBPAGE),
    (f"{IG}/nike/followers/", RejectReason.PROFILE_SUBPAGE),
    (f"{IG}/nike/guides/", RejectReason.PROFILE_SUBPAGE),
    (f"{IG}/nike/something/", RejectReason.NOT_A_PROFILE_URL),
    (f"{IG}/nike/a/b/c", RejectReason.NOT_A_PROFILE_URL),
    # app and messaging links
    ("instagram://user?username=nike", RejectReason.APP_LINK),
    ("INSTAGRAM://media?id=1", RejectReason.APP_LINK),
    (f"{IG}/_u/nike/", RejectReason.APP_LINK),
    ("https://ig.me/m/nike", RejectReason.APP_LINK),
    # Instagram hosts that do not serve profiles
    ("https://help.instagram.com/581066165581870", RejectReason.NON_PROFILE_HOST),
    ("https://about.instagram.com/blog", RejectReason.NON_PROFILE_HOST),
    ("https://business.instagram.com/", RejectReason.NON_PROFILE_HOST),
    ("https://l.instagram.com/?u=https%3A%2F%2Fexample.com", RejectReason.NON_PROFILE_HOST),
    ("https://scontent.cdninstagram.com/v/t51/photo.jpg", RejectReason.NON_PROFILE_HOST),
    ("https://instagr.am/nike", RejectReason.NON_PROFILE_HOST),
    ("https://www.instagr.am/p/abc/", RejectReason.NON_PROFILE_HOST),
    # no username
    (f"{IG}/", RejectReason.NO_USERNAME),
    ("https://instagram.com", RejectReason.NO_USERNAME),
    ("instagram.com", RejectReason.NO_USERNAME),
    (f"{IG}/?hl=en", RejectReason.NO_USERNAME),
    (f"{IG}/#", RejectReason.NO_USERNAME),
    # invalid usernames
    (f"{IG}/ni-ke/", RejectReason.INVALID_USERNAME),
    (f"{IG}/@nike/", RejectReason.INVALID_USERNAME),
    (f"{IG}/nike%20/", RejectReason.INVALID_USERNAME),
    (f"{IG}/n%69ke/", RejectReason.INVALID_USERNAME),
    (f"{IG}/.nike/", RejectReason.INVALID_USERNAME),
    (f"{IG}/nike.", RejectReason.INVALID_USERNAME),
    (f"{IG}/ni..ke/", RejectReason.INVALID_USERNAME),
    (f"{IG}/___/", RejectReason.INVALID_USERNAME),
    (f"{IG}/" + "a" * 31, RejectReason.INVALID_USERNAME),
    (f"{IG}/nike!", RejectReason.INVALID_USERNAME),
    (f"{IG}/n\u00edke/", RejectReason.INVALID_USERNAME),
    (f"{IG}/nike;jsessionid=1", RejectReason.INVALID_USERNAME),
    # credentials, ports, malformed
    ("https://user:pass@www.instagram.com/nike/", RejectReason.URL_HAS_CREDENTIALS),
    ("https://user@instagram.com/nike", RejectReason.URL_HAS_CREDENTIALS),
    ("user@instagram.com/nike", RejectReason.URL_HAS_CREDENTIALS),
    (f"{IG}:443/nike/", RejectReason.UNEXPECTED_PORT),
    (f"{IG}:8443/nike/", RejectReason.UNEXPECTED_PORT),
    ("www.instagram.com:443/nike", RejectReason.UNEXPECTED_PORT),
    (f"{IG}:abc/nike/", RejectReason.INVALID_URL),
    (f"{IG}/ni ke/", RejectReason.INVALID_URL),
    (f"{IG}/ni\tke", RejectReason.INVALID_URL),
    (f"{IG}/ni\nke/", RejectReason.INVALID_URL),
    (f"{IG}/nike\x00", RejectReason.INVALID_URL),
    ("ftp://www.instagram.com/nike/", RejectReason.INVALID_URL),
    (f"{IG}/nike/?" + "a" * 2100, RejectReason.TOO_LONG),
]

NOT_INSTAGRAM = [
    "", "   ", None, 123, 4.5, True, ["https://instagram.com/nike"], {"url": "x"}, b"https://instagram.com/nike",
    "https://example.com/",
    "https://example.com/blog/instagram-tips",
    "https://example.com/?u=https://instagram.com/nike",
    "https://www.facebook.com/sharer.php?u=https%3A%2F%2Finstagram.com%2Fnike",
    "https://l.facebook.com/l.php?u=https://instagram.com/nike",
    "https://instagram.com.evil.example/nike",
    "https://evilinstagram.com/nike",
    "https://notinstagram.com/nike",
    "https://instagram.co/nike",
    "https://big.me/nike",
    "https://xn--nstagram-abc.com/nike",
    "mailto:press@instagram.com",
    "javascript:window.open('https://instagram.com/nike')",
    "tel:+1-instagram",
    "data:text/plain,instagram.com/nike",
    "/relative/instagram/path",
    "../instagram.com/nike",
    "Follow us on instagram.com/nike today",
    "https://[instagram.com/nike",
    "#instagram",
]


@pytest.mark.parametrize("link, username", PROFILE_LINKS)
def test_profile_links_are_normalised(link, username):
    result = classify_instagram_link(link)
    assert result.kind is LinkKind.PROFILE and result.is_profile
    assert result.profile.username == username
    assert result.profile.profile_url == f"https://www.instagram.com/{username}/"
    assert result.reason is None and result.reason_code == "" and result.reason_text == ""


@pytest.mark.parametrize("link, reason", REJECTED_LINKS)
def test_non_profile_instagram_links_are_rejected_with_a_reason(link, reason):
    result = classify_instagram_link(link)
    assert result.kind is LinkKind.REJECTED and not result.is_profile
    assert result.profile is None
    assert result.reason is reason and result.reason_code == reason.value
    assert result.reason_text


@pytest.mark.parametrize("link", NOT_INSTAGRAM)
def test_other_links_are_not_instagram(link):
    result = classify_instagram_link(link)
    assert result.kind is LinkKind.NOT_INSTAGRAM
    assert result.profile is None and result.reason is None


def test_all_spellings_of_one_profile_share_a_key():
    spellings = ["https://www.instagram.com/Nike/", "http://instagram.com/nike", "https://m.instagram.com/NIKE?hl=en",
                 "instagram.com/nIkE/", "//www.instagram.com/nike/#x"]
    keys = {classify_instagram_link(s).profile.dedup_key for s in spellings}
    assert keys == {"instagram:profile:nike"}


def test_prefilter_never_hides_an_instagram_link():
    """looks_like_instagram_link may say yes too often, but never no for a real one."""
    for link, _ in PROFILE_LINKS + REJECTED_LINKS:
        assert looks_like_instagram_link(link), link
    assert not looks_like_instagram_link("https://example.com/") and not looks_like_instagram_link(None)


def test_every_reason_has_a_text():
    assert set(I.REASON_TEXT) == set(RejectReason)
    assert all(text.strip() for text in I.REASON_TEXT.values())


def test_rejection_detail_is_included():
    assert "help.instagram.com" in classify_instagram_link("https://help.instagram.com/1").reason_text
    assert "/accounts/" in classify_instagram_link(f"{IG}/accounts/login/").reason_text


@pytest.mark.parametrize("junk", [object(), b"\xff\xfe", "\x00" * 50, "instagram" * 1000, "https://" + "a" * 5000,
                                  "instagram.com/" + "\ud83d", "http://instagram.com:99999999999999999999/x",
                                  "instagram.com:", "//", "://instagram.com/nike", "ig.me", "instagr"])
def test_classification_never_raises(junk):
    assert classify_instagram_link(junk).kind in set(LinkKind)


# --- usernames --------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["nike", "Nike", "nike_official", "nike.running", "n", "_nike_", "a1.b2_c3",
                                  "x" * 30, "12345", "opositive.io"])
def test_valid_usernames(name):
    assert username_problem(name) is None
    assert normalize_username(name) == name.lower()


@pytest.mark.parametrize("name, fragment", [
    ("", "empty"), (None, "empty"), (5, "empty"),
    ("x" * 31, "at most 30"),
    ("ni-ke", "letters, numbers, periods and underscores"),
    ("nike!", "letters, numbers, periods and underscores"),
    ("n\u00edke", "letters, numbers, periods and underscores"),
    ("ni ke", "letters, numbers, periods and underscores"),
    ("ni\nke", "letters, numbers, periods and underscores"),
    (".nike", "start or end with a period"),
    ("nike.", "start or end with a period"),
    ("ni..ke", "consecutive periods"),
    ("___", "letter or number"),
    ("explore", "Instagram page"),
    ("P", "Instagram page"),
    ("Accounts", "Instagram page"),
])
def test_invalid_usernames(name, fragment):
    assert fragment in username_problem(name)
    with pytest.raises(IdentityError):
        normalize_username(name)


def test_normalize_username_trims_and_lowercases():
    assert normalize_username("  Nike_Run.Club ") == "nike_run.club"


@pytest.mark.parametrize("name", ["nike\n", "\nnike", "nike\r\n", "nike "])
def test_exact_check_does_not_accept_surrounding_whitespace(name):
    """username_problem checks the text exactly as given (a regex ending in "$"
    would wrongly accept "nike\\n"); only normalize_username trims first."""
    assert username_problem(name) is not None
    assert normalize_username(name) == "nike"


def test_profile_reference_object():
    ref = InstagramProfileRef("nike")
    assert (ref.handle, ref.profile_url, ref.dedup_key) == (
        "@nike", "https://www.instagram.com/nike/", "instagram:profile:nike")
    assert ref == InstagramProfileRef("nike") and len({ref, InstagramProfileRef("nike")}) == 1


@pytest.mark.parametrize("bad", ["Nike", "", "p", "ni ke", "nike.", "x" * 31, None])
def test_profile_reference_must_be_normalised(bad):
    with pytest.raises(IdentityError):
        InstagramProfileRef(bad)


def test_identity_error_is_a_value_error():
    assert issubclass(IdentityError, ValueError)


# --- what a person types ------------------------------------------------------------------
@pytest.mark.parametrize("typed", ["@nike", "nike", "NIKE", "  @Nike  ", f"{IG}/nike/", "https://instagram.com/nike/",
                                   f"{IG}/nike", "instagram.com/Nike"])
def test_parse_profile_reference(typed):
    assert parse_profile_reference(typed).username == "nike"


def test_parse_profile_reference_keeps_periods():
    assert parse_profile_reference("@opositive.io").username == "opositive.io"
    assert parse_profile_reference("nike.com").username == "nike.com"      # a username, not a website


@pytest.mark.parametrize("typed, fragment", [
    ("", "Enter an Instagram username"), ("   ", "Enter an Instagram username"), (None, "Enter an Instagram username"),
    ("@", "Not a valid Instagram username"), ("@@nike", "single '@'"), ("nike shoes", "spaces or control"),
    ("nike\x00", "spaces or control"), ("x" * 3000, "too long"),
    (f"{IG}/p/CxYz/", "Post URL"), (f"{IG}/reel/Cabc/", "Reel URL"), (f"{IG}/explore/", "Explore"),
    (f"{IG}/stories/nike/1/", "Story"), ("instagram.com", "without a username"),
    ("https://evil.example/nike", "Only Instagram usernames"), ("javascript:alert(1)", "Only Instagram usernames"),
    ("https://user:pw@instagram.com/nike", "user name or password"), ("explore", "Instagram page"),
    ("ni-ke", "letters, numbers"),
])
def test_parse_profile_reference_errors(typed, fragment):
    with pytest.raises(IdentityError) as error:
        parse_profile_reference(typed)
    assert fragment in str(error.value)
