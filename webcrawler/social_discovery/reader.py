"""Read the website crawler's existing output files.

Supported inputs (requirement: existing crawler output only):
  * ``items.json`` - the crawler writes ``[``, then one page record per line,
    then ``]``. That layout is read as a stream, one bounded line at a time,
    so a large file is never loaded whole and a cut-off file (interrupted
    crawl) still yields the records that are complete. JSON in any other
    layout (pretty-printed, minified) is read whole, up to a size limit.
  * ``items.csv``  - read with Python's csv module. The cell-size limit is
    raised only while the file is read. A file re-saved from a spreadsheet
    with ``;`` or tab separators, or with a byte-order mark, is accepted.

From each page record only this is kept: the page URL, the crawl timestamp
and the link texts that could be Instagram links, taken from

    social_profiles   links the crawler recognised as social profiles
    external_links    every external link on the page (CSV column / JSON key)
    links             every link on the page (JSON ``details.links``)

Nothing here follows a link or opens anything except the given files.
Problems never raise: they are recorded on the InputReport, and reading
continues wherever that is safe.
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Sequence

from webcrawler.social_common.identity import looks_like_instagram_link
from webcrawler.social_common.safe_io import (
    DEFAULT_CSV_FIELD_CHARS, DEFAULT_MAX_INPUT_BYTES, DEFAULT_MAX_RECORD_BYTES,
    DEFAULT_WHOLE_FILE_JSON_BYTES, MIB, InputError, check_input_file, csv_field_limit, format_size,
    iter_bounded_lines,
)

ITEMS_JSON = "items.json"
ITEMS_CSV = "items.csv"
RUN_FOLDER_PREFIX = "run_"
JOIN_SEPARATOR = " | "          # how the crawler joins lists in flat columns

FIELD_SOCIAL = "social_profiles"
FIELD_EXTERNAL = "external_links"
FIELD_LINKS = "links"

INPUT_FORMATS = ("auto", "json", "csv")
MAX_PROBLEMS_PER_INPUT = 50
_BOM = b"\xef\xbb\xbf"
_CSV_DELIMITERS = (",", ";", "\t")

STATUS_OK = "ok"
STATUS_PARTIAL = "partial"      # some records were read, some were not
STATUS_FAILED = "failed"        # nothing usable could be read
STATUS_SKIPPED = "skipped"      # nothing to read here (e.g. a run folder without items files)


@dataclass(frozen=True)
class Limits:
    max_input_bytes: int = DEFAULT_MAX_INPUT_BYTES
    max_record_bytes: int = DEFAULT_MAX_RECORD_BYTES
    whole_file_json_bytes: int = DEFAULT_WHOLE_FILE_JSON_BYTES
    csv_field_chars: int = DEFAULT_CSV_FIELD_CHARS


@dataclass(frozen=True)
class PageLinks:
    """What one page record contributes."""
    record_number: int          # 1-based position of the record in its file
    page_url: str | None
    crawl_timestamp: str | None
    links: tuple                # ((field name, link text), ...) - possible Instagram links only


@dataclass(frozen=True)
class InputSpec:
    path: Path
    display: str                # the path as shown in reports
    format: str                 # "json" | "csv"
    fallback: "InputSpec | None" = None   # tried if this file cannot be read at all


@dataclass
class InputReport:
    path: str
    format: str = ""
    status: str = STATUS_OK
    size_bytes: int | None = None
    page_records: int = 0
    records_skipped: int = 0
    has_link_fields: bool = False
    problems: list = field(default_factory=list)
    problems_not_listed: int = 0

    def problem(self, message: str) -> None:
        if len(self.problems) < MAX_PROBLEMS_PER_INPUT:
            self.problems.append(message)
        else:
            self.problems_not_listed += 1

    def partial(self, message: str) -> None:
        self.problem(message)
        if self.status == STATUS_OK:
            self.status = STATUS_PARTIAL

    def fail(self, message: str) -> None:
        self.problem(message)
        self.status = STATUS_FAILED

    @property
    def readable(self) -> bool:
        return self.status in (STATUS_OK, STATUS_PARTIAL)

    def to_dict(self) -> dict:
        return {"path": self.path, "format": self.format, "status": self.status,
                "size_bytes": self.size_bytes, "page_records": self.page_records,
                "records_skipped": self.records_skipped, "problems": list(self.problems),
                "problems_not_listed": self.problems_not_listed}


# --- choosing input files ---------------------------------------------------------
def display_path(path: Any) -> str:
    return Path(path).as_posix()


def _folder_spec(folder: Path, input_format: str) -> InputSpec | None:
    json_file, csv_file = folder / ITEMS_JSON, folder / ITEMS_CSV
    json_spec = InputSpec(json_file, display_path(json_file), "json") if json_file.is_file() else None
    csv_spec = InputSpec(csv_file, display_path(csv_file), "csv") if csv_file.is_file() else None
    if input_format == "json":
        return json_spec
    if input_format == "csv":
        return csv_spec
    if json_spec and csv_spec:       # JSON keeps exact lists; CSV is the stand-by
        return InputSpec(json_spec.path, json_spec.display, "json", fallback=csv_spec)
    return json_spec or csv_spec


def resolve_inputs(paths: Sequence, *, all_runs: bool = False,
                   input_format: str = "auto") -> tuple:
    """Turn the given paths into input files.

    A path may be a crawler output file (.json / .csv) or a folder holding
    ``items.json`` / ``items.csv``. With ``all_runs`` a folder also stands for
    its ``run_*`` sub-folders. Returns (specs, reports): one InputSpec per
    file to read, and an InputReport for every path that could not be used.
    """
    if input_format not in INPUT_FORMATS:
        raise ValueError(f"input_format must be one of {', '.join(INPUT_FORMATS)}")
    specs: list = []
    reports: list = []
    seen: set = set()

    def add(spec: InputSpec) -> None:
        try:
            key = str(spec.path.resolve())
        except OSError:
            key = str(spec.path)
        if key not in seen:
            seen.add(key)
            specs.append(spec)

    def unusable(shown: str, status: str, message: str) -> None:
        reports.append(InputReport(path=shown, status=status, problems=[message]))

    wanted = {"auto": f"{ITEMS_JSON} or {ITEMS_CSV}", "json": ITEMS_JSON, "csv": ITEMS_CSV}[input_format]
    for raw in paths:
        shown = display_path(raw)
        path = Path(raw)
        try:
            exists, is_dir = path.exists(), path.is_dir()
        except OSError:
            exists, is_dir = False, False
        if not exists:
            unusable(shown, STATUS_FAILED, "the path was not found")
            continue
        if not is_dir:
            file_format = {".json": "json", ".csv": "csv"}.get(path.suffix.lower())
            if file_format is None:
                unusable(shown, STATUS_FAILED,
                         "unsupported input type: only the crawler's .json and .csv output files can be read")
            elif input_format != "auto" and file_format != input_format:
                unusable(shown, STATUS_SKIPPED, f"not a .{input_format} file (input format is set to {input_format})")
            else:
                add(InputSpec(path, shown, file_format))
            continue

        folders = [path]
        if all_runs:
            try:
                folders += sorted((d for d in path.iterdir()
                                   if d.is_dir() and d.name.startswith(RUN_FOLDER_PREFIX)),
                                  key=lambda d: d.name)
            except OSError:
                pass
        found = False
        for folder in folders:
            spec = _folder_spec(folder, input_format)
            if spec is not None:
                add(spec)
                found = True
            elif folder is not path:
                unusable(display_path(folder), STATUS_SKIPPED, f"no {wanted} in this run folder")
        if not found:
            unusable(shown, STATUS_FAILED, f"no crawler output ({wanted}) was found in this folder")
    return specs, reports


# --- shared helpers ------------------------------------------------------------------
def _wanted(value: Any) -> bool:
    return isinstance(value, str) and looks_like_instagram_link(value)


def _split_joined(value: str) -> list:
    return [part for part in (p.strip() for p in value.split(JOIN_SEPARATOR)) if _wanted(part)]


# --- JSON -------------------------------------------------------------------------------
def _page_from_json(record: Any, number: int, report: InputReport) -> PageLinks | None:
    if not isinstance(record, dict):
        report.records_skipped += 1
        report.partial(f"record {number}: not a page record (expected an object); skipped")
        return None
    links: list = []
    saw_link_fields = False
    for key in (FIELD_SOCIAL, FIELD_EXTERNAL):
        if key not in record:
            continue
        saw_link_fields = True
        value = record[key]
        if isinstance(value, str):
            links.extend((key, part) for part in _split_joined(value))
        elif isinstance(value, list):
            links.extend((key, v.strip()) for v in value if _wanted(v))
        elif value is not None:
            report.problem(f"record {number}: '{key}' has an unexpected type and was ignored")

    details = record.get("details")
    if isinstance(details, dict):
        contacts = details.get("contacts")
        if isinstance(contacts, dict):
            if FIELD_SOCIAL in contacts:
                saw_link_fields = True
                profiles = contacts[FIELD_SOCIAL]
                if isinstance(profiles, list):
                    links.extend((FIELD_SOCIAL, v.strip()) for v in profiles if _wanted(v))
                elif profiles is not None:
                    report.problem(f"record {number}: 'details.contacts.{FIELD_SOCIAL}' has an "
                                   "unexpected type and was ignored")
        elif contacts is not None:
            report.problem(f"record {number}: 'details.contacts' has an unexpected type and was ignored")
        if FIELD_LINKS in details:
            saw_link_fields = True
            entries = details[FIELD_LINKS]
            if isinstance(entries, list):
                for entry in entries:
                    url = entry.get("url") if isinstance(entry, dict) else entry
                    if _wanted(url):
                        links.append((FIELD_LINKS, url.strip()))
            elif entries is not None:
                report.problem(f"record {number}: 'details.{FIELD_LINKS}' has an unexpected type and was ignored")
    elif details is not None:
        report.problem(f"record {number}: 'details' has an unexpected type and was ignored")

    if saw_link_fields:
        report.has_link_fields = True
    url, stamp = record.get("url"), record.get("crawl_timestamp")
    return PageLinks(number, url if isinstance(url, str) else None,
                     stamp if isinstance(stamp, str) else None, tuple(links))


def _json_whole_file(stream, limits: Limits, report: InputReport) -> Iterator[PageLinks]:
    if (report.size_bytes or 0) > limits.whole_file_json_bytes:
        report.fail("the file is not in the crawler's one-record-per-line layout and is larger than the "
                    f"{format_size(limits.whole_file_json_bytes)} limit for reading such files whole")
        return
    try:
        document = json.loads(stream.read().decode("utf-8-sig", errors="replace"))
    except (ValueError, RecursionError):
        report.fail("the file is not valid JSON")
        return
    if not isinstance(document, list):
        report.fail("this is not crawler output: expected a JSON list of page records")
        return
    for number, record in enumerate(document, start=1):
        page = _page_from_json(record, number, report)
        if page is not None:
            report.page_records += 1
            yield page


def _iter_json(path: Path, limits: Limits, report: InputReport) -> Iterator[PageLinks]:
    with open(path, "rb") as stream:
        first = stream.readline(64)
        if not first:
            report.fail("the file is empty")
            return
        head = first[len(_BOM):] if first.startswith(_BOM) else first
        if head.strip() != b"[":
            stream.seek(0)
            yield from _json_whole_file(stream, limits, report)
            return

        number = 0
        ended = False
        trailing_noted = False
        for line_number, data in iter_bounded_lines(stream, limits.max_record_bytes):
            line = line_number + 1            # the "[" line was line 1
            if data is None:
                report.records_skipped += 1
                report.partial(f"line {line}: record is larger than the "
                               f"{format_size(limits.max_record_bytes)} record limit; skipped")
                continue
            text = data.strip()
            if not text:
                continue
            if ended:
                if not trailing_noted:
                    report.partial(f"line {line}: content after the end of the list was ignored")
                    trailing_noted = True
                continue
            if text == b"]":
                ended = True
                continue
            if text.endswith(b","):
                text = text[:-1].rstrip()
            try:
                record = json.loads(text.decode("utf-8", errors="replace"))
            except (ValueError, RecursionError):
                looks_complete = text.startswith(b"{") and text.endswith(b"}")
                if number == 0 and report.records_skipped == 0 and not looks_complete:
                    # Not one record per line (e.g. pretty-printed): read the file whole.
                    stream.seek(0)
                    yield from _json_whole_file(stream, limits, report)
                    return
                report.records_skipped += 1
                report.partial(f"line {line}: record could not be parsed (malformed or cut off); skipped")
                continue
            number += 1
            page = _page_from_json(record, number, report)
            if page is not None:
                report.page_records += 1
                yield page
        if not ended:
            report.partial("the file ends without the closing ']' (the crawl may have been interrupted); "
                           "the records read so far were used")


# --- CSV --------------------------------------------------------------------------------
def _detect_delimiter(header_line: str) -> str:
    best, best_score = ",", 0
    for candidate in _CSV_DELIMITERS:
        names = [name.strip().strip('"').lower() for name in header_line.split(candidate)]
        if "url" in names and len(names) > best_score:
            best, best_score = candidate, len(names)
    return best


def _iter_csv(path: Path, limits: Limits, report: InputReport) -> Iterator[PageLinks]:
    with csv_field_limit(limits.csv_field_chars):
        with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as stream:
            header_line = stream.readline(MIB)
            if not header_line.strip():
                report.fail("the file is empty")
                return
            delimiter = _detect_delimiter(header_line)
            stream.seek(0)
            reader = csv.reader(stream, delimiter=delimiter)
            try:
                header = next(reader)
            except StopIteration:
                report.fail("the file is empty")
                return
            except csv.Error as exc:
                report.fail(f"the header row could not be read ({exc})")
                return
            index: dict = {}
            for position, name in enumerate(header):
                index.setdefault(name.strip().lower(), position)
            if "url" not in index:
                report.fail("this is not crawler output: the CSV has no 'url' column")
                return
            link_columns = [name for name in (FIELD_SOCIAL, FIELD_EXTERNAL) if name in index]
            report.has_link_fields = bool(link_columns)
            width = len(header)

            def cell(row: list, name: str) -> str | None:
                position = index.get(name)
                if position is None or position >= len(row):
                    return None
                return row[position]

            number = 0
            while True:
                try:
                    row = next(reader)
                except StopIteration:
                    break
                except csv.Error as exc:
                    report.partial(f"line {reader.line_num}: the CSV could not be read past this point "
                                   f"({exc}); the rows read so far were used")
                    break
                if not any(value.strip() for value in row):
                    continue
                number += 1
                if len(row) > width:
                    report.problem(f"row {number}: more columns than the header; its values may be misaligned")
                links: list = []
                for column in link_columns:
                    value = cell(row, column)
                    if value:
                        links.extend((column, part) for part in _split_joined(value))
                url, stamp = cell(row, "url"), cell(row, "crawl_timestamp")
                report.page_records += 1
                yield PageLinks(number, url.strip() if url and url.strip() else None,
                                stamp.strip() if stamp and stamp.strip() else None, tuple(links))


# --- entry point ----------------------------------------------------------------------------
def iter_pages(spec: InputSpec, limits: Limits, report: InputReport) -> Iterator[PageLinks]:
    """Yield the page records of one input file. Never raises for bad input:
    every problem is recorded on ``report``."""
    report.format = spec.format
    try:
        path = check_input_file(spec.path, limits.max_input_bytes)
        report.size_bytes = path.stat().st_size
    except InputError as exc:
        report.fail(str(exc))
        return
    except OSError as exc:
        report.fail(f"the file cannot be read ({type(exc).__name__})")
        return
    try:
        if spec.format == "json":
            yield from _iter_json(path, limits, report)
        else:
            yield from _iter_csv(path, limits, report)
    except OSError as exc:
        report.fail(f"the file could not be read ({type(exc).__name__})")
        return
    if report.status == STATUS_FAILED:
        return
    if report.page_records == 0:
        if report.records_skipped:
            report.fail("no page records could be read from this file")
        else:
            report.problem("the file contains no page records")
    elif not report.has_link_fields:
        report.problem("this crawl output has no social-link fields (it was written by an older version "
                       "of the crawler), so nothing can be discovered from it")
