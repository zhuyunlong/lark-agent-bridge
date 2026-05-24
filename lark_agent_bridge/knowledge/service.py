"""Knowledge-base orchestration and answer generation."""

from __future__ import annotations

import hashlib
import json
import re
import threading
from typing import Any

from ..models import BridgeConfig, KnowledgeSourceOptions, TaskResult
from . import source_investigation
from .ingestors import KnowledgeIngestError, ingest_source, source_ref, source_title
from .models import KnowledgeChunk, SearchHit
from .store import KnowledgeStore, query_terms
from .template_data import load_simulation_template_chunks, load_simulation_templates


_SIMULATION_INTENT_TERMS = ("模拟", "怎么", "如何", "命令", "指令", "广播", "mock", "造")
_SIGNAL_DOMAIN_TERMS = ("信号", "signal_", "adb", "mock", "broadcast", "proto", "protobuf", "pb", "bytearray", "byte array")
_MOCK_ACTION = "com.xiaopeng.guide.action.mock.datacenter"
_DERIVED_SOURCE_ID = "derived-adb-simulations"
_SIMULATION_TEMPLATES: tuple[dict[str, Any], ...] = tuple(load_simulation_templates())
_MIN_WRITEBACK_CONFIDENCE = 0.75
_MIN_RETRIEVAL_SCORE = 6.0
_LOW_CONFIDENCE_MAX_CANDIDATES = 3
_LOW_CONFIDENCE_SEARCH_LIMIT = 12
_COMMAND_MARKERS = ("adb ", "fastboot ")
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
_TEMPLATE_MATCH_SEPARATOR_RE = re.compile(r"[\s\-_./:：,，、|()（）\[\]【】{}]+")
_LOW_CONFIDENCE_GENERIC_TOPIC_TERMS = {"调试", "debug", "日志", "log"}


