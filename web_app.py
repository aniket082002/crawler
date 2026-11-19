#!/usr/bin/env python3
"""Browser front end for the WebCrawler (local only).

    python web_app.py            # opens http://127.0.0.1:8000 in your browser
    python web_app.py --port 8080 --no-browser

It is a thin layer over run_crawler.py: the same input validation, Docker
checks, run folders and the exact same `docker run` command (512 MB, 1 CPU,
non-root, --cap-drop ALL, no-new-privileges, robots.txt obeyed). Nothing is
crawled until you press "Start crawl" and confirm.

Security: listens on 127.0.0.1 only; rejects requests whose Host/Origin is
not this local address (blocks other websites and DNS-rebinding from
driving it); state-changing calls need a custom header; downloads are
limited to known report files inside outputs/run_<timestamp>/ folders.
Uses only the Python standard library.
"""
from __future__ import annotations

import argparse
import json
import re
import threading
import time
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

import run_crawler as rc

HOST = "127.0.0.1"
DEFAULT_PORT = 8000
STATIC_DIR = rc.PROJECT_DIR / "web"
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
}
RUN_ID_RE = re.compile(r"^run_\d{8}-\d{6}(-\d+)?$")
DOWNLOADABLE = {
    "items.csv": "text/csv; charset=utf-8",
    "items.json": "application/json",
    "items.xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "website_profile.md": "text/markdown; charset=utf-8",
    "website_profile.json": "application/json",
    "crawl_summary.json": "application/json",
    "crawl_summary.txt": "text/plain; charset=utf-8",
    "docker_run.log": "text/plain; charset=utf-8",
}
FORMAT_KEYS = {"csv": "1", "json": "2", "xlsx": "3", "excel": "3", "all": "4"}
CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "webcrawler-ui"
MAX_BODY = 10_000
MAX_LOG_LINES = 3000
PAGE_LINE_RE = re.compile(r"^\s*\[\s*(\d+)\]")
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


class ApiError(Exception):
    def __init__(self, status: HTTPStatus, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# --------------------------------------------------------------------------
# crawl jobs
# --------------------------------------------------------------------------
class Job:
    def __init__(self, run_id, run_dir, config, cmd):
        self.run_id = run_id
        self.run_dir = run_dir
        self.config = config
        self.cmd = cmd
        self.state = "running"          # running | finished | failed
        self.lines: list[str] = []
        self.pages = 0
        self.started = time.time()
        self.ended = None
        self.status_text = "RUNNING"
        self.ok = None
        self.error = None
        self.stop_requested = False
        self._lock = threading.Lock()

    def log(self, *args):
        text = " ".join(str(a) for a in args)
        with self._lock:
            for line in text.split("\n"):
                if not line.strip():
                    continue
                m = PAGE_LINE_RE.match(line)
                if m:
                    self.pages = max(self.pages, int(m.group(1)))
                self.lines.append(line)
            if len(self.lines) > MAX_LOG_LINES:
                del self.lines[: len(self.lines) - MAX_LOG_LINES]

    def finish(self, status_text, ok):
        with self._lock:
            self.state, self.status_text, self.ok, self.ended = "finished", status_text, ok, time.time()

    def fail(self, message):
        with self._lock:
            self.state, self.error, self.ok, self.ended = "failed", message, False, time.time()
            self.status_text = "FAILED"

    def snapshot(self, since: int = 0) -> dict:
        with self._lock:
            return {
                "run_id": self.run_dir.name, "state": self.state, "status": self.status_text,
                "ok": self.ok, "error": self.error, "url": self.config.url,
                "max_urls": self.config.max_urls, "formats": list(self.config.formats),
                "pages": self.pages, "elapsed": round((self.ended or time.time()) - self.started),
                "stop_requested": self.stop_requested,
                "log_total": len(self.lines), "log": self.lines[since:] if since >= 0 else [],
            }


class CrawlManager:
    """At most one crawl at a time; each in a background thread."""

    def __init__(self, outputs_dir: Path):
        self.outputs_dir = Path(outputs_dir)
        self.job: Job | None = None
        self._lock = threading.Lock()

    def running(self) -> bool:
        return self.job is not None and self.job.state == "running"

    def start(self, url, max_urls, fmt) -> Job:
        with self._lock:
            if self.running():
                raise ApiError(HTTPStatus.CONFLICT, "A crawl is already running. Stop it or wait for it to finish.")
            try:
                clean_url, _ = rc.validate_url(str(url or ""))
                n = rc.validate_max_urls("" if max_urls in (None, "") else str(max_urls))
                key = FORMAT_KEYS.get(str(fmt or "").strip().lower(), str(fmt or ""))
                label, formats = rc.parse_format_choice(key)
            except ValueError as exc:
                raise ApiError(HTTPStatus.BAD_REQUEST, str(exc)) from None
            try:
                rc.check_docker_available()
                rc.check_image_exists(rc.IMAGE)
            except rc.LauncherError as exc:
                raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, str(exc)) from None
            run_id, run_dir = rc.create_run_dir(self.outputs_dir)
            config = rc.CrawlConfig(clean_url, n, formats, label)
            cmd = rc.build_docker_command(config, run_id, self.outputs_dir)
            job = Job(run_id, run_dir, config, cmd)
            job.log(f"Run folder: {run_dir}")
            job.log("Command: " + rc.format_command(cmd))
            self.job = job
            threading.Thread(target=self._run, args=(job,), daemon=True).start()
            return job

    def _run(self, job: Job):
        try:
            outcome = rc.run_crawl(job.cmd, rc.container_name_for(job.run_id),
                                   job.run_dir / "docker_run.log", job.log)
            if job.stop_requested:
                outcome.interrupted = True
            result = rc.collect_results(job.run_dir, job.run_id, job.config.formats)
            status, ok = rc.crawl_status(outcome, result)
            if outcome.returncode not in (0, None):
                job.log(rc.describe_exit_code(outcome.returncode))
            for problem in result.problems:
                job.log(f"! {problem}")
            job.finish(status, ok)
        except Exception as exc:  # report, never crash the server
            job.fail(str(exc))

    def stop(self) -> bool:
        job = self.job
        if not job or job.state != "running":
            return False
        job.stop_requested = True
        job.log("Stop requested - stopping the crawl gracefully...")
        threading.Thread(target=rc.stop_container, args=(rc.container_name_for(job.run_id),),
                         daemon=True).start()
        return True

    def status(self, since: int = 0) -> dict:
        if self.job is None:
            return {"state": "idle"}
        return self.job.snapshot(since)


