"""Discovery: normalising, de-duplicating and keeping every source."""
import pytest

from tests.social_discovery.builders import (
    IG, LEGACY_COLUMNS, STAMP, STAMP_UTC, legacy_page, page, write_csv, write_json,
)
from webcrawler.social_discovery import discover as D
from webcrawler.social_discovery.discover import discover_paths, page_identity, page_timestamp
from webcrawler.social_discovery.reader import resolve_inputs


def run(tmp_path, records, name="items.json", **kwargs):
    writer = write_json if name.endswith(".json") else write_csv
    return discover_paths([writer(tmp_path / name, records)], **kwargs)


def usernames(result):
    return [p.username for p in result.profiles]


# --- de-duplication -------------------------------------------------------------------
def test_one_profile_however_the_link_is_written(tmp_path):
    records = [
        page("https://acme.example/", social=[f"{IG}/Nike"],
             links=[f"{IG}/Nike/", "http://instagram.com/nike", "https://m.instagram.com/NIKE?hl=en"]),
        page("https://acme.example/contact", links=["https://instagram.com/nike/#follow"]),
    ]
    result = run(tmp_path, records)
    assert usernames(result) == ["nike"]
    profile = result.profiles[0]
    assert profile.profile_url == "https://www.instagram.com/nike/"
    assert len(profile.sources) == 2                        # one per page, not one per spelling
    first = profile.sources[0]
    assert first.found_in == ["social_profiles", "external_links", "links"]
    assert first.link_as_found == f"{IG}/Nike/"             # the page's own spelling is kept
    assert profile.sources[1].link_as_found == "https://instagram.com/nike/#follow"


def test_mixed_case_usernames_are_one_profile_across_files(tmp_path):
    a = write_json(tmp_path / "a.json", [page("https://one.example/", links=[f"{IG}/OPositive.IO"])])
    b = write_csv(tmp_path / "b.csv", [page("https://two.example/", links=["https://instagram.com/opositive.io/"])])
    result = discover_paths([a, b])
    assert usernames(result) == ["opositive.io"]
    assert result.profiles[0].websites() == ["https://one.example", "https://two.example"]


def test_every_source_page_and_website_is_kept(tmp_path):
    records = [page(f"https://acme.example/page-{n}", links=[f"{IG}/nike/"]) for n in range(5)]
    records += [page("https://shop.acme.example/", links=[f"{IG}/nike"]),
                page("http://other.example:8080/about", links=[f"{IG}/nike"])]
    profile = run(tmp_path, records).profiles[0]
    assert len(profile.sources) == 7
    assert profile.page_urls() == [r["url"] for r in records]
    assert profile.websites() == ["http://other.example:8080", "https://acme.example", "https://shop.acme.example"]
    assert [s.input_record for s in profile.sources] == [1, 2, 3, 4, 5, 6, 7]


def test_same_link_twice_on_one_page_is_one_source(tmp_path):
    records = [page("https://acme.example/", links=[f"{IG}/nike/", f"{IG}/nike/", f"{IG}/nike"])]
    assert len(run(tmp_path, records).profiles[0].sources) == 1


def test_same_page_listed_twice_in_one_file_is_merged(tmp_path):
    records = [page("https://acme.example/", social=[f"{IG}/nike"]),
               page("https://ACME.example/#top", links=[f"{IG}/Nike/"])]     # the same page, written differently
    profile = run(tmp_path, records).profiles[0]
    assert len(profile.sources) == 1
    assert profile.sources[0].found_in == ["social_profiles", "external_links", "links"]
    assert profile.sources[0].link_as_found == f"{IG}/Nike/"
    assert profile.sources[0].source_page_url == "https://acme.example/"     # as first recorded


def test_same_page_in_two_crawl_outputs_stays_two_sources(tmp_path):
    first = write_json(tmp_path / "run1.json", [page("https://acme.example/", links=[f"{IG}/nike/"],
                                                     stamp="2026-11-12T10:00:00+00:00")])
    second = write_json(tmp_path / "run2.json", [page("https://acme.example/", links=[f"{IG}/nike/"],
                                                      stamp="2026-11-17T10:00:00+00:00")])
    profile = discover_paths([first, second]).profiles[0]
    assert [s.input_file for s in profile.sources] == [first.as_posix(), second.as_posix()]
    assert profile.page_urls() == ["https://acme.example/"]
    assert profile.first_discovered_at() == "2026-11-12T10:00:00Z"
    assert profile.last_discovered_at() == "2026-11-17T10:00:00Z"


