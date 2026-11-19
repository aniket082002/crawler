"""Safe ZIP handling: corrupted archives, path traversal, links, encryption,
duplicates, size / count / decompression limits, and nothing extracted."""
import os
import stat
import zipfile

import pytest

from tests.social_export.builders import TS, add_raw, followers_json, make_zip
from webcrawler.social_export.archive import (
    ArchiveError, ArchiveLimits, ExportArchive, MemberError, name_problem,
)
from webcrawler.social_export.records import process_export

FOLLOWERS = "connections/followers_and_following/followers_1.json"


@pytest.fixture
def good_zip(tmp_path):
    return make_zip(tmp_path / "export.zip", {FOLLOWERS: followers_json(["alice"])})


def test_clean_archive_is_listed_without_problems(good_zip):
    with ExportArchive(good_zip) as archive:
        assert [(e.name, e.problem) for e in archive.entries] == [(FOLLOWERS, None)]
        assert b"alice" in archive.read(archive.entries[0])


@pytest.mark.parametrize("name, code", [
    ("../evil.json", "parent_reference"), ("a/../../evil.json", "parent_reference"),
    ("..", "parent_reference"), ("/etc/passwd", "absolute_path"), ("C:/Windows/evil.json", "drive_letter"),
    ("c:evil.json", "drive_letter"), ("a\\..\\evil.json", "backslash"), ("bad\x01name.json", "control_characters"),
    ("x" * 2000, "name_too_long"), ("", "empty_name"),
])
def test_suspicious_names(name, code):
    assert name_problem(name, 1024) == code


@pytest.mark.parametrize("name", ["followers_1.json", "a/b/c.json", "media/posts/202301/a b.jpg", "a/./b.json",
                                  "folder/", "x..y.json"])
def test_normal_names_are_accepted(name):
    assert name_problem(name, 1024) is None


@pytest.mark.parametrize("name", ["../../evil_followers_1.json", "/tmp/followers_1.json",
                                  "C:/followers_1.json", "sub\\..\\..\\followers_1.json"])
def test_traversal_entries_are_rejected_and_never_written(tmp_path, good_zip, name):
    # (On Windows, zipfile turns "\" into "/" when reading names, so the last
    # case is caught as "..", elsewhere as a backslash: rejected either way.)
    add_raw(good_zip, name, followers_json(["mallory"]))
    before = sorted(os.listdir(tmp_path)), sorted(os.listdir(tmp_path.parent))
    result = process_export(good_zip)
    rejected = [f for f in result.files if f.action == "rejected"]
    assert len(rejected) == 1 and rejected[0].path.replace("\\", "/") == name.replace("\\", "/")
    assert [r.fields["username"].value for r in result.records["follower"]] == ["alice"]
    assert (sorted(os.listdir(tmp_path)), sorted(os.listdir(tmp_path.parent))) == before
    assert any("rejected" in p for p in result.problems)


def test_symlink_entry_is_rejected(good_zip):
    add_raw(good_zip, "followers_2.json", "/etc/passwd", external_attr=(stat.S_IFLNK | 0o777) << 16)
    with ExportArchive(good_zip) as archive:
        entry = archive.entries[-1]
        assert entry.problem == "symlink"
        with pytest.raises(MemberError):
            archive.read(entry)


def test_encrypted_entry_is_rejected(tmp_path):
    # zipfile cannot write encrypted entries, so set the "encrypted" flag bit
    # in the local and central headers of a one-entry archive.
    path = make_zip(tmp_path / "export.zip", {"followers_2.json": followers_json(["x"])})
    data = bytearray(path.read_bytes())
    for signature, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        position = data.find(signature)
        data[position + offset] |= 0x1
    path.write_bytes(bytes(data))
    result = process_export(path)
    assert [f.message for f in result.files if f.action == "rejected"] == ["the entry is encrypted"]
    assert result.records["follower"] == []


def test_duplicate_entry_name_is_rejected(good_zip):
    with pytest.warns(UserWarning):
        add_raw(good_zip, FOLLOWERS, followers_json(["mallory"]))
    result = process_export(good_zip)
    assert [f.action for f in result.files] == ["parsed", "rejected"]
    assert [r.fields["username"].value for r in result.records["follower"]] == ["alice"]


