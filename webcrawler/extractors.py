"""Page content, metadata, contact and structured-data extraction.

Pure functions over a page's HTML (lxml only -- no network, no scrapy
import), so everything here can be unit-tested offline with sample pages.

What is extracted is only what the page itself publicly serves in its HTML.
Scrapy does not execute JavaScript: content that a page renders client-side
is not visible here, and such pages are flagged `js_rendering_suspected`.
"""
from __future__ import annotations

import copy
import json
import re
from datetime import datetime, timezone
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from lxml import etree
from lxml import html as lxml_html

from webcrawler.url_validator import is_valid_url

DEFAULT_MAX_TEXT_CHARS = 200_000

# Never part of visible text.
NON_CONTENT_TAGS = ("script", "style", "noscript", "svg", "template", "iframe",
                    "object", "embed", "canvas", "head")
# Site chrome, excluded from "main content" (but still used for contacts/links).
BOILERPLATE_XPATH = (".//nav | .//header | .//footer | .//aside | "
                     ".//*[@role='navigation' or @role='banner' or @role='contentinfo']")
BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt",
    "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6",
    "header", "hr", "label", "li", "main", "nav", "ol", "p", "pre", "section",
    "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul",
}
HEADING_TAGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}

EMAIL_RE = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+")
PLACEHOLDER_EMAIL_LOCALS = {
    "your", "you", "yourname", "your.name", "youremail", "your.email", "name",
    "email", "user", "username", "someone", "john.doe", "jane.doe", "johndoe",
    "firstname.lastname", "first.last", "example", "test", "xxx", "abc",
}
PLACEHOLDER_EMAIL_DOMAINS = {
    "example.com", "example.org", "example.net", "domain.com", "yourdomain.com",
    "yourcompany.com", "company.com", "email.com", "website.com", "mysite.com",
}
RESERVED_TLDS = {"example", "invalid", "test", "localhost", "local"}
FILE_TLDS = {"png", "jpg", "jpeg", "gif", "svg", "webp", "avif", "css", "js", "ico",
             "pdf", "mp4", "woff", "woff2", "ttf"}

PHONE_RE = re.compile(r"(?<![\w+])(\+?\(?\d[\d \t().\-/]{6,}\d)(?![\w])")
PHONE_CONTEXT_RE = re.compile(
    r"(phone|tel|telephone|call|mobile|mob|whats\s?app|contact|ph)\b\.?\s*:?\s*$", re.I)
YEAR_RANGE_RE = re.compile(r"^(19|20)\d{2}\s*[-–/]\s*(19|20)\d{2}$")
DATE_RE = re.compile(r"^\d{1,4}[./-]\d{1,2}[./-]\d{1,4}$")
IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")

SOCIAL_NETWORKS = {
    "linkedin.com": "LinkedIn", "twitter.com": "X/Twitter", "x.com": "X/Twitter",
    "facebook.com": "Facebook", "instagram.com": "Instagram", "youtube.com": "YouTube",
    "github.com": "GitHub", "tiktok.com": "TikTok", "pinterest.com": "Pinterest",
    "threads.net": "Threads", "medium.com": "Medium", "behance.net": "Behance",
    "dribbble.com": "Dribbble", "t.me": "Telegram", "crunchbase.com": "Crunchbase",
    "clutch.co": "Clutch",
}
SOCIAL_NOT_PROFILE_RE = re.compile(
    r"/(intent|share|sharer|sharing|shareArticle|dialog|plugins|hashtag|search|watch|embed)\b"
    r"|share\.php|/p/[^/]+/?$", re.I)

