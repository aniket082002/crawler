"""Incremental, streaming export to CSV, Excel (.xlsx) and JSON
(Requirement 4). Each writer flushes per item -- the full item set is
never accumulated in memory. Output paths are validated against the
project's output directory before any file is opened (Requirement 7).

Every page becomes one flat row with the columns in FIELDNAMES (the five
original columns first, unchanged). CSV and Excel hold exactly those
columns; each JSON record holds the same columns with the same values plus
a "details" object with the full nested extraction (paragraphs, sections,
lists, tables, images, links, structured data, contacts).
"""
import csv
import json

import xlsxwriter

from webcrawler.url_validator import safe_output_path

ORIGINAL_FIELDNAMES = ["url", "status", "title", "emails", "depth"]
FIELDNAMES = ORIGINAL_FIELDNAMES + [
    "requested_url", "crawl_timestamp", "language", "meta_description",
    "meta_keywords", "canonical_url", "h1", "headings", "word_count",
    "main_text", "text_truncated", "phones", "addresses", "social_profiles",
    "image_count", "images", "internal_link_count", "external_link_count",
    "external_links", "og_title", "og_description", "og_type", "og_site_name",
    "jsonld_types", "organization_name", "js_rendering_suspected",
    "content_warnings",
]
DETAIL_FIELDS = ["headings", "paragraphs", "sections", "lists", "tables", "images",
                 "links", "structured_data", "organization", "services_structured",
                 "products_structured", "content_scope"]

# Excel refuses cell strings longer than this; longer values are cut in the
# .xlsx only and the row's content_warnings says which column was cut.
EXCEL_MAX_CELL = 32_767
EXCEL_TRUNCATION_MARKER = " ...[truncated in Excel; full text in CSV/JSON]"

# Formats this pipeline can write. EXPORT_FORMATS (a Scrapy setting, e.g.
# `-s EXPORT_FORMATS=csv` or `-s EXPORT_FORMATS=csv,json`) selects a subset;
# when it is not set, all three are written, exactly as before.
SUPPORTED_FORMATS = ("csv", "json", "xlsx")


def parse_export_formats(value) -> tuple:
    """Normalize an EXPORT_FORMATS value (comma string or list) into an
    ordered tuple of supported formats. Raises ValueError on unknown or
    empty input so a typo never silently produces no output."""
    if value is None:
        return SUPPORTED_FORMATS
    if isinstance(value, str):
        parts = value.split(",")
    else:
        parts = list(value)
    requested = {str(p).strip().lower() for p in parts if str(p).strip()}
    if not requested:
        raise ValueError("EXPORT_FORMATS is empty; expected any of csv,json,xlsx")
    unknown = requested - set(SUPPORTED_FORMATS)
    if unknown:
        raise ValueError(
            f"Unsupported EXPORT_FORMATS: {', '.join(sorted(unknown))} "
            f"(supported: {', '.join(SUPPORTED_FORMATS)})"
        )
    return tuple(f for f in SUPPORTED_FORMATS if f in requested)


def _join(values) -> str:
    return " | ".join(str(v) for v in values if v not in (None, ""))


def flatten_record(item) -> dict:
    """One flat row per page. Missing fields (e.g. items from older code)
    become empty strings, so the original 5-field items still export."""
    g = item.get
    og = (g("structured_data") or {}).get("open_graph") or {}
    images = g("images")
    links = g("links") or []
    org = g("organization") or {}
    return {
        "url": g("url", ""),
        "status": g("status", ""),
        "title": g("title", ""),
        "emails": ", ".join(sorted(g("emails", []) or [])),
        "depth": g("depth", ""),
        "requested_url": g("requested_url", ""),
        "crawl_timestamp": g("crawl_timestamp", ""),
        "language": g("language", ""),
        "meta_description": g("meta_description", ""),
        "meta_keywords": g("meta_keywords", ""),
        "canonical_url": g("canonical_url", ""),
        "h1": g("h1", ""),
        "headings": _join(f"H{h['level']}: {h['text']}" for h in (g("headings") or [])),
        "word_count": g("word_count", ""),
        "main_text": g("main_text", ""),
        "text_truncated": g("text_truncated", ""),
        "phones": _join(g("phones") or []),
        "addresses": _join(g("addresses") or []),
        "social_profiles": _join(g("social_profiles") or []),
        "image_count": len(images) if images is not None else "",
        "images": _join(f"{i['src']} (alt: {i['alt']})" if i.get("alt") else i["src"]
                        for i in (images or [])),
        "internal_link_count": g("internal_link_count", ""),
        "external_link_count": g("external_link_count", ""),
        "external_links": _join(dict.fromkeys(lk["url"] for lk in links if not lk.get("internal"))),
        "og_title": og.get("og:title", ""),
        "og_description": og.get("og:description", ""),
        "og_type": og.get("og:type", ""),
        "og_site_name": og.get("og:site_name", ""),
        "jsonld_types": _join(g("jsonld_types") or []),
        "organization_name": org.get("name", "") if isinstance(org, dict) else "",
        "js_rendering_suspected": g("js_rendering_suspected", ""),
        "content_warnings": _join(g("content_warnings") or []),
    }


