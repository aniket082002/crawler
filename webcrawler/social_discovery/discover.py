"""Find, normalise and de-duplicate Instagram profile links in crawl output.

For every page record the reader yields, each candidate link is classified
(``social_common.identity``):

  profile        -> added to the result under its lowercase username
  rejected       -> post, reel, story, explore and other non-profile Instagram
                    links are counted and listed with the reason, never
                    treated as profiles
  not_instagram  -> ignored

De-duplication:
  * one profile per username, however the link was written
    (http/https, www/m, letter case, trailing slash, query string);
  * one source entry per (profile, source page, input file): the same link
    twice on a page, or seen in several fields of the same record, is one
    entry. Every different page - and the same page in a different crawl
    output - stays as its own entry, so no source page URL is lost.

Missing data is never invented. If a page record has no usable URL or no
valid crawl timestamp, the profile is still reported, the missing value is
None, and ``unavailable`` says why.

No network access, no file writes: this module only reads through the reader.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence
from urllib.parse import urlsplit

from webcrawler.social_common.exporters import printable_text
from webcrawler.social_common.identity import (
    MAX_LINK_LENGTH, InstagramProfileRef, LinkKind, classify_instagram_link,
)
from webcrawler.social_common.schema import normalize_timestamp
from webcrawler.social_discovery.reader import (
    FIELD_EXTERNAL, FIELD_LINKS, FIELD_SOCIAL, STATUS_FAILED, STATUS_OK, InputReport, InputSpec,
    Limits, iter_pages, resolve_inputs,
)
from webcrawler.url_validator import is_valid_url, normalize_url

MAX_PAGE_URL_LENGTH = 8192
MAX_REJECTED_TRACKED = 50_000       # distinct rejected links remembered

FOUND_IN_ORDER = (FIELD_SOCIAL, FIELD_EXTERNAL, FIELD_LINKS)
# Which spelling to keep as "link_as_found": the page's own link text first,
# the crawler's normalised form last.
_LINK_PREFERENCE = {FIELD_LINKS: 0, FIELD_EXTERNAL: 1, FIELD_SOCIAL: 2}

REASON_PAGE_MISSING = "the page URL is missing in the crawl output"
REASON_PAGE_INVALID = "the page URL in the crawl output is not a valid http(s) URL"
REASON_PAGE_CREDENTIALS = "the page URL contains an embedded user name or password and was left out"
REASON_TIME_MISSING = "the crawl output has no crawl_timestamp for this page"
REASON_TIME_INVALID = "the crawl_timestamp in the crawl output is not a valid date-time"
_DEFAULT_PORTS = {"http": 80, "https": 443}


@dataclass
class Source:
    """One place a profile link was found."""
    username: str
    source_website: str | None
    source_page_url: str | None
    discovered_at: str | None       # when the crawler fetched the page (UTC)
    found_in: list                  # crawl-output fields the link appeared in
    link_as_found: str              # the link text before normalisation
    input_file: str
    input_record: int
    unavailable: dict = field(default_factory=dict)     # missing field -> reason
    link_rank: int = field(default=9, repr=False, compare=False)

    def to_dict(self) -> dict:
        return {"source_website": self.source_website, "source_page_url": self.source_page_url,
                "discovered_at": self.discovered_at, "found_in": list(self.found_in),
                "link_as_found": self.link_as_found, "input_file": self.input_file,
                "input_record": self.input_record, "unavailable": dict(self.unavailable)}


@dataclass
class DiscoveredProfile:
    ref: InstagramProfileRef
    sources: list = field(default_factory=list)

    @property
    def username(self) -> str:
        return self.ref.username

    @property
    def profile_url(self) -> str:
        return self.ref.profile_url

    def websites(self) -> list:
        return sorted({s.source_website for s in self.sources if s.source_website})

    def page_urls(self) -> list:
        return list(dict.fromkeys(s.source_page_url for s in self.sources if s.source_page_url))

    def first_discovered_at(self) -> str | None:
        stamps = [s.discovered_at for s in self.sources if s.discovered_at]
        return min(stamps) if stamps else None      # one fixed UTC format: text order = time order

    def last_discovered_at(self) -> str | None:
        stamps = [s.discovered_at for s in self.sources if s.discovered_at]
        return max(stamps) if stamps else None

    def first_source(self) -> Source:
        """The earliest dated source that has a page URL; else the first
        source with a page URL; else simply the first source."""
        with_page = [s for s in self.sources if s.source_page_url]
        dated = [s for s in with_page if s.discovered_at]
        if dated:
            return min(dated, key=lambda s: s.discovered_at)
        return with_page[0] if with_page else self.sources[0]


@dataclass
class RejectedLink:
    link: str
    reason: str
    reason_text: str
    first_source_page_url: str | None
    first_input_file: str
    page_count: int = 1

    def to_dict(self) -> dict:
        return {"link": self.link, "reason": self.reason, "reason_text": self.reason_text,
                "first_source_page_url": self.first_source_page_url,
                "first_input_file": self.first_input_file, "page_count": self.page_count}


@dataclass
class DiscoveryResult:
    profiles: list                      # DiscoveredProfile, sorted by username
    rejected: list                      # RejectedLink, in the order first seen
    rejected_not_tracked: int           # rejected sightings beyond MAX_REJECTED_TRACKED
    inputs: list                        # InputReport, one per input (and per unusable path)
    pages_with_profile_links: int

    def rejected_by_reason(self) -> dict:
        counts: dict = {}
        for entry in self.rejected:
            counts[entry.reason] = counts.get(entry.reason, 0) + 1
        return dict(sorted(counts.items()))

    def problems(self) -> list:
        return [f"{report.path}: {message}" for report in self.inputs for message in report.problems]

    def summary(self) -> dict:
        websites = {site for profile in self.profiles for site in profile.websites()}
        return {
            "inputs_given": len(self.inputs),
            "inputs_read": sum(1 for r in self.inputs if r.readable),
            "inputs_with_problems": sum(1 for r in self.inputs if r.problems or r.status != STATUS_OK),
            "page_records_read": sum(r.page_records for r in self.inputs),
            "pages_with_profile_links": self.pages_with_profile_links,
            "profiles_found": len(self.profiles),
            "source_entries": sum(len(p.sources) for p in self.profiles),
            "source_websites": len(websites),
            "rejected_links": len(self.rejected),
            "rejected_links_by_reason": self.rejected_by_reason(),
            "rejected_links_not_tracked": self.rejected_not_tracked,
            "problems": sum(len(r.problems) + r.problems_not_listed for r in self.inputs),
        }


# --- per-page helpers ---------------------------------------------------------------
def page_identity(raw: str | None) -> tuple:
    """(page URL, website, reason). The website is ``scheme://host`` of the
    page (default ports dropped). On any problem both are None and the reason
    says why; the URL is never repaired or guessed."""
    if raw is None or not raw.strip():
        return None, None, REASON_PAGE_MISSING
    url = raw.strip()
    if (len(url) > MAX_PAGE_URL_LENGTH
            or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 or 0xD800 <= ord(ch) <= 0xDFFF
                   for ch in url)
            or not is_valid_url(url)):
        return None, None, REASON_PAGE_INVALID
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        return None, None, REASON_PAGE_INVALID
    scheme = parts.scheme.lower()
    if not host or scheme not in _DEFAULT_PORTS:
        return None, None, REASON_PAGE_INVALID
    if "@" in parts.netloc:
        return None, None, REASON_PAGE_CREDENTIALS
    shown_host = f"[{host}]" if ":" in host else host
    website = f"{scheme}://{shown_host}"
    if port is not None and port != _DEFAULT_PORTS[scheme]:
        website += f":{port}"
    return url, website, None


def page_timestamp(raw: str | None) -> tuple:
    """(UTC timestamp ``YYYY-MM-DDTHH:MM:SSZ``, reason)."""
    if raw is None or not raw.strip():
        return None, REASON_TIME_MISSING
    stamp = normalize_timestamp(raw)
    if stamp is None:
        return None, REASON_TIME_INVALID
    return stamp, None


def _rejected_key(raw: str) -> str:
    """Same link written slightly differently (trailing slash, query, host
    case) counts once per page."""
    text = raw.strip()
    try:
        parts = urlsplit(text)
    except ValueError:
        return text
    if parts.netloc:
        return f"{parts.netloc.lower()}{parts.path.rstrip('/')}"
    return text.split("?", 1)[0].split("#", 1)[0].rstrip("/")


def _shown_link(raw: str) -> str:
    text = printable_text(raw.strip())
    if len(text) > MAX_LINK_LENGTH:
        text = text[:MAX_LINK_LENGTH] + " ...[cut]"
    return text


def _ordered_fields(names) -> list:
    known = [name for name in FOUND_IN_ORDER if name in names]
    return known + sorted(name for name in names if name not in FOUND_IN_ORDER)


# --- discovery -------------------------------------------------------------------------
class _Collector:
    def __init__(self) -> None:
        self.profiles: dict = {}        # username -> DiscoveredProfile
        self.sources: dict = {}         # (username, page key, input file) -> Source
        self.rejected: dict = {}        # (reason, link key) -> RejectedLink
        self.rejected_not_tracked = 0
        self.pages_with_profile_links = 0

    def add_page(self, page, input_file: str) -> None:
        page_url, website, page_reason = page_identity(page.page_url)
        discovered_at, time_reason = page_timestamp(page.crawl_timestamp)
        page_key = normalize_url(page_url) if page_url else f"record:{page.record_number}"

        found: dict = {}                # username -> [ref, field names, link text, rank]
        rejected_here: set = set()
        for field_name, raw in page.links:
            result = classify_instagram_link(raw)
            if result.kind is LinkKind.NOT_INSTAGRAM:
                continue
            if result.kind is LinkKind.REJECTED:
                key = (result.reason_code, _rejected_key(raw))
                if key not in rejected_here:
                    rejected_here.add(key)
                    self._add_rejected(key, raw, result, page_url, input_file)
                continue
            rank = _LINK_PREFERENCE.get(field_name, 9)
            entry = found.get(result.profile.username)
            if entry is None:
                found[result.profile.username] = [result.profile, {field_name}, raw.strip(), rank]
            else:
                entry[1].add(field_name)
                if rank < entry[3]:
                    entry[2], entry[3] = raw.strip(), rank

        if found:
            self.pages_with_profile_links += 1
        for username, (ref, fields, link_text, rank) in found.items():
            profile = self.profiles.get(username)
            if profile is None:
                profile = self.profiles[username] = DiscoveredProfile(ref)
            key = (username, page_key, input_file)
            existing = self.sources.get(key)
            if existing is not None:        # the same page listed twice in one file
                existing.found_in = _ordered_fields(set(existing.found_in) | fields)
                if rank < existing.link_rank:
                    existing.link_as_found, existing.link_rank = _shown_link(link_text), rank
                continue
            unavailable: dict = {}
            if page_url is None:
                unavailable["source_page_url"] = page_reason
                unavailable["source_website"] = page_reason
            if discovered_at is None:
                unavailable["discovered_at"] = time_reason
            source = Source(username=username, source_website=website, source_page_url=page_url,
                            discovered_at=discovered_at, found_in=_ordered_fields(fields),
                            link_as_found=_shown_link(link_text), input_file=input_file,
                            input_record=page.record_number, unavailable=unavailable, link_rank=rank)
            self.sources[key] = source
            profile.sources.append(source)

    def _add_rejected(self, key, raw, result, page_url, input_file) -> None:
        entry = self.rejected.get(key)
        if entry is not None:
            entry.page_count += 1
        elif len(self.rejected) >= MAX_REJECTED_TRACKED:
            self.rejected_not_tracked += 1
        else:
            self.rejected[key] = RejectedLink(
                link=_shown_link(raw), reason=result.reason_code, reason_text=result.reason_text,
                first_source_page_url=page_url, first_input_file=input_file)


def discover(specs: Sequence[InputSpec], unusable: Sequence[InputReport] = (),
             limits: Limits | None = None) -> DiscoveryResult:
    """Read every input and build the result. One unreadable input never stops
    the others. ``unusable`` are reports for paths that could not be resolved."""
    limits = limits or Limits()
    collector = _Collector()
    reports = list(unusable)

    def read(spec: InputSpec) -> InputReport:
        report = InputReport(path=spec.display)
        for page in iter_pages(spec, limits, report):
            collector.add_page(page, spec.display)
        return report

    for spec in specs:
        report = read(spec)
        reports.append(report)
        if report.status == STATUS_FAILED and spec.fallback is not None:
            stand_in = read(spec.fallback)
            stand_in.problems.insert(0, f"read instead of {spec.display}, which could not be read")
            reports.append(stand_in)

    profiles = sorted(collector.profiles.values(), key=lambda p: p.username)
    return DiscoveryResult(profiles=profiles, rejected=list(collector.rejected.values()),
                           rejected_not_tracked=collector.rejected_not_tracked, inputs=reports,
                           pages_with_profile_links=collector.pages_with_profile_links)


def discover_paths(paths: Sequence, *, all_runs: bool = False, input_format: str = "auto",
                   limits: Limits | None = None) -> DiscoveryResult:
    """Resolve the given files/folders and run discovery on them."""
    specs, unusable = resolve_inputs(paths, all_runs=all_runs, input_format=input_format)
    return discover(specs, unusable, limits)
