"""Defensive file handling: input checks, bounded reading, output folders."""
import csv
import datetime as dt
import io

import pytest

from webcrawler.social_common import safe_io as IO
from webcrawler.social_common.safe_io import InputError

NOW = dt.datetime(2026, 11, 18, 9, 30, 15)


# --- input files ---------------------------------------------------------------------
def test_check_input_file_accepts_a_regular_file(tmp_path):
    path = tmp_path / "items.csv"
    path.write_bytes(b"url\n")
    assert IO.check_input_file(path) == path
    assert IO.check_input_file(str(path), max_bytes=4) == path       # exactly at the limit


@pytest.mark.parametrize("size, text", [(4, "4 bytes"), (1023, "1023 bytes"), (1024 * 1024, "1 MB"),
                                        (64 * 1024 * 1024, "64 MB"), (2048 * 1024 * 1024, "2048 MB")])
def test_format_size(size, text):
    assert IO.format_size(size) == text


def test_check_input_file_missing(tmp_path):
    with pytest.raises(InputError, match="not found"):
        IO.check_input_file(tmp_path / "nope.csv")


def test_check_input_file_folder(tmp_path):
    with pytest.raises(InputError, match="not a regular file"):
        IO.check_input_file(tmp_path)


def test_check_input_file_too_large(tmp_path):
    path = tmp_path / "big.json"
    path.write_bytes(b"x" * 11)
    with pytest.raises(InputError, match="larger than the 10 bytes input limit"):
        IO.check_input_file(path, max_bytes=10)


def test_input_error_message_has_no_path(tmp_path):
    with pytest.raises(InputError) as error:
        IO.check_input_file(tmp_path / "secret-folder-name" / "x.csv")
    assert "secret-folder-name" not in str(error.value)


# --- bounded line reading ---------------------------------------------------------------
def lines(data: bytes, limit: int):
    return list(IO.iter_bounded_lines(io.BytesIO(data), limit))


def test_bounded_lines_normal():
    assert lines(b"a\nbb\nccc", 10) == [(1, b"a\n"), (2, b"bb\n"), (3, b"ccc")]
    assert lines(b"a\r\nb\r\n", 10) == [(1, b"a\r\n"), (2, b"b\r\n")]
    assert lines(b"", 10) == []
    assert lines(b"\n\n", 10) == [(1, b"\n"), (2, b"\n")]


def test_bounded_lines_skip_a_long_line_and_keep_going():
    data = b"short\n" + b"x" * 100_000 + b"\nafter\n"
    assert lines(data, 10) == [(1, b"short\n"), (2, None), (3, b"after\n")]


def test_bounded_lines_exact_boundary():
    assert lines(b"12345\nnext\n", 5) == [(1, b"12345\n"), (2, b"next\n")]       # 5 bytes + newline: allowed
    assert lines(b"123456\nnext\n", 5) == [(1, None), (2, b"next\n")]            # 6 bytes: skipped
    assert lines(b"12345", 5) == [(1, b"12345")]                                 # last line, no newline
    assert lines(b"123456", 5) == [(1, None)]


def test_bounded_lines_long_last_line_without_newline():
    assert lines(b"ok\n" + b"y" * 500_000, 100) == [(1, b"ok\n"), (2, None)]


def test_bounded_lines_never_hold_more_than_the_limit():
    """The reader asks the stream for at most limit+1 bytes at a time."""
    requested = []

    class Spy(io.BytesIO):
        def readline(self, size=-1):
            requested.append(size)
            return super().readline(size)

    list(IO.iter_bounded_lines(Spy(b"a" * 10_000_000 + b"\nb\n"), 1000))
    assert requested and all(0 < size <= 64 * 1024 for size in requested)


@pytest.mark.parametrize("limit", [0, -1])
def test_bounded_lines_need_a_positive_limit(limit):
    with pytest.raises(ValueError):
        list(IO.iter_bounded_lines(io.BytesIO(b"x"), limit))


# --- CSV field limit -----------------------------------------------------------------------
def test_csv_field_limit_is_restored():
    before = csv.field_size_limit()
    with IO.csv_field_limit(5_000_000):
        assert csv.field_size_limit() == 5_000_000
        with IO.csv_field_limit(1000):
            assert csv.field_size_limit() == 1000
        assert csv.field_size_limit() == 5_000_000
    assert csv.field_size_limit() == before


def test_csv_field_limit_is_restored_after_an_error():
    before = csv.field_size_limit()
    with pytest.raises(RuntimeError):
        with IO.csv_field_limit(123_456):
            raise RuntimeError("boom")
    assert csv.field_size_limit() == before


def test_default_csv_limit_covers_the_crawlers_largest_cell():
    assert IO.DEFAULT_CSV_FIELD_CHARS > 200_000 > 131_072      # page text limit vs Python's default


# --- output folders ---------------------------------------------------------------------------
def test_new_run_dir_creates_a_fresh_folder(tmp_path):
    root = tmp_path / "outputs" / "social"            # parents are created
    first = IO.new_run_dir(root, "discovery", NOW)
    assert first == root / "discovery_20261118-093015" and first.is_dir()


def test_new_run_dir_never_reuses_a_folder(tmp_path):
    first = IO.new_run_dir(tmp_path, "discovery", NOW)
    (first / "keep.txt").write_text("earlier result", encoding="utf-8")
    second = IO.new_run_dir(tmp_path, "discovery", NOW)
    third = IO.new_run_dir(tmp_path, "discovery", NOW)
    assert [p.name for p in (first, second, third)] == [
        "discovery_20261118-093015", "discovery_20261118-093015-2", "discovery_20261118-093015-3"]
    assert (first / "keep.txt").read_text(encoding="utf-8") == "earlier result"
    assert list(second.iterdir()) == []


def test_new_run_dir_uses_the_current_time_by_default(tmp_path):
    created = IO.new_run_dir(tmp_path, "discovery")
    assert created.name.startswith("discovery_20") and created.is_dir()


@pytest.mark.parametrize("prefix", ["", "Discovery", "../x", "a/b", "a b", "1abc", "x" * 60, None, "run-1"])
def test_new_run_dir_rejects_unsafe_prefixes(tmp_path, prefix):
    with pytest.raises(ValueError):
        IO.new_run_dir(tmp_path, prefix, NOW)
    assert list(tmp_path.iterdir()) == []


def test_new_run_dir_fails_cleanly_when_root_is_a_file(tmp_path):
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(OSError):
        IO.new_run_dir(blocker, "discovery", NOW)


def test_output_file_stays_inside_the_run_folder(tmp_path):
    run_dir = IO.new_run_dir(tmp_path, "discovery", NOW)
    path = IO.output_file(run_dir, "instagram_profiles.csv")
    assert path.parent == run_dir.resolve() and path.name == "instagram_profiles.csv"


@pytest.mark.parametrize("name", ["../escape.csv", "..\\escape.csv", "a/b.csv", "a\\b.csv", "/etc/passwd",
                                  "C:\\Windows\\x.csv", "C:evil.csv", "", ".hidden", "x..csv", "with space.csv",
                                  "name\n.csv", "x" * 200 + ".csv", None, 5, "nul\x00.csv", "caf\u00e9.csv"])
def test_output_file_rejects_unsafe_names(tmp_path, name):
    with pytest.raises(ValueError):
        IO.output_file(tmp_path, name)


def test_output_file_reuses_the_crawlers_path_check():
    from webcrawler import url_validator
    assert IO.safe_output_path is url_validator.safe_output_path
