"""Configuration for both authentication modes (no authentication performed)."""
import logging

import pytest

from tests.instagram.conftest import FAKE_FB_TOKEN, FAKE_IG_TOKEN, FAKE_SECRET
from webcrawler.instagram import config as C
from webcrawler.instagram.errors import ConfigError
from webcrawler.instagram.redaction import REDACTED, Redactor, default_redactor


def ig_env(**over):
    env = {"IG_AUTH_MODE": "instagram", "IG_LOGIN_APP_ID": "1234567890",
           "IG_LOGIN_APP_SECRET": FAKE_SECRET, "IG_LOGIN_ACCESS_TOKEN": FAKE_IG_TOKEN}
    env.update(over)
    return {k: v for k, v in env.items() if v is not None}


def fb_env(**over):
    env = {"IG_AUTH_MODE": "facebook", "FB_LOGIN_APP_ID": "9876543210",
           "FB_LOGIN_APP_SECRET": FAKE_SECRET, "FB_LOGIN_ACCESS_TOKEN": FAKE_FB_TOKEN}
    env.update(over)
    return {k: v for k, v in env.items() if v is not None}


# --- modes -------------------------------------------------------------------
def test_instagram_login_mode():
    cfg = C.load_config(ig_env())
    assert cfg.mode is C.AuthMode.INSTAGRAM_LOGIN
    assert cfg.graph_base_url == "https://graph.instagram.com"
    assert cfg.api_version == C.DEFAULT_API_VERSION == "v27.0"
    assert cfg.app_id == "1234567890"
    assert cfg.access_token.reveal() == FAKE_IG_TOKEN
    assert cfg.spec.requires_facebook_page is False
    assert cfg.spec.refresh_endpoint_available is True


def test_facebook_login_mode():
    cfg = C.load_config(fb_env())
    assert cfg.mode is C.AuthMode.FACEBOOK_LOGIN
    assert cfg.graph_base_url == "https://graph.facebook.com"
    assert cfg.app_id == "9876543210"
    assert cfg.access_token.reveal() == FAKE_FB_TOKEN
    assert cfg.spec.requires_facebook_page is True
    assert cfg.spec.refresh_endpoint_available is False


def test_modes_use_separate_variable_names():
    ig, fb = C.MODE_SPECS[C.AuthMode.INSTAGRAM_LOGIN], C.MODE_SPECS[C.AuthMode.FACEBOOK_LOGIN]
    ig_names = {ig.env_app_id, ig.env_app_secret, ig.env_access_token}
    fb_names = {fb.env_app_id, fb.env_app_secret, fb.env_access_token}
    assert ig_names == {"IG_LOGIN_APP_ID", "IG_LOGIN_APP_SECRET", "IG_LOGIN_ACCESS_TOKEN"}
    assert fb_names == {"FB_LOGIN_APP_ID", "FB_LOGIN_APP_SECRET", "FB_LOGIN_ACCESS_TOKEN"}
    assert not ig_names & fb_names
    assert ig.graph_host != fb.graph_host


def test_only_the_selected_modes_variables_are_read():
    # Facebook variables are ignored in Instagram mode and vice versa.
    env = {**ig_env(), "FB_LOGIN_ACCESS_TOKEN": FAKE_FB_TOKEN}
    assert C.load_config(env).access_token.reveal() == FAKE_IG_TOKEN
    env = {"IG_AUTH_MODE": "facebook", "IG_LOGIN_ACCESS_TOKEN": FAKE_IG_TOKEN}
    with pytest.raises(ConfigError) as exc:
        C.load_config(env)
    assert exc.value.missing == ("FB_LOGIN_ACCESS_TOKEN",)


@pytest.mark.parametrize("raw, mode", [
    ("instagram", C.AuthMode.INSTAGRAM_LOGIN), ("Instagram_Login", C.AuthMode.INSTAGRAM_LOGIN),
    ("ig", C.AuthMode.INSTAGRAM_LOGIN), (" facebook ", C.AuthMode.FACEBOOK_LOGIN),
    ("FB", C.AuthMode.FACEBOOK_LOGIN), ("facebook-login", C.AuthMode.FACEBOOK_LOGIN),
])
def test_mode_aliases(raw, mode):
    assert C.parse_auth_mode(raw) is mode


