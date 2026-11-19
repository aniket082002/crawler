"""Write the records of one export into a new folder ``<root>/export_<time>/``.

    instagram_export.json        everything: input, summary, file inventory, all records
    export_profile.csv           one row per profile field
    export_posts.csv             posts, reels, stories ...
    export_comments.csv
    export_likes.csv
    export_followers.csv
    export_following.csv
    export_media_files.csv       media files in the archive (listed, never extracted)
    export_files.csv             every archive entry and what was done with it
    instagram_export.xlsx        the same tables as worksheets
    export_summary.json          counts, files written, errors (always)

The csv / json / xlsx files follow the chosen formats; each format is
independent. The CSV and Excel writers of ``social_common`` are reused, so
spreadsheet formulas are neutralised and Excel cells are never formulas or
links. A missing value appears as its status label and ``notes`` says why.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from pathlib import Path

from webcrawler.social_common.exporters import ExportError, parse_formats, write_csv, write_json, write_xlsx
from webcrawler.social_common.identity import PLATFORM
from webcrawler.social_common.safe_io import new_run_dir, output_file
from webcrawler.social_common.schema import SCHEMA_VERSION, FieldStatus, utc_now
from webcrawler.social_export.records import MODULE, ExportResult

RUN_PREFIX = "export"
FILE_JSON = "instagram_export.json"
FILE_XLSX = "instagram_export.xlsx"
FILE_SUMMARY = "export_summary.json"

TAIL = ("source_file", "source_format", "location", "extraction_status", "extracted_at",
        "duplicates_merged", "warnings", "notes")
TABLES = {
    "posts": ("post", ("dedup_key", "post_type", "created_at", "created_at_local", "caption",
                       "media_count", "media_uris", "media_missing") + TAIL),
    "comments": ("comment", ("dedup_key", "comment_kind", "created_at", "created_at_local",
                             "comment_text", "media_owner") + TAIL),
    "likes": ("like", ("dedup_key", "like_kind", "created_at", "created_at_local", "media_owner",
                       "target_url", "reaction") + TAIL),
    "followers": ("follower", ("dedup_key", "username", "profile_url", "username_as_found",
                               "created_at", "created_at_local") + TAIL),
    "following": ("following", ("dedup_key", "username", "profile_url", "username_as_found",
                                "created_at", "created_at_local") + TAIL),
    "media_files": ("media_file", ("path", "size_bytes", "media_kind", "referenced_by")),
}
PROFILE_COLUMNS = ("field", "value", "status", "notes", "source_file", "source_format", "extracted_at")
FILES_COLUMNS = ("path", "size_bytes", "compressed_size_bytes", "category", "format", "action", "records",
                 "message")

LIMITATIONS = (
    "Only the export file was read; Instagram was not contacted. The data is as it was when the "
    "export was created.",
    "Messages, saved items, ads information and other categories are listed in the file inventory "
    "but not parsed.",
    "Times in HTML exports have no time zone; they are given as shown (created_at_local) and not "
    "converted to UTC.",
    "Instagram has changed its export layout over time. Files in an unknown layout are reported as "
    "failed instead of being guessed.",
    "Media files are listed and matched to posts, but never extracted or opened.",
)


def _row(record, columns) -> dict:
    row, notes = {}, []
    for name in columns:
        if name in record.fields:
            fr = record.fields[name]
            row[name] = fr.display()
            if not fr.available and fr.status is not FieldStatus.NOT_SUPPORTED:
                notes.append(f"{name}: {fr.reason}")
        elif name == "dedup_key":
            row[name] = record.dedup_key
        elif name == "notes":
            continue
        elif name in record.extra:
            row[name] = record.extra[name]
    if "notes" in columns:
        row["notes"] = "; ".join(notes)
    return row


def build_tables(result: ExportResult) -> dict:
    tables = {}
    profile_rows = []
    for record in result.records["profile"]:
        for name, fr in record.fields.items():
            profile_rows.append({"field": name, "value": fr.value if fr.available else "",
                                 "status": fr.status.value, "notes": fr.reason,
                                 "source_file": record.extra["source_file"],
                                 "source_format": record.extra["source_format"],
                                 "extracted_at": record.extra["extracted_at"]})
    tables["profile"] = (PROFILE_COLUMNS, profile_rows)
    for name, (record_type, columns) in TABLES.items():
        tables[name] = (columns, [_row(record, columns) for record in result.records[record_type]])
    tables["files"] = (FILES_COLUMNS, [entry.to_dict() for entry in result.files])
    return tables


def _input_block(result: ExportResult) -> dict:
    return {"file_name": result.input_name, "size_bytes": result.input_size_bytes,
            "sha256": result.input_sha256}


def build_document(result: ExportResult, generated_at: str) -> dict:
    return {
        "schema_version": SCHEMA_VERSION, "module": MODULE, "platform": PLATFORM,
        "generated_at": generated_at, "input": _input_block(result), "summary": result.summary(),
        "limitations": list(LIMITATIONS), "files": [entry.to_dict() for entry in result.files],
        "records": {kind: [record.to_dict() for record in records] for kind, records in result.records.items()},
        "problems": list(result.problems),
    }


@dataclass
class OutputReport:
    run_dir: Path
    files: dict = field(default_factory=dict)
    errors: dict = field(default_factory=dict)
    xlsx_truncated_cells: int = 0


def write_outputs(result: ExportResult, output_root, formats=None, *,
                  now: _dt.datetime | None = None, generated_at: str | None = None) -> OutputReport:
    selected = parse_formats(formats)
    generated_at = generated_at or utc_now()
    output = OutputReport(new_run_dir(output_root, RUN_PREFIX, now))
    tables = build_tables(result)

    def attempt(label, name, action):
        try:
            path = output_file(output.run_dir, name)
            action(path)
            output.files[name] = path
        except (ExportError, OSError, ValueError) as exc:
            output.errors[label] = str(exc) if isinstance(exc, ExportError) else f"{name} ({type(exc).__name__})"

    if "json" in selected:
        attempt("json", FILE_JSON, lambda path: write_json(path, build_document(result, generated_at)))
    if "csv" in selected:
        for table, (columns, rows) in tables.items():
            attempt("csv", f"export_{table}.csv", lambda path, c=columns, r=rows: write_csv(path, c, r))
    if "xlsx" in selected:
        def excel(path):
            stats = write_xlsx(path, [(table, columns, rows) for table, (columns, rows) in tables.items()])
            output.xlsx_truncated_cells = stats["truncated_cells"]
        attempt("xlsx", FILE_XLSX, excel)

    summary = {"schema_version": SCHEMA_VERSION, "module": MODULE, "platform": PLATFORM,
               "generated_at": generated_at, "input": _input_block(result), "summary": result.summary(),
               "output_files": sorted(list(output.files) + [FILE_SUMMARY]), "export_errors": dict(output.errors),
               "xlsx_truncated_cells": output.xlsx_truncated_cells, "limitations": list(LIMITATIONS)}
    try:
        write_json(output_file(output.run_dir, FILE_SUMMARY), summary)
        output.files[FILE_SUMMARY] = output.run_dir / FILE_SUMMARY
    except (ExportError, OSError, ValueError) as exc:
        output.errors["summary"] = str(exc) if isinstance(exc, ExportError) else type(exc).__name__
    return output
