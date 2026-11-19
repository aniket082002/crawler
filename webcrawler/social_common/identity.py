"""Instagram usernames, profile URLs and link classification.

``classify_instagram_link(text)`` sorts any link into one of three kinds:

  profile        the URL of a profile itself, e.g. https://www.instagram.com/nike/
                 -> normalised to a lowercase username and one canonical URL
  rejected       an Instagram link that is NOT a profile URL (a post, reel,
                 story, explore page, login or help page, a profile tab, an app
                 link, a malformed URL, ...) -> carries a reason code
  not_instagram  anything else (other websites, e-mail links, plain text)

Everything here is a local FORMAT check. Nothing contacts Instagram, so a
"profile" result means "this link has the form of a profile URL", not "this
account exists". Which names Instagram reserves for its own pages cannot be
confirmed offline either: RESERVED_PATHS is a conservative list, and a link
is only accepted as a profile when it is unambiguous. Doubtful links are
rejected with a reason instead of being guessed into profiles.

Standard library only; no I/O.
"""
from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

PLATFORM = "instagram"
MAX_LINK_LENGTH = 2048
MAX_USERNAME_LENGTH = 30
# Letters, digits, periods and underscores; at most 30 characters.
USERNAME_RE = re.compile(r"[A-Za-z0-9._]{1,30}")
CANONICAL_PROFILE_URL = "https://www.instagram.com/{username}/"

# Hosts that serve profile pages at /<username>/.
PROFILE_HOSTS = frozenset({"instagram.com", "www.instagram.com", "m.instagram.com"})
# Domains that belong to Instagram. A link to any other host is "not_instagram".
INSTAGRAM_DOMAINS = ("instagram.com", "cdninstagram.com", "instagr.am", "ig.me")

# First path segments that are Instagram pages, not usernames.
RESERVED_PATHS = frozenset({
    "p", "reel", "reels", "tv", "stories", "explore", "accounts", "direct",
    "about", "developer", "developers", "legal", "privacy", "terms", "web",
    "api", "graphql", "oauth", "challenge", "emails", "session", "login",
    "logout", "signup", "help", "static", "locations", "tags", "topics",
    "ar", "s", "admin", "nametag", "press", "jobs", "blog", "download",
    "directory", "lite", "threads", "your_activity", "settings", "share",
    "invites",
})
# Second path segments that are views of a profile (/<username>/<tab>/).
PROFILE_SUBPAGES = frozenset({
    "tagged", "reels", "feed", "channel", "guides", "guide", "saved",
    "followers", "following", "igtv", "live",
})

_AUTHORITY_SCHEME_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://")
_SCHEME_RE = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]*):")


class LinkKind(str, enum.Enum):
    PROFILE = "profile"
    REJECTED = "rejected"
    NOT_INSTAGRAM = "not_instagram"


class RejectReason(str, enum.Enum):
    POST_URL = "post_url"
    REEL_URL = "reel_url"
    STORY_URL = "story_url"
    EXPLORE_URL = "explore_url"
    NON_PROFILE_PAGE = "non_profile_page"
    PROFILE_SUBPAGE = "profile_subpage"
    APP_LINK = "app_link"
    NOT_A_PROFILE_URL = "not_a_profile_url"
    NON_PROFILE_HOST = "non_profile_host"
    NO_USERNAME = "no_username"
    INVALID_USERNAME = "invalid_username"
    URL_HAS_CREDENTIALS = "url_has_credentials"
    UNEXPECTED_PORT = "unexpected_port"
    INVALID_URL = "invalid_url"
    TOO_LONG = "too_long"


REASON_TEXT = {
    RejectReason.POST_URL: "Post URL, not a profile",
    RejectReason.REEL_URL: "Reel URL, not a profile",
    RejectReason.STORY_URL: "Story or highlight URL, not a profile",
    RejectReason.EXPLORE_URL: "Explore, hashtag or location page, not a profile",
    RejectReason.NON_PROFILE_PAGE: "Instagram page (login, help, legal and similar), not a profile",
    RejectReason.PROFILE_SUBPAGE: "A tab or sub-page of a profile, not the profile URL itself",
    RejectReason.APP_LINK: "App or messaging link, not a profile URL",
    RejectReason.NOT_A_PROFILE_URL: "Instagram URL with extra path segments, not a profile URL",
    RejectReason.NON_PROFILE_HOST: "Instagram host that does not serve profile pages",
    RejectReason.NO_USERNAME: "Instagram URL without a username",
    RejectReason.INVALID_USERNAME: "Not a valid Instagram username",
    RejectReason.URL_HAS_CREDENTIALS: "URL contains an embedded user name or password",
    RejectReason.UNEXPECTED_PORT: "URL contains a port number",
    RejectReason.INVALID_URL: "Malformed or unsupported Instagram URL",
    RejectReason.TOO_LONG: f"Link is longer than {MAX_LINK_LENGTH} characters",
}


