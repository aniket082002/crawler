"""Page extraction: text, headings, metadata, images, links, contacts,
structured data, JS detection and truncation. Offline; sample HTML only."""
import pytest

pytest.importorskip("lxml")

from tests import sample_pages as S  # noqa: E402
from webcrawler.extractors import (  # noqa: E402
    clean_email, extract_page, extract_phones, social_network,
)

BASE = "https://acme-widgets.co/"


@pytest.fixture(scope="module")
def home():
    return extract_page(BASE, S.HOME, status=200, depth=0, allowed_domains=["acme-widgets.co"])


# --- full text --------------------------------------------------------------
def test_main_text_contains_content(home):
    t = home["main_text"]
    assert "Precision widgets for modern manufacturers" in t
    assert "We help manufacturers and engineering teams reduce downtime" in t
    assert "Bespoke widget designs engineered to your specification." in t
    assert "Automotive" in t and "£99" in t
    assert home["word_count"] > 40
    assert home["content_scope"] == "main"
    assert home["text_truncated"] is False


def test_main_text_excludes_code_tracking_hidden_and_navigation(home):
    t = home["main_text"]
    for unwanted in ("font-family", ".hero", "dataLayer", "track@analytics-vendor.io",
                     "ld+json", "@context", "hidden-secret", "Blog"):
        assert unwanted not in t
    # navigation/footer are not main content (but are used for contacts below)
    assert "12 Gear Street" not in t


def test_main_text_keeps_block_line_breaks(home):
    lines = home["main_text"].split("\n")
    assert "Our Services" in lines
    assert "Custom Widget Design" in lines


def test_fallback_without_main_element_drops_nav():
    html = ("<html><body><nav><a href='/'>Home</a><a href='/x'>Menu item</a></nav>"
            "<div><h1>Title</h1><p>" + "Real content words here. " * 20 + "</p></div>"
            "<footer>Copyright footer</footer></body></html>")
    r = extract_page(BASE, html)
    assert r["content_scope"] == "body_without_navigation"
    assert "Real content words" in r["main_text"]
    assert "Menu item" not in r["main_text"] and "Copyright footer" not in r["main_text"]


# --- headings, paragraphs, sections, lists, tables ---------------------------
def test_headings_and_paragraphs(home):
    assert home["h1"] == "Precision widgets for modern manufacturers"
    assert [(h["level"], h["text"]) for h in home["headings"]] == [
        (1, "Precision widgets for modern manufacturers"), (2, "Our Services"),
        (3, "Custom Widget Design"), (3, "Widget Maintenance"),
        (2, "Industries we serve"), (2, "Pricing overview")]
    assert "Bespoke widget designs engineered to your specification." in home["paragraphs"]
    assert len(home["paragraphs"]) == len(set(home["paragraphs"]))


def test_sections_group_content_under_headings(home):
    by_heading = {s["heading"]: s for s in home["sections"]}
    assert by_heading["Custom Widget Design"]["level"] == 3
    assert by_heading["Custom Widget Design"]["paragraphs"] == [
        "Bespoke widget designs engineered to your specification."]
    assert by_heading["Industries we serve"]["items"] == ["Automotive", "Aerospace", "Food processing"]


def test_lists_and_tables(home):
    assert ["Automotive", "Aerospace", "Food processing"] in home["lists"]
    assert home["tables"] == [[["Plan", "Price"], ["Basic", "£99"]]]


# --- metadata -----------------------------------------------------------------
def test_metadata(home):
    assert home["title"] == "Acme Widgets | Industrial Widget Engineering"
    assert home["meta_description"].startswith("Acme Widgets designs")
    assert home["meta_keywords"] == "widgets, engineering, manufacturing"
    assert home["canonical_url"] == BASE
    assert home["language"] == "en-GB"
    assert home["status"] == 200 and home["depth"] == 0
    assert home["crawl_timestamp"].endswith("+00:00")
    assert home["requested_url"] == BASE


