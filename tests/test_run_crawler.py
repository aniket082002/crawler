"""Tests for run_crawler.py (the interactive launcher).

Every Docker interaction is mocked: subprocess.run / subprocess.Popen /
shutil.which are patched, so these tests never start a container and never
touch the network. They also never import scrapy, so they run on any host.
"""
import io
import json
import subprocess
from pathlib import Path
from unittest import mock

import pytest

import run_crawler as rc


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def scripted_input(*answers):
    """input() replacement returning the given answers in order; raises
    EOFError when exhausted (like a closed stdin)."""
    it = iter(answers)

    def _input(prompt=""):
        try:
            value = next(it)
        except StopIteration:
            raise EOFError
        if isinstance(value, BaseException):
            raise value
        return value

    return _input


class Collector:
    def __init__(self):
        self.lines = []

    def __call__(self, *args):
        self.lines.append(" ".join(str(a) for a in args))

    @property
    def text(self):
        return "\n".join(self.lines)


def completed(rc_=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=rc_, stdout=stdout, stderr=stderr)


def make_config(url="https://example.com/", max_urls=15, formats=("csv",), label="CSV"):
    return rc.CrawlConfig(url=url, max_urls=max_urls, formats=formats, format_label=label)


class FakePopen:
    """Stands in for subprocess.Popen(docker run ...). Optionally writes
    crawl outputs into the run folder named in the command's OUTPUT_DIR,
    the same way the real container would via the bind mount."""

    instances = []

    def __init__(self, lines=(), returncode=0, outputs_dir=None, write_outputs=True,
                 close_reason="finished", crawled=3):
        self._lines = list(lines)
        self._rc = returncode
        self._outputs_dir = outputs_dir
        self._write = write_outputs
        self._reason = close_reason
        self._crawled = crawled
        self.cmd = None

    def __call__(self, cmd, **kwargs):
        self.cmd = cmd
        self.kwargs = kwargs
        FakePopen.instances.append(self)
        if self._write and self._outputs_dir is not None:
            self._write_outputs(cmd)
        self.stdout = io.StringIO("".join(line + "\n" for line in self._lines))
        self.returncode = None
        return self

    def _write_outputs(self, cmd):
        opts = dict(cmd[i + 1].split("=", 1) for i, a in enumerate(cmd) if a == "-s")
        run_rel = opts["OUTPUT_DIR"]  # e.g. outputs/run_20261112-163500
        run_name = run_rel.split("/", 1)[1]
        run_dir = Path(self._outputs_dir) / run_name
        files = {}
        for fmt in opts["EXPORT_FORMATS"].split(","):
            name = rc.FORMAT_FILES[fmt]
            (run_dir / name).write_text("data", encoding="utf-8")
            files[fmt] = f"/app/{run_rel}/{name}"
        profile_files = {}
        for key, name in (("website_profile_json", "website_profile.json"),
                          ("website_profile_md", "website_profile.md")):
            (run_dir / name).write_text("profile", encoding="utf-8")
            profile_files[key] = f"/app/{run_rel}/{name}"
        summary = {
            "max_unique_urls": 15, "discovered": 3, "crawled": self._crawled,
            "failed": 0, "skipped": 0, "unique_urls_registered": 3,
            "emails_found": ["sales@acme-widgets.co"], "close_reason": self._reason,
            "output_files": files,
            "items_extracted": self._crawled, "phones_found": 2, "services_identified": 4,
            "products_identified": 0, "internal_links_discovered": 27,
            "external_links_discovered": 5, "social_profiles_found": 3,
            "robots_txt_blocked": 1, "js_rendering_suspected_pages": 0,
            "profile_files": profile_files,
        }
        (run_dir / "crawl_summary.json").write_text(json.dumps(summary), encoding="utf-8")
        (run_dir / "crawl_summary.txt").write_text("Crawl summary", encoding="utf-8")

    def poll(self):
        return self._rc

    def wait(self, timeout=None):
        self.returncode = self._rc
        return self._rc

    def kill(self):
        pass


