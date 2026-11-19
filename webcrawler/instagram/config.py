"""Configuration and authentication-capability model for the Instagram module.

PHASE 1: structure and validation only. Nothing here performs a login, an
OAuth redirect, a token exchange, a token refresh or any network call.

Two authentication modes are modelled as SEPARATE configurations, because
Meta treats them as separate setups with their own app credentials, host,
permission (scope) names and token flows, and they expose different data:

  * Instagram Login  -> graph.instagram.com, Instagram app ID/secret,
                        instagram_business_* scopes, no Facebook Page needed.
  * Facebook Login   -> graph.facebook.com, Meta app ID/secret,
                        instagram_* + pages_* scopes, Instagram professional
                        account linked to a Facebook Page.

Meta's App Review page states an app can use Facebook Login or Instagram
Login, but not both, so each mode reads its own environment variables.

Secrets are wrapped in Secret (repr/str print "[REDACTED]") and registered
with the module redactor as soon as they are loaded.
"""
from __future__ import annotations

import datetime as _dt
import enum
import hmac
import os
import re
from dataclasses import dataclass, field
from typing import Mapping

from webcrawler.instagram.errors import ConfigError
from webcrawler.instagram.redaction import REDACTED, Redactor, default_redactor

# --- environment variable names (values are never hardcoded) ----------------
ENV_AUTH_MODE = "IG_AUTH_MODE"
ENV_API_VERSION = "IG_GRAPH_API_VERSION"

ENV_IG_LOGIN_APP_ID = "IG_LOGIN_APP_ID"
ENV_IG_LOGIN_APP_SECRET = "IG_LOGIN_APP_SECRET"
ENV_IG_LOGIN_ACCESS_TOKEN = "IG_LOGIN_ACCESS_TOKEN"

ENV_FB_LOGIN_APP_ID = "FB_LOGIN_APP_ID"
ENV_FB_LOGIN_APP_SECRET = "FB_LOGIN_APP_SECRET"
ENV_FB_LOGIN_ACCESS_TOKEN = "FB_LOGIN_ACCESS_TOKEN"

# Latest Graph API version per Meta's changelog when this was written
# (v27.0, 2026-11-04): https://developers.facebook.com/docs/graph-api/changelog
DEFAULT_API_VERSION = "v27.0"
API_VERSION_RE = re.compile(r"^v\d{1,3}\.\d{1,2}$")
APP_ID_RE = re.compile(r"^\d{1,32}$")
# Meta app secrets (Meta app and Instagram app alike) are 32 hexadecimal
# characters, as shown in App Dashboard > App settings > Basic (finding L7).
APP_SECRET_RE = re.compile(r"[0-9a-fA-F]{32}")
MIN_TOKEN_LENGTH = 20
MAX_SECRET_LENGTH = 4096

DOCS_BASE = "https://developers.facebook.com/documentation/instagram-platform"


# --- secrets ------------------------------------------------------------------
class Secret:
    """A secret string that never shows its value in repr/str/format/logs.
    Call reveal() only at the point the value is actually needed."""

    __slots__ = ("_value",)

    def __init__(self, value: str):
        if not isinstance(value, str):
            raise TypeError("Secret value must be a string")
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return f"Secret({REDACTED})"

    def __str__(self) -> str:
        return REDACTED

    def __format__(self, spec: str) -> str:
        return REDACTED

    def __bool__(self) -> bool:
        return bool(self._value)

    def __eq__(self, other) -> bool:
        # constant-time comparison (audit finding I4)
        return isinstance(other, Secret) and hmac.compare_digest(
            other._value.encode("utf-8"), self._value.encode("utf-8"))

    def __hash__(self) -> int:
        return hash(("Secret", self._value))

    # copy/deepcopy (e.g. dataclasses.asdict) keep the same wrapped object, so
    # the value stays hidden behind Secret([REDACTED]) instead of being exposed.
    def __copy__(self) -> "Secret":
        return self

    def __deepcopy__(self, memo) -> "Secret":
        return self

    def __reduce__(self):
        raise TypeError("Secret objects cannot be pickled")


# --- authentication modes -----------------------------------------------------
class AuthMode(str, enum.Enum):
    INSTAGRAM_LOGIN = "instagram"
    FACEBOOK_LOGIN = "facebook"


