"""Read-only source investigation for knowledge QA.

Supports two execution paths:
1. **Direct API** — uses LLMClient to call the LLM directly (fast, ~3-8s).
2. **CLI subprocess** — falls back to ``codex exec`` (slow, ~30-120s).

The direct API path is preferred when the AI provider is configured and
enabled. The CLI path is kept as a fallback.
"""

from __future__ import annotations

import logging
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time
from typing import Any, TYPE_CHECKING
from .models import SearchHit
from ..models import BridgeConfig
from .. import prompt_snapshots

from .investigation.investigation_types import (  # noqa: F401
    SourceInvestigationResult,
    PendingSourceSnapshot,
)
from .investigation.warmup_state import (  # noqa: F401
    _CODEGRAPH_WARMUP_COOLDOWN_SECONDS,
    _CODEGRAPH_WARMUP_RUNNING_STALE_SECONDS,
    _warmup_repo_key,
    _codegraph_warmup_state_dir,
    _codegraph_repo_slug,
    _codegraph_repo_state_path,
    _codegraph_repo_lock_path,
    _read_codegraph_repo_state,
    _write_codegraph_repo_state,
    _as_float,
    _should_skip_codegraph_warmup_state,
    _try_repo_warmup_lock,
)
from .investigation.prompt_lines import (  # noqa: F401
    _LOW_VALUE_PATH_HINTS,
    _DEFAULT_PRIORITY_MODULES,
    _SIGNAL_PRIORITY_MODULES,
    _source_filter_prompt_lines,
    _candidate_hint_prompt_lines,
    _priority_module_prompt_lines,
    _priority_modules,
    _priority_module_roots,
    _prefetched_excerpt_prompt_lines,
    _code_index_prompt_lines,
)
from .investigation.path_utils import (  # noqa: F401
    _NATIVE_HINT_TERMS,
    _resolve_module_path,
    _display_repo_relative,
    _collect_file_excerpt_lines,
    _looks_like_native_question,
    _excerpt_tokens_for_module,
    _exact_excerpt_tokens,
)
from .investigation.local_probe import (  # noqa: F401
    _try_local_signal_probe,
    _signal_candidates_from_hits,
    _extract_signal_proto_field,
    _collect_priority_records,
    _collect_file_excerpt_records,
    _rank_signal_candidates,
    _detect_signal_format,
    _extract_datacenter_action,
    _local_probe_commands,
    _signal_format_code,
    _looks_like_binary_enable_signal,
    _local_probe_answer,
    _local_probe_evidence,
    _candidate_definition_evidence,
    _local_probe_coverage_boundary,
)
from .investigation.result_parse import (  # noqa: F401
    _result_from_payload,
    _schema_error,
    _parse_json_object,
    _last_json_object,
    _last_agent_message,
    _float,
    _extract_query_symbols,
    _code_index_confidence,
    _result_from_code_index,
    _codegraph_confidence,
    _result_from_codegraph,
    _codegraph_to_code_index_context,
)


logger = logging.getLogger(__name__)

_CODEGRAPH_WARMUP_LOCK = threading.Lock()

_CODEGRAPH_WARMUP_INFLIGHT: set[Path] = set()

