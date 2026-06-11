"""agent/code-index/codegraph 输出 → 调查结果解析与置信度。"""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any, TYPE_CHECKING
from ..models import SearchHit
from .investigation_types import (
    SourceInvestigationResult,
)


def _result_from_payload(
    payload: dict[str, Any],
    *,
    command: list[str],
    stdout: str,
    stderr: str,
) -> SourceInvestigationResult:
    evidence = payload.get("source_evidence")
    commands = payload.get("commands")
    result = SourceInvestigationResult(
        success=True,
        answer=str(payload.get("answer") or "").strip(),
        canonical_key=str(payload.get("canonical_key") or "").strip(),
        confidence=_float(payload.get("confidence")),
        commands=[str(item) for item in commands if str(item).strip()] if isinstance(commands, list) else [],
        source_evidence=[item for item in evidence if isinstance(item, dict)] if isinstance(evidence, list) else [],
        coverage_boundary=str(payload.get("coverage_boundary") or "").strip(),
        writeback_allowed=bool(payload.get("writeback_allowed")),
        command=command,
        stdout=stdout,
        stderr=stderr,
    )
    if not result.answer:
        result.success = False
        result.error = "source investigation returned empty answer"
    return result


def _schema_error(payload: dict[str, Any]) -> str:
    required = {
        "answer": str,
        "canonical_key": str,
        "confidence": (int, float),
        "commands": list,
        "source_evidence": list,
        "coverage_boundary": str,
        "writeback_allowed": bool,
    }
    for key, expected_type in required.items():
        if key not in payload:
            return f"missing {key}"
        if not isinstance(payload[key], expected_type):
            return f"{key} must be {expected_type}"
    return ""


def _parse_json_object(raw: str) -> dict[str, Any] | None:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _last_json_object(raw: str) -> str:
    for line in reversed((raw or "").splitlines()):
        stripped = line.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            return stripped
    return raw


