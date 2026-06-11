"""卡片 JSON 序列化与纯文本降级。"""

from __future__ import annotations

import json
from typing import Any


def card_to_json(card: dict[str, Any]) -> str:
    """Serialize a card dict to compact JSON for ``lark-cli --card``."""
    return json.dumps(card, ensure_ascii=False, separators=(",", ":"))


def build_text_fallback(card: dict[str, Any]) -> str:
    """Extract a plain-text fallback from a card for clients that don't render cards."""
    header = card.get("header", {})
    title = header.get("title", {}).get("content", "")
    parts = [title] if title else []
    for element in card.get("elements", []):
        if element.get("tag") == "div":
            text = element.get("text", {})
            if isinstance(text, dict):
                content = text.get("content", "")
                if content:
                    parts.append(content)
            fields = element.get("fields", [])
            for f in fields:
                if isinstance(f, dict):
                    ft = f.get("text", {})
                    if isinstance(ft, dict):
                        parts.append(ft.get("content", ""))
    return "\n".join(parts)
