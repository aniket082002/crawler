#!/usr/bin/env python3
"""Interactive launcher for the Docker-based WebCrawler.

Usage:
    python run_crawler.py            # interactive crawl
    python run_crawler.py --dry-run  # same prompts/checks, prints the Docker
                                     # command instead of running it

This script does NOT re-implement any crawling. It collects a URL, a URL
limit and an export format, then runs the existing `general` Scrapy spider
inside the existing `webcrawler:latest` image:

  * the URL and limit are passed as the spider's existing `-a start_url` /
    `-a max_urls` arguments;
  * the export format is passed as the `EXPORT_FORMATS` setting read by
    webcrawler/pipelines.py (`-s` is Scrapy's standard setting override);
  * every run writes into its own fresh folder, outputs/run_<timestamp>/,
    via the existing `OUTPUT_DIR` setting, so reports from earlier runs are
    never overwritten and can't be mistaken for the current run.

All politeness/safety settings (robots.txt, User-Agent, delays,
AutoThrottle, concurrency, retries) come from webcrawler/settings.py and are
not overridden here.

Only the Python standard library is used, plus the project's stdlib-only
helpers (url_validator, crawl_limiter) -- scrapy is never imported on the
host, so this runs on Windows despite the Application Control restriction.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import ipaddress
import json
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit, urlunsplit

PROJECT_DIR = Path(__file__).resolve().parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

# Both modules are stdlib-only (no scrapy import), safe on the Windows host.
from webcrawler.crawl_limiter import MAX_UNIQUE_URLS  # noqa: E402
from webcrawler.url_validator import is_valid_url  # noqa: E402

# --- Configuration ---------------------------------------------------------
IMAGE = "webcrawler:latest"
SPIDER = "general"
OUTPUTS_DIR = PROJECT_DIR / "outputs"
CONTAINER_OUTPUTS = PurePosixPath("/app/outputs")

DEFAULT_MAX_URLS = 15
MAX_URLS_LIMIT = MAX_UNIQUE_URLS  # project-wide hard cap (1000)
MAX_URL_LENGTH = 2048

DOCKER_MEMORY = "512m"
DOCKER_CPUS = "1"
CONTAINER_USER = "1000:1000"  # the image's non-root "crawler" user
DOCKER_CHECK_TIMEOUT = 30  # seconds
STOP_GRACE_SECONDS = 15

# Scrapy's per-request "Crawled (200) <GET ...>" and robots.txt-block lines
# are logged at DEBUG; this only changes log verbosity (for live progress
# and the saved docker_run.log), not crawling behaviour.
LOG_LEVEL = "DEBUG"

# Menu choice -> (label, formats understood by pipelines.EXPORT_FORMATS)
FORMAT_CHOICES = {
    "1": ("CSV", ("csv",)),
    "2": ("JSON", ("json",)),
    "3": ("Excel", ("xlsx",)),
    "4": ("All formats", ("csv", "json", "xlsx")),
}
FORMAT_ALIASES = {
    "csv": "1",
    "json": "2",
    "excel": "3",
    "xlsx": "3",
    "all": "4",
    "all formats": "4",
}
FORMAT_LABELS = {"csv": "CSV", "json": "JSON", "xlsx": "Excel"}
FORMAT_FILES = {"csv": "items.csv", "json": "items.json", "xlsx": "items.xlsx"}

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CANCELLED = 130

LINE = "=" * 40
THIN = "-" * 40


# --- Errors ----------------------------------------------------------------
class LauncherError(Exception):
    """A problem that stops the launcher; message is shown to the user."""


class DockerUnavailableError(LauncherError):
    pass


class ImageNotFoundError(LauncherError):
    pass


class UserCancelled(Exception):
    pass


@dataclass
class CrawlConfig:
    url: str
    max_urls: int
    formats: tuple
    format_label: str
    url_was_completed: bool = False  # True if https:// was added for the user


@dataclass
class RunOutcome:
    returncode: int | None
    interrupted: bool
    elapsed: float
    pages_seen: int = 0


@dataclass
class RunResult:
    summary: dict | None
    files: dict = field(default_factory=dict)  # fmt -> host Path
    missing: list = field(default_factory=list)  # fmts expected but absent
    problems: list = field(default_factory=list)
    profile_files: dict = field(default_factory=dict)  # name -> host Path


# --- Input validation ------------------------------------------------------
_SCHEME_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.-]*):(.*)$", re.DOTALL)
_PORT_ONLY_RE = re.compile(r"^\d+([/?#].*)?$", re.DOTALL)
_LABEL_RE = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")


def _valid_hostname(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    try:
        ascii_host = host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return False
    if ascii_host.endswith("."):
        ascii_host = ascii_host[:-1]
    if not ascii_host or len(ascii_host) > 253:
        return False
    labels = ascii_host.split(".")
    if len(labels) < 2:
        return False  # "localhost", "intranet" etc. are not public websites
    if labels[-1].isdigit():
        return False  # e.g. "1.2.3" -- not a valid IP and not a real TLD
    return all(_LABEL_RE.match(label) for label in labels)


def validate_url(raw: str) -> tuple[str, bool]:
    """Validate and normalize a user-entered website URL.

    Returns (normalized_url, scheme_was_added). Raises ValueError with a
    user-friendly message when the URL is not acceptable.
    """
    if raw is None:
        raise ValueError("URL cannot be empty.")
    url = raw.strip()
    if not url:
        raise ValueError("URL cannot be empty.")
    if len(url) > MAX_URL_LENGTH:
        raise ValueError(f"URL is too long (maximum {MAX_URL_LENGTH} characters).")
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in url):
        raise ValueError("URL must not contain spaces or control characters.")

    added = False
    if "://" in url:
        scheme = url.split("://", 1)[0].lower()
        if scheme not in ("http", "https"):
            raise ValueError("Only http:// and https:// URLs are supported.")
    else:
        m = _SCHEME_RE.match(url)
        if m and not _PORT_ONLY_RE.match(m.group(2)):
            # e.g. javascript:..., mailto:..., data:..., file:...
            raise ValueError("Only http:// and https:// URLs are supported.")
        url = "https://" + url
        added = True

    # The project's own validator: http/https scheme + non-empty host.
    if not is_valid_url(url):
        raise ValueError("Not a valid http(s) URL.")

    parts = urlsplit(url)
    if "@" in parts.netloc:
        raise ValueError("URLs with embedded usernames/passwords are not allowed.")
    try:
        port = parts.port
    except ValueError:
        raise ValueError("URL has an invalid port number.") from None
    if port is not None and not (1 <= port <= 65535):
        raise ValueError("URL has an invalid port number.")
    host = parts.hostname or ""
    if not _valid_hostname(host):
        raise ValueError(f"'{host or url}' is not a valid public website domain.")

    normalized = urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", parts.query, "")
    )
    return normalized, added


def validate_max_urls(raw: str | None) -> int:
    """Blank -> default (15). Otherwise a whole number in 1..MAX_URLS_LIMIT."""
    value = (raw or "").strip()
    if not value:
        return DEFAULT_MAX_URLS
    if not value.isdigit() or not value.isascii():
        raise ValueError("Please enter a whole number, e.g. 15.")
    n = int(value)
    if n < 1 or n > MAX_URLS_LIMIT:
        raise ValueError(f"Maximum URLs must be between 1 and {MAX_URLS_LIMIT}.")
    return n


def parse_format_choice(raw: str | None) -> tuple[str, tuple]:
    """Menu number (1-4) or name (csv/json/excel/all) -> (label, formats)."""
    value = (raw or "").strip().lower()
    key = FORMAT_ALIASES.get(value, value)
    if key not in FORMAT_CHOICES:
        raise ValueError("Please enter 1, 2, 3 or 4.")
    return FORMAT_CHOICES[key]


# --- Interactive prompts ---------------------------------------------------
# Invisible characters that sneak in via copy/paste or PowerShell pipes
# (byte-order mark, zero-width spaces/joiners, word joiner).
_INVISIBLE = "﻿​‌‍⁠"


def clean_answer(raw: str | None) -> str:
    return (raw or "").strip().strip(_INVISIBLE).strip()


def _ask(question: str, validator, input_func, out):
    """Ask until the validator accepts. EOF -> UserCancelled."""
    while True:
        out(question)
        try:
            answer = clean_answer(input_func("> "))
        except EOFError:
            raise UserCancelled() from None
        try:
            return validator(answer)
        except ValueError as exc:
            out(f"  Invalid input: {exc}\n")


def print_banner(out=print):
    out(LINE)
    out("          WEB CRAWLER")
    out(LINE)
    out("")


def collect_config(input_func=input, out=print) -> CrawlConfig:
    url, added = _ask("Enter website URL:", validate_url, input_func, out)
    out("")
    max_urls = _ask(
        f"Maximum URLs to crawl (default: {DEFAULT_MAX_URLS}):",
        validate_max_urls,
        input_func,
        out,
    )
    out("")
    menu = "Select export format:\n\n" + "\n".join(
        f"{k}. {label}" for k, (label, _) in FORMAT_CHOICES.items()
    ) + "\n\nEnter your choice:"
    label, formats = _ask(menu, parse_format_choice, input_func, out)
    out("")
    return CrawlConfig(url, max_urls, formats, label, url_was_completed=added)


def show_summary(config: CrawlConfig, outputs_dir: Path, out=print):
    files = ", ".join(FORMAT_FILES[f] for f in config.formats)
    note = "  (https:// added)" if config.url_was_completed else ""
    out(THIN)
    out("Configuration summary")
    out(THIN)
    out(f"  Website URL   : {config.url}{note}")
    out(f"  Maximum URLs  : {config.max_urls}")
    out(f"  Export format : {config.format_label} ({files})")
    out(f"  Output folder : {outputs_dir / 'run_<timestamp>'}")
    out(f"  Docker image  : {IMAGE}")
    out(f"  Limits        : {DOCKER_MEMORY.upper()}B RAM, {DOCKER_CPUS} CPU, non-root")
    out("  Politeness    : robots.txt obeyed, project delays/AutoThrottle")
    out(THIN)


def confirm(input_func=input, out=print) -> bool:
    try:
        answer = input_func("Start crawl? [y/N]: ")
    except EOFError:
        raise UserCancelled() from None
    return clean_answer(answer).lower() in ("y", "yes")


# --- Docker checks ---------------------------------------------------------
def _first_line(text: str | None) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def check_docker_available() -> str:
    """Return the Docker engine version, or raise DockerUnavailableError."""
    if shutil.which("docker") is None:
        raise DockerUnavailableError(
            "The 'docker' command was not found. Install Docker Desktop and "
            "make sure it is on your PATH."
        )
    try:
        proc = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=DOCKER_CHECK_TIMEOUT,
        )
    except FileNotFoundError:
        raise DockerUnavailableError(
            "The 'docker' command could not be executed. Is Docker Desktop installed?"
        ) from None
    except subprocess.TimeoutExpired:
        raise DockerUnavailableError(
            f"Docker did not respond within {DOCKER_CHECK_TIMEOUT}s. "
            "Docker Desktop may still be starting -- wait and try again."
        ) from None
    version = (proc.stdout or "").strip()
    if proc.returncode != 0 or not version:
        detail = _first_line(proc.stderr) or _first_line(proc.stdout)
        raise DockerUnavailableError(
            "Docker Desktop is not running (or the Docker engine is unreachable). "
            "Start Docker Desktop and try again."
            + (f"\n  Docker said: {detail}" if detail else "")
        )
    return version


def check_image_exists(image: str = IMAGE) -> str:
    """Return the image ID, or raise ImageNotFoundError / LauncherError."""
    try:
        proc = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", image],
            capture_output=True,
            text=True,
            timeout=DOCKER_CHECK_TIMEOUT,
        )
    except FileNotFoundError:
        raise DockerUnavailableError("The 'docker' command could not be executed.") from None
    except subprocess.TimeoutExpired:
        raise LauncherError(f"Timed out while checking for image {image}.") from None
    if proc.returncode == 0:
        return (proc.stdout or "").strip()
    stderr = proc.stderr or ""
    if "no such image" in stderr.lower() or "not found" in stderr.lower():
        raise ImageNotFoundError(
            f"Docker image '{image}' was not found.\n"
            f"  Build it once from the project folder with:\n"
            f"    docker build -t {image} ."
        )
    raise LauncherError(
        f"Could not inspect Docker image '{image}': {_first_line(stderr) or 'unknown error'}"
    )


# --- Run folder & command --------------------------------------------------
def make_run_id(now: _dt.datetime | None = None) -> str:
    return (now or _dt.datetime.now()).strftime("%Y%m%d-%H%M%S")


def next_run_id(outputs_dir: Path, now: _dt.datetime | None = None) -> str:
    base = make_run_id(now)
    run_id, n = base, 1
    while (outputs_dir / f"run_{run_id}").exists():
        n += 1
        run_id = f"{base}-{n}"
    return run_id


def create_run_dir(outputs_dir: Path, now: _dt.datetime | None = None) -> tuple[str, Path]:
    """Create a brand-new outputs/run_<id>/ folder. mkdir(exist_ok=False)
    guarantees the folder did not exist before, so everything found in it
    afterwards was produced by this run."""
    outputs_dir.mkdir(parents=True, exist_ok=True)
    for _ in range(100):
        run_id = next_run_id(outputs_dir, now)
        run_dir = outputs_dir / f"run_{run_id}"
        try:
            run_dir.mkdir(exist_ok=False)
            return run_id, run_dir
        except FileExistsError:
            continue
    raise LauncherError("Could not create a unique run folder under outputs/.")


def container_name_for(run_id: str) -> str:
    return f"webcrawler-run-{run_id}"


def build_docker_command(config: CrawlConfig, run_id: str, outputs_dir: Path,
                         image: str = IMAGE) -> list[str]:
    """Build the `docker run` argument list (never passed through a shell)."""
    return [
        "docker", "run", "--rm",
        "--name", container_name_for(run_id),
        "--user", CONTAINER_USER,
        "--memory", DOCKER_MEMORY,
        "--memory-swap", DOCKER_MEMORY,
        "--cpus", DOCKER_CPUS,
        "--security-opt", "no-new-privileges",
        "--cap-drop", "ALL",
        "-v", f"{Path(outputs_dir).resolve()}:{CONTAINER_OUTPUTS}",
        image,
        "crawl", SPIDER,
        "-a", f"start_url={config.url}",
        "-a", f"max_urls={config.max_urls}",
        "-s", f"OUTPUT_DIR=outputs/run_{run_id}",
        "-s", f"EXPORT_FORMATS={','.join(config.formats)}",
        "-s", f"LOG_LEVEL={LOG_LEVEL}",
    ]


def format_command(cmd: list[str]) -> str:
    """Human-readable, PowerShell-pasteable rendering of the command."""
    def q(arg):
        return f'"{arg}"' if re.search(r"[\s&;|<>()'`$]", arg) else arg
    return " ".join(q(a) for a in cmd)


# --- Progress display ------------------------------------------------------
_CRAWLED_RE = re.compile(r"Crawled \((\d{3})\) <(\w+) (\S+)>")
_ROBOTS_RE = re.compile(r"Forbidden by robots\.txt: <\w+ (\S+)>")
_RETRY_RE = re.compile(r"Retrying <\w+ (\S+)>")
_GAVEUP_RE = re.compile(r"Gave up retrying <\w+ (\S+)>")
_LEVEL_RE = re.compile(r"\] (WARNING|ERROR|CRITICAL): (.*)$")
_STATS_RE = re.compile(r"(Crawled \d+ pages \(at .*)$")
_CLOSING_RE = re.compile(r"Closing spider \((.*)\)")


class ProgressPrinter:
    """Turns raw Scrapy log lines into short progress messages."""

    def __init__(self, out=print):
        self.out = out
        self.pages = 0
        self.hidden_deprecations = 0
        self.cap_announced = False  # show "URL limit reached" once per crawl

    def feed(self, line: str) -> None:
        line = line.rstrip("\r\n")
        if not line:
            return
        if "ScrapyDeprecationWarning" in line:
            # Scrapy 2.20 API-deprecation notices about the project's existing
            # middleware/pipeline signatures: harmless, kept in the log only.
            self.hidden_deprecations += 1
        elif m := _GAVEUP_RE.search(line):
            self.out(f"  ! gave up after retries: {m.group(1)}")
        elif m := _RETRY_RE.search(line):
            self.out(f"  ~ retrying: {m.group(1)}")
        elif m := _ROBOTS_RE.search(line):
            self.out(f"  - blocked by robots.txt (respected): {m.group(1)}")
        elif m := _CRAWLED_RE.search(line):
            status, url = m.group(1), m.group(3)
            if url.rstrip("/").endswith("/robots.txt"):
                self.out(f"  robots.txt fetched ({status}): {url}")
            else:
                self.pages += 1
                self.out(f"  [{self.pages:>3}] {status} {url}")
        elif m := _LEVEL_RE.search(line):
            self.out(f"  ! {m.group(1)}: {m.group(2)}")
        elif "URL cap reached" in line and "INFO" in line:
            # The spider logs this on every page parsed after the cap fills;
            # already-queued pages still finish, so announce it only once.
            if not self.cap_announced:
                self.cap_announced = True
                self.out("  URL limit reached - no new links will be queued; "
                         "pages already queued will finish.")
        elif m := _STATS_RE.search(line):
            self.out(f"  ... {m.group(1)}")
        elif "] INFO: Spider opened" in line and "scrapy.core.engine" in line:
            self.out("  Crawler started.")
        elif m := _CLOSING_RE.search(line):
            self.out(f"  Closing spider ({m.group(1)})...")


# --- Running the container -------------------------------------------------
def _pump(stream, q):
    try:
        for line in iter(stream.readline, ""):
            q.put(line)
    finally:
        q.put(None)


def stop_container(name: str, kill: bool = False) -> None:
    cmd = ["docker", "kill", name] if kill else ["docker", "stop", "-t", str(STOP_GRACE_SECONDS), name]
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=STOP_GRACE_SECONDS + 45)
    except (OSError, subprocess.SubprocessError):
        pass  # container may already be gone (--rm); nothing else to do


def run_crawl(cmd: list[str], container_name: str, log_path: Path, out=print) -> RunOutcome:
    """Run the container, stream progress, save the full log. On Ctrl+C,
    stop the container gracefully (Scrapy then closes the exporters and
    writes the summary for the partial crawl); a second Ctrl+C kills it."""
    start = time.monotonic()
    printer = ProgressPrinter(out)
    interrupted = False

    with open(log_path, "w", encoding="utf-8") as log:
        log.write(f"# command: {format_command(cmd)}\n")
        log.write(f"# started: {_dt.datetime.now().isoformat(timespec='seconds')}\n\n")
        log.flush()
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except FileNotFoundError:
            raise DockerUnavailableError("The 'docker' command could not be executed.") from None
        except OSError as exc:
            raise LauncherError(f"Could not start Docker: {exc}") from None

        q: queue.Queue = queue.Queue()
        reader = threading.Thread(target=_pump, args=(proc.stdout, q), daemon=True)
        reader.start()

        def handle(line):
            log.write(line)
            log.flush()
            printer.feed(line)

        def drain():
            while True:
                try:
                    line = q.get(timeout=0.5)
                except queue.Empty:
                    if proc.poll() is not None and not reader.is_alive():
                        return
                    continue
                if line is None:
                    return
                handle(line)

        try:
            drain()
        except KeyboardInterrupt:
            interrupted = True
            out("\n  Interrupt received - stopping the crawl gracefully "
                f"(up to {STOP_GRACE_SECONDS}s). Press Ctrl+C again to force stop.")
            stop_container(container_name)
            try:
                drain()
            except KeyboardInterrupt:
                out("  Forcing container stop...")
                stop_container(container_name, kill=True)
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    proc.kill()

        try:
            rc = proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            rc = proc.wait()
        log.write(f"\n# exit code: {rc}\n")

    if printer.hidden_deprecations:
        out(f"  ({printer.hidden_deprecations} Scrapy deprecation notices hidden here; "
            "they are harmless and kept in docker_run.log)")
    return RunOutcome(rc, interrupted, time.monotonic() - start, printer.pages)


def describe_exit_code(rc: int | None) -> str:
    if rc == 0:
        return "Container exited normally."
    if rc == 125:
        return "Docker could not start the container (exit 125). See the log for Docker's message."
    if rc in (126, 127):
        return f"The crawl command could not be executed inside the image (exit {rc})."
    if rc == 137:
        return ("Container was killed (exit 137) - it was force-stopped or hit the "
                f"{DOCKER_MEMORY.upper()}B memory limit.")
    if rc == 143:
        return "Container was terminated (exit 143)."
    return f"Crawler exited with code {rc}."


# --- Results ---------------------------------------------------------------
def collect_results(run_dir: Path, run_id: str, formats: tuple) -> RunResult:
    """Read this run's crawl_summary.json and map its container paths to
    host paths. Only files inside this run's folder are accepted."""
    summary_path = run_dir / "crawl_summary.json"
    if not summary_path.is_file():
        return RunResult(
            summary=None,
            missing=list(formats),
            problems=["No crawl_summary.json was written - the crawl did not finish normally."],
        )
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return RunResult(summary=None, missing=list(formats),
                         problems=[f"crawl_summary.json could not be read: {exc}"])

    result = RunResult(summary=summary)
    container_run_dir = CONTAINER_OUTPUTS / f"run_{run_id}"

    def host_path(name, cpath):
        try:
            rel = PurePosixPath(str(cpath)).relative_to(container_run_dir)
        except ValueError:
            result.problems.append(f"{name}: '{cpath}' is not in this run's folder - ignored.")
            return None
        host = run_dir.joinpath(*rel.parts)
        if not host.is_file():
            result.problems.append(f"{name}: listed in summary but file not found: {host}")
            return None
        return host

    listed = summary.get("output_files") or {}
    for fmt, cpath in listed.items():
        if fmt not in formats:
            result.problems.append(f"Unexpected format '{fmt}' in summary (not requested).")
        host = host_path(fmt, cpath)
        if host:
            result.files[fmt] = host
    result.missing = [f for f in formats if f not in result.files]

    for name, cpath in (summary.get("profile_files") or {}).items():
        host = host_path(name, cpath)
        if host:
            result.profile_files[name] = host
    if "website_profile_md" not in result.profile_files:
        result.problems.append("The website profile report (website_profile.md) was not generated.")
    return result


