"""Read-only source investigation for knowledge QA.

Supports two execution paths:
1. **Direct API** — uses LLMClient to call the LLM directly (fast, ~3-8s).
2. **CLI subprocess** — falls back to ``codex exec`` (slow, ~30-120s).

The direct API path is preferred when the AI provider is configured and
enabled. The CLI path is kept as a fallback.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import subprocess
import threading
import time
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .code_index import CodeIndexClient as _CodeIndex
else:
    _CodeIndex = Any

from .models import SearchHit
from ..models import BridgeConfig
from .. import prompt_snapshots

logger = logging.getLogger(__name__)

_CODEGRAPH_WARMUP_LOCK = threading.Lock()
_CODEGRAPH_WARMUP_INFLIGHT: set[Path] = set()

_LOW_VALUE_PATH_HINTS = (
    "src/test",
    "src/androidTest",
    "build/",
    "generated/",
    "third_party/",
    "*.pb.cc",
    "*.pb.h",
    "*.pb.c",
)
_DEFAULT_PRIORITY_MODULES = (
    "module_floorcenter/module_proto/src/main/proto/signal.proto",
    "module_floorcenter/module_datacenter/src/main/java/com/xiaopeng/guideengine/define/mapping/code/SignalMapping.kt",
    "module_floorcenter/module_datacenter/src/main/java/com/xiaopeng/guideengine/center/DataCenter.kt",
    "module_floorcenter/module_datacenter/src/main/java/com/xiaopeng/guideengine/debug/broadcast/DataCenterBroadcastReceiver.java",
)
_SIGNAL_PRIORITY_MODULES = {
    "SIGNAL_CTL_": (
        "module_floorcenter/module_datacenter/src/main/java/com/xiaopeng/guideengine/helper/carcontrol/CarCtlXpilotHelper.kt",
        "module_core/unity_service/src/main/java/com/xiaopeng/guideengine/unity_service/channel/to_unity/AndroidUnityProxy.kt",
    ),
    "SIGNAL_X3D_": (
        "module_core/module_xdata_service/src/main/java/com/xiaopeng/guideengine/xdatanative/transport/XDataTransport.kt",
        "module_core/subreality_biz/src/main/java/com/xiaopeng/ainavi/subreality_biz/tips/TipsBizService.kt",
        "module_display/launcher_subreality_service/src/main/java/com/xiaopeng/ainavi/tips/TipsServiceRepository.kt",
        "module_display/launcher_subreality_service/src/main/java/com/xiaopeng/ainavi/utils/TipsMsgHelper.kt",
    ),
    "SIGNAL_XUI_": (
        "module_floorcenter/module_datacenter/src/main/java/com/xiaopeng/guideengine/helper/xuimanager/XuiContextInfoHelper.kt",
    ),
}
_NATIVE_HINT_TERMS = ("native", "jni", "c++", "cpp", ".so", "tombstone", "addr2line", "崩溃", "闪退")


def _warmup_repo_key(repo: Path) -> Path:
    try:
        return repo.expanduser().resolve()
    except OSError:
        return repo.expanduser()


@dataclass(slots=True)
class SourceInvestigationResult:
    success: bool
    answer: str = ""
    canonical_key: str = ""
    confidence: float = 0.0
    commands: list[str] = field(default_factory=list)
    source_evidence: list[dict[str, Any]] = field(default_factory=list)
    coverage_boundary: str = ""
    writeback_allowed: bool = False
    error: str = ""
    command: list[str] = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""


@dataclass(slots=True)
class PendingSourceSnapshot:
    snapshot: prompt_snapshots.BugPromptSnapshot
    first_seen_monotonic: float
    last_seen_monotonic: float


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
            with _CODEGRAPH_WARMUP_LOCK:
                if repo_key in _CODEGRAPH_WARMUP_INFLIGHT:
                    logger.debug("codegraph: warmup already in flight for %s; skipping duplicate", repo)
                    continue
                _CODEGRAPH_WARMUP_INFLIGHT.add(repo_key)
            t = threading.Thread(
                target=self._warmup_one_repo,
                args=(cg, repo),
                name=f"codegraph-sync-{repo.name}",
                daemon=True,
            )
            t.start()
            logger.info("codegraph: sync warmup started for %s (background)", repo)

    def _warmup_one_repo(self, cg: Any, repo: Path) -> None:
        try:
            logger.info("codegraph: syncing %s …", repo)
            ok = cg.sync_index(repo)
            if ok:
                logger.info("codegraph: sync finished for %s", repo)
            else:
                logger.warning("codegraph: sync failed for %s", repo)
        except Exception as exc:  # pragma: no cover
            logger.warning("codegraph: warmup error for %s: %s", repo, exc)
        finally:
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


def _source_filter_prompt_lines(
    question: str,
    *,
    exclude_paths: tuple[str, ...] = _LOW_VALUE_PATH_HINTS,
) -> list[str]:
    native_clause = (
        "问题明确涉及 native/JNI/C++ 时，才允许扩展到手写 .cpp/.cc/.h；否则不要读取这些文件。"
        if _looks_like_native_question(question)
        else "除非问题明确涉及 native/JNI/C++，否则不要读取 .cpp/.cc/.h。"
    )
    return [
        "搜索/读码规则：",
        "1. 默认只看手写 .kt/.java/.proto；先定义/映射/分发，再看下游消费。",
        f"2. 忽略以下低价值路径或文件：{', '.join(exclude_paths)}。",
        f"3. {native_clause}",
        "4. 使用 rg 时优先带排除条件，避免扫测试、generated、第三方和 proto 生成产物。",
    ]


def _candidate_hint_prompt_lines(hits: list[SearchHit], repo_roots: list[Path]) -> list[str]:
    if not hits:
        return []
    lines = [
        "候选锚点（仅用于缩小搜索范围，不代表最终结论）：",
        "你必须先验证候选是否真的匹配用户问题；如果候选与源码不符，必须推翻候选并继续搜索。",
    ]
    for index, hit in enumerate(hits[:3], start=1):
        signal = str(hit.metadata.get("signal") or "").strip()
        code = str(hit.metadata.get("code") or "").strip()
        source = _display_repo_relative(hit.source_ref, repo_roots)
        detail = " | ".join(
            part
            for part in (
                hit.title.strip() or hit.source_id.strip(),
                f"signal={signal}" if signal else "",
                f"code={code}" if code else "",
                f"source={source}" if source else "",
            )
            if part
        )
        lines.append(f"{index}. {detail}")
    return lines


def _priority_module_prompt_lines(
    question: str,
    hits: list[SearchHit],
    repo_roots: list[Path],
    *,
    default_modules: tuple[str, ...] = _DEFAULT_PRIORITY_MODULES,
    signal_modules: dict[str, tuple[str, ...]] = _SIGNAL_PRIORITY_MODULES,
) -> list[str]:
    modules = _priority_modules(
        question, hits, repo_roots,
        default_modules=default_modules,
        signal_modules=signal_modules,
    )
    if not modules:
        return []
    roots = _priority_module_roots(modules)
    return [
        "优先阅读这些重点模块（先看定义/映射/分发，再看下游消费；仍需自行验证是否相关）：",
        *[f"- {module}" for module in modules],
        "执行边界：",
        "1. 最多执行 12 个命令。",
        "2. 第一阶段只允许读取上面的重点模块，不要先做全仓搜索。",
        f"3. 第二阶段若仍不足，只允许在重点模块所在目录内补充 rg：{', '.join(roots)}。",
        "4. 第三阶段如果仍不能确认，直接输出低置信边界，不要继续扩仓搜索。",
        "5. 只要已经确认信号定义、映射/生产链、注入能力、至少一条消费/transport 证据，就立即停止搜索并输出 JSON。",
    ]


def _priority_modules(
    question: str,
    hits: list[SearchHit],
    repo_roots: list[Path],
    *,
    default_modules: tuple[str, ...] = _DEFAULT_PRIORITY_MODULES,
    signal_modules: dict[str, tuple[str, ...]] = _SIGNAL_PRIORITY_MODULES,
) -> list[str]:
    modules: list[str] = []
    seen: set[str] = set()

    def add(path: str) -> None:
        cleaned = path.strip()
        if not cleaned or cleaned in seen:
            return
        seen.add(cleaned)
        modules.append(cleaned)

    has_signal_hits = any(hit.kind == "signal_proto_entry" or str(hit.metadata.get("signal") or "").strip() for hit in hits)
    if "信号" in question or has_signal_hits:
        for path in default_modules:
            add(path)
    for hit in hits[:5]:
        source = _display_repo_relative(hit.source_ref, repo_roots)
        if source.endswith((".kt", ".java", ".proto")):
            add(source)
        signal = str(hit.metadata.get("signal") or "").strip()
        for prefix, paths in signal_modules.items():
            if signal.startswith(prefix):
                for path in paths:
                    add(path)
    return modules


def _priority_module_roots(modules: list[str]) -> list[str]:
    roots: list[str] = []
    seen: set[str] = set()
    for module in modules:
        parts = Path(module).parts
        if len(parts) >= 2:
            root = "/".join(parts[:2])
        else:
            root = module
        if root and root not in seen:
            seen.add(root)
            roots.append(root)
    return roots


def _prefetched_excerpt_prompt_lines(
    question: str,
    hits: list[SearchHit],
    repo_roots: list[Path],
    *,
    default_modules: tuple[str, ...] = _DEFAULT_PRIORITY_MODULES,
    signal_modules: dict[str, tuple[str, ...]] = _SIGNAL_PRIORITY_MODULES,
) -> list[str]:
    modules = _priority_modules(
        question, hits, repo_roots,
        default_modules=default_modules,
        signal_modules=signal_modules,
    )
    excerpts: list[str] = []
    for module in modules:
        full_path = _resolve_module_path(module, repo_roots)
        if not full_path or not full_path.is_file():
            continue
        lines = _collect_file_excerpt_lines(full_path, module, question, hits)
        excerpts.extend(lines)
        if len(excerpts) >= 8:
            break
    if not excerpts:
        return []
    return [
        "预采样源码摘录（这些是主流程提前抽取的关键片段；如果已足够回答，就不要再搜索）：",
        *excerpts[:8],
    ]


def _try_local_signal_probe(
    question: str,
    hits: list[SearchHit],
    repo_roots: list[Path],
) -> SourceInvestigationResult | None:
    candidates = _signal_candidates_from_hits(hits)
    if not candidates:
        return None
    modules = _priority_modules(question, hits, repo_roots)
    if not modules:
        return None
    records = _collect_priority_records(question, hits, repo_roots, modules)
    if not records:
        return None
    datacenter_action = _extract_datacenter_action(records)
    if not datacenter_action:
        return None
    ranked = _rank_signal_candidates(candidates, records)
    primary = ranked[0] if ranked else None
    if primary is None or not primary["has_mapping"] or not primary["has_producer"]:
        return None
    downstream = [item for item in ranked[1:] if item["has_consumer"]]
    command_lines = _local_probe_commands(primary, datacenter_action)
    if not command_lines:
        return None
    answer = _local_probe_answer(question, primary, downstream, datacenter_action, command_lines)
    evidence = _local_probe_evidence(primary, downstream, datacenter_action)
    if len(evidence) < 4:
        return None
    coverage_boundary = _local_probe_coverage_boundary(primary, downstream)
    return SourceInvestigationResult(
        success=True,
        answer=answer,
        canonical_key="guideengine.signal.front_car_start_remind.mock_path" if "前车起步" in question else "",
        confidence=0.82 if downstream else 0.74,
        commands=command_lines,
        source_evidence=evidence[:12],
        coverage_boundary=coverage_boundary,
        writeback_allowed=False,
    )


def _signal_candidates_from_hits(hits: list[SearchHit]) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    seen: set[str] = set()
    for hit in hits:
        if hit.kind != "signal_proto_entry":
            continue
        signal = str(hit.metadata.get("signal") or "").strip()
        if not signal or signal in seen:
            continue
        seen.add(signal)
        candidates.append(
            {
                "signal": signal,
                "code": str(hit.metadata.get("code") or "").strip(),
                "title": hit.title.strip(),
                "comment": _extract_signal_proto_field(hit.content, "comment"),
                "source_ref": str(hit.source_ref or "").strip(),
                "line": str(hit.metadata.get("line") or "").strip(),
            }
        )
    return candidates


def _extract_signal_proto_field(content: str, field: str) -> str:
    prefix = f"{field}:"
    for line in (content or "").splitlines():
        stripped = line.strip()
        if stripped.startswith(prefix):
            return stripped[len(prefix) :].strip()
    return ""


def _collect_priority_records(
    question: str,
    hits: list[SearchHit],
    repo_roots: list[Path],
    modules: list[str],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for module in modules:
        full_path = _resolve_module_path(module, repo_roots)
        if not full_path or not full_path.is_file():
            continue
        records.extend(_collect_file_excerpt_records(full_path, module, question, hits))
    return records


def _collect_file_excerpt_records(
    full_path: Path,
    module: str,
    question: str,
    hits: list[SearchHit],
) -> list[dict[str, Any]]:
    try:
        lines = full_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    exact_tokens = _exact_excerpt_tokens(question, hits)
    tokens = _excerpt_tokens_for_module(module, question, hits)
    records: list[dict[str, Any]] = []
    seen: set[int] = set()
    exact_match_lines = [
        lineno
        for lineno, line in enumerate(lines, start=1)
        if any(token and token in line for token in exact_tokens)
    ]
    for lineno in exact_match_lines[-2:]:
        for near in (lineno - 1, lineno, lineno + 1):
            if near < 1 or near > len(lines) or near in seen:
                continue
            seen.add(near)
            records.append({"file": module, "line": near, "text": lines[near - 1].strip()})
            if len(records) >= 6:
                break
        if len(records) >= 6:
            break
    if len(records) < 4:
        for lineno, line in enumerate(lines, start=1):
            if not any(token and token in line for token in tokens):
                continue
            if lineno in seen:
                continue
            seen.add(lineno)
            records.append({"file": module, "line": lineno, "text": line.strip()})
            if len(records) >= 6:
                break
    return records


def _exact_excerpt_tokens(question: str, hits: list[SearchHit]) -> list[str]:
    tokens: list[str] = []
    for hit in hits[:5]:
        signal = str(hit.metadata.get("signal") or "").strip()
        code = str(hit.metadata.get("code") or "").strip()
        if signal:
            tokens.append(signal)
        if code:
            tokens.append(code)
    if "前车起步" in question:
        tokens.append("前车起步")
    seen: set[str] = set()
    deduped: list[str] = []
    for token in tokens:
        if token and token not in seen:
            seen.add(token)
            deduped.append(token)
    return deduped


def _rank_signal_candidates(candidates: list[dict[str, str]], records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ranked: list[dict[str, Any]] = []
    for candidate in candidates:
        signal = candidate["signal"]
        candidate_files = {
            str(record.get("file") or "")
            for record in records
            if signal in str(record.get("text") or "")
        }
        matched = [
            record
            for record in records
            if signal in str(record.get("text") or "") or str(record.get("file") or "") in candidate_files
        ]
        has_mapping = any("SignalMapping.kt" in str(record.get("file") or "") for record in matched)
        has_producer = any(
            any(token in str(record.get("text") or "") for token in ("onNextData(", "SignalFormat.", "setFormat("))
            and any(part in str(record.get("file") or "") for part in ("Helper", "DataCenter", "Transport"))
            for record in matched
        )
        has_consumer = any(
            any(part in str(record.get("file") or "") for part in ("TipsBizService", "TipsServiceRepository", "TipsMsgHelper", "Proxy"))
            for record in matched
        )
        format_name = _detect_signal_format(candidate, matched)
        score = (
            (4 if has_mapping else 0)
            + (5 if has_producer else 0)
            + (2 if has_consumer else 0)
            + (1 if signal.startswith("SIGNAL_CTL_") else 0)
        )
        ranked.append(
            {
                **candidate,
                "matched_records": matched,
                "has_mapping": has_mapping,
                "has_producer": has_producer,
                "has_consumer": has_consumer,
                "format_name": format_name,
                "score": score,
            }
        )
    return sorted(ranked, key=lambda item: (-int(item["score"]), item["signal"]))


def _detect_signal_format(candidate: dict[str, Any], matched: list[dict[str, Any]]) -> str:
    for record in matched:
        text = str(record.get("text") or "")
        for name in ("Int32", "Int64", "String", "Boolean", "Float", "Double"):
            if f"SignalFormat.{name}" in text or f"Signal.SignalFormat.{name}" in text:
                return name
    comment = str(candidate.get("comment") or "").casefold()
    if " int" in comment or comment.endswith("int"):
        return "Int32"
    return ""


def _extract_datacenter_action(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    for record in records:
        text = str(record.get("text") or "")
        if "ACTION_MOCK" in text and "mock.datacenter" in text:
            match = re.search(r'"([^"]+mock\.datacenter[^"]*)"', text)
            action = match.group(1) if match else "com.xiaopeng.guide.action.mock.datacenter"
            return {"action": action, "record": record}
    return None


def _local_probe_commands(primary: dict[str, Any], datacenter_action: dict[str, Any]) -> list[str]:
    code = str(primary.get("code") or "").strip()
    format_name = str(primary.get("format_name") or "").strip()
    format_code = _signal_format_code(format_name)
    action = str(datacenter_action.get("action") or "").strip()
    if not code or not format_code or not action:
        return []
    comment = str(primary.get("comment") or "")
    if _looks_like_binary_enable_signal(comment):
        return [
            f"adb shell am broadcast -a {action} --ei code {code} --ei format {format_code} --es value 1",
            f"adb shell am broadcast -a {action} --ei code {code} --ei format {format_code} --es value 0",
        ]
    return [f"adb shell am broadcast -a {action} --ei code {code} --ei format {format_code} --es value <value>"]


def _signal_format_code(format_name: str) -> str:
    mapping = {
        "Int32": "3",
        "Int64": "4",
        "Float": "5",
        "Double": "6",
        "String": "7",
        "Boolean": "8",
    }
    return mapping.get(format_name, "")


def _looks_like_binary_enable_signal(comment: str) -> bool:
    text = (comment or "").casefold()
    return ("0" in text and "1" in text) and any(token in text for token in ("关闭", "开启", "off", "on"))


def _local_probe_answer(
    question: str,
    primary: dict[str, Any],
    downstream: list[dict[str, Any]],
    datacenter_action: dict[str, Any],
    command_lines: list[str],
) -> str:
    lines = []
    if downstream:
        lines.append(
            f"源码里“{question.replace('源码调查', '').strip()}”至少涉及 2 条相关信号。更适合作为模拟入口的是 "
            f"{primary['signal']} ({primary['code']})；"
            + "、".join(f"{item['signal']} ({item['code']})" for item in downstream)
            + " 更像下游消费/展示信号。"
        )
    else:
        lines.append(f"源码里更适合作为模拟入口的信号是 {primary['signal']} ({primary['code']})。")
    lines.append(
        f"推荐用 DataCenter mock 广播注入 {primary['signal']}：action={datacenter_action['action']}，"
        f"format={primary.get('format_name') or '未知'}。"
    )
    if _looks_like_binary_enable_signal(str(primary.get("comment") or "")):
        lines.append("值语义：0 关闭，1 开启。")
    lines.extend(command_lines)
    if downstream:
        lines.append(
            "下游消费侧证据："
            + "；".join(f"{item['signal']} 在直接消费模块出现" for item in downstream[:2])
            + "。"
        )
    return "\n".join(lines)


def _local_probe_evidence(
    primary: dict[str, Any],
    downstream: list[dict[str, Any]],
    datacenter_action: dict[str, Any],
) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    evidence.append(_candidate_definition_evidence(primary))
    evidence.extend(primary.get("matched_records", []))
    if datacenter_action.get("record"):
        evidence.append(datacenter_action["record"])
    for item in downstream[:2]:
        evidence.append(_candidate_definition_evidence(item))
        evidence.extend(item.get("matched_records", [])[:2])
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, int, str]] = set()
    for record in evidence:
        file = str(record.get("file") or "")
        line = int(record.get("line") or 0)
        text = str(record.get("text") or "")
        key = (file, line, text)
        if file and text and key not in seen:
            seen.add(key)
            deduped.append({"file": file, "line": line, "text": text})
    return deduped


def _candidate_definition_evidence(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "file": str(candidate.get("source_ref") or ""),
        "line": int(candidate.get("line") or 0),
        "text": f"{candidate.get('signal')} = {candidate.get('code')}; {candidate.get('comment')}".strip(),
    }


def _local_probe_coverage_boundary(primary: dict[str, Any], downstream: list[dict[str, Any]]) -> str:
    if downstream:
        return (
            f"已证明 {primary['signal']} 的定义、映射/生产链和 DataCenter mock 注入入口；"
            f"也已证明 {downstream[0]['signal']} 存在直接消费侧证据。"
            "当前固定链路未直接证明两者之间全部中间转换节点，因此结论用于指导模拟入口选择，"
            "不等同于完整运行时链路全证。"
        )
    return (
        f"已证明 {primary['signal']} 的定义、映射/生产链和 DataCenter mock 注入入口；"
        "当前未覆盖更下游的完整消费链。"
    )


def _resolve_module_path(module: str, repo_roots: list[Path]) -> Path | None:
    if not module:
        return None
    candidate = Path(module)
    if candidate.is_absolute():
        return candidate
    for root in repo_roots:
        full = root / module
        if full.exists():
            return full
    return None


def _collect_file_excerpt_lines(full_path: Path, module: str, question: str, hits: list[SearchHit]) -> list[str]:
    try:
        lines = full_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    tokens = _excerpt_tokens_for_module(module, question, hits)
    excerpts: list[str] = []
    for lineno, line in enumerate(lines, start=1):
        if any(token and token in line for token in tokens):
            excerpts.append(f"- {module}:{lineno} {line.strip()}")
        if len(excerpts) >= 2:
            break
    return excerpts


def _excerpt_tokens_for_module(module: str, question: str, hits: list[SearchHit]) -> list[str]:
    tokens: list[str] = []
    for hit in hits[:5]:
        signal = str(hit.metadata.get("signal") or "").strip()
        code = str(hit.metadata.get("code") or "").strip()
        if signal:
            tokens.append(signal)
        if code:
            tokens.append(code)
    module_lower = module.casefold()
    if "signalmapping" in module_lower:
        tokens.extend(["put(", "SignalCode."])
    if "datacenterbroadcastreceiver" in module_lower:
        tokens.extend(["ACTION_MOCK", "mockSignal"])
    if module_lower.endswith("/datacenter.kt"):
        tokens.extend(["mockSignal(", "notifyObservers", "signalFlow"])
    if "carctlxpilothelper" in module_lower:
        tokens.extend(["onNextData(", "前车起步", "StartRemind", "SignalFormat."])
    if "xdatatransport" in module_lower:
        tokens.extend(["Signal.SignalCode.", "START_REMIND"])
    if "tipsbizservice" in module_lower or "androidunityproxy" in module_lower:
        tokens.extend(["START_REMIND", "SignalCode."])
    if "tipsservicerepository" in module_lower:
        tokens.extend(["handleStartStateTips", "matchStartTipsTxt", "startSignal"])
    if "tipsmsghelper" in module_lower:
        tokens.extend(["matchStartTipsTxt", "Key_Tips_HU_START_REMIND"])
    if module_lower.endswith("signal.proto"):
        tokens.extend(["前车起步", "SIGNAL_"])
    if "前车起步" in question:
        tokens.append("前车起步")
    seen: set[str] = set()
    deduped: list[str] = []
    for token in tokens:
        cleaned = token.strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            deduped.append(cleaned)
    return deduped


def _display_repo_relative(path_text: str, repo_roots: list[Path]) -> str:
    path = (path_text or "").strip()
    if not path:
        return ""
    for root in repo_roots:
        root_text = str(root).rstrip("/") + "/"
        if path.startswith(root_text):
            return path[len(root_text) :]
    return path


def _looks_like_native_question(question: str) -> bool:
    lowered = (question or "").casefold()
    return any(term in lowered for term in _NATIVE_HINT_TERMS)


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


# ---------------------------------------------------------------------------
# Code index helpers
# ---------------------------------------------------------------------------

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


def _code_index_prompt_lines(contexts: list[tuple[Path, Any]]) -> list[str]:
    """Format code index context as prompt enrichment lines."""
    lines: list[str] = ["预索引符号信息（优先使用，减少搜索）："]
    for _, ctx in contexts:
        for defn in ctx.definitions[:5]:
            scope_prefix = f"{defn.scope}." if defn.scope else ""
            lines.append(f"- {defn.kind} {scope_prefix}{defn.name} @ {defn.path}:{defn.line}")
        for ref in ctx.references[:5]:
            lines.append(f"  引用: {ref.path}:{ref.line}: {ref.text}")
    return lines if len(lines) > 1 else []


# ---------------------------------------------------------------------------
# CodeGraph helpers
# ---------------------------------------------------------------------------

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
        from .code_index import CodeIndexContext, SymbolHit, ReferenceHit
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
