# Module B - Instagram Export Parser

Reads the "Download Your Information" ZIP file that you download from **your own** Instagram
account and exports its contents as CSV, JSON and Excel. Everything happens on this computer:
the module contains no network code, never contacts Instagram, and needs no login, cookie or
token.

## How to run it

From the project folder:

```
.venv\Scripts\python.exe -m webcrawler.social_export C:\path\to\instagram-export.zip
.venv\Scripts\python.exe -m webcrawler.social_export C:\path\to\instagram-export.zip --dry-run
.venv\Scripts\python.exe -m webcrawler.social_export C:\path\to\instagram-export.zip --formats csv,json
.venv\Scripts\python.exe -m webcrawler.social_export C:\path\to\instagram-export.zip --output-root D:\results
```

| Option | Meaning | Default |
|---|---|---|
| `EXPORT_ZIP` | The export ZIP file. It is only read, never changed. | required |
| `--formats` | `csv`, `json`, `xlsx`, separated by commas | all three |
| `--output-root DIR` | Folder that receives the new result folder | `outputs\social` in the project |
| `--dry-run` | Show the summary and write nothing | off |
| `--max-archive-mb MB` | Largest ZIP file that will be opened | 8192 |
| `--max-file-mb MB` | Largest single JSON or HTML file inside the ZIP that will be read | 64 |

Exit codes: 0 finished; 1 the ZIP could not be used, an option was invalid, or an output file
could not be written; 2 the command line was not understood.

The module runs with the Python on this computer. It needs no new package: Excel output uses
`xlsxwriter`, which the project already requires. It is not part of the Docker image and is not
connected to the crawler launcher or the browser interface.

## Supported input

Instagram has used several folder layouts. Files are recognised **by file name**, wherever they
are in the archive, including under an extra top-level folder.

| Category | Files (`.json`, `.html` or `.htm`) | Record type |
|---|---|---|
| Profile | `personal_information` | `profile` |
| Posts | `posts_N`, `posts`, `reels`, `stories`, `igtv_videos`, `archived_posts` | `post` (with `post_type`) |
| Comments | `post_comments_N`, `reels_comments`, `comments` | `comment` |
| Likes | `liked_posts`, `liked_comments` | `like` |
| Followers | `followers_N`, `followers` | `follower` |
| Following | `following` | `following` |
| Media | image, video and audio files (`jpg`, `png`, `webp`, `heic`, `mp4`, `mov`, `m4a`, ...) | `media_file` (listed only) |

**JSON** files are parsed in the known layouts (top-level lists, and wrappers such as
`relationships_followers`, `relationships_following`, `likes_media_likes`, `comments_media_comments`,
`ig_reels_media`, `ig_stories`, `profile_user`). Instagram's JSON stores accented characters and
emoji as one character per UTF-8 byte; such text is repaired when it decodes cleanly and kept
unchanged otherwise. Unix timestamps become UTC (`2023-01-15T10:30:00Z`).

**HTML** files are parsed by looking for Instagram's entry blocks, links, images, table rows and
dates (`Jan 15, 2023, 10:30 am`). HTML dates have no time zone, so they are kept as local time
in `created_at_local` and `created_at` is marked `Not supported` with the reason. HTML support
is best-effort: a page in an unrecognised layout is reported as failed rather than guessed.

Everything else (messages, saved items, ads information, security logs ...) is listed in the
file inventory as `not_processed`. Not every export contains every category; missing
categories simply produce no records.

## Architecture

```
webcrawler/social_export/
  archive.py   safe, read-only ZIP inspection: limits, suspicious entries, bounded in-memory reads
  htmldoc.py   tolerant, size-bounded HTML tree built with html.parser
  parsers.py   file recognition and parsing (JSON and HTML) into ParsedItem objects
  records.py   ParsedItem -> Record (field statuses, provenance), media matching, de-duplication
  report.py    CSV / JSON / Excel files in a new folder
  cli.py       command line; __main__.py makes "python -m" work
```

It reuses `webcrawler.social_common`: field records and statuses (`schema.py`), username checks
(`identity.py`), the CSV/JSON/Excel writers with their spreadsheet guards (`exporters.py`), and
the new-folder-per-run and file-name checks (`safe_io.py`). It does not use the crawler, Module C
or the old Instagram API prototype.

## Record structure

Every record has `record_type`, `module` (`social_export`), `platform`, `dedup_key` and
`fields`. Each field holds `value`, `status`, `source` (file and entry), `extracted_at` and
`reason`:

