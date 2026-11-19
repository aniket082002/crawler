"""Module C - Website Social Profile Discovery.

Finds the Instagram profile links that websites publish, using only what the
website crawler has ALREADY saved: the ``items.json`` / ``items.csv`` files in
its output folders. For every profile it reports the normalised username and
profile URL, and every website and page the link was found on, with the time
the crawler fetched that page.

    reader.py    reads crawler output files (JSON and CSV), defensively
    discover.py  classifies links, removes duplicates, keeps every source
    report.py    builds the JSON document and the CSV / Excel tables
    cli.py       command line:  python -m webcrawler.social_discovery <run folder>

This module never contacts Instagram or any other service: it contains no
network code at all, starts no processes and does not run a crawl. A profile
in its output therefore means "a link with the form of a profile URL was
published on this page", not "this account exists".
"""

__all__: list = []