# --- missing / invalid variables ----------------------------------------------
def test_missing_auth_mode():
    with pytest.raises(ConfigError) as exc:
        C.load_config({})
    assert exc.value.missing == ("IG_AUTH_MODE",)


def test_unknown_auth_mode():
    with pytest.raises(ConfigError) as exc:
        C.load_config({"IG_AUTH_MODE": "twitter"})
    assert exc.value.invalid == ("IG_AUTH_MODE",)


@pytest.mark.parametrize("env_fn, token_var", [(ig_env, "IG_LOGIN_ACCESS_TOKEN"),
                                                (fb_env, "FB_LOGIN_ACCESS_TOKEN")])
def test_missing_token(env_fn, token_var):
    with pytest.raises(ConfigError) as exc:
        C.load_config(env_fn(**{token_var: None}))
    assert exc.value.missing == (token_var,)
    assert token_var in str(exc.value)


@pytest.mark.parametrize("env_fn, token_var", [(ig_env, "IG_LOGIN_ACCESS_TOKEN"),
                                                (fb_env, "FB_LOGIN_ACCESS_TOKEN")])
def test_blank_token_counts_as_missing(env_fn, token_var):
    with pytest.raises(ConfigError) as exc:
        C.load_config(env_fn(**{token_var: "   "}))
    assert exc.value.missing == (token_var,)


def test_token_optional_when_not_required():
    cfg = C.load_config(ig_env(IG_LOGIN_ACCESS_TOKEN=None), require_token=False)
    assert cfg.access_token is None and not cfg.has_access_token


def test_app_id_and_secret_are_optional_in_phase1():
    cfg = C.load_config({"IG_AUTH_MODE": "facebook", "FB_LOGIN_ACCESS_TOKEN": FAKE_FB_TOKEN})
    assert cfg.app_id is None and cfg.app_secret is None
    assert cfg.missing_for_token_exchange() == ("FB_LOGIN_APP_ID", "FB_LOGIN_APP_SECRET")
    assert C.load_config(fb_env()).missing_for_token_exchange() == ()


@pytest.mark.parametrize("over, invalid", [
    ({"IG_LOGIN_ACCESS_TOKEN": "short"}, "IG_LOGIN_ACCESS_TOKEN"),
    ({"IG_LOGIN_ACCESS_TOKEN": "has space" + "x" * 30}, "IG_LOGIN_ACCESS_TOKEN"),
    ({"IG_LOGIN_ACCESS_TOKEN": "x" * 5000}, "IG_LOGIN_ACCESS_TOKEN"),
    ({"IG_LOGIN_APP_ID": "abc123"}, "IG_LOGIN_APP_ID"),
    ({"IG_LOGIN_APP_SECRET": "bad\tsecret"}, "IG_LOGIN_APP_SECRET"),
    ({"IG_GRAPH_API_VERSION": "27"}, "IG_GRAPH_API_VERSION"),
    ({"IG_GRAPH_API_VERSION": "v27.0; rm -rf"}, "IG_GRAPH_API_VERSION"),
])
def test_invalid_values(over, invalid):
    with pytest.raises(ConfigError) as exc:
        C.load_config(ig_env(**over))
    assert invalid in exc.value.invalid


def test_custom_api_version():
    assert C.load_config(ig_env(IG_GRAPH_API_VERSION="v26.0")).api_version == "v26.0"


def test_multiple_problems_reported_together():
    with pytest.raises(ConfigError) as exc:
        C.load_config(ig_env(IG_LOGIN_ACCESS_TOKEN=None, IG_LOGIN_APP_ID="x",
                             IG_GRAPH_API_VERSION="bad"))
    assert exc.value.missing == ("IG_LOGIN_ACCESS_TOKEN",)
    assert set(exc.value.invalid) == {"IG_LOGIN_APP_ID", "IG_GRAPH_API_VERSION"}


