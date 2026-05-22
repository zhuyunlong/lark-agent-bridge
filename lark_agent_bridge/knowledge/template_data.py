"""Bundled data-backed knowledge templates."""

from __future__ import annotations

from importlib import resources
import json
from typing import Any

from .models import KnowledgeChunk


_DATA_PACKAGE = "lark_agent_bridge.knowledge.templates"
_TEMPLATE_FILE = "adb_simulation_templates.json"


def load_template_payload() -> dict[str, Any]:
    with resources.files(_DATA_PACKAGE).joinpath(_TEMPLATE_FILE).open("r", encoding="utf-8") as fh:
        payload = json.load(fh)
    return payload if isinstance(payload, dict) else {}


def load_source_specs() -> list[dict[str, Any]]:
    specs = load_template_payload().get("source_specs", [])
    return [item for item in specs if isinstance(item, dict)]


def load_simulation_templates() -> list[dict[str, Any]]:
    templates = load_template_payload().get("simulation_templates", [])
    return [item for item in templates if isinstance(item, dict)]


def load_simulation_template_chunks(source_id: str) -> list[KnowledgeChunk]:
    chunks: list[KnowledgeChunk] = []
    for item in load_simulation_templates():
        canonical_key = str(item.get("canonical_key") or "").strip()
        if not canonical_key:
            continue
        title = str(item.get("title") or canonical_key).strip()
        content = _template_content(item)
        metadata = dict(item)
        metadata.setdefault("status", "verified")
        metadata.setdefault("keywords", _template_keywords(item))
        chunks.append(
            KnowledgeChunk(
                id=f"{source_id}:{canonical_key}",
                source_id=source_id,
                title=title,
                content=content,
                source_ref=str(item.get("source_ref") or "bundled:adb_simulation_templates"),
                kind="adb_signal_template",
                metadata=metadata,
            )
        )
    return chunks


def _template_content(item: dict[str, Any]) -> str:
    lines: list[str] = []
    for key in ("title", "summary", "signal", "code", "format", "capability", "status"):
        value = item.get(key)
        if value not in (None, ""):
            lines.append(f"{key}: {value}")
    for key in ("aliases", "negative_aliases"):
        values = item.get(key)
        if isinstance(values, list) and values:
            lines.append(f"{key}: {', '.join(str(value) for value in values)}")
    commands = item.get("commands")
    if isinstance(commands, list) and commands:
        lines.append("commands:")
        lines.extend(str(command) for command in commands)
    answer_blocks = item.get("answer_blocks")
    if isinstance(answer_blocks, list) and answer_blocks:
        lines.append("answer:")
        lines.extend(str(block) for block in answer_blocks)
    evidence = item.get("evidence")
    if isinstance(evidence, list) and evidence:
        lines.append("evidence:")
        lines.extend(_evidence_text(entry) for entry in evidence if isinstance(entry, dict))
    return "\n".join(line for line in lines if line)


def _template_keywords(item: dict[str, Any]) -> list[str]:
    keywords: list[str] = []
    for key in ("canonical_key", "signal", "code", "title", "summary", "category", "capability"):
        value = item.get(key)
        if value not in (None, ""):
            keywords.append(str(value))
    for key in ("aliases", "negative_aliases"):
        values = item.get(key)
        if isinstance(values, list):
            keywords.extend(str(value) for value in values if str(value).strip())
    return keywords


def _evidence_text(entry: dict[str, Any]) -> str:
    file_text = str(entry.get("file") or "")
    line_text = str(entry.get("line") or "")
    text = str(entry.get("text") or "")
    if line_text:
        return f"{file_text}:{line_text} {text}".strip()
    return f"{file_text} {text}".strip()