# --------------------------------------------------------------------------
# reading past runs
# --------------------------------------------------------------------------
def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _start_url_from_log(run_dir: Path) -> str:
    try:
        with open(run_dir / "docker_run.log", encoding="utf-8", errors="replace") as f:
            first = f.readline()
    except OSError:
        return ""
    m = re.search(r"start_url=(\S+)", first)
    return m.group(1).strip('"') if m else ""


def run_dir_for(outputs_dir: Path, run_id: str) -> Path:
    if not RUN_ID_RE.match(run_id or ""):
        raise ApiError(HTTPStatus.NOT_FOUND, "Unknown run.")
    path = (outputs_dir / run_id).resolve()
    if path.parent != outputs_dir.resolve() or not path.is_dir():
        raise ApiError(HTTPStatus.NOT_FOUND, "Unknown run.")
    return path


def list_runs(outputs_dir: Path) -> list[dict]:
    runs = []
    if not outputs_dir.is_dir():
        return runs
    for d in sorted(outputs_dir.iterdir(), key=lambda p: p.name, reverse=True):
        if not d.is_dir() or not RUN_ID_RE.match(d.name):
            continue
        s = _read_json(d / "crawl_summary.json") or {}
        runs.append({
            "run_id": d.name,
            "url": _start_url_from_log(d),
            "close_reason": s.get("close_reason"),
            "crawled": s.get("crawled"),
            "failed": s.get("failed"),
            "has_profile": (d / "website_profile.md").is_file(),
            "files": [n for n in DOWNLOADABLE if (d / n).is_file()],
        })
    return runs