class KnowledgeService:
    def __init__(self, config: BridgeConfig) -> None:
        self.config = config
        self.store = KnowledgeStore(config.knowledge.storage)
        self._ready_lock = threading.RLock()
        self._configured_sources_checked = False
        self._source_investigation_runner = source_investigation.SourceInvestigationRunner(config)

    def should_handle(self, text: str) -> bool:
        if not self.config.knowledge.enabled:
            return False
        cleaned = (text or "").strip()
        if not cleaned:
            return False
        for prefix in self.config.knowledge.trigger_prefixes:
            if prefix and cleaned.startswith(prefix):
                return True
        lowered = cleaned.casefold()
        if "知识库" in cleaned or "查知识" in cleaned:
            return True
        if _looks_like_signal_simulation_question(cleaned):
            return True
        if "SIGNAL_" in cleaned and any(term in lowered for term in ("adb", "mock", "模拟", "指令", "命令", "广播")):
            return True
        if "adb" in lowered and any(term in cleaned for term in ("怎么", "如何", "命令", "指令", "模拟")):
            return True
        return False

    def sync_all(self) -> dict[str, Any]:
        sources = self.config.knowledge.sources
        source_results: list[dict[str, Any]] = []
        total_chunks = 0
        for source in sources:
            status, error, chunks = self._sync_source(source)
            total_chunks += len(chunks)
            source_results.append(
                {
                    "id": source.id,
                    "type": source.type,
                    "status": status,
                    "error": error,
                    "chunk_count": len(chunks),
                }
            )
        return {"source_count": len(sources), "total_chunks": total_chunks, "sources": source_results}

    def _sync_source(self, source: KnowledgeSourceOptions) -> tuple[str, str, list[KnowledgeChunk]]:
        try:
            chunks = ingest_source(self.config, source)
            self.store.replace_source(
                source_id=source.id,
                source_type=source.type,
                title=source_title(source),
                source_ref=source_ref(source),
                chunks=chunks,
                metadata=_source_metadata(source),
            )
            return "ok", "", chunks
        except KnowledgeIngestError as exc:
            error = str(exc)
            self.store.replace_source(
                source_id=source.id,
                source_type=source.type,
                title=source_title(source),
                source_ref=source_ref(source),
                chunks=[],
                status="error",
                error=error,
                metadata=_source_metadata(source),
            )
            return "error", error, []

    def add_text(
        self,
        *,
        source_id: str,
        title: str,
        content: str,
        source_ref: str = "",
        kind: str = "manual",
    ) -> dict[str, Any]:
        cleaned_source = source_id.strip() or "manual"
        cleaned_title = title.strip() or "手工知识"
        cleaned_content = content.strip()
        if not cleaned_content:
            raise ValueError("content 不能为空")
        chunk = KnowledgeChunk(
            id=f"{cleaned_source}:manual:{hashlib.sha1(cleaned_content.encode('utf-8')).hexdigest()[:16]}",
            source_id=cleaned_source,
            title=cleaned_title,
            content=cleaned_content,
            source_ref=source_ref.strip(),
            kind=kind,
            metadata={"manual": True},
        )
        self.store.add_chunks(
            source_id=cleaned_source,
            source_type=kind,
            title=cleaned_source,
            source_ref=source_ref.strip(),
            chunks=[chunk],
        )
        return chunk.to_dict()

    def register_source(
        self,
        *,
        source_id: str,
        source_type: str = "manual",
        title: str = "",
        source_ref: str = "",
    ) -> dict[str, Any]:
        cleaned_source = source_id.strip()
        if not cleaned_source:
            raise ValueError("source_id 不能为空")
        cleaned_type = source_type.strip() or "manual"
        self.store.replace_source(
            source_id=cleaned_source,
            source_type=cleaned_type,
            title=title.strip() or cleaned_source,
            source_ref=source_ref.strip(),
            chunks=[],
        )
        return {
            "id": cleaned_source,
            "type": cleaned_type,
            "title": title.strip() or cleaned_source,
            "source_ref": source_ref.strip(),
            "chunk_count": 0,
        }

    def list_sources(self) -> list[dict[str, Any]]:
        self._ensure_index_ready()
        return self.store.list_sources()

    def search(self, query: str, *, limit: int | None = None) -> list[SearchHit]:
        self._ensure_index_ready(query=query)
        hits = self.store.search(query, limit=limit or self.config.knowledge.max_hits)
        return [hit for hit in hits if hit.score >= _MIN_RETRIEVAL_SCORE]

    def answer(self, question: str) -> TaskResult:
        cleaned = _strip_trigger_prefix(question, self.config.knowledge.trigger_prefixes)
        if _is_negative_template_question(cleaned):
            return TaskResult(
                success=False,
                message="知识库里没有命中可用内容。可以先同步知识库，或补充更具体的关键词。",
                error_code="knowledge_no_hits",
                details={"mode": "knowledge_qa", "question": cleaned, "knowledge_hits": []},
            )
        self._ensure_index_ready(query=cleaned)
        hits = _filter_command_hits_by_specific_terms(
            cleaned,
            self.search(cleaned, limit=self.config.knowledge.max_hits),
        )
        simulation_result = _build_simulation_result(cleaned, hits)
        if simulation_result is not None:
            return simulation_result
        command_hit_result = _build_command_hit_result(cleaned, hits, max_hits=self.config.knowledge.max_hits)
        if command_hit_result is not None:
            return command_hit_result
        if _should_prefer_executable_candidates(cleaned, hits):
            low_confidence_result = self.answer_low_confidence_candidates(cleaned, allow_with_hits=True)
            if low_confidence_result is not None:
                return low_confidence_result
        if self.config.source_investigation.enabled and _should_run_source_investigation(cleaned):
            return self._answer_from_source_investigation(cleaned, hits=hits)
        signal_source_candidate_result = _build_signal_source_candidate_result(cleaned, hits)
        if signal_source_candidate_result is not None:
            return signal_source_candidate_result
        if hits:
            message = _generic_answer(cleaned, hits)
            return TaskResult(
                success=True,
                message=message,
                details={
                    "mode": "knowledge_qa",
                    "question": cleaned,
                    "answer_type": "retrieval_summary",
                    "knowledge_hits": [hit.to_dict(include_content=False) for hit in hits],
                },
            )
        low_confidence_result = self.answer_low_confidence_candidates(cleaned)
        if low_confidence_result is not None:
            return low_confidence_result
        return TaskResult(
            success=False,
            message="知识库里没有命中可用内容。可以先同步知识库，或补充更具体的关键词。",
            error_code="knowledge_no_hits",
            details={"mode": "knowledge_qa", "question": cleaned, "knowledge_hits": []},
        )

    def answer_low_confidence_candidates(self, question: str, *, allow_with_hits: bool = False) -> TaskResult | None:
        cleaned = _strip_trigger_prefix(question, self.config.knowledge.trigger_prefixes)
        if not cleaned or _is_negative_template_question(cleaned):
            return None
        self._ensure_index_ready(query=cleaned)
        if not allow_with_hits and self.search(cleaned, limit=1):
            return None
        terms = _low_confidence_topic_terms(cleaned)
        if not _should_offer_low_confidence_candidates(cleaned, terms):
            return None
        candidates = _low_confidence_command_candidates(self.store, terms, self.config.knowledge.max_hits)
        if not candidates:
            return None
        return TaskResult(
            success=True,
            message=_low_confidence_candidates_answer(candidates),
            details={
                "mode": "knowledge_qa",
                "question": cleaned,
                "answer_type": "low_confidence_candidates",
                "knowledge_hits": [hit.to_dict(include_content=False) for hit, _commands in candidates],
            },
        )

    def _ensure_index_ready(self, *, query: str = "") -> None:
        if not self.config.knowledge.enabled:
            return
        with self._ready_lock:
            sources = self.store.list_sources()
            has_indexed_chunks = any(source.get("chunk_count", 0) for source in sources)
            if not self._configured_sources_checked and not has_indexed_chunks and self.config.knowledge.sources:
                self.sync_all()
                sources = self.store.list_sources()
            elif not self._configured_sources_checked:
                self._refresh_stale_guideengine_signal_sources()
                sources = self.store.list_sources()
            self._configured_sources_checked = True
            if _should_record_source_derived_simulation(query):
                self._record_source_derived_simulations(query)

    def _refresh_stale_guideengine_signal_sources(self) -> None:
        signal_sources = [source for source in self.config.knowledge.sources if source.type == "guideengine_signal"]
        if not signal_sources:
            return
        chunks = self.store.all_chunks()
        for source in signal_sources:
            has_signal_proto_entries = any(
                chunk.source_id == source.id and chunk.kind == "signal_proto_entry" for chunk in chunks
            )
            if has_signal_proto_entries:
                continue
            self._sync_source(source)

    def _record_source_derived_simulations(self, query: str) -> None:
        chunks = _derived_adb_simulation_chunks(query)
        if not chunks:
            return
        self.store.add_chunks(
            source_id=_DERIVED_SOURCE_ID,
            source_type="source_derived",
            title="源码派生 ADB 模拟知识",
            source_ref=str(self.config.guideengine_repo),
            chunks=chunks,
            metadata={"generated_by": "KnowledgeService", "kind": "adb_simulation_template"},
        )

    def _answer_from_source_investigation(self, question: str, *, hits: list[SearchHit] | None = None) -> TaskResult:
        result = self._source_investigation_runner.run(question, hits=hits or [])
        if not result.success:
            existing_hits = hits or []
            return TaskResult(
                success=True,
                message=_source_investigation_unavailable_answer(question, result, existing_hits),
                stdout=result.stdout,
                stderr=result.stderr,
                command=result.command,
                details={
                    "mode": "knowledge_qa",
                    "question": question,
                    "answer_type": "source_investigation_unavailable",
                    "source_investigation_error": result.error,
                    "knowledge_hits": [hit.to_dict(include_content=False) for hit in existing_hits],
                },
            )
        chunk: KnowledgeChunk | None = None
        if _can_write_back_source_result(result):
            chunk = _chunk_from_source_investigation(result)
            self.store.add_chunks(
                source_id=_DERIVED_SOURCE_ID,
                source_type="source_derived",
                title="源码调查沉淀知识",
                source_ref=result.coverage_boundary or str(self.config.guideengine_repo),
                chunks=[chunk],
                metadata={"generated_by": "SourceInvestigationRunner"},
            )
        return TaskResult(
            success=True,
            message=result.answer,
            stdout=result.stdout,
            stderr=result.stderr,
            command=result.command,
            details={
                "mode": "knowledge_qa",
                "question": question,
                "answer_type": "source_investigation",
                "canonical_key": result.canonical_key,
                "confidence": result.confidence,
                "coverage_boundary": result.coverage_boundary,
                "knowledge_hits": [chunk.to_dict()] if chunk else [],
            },
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


def _looks_like_signal_simulation_question(text: str) -> bool:
    lowered = text.casefold()
    has_intent = any(term in lowered for term in _SIMULATION_INTENT_TERMS) or any(
        term in text for term in _SIMULATION_INTENT_TERMS
    )
    if not has_intent:
        return False
    return (
        any(term in lowered for term in _SIGNAL_DOMAIN_TERMS)
        or "信号" in text
        or any(_template_positive_match(lowered, template) for template in _SIMULATION_TEMPLATES)
    )


def _build_simulation_result(question: str, hits: list[SearchHit]) -> TaskResult | None:
    matches, exact_signal = _matching_simulation_templates(question)
    if not matches:
        if _looks_like_complex_simulation_question(question):
            return TaskResult(
                success=True,
                message=_complex_simulation_policy_answer(),
                details={
                    "mode": "knowledge_qa",
                    "question": question,
                    "answer_type": "adb_simulation_policy",
                    "knowledge_hits": [hit.to_dict(include_content=False) for hit in hits],
                },
            )
        return None
    if not exact_signal and _looks_like_complex_simulation_question(question) and all(
        template.get("category") == "complex" for template in matches
    ):
        return TaskResult(
            success=True,
            message=_complex_simulation_policy_answer(),
            details={
                "mode": "knowledge_qa",
                "question": question,
                "answer_type": "adb_simulation_policy",
                "knowledge_hits": [hit.to_dict(include_content=False) for hit in _prefer_template_hits(hits, matches)],
            },
        )
    if exact_signal and len(matches) == 1 and not bool(matches[0].get("direct")):
        template = matches[0]
        return TaskResult(
            success=True,
            message=_custom_required_signal_answer(template),
            details={
                "mode": "knowledge_qa",
                "question": question,
                "answer_type": "adb_signal_custom_required",
                "exact_signal": exact_signal,
                "canonical_key": str(template.get("canonical_key") or ""),
                "candidates": [_candidate_payload(template)],
                "knowledge_hits": [
                    hit.to_dict(include_content=False) for hit in _primary_template_hits(hits, matches)
                ],
            },
        )
    if len(matches) == 1 and bool(matches[0].get("direct")):
        template = matches[0]
        return TaskResult(
            success=True,
            message=_direct_simulation_answer(template),
            details={
                "mode": "knowledge_qa",
                "question": question,
                "answer_type": "adb_signal_template",
                "exact_signal": exact_signal,
                "canonical_key": str(template.get("canonical_key") or ""),
                "knowledge_hits": [
                    hit.to_dict(include_content=False) for hit in _primary_template_hits(hits, matches)
                ],
            },
        )
    return TaskResult(
        success=True,
        message=_simulation_candidates_answer(matches),
        details={
            "mode": "knowledge_qa",
            "question": question,
            "answer_type": "adb_signal_candidates",
            "exact_signal": exact_signal,
            "candidates": [_candidate_payload(template) for template in matches],
            "knowledge_hits": [hit.to_dict(include_content=False) for hit in _prefer_template_hits(hits, matches)],
        },
    )


def _candidate_payload(template: dict[str, Any]) -> dict[str, Any]:
    return {
        "signal": str(template.get("signal") or ""),
        "code": str(template.get("code") or ""),
        "title": str(template.get("title") or ""),
        "direct": bool(template.get("direct")),
        "support": str(template.get("support") or ""),
    }


def _matching_simulation_templates(text: str) -> tuple[list[dict[str, Any]], bool]:
    lowered = text.casefold()
    exact_matches = [
        template
        for template in _SIMULATION_TEMPLATES
        if not _template_negative_match(lowered, template)
        and (
            _contains_signal_token(lowered, str(template.get("signal") or "").casefold())
            or (str(template.get("code") or "") and str(template.get("code")) in lowered)
        )
    ]
    if exact_matches:
        return _dedupe_templates(exact_matches), True

    matches: list[dict[str, Any]] = []
    for template in _SIMULATION_TEMPLATES:
        if _template_negative_match(lowered, template):
            continue
        if _template_positive_match(lowered, template):
            matches.append(template)
    if _looks_like_complex_simulation_question(text):
        matches.extend(template for template in _SIMULATION_TEMPLATES if template.get("category") == "complex")
    return _dedupe_templates(matches), False


def _template_positive_match(lowered_text: str, template: dict[str, Any]) -> bool:
    return any(_template_term_matches(lowered_text, term) for term in _template_terms(template))


def _template_negative_match(lowered_text: str, template: dict[str, Any]) -> bool:
    return any(_template_term_matches(lowered_text, term) for term in _template_terms(template, key="negative_aliases"))


def _template_terms(template: dict[str, Any], *, key: str = "aliases") -> list[str]:
    terms: list[str] = []
    if key == "aliases":
        for field in ("signal", "code", "title", "summary"):
            value = template.get(field)
            if value not in (None, ""):
                terms.append(str(value).casefold())
    for field in (key, "keywords") if key == "aliases" else (key,):
        values = template.get(field)
        if isinstance(values, list):
            terms.extend(str(value).casefold() for value in values if str(value).strip())
    return [term for term in terms if term]


def _template_term_matches(text: str, term: str) -> bool:
    lowered_text = (text or "").casefold()
    lowered_term = (term or "").casefold().strip()
    if not lowered_text or not lowered_term:
        return False
    if lowered_term in lowered_text:
        return True
    normalized_term = _normalize_template_match_text(lowered_term)
    if not normalized_term:
        return False
    return normalized_term in _normalize_template_match_text(lowered_text)


def _normalize_template_match_text(value: str) -> str:
    return _TEMPLATE_MATCH_SEPARATOR_RE.sub("", value.casefold())


def _looks_like_complex_simulation_question(text: str) -> bool:
    lowered = text.casefold()
    return any(term in lowered for term in ("pb", "proto", "protoobject", "bytearray", "byte array"))


def _contains_signal_token(text: str, signal: str) -> bool:
    return bool(signal) and re.search(rf"(?<![a-z0-9_]){re.escape(signal)}(?![a-z0-9_])", text) is not None


def _dedupe_templates(templates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for template in templates:
        template_id = str(template.get("id") or template.get("canonical_key") or template.get("signal") or "")
        if not template_id or template_id in seen:
            continue
        seen.add(template_id)
        deduped.append(template)
    return deduped


def _prefer_template_hits(hits: list[SearchHit], templates: list[dict[str, Any]]) -> list[SearchHit]:
    keywords: list[str] = []
    for template in templates:
        keywords.extend(_template_terms(template))
    lowered_keywords = [keyword.casefold() for keyword in keywords if keyword]
    preferred = [
        hit
        for hit in hits
        if any(_template_term_matches(hit.title + "\n" + hit.content, keyword) for keyword in lowered_keywords)
    ]
    return preferred or hits


def _primary_template_hits(hits: list[SearchHit], templates: list[dict[str, Any]]) -> list[SearchHit]:
    preferred = [*_prefer_template_hits(hits, templates), *_template_fallback_hits(templates)]
    return sorted(preferred, key=lambda hit: _template_reference_rank(hit, templates))[:1]


def _template_fallback_hits(templates: list[dict[str, Any]]) -> list[SearchHit]:
    fallback_hits: list[SearchHit] = []
    for template in templates:
        canonical_key = str(template.get("canonical_key") or template.get("id") or template.get("signal") or "").strip()
        if not canonical_key:
            continue
        title = str(template.get("title") or template.get("signal") or canonical_key).strip()
        content_parts = [
            title,
            str(template.get("summary") or "").strip(),
            str(template.get("signal") or "").strip(),
            str(template.get("code") or "").strip(),
        ]
        commands = template.get("commands")
        if isinstance(commands, list):
            content_parts.extend(str(command).strip() for command in commands)
        fallback_hits.append(
            SearchHit(
                chunk_id=f"template:{canonical_key}",
                source_id="bundled-adb-simulations",
                title=title,
                content="\n".join(part for part in content_parts if part),
                source_ref="generated:adb_simulation_templates",
                kind="adb_signal_template",
                score=0.0,
                metadata=dict(template),
            )
        )
    return fallback_hits


def _template_reference_rank(hit: SearchHit, templates: list[dict[str, Any]]) -> tuple[int, int, int, int, float, str]:
    signals = [str(template.get("signal") or "").casefold() for template in templates]
    codes = [str(template.get("code") or "").casefold() for template in templates]
    text = (hit.title + "\n" + hit.content).casefold()
    metadata_signal = str(hit.metadata.get("signal") or "").casefold()
    metadata_code = str(hit.metadata.get("code") or "").casefold()
    signal_match = metadata_signal in signals or any(_contains_signal_token(text, signal) for signal in signals)
    code_match = metadata_code in codes or any(code and code in text for code in codes)
    return (
        0 if signal_match or code_match else 1,
        0 if hit.kind == "adb_signal_template" else 1,
        0 if hit.source_id == _DERIVED_SOURCE_ID else 1,
        0 if hit.source_ref.startswith("generated:") else 1,
        -hit.score,
        hit.title,
    )


def _direct_simulation_answer(template: dict[str, Any]) -> str:
    answer_blocks = template.get("answer_blocks")
    if isinstance(answer_blocks, list) and answer_blocks:
        return "\n".join(str(block) for block in answer_blocks if str(block).strip())
    commands = template.get("commands")
    lines = [str(template.get("title") or template.get("signal") or "ADB 模拟指令")]
    if isinstance(commands, list):
        lines.extend(str(command) for command in commands if str(command).strip())
    return "\n".join(lines)


def _custom_required_signal_answer(template: dict[str, Any]) -> str:
    signal = str(template.get("signal") or "")
    code = str(template.get("code") or "")
    title = str(template.get("title") or signal)
    summary = str(template.get("summary") or "")
    return (
        f"{title}：{signal} ({code}) 当前不能生成通用 ADB 模拟命令。\n"
        f"{summary}\n\n"
        "原因：DataCenterBroadcastReceiver 的通用分支只把 value 字符串转换为基础类型；"
        "ByteArray/PB、ProtoObject、任意 JavaObject 需要源码里的 mockDataFactory 自定义构造对象。"
        "如果没有对应 factory，直接拼对象命令不会得到业务需要的 PB 对象。\n\n"
        "可行处理：\n"
        "1. 常用场景：新增 mockDataFactory 映射，根据简单 value 构造 PB/对象，再写入 dataCenter.mockSignal。\n"
        "2. 真实数据复现：用 ReplayReceiver/ProtocolFile 的 record 回放链路，把录制到的 ByteArray 按原始字节回灌。\n"
        "3. 知识库回答：没有源码 factory 或录制样本时，只能说明需要自定义，不能输出看似可执行的伪命令。"
    )


def _complex_simulation_policy_answer() -> str:
    return (
        "ADB 模拟要先按源码能力分层，不能只看 signal.proto 的 code/format：\n"
        "1. 基础类型可直接走通用广播：Int32、Int64、Float、Double、String、Boolean、Int32Array、FloatArray、DoubleArray。\n"
        f"示例形态：adb shell am broadcast -a {_MOCK_ACTION} --ei code <code> --ei format <format> --es value <value>\n"
        "2. 已有 mockDataFactory 的特殊对象可以给确定模板，例如源码里有 builder 的信号。\n"
        "3. ByteArray/PB、ProtoObject、任意 JavaObject 没有 factory 时不能通用模拟；value 字符串无法自动变成目标 PB 对象。\n"
        "4. 这类信号的解决方式是新增 mockDataFactory 自定义 builder，或使用 ReplayReceiver/ProtocolFile 的 record 回放链路灌真实 ByteArray。\n\n"
        "知识库输出策略：只对基础类型或已验证 factory 输出 ADB 命令；其他复杂类型明确提示“需要自定义”，不要生成看似通用的 PB ADB 命令。"
    )


def _simulation_candidates_answer(matches: list[dict[str, Any]]) -> str:
    if len(matches) == 1:
        lines = ["命中一个可能的 ADB 模拟信号，但当前还没有验证过的通用 ADB 模板："]
    else:
        intro = _candidate_intro(matches)
        lines = [intro or "命中多个可能的 ADB 模拟信号，先按用途选一个："]
    for index, template in enumerate(matches, start=1):
        signal = str(template.get("signal") or "")
        code = str(template.get("code") or "")
        title = str(template.get("title") or signal)
        summary = str(template.get("summary") or "")
        format_note = str(template.get("format_note") or "")
        lines.append(f"{index}. {title}：{signal} ({code})\n{summary}\n{format_note}".rstrip())
        example = str(template.get("example") or "")
        if example:
            lines.append(f"示例：{example}")
        lines.append(f"继续发：知识库 模拟 {signal}")
    return "\n\n".join(lines)


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


def _is_command_lookup_question(question: str) -> bool:
    lowered = question.casefold()
    command_terms = ("命令", "指令", "adb", "broadcast", "fastboot")
    return any(term in lowered for term in command_terms) or any(term in question for term in command_terms)


def _command_hits_answer(command_hits: list[tuple[SearchHit, list[str]]]) -> str:
    lines = ["知识库命中可执行命令："]
    for index, (hit, commands) in enumerate(command_hits, start=1):
        command_lines = "\n".join(commands[:3])
        lines.append(f"{index}. {hit.title}\n{command_lines}")
    return "\n\n".join(lines)


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


def _extract_command_lines(content: str) -> list[str]:
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        parsed = None
    raw_values = _flatten_string_values(parsed) if parsed is not None else [content]

    commands: list[str] = []
    for value in raw_values:
        for line in str(value).splitlines():
            command = _extract_command_from_line(line)
            if not command or command in commands:
                continue
            commands.append(command)
    return commands


def _flatten_string_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        values: list[str] = []
        for item in value.values():
            values.extend(_flatten_string_values(item))
        return values
    if isinstance(value, list):
        values = []
        for item in value:
            values.extend(_flatten_string_values(item))
        return values
    return []


def _extract_command_from_line(line: str) -> str:
    text = line.strip().strip("`")
    lowered = text.casefold()
    positions = [lowered.find(marker) for marker in _COMMAND_MARKERS if lowered.find(marker) >= 0]
    if not positions:
        return ""
    command = text[min(positions) :].strip()
    return command.rstrip("`,")


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


def _candidate_intro(matches: list[dict[str, Any]]) -> str:
    intros = {
        str(template.get("candidate_intro") or "").strip()
        for template in matches
        if str(template.get("candidate_intro") or "").strip()
    }
    if len(intros) == 1:
        return next(iter(intros))
    return ""


def _can_write_back_source_result(result: source_investigation.SourceInvestigationResult) -> bool:
    return (
        result.writeback_allowed
        and bool(result.canonical_key.strip())
        and result.confidence >= _MIN_WRITEBACK_CONFIDENCE
        and bool(result.source_evidence)
    )


def _is_negative_template_question(question: str) -> bool:
    lowered = (question or "").casefold()
    positive_match = any(_template_positive_match(lowered, template) for template in _SIMULATION_TEMPLATES)
    return not positive_match and any(_template_negative_match(lowered, template) for template in _SIMULATION_TEMPLATES)


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
