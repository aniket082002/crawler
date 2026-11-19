"""Defensive file handling shared by the social data modules.

Reading:  input files are checked (regular file, size limit) before they are
          opened, long lines are skipped instead of being loaded, and the CSV
          field-size limit is raised only while a file is being read.
Writing:  every run gets a brand-new folder (never an existing one), and each
          output file name is validated and resolved with the crawler's own
          path-traversal check (``webcrawler.url_validator.safe_output_path``).

Standard library only, plus that one standard-library-only crawler helper.
"""
from __future__ import annotations

import contextlib
import csv
import datetime as _dt
import re
from pathlib import Path
from typing import BinaryIO, Iterator

from webcrawler.url_validator import safe_output_path

MIB = 1024 * 1024
DEFAULT_MAX_INPUT_BYTES = 2048 * MIB          # one input file
DEFAULT_MAX_RECORD_BYTES = 32 * MIB           # one JSON record (one line)
DEFAULT_WHOLE_FILE_JSON_BYTES = 64 * MIB      # JSON that cannot be streamed
DEFAULT_CSV_FIELD_CHARS = 16 * MIB            # one CSV cell

FILENAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-]{0,99}")
PREFIX_RE = re.compile(r"[a-z][a-z0-9_]{0,39}")
_DRAIN_CHUNK = 64 * 1024


class InputError(Exception):
    """An input file cannot be used. The message is safe to show."""


def format_size(size: int) -> str:
    """A size limit for messages: whole megabytes, or bytes below 1 MB."""
    return f"{size // MIB} MB" if size >= MIB else f"{size} bytes"


def check_input_file(path, max_bytes: int = DEFAULT_MAX_INPUT_BYTES) -> Path:
    """Return the path if it is an existing regular file within the size
    limit; otherwise raise InputError."""
    candidate = Path(path)
    try:
        if not candidate.exists():
            raise InputError("the file was not found")
        if not candidate.is_file():
            raise InputError("this is not a regular file")
        size = candidate.stat().st_size
    except OSError as exc:
        raise InputError(f"the file cannot be read ({type(exc).__name__})") from None
    if size > max_bytes:
        raise InputError(f"the file is larger than the {format_size(max_bytes)} input limit")
    return candidate


def iter_bounded_lines(stream: BinaryIO, max_line_bytes: int) -> Iterator[tuple]:
    """Yield (line_number, line_bytes) from a binary stream. A line longer than
    ``max_line_bytes`` (not counting its line ending) is never loaded: the
    rest of it is skipped and (line_number, None) is yielded instead."""
    if max_line_bytes < 1:
        raise ValueError("max_line_bytes must be at least 1.")
    number = 0
    while True:
        chunk = stream.readline(max_line_bytes + 1)
        if not chunk:
            return
        number += 1
        if len(chunk) > max_line_bytes and not chunk.endswith(b"\n"):
            while True:                         # discard the remainder of this line
                more = stream.readline(_DRAIN_CHUNK)
                if not more or more.endswith(b"\n"):
                    break
            yield number, None
        else:
            yield number, chunk


@contextlib.contextmanager
def csv_field_limit(limit: int = DEFAULT_CSV_FIELD_CHARS):
    """Temporarily change Python's process-wide CSV cell-size limit and always
    put the previous value back. (The default of 131,072 characters is smaller
    than the page text the crawler can write into one cell.)"""
    previous = csv.field_size_limit(limit)
    try:
        yield
    finally:
        csv.field_size_limit(previous)


def new_run_dir(root, prefix: str, now: _dt.datetime | None = None) -> Path:
    """Create and return a new folder ``<root>/<prefix>_<YYYYMMDD-HHMMSS>``
    (with ``-2``, ``-3`` ... if that name is taken). An existing folder is
    never reused, so earlier results are never overwritten."""
    if not isinstance(prefix, str) or not PREFIX_RE.fullmatch(prefix):
        raise ValueError("prefix must be a short lowercase name.")
    base = Path(root)
    base.mkdir(parents=True, exist_ok=True)
    stamp = (now or _dt.datetime.now()).strftime("%Y%m%d-%H%M%S")
    for attempt in range(1, 1000):
        name = f"{prefix}_{stamp}" if attempt == 1 else f"{prefix}_{stamp}-{attempt}"
        candidate = base / name
        try:
            candidate.mkdir(exist_ok=False)
        except FileExistsError:
            continue
        return candidate
    raise OSError("Could not create a unique output folder.")


def output_file(run_dir, filename: str) -> Path:
    """Path of an output file inside ``run_dir``. The name must be a plain
    file name; anything that could leave the folder is refused."""
    if not isinstance(filename, str) or not FILENAME_RE.fullmatch(filename) or ".." in filename:
        raise ValueError("Unsafe output file name.")
    return safe_output_path(run_dir, filename)
