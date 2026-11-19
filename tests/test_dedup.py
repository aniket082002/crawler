from webcrawler.crawl_limiter import UrlRegistry
from webcrawler.url_validator import normalize_url


def test_duplicate_url_rejected():
    reg = UrlRegistry(max_urls=1000)
    assert reg.try_register("https://example.com/a") is True
    assert reg.try_register("https://example.com/a") is False
    assert reg.discovered == 1


def test_duplicate_detected_ignoring_fragment():
    reg = UrlRegistry(max_urls=1000)
    assert reg.try_register("https://example.com/a#section1") is True
    assert reg.try_register("https://example.com/a#section2") is False
    assert reg.discovered == 1


def test_duplicate_detected_case_insensitive_host():
    reg = UrlRegistry(max_urls=1000)
    assert reg.try_register("https://Example.com/a") is True
    assert reg.try_register("https://example.com/a") is False


def test_normalize_url_strips_fragment_and_lowercases_host():
    assert normalize_url("https://Example.com/a#frag") == "https://example.com/a"


def test_different_paths_are_not_duplicates():
    reg = UrlRegistry(max_urls=1000)
    assert reg.try_register("https://example.com/a") is True
    assert reg.try_register("https://example.com/b") is True
    assert reg.discovered == 2