# --------------------------------------------------------------------------
# URL validation
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw, expected, added",
    [
        ("https://example.com", "https://example.com/", False),
        ("http://example.com/path?q=1", "http://example.com/path?q=1", False),
        ("  https://example.com/a  ", "https://example.com/a", False),
        ("HTTPS://Example.COM/Path", "https://example.com/Path", False),
        ("https://example.com:8443/x", "https://example.com:8443/x", False),
        ("https://sub.example.co.uk/", "https://sub.example.co.uk/", False),
        ("https://example.com/page#section", "https://example.com/page", False),
        ("https://93.184.216.34/", "https://93.184.216.34/", False),
        ("example.com", "https://example.com/", True),
        ("quotes.toscrape.com/page/2/", "https://quotes.toscrape.com/page/2/", True),
        ("example.com:8080/x", "https://example.com:8080/x", True),
        ("https://bücher.example/", "https://bücher.example/", False),
    ],
)
def test_valid_urls(raw, expected, added):
    assert rc.validate_url(raw) == (expected, added)


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        None,
        "https://exa mple.com",
        "https://example.com/\x00",
        "ftp://example.com",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "mailto:someone@example.com",
        "data:text/html,<script>",
        "http://",
        "https://",
        "https://user:pass@example.com/",
        "https://user@example.com/",
        "https://localhost/",
        "localhost:8080",
        "https://intranet/",
        "https://exa_mple.com/",
        "https://-bad.example.com/",
        "https://example.123/",
        "https://example.com:99999/",
        "https://example.com:abc/",
        "https://" + "a" * 2100 + ".com/",
    ],
)
def test_invalid_urls_rejected(raw):
    with pytest.raises(ValueError):
        rc.validate_url(raw)


def test_invalid_url_message_is_user_friendly():
    with pytest.raises(ValueError, match="http:// and https://"):
        rc.validate_url("ftp://example.com")


def test_url_validation_uses_project_validator():
    with mock.patch.object(rc, "is_valid_url", return_value=False) as m:
        with pytest.raises(ValueError):
            rc.validate_url("https://example.com/")
    m.assert_called_once_with("https://example.com/")


# --------------------------------------------------------------------------
# Max URL validation
# --------------------------------------------------------------------------
@pytest.mark.parametrize("raw, expected", [("", 15), ("  ", 15), (None, 15), ("1", 1),
                                           ("15", 15), (" 250 ", 250), ("1000", 1000)])
def test_valid_max_urls(raw, expected):
    assert rc.validate_max_urls(raw) == expected


@pytest.mark.parametrize("raw", ["0", "1001", "5000", "-5", "+5", "abc", "1.5", "1e3", "١٢"])
def test_invalid_max_urls(raw):
    with pytest.raises(ValueError):
        rc.validate_max_urls(raw)


def test_max_urls_limit_matches_project_cap():
    from webcrawler.crawl_limiter import MAX_UNIQUE_URLS
    assert rc.MAX_URLS_LIMIT == MAX_UNIQUE_URLS == 1000


# --------------------------------------------------------------------------
# Export format selection
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw, label, formats",
    [
        ("1", "CSV", ("csv",)),
        ("2", "JSON", ("json",)),
        ("3", "Excel", ("xlsx",)),
        ("4", "All formats", ("csv", "json", "xlsx")),
        (" 3 ", "Excel", ("xlsx",)),
        ("csv", "CSV", ("csv",)),
        ("Excel", "Excel", ("xlsx",)),
        ("ALL", "All formats", ("csv", "json", "xlsx")),
    ],
)
def test_format_choice(raw, label, formats):
    assert rc.parse_format_choice(raw) == (label, formats)


@pytest.mark.parametrize("raw", ["", "0", "5", "pdf", "1,2", None])
def test_invalid_format_choice(raw):
    with pytest.raises(ValueError):
        rc.parse_format_choice(raw)


def test_formats_are_ones_the_pipeline_supports():
    used = {f for _, fmts in rc.FORMAT_CHOICES.values() for f in fmts}
    assert used == {"csv", "json", "xlsx"}


# --------------------------------------------------------------------------
# Interactive collection
# --------------------------------------------------------------------------
def test_collect_config_reprompts_on_invalid_input():
    out = Collector()
    inp = scripted_input("ftp://bad", "example.com", "0", "abc", "", "9", "4")
    cfg = rc.collect_config(inp, out)
    assert cfg.url == "https://example.com/"
    assert cfg.url_was_completed is True
    assert cfg.max_urls == 15
    assert cfg.formats == ("csv", "json", "xlsx")
    assert out.text.count("Invalid input") == 4


