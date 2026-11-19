"""Website profile generation: facts vs heuristics vs inference, source
URL attribution, "Not found" handling, no invention. Offline."""
import json

import pytest

pytest.importorskip("lxml")

from tests import sample_pages as S  # noqa: E402
from webcrawler.extractors import extract_page  # noqa: E402
from webcrawler.profile import (  # noqa: E402
    EV_HEURISTIC, EV_INFERRED, EV_METADATA, EV_QUOTE, EV_STRUCTURED, NOT_FOUND,
    ProfileBuilder, WebsiteProfilePipeline, render_markdown, write_profile,
)

B = "https://acme-widgets.co"
SITE = [("/", S.HOME, 0), ("/about", S.ABOUT, 1), ("/services/design", S.SERVICE_DESIGN, 1),
        ("/services/news", S.SERVICES_URL_ONLY, 1), ("/contact", S.CONTACT, 1), ("/app", S.JS_APP, 1)]


def build(pages=SITE, coverage=None):
    b = ProfileBuilder(B + "/")
    for path, html, depth in pages:
        b.add_page(extract_page(B + path, html, depth=depth, allowed_domains=["acme-widgets.co"]))
    return b.build(coverage or {"url_limit": 15, "pages_crawled": len(pages), "pages_failed": 0,
                                "robots_txt_blocked": 0})


@pytest.fixture(scope="module")
def profile():
    return build()


def by_name(section):
    return {i["name"]: i for i in section["items"]}


# --- company identity ---------------------------------------------------------
def test_company_name_prefers_structured_data(profile):
    cn = profile["company_name"]
    assert cn["value"] == "Acme Widgets Ltd"
    assert cn["evidence"] == EV_STRUCTURED and cn["source_url"] == B + "/"
    assert [a["value"] for a in cn["alternatives"]] == ["Acme Widgets", "Acme Widgets"]


def test_company_name_fallback_og_site_name():
    page = "<html><head><title>Home</title><meta property='og:site_name' content='Gizmo Co'></head><body></body></html>"
    p = build([("/", page, 0)])
    assert p["company_name"]["value"] == "Gizmo Co"
    assert p["company_name"]["evidence"] == EV_METADATA


def test_company_name_fallback_shared_title_segment():
    pages = [("/", "<html><head><title>Welcome | Gizmo Co</title></head></html>", 0),
             ("/a", "<html><head><title>Pricing | Gizmo Co</title></head></html>", 1),
             ("/b", "<html><head><title>Team | Gizmo Co</title></head></html>", 1)]
    cn = build(pages)["company_name"]
    assert cn["value"] == "Gizmo Co" and cn["evidence"] == EV_HEURISTIC
    assert "3 of 3 page titles" in cn["note"]


def test_business_category(profile):
    bc = profile["business_category"]
    assert bc["value"] == "ProfessionalService" and bc["evidence"] == EV_STRUCTURED
    assert bc["homepage_self_description"]["text"] == "Acme Widgets | Industrial Widget Engineering"


def test_what_company_does_is_quoted_with_sources(profile):
    items = profile["what_the_company_does"]["items"]
    texts = {i["text"]: i for i in items}
    assert texts["Engineering firm specialising in custom industrial widgets."]["evidence"] == EV_STRUCTURED
    assert texts["Acme Widgets designs and manufactures precision widgets for manufacturers."]["evidence"] == EV_METADATA
    about = texts["Acme Widgets is a family-run engineering company based in Manchester, building widgets for over twenty years."]
    assert about["evidence"] == EV_QUOTE and about["source_url"] == B + "/about"
    # every statement is verbatim text from some crawled page / its metadata
    for i in items:
        assert i["source_url"].startswith(B)


# --- services / products -------------------------------------------------------
def test_services_identified_with_descriptions_and_sources(profile):
    s = by_name(profile["services"])
    assert set(s) == {"Widget Prototyping", "Custom Widget Design", "Widget Maintenance"}
    assert s["Widget Prototyping"]["evidence"] == [EV_STRUCTURED]
    assert s["Widget Prototyping"]["description"] == "Rapid prototypes of custom widgets in 10 days."
    design = s["Custom Widget Design"]
    assert design["description"] == "Bespoke widget designs engineered to your specification."
    assert design["source_urls"] == [B + "/", B + "/services/design"]
    assert any("Our Services" in n for n in design["notes"])
    assert s["Widget Maintenance"]["source_urls"] == [B + "/"]