class IdentityError(ValueError):
    """A username or profile reference is not acceptable. The message is safe to show."""


# --- usernames --------------------------------------------------------------------
def username_problem(name: Any) -> str | None:
    """None if ``name`` is a well-formed Instagram username, else what is wrong."""
    if not isinstance(name, str) or not name:
        return "the username is empty"
    if len(name) > MAX_USERNAME_LENGTH:
        return f"usernames are at most {MAX_USERNAME_LENGTH} characters"
    if not USERNAME_RE.fullmatch(name):
        return "usernames may only contain letters, numbers, periods and underscores"
    if name.startswith(".") or name.endswith("."):
        return "usernames cannot start or end with a period"
    if ".." in name:
        return "usernames cannot contain consecutive periods"
    if not any(ch.isalnum() for ch in name):
        return "usernames must contain a letter or number"
    if name.lower() in RESERVED_PATHS:
        return "this is the name of an Instagram page, not a profile"
    return None


def normalize_username(name: Any) -> str:
    """Lowercase, validated username. Raises IdentityError."""
    text = name.strip() if isinstance(name, str) else name
    problem = username_problem(text)
    if problem:
        raise IdentityError(f"Not a valid Instagram username: {problem}.")
    return text.lower()


@dataclass(frozen=True)
class InstagramProfileRef:
    """A normalised reference to a profile: lowercase username, no '@'."""
    username: str

    def __post_init__(self):
        if username_problem(self.username) or self.username != self.username.lower():
            raise IdentityError("username must be a valid, lowercase Instagram username.")

    @property
    def handle(self) -> str:
        return f"@{self.username}"

    @property
    def profile_url(self) -> str:
        return CANONICAL_PROFILE_URL.format(username=self.username)

    @property
    def dedup_key(self) -> str:
        return f"{PLATFORM}:profile:{self.username}"


# --- link classification -------------------------------------------------------------
@dataclass(frozen=True)
class LinkClassification:
    kind: LinkKind
    profile: InstagramProfileRef | None = None
    reason: RejectReason | None = None
    detail: str = ""

    @property
    def is_profile(self) -> bool:
        return self.kind is LinkKind.PROFILE

    @property
    def reason_code(self) -> str:
        return self.reason.value if self.reason else ""

    @property
    def reason_text(self) -> str:
        if not self.reason:
            return ""
        text = REASON_TEXT[self.reason]
        return f"{text} ({self.detail})" if self.detail else text


_NOT_INSTAGRAM = LinkClassification(LinkKind.NOT_INSTAGRAM)


def _rejected(reason: RejectReason, detail: str = "") -> LinkClassification:
    return LinkClassification(LinkKind.REJECTED, None, reason, detail)


def is_instagram_host(host: str) -> bool:
    return any(host == d or host.endswith("." + d) for d in INSTAGRAM_DOMAINS)


def looks_like_instagram_link(text: Any) -> bool:
    """Cheap pre-filter: False means the text is certainly not an Instagram
    link, so the full classification can be skipped."""
    if not isinstance(text, str):
        return False
    low = text.lower()
    return "instagr" in low or "ig.me" in low


def _leading_host(text: str) -> str:
    """Host of a scheme-less string such as ``www.instagram.com/nike``."""
    authority = re.split(r"[/?#]", text, maxsplit=1)[0]
    host = authority.rsplit("@", 1)[-1].split(":", 1)[0]
    return host.rstrip(".").lower()


