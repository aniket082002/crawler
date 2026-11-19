"""The shared record structure, the flat tables and the files written."""
import csv
import datetime as dt
import json

import pytest

from tests.social_discovery.builders import IG, STAMP_UTC, page, write_json
from webcrawler.social_common.exporters import ExportError
from webcrawler.social_common.schema import FieldStatus, Record
from webcrawler.social_discovery import report as R
from webcrawler.social_discovery.discover import discover_paths

NOW = dt.datetime(2026, 11, 18, 9, 30, 15)
GENERATED = "2026-11-18T04:00:15Z"
ALL_FILES = {R.FILE_JSON, R.FILE_PROFILES_CSV, R.FILE_SOURCES_CSV, R.FILE_REJECTED_CSV, R.FILE_XLSX, R.FILE_SUMMARY}


@pytest.fixture
def result(tmp_path):
    records = [
        page("https://acme.example/", social=[f"{IG}/nike"], links=[f"{IG}/Nike/", f"{IG}/p/CxYz/"]),
        page("https://acme.example/contact", links=[f"{IG}/nike", f"{IG}/adidas/"], stamp="2026-11-14T08:00:00+00:00"),
        page("https://shop.acme.example/", links=[f"{IG}/nike/"], stamp=""),
    ]
    del records[2]["url"]                                        # a record with no page URL and no time
    return discover_paths([write_json(tmp_path / "items.json", records)])


