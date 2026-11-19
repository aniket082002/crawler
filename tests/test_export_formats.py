"""EXPORT_FORMATS selection in MultiFormatExportPipeline, and consistency
of the crawl summary with the formats actually written.

Imports the pipeline (xlsxwriter) and the summary extension (scrapy), so
like test_middlewares.py this is meant to run inside the Linux Docker image.
"""
import json

import openpyxl
import pytest

from webcrawler.pipelines import (
    SUPPORTED_FORMATS,
    MultiFormatExportPipeline,
    parse_export_formats,
)

ITEMS = [
    {"url": "https://example.com/a", "status": 200, "title": "A",
     "emails": {"a@example.com"}, "depth": 0},
    {"url": "https://example.com/b", "status": 200, "title": "B",
     "emails": set(), "depth": 1},
]
FILES = {"csv": "items.csv", "json": "items.json", "xlsx": "items.xlsx"}


class DummySpider:
    pass


class FakeSettings(dict):
    def get(self, key, default=None):
        return super().get(key, default)


class FakeCrawler:
    def __init__(self, **settings):
        self.settings = FakeSettings(settings)


def run_pipeline(tmp_path, formats=None):
    if formats is None:
        pipeline = MultiFormatExportPipeline(output_dir=str(tmp_path))
    else:
        pipeline = MultiFormatExportPipeline(output_dir=str(tmp_path), formats=formats)
    spider = DummySpider()
    pipeline.open_spider(spider)
    for item in ITEMS:
        pipeline.process_item(dict(item), spider)
    pipeline.close_spider(spider)
    return spider


def assert_content(fmt, path):
    if fmt == "csv":
        text = path.read_text(encoding="utf-8")
        # original five columns first, richer extraction columns after them
        assert text.splitlines()[0].startswith("url,status,title,emails,depth,")
        assert "https://example.com/a" in text and "a@example.com" in text
    elif fmt == "json":
        data = json.loads(path.read_text(encoding="utf-8"))
        assert [d["url"] for d in data] == ["https://example.com/a", "https://example.com/b"]
    else:
        rows = list(openpyxl.load_workbook(path)["items"].iter_rows(values_only=True))
        assert rows[0][0] == "url"
        assert [r[0] for r in rows[1:]] == ["https://example.com/a", "https://example.com/b"]


@pytest.mark.parametrize("formats", [("csv",), ("json",), ("xlsx",),
                                     ("csv", "json", "xlsx"), ("csv", "xlsx")])
def test_only_selected_formats_written(tmp_path, formats):
    spider = run_pipeline(tmp_path, ",".join(formats))
    assert set(spider.output_paths) == set(formats)
    produced = {p.name for p in tmp_path.iterdir()}
    assert produced == {FILES[f] for f in formats}
    for fmt in formats:
        assert_content(fmt, tmp_path / FILES[fmt])


def test_default_still_writes_all_three(tmp_path):
    """Backward compatibility: no EXPORT_FORMATS -> identical to before."""
    spider = run_pipeline(tmp_path)
    assert set(spider.output_paths) == {"csv", "json", "xlsx"}
    for fmt in SUPPORTED_FORMATS:
        assert_content(fmt, tmp_path / FILES[fmt])


def test_from_crawler_reads_setting(tmp_path):
    p = MultiFormatExportPipeline.from_crawler(
        FakeCrawler(OUTPUT_DIR=str(tmp_path), EXPORT_FORMATS="json"))
    assert p.formats == ("json",)
    assert p.output_dir == str(tmp_path)


def test_from_crawler_default_when_setting_absent(tmp_path):
    p = MultiFormatExportPipeline.from_crawler(FakeCrawler(OUTPUT_DIR=str(tmp_path)))
    assert p.formats == ("csv", "json", "xlsx")


@pytest.mark.parametrize("value, expected", [
    (" CSV , Json ", ("csv", "json")),
    ("xlsx,csv", ("csv", "xlsx")),  # canonical order
    (["json"], ("json",)),
    ("csv,csv", ("csv",)),
    (None, ("csv", "json", "xlsx")),
])
def test_parse_export_formats(value, expected):
    assert parse_export_formats(value) == expected


@pytest.mark.parametrize("value", ["", " , ", "pdf", "csv,pdf", "excel"])
def test_parse_export_formats_rejects_bad_values(value):
    with pytest.raises(ValueError):
        parse_export_formats(value)


def test_launcher_formats_match_pipeline():
    import run_crawler
    used = {f for _, fmts in run_crawler.FORMAT_CHOICES.values() for f in fmts}
    assert used == set(SUPPORTED_FORMATS)
    assert set(run_crawler.FORMAT_FILES) == set(SUPPORTED_FORMATS)


@pytest.mark.parametrize("formats", [("csv",), ("json",), ("xlsx",), ("csv", "json", "xlsx")])
def test_summary_lists_only_written_formats(tmp_path, formats):
    """crawl_summary.json/.txt (existing extension, unchanged) must list
    exactly the files the pipeline produced for this run."""
    from webcrawler.extensions import CrawlSummaryExtension

    spider = run_pipeline(tmp_path, ",".join(formats))
    CrawlSummaryExtension(output_dir=str(tmp_path)).spider_closed(spider, "finished")

    summary = json.loads((tmp_path / "crawl_summary.json").read_text(encoding="utf-8"))
    assert set(summary["output_files"]) == set(formats)
    assert summary["close_reason"] == "finished"
    txt = (tmp_path / "crawl_summary.txt").read_text(encoding="utf-8")
    for fmt in SUPPORTED_FORMATS:
        assert (f"  - {fmt}: " in txt) == (fmt in formats)
