"""Cursor pagination over fake responses."""
import pytest

from tests.instagram.conftest import FAKE_FB_TOKEN, FakeTransport, SleepRecorder, response
from webcrawler.instagram.client import GraphApiClient, Paginator
from webcrawler.instagram.errors import AuthenticationError
from webcrawler.instagram.redaction import Redactor


def client_for(*pages):
    t = FakeTransport(*[p if not isinstance(p, dict) else response(200, p) for p in pages])
    c = GraphApiClient(base_url="https://graph.facebook.com", api_version="v27.0",
                       access_token=FAKE_FB_TOKEN, transport=t, sleep=SleepRecorder(),
                       redactor=Redactor(), backoff_base=0)
    return c, t


def page(ids, after=None, next_=True):
    body = {"data": [{"id": i} for i in ids]}
    if after is not None:
        body["paging"] = {"cursors": {"before": "B", "after": after}}
        if next_:
            body["paging"]["next"] = f"https://graph.facebook.com/v27.0/me/media?after={after}&access_token=x"
    return body


def ids(items):
    return [i["id"] for i in items]


def test_follows_after_cursor_until_no_next():
    c, t = client_for(page([1, 2], "c1"), page([3, 4], "c2"), page([5], "c3", next_=False))
    p = c.paginate("me/media", {"limit": 2}, ["id"])
    assert ids(p) == [1, 2, 3, 4, 5]
    assert p.summary() == {"pages_fetched": 3, "items": 5, "stop_reason": "end_of_results"}
    urls = [r.url for r in t.requests]
    assert "after=" not in urls[0]
    assert "after=c1" in urls[1] and "after=c2" in urls[2]


def test_never_follows_paging_next_url_directly():
    # paging.next points at another host; the paginator must ignore that URL.
    evil = page([1], "c1")
    evil["paging"]["next"] = "https://evil.example/steal?x=1"
    c, t = client_for(evil, page([2], None))
    assert ids(c.paginate("me/media")) == [1, 2]
    assert all(r.url.startswith("https://graph.facebook.com/v27.0/me/media") for r in t.requests)


def test_single_page_without_paging():
    c, t = client_for({"data": [{"id": 1}]})
    p = c.paginate("me/media")
    assert ids(p) == [1] and p.stop_reason == "end_of_results" and len(t.requests) == 1


def test_empty_result():
    c, _ = client_for({"data": []})
    p = c.paginate("me/media")
    assert list(p) == [] and p.pages_fetched == 1


def test_empty_page_with_next_continues():
    c, _ = client_for(page([], "c1"), page([7], "c2", next_=False))
    assert ids(c.paginate("me/media")) == [7]


def test_repeated_cursor_stops_loop():
    c, t = client_for(page([1], "same"), page([2], "same"), page([3], "x"))
    p = c.paginate("me/media")
    assert ids(p) == [1, 2] and p.stop_reason == "repeated_cursor" and len(t.requests) == 2


def test_max_pages_limit():
    c, t = client_for(*[page([n], f"c{n}") for n in range(10)])
    p = c.paginate("me/media", max_pages=3)
    assert ids(p) == [0, 1, 2] and p.stop_reason == "max_pages" and len(t.requests) == 3


def test_max_items_limit_stops_mid_page_without_extra_request():
    c, t = client_for(page([1, 2, 3], "c1"), page([4, 5, 6], "c2"))
    p = c.paginate("me/media", max_items=4)
    assert ids(p) == [1, 2, 3, 4] and p.stop_reason == "max_items" and len(t.requests) == 2


def test_max_items_zero_sends_nothing():
    c, t = client_for(page([1], "c1"))
    p = c.paginate("me/media", max_items=0)
    assert list(p) == [] and t.requests == [] and p.stop_reason == "max_items"


def test_page_size_sent_as_limit():
    c, t = client_for(page([1], None))
    list(c.paginate("me/media", page_size=50))
    assert "limit=50" in t.requests[0].url


def test_edges_without_next_use_cursor_until_empty():
    # Edges like /tags or Business Discovery return cursors but no "next" URL.
    c, t = client_for(page([1], "c1", next_=False), page([2], "c2", next_=False), page([], "c3", next_=False))
    p = c.paginate("me/tags", require_next=False)
    assert ids(p) == [1, 2] and p.stop_reason == "empty_page" and len(t.requests) == 3


def test_nested_cursor_via_request_factory_and_container():
    """Business-Discovery-shaped pagination: cursor inside the fields expression."""
    def bd(ids_, after):
        media = {"data": [{"id": i} for i in ids_]}
        if after:
            media["paging"] = {"cursors": {"after": after}}
        return {"business_discovery": {"id": "99", "media": media}, "id": "17841400000000000"}

    c, t = client_for(bd([1, 2], "m1"), bd([3], None))

    def factory(after):
        media = "media.after(%s){id,caption}" % after if after else "media{id,caption}"
        return "17841400000000000", None, [f"business_discovery.username(nike){{{media}}}"]

    p = Paginator(c, request_factory=factory, container=lambda body: body["business_discovery"]["media"],
                  require_next=False)
    assert ids(p) == [1, 2, 3]
    assert "media.after(m1)" in t.requests[1].url


def test_missing_container_stops_cleanly():
    c, _ = client_for({"unexpected": True})
    p = Paginator(c, path="x", container=lambda b: b.get("business_discovery"))
    assert list(p) == [] and p.stop_reason == "no_page_container"


def test_non_list_data_treated_as_empty():
    c, _ = client_for({"data": {"id": 1}})
    assert list(c.paginate("me/media")) == []


def test_error_mid_pagination_propagates_and_keeps_counts():
    c, _ = client_for(page([1], "c1"), response(400, {"error": {"code": 190, "message": "expired"}}))
    p = c.paginate("me/media")
    got = []
    with pytest.raises(AuthenticationError):
        for item in p:
            got.append(item)
    assert ids(got) == [1] and p.pages_fetched == 1


def test_transient_error_mid_pagination_is_retried():
    c, t = client_for(page([1], "c1"), response(503, {}), page([2], None))
    assert ids(c.paginate("me/media")) == [1, 2] and len(t.requests) == 3


@pytest.mark.parametrize("kwargs", [{"max_pages": 0}, {"max_items": -1}, {"page_size": 0}])
def test_invalid_paginator_settings(kwargs):
    c, _ = client_for()
    with pytest.raises(ValueError):
        Paginator(c, path="me/media", **kwargs)


def test_paginator_requires_path_or_factory():
    c, _ = client_for()
    with pytest.raises(ValueError):
        Paginator(c)