def test_invisible_paste_characters_are_stripped():
    """PowerShell 5.1 pipes prepend a BOM; pasted text can carry
    zero-width spaces. Neither should make a valid answer fail."""
    out = Collector()
    inp = scripted_input("﻿https://quotes.toscrape.com​", "​15", "4﻿")
    cfg = rc.collect_config(inp, out)
    assert cfg.url == "https://quotes.toscrape.com/"
    assert cfg.max_urls == 15 and cfg.formats == ("csv", "json", "xlsx")
    assert "Invalid input" not in out.text
    assert rc.confirm(scripted_input("﻿y"), out) is True


def test_prompts_match_requested_layout():
    out = Collector()
    rc.print_banner(out)
    rc.collect_config(scripted_input("https://example.com", "", "1"), out)
    text = out.text
    assert "          WEB CRAWLER" in text
    assert "Enter website URL:" in text
    assert "Maximum URLs to crawl (default: 15):" in text
    assert "1. CSV\n2. JSON\n3. Excel\n4. All formats" in text
    assert "Enter your choice:" in text


# --------------------------------------------------------------------------
# Docker availability
# --------------------------------------------------------------------------
def test_docker_cli_missing():
    with mock.patch.object(rc.shutil, "which", return_value=None), \
         mock.patch.object(rc.subprocess, "run") as run:
        with pytest.raises(rc.DockerUnavailableError, match="not found"):
            rc.check_docker_available()
    run.assert_not_called()


def test_docker_daemon_not_running():
    err = "error during connect: open //./pipe/dockerDesktopLinuxEngine: The system cannot find the file specified."
    with mock.patch.object(rc.shutil, "which", return_value="docker"), \
         mock.patch.object(rc.subprocess, "run", return_value=completed(1, "", err)):
        with pytest.raises(rc.DockerUnavailableError, match="Docker Desktop is not running") as ei:
            rc.check_docker_available()
    assert "error during connect" in str(ei.value)


def test_docker_check_timeout():
    with mock.patch.object(rc.shutil, "which", return_value="docker"), \
         mock.patch.object(rc.subprocess, "run",
                           side_effect=subprocess.TimeoutExpired(cmd="docker", timeout=30)):
        with pytest.raises(rc.DockerUnavailableError, match="did not respond"):
            rc.check_docker_available()


def test_docker_executable_not_runnable():
    with mock.patch.object(rc.shutil, "which", return_value="docker"), \
         mock.patch.object(rc.subprocess, "run", side_effect=FileNotFoundError):
        with pytest.raises(rc.DockerUnavailableError):
            rc.check_docker_available()


def test_docker_available_returns_version():
    with mock.patch.object(rc.shutil, "which", return_value="docker"), \
         mock.patch.object(rc.subprocess, "run", return_value=completed(0, "29.9.2\n")) as run:
        assert rc.check_docker_available() == "29.9.2"
    args, kwargs = run.call_args
    assert args[0] == ["docker", "info", "--format", "{{.ServerVersion}}"]
    assert kwargs["timeout"] == rc.DOCKER_CHECK_TIMEOUT
    assert "shell" not in kwargs


# --------------------------------------------------------------------------
# Docker image checks
# --------------------------------------------------------------------------
def test_image_exists():
    with mock.patch.object(rc.subprocess, "run", return_value=completed(0, "sha256:abc\n")) as run:
        assert rc.check_image_exists() == "sha256:abc"
    assert run.call_args[0][0] == ["docker", "image", "inspect", "--format", "{{.Id}}",
                                   "webcrawler:latest"]


def test_image_missing_gives_build_hint():
    with mock.patch.object(rc.subprocess, "run",
                           return_value=completed(1, "", "Error: No such image: webcrawler:latest")):
        with pytest.raises(rc.ImageNotFoundError, match="docker build -t webcrawler:latest"):
            rc.check_image_exists()


def test_image_check_other_error():
    with mock.patch.object(rc.subprocess, "run",
                           return_value=completed(1, "", "permission denied")):
        with pytest.raises(rc.LauncherError, match="permission denied"):
            rc.check_image_exists()


def test_image_check_timeout():
    with mock.patch.object(rc.subprocess, "run",
                           side_effect=subprocess.TimeoutExpired(cmd="docker", timeout=30)):
        with pytest.raises(rc.LauncherError, match="Timed out"):
            rc.check_image_exists()