def test_several_profiles_are_sorted_by_username(tmp_path):
    records = [page("https://acme.example/", links=[f"{IG}/zara/", f"{IG}/Adidas/", f"{IG}/nike/"]),
               page("https://acme.example/b", links=[f"{IG}/adidas/"])]
    result = run(tmp_path, records)
    assert usernames(result) == ["adidas", "nike", "zara"]
    assert [len(p.sources) for p in result.profiles] == [2, 1, 1]


# --- rejected links -----------------------------------------------------------------------
def test_posts_reels_stories_and_explore_links_are_never_profiles(tmp_path):
    links = [f"{IG}/p/CxYz123/", f"{IG}/reel/Cabc/", f"{IG}/stories/nike/1/", f"{IG}/explore/tags/run/",
             f"{IG}/accounts/login/", f"{IG}/nike/tagged/", "https://help.instagram.com/1", f"{IG}/",
             f"{IG}/ni-ke/", "instagram://user?username=nike", f"{IG}/nike/p/Cdef/"]
    result = run(tmp_path, [page("https://acme.example/", links=links)])
    assert result.profiles == []
    assert result.rejected_by_reason() == {
        "app_link": 1, "explore_url": 1, "invalid_username": 1, "no_username": 1, "non_profile_host": 1,
        "non_profile_page": 1, "post_url": 2, "profile_subpage": 1, "reel_url": 1, "story_url": 1}
    assert result.summary()["rejected_links"] == 11 and result.summary()["profiles_found"] == 0
    post = next(r for r in result.rejected if r.link == f"{IG}/p/CxYz123/")
    assert post.reason == "post_url" and "Post URL" in post.reason_text
    assert post.first_source_page_url == "https://acme.example/" and post.page_count == 1


def test_rejected_link_counts_pages_not_spellings(tmp_path):
    # The crawler treats a reel URL as a "social profile", so it appears in several fields and forms.
    records = [page(f"https://acme.example/{n}", social=[f"{IG}/reel/Cabc"],
                    links=[f"{IG}/reel/Cabc/", f"{IG}/reel/Cabc/?utm_source=x"]) for n in range(3)]
    result = run(tmp_path, records)
    assert len(result.rejected) == 1
    assert result.rejected[0].page_count == 3 and result.rejected[0].reason == "reel_url"


def test_other_links_are_ignored_silently(tmp_path):
    links = ["https://example.org/blog/instagram-tips", "https://twitter.com/acme", "mailto:press@instagram.com",
             "https://www.facebook.com/sharer.php?u=https://instagram.com/nike", "https://evilinstagram.com/nike"]
    result = run(tmp_path, [page("https://acme.example/", social=["https://twitter.com/acme"], links=links)])
    assert result.profiles == [] and result.rejected == [] and result.problems() == []


def test_profile_and_rejected_links_on_the_same_page(tmp_path):
    result = run(tmp_path, [page("https://acme.example/", links=[f"{IG}/nike/", f"{IG}/p/CxYz/"])])
    assert usernames(result) == ["nike"] and result.rejected_by_reason() == {"post_url": 1}
    assert result.pages_with_profile_links == 1


def test_tracking_of_rejected_links_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(D, "MAX_REJECTED_TRACKED", 3)
    links = [f"{IG}/p/post{n}/" for n in range(5)]
    result = run(tmp_path, [page("https://acme.example/", links=links), page("https://acme.example/b", links=links)])
    assert len(result.rejected) == 3 and all(r.page_count == 2 for r in result.rejected)
    assert result.rejected_not_tracked == 4             # 2 untracked links x 2 pages
    assert result.summary()["rejected_links_not_tracked"] == 4


def test_very_long_rejected_link_is_cut_for_display(tmp_path):
    long_link = f"{IG}/nike/?" + "a" * 5000
    result = run(tmp_path, [page("https://acme.example/", links=[long_link])])
    assert result.rejected[0].reason == "too_long"
    assert len(result.rejected[0].link) < 2100 and result.rejected[0].link.endswith("...[cut]")