def crawl_status(outcome: RunOutcome, result: RunResult) -> tuple[str, bool]:
    """Return (status text, success flag)."""
    if outcome.interrupted:
        return "INTERRUPTED (partial results)", False
    if result.summary is None or outcome.returncode != 0:
        return "FAILED", False
    reason = result.summary.get("close_reason")
    if result.missing:
        return "FAILED (missing report files)", False
    if reason != "finished":
        return f"STOPPED ({reason})", False
    if not result.summary.get("crawled"):
        return "FINISHED - no pages crawled (check robots.txt / the log)", True
    return "SUCCESS", True


def print_report(config, run_dir, log_path, outcome, result, out=print):
    status, _ = crawl_status(outcome, result)
    mins, secs = divmod(int(outcome.elapsed), 60)
    out("")
    out(LINE)
    out(f" CRAWL STATUS: {status}")
    out(LINE)
    if outcome.returncode not in (0, None) or outcome.interrupted:
        out(f"  {describe_exit_code(outcome.returncode)}")
    s = result.summary
    if s:
        out(f"  Close reason  : {s.get('close_reason')}")
        out(f"  Start URL     : {config.url}")
        out(f"  URL limit     : {s.get('max_unique_urls')}")
        out(f"  Discovered    : {s.get('discovered')}")
        out(f"  Crawled       : {s.get('crawled')}")
        out(f"  Failed        : {s.get('failed')}")
        out(f"  Skipped       : {s.get('skipped')}")
        out("")
        out("Extraction results:")
        rows = [
            ("Items extracted", s.get("items_extracted")),
            ("Emails found", len(s.get("emails_found") or [])),
            ("Phone numbers found", s.get("phones_found")),
            ("Services identified", s.get("services_identified")),
            ("Products identified", s.get("products_identified")),
            ("Internal links found", s.get("internal_links_discovered")),
            ("External links found", s.get("external_links_discovered")),
            ("Social profiles found", s.get("social_profiles_found")),
            ("Blocked by robots.txt", s.get("robots_txt_blocked")),
            ("JS-rendered pages (maybe incomplete)", s.get("js_rendering_suspected_pages")),
        ]
        for label, value in rows:
            out(f"  {label:<37}: {'n/a' if value is None else value}")
    out(f"  Duration      : {mins}m {secs:02d}s")
    for p in result.problems:
        out(f"  ! {p}")
    if s:
        out(f"  Note: only the {s.get('crawled')} crawled page(s) were analysed; pages beyond the URL "
            "limit, blocked pages and JavaScript-rendered content are not included.")
    out("")
    out("Reports for THIS run (older runs are untouched):")
    for fmt in config.formats:
        path = result.files.get(fmt)
        label = f"{FORMAT_LABELS[fmt]} ({FORMAT_FILES[fmt]})"
        out(f"  {label:<20}: {path if path else 'NOT CREATED'}")
    for key, name in (("website_profile_md", "website_profile.md"),
                      ("website_profile_json", "website_profile.json")):
        path = result.profile_files.get(key)
        out(f"  {name:<20}: {path if path else 'NOT CREATED'}")
    for name in ("crawl_summary.json", "crawl_summary.txt"):
        p = run_dir / name
        out(f"  {name:<20}: {p if p.is_file() else 'NOT CREATED'}")
    out(f"  {'Docker log':<20}: {log_path}")
    out(f"  {'Run folder':<20}: {run_dir}")


