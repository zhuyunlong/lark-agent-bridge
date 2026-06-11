"""低置信候选筛选、主题词过滤与候选答案。"""

from __future__ import annotations

import re
from typing import Any
from ...models import BridgeConfig, KnowledgeSourceOptions, TaskResult
from ..models import KnowledgeChunk, SearchHit
from ..store import KnowledgeStore, query_terms
from .answer_command import (
    _command_hits_answer,
    _extract_command_lines,
    _is_command_lookup_question,
)


_SIGNAL_DOMAIN_TERMS = ("信号", "signal_", "adb", "mock", "broadcast", "proto", "protobuf", "pb", "bytearray", "byte array")


_MIN_RETRIEVAL_SCORE = 6.0


_LOW_CONFIDENCE_MAX_CANDIDATES = 3


_LOW_CONFIDENCE_SEARCH_LIMIT = 12


_LOW_CONFIDENCE_GENERIC_TOPIC_TERMS = {"调试", "debug", "日志", "log"}


def _low_confidence_topic_terms(question: str) -> list[str]:
    terms: list[str] = []
    for term in query_terms(question):
        if _is_low_confidence_topic_term(term):
            terms.append(term)
    specific_terms = _filter_specific_low_confidence_topic_terms(terms)
    return specific_terms or terms


def _specific_low_confidence_topic_terms(question: str) -> list[str]:
    return _filter_specific_low_confidence_topic_terms(
        [term for term in query_terms(question) if _is_low_confidence_topic_term(term)]
    )


def _filter_specific_low_confidence_topic_terms(terms: list[str]) -> list[str]:
    return [term for term in terms if term.strip().casefold() not in _LOW_CONFIDENCE_GENERIC_TOPIC_TERMS]


def _is_low_confidence_topic_term(term: str) -> bool:
    cleaned = term.strip().casefold()
    if not cleaned:
        return False
    if re.search(r"[\u4e00-\u9fff]", cleaned):
        return len(cleaned) >= 2
    return len(cleaned) >= 2


def _should_offer_low_confidence_candidates(question: str, terms: list[str]) -> bool:
    if not terms:
        return False
    lowered = question.casefold()
    clear_intent_terms = ("模拟", "命令", "指令", "广播", "mock", "adb", "broadcast", "构造", "造数据", "造")
    return any(term in lowered for term in clear_intent_terms) or any(term in question for term in clear_intent_terms)


def _should_prefer_executable_candidates(question: str, hits: list[SearchHit]) -> bool:
    if not hits:
        return False
    if _has_specific_non_command_hits(hits):
        return False
    terms = _low_confidence_topic_terms(question)
    if not _should_offer_low_confidence_candidates(question, terms):
        return False
    return not _has_explicit_signal_domain(question)


def _filter_command_hits_by_specific_terms(question: str, hits: list[SearchHit]) -> list[SearchHit]:
    specific_terms = _specific_low_confidence_topic_terms(question)
    if not specific_terms:
        return hits
    return [
        hit
        for hit in hits
        if hit.kind != "adb_command" or _search_hit_matches_any_topic_term(hit, specific_terms)
    ]


def _has_specific_non_command_hits(hits: list[SearchHit]) -> bool:
    specific_kinds = {"guideengine_source", "signal_proto_entry"}
    return any(hit.kind in specific_kinds for hit in hits)


def _has_explicit_signal_domain(question: str) -> bool:
    lowered = (question or "").casefold()
    domain_terms = (
        "信号",
        "signal_",
        "adb",
        "mock",
        "broadcast",
        "datacenter",
        "code",
        "format",
        "proto",
        "protobuf",
        "pb",
        "bytearray",
        "byte array",
        "xdata",
    )
    return any(term in lowered for term in domain_terms)