@dataclass(frozen=True)
class ModeSpec:
    mode: AuthMode
    label: str
    graph_host: str
    env_app_id: str
    env_app_secret: str
    env_access_token: str
    token_kind: str
    long_lived_token_days: int
    refresh_endpoint_available: bool
    requires_facebook_page: bool
    setup_docs: str

    @property
    def graph_base_url(self) -> str:
        return f"https://{self.graph_host}"


MODE_SPECS: dict[AuthMode, ModeSpec] = {
    AuthMode.INSTAGRAM_LOGIN: ModeSpec(
        mode=AuthMode.INSTAGRAM_LOGIN,
        label="Instagram Login (Business Login for Instagram)",
        graph_host="graph.instagram.com",
        env_app_id=ENV_IG_LOGIN_APP_ID,
        env_app_secret=ENV_IG_LOGIN_APP_SECRET,
        env_access_token=ENV_IG_LOGIN_ACCESS_TOKEN,
        token_kind="Instagram User access token",
        long_lived_token_days=60,
        refresh_endpoint_available=True,   # GET /refresh_access_token (ig_refresh_token)
        requires_facebook_page=False,
        setup_docs=f"{DOCS_BASE}/instagram-api-with-instagram-login/business-login",
    ),
    AuthMode.FACEBOOK_LOGIN: ModeSpec(
        mode=AuthMode.FACEBOOK_LOGIN,
        label="Facebook Login for Business",
        graph_host="graph.facebook.com",
        env_app_id=ENV_FB_LOGIN_APP_ID,
        env_app_secret=ENV_FB_LOGIN_APP_SECRET,
        env_access_token=ENV_FB_LOGIN_ACCESS_TOKEN,
        token_kind="Facebook User access token",
        long_lived_token_days=60,
        refresh_endpoint_available=False,  # re-login when a long-lived user token expires
        requires_facebook_page=True,
        setup_docs=f"{DOCS_BASE}/instagram-api-with-facebook-login/business-login-for-instagram",
    ),
}

MODE_ALIASES = {
    "instagram": AuthMode.INSTAGRAM_LOGIN,
    "instagram_login": AuthMode.INSTAGRAM_LOGIN,
    "instagram-login": AuthMode.INSTAGRAM_LOGIN,
    "ig": AuthMode.INSTAGRAM_LOGIN,
    "facebook": AuthMode.FACEBOOK_LOGIN,
    "facebook_login": AuthMode.FACEBOOK_LOGIN,
    "facebook-login": AuthMode.FACEBOOK_LOGIN,
    "fb": AuthMode.FACEBOOK_LOGIN,
}

ALLOWED_GRAPH_HOSTS = frozenset(spec.graph_host for spec in MODE_SPECS.values())


def parse_auth_mode(value) -> AuthMode:
    if isinstance(value, AuthMode):
        return value
    key = str(value or "").strip().lower()
    if key not in MODE_ALIASES:
        raise ConfigError(
            f"{ENV_AUTH_MODE} must be 'instagram' or 'facebook'.", invalid=(ENV_AUTH_MODE,))
    return MODE_ALIASES[key]


# --- configuration ------------------------------------------------------------
@dataclass(frozen=True)
class InstagramConfig:
    mode: AuthMode
    api_version: str
    access_token: Secret | None = field(default=None, repr=False)
    app_id: str | None = None
    app_secret: Secret | None = field(default=None, repr=False)

    @property
    def spec(self) -> ModeSpec:
        return MODE_SPECS[self.mode]

    @property
    def graph_base_url(self) -> str:
        return self.spec.graph_base_url

    @property
    def has_access_token(self) -> bool:
        return bool(self.access_token)

    def missing_for_token_exchange(self) -> tuple[str, ...]:
        """Variable names a later phase would need for token exchange/refresh."""
        missing = []
        if not self.app_id:
            missing.append(self.spec.env_app_id)
        if not self.app_secret:
            missing.append(self.spec.env_app_secret)
        return tuple(missing)

    def describe(self) -> dict:
        """Safe summary for logs/reports: says WHETHER secrets are set, never what they are."""
        spec = self.spec
        return {
            "auth_mode": self.mode.value,
            "auth_mode_label": spec.label,
            "graph_host": spec.graph_host,
            "api_version": self.api_version,
            "app_id_set": bool(self.app_id),
            "app_secret_set": bool(self.app_secret),
            "access_token_set": bool(self.access_token),
            "variables": {
                "app_id": spec.env_app_id,
                "app_secret": spec.env_app_secret,
                "access_token": spec.env_access_token,
            },
        }

    def __repr__(self) -> str:
        return (f"InstagramConfig(mode={self.mode.value!r}, api_version={self.api_version!r}, "
                f"app_id={'set' if self.app_id else 'missing'}, "
                f"app_secret={'set' if self.app_secret else 'missing'}, "
                f"access_token={'set' if self.access_token else 'missing'})")

    __str__ = __repr__


