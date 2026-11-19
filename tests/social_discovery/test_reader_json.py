"""Reading the crawler's items.json, including damaged and unexpected files."""
import json

import pytest

from tests.social_discovery.builders import IG, STAMP, legacy_page, page, read, write_json
from webcrawler.social_discovery.reader import MAX_PROBLEMS_PER_INPUT, Limits

NIKE = [page("https://acme.example/", social=[f"{IG}/nike"], links=[f"{IG}/nike/", "https://example.org/other"]),
        page("https://acme.example/contact", links=[f"{IG}/p/CxYz/", "https://acme.example/about"])]


def link_set(page_links):
    return set(page_links.links)


@pytest.mark.parametrize("layout", ["crawler", "pretty", "minified"])
def test_reads_every_layout(tmp_path, layout):
    pages, report = read(write_json(tmp_path / "items.json", NIKE, layout=layout))
    assert report.status == "ok" and report.problems == []
    assert report.page_records == 2 and report.records_skipped == 0 and report.has_link_fields
    assert [p.record_number for p in pages] == [1, 2]
    assert pages[0].page_url == "https://acme.example/" and pages[0].crawl_timestamp == STAMP
    # Only possible Instagram links are kept, each with the field it came from.
    assert link_set(pages[0]) == {("social_profiles", f"{IG}/nike"), ("external_links", f"{IG}/nike/"),
                                  ("links", f"{IG}/nike/")}
    assert link_set(pages[1]) == {("external_links", f"{IG}/p/CxYz/"), ("links", f"{IG}/p/CxYz/")}


def test_report_records_format_and_size(tmp_path):
    path = write_json(tmp_path / "items.json", NIKE)
    _, report = read(path)
    assert report.format == "json" and report.size_bytes == path.stat().st_size
    assert report.to_dict()["path"].endswith("items.json")


@pytest.mark.parametrize("options", [{"bom": True}, {"newline": "\r\n"}, {"bom": True, "newline": "\r\n"}])
def test_byte_order_mark_and_windows_line_endings(tmp_path, options):
    pages, report = read(write_json(tmp_path / "items.json", NIKE, **options))
    assert report.status == "ok" and len(pages) == 2


def test_cut_off_file_keeps_the_complete_records(tmp_path):
    """An interrupted crawl: the last record is cut and the closing ] is missing."""
    path = write_json(tmp_path / "items.json", NIKE + [page("https://acme.example/third", links=[f"{IG}/nike/"])],
                      closed=False)
    data = path.read_bytes()
    path.write_bytes(data[:-200])                       # cut into the last record
    pages, report = read(path)
    assert [p.page_url for p in pages] == ["https://acme.example/", "https://acme.example/contact"]
    assert report.status == "partial" and report.records_skipped == 1
    assert any("could not be parsed" in m for m in report.problems)
    assert any("closing ']'" in m for m in report.problems)


@pytest.mark.parametrize("position", [0, 1, 2])
def test_one_malformed_record_is_skipped(tmp_path, position):
    lines = [json.dumps(page(f"https://acme.example/{n}", links=[f"{IG}/nike/"])) for n in range(3)]
    lines[position] = '{"url": "https://acme.example/bad", "broken": }'
    path = tmp_path / "items.json"
    path.write_bytes(("[\n" + ",\n".join(lines) + "\n]\n").encode("utf-8"))
    pages, report = read(path)
    assert len(pages) == 2 and report.status == "partial" and report.records_skipped == 1
    assert f"line {position + 2}" in report.problems[0]
    assert [p.record_number for p in pages] == [1, 2]


def test_records_that_are_not_objects_are_skipped(tmp_path):
    path = tmp_path / "items.json"
    path.write_bytes(('[\n1,\n"text",\nnull,\n[1, 2],\n' + json.dumps(NIKE[0]) + "\n]\n").encode("utf-8"))
    pages, report = read(path)
    assert len(pages) == 1 and report.records_skipped == 4 and report.status == "partial"
    assert all("not a page record" in m for m in report.problems)