def read_csv(path):
    with open(path, encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


# --- shared record structure ------------------------------------------------------------
def test_profile_record_uses_the_shared_structure(result):
    record = R.profile_record(result.profiles[1])               # nike
    assert isinstance(record, Record)
    data = record.to_dict()
    assert (data["record_type"], data["module"], data["platform"], data["dedup_key"]) == (
        "discovered_profile", "social_discovery", "instagram", "instagram:profile:nike")
    assert data["fields"]["username"] == {
        "value": "nike", "status": "Available", "source": "https://acme.example/",
        "extracted_at": STAMP_UTC, "reason": ""}
    assert data["fields"]["profile_url"]["value"] == "https://www.instagram.com/nike/"
    assert data["fields"]["source_websites"]["value"] == ["https://acme.example"]
    assert data["fields"]["source_page_count"]["value"] == 2
    assert data["fields"]["first_source_page_url"]["value"] == "https://acme.example/"
    assert data["fields"]["first_discovered_at"]["value"] == STAMP_UTC
    assert data["fields"]["last_discovered_at"]["value"] == "2026-11-14T08:00:00Z"
    assert len(data["sources"]) == 3
    assert data["sources"][2] == {
        "source_website": None, "source_page_url": None, "discovered_at": None,
        "found_in": ["external_links", "links"], "link_as_found": f"{IG}/nike/",
        "input_file": result.inputs[0].path, "input_record": 3,
        "unavailable": {"source_page_url": "the page URL is missing in the crawl output",
                        "source_website": "the page URL is missing in the crawl output",
                        "discovered_at": "the crawl output has no crawl_timestamp for this page"}}


def test_profile_with_nothing_but_the_link(tmp_path):
    record = page("x", links=[f"{IG}/nike/"], stamp="")
    del record["url"]
    found = discover_paths([write_json(tmp_path / "items.json", [record])])
    fields = R.profile_record(found.profiles[0]).fields
    assert fields["username"].available and fields["username"].extracted_at == ""
    assert "record 1" in fields["username"].source              # where it came from is still stated
    for name in ("source_websites", "first_source_page_url", "first_discovered_at", "last_discovered_at"):
        assert fields[name].status is FieldStatus.NOT_AVAILABLE and fields[name].value is None
        assert fields[name].reason
    assert fields["source_page_count"].value == 0


def test_document(result):
    document = R.build_document(result, GENERATED)
    assert list(document) == ["schema_version", "module", "platform", "generated_at", "summary", "limitations",
                              "inputs", "profiles", "rejected_links", "problems"]
    assert document["generated_at"] == GENERATED and document["schema_version"] == 1
    assert [p["dedup_key"] for p in document["profiles"]] == ["instagram:profile:adidas", "instagram:profile:nike"]
    assert document["summary"]["profiles_found"] == 2
    assert document["rejected_links"] == [{
        "link": f"{IG}/p/CxYz/", "reason": "post_url", "reason_text": "Post URL, not a profile",
        "first_source_page_url": "https://acme.example/", "first_input_file": result.inputs[0].path,
        "page_count": 1}]
    assert any("never contacted" in text for text in document["limitations"])
    assert json.loads(json.dumps(document, allow_nan=False)) == document        # strict JSON


# --- flat tables ---------------------------------------------------------------------------
def test_tables(result):
    tables = R.build_tables(result)
    assert list(tables) == ["profiles", "sources", "rejected_links"]
    columns, rows = tables["profiles"]
    assert columns == R.PROFILE_COLUMNS and [r["username"] for r in rows] == ["adidas", "nike"]
    assert rows[1] == {
        "username": "nike", "profile_url": "https://www.instagram.com/nike/", "source_website_count": 1,
        "source_websites": ["https://acme.example"], "source_page_count": 2,
        "first_source_page_url": "https://acme.example/", "first_discovered_at": STAMP_UTC,
        "last_discovered_at": "2026-11-14T08:00:00Z", "notes": ""}

    columns, rows = tables["sources"]
    assert columns == R.SOURCE_COLUMNS and len(rows) == 4       # adidas x1, nike x3
    assert all(set(row) == set(columns) for row in rows)
    missing = rows[3]
    assert (missing["source_website"], missing["source_page_url"], missing["discovered_at"]) == (
        "Not available", "Not available", "Not available")
    assert missing["notes"] == ("source_page_url, source_website: the page URL is missing in the crawl output; "
                                "discovered_at: the crawl output has no crawl_timestamp for this page")
    assert rows[1]["notes"] == "" and rows[1]["found_in"] == ["social_profiles", "external_links", "links"]

    columns, rows = tables["rejected_links"]
    assert columns == R.REJECTED_COLUMNS and rows[0]["reason"] == "post_url"


def test_tables_for_an_empty_result(tmp_path):
    empty = discover_paths([write_json(tmp_path / "items.json", [page("https://acme.example/")])])
    tables = R.build_tables(empty)
    assert all(rows == [] for _, rows in tables.values())


# --- files ----------------------------------------------------------------------------------
def test_all_formats_are_written_into_a_new_folder(tmp_path, result):
    openpyxl = pytest.importorskip("openpyxl")
    pytest.importorskip("xlsxwriter")
    root = tmp_path / "outputs" / "social"
    output = R.write_outputs(result, root, now=NOW, generated_at=GENERATED)
    assert output.run_dir == root / "discovery_20261118-093015"
    assert {p.name for p in output.run_dir.iterdir()} == ALL_FILES == set(output.files)
    assert output.errors == {} and output.xlsx_truncated_cells == 0

    document = json.loads((output.run_dir / R.FILE_JSON).read_text(encoding="utf-8"))
    assert document == R.build_document(result, GENERATED)

    profiles = read_csv(output.run_dir / R.FILE_PROFILES_CSV)
    assert [(r["username"], r["profile_url"], r["source_page_count"]) for r in profiles] == [
        ("adidas", "https://www.instagram.com/adidas/", "1"), ("nike", "https://www.instagram.com/nike/", "2")]
    sources = read_csv(output.run_dir / R.FILE_SOURCES_CSV)
    assert len(sources) == 4 and sources[1]["found_in"] == "social_profiles | external_links | links"
    assert sources[3]["source_page_url"] == "Not available" and "missing" in sources[3]["notes"]
    assert read_csv(output.run_dir / R.FILE_REJECTED_CSV)[0]["link"] == f"{IG}/p/CxYz/"

    book = openpyxl.load_workbook(output.run_dir / R.FILE_XLSX)
    assert book.sheetnames == ["profiles", "sources", "rejected_links"]
    assert [c.value for c in book["profiles"][1]] == list(R.PROFILE_COLUMNS)
    assert book["profiles"]["A3"].value == "nike" and book["profiles"]["E3"].value == 2
    assert book["sources"].max_row == 5 and book["rejected_links"].max_row == 2

    summary = json.loads((output.run_dir / R.FILE_SUMMARY).read_text(encoding="utf-8"))
    assert summary["output_files"] == sorted(ALL_FILES) and summary["export_errors"] == {}
    assert summary["summary"] == result.summary() and summary["generated_at"] == GENERATED


@pytest.mark.parametrize("formats, expected", [
    ("csv", {R.FILE_PROFILES_CSV, R.FILE_SOURCES_CSV, R.FILE_REJECTED_CSV, R.FILE_SUMMARY}),
    ("json", {R.FILE_JSON, R.FILE_SUMMARY}),
    (["xlsx"], {R.FILE_XLSX, R.FILE_SUMMARY}),
    ("json,csv", {R.FILE_JSON, R.FILE_PROFILES_CSV, R.FILE_SOURCES_CSV, R.FILE_REJECTED_CSV, R.FILE_SUMMARY}),
])
def test_only_the_selected_formats_are_written(tmp_path, result, formats, expected):
    pytest.importorskip("xlsxwriter")
    output = R.write_outputs(result, tmp_path, formats, now=NOW)
    assert {p.name for p in output.run_dir.iterdir()} == expected


def test_unknown_format_writes_nothing(tmp_path, result):
    with pytest.raises(ValueError):
        R.write_outputs(result, tmp_path / "out", "pdf", now=NOW)
    assert not (tmp_path / "out").exists()


def test_earlier_results_are_never_overwritten(tmp_path, result):
    first = R.write_outputs(result, tmp_path, "json", now=NOW)
    marker = first.run_dir / R.FILE_JSON
    before = marker.read_bytes()
    second = R.write_outputs(result, tmp_path, "json", now=NOW)
    assert second.run_dir.name == "discovery_20261118-093015-2" and second.run_dir != first.run_dir
    assert marker.read_bytes() == before


def test_one_failing_format_does_not_stop_the_others(tmp_path, result, monkeypatch):
    def broken(path, sheets):
        raise ExportError("Excel export needs the xlsxwriter package, which is not installed.")
    monkeypatch.setattr(R, "write_xlsx", broken)
    output = R.write_outputs(result, tmp_path, now=NOW)
    assert set(output.files) == ALL_FILES - {R.FILE_XLSX}
    assert output.errors == {"xlsx": "Excel export needs the xlsxwriter package, which is not installed."}
    summary = json.loads((output.run_dir / R.FILE_SUMMARY).read_text(encoding="utf-8"))
    assert summary["export_errors"] == output.errors and R.FILE_XLSX not in summary["output_files"]
    assert (output.run_dir / R.FILE_PROFILES_CSV).is_file() and (output.run_dir / R.FILE_JSON).is_file()


def test_nothing_is_written_outside_the_new_folder(tmp_path, result):
    root = tmp_path / "root"
    output = R.write_outputs(result, root, "csv,json", now=NOW)
    everything = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.name != "items.json")
    assert everything == ["root", f"root/{output.run_dir.name}"] + sorted(
        f"root/{output.run_dir.name}/{name}" for name in output.files)


def test_spreadsheet_formulas_in_the_data_are_neutralised(tmp_path):
    """A hostile crawl output cannot plant a formula in the CSV."""
    record = page("https://acme.example/", links=[f"{IG}/nike/"])
    record["crawl_timestamp"] = "=HYPERLINK(\"http://evil.example\")"
    found = discover_paths([write_json(tmp_path / "=cmd.json", [record])])
    output = R.write_outputs(found, tmp_path / "out", "csv", now=NOW)
    for name in (R.FILE_PROFILES_CSV, R.FILE_SOURCES_CSV):
        with open(output.run_dir / name, encoding="utf-8", newline="") as handle:
            for row in csv.reader(handle):
                assert not any(cell.startswith(("=", "+", "-", "@")) for cell in row), row
    assert read_csv(output.run_dir / R.FILE_SOURCES_CSV)[0]["discovered_at"] == "Not available"