# --------------------------------------------------------------------------
# Command construction
# --------------------------------------------------------------------------
def test_build_docker_command_exact(tmp_path):
    cmd = rc.build_docker_command(make_config(), "20261112-163500", tmp_path)
    assert cmd == [
        "docker", "run", "--rm",
        "--name", "webcrawler-run-20261112-163500",
        "--user", "1000:1000",
        "--memory", "512m",
        "--memory-swap", "512m",
        "--cpus", "1",
        "--security-opt", "no-new-privileges",
        "--cap-drop", "ALL",
        "-v", f"{tmp_path.resolve()}:/app/outputs",
        "webcrawler:latest",
        "crawl", "general",
        "-a", "start_url=https://example.com/",
        "-a", "max_urls=15",
        "-s", "OUTPUT_DIR=outputs/run_20261112-163500",
        "-s", "EXPORT_FORMATS=csv",
        "-s", "LOG_LEVEL=DEBUG",
    ]


@pytest.mark.parametrize("formats, expected", [
    (("csv",), "EXPORT_FORMATS=csv"),
    (("json",), "EXPORT_FORMATS=json"),
    (("xlsx",), "EXPORT_FORMATS=xlsx"),
    (("csv", "json", "xlsx"), "EXPORT_FORMATS=csv,json,xlsx"),
])
def test_command_export_formats(tmp_path, formats, expected):
    cmd = rc.build_docker_command(make_config(formats=formats), "r1", tmp_path)
    assert expected in cmd


def test_command_never_overrides_safety_settings(tmp_path):
    cmd = rc.build_docker_command(make_config(), "r1", tmp_path)
    settings = [cmd[i + 1].split("=", 1)[0] for i, a in enumerate(cmd) if a == "-s"]
    assert set(settings) == {"OUTPUT_DIR", "EXPORT_FORMATS", "LOG_LEVEL"}
    forbidden = ("ROBOTSTXT_OBEY", "USER_AGENT", "DOWNLOAD_DELAY", "AUTOTHROTTLE",
                 "CONCURRENT", "RETRY", "--privileged", "--network")
    joined = " ".join(cmd)
    for word in forbidden:
        assert word not in joined


def test_command_url_is_single_argument(tmp_path):
    url = "https://example.com/search?q=a&b=c;d"
    cmd = rc.build_docker_command(make_config(url=url), "r1", tmp_path)
    assert f"start_url={url}" in cmd
    assert cmd[0] == "docker"


def test_format_command_quotes_special_args():
    rendered = rc.format_command(["docker", "-v", r"C:\My Dir\outputs:/app/outputs",
                                  "-a", "start_url=https://e.com/?a=1&b=2"])
    assert '"C:\\My Dir\\outputs:/app/outputs"' in rendered
    assert '"start_url=https://e.com/?a=1&b=2"' in rendered


# --------------------------------------------------------------------------
# Run folders (stale-report protection)
# --------------------------------------------------------------------------
def test_create_run_dir_is_new_and_unique(tmp_path):
    import datetime as dt
    now = dt.datetime(2026, 11, 12, 16, 35, 0)
    rid1, d1 = rc.create_run_dir(tmp_path, now)
    rid2, d2 = rc.create_run_dir(tmp_path, now)
    assert rid1 == "20261112-163500" and d1 == tmp_path / "run_20261112-163500"
    assert rid2 == "20261112-163500-2" and d2 != d1
    assert d1.is_dir() and d2.is_dir()


def test_create_run_dir_leaves_previous_reports_untouched(tmp_path):
    old = tmp_path / "items.csv"
    old.write_text("old run", encoding="utf-8")
    old_run = tmp_path / "run_20200101-000000"
    old_run.mkdir()
    (old_run / "items.json").write_text("[]", encoding="utf-8")
    rc.create_run_dir(tmp_path)
    assert old.read_text(encoding="utf-8") == "old run"
    assert (old_run / "items.json").read_text(encoding="utf-8") == "[]"


