"""Structured report composition helpers for bridge-owned reports."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ReportVerdict:
    sev: str = "green"
    text: str = ""


@dataclass(slots=True)
class ReportSection:
    kind: str
    title: str
    description: str = ""
    empty_text: str = ""
    items: list[dict[str, Any]] = field(default_factory=list)
    nodes: list[dict[str, Any]] = field(default_factory=list)
    rows: list[tuple[Any, ...]] = field(default_factory=list)
    cols: list[str] = field(default_factory=list)
    text: str = ""
    class_name: str = ""
    summary: str = ""
    body_html: str = ""


@dataclass(slots=True)
class ReportComposition:
    title: str
    heading: str
    subtitle: str = ""
    verdict: ReportVerdict = field(default_factory=ReportVerdict)
    cards: list[tuple[Any, ...]] = field(default_factory=list)
    sections: list[ReportSection] = field(default_factory=list)


def section_to_dict(section: ReportSection) -> dict[str, Any]:
    return {
        "kind": section.kind,
        "title": section.title,
        "description": section.description,
        "empty_text": section.empty_text,
        "items": section.items,
        "nodes": section.nodes,
        "rows": section.rows,
        "cols": section.cols,
        "text": section.text,
        "class_name": section.class_name,
        "summary": section.summary,
        "body_html": section.body_html,
    }


def composition_to_renderer_payload(composition: ReportComposition) -> dict[str, Any]:
    return {
        "title": composition.title,
        "heading": composition.heading,
        "subtitle": composition.subtitle,
        "verdict": {"sev": composition.verdict.sev, "text": composition.verdict.text},
        "cards": composition.cards,
        "sections": [section_to_dict(section) for section in composition.sections],
    }
