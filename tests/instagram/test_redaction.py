"""Secret redaction and token-leak prevention (fake secrets only)."""
import io
import logging

import pytest

from tests.instagram.conftest import FAKE_FB_TOKEN, FAKE_IG_TOKEN, FAKE_SECRET
from webcrawler.instagram.redaction import (
    MIN_SECRET_LENGTH, PACKAGE_LOGGER_NAME, REDACTED, RedactingFilter, Redactor, default_redactor,
    get_logger, protect_handlers, redact, register_secret,
)

# A secret with no recognizable token shape: only registration can catch it.
PLAIN_SECRET = "plain-registered-secret-value"


def test_registered_secret_replaced_everywhere(redactor):
    redactor.add_secret("my-registered-secret-1")
    assert redactor.redact("a my-registered-secret-1 b my-registered-secret-1") == f"a {REDACTED} b {REDACTED}"


def test_short_or_non_string_values_not_registered(redactor):
    for v in ("", "abc", "nike", "x" * (MIN_SECRET_LENGTH - 1), None, 12345678):
        redactor.add_secret(v)
    assert redactor.secret_count == 0
    assert redactor.redact("Just do it - nike 12345678") == "Just do it - nike 12345678"


def test_longest_secret_wins(redactor):
    redactor.add_secret("secretvalue")
    redactor.add_secret("secretvalue-extended")
    assert redactor.redact("secretvalue-extended") == REDACTED


@pytest.mark.parametrize("text", [
    f"https://graph.facebook.com/v27.0/me?fields=id&access_token={FAKE_FB_TOKEN}",
    f"access_token={FAKE_FB_TOKEN}&x=1",
    f"client_secret={FAKE_SECRET}",
    f"fb_exchange_token={FAKE_FB_TOKEN}",
    f"Authorization: Bearer {FAKE_IG_TOKEN}",
    f"authorization=OAuth {FAKE_FB_TOKEN}",
    f'{{"access_token": "{FAKE_IG_TOKEN}", "token_type": "bearer"}}',
    f"token is {FAKE_FB_TOKEN} here",
    f"token is {FAKE_IG_TOKEN} here",
    "IGQV" + "a1B2c3D4" * 5,
    # URL-encoded parameter names and values (audit finding I3)
    f"next=https%3A%2F%2Fgraph%3Faccess_token%3D{FAKE_SECRET}%26limit%3D5",
    f"client_secret%3D{FAKE_SECRET}",
])
def test_unregistered_secrets_caught_by_shape(text):
    out = Redactor(share=False).redact(text)
    for secret in (FAKE_FB_TOKEN, FAKE_IG_TOKEN, FAKE_SECRET, "IGQV" + "a1B2c3D4" * 5):
        assert secret not in out
    assert REDACTED in out


def test_url_encoded_token_value_redacted():
    out = Redactor(share=False).redact("access_token%3Dabc%2Fdef%2Bghi%3D%3D&x=1")
    assert "abc" not in out and "ghi" not in out and out.endswith("&x=1")


def test_ordinary_text_untouched(redactor):
    text = "Nike posted 12 reels; code=100 subcode=33 https://www.instagram.com/nike/ Bearer"
    assert redactor.redact(text) == text


def test_redact_url_by_parameter_name(redactor):
    url = ("https://graph.instagram.com/v27.0/me?fields=id,username&access_token=abc123token"
           "&client_secret=s3cr3tvalue&code=authcode999&limit=25")
    out = redactor.redact_url(url)
    assert "abc123token" not in out and "s3cr3tvalue" not in out and "authcode999" not in out
    assert "fields=id,username" in out and "limit=25" in out


def test_redact_url_drops_userinfo_and_fragment(redactor):
    out = redactor.redact_url("https://user:pw@graph.facebook.com/v27.0/me#frag")
    assert out == "https://graph.facebook.com/v27.0/me"


