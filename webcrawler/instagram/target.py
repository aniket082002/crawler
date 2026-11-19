"""Validate an Instagram target given as a username or a profile URL.

Accepted:  nike   @nike   https://www.instagram.com/nike/   https://instagram.com/nike
           (also m.instagram.com, http://, and the same without a scheme)
Rejected:  post (/p/), reel (/reel/, /reels/), TV, stories, explore, account
           and other non-profile paths; URLs with credentials, ports or other
           hosts; invalid usernames.

This is a purely local format check. It never contacts Instagram, so it
cannot tell whether an account exists, is private, or is a professional
account - later phases learn that from the official API.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from webcrawler.instagram.errors import TargetValidationError

MAX_INPUT_LENGTH = 2048
MAX_USERNAME_LENGTH = 30
# Letters, digits, periods and underscores; at most 30 characters.
USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")

INSTAGRAM_HOSTS = frozenset({"instagram.com", "www.instagram.com", "m.instagram.com"})
CANONICAL_PROFILE_URL = "https://www.instagram.com/{username}/"

# First path segments that are Instagram pages, not profiles. They are also
# refused as bare usernames, because a profile URL for them cannot exist.
RESERVED_PATHS = frozenset({
    "p", "reel", "reels", "tv", "stories", "explore", "accounts", "direct",
    "about", "developer", "developers", "legal", "privacy", "terms", "web",
    "api", "graphql", "oauth", "challenge", "emails", "session", "login",
    "logout", "signup", "help", "static", "locations", "tags", "topics",
    "ar", "s", "admin", "nametag", "press", "jobs", "blog", "download",
    "directory", "lite", "threads", "your_activity", "settings",
})
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")


@dataclass(frozen=True)
class InstagramTarget:
    username: str        # lowercase, without "@"
    input_type: str      # "username" | "handle" | "url"
    raw_input: str       # the input as given (trimmed)

    @property
    def handle(self) -> str:
        return f"@{self.username}"

    @property
    def profile_url(self) -> str:
        return CANONICAL_PROFILE_URL.format(username=self.username)

    def to_dict(self) -> dict:
        return {"username": self.username, "handle": self.handle,
                "profile_url": self.profile_url, "input_type": self.input_type}


def validate_username(name: str) -> str:
    """Return the normalized (lowercase) username or raise TargetValidationError."""
    if not name:
        raise TargetValidationError("Username cannot be empty.")
    if len(name) > MAX_USERNAME_LENGTH:
        raise TargetValidationError(
            f"Instagram usernames are at most {MAX_USERNAME_LENGTH} characters.")
    if not USERNAME_RE.fullmatch(name):
        raise TargetValidationError(
            "Instagram usernames may only contain letters, numbers, periods and underscores.")
    if name.startswith(".") or name.endswith("."):
        raise TargetValidationError("Instagram usernames cannot start or end with a period.")
    if ".." in name:
        raise TargetValidationError("Instagram usernames cannot contain consecutive periods.")
    if name.replace(".", "").replace("_", "") == "":
        raise TargetValidationError("Instagram usernames must contain a letter or number.")
    lowered = name.lower()
    if lowered in RESERVED_PATHS:
        raise TargetValidationError(f"'{name}' is an Instagram page name, not a profile.")
    return lowered


def _from_url(text: str) -> str:
    if not _SCHEME_RE.match(text):
        text = "https://" + text
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        raise TargetValidationError("Not a valid Instagram profile URL.") from None
    if parts.scheme.lower() not in ("http", "https"):
        raise TargetValidationError("Only http:// and https:// Instagram URLs are supported.")
    if "@" in parts.netloc or parts.username is not None or parts.password is not None:
        raise TargetValidationError("URLs with embedded usernames or passwords are not allowed.")
    if port is not None:
        raise TargetValidationError("Instagram profile URLs must not include a port number.")
    host = (parts.hostname or "").lower().rstrip(".")
    if host not in INSTAGRAM_HOSTS:
        raise TargetValidationError(
            "Only instagram.com profile URLs are supported "
            "(instagram.com, www.instagram.com or m.instagram.com).")

    segments = [s for s in parts.path.split("/") if s]
    if not segments:
        raise TargetValidationError("The URL does not contain a profile username.")
    first = segments[0].lower()
    if first in ("p", "tv"):
        raise TargetValidationError("Post URLs are not supported; enter a profile URL.")
    if first in ("reel", "reels"):
        raise TargetValidationError("Reel URLs are not supported; enter a profile URL.")
    if first == "explore":
        raise TargetValidationError("Explore URLs are not supported; enter a profile URL.")
    if first == "stories":
        raise TargetValidationError("Story URLs are not supported; enter a profile URL.")
    if first in RESERVED_PATHS:
        raise TargetValidationError("This Instagram URL is not a profile URL.")
    if len(segments) > 1:
        raise TargetValidationError(
            "Only the profile URL itself is supported (e.g. https://www.instagram.com/nike/), "
            "not a profile's tabs or posts.")
    # The query string (e.g. ?hl=en or share tracking) and fragment are ignored:
    # they do not change which profile is meant.
    return validate_username(segments[0])


def parse_target(raw: str) -> InstagramTarget:
    """Validate and normalize a username, @handle or profile URL."""
    if raw is None:
        raise TargetValidationError("Enter an Instagram username or profile URL.")
    text = str(raw).strip()
    if not text:
        raise TargetValidationError("Enter an Instagram username or profile URL.")
    if len(text) > MAX_INPUT_LENGTH:
        raise TargetValidationError(f"Input is too long (maximum {MAX_INPUT_LENGTH} characters).")
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise TargetValidationError("Input must not contain spaces or control characters.")

    looks_like_url = "/" in text or _SCHEME_RE.match(text) is not None or \
        text.lower().split("/")[0] in INSTAGRAM_HOSTS
    if looks_like_url:
        return InstagramTarget(_from_url(text), "url", text)
    if text.startswith("@"):
        name = text[1:]
        if name.startswith("@"):
            raise TargetValidationError("Use a single '@' before the username.")
        return InstagramTarget(validate_username(name), "handle", text)
    if ":" in text:
        raise TargetValidationError("Only Instagram usernames or instagram.com profile URLs are supported.")
    return InstagramTarget(validate_username(text), "username", text)
