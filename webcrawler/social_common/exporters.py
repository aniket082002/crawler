"""CSV, JSON and Excel writers shared by the social data modules.

The formats and their names match the website crawler's exports
(csv / json / xlsx). Safety rules applied to every flat (CSV / Excel) cell:

  * Spreadsheet formulas: a CSV text cell that starts with ``=``, ``+``,
    ``-``, ``@``, a tab or a carriage return is prefixed with a single quote,
    so a spreadsheet shows it as text instead of running it. Excel cells are
    written as explicit strings, never as formulas or hyperlinks. Numbers are
    written as numbers and are not affected.
  * Unprintable characters: control characters (except tab, line feed and
    carriage return), lone surrogates and the two non-characters U+FFFE/U+FFFF
    are shown as a visible ``\\uXXXX`` escape, because they can break CSV and
    are illegal in Excel files.
  * Excel's 32,767-character cell limit: longer text is cut in the .xlsx only
    and marked; CSV and JSON keep the full value.

JSON output is strict JSON and keeps every value exactly as given.

Standard library only, except that Excel export needs ``xlsxwriter`` (already
a requirement of the crawler); it is imported only when an .xlsx is written.
"""
from __future__ import annotations

import csv
import json
import math
import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

SUPPORTED_FORMATS = ("csv", "json", "xlsx")
LIST_SEPARATOR = " | "

EXCEL_MAX_CELL = 32_767
EXCEL_MAX_ROWS = 1_048_576
EXCEL_TRUNCATION_MARKER = " ...[truncated in Excel; full value in CSV/JSON]"

FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")
FORMULA_GUARD = "'"

_UNPRINTABLE_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ud800-\udfff\ufffe\uffff]")


class ExportError(Exception):
    """An export file could not be written. The message is safe to show."""


def parse_formats(value: Any) -> tuple:
    """Normalise a format selection (comma string or list) into an ordered
    tuple of supported formats. None means all. Unknown or empty selections
    raise ValueError, so a typo never silently produces no output."""
    if value is None:
        return SUPPORTED_FORMATS
    parts = value.split(",") if isinstance(value, str) else list(value)
    requested = {str(p).strip().lower() for p in parts if str(p).strip()}
    if not requested:
        raise ValueError("No export format given; expected any of csv,json,xlsx")
    unknown = requested - set(SUPPORTED_FORMATS)
    if unknown:
        raise ValueError(f"Unsupported export format: {', '.join(sorted(unknown))} "
                         f"(supported: {', '.join(SUPPORTED_FORMATS)})")
    return tuple(f for f in SUPPORTED_FORMATS if f in requested)


# --- cell conversion -------------------------------------------------------------
def printable_text(text: str) -> str:
    """Show control characters, lone surrogates and non-characters as \\uXXXX."""
    return _UNPRINTABLE_RE.sub(lambda m: f"\\u{ord(m.group()):04x}", text)


