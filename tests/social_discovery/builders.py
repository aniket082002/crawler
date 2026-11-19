"""Build small files shaped exactly like the website crawler's output.

``CRAWLER_COLUMNS`` and the JSON layout are copies of what
``webcrawler/pipelines.py`` writes; ``test_crawler_integration.py`` checks
them against the real pipeline so they cannot drift unnoticed.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

from webcrawler.social_discovery.reader import InputReport, InputSpec, Limits, display_path, iter_pages

CRAWLER_COLUMNS = [
    "url", "status", "title", "emails", "depth",
    "requested_url", "crawl_timestamp", "language", "meta_description",
    "meta_keywords", "canonical_url", "h1", "headings", "word_count",
    "main_text", "text_truncated", "phones", "addresses", "social_profiles",
    "image_count", "images", "internal_link_count", "external_link_count",
    "external_links", "og_title", "og_description", "og_type", "og_site_name",
    "jsonld_types", "organization_name", "js_rendering_suspected",
    "content_warnings",
]
LEGACY_COLUMNS = ["url", "status", "title", "emails", "depth"]      # the crawler's first output format
STAMP = "2026-11-13T04:35:25+00:00"                                 # the crawler's timestamp format
STAMP_UTC = "2026-11-13T04:35:25Z"                                  # the same moment as Module C reports it
IG = "https://www.instagram.com"


def page(url, social=(), links=(), stamp=STAMP, **overrides) -> dict:
    """One page record as the crawler writes it to items.json.

    social: what the crawler put in social_profiles (its normalised form)
    links:  every link on the page, as written in the page's HTML
    """
    record = {name: "" for name in CRAWLER_COLUMNS}
    record.update({
        "url": url, "status": 200, "title": "A page", "depth": 0, "requested_url": url,
        "crawl_timestamp": stamp, "word_count": 3, "main_text": "Some page text.",
        "social_profiles": " | ".join(social),
        "external_links": " | ".join(dict.fromkeys(links)),
        "details": {
            "links": [{"url": link, "text": "a link", "internal": False, "nofollow": False} for link in links],
            "contacts": {"emails": [], "phones": [], "addresses": [], "social_profiles": list(social)},
        },
    })
    record.update(overrides)
    return record


def legacy_page(url) -> dict:
    return {"url": url, "status": 200, "title": "Old page", "emails": "", "depth": 0}


def write_json(path, records, layout="crawler", closed=True, bom=False, newline="\n") -> Path:
    """layout: "crawler" (one record per line, as pipelines.py writes it),
    "pretty" (indented) or "minified" (a single line)."""
    path = Path(path)
    if layout == "crawler":
        text = "[" + newline + ("," + newline).join(json.dumps(r, ensure_ascii=False) for r in records)
        text += (newline + "]" + newline) if closed else ""
    elif layout == "pretty":
        text = json.dumps(records, ensure_ascii=False, indent=2)
    elif layout == "minified":
        text = json.dumps(records, ensure_ascii=False, separators=(",", ":"))
    else:
        raise ValueError(layout)
    data = text.encode("utf-8")
    path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + data)
    return path


def write_csv(path, records, columns=CRAWLER_COLUMNS, delimiter=",", bom=False) -> Path:
    path = Path(path)
    with open(path, "w", newline="", encoding="utf-8-sig" if bom else "utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, delimiter=delimiter, extrasaction="ignore")
        writer.writeheader()
        for record in records:
            writer.writerow(record)
    return path


def read(path, fmt=None, limits=None):
    """Run the reader on one file. Returns (pages, report)."""
    path = Path(path)
    spec = InputSpec(path, display_path(path), fmt or path.suffix.lstrip(".").lower())
    report = InputReport(path=spec.display)
    pages = list(iter_pages(spec, limits or Limits(), report))
    return pages, report