# --- missing fields are reported, never invented ------------------------------------------------
def test_missing_page_url(tmp_path):
    records = [page("x", links=[f"{IG}/nike/"]), page("x", links=[f"{IG}/nike/"])]
    for record in records:
        del record["url"]
    profile = run(tmp_path, records).profiles[0]
    assert len(profile.sources) == 2                     # two records, so two sightings
    for source in profile.sources:
        assert source.source_page_url is None and source.source_website is None
        assert source.unavailable == {"source_page_url": D.REASON_PAGE_MISSING,
                                      "source_website": D.REASON_PAGE_MISSING}
        assert source.discovered_at == STAMP_UTC
    assert profile.page_urls() == [] and profile.websites() == []
    assert profile.first_source() is profile.sources[0]


@pytest.mark.parametrize("url, reason", [
    ("", D.REASON_PAGE_MISSING), ("   ", D.REASON_PAGE_MISSING), (None, D.REASON_PAGE_MISSING),
    ("not a url", D.REASON_PAGE_INVALID), ("javascript:void(0)", D.REASON_PAGE_INVALID),
    ("ftp://acme.example/file", D.REASON_PAGE_INVALID), ("//acme.example/", D.REASON_PAGE_INVALID),
    ("https://", D.REASON_PAGE_INVALID), ("https://acme.example/a b", D.REASON_PAGE_INVALID),
    ("https://acme.example/\x00", D.REASON_PAGE_INVALID), ("https://acme.example/\ud83d", D.REASON_PAGE_INVALID),
    ("https://acme.example:port/", D.REASON_PAGE_INVALID), ("https://" + "a" * 9000 + ".example/", D.REASON_PAGE_INVALID),
    ("https://user:secret@acme.example/page", D.REASON_PAGE_CREDENTIALS),
    ("https://user@acme.example/", D.REASON_PAGE_CREDENTIALS),
])
def test_unusable_page_urls(url, reason):
    assert page_identity(url) == (None, None, reason)


@pytest.mark.parametrize("url, website", [
    ("https://acme.example/", "https://acme.example"),
    ("https://Shop.ACME.example:443/a/b?x=1#f", "https://shop.acme.example"),
    ("http://acme.example:80/", "http://acme.example"),
    ("http://acme.example:8080/x", "http://acme.example:8080"),
    ("https://acme.example:8443", "https://acme.example:8443"),
    ("HTTPS://acme.example/x", "https://acme.example"),
    ("http://127.0.0.1:8000/page", "http://127.0.0.1:8000"),
    ("http://[::1]:8080/x", "http://[::1]:8080"),
    ("  https://acme.example/trim  ", "https://acme.example"),
])
def test_source_website_is_the_site_of_the_page(url, website):
    page_url, site, reason = page_identity(url)
    assert (page_url, site, reason) == (url.strip(), website, None)


def test_credentials_in_a_page_url_never_reach_the_result(tmp_path):
    records = [page("https://user:secret@acme.example/page", links=[f"{IG}/nike/"])]
    result = run(tmp_path, records)
    source = result.profiles[0].sources[0]
    assert source.source_page_url is None and source.unavailable["source_page_url"] == D.REASON_PAGE_CREDENTIALS
    assert "secret" not in repr(result)


@pytest.mark.parametrize("raw, expected", [
    (STAMP, (STAMP_UTC, None)),
    ("2026-11-13T10:05:25+05:30", ("2026-11-13T04:35:25Z", None)),
    ("2026-11-13T04:35:25", ("2026-11-13T04:35:25Z", None)),
    (None, (None, D.REASON_TIME_MISSING)), ("", (None, D.REASON_TIME_MISSING)), ("  ", (None, D.REASON_TIME_MISSING)),
    ("yesterday", (None, D.REASON_TIME_INVALID)), ("2026-99-99T00:00:00", (None, D.REASON_TIME_INVALID)),
    ("=NOW()", (None, D.REASON_TIME_INVALID)), ("2026-11-13", (None, D.REASON_TIME_INVALID)),
])
def test_page_timestamp(raw, expected):
    assert page_timestamp(raw) == expected


