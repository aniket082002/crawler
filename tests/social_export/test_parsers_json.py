"""JSON export files: every category, the formats Instagram has used, text
repair, timestamps, missing data and malformed files."""
import json

import pytest

from tests.social_export import builders as B
from webcrawler.social_export.parsers import (
    detect_category, epoch_to_utc, fix_text, parse_json_file, slug,
)


def parse(document, category, name="file.json"):
    data = document if isinstance(document, bytes) else json.dumps(document).encode()
    return parse_json_file(data, f"x/{name}", category, name)


@pytest.mark.parametrize("name, expected", [
    ("personal_information.json", ("profile", "json")), ("posts_1.json", ("posts", "json")),
    ("posts.json", ("posts", "json")), ("reels.json", ("posts", "json")), ("stories.json", ("posts", "json")),
    ("post_comments_1.json", ("comments", "json")), ("reels_comments.json", ("comments", "json")),
    ("liked_posts.json", ("likes", "json")), ("liked_comments.json", ("likes", "json")),
    ("followers_1.json", ("followers", "json")), ("followers.json", ("followers", "json")),
    ("following.json", ("following", "json")), ("FOLLOWERS_2.JSON", ("followers", "json")),
    ("followers_1.html", ("followers", "html")), ("posts_1.htm", ("posts", "html")),
    ("following_hashtags.json", (None, None)), ("message_1.json", (None, None)),
    ("followers_1.txt", (None, None)), ("recently_unfollowed_accounts.json", (None, None)),
])
def test_detect_category(name, expected):
    assert detect_category(name) == expected


def test_followers_current_format_with_mixed_case():
    result = parse(B.followers_json(["alice", "Bob.Smith"]), "followers", "followers_1.json")
    assert [i.values["username"] for i in result.items] == ["alice", "bob.smith"]
    assert result.items[1].values["username_as_found"] == "Bob.Smith"
    assert result.items[0].values["created_at"] == B.TS_TEXT


def test_followers_older_wrapped_format():
    document = {"relationships_followers": B.followers_json(["alice"])}
    assert [i.values["username"] for i in parse(document, "followers").items] == ["alice"]


def test_following_from_href_only_and_u_links():
    document = {"relationships_following": [
        {"title": "", "string_list_data": [{"href": "https://www.instagram.com/carol", "timestamp": B.TS}]},
        {"title": "dave", "string_list_data": [{"href": "https://www.instagram.com/_u/dave", "timestamp": B.TS}]},
        {"title": "", "string_list_data": [{"href": "https://www.instagram.com/_u/erin"}]},
    ]}
    items = parse(document, "following", "following.json").items
    assert [i.values["username"] for i in items] == ["carol", "dave", "erin"]
    assert all(i.record_type == "following" for i in items)


def test_invalid_username_is_reported_not_invented():
    document = [{"string_list_data": [{"href": "https://example.com/x", "value": "not valid!", "timestamp": B.TS}]}]
    item = parse(document, "followers").items[0]
    assert "username" not in item.values
    assert item.failed["username"] and item.values["username_as_found"] == "not valid!"


def test_follower_entry_without_username_is_skipped_with_warning():
    result = parse([{"string_list_data": [{"timestamp": B.TS}]}, "junk"], "followers")
    assert result.items == [] and len(result.warnings) == 2


def test_posts_with_several_media_and_caption():
    result = parse([B.post(["media/a.jpg", "media/b.jpg"], caption="Day out")], "posts", "posts_1.json")
    item = result.items[0]
    assert item.values["media_uris"] == ["media/a.jpg", "media/b.jpg"]
    assert item.values["media_count"] == 2 and item.values["caption"] == "Day out"
    assert item.values["post_type"] == "post" and item.values["created_at"] == B.TS_TEXT


def test_single_media_post_takes_caption_and_time_from_media():
    document = [{"media": [{"uri": "media/a.jpg", "creation_timestamp": B.TS, "title": "From media"}]}]
    item = parse(document, "posts", "posts_1.json").items[0]
    assert item.values["caption"] == "From media" and item.values["created_at"] == B.TS_TEXT


def test_stories_and_reels_formats():
    stories = {"ig_stories": [{"uri": "media/stories/s.mp4", "creation_timestamp": B.TS, "title": ""}]}
    item = parse(stories, "posts", "stories.json").items[0]
    assert item.values["post_type"] == "story" and item.values["media_uris"] == ["media/stories/s.mp4"]
    assert item.values["caption"] == ""                       # an empty caption is real, not missing
    reels = {"ig_reels_media": [B.post(["media/reels/r.mp4"])]}
    assert parse(reels, "posts", "reels.json").items[0].values["post_type"] == "reel"


