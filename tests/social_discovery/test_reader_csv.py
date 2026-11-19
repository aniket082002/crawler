"""Reading the crawler's items.csv, including damaged and unexpected files."""
import csv

import pytest

from tests.social_discovery.builders import (
    CRAWLER_COLUMNS, IG, LEGACY_COLUMNS, STAMP, legacy_page, page, read, write_csv,
)
from webcrawler.social_discovery.reader import Limits

NIKE = [page("https://acme.example/", social=[f"{IG}/nike"], links=[f"{IG}/nike/", "https://example.org/other"]),
        page("https://acme.example/contact", links=[f"{IG}/p/CxYz/", f"{IG}/adidas"])]


def test_reads_crawler_csv(tmp_path):
    pages, report = read(write_csv(tmp_path / "items.csv", NIKE))
    assert report.status == "ok" and report.problems == [] and report.format == "csv"
    assert report.page_records == 2 and report.has_link_fields
    assert [p.record_number for p in pages] == [1, 2]
    assert pages[0].page_url == "https://acme.example/" and pages[0].crawl_timestamp == STAMP
    assert pages[0].links == (("social_profiles", f"{IG}/nike"), ("external_links", f"{IG}/nike/"))
    assert pages[1].links == (("external_links", f"{IG}/p/CxYz/"), ("external_links", f"{IG}/adidas"))


def test_older_five_column_output(tmp_path):
    path = write_csv(tmp_path / "items.csv", [legacy_page("https://old.example/"), legacy_page("https://old.example/a")],
                     columns=LEGACY_COLUMNS)
    pages, report = read(path)
    assert [p.page_url for p in pages] == ["https://old.example/", "https://old.example/a"]
    assert all(p.links == () and p.crawl_timestamp is None for p in pages)
    assert report.status == "ok" and not report.has_link_fields
    assert len(report.problems) == 1 and "older version of the crawler" in report.problems[0]


def test_csv_without_a_url_column_is_not_crawler_output(tmp_path):
    path = tmp_path / "items.csv"
    path.write_bytes(b"name,amount\r\nwidget,5\r\n")
    pages, report = read(path)
    assert pages == [] and report.status == "failed" and "no 'url' column" in report.problems[0]


def test_cells_larger_than_pythons_default_limit(tmp_path):
    """The crawler can write 200,000 characters of page text into one cell;
    Python's CSV reader refuses anything over 131,072 unless told otherwise."""
    before = csv.field_size_limit()
    records = [page("https://acme.example/long", links=[f"{IG}/nike/"], main_text="word " * 50_000)]
    pages, report = read(write_csv(tmp_path / "items.csv", records))
    assert len(pages) == 1 and report.status == "ok"
    assert pages[0].links == (("external_links", f"{IG}/nike/"),)
    assert csv.field_size_limit() == before             # the process-wide limit was put back


def test_field_limit_is_restored_when_the_file_is_rejected(tmp_path):
    before = csv.field_size_limit()
    path = tmp_path / "items.csv"
    path.write_bytes(b"name\r\nx\r\n")
    read(path)
    assert csv.field_size_limit() == before


@pytest.mark.parametrize("delimiter", [";", "\t"])
def test_file_resaved_with_another_separator(tmp_path, delimiter):
    pages, report = read(write_csv(tmp_path / "items.csv", NIKE, delimiter=delimiter))
    assert report.status == "ok" and len(pages) == 2
    assert ("external_links", f"{IG}/adidas") in pages[1].links


def test_byte_order_mark(tmp_path):
    pages, report = read(write_csv(tmp_path / "items.csv", NIKE, bom=True))
    assert report.status == "ok" and pages[0].page_url == "https://acme.example/"


def test_quoted_cells_with_commas_quotes_and_line_breaks(tmp_path):
    records = [page("https://acme.example/a", links=[f"{IG}/nike/"],
                    main_text='Line one, with a comma\nLine "two"\r\nLine three', title="Title, with comma"),
               page("https://acme.example/b", links=[f"{IG}/puma/"])]
    pages, report = read(write_csv(tmp_path / "items.csv", records))
    assert [p.page_url for p in pages] == ["https://acme.example/a", "https://acme.example/b"]
    assert report.status == "ok" and report.page_records == 2


def test_header_names_are_matched_loosely(tmp_path):
    path = tmp_path / "items.csv"
    path.write_bytes((" URL ,Crawl_Timestamp,SOCIAL_PROFILES\r\n"
                      f"https://acme.example/,{STAMP},{IG}/nike\r\n").encode("utf-8"))
    pages, report = read(path)
    assert report.status == "ok" and pages[0].crawl_timestamp == STAMP
    assert pages[0].links == (("social_profiles", f"{IG}/nike"),)


def test_duplicate_header_names_use_the_first(tmp_path):
    path = tmp_path / "items.csv"
    path.write_bytes(f"url,social_profiles,social_profiles\r\nhttps://acme.example/,{IG}/first,{IG}/second\r\n"
                     .encode("utf-8"))
    pages, _ = read(path)
    assert pages[0].links == (("social_profiles", f"{IG}/first"),)