| Status | When |
|---|---|
| `Available` | The value was in the file (an empty caption is a real, empty value) |
| `Not available` | The file does not contain it |
| `Parsing failed` | It was there but unreadable, for example an invalid timestamp or username |
| `Not supported` | The file's format cannot carry it (UTC time in HTML, local time in JSON) |

Each record also carries `source_file`, `source_format`, `location` (entry number),
`extraction_status` (`complete` or `partial`), `extracted_at`, `warnings`,
`duplicates_merged` and `also_found_in`.

Fields per type:

| Type | Fields |
|---|---|
| profile | `username`, `name`, `bio`, `website`, `profile_photo_uri`, plus every other field the profile file contains |
| post | `post_type`, `caption`, `created_at`, `created_at_local`, `media_uris`, `media_count`, `media_missing` |
| comment | `comment_kind`, `comment_text`, `media_owner`, `created_at`, `created_at_local` |
| like | `like_kind`, `media_owner`, `target_url`, `reaction`, `created_at`, `created_at_local` |
| follower / following | `username`, `profile_url`, `username_as_found`, `created_at`, `created_at_local` |
| media_file | `path`, `size_bytes`, `media_kind`, `referenced_by` |

**De-duplication.** Followers and following are keyed by lowercase username, the profile by
username, other records by their content. A repeated item (for example the same follower in
`followers_1.json` and `followers_2.json`) is kept once; the record says how many copies were
merged and where they were. The same account in followers and following stays as two records.

**Media references.** Each post lists its media paths. `media_missing` names the ones that are
not in the archive. Each media file record counts how many references point to it.

## Output folder

Every run creates a new folder; nothing is overwritten:

```
outputs\social\export_<YYYYMMDD>-<HHMMSS>\
  instagram_export.json     input (name, size, SHA-256), summary, limitations, file inventory,
                            every record with all fields, problems
  export_profile.csv        one row per profile field: field, value, status, notes, ...
  export_posts.csv
  export_comments.csv
  export_likes.csv
  export_followers.csv
  export_following.csv
  export_media_files.csv
  export_files.csv          every archive entry: size, category, format, action, records, message
  instagram_export.xlsx     the same eight tables as worksheets
  export_summary.json       counts, files written, export errors (always written)
```

In CSV and Excel a missing value shows its status label, and `notes` gives the reason. CSV
cells starting with `=`, `+`, `-` or `@` get a leading `'`; Excel cells are always plain text,
never formulas or links. The `files` table uses these actions: `parsed`,
`parsed_with_warnings`, `failed`, `rejected`, `media_listed`, `not_processed`.

## Security protections

| Protection | Detail |
|---|---|
| Offline | No network code; tests block the network and check that no network module is loaded |
| No extraction | Nothing from the ZIP is ever written to disk. Parsed files are read into memory; media files are only listed. Path traversal therefore cannot write anywhere. |
| Suspicious entries rejected | Absolute paths, drive letters, `..` segments, backslashes, control characters, overlong names, symbolic links, encrypted entries, duplicate names |
| Archive limits | ZIP file size (8 GB), number of entries (200,000), total declared uncompressed size (64 GB) |
| Decompression limits | 64 MB per parsed file, 1 GB read in total, compression ratio above 200:1 refused for files over 1 MB; reads stop at the limit even if an entry lies about its size |
| Damaged data | Corrupted, truncated and CRC-damaged archives or entries are reported, not raised; other files continue |
| Nothing executed | Files are never run; HTML scripts are ignored; HTML parsing is bounded in size and depth |
| Original untouched | The ZIP is opened read-only; its SHA-256 is recorded in the output |
| Bounded memory | At most 2,000,000 records are kept; the rest are counted |

## Known limitations

1. The export layouts are Instagram's and change over time. The parser follows the layouts
   known when it was written; unknown layouts are reported as failed, not guessed. It has been
   tested with built test fixtures, not yet with a real export.
2. HTML parsing is best-effort and expects English month names in dates.
3. HTML times have no time zone and are not converted to UTC. A JSON and an HTML copy of the
   same data are therefore not recognised as duplicates of each other.
4. Messages, saved items and other categories are not parsed.
5. Media files are not opened, so their content, size of images, or EXIF data is not read.
6. Usernames in followers/following are checked for form only; whether an account still exists
   is not known (Instagram is never contacted).
7. The profile file contains personal data (for example email address and phone number) and
   the export contains other people's usernames. The output files hold the same data; store and
   share them accordingly.
