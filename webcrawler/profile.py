"""Website intelligence report: website_profile.json + website_profile.md.

Built locally with plain rules -- no AI service. Every finding carries an
evidence label and the source URL(s) it came from:

  * "fact: structured data"   -- schema.org / JSON-LD published by the site
  * "fact: page metadata"     -- <title>, meta description, Open Graph
  * "fact: quoted page text"  -- the site's own words, copied verbatim
  * "heuristic: page content" -- identified from headings/sections on
                                 crawled pages (rule-based, may misclassify)
  * "inferred summary"        -- generated sentence assembled from the facts

Nothing is derived from URL keywords alone, and anything not found on the
crawled pages is reported as "Not found". Only the pages actually crawled
(within the URL limit) are analysed.
"""
from __future__ import annotations

import json
import logging
import re
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

from webcrawler.crawl_limiter import registry
from webcrawler.extractors import social_network
from webcrawler.url_validator import safe_output_path

logger = logging.getLogger(__name__)

NOT_FOUND = "Not found"
EV_STRUCTURED = "fact: structured data (schema.org / JSON-LD)"
EV_METADATA = "fact: page metadata"
EV_QUOTE = "fact: quoted page text"
EV_HEURISTIC = "heuristic: page content"
EV_INFERRED = "inferred summary"

SERVICE_HEADING_RE = re.compile(
    r"\b(our services|services|what we do|what we offer|our expertise|expertise|"
    r"capabilities|our offerings|offerings|how we help|we offer|we provide)\b", re.I)
PRODUCT_HEADING_RE = re.compile(
    r"\b(our products|products?|our solutions|solutions|our platform|our tools)\b", re.I)
INDUSTRY_HEADING_RE = re.compile(
    r"\b(industr(y|ies)( we serve)?|sectors?|verticals?|who we serve|we serve|markets we serve)\b", re.I)
SERVICE_PAGE_RE = re.compile(r"\bservices?\b", re.I)
PRODUCT_PAGE_RE = re.compile(r"\bproducts?\b", re.I)
GENERIC_NAME_RE = re.compile(
    r"^(our |all |view all |see all |more )?(services?|products?|solutions?|offerings?|"
    r"what we do|what we offer|capabilities|expertise|industries)\s*:?$", re.I)
CTA_RE = re.compile(r"^(learn more|read more|view all|see all|see more|contact( us)?|get started|"
                    r"book (a )?(call|demo)|explore|discover more|click here|know more)\b", re.I)
AUDIENCE_RE = re.compile(
    r"\b(for|help(s|ing)?|serv(e|es|ing)|built for|designed for|trusted by|tailored for|"
    r"ideal for|partner(s|ing)? with|work(s|ing)? with)\b.{0,90}?\b(startups?|businesses|brands|"
    r"companies|enterprises?|teams|agencies|founders|marketers|small businesses|smbs?|b2b|b2c|"
    r"e-?commerce|retailers|clients|organi[sz]ations|professionals|creators|publishers)\b", re.I)
INDUSTRY_SENTENCE_RE = re.compile(r"\b(industr(y|ies)|sectors?|verticals?)\b", re.I)
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
TITLE_SEPARATOR_RE = re.compile(r"\s+[|\-–—:·•]\s+")

PAGE_CATEGORIES = [
    ("About", r"\babout\b", r"/about"),
    ("Contact", r"\bcontact\b", r"/contact"),
    ("Services", r"\bservices?\b", None),
    ("Products", r"\bproducts?\b", None),
    ("Pricing", r"\b(pricing|plans)\b", r"/pricing"),
    ("Careers", r"\b(careers?|jobs|join (our|the) team|we'?re hiring)\b", r"/careers?"),
    ("Case studies / work", r"\b(case stud(y|ies)|portfolio|our work|success stories|testimonials)\b", None),
    ("Team", r"\b(our team|leadership|founders?|meet the team)\b", r"/team"),
    ("Blog / resources", r"\b(blog|articles|insights|resources|news)\b", r"/blog"),
    ("Awards", r"\b(awards?|recognition)\b", None),
    ("Legal", r"\b(privacy|terms|cookie policy|legal|disclaimer)\b", r"/(privacy|terms)"),
    ("Login / account", r"\b(log ?in|sign ?in|sign ?up|register|my account)\b", r"/(login|signin|signup)"),
]