ORG_TYPES = {
    "Organization", "Corporation", "LocalBusiness", "ProfessionalService",
    "OnlineBusiness", "OnlineStore", "NGO", "EducationalOrganization",
    "MedicalOrganization", "GovernmentOrganization", "NewsMediaOrganization",
    "SportsOrganization", "Store", "Restaurant", "LegalService", "FinancialService",
    "AccountingService", "HomeAndConstructionBusiness", "MedicalBusiness",
    "AutomotiveBusiness", "EmploymentAgency", "TravelAgency", "RealEstateAgent",
    "InsuranceAgency", "Dentist", "Hotel", "HealthAndBeautyBusiness",
}
SERVICE_TYPES = {"Service", "FinancialProduct", "BroadcastService", "CableOrSatelliteService",
                 "GovernmentService", "TaxiService", "WebAPI"}
PRODUCT_TYPES = {"Product", "ProductModel", "ProductGroup", "IndividualProduct",
                 "SoftwareApplication", "WebApplication", "MobileApplication", "Book",
                 "Course", "Vehicle"}

JS_APP_ROOT_XPATH = ("//*[@id='root' or @id='app' or @id='__next' or @id='__nuxt' "
                     "or @id='___gatsby' or @data-reactroot or @ng-version]")
JS_REQUIRED_RE = re.compile(r"(enable|turn on|requires?)\s+javascript|javascript\s+(is\s+)?required", re.I)

MAX_LIST_ITEMS = 500
MAX_TABLES = 50
MAX_TABLE_ROWS = 300
MAX_IMAGES = 500
MAX_LINKS = 2000


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def norm(text) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _schema_type_names(obj) -> list[str]:
    t = obj.get("@type") if isinstance(obj, dict) else None
    if t is None:
        return []
    values = t if isinstance(t, list) else [t]
    out = []
    for v in values:
        if isinstance(v, str) and v:
            out.append(re.sub(r"^(https?://schema\.org/|schema:)", "", v.strip()))
    return out


def _as_text(value) -> str:
    """Flatten a schema.org property value into readable text."""
    if value is None:
        return ""
    if isinstance(value, str):
        return norm(value)
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return ", ".join(t for t in (_as_text(v) for v in value) if t)
    if isinstance(value, dict):
        if "PostalAddress" in _schema_type_names(value) or "streetAddress" in value:
            return format_postal_address(value)
        for key in ("name", "url", "@id", "contentUrl"):
            if value.get(key):
                return _as_text(value[key])
    return ""


def format_postal_address(addr: dict) -> str:
    parts = [addr.get(k) for k in ("streetAddress", "addressLocality", "addressRegion",
                                   "postalCode", "addressCountry")]
    return ", ".join(norm(_as_text(p)) for p in parts if p and _as_text(p))


def _parse(html_text: str):
    if not html_text or not html_text.strip():
        return None
    try:
        # huge_tree matches how Scrapy/parsel already parse the same response;
        # without it libxml2 silently drops content nested > 256 levels deep.
        parser = lxml_html.HTMLParser(encoding="utf-8", huge_tree=True)
        return lxml_html.document_fromstring(html_text.encode("utf-8", "replace"), parser=parser)
    except (etree.ParserError, ValueError):
        return None


def _clean_copy(doc):
    """A copy of <body> without scripts, styles, comments or hidden elements."""
    bodies = doc.xpath("//body")
    body = _deepcopy(bodies[0] if bodies else doc)
    etree.strip_elements(body, etree.Comment, etree.ProcessingInstruction, with_tail=False)
    etree.strip_elements(body, *NON_CONTENT_TAGS, with_tail=False)
    hidden = body.xpath(".//*[@hidden] | .//*[@aria-hidden='true'] | "
                        ".//*[contains(translate(@style, ' ', ''), 'display:none')]")
    for el in hidden:
        if el.getparent() is not None:
            el.drop_tree()
    return body


def _deepcopy(el):
    return copy.deepcopy(el)


