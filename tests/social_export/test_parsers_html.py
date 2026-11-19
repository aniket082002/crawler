"""HTML export files: every category, local dates, broken and hostile markup."""
import pytest

from tests.social_export import builders as B
from webcrawler.social_export.htmldoc import MAX_DEPTH, parse_html
from webcrawler.social_export.parsers import html_date, parse_html_file


def parse(html, category, name="file.html"):
    return parse_html_file(html.encode("utf-8") if isinstance(html, str) else html, f"x/{name}", category, name)


@pytest.mark.parametrize("text, expected", [
    ("Jan 15, 2023, 10:30 am", "2023-01-15T10:30:00"), ("Jan 15, 2023 10:30 pm", "2023-01-15T22:30:00"),
    ("Dec 1, 2022, 12:05 am", "2022-12-01T00:05:00"), ("Dec 1, 2022, 12:05 PM", "2022-12-01T12:05:00"),
    ("September 9, 2021, 7:01:02 p.m.", "2021-09-09T19:01:02"), ("Mar 3, 2020 23:15", "2020-03-03T23:15:00"),
])
def test_html_date(text, expected):
    assert html_date(text)[0] == expected


@pytest.mark.parametrize("text", ["Feb 30, 2023, 10:00 am", "Foo 3, 2020 10:00", "Jan 3, 2020, 13:00 pm"])
def test_impossible_dates_are_not_invented(text):
    local, shown = html_date(text)
    assert local is None and shown


def test_no_date():
    assert html_date("no date here") == (None, None)


def test_followers_and_following():
    page = B.html_page([B.html_connection("alice"), B.html_connection("Bob")])
    items = parse(page, "followers", "followers_1.html").items
    assert [i.values["username"] for i in items] == ["alice", "bob"]
    assert items[0].values["created_at_local"] == "2023-01-15T10:30:00"
    assert "created_at" not in items[0].values
    assert parse(B.html_page([B.html_connection("carol")]), "following").items[0].record_type == "following"


def test_followers_in_unknown_layout_fall_back_to_links():
    page = "<html><body><ul><li><a href='https://www.instagram.com/alice'>alice</a></li></ul></body></html>"
    assert [i.values["username"] for i in parse(page, "followers").items] == ["alice"]


def test_posts():
    page = B.html_page([B.html_post("media/posts/202301/a.jpg", caption="Hello there")])
    item = parse(page, "posts", "posts_1.html").items[0]
    assert item.values["media_uris"] == ["media/posts/202301/a.jpg"]
    assert item.values["caption"] == "Hello there" and item.values["created_at_local"] == "2023-01-15T10:30:00"


def test_post_with_bad_date_is_marked_failed():
    page = B.html_page([B.html_post("media/a.jpg", when="Feb 30, 2023, 10:00 am")])
    assert "created_at_local" in parse(page, "posts").items[0].failed


def test_comments_table_layout():
    item = parse(B.html_page([B.html_comment(text="so good")]), "comments", "post_comments_1.html").items[0]
    assert item.values["comment_text"] == "so good" and item.values["media_owner"] == "friend"
    assert item.values["created_at_local"] == "2023-01-15T10:30:00"


def test_likes():
    item = parse(B.html_page([B.html_like()]), "likes", "liked_posts.html").items[0]
    assert item.values["target_url"] == "https://www.instagram.com/p/abc/"
    assert item.values["media_owner"] == "friend" and item.values["reaction"] == B.THUMBS_UP


@pytest.mark.parametrize("combined", [False, True])
def test_profile_both_table_layouts(combined):
    item = parse(B.html_profile("Nike.Fan", combined=combined), "profile").items[0]
    assert item.values["username"] == "nike.fan" and item.values["name"] == "Nike Fan"
    assert item.values["profile_photo_uri"] == "media/other/photo.jpg"


@pytest.mark.parametrize("category", ["posts", "comments", "likes", "followers", "profile"])
def test_unknown_layout_fails_instead_of_guessing(category):
    result = parse("<html><body><p>Nothing to see</p></body></html>", category)
    assert result.failed and result.items == []


@pytest.mark.parametrize("html", [
    "<div class='uiBoxWhite'><a href='https://www.instagram.com/alice'>alice</a>",      # never closed
    "</div></div><div class='uiBoxWhite'><a href='https://www.instagram.com/alice'>alice</a></span></div>",
    "<div class='uiBoxWhite'><<a href='https://www.instagram.com/alice'>alice</a><!-- open comment",
])
def test_broken_html_is_tolerated(html):
    assert [i.values["username"] for i in parse(html, "followers").items] == ["alice"]


def test_random_bytes_do_not_raise():
    result = parse(bytes(range(256)) * 20, "followers")
    assert result.items == []


def test_scripts_are_ignored():
    page = B.html_page(["<script>alert('x')</script>" + B.html_post("media/a.jpg", caption="Real caption")])
    assert parse(page, "posts").items[0].values["caption"] == "Real caption"


def test_deep_nesting_is_bounded():
    root, warnings = parse_html(b"<div>" * (MAX_DEPTH * 4) + b"x")
    assert any("nested" in w for w in warnings)
    depth, node = 0, root
    while [c for c in node.children if not isinstance(c, str)]:
        node = [c for c in node.children if not isinstance(c, str)][0]
        depth += 1
    assert depth <= MAX_DEPTH + 1


def test_invalid_utf8_warning():
    assert any("UTF-8" in w for w in parse_html(b"<p>caf\xe9</p>")[1])
