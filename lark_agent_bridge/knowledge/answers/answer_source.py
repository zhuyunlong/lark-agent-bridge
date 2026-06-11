"""源码调查触发判定、写回与派生模拟。"""

from __future__ import annotations

import re
from ...models import BridgeConfig, KnowledgeSourceOptions, TaskResult
from .. import source_investigation
from ..ingestors import KnowledgeIngestError, ingest_source, source_ref, source_title
from ..models import KnowledgeChunk, SearchHit
from ..template_data import load_simulation_template_chunks, load_simulation_templates
from .answer_simulation import (
    _DERIVED_SOURCE_ID,
    _SIMULATION_TEMPLATES,
    _is_negative_template_question,
    _looks_like_signal_simulation_question,
    _template_negative_match,
    _template_positive_match,
)


_MIN_WRITEBACK_CONFIDENCE = 0.75


_SOURCE_INVESTIGATION_TERMS = (
    "源码调查",
    "查源码",
    "看源码",
    "读源码",
    "源码分析",
    "基于源码",
    "重新源码",
    "重新调查",
    "深入查",
    "深入调查",
    "沉淀模板",
    "沉淀知识",
    "source investigation",
    "source investigate",
)


def _source_metadata(source: KnowledgeSourceOptions) -> dict[str, str]:
    return {
        "url": source.url,
        "path": source.path,
        "table_id": source.table_id,
        "view_id": source.view_id,
    }


def _source_investigation_unavailable_answer(
    question: str,
    result: source_investigation.SourceInvestigationResult,
    hits: list[SearchHit],
) -> str:
    error = result.error or "unknown source investigation error"
    if "timed out" in error:
        first_line = f"源码调查未在限定时间内完成：{error}"
    else:
        first_line = f"源码调查未能得到可用结构化结论：{error}"
    lines = [
        first_line,
        "当前不会直接给模拟命令，避免编造不可用指令。",
    ]
    if hits:
        lines.append("知识库可参考命中：")
        for index, hit in enumerate(hits[:3], start=1):
            title = (hit.title or hit.source_id or "知识条目").strip()
            source = (hit.source_ref or hit.source_id or "").strip()
            suffix = f"（{source}）" if source else ""
            lines.append(f"{index}. {title}{suffix}")
    else:
        lines.append("知识库也没有命中已验证模板。")
    lines.append(f"问题：{question}")
    lines.append("建议补充具体信号名、模块名或业务现象后重试；需要深度源码调查时可以继续发起。")
    return "\n".join(lines)


def _strip_trigger_prefix(text: str, prefixes: list[str]) -> str:
    cleaned = (text or "").strip()
    for prefix in prefixes:
        if prefix and cleaned.startswith(prefix):
            return cleaned[len(prefix) :].strip()
    return cleaned


def _should_record_source_derived_simulation(query: str) -> bool:
    lowered = (query or "").casefold()
    return any(
        template.get("support") == "source_derived"
        and not _template_negative_match(lowered, template)
        and _template_positive_match(lowered, template)
        for template in _SIMULATION_TEMPLATES
    )


def _derived_adb_simulation_chunks(query: str) -> list[KnowledgeChunk]:
    lowered = (query or "").casefold()
    matching_keys = {
        str(template.get("canonical_key") or "")
        for template in _SIMULATION_TEMPLATES
        if template.get("support") == "source_derived"
        and not _template_negative_match(lowered, template)
        and _template_positive_match(lowered, template)
    }
    chunks = load_simulation_template_chunks(_DERIVED_SOURCE_ID)
    return [chunk for chunk in chunks if str(chunk.metadata.get("canonical_key") or "") in matching_keys]


def _should_run_source_investigation(question: str) -> bool:
    lowered = (question or "").casefold()
    if _is_negative_template_question(question):
        return False
    return _explicit_source_investigation_requested(question) and _looks_like_signal_simulation_question(question)


def _explicit_source_investigation_requested(question: str) -> bool:
    lowered = (question or "").casefold()
    return any(term in lowered for term in _SOURCE_INVESTIGATION_TERMS)


def _can_write_back_source_result(result: source_investigation.SourceInvestigationResult) -> bool:
    return (
        result.writeback_allowed
        and bool(result.canonical_key.strip())
        and result.confidence >= _MIN_WRITEBACK_CONFIDENCE
        and bool(result.source_evidence)
    )


def _chunk_from_source_investigation(result: source_investigation.SourceInvestigationResult) -> KnowledgeChunk:
    content_lines = [result.answer]
    if result.commands:
        content_lines.append("commands:")
        content_lines.extend(result.commands)
    if result.source_evidence:
        content_lines.append("source_evidence:")
        for item in result.source_evidence:
            content_lines.append(
                f"{item.get('file', '')}:{item.get('line', '')} {item.get('text', '')}".strip()
            )
    content = "\n".join(line for line in content_lines if line)
    safe_key = re.sub(r"[^A-Za-z0-9_.:-]+", "_", result.canonical_key).strip("_") or "source-investigation"
    return KnowledgeChunk(
        id=f"{_DERIVED_SOURCE_ID}:{safe_key}",
        source_id=_DERIVED_SOURCE_ID,
        title=result.answer.splitlines()[0][:120] if result.answer else result.canonical_key,
        content=content,
        source_ref=result.coverage_boundary,
        kind="adb_signal_template",
        metadata={
            "canonical_key": result.canonical_key,
            "confidence": result.confidence,
            "source_evidence": result.source_evidence,
            "commands": result.commands,
        },
    )


def _preview(text: str, *, max_chars: int = 500) -> str:
    cleaned = re.sub(r"\n{3,}", "\n\n", text.strip())
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[: max_chars - 1].rstrip() + "…"