def test_unexpected_types_inside_a_record_are_ignored(tmp_path):
    records = [
        page("https://acme.example/a", details="not an object"),
        page("https://acme.example/b", details={"contacts": ["x"], "links": "nope"}),
        page("https://acme.example/c", details={"contacts": {"social_profiles": "x"}, "links": [f"{IG}/nike/"]}),
        page("https://acme.example/d", social_profiles=5, external_links={"a": 1}),
        page("https://acme.example/e", details={"links": [None, 5, {"text": "no url"}, {"url": 7},
                                                          {"url": f"{IG}/adidas/"}]}),
    ]
    pages, report = read(write_json(tmp_path / "items.json", records))
    assert len(pages) == 5 and report.status == "ok"            # nothing was lost, only noted
    assert len(report.problems) == 6
    assert ("links", f"{IG}/nike/") in pages[2].links           # a plain string in details.links is accepted
    assert pages[4].links == (("links", f"{IG}/adidas/"),)


def test_flat_fields_given_as_lists_are_accepted(tmp_path):
    records = [page("https://acme.example/", social_profiles=[f"{IG}/nike", 5, None], external_links=[f" {IG}/puma/ "],
                    details={})]
    pages, _ = read(write_json(tmp_path / "items.json", records))
    assert link_set(pages[0]) == {("social_profiles", f"{IG}/nike"), ("external_links", f"{IG}/puma/")}


def test_older_output_without_link_fields(tmp_path):
    pages, report = read(write_json(tmp_path / "items.json", [legacy_page("https://old.example/"),
                                                              legacy_page("https://old.example/a")]))
    assert len(pages) == 2 and all(p.links == () for p in pages)
    assert all(p.crawl_timestamp is None for p in pages)
    assert report.status == "ok" and not report.has_link_fields
    assert len(report.problems) == 1 and "older version of the crawler" in report.problems[0]


def test_missing_or_odd_url_and_timestamp(tmp_path):
    records = [page("x"), page("x"), page("x", crawl_timestamp=None), page("x", crawl_timestamp=7)]
    del records[0]["url"]
    records[1]["url"] = 12345
    pages, report = read(write_json(tmp_path / "items.json", records))
    assert [p.page_url for p in pages] == [None, None, "x", "x"]
    assert [p.crawl_timestamp for p in pages] == [STAMP, STAMP, None, None]
    assert report.status == "ok"


@pytest.mark.parametrize("content, message", [
    (b"", "empty"),
    (b"hello, this is not JSON", "not valid JSON"),
    (b'{"items": []}', "expected a JSON list"),
    (b'"just a string"', "expected a JSON list"),
    (b"[\n", "no page records"),
])
def test_files_that_are_not_crawler_output(tmp_path, content, message):
    path = tmp_path / "items.json"
    path.write_bytes(content)
    pages, report = read(path)
    assert pages == []
    assert any(message in m for m in report.problems)
    assert report.status == ("failed" if content != b"[\n" else "partial")


def test_empty_list_is_fine(tmp_path):
    pages, report = read(write_json(tmp_path / "items.json", []))
    assert pages == [] and report.status == "ok"
    assert report.problems == ["the file contains no page records"]


def test_first_record_cut_off(tmp_path):
    path = tmp_path / "items.json"
    path.write_bytes(b'[\n{"url": "https://acme.example/", "title": "cut he')
    pages, report = read(path)
    assert pages == [] and report.status == "failed"


def test_oversized_record_is_skipped_not_loaded(tmp_path):
    big = page("https://acme.example/big", links=[f"{IG}/nike/"], main_text="x" * 50_000)
    path = write_json(tmp_path / "items.json", [NIKE[0], big, NIKE[1]])
    pages, report = read(path, limits=Limits(max_record_bytes=10_000))
    assert [p.page_url for p in pages] == ["https://acme.example/", "https://acme.example/contact"]
    assert report.status == "partial" and report.records_skipped == 1
    assert "larger than the 10000 bytes record limit" in report.problems[0] and "line 3" in report.problems[0]