@pytest.mark.parametrize("content", [b"", b"not a zip file at all", b"PK\x03\x04" + b"\x00" * 40])
def test_corrupted_or_empty_files_raise_archive_error(tmp_path, content):
    path = tmp_path / "export.zip"
    path.write_bytes(content)
    with pytest.raises(ArchiveError):
        ExportArchive(path)


def test_truncated_zip_raises_archive_error(tmp_path, good_zip):
    data = good_zip.read_bytes()
    cut = tmp_path / "cut.zip"
    cut.write_bytes(data[: len(data) // 2])
    with pytest.raises(ArchiveError, match="damaged"):
        ExportArchive(cut)


def test_missing_file_and_folder_are_refused(tmp_path):
    with pytest.raises(ArchiveError, match="not found"):
        ExportArchive(tmp_path / "nope.zip")
    with pytest.raises(ArchiveError, match="not a regular file"):
        ExportArchive(tmp_path)


def test_damaged_member_data_is_reported_not_raised(tmp_path):
    path = make_zip(tmp_path / "export.zip", {FOLLOWERS: followers_json(["alice"])}, compression=zipfile.ZIP_STORED)
    data = bytearray(path.read_bytes())
    position = data.find(b"alice")
    data[position:position + 5] = b"XXXXX"           # CRC no longer matches
    path.write_bytes(bytes(data))
    result = process_export(path)
    assert result.files[0].action == "failed" and "damaged" in result.files[0].message
    assert result.records["follower"] == []


def test_archive_size_limit(good_zip):
    with pytest.raises(ArchiveError, match="input limit"):
        ExportArchive(good_zip, ArchiveLimits(max_archive_bytes=10))


def test_entry_count_limit(tmp_path):
    path = make_zip(tmp_path / "export.zip", {f"f{n}.txt": "x" for n in range(5)})
    with pytest.raises(ArchiveError, match="entries"):
        ExportArchive(path, ArchiveLimits(max_entries=4))


def test_total_declared_size_limit(tmp_path):
    path = make_zip(tmp_path / "export.zip", {"a.txt": "x" * 1000, "b.txt": "y" * 1000})
    with pytest.raises(ArchiveError, match="unpack"):
        ExportArchive(path, ArchiveLimits(max_total_uncompressed_bytes=1500))


def test_member_size_limit(tmp_path):
    path = make_zip(tmp_path / "export.zip", {FOLLOWERS: followers_json(["user%d" % n for n in range(200)])})
    result = process_export(path, ArchiveLimits(max_member_bytes=100))
    assert result.files[0].action == "failed" and "larger than the limit" in result.files[0].message


def test_zip_bomb_ratio_is_refused(tmp_path):
    bomb = b'[{"string_list_data": [{"value": "a"}]}' + b" " * (3 * 1024 * 1024) + b"]"
    path = make_zip(tmp_path / "export.zip", {FOLLOWERS: bomb})
    with ExportArchive(path) as archive:
        with pytest.raises(MemberError) as info:
            archive.read(archive.entries[0])
    assert info.value.code == "compression_ratio"


def test_total_read_budget(tmp_path):
    files = {f"followers_{n}.json": followers_json([f"user{n}"]) for n in range(1, 4)}
    path = make_zip(tmp_path / "export.zip", files)
    size = len(zipfile.ZipFile(path).read("followers_1.json"))
    result = process_export(path, ArchiveLimits(max_total_read_bytes=size * 2))
    assert [f.action for f in result.files] == ["parsed", "parsed", "failed"]
    assert "total read limit" in result.files[2].message


def test_nothing_is_ever_extracted(good_zip, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("extract must never be called")
    monkeypatch.setattr(zipfile.ZipFile, "extract", refuse)
    monkeypatch.setattr(zipfile.ZipFile, "extractall", refuse)
    assert len(process_export(good_zip).records["follower"]) == 1


def test_original_export_is_not_modified(good_zip):
    before = (good_zip.read_bytes(), good_zip.stat().st_mtime_ns)
    process_export(good_zip)
    assert (good_zip.read_bytes(), good_zip.stat().st_mtime_ns) == before


def test_archive_is_closed_after_use(good_zip):
    with ExportArchive(good_zip) as archive:
        pass
    with pytest.raises(ValueError):
        archive._zip.open(FOLLOWERS)


def test_timestamp_fixture_is_utc():
    assert TS == 1673778600
