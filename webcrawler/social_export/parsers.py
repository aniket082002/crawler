"""Recognise Instagram export files and parse them, in JSON and HTML layouts.

Files are recognised by their file name, wherever they sit in the archive,
so the different folder structures Instagram has used (for example
``content/posts_1.json``, ``your_instagram_activity/content/posts_1.json``,
``connections/followers_and_following/followers_1.json``, and the same under
an extra top-level folder) are all handled.

    profile     personal_information.json / .html
    posts       posts_N, reels, stories, igtv_videos, archived_posts
    comments    post_comments_N, reels_comments, comments
    likes       liked_posts, liked_comments
    followers   followers_N
    following   following

Every parser returns ParsedItem objects. A value is only ever taken from the
file: what a file does not contain is simply absent, and what it contains
but cannot be read is listed in ``failed`` with the reason. Nothing raises
for bad content; problems become warnings.

Standard library only; no I/O (the bytes are handed in by the caller).
"""
from __future__ import annotations

import datetime as _dt
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

from webcrawler.social_common.identity import IdentityError, classify_instagram_link, normalize_username
from webcrawler.social_common.schema import format_timestamp
from webcrawler.social_export.htmldoc import Node, parse_html

_EXT = r"\.(json|html?)$"
CATEGORY_PATTERNS = (
    ("profile", re.compile(r"^personal_information" + _EXT)),
    ("posts", re.compile(r"^(posts(_\d+)?|reels|stories|igtv_videos|archived_posts)" + _EXT)),
    ("comments", re.compile(r"^(post_comments(_\d+)?|reels_comments|comments)" + _EXT)),
    ("likes", re.compile(r"^(liked_posts|liked_comments)" + _EXT)),
    ("followers", re.compile(r"^followers(_\d+)?" + _EXT)),
    ("following", re.compile(r"^following" + _EXT)),
)
POST_TYPES = {"posts": "post", "reels": "reel", "stories": "story", "igtv_videos": "igtv",
              "archived_posts": "archived_post"}
RECORD_TYPE = {"profile": "profile", "posts": "post", "comments": "comment", "likes": "like",
               "followers": "follower", "following": "following"}

MIN_EPOCH = 1_000_000_000       # 2001-09-09; Instagram started in 2010
MAX_EPOCH = 4_102_444_800       # 2100-01-01
_UTC = _dt.timezone.utc
_MONTHS = {name: number for number, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}
HTML_DATE_RE = re.compile(
    r"\b([A-Z][a-z]{2,8})\.? (\d{1,2}), (\d{4}),? (\d{1,2}):(\d{2})(?::(\d{2}))?\s?([AaPp]\.?[Mm]\.?)?")
_U_LINK_RE = re.compile(r"instagram\.com/_u/([^/?#]+)", re.IGNORECASE)
_SLUG_RE = re.compile(r"[^a-z0-9]+")


@dataclass
class ParsedItem:
    record_type: str
    values: dict                    # field -> value found in the file
    failed: dict                    # field -> why a value present in the file could not be read
    source_file: str
    source_format: str              # "json" | "html"
    location: str                   # where in the file, e.g. "entry 3"
    warnings: list = field(default_factory=list)


@dataclass
class FileResult:
    items: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    failed: str = ""                # set when nothing could be read from the file


def detect_category(basename: str) -> tuple:
    """(category, format) for a recognised export file, else (None, None)."""
    name = basename.lower()
    for category, pattern in CATEGORY_PATTERNS:
        match = pattern.match(name)
        if match:
            return category, ("json" if name.endswith(".json") else "html")
    return None, None


def file_stem(basename: str) -> str:
    stem = basename.lower().rsplit(".", 1)[0]
    return re.sub(r"_\d+$", "", stem)


# --- value helpers --------------------------------------------------------------------
def fix_text(value: Any) -> Any:
    """Instagram's JSON writes each UTF-8 byte as its own character
    ("\\u00c3\\u00a9" for an accented e). Repair that when the text decodes
    cleanly; otherwise keep it exactly as it is."""
    if not isinstance(value, str):
        return value
    if any(ord(ch) > 0xFF for ch in value) or not any(0x80 <= ord(ch) <= 0xFF for ch in value):
        return value
    try:
        return value.encode("latin-1").decode("utf-8")
    except UnicodeError:
        return value