def render_text(el) -> str:
    """Visible text with line breaks at block boundaries (iterative, so
    deeply nested markup cannot hit Python's recursion limit)."""
    parts: list[str] = []
    for event, e in etree.iterwalk(el, events=("start", "end")):
        tag = e.tag if isinstance(e.tag, str) else None
        if event == "start":
            if tag in BLOCK_TAGS:
                parts.append("\n")
            if tag is not None and e.text:
                parts.append(e.text)
        else:
            if tag in BLOCK_TAGS:
                parts.append("\n")
            if e is not el and e.tail:
                parts.append(e.tail)
    lines = (norm(line) for line in "".join(parts).split("\n"))
    return "\n".join(line for line in lines if line)


def _direct_text(el) -> str:
    chunks = [el.text or ""] + [c.tail or "" for c in el]
    return norm(" ".join(chunks))


def _li_text(li) -> str:
    return norm(" ".join(li.xpath(
        "./text() | ./*[not(self::ul) and not(self::ol)]/descendant-or-self::text()")))


# --------------------------------------------------------------------------
# metadata & structured data
# --------------------------------------------------------------------------
def _meta_maps(doc):
    names, props = {}, {}
    for m in doc.xpath("//meta"):
        content = m.get("content")
        if content is None:
            continue
        content = norm(content)
        if m.get("name"):
            names.setdefault(m.get("name").strip().lower(), content)
        if m.get("property"):
            props.setdefault(m.get("property").strip().lower(), content)
        if m.get("http-equiv"):
            names.setdefault("http-equiv:" + m.get("http-equiv").strip().lower(), content)
    return names, props


def extract_json_ld(doc) -> tuple[list, int]:
    blocks, errors = [], 0
    for s in doc.xpath("//script[contains(@type, 'ld+json')]"):
        raw = (s.text or "").strip()
        raw = re.sub(r"^<!--|-->$", "", raw).strip()
        if not raw:
            continue
        try:
            blocks.append(json.loads(raw))
        except ValueError:
            errors += 1
    return blocks, errors


def iter_entities(obj, out=None, depth=0):
    """All dicts carrying an @type anywhere inside JSON-LD (incl. @graph)."""
    if out is None:
        out = []
    if depth > 15 or len(out) > 1000:
        return out
    if isinstance(obj, list):
        for v in obj:
            iter_entities(v, out, depth + 1)
    elif isinstance(obj, dict):
        if _schema_type_names(obj):
            out.append(obj)
        for v in obj.values():
            if isinstance(v, (dict, list)):
                iter_entities(v, out, depth + 1)
    return out


def summarize_entities(entities):
    """-> (organization dict | None, services list, products list, addresses, same_as)"""
    org = None
    services, products, addresses, same_as = [], [], [], []
    for ent in entities:
        types = set(_schema_type_names(ent))
        if types & ORG_TYPES and org is None and ent.get("name"):
            org = {
                "types": sorted(types),
                "name": _as_text(ent.get("name")),
                "legal_name": _as_text(ent.get("legalName")),
                "description": _as_text(ent.get("description")),
                "url": _as_text(ent.get("url")),
                "logo": _as_text(ent.get("logo")),
                "email": _as_text(ent.get("email")),
                "telephone": _as_text(ent.get("telephone")),
                "address": _as_text(ent.get("address")),
                "area_served": _as_text(ent.get("areaServed")),
                "founding_date": _as_text(ent.get("foundingDate")),
                "same_as": [s for s in (ent.get("sameAs") if isinstance(ent.get("sameAs"), list)
                                        else [ent.get("sameAs")]) if isinstance(s, str)],
            }
        if types & SERVICE_TYPES and ent.get("name"):
            services.append({
                "name": _as_text(ent.get("name")),
                "description": _as_text(ent.get("description")),
                "service_type": _as_text(ent.get("serviceType")),
                "provider": _as_text(ent.get("provider")),
                "area_served": _as_text(ent.get("areaServed")),
            })
        if types & PRODUCT_TYPES and ent.get("name"):
            products.append({
                "name": _as_text(ent.get("name")),
                "description": _as_text(ent.get("description")),
                "brand": _as_text(ent.get("brand")),
                "category": _as_text(ent.get("category") or ent.get("applicationCategory")),
                "type": sorted(types & PRODUCT_TYPES)[0],
            })
        if "PostalAddress" in types:
            formatted = format_postal_address(ent)
            if formatted:
                addresses.append(formatted)
        sa = ent.get("sameAs")
        for s in (sa if isinstance(sa, list) else [sa]):
            if isinstance(s, str):
                same_as.append(s)
    if org and org.get("address"):
        addresses.append(org["address"])
    return org, services, products, addresses, same_as


