"""CSV, JSON and Excel writers, including the spreadsheet-safety guards."""
import csv
import json
import sys

import pytest

from webcrawler.social_common import exporters as E
from webcrawler.social_common.exporters import ExportError

COLUMNS = ("username", "count", "note")


# --- formats ------------------------------------------------------------------------
@pytest.mark.parametrize("value, expected", [
    (None, ("csv", "json", "xlsx")), ("csv", ("csv",)), ("CSV, json", ("csv", "json")),
    ("xlsx,csv", ("csv", "xlsx")), (["xlsx", "json"], ("json", "xlsx")), ("json,json", ("json",)),
    ("csv,json,xlsx", ("csv", "json", "xlsx")),
])
def test_parse_formats(value, expected):
    assert E.parse_formats(value) == expected


@pytest.mark.parametrize("value", ["", "  ", ",", "pdf", "csv,pdf", [], ["html"]])
def test_parse_formats_rejects_bad_selections(value):
    with pytest.raises(ValueError):
        E.parse_formats(value)


def test_formats_and_limits_match_the_website_crawler():
    pytest.importorskip("xlsxwriter")
    pipelines = pytest.importorskip("webcrawler.pipelines")
    assert E.SUPPORTED_FORMATS == pipelines.SUPPORTED_FORMATS
    assert E.EXCEL_MAX_CELL == pipelines.EXCEL_MAX_CELL
    for selection in (None, "csv", "json,xlsx", ["xlsx", "csv"]):
        assert E.parse_formats(selection) == pipelines.parse_export_formats(selection)


# --- cells ----------------------------------------------------------------------------
@pytest.mark.parametrize("value, expected", [
    (None, ""), ("", ""), ("nike", "nike"), (5, 5), (-5, -5), (2.5, 2.5), (True, True), (False, False),
    (["a", "b"], "a | b"), (("a", None, "", "c"), "a | c"), ([], ""), ([1, 2], "1 | 2"),
    ({"b": 1, "a": "x"}, '{"a": "x", "b": 1}'),
    (float("nan"), "nan"),
])
def test_flat_cell(value, expected):
    assert E.flat_cell(value) == expected


@pytest.mark.parametrize("text, expected", [
    ("a\x00b", "a\\u0000b"), ("bell\x07", "bell\\u0007"), ("del\x7f", "del\\u007f"), ("c1\x85", "c1\\u0085"),
    ("lone\ud83d", "lone\\ud83d"), ("non\ufffe", "non\\ufffe"),
    ("tab\tnew\nline\r", "tab\tnew\nline\r"),                    # ordinary whitespace is kept
    ("caf\u00e9 \U0001f45f \u200d", "caf\u00e9 \U0001f45f \u200d"),   # accents, emoji, joiners are kept
])
def test_unprintable_characters_are_made_visible(text, expected):
    assert E.printable_text(text) == expected
    assert E.flat_cell(text) == expected


@pytest.mark.parametrize("text", ["=1+1", "=HYPERLINK(\"http://evil.example\",\"x\")", "+1", "-1+2", "@SUM(A1)",
                                  "\tx", "\rx", "=cmd|' /C calc'!A0"])
def test_csv_formula_guard(text):
    assert E.csv_cell(text) == "'" + text


@pytest.mark.parametrize("value", ["safe=1", "a+b", "nike", "", "https://acme.example/-x", " =1", 5, -5, -2.5, True])
def test_csv_formula_guard_leaves_safe_cells_alone(value):
    assert E.csv_cell(value) == value


# --- CSV --------------------------------------------------------------------------------
def read_csv(path):
    with open(path, encoding="utf-8", newline="") as handle:
        return list(csv.reader(handle))


def test_write_csv_round_trip(tmp_path):
    path = tmp_path / "out.csv"
    rows = [{"username": "nike", "count": 3, "note": "caf\u00e9, with comma"},
            {"username": "opositive.io", "count": 0},                      # missing key -> empty cell
            {"username": "multi", "count": 1, "note": "line one\nline two"},
            {"username": "list", "count": 2, "note": ["a", "b"]}]
    assert E.write_csv(path, COLUMNS, rows) == 4
    assert read_csv(path) == [["username", "count", "note"], ["nike", "3", "caf\u00e9, with comma"],
                              ["opositive.io", "0", ""], ["multi", "1", "line one\nline two"],
                              ["list", "2", "a | b"]]
    raw = path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")          # same as the crawler: UTF-8 without a byte-order mark
    assert b"\r\n" in raw


def test_write_csv_neutralises_formulas_and_odd_characters(tmp_path):
    path = tmp_path / "out.csv"
    E.write_csv(path, COLUMNS, [{"username": "=1+1", "count": -5, "note": "x\x00y\ud83d"}])
    assert read_csv(path)[1] == ["'=1+1", "-5", "x\\u0000y\\ud83d"]


def test_write_csv_empty_table(tmp_path):
    path = tmp_path / "out.csv"
    assert E.write_csv(path, COLUMNS, []) == 0
    assert read_csv(path) == [["username", "count", "note"]]


@pytest.mark.parametrize("rows", [[{"username": "x", "surprise": 1}], [["not", "a", "mapping"]], ["text"]])
def test_write_csv_rejects_bad_rows(tmp_path, rows):
    with pytest.raises(ExportError):
        E.write_csv(tmp_path / "out.csv", COLUMNS, rows)


def test_write_csv_reports_unwritable_path(tmp_path):
    with pytest.raises(ExportError):
        E.write_csv(tmp_path / "missing-folder" / "out.csv", COLUMNS, [])