def epoch_to_utc(value: Any) -> tuple:
    """(UTC timestamp text or None, problem or None). 0 and None mean "no time"."""
    if value is None or value == 0 or value == "":
        return None, None
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
            or not MIN_EPOCH <= value <= MAX_EPOCH:
        return None, f"not a valid Unix timestamp ({str(value)[:40]})"
    return format_timestamp(_dt.datetime.fromtimestamp(value, _UTC)), None


def html_date(text: str) -> tuple:
    """(local date-time ``YYYY-MM-DDTHH:MM:SS`` or None, the matched text or None)."""
    match = HTML_DATE_RE.search(text or "")
    if not match:
        return None, None
    month = _MONTHS.get(match.group(1)[:3].lower())
    hour, minute, second = int(match.group(4)), int(match.group(5)), int(match.group(6) or 0)
    half = (match.group(7) or "").replace(".", "").lower()
    if half:
        if not 1 <= hour <= 12:
            return None, match.group(0)
        hour = hour % 12 + (12 if half == "pm" else 0)
    try:
        moment = _dt.datetime(int(match.group(3)), month or 0, int(match.group(2)), hour, minute, second)
    except (ValueError, TypeError):
        return None, match.group(0)
    return moment.strftime("%Y-%m-%dT%H:%M:%S"), match.group(0)


def resolve_username(text: Any, href: Any) -> str | None:
    """A valid lowercase username from an entry's text or link, else None."""
    if isinstance(text, str) and text.strip():
        try:
            return normalize_username(text.strip().lstrip("@"))
        except IdentityError:
            pass
    if isinstance(href, str) and href.strip():
        result = classify_instagram_link(href)
        if result.is_profile:
            return result.profile.username
        match = _U_LINK_RE.search(href)
        if match:
            try:
                return normalize_username(match.group(1))
            except IdentityError:
                return None
    return None


def slug(label: str) -> str:
    name = _SLUG_RE.sub("_", label.lower()).strip("_")
    if not name:
        return ""
    if not name[0].isalpha():
        name = f"field_{name}"
    return name[:60].rstrip("_")


def _item(record_type, source, fmt, location) -> ParsedItem:
    return ParsedItem(record_type, {}, {}, source, fmt, location)


def _set_time(item: ParsedItem, raw: Any) -> None:
    stamp, problem = epoch_to_utc(raw)
    if stamp:
        item.values["created_at"] = stamp
    elif problem:
        item.failed["created_at"] = problem


def _set_html_time(item: ParsedItem, chunks: list) -> None:
    for chunk in chunks:
        local, shown = html_date(chunk)
        if local:
            item.values["created_at_local"] = local
            return
        if shown:
            item.failed["created_at_local"] = f"the date '{shown}' is not a valid date"
            return


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


# --- JSON ---------------------------------------------------------------------------------
def _entries(document: Any, keys: tuple) -> list | None:
    """The list of entries in a JSON export file, whatever its wrapper."""
    if isinstance(document, list):
        return document
    if not isinstance(document, dict):
        return None
    for key in keys:
        if isinstance(document.get(key), list):
            return document[key]
    for value in document.values():
        if isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
            return value
    if any(k in document for k in ("string_list_data", "string_map_data", "media", "uri")):
        return [document]
    return []


def _first_dict(value: Any) -> dict:
    if isinstance(value, list):
        for v in value:
            if isinstance(v, dict):
                return v
    return value if isinstance(value, dict) else {}


def _json_profile(document, source, result: FileResult) -> None:
    entries = _entries(document, ("profile_user",))
    entry = _first_dict(entries)
    if not entry:
        result.failed = "no profile information was found in the file"
        return
    item = _item("profile", source, "json", "profile_user 1")
    data = entry.get("string_map_data")
    if isinstance(data, dict):
        for label, cell in data.items():
            name = slug(str(label))
            if not name or name in item.values:
                continue
            value = cell.get("value") if isinstance(cell, dict) else None
            if isinstance(value, str):
                item.values[name] = fix_text(value)
            elif value is not None:
                item.failed[name] = "the value has an unexpected type"
    media = entry.get("media_map_data")
    if isinstance(media, dict):
        photo = _first_dict(media.get("Profile Photo"))
        if isinstance(photo.get("uri"), str) and photo["uri"]:
            item.values["profile_photo_uri"] = photo["uri"]
    if "username" in item.values:
        try:
            item.values["username"] = normalize_username(item.values["username"])
        except IdentityError as exc:
            item.failed["username"] = str(exc)
            item.values["username_as_found"] = item.values.pop("username")
    if not item.values and not item.failed:
        result.failed = "no profile information was found in the file"
        return
    result.items.append(item)