def test_requested_url_records_redirect_origin():
    r = extract_page("https://acme-widgets.co/new", S.ABOUT, requested_url="https://acme-widgets.co/old")
    assert r["url"] == "https://acme-widgets.co/new"
    assert r["requested_url"] == "https://acme-widgets.co/old"


# --- images -------------------------------------------------------------------
def test_images(home):
    assert home["images"] == [
        {"src": "https://acme-widgets.co/img/factory.jpg", "alt": "Our factory floor",
         "title": "Factory", "caption": "Our Manchester factory"},
        {"src": "https://acme-widgets.co/img/lazy.png", "alt": "Lazy widget", "title": "", "caption": ""},
    ]


def test_inline_data_images_are_not_dumped():
    r = extract_page(BASE, "<html><body><img src='data:image/png;base64,AAAA' alt='x'></body></html>")
    assert r["images"][0]["src"] == "data:(inline image)"


# --- links --------------------------------------------------------------------
def test_internal_and_external_links(home):
    links = {lk["url"]: lk for lk in home["links"]}
    assert links["https://acme-widgets.co/about"]["internal"] is True
    assert links["https://acme-widgets.co/about"]["text"] == "About us"
    partner = links["https://partner.example.org/page"]
    assert partner["internal"] is False and partner["nofollow"] is True
    assert partner["text"] == "Partner portal"
    assert not any(u.startswith(("mailto:", "tel:")) for u in links)
    assert home["internal_link_count"] == 5
    assert home["external_link_count"] == 4


def test_links_with_port_in_allowed_domain_are_internal():
    r = extract_page("http://127.0.0.1:8000/", "<html><body><a href='/a'>A</a></body></html>",
                     allowed_domains=["127.0.0.1:8000"])
    assert r["links"][0]["internal"] is True


# --- contacts -----------------------------------------------------------------
def test_emails_from_visible_text_and_mailto_only(home):
    assert home["emails"] == {"sales@acme-widgets.co"}


def test_email_false_positives_rejected(home):
    # form placeholder attribute, script contents, hidden element, example.com
    for bad in ("your@email.com", "track@analytics-vendor.io",
                "hidden-secret@acme-widgets.co", "someone@example.com"):
        assert bad not in home["emails"]


@pytest.mark.parametrize("raw, expected", [
    ("Hello@Acme-Widgets.co.", "hello@acme-widgets.co"),
    ("your@email.com", None), ("name@domain.com", None), ("logo@2x.png", None),
    ("info@example.org", None), ("a@b.invalid", None), ("team@acme.test", None),
])
def test_clean_email(raw, expected):
    assert clean_email(raw) == expected


def test_mailto_with_query_and_encoding():
    r = extract_page(BASE, "<html><body><a href='mailto:info%40acme-widgets.co?subject=x'>Mail</a></body></html>")
    assert r["emails"] == {"info@acme-widgets.co"}


def test_phones(home):
    assert home["phones"] == ["+44 20 7946 0958", "0161 496 0000"]  # tel: dup merged


@pytest.mark.parametrize("text, expected", [
    ("Call us on +91 98765 43210 today", ["+91 98765 43210"]),
    ("Tel: (020) 7946 0958", ["(020) 7946 0958"]),
    ("Projects from 2019 - 2024", []),
    ("Order number 1234567890", []),          # no phone context
    ("Updated 2024-01-15 and 12/05/2023", []),
    ("Server 192.168.100.200", []),
    ("Phone: 123", []),                        # too short
])
def test_phone_detection_is_conservative(text, expected):
    assert extract_phones(text, []) == expected


def test_addresses_deduplicated(home):
    assert home["addresses"] == ["12 Gear Street, Manchester, M1 2AB, GB"]


def test_social_profiles(home):
    assert home["social_profiles"] == [
        "https://twitter.com/acmewidgets", "https://www.instagram.com/acmewidgets",
        "https://www.linkedin.com/company/acme-widgets"]