def _clean(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name)
    return value.strip() if isinstance(value, str) else ""


def access_token_problem(value) -> str | None:
    """Shared access-token check used by load_config() AND GraphApiClient.

    Returns None if the token is acceptable, otherwise a short reason that
    never contains the value. Tokens must be MIN_TOKEN_LENGTH..MAX_SECRET_LENGTH
    printable ASCII characters (0x21-0x7E): no spaces, CR/LF, tabs, other
    control characters or non-ASCII text, so a token can never break an
    HTTP header line.
    """
    if hasattr(value, "reveal"):
        value = value.reveal()
    if not isinstance(value, str) or not value:
        return "token is missing or not a string"
    if len(value) < MIN_TOKEN_LENGTH:
        return f"token is shorter than {MIN_TOKEN_LENGTH} characters"
    if len(value) > MAX_SECRET_LENGTH:
        return f"token is longer than {MAX_SECRET_LENGTH} characters"
    if any(not (0x21 <= ord(ch) <= 0x7E) for ch in value):
        return "token contains whitespace, control or non-ASCII characters"
    return None


def load_config(environ: Mapping[str, str] | None = None, *, require_token: bool = True,
                redactor: Redactor | None = None) -> InstagramConfig:
    """Read and validate configuration from environment variables.

    Only the variables of the selected mode are read. Errors name variables,
    never values. Loaded secrets are registered with the redactor immediately,
    so they are masked in any later log line or exception message.
    """
    environ = os.environ if environ is None else environ
    redactor = redactor or default_redactor

    raw_mode = _clean(environ, ENV_AUTH_MODE)
    if not raw_mode:
        raise ConfigError(
            f"{ENV_AUTH_MODE} is not set. Set it to 'instagram' (Instagram Login) "
            f"or 'facebook' (Facebook Login).", missing=(ENV_AUTH_MODE,))
    mode = parse_auth_mode(raw_mode)
    spec = MODE_SPECS[mode]

    missing: list[str] = []
    invalid: list[str] = []

    version = _clean(environ, ENV_API_VERSION) or DEFAULT_API_VERSION
    if not API_VERSION_RE.fullmatch(version):
        invalid.append(ENV_API_VERSION)

    token_value = _clean(environ, spec.env_access_token)
    secret_value = _clean(environ, spec.env_app_secret)
    app_id = _clean(environ, spec.env_app_id)

    # Register before any validation message is built (defence in depth).
    # Always also with the shared logging redactor (finding R7), even when the
    # caller supplied a separate redactor that does not share its secrets.
    for value in (token_value, secret_value):
        if value:
            redactor.add_secret(value)
            if redactor is not default_redactor:
                default_redactor.add_secret(value)

    if not token_value:
        if require_token:
            missing.append(spec.env_access_token)
    elif access_token_problem(token_value):
        invalid.append(spec.env_access_token)

    if secret_value and not APP_SECRET_RE.fullmatch(secret_value):
        invalid.append(spec.env_app_secret)
    if app_id and not APP_ID_RE.fullmatch(app_id):
        invalid.append(spec.env_app_id)

    if missing or invalid:
        parts = []
        if missing:
            parts.append("missing: " + ", ".join(missing))
        if invalid:
            parts.append("invalid: " + ", ".join(invalid))
        raise ConfigError(f"Instagram configuration for {spec.label} is incomplete "
                          f"({'; '.join(parts)}).", missing=tuple(missing), invalid=tuple(invalid))

    return InstagramConfig(
        mode=mode,
        api_version=version,
        access_token=Secret(token_value) if token_value else None,
        app_id=app_id or None,
        app_secret=Secret(secret_value) if secret_value else None,
    )


