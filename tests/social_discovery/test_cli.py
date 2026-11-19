"""The command line, end to end, with everything in temporary folders."""
import datetime as dt
import json
import pathlib
import subprocess
import sys

import pytest

from tests.social_discovery.builders import IG, LEGACY_COLUMNS, legacy_page, page, write_csv, write_json
from webcrawler.social_discovery import cli
from webcrawler.social_discovery import report as R

PROJECT = pathlib.Path(__file__).resolve().parents[2]
NOW = dt.datetime(2026, 11, 18, 9, 30, 15)
RUN = "discovery_20261118-093015"


class Lines:
    def __init__(self):
        self.lines = []

    def __call__(self, text=""):
        self.lines.append(str(text))

    @property
    def text(self):
        return "\n".join(self.lines)


@pytest.fixture
def crawl(tmp_path):
    folder = tmp_path / "outputs" / "run_20261113-100519"
    folder.mkdir(parents=True)
    records = [page("https://acme.example/", social=[f"{IG}/nike"], links=[f"{IG}/Nike/", f"{IG}/p/CxYz/"]),
               page("https://acme.example/about", links=[f"{IG}/nike/", f"{IG}/adidas"])]
    write_json(folder / "items.json", records)
    write_csv(folder / "items.csv", records)
    return folder


def test_full_run(tmp_path, crawl):
    out = Lines()
    root = tmp_path / "results"
    assert cli.main([str(crawl), "--output-root", str(root)], out=out, now=NOW) == cli.EXIT_OK
    assert {p.name for p in (root / RUN).iterdir()} == {
        R.FILE_JSON, R.FILE_PROFILES_CSV, R.FILE_SOURCES_CSV, R.FILE_REJECTED_CSV, R.FILE_XLSX, R.FILE_SUMMARY}
    assert "Inputs read              : 1 of 1" in out.text
    assert "Page records read        : 2" in out.text
    assert "Instagram profiles found : 2" in out.text
    assert "  @nike  https://www.instagram.com/nike/  (1 website, 2 pages)" in out.text
    assert "  @adidas  https://www.instagram.com/adidas/  (1 website, 1 page)" in out.text
    assert "Rejected Instagram links : 1" in out.text and "  post_url: 1" in out.text
    assert "Problems                 : 0" in out.text
    assert RUN in out.text and "Instagram was not contacted" in out.text


def test_dry_run_writes_nothing(tmp_path, crawl):
    out = Lines()
    root = tmp_path / "results"
    before = sorted(p.as_posix() for p in tmp_path.rglob("*"))
    assert cli.main([str(crawl), "--output-root", str(root), "--dry-run"], out=out) == cli.EXIT_OK
    assert sorted(p.as_posix() for p in tmp_path.rglob("*")) == before and not root.exists()
    assert "no files were written" in out.text and "Instagram profiles found : 2" in out.text


@pytest.mark.parametrize("formats, files", [
    ("csv", {R.FILE_PROFILES_CSV, R.FILE_SOURCES_CSV, R.FILE_REJECTED_CSV, R.FILE_SUMMARY}),
    ("json", {R.FILE_JSON, R.FILE_SUMMARY}),
    ("JSON, csv", {R.FILE_JSON, R.FILE_PROFILES_CSV, R.FILE_SOURCES_CSV, R.FILE_REJECTED_CSV, R.FILE_SUMMARY}),
])
def test_format_selection(tmp_path, crawl, formats, files):
    root = tmp_path / "results"
    assert cli.main([str(crawl), "--output-root", str(root), "--formats", formats], out=Lines(), now=NOW) == 0
    assert {p.name for p in (root / RUN).iterdir()} == files


def test_unknown_format_is_refused_before_anything_is_read_or_written(tmp_path, crawl):
    out = Lines()
    root = tmp_path / "results"
    assert cli.main([str(crawl), "--output-root", str(root), "--formats", "pdf"], out=out) == cli.EXIT_ERROR
    assert "ERROR: Unsupported export format: pdf" in out.text and not root.exists()


def test_csv_input_and_file_arguments(tmp_path, crawl):
    out = Lines()
    args = [str(crawl / "items.csv"), "--output-root", str(tmp_path / "results"), "--formats", "json"]
    assert cli.main(args, out=out, now=NOW) == cli.EXIT_OK
    document = json.loads((tmp_path / "results" / RUN / R.FILE_JSON).read_text(encoding="utf-8"))
    assert [p["fields"]["username"]["value"] for p in document["profiles"]] == ["adidas", "nike"]
    assert document["inputs"][0]["format"] == "csv"

    out = Lines()
    assert cli.main([str(crawl), "--input-format", "csv", "--dry-run"], out=out) == cli.EXIT_OK
    assert "Instagram profiles found : 2" in out.text


def test_all_runs(tmp_path, crawl):
    outputs = crawl.parent
    other = outputs / "run_20261117-152859"
    other.mkdir()
    write_json(other / "items.json", [page("https://beta.example/", links=[f"{IG}/NIKE"])])
    write_csv(outputs / "items.csv", [legacy_page("https://old.example/")], columns=LEGACY_COLUMNS)
    out = Lines()
    assert cli.main([str(outputs), "--all-runs", "--dry-run"], out=out) == cli.EXIT_OK
    assert "Inputs read              : 3 of 3" in out.text
    assert "  @nike  https://www.instagram.com/nike/  (2 websites, 3 pages)" in out.text
    assert "older version of the crawler" in out.text