# --------------------------------------------------------------------------
# contacts
# --------------------------------------------------------------------------
def clean_email(candidate: str) -> str | None:
    email = candidate.strip().strip(".").lower()
    if not EMAIL_RE.fullmatch(email):
        return None
    local, _, domain = email.rpartition("@")
    tld = domain.rsplit(".", 1)[-1]
    if not tld.isalpha() or len(tld) < 2 or tld in FILE_TLDS or tld in RESERVED_TLDS:
        return None
    if local in PLACEHOLDER_EMAIL_LOCALS or domain in PLACEHOLDER_EMAIL_DOMAINS:
        return None
    if domain.startswith("example.") or ".example." in domain:
        return None
    return email


def extract_emails(visible_text: str, mailto_values) -> set:
    found = set()
    for value in mailto_values:
        addr = unquote(value.split(":", 1)[1]).split("?", 1)[0]
        for part in addr.split(","):
            e = clean_email(part)
            if e:
                found.add(e)
    for m in EMAIL_RE.finditer(visible_text or ""):
        e = clean_email(m.group(0))
        if e:
            found.add(e)
    return found


def _phone_digits(s: str) -> str:
    return re.sub(r"\D", "", s)


def extract_phones(visible_text: str, tel_values) -> list[str]:
    seen, out = set(), []

    def add(number):
        number = norm(number)
        digits = _phone_digits(number)
        if 8 <= len(digits) <= 15 and digits not in seen:
            seen.add(digits)
            out.append(number)

    text = visible_text or ""
    for m in PHONE_RE.finditer(text):  # visible (human-formatted) first
        cand = m.group(1).strip(" -./")
        digits = _phone_digits(cand)
        if not 8 <= len(digits) <= 15:
            continue
        if YEAR_RANGE_RE.match(cand) or DATE_RE.match(cand) or IP_RE.match(cand):
            continue
        before = text[max(0, m.start() - 30):m.start()]
        if cand.startswith(("+", "(")) or PHONE_CONTEXT_RE.search(before):
            add(cand)
    for value in tel_values:
        add(unquote(value.split(":", 1)[1]).split("?", 1)[0])
    return out


def dedupe_addresses(addresses) -> list[str]:
    """Drop exact and contained duplicates (e.g. the same address from
    JSON-LD and from an <address> tag)."""
    keyed = []
    for a in addresses:
        k = re.sub(r"[^a-z0-9]", "", a.lower())
        if k:
            keyed.append((k, a))
    out, seen = [], set()
    for k, a in keyed:
        if k in seen or any(k != k2 and k in k2 for k2, _ in keyed):
            continue
        seen.add(k)
        out.append(a)
    return out


def social_network(url: str) -> str | None:
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    for domain, name in SOCIAL_NETWORKS.items():
        if host == domain or host.endswith("." + domain):
            path = parts.path.strip("/")
            if not path or SOCIAL_NOT_PROFILE_RE.search("/" + parts.path + ("?" + parts.query if parts.query else "")):
                return None
            return name
    return None


def normalize_social(url: str) -> str:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    return urlunsplit((parts.scheme.lower() or "https", host, parts.path.rstrip("/"), "", ""))