@pytest.mark.parametrize("url, network", [
    ("https://www.linkedin.com/company/acme", "LinkedIn"),
    ("https://x.com/acme", "X/Twitter"),
    ("https://www.facebook.com/sharer/sharer.php?u=1", None),
    ("https://twitter.com/intent/tweet?text=hi", None),
    ("https://www.youtube.com/watch?v=abc", None),
    ("https://www.linkedin.com/", None),
    ("https://notlinkedin.com/acme", None),
])
def test_social_network_detection(url, network):
    assert social_network(url) == network


# --- structured data ----------------------------------------------------------
def test_json_ld_open_graph_twitter(home):
    sd = home["structured_data"]
    assert len(sd["json_ld"]) == 1 and sd["json_ld_errors"] == 1
    assert "json_ld_parse_errors:1" in home["content_warnings"]
    assert sd["open_graph"]["og:site_name"] == "Acme Widgets"
    assert sd["open_graph"]["og:type"] == "website"
    assert sd["twitter"]["twitter:card"] == "summary"
    assert home["jsonld_types"] == ["PostalAddress", "ProfessionalService", "Service"]


def test_organization_service_product_entities(home):
    org = home["organization"]
    assert org["name"] == "Acme Widgets Ltd"
    assert org["types"] == ["ProfessionalService"]
    assert org["email"] == "hello@acme-widgets.co"
    assert org["founding_date"] == "1999"
    assert org["address"] == "12 Gear Street, Manchester, M1 2AB, GB"
    assert home["services_structured"][0]["name"] == "Widget Prototyping"
    assert home["products_structured"] == []
    p = extract_page(BASE, """<html><head><script type="application/ld+json">
        {"@type": "SoftwareApplication", "name": "WidgetCAD", "description": "Design tool",
         "applicationCategory": "DesignApplication"}</script></head><body></body></html>""")
    assert p["products_structured"] == [{"name": "WidgetCAD", "description": "Design tool",
                                         "brand": "", "category": "DesignApplication",
                                         "type": "SoftwareApplication"}]


def test_microdata_types():
    r = extract_page(BASE, "<html><body><div itemscope itemtype='https://schema.org/Product'>x</div></body></html>")
    assert r["structured_data"]["microdata_types"] == ["https://schema.org/Product"]


# --- quality flags & robustness ----------------------------------------------
def test_js_rendered_page_flagged():
    r = extract_page(BASE + "app", S.JS_APP)
    assert r["js_rendering_suspected"] is True
    assert "js_rendering_suspected" in r["content_warnings"]


def test_normal_page_not_flagged_as_js(home):
    assert home["js_rendering_suspected"] is False


def test_truncation_is_recorded():
    html = "<html><body><main>" + "<p>" + "word " * 400 + "</p>" * 1 + "</main></body></html>"
    r = extract_page(BASE, html, max_text_chars=100)
    assert len(r["main_text"]) == 100
    assert r["text_truncated"] is True
    assert "text_truncated_at_100_chars" in r["content_warnings"]


def test_empty_and_garbage_input_does_not_crash():
    for html in ("", "   ", "not html at all", "<?xml version='1.0' encoding='latin-1'?><html><body><p>Hi</p></body></html>"):
        r = extract_page(BASE, html)
        assert isinstance(r["main_text"], str)
    assert "no_html_content" in extract_page(BASE, "")["content_warnings"]


def test_non_ascii_content():
    r = extract_page(BASE, "<html><body><main><h1>Café Zürich</h1><p>Prix: 10 €</p></main></body></html>")
    assert r["h1"] == "Café Zürich" and "10 €" in r["main_text"]


def test_deeply_nested_html_does_not_hit_recursion_limit():
    # deeper than libxml2's default 256 limit and Python's 1000 recursion limit
    html = "<html><body>" + "<div>" * 1500 + "<p>deep text</p>" + "</div>" * 1500 + "</body></html>"
    r = extract_page(BASE, html)
    assert "deep text" in r["main_text"]
