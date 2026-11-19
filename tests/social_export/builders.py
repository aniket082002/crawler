"""Build small Instagram export ZIP files for the tests, in the JSON and HTML
layouts Instagram uses. Everything is fictional and built inside the tests;
nothing is downloaded. Non-ASCII text is written with chr() so these files
stay plain ASCII."""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

TS = 1673778600                      # 2023-01-15T10:30:00Z
TS_TEXT = "2023-01-15T10:30:00Z"
HTML_TIME = "Jan 15, 2023, 10:30 am"
THUMBS_UP = chr(0x1F44D)
E_ACUTE = chr(0xE9)
# How Instagram's JSON writes "cafe" with an accent: one character per UTF-8 byte.
CAFE_MOJIBAKE = "caf" + chr(0xC3) + chr(0xA9)
CAFE = "caf" + E_ACUTE


def make_zip(path: Path, files: dict, compression=zipfile.ZIP_DEFLATED) -> Path:
    """files: name -> dict/list (written as JSON), str or bytes."""
    with zipfile.ZipFile(path, "w", compression) as archive:
        for name, content in files.items():
            archive.writestr(name, encode(content))
    return path


def encode(content) -> bytes:
    if isinstance(content, (dict, list)):
        content = json.dumps(content)
    if isinstance(content, str):
        content = content.encode("utf-8")
    return content


def add_raw(path: Path, name: str, content, *, external_attr: int | None = None, flag_bits: int = 0) -> None:
    """Append one entry with a hand-made header (hostile names, links, flags)."""
    info = zipfile.ZipInfo(name)
    if external_attr is not None:
        info.external_attr = external_attr
        info.create_system = 3
    info.flag_bits |= flag_bits
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr(info, encode(content))


# --- JSON layouts ----------------------------------------------------------------------
def connection(name, ts=TS, href=None):
    return {"title": "", "media_list_data": [],
            "string_list_data": [{"href": href or f"https://www.instagram.com/{name}", "value": name,
                                  "timestamp": ts}]}


def followers_json(names, ts=TS):
    return [connection(n, ts) for n in names]


def following_json(names, ts=TS):
    return {"relationships_following": [connection(n, ts) for n in names]}


def post(uris, caption="Hello", ts=TS):
    return {"media": [{"uri": u, "creation_timestamp": ts, "title": caption} for u in uris],
            "title": caption, "creation_timestamp": ts}


def profile_json(username="Nike.Fan", **labels):
    data = {"Username": {"href": "", "value": username, "timestamp": 0},
            "Name": {"href": "", "value": "Nike Fan", "timestamp": 0},
            "Bio": {"href": "", "value": "Runner", "timestamp": 0}}
    for label, value in labels.items():
        data[label] = {"href": "", "value": value, "timestamp": 0}
    return {"profile_user": [{"media_map_data": {"Profile Photo": {"uri": "media/other/photo.jpg",
                                                                    "creation_timestamp": TS}},
                              "string_map_data": data}]}


def comment_new(text="nice", owner="friend", ts=TS):
    return {"media_list_data": [{"uri": ""}],
            "string_map_data": {"Comment": {"value": text}, "Media Owner": {"value": owner},
                                "Time": {"timestamp": ts}}}


def comments_old(text="nice", owner="friend", ts=TS):
    return {"comments_media_comments": [{"title": owner, "string_list_data": [{"value": text, "timestamp": ts}]}]}


def likes_json(owner="friend", hrefs=("https://www.instagram.com/p/abc/",), ts=TS):
    return {"likes_media_likes": [{"title": owner, "string_list_data": [
        {"href": h, "value": THUMBS_UP, "timestamp": ts} for h in hrefs]}]}


# --- HTML layouts ----------------------------------------------------------------------------
def html_page(blocks) -> str:
    body = "".join(f'<div class="pam _3-95 _2ph- _a6-g uiBoxWhite noborder">{b}</div>' for b in blocks)
    return f"<html><head><title>Export</title></head><body><div role='main'>{body}</div></body></html>"


def html_connection(name, when=HTML_TIME):
    return f'<div><div><a target="_blank" href="https://www.instagram.com/{name}">{name}</a></div><div>{when}</div></div>'


