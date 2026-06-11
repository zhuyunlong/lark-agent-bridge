"""Knowledge-base orchestration and answer generation."""

from __future__ import annotations

import hashlib
import threading
from typing import Any
from ..models import BridgeConfig, KnowledgeSourceOptions, TaskResult
from . import source_investigation
from .ingestors import KnowledgeIngestError, ingest_source, source_ref, source_title
from .models import KnowledgeChunk, SearchHit
from .store import KnowledgeStore, query_terms

from .answers.answer_simulation import (  # noqa: F401
    _SIMULATION_INTENT_TERMS,
    _MOCK_ACTION,
    _SIMULATION_TEMPLATES,
    _DERIVED_SOURCE_ID,
    _TEMPLATE_MATCH_SEPARATOR_RE,
    _looks_like_signal_simulation_question,
    _build_simulation_result,
    _candidate_payload,
    _matching_simulation_templates,
    _template_positive_match,
    _template_negative_match,
    _template_terms,
    _template_term_matches,
    _normalize_template_match_text,
    _looks_like_complex_simulation_question,
    _contains_signal_token,
    _dedupe_templates,
    _prefer_template_hits,
    _primary_template_hits,
    _template_fallback_hits,
    _template_reference_rank,
    _direct_simulation_answer,
    _custom_required_signal_answer,
    _complex_simulation_policy_answer,
    _simulation_candidates_answer,
    _is_negative_template_question,
)
from .answers.answer_signal import (  # noqa: F401
    _build_signal_source_candidate_result,
    _signal_source_candidates,
    _signal_source_candidates_answer,
    _extract_signal_proto_field,
    _signal_proto_summary,
    _generic_answer,
)
from .answers.answer_command import (  # noqa: F401
    _COMMAND_MARKERS,
    _is_command_lookup_question,
    _command_hits_answer,
    _extract_command_lines,
    _flatten_string_values,
    _extract_command_from_line,
)
from .answers.answer_lowconf import (  # noqa: F401
    _SIGNAL_DOMAIN_TERMS,
    _MIN_RETRIEVAL_SCORE,
    _LOW_CONFIDENCE_MAX_CANDIDATES,
    _LOW_CONFIDENCE_SEARCH_LIMIT,
    _LOW_CONFIDENCE_GENERIC_TOPIC_TERMS,
    _low_confidence_topic_terms,
    _specific_low_confidence_topic_terms,
    _filter_specific_low_confidence_topic_terms,
    _is_low_confidence_topic_term,
    _should_offer_low_confidence_candidates,
    _should_prefer_executable_candidates,
    _filter_command_hits_by_specific_terms,
    _has_specific_non_command_hits,
    _has_explicit_signal_domain,
    _low_confidence_command_candidates,
    _search_hit_matches_any_topic_term,
    _search_text_matches_topic_term,
    _low_confidence_candidates_answer,
    _candidate_intro,
    _build_command_hit_result,
)
from .answers.answer_source import (  # noqa: F401
    _MIN_WRITEBACK_CONFIDENCE,
    _SOURCE_INVESTIGATION_TERMS,
    _source_metadata,
    _source_investigation_unavailable_answer,
    _strip_trigger_prefix,
    _should_record_source_derived_simulation,
    _derived_adb_simulation_chunks,
    _should_run_source_investigation,
    _explicit_source_investigation_requested,
    _can_write_back_source_result,
    _chunk_from_source_investigation,
    _preview,
)


class KnowledgeService:
    def __init__(self, config: BridgeConfig, *, warmup_codegraph: bool = True) -> None:
        self.config = config
        self.store = KnowledgeStore(config.knowledge.storage)
        self._ready_lock = threading.RLock()
        self._configured_sources_checked = False
        self._source_investigation_runner = source_investigation.SourceInvestigationRunner(config)
        if warmup_codegraph:
            self._source_investigation_runner.warmup_codegraph()

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