def docker_env_passthrough(mode) -> list[str]:
    """`docker run` arguments that pass the needed variables BY NAME ONLY
    (``-e NAME``), so Docker reads the values from the launcher's own
    environment and no secret ever appears in the command line or its log.
    The app secret is deliberately not passed: extraction never needs it."""
    spec = MODE_SPECS[parse_auth_mode(mode)]
    names = (ENV_AUTH_MODE, ENV_API_VERSION, spec.env_access_token)
    args: list[str] = []
    for name in names:
        args += ["-e", name]
    return args


# =============================================================================
# Authentication capability model
# =============================================================================
class FieldStatus(str, enum.Enum):
    """Status of a field or capability. The non-AVAILABLE values are the exact
    labels written into outputs in place of data that could not be obtained."""
    AVAILABLE = "Available"
    # Value obtained but altered before output (audit finding I1/I2): it is
    # never presented as a complete, verbatim value.
    AVAILABLE_REDACTED = "Available (redacted)"   # part of the value was masked as a secret
    AVAILABLE_MODIFIED = "Available (modified)"   # e.g. too deeply nested, non-finite number
    NOT_AVAILABLE = "Not available"               # the API offers no such data here
    NOT_ACCESSIBLE = "Not accessible"             # exists, but not for this account/target
    PERMISSION_NOT_GRANTED = "Permission not granted"  # a scope/feature is missing
    AUTHENTICATION_REQUIRED = "Authentication required"  # no valid token


class Capability(str, enum.Enum):
    OWN_PROFILE = "own_profile"
    OWN_MEDIA = "own_media"
    MEDIA_PRODUCT_TYPE = "media_product_type"   # FEED / REELS / STORY classification
    STORIES = "stories"
    COMMENTS = "comments"
    MEDIA_INSIGHTS = "media_insights"
    ACCOUNT_INSIGHTS = "account_insights"
    TAGGED_MEDIA = "tagged_media"
    BUSINESS_DISCOVERY = "business_discovery"
    HASHTAG_SEARCH = "hashtag_search"
    CONTACT_INFORMATION = "contact_information"
    FOLLOWER_LISTS = "follower_lists"


class TargetScope(str, enum.Enum):
    OWN = "own"      # only the authenticated account itself
    ANY = "any"      # the authenticated account or another account


class AccountType(str, enum.Enum):
    BUSINESS = "business"
    CREATOR = "creator"
    PERSONAL = "personal"
    UNKNOWN = "unknown"


ACCOUNT_TYPE_ALIASES = {
    "business": AccountType.BUSINESS,
    "creator": AccountType.CREATOR,
    "media_creator": AccountType.CREATOR,
    "personal": AccountType.PERSONAL,
}


def parse_account_type(value) -> AccountType:
    if isinstance(value, AccountType):
        return value
    return ACCOUNT_TYPE_ALIASES.get(str(value or "").strip().lower(), AccountType.UNKNOWN)


@dataclass(frozen=True)
class CapabilityRule:
    supported: bool
    scope: TargetScope = TargetScope.OWN
    permissions: frozenset = frozenset()
    features: frozenset = frozenset()     # app features needing review, e.g. Public Content Access
    requires_facebook_page: bool = False
    verified: bool = True                 # False: docs unclear, confirm in a later phase
    note: str = ""
    doc: str = ""


# Permission (scope) names. They differ per mode; never assume they map 1:1.
IG_BUSINESS_BASIC = "instagram_business_basic"
IG_BUSINESS_MANAGE_COMMENTS = "instagram_business_manage_comments"
IG_BUSINESS_MANAGE_INSIGHTS = "instagram_business_manage_insights"

