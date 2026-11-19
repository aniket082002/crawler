"""Module B - Instagram Export Parser.

Reads the "Download Your Information" ZIP file that a person downloads from
their OWN Instagram account, entirely offline, and turns what it contains
into the shared record structure of ``webcrawler.social_common``.

    archive.py   safe ZIP inspection: limits, suspicious entries, bounded reads
    htmldoc.py   tolerant, size-bounded HTML reader (standard library only)
    parsers.py   recognises export files and parses them (JSON and HTML)
    records.py   field records, de-duplication, media references
    report.py    CSV / JSON / Excel output in a new timestamped folder
    cli.py       command line:  python -m webcrawler.social_export <export.zip>

Nothing in this package contacts Instagram or any other service, starts a
process, or writes anything from the archive to disk: archive members are
read into memory, with size limits, and only the JSON and HTML files that
are parsed are read at all. The original ZIP file is opened read-only.
"""

__all__: list = []
