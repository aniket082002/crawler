"""Writes the end-of-crawl summary report (Requirement 6): counts of
discovered/crawled/failed/skipped URLs, extracted email addresses, and
the exact file paths of every generated output.

Extraction/report counts (items, phones, services, links, ...) are added
as extra keys; all original keys keep their names and meaning.
"""
import json

from scrapy import signals

from webcrawler.crawl_limiter import registry
from webcrawler.url_validator import safe_output_path

PROFILE_STAT_KEYS = (
    "phones_found", "addresses_found", "services_identified", "products_identified",
    "social_profiles_found", "internal_links_discovered", "external_links_discovered",
    "js_rendering_suspected_pages", "text_truncated_pages",
)


class CrawlSummaryExtension:
    def __init__(self, output_dir: str):
        self.output_dir = output_dir
        self.crawler = None

    @classmethod
    def from_crawler(cls, crawler):
        output_dir = crawler.settings.get("OUTPUT_DIR", "outputs")
        ext = cls(output_dir=output_dir)
        ext.crawler = crawler
        crawler.signals.connect(ext.spider_closed, signal=signals.spider_closed)
        return ext

    def _stat(self, key):
        stats = getattr(self.crawler, "stats", None) if self.crawler is not None else None
        return (stats.get_value(key, 0) or 0) if stats is not None else 0

    def spider_closed(self, spider, reason):
        summary = registry.summary()
        summary["close_reason"] = reason
        summary["output_files"] = getattr(spider, "output_paths", {})

        # --- additional keys ---
        profile_stats = getattr(spider, "profile_stats", {}) or {}
        summary["items_extracted"] = self._stat("item_scraped_count")
        summary["emails_found_count"] = len(summary["emails_found"])
        summary["robots_txt_blocked"] = self._stat("robotstxt/forbidden")
        for key in PROFILE_STAT_KEYS:
            summary[key] = profile_stats.get(key, 0)
        summary["profile_files"] = getattr(spider, "profile_paths", {})

        summary_path = safe_output_path(self.output_dir, "crawl_summary.json")
        with open(summary_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)

        txt_path = safe_output_path(self.output_dir, "crawl_summary.txt")
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write("Crawl summary\n")
            f.write("=============\n")
            f.write(f"Close reason: {reason}\n")
            f.write(f"Unique URL cap: {summary['max_unique_urls']}\n")
            f.write(f"Discovered: {summary['discovered']}\n")
            f.write(f"Crawled: {summary['crawled']}\n")
            f.write(f"Failed: {summary['failed']}\n")
            f.write(f"Skipped: {summary['skipped']}\n")
            f.write(f"Emails found ({len(summary['emails_found'])}):\n")
            for email in summary["emails_found"]:
                f.write(f"  - {email}\n")
            f.write(f"Items extracted: {summary['items_extracted']}\n")
            f.write(f"Blocked by robots.txt: {summary['robots_txt_blocked']}\n")
            for key in PROFILE_STAT_KEYS:
                f.write(f"{key.replace('_', ' ').capitalize()}: {summary[key]}\n")
            f.write("Output files:\n")
            for fmt, path in summary["output_files"].items():
                f.write(f"  - {fmt}: {path}\n")
            for name, path in summary["profile_files"].items():
                f.write(f"  - {name.replace('_', ' ')}: {path}\n")
            f.write(f"  - summary json: {summary_path}\n")
            f.write(f"  - summary txt: {txt_path}\n")
