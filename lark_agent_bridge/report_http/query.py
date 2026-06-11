"""Query-string parameter helpers for the report HTTP APIs."""

from __future__ import annotations


def _query_str(params: dict[str, list[str]], key: str) -> str:
    values = params.get(key) or []
    return str(values[0]).strip() if values else ""


def _query_int(params: dict[str, list[str]], key: str, default: int) -> int:
    raw = _query_str(params, key)
    if not raw:
        return default
    try:
        return max(1, min(int(raw), 1000))
    except ValueError:
        return default