def test_collect_results_maps_container_paths(tmp_path):
    run_dir = tmp_path / "run_r1"
    run_dir.mkdir()
    (run_dir / "items.csv").write_text("x", encoding="utf-8")
    (run_dir / "website_profile.md").write_text("x", encoding="utf-8")
    (run_dir / "crawl_summary.json").write_text(json.dumps({
        "close_reason": "finished", "crawled": 2,
        "output_files": {"csv": "/app/outputs/run_r1/items.csv"},
        "profile_files": {"website_profile_md": "/app/outputs/run_r1/website_profile.md"},
    }), encoding="utf-8")
    res = rc.collect_results(run_dir, "r1", ("csv",))
    assert res.files == {"csv": run_dir / "items.csv"}
    assert res.profile_files == {"website_profile_md": run_dir / "website_profile.md"}
    assert res.missing == [] and res.problems == []


def test_collect_results_flags_missing_profile(tmp_path):
    run_dir = tmp_path / "run_r1"
    run_dir.mkdir()
    (run_dir / "items.csv").write_text("x", encoding="utf-8")
    (run_dir / "crawl_summary.json").write_text(json.dumps({
        "close_reason": "finished", "crawled": 2,
        "output_files": {"csv": "/app/outputs/run_r1/items.csv"},
    }), encoding="utf-8")
    res = rc.collect_results(run_dir, "r1", ("csv",))
    assert res.missing == []
    assert any("website_profile.md" in p for p in res.problems)


def test_collect_results_rejects_stale_paths(tmp_path):
    """A summary pointing at files outside this run's folder (e.g. the old
    outputs/items.csv) must not be reported as this run's output."""
    (tmp_path / "items.csv").write_text("stale", encoding="utf-8")
    run_dir = tmp_path / "run_r1"
    run_dir.mkdir()
    (run_dir / "crawl_summary.json").write_text(json.dumps({
        "close_reason": "finished", "crawled": 2,
        "output_files": {"csv": "/app/outputs/items.csv"},
    }), encoding="utf-8")
    res = rc.collect_results(run_dir, "r1", ("csv",))
    assert res.files == {}
    assert res.missing == ["csv"]
    assert any("not in this run's folder" in p for p in res.problems)


def test_collect_results_without_summary(tmp_path):
    res = rc.collect_results(tmp_path, "r1", ("csv", "json"))
    assert res.summary is None and res.missing == ["csv", "json"]


def test_collect_results_corrupt_summary(tmp_path):
    (tmp_path / "crawl_summary.json").write_text("{not json", encoding="utf-8")
    res = rc.collect_results(tmp_path, "r1", ("csv",))
    assert res.summary is None and "could not be read" in res.problems[0]


# --------------------------------------------------------------------------
# Progress parsing
# --------------------------------------------------------------------------
def test_progress_printer():
    out = Collector()
    p = rc.ProgressPrinter(out)
    for line in [
        "2026-11-12 16:35:00 [scrapy.core.engine] INFO: Spider opened",
        "2026-11-12 16:35:01 [scrapy.core.engine] DEBUG: Crawled (200) <GET https://e.com/robots.txt> (referer: None)",
        "2026-11-12 16:35:02 [scrapy.core.engine] DEBUG: Crawled (200) <GET https://e.com/> (referer: None)",
        "2026-11-12 16:35:03 [scrapy.downloadermiddlewares.robotstxt] DEBUG: Forbidden by robots.txt: <GET https://e.com/private>",
        "2026-11-12 16:35:04 [scrapy.downloadermiddlewares.retry] DEBUG: Retrying <GET https://e.com/x> (failed 1 times): 503",
        "2026-11-12 16:35:05 [general] WARNING: Request failed: https://e.com/x",
        "2026-11-12 16:35:06 [general] INFO: URL cap reached (15); not enqueuing further links.",
        "2026-11-12 16:35:07 [scrapy.core.engine] INFO: Closing spider (finished)",
        "2026-11-12 16:35:07 [scrapy.core.scraper] DEBUG: Scraped from <200 https://e.com/>",
    ]:
        p.feed(line)
    assert p.pages == 1
    t = out.text
    assert "Crawler started." in t
    assert "robots.txt fetched (200)" in t
    assert "[  1] 200 https://e.com/" in t
    assert "blocked by robots.txt (respected): https://e.com/private" in t
    assert "retrying: https://e.com/x" in t
    assert "WARNING: Request failed" in t
    assert "URL limit reached" in t
    assert "Closing spider (finished)" in t


CAP_LINES = [
    "2026-11-12 11:46:54 [general] INFO: URL cap reached (15); not enqueuing further links.",
    "2026-11-12 11:46:54 [general] INFO: URL cap reached; stopping link enqueue.",
]