def flat_cell(value: Any) -> Any:
    """One spreadsheet cell: str, int, float or bool. None -> "", lists are
    joined with " | ", dicts become compact JSON text."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        return printable_text(value)
    if isinstance(value, (list, tuple)):
        return LIST_SEPARATOR.join(str(flat_cell(v)) for v in value if v not in (None, ""))
    if isinstance(value, Mapping):
        return printable_text(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))
    return printable_text(str(value))


def csv_cell(value: Any) -> Any:
    """flat_cell plus the formula guard for text cells."""
    cell = flat_cell(value)
    if isinstance(cell, str) and cell.startswith(FORMULA_TRIGGERS):
        return FORMULA_GUARD + cell
    return cell


def _check_row(row: Any, columns: Sequence[str]) -> Mapping:
    if not isinstance(row, Mapping):
        raise ExportError("Export rows must be mappings of column name -> value.")
    extra = set(row) - set(columns)
    if extra:
        raise ExportError(f"Export row has unknown columns: {', '.join(sorted(map(str, extra)))}")
    return row


# --- writers ------------------------------------------------------------------------
def write_csv(path: Path, columns: Sequence[str], rows: Iterable[Mapping[str, Any]]) -> int:
    """Write a UTF-8 CSV with a header row. Returns the number of data rows."""
    count = 0
    try:
        with open(path, "w", newline="", encoding="utf-8", errors="backslashreplace") as handle:
            writer = csv.writer(handle)
            writer.writerow([csv_cell(c) for c in columns])
            for row in rows:
                row = _check_row(row, columns)
                writer.writerow([csv_cell(row.get(c)) for c in columns])
                count += 1
    except (OSError, csv.Error) as exc:
        raise ExportError(f"The CSV file could not be written ({type(exc).__name__}).") from None
    return count


def write_json(path: Path, document: Any) -> None:
    """Write strict, indented UTF-8 JSON (NaN/Infinity are refused)."""
    try:
        text = json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ExportError(f"The data could not be written as JSON ({type(exc).__name__}).") from None
    try:
        # backslashreplace turns a lone surrogate into the JSON escape \udXXX,
        # which reads back as the same character.
        with open(path, "w", encoding="utf-8", errors="backslashreplace", newline="\n") as handle:
            handle.write(text)
            handle.write("\n")
    except OSError as exc:
        raise ExportError(f"The JSON file could not be written ({type(exc).__name__}).") from None


def write_xlsx(path: Path, sheets: Sequence[tuple]) -> dict:
    """Write an .xlsx with one worksheet per (name, columns, rows) entry.
    Returns {"rows": {sheet: n}, "truncated_cells": n}."""
    try:
        import xlsxwriter
    except ImportError:
        raise ExportError("Excel export needs the xlsxwriter package, which is not installed.") from None

    stats = {"rows": {}, "truncated_cells": 0}
    try:
        workbook = xlsxwriter.Workbook(str(path), {
            "in_memory": True,              # no temporary files
            "strings_to_formulas": False,
            "strings_to_urls": False,
            "strings_to_numbers": False,
        })
    except Exception as exc:  # noqa: BLE001 - library-specific errors
        raise ExportError(f"The Excel file could not be created ({type(exc).__name__}).") from None

    failure: ExportError | None = None
    try:
        for name, columns, rows in sheets:
            sheet = workbook.add_worksheet(name)
            for col, title in enumerate(columns):
                sheet.write_string(0, col, printable_text(str(title)))
            line = 1
            for row in rows:
                row = _check_row(row, columns)
                if line >= EXCEL_MAX_ROWS:
                    raise ExportError(f"Sheet '{name}' has more rows than Excel allows.")
                for col, key in enumerate(columns):
                    cell = flat_cell(row.get(key))
                    if isinstance(cell, bool):
                        sheet.write_boolean(line, col, cell)
                    elif isinstance(cell, (int, float)):
                        sheet.write_number(line, col, cell)
                    elif cell:
                        if len(cell) > EXCEL_MAX_CELL:
                            cell = cell[:EXCEL_MAX_CELL - len(EXCEL_TRUNCATION_MARKER)] + EXCEL_TRUNCATION_MARKER
                            stats["truncated_cells"] += 1
                        sheet.write_string(line, col, cell)
                line += 1
            stats["rows"][name] = line - 1
    except ExportError as exc:
        failure = exc
    except Exception as exc:  # noqa: BLE001 - library-specific errors
        failure = ExportError(f"The Excel file could not be written ({type(exc).__name__}).")
    try:
        workbook.close()
    except Exception as exc:  # noqa: BLE001 - e.g. the file is open in Excel
        failure = failure or ExportError(f"The Excel file could not be saved ({type(exc).__name__}).")
    if failure is not None:
        try:
            Path(path).unlink(missing_ok=True)   # never leave a half-written workbook
        except OSError:
            pass
        raise failure
    return stats