class SourceInvestigationRunner:
    def __init__(self, config: BridgeConfig) -> None:
        self.config = config
        opts = config.source_investigation
        self._priority_modules = tuple(opts.priority_modules) if opts.priority_modules else _DEFAULT_PRIORITY_MODULES
        self._signal_priority_modules = (
            {k: tuple(v) for k, v in opts.signal_priority_modules.items()}
            if opts.signal_priority_modules
            else _SIGNAL_PRIORITY_MODULES
        )
        self._exclude_paths = tuple(opts.exclude_paths) if opts.exclude_paths else _LOW_VALUE_PATH_HINTS
        self._pending_snapshot_lock = threading.RLock()
        self._pending_source_snapshots: dict[str, PendingSourceSnapshot] = {}
        self._pending_snapshot_ttl_seconds = 1800.0
        self._pending_snapshot_max_entries = 64
        self._code_index: _CodeIndex | None = None
        self._codegraph: Any | None = None
        self._last_code_index_context: list[tuple[Path, Any]] | None = None

    def warmup_codegraph(self) -> None:
        """Incrementally sync indexed repo_roots in background daemon threads.

        Startup must not create new indexes for large repos.  If a repo is not
        already indexed, the request path will fall through to lighter evidence
        providers instead of paying an init cost.
        """
        opts = self.config.source_investigation
        if not opts.codegraph_enabled:
            return
        cg = self._get_codegraph()
        if cg is None:
            return
        repo_roots = [p.expanduser() for p in (opts.repo_roots or [self.config.guideengine_repo])]
        for repo in repo_roots:
            if not repo.exists():
                continue
            if not cg.is_indexed(repo):
                logger.debug("codegraph: %s is not indexed; skipping warmup", repo)
                continue
            repo_key = _warmup_repo_key(repo)
            now = time.time()
            with _try_repo_warmup_lock(self.config, repo) as lock_handle:
                if lock_handle is None:
                    logger.debug("codegraph: warmup lock unavailable for %s; skipping duplicate", repo)
                    break
                state = _read_codegraph_repo_state(self.config, repo)
                if _should_skip_codegraph_warmup_state(state, now=now):
                    logger.debug("codegraph: recent warmup state for %s; skipping startup sync", repo)
                    break
                with _CODEGRAPH_WARMUP_LOCK:
                    if repo_key in _CODEGRAPH_WARMUP_INFLIGHT:
                        logger.debug("codegraph: warmup already in flight for %s; skipping duplicate", repo)
                        break
                    _CODEGRAPH_WARMUP_INFLIGHT.add(repo_key)
                _write_codegraph_repo_state(
                    self.config,
                    repo,
                    {
                        "repo": str(repo),
                        "started_at": now,
                        "finished_at": None,
                        "success": False,
                        "owner_pid": os.getpid(),
                    },
                )
            t = threading.Thread(
                target=self._warmup_one_repo,
                args=(cg, repo, now),
                name=f"codegraph-sync-{repo.name}",
                daemon=True,
            )
            t.start()
            logger.info("codegraph: sync warmup started for %s (background)", repo)
            break

    def _warmup_one_repo(self, cg: Any, repo: Path, started_at: float) -> None:
        ok = False
        try:
            logger.info("codegraph: syncing %s …", repo)
            ok = bool(cg.sync_index(repo))
            if ok:
                logger.info("codegraph: sync finished for %s", repo)
            else:
                logger.warning("codegraph: sync failed for %s", repo)
        except Exception as exc:  # pragma: no cover
            logger.warning("codegraph: warmup error for %s: %s", repo, exc)
        finally:
            _write_codegraph_repo_state(
                self.config,
                repo,
                {
                    "repo": str(repo),
                    "started_at": started_at,
                    "finished_at": time.time(),
                    "success": ok,
                    "owner_pid": os.getpid(),
                },
            )
            repo_key = _warmup_repo_key(repo)
            with _CODEGRAPH_WARMUP_LOCK:
                _CODEGRAPH_WARMUP_INFLIGHT.discard(repo_key)
    def run(self, question: str, *, hits: list[SearchHit] | None = None) -> SourceInvestigationResult:
        options = self.config.source_investigation
        if not options.enabled:
            return SourceInvestigationResult(success=False, error="source investigation disabled")
        repo_roots = [path.expanduser() for path in (options.repo_roots or [self.config.guideengine_repo])]
        local_result = _try_local_signal_probe(question, hits or [], repo_roots)
        if local_result is not None:
            return local_result

        # CodeGraph fast path (semantic index, ~100-500ms)
        self._last_code_index_context = None
        if options.codegraph_enabled:
            cg_result = self._try_codegraph(question, hits=hits or [], repo_roots=repo_roots)
            if cg_result is not None:
                self._record_successful_non_local_snapshot(question, hits or [], cg_result)
                return cg_result

        # Code index fallback (ctags + rg, ~100-300ms)
        if options.code_index_enabled:
            ci_result = self._try_code_index(question, hits=hits or [], repo_roots=repo_roots)
            if ci_result is not None:
                self._record_successful_non_local_snapshot(question, hits or [], ci_result)
                return ci_result

        # Try direct API first (fast path, ~3-8s)
        api_result = self._run_via_api(question, hits=hits, repo_roots=repo_roots)
        if api_result is not None:
            self._record_successful_non_local_snapshot(question, hits or [], api_result)
            return api_result

        # Fall back to CLI subprocess (slow path, ~30-120s)
        if options.provider.strip().casefold() != "codex":
            return SourceInvestigationResult(success=False, error=f"unsupported provider: {options.provider}")
        cli_result = self._run_via_cli(question, hits=hits, repo_roots=repo_roots)
        self._record_successful_non_local_snapshot(question, hits or [], cli_result)
        return cli_result

    # ------------------------------------------------------------------
    # CodeGraph fast path (semantic code intelligence)
    # ------------------------------------------------------------------

    def _get_codegraph(self) -> Any | None:
        if self._codegraph is not None:
            return self._codegraph
        try:
            from .codegraph_client import CodeGraphClient
        except ImportError:
            return None
        opts = self.config.source_investigation
        client = CodeGraphClient(
            command=opts.codegraph_command,
            timeout=opts.codegraph_timeout_seconds,
        )
        if not client.is_available():
            return None
        self._codegraph = client
        return client

    def _try_codegraph(
        self,
        question: str,
        *,
        hits: list[SearchHit],
        repo_roots: list[Path],
    ) -> SourceInvestigationResult | None:
        """Try to answer from CodeGraph semantic index. Returns None on miss."""
        cg = self._get_codegraph()
        if cg is None:
            return None

        symbols = _extract_query_symbols(question, hits)
        if not symbols:
            return None

        from .codegraph_client import CgContext
        all_contexts: list[tuple[Path, CgContext]] = []
        all_callers: list[tuple[Path, str, list[Any]]] = []

        for repo in repo_roots:
            if not cg.is_indexed(repo):
                continue

            for symbol in symbols[:3]:
                cg_hits = cg.search_symbol(symbol, repo, limit=5)
                if cg_hits:
                    callers = cg.get_callers(symbol, repo, limit=10)
                    ctx = CgContext(
                        summary=f"Found {len(cg_hits)} definitions for {symbol}",
                        entry_points=[{
                            "name": h.name, "kind": h.kind,
                            "qualifiedName": h.qualified_name,
                            "filePath": h.path, "startLine": h.line,
                        } for h in cg_hits],
                    )
                    all_contexts.append((repo, ctx))
                    if callers:
                        all_callers.append((repo, symbol, callers))

        if not all_contexts:
            return None

        confidence = _codegraph_confidence(all_contexts, all_callers, question, hits)
        opts = self.config.source_investigation
        if confidence < opts.codegraph_min_confidence:
            # Cache for prompt enrichment
            self._last_code_index_context = [
                (repo, _codegraph_to_code_index_context(ctx, cg, repo))
                for repo, ctx in all_contexts
            ]
            return None

        return _result_from_codegraph(all_contexts, all_callers, question, hits, confidence=confidence)

    # ------------------------------------------------------------------
    # Code index fast path (ctags + rg)
    # ------------------------------------------------------------------

    def _get_code_index(self) -> _CodeIndex | None:
        if self._code_index is not None:
            return self._code_index
        try:
            from .code_index import CodeIndexClient
        except ImportError:
            return None
        opts = self.config.source_investigation
        repo_roots = [p.expanduser() for p in (opts.repo_roots or [self.config.guideengine_repo])]
        self._code_index = CodeIndexClient(
            repo_roots,
            ctags_command=opts.ctags_command,
            timeout=opts.code_index_timeout_seconds,
        )
        return self._code_index

    def _try_code_index(
        self,
        question: str,
        *,
        hits: list[SearchHit],
        repo_roots: list[Path],
    ) -> SourceInvestigationResult | None:
        """Try to answer from local code index. Returns None on miss."""
        idx = self._get_code_index()
        if idx is None or not idx.is_available():
            return None

        symbols = _extract_query_symbols(question, hits)
        if not symbols:
            return None

        from .code_index import CodeIndexContext
        all_contexts: list[tuple[Path, CodeIndexContext]] = []
        for repo in repo_roots:
            if not idx.is_available(repo):
                continue
            idx.ensure_index(repo)
            for symbol in symbols[:3]:
                ctx = idx.get_context(symbol, repo)
                if ctx and ctx.definitions:
                    all_contexts.append((repo, ctx))

        if not all_contexts:
            return None

        confidence = _code_index_confidence(all_contexts, question, hits)
        opts = self.config.source_investigation
        if confidence < opts.code_index_min_confidence:
            self._last_code_index_context = all_contexts
            return None

        return _result_from_code_index(all_contexts, question, hits, confidence=confidence)

    def _run_via_api(
        self,
        question: str,
        *,
        hits: list[SearchHit] | None = None,
        repo_roots: list[Path] | None = None,
    ) -> SourceInvestigationResult | None:
        """Direct API call to LLM — returns None if not available."""
        try:
            from ..agents.llm_client import LLMClient
        except ImportError:
            return None
        ai_opts = self.config.ai_provider
        client = LLMClient(ai_opts)
        if not client.is_available():
            return None
        prompt = self._prompt(question, hits=hits or [])
        system_prompt = (
            "You are a read-only source code investigator. "
            "Respond with a single JSON object (no markdown, no code block). "
            "Fields: answer(string), canonical_key(string), confidence(number 0-1), "
            "commands(array string), source_evidence(array object with file,line,text), "
            "coverage_boundary(string), writeback_allowed(boolean)."
        )
        try:
            started = time.monotonic()
            response = client.chat(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
                max_tokens=self.config.source_investigation.max_evidence * 200 + 2048,
                timeout_seconds=max(1.0, self.config.source_investigation.timeout_seconds),
            )
            duration = time.monotonic() - started
            logger.info(
                "source investigation via API completed in %.1fs (model=%s)",
                duration, response.model,
            )
        except Exception as exc:
            logger.warning("source investigation API call failed, falling back to CLI: %s", exc)
            return None

        parsed = _parse_json_object(response.content)
        if parsed is None:
            logger.warning("source investigation API returned non-JSON, falling back to CLI")
            return None
        schema_error = _schema_error(parsed)
        if schema_error:
            logger.warning("source investigation API returned invalid schema: %s", schema_error)
            return None
        return _result_from_payload(parsed, command=["llm_client.chat()"], stdout=response.content, stderr="")

    def _run_via_cli(
        self,
        question: str,
        *,
        hits: list[SearchHit] | None = None,
        repo_roots: list[Path] | None = None,
    ) -> SourceInvestigationResult:
        """Original CLI subprocess path (codex exec)."""
        options = self.config.source_investigation
        roots = repo_roots or [path.expanduser() for path in (options.repo_roots or [self.config.guideengine_repo])]
        primary_root = roots[0] if roots else self.config.guideengine_repo
        output_path = self._output_path()
        command = self._build_command(
            question=question,
            output_path=output_path,
            primary_root=primary_root,
            hits=hits or [],
        )
        try:
            completed = subprocess.run(
                command,
                cwd=str(primary_root),
                capture_output=True,
                text=True,
                timeout=max(1, int(options.timeout_seconds)),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return SourceInvestigationResult(
                success=False,
                error=f"source investigation timed out after {exc.timeout}s",
                command=command,
                stdout=str(exc.output or ""),
                stderr=str(exc.stderr or ""),
            )
        except OSError as exc:
            return SourceInvestigationResult(success=False, error=str(exc), command=command)
        if completed.returncode != 0:
            return SourceInvestigationResult(
                success=False,
                error=(completed.stderr or completed.stdout or f"codex exited {completed.returncode}")[:1000],
                command=command,
                stdout=completed.stdout or "",
                stderr=completed.stderr or "",
            )
        raw = ""
        if output_path.is_file():
            raw = output_path.read_text(encoding="utf-8", errors="replace")
        if not raw.strip():
            raw = _last_agent_message(completed.stdout or "") or _last_json_object(completed.stdout or "")
        parsed = _parse_json_object(raw)
        if parsed is None:
            return SourceInvestigationResult(
                success=False,
                error="source investigation returned non-json output",
                command=command,
                stdout=completed.stdout or "",
                stderr=completed.stderr or "",
            )
        schema_error = _schema_error(parsed)
        if schema_error:
            return SourceInvestigationResult(
                success=False,
                error=f"source investigation returned invalid schema: {schema_error}",
                command=command,
                stdout=completed.stdout or "",
                stderr=completed.stderr or "",
            )
        return _result_from_payload(parsed, command=command, stdout=completed.stdout or "", stderr=completed.stderr or "")

    def _build_command(
        self,
        *,
        question: str,
        output_path: Path,
        primary_root: Path,
        hits: list[SearchHit] | None = None,
    ) -> list[str]:
        options = self.config.source_investigation
        command = [
            options.command.strip() or "codex",
            "exec",
            "--skip-git-repo-check",
            "-s",
            "read-only",
            "-C",
            str(primary_root),
        ]
        for path in options.add_dirs:
            command.extend(["--add-dir", str(path.expanduser())])
        if options.model.strip():
            command.extend(["-m", options.model.strip()])
        command.extend(
            [
                "--json",
                "--output-last-message",
                str(output_path),
                self._prompt(question, hits=hits or []),
            ]
        )
        return command

    def _prompt(self, question: str, *, hits: list[SearchHit] | None = None) -> str:
        loaded_snapshot = self._load_source_snapshot(question, hits or [])
        if loaded_snapshot is None:
            return self._prompt_without_snapshot(question, hits=hits or [], include_question_label=True)
        return "\n\n".join(
            [
                self._render_source_snapshot_prefix(loaded_snapshot),
                f"### 当前问题增量\n{question}",
                self._prompt_without_snapshot(question, hits=hits or [], include_question_label=False),
            ]
        ).strip()

    def _prompt_without_snapshot(
        self,
        question: str,
        *,
        hits: list[SearchHit] | None = None,
        include_question_label: bool,
    ) -> str:
        options = self.config.source_investigation
        repo_roots = options.repo_roots or [self.config.guideengine_repo]
        add_dirs = options.add_dirs
        prompt_parts = [
            "你是 lark-agent-bridge 的只读源码调查 agent。",
            "你是被主流程派发的子 agent，不是主对话代理。",
            "不要做 memory pass，不要读取技能文件，不要使用 using-superpowers、ask、brainstorming 等流程技能。",
            "直接按本提示执行源码调查：rg 定位 -> 读关键片段 -> 输出 JSON。",
            "目标：回答用户的知识库/ADB/源码可验证问题，并判断是否可沉淀为知识库模板。",
            "限制：只读；优先使用 rg 搜索锚点；只读取关键片段；不要读取全仓大文件；不要修改文件。",
            "源码根：",
            *[f"- {path}" for path in repo_roots],
        ]
        if include_question_label:
            prompt_parts.insert(6, f"用户问题：{question}")
        if add_dirs:
            prompt_parts.extend(["附加只读目录：", *[f"- {path}" for path in add_dirs]])
        prompt_parts.extend(_source_filter_prompt_lines(question, exclude_paths=self._exclude_paths))
        prompt_parts.extend(_candidate_hint_prompt_lines(hits or [], repo_roots))
        prompt_parts.extend(_priority_module_prompt_lines(
            question, hits or [], repo_roots,
            default_modules=self._priority_modules,
            signal_modules=self._signal_priority_modules,
        ))
        prompt_parts.extend(_prefetched_excerpt_prompt_lines(
            question, hits or [], repo_roots,
            default_modules=self._priority_modules,
            signal_modules=self._signal_priority_modules,
        ))
        # Inject code index context from low-confidence miss
        if self._last_code_index_context:
            ci_lines = _code_index_prompt_lines(self._last_code_index_context)
            if ci_lines:
                prompt_parts.extend(ci_lines)
        prompt_parts.append(
            "输出必须是一个 JSON 对象，不要 Markdown，不要代码块。字段："
            "answer(string), canonical_key(string), confidence(number 0-1), commands(array string), "
            "source_evidence(array object with file,line,text), coverage_boundary(string), writeback_allowed(boolean)。"
        )
        prompt_parts.append(f"source_evidence 最多 {max(1, int(options.max_evidence))} 条。")
        prompt_parts.append("只有源码证据能支持结论时 writeback_allowed 才能为 true。")
        return "\n".join(part for part in prompt_parts if part)

    def _source_snapshot_key(self, question: str, hits: list[SearchHit]) -> str:
        semantic_identifier = self._best_source_family_identifier(hits)
        if semantic_identifier:
            return semantic_identifier[:120]
        normalized_question = self._normalize_source_question_family(question)
        return normalized_question[:120]

    def _best_source_family_identifier(self, hits: list[SearchHit]) -> str:
        candidates: list[tuple[float, int, str]] = []
        for hit in hits[:5]:
            metadata = hit.metadata or {}
            if not isinstance(metadata, dict):
                continue
            for priority, field_name in enumerate(("canonical_key", "signal", "code"), start=1):
                raw = str(metadata.get(field_name) or "").strip()
                if not raw:
                    continue
                normalized = re.sub(r"[^a-z0-9\u4e00-\u9fff:_-]+", "-", raw.casefold()).strip("-")
                if normalized:
                    candidates.append((float(hit.score or 0.0), priority, normalized))
                    break
        if not candidates:
            return ""
        candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
        return candidates[0][2]

    def _source_snapshot_path(self, key: str) -> Path:
        return ((self.config.data_dir / "source_investigations" / "snapshots" / f"{key}.json").expanduser().resolve())

    def _normalize_source_question_family(self, question: str) -> str:
        lowered = question.casefold()
        for token in (
            "源码分析",
            "源码调查",
            "基于源码",
            "重新源码",
            "重新调查",
            "源码",
            "调查",
            "分析",
            "如何",
            "怎么",
            "帮我",
            "一下",
        ):
            lowered = lowered.replace(token, " ")
        lowered = re.sub(r"^\s*请", " ", lowered)
        lowered = re.sub(r"[^\w\u4e00-\u9fff]+", " ", lowered)
        lowered = re.sub(r"\s+", "-", lowered).strip("-")
        return lowered

    def _load_source_snapshot(
        self,
        question: str,
        hits: list[SearchHit],
    ) -> prompt_snapshots.BugPromptSnapshot | None:
        key = self._source_snapshot_key(question, hits)
        if not key:
            return None
        path = self._source_snapshot_path(key)
        if path.exists():
            try:
                return prompt_snapshots.read_prompt_snapshot(path)
            except (OSError, ValueError):
                return None
        now = time.monotonic()
        with self._pending_snapshot_lock:
            self._cleanup_pending_source_snapshots_locked(now)
            pending = self._pending_source_snapshots.get(key)
            if pending is None:
                return None
            pending.last_seen_monotonic = now
            return pending.snapshot

    def _render_source_snapshot_prefix(self, snapshot: prompt_snapshots.BugPromptSnapshot) -> str:
        lines = ["### 调查事实快照"]
        lines.extend(f"- {fact.label}: {fact.value}" for fact in snapshot.stable_facts)
        if snapshot.evidence_refs:
            lines.append("### 证据目录")
            for item in snapshot.evidence_refs:
                locator = f" {item.locator}" if item.locator else ""
                lines.append(f"- {item.title}: {item.path}{locator}")
        if snapshot.open_questions:
            lines.append("### 未决问题")
            lines.extend(f"- {question}" for question in snapshot.open_questions)
        return "\n".join(lines)

    def _record_successful_non_local_snapshot(
        self,
        question: str,
        hits: list[SearchHit],
        result: SourceInvestigationResult,
    ) -> None:
        if not result.success:
            return
        key = self._source_snapshot_key(question, hits)
        if not key:
            return
        stable_facts: list[prompt_snapshots.SnapshotFact] = [
            prompt_snapshots.SnapshotFact(label="question_family", value=key),
        ]
        if result.canonical_key.strip():
            stable_facts.append(prompt_snapshots.SnapshotFact(label="canonical_key", value=result.canonical_key.strip()))
        if result.coverage_boundary.strip():
            stable_facts.append(
                prompt_snapshots.SnapshotFact(label="coverage_boundary", value=result.coverage_boundary.strip())
            )
        seen_signals: set[str] = set()
        for hit in hits[:5]:
            signal = str(hit.metadata.get("signal") or "").strip()
            if signal and signal not in seen_signals:
                seen_signals.add(signal)
                stable_facts.append(prompt_snapshots.SnapshotFact(label="signal", value=signal))
        evidence_refs: list[prompt_snapshots.SnapshotEvidence] = []
        for item in result.source_evidence[:5]:
            if not isinstance(item, dict):
                continue
            file_path = str(item.get("file") or "").strip()
            if not file_path:
                continue
            line = str(item.get("line") or "").strip()
            locator = f"L{line}" if line else ""
            text = str(item.get("text") or "").strip()
            title = text[:80] if text else Path(file_path).name
            evidence_refs.append(prompt_snapshots.SnapshotEvidence(title=title, path=file_path, locator=locator))
        snapshot = prompt_snapshots.BugPromptSnapshot(
            scope_key=key,
            analysis_kind="source_investigation",
            stable_facts=stable_facts,
            evidence_refs=evidence_refs,
            open_questions=[],
        )
        path = self._source_snapshot_path(key)
        if path.exists():
            prompt_snapshots.write_prompt_snapshot(path, snapshot)
            with self._pending_snapshot_lock:
                self._pending_source_snapshots.pop(key, None)
            return
        now = time.monotonic()
        with self._pending_snapshot_lock:
            self._cleanup_pending_source_snapshots_locked(now)
            pending = self._pending_source_snapshots.get(key)
            if pending is None:
                self._remember_pending_source_snapshot_locked(key, snapshot, now)
                return
            self._promote_pending_source_snapshot_locked(key, snapshot, path)

    def _remember_pending_source_snapshot_locked(
        self,
        key: str,
        snapshot: prompt_snapshots.BugPromptSnapshot,
        now: float,
    ) -> None:
        self._pending_source_snapshots[key] = PendingSourceSnapshot(
            snapshot=snapshot,
            first_seen_monotonic=now,
            last_seen_monotonic=now,
        )
        self._cleanup_pending_source_snapshots_locked(now)

    def _promote_pending_source_snapshot_locked(
        self,
        key: str,
        snapshot: prompt_snapshots.BugPromptSnapshot,
        path: Path,
    ) -> None:
        prompt_snapshots.write_prompt_snapshot(path, snapshot)
        self._pending_source_snapshots.pop(key, None)

    def _cleanup_pending_source_snapshots_locked(self, now: float) -> None:
        stale_keys = [
            key
            for key, pending in self._pending_source_snapshots.items()
            if now - pending.last_seen_monotonic > self._pending_snapshot_ttl_seconds
        ]
        for key in stale_keys:
            self._pending_source_snapshots.pop(key, None)
        overflow = len(self._pending_source_snapshots) - self._pending_snapshot_max_entries
        if overflow <= 0:
            return
        oldest = sorted(
            self._pending_source_snapshots.items(),
            key=lambda item: (item[1].last_seen_monotonic, item[1].first_seen_monotonic, item[0]),
        )
        for key, _pending in oldest[:overflow]:
            self._pending_source_snapshots.pop(key, None)

    def _output_path(self) -> Path:
        base = (self.config.data_dir / "source_investigations").expanduser()
        base.mkdir(parents=True, exist_ok=True)
        return (base / f"source_investigation_{int(time.time() * 1000)}.json").resolve()
