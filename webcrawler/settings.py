BOT_NAME = "webcrawler"

SPIDER_MODULES = ["webcrawler.spiders"]
NEWSPIDER_MODULE = "webcrawler.spiders"

# --- Politeness (Requirement 2) ---------------------------------------
ROBOTSTXT_OBEY = True
USER_AGENT = "WebCrawlerBot/1.0 (educational website crawler)"
DOWNLOAD_DELAY = 2.0
RANDOMIZE_DOWNLOAD_DELAY = True
CONCURRENT_REQUESTS_PER_DOMAIN = 2
CONCURRENT_REQUESTS = 4

AUTOTHROTTLE_ENABLED = True
AUTOTHROTTLE_START_DELAY = 2.0
AUTOTHROTTLE_MAX_DELAY = 30.0
AUTOTHROTTLE_TARGET_CONCURRENCY = 1.0

RETRY_ENABLED = True
RETRY_TIMES = 2

# --- Explicitly NOT implemented, by design (Requirement 3) -------------
# No User-Agent rotation middleware, no CAPTCHA-solving integration, no
# authentication-bypass logic, and no access-control/paywall-evasion
# logic exists anywhere in this project.

# --- Output location (used by pipelines.py / extensions.py) -----------
OUTPUT_DIR = "outputs"

ITEM_PIPELINES = {
    "webcrawler.pipelines.MultiFormatExportPipeline": 300,
    # Builds website_profile.md/.json from the crawled pages (always on,
    # independent of EXPORT_FORMATS).
    "webcrawler.profile.WebsiteProfilePipeline": 400,
}

# Per-page cap on extracted visible text (characters). Pages over the cap are
# cut and flagged text_truncated=True in every export.
MAX_TEXT_CHARS = 200000

EXTENSIONS = {
    "webcrawler.extensions.CrawlSummaryExtension": 500,
}

SPIDER_MIDDLEWARES = {
    "webcrawler.middlewares.WebcrawlerSpiderMiddleware": 543,
}
DOWNLOADER_MIDDLEWARES = {
    "webcrawler.middlewares.WebcrawlerDownloaderMiddleware": 543,
}

# --- scrapy-playwright: optional, DISABLED by default (Requirement 5) --
# Plain HTTP download handlers are used unless this is explicitly turned
# on. To enable Playwright-based rendering:
#   1. pip install scrapy-playwright
#   2. playwright install
#   3. Set USE_PLAYWRIGHT = True below and uncomment the handlers.
USE_PLAYWRIGHT = False
TWISTED_REACTOR = "twisted.internet.asyncioreactor.AsyncioSelectorReactor"
if USE_PLAYWRIGHT:
    DOWNLOAD_HANDLERS = {
        "http": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
        "https": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
    }

REQUEST_FINGERPRINTER_IMPLEMENTATION = "2.7"
FEED_EXPORT_ENCODING = "utf-8"

LOG_LEVEL = "INFO"
