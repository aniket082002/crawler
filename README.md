# webcrawler

A Scrapy-based crawler scaffolded to the user's approved spec.

## Requirements implemented
1. Hard cap of 1,000 unique URLs for the whole crawl (`webcrawler/crawl_limiter.py`), counted at discovery time so it covers crawled, failed, and retried URLs alike.
2. `ROBOTSTXT_OBEY = True`, per-domain `DOWNLOAD_DELAY` + AutoThrottle, descriptive `USER_AGENT` with a contact placeholder (`webcrawler/settings.py`).
3. No User-Agent rotation, no CAPTCHA solving, no authentication bypass, no access-control/paywall evasion anywhere in this codebase (by design — see `middlewares.py` and `settings.py` comments).
4. Incremental CSV / JSON / XLSX export that writes and flushes each item as it's scraped; the full item set is never held in memory (`webcrawler/pipelines.py`, XLSX uses `xlsxwriter` constant-memory streaming mode).
5. `scrapy-playwright` is an optional, commented-out dependency; `USE_PLAYWRIGHT = False` by default, plain HTTP only (`webcrawler/settings.py`).
6. End-of-crawl summary report (`outputs/crawl_summary.json` and `.txt`) with discovered/crawled/failed/skipped counts, extracted emails, and exact output file paths (`webcrawler/extensions.py`).
7. Input URL validation (scheme + domain allowlist, rejects `file://`, `javascript:`, etc.) and output path validation (no path traversal outside `outputs/`) (`webcrawler/url_validator.py`).
8. Tests for the URL cap, duplicate detection, internal/external link filtering, and CSV/XLSX/JSON export (`tests/`).

## Install (not yet run — pending approval)
```
pip install -r requirements.txt
```

## Run tests (not yet run — pending approval)
```
pytest
```

## Start a crawl
Run from inside this `webcrawler/` folder (where `scrapy.cfg` lives):
```
scrapy crawl general -a start_url=https://example.com
```
Optional: restrict to specific domains explicitly (comma-separated) instead of inferring from `start_url`:
```
scrapy crawl general -a start_url=https://example.com -a allowed_domains=example.com,blog.example.com
```

Outputs are written to `outputs/`: `items.csv`, `items.json`, `items.xlsx`, `crawl_summary.json`, `crawl_summary.txt`.

Optional: write only some formats with the `EXPORT_FORMATS` setting (default: all three):
```
scrapy crawl general -a start_url=https://example.com -s EXPORT_FORMATS=csv
```

## Interactive launcher (Docker)
From this folder, with Docker Desktop running and the image built (`docker build -t webcrawler:latest .`):
```
python run_crawler.py            # prompts for URL, max URLs (1-1000, default 15) and format
python run_crawler.py --dry-run  # same prompts and checks, prints the docker command only
```
The launcher validates input, checks that Docker and the `webcrawler:latest` image are available,
then runs the existing `general` spider in a container (non-root, 512 MB RAM with no extra swap,
1 CPU, `--cap-drop ALL`, `no-new-privileges`). All politeness settings come from `settings.py`.

Each run writes to its own new folder, `outputs/run_<YYYYMMDD-HHMMSS>/`: the selected `items.*`
files, `crawl_summary.json`, `crawl_summary.txt` and the full container log `docker_run.log`.
Earlier runs are never overwritten. Ctrl+C stops the container gracefully, and the partial results
and summary are still written.

## What is extracted per page
`webcrawler/extractors.py` reads each crawled page's HTML (no JavaScript is executed):
- **Page info:** URL, requested URL (before redirects), status, title, meta description and keywords, canonical URL, language, crawl timestamp (UTC), depth.
- **Content:** visible main text (scripts, styles, hidden elements and navigation removed), headings H1–H6, paragraphs, sections grouped by heading, lists, tables. Text over `MAX_TEXT_CHARS` (default 200,000) is cut and flagged `text_truncated`.
- **Images:** URL, alt text, title, caption.
- **Links:** every link's URL and anchor text, internal or external, nofollow.
- **Contacts:** emails (visible text and `mailto:` only; placeholders and example domains are ignored), phone numbers (`tel:` links, or numbers with a phone label or international format), addresses (schema.org `PostalAddress` and `<address>`), social profile links.
- **Structured data:** JSON-LD, Open Graph, Twitter cards, microdata types, schema.org Organization, Service and Product entries.
- **Flags:** `js_rendering_suspected` (page probably needs JavaScript, so its content may be incomplete) and `content_warnings`.

`items.csv` and `items.xlsx` hold one flat row per page; the original five columns come first. Each `items.json` record has the same columns plus `details` with the full nested data. Excel cells over 32,767 characters are cut in the `.xlsx` only and flagged in `content_warnings`.

## Website profile report
Every crawl also writes `website_profile.md` and `website_profile.json`, whatever export format was chosen. They cover the company overview, name, category, what the company does, services, products, audience, industries, contacts, social profiles, important pages and an overall summary.

They are built locally by rules, with no AI service. Every finding names its source URL and its evidence type:
- *structured data* or *page metadata*: published by the site itself;
- *quoted page text*: copied verbatim from a page;
- *heuristic*: identified from headings and content by rules;
- *inferred summary*: generated wording.

Nothing is derived from URL keywords alone. Anything missing is reported as "Not found". Only the crawled pages are analysed, and the report's "Coverage and limitations" section says so.

## Run tests (inside the Linux image, no network)
```
docker run --rm --network none -v "${PWD}:/src:ro" -w /src -e PYTHONDONTWRITEBYTECODE=1 --entrypoint python webcrawler:latest -m pytest -p no:cacheprovider
```