MAX_SECTIONS_PER_PAGE = 80
MAX_PARAS_PER_SECTION = 6
MAX_ITEMS_PER_SECTION = 40
MAX_TEXT = 700


def _norm(s) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _key(name: str) -> str:
    k = re.sub(r"[^a-z0-9]+", " ", name.lower())
    k = re.sub(r"\b(services?|agency|company|solutions?|the|our)\b", " ", k)
    return re.sub(r"\s+", " ", k).strip()


def _url_key(url: str) -> str:
    p = urlsplit(url)
    return urlunsplit(("", (p.hostname or "").lower(), p.path.rstrip("/") or "/", p.query, ""))


def _short(text: str, n: int = MAX_TEXT) -> str:
    text = _norm(text)
    return text if len(text) <= n else text[: n - 3].rstrip() + "..."


def _main_title_segment(title: str, brand: str | None) -> str:
    parts = TITLE_SEPARATOR_RE.split(title or "")
    if brand:
        parts = [p for p in parts if _norm(p).lower() != brand.lower()] or parts
    return _norm(parts[0]) if parts else ""


class ProfileBuilder:
    """Accumulates a compact view of each crawled page (bounded size), then
    builds the profile once the crawl ends."""

    def __init__(self, start_url: str = ""):
        self.start_url = start_url
        self.pages: list[dict] = []
        self.anchor_texts: dict[str, list[str]] = {}
        self.internal_urls: set[str] = set()
        self.external_urls: set[str] = set()

    # -- accumulation ----------------------------------------------------
    def add_page(self, item: dict) -> None:
        sd = item.get("structured_data") or {}
        og = sd.get("open_graph") or {}
        sections = []
        for s in (item.get("sections") or [])[:MAX_SECTIONS_PER_PAGE]:
            sections.append({
                "heading": _short(s.get("heading", ""), 200),
                "level": s.get("level", 0),
                "paragraphs": [_short(p) for p in s.get("paragraphs", [])[:MAX_PARAS_PER_SECTION]],
                "items": [_short(i, 200) for i in s.get("items", [])[:MAX_ITEMS_PER_SECTION]],
            })
        self.pages.append({
            "url": item.get("url", ""),
            "depth": item.get("depth", 0),
            "status": item.get("status", ""),
            "title": _norm(item.get("title", "")),
            "h1": _norm(item.get("h1", "")),
            "meta_description": _norm(item.get("meta_description", "")),
            "og_site_name": og.get("og:site_name", ""),
            "og_description": og.get("og:description", ""),
            "word_count": item.get("word_count", 0) or 0,
            "js_rendering_suspected": bool(item.get("js_rendering_suspected")),
            "text_truncated": bool(item.get("text_truncated")),
            "sections": sections,
            "paragraphs": [_short(p) for p in (item.get("paragraphs") or [])[:20]],
            "emails": sorted(item.get("emails") or []),
            "phones": list(item.get("phones") or []),
            "addresses": list(item.get("addresses") or []),
            "social_profiles": list(item.get("social_profiles") or []),
            "organization": item.get("organization"),
            "services_structured": list(item.get("services_structured") or [])[:50],
            "products_structured": list(item.get("products_structured") or [])[:50],
            "jsonld_types": list(item.get("jsonld_types") or []),
        })
        for link in item.get("links") or []:
            if link.get("internal"):
                if len(self.internal_urls) < 100_000:
                    self.internal_urls.add(_url_key(link["url"]))
                k = _url_key(link["url"])
                texts = self.anchor_texts.setdefault(k, []) if len(self.anchor_texts) < 5000 \
                    else self.anchor_texts.get(k)
                if texts is not None and link.get("text") and link["text"] not in texts and len(texts) < 5:
                    texts.append(_short(link["text"], 100))
            elif len(self.external_urls) < 100_000:
                self.external_urls.add(_url_key(link["url"]))

    # -- helpers ---------------------------------------------------------
    def _home(self):
        if not self.pages:
            return None
        start = _url_key(self.start_url) if self.start_url else None
        for p in self.pages:
            if start and _url_key(p["url"]) == start:
                return p
        return min(self.pages, key=lambda p: p["depth"])

    def _page_signals(self, page) -> str:
        return " ".join([page["title"], page["h1"]] + self.anchor_texts.get(_url_key(page["url"]), []))

    # -- sections of the report ---------------------------------------
    def _company_name(self, home):
        candidates = []
        for p in self.pages:
            org = p.get("organization")
            if org and org.get("name"):
                candidates.append({"value": org["name"], "evidence": EV_STRUCTURED,
                                   "source_url": p["url"], "note": "schema.org Organization name"})
                break
        site_names = [(p["og_site_name"], p["url"]) for p in ([home] if home else []) + self.pages
                      if p and p["og_site_name"]]
        if site_names:
            candidates.append({"value": site_names[0][0], "evidence": EV_METADATA,
                               "source_url": site_names[0][1], "note": "og:site_name"})
        titled = [p for p in self.pages if p["title"]]
        if len(titled) >= 2:
            firsts, lasts = Counter(), Counter()
            for p in titled:
                parts = [_norm(x) for x in TITLE_SEPARATOR_RE.split(p["title"]) if _norm(x)]
                if len(parts) >= 2:
                    firsts[parts[0]] += 1
                    lasts[parts[-1]] += 1
            best = max([(c, v) for v, c in lasts.items()] + [(c, v) for v, c in firsts.items()],
                       default=(0, ""))
            if best[0] >= 2 and best[0] >= 0.5 * len(titled) and len(best[1]) <= 60:
                src = next(p["url"] for p in titled if best[1] in p["title"])
                candidates.append({"value": best[1], "evidence": EV_HEURISTIC, "source_url": src,
                                   "note": f"brand segment shared by {best[0]} of {len(titled)} page titles"})
        if not candidates:
            return {"value": NOT_FOUND, "evidence": None, "source_url": None, "alternatives": []}
        return {**candidates[0], "alternatives": candidates[1:]}

    def _organization(self):
        for p in self.pages:
            if p.get("organization"):
                return p["organization"], p["url"]
        return None, None

    def _category(self, home):
        org, src = self._organization()
        result = {"value": NOT_FOUND, "evidence": None, "source_url": None,
                  "homepage_self_description": None}
        if org:
            specific = [t for t in org.get("types", []) if t != "Organization"]
            if specific:
                result.update(value=", ".join(specific), evidence=EV_STRUCTURED, source_url=src)
            else:
                result.update(value="Organization (no more specific schema.org type declared)",
                              evidence=EV_STRUCTURED, source_url=src)
        if home and home["title"]:
            result["homepage_self_description"] = {"text": home["title"], "evidence": EV_METADATA,
                                                   "source_url": home["url"], "note": "homepage <title>"}
        return result

    def _about_pages(self):
        out = []
        for p in self.pages:
            path = urlsplit(p["url"]).path.lower()
            if re.search(r"\babout\b", self._page_signals(p), re.I) or re.search(r"/about", path):
                if p is not self._home():
                    out.append(p)
        return out

    def _what_it_does(self, home):
        statements, seen = [], set()

        def add(text, evidence, url, note):
            text = _norm(text)
            if text and text.lower() not in seen:
                seen.add(text.lower())
                statements.append({"text": text, "evidence": evidence, "source_url": url, "note": note})

        org, src = self._organization()
        if org and org.get("description"):
            add(org["description"], EV_STRUCTURED, src, "schema.org Organization description")
        if home:
            add(home["meta_description"], EV_METADATA, home["url"], "homepage meta description")
            add(home["og_description"], EV_METADATA, home["url"], "homepage og:description")
            add(home["h1"], EV_QUOTE, home["url"], "homepage main heading (H1)")
            for para in [p for p in home["paragraphs"] if len(p) >= 60][:2]:
                add(para, EV_QUOTE, home["url"], "homepage paragraph")
        for about in self._about_pages()[:1]:
            add(about["meta_description"], EV_METADATA, about["url"], "About page meta description")
            for para in [p for p in about["paragraphs"] if len(p) >= 60][:3]:
                add(para, EV_QUOTE, about["url"], "About page paragraph")
        return statements

    def _offerings(self, heading_re, exclude_re, page_re, structured_key, home, brand):
        found: dict[str, dict] = {}

        def add(name, description, evidence, url, note):
            name = _norm(name)
            if not name or len(name) > 100 or len(name.split()) > 12 or name.endswith("?"):
                return
            if GENERIC_NAME_RE.match(name) or CTA_RE.match(name):
                return
            key = _key(name) or name.lower()
            entry = found.get(key)
            if entry is None:
                if len(found) >= 60:
                    return
                entry = found[key] = {"name": name, "description": "", "evidence": [], "source_urls": [],
                                      "notes": []}
            if description and not entry["description"]:
                entry["description"] = _short(description, 500)
            if evidence not in entry["evidence"]:
                entry["evidence"].append(evidence)
            if url not in entry["source_urls"]:
                entry["source_urls"].append(url)
            if note and note not in entry["notes"] and len(entry["notes"]) < 5:
                entry["notes"].append(note)

        # 1. structured data
        for p in self.pages:
            for s in p.get(structured_key) or []:
                add(s.get("name"), s.get("description"), EV_STRUCTURED, p["url"],
                    "schema.org " + ("Service" if structured_key == "services_structured" else s.get("type", "Product")))
        # 2. items / sub-headings listed under a matching heading
        for p in self.pages:
            secs = p["sections"]
            for i, sec in enumerate(secs):
                heading = sec["heading"]
                if not heading or not heading_re.search(heading):
                    continue
                if exclude_re is not None and exclude_re.search(heading):
                    continue  # e.g. "Services & Solutions" counts as services only
                note = f"listed under heading '{_short(heading, 80)}'"
                for item in sec["items"]:
                    add(item, "", EV_HEURISTIC, p["url"], note)
                for child in secs[i + 1:]:
                    if child["level"] <= sec["level"]:
                        break
                    if child["heading"]:
                        desc = child["paragraphs"][0] if child["paragraphs"] else ""
                        add(child["heading"], desc, EV_HEURISTIC, p["url"], note)
        # 3. pages whose own H1/title presents the offering (content, not URL)
        for p in self.pages:
            if p is home:
                continue
            heading = p["h1"] or _main_title_segment(p["title"], brand)
            if heading and page_re.search(p["h1"] + " " + p["title"]) and not GENERIC_NAME_RE.match(heading):
                desc = p["meta_description"] or (p["paragraphs"][0] if p["paragraphs"] else "")
                field = "H1" if p["h1"] else "title"
                add(heading, desc, EV_HEURISTIC, p["url"], f"page {field} presents it: '{_short(heading, 80)}'")
        # fill missing descriptions from a crawled page about the same thing
        for entry in found.values():
            if entry["description"]:
                continue
            k = _key(entry["name"])
            for p in self.pages:
                if k and (_key(p["h1"]) == k or _key(_main_title_segment(p["title"], brand)) == k):
                    desc = p["meta_description"] or (p["paragraphs"][0] if p["paragraphs"] else "")
                    if desc:
                        entry["description"] = _short(desc, 500)
                        if p["url"] not in entry["source_urls"]:
                            entry["source_urls"].append(p["url"])
                        break
        out = list(found.values())
        for e in out:
            if not e["description"]:
                e["description"] = NOT_FOUND
        return out

    def _sentences(self, regex, limit=10):
        out, seen = [], set()
        for p in self.pages:
            texts = list(p["paragraphs"]) + [x for s in p["sections"] for x in s["paragraphs"]]
            for text in texts:
                for sentence in SENTENCE_SPLIT_RE.split(text):
                    sentence = _norm(sentence)
                    if 20 <= len(sentence) <= 400 and regex.search(sentence) and sentence.lower() not in seen:
                        seen.add(sentence.lower())
                        out.append({"text": sentence, "evidence": EV_QUOTE, "source_url": p["url"]})
                        if len(out) >= limit:
                            return out
        return out

    def _industries(self):
        items = []
        seen = set()
        for p in self.pages:
            secs = p["sections"]
            for i, sec in enumerate(secs):
                if not sec["heading"] or not INDUSTRY_HEADING_RE.search(sec["heading"]):
                    continue
                names = list(sec["items"])
                for child in secs[i + 1:]:
                    if child["level"] <= sec["level"]:
                        break
                    if child["heading"]:
                        names.append(child["heading"])
                for n in names:
                    n = _norm(n)
                    if n and len(n) <= 80 and n.lower() not in seen and not CTA_RE.match(n):
                        seen.add(n.lower())
                        items.append({"text": n, "evidence": EV_HEURISTIC, "source_url": p["url"],
                                      "note": f"listed under heading '{_short(sec['heading'], 80)}'"})
        return items + self._sentences(INDUSTRY_SENTENCE_RE, limit=8)

    def _contacts(self):
        emails, phones, addresses = {}, {}, {}
        org, org_src = self._organization()
        for p in self.pages:
            for e in p["emails"]:
                emails.setdefault(e, [])
                if len(emails[e]) < 10:
                    emails[e].append(p["url"])
            for ph in p["phones"]:
                key = re.sub(r"\D", "", ph)
                entry = phones.setdefault(key, {"value": ph, "sources": []})
                if len(entry["sources"]) < 10:
                    entry["sources"].append(p["url"])
            for a in p["addresses"]:
                addresses.setdefault(a, [])
                if len(addresses[a]) < 10:
                    addresses[a].append(p["url"])

        def pack(value, sources, evidence):
            return {"value": value, "evidence": evidence, "source_urls": sources,
                    "pages_found_on": len(sources)}
        result = {
            "emails": [pack(e, s, EV_QUOTE) for e, s in emails.items()],
            "phones": [pack(v["value"], v["sources"], EV_QUOTE) for v in phones.values()],
            "addresses": [pack(a, s, EV_QUOTE) for a, s in addresses.items()],
            "structured": None,
        }
        if org:
            result["structured"] = {k: org.get(k) for k in ("email", "telephone", "address", "area_served")
                                    if org.get(k)}
            if result["structured"]:
                result["structured"]["source_url"] = org_src
                result["structured"]["evidence"] = EV_STRUCTURED
        return result

    def _social(self):
        out = {}
        for p in self.pages:
            for url in p["social_profiles"]:
                entry = out.setdefault(url, {"url": url, "network": social_network(url) or "",
                                             "evidence": EV_QUOTE, "source_urls": []})
                if len(entry["source_urls"]) < 5:
                    entry["source_urls"].append(p["url"])
        return list(out.values())

    def _important_pages(self, home):
        out = []
        for p in self.pages:
            cats, evidence = [], []
            if p is home:
                cats.append("Home")
                evidence.append("start page")
            signals = self._page_signals(p)
            path = urlsplit(p["url"]).path.lower()
            for name, text_re, path_re in PAGE_CATEGORIES:
                if re.search(text_re, signals, re.I):
                    cats.append(name)
                    evidence.append(f"{name}: title/H1/link text")
                elif path_re and re.search(path_re, path):
                    cats.append(name)
                    evidence.append(f"{name}: URL path")
            if cats:
                out.append({"url": p["url"], "title": p["title"], "categories": cats,
                            "evidence": EV_HEURISTIC, "basis": evidence})
        return out

    # -- assemble ----------------------------------------------------
    def build(self, coverage: dict | None = None) -> dict:
        coverage = dict(coverage or {})
        home = self._home()
        name = self._company_name(home)
        brand = name["value"] if name["value"] != NOT_FOUND else None
        category = self._category(home)
        does = self._what_it_does(home)
        services = self._offerings(SERVICE_HEADING_RE, None, SERVICE_PAGE_RE,
                                   "services_structured", home, brand)
        products = self._offerings(PRODUCT_HEADING_RE, SERVICE_HEADING_RE, PRODUCT_PAGE_RE,
                                   "products_structured", home, brand)
        audience = self._sentences(AUDIENCE_RE, limit=10)
        industries = self._industries()
        contacts = self._contacts()
        social = self._social()
        important = self._important_pages(home)
        org, org_src = self._organization()

        overview = []
        if brand:
            overview.append({"text": f"Company name: {brand}", "evidence": name["evidence"],
                             "source_url": name["source_url"]})
        if category["value"] != NOT_FOUND:
            overview.append({"text": f"Declared schema.org type: {category['value']}",
                             "evidence": category["evidence"], "source_url": category["source_url"]})
        if home and home["title"]:
            overview.append({"text": f"Homepage title: {home['title']}", "evidence": EV_METADATA,
                             "source_url": home["url"]})
        if home and home["meta_description"]:
            overview.append({"text": f"Homepage description: {home['meta_description']}",
                             "evidence": EV_METADATA, "source_url": home["url"]})
        if org:
            for label, key in (("Legal name", "legal_name"), ("Founded", "founding_date"),
                               ("Area served", "area_served"), ("Website", "url")):
                if org.get(key):
                    overview.append({"text": f"{label}: {org[key]}", "evidence": EV_STRUCTURED,
                                     "source_url": org_src})

        js_pages = [p["url"] for p in self.pages if p["js_rendering_suspected"]]
        truncated = [p["url"] for p in self.pages if p["text_truncated"]]
        stats = {
            "pages_analyzed": len(self.pages),
            "emails_found": len(contacts["emails"]),
            "phones_found": len(contacts["phones"]),
            "addresses_found": len(contacts["addresses"]),
            "services_identified": len(services),
            "products_identified": len(products),
            "social_profiles_found": len(social),
            "internal_links_discovered": len(self.internal_urls),
            "external_links_discovered": len(self.external_urls),
            "js_rendering_suspected_pages": len(js_pages),
            "text_truncated_pages": len(truncated),
        }
        limitations = [
            f"Only the {len(self.pages)} page(s) crawled were analysed"
            + (f" (URL limit {coverage['url_limit']})" if coverage.get("url_limit") else "")
            + "; other pages on the website were not visited.",
            "Pages blocked by robots.txt, behind logins, or that failed to load are not included.",
            "The crawler does not execute JavaScript; content rendered client-side is not captured.",
            "Services, products, audience and industries are identified by rules applied to page "
            "headings and text; they may be incomplete or misclassified and should be verified.",
        ]
        if js_pages:
            limitations.append(f"{len(js_pages)} page(s) look JavaScript-rendered and may be incomplete: "
                               + ", ".join(js_pages[:10]))
        if truncated:
            limitations.append(f"Text was truncated on {len(truncated)} page(s) (MAX_TEXT_CHARS).")
        coverage.update({"pages_analyzed": len(self.pages),
                         "js_rendering_suspected_pages": js_pages, "text_truncated_pages": truncated,
                         "limitations": limitations})

        def section(items):
            return {"status": "found" if items else NOT_FOUND, "items": items}

        profile = {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "start_url": self.start_url,
            "method": "Rule-based local extraction from crawled pages (no AI service).",
            "evidence_legend": {
                "structured": EV_STRUCTURED, "metadata": EV_METADATA, "quote": EV_QUOTE,
                "heuristic": EV_HEURISTIC, "inferred": EV_INFERRED,
            },
            "coverage": coverage,
            "company_overview": section(overview),
            "company_name": name,
            "business_category": category,
            "what_the_company_does": section(does),
            "services": section(services),
            "products_and_solutions": section(products),
            "target_audience": section(audience),
            "industries_served": section(industries),
            "contact_information": {
                "status": "found" if (contacts["emails"] or contacts["phones"] or contacts["addresses"]
                                      or contacts["structured"]) else NOT_FOUND,
                **contacts,
            },
            "social_media_profiles": section(social),
            "important_pages": section(important),
            "pages_crawled": [{"url": p["url"], "title": p["title"] or NOT_FOUND, "status": p["status"],
                               "word_count": p["word_count"], "js_rendering_suspected": p["js_rendering_suspected"]}
                              for p in self.pages],
            "statistics": stats,
        }
        profile["overall_summary"] = {"text": self._summary(profile, home), "evidence": EV_INFERRED,
                                      "note": "Generated from the facts above; not quoted from the website."}
        return profile

    def _summary(self, profile, home) -> str:
        if not self.pages:
            return "No pages were crawled, so no summary could be produced."
        name = profile["company_name"]["value"]
        subject = name if name != NOT_FOUND else "The website's owner (company name not found)"
        parts = []
        cat = profile["business_category"]["value"]
        if cat != NOT_FOUND and not cat.startswith("Organization ("):
            parts.append(f"{subject} declares itself in structured data as: {cat}.")
        else:
            parts.append(f"{subject} is the subject of this report.")
        if home and home["meta_description"]:
            parts.append(f'Its homepage describes it as: "{home["meta_description"].rstrip(". ")}".')
        elif home and home["title"]:
            parts.append(f'Its homepage title is: "{home["title"].rstrip(". ")}".')
        for label, key in (("services", "services"), ("products/solutions", "products_and_solutions")):
            items = profile[key]["items"]
            if items:
                names = ", ".join(i["name"] for i in items[:6])
                more = f" and {len(items) - 6} more" if len(items) > 6 else ""
                parts.append(f"{label.capitalize()} identified on the crawled pages include: {names}{more}.")
            else:
                parts.append(f"No {label} were identified on the crawled pages.")
        if profile["target_audience"]["items"]:
            parts.append("The site makes statements about its audience (see Target customers).")
        c = profile["contact_information"]
        parts.append(f"Public contact details found: {len(c['emails'])} email(s), {len(c['phones'])} "
                     f"phone number(s), {len(c['addresses'])} address(es); "
                     f"{len(profile['social_media_profiles']['items'])} social profile(s).")
        parts.append(f"Based on {len(self.pages)} crawled page(s); information on pages that were not "
                     "crawled is not reflected.")
        return " ".join(parts)


