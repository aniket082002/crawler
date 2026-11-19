"""Tests for web_app.py (browser front end). Docker is fully mocked: no
container is started and nothing is crawled. A real HTTP server runs on an
ephemeral 127.0.0.1 port inside the test."""
import hashlib
import http.client
import json
import threading
import time
from unittest import mock

import pytest

import run_crawler as rc
import web_app as wa

HDR = {"Content-Type": "application/json", wa.CSRF_HEADER: wa.CSRF_VALUE}


@pytest.fixture
def server(tmp_path):
    srv, manager = wa.make_server(tmp_path, port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    port = srv.server_address[1]
    yield port, manager, tmp_path
    srv.shutdown()
    srv.server_close()


def request(port, method, path, body=None, headers=None, host=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = {"Host": host or f"127.0.0.1:{port}"}
    h.update(headers or {})
    data = json.dumps(body).encode() if isinstance(body, dict) else body
    conn.request(method, path, body=data, headers=h)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    try:
        payload = json.loads(raw)
    except ValueError:
        payload = raw
    return resp.status, payload, resp


def docker_ok():
    return (mock.patch.object(rc, "check_docker_available", return_value="29.9.2"),
            mock.patch.object(rc, "check_image_exists", return_value="sha256:x"))


def fake_run_crawl(release=None):
    """Stands in for rc.run_crawl: writes the files a real crawl would."""
    def _run(cmd, container_name, log_path, out):
        opts = dict(cmd[i + 1].split("=", 1) for i, a in enumerate(cmd) if a == "-s")
        run_dir = log_path.parent
        out("  Crawler started.")
        out("  [  1] 200 https://acme-widgets.co/")
        if release is not None:
            release.wait(10)
        out("  [  2] 200 https://acme-widgets.co/about")
        log_path.write_text(f"# command: {rc.format_command(cmd)}\n", encoding="utf-8")
        files = {}
        for fmt in opts["EXPORT_FORMATS"].split(","):
            name = rc.FORMAT_FILES[fmt]
            (run_dir / name).write_text("data", encoding="utf-8")
            files[fmt] = f"/app/{opts['OUTPUT_DIR']}/{name}"
        prof = {}
        for key, name in (("website_profile_md", "website_profile.md"),
                          ("website_profile_json", "website_profile.json")):
            (run_dir / name).write_text("# Website profile: Acme\n\n## 1. Company overview\nNot found\n",
                                        encoding="utf-8")
            prof[key] = f"/app/{opts['OUTPUT_DIR']}/{name}"
        (run_dir / "crawl_summary.json").write_text(json.dumps({
            "max_unique_urls": 15, "discovered": 2, "crawled": 2, "failed": 0, "skipped": 0,
            "emails_found": ["sales@acme-widgets.co"], "close_reason": "finished",
            "output_files": files, "profile_files": prof, "items_extracted": 2,
            "services_identified": 3}), encoding="utf-8")
        return rc.RunOutcome(0, False, 1.0, 2)
    return _run


def wait_done(manager, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if manager.job and manager.job.state != "running":
            return manager.job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


# --- pages & security headers ------------------------------------------------
def test_index_served_with_csp(server):
    port, _, _ = server
    status, body, resp = request(port, "GET", "/")
    assert status == 200 and b"<title>Web Crawler</title>" in body
    csp = resp.getheader("Content-Security-Policy")
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert resp.getheader("X-Content-Type-Options") == "nosniff"
    for path in ("/app.js", "/app.css"):
        assert request(port, "GET", path)[0] == 200


def test_static_files_have_no_inline_script():
    html = (wa.STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert "<script>" not in html and "onclick" not in html
    js = (wa.STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "innerHTML" not in js and "insertAdjacentHTML" not in js and "eval(" not in js


def test_server_binds_to_localhost_only(server):
    assert wa.HOST == "127.0.0.1"


def test_foreign_host_header_rejected(server):
    port, _, _ = server
    assert request(port, "GET", "/api/runs", host="evil.example:80")[0] == 403
    assert request(port, "GET", "/", host=f"attacker.com:{port}")[0] == 403
    assert request(port, "GET", "/api/runs", host=f"localhost:{port}")[0] == 200


def test_post_requires_custom_header(server):
    port, manager, _ = server
    status, body, _ = request(port, "POST", "/api/crawl", {"url": "https://acme-widgets.co"},
                              {"Content-Type": "application/json"})
    assert status == 403 and manager.job is None


def test_post_from_other_origin_rejected(server):
    port, manager, _ = server
    status, _, _ = request(port, "POST", "/api/crawl", {"url": "https://acme-widgets.co"},
                           {**HDR, "Origin": "https://evil.example"})
    assert status == 403 and manager.job is None


def test_unknown_routes_404(server):
    port, _, _ = server
    assert request(port, "GET", "/etc/passwd")[0] == 404
    assert request(port, "POST", "/api/nope", {}, HDR)[0] == 404


# --- input validation (same rules as the terminal launcher) ------------------
@pytest.mark.parametrize("body, message", [
    ({"url": "", "max_urls": 15, "format": "all"}, "URL cannot be empty"),
    ({"url": "ftp://x.com", "max_urls": 15, "format": "all"}, "http:// and https://"),
    ({"url": "https://localhost/", "max_urls": 15, "format": "all"}, "not a valid public website"),
    ({"url": "https://acme-widgets.co", "max_urls": 5000, "format": "all"}, "between 1 and 1000"),
    ({"url": "https://acme-widgets.co", "max_urls": "abc", "format": "all"}, "whole number"),
    ({"url": "https://acme-widgets.co", "max_urls": 15, "format": "pdf"}, "1, 2, 3 or 4"),
])
def test_invalid_input_rejected_before_docker(server, body, message):
    port, manager, tmp = server
    with mock.patch.object(rc, "check_docker_available") as chk:
        status, payload, _ = request(port, "POST", "/api/crawl", body, HDR)
    assert status == 400 and message in payload["error"]
    chk.assert_not_called()
    assert manager.job is None and list(tmp.iterdir()) == []


def test_invalid_json_rejected(server):
    port, _, _ = server
    assert request(port, "POST", "/api/crawl", b"{not json", HDR)[0] == 400


def test_docker_not_running(server):
    port, manager, tmp = server
    with mock.patch.object(rc, "check_docker_available",
                           side_effect=rc.DockerUnavailableError("Docker Desktop is not running")):
        status, payload, _ = request(port, "POST", "/api/crawl",
                                     {"url": "acme-widgets.co", "max_urls": 15, "format": "csv"}, HDR)
    assert status == 503 and "Docker Desktop is not running" in payload["error"]
    assert manager.job is None and list(tmp.iterdir()) == []


# --- full lifecycle ---------------------------------------------------------------
@pytest.mark.parametrize("fmt, expected", [("csv", ["csv"]), ("json", ["json"]),
                                           ("xlsx", ["xlsx"]), ("all", ["csv", "json", "xlsx"])])
def test_crawl_lifecycle(server, fmt, expected):
    port, manager, tmp = server
    a, b = docker_ok()
    with a, b, mock.patch.object(rc, "run_crawl", side_effect=fake_run_crawl()) as run:
        status, payload, _ = request(port, "POST", "/api/crawl",
                                     {"url": "acme-widgets.co", "max_urls": "20", "format": fmt}, HDR)
        assert status == 202
        job = wait_done(manager)
    cmd = run.call_args[0][0]
    # exactly the launcher's command, with all security flags
    assert cmd == rc.build_docker_command(job.config, job.run_id, tmp)
    assert cmd[cmd.index("--cap-drop") + 1] == "ALL"
    assert f"EXPORT_FORMATS={','.join(expected)}" in cmd and "-a" in cmd and "max_urls=20" in cmd
    assert job.state == "finished" and job.ok is True and job.status_text == "SUCCESS"

    st = request(port, "GET", "/api/status?since=0")[1]
    assert st["state"] == "finished" and st["pages"] == 2 and st["url"] == "https://acme-widgets.co/"
    assert any("[  2]" in line for line in st["log"])
    assert request(port, "GET", f"/api/status?since={st['log_total']}")[1]["log"] == []

    runs = request(port, "GET", "/api/runs")[1]["runs"]
    assert runs[0]["run_id"] == job.run_dir.name and runs[0]["crawled"] == 2
    assert runs[0]["url"] == "https://acme-widgets.co/" and runs[0]["has_profile"] is True

    detail = request(port, "GET", f"/api/runs/{job.run_dir.name}")[1]
    assert detail["summary"]["services_identified"] == 3
    assert detail["profile_md"].startswith("# Website profile")
    assert set(detail["files"]) >= {rc.FORMAT_FILES[f] for f in expected} | {"website_profile.md"}

    status, body, resp = request(port, "GET", f"/api/runs/{job.run_dir.name}/files/{rc.FORMAT_FILES[expected[0]]}")
    assert status == 200 and body in (b"data", "data")
    assert resp.getheader("Content-Disposition").startswith("attachment;")


def test_only_one_crawl_at_a_time_and_stop(server):
    port, manager, _ = server
    release = threading.Event()
    a, b = docker_ok()
    with a, b, mock.patch.object(rc, "run_crawl", side_effect=fake_run_crawl(release)), \
            mock.patch.object(rc, "stop_container") as stop:
        body = {"url": "acme-widgets.co", "max_urls": 15, "format": "csv"}
        assert request(port, "POST", "/api/crawl", body, HDR)[0] == 202
        status, payload, _ = request(port, "POST", "/api/crawl", body, HDR)
        assert status == 409 and "already running" in payload["error"]
        assert request(port, "POST", "/api/stop", {}, HDR)[1] == {"stopping": True}
        release.set()
        job = wait_done(manager)
        time.sleep(0.1)
    stop.assert_called_once_with(rc.container_name_for(job.run_id))
    assert job.status_text.startswith("INTERRUPTED")
    assert request(port, "POST", "/api/stop", {}, HDR)[1] == {"stopping": False}


def test_crawl_failure_is_reported_not_crashing(server):
    port, manager, _ = server
    a, b = docker_ok()
    with a, b, mock.patch.object(rc, "run_crawl", side_effect=rc.LauncherError("boom")):
        request(port, "POST", "/api/crawl", {"url": "acme-widgets.co", "max_urls": 15, "format": "csv"}, HDR)
        job = wait_done(manager)
    assert job.state == "failed" and job.error == "boom"
    assert request(port, "GET", "/api/status")[1]["status"] == "FAILED"


# --- downloads are restricted ----------------------------------------------------------
@pytest.mark.parametrize("path", [
    "/api/runs/run_20260101-000000/files/secret.txt",
    "/api/runs/run_20260101-000000/files/..%2F..%2Fsettings.py",
    "/api/runs/..%2F..%2Fwebcrawler/files/settings.py",
    "/api/runs/run_x/files/items.csv",
    "/api/runs/..%2Frun_20260101-000000/files/items.csv",
    "/api/runs/run_20260101-000000/files/items.csv/extra",
    "/api/runs/run_20991231-000000/files/items.csv",
])
def test_download_rejects_anything_outside_known_report_files(server, path):
    port, _, tmp = server
    run = tmp / "run_20260101-000000"
    run.mkdir()
    (run / "secret.txt").write_text("nope", encoding="utf-8")
    (run / "items.csv").write_text("ok", encoding="utf-8")
    assert request(port, "GET", path)[0] == 404


def test_previous_runs_untouched(server):
    port, manager, tmp = server
    old = tmp / "run_20200101-000000"
    old.mkdir()
    for n in ("items.csv", "website_profile.md", "crawl_summary.json"):
        (old / n).write_text(f"old {n}", encoding="utf-8")
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in old.iterdir()}
    a, b = docker_ok()
    with a, b, mock.patch.object(rc, "run_crawl", side_effect=fake_run_crawl()):
        request(port, "POST", "/api/crawl", {"url": "acme-widgets.co", "max_urls": 15, "format": "all"}, HDR)
        job = wait_done(manager)
    assert job.run_dir != old
    assert before == {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in old.iterdir()}
    ids = [r["run_id"] for r in request(port, "GET", "/api/runs")[1]["runs"]]
    assert ids == [job.run_dir.name, old.name]   # newest first, both listed
