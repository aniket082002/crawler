"""Shared building blocks for the social data modules.

    schema.py     field records (value, source, extraction time, status, reason)
                  and the record envelope every module writes
    identity.py   Instagram username / profile URL normalisation and link
                  classification (profile, rejected with a reason, or not an
                  Instagram link at all)
    exporters.py  CSV, JSON and Excel writers with spreadsheet-safety guards
    safe_io.py    bounded, defensive file reading and output-folder handling

Everything here works offline. No module in this package contains network
code, starts a process, or imports Scrapy; the only crawler code it reuses is
the standard-library-only ``webcrawler.url_validator``. It does not use the
Instagram API prototype in ``webcrawler.instagram``.
"""

__all__: list = []
