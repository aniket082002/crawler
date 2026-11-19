"""Rich exports: CSV/JSON/Excel field consistency, full nested JSON details,
Excel cell limits, backward compatibility, summary keys, profile written
regardless of EXPORT_FORMATS, and previous run folders left untouched."""
import csv
import hashlib
import json

import openpyxl
import pytest

pytest.importorskip("scrapy")

from tests import sample_pages as S  # noqa: E402
from webcrawler.extensions import CrawlSummaryExtension  # noqa: E402
from webcrawler.extractors import extract_page  # noqa: E402
from webcrawler.pipelines import (  # noqa: E402
    EXCEL_MAX_CELL, FIELDNAMES, ORIGINAL_FIELDNAMES, MultiFormatExportPipeline, flatten_record,
)
from webcrawler.profile import WebsiteProfilePipeline  # noqa: E402

B = "https://acme-widgets.co"


class Spider:
    start_urls = [B + "/"]


def pages():
    return [extract_page(B + p, html, depth=d, allowed_domains=["acme-widgets.co"])
            for p, html, d in [("/", S.HOME, 0), ("/about", S.ABOUT, 1), ("/contact", S.CONTACT, 1)]]


def run_pipelines(out_dir, formats="csv,json,xlsx", items=None):
    spider = Spider()
    export = MultiFormatExportPipeline(str(out_dir), formats=formats)
    profile = WebsiteProfilePipeline(str(out_dir))
    export.open_spider(spider)
    profile.open_spider(spider)
    for item in items if items is not None else pages():
        profile.process_item(export.process_item(item, spider), spider)
    export.close_spider(spider)
    profile.close_spider(spider)
    return spider


def read_all(out_dir):
    with open(out_dir / "items.csv", newline="", encoding="utf-8") as f:
        csv_rows = list(csv.DictReader(f))
    json_rows = json.loads((out_dir / "items.json").read_text(encoding="utf-8"))
    ws = openpyxl.load_workbook(out_dir / "items.xlsx")["items"]
    rows = list(ws.iter_rows(values_only=True))
    xl_rows = [dict(zip(rows[0], r)) for r in rows[1:]]
    return csv_rows, json_rows, list(rows[0]), xl_rows


def as_csv_text(v):
    return "" if v is None else str(v)


def test_columns_keep_original_five_first():
    assert FIELDNAMES[:5] == ORIGINAL_FIELDNAMES == ["url", "status", "title", "emails", "depth"]
    assert len(FIELDNAMES) == len(set(FIELDNAMES))


def test_csv_json_excel_field_consistency(tmp_path):
    run_pipelines(tmp_path)
    csv_rows, json_rows, xl_header, xl_rows = read_all(tmp_path)
    assert list(csv_rows[0].keys()) == FIELDNAMES
    assert xl_header == FIELDNAMES
    assert len(csv_rows) == len(json_rows) == len(xl_rows) == 3
    for c, j, x in zip(csv_rows, json_rows, xl_rows):
        for col in FIELDNAMES:
            assert as_csv_text(j[col]) == c[col], col           # JSON == CSV
            xv = x[col]
            if isinstance(xv, bool):
                xv = str(xv)
            assert as_csv_text(xv) == c[col] or (xv in (None, "") and c[col] == ""), col  # Excel == CSV


def test_exports_contain_real_content_not_just_urls(tmp_path):
    run_pipelines(tmp_path, formats="csv,json")
    csv_rows, json_rows = read_all_csv_json(tmp_path)
    home = csv_rows[0]
    assert "reduce downtime with reliable widgets" in home["main_text"]
    assert home["meta_description"].startswith("Acme Widgets designs")
    assert "H2: Our Services" in home["headings"]
    assert home["emails"] == "sales@acme-widgets.co"
    assert home["phones"] == "+44 20 7946 0958 | 0161 496 0000"
    assert home["organization_name"] == "Acme Widgets Ltd"
    assert home["og_site_name"] == "Acme Widgets"
    assert "https://acme-widgets.co/img/factory.jpg (alt: Our factory floor)" in home["images"]
    assert "https://partner.example.org/page" in home["external_links"]
    details = json_rows[0]["details"]
    assert details["sections"][1]["heading"] == "Our Services"
    assert details["tables"] == [[["Plan", "Price"], ["Basic", "£99"]]]
    assert details["images"][0]["caption"] == "Our Manchester factory"
    assert any(lk["text"] == "About us" and lk["internal"] for lk in details["links"])
    assert details["contacts"]["emails"] == ["sales@acme-widgets.co"]
    assert details["structured_data"]["open_graph"]["og:type"] == "website"
    assert details["organization"]["name"] == "Acme Widgets Ltd"