def test_url_limit_message_shown_once_per_crawl():
    """Regression: the spider logs 'URL cap reached' after every page once
    the cap is full (15x in the first live crawl); show it only once, and
    keep reporting the pages that were already queued."""
    out = Collector()
    p = rc.ProgressPrinter(out)
    for i in range(1, 16):
        p.feed(f"x [scrapy.core.engine] DEBUG: Crawled (200) <GET https://e.com/p{i}> (referer: None)")
        for line in CAP_LINES:
            p.feed(line)
    assert out.text.count("URL limit reached") == 1
    assert p.pages == 15
    assert "[ 15] 200 https://e.com/p15" in out.text
    # the message comes right after the first page, before the rest
    lines = out.lines
    assert lines.index(next(l for l in lines if "URL limit reached" in l)) == 1


def test_url_limit_message_once_in_full_run(tmp_path):
    """Same through run_crawl(): the console shows it once, while the saved
    docker_run.log keeps every original line untouched."""
    lines = []
    for i in range(1, 6):
        lines.append(f"x [scrapy.core.engine] DEBUG: Crawled (200) <GET https://e.com/{i}> (referer: None)")
        lines.extend(CAP_LINES)
    out = Collector()
    log = tmp_path / "docker_run.log"
    with mock.patch.object(rc.subprocess, "Popen", FakePopen(lines=lines)):
        outcome = rc.run_crawl(["docker", "run"], "c1", log, out)
    assert out.text.count("URL limit reached") == 1
    assert outcome.pages_seen == 5
    assert log.read_text(encoding="utf-8").count("URL cap reached") == 10


def test_url_limit_message_resets_for_each_crawl():
    for _ in range(2):
        out = Collector()
        p = rc.ProgressPrinter(out)
        p.feed(CAP_LINES[0])
        p.feed(CAP_LINES[0])
        assert out.text.count("URL limit reached") == 1


def test_progress_hides_scrapy_deprecation_warnings():
    out = Collector()
    p = rc.ProgressPrinter(out)
    p.feed("2026-11-12 [py.warnings] WARNING: /x/engine.py:238: ScrapyDeprecationWarning: "
           "The RANDOMIZE_DOWNLOAD_DELAY setting is deprecated")
    p.feed("2026-11-12 [general] WARNING: Request failed: https://e.com/x")
    assert p.hidden_deprecations == 1
    assert "Deprecation" not in out.text
    assert "Request failed" in out.text


# --------------------------------------------------------------------------
# Running the container (Popen mocked)
# --------------------------------------------------------------------------
def test_run_crawl_streams_and_logs(tmp_path):
    fake = FakePopen(lines=[
        "x [scrapy.core.engine] DEBUG: Crawled (200) <GET https://e.com/> (referer: None)",
    ], returncode=0)
    out = Collector()
    log = tmp_path / "docker_run.log"
    with mock.patch.object(rc.subprocess, "Popen", fake):
        outcome = rc.run_crawl(["docker", "run"], "c1", log, out)
    assert outcome.returncode == 0 and not outcome.interrupted and outcome.pages_seen == 1
    assert "Crawled (200)" in log.read_text(encoding="utf-8")
    assert "# exit code: 0" in log.read_text(encoding="utf-8")
    assert "shell" not in fake.kwargs


def test_run_crawl_docker_missing(tmp_path):
    with mock.patch.object(rc.subprocess, "Popen", side_effect=FileNotFoundError):
        with pytest.raises(rc.DockerUnavailableError):
            rc.run_crawl(["docker", "run"], "c1", tmp_path / "log", Collector())


def test_run_crawl_interrupt_stops_container(tmp_path):
    fake = FakePopen(lines=["a [scrapy.core.engine] INFO: Spider opened", "b", "c"], returncode=0)
    calls = {"n": 0}

    def out(*args):
        calls["n"] += 1
        if calls["n"] == 1:  # Ctrl+C arrives while streaming progress
            raise KeyboardInterrupt

    with mock.patch.object(rc.subprocess, "Popen", fake), \
         mock.patch.object(rc.subprocess, "run", return_value=completed(0)) as run:
        outcome = rc.run_crawl(["docker", "run"], "webcrawler-run-x", tmp_path / "log", out)
    assert outcome.interrupted is True
    run.assert_called_once()
    assert run.call_args[0][0] == ["docker", "stop", "-t", str(rc.STOP_GRACE_SECONDS),
                                   "webcrawler-run-x"]


