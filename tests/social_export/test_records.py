"""Whole exports: record structure, honest statuses, media references,
de-duplication, different folder structures and missing categories."""
import json

import pytest

from tests.social_export import builders as B
from webcrawler.social_common.schema import FieldStatus
from webcrawler.social_export.records import MediaIndex, media_kind, process_export

WHEN = "2026-11-19T00:00:00Z"


def run(tmp_path, files, **kwargs):
    return process_export(B.make_zip(tmp_path / "export.zip", files), extracted_at=WHEN, **kwargs)


def counts(result):
    return {kind: len(records) for kind, records in result.records.items()}


EXPECTED_JSON = {"profile": 1, "post": 2, "comment": 1, "like": 1, "follower": 2, "following": 1, "media_file": 3}


@pytest.mark.parametrize("layout", ["current", "older", "wrapped"])
def test_every_folder_structure_gives_the_same_records(tmp_path, layout):
    result = run(tmp_path, B.json_export(layout))
    assert counts(result) == EXPECTED_JSON
    assert result.problems == []


def test_html_export(tmp_path):
    result = run(tmp_path, B.html_export())
    assert counts(result) == {"profile": 1, "post": 1, "comment": 1, "like": 1, "follower": 2, "following": 1,
                              "media_file": 2}


def test_record_provenance_and_status(tmp_path):
    record = run(tmp_path, B.json_export()).records["follower"][1]
    assert record.dedup_key == "instagram:follower:bob"
    assert record.extra["source_file"] == "connections/followers_and_following/followers_1.json"
    assert record.extra["source_format"] == "json" and record.extra["location"] == "entry 2"
    assert record.extra["extracted_at"] == WHEN and record.extra["extraction_status"] == "complete"
    assert record.fields["username"].value == "bob"
    assert record.fields["username_as_found"].value == "Bob"
    assert record.fields["profile_url"].value == "https://www.instagram.com/bob/"
    assert record.fields["username"].source.endswith("(entry 2)")


def test_time_fields_by_format(tmp_path):
    json_post = run(tmp_path, B.json_export()).records["post"][0]
    assert json_post.fields["created_at"].value == B.TS_TEXT
    assert json_post.fields["created_at_local"].status is FieldStatus.NOT_SUPPORTED
    html_post = process_export(B.make_zip(tmp_path / "h.zip", B.html_export())).records["post"][0]
    assert html_post.fields["created_at"].status is FieldStatus.NOT_SUPPORTED
    assert "time zone" in html_post.fields["created_at"].reason
    assert html_post.fields["created_at_local"].value == "2023-01-15T10:30:00"


def test_missing_values_are_not_invented(tmp_path):
    files = {"followers_1.json": [{"string_list_data": [{"value": "alice"}]}],
             "posts_1.json": [{"media": [{"uri": "media/x.jpg"}]}]}
    result = run(tmp_path, files)
    follower = result.records["follower"][0]
    assert follower.fields["created_at"].status is FieldStatus.NOT_AVAILABLE
    assert follower.fields["created_at"].value is None and follower.fields["created_at"].reason
    post = result.records["post"][0]
    assert post.fields["caption"].status is FieldStatus.NOT_AVAILABLE
    assert post.fields["media_missing"].value == ["media/x.jpg"]


def test_unreadable_values_are_parsing_failed_and_record_partial(tmp_path):
    files = {"followers_1.json": [{"string_list_data": [{"value": "not valid!", "timestamp": "never"}]}]}
    record = run(tmp_path, files).records["follower"][0]
    assert record.fields["username"].status is FieldStatus.PARSING_FAILED
    assert record.fields["created_at"].status is FieldStatus.PARSING_FAILED
    assert record.extra["extraction_status"] == "partial"
    assert record.dedup_key.startswith("instagram:follower:")


def test_media_references(tmp_path):
    result = run(tmp_path, B.json_export())
    first, second = result.records["post"]
    assert first.fields["media_missing"].value == []
    assert second.fields["media_missing"].value == ["media/posts/202301/missing.jpg"]
    media = {r.fields["path"].value: r.fields["referenced_by"].value for r in result.records["media_file"]}
    assert media == {"media/posts/202301/a.jpg": 1, "media/posts/202301/b.jpg": 1, "media/other/photo.jpg": 1}
    kinds = {r.fields["media_kind"].value for r in result.records["media_file"]}
    assert kinds == {"image"}