def _low_confidence_command_candidates(
    store: KnowledgeStore,
    terms: list[str],
    max_hits: int,
) -> list[tuple[SearchHit, list[str]]]:
    candidates: dict[str, tuple[SearchHit, list[str]]] = {}
    for term in terms:
        for hit in store.search(term, limit=_LOW_CONFIDENCE_SEARCH_LIMIT):
            if hit.score < _MIN_RETRIEVAL_SCORE:
                continue
            commands = _extract_command_lines(hit.content)
            if not commands:
                continue
            existing = candidates.get(hit.chunk_id)
            if existing is None or hit.score > existing[0].score:
                candidates[hit.chunk_id] = (hit, commands)
    ranked = sorted(candidates.values(), key=lambda item: (-item[0].score, item[0].source_id, item[0].title))
    limit = min(_LOW_CONFIDENCE_MAX_CANDIDATES, max(1, max_hits))
    return ranked[:limit]


def _search_hit_matches_any_topic_term(hit: SearchHit, terms: list[str]) -> bool:
    metadata_keywords = hit.metadata.get("keywords")
    keyword_text = ""
    if isinstance(metadata_keywords, list):
        keyword_text = "\n".join(str(item) for item in metadata_keywords)
    text = f"{hit.title}\n{hit.content}\n{keyword_text}".casefold()
    return any(_search_text_matches_topic_term(text, term) for term in terms)


def _search_text_matches_topic_term(text: str, term: str) -> bool:
    cleaned = term.strip().casefold()
    if not cleaned:
        return False
    if re.fullmatch(r"[a-z0-9_]+", cleaned):
        return re.search(rf"(?<![a-z0-9]){re.escape(cleaned)}(?![a-z0-9])", text) is not None
    return cleaned in text


def _low_confidence_candidates_answer(candidates: list[tuple[SearchHit, list[str]]]) -> str:
    lines = [
        "没有命中足够确定的知识模板。下面是按核心关键词找到的可执行候选，请先确认是否就是你要的场景："
    ]
    for index, (hit, commands) in enumerate(candidates, start=1):
        command_lines = "\n".join(commands[:3])
        lines.append(f"{index}. {hit.title}\n{command_lines}")
    lines.append(
        "边界：这些只是低置信候选，只证明知识库里存在相关命令；"
        "不能等同于真实信号或业务数据状态已经被模拟。"
        "如果要精确链路，请补充具体信号名、业务模块，或要求继续源码调查后再沉淀为确定模板。"
    )
    return "\n\n".join(lines)


def _candidate_intro(matches: list[dict[str, Any]]) -> str:
    intros = {
        str(template.get("candidate_intro") or "").strip()
        for template in matches
        if str(template.get("candidate_intro") or "").strip()
    }
    if len(intros) == 1:
        return next(iter(intros))
    return ""


def _build_command_hit_result(question: str, hits: list[SearchHit], *, max_hits: int) -> TaskResult | None:
    if not _is_command_lookup_question(question):
        return None
    specific_terms = _specific_low_confidence_topic_terms(question)
    command_hits: list[tuple[SearchHit, list[str]]] = []
    for hit in hits:
        if hit.kind != "adb_command":
            continue
        if specific_terms and not _search_hit_matches_any_topic_term(hit, specific_terms):
            continue
        commands = _extract_command_lines(hit.content)
        if not commands:
            metadata_command = str(hit.metadata.get("command") or "").strip()
            commands = [metadata_command] if metadata_command else []
        if commands:
            command_hits.append((hit, commands))
    if not command_hits:
        return None
    limit = min(max(1, max_hits), _LOW_CONFIDENCE_MAX_CANDIDATES, len(command_hits))
    selected = command_hits[:limit]
    return TaskResult(
        success=True,
        message=_command_hits_answer(selected),
        details={
            "mode": "knowledge_qa",
            "question": question,
            "answer_type": "adb_command_hits",
            "knowledge_hits": [hit.to_dict(include_content=False) for hit, _commands in selected],
        },
    )
