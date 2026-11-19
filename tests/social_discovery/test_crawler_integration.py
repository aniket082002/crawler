"""Module C against files written by the REAL website crawler code.

The other tests use hand-built files. Here the crawler's own extractor and
export pipeline produce items.json and items.csv from HTML (no crawl, no
network: the HTML is in this file), and Module C reads those. If the
crawler's output format ever changes, these tests fail.
"""
import json

import pytest

pytest.importorskip("lxml")
pytest.importorskip("xlsxwriter")

from tests.social_discovery.builders import CRAWLER_COLUMNS, read, write_json  # noqa: E402
from webcrawler.extractors import extract_page  # noqa: E402
from webcrawler.pipelines import MultiFormatExportPipeline  # noqa: E402
from webcrawler.social_discovery.discover import discover_paths  # noqa: E402

HOME = """<html><head><title>Acme Widgets</title>
<script type="application/ld+json">
{"@context": "https://schema.org", "@type": "Organization", "name": "Acme Widgets",
 "url": "https://acme.example/", "sameAs": ["https://www.instagram.com/AcmeOfficial",
                                             "https://www.linkedin.com/company/acme"]}
</script></head>
<body><nav><a href="/">Home</a> <a href="/contact">Contact</a></nav>
<main><h1>Acme Widgets</h1><p>We make dependable widgets for workshops of every size.</p></main>
<footer>
  <a href="https://www.instagram.com/Acme.Widgets/?hl=en">Instagram</a>
  <a href="https://www.instagram.com/p/CxYz123/">Our latest post</a>
  <a href="https://www.instagram.com/reel/Cabc999/">Watch the reel</a>
  <a href="https://www.instagram.com/explore/tags/widgets/">#widgets</a>
  <a href="https://twitter.com/acme">X</a>
  <a href="mailto:hello@acme.example">Email</a>
</footer></body></html>"""

CONTACT = """<html><head><title>Contact - Acme Widgets</title></head>
<body><main><h1>Contact</h1><p>Write to us or follow our accounts for workshop news.</p>
<p><a href="http://instagram.com/acme.widgets">@acme.widgets</a>
   <a href="https://www.instagram.com/stories/acme.widgets/31415/">Today's story</a></p></main></body></html>"""


class Spider:
    name = "general"
    output_paths: dict = {}


@pytest.fixture(scope="module")
def crawl_folder(tmp_path_factory):
    folder = tmp_path_factory.mktemp("run_real")
    pipeline = MultiFormatExportPipeline(output_dir=str(folder), formats="csv,json")
    spider = Spider()
    pipeline.open_spider(spider)
    for url, html in (("https://acme.example/", HOME), ("https://acme.example/contact", CONTACT)):
        pipeline.process_item(extract_page(url, html, status=200, depth=0,
                                           allowed_domains=["acme.example"]), spider)
    pipeline.close_spider(spider)
    return folder


def test_the_real_json_layout_is_one_record_per_line(crawl_folder):
    # On Windows the crawler writes \r\n line endings, in its Linux container \n; both are read.
    lines = [line.rstrip(b"\r") for line in (crawl_folder / "items.json").read_bytes().split(b"\n")]
    assert lines[0] == b"[" and lines[-2] == b"]" and lines[-1] == b""
    assert all(line.rstrip(b",").startswith(b"{") and line.rstrip(b",").endswith(b"}") for line in lines[1:-2])


def test_the_real_csv_header_matches_the_builders(crawl_folder):
    header = (crawl_folder / "items.csv").read_text(encoding="utf-8").splitlines()[0]
    assert header.split(",") == CRAWLER_COLUMNS


def test_reader_understands_the_real_files(crawl_folder):
    for name in ("items.json", "items.csv"):
        pages, report = read(crawl_folder / name)
        assert report.status == "ok" and report.problems == [] and report.has_link_fields
        assert [p.page_url for p in pages] == ["https://acme.example/", "https://acme.example/contact"]
        assert all(p.crawl_timestamp and p.crawl_timestamp.endswith("+00:00") for p in pages)


@pytest.mark.parametrize("input_format", ["json", "csv"])
def test_discovery_on_real_crawler_output(crawl_folder, input_format):
    result = discover_paths([crawl_folder], input_format=input_format)
    assert [p.username for p in result.profiles] == ["acme.widgets", "acmeofficial"]

    widgets, official = result.profiles
    assert widgets.profile_url == "https://www.instagram.com/acme.widgets/"
    assert widgets.page_urls() == ["https://acme.example/", "https://acme.example/contact"]
    assert widgets.websites() == ["https://acme.example"]
    assert all(source.discovered_at and source.discovered_at.endswith("Z") for source in widgets.sources)
    # only published in the page's structured data (schema.org sameAs), not as a visible link
    assert official.page_urls() == ["https://acme.example/"]
    assert official.sources[0].found_in == ["social_profiles"]

    assert result.rejected_by_reason() == {"explore_url": 1, "post_url": 1, "reel_url": 1, "story_url": 1}
    assert result.problems() == [] and result.summary()["page_records_read"] == 2


def test_json_keeps_the_pages_own_spelling_of_the_link(crawl_folder):
    widgets = discover_paths([crawl_folder], input_format="json").profiles[0]
    assert widgets.sources[0].link_as_found == "https://www.instagram.com/Acme.Widgets/?hl=en"
    assert widgets.sources[1].link_as_found == "http://instagram.com/acme.widgets"
    assert widgets.sources[0].found_in == ["social_profiles", "external_links", "links"]


def test_real_record_survives_a_round_trip_through_other_layouts(crawl_folder, tmp_path):
    records = json.loads((crawl_folder / "items.json").read_text(encoding="utf-8"))
    for layout in ("pretty", "minified"):
        path = write_json(tmp_path / f"{layout}.json", records, layout=layout)
        assert [p.username for p in discover_paths([path]).profiles] == ["acme.widgets", "acmeofficial"]