def test_media_index_matches_through_extra_folders():
    index = MediaIndex(["export-x/media/posts/a.jpg"])
    assert index.find("media/posts/a.jpg") == "export-x/media/posts/a.jpg"
    assert index.find("/media/posts/a.jpg") == "export-x/media/posts/a.jpg"
    assert index.find("media/posts/b.jpg") is None


@pytest.mark.parametrize("name, kind", [("a.JPG", "image"), ("b.mp4", "video"), ("c.m4a", "audio"),
                                        ("d.json", None), ("noext", None)])
def test_media_kind(name, kind):
    assert media_kind(name) == kind


def test_duplicates_across_files_are_merged(tmp_path):
    files = {"followers_1.json": B.followers_json(["alice", "bob"]),
             "followers_2.json": B.followers_json(["Alice", "carol"]),
             "posts_1.json": [B.post(["media/a.jpg"]), B.post(["media/a.jpg"])]}
    result = run(tmp_path, files)
    assert [r.fields["username"].value for r in result.records["follower"]] == ["alice", "bob", "carol"]
    alice = result.records["follower"][0]
    assert alice.extra["duplicates_merged"] == 1
    assert alice.extra["also_found_in"] == ["followers_2.json (entry 1)"]
    assert len(result.records["post"]) == 1 and result.duplicates_removed == 2


def test_same_person_as_follower_and_following_is_two_records(tmp_path):
    files = {"followers_1.json": B.followers_json(["alice"]), "following.json": B.following_json(["alice"])}
    result = run(tmp_path, files)
    assert len(result.records["follower"]) == 1 and len(result.records["following"]) == 1


def test_export_with_only_some_categories(tmp_path):
    result = run(tmp_path, {"following.json": B.following_json(["carol"])})
    assert counts(result)["following"] == 1
    assert all(counts(result)[k] == 0 for k in ("profile", "post", "comment", "like", "follower"))
    assert result.problems == []


def test_empty_archive(tmp_path):
    result = run(tmp_path, {})
    assert sum(counts(result).values()) == 0 and result.files == []


def test_unprocessed_files_are_listed(tmp_path):
    files = {"messages/inbox/a/message_1.json": {"messages": []}, "readme.txt": "hi", "tool.exe": b"MZ",
             "folder/": b""}
    result = run(tmp_path, files)
    by_path = {f.path: (f.action, f.message) for f in result.files}
    assert by_path["messages/inbox/a/message_1.json"] == (
        "not_processed", "this kind of export file is not processed by Module B")
    assert by_path["tool.exe"] == ("not_processed", "not an export data file")
    assert "folder/" not in by_path


def test_malformed_file_does_not_stop_the_others(tmp_path):
    files = {"followers_1.json": b"{broken", "following.json": B.following_json(["carol"])}
    result = run(tmp_path, files)
    assert [f.action for f in result.files] == ["failed", "parsed"]
    assert len(result.records["following"]) == 1
    assert any("not valid JSON" in p for p in result.problems)


def test_file_warnings_reach_the_inventory(tmp_path):
    result = run(tmp_path, {"followers_1.json": ["junk", B.connection("alice")]})
    assert result.files[0].action == "parsed_with_warnings" and "skipped" in result.files[0].message


def test_input_identity_is_recorded(tmp_path):
    path = B.make_zip(tmp_path / "export.zip", B.json_export())
    result = process_export(path)
    import hashlib
    assert result.input_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert result.input_name == "export.zip" and result.input_size_bytes == path.stat().st_size


def test_records_are_plain_json(tmp_path):
    result = run(tmp_path, B.json_export())
    for records in result.records.values():
        for record in records:
            json.dumps(record.to_dict(), allow_nan=False)


def test_record_limit(tmp_path, monkeypatch):
    import webcrawler.social_export.records as records_module
    monkeypatch.setattr(records_module, "MAX_RECORDS", 2)
    result = run(tmp_path, {"followers_1.json": B.followers_json(["a1", "b2", "c3"])})
    assert len(result.records["follower"]) == 2 and result.records_not_kept == 1
    assert any("not kept" in p for p in result.problems)