def test_missing_and_invalid_timestamps(tmp_path):
    records = [page("https://acme.example/a", links=[f"{IG}/nike/"], stamp=""),
               page("https://acme.example/b", links=[f"{IG}/nike/"], stamp="not a time"),
               page("https://acme.example/c", links=[f"{IG}/nike/"], stamp="2026-11-14T08:00:00+00:00"),
               page("https://acme.example/d", links=[f"{IG}/nike/"], stamp="2026-11-12T08:00:00+00:00")]
    profile = run(tmp_path, records).profiles[0]
    assert [s.discovered_at for s in profile.sources] == [None, None, "2026-11-14T08:00:00Z", "2026-11-12T08:00:00Z"]
    assert profile.sources[0].unavailable == {"discovered_at": D.REASON_TIME_MISSING}
    assert profile.sources[1].unavailable == {"discovered_at": D.REASON_TIME_INVALID}
    assert profile.sources[2].unavailable == {}
    assert profile.first_discovered_at() == "2026-11-12T08:00:00Z"
    assert profile.last_discovered_at() == "2026-11-14T08:00:00Z"
    assert profile.first_source().source_page_url == "https://acme.example/d"     # the earliest dated page


def test_profile_with_no_dates_at_all(tmp_path):
    profile = run(tmp_path, [page("https://acme.example/", links=[f"{IG}/nike/"], stamp="")]).profiles[0]
    assert profile.first_discovered_at() is None and profile.last_discovered_at() is None
    assert profile.first_source().source_page_url == "https://acme.example/"


def test_older_output_gives_no_profiles_and_says_why(tmp_path):
    old = write_csv(tmp_path / "items.csv", [legacy_page("https://old.example/")], columns=LEGACY_COLUMNS)
    result = discover_paths([old])
    assert result.profiles == [] and result.summary()["inputs_read"] == 1
    assert "older version of the crawler" in result.problems()[0]


# --- inputs ---------------------------------------------------------------------------------------
def make_run(folder, records, json=True, csv=True):
    folder.mkdir(parents=True)
    if json:
        write_json(folder / "items.json", records)
    if csv:
        write_csv(folder / "items.csv", records)
    return folder


def test_one_bad_input_does_not_stop_the_others(tmp_path):
    good = write_json(tmp_path / "good.json", [page("https://acme.example/", links=[f"{IG}/nike/"])])
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_bytes(b"this is not json")
    spreadsheet = tmp_path / "items.xlsx"
    spreadsheet.write_bytes(b"PK")
    result = discover_paths([tmp_path / "missing.json", corrupt, spreadsheet, good])
    assert usernames(result) == ["nike"]
    statuses = {r.path.rsplit("/", 1)[-1]: r.status for r in result.inputs}
    assert statuses == {"missing.json": "failed", "items.xlsx": "failed", "corrupt.json": "failed", "good.json": "ok"}
    summary = result.summary()
    assert (summary["inputs_given"], summary["inputs_read"], summary["inputs_with_problems"]) == (4, 1, 3)
    assert any("unsupported input type" in m for m in result.problems())


def test_folder_prefers_json_and_falls_back_to_csv(tmp_path):
    folder = make_run(tmp_path / "run_1", [page("https://acme.example/", links=[f"{IG}/nike/"])])
    result = discover_paths([folder])
    assert [r.path.rsplit("/", 1)[-1] for r in result.inputs] == ["items.json"]
    (folder / "items.json").write_bytes(b"corrupt")
    result = discover_paths([folder])
    assert usernames(result) == ["nike"]
    assert [(r.path.rsplit("/", 1)[-1], r.status) for r in result.inputs] == [("items.json", "failed"), ("items.csv", "ok")]
    assert "read instead of" in result.inputs[1].problems[0]


def test_json_and_csv_of_the_same_crawl_give_the_same_profiles(tmp_path):
    records = [page("https://acme.example/", social=[f"{IG}/nike"], links=[f"{IG}/nike/", f"{IG}/p/CxYz/"]),
               page("https://acme.example/b", links=[f"{IG}/Adidas"])]
    folder = make_run(tmp_path / "run_1", records)
    from_json = discover_paths([folder], input_format="json")
    from_csv = discover_paths([folder], input_format="csv")
    assert usernames(from_json) == usernames(from_csv) == ["adidas", "nike"]
    assert from_json.rejected_by_reason() == from_csv.rejected_by_reason() == {"post_url": 1}
    assert [p.page_urls() for p in from_json.profiles] == [p.page_urls() for p in from_csv.profiles]


