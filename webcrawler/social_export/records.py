"""Turn an export archive into records: field records, media references and
de-duplication.

Every record is a ``social_common.schema.Record``. Its ``fields`` hold one
FieldRecord per expected field: the value when the file had it, "Not
available" with the reason when it did not, "Parsing failed" when it was
there but unreadable, and "Not supported" for a field the file's format
cannot carry. Its ``extra`` holds the provenance: source file, source format,
location in the file, extraction status, extraction time and warnings.

De-duplication: two items with the same content (the same follower, the same
post, ...) become one record; ``duplicates_merged`` and ``also_found_in`` say
where the copies were.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from webcrawler.social_common.identity import CANONICAL_PROFILE_URL, PLATFORM
from webcrawler.social_common.schema import (
    FieldRecord, FieldStatus, Record, available, make_dedup_key, unavailable, utc_now,
)
from webcrawler.social_export.archive import ArchiveLimits, ExportArchive, MemberError
from webcrawler.social_export.parsers import ParsedItem, detect_category, parse_file

MODULE = "social_export"
MAX_RECORDS = 2_000_000
MAX_ALSO_FOUND_IN = 20
MAX_WARNINGS_PER_FILE = 50

MEDIA_KINDS = {
    "image": ("jpg", "jpeg", "png", "gif", "webp", "heic", "heif", "bmp"),
    "video": ("mp4", "mov", "m4v", "webm", "3gp"),
    "audio": ("m4a", "mp3", "aac", "wav", "ogg", "opus"),
}
_MEDIA_EXT = {ext: kind for kind, exts in MEDIA_KINDS.items() for ext in exts}

RECORD_TYPES = ("profile", "post", "comment", "like", "follower", "following", "media_file")
_TIME_FIELDS = ("created_at", "created_at_local")
EXPECTED_FIELDS = {
    "post": ("post_type", "caption", *_TIME_FIELDS, "media_uris", "media_count", "media_missing"),
    "comment": ("comment_kind", "comment_text", "media_owner", *_TIME_FIELDS),
    "like": ("like_kind", "media_owner", "target_url", "reaction", *_TIME_FIELDS),
    "follower": ("username", "profile_url", "username_as_found", *_TIME_FIELDS),
    "following": ("username", "profile_url", "username_as_found", *_TIME_FIELDS),
    "profile": ("username", "name", "bio", "website", "profile_photo_uri"),
}
# Fields whose absence makes a record "partial".
CORE_FIELDS = {
    "post": ("media_uris",), "comment": ("comment_text",), "like": ("target_url",),
    "follower": ("username",), "following": ("username",), "profile": ("username",),
}
NOT_IN_JSON = "JSON exports give times in UTC; see created_at"
NOT_IN_HTML = ("the HTML export shows a local time without a time zone, so it cannot be converted to UTC; "
               "see created_at_local")

ACTION_PARSED = "parsed"
ACTION_PARTIAL = "parsed_with_warnings"
ACTION_FAILED = "failed"
ACTION_REJECTED = "rejected"
ACTION_MEDIA = "media_listed"
ACTION_NOT_PROCESSED = "not_processed"


@dataclass
class FileEntry:
    path: str
    size_bytes: int
    compressed_size_bytes: int
    category: str
    format: str
    action: str
    records: int = 0
    message: str = ""

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class ExportResult:
    input_name: str
    input_size_bytes: int
    input_sha256: str
    extracted_at: str
    files: list = field(default_factory=list)                 # FileEntry
    records: dict = field(default_factory=lambda: {t: [] for t in RECORD_TYPES})
    duplicates_removed: int = 0
    records_not_kept: int = 0
    problems: list = field(default_factory=list)

    def summary(self) -> dict:
        actions: dict = {}
        for entry in self.files:
            actions[entry.action] = actions.get(entry.action, 0) + 1
        return {
            "archive_entries": len(self.files),
            "entries_by_action": dict(sorted(actions.items())),
            "records": {kind: len(items) for kind, items in self.records.items()},
            "duplicates_removed": self.duplicates_removed,
            "records_not_kept": self.records_not_kept,
            "problems": len(self.problems),
        }


def media_kind(name: str) -> str | None:
    if "." not in name:
        return None
    return _MEDIA_EXT.get(name.rsplit(".", 1)[-1].lower())


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class MediaIndex:
    """Find an archive media file from a reference such as
    ``media/posts/202301/abc.jpg``, whatever top-level folder the archive uses."""

    def __init__(self, paths):
        self.by_suffix: dict = {}
        self.referenced: dict = {}
        for path in paths:
            parts = path.split("/")
            for start in range(len(parts)):
                self.by_suffix.setdefault("/".join(parts[start:]), path)

    def find(self, reference: str) -> str | None:
        ref = reference.strip().lstrip("./").lstrip("/")
        path = self.by_suffix.get(ref)
        if path is not None:
            self.referenced[path] = self.referenced.get(path, 0) + 1
        return path


def _content_key(item: ParsedItem) -> str:
    v = item.values
    if item.record_type in ("follower", "following") and v.get("username"):
        return make_dedup_key(PLATFORM, item.record_type, v["username"])
    if item.record_type == "profile" and v.get("username"):
        return make_dedup_key(PLATFORM, "account", v["username"])
    parts = [item.record_type] + [v.get(name) for name in EXPECTED_FIELDS[item.record_type]
                                  if name not in ("media_missing", "media_count")]
    digest = hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:24]
    return make_dedup_key(PLATFORM, item.record_type, digest)


def _field(item: ParsedItem, name: str, source: str, when: str) -> FieldRecord:
    if name in item.values:
        return available(item.values[name], source, when)
    if name in item.failed:
        return unavailable(FieldStatus.PARSING_FAILED, item.failed[name], source, when)
    if name == "created_at_local" and item.source_format == "json":
        return unavailable(FieldStatus.NOT_SUPPORTED, NOT_IN_JSON, source, when)
    if name == "created_at" and item.source_format == "html":
        return unavailable(FieldStatus.NOT_SUPPORTED, NOT_IN_HTML, source, when)
    return unavailable(FieldStatus.NOT_AVAILABLE, "the export file does not contain this value", source, when)


def build_record(item: ParsedItem, when: str, media: MediaIndex) -> Record:
    if item.record_type == "post":
        missing = [uri for uri in item.values.get("media_uris", []) if media.find(uri) is None]
        item.values["media_missing"] = missing
    if item.record_type == "profile" and item.values.get("profile_photo_uri"):
        if media.find(item.values["profile_photo_uri"]) is None:
            item.warnings.append("the profile photo file is not in the archive")
    if item.record_type in ("follower", "following") and item.values.get("username"):
        item.values["profile_url"] = CANONICAL_PROFILE_URL.format(username=item.values["username"])
    source = f"{item.source_file} ({item.location})"
    names = list(EXPECTED_FIELDS[item.record_type])
    names += [name for name in list(item.values) + list(item.failed) if name not in names]
    fields = {name: _field(item, name, source, when) for name in names}
    core_ok = all(fields[name].available for name in CORE_FIELDS[item.record_type])
    failed = any(record.status is FieldStatus.PARSING_FAILED for record in fields.values())
    status = "complete" if core_ok and not failed and not item.warnings else "partial"
    extra = {"source_file": item.source_file, "source_format": item.source_format,
             "location": item.location, "extraction_status": status, "extracted_at": when,
             "warnings": list(item.warnings), "duplicates_merged": 0, "also_found_in": []}
    return Record(item.record_type, MODULE, PLATFORM, _content_key(item), fields, extra)


def process_export(path, limits: ArchiveLimits | None = None, *, extracted_at: str | None = None) -> ExportResult:
    """Read the export ZIP and build every record. Raises ArchiveError only
    when the archive as a whole cannot be used."""
    when = extracted_at or utc_now()
    path = Path(path)
    with ExportArchive(path, limits) as archive:
        result = ExportResult(input_name=path.name, input_size_bytes=archive.size_bytes,
                              input_sha256=file_sha256(path), extracted_at=when)
        usable = [e for e in archive.entries if not e.problem and not e.is_dir]
        media = MediaIndex(e.name for e in usable if media_kind(e.basename))
        by_key: dict = {}
        kept = 0
        for entry in archive.entries:
            if entry.is_dir and not entry.problem:
                continue
            category, fmt = detect_category(entry.basename)
            row = FileEntry(entry.name, entry.size, entry.compressed_size, category or "", fmt or "",
                            ACTION_NOT_PROCESSED)
            result.files.append(row)
            if entry.problem:
                row.action, row.message = ACTION_REJECTED, entry.problem_text
                result.problems.append(f"{entry.name[:300]}: rejected - {entry.problem_text}")
                continue
            kind = media_kind(entry.basename)
            if kind:
                row.category, row.action = f"media_{kind}", ACTION_MEDIA
                continue
            if category is None:
                low = entry.basename.lower()
                row.message = ("this kind of export file is not processed by Module B"
                               if low.endswith((".json", ".html", ".htm")) else "not an export data file")
                continue
            try:
                data = archive.read(entry)
            except MemberError as exc:
                row.action, row.message = ACTION_FAILED, str(exc)
                result.problems.append(f"{entry.name}: {exc}")
                continue
            parsed = parse_file(data, entry.name, category, fmt, entry.basename)
            warnings = parsed.warnings[:MAX_WARNINGS_PER_FILE]
            if len(parsed.warnings) > MAX_WARNINGS_PER_FILE:
                warnings.append(f"... and {len(parsed.warnings) - MAX_WARNINGS_PER_FILE} more warnings")
            if parsed.failed:
                row.action, row.message = ACTION_FAILED, parsed.failed
                result.problems.append(f"{entry.name}: {parsed.failed}")
                continue
            row.action = ACTION_PARTIAL if warnings else ACTION_PARSED
            row.message = "; ".join(warnings)
            for warning in warnings:
                result.problems.append(f"{entry.name}: {warning}")
            for item in parsed.items:
                record = build_record(item, when, media)
                existing = by_key.get(record.dedup_key)
                if existing is not None:
                    existing.extra["duplicates_merged"] += 1
                    also = existing.extra["also_found_in"]
                    where = f"{item.source_file} ({item.location})"
                    if len(also) < MAX_ALSO_FOUND_IN and where not in also:
                        also.append(where)
                    result.duplicates_removed += 1
                    continue
                if kept >= MAX_RECORDS:
                    result.records_not_kept += 1
                    continue
                by_key[record.dedup_key] = record
                result.records[record.record_type].append(record)
                kept += 1
                row.records += 1
        if result.records_not_kept:
            result.problems.append(f"{result.records_not_kept} records were not kept (limit {MAX_RECORDS})")
        for entry in usable:
            kind = media_kind(entry.basename)
            if not kind:
                continue
            fields = {"path": available(entry.name, entry.name, when),
                      "size_bytes": available(entry.size, entry.name, when),
                      "media_kind": available(kind, entry.name, when),
                      "referenced_by": available(media.referenced.get(entry.name, 0), "records of this export", when)}
            extra = {"source_file": entry.name, "source_format": "media", "location": "archive entry",
                     "extraction_status": "complete", "extracted_at": when, "warnings": [],
                     "duplicates_merged": 0, "also_found_in": []}
            key = make_dedup_key(PLATFORM, "media", hashlib.sha256(entry.name.encode("utf-8")).hexdigest()[:24])
            result.records["media_file"].append(Record("media_file", MODULE, PLATFORM, key, fields, extra))
    return result
