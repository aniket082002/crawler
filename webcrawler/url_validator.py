"""Input URL and output file path validation (Requirement 7)."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

ALLOWED_SCHEMES = {"http", "https"}


def normalize_url(url: str) -> str:
    """Canonicalize a URL for dedup/registry purposes (strip fragment, lowercase host)."""
    parsed = urlparse(url.strip())
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    path = parsed.path or "/"
    query = parsed.query
    normalized = f"{scheme}://{netloc}{path}"
    if query:
        normalized += f"?{query}"
    return normalized


def is_valid_url(url: str, allowed_domains: list[str] | None = None) -> bool:
    """Reject malformed/unsafe schemes (file://, javascript:, data:, etc.) and,
    when allowed_domains is given, reject anything outside that domain allowlist.
    """
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return False

    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        return False
    if not parsed.netloc:
        return False

    if allowed_domains:
        host = parsed.netloc.lower().split(":")[0]
        if not any(host == d.lower() or host.endswith("." + d.lower()) for d in allowed_domains):
            return False

    return True


def safe_output_path(base_dir: str | Path, filename: str) -> Path:
    """Resolve filename under base_dir, rejecting any path traversal outside it."""
    base = Path(base_dir).resolve()
    base.mkdir(parents=True, exist_ok=True)
    candidate = (base / filename).resolve()
    if base != candidate and base not in candidate.parents:
        raise ValueError(f"Unsafe output path outside project output directory: {filename}")
    return candidate
