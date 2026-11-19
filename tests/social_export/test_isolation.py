"""Module B is offline and isolated: no network or process code, no crawler
or API-prototype imports, and nothing existing depends on it."""
import pathlib
import re
import subprocess
import sys

import pytest

from tests.social_common.test_isolation import check_module, package_modules
from tests.social_export.builders import json_export, make_zip

PROJECT = pathlib.Path(__file__).resolve().parents[2]
PACKAGE = PROJECT / "webcrawler" / "social_export"
ALLOWED_PROJECT_IMPORTS = ("webcrawler.social_common", "webcrawler.social_export")


def test_package_files():
    assert {p.name for p in package_modules(PACKAGE)} == {
        "__init__.py", "__main__.py", "archive.py", "htmldoc.py", "parsers.py", "records.py", "report.py",
        "cli.py"}


@pytest.mark.parametrize("path", package_modules(PACKAGE), ids=lambda p: p.name)
def test_no_network_process_or_crawler_imports(path):
    check_module(path, ALLOWED_PROJECT_IMPORTS)


def test_no_extraction_calls_anywhere():
    for path in package_modules(PACKAGE):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"\.extract(all)?\(", text), path.name


def test_running_module_b_loads_no_network_code(tmp_path):
    source = make_zip(tmp_path / "export.zip", json_export())
    watched = ("socket", "ssl", "http.client", "urllib.request", "subprocess", "scrapy", "twisted", "lxml",
               "webcrawler.instagram", "webcrawler.pipelines", "webcrawler.extractors", "webcrawler.spiders",
               "webcrawler.social_discovery")
    code = (
        f"import sys; sys.path.insert(0, {str(PROJECT)!r}); "
        "from webcrawler.social_export import cli; "
        f"code = cli.main([{str(source)!r}, '--output-root', {str(tmp_path / 'out')!r}, '--formats', 'csv,json'], "
        "out=lambda *a: None); "
        f"print(code, sorted(m for m in {watched!r} if m in sys.modules))")
    result = subprocess.run([sys.executable, "-I", "-B", "-c", code], capture_output=True, text=True,
                            timeout=120, cwd=str(PROJECT))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "0 []"


def existing_python_files():
    files = [PROJECT / "run_crawler.py", PROJECT / "web_app.py"]
    for folder in ("webcrawler", "webcrawler/spiders", "webcrawler/instagram", "webcrawler/social_common",
                   "webcrawler/social_discovery"):
        files += sorted((PROJECT / folder).glob("*.py"))
    return files


@pytest.mark.parametrize("path", existing_python_files(), ids=lambda p: p.relative_to(PROJECT).as_posix())
def test_existing_code_does_not_use_module_b(path):
    assert "social_export" not in path.read_text(encoding="utf-8")


def test_launcher_command_keeps_its_security_settings(tmp_path):
    import run_crawler as rc
    command = rc.build_docker_command(rc.CrawlConfig("https://example.com/", 15, ("csv",), "CSV"),
                                      "20260101-000000", tmp_path)
    assert command[command.index("--cap-drop") + 1] == "ALL"
    assert not any("social" in argument for argument in command)
