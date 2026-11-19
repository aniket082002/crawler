import json
from pathlib import Path

import openpyxl
import pytest

from webcrawler.pipelines import MultiFormatExportPipeline
from webcrawler.url_validator import safe_output_path


class DummySpider:
    pass


def test_incremental_export_all_formats(tmp_path):
    pipeline = MultiFormatExportPipeline(output_dir=str(tmp_path))
    spider = DummySpider()
    pipeline.open_spider(spider)

    items = [
        {
            "url": "https://example.com/a",
            "status": 200,
            "title": "A",
            "emails": {"a@example.com"},
            "depth": 0,
        },
        {
            "url": "https://example.com/b",
            "status": 200,
            "title": "B",
            "emails": set(),
            "depth": 1,
        },
    ]
    for item in items:
        pipeline.process_item(item, spider)

    pipeline.close_spider(spider)

    csv_path = Path(spider.output_paths["csv"])
    json_path = Path(spider.output_paths["json"])
    xlsx_path = Path(spider.output_paths["xlsx"])

    assert csv_path.exists()
    assert json_path.exists()
    assert xlsx_path.exists()

    csv_text = csv_path.read_text(encoding="utf-8")
    assert "https://example.com/a" in csv_text
    assert "https://example.com/b" in csv_text
    assert "a@example.com" in csv_text

    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert len(data) == 2
    assert data[0]["url"] == "https://example.com/a"
    assert data[1]["url"] == "https://example.com/b"

    wb = openpyxl.load_workbook(xlsx_path)
    ws = wb["items"]
    rows = list(ws.iter_rows(values_only=True))
    assert rows[0][0] == "url"
    assert rows[1][0] == "https://example.com/a"
    assert rows[2][0] == "https://example.com/b"


def test_output_path_traversal_rejected(tmp_path):
    with pytest.raises(ValueError):
        safe_output_path(str(tmp_path), "../escape.csv")


def test_output_path_stays_within_dir(tmp_path):
    path = safe_output_path(str(tmp_path), "items.csv")
    assert str(path).startswith(str(tmp_path.resolve()))
