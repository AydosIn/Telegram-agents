from __future__ import annotations

from urllib.parse import urlparse

from app.config import Settings


def is_url_allowed(url: str, settings: Settings) -> bool:
    """Only http(s) to hosts in the browser allowlist (default: localhost-style)."""
    raw = (url or "").strip()
    if not raw:
        return False
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    allowed = {h.strip().lower() for h in settings.browser_url_allowlist_hosts}
    return host in allowed