def test_stop_container_ignores_errors():
    with mock.patch.object(rc.subprocess, "run", side_effect=subprocess.TimeoutExpired("docker", 1)):
        rc.stop_container("x")  # must not raise


@pytest.mark.parametrize("code, text", [(0, "normally"), (125, "could not start"),
                                        (137, "memory limit"), (2, "code 2")])
def test_describe_exit_code(code, text):
    assert text in rc.describe_exit_code(code)


# --------------------------------------------------------------------------
# main(): end-to-end flow with everything mocked
# --------------------------------------------------------------------------
def _docker_ok():
    """Patch which/run so Docker looks available and the image exists."""
    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["docker", "info"]:
            return completed(0, "29.9.2\n")
        if cmd[:3] == ["docker", "image", "inspect"]:
            return completed(0, "sha256:abc\n")
        return completed(0)
    return (mock.patch.object(rc.shutil, "which", return_value="docker"),
            mock.patch.object(rc.subprocess, "run", side_effect=fake_run))


def test_main_user_declines_confirmation(tmp_path):
    out = Collector()
    with mock.patch.object(rc.subprocess, "run") as run, \
         mock.patch.object(rc.subprocess, "Popen") as popen:
        code = rc.main([], scripted_input("example.com", "", "1", "n"), out, tmp_path)
    assert code == rc.EXIT_CANCELLED
    assert "Cancelled" in out.text
    run.assert_not_called()
    popen.assert_not_called()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("answers", [
    (),                                      # EOF at the URL prompt
    ("example.com",),                        # EOF at max URLs
    ("example.com", "", KeyboardInterrupt()),  # Ctrl+C at format menu
    ("example.com", "", "1", KeyboardInterrupt()),  # Ctrl+C at confirmation
])
def test_main_cancel_during_prompts(tmp_path, answers):
    out = Collector()
    with mock.patch.object(rc.subprocess, "run") as run, \
         mock.patch.object(rc.subprocess, "Popen") as popen:
        code = rc.main([], scripted_input(*answers), out, tmp_path)
    assert code == rc.EXIT_CANCELLED
    run.assert_not_called()
    popen.assert_not_called()


def test_main_docker_not_running(tmp_path):
    out = Collector()
    with mock.patch.object(rc.shutil, "which", return_value="docker"), \
         mock.patch.object(rc.subprocess, "run", return_value=completed(1, "", "cannot connect")), \
         mock.patch.object(rc.subprocess, "Popen") as popen:
        code = rc.main([], scripted_input("example.com", "", "1", "y"), out, tmp_path)
    assert code == rc.EXIT_ERROR
    assert "Docker Desktop is not running" in out.text
    popen.assert_not_called()
    assert list(tmp_path.iterdir()) == []


def test_main_image_missing(tmp_path):
    out = Collector()

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["docker", "info"]:
            return completed(0, "29.9.2\n")
        return completed(1, "", "Error: No such image: webcrawler:latest")

    with mock.patch.object(rc.shutil, "which", return_value="docker"), \
         mock.patch.object(rc.subprocess, "run", side_effect=fake_run), \
         mock.patch.object(rc.subprocess, "Popen") as popen:
        code = rc.main([], scripted_input("example.com", "", "1", "y"), out, tmp_path)
    assert code == rc.EXIT_ERROR
    assert "docker build" in out.text
    popen.assert_not_called()


def test_main_dry_run_does_not_start_container(tmp_path):
    out = Collector()
    which, run = _docker_ok()
    with which, run, mock.patch.object(rc.subprocess, "Popen") as popen:
        code = rc.main(["--dry-run"], scripted_input("example.com", "20", "4", "y"), out, tmp_path)
    assert code == rc.EXIT_OK
    popen.assert_not_called()
    assert "EXPORT_FORMATS=csv,json,xlsx" in out.text
    assert "max_urls=20" in out.text
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("choice, formats", [("1", ["csv"]), ("2", ["json"]),
                                             ("3", ["xlsx"]), ("4", ["csv", "json", "xlsx"])])