def _json_posts(entries, source, stem, result: FileResult) -> None:
    post_type = POST_TYPES.get(stem, "post")
    for number, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            result.warnings.append(f"entry {number}: not an object; skipped")
            continue
        media = entry.get("media")
        if not isinstance(media, list):
            media = [entry] if "uri" in entry else []
        media = [m for m in media if isinstance(m, dict)]
        item = _item("post", source, "json", f"entry {number}")
        item.values["post_type"] = post_type
        uris = [m["uri"] for m in media if isinstance(m.get("uri"), str) and m["uri"].strip()]
        item.values["media_uris"] = uris
        item.values["media_count"] = len(uris)
        caption = _str(entry.get("title"))
        if not caption:
            caption = next((m["title"] for m in media if isinstance(m.get("title"), str) and m["title"]), caption)
        if caption is not None:
            item.values["caption"] = fix_text(caption)
        stamp = entry.get("creation_timestamp")
        if stamp in (None, 0) and media:
            stamp = media[0].get("creation_timestamp")
        _set_time(item, stamp)
        if not uris and "caption" not in item.values and "created_at" not in item.values:
            result.warnings.append(f"entry {number}: no media, caption or time; skipped")
            continue
        result.items.append(item)


def _json_comments(entries, source, stem, result: FileResult) -> None:
    kind = "reel_comment" if stem.startswith("reels") else "post_comment"
    for number, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            result.warnings.append(f"entry {number}: not an object; skipped")
            continue
        item = _item("comment", source, "json", f"entry {number}")
        item.values["comment_kind"] = kind
        data = entry.get("string_map_data")
        if isinstance(data, dict):
            text = _first_dict(data.get("Comment")).get("value")
            owner = _first_dict(data.get("Media Owner")).get("value")
            stamp = _first_dict(data.get("Time")).get("timestamp")
        else:
            first = _first_dict(entry.get("string_list_data"))
            text, stamp, owner = first.get("value"), first.get("timestamp"), entry.get("title")
        if isinstance(text, str):
            item.values["comment_text"] = fix_text(text)
        if isinstance(owner, str) and owner:
            item.values["media_owner"] = fix_text(owner)
        _set_time(item, stamp)
        if "comment_text" not in item.values:
            result.warnings.append(f"entry {number}: no comment text; skipped")
            continue
        result.items.append(item)


def _json_likes(entries, source, stem, result: FileResult) -> None:
    kind = "comment_like" if "comment" in stem else "post_like"
    for number, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            result.warnings.append(f"entry {number}: not an object; skipped")
            continue
        owner = _str(entry.get("title"))
        values = entry.get("string_list_data")
        if not isinstance(values, list) or not values:
            data = entry.get("string_map_data")
            values = [data] if isinstance(data, dict) else []
            if values:
                values = [{"href": _first_dict(data.get("URL")).get("value") or _first_dict(data.get("URL")).get("href"),
                           "timestamp": _first_dict(data.get("Time")).get("timestamp")}]
        if not values:
            result.warnings.append(f"entry {number}: no like data; skipped")
            continue
        for part, cell in enumerate(values, start=1):
            if not isinstance(cell, dict):
                continue
            item = _item("like", source, "json", f"entry {number}" + (f".{part}" if len(values) > 1 else ""))
            item.values["like_kind"] = kind
            if owner:
                item.values["media_owner"] = fix_text(owner)
            if isinstance(cell.get("href"), str) and cell["href"]:
                item.values["target_url"] = cell["href"]
            if isinstance(cell.get("value"), str) and cell["value"]:
                item.values["reaction"] = fix_text(cell["value"])
            _set_time(item, cell.get("timestamp"))
            result.items.append(item)