FB_INSTAGRAM_BASIC = "instagram_basic"
FB_INSTAGRAM_MANAGE_COMMENTS = "instagram_manage_comments"
FB_INSTAGRAM_MANAGE_INSIGHTS = "instagram_manage_insights"
FB_PAGES_SHOW_LIST = "pages_show_list"
FB_PAGES_READ_ENGAGEMENT = "pages_read_engagement"
FEATURE_PUBLIC_CONTENT_ACCESS = "Instagram Public Content Access"

_IG = frozenset({IG_BUSINESS_BASIC})
# pages_show_list is needed to find the Instagram account through the user's Pages.
_FB = frozenset({FB_INSTAGRAM_BASIC, FB_PAGES_SHOW_LIST, FB_PAGES_READ_ENGAGEMENT})

_NO_OFFICIAL_FIELD = "No official Instagram API field or edge provides this data."

_DOC_MEDIA = f"{DOCS_BASE}/reference/instagram-media"
_DOC_USER = f"{DOCS_BASE}/instagram-graph-api/reference/ig-user"
_DOC_STORIES = f"{DOCS_BASE}/instagram-graph-api/reference/ig-user/stories"
_DOC_COMMENTS = f"{DOCS_BASE}/instagram-graph-api/reference/ig-media/comments"
_DOC_MEDIA_INSIGHTS = f"{DOCS_BASE}/reference/instagram-media/insights"
_DOC_USER_INSIGHTS = f"{DOCS_BASE}/api-reference/instagram-user/insights"
_DOC_TAGS = f"{DOCS_BASE}/instagram-graph-api/reference/ig-user/tags"
_DOC_BD = f"{DOCS_BASE}/instagram-api-with-facebook-login/business-discovery"
_DOC_HASHTAG = f"{DOCS_BASE}/instagram-api-with-facebook-login/hashtag-search"
_DOC_IG_LOGIN = f"{DOCS_BASE}/instagram-api-with-instagram-login"

