"""Single-page admin console for the local report server."""

from __future__ import annotations

from pathlib import Path

_TEMPLATE_DIR = Path(__file__).parent / "templates"
_ADMIN_HTML_CACHE: str | None = None


def render_admin_page() -> str:
    global _ADMIN_HTML_CACHE
    if _ADMIN_HTML_CACHE is None:
        _ADMIN_HTML_CACHE = (_TEMPLATE_DIR / "admin.html").read_text(encoding="utf-8")
    return _ADMIN_HTML_CACHE
