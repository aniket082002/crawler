"""Regression tests for the Scrapy 2.20.0 async spider-output requirement.

WebcrawlerSpiderMiddleware.process_spider_output() must be an async
generator (or the middleware must additionally define
process_spider_output_async), or Scrapy's SpiderMiddlewareManager raises
TypeError at crawler startup -- this is what broke the first live test
crawl before the first fix.

Scrapy 2.20 also passes `result` itself as an async generator (not a plain
list), so the method's body must use `async for`, not a sync `for` -- a
sync `for` over an async-generator `result` raises
``TypeError: 'async_generator' object is not iterable`` at runtime, which is
what broke the *second* live test crawl attempt (0 items scraped) after the
first fix only changed the method signature, not the loop inside it.

NOTE: importing webcrawler.middlewares imports scrapy, which pulls in the
frostwork native extension. On this Windows machine that import is blocked
by Application Control policy (a known, already-reported environment
limitation -- see test_link_filter.py's history and linkfilter.py's
extraction for the same issue). This test therefore cannot be collected
on Windows; it is expected to pass inside the project's Linux Docker
image, where frostwork's manylinux wheel loads with no DLL error.
"""
import asyncio
import inspect

from webcrawler.middlewares import WebcrawlerSpiderMiddleware


def test_process_spider_output_is_async_compatible():
    mw = WebcrawlerSpiderMiddleware
    assert inspect.isasyncgenfunction(mw.process_spider_output) or hasattr(
        mw, "process_spider_output_async"
    )


async def _collect(agen):
    return [x async for x in agen]


async def _async_gen(items):
    for item in items:
        yield item


def test_process_spider_output_preserves_items():
    """Behavior is unchanged: still yields every item from result, in
    order, without filtering or transforming anything. `result` is an
    async generator here, matching what Scrapy 2.20 actually passes in."""
    mw = WebcrawlerSpiderMiddleware()
    result = _async_gen(["a", "b", "c"])
    output = asyncio.run(_collect(mw.process_spider_output(None, result, None)))
    assert output == ["a", "b", "c"]


def test_process_spider_output_iterates_async_generator_result():
    """Regression for the `async for` fix: `result` must be consumed as an
    async generator (Scrapy 2.20's real call pattern), not treated as a
    plain sync iterable. A sync `for` over this input raises
    TypeError: 'async_generator' object is not iterable -- the exact crash
    seen in the second live crawl attempt."""
    mw = WebcrawlerSpiderMiddleware()
    items = [{"url": "a"}, {"url": "b"}, {"url": "c"}]
    result = _async_gen(items)
    output = asyncio.run(_collect(mw.process_spider_output(None, result, None)))
    assert output == items
