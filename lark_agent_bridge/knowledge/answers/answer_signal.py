"""信号源候选与 proto 字段摘要类答案。"""

from __future__ import annotations

import re
from typing import Any
from ...models import BridgeConfig, KnowledgeSourceOptions, TaskResult
from ..models import KnowledgeChunk, SearchHit
from .answer_simulation import (
    _looks_like_signal_simulation_question,
)
from .answer_lowconf import (
    _LOW_CONFIDENCE_MAX_CANDIDATES,
)
from .answer_source import (
    _preview,
)


def _build_signal_source_candidate_result(question: str, hits: list[SearchHit]) -> TaskResult | None:
    if not _looks_like_signal_simulation_question(question):
        return None
    candidates = _signal_source_candidates(hits)
    if not candidates:
        return None
    return TaskResult(
        success=True,
        message=_signal_source_candidates_answer(question, candidates),
        details={
            "mode": "knowledge_qa",
            "question": question,
            "answer_type": "adb_signal_source_candidates",
            "candidates": candidates,
            "knowledge_hits": [
                hit.to_dict(include_content=False)
                for hit in hits
                if hit.kind == "signal_proto_entry" and any(hit.chunk_id == candidate["chunk_id"] for candidate in candidates)
            ],
        },
    )


def _signal_source_candidates(hits: list[SearchHit]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for hit in hits:
        if hit.kind != "signal_proto_entry":
            continue
        signal = str(hit.metadata.get("signal") or _extract_signal_proto_field(hit.content, "signal") or "").strip()
        if not signal or signal in seen:
            continue
        seen.add(signal)
        code = str(hit.metadata.get("code") or _extract_signal_proto_field(hit.content, "code") or "").strip()
        summary = _signal_proto_summary(hit)
        title = hit.title.strip() or f"{signal} ({code})".strip()
        candidates.append(
            {
                "chunk_id": hit.chunk_id,
                "signal": signal,
                "code": code,
                "title": title,
                "summary": summary,
            }
        )
    return candidates[:_LOW_CONFIDENCE_MAX_CANDIDATES]


def _signal_source_candidates_answer(question: str, candidates: list[dict[str, Any]]) -> str:
    count = len(candidates)
    if count == 1:
        lines = ["当前命中 1 个可能相关的信号候选，但还不能直接确认这就是你要模拟的信号："]
    else:
        lines = [f"当前命中 {count} 个可能相关的信号候选，还不能直接确认你要的是哪一个："]
    for index, candidate in enumerate(candidates, start=1):
        title = str(candidate.get("title") or candidate.get("signal") or "候选信号")
        summary = str(candidate.get("summary") or "源码里命中了相关信号定义，但还缺少已验证模板。").strip()
        signal = str(candidate.get("signal") or "").strip()
        lines.append(f"{index}. {title}\n适用提示：{summary}")
        if signal:
            lines.append(f"继续发：知识库 模拟 {signal}")
    lines.append(
        "边界：当前仅定位到候选信号，不能直接给可执行 ADB 命令；"
        "只有命中已验证模板或完成源码调查后，才适合返回确定指令。"
    )
    lines.append(f"继续发：知识库 源码调查 {question}")
    return "\n\n".join(lines)


def _extract_signal_proto_field(content: str, field: str) -> str:
    prefix = f"{field}:"
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith(prefix):
            return stripped[len(prefix) :].strip()
    return ""


def _signal_proto_summary(hit: SearchHit) -> str:
    comment = _extract_signal_proto_field(hit.content, "comment")
    if not comment:
        return "源码里命中了相关信号定义，但还缺少已验证模板。"
    cleaned = comment.strip()
    if "|" in cleaned:
        parts = [part.strip(" =") for part in cleaned.split("|") if part.strip(" =")]
        if parts:
            cleaned = parts[-1]
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" =|-")
    return cleaned or "源码里命中了相关信号定义，但还缺少已验证模板。"


def _generic_answer(question: str, hits: list[SearchHit]) -> str:
    top_hits = hits[:3]
    lines = [f"知识库命中 {len(hits)} 条，先列最相关摘要："]
    for index, hit in enumerate(top_hits, start=1):
        preview = _preview(hit.content, max_chars=220)
        lines.append(f"{index}. {hit.title}\n{preview}")
    remaining = len(hits) - len(top_hits)
    if remaining > 0:
        lines.append(f"另有 {remaining} 条参考来源，已在卡片底部收敛展示。")
    return "\n\n".join(lines)
