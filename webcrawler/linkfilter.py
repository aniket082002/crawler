"""Internal/external link filtering (Requirement 7's domain-allowlist
check applied to outgoing links).

Deliberately has NO dependency on scrapy/parsel/w3lib -- only on
webcrawler.url_validator, which itself uses nothing but stdlib
urllib.parse/pathlib. This lets the filtering logic be imported and
tested without pulling in frostwork, the native extension that
`import scrapy` triggers (and which is blocked by this machine's
Application Control policy).
"""
from webcrawler.url_validator import is_valid_url


def filter_internal_links(links, allowed_domains):
    """Split absolute URLs into (internal, external) based on whether
    their host is in allowed_domains. Also rejects malformed/unsafe
    schemes via is_valid_url (Requirement 7)."""
    internal, external = [], []
    for link in links:
        if is_valid_url(link, allowed_domains):
            internal.append(link)
        else:
            external.append(link)
    return internal, external
