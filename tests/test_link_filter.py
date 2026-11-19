from webcrawler.linkfilter import filter_internal_links


def test_internal_links_kept():
    links = [
        "https://example.com/page1",
        "https://sub.example.com/page2",
    ]
    internal, external = filter_internal_links(links, ["example.com"])
    assert internal == links
    assert external == []


def test_external_links_filtered_out():
    links = ["https://other.com/page3", "https://not-example.com/x"]
    internal, external = filter_internal_links(links, ["example.com"])
    assert internal == []
    assert external == links


def test_unsafe_schemes_treated_as_external_and_rejected():
    links = [
        "javascript:void(0)",
        "file:///etc/passwd",
        "data:text/html,<script>",
    ]
    internal, external = filter_internal_links(links, ["example.com"])
    assert internal == []
    assert set(external) == set(links)


def test_mixed_internal_and_external():
    links = [
        "https://example.com/a",
        "https://other.com/b",
        "https://example.com/c",
    ]
    internal, external = filter_internal_links(links, ["example.com"])
    assert internal == ["https://example.com/a", "https://example.com/c"]
    assert external == ["https://other.com/b"]