# --------------------------------------------------------------------------
# content
# --------------------------------------------------------------------------
def _main_region(body):
    """Pick the main content container. Returns (element, scope_label)."""
    mains = body.xpath(".//main | .//*[@role='main']")
    if mains:
        return max(mains, key=lambda e: len(e.text_content())), "main"
    articles = body.xpath(".//article")
    if len(articles) == 1:
        return articles[0], "article"
    trimmed = _deepcopy(body)
    for el in trimmed.xpath(BOILERPLATE_XPATH):
        if el.getparent() is not None:
            el.drop_tree()
    full_words = len(body.text_content().split())
    if full_words > 50 and len(trimmed.text_content().split()) < 0.2 * full_words:
        return body, "full_body"
    return trimmed, "body_without_navigation"


def extract_sections(region, max_chars: int):
    """Headings, paragraphs and list items in document order, grouped under
    the nearest preceding heading. Also returns flat headings/paragraphs.
    Stops collecting text once max_chars is exceeded."""
    sections = [{"heading": "", "level": 0, "paragraphs": [], "items": []}]
    headings, paragraphs, seen_p = [], [], set()
    total, truncated = 0, False
    for el in region.iter():
        tag = el.tag if isinstance(el.tag, str) else None
        if tag is None:
            continue
        if tag in HEADING_TAGS:
            text = norm(el.text_content())
            if text:
                headings.append({"level": HEADING_TAGS[tag], "text": text})
                sections.append({"heading": text, "level": HEADING_TAGS[tag],
                                 "paragraphs": [], "items": []})
            continue
        text = ""
        kind = None
        if tag in ("p", "blockquote", "dd", "figcaption"):
            if not any(isinstance(a.tag, str) and a.tag in ("p", "li") for a in el.iterancestors()):
                text, kind = norm(el.text_content()), "p"
        elif tag == "li":
            text, kind = _li_text(el), "li"
        elif tag in ("div", "section", "span", "td") and not any(
                isinstance(a.tag, str) and a.tag in ("p", "li", "h1", "h2", "h3", "h4", "h5", "h6")
                for a in el.iterancestors()):
            direct = _direct_text(el)
            if len(direct) >= 25:
                text, kind = direct, "p"
        if not text or kind is None:
            continue
        if total + len(text) > max_chars:
            truncated = True
            break
        total += len(text)
        if kind == "p":
            sections[-1]["paragraphs"].append(text)
            if text not in seen_p:
                seen_p.add(text)
                paragraphs.append(text)
        else:
            sections[-1]["items"].append(text)
    sections = [s for s in sections if s["heading"] or s["paragraphs"] or s["items"]]
    return sections, headings, paragraphs, truncated


def extract_lists(region):
    lists, count = [], 0
    for lst in region.iter("ul", "ol"):
        items = [t for t in (_li_text(li) for li in lst.iterchildren("li")) if t]
        if items:
            lists.append(items[:MAX_LIST_ITEMS])
            count += 1
        if count >= MAX_LIST_ITEMS:
            break
    return lists


def extract_tables(region):
    tables = []
    for table in region.iter("table"):
        rows = []
        for tr in table.iter("tr"):
            cells = [norm(c.text_content()) for c in tr if isinstance(c.tag, str) and c.tag in ("td", "th")]
            if any(cells):
                rows.append(cells)
            if len(rows) >= MAX_TABLE_ROWS:
                break
        if rows:
            tables.append(rows)
        if len(tables) >= MAX_TABLES:
            break
    return tables


def extract_images(body, base_url):
    images, seen = [], set()
    for img in body.iter("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-lazy-src") or ""
        if not src and img.get("srcset"):
            src = img.get("srcset").split(",")[0].strip().split(" ")[0]
        src = src.strip()
        if not src:
            continue
        src = "data:(inline image)" if src.startswith("data:") else urljoin(base_url, src)
        alt = norm(img.get("alt"))
        key = (src, alt)
        if key in seen:
            continue
        seen.add(key)
        caption = ""
        for fig in img.iterancestors("figure"):
            caption = norm(" ".join(fig.xpath(".//figcaption//text()")))
            break
        images.append({"src": src, "alt": alt, "title": norm(img.get("title")), "caption": caption})
        if len(images) >= MAX_IMAGES:
            break
    return images


