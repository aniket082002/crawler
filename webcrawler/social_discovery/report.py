"""Turn a discovery result into the shared record structure and export files.

Every run writes into a brand-new folder ``<output root>/discovery_<time>/``:

    instagram_profiles.json           everything: summary, inputs, profiles with
                                      all their sources, rejected links, problems
    instagram_profiles.csv            one row per profile (no duplicates)
    instagram_profile_sources.csv     one row per profile and source page
    instagram_rejected_links.csv      non-profile Instagram links and why
    instagram_profiles.xlsx           the three tables as worksheets
    discovery_summary.json            counts, inputs, files written (always)

Which of the csv / json / xlsx files are written follows the chosen formats.
The formats are independent: if one cannot be written, the others still are,
and the summary names the failure.

In the flat tables a missing value is shown as its status label (for example
``Not available``) and the ``notes`` column gives the reason.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from webcrawler.social_common.exporters import ExportError, parse_formats, write_csv, write_json, write_xlsx
from webcrawler.social_common.identity import PLATFORM
from webcrawler.social_common.safe_io import new_run_dir, output_file
from webcrawler.social_common.schema import (
    SCHEMA_VERSION, FieldRecord, FieldStatus, Record, available, not_available, utc_now,
)
from webcrawler.social_discovery.discover import DiscoveredProfile, DiscoveryResult, Source

MODULE = "social_discovery"
RECORD_TYPE = "discovered_profile"
RUN_PREFIX = "discovery"

FILE_JSON = "instagram_profiles.json"
FILE_PROFILES_CSV = "instagram_profiles.csv"
FILE_SOURCES_CSV = "instagram_profile_sources.csv"
FILE_REJECTED_CSV = "instagram_rejected_links.csv"
FILE_XLSX = "instagram_profiles.xlsx"
FILE_SUMMARY = "discovery_summary.json"

SHEET_PROFILES = "profiles"
SHEET_SOURCES = "sources"
SHEET_REJECTED = "rejected_links"

PROFILE_COLUMNS = ("username", "profile_url", "source_website_count", "source_websites",
                   "source_page_count", "first_source_page_url", "first_discovered_at",
                   "last_discovered_at", "notes")
SOURCE_COLUMNS = ("username", "profile_url", "source_website", "source_page_url", "discovered_at",
                  "found_in", "link_as_found", "input_file", "input_record", "notes")
REJECTED_COLUMNS = ("link", "reason", "reason_text", "first_source_page_url", "page_count",
                    "first_input_file")

LIMITATIONS = (
    "Profiles are identified only from the form of links found in the crawl output. Instagram is "
    "never contacted, so this report does not confirm that an account exists, is public, or still "
    "uses the username.",
    "Only pages the website crawler actually fetched are covered. Links on pages beyond the crawl's "
    "URL limit, blocked by robots.txt, or added to a website after the crawl are not included.",
    "Links to posts, reels, stories, explore pages and other non-profile Instagram URLs are listed "
    "under rejected links and are never treated as profiles.",
)
NO_PAGE_REASON = "no source page URL was recorded for this link"
NO_TIME_REASON = "no source page has a valid crawl timestamp"


# --- shared record structure -----------------------------------------------------------
def profile_record(profile: DiscoveredProfile) -> Record:
    first = profile.first_source()
    origin = first.source_page_url or f"{first.input_file} (record {first.input_record})"
    first_seen, last_seen = profile.first_discovered_at(), profile.last_discovered_at()
    when = first_seen or ""
    websites = profile.websites()
    pages = profile.page_urls()
    fields = {
        "username": available(profile.username, origin, when),
        "profile_url": available(profile.profile_url, origin, when),
        "source_websites": (available(websites, "website of each source page", when) if websites
                            else not_available(NO_PAGE_REASON)),
        "source_page_count": available(len(pages), "number of different source pages", when),
        "first_source_page_url": (available(first.source_page_url, origin, when) if first.source_page_url
                                  else not_available(NO_PAGE_REASON)),
        "first_discovered_at": (available(first_seen, "earliest crawl timestamp of the source pages", first_seen)
                                if first_seen else not_available(NO_TIME_REASON)),
        "last_discovered_at": (available(last_seen, "latest crawl timestamp of the source pages", last_seen)
                               if last_seen else not_available(NO_TIME_REASON)),
    }
    return Record(RECORD_TYPE, MODULE, PLATFORM, profile.ref.dedup_key, fields,
                  extra={"sources": [source.to_dict() for source in profile.sources]})


def build_document(result: DiscoveryResult, generated_at: str) -> dict:
    """The complete JSON document."""
    return {
        "schema_version": SCHEMA_VERSION,
        "module": MODULE,
        "platform": PLATFORM,
        "generated_at": generated_at,
        "summary": result.summary(),
        "limitations": list(LIMITATIONS),
        "inputs": [report.to_dict() for report in result.inputs],
        "profiles": [profile_record(profile).to_dict() for profile in result.profiles],
        "rejected_links": [entry.to_dict() for entry in result.rejected],
        "problems": result.problems(),
    }


# --- flat tables ---------------------------------------------------------------------------
def _cell(name: str, record: FieldRecord, notes: list) -> Any:
    if not record.available:
        notes.append(f"{name}: {record.reason}")
    return record.display()


def _profile_row(profile: DiscoveredProfile) -> dict:
    record = profile_record(profile)
    notes: list = []
    websites = record.fields["source_websites"]
    return {
        "username": profile.username,
        "profile_url": profile.profile_url,
        "source_website_count": len(websites.value) if websites.available else 0,
        "source_websites": _cell("source_websites", websites, notes),
        "source_page_count": record.fields["source_page_count"].value,
        "first_source_page_url": _cell("first_source_page_url", record.fields["first_source_page_url"], notes),
        "first_discovered_at": _cell("first_discovered_at", record.fields["first_discovered_at"], notes),
        "last_discovered_at": _cell("last_discovered_at", record.fields["last_discovered_at"], notes),
        "notes": "; ".join(notes),
    }


def _source_row(profile: DiscoveredProfile, source: Source) -> dict:
    missing = FieldStatus.NOT_AVAILABLE.value
    by_reason: dict = {}
    for name, reason in source.unavailable.items():
        by_reason.setdefault(reason, []).append(name)
    notes = "; ".join(f"{', '.join(sorted(names))}: {reason}" for reason, names in by_reason.items())
    return {
        "username": profile.username,
        "profile_url": profile.profile_url,
        "source_website": source.source_website if source.source_website is not None else missing,
        "source_page_url": source.source_page_url if source.source_page_url is not None else missing,
        "discovered_at": source.discovered_at if source.discovered_at is not None else missing,
        "found_in": list(source.found_in),
        "link_as_found": source.link_as_found,
        "input_file": source.input_file,
        "input_record": source.input_record,
        "notes": notes,
    }


def _rejected_row(entry) -> dict:
    row = entry.to_dict()
    if row["first_source_page_url"] is None:
        row["first_source_page_url"] = FieldStatus.NOT_AVAILABLE.value
    return row


def build_tables(result: DiscoveryResult) -> dict:
    """{table name: (columns, rows)} for the CSV files and Excel sheets."""
    return {
        SHEET_PROFILES: (PROFILE_COLUMNS, [_profile_row(p) for p in result.profiles]),
        SHEET_SOURCES: (SOURCE_COLUMNS, [_source_row(p, s) for p in result.profiles for s in p.sources]),
        SHEET_REJECTED: (REJECTED_COLUMNS, [_rejected_row(entry) for entry in result.rejected]),
    }


# --- writing ---------------------------------------------------------------------------------
@dataclass
class OutputReport:
    run_dir: Path
    files: dict = field(default_factory=dict)       # file name -> Path
    errors: dict = field(default_factory=dict)      # format -> message
    xlsx_truncated_cells: int = 0


def build_summary(result: DiscoveryResult, generated_at: str, output: OutputReport) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "module": MODULE,
        "platform": PLATFORM,
        "generated_at": generated_at,
        "summary": result.summary(),
        "inputs": [report.to_dict() for report in result.inputs],
        "output_files": sorted(output.files),
        "export_errors": dict(output.errors),
        "xlsx_truncated_cells": output.xlsx_truncated_cells,
        "limitations": list(LIMITATIONS),
    }


def write_outputs(result: DiscoveryResult, output_root, formats=None, *,
                  now: _dt.datetime | None = None, generated_at: str | None = None) -> OutputReport:
    """Write the selected formats into a new folder under ``output_root``."""
    selected = parse_formats(formats)
    generated_at = generated_at or utc_now()
    output = OutputReport(new_run_dir(output_root, RUN_PREFIX, now))
    tables = build_tables(result)

    def attempt(label: str, name: str, action) -> None:
        try:
            path = output_file(output.run_dir, name)
            action(path)
            output.files[name] = path
        except (ExportError, OSError, ValueError) as exc:
            message = str(exc) if isinstance(exc, ExportError) else f"{name} could not be written ({type(exc).__name__})"
            output.errors[label] = message

    if "json" in selected:
        attempt("json", FILE_JSON, lambda path: write_json(path, build_document(result, generated_at)))
    if "csv" in selected:
        for name, sheet in ((FILE_PROFILES_CSV, SHEET_PROFILES), (FILE_SOURCES_CSV, SHEET_SOURCES),
                            (FILE_REJECTED_CSV, SHEET_REJECTED)):
            columns, rows = tables[sheet]
            attempt("csv", name, lambda path, c=columns, r=rows: write_csv(path, c, r))
    if "xlsx" in selected:
        def excel(path: Path) -> None:
            stats = write_xlsx(path, [(sheet, *tables[sheet]) for sheet in
                                      (SHEET_PROFILES, SHEET_SOURCES, SHEET_REJECTED)])
            output.xlsx_truncated_cells = stats["truncated_cells"]
        attempt("xlsx", FILE_XLSX, excel)

    # The summary is written last so that it can list what was written.
    output.files[FILE_SUMMARY] = output.run_dir / FILE_SUMMARY
    try:
        write_json(output_file(output.run_dir, FILE_SUMMARY), build_summary(result, generated_at, output))
    except (ExportError, OSError, ValueError) as exc:
        del output.files[FILE_SUMMARY]
        output.errors["summary"] = str(exc) if isinstance(exc, ExportError) else type(exc).__name__
    return output
