"""Command line for Module C (Website Social Profile Discovery).

    python -m webcrawler.social_discovery outputs/run_20261113-100519
    python -m webcrawler.social_discovery outputs --all-runs
    python -m webcrawler.social_discovery outputs/run_X/items.csv --formats csv
    python -m webcrawler.social_discovery outputs --all-runs --dry-run

It reads crawler output that already exists and writes a new folder under
``outputs/social/``. It does not crawl, and it never contacts Instagram or
any other service.
"""
from __future__ import annotations

import argparse
import datetime as _dt
from pathlib import Path
from typing import Callable, Sequence

from webcrawler.social_common.exporters import SUPPORTED_FORMATS, parse_formats
from webcrawler.social_common.safe_io import MIB
from webcrawler.social_discovery.discover import DiscoveryResult, discover_paths
from webcrawler.social_discovery.reader import INPUT_FORMATS, Limits
from webcrawler.social_discovery.report import OutputReport, write_outputs

PROJECT_DIR = Path(__file__).resolve().parents[2]

EXIT_OK = 0
EXIT_ERROR = 1

LINE = "=" * 44
MAX_PROFILES_SHOWN = 25
MAX_PROBLEMS_SHOWN = 10


def default_output_root() -> Path:
    """``outputs/social`` next to the crawler's own run folders."""
    return PROJECT_DIR / "outputs" / "social"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m webcrawler.social_discovery",
        description="Find the Instagram profile links that crawled websites publish, using only "
                    "existing crawler output files. Nothing is fetched from the internet.")
    parser.add_argument("paths", nargs="+", metavar="PATH",
                        help="a crawler run folder (holding items.json or items.csv), or one of those "
                             "files; several may be given")
    parser.add_argument("--all-runs", action="store_true",
                        help="for each folder given, also read its run_* sub-folders "
                             "(for example: outputs --all-runs)")
    parser.add_argument("--input-format", choices=INPUT_FORMATS, default="auto",
                        help="which file to read in a folder: auto = items.json if present, "
                             "otherwise items.csv (default: auto)")
    parser.add_argument("--formats", default=",".join(SUPPORTED_FORMATS),
                        help="export formats, comma-separated: csv, json, xlsx (default: all three)")
    parser.add_argument("--output-root", default=None, metavar="DIR",
                        help="folder that receives a new discovery_<time> sub-folder "
                             "(default: outputs/social in the project)")
    parser.add_argument("--dry-run", action="store_true",
                        help="show the results without writing any files")
    parser.add_argument("--max-input-mb", type=int, default=Limits().max_input_bytes // MIB, metavar="MB",
                        help="largest input file that will be read (default: %(default)s)")
    return parser


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def print_summary(result: DiscoveryResult, out: Callable, output: OutputReport | None = None,
                  dry_run: bool = False) -> None:
    summary = result.summary()
    out(LINE)
    out(" WEBSITE SOCIAL DISCOVERY - INSTAGRAM")
    out(LINE)
    out(f"Inputs read              : {summary['inputs_read']} of {summary['inputs_given']}")
    out(f"Page records read        : {summary['page_records_read']}")
    out(f"Instagram profiles found : {summary['profiles_found']}")
    for profile in result.profiles[:MAX_PROFILES_SHOWN]:
        where = f"{_count(len(profile.websites()), 'website')}, {_count(len(profile.page_urls()), 'page')}"
        out(f"  {profile.ref.handle}  {profile.profile_url}  ({where})")
    if len(result.profiles) > MAX_PROFILES_SHOWN:
        out(f"  ... and {len(result.profiles) - MAX_PROFILES_SHOWN} more (see the export files)")

    out(f"Rejected Instagram links : {summary['rejected_links']}")
    for reason, number in summary["rejected_links_by_reason"].items():
        out(f"  {reason}: {number}")
    if summary["rejected_links_not_tracked"]:
        out(f"  (plus {summary['rejected_links_not_tracked']} further sightings that were only counted)")

    problems = result.problems()
    out(f"Problems                 : {summary['problems']}")
    for message in problems[:MAX_PROBLEMS_SHOWN]:
        out(f"  ! {message}")
    if summary["problems"] > MAX_PROBLEMS_SHOWN:
        out(f"  ... and {summary['problems'] - MAX_PROBLEMS_SHOWN} more")

    if output is not None:
        out(f"Output folder            : {output.run_dir}")
        for name in sorted(output.files):
            out(f"  {name}")
        for label, message in output.errors.items():
            out(f"  ! {label} export failed: {message}")
    elif dry_run:
        out("Dry run                  : no files were written")
    out("Note: Instagram was not contacted. Profiles are identified from the form of the")
    out("      links only, so this does not confirm that an account exists.")


def main(argv: Sequence[str] | None = None, out: Callable = print,
         now: _dt.datetime | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        formats = parse_formats(args.formats)
    except ValueError as exc:
        out(f"ERROR: {exc}")
        return EXIT_ERROR
    if args.max_input_mb < 1:
        out("ERROR: --max-input-mb must be at least 1")
        return EXIT_ERROR

    limits = Limits(max_input_bytes=args.max_input_mb * MIB)
    result = discover_paths(args.paths, all_runs=args.all_runs, input_format=args.input_format,
                            limits=limits)
    readable = result.summary()["inputs_read"] > 0

    output: OutputReport | None = None
    write_error = ""
    if readable and not args.dry_run:
        root = Path(args.output_root) if args.output_root else default_output_root()
        try:
            output = write_outputs(result, root, formats, now=now)
        except (OSError, ValueError) as exc:
            write_error = f"the output folder could not be created ({type(exc).__name__})"

    print_summary(result, out, output, dry_run=args.dry_run)
    if not readable:
        out("ERROR: no crawler output could be read.")
        return EXIT_ERROR
    if write_error:
        out(f"ERROR: {write_error}")
        return EXIT_ERROR
    if output is not None and output.errors:
        return EXIT_ERROR
    return EXIT_OK