def read_all_csv_json(out_dir):
    with open(out_dir / "items.csv", newline="", encoding="utf-8") as f:
        csv_rows = list(csv.DictReader(f))
    return csv_rows, json.loads((out_dir / "items.json").read_text(encoding="utf-8"))


def test_excel_cell_limit_truncates_with_warning(tmp_path):
    item = extract_page(B + "/", "<html><body><main><p>" + "x" * 40_000 + "</p></main></body></html>")
    run_pipelines(tmp_path, items=[item])
    csv_rows, json_rows, _, xl_rows = read_all(tmp_path)
    assert len(csv_rows[0]["main_text"]) == 40_000              # CSV keeps full text
    assert len(json_rows[0]["main_text"]) == 40_000             # JSON keeps full text
    assert len(xl_rows[0]["main_text"]) <= EXCEL_MAX_CELL
    assert xl_rows[0]["main_text"].endswith("full text in CSV/JSON]")
    assert "excel_cell_truncated:main_text" in xl_rows[0]["content_warnings"]


def test_excel_does_not_turn_text_into_formulas(tmp_path):
    item = extract_page(B + "/", "<html><head><meta name='description' content='=SUM(A1:A9)'></head><body></body></html>")
    run_pipelines(tmp_path, formats="xlsx", items=[item])
    ws = openpyxl.load_workbook(tmp_path / "items.xlsx")["items"]
    col = FIELDNAMES.index("meta_description") + 1
    assert ws.cell(row=2, column=col).data_type == "s"


def test_original_minimal_items_still_export(tmp_path):
    """Items carrying only the original five fields (old code / tests)."""
    old = {"url": B + "/a", "status": 200, "title": "A", "emails": {"a@acme-widgets.co"}, "depth": 0}
    run_pipelines(tmp_path, items=[old])
    csv_rows, json_rows, _, xl_rows = read_all(tmp_path)
    assert csv_rows[0]["url"] == B + "/a" and csv_rows[0]["emails"] == "a@acme-widgets.co"
    assert csv_rows[0]["main_text"] == ""
    assert json_rows[0]["details"]["contacts"]["emails"] == ["a@acme-widgets.co"]
    assert xl_rows[0]["title"] == "A"


def test_flatten_handles_missing_fields():
    row = flatten_record({})
    assert set(row) == set(FIELDNAMES)
    assert all(v == "" for v in row.values())


@pytest.mark.parametrize("formats", ["csv", "json", "xlsx"])
def test_profile_generated_regardless_of_export_format(tmp_path, formats):
    spider = run_pipelines(tmp_path, formats=formats)
    names = {p.name for p in tmp_path.iterdir()}
    assert {"website_profile.md", "website_profile.json"} <= names
    assert names - {"website_profile.md", "website_profile.json"} == {
        {"csv": "items.csv", "json": "items.json", "xlsx": "items.xlsx"}[formats]}
    assert spider.profile_stats["pages_analyzed"] == 3


def test_summary_includes_new_counts_and_profile_files(tmp_path):
    spider = run_pipelines(tmp_path, formats="csv")
    CrawlSummaryExtension(output_dir=str(tmp_path)).spider_closed(spider, "finished")
    summary = json.loads((tmp_path / "crawl_summary.json").read_text(encoding="utf-8"))
    # original keys unchanged
    for key in ("max_unique_urls", "discovered", "crawled", "failed", "skipped",
                "unique_urls_registered", "emails_found", "close_reason", "output_files"):
        assert key in summary
    assert set(summary["output_files"]) == {"csv"}
    assert set(summary["profile_files"]) == {"website_profile_json", "website_profile_md"}
    assert summary["phones_found"] == 3
    assert summary["services_identified"] >= 2
    assert summary["internal_links_discovered"] >= 5
    txt = (tmp_path / "crawl_summary.txt").read_text(encoding="utf-8")
    assert "website profile md:" in txt and "Services identified:" in txt


def test_previous_run_folders_are_not_touched(tmp_path):
    old_run = tmp_path / "run_20260101-000000"
    old_run.mkdir()
    for name in ("items.csv", "items.json", "website_profile.md", "crawl_summary.json"):
        (old_run / name).write_text(f"old {name}", encoding="utf-8")
    (tmp_path / "items.csv").write_text("legacy", encoding="utf-8")
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp_path.rglob("*") if p.is_file()}

    new_run = tmp_path / "run_20260102-000000"
    spider = run_pipelines(new_run)
    CrawlSummaryExtension(output_dir=str(new_run)).spider_closed(spider, "finished")

    after = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in before}
    assert before == after
    assert (new_run / "website_profile.md").is_file()