def test_url_keyword_alone_never_creates_a_service(profile):
    names = " ".join(by_name(profile["services"]))
    assert "news" not in names.lower()
    for item in profile["services"]["items"]:
        assert B + "/services/news" not in item["source_urls"]


def test_service_page_without_listing_detected_from_h1():
    p = build([("/", "<html><head><title>X Co</title></head><body><main><h1>Hi</h1></main></body></html>", 0),
               ("/s", S.SERVICE_DESIGN, 1)])
    s = by_name(p["services"])
    assert list(s) == ["Custom Widget Design Services"]
    assert s["Custom Widget Design Services"]["description"] == "Bespoke widget design from concept to production drawings."
    assert s["Custom Widget Design Services"]["evidence"] == [EV_HEURISTIC]


def test_products_not_found_when_absent(profile):
    assert profile["products_and_solutions"] == {"status": NOT_FOUND, "items": []}


def test_products_from_listing_and_structured_data():
    page = """<html><head><title>Tools | Gizmo</title><script type="application/ld+json">
      {"@type": "Product", "name": "Gizmo One", "description": "Our first gizmo."}</script></head>
      <body><main><h1>Gizmo</h1><h2>Our Products</h2><ul><li>Gizmo Pro</li><li>Learn more</li></ul></main></body></html>"""
    prods = by_name(build([("/", page, 0)])["products_and_solutions"])
    assert set(prods) == {"Gizmo One", "Gizmo Pro"}  # CTA "Learn more" ignored
    assert prods["Gizmo One"]["evidence"] == [EV_STRUCTURED]
    assert prods["Gizmo Pro"]["description"] == NOT_FOUND


# --- audience / industries ---------------------------------------------------------
def test_audience_and_industries_are_quotes_or_listed_items(profile):
    aud = [i["text"] for i in profile["target_audience"]["items"]]
    assert "We help manufacturers and engineering teams reduce downtime with reliable widgets." in aud
    ind = [i["text"] for i in profile["industries_served"]["items"]]
    assert ind[:3] == ["Automotive", "Aerospace", "Food processing"]
    assert all(i["source_url"].startswith(B) for i in profile["industries_served"]["items"])


# --- contacts & social ------------------------------------------------------------
def test_contact_information_with_sources(profile):
    ci = profile["contact_information"]
    emails = {e["value"]: e for e in ci["emails"]}
    assert set(emails) == {"sales@acme-widgets.co", "support@acme-widgets.co"}
    assert emails["support@acme-widgets.co"]["source_urls"] == [B + "/contact"]
    assert "your@email.com" not in emails
    phones = {p["value"] for p in ci["phones"]}
    assert phones == {"+44 20 7946 0958", "0161 496 0000", "+44 161 496 0001"}
    assert ci["structured"]["email"] == "hello@acme-widgets.co"
    assert ci["structured"]["evidence"] == EV_STRUCTURED


def test_social_profiles(profile):
    nets = {s["network"] for s in profile["social_media_profiles"]["items"]}
    assert nets == {"X/Twitter", "Instagram", "LinkedIn"}


def test_important_pages(profile):
    cats = {p["url"]: p["categories"] for p in profile["important_pages"]["items"]}
    assert cats[B + "/"][0] == "Home"
    assert "About" in cats[B + "/about"]
    assert "Contact" in cats[B + "/contact"]
    assert "Services" in cats[B + "/services/design"]
    assert "Services" not in cats.get(B + "/services/news", [])


# --- summary, coverage, statistics --------------------------------------------------
def test_summary_is_labelled_inferred(profile):
    s = profile["overall_summary"]
    assert s["evidence"] == EV_INFERRED
    assert "Acme Widgets Ltd" in s["text"] and "Widget Prototyping" in s["text"]
    assert "Based on 6 crawled page(s)" in s["text"]
    assert '..' not in s["text"]