def test_short_rows_have_missing_values(tmp_path):
    path = tmp_path / "items.csv"
    path.write_bytes(("url,crawl_timestamp,social_profiles\r\n"
                      "https://acme.example/only-url\r\n"
                      f"https://acme.example/full,{STAMP},{IG}/nike\r\n").encode("utf-8"))
    pages, report = read(path)
    assert pages[0].page_url == "https://acme.example/only-url"
    assert pages[0].crawl_timestamp is None and pages[0].links == ()
    assert pages[1].links == (("social_profiles", f"{IG}/nike"),) and report.status == "ok"


def test_rows_with_extra_columns_are_flagged(tmp_path):
    path = tmp_path / "items.csv"
    path.write_bytes(f"url,social_profiles\r\nhttps://acme.example/,{IG}/nike,unexpected,extra\r\n".encode("utf-8"))
    pages, report = read(path)
    assert len(pages) == 1 and pages[0].links == (("social_profiles", f"{IG}/nike"),)
    assert "more columns than the header" in report.problems[0]


def test_blank_rows_and_blank_cells(tmp_path):
    path = tmp_path / "items.csv"
    path.write_bytes(("url,crawl_timestamp,social_profiles\r\n"
                      "\r\n"
                      " , , \r\n"
                      f" ,  ,{IG}/nike\r\n"
                      f"https://acme.example/,{STAMP},\r\n").encode("utf-8"))
    pages, report = read(path)
    assert len(pages) == 2 and report.page_records == 2
    assert pages[0].page_url is None and pages[0].crawl_timestamp is None
    assert pages[0].links == (("social_profiles", f"{IG}/nike"),)
    assert pages[1].links == ()


def test_joined_cells_are_split_on_the_crawlers_separator(tmp_path):
    path = tmp_path / "items.csv"
    path.write_bytes(("url,social_profiles,external_links\r\n"
                      f"https://acme.example/,{IG}/nike | https://twitter.com/acme | {IG}/adidas,"
                      f"https://example.org/ | {IG}/p/CxYz/\r\n").encode("utf-8"))
    pages, _ = read(path)
    assert pages[0].links == (("social_profiles", f"{IG}/nike"), ("social_profiles", f"{IG}/adidas"),
                              ("external_links", f"{IG}/p/CxYz/"))


def test_a_cell_over_the_limit_stops_reading_but_keeps_earlier_rows(tmp_path):
    records = [page("https://acme.example/a", links=[f"{IG}/nike/"]),
               page("https://acme.example/b", links=[f"{IG}/puma/"], main_text="x" * 5000),
               page("https://acme.example/c", links=[f"{IG}/adidas/"])]
    pages, report = read(write_csv(tmp_path / "items.csv", records), limits=Limits(csv_field_chars=1000))
    assert [p.page_url for p in pages] == ["https://acme.example/a"]
    assert report.status == "partial"
    assert "could not be read past this point" in report.problems[0]


@pytest.mark.parametrize("content, status, message", [
    (b"", "failed", "empty"),
    (b"\r\n\r\n", "failed", "empty"),
    (b"url,social_profiles\r\n", "ok", "no page records"),
])
def test_empty_files(tmp_path, content, status, message):
    path = tmp_path / "items.csv"
    path.write_bytes(content)
    pages, report = read(path)
    assert pages == [] and report.status == status and message in report.problems[0]


def test_invalid_bytes_and_nul_characters_do_not_crash(tmp_path):
    path = tmp_path / "items.csv"
    path.write_bytes(b"url,social_profiles\r\nhttps://acme.example/\xff\xfe,https://www.instagram.com/nike\r\n"
                     b"https://acme.example/nul\x00,https://www.instagram.com/puma\r\n")
    pages, report = read(path)
    assert report.status in ("ok", "partial")
    assert pages and pages[0].links == (("social_profiles", f"{IG}/nike"),)


def test_file_over_the_input_limit_is_not_opened(tmp_path):
    pages, report = read(write_csv(tmp_path / "items.csv", NIKE), limits=Limits(max_input_bytes=50))
    assert pages == [] and report.status == "failed" and "larger than the 50 bytes input limit" in report.problems[0]


def test_many_rows(tmp_path):
    records = [page(f"https://acme.example/page-{n}", links=[f"{IG}/nike/"]) for n in range(1000)]
    pages, report = read(write_csv(tmp_path / "items.csv", records))
    assert len(pages) == 1000 and report.status == "ok" and pages[-1].record_number == 1000


def test_builder_columns_are_the_crawlers_columns():
    pytest.importorskip("xlsxwriter")
    pipelines = pytest.importorskip("webcrawler.pipelines")
    assert CRAWLER_COLUMNS == pipelines.FIELDNAMES
    assert LEGACY_COLUMNS == pipelines.ORIGINAL_FIELDNAMES