def json_record(item, row: dict) -> dict:
    details = {k: item.get(k) for k in DETAIL_FIELDS if item.get(k) is not None}
    details["contacts"] = {
        "emails": sorted(item.get("emails", []) or []),
        "phones": list(item.get("phones") or []),
        "addresses": list(item.get("addresses") or []),
        "social_profiles": list(item.get("social_profiles") or []),
    }
    return {**row, "details": details}


def excel_row(row: dict) -> dict:
    out, cut = dict(row), []
    for key, value in row.items():
        if isinstance(value, str) and len(value) > EXCEL_MAX_CELL:
            out[key] = value[:EXCEL_MAX_CELL - len(EXCEL_TRUNCATION_MARKER)] + EXCEL_TRUNCATION_MARKER
            cut.append(key)
    if cut:
        out["content_warnings"] = _join([row.get("content_warnings", "")] +
                                        [f"excel_cell_truncated:{c}" for c in cut])
    return out


class MultiFormatExportPipeline:
    def __init__(self, output_dir: str, formats=SUPPORTED_FORMATS):
        self.output_dir = output_dir
        self.formats = parse_export_formats(formats)
        self.csv_file = None
        self.csv_writer = None
        self.json_file = None
        self.json_first_item = True
        self.xlsx_workbook = None
        self.xlsx_worksheet = None
        self.xlsx_row = 0

    @classmethod
    def from_crawler(cls, crawler):
        output_dir = crawler.settings.get("OUTPUT_DIR", "outputs")
        formats = crawler.settings.get("EXPORT_FORMATS", None)
        return cls(output_dir=output_dir, formats=formats)

    def open_spider(self, spider):
        output_paths = {}

        if "csv" in self.formats:
            csv_path = safe_output_path(self.output_dir, "items.csv")
            self.csv_file = open(csv_path, "w", newline="", encoding="utf-8")
            self.csv_writer = csv.DictWriter(self.csv_file, fieldnames=FIELDNAMES)
            self.csv_writer.writeheader()
            self.csv_file.flush()
            output_paths["csv"] = str(csv_path)

        if "json" in self.formats:
            json_path = safe_output_path(self.output_dir, "items.json")
            self.json_file = open(json_path, "w", encoding="utf-8")
            self.json_file.write("[\n")
            self.json_first_item = True
            output_paths["json"] = str(json_path)

        if "xlsx" in self.formats:
            xlsx_path = safe_output_path(self.output_dir, "items.xlsx")
            # constant_memory streams rows straight to disk instead of buffering
            # the whole workbook in memory.
            self.xlsx_workbook = xlsxwriter.Workbook(
                str(xlsx_path), {"constant_memory": True}
            )
            self.xlsx_worksheet = self.xlsx_workbook.add_worksheet("items")
            for col, name in enumerate(FIELDNAMES):
                self.xlsx_worksheet.write(0, col, name)
            self.xlsx_row = 1
            output_paths["xlsx"] = str(xlsx_path)

        spider.output_paths = output_paths

    def process_item(self, item, spider):
        row = flatten_record(item)

        if self.csv_writer:
            self.csv_writer.writerow(row)
            self.csv_file.flush()

        if self.json_file:
            if not self.json_first_item:
                self.json_file.write(",\n")
            self.json_first_item = False
            json.dump(json_record(item, row), self.json_file, ensure_ascii=False, default=str)
            self.json_file.flush()

        if self.xlsx_worksheet:
            xl = excel_row(row)
            for col, name in enumerate(FIELDNAMES):
                value = xl[name]
                if name in ORIGINAL_FIELDNAMES:
                    self.xlsx_worksheet.write(self.xlsx_row, col, value)  # as before
                elif isinstance(value, str):
                    # write_string: never turned into formulas or hyperlinks
                    self.xlsx_worksheet.write_string(self.xlsx_row, col, value)
                else:
                    self.xlsx_worksheet.write(self.xlsx_row, col, value)
            self.xlsx_row += 1

        return item

    def close_spider(self, spider):
        if self.csv_file:
            self.csv_file.close()
        if self.json_file:
            self.json_file.write("\n]\n")
            self.json_file.close()
        if self.xlsx_workbook:
            self.xlsx_workbook.close()