def run_detail(outputs_dir: Path, run_id: str) -> dict:
    d = run_dir_for(outputs_dir, run_id)
    profile_md = None
    if (d / "website_profile.md").is_file():
        profile_md = (d / "website_profile.md").read_text(encoding="utf-8", errors="replace")
    return {
        "run_id": d.name,
        "url": _start_url_from_log(d),
        "summary": _read_json(d / "crawl_summary.json"),
        "files": [n for n in DOWNLOADABLE if (d / n).is_file()],
        "profile_md": profile_md,
        "folder": str(d),
    }


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def make_handler(manager: CrawlManager, port: int):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    allowed_origins = {f"http://{h}" for h in allowed_hosts}

    class Handler(BaseHTTPRequestHandler):
        server_version = "WebCrawlerUI"
        sys_version = ""

        def log_message(self, fmt, *args):  # keep the console quiet
            pass

        # -- helpers --
        def _send(self, status, body: bytes, ctype: str, extra=None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in SECURITY_HEADERS.items():
                self.send_header(k, v)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status, payload):
            self._send(status, json.dumps(payload).encode("utf-8"), "application/json")

        def _error(self, status, message):
            self._json(status, {"error": message})

        def _check_host(self) -> bool:
            if self.headers.get("Host", "") not in allowed_hosts:
                self._error(HTTPStatus.FORBIDDEN, "Forbidden host.")
                return False
            return True

        def _check_post(self) -> bool:
            origin = self.headers.get("Origin")
            if origin is not None and origin not in allowed_origins:
                self._error(HTTPStatus.FORBIDDEN, "Forbidden origin.")
                return False
            if self.headers.get(CSRF_HEADER) != CSRF_VALUE:
                self._error(HTTPStatus.FORBIDDEN, "Missing request header.")
                return False
            return True

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Request too large.")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw or b"{}")
            except ValueError:
                raise ApiError(HTTPStatus.BAD_REQUEST, "Invalid JSON.") from None
            if not isinstance(data, dict):
                raise ApiError(HTTPStatus.BAD_REQUEST, "Invalid request.")
            return data

        # -- routes --
        def do_GET(self):
            if not self._check_host():
                return
            path = urlsplit(self.path).path
            query = urlsplit(self.path).query
            try:
                if path in STATIC_FILES:
                    name, ctype = STATIC_FILES[path]
                    body = (STATIC_DIR / name).read_bytes()
                    extra = {"Content-Security-Policy": CSP} if name.endswith(".html") else None
                    return self._send(HTTPStatus.OK, body, ctype, extra)
                if path == "/api/status":
                    m = re.search(r"(?:^|&)since=(\d+)", query)
                    return self._json(HTTPStatus.OK, manager.status(int(m.group(1)) if m else 0))
                if path == "/api/runs":
                    return self._json(HTTPStatus.OK, {"runs": list_runs(manager.outputs_dir)})
                parts = [unquote(p) for p in path.split("/") if p]
                if len(parts) == 3 and parts[:2] == ["api", "runs"]:
                    return self._json(HTTPStatus.OK, run_detail(manager.outputs_dir, parts[2]))
                if len(parts) == 5 and parts[:2] == ["api", "runs"] and parts[3] == "files":
                    return self._download(parts[2], parts[4])
                self._error(HTTPStatus.NOT_FOUND, "Not found.")
            except ApiError as exc:
                self._error(exc.status, exc.message)

        def _download(self, run_id, name):
            if name not in DOWNLOADABLE:
                raise ApiError(HTTPStatus.NOT_FOUND, "Unknown file.")
            d = run_dir_for(manager.outputs_dir, run_id)
            f = d / name
            if not f.is_file():
                raise ApiError(HTTPStatus.NOT_FOUND, "File not found.")
            self._send(HTTPStatus.OK, f.read_bytes(), DOWNLOADABLE[name],
                       {"Content-Disposition": f'attachment; filename="{run_id}_{name}"'})

        def do_POST(self):
            if not self._check_host() or not self._check_post():
                return
            path = urlsplit(self.path).path
            try:
                if path == "/api/crawl":
                    data = self._body()
                    job = manager.start(data.get("url"), data.get("max_urls"), data.get("format"))
                    return self._json(HTTPStatus.ACCEPTED, job.snapshot())
                if path == "/api/stop":
                    return self._json(HTTPStatus.OK, {"stopping": manager.stop()})
                self._error(HTTPStatus.NOT_FOUND, "Not found.")
            except ApiError as exc:
                self._error(exc.status, exc.message)

    return Handler


def make_server(outputs_dir: Path, port: int = DEFAULT_PORT, tries: int = 10):
    manager = CrawlManager(outputs_dir)
    last_error = None
    for p in ([0] if port == 0 else range(port, port + tries)):
        try:
            server = ThreadingHTTPServer((HOST, p), None)
        except OSError as exc:
            last_error = exc
            continue
        actual = server.server_address[1]
        server.RequestHandlerClass = make_handler(manager, actual)
        server.daemon_threads = True
        return server, manager
    raise OSError(f"No free port from {port} to {port + tries - 1}: {last_error}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Browser front end for the WebCrawler (local only).")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser window")
    args = parser.parse_args(argv)

    server, manager = make_server(rc.OUTPUTS_DIR, args.port)
    url = f"http://{HOST}:{server.server_address[1]}/"
    print(f"WebCrawler UI running at {url}  (local only; press Ctrl+C to stop)")
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        if manager.running():
            manager.stop()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