def _last_agent_message(raw: str) -> str:
    message = ""
    for line in (raw or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        if item.get("type") != "agent_message":
            continue
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            message = text.strip()
    return message


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _extract_query_symbols(question: str, hits: list[SearchHit]) -> list[str]:
    """Extract symbol names from question text and search hits."""
    symbols: list[str] = []
    seen: set[str] = set()

    # From hits metadata
    for hit in hits[:10]:
        signal = str(hit.metadata.get("signal") or "").strip()
        if signal and signal not in seen:
            seen.add(signal)
            symbols.append(signal)

    # From question — look for CamelCase or UPPER_SNAKE identifiers
    for match in re.finditer(r'\b([A-Z][a-zA-Z0-9]{2,}(?:[A-Z][a-z]+)*)\b', question):
        name = match.group(1)
        if name not in seen:
            seen.add(name)
            symbols.append(name)
    for match in re.finditer(r'\b(SIGNAL_[A-Z0-9_]+)\b', question):
        name = match.group(1)
        if name not in seen:
            seen.add(name)
            symbols.append(name)

    return symbols[:10]


def _code_index_confidence(
    contexts: list[tuple[Path, Any]],
    question: str,
    hits: list[SearchHit],
) -> float:
    """Estimate confidence of code index results."""
    if not contexts:
        return 0.0

    total_defs = sum(len(ctx.definitions) for _, ctx in contexts)
    total_refs = sum(len(ctx.references) for _, ctx in contexts)

    if total_defs == 0:
        return 0.0

    score = 0.3  # base score for having any definitions
    if total_defs >= 2:
        score += 0.15
    if total_refs >= 3:
        score += 0.2
    if total_refs >= 8:
        score += 0.1

    # Bonus if definitions span multiple kinds (e.g. class + method)
    all_kinds = set()
    for _, ctx in contexts:
        for d in ctx.definitions:
            all_kinds.add(d.kind)
    if len(all_kinds) >= 2:
        score += 0.1

    return min(score, 1.0)


def _result_from_code_index(
    contexts: list[tuple[Path, Any]],
    question: str,
    hits: list[SearchHit],
    *,
    confidence: float = 0.7,
) -> SourceInvestigationResult:
    """Build a SourceInvestigationResult from code index contexts."""
    evidence: list[dict[str, Any]] = []
    answer_parts: list[str] = []
    canonical_parts: list[str] = []

    for repo, ctx in contexts:
        for defn in ctx.definitions[:5]:
            scope_prefix = f"{defn.scope}." if defn.scope else ""
            answer_parts.append(
                f"{defn.kind} {scope_prefix}{defn.name} 定义在 {defn.path}:{defn.line}"
            )
            evidence.append({
                "file": defn.path,
                "line": defn.line,
                "text": f"[{defn.kind}] {scope_prefix}{defn.name}",
            })
            if not canonical_parts:
                canonical_parts.append(defn.name)

        for ref in ctx.references[:8]:
            evidence.append({
                "file": ref.path,
                "line": ref.line,
                "text": ref.text[:200],
            })

    answer = "代码索引快速查找结果：\n" + "\n".join(answer_parts) if answer_parts else ""
    return SourceInvestigationResult(
        success=True,
        answer=answer,
        canonical_key=".".join(canonical_parts)[:120] if canonical_parts else "",
        confidence=confidence,
        commands=[],
        source_evidence=evidence[:20],
        coverage_boundary="code_index: definitions + references only",
        writeback_allowed=False,
    )


def _codegraph_confidence(
    contexts: list[tuple[Path, Any]],
    callers: list[tuple[Path, str, list[Any]]],
    question: str,
    hits: list[SearchHit],
) -> float:
    """Estimate confidence of codegraph results."""
    if not contexts:
        return 0.0

    total_entries = sum(len(ctx.entry_points) for _, ctx in contexts)
    total_callers = sum(len(c) for _, _, c in callers)

    if total_entries == 0:
        return 0.0

    score = 0.35  # base score for having definitions
    if total_entries >= 2:
        score += 0.15
    if total_callers >= 2:
        score += 0.2
    if total_callers >= 5:
        score += 0.1

    # Bonus for multiple kinds
    all_kinds = set()
    for _, ctx in contexts:
        for ep in ctx.entry_points:
            all_kinds.add(ep.get("kind", ""))
    if len(all_kinds) >= 2:
        score += 0.1

    return min(score, 1.0)


def _result_from_codegraph(
    contexts: list[tuple[Path, Any]],
    callers: list[tuple[Path, str, list[Any]]],
    question: str,
    hits: list[SearchHit],
    *,
    confidence: float = 0.7,
) -> SourceInvestigationResult:
    """Build a SourceInvestigationResult from codegraph contexts."""
    evidence: list[dict[str, Any]] = []
    answer_parts: list[str] = []
    canonical_parts: list[str] = []

    for repo, ctx in contexts:
        for ep in ctx.entry_points[:5]:
            name = ep.get("qualifiedName") or ep.get("name", "")
            kind = ep.get("kind", "")
            path = ep.get("filePath", "")
            line = ep.get("startLine", 0)
            answer_parts.append(f"{kind} {name} 定义在 {path}:{line}")
            evidence.append({"file": path, "line": line, "text": f"[{kind}] {name}"})
            if not canonical_parts:
                canonical_parts.append(name)

    for repo, symbol, caller_list in callers:
        for c in caller_list[:5]:
            evidence.append({
                "file": c.path, "line": c.line,
                "text": f"[caller] {c.name} → {symbol}",
            })

    answer = "CodeGraph 语义索引快速查找结果：\n" + "\n".join(answer_parts) if answer_parts else ""
    return SourceInvestigationResult(
        success=True,
        answer=answer,
        canonical_key=".".join(canonical_parts)[:120] if canonical_parts else "",
        confidence=confidence,
        commands=[],
        source_evidence=evidence[:20],
        coverage_boundary="codegraph: semantic definitions + call graph",
        writeback_allowed=False,
    )


def _codegraph_to_code_index_context(ctx: Any, cg: Any, repo: Path) -> Any:
    """Convert codegraph context to code_index CodeIndexContext for prompt enrichment."""
    try:
        from ..code_index import CodeIndexContext, SymbolHit, ReferenceHit
    except ImportError:
        return ctx
    defs = [
        SymbolHit(
            name=ep.get("name", ""),
            kind=ep.get("kind", ""),
            path=ep.get("filePath", ""),
            line=int(ep.get("startLine", 0)),
            language="",
            scope=ep.get("qualifiedName", "").rsplit("::", 1)[0] if "::" in ep.get("qualifiedName", "") else "",
        )
        for ep in ctx.entry_points[:5]
    ]
    return CodeIndexContext(definitions=defs, references=[])