# --------------------------------------------------------------------------
# Markdown rendering
# --------------------------------------------------------------------------
def _md(text) -> str:
    return _norm(str(text)).replace("|", "\\|")


def _src(url) -> str:
    return f"[source]({url})" if url else ""


def render_markdown(profile: dict) -> str:
    L = []
    w = L.append
    name = profile["company_name"]["value"]
    w(f"# Website profile: {name if name != NOT_FOUND else profile.get('start_url', '')}")
    w("")
    w(f"- **Start URL:** {profile.get('start_url', '')}")
    w(f"- **Generated:** {profile['generated_at']}")
    w(f"- **Method:** {profile['method']}")
    w(f"- **Pages analysed:** {profile['statistics']['pages_analyzed']}")
    w("")
    w("> **How to read this report.** Each finding is labelled: *fact: structured data* (the site's "
      "schema.org/JSON-LD), *fact: page metadata* (titles/descriptions), *fact: quoted page text* "
      "(verbatim), *heuristic: page content* (rule-based identification, verify it), or *inferred "
      "summary* (generated wording). \"Not found\" means the crawled pages did not contain it.")
    w("")

    def statements(sec):
        if sec["status"] == NOT_FOUND:
            w(NOT_FOUND)
            return
        for s in sec["items"]:
            note = f" ({s['note']})" if s.get("note") else ""
            w(f"- {_md(s['text'])} — *{s['evidence']}*{note} {_src(s.get('source_url'))}")

    w("## 1. Company overview")
    statements(profile["company_overview"])
    w("")
    w("## 2. Company name")
    cn = profile["company_name"]
    if cn["value"] == NOT_FOUND:
        w(NOT_FOUND)
    else:
        w(f"**{_md(cn['value'])}** — *{cn['evidence']}* ({cn.get('note', '')}) {_src(cn['source_url'])}")
        for alt in cn.get("alternatives", []):
            w(f"- Also seen: {_md(alt['value'])} — *{alt['evidence']}* ({alt.get('note', '')}) {_src(alt['source_url'])}")
    w("")
    w("## 3. Business category")
    bc = profile["business_category"]
    if bc["value"] == NOT_FOUND:
        w(f"{NOT_FOUND} (no schema.org organization type declared on the crawled pages).")
    else:
        w(f"{_md(bc['value'])} — *{bc['evidence']}* {_src(bc['source_url'])}")
    if bc.get("homepage_self_description"):
        h = bc["homepage_self_description"]
        w(f"- Homepage presents itself as: \"{_md(h['text'])}\" — *{h['evidence']}* {_src(h['source_url'])}")
    w("")
    w("## 4. What the company does")
    statements(profile["what_the_company_does"])
    w("")

    def offerings(sec):
        if sec["status"] == NOT_FOUND:
            w(NOT_FOUND)
            return
        w("| Name | Description | Evidence | Source |")
        w("|---|---|---|---|")
        for o in sec["items"]:
            ev = "; ".join(o["evidence"]) + (f" — {'; '.join(o['notes'])}" if o.get("notes") else "")
            srcs = " ".join(f"[{i + 1}]({u})" for i, u in enumerate(o["source_urls"][:5]))
            w(f"| {_md(o['name'])} | {_md(o['description'])} | {_md(ev)} | {srcs} |")

    w("## 5. Main services")
    offerings(profile["services"])
    w("")
    w("## 6. Products and solutions")
    offerings(profile["products_and_solutions"])
    w("")
    w("## 7. Target customers or audience")
    statements(profile["target_audience"])
    w("")
    w("## 8. Industries served")
    statements(profile["industries_served"])
    w("")
    w("## 9. Contact information")
    ci = profile["contact_information"]
    if ci["status"] == NOT_FOUND:
        w(NOT_FOUND)
    else:
        for label, key in (("Email", "emails"), ("Phone", "phones"), ("Address", "addresses")):
            if not ci[key]:
                w(f"- **{label}:** {NOT_FOUND}")
            for c in ci[key]:
                w(f"- **{label}:** {_md(c['value'])} — *{c['evidence']}*, on {c['pages_found_on']} "
                  f"page(s) {_src(c['source_urls'][0])}")
        if ci.get("structured"):
            s = ci["structured"]
            details = ", ".join(f"{k}: {_md(v)}" for k, v in s.items() if k not in ("source_url", "evidence"))
            w(f"- **Structured data:** {details} — *{s['evidence']}* {_src(s['source_url'])}")
    w("")
    w("## 10. Social media profiles")
    sm = profile["social_media_profiles"]
    if sm["status"] == NOT_FOUND:
        w(NOT_FOUND)
    for s in sm["items"]:
        w(f"- {s['network'] or 'Profile'}: {s['url']} — *{s['evidence']}* {_src(s['source_urls'][0])}")
    w("")
    w("## 11. Important website pages")
    ip = profile["important_pages"]
    if ip["status"] == NOT_FOUND:
        w(NOT_FOUND)
    else:
        w("| Category | Page | Title | Basis |")
        w("|---|---|---|---|")
        for p in ip["items"]:
            w(f"| {_md(', '.join(p['categories']))} | {p['url']} | {_md(p['title'] or NOT_FOUND)} | "
              f"{_md('; '.join(p['basis']))} (*{p['evidence']}*) |")
    w("")
    w("## 12. Overall business summary")
    os_ = profile["overall_summary"]
    w(f"*{os_['evidence']} — {os_['note']}*")
    w("")
    w(os_["text"])
    w("")
    w("## Coverage and limitations")
    for line in profile["coverage"].get("limitations", []):
        w(f"- {line}")
    cov = profile["coverage"]
    for label, key in (("Pages crawled", "pages_crawled"), ("Pages failed", "pages_failed"),
                       ("Blocked by robots.txt", "robots_txt_blocked"), ("URL limit", "url_limit")):
        if key in cov:
            w(f"- {label}: {cov[key]}")
    w("")
    w("## All crawled pages")
    w("| URL | Title | Status | Words | JS-rendered? |")
    w("|---|---|---|---|---|")
    for p in profile["pages_crawled"]:
        w(f"| {p['url']} | {_md(p['title'])} | {p['status']} | {p['word_count']} | "
          f"{'possibly' if p['js_rendering_suspected'] else 'no'} |")
    w("")
    return "\n".join(L)