def _connection(record_type, source, fmt, location, text, href) -> ParsedItem:
    item = _item(record_type, source, fmt, location)
    found = text if isinstance(text, str) and text.strip() else href
    if isinstance(found, str) and found.strip():
        item.values["username_as_found"] = fix_text(found.strip())
    username = resolve_username(fix_text(text), href)
    if username:
        item.values["username"] = username
    elif "username_as_found" in item.values:
        item.failed["username"] = "the entry does not contain a valid Instagram username"
    return item


def _json_connections(entries, source, record_type, result: FileResult) -> None:
    for number, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            result.warnings.append(f"entry {number}: not an object; skipped")
            continue
        cells = [c for c in entry.get("string_list_data") or [] if isinstance(c, dict)] \
            if isinstance(entry.get("string_list_data"), list) else []
        if not cells:
            cells = [{}]
        for part, cell in enumerate(cells, start=1):
            text = cell.get("value") if isinstance(cell.get("value"), str) and cell.get("value") else entry.get("title")
            href = cell.get("href")
            item = _connection(record_type, source, "json", f"entry {number}" + (f".{part}" if len(cells) > 1 else ""),
                               text, href)
            _set_time(item, cell.get("timestamp"))
            if not item.values.get("username_as_found"):
                result.warnings.append(f"entry {number}: no username; skipped")
                continue
            result.items.append(item)


def parse_json_file(data: bytes, source: str, category: str, basename: str) -> FileResult:
    result = FileResult()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("utf-8", errors="replace")
        result.warnings.append("the file is not valid UTF-8; unreadable characters were replaced")
    try:
        document = json.loads(text)
    except (ValueError, RecursionError):
        result.failed = "the file is not valid JSON (malformed or cut off)"
        return result
    if category == "profile":
        _json_profile(document, source, result)
        return result
    stem = file_stem(basename)
    keys = {"posts": ("ig_reels_media", "ig_stories", "ig_igtv_media", "ig_archived_post_media"),
            "comments": ("comments_media_comments", "comments_reels_comments"),
            "likes": ("likes_media_likes", "likes_comment_likes"),
            "followers": ("relationships_followers",),
            "following": ("relationships_following",)}[category]
    entries = _entries(document, keys)
    if entries is None:
        result.failed = "the file does not have the structure of an Instagram export file"
        return result
    if category == "posts":
        _json_posts(entries, source, stem, result)
    elif category == "comments":
        _json_comments(entries, source, stem, result)
    elif category == "likes":
        _json_likes(entries, source, stem, result)
    else:
        _json_connections(entries, source, RECORD_TYPE[category], result)
    return result


# --- HTML -----------------------------------------------------------------------------------
def html_entries(root: Node) -> list:
    """Entry blocks of an Instagram HTML export page (outermost matches only)."""
    for marker in ("uiBoxWhite", "pam"):
        found, inside = [], set()
        for node in root.iter():
            if marker in node.classes():
                parent, nested = node.parent, False
                while parent is not None:
                    if id(parent) in inside:
                        nested = True
                        break
                    parent = parent.parent
                if not nested:
                    found.append(node)
                    inside.add(id(node))
        if found:
            return found
    return []


def _row_map(node: Node) -> dict:
    out = {}
    for chunks in node.rows():
        if len(chunks) >= 2:
            out.setdefault(chunks[0].strip().rstrip(":"), " ".join(chunks[1:]))
    return out


def _not_date(chunks: list) -> list:
    return [c for c in chunks if not (HTML_DATE_RE.search(c) and len(c) < 60)]


def _html_profile(root, source, result: FileResult) -> None:
    item = _item("profile", source, "html", "document")
    for label, value in _row_map(root).items():
        name = slug(label)
        if name and name not in item.values:
            item.values[name] = value
    photos = [src for src in root.media_sources() if not src.startswith("data:")]
    if photos:
        item.values["profile_photo_uri"] = photos[0]
    if "username" in item.values:
        try:
            item.values["username"] = normalize_username(item.values["username"])
        except IdentityError as exc:
            item.failed["username"] = str(exc)
            item.values["username_as_found"] = item.values.pop("username")
    if not item.values:
        result.failed = "no profile information was found in the HTML file"
        return
    result.items.append(item)