def classify_instagram_link(raw: Any) -> LinkClassification:
    """Classify one link. Never raises; never contacts anything."""
    if not isinstance(raw, str):
        return _NOT_INSTAGRAM
    text = raw.strip()
    if not text or not looks_like_instagram_link(text):
        return _NOT_INSTAGRAM
    if text.lower().startswith("instagram:"):
        return _rejected(RejectReason.APP_LINK, "instagram:// app link")

    candidate = text
    if candidate.startswith("//"):                      # protocol-relative
        candidate = "https:" + candidate
    elif not _AUTHORITY_SCHEME_RE.match(candidate):
        scheme = _SCHEME_RE.match(candidate)
        if scheme:
            # "mailto:", "javascript:", "tel:" ... are not web links. The one
            # exception is a scheme-less "host:port/path".
            rest = candidate[scheme.end():]
            if is_instagram_host(scheme.group(1).rstrip(".").lower()) and rest[:1].isdigit():
                candidate = "https://" + candidate
            else:
                return _NOT_INSTAGRAM
        elif is_instagram_host(_leading_host(candidate)):
            candidate = "https://" + candidate
        else:
            return _NOT_INSTAGRAM                       # relative path or plain text

    has_bad_chars = any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in text)
    try:
        parts = urlsplit(candidate)
        host = (parts.hostname or "").rstrip(".").lower()
    except ValueError:
        after_scheme = candidate.split("://", 1)[-1]
        return _rejected(RejectReason.INVALID_URL) if is_instagram_host(_leading_host(after_scheme)) \
            else _NOT_INSTAGRAM
    if not is_instagram_host(host):
        return _NOT_INSTAGRAM

    if len(text) > MAX_LINK_LENGTH:
        return _rejected(RejectReason.TOO_LONG)
    if has_bad_chars:
        return _rejected(RejectReason.INVALID_URL, "contains spaces or control characters")
    if parts.scheme.lower() not in ("http", "https"):
        return _rejected(RejectReason.INVALID_URL, "not an http(s) link")
    if "@" in parts.netloc:
        return _rejected(RejectReason.URL_HAS_CREDENTIALS)
    try:
        port = parts.port
    except ValueError:
        return _rejected(RejectReason.INVALID_URL, "invalid port")
    if port is not None:
        return _rejected(RejectReason.UNEXPECTED_PORT)
    if host not in PROFILE_HOSTS:
        if host == "ig.me" or host.endswith(".ig.me"):
            return _rejected(RejectReason.APP_LINK, "ig.me messaging link")
        return _rejected(RejectReason.NON_PROFILE_HOST, host)

    segments = [s for s in parts.path.split("/") if s]
    if not segments:
        return _rejected(RejectReason.NO_USERNAME)
    first = segments[0].lower()
    if first in ("p", "tv"):
        return _rejected(RejectReason.POST_URL)
    if first in ("reel", "reels"):
        return _rejected(RejectReason.REEL_URL)
    if first in ("stories", "s"):                       # /s/... are highlight share links
        return _rejected(RejectReason.STORY_URL)
    if first == "explore":
        return _rejected(RejectReason.EXPLORE_URL)
    if first == "_u":
        return _rejected(RejectReason.APP_LINK, "/_u/ app link")
    if first in RESERVED_PATHS:
        return _rejected(RejectReason.NON_PROFILE_PAGE, f"/{first}/")
    if len(segments) > 1:
        second = segments[1].lower()
        if second in ("p", "tv"):
            return _rejected(RejectReason.POST_URL)
        if second == "reel":
            return _rejected(RejectReason.REEL_URL)
        if second in PROFILE_SUBPAGES:
            return _rejected(RejectReason.PROFILE_SUBPAGE, f"/{second}/")
        return _rejected(RejectReason.NOT_A_PROFILE_URL)
    problem = username_problem(segments[0])
    if problem:
        return _rejected(RejectReason.INVALID_USERNAME, problem)
    return LinkClassification(LinkKind.PROFILE, InstagramProfileRef(first))


def parse_profile_reference(raw: Any) -> InstagramProfileRef:
    """Turn what a person types (``nike``, ``@nike`` or a profile URL) into a
    profile reference. Raises IdentityError with a message that is safe to show."""
    if not isinstance(raw, str) or not raw.strip():
        raise IdentityError("Enter an Instagram username or profile URL.")
    text = raw.strip()
    if len(text) > MAX_LINK_LENGTH:
        raise IdentityError(f"Input is too long (maximum {MAX_LINK_LENGTH} characters).")
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise IdentityError("Input must not contain spaces or control characters.")
    looks_like_url = ("/" in text or ":" in text
                      or is_instagram_host(text.rstrip(".").lower()))
    if looks_like_url:
        result = classify_instagram_link(text)
        if result.is_profile:
            return result.profile
        if result.kind is LinkKind.REJECTED:
            raise IdentityError(f"{result.reason_text}.")
        raise IdentityError("Only Instagram usernames or instagram.com profile URLs are supported.")
    if text.startswith("@"):
        text = text[1:]
        if text.startswith("@"):
            raise IdentityError("Use a single '@' before the username.")
    return InstagramProfileRef(normalize_username(text))