def test_coverage_reports_limits_and_js_pages(profile):
    cov = profile["coverage"]
    assert cov["url_limit"] == 15
    assert cov["js_rendering_suspected_pages"] == [B + "/app"]
    text = " ".join(cov["limitations"])
    assert "does not execute JavaScript" in text and "Only the 6 page(s)" in text


def test_statistics(profile):
    st = profile["statistics"]
    assert st["pages_analyzed"] == 6
    assert st["emails_found"] == 2 and st["phones_found"] == 3
    assert st["services_identified"] == 3 and st["products_identified"] == 0
    assert st["internal_links_discovered"] >= 5
    assert st["js_rendering_suspected_pages"] == 1


# --- missing information / no invention ------------------------------------------
def test_page_without_business_information_reports_not_found():
    page = "<html><head><title>Hello</title></head><body><main><p>Just a short note.</p></main></body></html>"
    p = build([("/", page, 0)])
    assert p["company_name"]["value"] == NOT_FOUND
    assert p["business_category"]["value"] == NOT_FOUND
    for key in ("services", "products_and_solutions", "target_audience", "industries_served",
                "social_media_profiles"):
        assert p[key]["status"] == NOT_FOUND and p[key]["items"] == []
    assert p["contact_information"]["status"] == NOT_FOUND
    md = render_markdown(p)
    assert md.count(NOT_FOUND) >= 7
    assert "company name not found" in p["overall_summary"]["text"]


def test_no_pages_at_all():
    p = ProfileBuilder("https://x.co/").build({})
    assert p["statistics"]["pages_analyzed"] == 0
    assert "No pages were crawled" in p["overall_summary"]["text"]
    render_markdown(p)


def test_every_finding_has_a_source(profile):
    for key in ("company_overview", "what_the_company_does", "target_audience", "industries_served"):
        for item in profile[key]["items"]:
            assert item["source_url"], (key, item)
    for key in ("services", "products_and_solutions"):
        for item in profile[key]["items"]:
            assert item["source_urls"], (key, item)


# --- markdown & files ---------------------------------------------------------------
def test_markdown_has_all_sections(profile):
    md = render_markdown(profile)
    for n, title in enumerate(["Company overview", "Company name", "Business category",
                               "What the company does", "Main services", "Products and solutions",
                               "Target customers or audience", "Industries served", "Contact information",
                               "Social media profiles", "Important website pages",
                               "Overall business summary"], start=1):
        assert f"## {n}. {title}" in md
    assert "## Coverage and limitations" in md
    assert "[source](https://acme-widgets.co/about)" in md
    assert "| Widget Prototyping |" in md


def test_write_profile_files(tmp_path, profile):
    paths = write_profile(profile, tmp_path)
    data = json.loads((tmp_path / "website_profile.json").read_text(encoding="utf-8"))
    assert data["company_name"]["value"] == "Acme Widgets Ltd"
    assert (tmp_path / "website_profile.md").read_text(encoding="utf-8").startswith("# Website profile")
    assert set(paths) == {"website_profile_json", "website_profile_md"}


def test_pipeline_writes_profile_and_exposes_stats(tmp_path):
    class Spider:
        start_urls = [B + "/"]

    pipe = WebsiteProfilePipeline(str(tmp_path))
    spider = Spider()
    pipe.open_spider(spider)
    for path, html, depth in SITE[:2]:
        item = extract_page(B + path, html, depth=depth, allowed_domains=["acme-widgets.co"])
        assert pipe.process_item(item, spider) is item
    pipe.close_spider(spider)
    assert (tmp_path / "website_profile.md").is_file()
    assert spider.profile_stats["pages_analyzed"] == 2
    assert set(spider.profile_paths) == {"website_profile_json", "website_profile_md"}


def test_pipeline_survives_bad_items(tmp_path):
    class Spider:
        start_urls = [B + "/"]

    pipe = WebsiteProfilePipeline(str(tmp_path))
    spider = Spider()
    pipe.open_spider(spider)
    pipe.process_item({"url": B + "/", "sections": "not-a-list"}, spider)
    pipe.close_spider(spider)
    assert (tmp_path / "website_profile.json").is_file()
