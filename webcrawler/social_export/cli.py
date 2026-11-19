"""Command line for Module B (Instagram Export Parser).

    python -m webcrawler.social_export C:\\Downloads\\instagram-export.zip
    python -m webcrawler.social_export export.zip --formats csv,json --output-root D:\\results
    python -m webcrawler.social_export export.zip --dry-run

Reads the ZIP read-only and writes a new folder under ``outputs/social/``.
Nothing is fetched from the internet.
"""
from __future__ import annotations

import argparse
import datetime as _dt
from pathlib import Path
from typing import Callable, Sequence

from webcrawler.social_common.exporters import SUPPORTED_FORMATS, parse_formats
from webcrawler.social_common.safe_io import MIB
from webcrawler.social_export.archive import ArchiveError, ArchiveLimits
from webcrawler.social_export.records import ExportResult, process_export
from webcrawler.social_export.report import OutputReport, write_outputs

PROJECT_DIR = Path(__file__).resolve().parents[2]
EXIT_OK = 0
EXIT_ERROR = 1
LINE = "=" * 44
MAX_PROBLEMS_SHOWN = 10


def default_output_root() -> Path:
    return PROJECT_DIR / "outputs" / "social"


def build_parser() -> argparse.ArgumentParser:
    defaults = ArchiveLimits()
    parser = argparse.ArgumentParser(
        prog="python -m webcrawler.social_export",
        description="Read your own Instagram 'Download Your Information' ZIP file offline and export "
                    "its profile, posts, comments, likes, followers and following as CSV, JSON and Excel.")
    parser.add_argument("zip_file", metavar="EXPORT_ZIP", help="the export ZIP file (it is only read)")
    parser.add_argument("--formats", default=",".join(SUPPORTED_FORMATS),
                        help="export formats, comma-separated: csv, json, xlsx (default: all three)")
    parser.add_argument("--output-root", default=None, metavar="DIR",
                        help="folder that receives a new export_<time> sub-folder "
                             "(default: outputs/social in the project)")
    parser.add_argument("--dry-run", action="store_true", help="show the results without writing any files")
    parser.add_argument("--max-archive-mb", type=int, default=defaults.max_archive_bytes // MIB, metavar="MB",
                        help="largest ZIP file that will be opened (default: %(default)s)")
    parser.add_argument("--max-file-mb", type=int, default=defaults.max_member_bytes // MIB, metavar="MB",
                        help="largest single JSON/HTML file inside the ZIP that will be read "
                             "(default: %(default)s)")
    return parser


def print_summary(result: ExportResult, out: Callable, output: OutputReport | None, dry_run: bool) -> None:
    summary = result.summary()
    out(LINE)
    out(" INSTAGRAM EXPORT PARSER")
    out(LINE)
    out(f"Export file              : {result.input_name} ({result.input_size_bytes} bytes)")
    out(f"Archive entries          : {summary['archive_entries']}")
    for action, number in summary["entries_by_action"].items():
        out(f"  {action}: {number}")
    out("Records")
    for kind, number in summary["records"].items():
        out(f"  {kind:<12}: {number}")
    out(f"Duplicates removed       : {summary['duplicates_removed']}")
    out(f"Problems                 : {summary['problems']}")
    for message in result.problems[:MAX_PROBLEMS_SHOWN]:
        out(f"  ! {message[:300]}")
    if len(result.problems) > MAX_PROBLEMS_SHOWN:
        out(f"  ... and {len(result.problems) - MAX_PROBLEMS_SHOWN} more (see the export files)")
    if output is not None:
        out(f"Output folder            : {output.run_dir}")
        for name in sorted(output.files):
            out(f"  {name}")
        for label, message in output.errors.items():
            out(f"  ! {label} export failed: {message}")
    elif dry_run:
        out("Dry run                  : no files were written")
    out("Note: only the export file was read. Instagram was not contacted.")


def main(argv: Sequence[str] | None = None, out: Callable = print, now: _dt.datetime | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        formats = parse_formats(args.formats)
    except ValueError as exc:
        out(f"ERROR: {exc}")
        return EXIT_ERROR
    if args.max_archive_mb < 1 or args.max_file_mb < 1:
        out("ERROR: --max-archive-mb and --max-file-mb must be at least 1")
        return EXIT_ERROR
    defaults = ArchiveLimits()
    limits = ArchiveLimits(max_archive_bytes=args.max_archive_mb * MIB,
                           max_member_bytes=args.max_file_mb * MIB,
                           max_total_read_bytes=max(defaults.max_total_read_bytes, args.max_file_mb * MIB))
    try:
        result = process_export(args.zip_file, limits)
    except ArchiveError as exc:
        out(f"ERROR: {exc}")
        return EXIT_ERROR
    output = None
    if not args.dry_run:
        root = Path(args.output_root) if args.output_root else default_output_root()
        try:
            output = write_outputs(result, root, formats, now=now)
        except (OSError, ValueError) as exc:
            print_summary(result, out, None, False)
            out(f"ERROR: the output folder could not be created ({type(exc).__name__})")
            return EXIT_ERROR
    print_summary(result, out, output, args.dry_run)
    if output is not None and output.errors:
        return EXIT_ERROR
    return EXIT_OK