def test_file_over_the_input_limit_is_not_opened(tmp_path):
    pages, report = read(write_json(tmp_path / "items.json", NIKE), limits=Limits(max_input_bytes=100))
    assert pages == [] and report.status == "failed"
    assert "larger than the 100 bytes input limit" in report.problems[0]


def test_unstreamable_json_has_its_own_size_limit(tmp_path):
    path = write_json(tmp_path / "items.json", NIKE, layout="pretty")
    pages, report = read(path, limits=Limits(whole_file_json_bytes=100))
    assert pages == [] and report.status == "failed"
    assert "one-record-per-line layout" in report.problems[0]
    # the crawler's own layout is streamed, so the same limit does not apply to it
    pages, report = read(write_json(tmp_path / "items.json", NIKE), limits=Limits(whole_file_json_bytes=100))
    assert len(pages) == 2 and report.status == "ok"


@pytest.mark.parametrize("layout_first", [True, False])
def test_absurdly_nested_record_does_not_crash(tmp_path, layout_first):
    deep = '{"url": "https://acme.example/deep", "x": ' + "[" * 100_000 + "]" * 100_000 + "}"
    good = json.dumps(NIKE[0])
    lines = [deep, good] if layout_first else [good, deep]
    path = tmp_path / "items.json"
    path.write_bytes(("[\n" + ",\n".join(lines) + "\n]\n").encode("utf-8"))
    pages, report = read(path)
    assert [p.page_url for p in pages] == ["https://acme.example/"]
    assert report.status == "partial" and report.records_skipped == 1


def test_absurdly_nested_whole_file(tmp_path):
    path = tmp_path / "items.json"
    path.write_bytes(b"[" * 100_000 + b"]" * 100_000)
    pages, report = read(path)
    assert pages == [] and report.status == "failed" and "not valid JSON" in report.problems[0]


def test_invalid_utf8_bytes_are_tolerated(tmp_path):
    path = tmp_path / "items.json"
    record = json.dumps(page("https://acme.example/", links=[f"{IG}/nike/"])).encode("utf-8")
    record = record.replace(b"A page", b"A \xff\xfe page")
    path.write_bytes(b"[\n" + record + b"\n]\n")
    pages, report = read(path)
    assert len(pages) == 1 and report.status == "ok" and ("links", f"{IG}/nike/") in pages[0].links


def test_content_after_the_end_of_the_list(tmp_path):
    path = write_json(tmp_path / "items.json", NIKE)
    with open(path, "ab") as handle:
        handle.write(b'{"url": "https://acme.example/extra"}\nmore\n')
    pages, report = read(path)
    assert len(pages) == 2 and report.status == "partial"
    assert sum("after the end of the list" in m for m in report.problems) == 1


def test_missing_file_and_folder(tmp_path):
    pages, report = read(tmp_path / "nope.json")
    assert pages == [] and report.status == "failed" and "not found" in report.problems[0]
    (tmp_path / "folder.json").mkdir()
    pages, report = read(tmp_path / "folder.json")
    assert pages == [] and report.status == "failed" and "not a regular file" in report.problems[0]


def test_problem_list_is_capped(tmp_path):
    path = tmp_path / "items.json"
    path.write_bytes(("[\n" + ",\n".join(["1"] * 80) + "\n]\n").encode("utf-8"))
    pages, report = read(path)
    assert pages == [] and report.status == "failed"
    assert len(report.problems) == MAX_PROBLEMS_PER_INPUT
    assert report.problems_not_listed == 80 - MAX_PROBLEMS_PER_INPUT + 1
    assert report.to_dict()["problems_not_listed"] == report.problems_not_listed


def test_large_file_is_read_as_a_stream(tmp_path):
    """500 pages, each with a link: all found, nothing held back."""
    records = [page(f"https://acme.example/page-{n}", links=[f"{IG}/nike/"], main_text="word " * 2000)
               for n in range(500)]
    pages, report = read(write_json(tmp_path / "items.json", records))
    assert len(pages) == 500 and report.status == "ok"
    assert all(("links", f"{IG}/nike/") in p.links for p in pages)
