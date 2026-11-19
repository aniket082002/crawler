"""Module C is offline and isolated: it cannot reach the network, it does not
touch the website crawler or the Instagram API prototype, and they do not
depend on it."""
import pathlib
import re
import subprocess
import sys

import pytest

from tests.social_common.test_isolation import check_module, package_modules
from tests.social_discovery.builders import IG, page, write_json

PROJECT = pathlib.Path(__file__).resolve().parents[2]
PACKAGE = PROJECT / "webcrawler" / "social_discovery"
ALLOWED_PROJECT_IMPORTS = ("webcrawler.url_validator", "webcrawler.social_common", "webcrawler.social_discovery")
NEW_PACKAGES_RE = re.compile(r"social_common|social_discovery")


def test_package_files():
    assert {p.name for p in package_modules(PACKAGE)} == {
        "__init__.py", "__main__.py", "reader.py", "discover.py", "report.py", "cli.py"}


@pytest.mark.parametrize("path", package_modules(PACKAGE), ids=lambda p: p.name)
def test_no_network_process_or_crawler_imports(path):
    """No networking, no subprocesses, no Scrapy/lxml, no API prototype; the
    only crawler code used is the standard-library-only url_validator."""
    check_module(path, ALLOWED_PROJECT_IMPORTS)


def test_importing_and_running_module_c_loads_no_network_code(tmp_path):
    """Fresh interpreter: run a complete discovery with CSV and JSON export,
    then list which sensitive modules were loaded."""
    source = write_json(tmp_path / "items.json", [page("https://acme.example/", links=[f"{IG}/nike/"])])
    watched = ("socket", "ssl", "http.client", "urllib.request", "subprocess", "scrapy", "twisted", "lxml",
               "webcrawler.instagram", "webcrawler.pipelines", "webcrawler.extractors", "webcrawler.spiders")
    code = (
        f"import sys; sys.path.insert(0, {str(PROJECT)!r}); "
        "from webcrawler.social_discovery import cli; "
        f"code = cli.main([{str(source)!r}, '--output-root', {str(tmp_path / 'out')!r}, '--formats', 'csv,json'], "
        "out=lambda *a: None); "
        f"print(code, sorted(m for m in {watched!r} if m in sys.modules))")
    result = subprocess.run([sys.executable, "-I", "-B", "-c", code], capture_output=True, text=True,
                            timeout=120, cwd=str(PROJECT))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "0 []"
    assert any((tmp_path / "out").rglob("instagram_profiles.csv"))


def existing_project_files():
    crawler = [PROJECT / "run_crawler.py", PROJECT / "web_app.py",
               *sorted((PROJECT / "webcrawler").glob("*.py")),
               *sorted((PROJECT / "webcrawler" / "spiders").glob("*.py"))]
    prototype = sorted((PROJECT / "webcrawler" / "instagram").glob("*.py"))
    return crawler + prototype


@pytest.mark.parametrize("path", existing_project_files(), ids=lambda p: p.relative_to(PROJECT).as_posix())
def test_crawler_and_prototype_do_not_use_the_new_packages(path):
    assert not NEW_PACKAGES_RE.search(path.read_text(encoding="utf-8")), path.name


def test_new_packages_do_not_use_the_api_prototype():
    for package in (PROJECT / "webcrawler" / "social_common", PACKAGE):
        for path in package_modules(package):
            text = path.read_text(encoding="utf-8")
            assert not re.search(r"^\s*(from|import)\s+webcrawler\.instagram\b", text, re.MULTILINE), path.name


def test_crawler_settings_do_not_load_the_new_packages():
    """Scrapy only loads spiders, pipelines and extensions named in settings.py."""
    settings = (PROJECT / "webcrawler" / "settings.py").read_text(encoding="utf-8")
    assert 'SPIDER_MODULES = ["webcrawler.spiders"]' in settings
    assert not NEW_PACKAGES_RE.search(settings)


def test_launcher_command_is_unchanged_by_this_module(tmp_path):
    """The website crawler's Docker command keeps its security settings."""
    import run_crawler as rc
    config = rc.CrawlConfig("https://example.com/", 15, ("csv",), "CSV")
    command = rc.build_docker_command(config, "20260101-000000", tmp_path)
    position = command.index("--cap-drop")
    assert command[position + 1] == "ALL" and "--cap-drop ALL" not in command
    assert command[command.index("--security-opt") + 1] == "no-new-privileges"
    assert not any("social" in argument for argument in command)
