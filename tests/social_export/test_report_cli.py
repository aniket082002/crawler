"""Output files and the command line, end to end, in temporary folders."""
import csv
import datetime as dt
import json

import pytest

from tests.social_export import builders as B
from webcrawler.social_export import cli
from webcrawler.social_export.records import process_export
from webcrawler.social_export.report import build_tables, write_outputs

NOW = dt.datetime(2026, 11, 19, 9, 0, 0)
ALL_FILES = ["export_comments.csv", "export_files.csv", "export_followers.csv", "export_following.csv",
             "export_likes.csv", "export_media_files.csv", "export_posts.csv", "export_profile.csv",
             "export_summary.json", "instagram_export.json", "instagram_export.xlsx"]


def read_csv(path):
    with open(path, encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


@pytest.fixture
def export_zip(tmp_path):
    return B.make_zip(tmp_path / "export.zip", B.json_export())


def run_cli(*args):
    lines = []
    code = cli.main([str(a) for a in args], out=lines.append, now=NOW)
    return code, "\n".join(lines)


def test_all_files_are_written(tmp_path, export_zip):
    output = write_outputs(process_export(export_zip), tmp_path / "out", now=NOW)
    assert output.run_dir.name == "export_20261119-090000"
    assert sorted(p.name for p in output.run_dir.iterdir()) == ALL_FILES
    assert output.errors == {}


def test_each_run_gets_a_new_folder(tmp_path, export_zip):
    result = process_export(export_zip)
    first = write_outputs(result, tmp_path / "out", now=NOW)
    second = write_outputs(result, tmp_path / "out", now=NOW)
    assert first.run_dir != second.run_dir and second.run_dir.name.endswith("-2")
    assert sorted(p.name for p in first.run_dir.iterdir()) == ALL_FILES


@pytest.mark.parametrize("formats, expected", [
    ("csv", {"export_summary.json"} | {n for n in ALL_FILES if n.endswith(".csv")}),
    ("json", {"export_summary.json", "instagram_export.json"}),
    ("xlsx", {"export_summary.json", "instagram_export.xlsx"}),
])
def test_format_selection(tmp_path, export_zip, formats, expected):
    output = write_outputs(process_export(export_zip), tmp_path / "out", formats, now=NOW)
    assert {p.name for p in output.run_dir.iterdir()} == expected


def test_csv_contents(tmp_path, export_zip):
    run_dir = write_outputs(process_export(export_zip), tmp_path / "out", "csv", now=NOW).run_dir
    followers = read_csv(run_dir / "export_followers.csv")
    assert [r["username"] for r in followers] == ["alice", "bob"]
    assert followers[0]["created_at"] == B.TS_TEXT and followers[0]["created_at_local"] == "Not supported"
    posts = read_csv(run_dir / "export_posts.csv")
    assert posts[0]["media_uris"] == "media/posts/202301/a.jpg | media/posts/202301/b.jpg"
    assert posts[1]["media_missing"] == "media/posts/202301/missing.jpg"
    profile = {r["field"]: r for r in read_csv(run_dir / "export_profile.csv")}
    assert profile["username"]["value"] == "nike.fan" and profile["website"]["status"] == "Not available"
    files = {r["path"]: r["action"] for r in read_csv(run_dir / "export_files.csv")}
    assert files["messages/inbox/someone/message_1.json"] == "not_processed"


def test_notes_explain_missing_values(tmp_path):
    path = B.make_zip(tmp_path / "e.zip", {"followers_1.json": [{"string_list_data": [{"value": "alice"}]}]})
    tables = build_tables(process_export(path))
    row = tables["followers"][1][0]
    assert row["created_at"] == "Not available" and "created_at:" in row["notes"]
    assert "created_at_local" not in row["notes"]           # "Not supported" is not repeated as a note


def test_formulas_in_export_text_are_neutralised(tmp_path):
    path = B.make_zip(tmp_path / "e.zip", {"posts_1.json": [B.post(["media/a.jpg"], caption="=HYPERLINK(1)")]})
    run_dir = write_outputs(process_export(path), tmp_path / "out", "csv,xlsx", now=NOW).run_dir
    assert read_csv(run_dir / "export_posts.csv")[0]["caption"] == "'=HYPERLINK(1)"
    openpyxl = pytest.importorskip("openpyxl")
    sheet = openpyxl.load_workbook(run_dir / "instagram_export.xlsx")["posts"]
    column = [c.value for c in sheet[1]].index("caption") + 1
    assert sheet.cell(row=2, column=column).data_type == "s"


def test_excel_sheets(tmp_path, export_zip):
    openpyxl = pytest.importorskip("openpyxl")
    run_dir = write_outputs(process_export(export_zip), tmp_path / "out", "xlsx", now=NOW).run_dir
    book = openpyxl.load_workbook(run_dir / "instagram_export.xlsx")
    assert book.sheetnames == ["profile", "posts", "comments", "likes", "followers", "following",
                               "media_files", "files"]


def test_json_document_and_summary(tmp_path, export_zip):
    run_dir = write_outputs(process_export(export_zip), tmp_path / "out", "json", now=NOW).run_dir
    document = json.loads((run_dir / "instagram_export.json").read_text(encoding="utf-8"))
    assert document["module"] == "social_export" and document["schema_version"] == 1
    assert document["input"]["file_name"] == "export.zip" and len(document["input"]["sha256"]) == 64
    assert len(document["records"]["follower"]) == 2 and document["limitations"]
    summary = json.loads((run_dir / "export_summary.json").read_text(encoding="utf-8"))
    assert summary["summary"]["records"]["post"] == 2
    assert summary["output_files"] == ["export_summary.json", "instagram_export.json"]


def test_cli_end_to_end(tmp_path, export_zip):
    before = export_zip.read_bytes()
    code, text = run_cli(export_zip, "--output-root", tmp_path / "out")
    assert code == 0
    assert "follower    : 2" in text and "Instagram was not contacted" in text
    assert (tmp_path / "out" / "export_20261119-090000" / "instagram_export.json").is_file()
    assert export_zip.read_bytes() == before


def test_cli_dry_run_writes_nothing(tmp_path, export_zip):
    code, text = run_cli(export_zip, "--dry-run", "--output-root", tmp_path / "out")
    assert code == 0 and "no files were written" in text
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("content", [b"garbage", b""])
def test_cli_corrupted_zip(tmp_path, content):
    path = tmp_path / "bad.zip"
    path.write_bytes(content)
    code, text = run_cli(path, "--output-root", tmp_path / "out")
    assert code == 1 and text.startswith("ERROR:")
    assert not (tmp_path / "out").exists()


def test_cli_missing_file(tmp_path):
    code, text = run_cli(tmp_path / "nope.zip", "--output-root", tmp_path / "out")
    assert code == 1 and "not found" in text


def test_cli_size_limit(tmp_path, export_zip):
    big = tmp_path / "big.zip"
    big.write_bytes(export_zip.read_bytes() + b"\x00" * (2 * 1024 * 1024))
    code, text = run_cli(big, "--max-archive-mb", "1", "--output-root", tmp_path / "out")
    assert code == 1 and "limit" in text


@pytest.mark.parametrize("args", [["--formats", "pdf"], ["--max-archive-mb", "0"], ["--max-file-mb", "0"]])
def test_cli_bad_options(tmp_path, export_zip, args):
    code, _ = run_cli(export_zip, *args, "--output-root", tmp_path / "out")
    assert code == 1


def test_cli_usage_error():
    with pytest.raises(SystemExit) as info:
        cli.main([], out=lambda *a: None)
    assert info.value.code == 2


def test_cli_reports_problems(tmp_path):
    path = B.make_zip(tmp_path / "e.zip", {"followers_1.json": b"{broken"})
    B.add_raw(path, "../evil.json", "{}")
    code, text = run_cli(path, "--dry-run")
    assert code == 0 and "Problems                 : 2" in text and "path traversal" in text


def test_default_output_root_is_inside_outputs():
    root = cli.default_output_root()
    assert root.parts[-2:] == ("outputs", "social")