def _html_posts(blocks, source, stem, result: FileResult) -> None:
    for number, block in enumerate(blocks, start=1):
        item = _item("post", source, "html", f"entry {number}")
        item.values["post_type"] = POST_TYPES.get(stem, "post")
        uris = [s for s in block.media_sources() if not s.startswith("data:")]
        uris += [href for href, _ in block.links() if href.startswith("media/") and href not in uris]
        item.values["media_uris"] = uris
        item.values["media_count"] = len(uris)
        chunks = block.chunks()
        texts = _not_date(chunks)
        if texts:
            item.values["caption"] = texts[0]
        _set_html_time(item, chunks)
        if not uris and not texts and "created_at_local" not in item.values:
            result.warnings.append(f"entry {number}: no media, caption or date; skipped")
            continue
        result.items.append(item)


def _html_comments(blocks, source, stem, result: FileResult) -> None:
    kind = "reel_comment" if stem.startswith("reels") else "post_comment"
    for number, block in enumerate(blocks, start=1):
        item = _item("comment", source, "html", f"entry {number}")
        item.values["comment_kind"] = kind
        rows = _row_map(block)
        chunks = block.chunks()
        text = rows.get("Comment")
        if text is None:
            texts = _not_date(chunks)
            text = texts[0] if texts else None
        if text is not None:
            item.values["comment_text"] = text
        if rows.get("Media Owner"):
            item.values["media_owner"] = rows["Media Owner"]
        _set_html_time(item, [rows["Time"]] if rows.get("Time") else chunks)
        if "comment_text" not in item.values:
            result.warnings.append(f"entry {number}: no comment text; skipped")
            continue
        result.items.append(item)


def _html_likes(blocks, source, stem, result: FileResult) -> None:
    kind = "comment_like" if "comment" in stem else "post_like"
    for number, block in enumerate(blocks, start=1):
        links = block.links()
        if not links:
            result.warnings.append(f"entry {number}: no link; skipped")
            continue
        item = _item("like", source, "html", f"entry {number}")
        item.values["like_kind"] = kind
        href, reaction = links[0]
        item.values["target_url"] = href
        if reaction:
            item.values["reaction"] = reaction
        texts = [c for c in _not_date(block.chunks()) if c != reaction]
        if texts:
            item.values["media_owner"] = texts[0]
        _set_html_time(item, block.chunks())
        result.items.append(item)


def _html_connections(root, blocks, source, record_type, result: FileResult) -> None:
    if not blocks:       # unknown layout: every Instagram link is one entry
        blocks = [node for node in root.iter() if node.tag == "a"
                  and "instagram.com" in (node.attrs.get("href") or "").lower()]
    for number, block in enumerate(blocks, start=1):
        links = block.links() if block.tag != "a" else [(block.attrs.get("href", ""), block.text())]
        href, text = links[0] if links else ("", "")
        if not text:
            texts = _not_date(block.chunks())
            text = texts[0] if texts else ""
        item = _connection(record_type, source, "html", f"entry {number}", text, href)
        _set_html_time(item, block.chunks() if block.tag != "a" else [])
        if not item.values.get("username_as_found"):
            result.warnings.append(f"entry {number}: no username; skipped")
            continue
        result.items.append(item)


def parse_html_file(data: bytes, source: str, category: str, basename: str) -> FileResult:
    result = FileResult()
    root, warnings = parse_html(data)
    result.warnings.extend(warnings)
    if category == "profile":
        _html_profile(root, source, result)
        return result
    stem = file_stem(basename)
    blocks = html_entries(root)
    if not blocks and category != "followers" and category != "following":
        result.failed = "no entries were recognised in the HTML file (unknown layout)"
        return result
    if category == "posts":
        _html_posts(blocks, source, stem, result)
    elif category == "comments":
        _html_comments(blocks, source, stem, result)
    elif category == "likes":
        _html_likes(blocks, source, stem, result)
    else:
        _html_connections(root, blocks, source, RECORD_TYPE[category], result)
    if not result.items and not result.failed and category in ("followers", "following") and not blocks:
        result.failed = "no entries were recognised in the HTML file (unknown layout)"
    return result


def parse_file(data: bytes, source: str, category: str, fmt: str, basename: str) -> FileResult:
    if fmt == "json":
        return parse_json_file(data, source, category, basename)
    return parse_html_file(data, source, category, basename)