CAPABILITY_RULES: dict[AuthMode, dict[Capability, CapabilityRule]] = {
    AuthMode.INSTAGRAM_LOGIN: {
        Capability.OWN_PROFILE: CapabilityRule(
            True, permissions=_IG, doc=f"{_DOC_IG_LOGIN}/get-started",
            note="biography/website are not listed for Instagram Login; confirm in Phase 2."),
        Capability.OWN_MEDIA: CapabilityRule(True, permissions=_IG, doc=_DOC_MEDIA),
        Capability.MEDIA_PRODUCT_TYPE: CapabilityRule(
            False, verified=False, doc=_DOC_MEDIA,
            note="The IG Media reference marks media_product_type as Facebook Login only, "
                 "so Reels cannot be told apart from feed videos; confirm in Phase 2."),
        Capability.STORIES: CapabilityRule(
            True, permissions=_IG, verified=False, doc=_DOC_STORIES,
            note="The stories reference does not show Instagram Login; confirm in Phase 2."),
        Capability.COMMENTS: CapabilityRule(
            True, permissions=_IG | {IG_BUSINESS_MANAGE_COMMENTS}, doc=_DOC_COMMENTS),
        Capability.MEDIA_INSIGHTS: CapabilityRule(
            True, permissions=_IG | {IG_BUSINESS_MANAGE_INSIGHTS}, doc=_DOC_MEDIA_INSIGHTS),
        Capability.ACCOUNT_INSIGHTS: CapabilityRule(
            True, permissions=_IG | {IG_BUSINESS_MANAGE_INSIGHTS}, doc=_DOC_USER_INSIGHTS),
        Capability.TAGGED_MEDIA: CapabilityRule(
            True, permissions=_IG | {IG_BUSINESS_MANAGE_COMMENTS}, doc=_DOC_TAGS),
        Capability.BUSINESS_DISCOVERY: CapabilityRule(
            False, scope=TargetScope.ANY, doc=_DOC_BD,
            note="Business Discovery is documented for Facebook Login only."),
        Capability.HASHTAG_SEARCH: CapabilityRule(
            False, scope=TargetScope.ANY, doc=_DOC_IG_LOGIN,
            note="Instagram Login cannot use hashtag search."),
        Capability.CONTACT_INFORMATION: CapabilityRule(False, scope=TargetScope.ANY, note=_NO_OFFICIAL_FIELD,
                                                       doc=_DOC_USER),
        Capability.FOLLOWER_LISTS: CapabilityRule(False, scope=TargetScope.ANY, note=_NO_OFFICIAL_FIELD,
                                                  doc=_DOC_USER),
    },
    AuthMode.FACEBOOK_LOGIN: {
        Capability.OWN_PROFILE: CapabilityRule(True, permissions=_FB, requires_facebook_page=True,
                                               doc=_DOC_USER),
        Capability.OWN_MEDIA: CapabilityRule(True, permissions=_FB, requires_facebook_page=True,
                                             doc=_DOC_MEDIA),
        Capability.MEDIA_PRODUCT_TYPE: CapabilityRule(True, permissions=_FB, requires_facebook_page=True,
                                                      doc=_DOC_MEDIA),
        Capability.STORIES: CapabilityRule(True, permissions=_FB, requires_facebook_page=True,
                                           doc=_DOC_STORIES, note="Stories are returned only for 24 hours."),
        Capability.COMMENTS: CapabilityRule(
            True, permissions=_FB | {FB_INSTAGRAM_MANAGE_COMMENTS}, requires_facebook_page=True,
            doc=_DOC_COMMENTS),
        Capability.MEDIA_INSIGHTS: CapabilityRule(
            True, permissions=_FB | {FB_INSTAGRAM_MANAGE_INSIGHTS}, requires_facebook_page=True,
            doc=_DOC_MEDIA_INSIGHTS),
        Capability.ACCOUNT_INSIGHTS: CapabilityRule(
            True, permissions=_FB | {FB_INSTAGRAM_MANAGE_INSIGHTS}, requires_facebook_page=True,
            doc=_DOC_USER_INSIGHTS),
        Capability.TAGGED_MEDIA: CapabilityRule(
            True, permissions=_FB | {FB_INSTAGRAM_MANAGE_COMMENTS}, requires_facebook_page=True,
            doc=_DOC_TAGS),
        Capability.BUSINESS_DISCOVERY: CapabilityRule(
            True, scope=TargetScope.ANY, permissions=_FB | {FB_INSTAGRAM_MANAGE_INSIGHTS},
            requires_facebook_page=True, doc=_DOC_BD,
            note="Target must be a public Business or Creator account; returns public fields only."),
        Capability.HASHTAG_SEARCH: CapabilityRule(
            True, scope=TargetScope.ANY, permissions=_FB, features=frozenset({FEATURE_PUBLIC_CONTENT_ACCESS}),
            requires_facebook_page=True, doc=_DOC_HASHTAG,
            note="Limited to 30 unique hashtags per rolling 7 days."),
        Capability.CONTACT_INFORMATION: CapabilityRule(False, scope=TargetScope.ANY, note=_NO_OFFICIAL_FIELD,
                                                       doc=_DOC_USER),
        Capability.FOLLOWER_LISTS: CapabilityRule(False, scope=TargetScope.ANY, note=_NO_OFFICIAL_FIELD,
                                                  doc=_DOC_USER),
    },
}


@dataclass(frozen=True)
class TokenMetadata:
    """Expiry information about a token (never the token itself)."""
    expires_at: _dt.datetime | None = None          # None: no expiry known / never expires
    data_access_expires_at: _dt.datetime | None = None
    issued_at: _dt.datetime | None = None
    long_lived: bool | None = None

    @staticmethod
    def _aware(value: _dt.datetime | None) -> _dt.datetime | None:
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=_dt.timezone.utc)
        return value

    def _now(self, now: _dt.datetime | None) -> _dt.datetime:
        return self._aware(now) or _dt.datetime.now(_dt.timezone.utc)

    def is_expired(self, now: _dt.datetime | None = None) -> bool:
        now = self._now(now)
        for limit in (self._aware(self.expires_at), self._aware(self.data_access_expires_at)):
            if limit is not None and now >= limit:
                return True
        return False

    def seconds_remaining(self, now: _dt.datetime | None = None) -> float | None:
        exp = self._aware(self.expires_at)
        if exp is None:
            return None
        return (exp - self._now(now)).total_seconds()

    def expires_within(self, delta: _dt.timedelta, now: _dt.datetime | None = None) -> bool:
        remaining = self.seconds_remaining(now)
        return remaining is not None and remaining <= delta.total_seconds()

    def to_dict(self) -> dict:
        def iso(v):
            v = self._aware(v)
            return v.isoformat().replace("+00:00", "Z") if v else None
        return {"expires_at": iso(self.expires_at),
                "data_access_expires_at": iso(self.data_access_expires_at),
                "issued_at": iso(self.issued_at), "long_lived": self.long_lived}