def test_redact_obj_recursive_and_does_not_mutate(redactor):
    redactor.add_secret(FAKE_SECRET)
    data = {"access_token": "anything", "nested": [{"client_secret": "x"}, f"say {FAKE_SECRET}"],
            "Authorization": "Bearer abc", "code": 190, "caption": "hello"}
    out = redactor.redact_obj(data)
    assert out["access_token"] == REDACTED and out["Authorization"] == REDACTED
    assert out["nested"][0]["client_secret"] == REDACTED
    assert out["nested"][1] == f"say {REDACTED}"
    assert out["code"] == 190 and out["caption"] == "hello"   # error codes are not secrets
    assert data["access_token"] == "anything"


def test_contains_secret(redactor):
    redactor.add_secret("zzz-secret-zzz")
    assert redactor.contains_secret("x zzz-secret-zzz")
    assert redactor.contains_secret(f"t={FAKE_FB_TOKEN}")
    assert not redactor.contains_secret("plain text")


def test_secret_wrapper_accepted_by_add_secret(redactor):
    from webcrawler.instagram.config import Secret
    redactor.add_secret(Secret("wrapped-secret-value"))
    assert redactor.redact("wrapped-secret-value") == REDACTED


def test_module_level_helpers():
    register_secret("module-level-secret")
    assert redact("module-level-secret") == REDACTED


# --- L4: custom redactors share secrets with the logging redactor ------------------------
def test_custom_redactor_secret_shared_with_default():
    custom = Redactor()
    custom.add_secret(PLAIN_SECRET)
    assert default_redactor.redact(PLAIN_SECRET) == REDACTED


def test_unshared_redactor_stays_private():
    private = Redactor(share=False)
    private.add_secret("only-in-private-one")
    assert private.redact("only-in-private-one") == REDACTED
    assert default_redactor.redact("only-in-private-one") == "only-in-private-one"


def test_independent_redactors():
    a, b = Redactor(share=False), Redactor(["only-in-b-12345"], share=False)
    assert a.redact("only-in-b-12345") == "only-in-b-12345"
    assert b.redact("only-in-b-12345") == REDACTED