def extract_links(body, base_url, hosts):
    links, seen, mailto, tel = [], set(), [], []
    for a in body.iter("a"):
        href = (a.get("href") or "").strip()
        if not href or href.startswith("#"):
            continue
        low = href.lower()
        if low.startswith("mailto:"):
            mailto.append(href)
            continue
        if low.startswith("tel:"):
            tel.append(href)
            continue
        if low.startswith(("javascript:", "data:", "sms:", "whatsapp:")):
            continue
        absolute = urljoin(base_url, href)
        try:
            parts = urlsplit(absolute)
        except ValueError:
            continue
        if parts.scheme not in ("http", "https") or not parts.netloc:
            continue
        absolute = urlunsplit(parts._replace(fragment=""))
        text = norm(a.text_content()) or norm(a.get("aria-label")) or norm(a.get("title"))
        if not text:
            alts = [norm(i.get("alt")) for i in a.iter("img") if norm(i.get("alt"))]
            text = alts[0] if alts else ""
        key = (absolute, text)
        if key in seen:
            continue
        seen.add(key)
        rel = (a.get("rel") or "").lower().split()
        links.append({"url": absolute, "text": text,
                      "internal": bool(hosts) and is_valid_url(absolute, hosts),
                      "nofollow": "nofollow" in rel})
        if len(links) >= MAX_LINKS:
            break
    return links, mailto, tel


def detect_js_rendering(doc, word_count: int) -> bool:
    if word_count >= 80:
        return False
    markers = False
    for el in doc.xpath(JS_APP_ROOT_XPATH):
        if len(norm(el.text_content())) < 40:
            markers = True
            break
    if doc.xpath("//script[@id='__NEXT_DATA__' or @id='__NUXT_DATA__']"):
        markers = True
    for ns in doc.xpath("//noscript"):
        if JS_REQUIRED_RE.search(ns.text_content() or ""):
            markers = True
    scripts = len(doc.xpath("//script[@src]"))
    return markers or scripts >= 3


# --------------------------------------------------------------------------
# main entry point
# --------------------------------------------------------------------------
def empty_record(url, status, depth, requested_url=None, warnings=None, title=""):
    return {
        "url": url, "status": status, "title": title, "emails": set(), "depth": depth,
        "requested_url": requested_url or url,
        "crawl_timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "language": "", "meta_description": "", "meta_keywords": "", "canonical_url": "",
        "h1": "", "headings": [], "paragraphs": [], "sections": [], "lists": [], "tables": [],
        "main_text": "", "word_count": 0, "text_truncated": False, "content_scope": "",
        "images": [], "links": [], "internal_link_count": 0, "external_link_count": 0,
        "phones": [], "addresses": [], "social_profiles": [],
        "structured_data": {"json_ld": [], "json_ld_errors": 0, "open_graph": {},
                            "twitter": {}, "microdata_types": []},
        "jsonld_types": [], "organization": None,
        "services_structured": [], "products_structured": [],
        "js_rendering_suspected": False, "content_warnings": list(warnings or []),
    }