# --- secrets never leak -----------------------------------------------------------
def test_config_repr_str_and_describe_hide_secrets():
    cfg = C.load_config(ig_env())
    for text in (repr(cfg), str(cfg), f"{cfg}", str(cfg.describe()), repr(cfg.access_token),
                 str(cfg.app_secret), f"{cfg.access_token}", f"{cfg.access_token:>80}"):
        assert FAKE_IG_TOKEN not in text and FAKE_SECRET not in text
    d = cfg.describe()
    assert d["access_token_set"] is True and d["app_secret_set"] is True
    assert d["variables"]["access_token"] == "IG_LOGIN_ACCESS_TOKEN"


def test_error_messages_never_contain_values():
    bad_token = "bad token value 123456789"
    with pytest.raises(ConfigError) as exc:
        C.load_config(ig_env(IG_LOGIN_ACCESS_TOKEN=bad_token, IG_LOGIN_APP_ID="not-a-number-777"))
    text = str(exc.value) + repr(exc.value)
    assert "bad token value" not in text and "not-a-number-777" not in text


def test_loaded_secrets_are_registered_for_redaction(caplog):
    r = Redactor()
    C.load_config(ig_env(), redactor=r)
    assert r.redact(f"x {FAKE_IG_TOKEN} y {FAKE_SECRET}") == f"x {REDACTED} y {REDACTED}"
    # default redactor used when none is given
    C.load_config(fb_env())
    assert FAKE_FB_TOKEN not in default_redactor.redact(FAKE_FB_TOKEN)


def test_secrets_registered_even_when_config_invalid():
    r = Redactor()
    with pytest.raises(ConfigError):
        C.load_config(ig_env(IG_LOGIN_APP_ID="bad-id"), redactor=r)
    assert r.redact(FAKE_IG_TOKEN) == REDACTED


def test_secret_wrapper():
    s = C.Secret("supersecretvalue123")
    assert s.reveal() == "supersecretvalue123"
    assert "supersecretvalue123" not in repr(s) + str(s) + f"{s}" + "%s" % s
    assert s == C.Secret("supersecretvalue123") and bool(s) and not C.Secret("")
    with pytest.raises(TypeError):
        import pickle
        pickle.dumps(s)
    with pytest.raises(TypeError):
        C.Secret(123)


def test_secret_not_json_serializable():
    import json
    with pytest.raises(TypeError):
        json.dumps({"t": C.Secret("abcdefgh")})


def test_secret_in_log_message_is_masked(caplog):
    cfg = C.load_config(ig_env())
    with caplog.at_level(logging.INFO):
        logging.getLogger("t").info("config: %s token=%s", cfg, cfg.access_token)
    assert FAKE_IG_TOKEN not in caplog.text


# --- docker passthrough (names only) ---------------------------------------------------
@pytest.mark.parametrize("mode, token_var", [("instagram", "IG_LOGIN_ACCESS_TOKEN"),
                                             ("facebook", "FB_LOGIN_ACCESS_TOKEN")])
def test_docker_env_passthrough_names_only(mode, token_var):
    args = C.docker_env_passthrough(mode)
    assert args == ["-e", "IG_AUTH_MODE", "-e", "IG_GRAPH_API_VERSION", "-e", token_var]
    assert all("=" not in a for a in args)
    assert not any("SECRET" in a for a in args)  # the app secret is never passed


def test_docker_env_passthrough_contains_no_values(monkeypatch):
    # monkeypatch restores the real environment automatically (audit finding I5)
    monkeypatch.setenv("IG_AUTH_MODE", "instagram")
    monkeypatch.setenv("IG_LOGIN_ACCESS_TOKEN", FAKE_IG_TOKEN)
    args = C.docker_env_passthrough(C.load_config().mode)
    assert FAKE_IG_TOKEN not in " ".join(args)


def test_no_secret_literals_in_source():
    """No token-shaped or secret-looking literals are hardcoded in the package."""
    import pathlib
    import re
    pkg = pathlib.Path(C.__file__).parent
    shapes = re.compile(r"EAA[A-Za-z0-9]{20,}|IG(?:AA|QV)[A-Za-z0-9_\-]{20,}|[0-9a-f]{32}")
    for path in pkg.glob("*.py"):
        assert not shapes.search(path.read_text(encoding="utf-8")), path.name
