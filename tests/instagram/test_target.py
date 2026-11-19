"""Username / profile URL validation (offline, format checks only)."""
import pytest

from webcrawler.instagram.errors import TargetValidationError
from webcrawler.instagram.target import (
    InstagramTarget, MAX_INPUT_LENGTH, parse_target, validate_username,
)


@pytest.mark.parametrize("raw, input_type", [
    ("@nike", "handle"),
    ("nike", "username"),
    ("https://www.instagram.com/nike/", "url"),
    ("https://instagram.com/nike/", "url"),
    ("https://www.instagram.com/nike", "url"),
    ("  @nike  ", "handle"),
    ("NIKE", "username"),
    ("https://WWW.Instagram.COM/Nike/", "url"),
    ("http://www.instagram.com/nike/", "url"),
    ("https://m.instagram.com/nike/", "url"),
    ("www.instagram.com/nike/", "url"),
    ("instagram.com/nike", "url"),
    ("https://www.instagram.com/nike/?hl=en", "url"),
    ("https://www.instagram.com/nike/#top", "url"),
    ("https://www.instagram.com//nike//", "url"),
])
def test_accepted_inputs_normalize_to_nike(raw, input_type):
    t = parse_target(raw)
    assert t.username == "nike"
    assert t.input_type == input_type
    assert t.handle == "@nike"
    assert t.profile_url == "https://www.instagram.com/nike/"


@pytest.mark.parametrize("name", [
    "nike", "nike_official", "nike.running", "n", "_nike_", "a1.b2_c3", "x" * 30, "12345", "nike.com",
])
def test_valid_usernames(name):
    assert parse_target(name).username == name.lower()


@pytest.mark.parametrize("raw, message", [
    ("", "Enter an Instagram username"),
    ("   ", "Enter an Instagram username"),
    (None, "Enter an Instagram username"),
    ("x" * 31, "at most 30"),
    ("nike!", "letters, numbers, periods and underscores"),
    ("ni-ke", "letters, numbers, periods and underscores"),
    ("níke", "letters, numbers, periods and underscores"),
    ("nike shoes", "spaces or control characters"),
    ("nike\n", None),  # trailing newline is trimmed -> valid; checked separately
    ("nike\x00", "spaces or control characters"),
    (".nike", "start or end with a period"),
    ("nike.", "start or end with a period"),
    ("ni..ke", "consecutive periods"),
    ("___", "must contain a letter or number"),
    ("@", "Username cannot be empty"),
    ("@@nike", "single '@'"),
    ("explore", "page name, not a profile"),
    ("p", "page name, not a profile"),
    ("javascript:alert(1)", "Only Instagram usernames"),
    ("mailto:nike", "Only Instagram usernames"),
])
def test_invalid_usernames(raw, message):
    if message is None:
        assert parse_target(raw).username == "nike"
        return
    with pytest.raises(TargetValidationError) as exc:
        parse_target(raw)
    assert message in str(exc.value)


@pytest.mark.parametrize("raw, message", [
    ("https://www.instagram.com/p/C0abcDEF123/", "Post URLs"),
    ("https://www.instagram.com/tv/C0abcDEF123/", "Post URLs"),
    ("https://www.instagram.com/reel/C0abcDEF123/", "Reel URLs"),
    ("https://www.instagram.com/reels/C0abcDEF123/", "Reel URLs"),
    ("https://www.instagram.com/explore/", "Explore URLs"),
    ("https://www.instagram.com/explore/tags/nike/", "Explore URLs"),
    ("https://www.instagram.com/stories/nike/123/", "Story URLs"),
    ("https://www.instagram.com/accounts/login/", "not a profile URL"),
    ("https://www.instagram.com/direct/inbox/", "not a profile URL"),
    ("https://www.instagram.com/nike/reels/", "not a profile's tabs or posts"),
    ("https://www.instagram.com/nike/p/C0abc/", "not a profile's tabs or posts"),
    ("https://www.instagram.com/nike/tagged/", "not a profile's tabs or posts"),
    ("https://www.instagram.com/", "does not contain a profile username"),
    ("https://instagram.com", "does not contain a profile username"),
    ("instagram.com", "does not contain a profile username"),
    ("https://user:pass@www.instagram.com/nike/", "embedded usernames or passwords"),
    ("https://user@www.instagram.com/nike/", "embedded usernames or passwords"),
    ("https://www.instagram.com:8443/nike/", "port number"),
    ("https://www.instagram.com:abc/nike/", "Not a valid Instagram profile URL"),
    ("https://evil.example/nike/", "Only instagram.com profile URLs"),
    ("https://instagram.com.evil.example/nike/", "Only instagram.com profile URLs"),
    ("https://evilinstagram.com/nike/", "Only instagram.com profile URLs"),
    ("https://www.facebook.com/nike/", "Only instagram.com profile URLs"),
    ("https://instagr.am/nike/", "Only instagram.com profile URLs"),
    ("ftp://www.instagram.com/nike/", "Only http:// and https://"),
    ("file:///etc/passwd", "Only http:// and https://"),
    ("https://www.instagram.com/ni-ke/", "letters, numbers, periods and underscores"),
    ("https://www.instagram.com/" + "x" * 31 + "/", "at most 30"),
])
def test_rejected_urls(raw, message):
    with pytest.raises(TargetValidationError) as exc:
        parse_target(raw)
    assert message in str(exc.value)


def test_overlong_input_rejected():
    with pytest.raises(TargetValidationError, match="too long"):
        parse_target("https://www.instagram.com/" + "a" * MAX_INPUT_LENGTH)


def test_validation_error_is_a_value_error():
    with pytest.raises(ValueError):
        parse_target("https://www.instagram.com/p/abc/")


def test_validate_username_lowercases():
    assert validate_username("Nike_Run.Club") == "nike_run.club"


def test_target_keeps_raw_input_and_is_immutable():
    t = parse_target(" https://instagram.com/Nike ")
    assert t.raw_input == "https://instagram.com/Nike"
    assert t.to_dict() == {"username": "nike", "handle": "@nike",
                           "profile_url": "https://www.instagram.com/nike/", "input_type": "url"}
    with pytest.raises(Exception):
        t.username = "other"  # frozen dataclass
    assert isinstance(t, InstagramTarget)


def test_validation_is_offline():
    # conftest blocks sockets/DNS/urlopen; validating many inputs must not try to use them
    for raw in ("@nike", "https://www.instagram.com/adidas/", "puma"):
        parse_target(raw)


@pytest.mark.parametrize("name", ["nike\n", "nike\r\n", "\nnike"])
def test_validate_username_rejects_embedded_newlines(name):
    # Regression: "$" in a regex also matches before a trailing newline.
    with pytest.raises(TargetValidationError):
        validate_username(name)