def write_profile(profile: dict, output_dir) -> dict:
    json_path = safe_output_path(output_dir, "website_profile.json")
    md_path = safe_output_path(output_dir, "website_profile.md")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(profile, f, indent=2, ensure_ascii=False, default=str)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(render_markdown(profile))
    return {"website_profile_json": str(json_path), "website_profile_md": str(md_path)}


class WebsiteProfilePipeline:
    """Feeds every item into a ProfileBuilder and writes the profile when
    the spider closes (pipelines close before the spider_closed signal, so
    the crawl summary can list the profile files). Independent of
    EXPORT_FORMATS."""

    def __init__(self, output_dir: str, crawler=None):
        self.output_dir = output_dir
        self.crawler = crawler
        self.builder = ProfileBuilder()

    @classmethod
    def from_crawler(cls, crawler):
        return cls(crawler.settings.get("OUTPUT_DIR", "outputs"), crawler)

    def open_spider(self, spider):
        start_urls = getattr(spider, "start_urls", None) or [""]
        self.builder = ProfileBuilder(start_url=start_urls[0])

    def process_item(self, item, spider):
        try:
            self.builder.add_page(dict(item))
        except Exception:  # never let reporting break the crawl/exports
            logger.exception("Could not add page to website profile")
        return item

    def _coverage(self):
        summary = registry.summary()
        robots = 0
        if self.crawler is not None and getattr(self.crawler, "stats", None) is not None:
            robots = self.crawler.stats.get_value("robotstxt/forbidden", 0) or 0
        return {"url_limit": summary["max_unique_urls"], "pages_crawled": summary["crawled"],
                "pages_failed": summary["failed"], "urls_discovered": summary["discovered"],
                "robots_txt_blocked": robots}

    def close_spider(self, spider):
        try:
            profile = self.builder.build(self._coverage())
        except Exception as exc:
            logger.exception("Website profile generation failed")
            profile = {"error": f"Profile generation failed: {exc}", "statistics": {},
                       "start_url": self.builder.start_url}
            path = safe_output_path(self.output_dir, "website_profile.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(profile, f, indent=2)
            spider.profile_paths = {"website_profile_json": str(path)}
            spider.profile_stats = {}
            return
        spider.profile_paths = write_profile(profile, self.output_dir)
        spider.profile_stats = profile["statistics"]
