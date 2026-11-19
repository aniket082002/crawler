"""Shared URL registry enforcing the hard 1,000-unique-URL crawl cap
(Requirement 1) and providing duplicate-URL detection (Requirement 8).

The cap counts every unique URL the moment it is registered for the
frontier -- i.e. at discovery time, before the outcome (success, failure,
retry) is known. So the 1,000 limit applies regardless of whether a URL
later succeeds, fails, or gets retried: it is never counted twice, and
retries of an already-registered URL do not consume extra capacity.
"""
from __future__ import annotations

from webcrawler.url_validator import normalize_url

MAX_UNIQUE_URLS = 1000


class UrlRegistry:
    def __init__(self, max_urls: int = MAX_UNIQUE_URLS):
        self.max_urls = max_urls
        self._seen: set[str] = set()
        self.discovered = 0
        self.crawled = 0
        self.failed = 0
        self.skipped = 0
        self.emails_found: set[str] = set()

    def is_full(self) -> bool:
        return len(self._seen) >= self.max_urls

    def remaining_capacity(self) -> int:
        return max(0, self.max_urls - len(self._seen))

    def try_register(self, url: str) -> bool:
        """Register a newly discovered unique URL against the cap.

        Returns True if the URL was newly registered (counts toward the
        cap). Returns False if it is a duplicate (already registered) or
        the cap has already been reached -- in both cases the caller must
        not enqueue/schedule it again.
        """
        key = normalize_url(url)
        if key in self._seen:
            return False
        if self.is_full():
            return False
        self._seen.add(key)
        self.discovered += 1
        return True

    def mark_crawled(self) -> None:
        self.crawled += 1

    def mark_failed(self) -> None:
        self.failed += 1

    def mark_skipped(self) -> None:
        self.skipped += 1

    def add_emails(self, emails: set[str]) -> None:
        self.emails_found.update(emails)

    def summary(self) -> dict:
        return {
            "max_unique_urls": self.max_urls,
            "discovered": self.discovered,
            "crawled": self.crawled,
            "failed": self.failed,
            "skipped": self.skipped,
            "unique_urls_registered": len(self._seen),
            "emails_found": sorted(self.emails_found),
        }


# Single shared instance for the lifetime of one crawl process.
registry = UrlRegistry()