def test_all_runs_reads_the_folder_and_its_run_folders(tmp_path):
    outputs = tmp_path / "outputs"
    make_run(outputs, [legacy_page("https://old.example/")])
    make_run(outputs / "run_20261113-100519", [page("https://acme.example/", links=[f"{IG}/nike/"])])
    make_run(outputs / "run_20261117-152859", [page("https://acme.example/", links=[f"{IG}/Nike"])], json=False)
    (outputs / "run_20261118-000000").mkdir()                    # a run that produced nothing
    make_run(outputs / "social", [page("https://x.example/", links=[f"{IG}/ignored/"])])    # not a run folder
    result = discover_paths([outputs], all_runs=True)
    assert usernames(result) == ["nike"] and len(result.profiles[0].sources) == 2
    names = [r.path[len(outputs.as_posix()) + 1:] for r in result.inputs]
    assert names == ["run_20261118-000000", "items.json", "run_20261113-100519/items.json",
                     "run_20261117-152859/items.csv"]
    assert result.inputs[0].status == "skipped"
    without = discover_paths([outputs])
    assert without.profiles == [] and len(without.inputs) == 1


def test_resolve_inputs_reports_what_it_cannot_use(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    text = tmp_path / "notes.txt"
    text.write_text("x", encoding="utf-8")
    csv_only = make_run(tmp_path / "csv_only", [page("https://acme.example/")], json=False)
    specs, reports = resolve_inputs([empty, text, tmp_path / "gone", csv_only], input_format="json")
    assert specs == []
    assert [(r.status, r.problems[0]) for r in reports] == [
        ("failed", "no crawler output (items.json) was found in this folder"),
        ("failed", "unsupported input type: only the crawler's .json and .csv output files can be read"),
        ("failed", "the path was not found"),
        ("failed", "no crawler output (items.json) was found in this folder")]
    specs, reports = resolve_inputs([csv_only / "items.csv"], input_format="json")
    assert specs == [] and reports[0].status == "skipped"
    with pytest.raises(ValueError):
        resolve_inputs([csv_only], input_format="xml")


def test_the_same_file_given_twice_is_read_once(tmp_path):
    folder = make_run(tmp_path / "run_1", [page("https://acme.example/", links=[f"{IG}/nike/"])], csv=False)
    result = discover_paths([folder, folder / "items.json", str(folder / "items.json")])
    assert len(result.inputs) == 1 and len(result.profiles[0].sources) == 1


# --- summary ---------------------------------------------------------------------------------------
def test_summary_counts(tmp_path):
    records = [page("https://acme.example/", links=[f"{IG}/nike/", f"{IG}/adidas/", f"{IG}/p/CxYz/"]),
               page("https://acme.example/b", links=[f"{IG}/nike/"]),
               page("https://other.example/", links=["https://twitter.com/x"])]
    summary = run(tmp_path, records).summary()
    assert summary == {
        "inputs_given": 1, "inputs_read": 1, "inputs_with_problems": 0, "page_records_read": 3,
        "pages_with_profile_links": 2, "profiles_found": 2, "source_entries": 3, "source_websites": 1,
        "rejected_links": 1, "rejected_links_by_reason": {"post_url": 1}, "rejected_links_not_tracked": 0,
        "problems": 0}


def test_footer_link_on_every_page(tmp_path):
    records = [page(f"https://acme.example/p{n}", social=[f"{IG}/acme"], links=[f"{IG}/acme/"]) for n in range(300)]
    result = run(tmp_path, records)
    assert usernames(result) == ["acme"] and len(result.profiles[0].sources) == 300
    assert result.summary()["pages_with_profile_links"] == 300


def test_discovery_does_not_change_its_input_files(tmp_path):
    path = write_json(tmp_path / "items.json", [page("https://acme.example/", links=[f"{IG}/nike/"])])
    before = (path.read_bytes(), path.stat().st_mtime_ns, sorted(p.name for p in tmp_path.iterdir()))
    discover_paths([path])
    assert (path.read_bytes(), path.stat().st_mtime_ns, sorted(p.name for p in tmp_path.iterdir())) == before
