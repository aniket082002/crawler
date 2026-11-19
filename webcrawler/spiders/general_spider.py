from urllib.parse import urljoin, urlparse

import scrapy
from scrapy.http import TextResponse

from webcrawler.crawl_limiter import registry
from webcrawler.extractors import DEFAULT_MAX_TEXT_CHARS, empty_record, extract_page
from webcrawler.items import PageItem
from webcrawler.linkfilter import filter_internal_links
from webcrawler.url_validator import is_valid_url


class GeneralSpider(scrapy.Spider):
    name = "general"

    def __init__(self, start_url=None, allowed_domains=None, max_urls=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not start_url:
            raise ValueError(
                "Usage: scrapy crawl general -a start_url=https://example.com"
            )
        if not is_valid_url(start_url):
            raise ValueError(f"Invalid or unsafe start_url: {start_url}")

        self.start_urls = [start_url]
        parsed = urlparse(start_url)
        if allowed_domains:
            self.allowed_domains = [d.strip() for d in allowed_domains.split(",")]
        else:
            self.allowed_domains = [parsed.netloc]
        self.output_paths = {}

        if max_urls is not None:
            # Per-run cap override only (e.g. -a max_urls=15 for a small
            # test crawl). The project-wide default in crawl_limiter.py's
            # MAX_UNIQUE_URLS constant (1000) is left untouched -- this just
            # tightens the shared registry instance's limit for this run.
            registry.max_urls = int(max_urls)

    async def start(self):
        # Scrapy >= 2.13 calls the async start() and never calls
        # start_requests(); its default start() would request start_urls
        # directly, bypassing the URL registry (cap + dedup) and on_error.
        # Delegate so both old and new Scrapy versions share one code path.
        for request in self.start_requests():
            yield request

    def start_requests(self):
        # Used directly by Scrapy < 2.13, and via start() on newer versions.
        for url in self.start_urls:
            if registry.try_register(url):
                yield scrapy.Request(
                    url, callback=self.parse, errback=self.on_error, meta={"depth": 0}
                )
            else:
                registry.mark_skipped()

    def parse(self, response):
        if response.status >= 400:
            registry.mark_failed()
            return

        registry.mark_crawled()
        depth = response.meta.get("depth", 0)
        redirects = response.meta.get("redirect_urls") or []
        requested_url = redirects[0] if redirects else response.url

        if not isinstance(response, TextResponse):
            # Binary response (PDF, image, ...): record it, nothing to parse.
            ctype = response.headers.get("Content-Type", b"").decode("latin-1", "replace")
            yield PageItem(**empty_record(
                response.url, response.status, depth, requested_url,
                warnings=[f"non_html_response:{ctype or 'unknown'}"]))
            return

        settings = getattr(self, "settings", None)
        max_chars = settings.getint("MAX_TEXT_CHARS", DEFAULT_MAX_TEXT_CHARS) \
            if settings is not None else DEFAULT_MAX_TEXT_CHARS
        # Emails now come from visible text + mailto: links (not raw HTML),
        # so form placeholders and script contents are not reported.
        record = extract_page(
            response.url, response.text, status=response.status, depth=depth,
            allowed_domains=self.allowed_domains, max_text_chars=max_chars,
            requested_url=requested_url,
        )
        registry.add_emails(record["emails"])

        yield PageItem(**record)

        if registry.is_full():
            self.logger.info(
                "URL cap reached (%d); not enqueuing further links.",
                registry.max_urls,
            )
            return

        raw_links = response.css("a::attr(href)").getall()
        absolute_links = [urljoin(response.url, link) for link in raw_links]
        internal, external = filter_internal_links(absolute_links, self.allowed_domains)
        self.logger.debug(
            "Found %d internal, %d external links on %s",
            len(internal),
            len(external),
            response.url,
        )

        for link in internal:
            if registry.is_full():
                self.logger.info("URL cap reached; stopping link enqueue.")
                break
            if registry.try_register(link):
                yield scrapy.Request(
                    link,
                    callback=self.parse,
                    errback=self.on_error,
                    meta={"depth": depth + 1},
                )
            else:
                registry.mark_skipped()

    def on_error(self, failure):
        registry.mark_failed()
        self.logger.warning("Request failed: %s", failure.request.url)