def test_readable_input_without_profiles_is_a_success(tmp_path):
    path = write_json(tmp_path / "items.json", [page("https://acme.example/", links=["https://twitter.com/acme"])])
    out = Lines()
    assert cli.main([str(path), "--output-root", str(tmp_path / "results")], out=out, now=NOW) == cli.EXIT_OK
    assert "Instagram profiles found : 0" in out.text
    document = json.loads((tmp_path / "results" / RUN / R.FILE_JSON).read_text(encoding="utf-8"))
    assert document["profiles"] == []


def test_no_readable_input_is_an_error_and_writes_nothing(tmp_path):
    corrupt = tmp_path / "items.json"
    corrupt.write_bytes(b"not json")
    out = Lines()
    root = tmp_path / "results"
    code = cli.main([str(tmp_path / "missing"), str(corrupt), "--output-root", str(root)], out=out)
    assert code == cli.EXIT_ERROR and not root.exists()
    assert "ERROR: no crawler output could be read." in out.text
    assert "the path was not found" in out.text and "not valid JSON" in out.text


def test_no_paths_is_a_usage_error():
    with pytest.raises(SystemExit) as error:
        cli.main([], out=Lines())
    assert error.value.code == 2


@pytest.mark.parametrize("value", ["0", "-5"])
def test_input_limit_must_be_positive(crawl, value):
    out = Lines()
    assert cli.main([str(crawl), "--dry-run", "--max-input-mb", value], out=out) == cli.EXIT_ERROR
    assert "--max-input-mb must be at least 1" in out.text


def test_output_folder_that_cannot_be_created(tmp_path, crawl):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a folder", encoding="utf-8")
    out = Lines()
    assert cli.main([str(crawl), "--output-root", str(blocker)], out=out) == cli.EXIT_ERROR
    assert "ERROR: the output folder could not be created" in out.text
    assert blocker.read_text(encoding="utf-8") == "a file, not a folder"


def test_export_error_is_reported_and_other_files_are_kept(tmp_path, crawl, monkeypatch):
    from webcrawler.social_common.exporters import ExportError

    def broken(path, sheets):
        raise ExportError("Excel export needs the xlsxwriter package, which is not installed.")
    monkeypatch.setattr(R, "write_xlsx", broken)
    out = Lines()
    root = tmp_path / "results"
    assert cli.main([str(crawl), "--output-root", str(root)], out=out, now=NOW) == cli.EXIT_ERROR
    assert "! xlsx export failed: Excel export needs the xlsxwriter package" in out.text
    assert (root / RUN / R.FILE_PROFILES_CSV).is_file() and (root / RUN / R.FILE_JSON).is_file()


def test_long_profile_list_is_shortened_on_screen(tmp_path):
    links = [f"{IG}/account{n:03d}/" for n in range(40)]
    path = write_json(tmp_path / "items.json", [page("https://acme.example/", links=links)])
    out = Lines()
    assert cli.main([str(path), "--dry-run"], out=out) == cli.EXIT_OK
    assert "Instagram profiles found : 40" in out.text and "... and 15 more" in out.text
    assert sum(line.startswith("  @account") for line in out.lines) == cli.MAX_PROFILES_SHOWN


def test_default_output_folder_is_next_to_the_crawlers_run_folders():
    assert cli.default_output_root() == PROJECT / "outputs" / "social"
    assert cli.PROJECT_DIR == PROJECT


def test_default_output_folder_is_used_when_none_is_given(tmp_path, crawl, monkeypatch):
    monkeypatch.setattr(cli, "default_output_root", lambda: tmp_path / "default-root")
    assert cli.main([str(crawl), "--formats", "json"], out=Lines(), now=NOW) == cli.EXIT_OK
    assert (tmp_path / "default-root" / RUN / R.FILE_JSON).is_file()


# --- as a separate process -------------------------------------------------------------------
def run_module(*args):
    return subprocess.run([sys.executable, "-B", "-m", "webcrawler.social_discovery", *args],
                          capture_output=True, text=True, timeout=120, cwd=str(PROJECT))


def test_help_text():
    result = run_module("--help")
    assert result.returncode == 0
    assert "Nothing is fetched from the internet" in result.stdout and "--all-runs" in result.stdout


def test_module_runs_end_to_end_in_a_fresh_process(tmp_path, crawl):
    root = tmp_path / "results"
    result = run_module(str(crawl), "--output-root", str(root), "--formats", "csv,json")
    assert result.returncode == 0, result.stderr
    assert "Instagram profiles found : 2" in result.stdout
    (run_dir,) = list(root.iterdir())
    assert run_dir.name.startswith("discovery_") and (run_dir / R.FILE_PROFILES_CSV).is_file()


def test_fresh_process_exit_codes(tmp_path):
    assert run_module().returncode == 2                                      # no path given
    assert run_module(str(tmp_path / "missing"), "--dry-run").returncode == 1