def test_main_success_each_format(tmp_path, choice, formats):
    (tmp_path / "items.csv").write_text("previous run", encoding="utf-8")
    out = Collector()
    fake = FakePopen(outputs_dir=tmp_path)
    which, run = _docker_ok()
    with which, run, mock.patch.object(rc.subprocess, "Popen", fake):
        code = rc.main([], scripted_input("https://example.com", "", choice, "y"), out, tmp_path)
    assert code == rc.EXIT_OK, out.text
    assert "CRAWL STATUS: SUCCESS" in out.text
    assert f"EXPORT_FORMATS={','.join(formats)}" in fake.cmd
    run_dirs = [p for p in tmp_path.iterdir() if p.name.startswith("run_")]
    assert len(run_dirs) == 1
    produced = {p.name for p in run_dirs[0].iterdir()}
    assert produced == {rc.FORMAT_FILES[f] for f in formats} | {
        "crawl_summary.json", "crawl_summary.txt", "docker_run.log",
        "website_profile.md", "website_profile.json"}
    assert str(run_dirs[0] / "website_profile.md") in out.text
    for f in formats:
        assert str(run_dirs[0] / rc.FORMAT_FILES[f]) in out.text
    # previous report untouched
    assert (tmp_path / "items.csv").read_text(encoding="utf-8") == "previous run"


def test_main_reports_extraction_summary(tmp_path):
    out = Collector()
    fake = FakePopen(outputs_dir=tmp_path,
                     lines=["x [scrapy.core.scraper] DEBUG: Scraped from <200 https://e.com/>",
                            "{'main_text': 'SECRET FULL PAGE CONTENT', 'url': 'https://e.com/'}"])
    which, run = _docker_ok()
    with which, run, mock.patch.object(rc.subprocess, "Popen", fake):
        code = rc.main([], scripted_input("https://example.com", "", "4", "y"), out, tmp_path)
    assert code == rc.EXIT_OK
    t = out.text
    for label, value in [("Items extracted", 3), ("Emails found", 1), ("Phone numbers found", 2),
                         ("Services identified", 4), ("Products identified", 0),
                         ("Internal links found", 27), ("Blocked by robots.txt", 1)]:
        assert any(line.strip().startswith(label) and line.rstrip().endswith(f": {value}")
                   for line in out.lines), label
    assert "website_profile.md" in t and "website_profile.json" in t
    assert "only the 3 crawled page(s) were analysed" in t
    assert "SECRET FULL PAGE CONTENT" not in t  # page content is never printed


def test_cap_drop_all_are_separate_arguments(tmp_path):
    cmd = rc.build_docker_command(make_config(), "r1", tmp_path)
    i = cmd.index("--cap-drop")
    assert cmd[i + 1] == "ALL"
    assert "--cap-drop ALL" not in cmd and "--cap-drop=ALL" not in cmd


def test_main_crawl_failure_reports_failed(tmp_path):
    out = Collector()
    fake = FakePopen(lines=["Traceback ...", "ValueError: Invalid or unsafe start_url"],
                     returncode=1, outputs_dir=tmp_path, write_outputs=False)
    which, run = _docker_ok()
    with which, run, mock.patch.object(rc.subprocess, "Popen", fake):
        code = rc.main([], scripted_input("https://example.com", "", "1", "y"), out, tmp_path)
    assert code == rc.EXIT_ERROR
    assert "CRAWL STATUS: FAILED" in out.text
    assert "NOT CREATED" in out.text
    assert "docker_run.log" in out.text


def test_main_oom_kill_reported(tmp_path):
    out = Collector()
    fake = FakePopen(returncode=137, outputs_dir=tmp_path, write_outputs=False)
    which, run = _docker_ok()
    with which, run, mock.patch.object(rc.subprocess, "Popen", fake):
        code = rc.main([], scripted_input("https://example.com", "", "1", "y"), out, tmp_path)
    assert code == rc.EXIT_ERROR
    assert "memory limit" in out.text


def test_main_zero_pages_crawled(tmp_path):
    out = Collector()
    fake = FakePopen(outputs_dir=tmp_path, crawled=0)
    which, run = _docker_ok()
    with which, run, mock.patch.object(rc.subprocess, "Popen", fake):
        code = rc.main([], scripted_input("https://example.com", "", "1", "y"), out, tmp_path)
    assert code == rc.EXIT_OK
    assert "no pages crawled" in out.text
