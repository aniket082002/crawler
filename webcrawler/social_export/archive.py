"""Safe, read-only inspection of an Instagram export ZIP file.

Nothing is ever extracted to disk. Archive members are read into memory, one
at a time and with byte limits, and only the members that are parsed (JSON
and HTML files) are read at all; media files are only listed. A path inside
the archive is therefore never used as a path on this computer, so a
"../../" entry cannot write anywhere. Such entries are still detected,
skipped and reported, because they mean the archive was not produced by
Instagram as-is.

Checks on the whole archive (failure raises ArchiveError):
  * the input is an existing regular file, within the size limit;
  * it is a readable ZIP file;
  * the number of entries and the total declared uncompressed size are
    within their limits.

Checks on each entry (failure marks the entry as rejected, others continue):
  * empty or overlong name, control characters, backslashes, absolute paths,
    drive letters, ".." segments;
  * symbolic links, encrypted entries, duplicate names;
  * when read: the size limit for one parsed file, a compression ratio that
    suggests a "zip bomb", the total read budget, and damaged data (CRC and
    decompression errors).

The file is opened with mode "r" only. Standard library only.
"""
from __future__ import annotations

import re
import stat
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from webcrawler.social_common.safe_io import MIB, InputError, check_input_file, format_size

GIB = 1024 * MIB

PROBLEM_TEXT = {
    "empty_name": "the entry has no name",
    "name_too_long": "the entry name is too long",
    "control_characters": "the entry name contains control characters",
    "backslash": "the entry name contains a backslash (not a valid ZIP path separator)",
    "absolute_path": "the entry name is an absolute path",
    "drive_letter": "the entry name starts with a drive letter",
    "parent_reference": "the entry name contains a '..' segment (path traversal attempt)",
    "symlink": "the entry is a symbolic link",
    "encrypted": "the entry is encrypted",
    "duplicate_name": "another entry has the same name",
    "too_large": "the file is larger than the limit for one parsed file",
    "compression_ratio": "the compression ratio is implausibly high (possible zip bomb)",
    "read_budget": "the total read limit for this archive was reached",
    "damaged": "the entry's data is damaged or uses an unsupported compression",
}
_DRIVE_RE = re.compile(r"[A-Za-z]:")


class ArchiveError(Exception):
    """The archive as a whole cannot be used. The message is safe to show."""


class MemberError(Exception):
    """One entry cannot be read. ``code`` is a key of PROBLEM_TEXT."""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        text = PROBLEM_TEXT.get(code, code)
        super().__init__(f"{text} ({detail})" if detail else text)


@dataclass(frozen=True)
class ArchiveLimits:
    max_archive_bytes: int = 8 * GIB              # the ZIP file itself
    max_entries: int = 200_000
    max_total_uncompressed_bytes: int = 64 * GIB  # sum of declared sizes
    max_member_bytes: int = 64 * MIB              # one JSON / HTML file that is parsed
    max_total_read_bytes: int = 1 * GIB           # all parsed files together
    max_compression_ratio: int = 200
    ratio_check_min_bytes: int = 1 * MIB          # small files are not ratio-checked
    max_name_length: int = 1024


@dataclass
class Entry:
    name: str
    size: int
    compressed_size: int
    is_dir: bool
    problem: str | None = None
    info: zipfile.ZipInfo | None = field(default=None, repr=False, compare=False)

    @property
    def problem_text(self) -> str:
        return PROBLEM_TEXT.get(self.problem, self.problem or "")

    @property
    def basename(self) -> str:
        return self.name.rstrip("/").rsplit("/", 1)[-1]


def name_problem(name: str, max_length: int) -> str | None:
    if not name:
        return "empty_name"
    if len(name) > max_length:
        return "name_too_long"
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        return "control_characters"
    if "\\" in name:
        return "backslash"
    if name.startswith("/"):
        return "absolute_path"
    if _DRIVE_RE.match(name):
        return "drive_letter"
    if ".." in name.split("/"):
        return "parent_reference"
    return None


class ExportArchive:
    """Use as a context manager: ``with ExportArchive(path) as archive: ...``"""

    def __init__(self, path, limits: ArchiveLimits | None = None):
        self.limits = limits or ArchiveLimits()
        self.path = Path(path)
        self.bytes_read = 0
        try:
            checked = check_input_file(self.path, self.limits.max_archive_bytes)
        except InputError as exc:
            raise ArchiveError(f"The export file cannot be used: {exc}.") from None
        self.size_bytes = checked.stat().st_size
        try:
            self._zip = zipfile.ZipFile(checked, "r")
        except zipfile.BadZipFile:
            raise ArchiveError("The file is not a ZIP file, or the ZIP file is damaged.") from None
        except (zipfile.LargeZipFile, NotImplementedError, ValueError, EOFError) as exc:
            raise ArchiveError(f"The ZIP file cannot be read ({type(exc).__name__}).") from None
        except OSError as exc:
            raise ArchiveError(f"The export file cannot be opened ({type(exc).__name__}).") from None
        try:
            self.entries = self._inspect()
        except ArchiveError:
            self._zip.close()
            raise

    def _inspect(self) -> list:
        infos = self._zip.infolist()
        if len(infos) > self.limits.max_entries:
            raise ArchiveError(f"The ZIP file has {len(infos)} entries; the limit is {self.limits.max_entries}.")
        declared = sum(info.file_size for info in infos)
        if declared > self.limits.max_total_uncompressed_bytes:
            raise ArchiveError("The ZIP file would unpack to more than the "
                               f"{format_size(self.limits.max_total_uncompressed_bytes)} limit.")
        entries, seen = [], set()
        for info in infos:
            name = info.filename
            problem = name_problem(name, self.limits.max_name_length)
            if problem is None and stat.S_ISLNK(info.external_attr >> 16):
                problem = "symlink"
            if problem is None and info.flag_bits & 0x1:
                problem = "encrypted"
            if problem is None and name in seen:
                problem = "duplicate_name"
            seen.add(name)
            entries.append(Entry(name=name, size=info.file_size, compressed_size=info.compress_size,
                                 is_dir=name.endswith("/"), problem=problem, info=info))
        return entries

    def read(self, entry: Entry) -> bytes:
        """The entry's bytes. Raises MemberError; never extracts anything."""
        limits = self.limits
        if entry.problem:
            raise MemberError(entry.problem)
        if entry.size > limits.max_member_bytes:
            raise MemberError("too_large", format_size(limits.max_member_bytes))
        if entry.size >= limits.ratio_check_min_bytes and (
                entry.compressed_size <= 0 or entry.size / entry.compressed_size > limits.max_compression_ratio):
            raise MemberError("compression_ratio")
        remaining = limits.max_total_read_bytes - self.bytes_read
        if remaining <= 0:
            raise MemberError("read_budget")
        cap = min(limits.max_member_bytes, remaining)
        try:
            with self._zip.open(entry.info, "r") as stream:
                data = stream.read(cap + 1)
        except (zipfile.BadZipFile, zlib.error, EOFError, OSError, NotImplementedError,
                RuntimeError, ValueError) as exc:
            raise MemberError("damaged", type(exc).__name__) from None
        if len(data) > cap:
            # More data than allowed, whatever the entry claimed about its size.
            raise MemberError("too_large" if cap == limits.max_member_bytes else "read_budget")
        self.bytes_read += len(data)
        return data

    def close(self) -> None:
        self._zip.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