def html_post(uri, caption="Hello", when=HTML_TIME):
    return f'<h2>{caption}</h2><div><img src="{uri}" /></div><div>{when}</div>'


def html_comment(text="nice", owner="friend", when="Jan 15, 2023 10:30 am"):
    return ("<table><tr><td>Comment<div><div>" + text + "</div></div></td></tr>"
            "<tr><td>Media Owner<div><div>" + owner + "</div></div></td></tr>"
            "<tr><td>Time<div><div>" + when + "</div></div></td></tr></table>")


def html_like(owner="friend", href="https://www.instagram.com/p/abc/", when=HTML_TIME):
    return f'<h2>{owner}</h2><div><a href="{href}">{THUMBS_UP}</a></div><div>{when}</div>'


def html_profile(username="nike.fan", combined=False) -> str:
    if combined:
        rows = (f"<tr><td>Username<div><div>{username}</div></div></td></tr>"
                "<tr><td>Name<div><div>Nike Fan</div></div></td></tr>")
    else:
        rows = (f"<tr><td>Username</td><td>{username}</td></tr>"
                "<tr><td>Name</td><td>Nike Fan</td></tr>")
    return (f"<html><body><img src='media/other/photo.jpg'><table>{rows}</table></body></html>")


# --- whole exports ------------------------------------------------------------------------------
LAYOUTS = {
    "current": {"profile": "personal_information/personal_information/personal_information",
                "posts": "your_instagram_activity/content/posts_1",
                "comments": "your_instagram_activity/comments/post_comments_1",
                "likes": "your_instagram_activity/likes/liked_posts",
                "followers": "connections/followers_and_following/followers_1",
                "following": "connections/followers_and_following/following"},
    "older": {"profile": "account_information/personal_information",
              "posts": "content/posts_1",
              "comments": "comments/post_comments",
              "likes": "likes/liked_posts",
              "followers": "followers_and_following/followers",
              "following": "followers_and_following/following"},
    "wrapped": {"profile": "instagram-nike.fan-2024-01-01/personal_information/personal_information",
                "posts": "instagram-nike.fan-2024-01-01/content/posts_1",
                "comments": "instagram-nike.fan-2024-01-01/comments/post_comments_1",
                "likes": "instagram-nike.fan-2024-01-01/likes/liked_posts",
                "followers": "instagram-nike.fan-2024-01-01/connections/followers_1",
                "following": "instagram-nike.fan-2024-01-01/connections/following"},
}
JPEG = b"\xff\xd8\xff\xe0fake-jpeg-bytes"


def json_export(layout="current") -> dict:
    paths = LAYOUTS[layout]
    prefix = paths["posts"].split("/")[0] + "/" if layout == "wrapped" else ""
    return {
        paths["profile"] + ".json": profile_json(),
        paths["posts"] + ".json": [post(["media/posts/202301/a.jpg", "media/posts/202301/b.jpg"]),
                                   post(["media/posts/202301/missing.jpg"], caption="Second", ts=TS + 60)],
        paths["comments"] + ".json": [comment_new()],
        paths["likes"] + ".json": likes_json(),
        paths["followers"] + ".json": followers_json(["alice", "Bob"]),
        paths["following"] + ".json": following_json(["carol"]),
        prefix + "media/posts/202301/a.jpg": JPEG,
        prefix + "media/posts/202301/b.jpg": JPEG,
        prefix + "media/other/photo.jpg": JPEG,
        prefix + "messages/inbox/someone/message_1.json": {"messages": []},
    }


def html_export() -> dict:
    return {
        "personal_information/personal_information.html": html_profile(),
        "your_instagram_activity/content/posts_1.html": html_page([html_post("media/posts/202301/a.jpg")]),
        "your_instagram_activity/comments/post_comments_1.html": html_page([html_comment()]),
        "your_instagram_activity/likes/liked_posts.html": html_page([html_like()]),
        "connections/followers_and_following/followers_1.html": html_page(
            [html_connection("alice"), html_connection("bob")]),
        "connections/followers_and_following/following.html": html_page([html_connection("carol")]),
        "media/posts/202301/a.jpg": JPEG,
        "media/other/photo.jpg": JPEG,
    }
