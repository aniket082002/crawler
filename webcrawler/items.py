import scrapy


class PageItem(scrapy.Item):
    # --- original fields (unchanged) ---
    url = scrapy.Field()
    status = scrapy.Field()
    title = scrapy.Field()
    emails = scrapy.Field()
    depth = scrapy.Field()

    # --- page information ---
    requested_url = scrapy.Field()      # URL before any redirects
    crawl_timestamp = scrapy.Field()    # UTC, ISO 8601
    language = scrapy.Field()
    meta_description = scrapy.Field()
    meta_keywords = scrapy.Field()
    canonical_url = scrapy.Field()

    # --- content ---
    h1 = scrapy.Field()
    headings = scrapy.Field()           # [{level, text}]
    paragraphs = scrapy.Field()         # [str]
    sections = scrapy.Field()           # [{heading, level, paragraphs, items}]
    lists = scrapy.Field()              # [[str]]
    tables = scrapy.Field()             # [[[cell]]]
    main_text = scrapy.Field()
    word_count = scrapy.Field()
    text_truncated = scrapy.Field()
    content_scope = scrapy.Field()      # main | article | body_without_navigation | full_body

    # --- images & links ---
    images = scrapy.Field()             # [{src, alt, title, caption}]
    links = scrapy.Field()              # [{url, text, internal, nofollow}]
    internal_link_count = scrapy.Field()
    external_link_count = scrapy.Field()

    # --- contacts ---
    phones = scrapy.Field()
    addresses = scrapy.Field()
    social_profiles = scrapy.Field()

    # --- structured data ---
    structured_data = scrapy.Field()    # {json_ld, json_ld_errors, open_graph, twitter, microdata_types}
    jsonld_types = scrapy.Field()
    organization = scrapy.Field()
    services_structured = scrapy.Field()
    products_structured = scrapy.Field()

    # --- quality flags ---
    js_rendering_suspected = scrapy.Field()
    content_warnings = scrapy.Field()
