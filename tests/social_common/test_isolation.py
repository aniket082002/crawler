"""social_common is offline, self-contained and independent of the crawler's
heavy parts and of the Instagram API prototype."""
import ast
import pathlib
import socket
import subprocess
import sys
import urllib.request

import pytest

from tests.social_common.offline import NetworkBlocked

PROJECT = pathlib.Path(__file__).resolve().parents[2]
PACKAGE = PROJECT / "webcrawler" / "social_common"

# Modules that could reach the network, start programs or load code.
FORBIDDEN_IMPORTS = {
    "socket", "ssl", "http", "urllib.request", "urllib.error", "requests", "httpx", "aiohttp",
    "ftplib", "smtplib", "poplib", "imaplib", "telnetlib", "xmlrpc", "webbrowser", "asyncio",
    "subprocess", "multiprocessing", "ctypes", "pickle", "shelve", "marshal", "importlib",
    "scrapy", "twisted", "lxml", "parsel", "w3lib", "playwright", "selenium",
}
# The only crawler code these packages may use (standard library only itself).
ALLOWED_PROJECT_IMPORTS = ("webcrawler.url_validator", "webcrawler.social_common")
FORBIDDEN_NAMES = {"eval", "exec", "compile", "__import__", "print", "input", "urlopen"}
FORBIDDEN_ATTRIBUTES = {("os", "system"), ("os", "popen"), ("os", "startfile")}


def package_modules(package=PACKAGE):
    return sorted(package.glob("*.py"))


def imported_names(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node, alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                yield node, "<relative>"
            elif node.module:
                yield node, node.module


def check_module(path, allowed_project_imports, third_party_allowed=()):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node, name in imported_names(tree):
        where = f"{path.name}:{node.lineno}"
        assert name != "<relative>", f"{where} uses a relative import"
        for bad in FORBIDDEN_IMPORTS:
            assert not (name == bad or name.startswith(bad + ".")), f"{where} imports {name}"
        if name == "webcrawler" or name.startswith("webcrawler."):
            assert any(name == ok or name.startswith(ok + ".") for ok in allowed_project_imports), \
                f"{where} imports {name}"
        if name.split(".")[0] in ("xlsxwriter", "openpyxl"):
            assert path.name in third_party_allowed, f"{where} imports {name}"
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in FORBIDDEN_NAMES, f"{path.name}:{node.lineno} calls {node.func.id}()"
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            assert (node.value.id, node.attr) not in FORBIDDEN_ATTRIBUTES, \
                f"{path.name}:{node.lineno} uses {node.value.id}.{node.attr}"


def test_package_files():
    assert {p.name for p in package_modules()} == {
        "__init__.py", "schema.py", "identity.py", "exporters.py", "safe_io.py"}


@pytest.mark.parametrize("path", package_modules(), ids=lambda p: p.name)
def test_no_network_process_or_crawler_imports(path):
    check_module(path, ALLOWED_PROJECT_IMPORTS, third_party_allowed=("exporters.py",))


def test_importing_the_package_loads_nothing_it_should_not():
    """Fresh interpreter: import every module, then list what got loaded."""
    modules = ", ".join(f"webcrawler.social_common.{p.stem}" for p in package_modules() if p.stem != "__init__")
    watched = ("socket", "ssl", "http.client", "urllib.request", "subprocess", "scrapy", "lxml",
               "xlsxwriter", "webcrawler.instagram", "webcrawler.pipelines", "webcrawler.extractors")
    code = (f"import sys; sys.path.insert(0, {str(PROJECT)!r}); import {modules}; "
            f"print(sorted(m for m in {watched!r} if m in sys.modules))")
    result = subprocess.run([sys.executable, "-I", "-B", "-c", code], capture_output=True, text=True,
                            timeout=60, cwd=str(PROJECT))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"


def test_the_offline_guard_really_blocks_the_network():
    with pytest.raises(NetworkBlocked):
        socket.create_connection(("127.0.0.1", 9), timeout=1)
    with pytest.raises(NetworkBlocked):
        socket.getaddrinfo("localhost", 80)
    with pytest.raises(NetworkBlocked):
        socket.socket().connect(("127.0.0.1", 9))
    with pytest.raises(NetworkBlocked):
        urllib.request.urlopen("http://127.0.0.1:9/")