# --- Main ------------------------------------------------------------------
def parse_args(argv):
    parser = argparse.ArgumentParser(description="Interactive launcher for the Docker WebCrawler.")
    parser.add_argument("--dry-run", action="store_true",
                        help="ask the questions and run the checks, then print the "
                             "docker command instead of running it")
    return parser.parse_args(argv)


def main(argv=None, input_func=input, out=print, outputs_dir: Path | None = None) -> int:
    args = parse_args(argv)
    outputs_dir = Path(outputs_dir or OUTPUTS_DIR)

    print_banner(out)
    try:
        config = collect_config(input_func, out)
        show_summary(config, outputs_dir, out)
        if not confirm(input_func, out):
            out("Cancelled. Nothing was run.")
            return EXIT_CANCELLED
    except (KeyboardInterrupt, UserCancelled):
        out("\nCancelled by user. Nothing was run.")
        return EXIT_CANCELLED

    # Re-validate after confirmation (defence in depth).
    try:
        validate_url(config.url)
        validate_max_urls(str(config.max_urls))
    except ValueError as exc:
        out(f"ERROR: {exc}")
        return EXIT_ERROR

    out("")
    out("Checking Docker...")
    try:
        version = check_docker_available()
        out(f"  Docker engine {version} is running.")
        check_image_exists(IMAGE)
        out(f"  Image {IMAGE} found.")
    except LauncherError as exc:
        out(f"ERROR: {exc}")
        return EXIT_ERROR
    except KeyboardInterrupt:
        out("\nCancelled by user. Nothing was run.")
        return EXIT_CANCELLED

    if args.dry_run:
        run_id = next_run_id(outputs_dir)
        cmd = build_docker_command(config, run_id, outputs_dir)
        out("")
        out("Dry run - the crawl was NOT started. Command that would run:")
        out(f"  {format_command(cmd)}")
        return EXIT_OK

    try:
        run_id, run_dir = create_run_dir(outputs_dir)
    except (OSError, LauncherError) as exc:
        out(f"ERROR: could not create the run folder: {exc}")
        return EXIT_ERROR
    cmd = build_docker_command(config, run_id, outputs_dir)
    log_path = run_dir / "docker_run.log"

    out("")
    out(f"Starting crawl (run {run_id}). Press Ctrl+C to stop early.")
    out(f"  {format_command(cmd)}")
    out("")
    try:
        outcome = run_crawl(cmd, container_name_for(run_id), log_path, out)
    except LauncherError as exc:
        out(f"ERROR: {exc}")
        return EXIT_ERROR

    result = collect_results(run_dir, run_id, config.formats)
    print_report(config, run_dir, log_path, outcome, result, out)
    if outcome.interrupted:
        return EXIT_CANCELLED
    _, ok = crawl_status(outcome, result)
    if not ok:
        out(f"\nSee the full log for details: {log_path}")
    return EXIT_OK if ok else EXIT_ERROR


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