@dataclass(frozen=True)
class AuthContext:
    """What a (future) authentication step learned. In Phase 1 it is only
    constructed by tests; nothing fills it from a live API."""
    mode: AuthMode
    authenticated: bool = False
    granted_permissions: frozenset = frozenset()
    granted_features: frozenset = frozenset()
    account_type: AccountType = AccountType.UNKNOWN
    facebook_page_linked: bool | None = None   # None: not known yet
    ig_user_id: str | None = None
    username: str | None = None
    token: TokenMetadata | None = None

    @classmethod
    def unauthenticated(cls, mode) -> "AuthContext":
        return cls(mode=parse_auth_mode(mode))

    def is_own_target(self, target_username: str | None) -> bool:
        return bool(self.username and target_username
                    and self.username.strip().lower() == target_username.strip().lower())


@dataclass(frozen=True)
class CapabilityDecision:
    capability: Capability
    status: FieldStatus
    reason: str
    missing_permissions: tuple = ()
    missing_features: tuple = ()
    verified: bool = True
    doc: str = ""

    @property
    def allowed(self) -> bool:
        return self.status is FieldStatus.AVAILABLE

    def to_dict(self) -> dict:
        return {"capability": self.capability.value, "status": self.status.value,
                "reason": self.reason, "missing_permissions": list(self.missing_permissions),
                "missing_features": list(self.missing_features), "verified": self.verified,
                "doc": self.doc}


def evaluate_capability(capability: Capability, context: AuthContext, *, target_is_own: bool,
                        now: _dt.datetime | None = None) -> CapabilityDecision:
    """Decide whether a capability can be used, and if not, why.

    Order: not offered by the API/mode -> authentication -> target scope ->
    account eligibility -> permissions and features.
    """
    rule = CAPABILITY_RULES[context.mode][capability]

    def decide(status, reason, perms=(), feats=()):
        return CapabilityDecision(capability, status, reason, tuple(sorted(perms)),
                                  tuple(sorted(feats)), rule.verified, rule.doc)

    if not rule.supported:
        return decide(FieldStatus.NOT_AVAILABLE,
                      rule.note or f"Not offered with {MODE_SPECS[context.mode].label}.")
    if not context.authenticated:
        return decide(FieldStatus.AUTHENTICATION_REQUIRED, "No authenticated session.")
    if context.token is not None and context.token.is_expired(now):
        return decide(FieldStatus.AUTHENTICATION_REQUIRED, "The access token has expired.")
    if rule.scope is TargetScope.OWN and not target_is_own:
        return decide(FieldStatus.NOT_ACCESSIBLE,
                      "Only available for the authenticated account itself, not for other accounts.")
    if context.account_type is AccountType.PERSONAL:
        return decide(FieldStatus.NOT_ACCESSIBLE,
                      "An Instagram professional (Business or Creator) account is required.")
    if rule.requires_facebook_page and context.facebook_page_linked is False:
        return decide(FieldStatus.NOT_ACCESSIBLE,
                      "The Instagram account must be linked to a Facebook Page for Facebook Login.")
    missing_perms = rule.permissions - context.granted_permissions
    missing_feats = rule.features - context.granted_features
    if missing_perms or missing_feats:
        what = ", ".join(sorted(missing_perms) + sorted(missing_feats))
        return decide(FieldStatus.PERMISSION_NOT_GRANTED, f"Not granted: {what}.",
                      missing_perms, missing_feats)
    return decide(FieldStatus.AVAILABLE, rule.note or "All requirements met.")


def evaluate_all(context: AuthContext, *, target_is_own: bool,
                 now: _dt.datetime | None = None) -> dict[Capability, CapabilityDecision]:
    return {cap: evaluate_capability(cap, context, target_is_own=target_is_own, now=now)
            for cap in Capability}