# --- JSON -------------------------------------------------------------------------------
def test_write_json_round_trip(tmp_path):
    path = tmp_path / "out.json"
    document = {"profiles": [{"username": "nike", "pages": 3, "ok": True, "none": None, "text": "caf\u00e9 \U0001f45f"}]}
    E.write_json(path, document)
    text = path.read_text(encoding="utf-8")
    assert json.loads(text) == document
    assert text.endswith("\n") and "caf\u00e9" in text      # readable, not \\u-escaped


def test_write_json_keeps_odd_characters_exactly(tmp_path):
    path = tmp_path / "out.json"
    document = {"a": "nul\x00", "b": "lone\ud83d", "c": "back\\slash\ud83d"}
    E.write_json(path, document)
    assert json.loads(path.read_text(encoding="utf-8")) == document


@pytest.mark.parametrize("document", [{"x": float("nan")}, {"x": float("inf")}, {"x": object()}, {1.5: {2, 3}}])
def test_write_json_is_strict(tmp_path, document):
    path = tmp_path / "out.json"
    with pytest.raises(ExportError):
        E.write_json(path, document)
    assert not path.exists()            # nothing half-written


def test_write_json_handles_absurd_nesting(tmp_path):
    nested = []
    for _ in range(5000):
        nested = [nested]
    with pytest.raises(ExportError):
        E.write_json(tmp_path / "out.json", nested)


def test_write_json_reports_unwritable_path(tmp_path):
    with pytest.raises(ExportError):
        E.write_json(tmp_path / "missing-folder" / "out.json", {})


# --- Excel -------------------------------------------------------------------------------
@pytest.fixture
def openpyxl():
    pytest.importorskip("xlsxwriter")
    return pytest.importorskip("openpyxl")


def test_write_xlsx_round_trip(tmp_path, openpyxl):
    path = tmp_path / "out.xlsx"
    stats = E.write_xlsx(path, [
        ("profiles", COLUMNS, [{"username": "nike", "count": 3, "note": "hello"},
                               {"username": "opositive.io", "count": 0, "note": None}]),
        ("sources", ("url", "ok"), [{"url": "https://acme.example/", "ok": True}]),
    ])
    assert stats == {"rows": {"profiles": 2, "sources": 1}, "truncated_cells": 0}
    book = openpyxl.load_workbook(path)
    assert book.sheetnames == ["profiles", "sources"]
    rows = [[c.value for c in row] for row in book["profiles"].iter_rows()]
    assert rows == [["username", "count", "note"], ["nike", 3, "hello"], ["opositive.io", 0, None]]
    assert book["sources"]["B2"].value is True


def test_write_xlsx_never_creates_formulas_or_hyperlinks(tmp_path, openpyxl):
    path = tmp_path / "out.xlsx"
    E.write_xlsx(path, [("s", ("a", "b", "c"), [{"a": "=1+1", "b": "https://evil.example/x", "c": "@SUM(A1)"}])])
    sheet = openpyxl.load_workbook(path)["s"]
    assert [sheet[ref].value for ref in ("A2", "B2", "C2")] == ["=1+1", "https://evil.example/x", "@SUM(A1)"]
    assert all(sheet[ref].data_type == "s" for ref in ("A2", "B2", "C2"))     # plain strings
    assert sheet["B2"].hyperlink is None


def test_write_xlsx_cuts_long_cells_and_counts_them(tmp_path, openpyxl):
    path = tmp_path / "out.xlsx"
    long_text = "x" * (E.EXCEL_MAX_CELL + 500)
    stats = E.write_xlsx(path, [("s", ("a", "b"), [{"a": long_text, "b": "short"}])])
    assert stats["truncated_cells"] == 1
    value = openpyxl.load_workbook(path)["s"]["A2"].value
    assert len(value) == E.EXCEL_MAX_CELL and value.endswith(E.EXCEL_TRUNCATION_MARKER)


def test_write_xlsx_makes_odd_characters_visible(tmp_path, openpyxl):
    path = tmp_path / "out.xlsx"
    E.write_xlsx(path, [("s", ("a",), [{"a": "x\x00y\ud83d"}])])
    assert openpyxl.load_workbook(path)["s"]["A2"].value == "x\\u0000y\\ud83d"


def test_write_xlsx_bad_row_leaves_no_file(tmp_path, openpyxl):
    path = tmp_path / "out.xlsx"
    with pytest.raises(ExportError):
        E.write_xlsx(path, [("s", ("a",), [{"a": 1}, {"zzz": 2}])])
    assert not path.exists()


def test_write_xlsx_row_limit(tmp_path, openpyxl, monkeypatch):
    monkeypatch.setattr(E, "EXCEL_MAX_ROWS", 3)
    path = tmp_path / "out.xlsx"
    with pytest.raises(ExportError, match="more rows than Excel allows"):
        E.write_xlsx(path, [("s", ("a",), [{"a": n} for n in range(5)])])
    assert not path.exists()


def test_write_xlsx_reports_unwritable_path(tmp_path, openpyxl):
    with pytest.raises(ExportError):
        E.write_xlsx(tmp_path / "missing-folder" / "out.xlsx", [("s", ("a",), [{"a": 1}])])


def test_write_xlsx_without_xlsxwriter(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "xlsxwriter", None)       # makes "import xlsxwriter" fail
    with pytest.raises(ExportError, match="xlsxwriter"):
        E.write_xlsx(tmp_path / "out.xlsx", [("s", ("a",), [{"a": 1}])])
    assert not (tmp_path / "out.xlsx").exists()
