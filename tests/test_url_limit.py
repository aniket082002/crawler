from webcrawler.crawl_limiter import UrlRegistry


def test_cap_enforced_at_limit():
    reg = UrlRegistry(max_urls=1000)
    for i in range(1000):
        assert reg.try_register(f"https://example.com/page{i}") is True
    assert reg.is_full()
    assert reg.discovered == 1000
    # The 1001st unique URL must be rejected once the cap is hit.
    assert reg.try_register("https://example.com/page1000") is False
    assert reg.discovered == 1000


def test_cap_configurable_small():
    reg = UrlRegistry(max_urls=5)
    results = [reg.try_register(f"https://example.com/{i}") for i in range(10)]
    assert results.count(True) == 5
    assert results.count(False) == 5
    assert reg.remaining_capacity() == 0


def test_cap_counts_regardless_of_outcome_category():
    """The cap counts a URL the moment it is discovered, independent of
    whether it later succeeds, fails, or is retried."""
    reg = UrlRegistry(max_urls=3)
    reg.try_register("https://example.com/a")
    reg.try_register("https://example.com/b")
    reg.try_register("https://example.com/c")
    reg.mark_crawled()
    reg.mark_failed()
    reg.mark_failed()  # simulate a retry that failed again
    assert reg.discovered == 3
    assert reg.is_full()
    assert reg.try_register("https://example.com/d") is False


def test_max_urls_overridable_after_construction():
    """GeneralSpider applies a per-run cap override (e.g. -a max_urls=15)
    by setting registry.max_urls post-construction, without touching the
    MAX_UNIQUE_URLS=1000 project default. This exercises that same
    override mechanism directly on a registry instance -- it does not
    import the spider module, since that pulls in scrapy."""
    reg = UrlRegistry()  # defaults to MAX_UNIQUE_URLS (1000)
    assert reg.max_urls == 1000

    reg.max_urls = 15  # simulates the spider's max_urls=15 override
    for i in range(20):
        reg.try_register(f"https://example.com/{i}")
    assert reg.discovered == 15
    assert reg.is_full()
