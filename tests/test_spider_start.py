"""Regression tests for the Scrapy >= 2.13 start() API.

Scrapy 2.13+ calls the async ``Spider.start()`` and never calls
``start_requests()``. Before the fix, GeneralSpider only defined
start_requests(), so Scrapy's default start() requested the start URL
directly: it bypassed the URL registry (not counted toward the cap, not
deduplicated -- the homepage could be crawled twice) and had no errback
(start-URL failures were not counted). These tests pin the fixed behaviour
and verify link discovery / cap handling in parse() are unchanged.

Imports scrapy, so (like test_middlewares.py) this runs inside the Linux
Docker image.
"""
import asyncio
import inspect

import pytest

scrapy = pytest.importorskip("scrapy")
from scrapy.http import HtmlResponse, Request  # noqa: E402

import webcrawler.spiders.general_spider as gs  # noqa: E402
from webcrawler.crawl_limiter import UrlRegistry  # noqa: E402

START = "https://example.com/"


@pytest.fixture(autouse=True)
def fresh_registry(monkeypatch):
    """The spider uses a module-level shared registry; isolate each test."""
    reg = UrlRegistry()
    monkeypatch.setattr(gs, "registry", reg)
    return reg


async def _collect(agen):
    return [x async for x in agen]


def start_output(spider):
    return asyncio.run(_collect(spider.start()))


def html_response(url, body, status=200, depth=0):
    request = Request(url, meta={"depth": depth})
    return HtmlResponse(url=url, body=body.encode("utf-8"), encoding="utf-8",
                        status=status, request=request)


# --- start() ---------------------------------------------------------------
def test_start_is_overridden_async_generator():
    assert inspect.isasyncgenfunction(gs.GeneralSpider.start)
    assert gs.GeneralSpider.start is not scrapy.Spider.start


def test_installed_scrapy_uses_start_api():
    """Documents why the fix is needed: this Scrapy's base start() reads
    start_urls directly and never calls start_requests()."""
    src = inspect.getsource(scrapy.Spider.start)
    assert "start_requests" not in src.split('"""')[-1]


def test_start_registers_start_url_and_attaches_errback(fresh_registry):
    spider = gs.GeneralSpider(start_url=START)
    out = start_output(spider)
    assert len(out) == 1
    req = out[0]
    assert isinstance(req, Request)
    assert req.url == START
    assert req.callback == spider.parse
    assert req.errback == spider.on_error
    assert req.meta["depth"] == 0
    assert not req.dont_filter  # default start() used dont_filter=True
    assert fresh_registry.discovered == 1


def test_start_url_counts_toward_cap(fresh_registry):
    spider = gs.GeneralSpider(start_url=START, max_urls="1")
    start_output(spider)
    assert fresh_registry.max_urls == 1
    assert fresh_registry.is_full()


def test_start_url_not_crawled_twice(fresh_registry):
    """A link back to the homepage must be treated as a duplicate."""
    spider = gs.GeneralSpider(start_url=START)
    start_output(spider)
    assert fresh_registry.try_register(START) is False
    assert fresh_registry.try_register("https://EXAMPLE.com/#top") is False


def test_start_delegates_to_start_requests(fresh_registry):
    """Same results whether Scrapy calls start() (>= 2.13) or
    start_requests() (< 2.13)."""
    spider_new = gs.GeneralSpider(start_url=START)
    new = start_output(spider_new)
    gs.registry.__init__()  # reset counts
    spider_old = gs.GeneralSpider(start_url=START)
    old = list(spider_old.start_requests())
    assert [(r.url, r.meta, r.dont_filter) for r in new] == \
           [(r.url, r.meta, r.dont_filter) for r in old]


def test_start_url_failure_is_counted(fresh_registry):
    spider = gs.GeneralSpider(start_url=START)
    req = start_output(spider)[0]

    class FakeFailure:
        request = req

    req.errback(FakeFailure())
    assert fresh_registry.failed == 1


def test_invalid_start_url_still_rejected():
    with pytest.raises(ValueError):
        gs.GeneralSpider(start_url="file:///etc/passwd")
    with pytest.raises(ValueError):
        gs.GeneralSpider()


# --- parse(): link discovery unchanged ---------------------------------------
PAGE = """<html><head><title> Home </title></head><body>
<a href="/a">A</a>
<a href="/b">B</a>
<a href="/a#frag">A again</a>
<a href="/">Home</a>
<a href="https://other.example/x">External</a>
<a href="javascript:void(0)">JS</a>
<a href="mailto:info@acme-widgets.co">info@acme-widgets.co</a>
</body></html>"""


def test_parse_link_discovery_and_dedup(fresh_registry):
    spider = gs.GeneralSpider(start_url=START)
    start_output(spider)
    results = list(spider.parse(html_response(START, PAGE)))
    items = [r for r in results if not isinstance(r, Request)]
    requests = [r for r in results if isinstance(r, Request)]

    assert len(items) == 1
    assert items[0]["url"] == START and items[0]["title"] == "Home"
    assert items[0]["emails"] == {"info@acme-widgets.co"}
    # richer extraction is attached to the same item
    assert items[0]["h1"] == "" and items[0]["internal_link_count"] == 3
    assert {lk["text"] for lk in items[0]["links"]} >= {"A", "B", "Home", "External"}
    # internal, unique, not the homepage again, no external/js links
    assert [r.url for r in requests] == ["https://example.com/a", "https://example.com/b"]
    assert all(r.meta["depth"] == 1 and r.errback == spider.on_error for r in requests)
    assert fresh_registry.crawled == 1
    assert fresh_registry.discovered == 3
    assert fresh_registry.skipped == 2  # "/a#frag" and "/" duplicates


def test_parse_handles_non_html_response(fresh_registry):
    from scrapy.http import Response
    spider = gs.GeneralSpider(start_url=START)
    start_output(spider)
    resp = Response(START + "file.pdf", status=200, body=b"%PDF-1.4",
                    headers={"Content-Type": "application/pdf"},
                    request=Request(START + "file.pdf", meta={"depth": 1}))
    results = list(spider.parse(resp))
    assert len(results) == 1 and results[0]["url"] == START + "file.pdf"
    assert results[0]["content_warnings"] == ["non_html_response:application/pdf"]
    assert fresh_registry.crawled == 1


def test_parse_respects_cap_including_start_url(fresh_registry):
    spider = gs.GeneralSpider(start_url=START, max_urls="2")
    start_output(spider)
    results = list(spider.parse(html_response(START, PAGE)))
    requests = [r for r in results if isinstance(r, Request)]
    assert [r.url for r in requests] == ["https://example.com/a"]
    assert fresh_registry.discovered == 2 and fresh_registry.is_full()