def test_post_without_caption_key_has_no_caption_value():
    item = parse([{"media": [{"uri": "media/a.jpg", "creation_timestamp": B.TS}]}], "posts").items[0]
    assert "caption" not in item.values


def test_caption_mojibake_is_repaired():
    item = parse([B.post(["media/a.jpg"], caption=B.CAFE_MOJIBAKE)], "posts").items[0]
    assert item.values["caption"] == B.CAFE


@pytest.mark.parametrize("text", [B.CAFE, "plain", "emoji " + B.THUMBS_UP, "lone " + chr(0xE9) + " accent", ""])
def test_fix_text_keeps_correct_text(text):
    assert fix_text(text) == text


@pytest.mark.parametrize("value, expected", [
    (B.TS, (B.TS_TEXT, None)), (str(B.TS), (B.TS_TEXT, None)), (float(B.TS), (B.TS_TEXT, None)),
    (0, (None, None)), (None, (None, None)), ("", (None, None)),
])
def test_epoch_to_utc(value, expected):
    assert epoch_to_utc(value) == expected


@pytest.mark.parametrize("value", [-5, 12, 99999999999, "soon", True, float("nan"), [1]])
def test_bad_timestamps_are_reported(value):
    stamp, problem = epoch_to_utc(value)
    assert stamp is None and problem.startswith("not a valid Unix timestamp")


def test_bad_timestamp_marks_field_failed():
    item = parse([B.post(["media/a.jpg"], ts="yesterday")], "posts").items[0]
    assert "created_at" not in item.values and "Unix timestamp" in item.failed["created_at"]


def test_comments_current_and_older_formats():
    current = parse([B.comment_new(text="great " + B.CAFE_MOJIBAKE)], "comments", "post_comments_1.json").items[0]
    assert current.values["comment_text"] == "great " + B.CAFE
    assert current.values["media_owner"] == "friend" and current.values["created_at"] == B.TS_TEXT
    older = parse(B.comments_old(), "comments", "post_comments.json").items[0]
    assert older.values["comment_text"] == "nice" and older.values["media_owner"] == "friend"
    reels = parse(B.comments_old(), "comments", "reels_comments.json").items[0]
    assert reels.values["comment_kind"] == "reel_comment"


def test_likes_one_record_per_like():
    document = B.likes_json(hrefs=("https://www.instagram.com/p/a/", "https://www.instagram.com/p/b/"))
    items = parse(document, "likes", "liked_posts.json").items
    assert [i.values["target_url"] for i in items] == ["https://www.instagram.com/p/a/", "https://www.instagram.com/p/b/"]
    assert items[0].values["reaction"] == B.THUMBS_UP and items[0].values["like_kind"] == "post_like"
    assert parse({"likes_comment_likes": []}, "likes", "liked_comments.json").items == []


def test_profile_fields_and_photo():
    item = parse(B.profile_json(**{"Private Account": "False", "Date of birth": "2000-01-01"}), "profile").items[0]
    assert item.values["username"] == "nike.fan"
    assert item.values["name"] == "Nike Fan" and item.values["bio"] == "Runner"
    assert item.values["private_account"] == "False" and item.values["date_of_birth"] == "2000-01-01"
    assert item.values["profile_photo_uri"] == "media/other/photo.jpg"


def test_profile_with_invalid_username():
    item = parse(B.profile_json(username="bad name!"), "profile").items[0]
    assert item.failed["username"] and item.values["username_as_found"] == "bad name!"


def test_empty_profile_file_fails():
    assert parse({"profile_user": []}, "profile").failed


@pytest.mark.parametrize("data", [b"{not json", b'[{"string_list_data": [', b"", b"\xff\xfe\x00"])
def test_malformed_json_fails_gracefully(data):
    result = parse(data, "followers")
    assert result.failed and result.items == []


@pytest.mark.parametrize("document", [42, "text", True])
def test_wrong_structure_fails(document):
    assert parse(document, "posts").failed


def test_unknown_dict_gives_no_items_and_no_crash():
    assert parse({"something_else": {"a": 1}}, "followers").items == []


def test_invalid_utf8_is_replaced_with_warning():
    data = b'[{"string_list_data": [{"value": "alice\xff", "timestamp": 1673778600}]}]'
    result = parse(data, "followers")
    assert any("UTF-8" in w for w in result.warnings)


def test_deeply_nested_json_fails_gracefully():
    assert parse(b"[" * 100_000 + b"]" * 100_000, "posts").failed


@pytest.mark.parametrize("label, expected", [("Username", "username"), ("Private Account", "private_account"),
                                             ("Date of birth", "date_of_birth"), ("2FA", "field_2fa"),
                                             ("!!!", ""), ("A" * 100, "a" * 60)])
def test_slug(label, expected):
    assert slug(label) == expected
