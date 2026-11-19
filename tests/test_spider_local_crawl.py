"""End-to-end regression test: a real `scrapy crawl general` run against a
tiny HTTP server on 127.0.0.1 inside the test process.

No external website is contacted (run the suite with `docker run
--network none`; loopback still works). The project's real settings are
used unchanged -- robots.txt obeyed, delays, AutoThrottle, retries -- so
this verifies, through Scrapy's own engine, that:

  * Scrapy actually uses GeneralSpider.start() (start URL registered once,
    counted toward the cap, never crawled twice);
  * robots.txt is fetched first and Disallow rules are obeyed;
  * external links are not followed;
  * failures (404) are counted;
  * exports and the summary report are consistent.

Each crawl runs in a subprocess because Twisted's reactor can't restart.
"""
import csv
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytest.importorskip("scrapy")

PROJECT_DIR = Path(__file__).resolve().parent.parent

SITE = {
    "/robots.txt": ("text/plain", "User-agent: *\nDisallow: /private\n"),
    "/": ("text/html", """<html><head><title>Home</title></head><body>
        <a href="/a">A</a> <a href="/b">B</a> <a href="/a#frag">A again</a>
        <a href="/private/secret">Secret</a> <a href="/">Home</a>
        <a href="https://external.invalid/x">External</a>
        contact: team@acme-widgets.co</body></html>"""),
    "/a": ("text/html", """<html><head><title>A</title></head><body>
        <a href="/c">C</a> <a href="/missing">Missing</a> <a href="/">Home</a>
        </body></html>"""),
    "/b": ("text/html", '<html><head><title>B</title></head><body><a href="/a">A</a></body></html>'),
    "/c": ("text/html", "<html><head><title>C</title></head><body>end</body></html>"),
    "/private/secret": ("text/html", "<html><title>SECRET</title></html>"),
}


@pytest.fixture
def local_site():
    yield from _serve(SITE)


def _serve(site):
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            if self.path in site:
                ctype, body = site[self.path]
                data = body.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", f"{ctype}; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send_error(404)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", hits
    finally:
        server.shutdown()
        server.server_close()


