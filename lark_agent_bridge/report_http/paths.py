"""Filesystem helpers shared by report publishing and history cleanup."""

from __future__ import annotations

from pathlib import Path
import time


def _safe_slug(value: str) -> str:
    cleaned = []
    for char in value:
        if char.isalnum() or char in {"-", "_", "."}:
            cleaned.append(char)
        else:
            cleaned.append("-")
    slug = "".join(cleaned).strip("-._")
    return slug or "report"


def _current_timestamp() -> float:
    return time.time()


def _latest_mtime(path: Path) -> float:
    latest = path.stat().st_mtime
    for child in path.rglob("*"):
        try:
            child_mtime = child.stat().st_mtime
        except OSError:
            continue
        if child_mtime > latest:
            latest = child_mtime
    return latest