def extract_page(url: str, html_text: str, status: int = 200, depth: int = 0,
                 allowed_domains=None, max_text_chars: int = DEFAULT_MAX_TEXT_CHARS,
                 requested_url: str | None = None) -> dict:
    """Extract everything publicly present in one page's HTML."""
    record = empty_record(url, status, depth, requested_url)
    doc = _parse(html_text)
    if doc is None:
        record["content_warnings"].append("no_html_content")
        return record

    base = url
    base_href = doc.xpath("//base/@href")
    if base_href:
        base = urljoin(url, base_href[0].strip())
    hosts = [d.split(":")[0].lower() for d in (allowed_domains or []) if d]
    if not hosts:
        hosts = [(urlsplit(url).hostname or "").lower()]

    # -- metadata (same title semantics as the original spider) --
    titles = doc.xpath("//title/text()")
    record["title"] = titles[0].strip() if titles else ""
    names, props = _meta_maps(doc)
    record["meta_description"] = names.get("description", "")
    record["meta_keywords"] = names.get("keywords", "")
    for link in doc.xpath("//link[@rel and @href]"):
        if "canonical" in link.get("rel", "").lower().split():
            record["canonical_url"] = urljoin(base, link.get("href").strip())
            break
    record["language"] = norm(doc.get("lang") or doc.get("{http://www.w3.org/XML/1998/namespace}lang")
                              or names.get("http-equiv:content-language") or props.get("og:locale", ""))

    # -- structured data --
    blocks, errors = extract_json_ld(doc)
    entities = iter_entities(blocks)
    org, services, products, ld_addresses, same_as = summarize_entities(entities)
    record["structured_data"] = {
        "json_ld": blocks,
        "json_ld_errors": errors,
        "open_graph": {k: v for k, v in props.items() if k.startswith("og:")},
        "twitter": {k: v for k, v in {**names, **props}.items() if k.startswith("twitter:")},
        "microdata_types": sorted(set(doc.xpath("//*[@itemtype]/@itemtype"))),
    }
    record["jsonld_types"] = sorted({t for e in entities for t in _schema_type_names(e)})
    record["organization"] = org
    record["services_structured"] = services
    record["products_structured"] = products
    if errors:
        record["content_warnings"].append(f"json_ld_parse_errors:{errors}")

    # -- visible content --
    body = _clean_copy(doc)
    region, scope = _main_region(body)
    record["content_scope"] = scope
    main_text = render_text(region)
    if len(main_text) > max_text_chars:
        main_text = main_text[:max_text_chars]
        record["text_truncated"] = True
    record["main_text"] = main_text
    record["word_count"] = len(main_text.split())
    sections, headings, paragraphs, sec_truncated = extract_sections(region, max_text_chars)
    record["sections"], record["headings"], record["paragraphs"] = sections, headings, paragraphs
    record["text_truncated"] = record["text_truncated"] or sec_truncated
    h1s = [h["text"] for h in headings if h["level"] == 1] or \
        [norm(h.text_content()) for h in body.iter("h1") if norm(h.text_content())]
    record["h1"] = h1s[0] if h1s else ""
    record["lists"] = extract_lists(region)
    record["tables"] = extract_tables(region)

    # -- images & links (whole visible body, including navigation) --
    record["images"] = extract_images(body, base)
    links, mailto, tel = extract_links(body, base, hosts)
    record["links"] = links
    record["internal_link_count"] = len({lk["url"] for lk in links if lk["internal"]})
    record["external_link_count"] = len({lk["url"] for lk in links if not lk["internal"]})

    # -- contacts: visible text incl. header/footer, plus mailto:/tel: --
    visible = render_text(body)
    record["emails"] = extract_emails(visible, mailto)
    record["phones"] = extract_phones(visible, tel)
    addresses = list(ld_addresses)
    for addr in body.iter("address"):
        text = norm(addr.text_content())
        if len(text) >= 10 and (re.search(r"\d", text) or "," in text) and not EMAIL_RE.fullmatch(text):
            addresses.append(text)
    record["addresses"] = dedupe_addresses(addresses)
    socials = []
    for candidate in [lk["url"] for lk in links] + same_as + (org["same_as"] if org else []):
        if social_network(candidate):
            socials.append(normalize_social(candidate))
    record["social_profiles"] = list(dict.fromkeys(socials))

    # -- warnings --
    record["js_rendering_suspected"] = detect_js_rendering(doc, record["word_count"])
    if record["js_rendering_suspected"]:
        record["content_warnings"].append("js_rendering_suspected")
    if record["text_truncated"]:
        record["content_warnings"].append(f"text_truncated_at_{max_text_chars}_chars")
    if scope == "full_body":
        record["content_warnings"].append("main_content_not_identified_used_full_body")
    return record
