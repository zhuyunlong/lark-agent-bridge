"""Prompt snapshot primitives for cache-friendly followups."""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(slots=True)
class SnapshotFact:
    label: str
    value: str


@dataclass(slots=True)
class SnapshotEvidence:
    title: str
    path: str
    locator: str


@dataclass(slots=True)
class BugPromptSnapshot:
    scope_key: str
    analysis_kind: str
    stable_facts: list[SnapshotFact]
    evidence_refs: list[SnapshotEvidence]
    open_questions: list[str]
    prior_hypothesis: str = ""


def write_prompt_snapshot(path: Path, snapshot: BugPromptSnapshot) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(
            json.dumps(asdict(snapshot), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def read_prompt_snapshot(path: Path) -> BugPromptSnapshot:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid snapshot JSON in {path}: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Invalid snapshot payload: expected object")
    return BugPromptSnapshot(
        scope_key=_require_str(payload, "scope_key"),
        analysis_kind=_require_str(payload, "analysis_kind"),
        stable_facts=_read_snapshot_facts(payload),
        evidence_refs=_read_snapshot_evidence(payload),
        open_questions=_read_open_questions(payload),
        prior_hypothesis=_read_optional_str(payload, "prior_hypothesis"),
    )


def render_bug_snapshot_prefix(snapshot: BugPromptSnapshot, *, followup_text: str) -> str:
    conflict = any(
        term in followup_text
        for term in ("不对", "结果不合理", "重新从源码看", "重新源码分析", "不要沿用")
    )
    lines = ["### 会话事实快照"]
    lines.extend(f"- {fact.label}: {fact.value}" for fact in snapshot.stable_facts)
    lines.append("### 证据目录")
    lines.extend(
        f"- {item.title}: {item.path} {item.locator}"
        for item in snapshot.evidence_refs
    )
    if snapshot.open_questions:
        lines.append("### 未决问题")
        lines.extend(f"- {question}" for question in snapshot.open_questions)
    if snapshot.prior_hypothesis and not conflict:
        lines.append("### 候选假设")
        lines.append(f"- {snapshot.prior_hypothesis}")
    return "\n".join(lines)


def _require_str(payload: dict[str, object], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str):
        raise ValueError(f"Invalid snapshot field '{field_name}': expected string")
    return value


def _read_optional_str(payload: dict[str, object], field_name: str) -> str:
    value = payload.get(field_name, "")
    if not isinstance(value, str):
        raise ValueError(f"Invalid snapshot field '{field_name}': expected string")
    return value


def _require_list(payload: dict[str, object], field_name: str) -> list[object]:
    value = payload.get(field_name)
    if not isinstance(value, list):
        raise ValueError(f"Invalid snapshot field '{field_name}': expected list")
    return value


def _read_snapshot_facts(payload: dict[str, object]) -> list[SnapshotFact]:
    items = _require_list(payload, "stable_facts")
    facts: list[SnapshotFact] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(
                f"Invalid snapshot field 'stable_facts[{index}]': expected object",
            )
        label = item.get("label")
        value = item.get("value")
        if not isinstance(label, str) or not isinstance(value, str):
            raise ValueError(
                "Invalid snapshot field 'stable_facts': expected string label/value",
            )
        facts.append(SnapshotFact(label=label, value=value))
    return facts


def _read_snapshot_evidence(payload: dict[str, object]) -> list[SnapshotEvidence]:
    items = _require_list(payload, "evidence_refs")
    evidence_refs: list[SnapshotEvidence] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(
                f"Invalid snapshot field 'evidence_refs[{index}]': expected object",
            )
        title = item.get("title")
        path = item.get("path")
        locator = item.get("locator")
        if (
            not isinstance(title, str)
            or not isinstance(path, str)
            or not isinstance(locator, str)
        ):
            raise ValueError(
                "Invalid snapshot field 'evidence_refs': expected string title/path/locator",
            )
        evidence_refs.append(SnapshotEvidence(title=title, path=path, locator=locator))
    return evidence_refs


def _read_open_questions(payload: dict[str, object]) -> list[str]:
    items = _require_list(payload, "open_questions")
    for index, item in enumerate(items):
        if not isinstance(item, str):
            raise ValueError(
                f"Invalid snapshot field 'open_questions[{index}]': expected string",
            )
    return list(items)


__all__ = [
    "BugPromptSnapshot",
    "SnapshotEvidence",
    "SnapshotFact",
    "read_prompt_snapshot",
    "render_bug_snapshot_prefix",
    "write_prompt_snapshot",
]