# --- M1: child-logger records through real output handlers -------------------------------------
@pytest.fixture
def root_capture():
    """A real StreamHandler on the ROOT logger, as an application would configure."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    yield stream, handler
    root.removeHandler(handler)
    root.setLevel(old_level)


def test_child_logger_record_redacted_at_source_through_root_handler(root_capture):
    stream, _ = root_capture
    register_secret(PLAIN_SECRET)
    log = get_logger("webcrawler.instagram.client.child")
    log.warning("calling with %s and access_token=%s", PLAIN_SECRET, FAKE_FB_TOKEN)
    try:
        raise RuntimeError(f"boom {PLAIN_SECRET}")
    except RuntimeError:
        log.exception("failed")
    out = stream.getvalue()
    assert "webcrawler.instagram.client.child" in out
    assert PLAIN_SECRET not in out and FAKE_FB_TOKEN not in out
    assert "boom [REDACTED]" in out


def test_package_client_logger_is_protected(root_capture):
    stream, _ = root_capture
    register_secret(PLAIN_SECRET)
    import webcrawler.instagram.client as client_module
    client_module.logger.error("leak %s", PLAIN_SECRET)
    assert PLAIN_SECRET not in stream.getvalue()


@pytest.fixture
def isolated_parent():
    """A plain parent logger with its own handler and propagate=False, so the
    deliberately unredacted records in these tests never reach pytest's (or any
    other) handlers. Plain loggers below it stand in for code that does not
    use get_logger()."""
    created = []

    def make(name):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        parent = logging.getLogger(name)
        parent.handlers[:] = [handler]
        parent.propagate = False
        parent.setLevel(logging.DEBUG)
        created.append(parent)
        return parent, handler, stream

    yield make
    for parent in created:
        parent.handlers[:] = []
        parent.filters[:] = []
        parent.propagate = True


def test_protect_handlers_covers_plain_child_loggers(isolated_parent):
    """Output-level protection: even loggers NOT created via get_logger()
    (plain child loggers) are redacted once the output handler is protected."""
    parent, handler, stream = isolated_parent("isolated_output_test")
    register_secret(PLAIN_SECRET)
    assert protect_handlers(handler) == 1
    logging.getLogger("isolated_output_test.webcrawler_child").error("x %s", PLAIN_SECRET)
    logging.getLogger("isolated_output_test.unrelated.library").error("y %s", PLAIN_SECRET)
    out = stream.getvalue()
    assert PLAIN_SECRET not in out and out.count(REDACTED) == 2


def test_unprotected_plain_logger_documents_the_original_gap(isolated_parent):
    """A filter on a PARENT logger does not filter records created on a child
    logger - the behaviour finding M1 described, and why package code must use
    get_logger() and entry points protect_handlers()."""
    parent, _, stream = isolated_parent("isolated_gap_test")
    register_secret(PLAIN_SECRET)
    parent.addFilter(RedactingFilter())  # parent has the filter...
    logging.getLogger("isolated_gap_test.child").error("z %s", PLAIN_SECRET)
    assert PLAIN_SECRET in stream.getvalue()  # ...but the child record is not filtered by it
    parent.error("p %s", PLAIN_SECRET)       # records created on the parent itself are
    assert stream.getvalue().count(REDACTED) == 1


def test_filters_are_not_duplicated():
    log = get_logger("webcrawler.instagram.dup")
    get_logger("webcrawler.instagram.dup")
    assert sum(isinstance(f, RedactingFilter) for f in log.filters) == 1
    handler = logging.StreamHandler(io.StringIO())
    assert protect_handlers(handler) == 1
    assert protect_handlers(handler) == 0
    assert sum(isinstance(f, RedactingFilter) for f in handler.filters) == 1


def test_get_logger_refuses_loggers_outside_the_package():
    for name in ("webcrawler", "webcrawler.spiders", "webcrawler.instagramx", "", "root"):
        with pytest.raises(ValueError):
            get_logger(name)


def test_importing_package_adds_no_root_handlers_or_filters():
    """Fresh interpreter: importing every module must not touch global logging."""
    import pathlib
    import subprocess
    import sys
    project = pathlib.Path(__file__).resolve().parents[2]
    code = ("import sys, logging; sys.path.insert(0, %r); r=logging.getLogger(); "
            "b=(len(r.handlers), len(r.filters)); "
            "import webcrawler.instagram.client, webcrawler.instagram.normalizer, "
            "webcrawler.instagram.target, webcrawler.instagram.config; "
            "a=(len(r.handlers), len(r.filters)); "
            "others=[n for n,l in logging.Logger.manager.loggerDict.items() "
            "if isinstance(l, logging.Logger) and l.filters and not n.startswith('webcrawler.instagram')]; "
            "print(b == a, others)") % str(project)
    out = subprocess.run([sys.executable, "-I", "-B", "-c", code], capture_output=True, text=True,
                         timeout=60, cwd=str(project))
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "True []"


def test_protect_handlers_rejects_non_handlers():
    with pytest.raises(TypeError):
        protect_handlers(logging.getLogger("x"))


def test_log_filter_survives_bad_format_args(root_capture):
    stream, _ = root_capture
    get_logger("webcrawler.instagram.badargs").error("needs two %s %s", FAKE_FB_TOKEN)
    assert FAKE_FB_TOKEN not in stream.getvalue()


def test_redacting_filter_defaults_to_module_redactor():
    register_secret("default-filter-secret")
    rec = logging.LogRecord("x", logging.INFO, __file__, 1, "v=%s", ("default-filter-secret",), None)
    assert RedactingFilter().filter(rec) is True
    assert rec.getMessage() == f"v={REDACTED}"


def test_filter_with_custom_redactor_and_stack_info():
    custom = Redactor(["stack-secret-123"], share=False)
    rec = logging.LogRecord("x", logging.INFO, __file__, 1, "m", (), None, sinfo="trace stack-secret-123")
    RedactingFilter(custom).filter(rec)
    assert rec.stack_info == f"trace {REDACTED}"