def run_crawl(base_url, out_dir, max_urls, formats="csv,json"):
    cmd = [
        sys.executable, "-m", "scrapy", "crawl", "general",
        "-a", f"start_url={base_url}/",
        "-a", "allowed_domains=127.0.0.1",
        "-a", f"max_urls={max_urls}",
        "-s", f"OUTPUT_DIR={out_dir}",
        "-s", f"EXPORT_FORMATS={formats}",
    ]
    proc = subprocess.run(cmd, cwd=PROJECT_DIR, capture_output=True, text=True, timeout=240)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    summary = json.loads((Path(out_dir) / "crawl_summary.json").read_text(encoding="utf-8"))
    with open(Path(out_dir) / "items.csv", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return summary, rows, proc.stderr


def test_full_crawl_obeys_robots_and_crawls_start_url_once(local_site, tmp_path):
    base, hits = local_site
    summary, rows, log = run_crawl(base, tmp_path, max_urls=15)

    # robots.txt fetched before anything else, Disallow obeyed
    assert hits[0] == "/robots.txt"
    assert "/private/secret" not in hits
    assert "'robotstxt/forbidden': 1" in log  # Scrapy's own stat (INFO dump)
    # each page requested exactly once -- homepage not crawled twice
    page_hits = [h for h in hits if h != "/robots.txt"]
    assert sorted(page_hits) == sorted(["/", "/a", "/b", "/c", "/missing"])
    assert len(page_hits) == len(set(page_hits))

    urls = [r["url"] for r in rows]
    assert sorted(urls) == sorted(f"{base}{p}" for p in ["/", "/a", "/b", "/c"])
    assert len(urls) == len(set(urls))
    home = next(r for r in rows if r["url"] == f"{base}/")
    assert home["depth"] == "0" and home["emails"] == "team@acme-widgets.co"

    # start URL is part of the registry now: discovered counts it
    # (/, /a, /b, /private/secret, /c, /missing)
    assert summary["discovered"] == 6
    assert summary["crawled"] == 4
    assert summary["failed"] == 2  # /missing (404) + /private (robots-blocked)
    assert summary["crawled"] <= summary["discovered"] <= summary["max_unique_urls"]
    assert summary["close_reason"] == "finished"
    assert set(summary["output_files"]) == {"csv", "json"}


def test_cap_includes_start_url(local_site, tmp_path):
    """Before the fix the start URL bypassed the registry, so a cap of 2
    crawled 3 pages (/, /a, /b). Now the cap is exact."""
    base, hits = local_site
    summary, rows, _ = run_crawl(base, tmp_path, max_urls=2)
    page_hits = [h for h in hits if h != "/robots.txt"]
    assert sorted(page_hits) == ["/", "/a"]
    assert len(rows) == 2
    assert summary["discovered"] == 2
    assert summary["crawled"] == 2
    assert summary["unique_urls_registered"] == 2


# --- rich extraction + website profile, end to end -------------------------
def _rich_site():
    from tests import sample_pages as S
    return {
        "/robots.txt": ("text/plain", S.ROBOTS),
        "/": ("text/html", S.HOME),
        "/about": ("text/html", S.ABOUT),
        "/services/design": ("text/html", S.SERVICE_DESIGN),
        "/contact": ("text/html", S.CONTACT),
        "/private/secret": ("text/html", S.PRIVATE),
    }


@pytest.fixture
def rich_site():
    yield from _serve(_rich_site())


def test_rich_extraction_and_profile_end_to_end(rich_site, tmp_path):
    base, hits = rich_site
    summary, rows, log = run_crawl(base, tmp_path, max_urls=15, formats="csv,json,xlsx")

    assert hits[0] == "/robots.txt"
    names = {p.name for p in tmp_path.iterdir()}
    assert names == {"items.csv", "items.json", "items.xlsx", "website_profile.md",
                     "website_profile.json", "crawl_summary.json", "crawl_summary.txt"}

    # summary: original keys + new counts + profile files
    assert summary["crawled"] == 4 and summary["failed"] == 1  # /blog/widget-news is 404
    assert summary["items_extracted"] == 4
    assert summary["emails_found"] == ["sales@acme-widgets.co", "support@acme-widgets.co"]
    assert summary["phones_found"] == 3
    assert summary["services_identified"] >= 3
    assert summary["internal_links_discovered"] >= 5
    assert set(summary["profile_files"]) == {"website_profile_json", "website_profile_md"}

    # page records carry real content
    home = next(r for r in rows if r["url"] == f"{base}/")
    assert "reduce downtime with reliable widgets" in home["main_text"]
    assert home["h1"] == "Precision widgets for modern manufacturers"
    assert home["organization_name"] == "Acme Widgets Ltd"
    assert "your@email.com" not in home["emails"]
    data = json.loads((tmp_path / "items.json").read_text(encoding="utf-8"))
    jhome = next(r for r in data if r["url"] == f"{base}/")
    assert jhome["details"]["tables"] == [[["Plan", "Price"], ["Basic", "£99"]]]

    # website profile built from the crawled pages, with sources
    profile = json.loads((tmp_path / "website_profile.json").read_text(encoding="utf-8"))
    assert profile["company_name"]["value"] == "Acme Widgets Ltd"
    services = {s["name"]: s for s in profile["services"]["items"]}
    assert {"Widget Prototyping", "Custom Widget Design", "Widget Maintenance"} <= set(services)
    assert all(u.startswith(base) for s in services.values() for u in s["source_urls"])
    assert profile["coverage"]["pages_crawled"] == 4
    assert profile["coverage"]["url_limit"] == 15
    md = (tmp_path / "website_profile.md").read_text(encoding="utf-8")
    assert "## 5. Main services" in md and "Acme Widgets Ltd" in md
