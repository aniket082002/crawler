"""Entry point for ``python -m webcrawler.social_export``."""
import sys

from webcrawler.social_export.cli import main

if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(errors="replace")   # never crash on an unprintable character
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
