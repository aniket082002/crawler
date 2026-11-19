"""The Instagram package is independent of the website crawler, contains no
network code in Phase 1, and leaves the existing launcher's Docker security
settings untouched."""
import ast
import pathlib
import subprocess
import sys

import pytest

import run_crawler as rc

PROJECT = pathlib.Path(__file__).resolve().parents[2]
PKG = PROJECT / "webcrawler" / "instagram"
FORBIDDEN_IMPORTS = {"scrapy", "twisted", "socket", "ssl", "http", "urllib.request", "requests",
                     "httpx", "aiohttp", "webcrawler.spiders", "webcrawler.pipelines",
                     "webcrawler.extractors", "webcrawler.profile", "webcrawler.settings",
                     "webcrawler.middlewares", "webcrawler.extensions", "run_crawler", "web_app"}
FORBIDDEN_CALLS = {"urlopen", "create_connection", "HTTPSConnection", "HTTPConnection"}


def _modules():
    return sorted(PKG.rglob("*.py"))


def test_package_has_expected_phase1_files():
    names = {p.name for p in _modules()}
    assert names == {"__init__.py", "config.py", "target.py", "client.py", "errors.py",
                     "redaction.py", "normalizer.py"}


@pytest.mark.parametrize("path", _modules(), ids=lambda p: p.name)
def test_no_network_or_crawler_imports(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        for name in names:
            for bad in FORBIDDEN_IMPORTS:
                assert not (name == bad or name.startswith(bad + ".")), f"{path.name} imports {name}"
        if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_CALLS:
            pytest.fail(f"{path.name} references {node.attr}")
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_CALLS:
            pytest.fail(f"{path.name} references {node.id}")


def test_importing_package_loads_no_network_or_scrapy_modules():
    code = ("import sys, webcrawler.instagram.client, webcrawler.instagram.normalizer, "
            "webcrawler.instagram.target; "
            "bad=[m for m in ('scrapy','socket','ssl','http.client','urllib.request') if m in sys.modules]; "
            "print(','.join(bad))")
    out = subprocess.run([sys.executable, "-I", "-B", "-c", f"import sys; sys.path.insert(0, {str(PROJECT)!r}); {code}"],
                         capture_output=True, text=True, timeout=60, cwd=str(PROJECT))
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == ""


def test_crawler_does_not_import_instagram_package():
    import re
    pattern = re.compile(r"webcrawler\.instagram|from\s+webcrawler\s+import\s+[^\n]*\binstagram\b")
    for path in [PROJECT / "run_crawler.py", PROJECT / "web_app.py", *(PROJECT / "webcrawler").glob("*.py"),
                 *(PROJECT / "webcrawler" / "spiders").glob("*.py")]:
        assert not pattern.search(path.read_text(encoding="utf-8")), path.name


def test_existing_docker_command_keeps_security_settings(tmp_path):
    cfg = rc.CrawlConfig("https://example.com/", 15, ("csv",), "CSV")
    cmd = rc.build_docker_command(cfg, "20260101-000000", tmp_path)
    i = cmd.index("--cap-drop")
    assert cmd[i + 1] == "ALL"
    assert "--cap-drop ALL" not in cmd and "--cap-drop=ALL" not in cmd
    for flag, value in (("--security-opt", "no-new-privileges"), ("--user", "1000:1000"),
                        ("--memory", "512m"), ("--memory-swap", "512m"), ("--cpus", "1")):
        assert cmd[cmd.index(flag) + 1] == value
    assert cmd[cmd.index("webcrawler:latest") + 1:cmd.index("webcrawler:latest") + 3] == ["crawl", "general"]
    assert not any(a.startswith("-e") or "TOKEN" in a or "SECRET" in a for a in cmd)
