"""Bug analysis runner and related models."""

from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import html as html_lib
import importlib
import json
import math
import os
import re
import select
import shlex
import signal
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from typing import Callable
import urllib.error
import urllib.request

from ..downloader import DownloadError, LogDownloader
from ..evidence_logs import preserve_evidence_log_bundle
from ..health import ProcessWatchdog, _safe_terminate, _write_subprocess_debug_log
from ..log import get_logger
from ..log_analyzer import SmartLogAnalyzer
from ..models import (
    BridgeConfig,
    BugRequest,
    DownloadResource,
    LarkEvent,
    TaskResult,
    create_job_context,
)
from ..network_env import build_internal_network_env
from ..parser import parse_followup_action, parse_signal_request
from ..reporting import (
    ReportComposition,
    ReportSection,
    ReportVerdict,
    build_structured_summary_sections,
    combined_bug_html,
    composition_to_renderer_payload,
    plan_signal_report,
    plan_startup_stuck_report,
)
from ..signal_resolver import SignalResolver
from ..skill_registry import AUX_BUG_SKILLS, PRIMARY_BUG_SKILL_MAP, extract_skill_frontmatter
from ..skill_manager import SkillManager
from ..token_usage import TOKEN_USAGE_ALIASES, normalize_token_usage
from .. import prompt_snapshots
from ._helpers import (
    _default_command_for_provider,
    _detect_available_provider,
    _normalize_provider_name,
    _provider_candidates,
)
from .bug_summary_policy import SummaryBackendInput, choose_summary_backend
from .codex_app_server_runtime import (
    CodexAppServerRuntime,
    app_server_event_preview,
    check_codex_app_server_available,
)
from .omlx_client import OmlxChatClient
from .routing_terms import (
    CRASH_ROUTE_TERMS,
    LD_LANE_LEVEL_ROUTE_TERMS,
    PERCEPTION_ROUTE_TERMS,
    PULLOVER_CHAIN_ROUTE_TERMS,
    SCENE_SIGNAL_ROUTE_TERMS,
    SIGNAL_ROUTE_TERMS,
    STARTUP_BLOCK_ROUTE_TERMS,
    STARTUP_ROUTE_TERMS,
    STUCK_ROUTE_TERMS,
    XTHEME_ROUTE_TERMS,
    _has_strong_scene_signal_intent,
    _is_core_scene_signal,
    looks_like_scene_signal_request,
)

logger = get_logger("agents")

SOURCE_STAGE_KIND = "source_stage"
# Canonical name for the source-code analysis capability.
# source_stage is kept as an alias for backward compatibility.
SOURCE_CODE_SKILL_KIND = "source_code_skill"
SOURCE_STAGE_KINDS = {SOURCE_STAGE_KIND, SOURCE_CODE_SKILL_KIND}


@dataclass(frozen=True)
class PlanKindSpec:
    """Irreducible business-policy attributes of a plan kind.

    Excluded by design (handled elsewhere):
      - decoding: now in _ensure_decoded_in_place (runs for all kinds with raw logs)
      - target-time: in _SCRIPT_ACCEPTS_TARGET_TIME (script-side capability)
      - report path discovery & verdict normalization: in BugReportAdapter
    """
    is_agent_handled: bool = False        # 走 AI agent 路径而非确定性脚本
    is_custom_agent: bool = False         # custom/source-stage 分发器分支
    is_source_stage: bool = False         # source_stage 家族成员
    needs_source_evidence: bool = False   # 触发源码证据收集（非执行路径属性）

    @property
    def log_dependent(self) -> bool:
        # 脚本类 kind（非 agent）才需要智能日志分析
        return not self.is_agent_handled

    @property
    def needs_custom_executor_check(self) -> bool:
        # 既走 custom agent 分发、又需要源码证据的 kind 才检查 executor
        return self.is_custom_agent and self.needs_source_evidence


@dataclass(frozen=True)
class DirectApiCompactionProfileSpec:
    name: str
    analysis_kind: str
    skill_name: str
    handler: str
    metadata_markers: tuple[str, ...] = ()


# Single source of truth for all plan-kind policy decisions.
# Adding a new kind: insert one entry here — all call sites pick it up automatically.
PLAN_KIND_REGISTRY: dict[str, PlanKindSpec] = {
    "startup":       PlanKindSpec(),
    "stuck":         PlanKindSpec(),
    "crash":         PlanKindSpec(),
    "scene_signal":  PlanKindSpec(),
    "perception":    PlanKindSpec(),
    "xtheme":        PlanKindSpec(),
    "signal":        PlanKindSpec(),
    "ld_lane_level": PlanKindSpec(is_agent_handled=True, is_custom_agent=True),
    "pullover_chain": PlanKindSpec(is_agent_handled=True, is_custom_agent=True),
    "general":       PlanKindSpec(is_agent_handled=True, needs_source_evidence=True),
    "custom_skill":  PlanKindSpec(is_agent_handled=True, is_custom_agent=True, needs_source_evidence=True),  # legacy alias
    SOURCE_STAGE_KIND: PlanKindSpec(
        is_agent_handled=True, is_custom_agent=True,
        needs_source_evidence=True, is_source_stage=True,
    ),
    SOURCE_CODE_SKILL_KIND: PlanKindSpec(
        is_agent_handled=True, is_custom_agent=True,
        needs_source_evidence=True, is_source_stage=True,
    ),
}

# Canonical set for "custom_skill or source_code_skill" checks.
_SOURCE_SKILL_KINDS = {"custom_skill", SOURCE_CODE_SKILL_KIND}

_DIRECT_API_COMPACTION_PROFILES: tuple[DirectApiCompactionProfileSpec, ...] = (
    DirectApiCompactionProfileSpec(
        name="startup_unity_lifecycle",
        analysis_kind="startup",
        skill_name="unity-startup-lifecycle-check",
        handler="_direct_api_context_excerpt_for_startup_unity_lifecycle",
        metadata_markers=("bug_3d_startup_report",),
    ),
)

# Derived sets — kept for call sites that need a set (e.g. set membership tests
# on lists of plans, or unpacking into other sets).
ALL_PLAN_KINDS = frozenset(PLAN_KIND_REGISTRY)


def _kind_spec(kind: str) -> PlanKindSpec:
    return PLAN_KIND_REGISTRY.get(kind, PlanKindSpec())


# Script capability table — which analysis scripts accept the --target-time CLI arg.
# Decoupled from PlanKindSpec because it's a property of the underlying script,
# not of the kind's policy.
_SCRIPT_ACCEPTS_TARGET_TIME = frozenset({
    "startup", "stuck", "crash", "perception", "xtheme", "scene_signal",
})


# Kinds whose analysis script gets one automatic retry on non-zero returncode.
# Historically only startup, due to higher transient-failure rate in tracetag/PID
# correlation. Add more kinds here only after confirming retry is safe and useful.
_RETRY_KINDS = frozenset({"startup"})


def _kind_accepts_target_time(kind: str) -> bool:
    return kind in _SCRIPT_ACCEPTS_TARGET_TIME


@dataclass(frozen=True)
class NormalizedVerdict:
    """Unified verdict shape across all analysis-script outputs.

    sev: "red" | "yellow" | "green" | "info" | "unknown"
    msg: one-line conclusion
    issues: optional list of {sev, title, detail}
    """
    sev: str = "unknown"
    msg: str = ""
    issues: list[dict[str, str]] = field(default_factory=list)


# Tech debt — once we migrate the following skill scripts to a v2 schema
# (verdict.{sev,msg,issues} + stdout `[OK] HTML:`/`[OK] JSON:`/`[OK] 结论:`),
# the adapter branches below can collapse to a single direct read:
#   - analyze_unity_startup.py: verdict={severity,message,issues}, stdout `[OK] HTML report:`/`[OK] JSON report:`
#   - analyze_3d_stuck.py: verdict={verdict_sev,verdict_msg,issues,chain}, stdout `[OK] 报告:`/`[OK] JSON:`
#   - analyze_perception_data_summary.py: verdict at summary.verdict={sev,msg}, stdout `[OK] HTML:`/`[OK] JSON:`
#   - analyze_xtheme.py: verdict={sev,msg}, stdout `[OK] HTML:`/`[OK] JSON:`
#   - extract_scene_signal_events.py: severity flat string + verdict flat string, fixed-path output
#   - analyze_signal_chain.py: no verdict (summary string + warnings list), CLI direct-write
class BugReportAdapter:
    """Translate per-script output conventions into uniform structures.

    All methods are static; the adapter holds no state. It centralizes
    the per-kind branching that would otherwise spread across _run_analysis
    and _build_summary_from_report.
    """

    @staticmethod
    def locate_report(
        *,
        kind: str,
        completed_stdout: str,
        analysis_dir: Path,
        html_path: Path,
        json_path: Path,
    ) -> tuple[Path | None, Path | None]:
        """Return (generated_html, generated_json) paths produced by the script.

        Caller is responsible for copy/move to the canonical html_path/json_path.
        For agent-handled kinds and signal kind, the script writes directly to
        html_path/json_path (no copy needed) — adapter returns those paths.
        """
        if kind == "startup":
            return (
                analysis_dir / "unity_startup_lifecycle_report.html",
                analysis_dir / "unity_startup_lifecycle_report.json",
            )
        if kind == "scene_signal":
            return (
                analysis_dir / "scene_signal_events.html",
                analysis_dir / "scene_signal_events.json",
            )
        if kind in {"stuck", "crash"}:
            return (
                BugReportAdapter._extract_stdout_path(completed_stdout, r"^\[OK\] 报告:\s*(.+)$"),
                BugReportAdapter._extract_stdout_path(completed_stdout, r"^\[OK\] JSON:\s*(.+)$"),
            )
        if kind in {"perception", "xtheme"}:
            return (
                BugReportAdapter._extract_stdout_path(completed_stdout, r"^\[OK\] HTML:\s*(.+)$"),
                BugReportAdapter._extract_stdout_path(completed_stdout, r"^\[OK\] JSON:\s*(.+)$"),
            )
        # signal & agent-handled kinds write directly to the requested paths.
        return html_path if html_path.exists() else None, json_path if json_path.exists() else None

    @staticmethod
    def normalize_verdict(*, kind: str, payload: object) -> NormalizedVerdict:
        """Map per-kind verdict shapes to NormalizedVerdict."""
        if not isinstance(payload, dict):
            return NormalizedVerdict()
        if kind == "startup":
            v = payload.get("verdict") or {}
            if isinstance(v, dict):
                return NormalizedVerdict(
                    sev=str(v.get("severity") or "unknown"),
                    msg=str(v.get("message") or ""),
                    issues=BugReportAdapter._coerce_issues(v.get("issues")),
                )
        elif kind in {"stuck", "crash"}:
            v = payload.get("verdict") or {}
            if isinstance(v, dict):
                return NormalizedVerdict(
                    sev=str(v.get("verdict_sev") or "unknown"),
                    msg=str(v.get("verdict_msg") or ""),
                    issues=BugReportAdapter._coerce_issues(v.get("issues")),
                )
        elif kind == "perception":
            summary = payload.get("summary") or {}
            v = summary.get("verdict") if isinstance(summary, dict) else None
            if isinstance(v, dict):
                return NormalizedVerdict(
                    sev=str(v.get("sev") or "unknown"),
                    msg=str(v.get("msg") or ""),
                    issues=BugReportAdapter._coerce_issues(
                        summary.get("issues") if isinstance(summary, dict) else None
                    ),
                )
        elif kind == "xtheme":
            v = payload.get("verdict") or {}
            if isinstance(v, dict):
                return NormalizedVerdict(
                    sev=str(v.get("sev") or "unknown"),
                    msg=str(v.get("msg") or ""),
                    issues=BugReportAdapter._coerce_issues(payload.get("issues")),
                )
        elif kind == "scene_signal":
            sev_raw = str(payload.get("severity") or "").lower()
            sev = {"success": "green", "warning": "yellow"}.get(sev_raw, "info")
            return NormalizedVerdict(
                sev=sev,
                msg=str(payload.get("verdict") or ""),
                issues=[],
            )
        elif kind == "signal":
            warnings = payload.get("warnings") or []
            issues = [
                {"sev": "yellow", "title": "warning", "detail": str(w)}
                for w in warnings
                if isinstance(warnings, list)
            ]
            return NormalizedVerdict(
                sev="info",
                msg=str(payload.get("summary") or ""),
                issues=issues,
            )
        else:
            # general / custom_skill / source_stage / ld_lane_level: bug_runner constructs
            # `{"verdict": {"sev": ..., "text": ...}}` itself.
            v = payload.get("verdict") or {}
            if isinstance(v, dict):
                return NormalizedVerdict(
                    sev=str(v.get("sev") or "unknown"),
                    msg=str(v.get("text") or v.get("msg") or ""),
                    issues=BugReportAdapter._coerce_issues(v.get("issues")),
                )
        return NormalizedVerdict()

    @staticmethod
    def _extract_stdout_path(output: str, pattern: str) -> Path | None:
        match = re.search(pattern, output, re.MULTILINE)
        if not match:
            return None
        return Path(match.group(1).strip())

    @staticmethod
    def _coerce_issues(raw: object) -> list[dict[str, str]]:
        if not isinstance(raw, list):
            return []
        out: list[dict[str, str]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            out.append({
                "sev": str(item.get("sev") or item.get("severity") or "info"),
                "title": str(item.get("title") or ""),
                "detail": str(item.get("detail") or item.get("message") or ""),
            })
        return out


def _run_tracked_process(*args, **kwargs):
    return importlib.import_module("lark_agent_bridge.agents").run_tracked_process(*args, **kwargs)


def _coerce_process_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


_NON_LOG_XP_MAGIC_HEADERS = (
    b"\x89PNG\r\n\x1a\n",
    b"\xff\xd8\xff",
    b"GIF87a",
    b"GIF89a",
    b"%PDF-",
)
_BUG_ATTACHMENT_DOWNLOAD_SUFFIXES = (
    ".xp.zip.001",
    ".tar.gz",
    ".tar.bz2",
    ".tar.xz",
    ".zip",
    ".7z",
    ".rar",
    ".tgz",
    ".tar",
    ".gz",
    ".bz2",
    ".xz",
    ".xp",
    ".alog",
    ".xlog",
    ".log",
    ".txt",
)
_BUG_ARCHIVE_SUFFIXES = (
    ".zip",
    ".7z",
    ".rar",
    ".tar.gz",
    ".tar.bz2",
    ".tar.xz",
    ".tgz",
    ".tar",
    ".gz",
    ".bz2",
    ".xz",
)
_BUG_LOG_INPUT_PRIORITY_SUFFIXES = (
    ".xp.zip.001",
    ".xp",
    ".zip",
    ".7z",
    ".rar",
    ".tar.gz",
    ".tar.bz2",
    ".tar.xz",
    ".tgz",
    ".tar",
    ".gz",
    ".bz2",
    ".xz",
    ".alog",
    ".xlog",
    ".log",
    ".txt",
)
_TOKEN_USAGE_KEYS = TOKEN_USAGE_ALIASES
_RUNTIME_HTML_MARKER_START = "<!-- LARK_AGENT_RUNTIME_START -->"
_RUNTIME_HTML_MARKER_END = "<!-- LARK_AGENT_RUNTIME_END -->"
_BUG_LOG_COVERAGE_WINDOW_MINUTES = 10
_BUG_LOG_COVERAGE_MAX_FILES = 400
_BUG_LOG_COVERAGE_MAX_LINES_PER_FILE = 20000
_BUG_LOG_COVERAGE_SUFFIXES = (
    ".alog.log",
    ".xlog.log",
    ".alog",
    ".xlog",
    ".log",
    ".txt",
)
_GENERIC_BUG_PROMPT_TERMS = (
    "分析",
    "分析下",
    "分析一下",
    "看下",
    "看一下",
    "查下",
    "查一下",
    "帮我看下",
    "帮我看一下",
    "调查",
    "排查",
    "日志分析",
    "重新分析",
    "再分析",
)
_GENERAL_SCOPE_PATTERNS = (
    re.compile(r"[/\\][^\s，。；；、]+"),
    re.compile(r"\b[\w.-]+\.(?:kt|java|cpp|cc|c|h|hpp|py|md|log|xlog|alog|zip|7z|rar|tar|gz|json|xml)\b", re.I),
    re.compile(r"\b(?:[a-z_][a-z0-9_]*\.){2,}[a-z_][a-z0-9_]*\b", re.I),
    re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)+\b"),
    re.compile(r"\b(?=[A-Za-z0-9_]*[a-z])(?=[A-Za-z0-9_]*[A-Z])[A-Za-z_][A-Za-z0-9_]{5,}\b"),
    re.compile(r"\b[A-Z][A-Z0-9]+(?:_[A-Z0-9]+){2,}\b"),
    re.compile(r"\bpid\s*[:=]?\s*\d+\b", re.I),
    re.compile(r"进程(?:号)?\s*[:=]?\s*\d+"),
)

_PRIMARY_BUG_SKILL_MAP = PRIMARY_BUG_SKILL_MAP
_AUX_BUG_SKILLS = AUX_BUG_SKILLS
_extract_skill_frontmatter = extract_skill_frontmatter


@dataclass(slots=True)
class CodexAppServerExecutionPolicy:
    """Single source of truth for codex app-server process inputs.

    User config is authoritative: ``disable_node_repl`` is never flipped behind
    the user's back. Minimal-home mode only decides whether the disable FLAG is
    emitted, because the minimal home config has no node_repl section.
    """

    env: dict[str, str]
    codex_home: Path | None
    cwd: Path
    disable_node_repl: bool
    emit_node_repl_flag: bool


class BugAnalysisRunner:
    _SOURCE_EVIDENCE_WAIT_SECONDS = 30
    _SOURCE_EVIDENCE_TOTAL_BUDGET_SECONDS = 30.0
    _SOURCE_EVIDENCE_CODEGRAPH_CALL_SECONDS = 5.0

    def __init__(
        self,
        config: BridgeConfig,
        process_watchdog: ProcessWatchdog | None = None,
        lark_client: object | None = None,
        skill_manager: SkillManager | None = None,
    ) -> None:
        self.config = config
        self.process_watchdog = process_watchdog
        self.signal_resolver = SignalResolver(
            config.guideengine_repo,
            cache_dir=config.data_dir / "cache",
            cache_ttl_seconds=config.signal_resolver.cache_ttl_seconds,
            preferred_paths=config.signal_resolver.preferred_paths or None,
            source_suffixes=config.signal_resolver.source_suffixes or None,
        )
        self._lark_client = lark_client
        self.skill_manager = skill_manager or SkillManager(config)

        # 智能日志分析器（内部工具）
        smart_config = getattr(config, 'smart_log_analysis', None)
        if smart_config and getattr(smart_config, 'enabled', False):
            self.log_analyzer = SmartLogAnalyzer(
                process_name=getattr(smart_config, 'process_name', 'com.xiaopeng.montecarlo'),
                time_window_minutes=getattr(smart_config, 'time_window_minutes', 1),
                max_reverse_lines=getattr(smart_config, 'max_reverse_lines', 1000),
                workspace_root=config.workspace_root,
            )
        else:
            # 默认配置
            self.log_analyzer = SmartLogAnalyzer(
                process_name='com.xiaopeng.montecarlo',
                time_window_minutes=1,
                max_reverse_lines=1000,
                workspace_root=config.workspace_root,
            )

    def _resolve_source_evidence_future(self, future: object) -> Path | None:
        try:
            return future.result(timeout=self._SOURCE_EVIDENCE_WAIT_SECONDS)  # type: ignore[attr-defined]
        except Exception:
            return None

    def _source_evidence_call_timeout(self, deadline: float | None, configured_timeout: float) -> float:
        timeout = min(configured_timeout, self._SOURCE_EVIDENCE_CODEGRAPH_CALL_SECONDS)
        if deadline is None:
            return timeout
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return 0.0
        return min(timeout, remaining)

    def _available_bug_skills(self) -> list[dict[str, object]]:
        skills_dir = self.config.workspace_root / ".ai/skills"
        entries: list[dict[str, object]] = []
        primary_skill_map = self.skill_manager.primary_skill_map()
        auxiliary_skill_names = self.skill_manager.auxiliary_skill_names()
        if not skills_dir.exists():
            for skill_name, (kind, label, requires_logs) in primary_skill_map.items():
                if skill_name == "general":
                    continue
                entries.append(
                    {
                        "name": skill_name,
                        "kind": kind,
                        "label": label,
                        "requires_logs": requires_logs,
                        "role": "primary",
                        "description": "",
                    }
                )
            entries.append(
                {
                    "name": "general",
                    "kind": "general",
                    "label": "通用问题分析",
                    "requires_logs": False,
                    "role": "primary",
                    "description": "没有合适专用 skill 时只做分诊和材料检查；缺少明确方向时要求用户补充，不盲扫源码给根因。",
                }
            )
            return entries

        for path in sorted(skills_dir.glob("*/SKILL.md")):
            skill_name = path.parent.name
            try:
                body = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            frontmatter_name, description = _extract_skill_frontmatter(body)
            if skill_name in primary_skill_map:
                kind, label, requires_logs = primary_skill_map[skill_name]
                entries.append(
                    {
                        "name": skill_name,
                        "kind": kind,
                        "label": label,
                        "requires_logs": requires_logs,
                        "role": "primary",
                        "description": description or frontmatter_name or "",
                    }
                )
            elif skill_name in auxiliary_skill_names:
                entries.append(
                    {
                        "name": skill_name,
                        "kind": "",
                        "label": frontmatter_name or skill_name,
                        "requires_logs": False,
                        "role": "auxiliary",
                        "description": description or "",
                    }
                )
        entries.append(
            {
                "name": "general",
                "kind": "general",
                "label": "通用问题分析",
                "requires_logs": False,
                "role": "primary",
                "description": "没有合适专用 skill 时只做分诊和材料检查；缺少明确方向时要求用户补充，不盲扫源码给根因。",
            }
        )
        return entries

    def supported_primary_bug_skills(self) -> list[dict[str, object]]:
        return [
            record.to_dict(include_content=False)
            for record in self.skill_manager.list_skills()
            if record.role == "primary" and record.name != "general" and record.selectable_in_report_card
        ]

    def selection_for_skill_name(
        self,
        skill_name: str,
        *,
        source: str,
        reason: str = "",
        provider: str = "",
    ) -> "BugAnalysisSelection | None":
        normalized = skill_name.strip()
        primary_skill_map = self.skill_manager.primary_skill_map()
        if normalized == "general" or normalized not in primary_skill_map:
            return None
        kind, label, _requires_logs = primary_skill_map[normalized]
        return BugAnalysisSelection(
            plans=[BugAnalysisPlan(kind=kind)],
            skill_name=normalized,
            skill_label=label,
            source=source,
            reason=reason or f"用户选择专用 skill：{label}",
            provider=provider,
        )

    def _manual_bug_selection(self, *, prompt_text: str, title: str, description: str) -> BugAnalysisSelection:
        plans = self.classify_requests(prompt_text=prompt_text, title=title, description=description)
        return self._selection_from_plans(plans, source="manual_fallback", reason="Agent 分类不可用，退回本地规则分类。")

    def _selection_from_plans(
        self,
        plans: list["BugAnalysisPlan"],
        *,
        source: str,
        reason: str,
        provider: str = "",
    ) -> BugAnalysisSelection:
        plan = plans[0] if plans else BugAnalysisPlan(kind="general")
        skill_name = self._skill_name_for_kind(plan.kind)
        return BugAnalysisSelection(
            plans=plans or [BugAnalysisPlan(kind="general")],
            skill_name=skill_name,
            skill_label=self._analysis_label(plan.kind),
            source=source,
            reason=reason,
            provider=provider,
        )

    def _source_analysis_shortcut(self, *texts: str) -> bool:
        merged = "\n".join(str(text or "") for text in texts).strip()
        if not merged:
            return False
        return re.search(r"(^|[\s@])debug(\b|[\s:：_-])", merged, re.I) is not None

    def _source_analysis_targets_from_texts(self, *texts: str) -> list[str]:
        terms: list[str] = []
        for text in texts:
            for term in self._explicit_source_terms_from_text(str(text or "")):
                self._append_unique(terms, term)
        return terms[:12]

    def _domain_kind_from_plans(self, plans: list["BugAnalysisPlan"]) -> str:
        for plan in plans:
            if plan.kind != "general" and not _kind_spec(plan.kind).is_source_stage:
                return plan.kind
        return "general"

    def _context_profile_for_domain(self, domain_kind: str, skill_name: str) -> str:
        normalized = skill_name.strip()
        if normalized and normalized not in {"general", "source_analysis"}:
            return normalized
        if domain_kind and domain_kind != "general":
            candidate = self._skill_name_for_kind(domain_kind)
            if candidate != "general":
                return candidate
        return ""

    def _has_explicit_source_request(self, *, request_text: str, prompt_text: str, skill_name: str) -> bool:
        if skill_name.strip() == "source_analysis":
            return True
        if self._source_analysis_shortcut(prompt_text, request_text):
            return True
        if self._should_collect_source_evidence(prompt_text, request_text):
            return True
        return False

    def _build_analysis_decision(
        self,
        *,
        request_text: str,
        prompt_text: str,
        title: str,
        description: str,
        plans: list["BugAnalysisPlan"],
        skill_name: str,
    ) -> "AnalysisDecision":
        domain_kind = self._domain_kind_from_plans(plans)
        context_profile = self._context_profile_for_domain(domain_kind, skill_name)
        source_targets = self._source_analysis_targets_from_texts(prompt_text, request_text)
        explicit_source = self._has_explicit_source_request(
            request_text=request_text,
            prompt_text=prompt_text,
            skill_name=skill_name,
        )
        source_mode = "off"
        reason = "未检测到明确源码诉求，默认只执行领域分析。"
        if explicit_source:
            if domain_kind == "general":
                source_mode = "standalone"
                reason = "用户明确要求源码分析，且未命中稳定领域 skill，执行独立源码阶段。"
            else:
                source_mode = "append"
                reason = "用户明确要求源码分析，在领域分析后追加源码阶段。"
        return AnalysisDecision(
            domain_kind=domain_kind,
            source_mode=source_mode,
            context_profile=context_profile,
            source_targets=source_targets,
            reason=reason,
        )

    def _build_analysis_plan(self, decision: "AnalysisDecision") -> "AnalysisPlan":
        stages: list[AnalysisStage] = []
        if decision.source_mode in {"off", "append"}:
            stages.append(
                AnalysisStage(
                    kind="domain",
                    domain_kind=decision.domain_kind,
                    context_profile=decision.context_profile,
                    source_targets=list(decision.source_targets),
                )
            )
        if decision.source_mode in {"append", "standalone"}:
            stages.append(
                AnalysisStage(
                    kind="source",
                    domain_kind=decision.domain_kind,
                    context_profile=decision.context_profile,
                    source_targets=list(decision.source_targets),
                )
            )
        stages.append(
            AnalysisStage(
                kind="summary",
                domain_kind=decision.domain_kind,
                context_profile=decision.context_profile,
                source_targets=list(decision.source_targets),
            )
        )
        return AnalysisPlan(
            domain_kind=decision.domain_kind,
            context_profile=decision.context_profile,
            stages=stages,
        )

    def _decide_source_analysis_request(
        self,
        *,
        request_text: str,
        prompt_text: str,
        title: str,
        description: str,
        plans: list["BugAnalysisPlan"],
        skill_name: str,
    ) -> SourceAnalysisDecision:
        analysis_decision = self._build_analysis_decision(
            request_text=request_text,
            prompt_text=prompt_text,
            title=title,
            description=description,
            plans=plans,
            skill_name=skill_name,
        )
        analysis_plan = self._build_analysis_plan(analysis_decision)
        requested = analysis_decision.source_mode in {"append", "standalone"}
        return SourceAnalysisDecision(
            requested=requested,
            reason=analysis_decision.reason,
            targets=list(analysis_decision.source_targets),
            source="stage_rules",
            debug_shortcut=self._source_analysis_shortcut(prompt_text, request_text),
            domain_kind=analysis_decision.domain_kind,
            source_mode=analysis_decision.source_mode,
            context_profile=analysis_decision.context_profile,
            stage_kinds=[stage.kind for stage in analysis_plan.stages],
        )

    def _augment_plans_for_source_analysis(
        self,
        plans: list["BugAnalysisPlan"],
        *,
        source_decision: SourceAnalysisDecision,
    ) -> list["BugAnalysisPlan"]:
        normalized = [BugAnalysisPlan(kind=plan.kind, signal_code=plan.signal_code) for plan in plans]
        if not source_decision.requested:
            return normalized
        if all(plan.kind == "general" for plan in normalized):
            return [BugAnalysisPlan(kind=SOURCE_STAGE_KIND)]
        if any(_kind_spec(plan.kind).is_source_stage for plan in normalized):
            return normalized
        return [*normalized, BugAnalysisPlan(kind=SOURCE_STAGE_KIND)]

    # ------------------------------------------------------------------
    # Unified classification + source-decision (Phase 2)
    # ------------------------------------------------------------------

    def _unified_classify_and_decide(
        self,
        *,
        request_text: str,
        prompt_text: str,
        title: str,
        description: str,
        attachments: object = (),
        time_context: "BugTimeContext | None" = None,
    ) -> "UnifiedBugDecision":
        """Single entry-point that replaces the old two-step flow.

        1. Agent classification → ``BugAnalysisSelection``
        2. Source analysis decision → ``SourceAnalysisDecision``
        3. Plan augmentation (append ``source_stage`` if needed)
        4. Skill-name normalization (rename to ``source_analysis`` when applicable)

        The result is a ``UnifiedBugDecision`` with both sub-decisions.
        """
        # --- step 1: domain classification ---
        selection = self._classify_bug_request_with_agent(
            prompt_text=prompt_text,
            title=title,
            description=description,
            attachments=attachments,
            time_context=time_context,
        )
        if selection is not None and any(
            plan.kind == "signal" and not plan.signal_code for plan in selection.plans
        ):
            selection = None
        if selection is None:
            selection = self._manual_bug_selection(
                prompt_text=prompt_text, title=title, description=description,
            )

        # --- step 2: source analysis decision ---
        source_decision = self._decide_source_analysis_request(
            request_text=request_text,
            prompt_text=prompt_text,
            title=title,
            description=description,
            plans=selection.plans,
            skill_name=selection.skill_name,
        )

        # --- step 3: augment plans ---
        selection.plans = self._augment_plans_for_source_analysis(
            selection.plans, source_decision=source_decision,
        )

        # --- step 4: normalize skill name ---
        if (
            source_decision.requested
            and all(_kind_spec(plan.kind).is_source_stage for plan in selection.plans)
            and selection.skill_name.strip() in {"", "general"}
        ):
            selection.skill_name = "source_analysis"
            selection.skill_label = self._analysis_label(SOURCE_STAGE_KIND)
            if source_decision.reason:
                selection.reason = source_decision.reason

        return UnifiedBugDecision(selection=selection, source_decision=source_decision)

    def classify_and_decide(
        self,
        *,
        request_text: str,
        prompt_text: str,
        title: str = "",
        description: str = "",
        plans: "list[BugAnalysisPlan] | None" = None,
    ) -> "UnifiedBugDecision":
        """Public API for external callers (e.g. ``app.py`` preflight).

        When *plans* is provided the classification step is skipped
        and only the source-analysis decision is performed.
        """
        if plans is not None:
            first_plan = plans[0] if plans else BugAnalysisPlan(kind="general")
            skill_name = (
                self._skill_name_for_kind(first_plan.kind)
                if first_plan.kind != "general"
                else ""
            )
            selection = self._selection_from_plans(
                plans,
                source="preflight_rules",
                reason="",
            )
            if skill_name:
                selection.skill_name = skill_name
                selection.skill_label = self._skill_label_for_name(
                    skill_name, first_plan.kind,
                )
            source_decision = self._decide_source_analysis_request(
                request_text=request_text,
                prompt_text=prompt_text,
                title=title,
                description=description,
                plans=plans,
                skill_name=skill_name,
            )
            selection.plans = self._augment_plans_for_source_analysis(
                selection.plans, source_decision=source_decision,
            )
            if (
                source_decision.requested
                and all(_kind_spec(plan.kind).is_source_stage for plan in selection.plans)
                and selection.skill_name.strip() in {"", "general"}
            ):
                selection.skill_name = "source_analysis"
                selection.skill_label = self._analysis_label(SOURCE_STAGE_KIND)
                if source_decision.reason:
                    selection.reason = source_decision.reason
            return UnifiedBugDecision(selection=selection, source_decision=source_decision)

        return self._unified_classify_and_decide(
            request_text=request_text,
            prompt_text=prompt_text,
            title=title,
            description=description,
        )

    def _needs_general_direction(
        self,
        selection: "BugAnalysisSelection",
        *,
        prompt_text: str,
    ) -> bool:
        if selection.skill_name != "general":
            return False
        if any(plan.kind != "general" for plan in selection.plans):
            return False
        return not self._has_explicit_general_scope(prompt_text)

    def _has_explicit_general_scope(self, prompt_text: str) -> bool:
        prompt = self._normalize_general_prompt(prompt_text)
        if not prompt:
            return False
        if prompt.casefold() in _GENERIC_BUG_PROMPT_TERMS:
            return False
        return any(pattern.search(prompt) for pattern in _GENERAL_SCOPE_PATTERNS)

    def _normalize_general_prompt(self, prompt_text: str) -> str:
        prompt = re.sub(r"https?://\S+", " ", prompt_text or "")
        prompt = re.sub(r"\s+", " ", prompt).strip(" \t\r\n，。；;、:：")
        return prompt

    def _general_direction_needed_result(
        self,
        *,
        context,
        selection: "BugAnalysisSelection",
        started: float,
        request_text: str,
        bug_url: str,
        title: str,
        progress_callback: Callable[[dict[str, object]], None] | None,
    ) -> TaskResult:
        self._emit_progress(
            progress_callback,
            stage="bug_need_analysis_direction",
            message="未命中专用预设，等待用户补充明确分析方向",
            job_id=context.job_id,
            bug_url=bug_url,
            title=title,
            classification_skill=selection.skill_name,
            classification_source=selection.source,
            classification_reason=selection.reason,
        )
        message = (
            "当前没有命中专用分析预设，且请求中缺少明确分析方向。\n"
            "为了避免盲扫源码/日志后给出不可靠结论，我没有继续自动分析。\n\n"
            "请补充一个可约束的方向，例如：\n"
            "- 启动 / 卡顿 / Crash / 感知数据 / 主题切换 / 场景信号\n"
            "- 具体 SignalCode、枚举名、类名、函数名、进程号、包名或日志关键词\n"
            "- 只检查日志材料是否完整，或指定要看的时间窗口\n\n"
            "如果仍没有明确方向，目前能力不足以给出可靠根因。"
        )
        return TaskResult(
            success=True,
            message=message,
            skipped=True,
            job_id=context.job_id,
            job_dir=context.job_dir,
            duration_seconds=time.monotonic() - started,
            details={
                "mode": "bug_clarification",
                "analysis_kind": "general",
                "analysis_kinds": ["general"],
                "analysis_skill": "general",
                "analysis_skill_label": "通用问题分诊",
                "classification_source": selection.source,
                "classification_reason": selection.reason,
                "classification_provider": selection.provider,
                "bug_url": bug_url,
                "user_request_text": request_text,
                "needs_user_direction": True,
                "supported_bug_skills": self.supported_primary_bug_skills(),
            },
        )

    def _bug_time_clarification_result(
        self,
        *,
        context,
        started: float,
        request_text: str,
        bug_url: str,
        time_context: BugTimeContext,
        status: str,
        progress_callback: Callable[[dict[str, object]], None] | None,
        log_coverage: LogCoverage | None = None,
    ) -> TaskResult:
        self._emit_progress(
            progress_callback,
            stage="bug_time_gate_blocked",
            message="问题时间或日志覆盖不满足分析前置条件",
            job_id=context.job_id,
            bug_url=bug_url,
            time_gate_status=status,
            fault_time=time_context.fault_time,
            time_source=time_context.source,
            log_start=log_coverage.start_time if log_coverage else "",
            log_end=log_coverage.end_time if log_coverage else "",
        )
        if status == "missing_fault_time":
            message = (
                "缺少明确问题时间：请补充几月几日 几点几分，越精确越好。\n"
                "我已检查用户输入、Bug 标题和描述，但没有找到可用于定位日志的完整时间点。\n"
                "补充示例：`问题时间 2026-05-11 23:12:30，分析3D卡顿`。"
            )
        elif status == "log_time_unknown":
            message = (
                f"已识别问题时间 `{time_context.fault_time}`，但当前日志无法解析出有效时间范围。\n"
                "请补充包含该时间点附近的已解密文本日志，或确认附件是否为正确日志包。"
            )
        else:
            coverage_text = (
                f"{log_coverage.start_time} ~ {log_coverage.end_time}"
                if log_coverage and log_coverage.start_time
                else "未识别"
            )
            message = (
                f"日志时间范围未覆盖问题时间 `{time_context.fault_time}`。\n"
                f"当前日志覆盖范围：`{coverage_text}`。\n"
                "请补充覆盖该问题时间前后约 10 分钟的日志，或修正问题时间后再继续分析。"
            )
        return TaskResult(
            success=True,
            message=message,
            skipped=True,
            job_id=context.job_id,
            job_dir=context.job_dir,
            duration_seconds=time.monotonic() - started,
            details={
                "mode": "bug_time_clarification",
                "bug_url": bug_url,
                "user_request_text": request_text,
                "time_gate_status": status,
                "fault_time": time_context.fault_time,
                "fault_time_source": time_context.source,
                "fault_time_note": time_context.note,
                "log_coverage_start": log_coverage.start_time if log_coverage else "",
                "log_coverage_end": log_coverage.end_time if log_coverage else "",
                "log_coverage_scanned_files": log_coverage.scanned_files if log_coverage else 0,
                "log_coverage_scanned_lines": log_coverage.scanned_lines if log_coverage else 0,
            },
        )

    def _bug_time_context_payload(self, time_context: BugTimeContext | None) -> dict[str, object]:
        if time_context is None:
            return {}
        return {
            "fault_time": time_context.fault_time,
            "source": time_context.source,
            "note": time_context.note,
            "has_full_datetime": time_context.has_full_datetime,
            "candidates": time_context.candidates,
        }

    def _event_reference_time_text(self, event: LarkEvent | None) -> str:
        if event is None:
            return datetime.now().astimezone().isoformat(timespec="seconds")
        for raw in (event.create_time, event.timestamp):
            text = str(raw or "").strip()
            if not text:
                continue
            if text.isdigit():
                try:
                    return datetime.fromtimestamp(int(text) / 1000, tz=timezone.utc).astimezone().isoformat(
                        timespec="seconds"
                    )
                except (OverflowError, OSError, ValueError):
                    continue
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone().isoformat(timespec="seconds")
        return datetime.now().astimezone().isoformat(timespec="seconds")

    def _skill_name_for_kind(self, kind: str) -> str:
        if _kind_spec(kind).is_source_stage:
            return "source_analysis"
        for skill_name, (mapped_kind, _label, _requires_logs) in self.skill_manager.primary_skill_map().items():
            if mapped_kind == kind:
                return skill_name
        return "general"

    def _resolve_source_stage_skill_name(self, requested_skill: str) -> str:
        normalized = str(requested_skill or "").strip()
        fallback = self._skill_name_for_kind(SOURCE_STAGE_KIND)
        if not normalized or normalized == fallback:
            return fallback
        executor = self.skill_manager.custom_skill_executor_for(normalized)
        if executor in {"file_agent", "pydantic_ai"}:
            # Explicitly configured executor → use this skill
            return normalized
        # executor == "" for two reasons:
        #   (a) builtin/non-routed skill (scene-signal-diagnosis, signal-chain-analyzer, etc.)
        #       → use fallback "source_analysis" so pydantic-ai runs without custom routing
        #   (b) explicitly registered as primary source skill but no executor configured
        #       → return name so executor_not_ready check fires downstream
        # Distinguish by checking if primary_skill_map kind is a source skill kind.
        entry = self.skill_manager.primary_skill_map().get(normalized)
        if entry is not None and entry[0] in _SOURCE_SKILL_KINDS:
            return normalized  # explicitly configured as source skill → trigger executor_not_ready
        return fallback

    def _skill_label_for_name(self, skill_name: str, fallback_kind: str = "general") -> str:
        route = self.skill_manager.primary_skill_map().get(skill_name)
        if route is not None:
            return route[1]
        return self._analysis_label(fallback_kind)

    def _strip_bug_followup_suffix(self, request_text: str) -> str:
        text = request_text.strip()
        for marker in ("\n\n追问/修正：", "\n追问/修正："):
            idx = text.find(marker)
            if idx != -1:
                return text[:idx].rstrip()
        return text

    def _original_bug_request_text(self, previous_context: object, details: dict[str, object]) -> str:
        original = str(details.get("user_request_text") or "").strip()
        if original:
            return original
        request_text = str(getattr(previous_context, "request_text", "") or "").strip()
        return self._strip_bug_followup_suffix(request_text)

    def _resolve_bug_plans(self, *, analysis_kind: str, signal_hint: str, combined_text: str) -> list["BugAnalysisPlan"]:
        kind = (analysis_kind or "").strip()
        if kind == "signal":
            signal_request = parse_signal_request(
                "\n".join(part for part in [signal_hint, combined_text] if part),
                signal_aliases=self.config.signal_aliases,
                command_prefixes=self.config.command_prefixes,
                signal_resolver=self.signal_resolver,
            )
            return [BugAnalysisPlan(kind="signal", signal_code=signal_request.signal)]
        if kind in PLAN_KIND_REGISTRY:
            return [BugAnalysisPlan(kind=kind)]
        return [BugAnalysisPlan(kind="general")]

    def _classify_bug_request_with_agent(
        self,
        *,
        prompt_text: str,
        title: str,
        description: str,
        attachments: object,
        time_context: BugTimeContext | None = None,
    ) -> BugAnalysisSelection | None:
        primary_skills = [item for item in self._available_bug_skills() if item.get("role") == "primary"]
        aux_skills = [item for item in self._available_bug_skills() if item.get("role") == "auxiliary"]
        primary_skill_names = [str(item.get("name") or "") for item in primary_skills if item.get("name")]
        analysis_kinds = sorted({str(item.get("kind") or "general") for item in primary_skills if item.get("kind")})
        payload = {
            "user_prompt": prompt_text,
            "bug_title": title,
            "bug_description": description[:4000],
            "attachments": attachments if isinstance(attachments, list) else [],
            "problem_time": self._bug_time_context_payload(time_context),
            "primary_skills": primary_skills,
            "auxiliary_skills": aux_skills,
        }
        prompt = (
            "你是 Lark Agent Bridge 的 bug skill 分类器。"
            "请根据用户请求、Bug 标题、描述和当前工作区技能，选择最合适的主分析 skill。"
            "在选择 skill 前必须先参考 problem_time；如果 has_full_datetime=false，表示问题时间不足，后续应先要求补充时间而不是继续分析。"
            "只有当没有任何专用 skill 明确匹配时，才选择 general。"
            "general 不是让系统盲扫源码给根因，而是表示需要分诊、材料检查或要求用户补充更明确方向。"
            "先判断是否有更专一的 primary skill；signal-chain-analyzer 优先级最低，只在用户明确要排查某个具体 SignalCode / SIGNAL_... 的通用信号链路、信号来源或是否送达时使用，"
            "不要因为文本里出现 signal/信号 字样就滥用。"
            "3D场景信号 / SceneType / 上电P / 临停P / 特殊场景 / 小憩 / 露营 / 洗车 / 充电场景 / 放电场景 / 场景选择 / 离车舒享 / 行车场景 / 泊车场景 优先考虑 scene-signal-diagnosis。"
            "xtheme / 105004 / 105009 / 晨曦 / 傍晚 / 主题切换 / XuiConditionHelper 应优先考虑 xtheme-analyzer。"
            "只输出一个 JSON 对象，字段必须完整："
            f'{{"analysis_kind":"{"|".join(analysis_kinds or ["general"])}",'
            f'"skill":"{"|".join(primary_skill_names or ["general"])}",'
            '"signal_hint":"可为空",'
            '"reason":"一句中文理由"}'
            "\n输入 JSON：\n"
            f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
        )
        parsed, provider = self._run_bug_decision_agent(prompt)
        if parsed is None:
            return None
        kind = str(parsed.get("analysis_kind") or "").strip()
        skill = str(parsed.get("skill") or "").strip()
        reason = str(parsed.get("reason") or "").strip()
        signal_hint = str(parsed.get("signal_hint") or "").strip()
        primary_skill_map = self.skill_manager.primary_skill_map()
        if skill in primary_skill_map:
            kind = primary_skill_map[skill][0] or kind
        plans = self._resolve_bug_plans(
            analysis_kind=kind,
            signal_hint=signal_hint,
            combined_text="\n".join(part for part in [prompt_text, title, description] if part),
        )
        selection = self._selection_from_plans(
            plans,
            source="agent",
            reason=reason or "Agent 已完成 bug skill 分类。",
            provider=provider,
        )
        if skill:
            selection.skill_name = skill
            selection.skill_label = self._skill_label_for_name(skill, plans[0].kind if plans else "general")
        return selection

    def decide_bug_followup(
        self,
        *,
        followup_text: str,
        previous_context: object,
        previous_session: dict[str, object],
    ) -> BugFollowupSelection | None:
        details = previous_session.get("details", {}) if isinstance(previous_session, dict) else {}
        if not isinstance(details, dict):
            details = {}
        request_text = self._original_bug_request_text(previous_context, details)
        summary_text = str(getattr(previous_context, "summary_text", "") or "")
        report_excerpt = str(getattr(previous_context, "report_excerpt", "") or "")
        prepared_log_input = str(details.get("prepared_log_input") or "")
        selected_log_input = str(details.get("selected_log_input") or "")
        primary_skills = [item for item in self._available_bug_skills() if item.get("role") == "primary"]
        primary_skill_names = [str(item.get("name") or "") for item in primary_skills if item.get("name")]
        analysis_kinds = sorted({str(item.get("kind") or "general") for item in primary_skills if item.get("kind")})
        payload = {
            "request_text": request_text,
            "followup_text": followup_text,
            "summary_text": summary_text[:3000],
            "report_excerpt": report_excerpt[:5000],
            "prepared_log_input": prepared_log_input,
            "selected_log_input": selected_log_input,
            "current_analysis_kinds": details.get("analysis_kinds") or [],
            "primary_skills": primary_skills,
        }
        deterministic = self._deterministic_bug_followup_selection(
            followup_text=followup_text,
            request_text=request_text,
        )
        if deterministic is not None:
            return deterministic
        prompt = (
            "你是 Lark Agent Bridge 的 bug 续聊决策器。"
            "请先判断当前追问能否直接基于已有分析结果回答；如果不能，再决定是否需要重新分析，"
            "并选择最合适的主分析 skill。"
            "如果没有专用 skill 明确匹配，general 只代表分诊或澄清，不要把泛泛请求改写成源码根因分析。"
            "先判断是否有更专一的 primary skill；signal-chain-analyzer 优先级最低，只用于明确给出具体 SignalCode / SIGNAL_... 的通用信号链路问题；"
            "3D场景信号 / SceneType / 上电P / 临停P / 特殊场景 / 小憩 / 露营 / 洗车 / 充电场景 / 放电场景 / 场景选择 / 离车舒享 / 行车场景 / 泊车场景 优先考虑 scene-signal-diagnosis。"
            "xtheme / 105004 / 105009 / 晨曦 / 傍晚 / 主题切换 / XuiConditionHelper 优先考虑 xtheme-analyzer。"
            "如果选择的 skill 需要日志，而当前 prepared_log_input / selected_log_input 为空，请把 retry_download_if_missing 设为 true。"
            "只输出一个 JSON 对象，字段必须完整："
            '{"action":"answer_from_existing|reanalyze",'
            f'"analysis_kind":"{"|".join(analysis_kinds or ["general"])}",'
            f'"skill":"{"|".join(primary_skill_names or ["general"])}",'
            '"signal_hint":"可为空",'
            '"retry_download_if_missing":true,'
            '"reason":"一句中文理由"}'
            "\n输入 JSON：\n"
            f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
        )
        parsed, provider = self._run_bug_decision_agent(prompt)
        if parsed is None:
            return None
        action = str(parsed.get("action") or "").strip()
        kind = str(parsed.get("analysis_kind") or "").strip()
        skill = str(parsed.get("skill") or "").strip()
        signal_hint = str(parsed.get("signal_hint") or "").strip()
        reason = str(parsed.get("reason") or "").strip()
        retry_download = bool(parsed.get("retry_download_if_missing"))
        primary_skill_map = self.skill_manager.primary_skill_map()
        if skill in primary_skill_map:
            kind = primary_skill_map[skill][0] or kind
        plans = self._resolve_bug_plans(
            analysis_kind=kind,
            signal_hint=signal_hint,
            combined_text="\n".join(part for part in [request_text, followup_text] if part),
        )
        if any(plan.kind == "signal" and not plan.signal_code for plan in plans):
            return None
        selection = self._selection_from_plans(
            plans,
            source="agent",
            reason=reason or "Agent 已完成 bug 续聊决策。",
            provider=provider,
        )
        if skill:
            selection.skill_name = skill
            selection.skill_label = self._skill_label_for_name(skill, plans[0].kind if plans else "general")
        return BugFollowupSelection(
            should_reanalyze=action == "reanalyze",
            force_rerun=action == "reanalyze",
            plans=selection.plans,
            skill_name=selection.skill_name,
            skill_label=selection.skill_label,
            source=selection.source,
            reason=selection.reason,
            provider=selection.provider,
        )

    def _deterministic_bug_followup_selection(
        self,
        *,
        followup_text: str,
        request_text: str,
    ) -> BugFollowupSelection | None:
        lowered = followup_text.casefold()
        followup_action = parse_followup_action(followup_text)
        force_terms = tuple(term.casefold() for term in self.config.bug_analysis.force_reanalysis_terms)
        selection = self._manual_followup_selection_if_explicit(
            followup_text=followup_text,
            request_text=request_text,
        )
        if any(term in lowered for term in force_terms):
            return self._build_bug_followup_selection(
                should_reanalyze=True,
                force_rerun=True,
                selection=selection,
                reason=(
                    "命中本地强制重分析词，并识别到新的分析方向。"
                    if selection is not None
                    else "命中本地强制重分析词，沿用上一轮分析类型重新执行。"
                ),
                source="deterministic_fallback",
            )
        if followup_action == "retry":
            return self._build_bug_followup_selection(
                should_reanalyze=True,
                force_rerun=True,
                selection=selection,
                reason=(
                    "命中统一续跑动作，并识别到新的分析方向。"
                    if selection is not None
                    else "命中统一续跑动作，沿用上一轮分析类型重新执行。"
                ),
                source="deterministic_fallback",
            )
        if parse_signal_request(
            followup_text,
            signal_aliases=self.config.signal_aliases,
            command_prefixes=self.config.command_prefixes,
            signal_resolver=self.signal_resolver,
        ).signal:
            return self._build_bug_followup_selection(
                should_reanalyze=True,
                force_rerun=True,
                selection=selection,
                reason="本地回退识别到明确信号请求，触发重分析。",
                source="deterministic_fallback",
            )
        has_correction = any(term in lowered for term in ("修正", "修复问题时间", "更正", "修改", "改成"))
        has_time = re.search(r"(?<!\d)\d{1,2}[:：]\d{2}(?:\s*分)?(?!\d)", followup_text) is not None
        if has_correction and has_time:
            return self._build_bug_followup_selection(
                should_reanalyze=True,
                force_rerun=True,
                selection=selection,
                reason=(
                    "本地回退识别到时间修正和新的分析方向，触发重分析。"
                    if selection is not None
                    else "本地回退识别到时间修正，沿用上一轮分析类型重分析。"
                ),
                source="deterministic_fallback",
            )
        return None

    def _manual_followup_selection_if_explicit(
        self,
        *,
        followup_text: str,
        request_text: str,
    ) -> "BugAnalysisSelection | None":
        lowered_followup = (followup_text or "").casefold()
        combined_lowered = f"{request_text}\n{followup_text}".casefold()

        def _scene_signal_source_followup_selection() -> "BugAnalysisSelection | None":
            if (
                self._followup_explicitly_requests_source_analysis(request_text, followup_text)
                and ("信号" in followup_text or "signal" in lowered_followup)
                and (
                    _has_strong_scene_signal_intent(combined_lowered)
                    or looks_like_scene_signal_request(request_text)
                )
            ):
                return self.selection_for_skill_name(
                    "scene-signal-diagnosis",
                    source="deterministic_fallback",
                    reason="源码追问命中场景信号上下文。",
                )
            return None

        if not self._followup_has_explicit_bug_route(followup_text):
            return _scene_signal_source_followup_selection()
        selection = self._manual_bug_selection(
            prompt_text=followup_text,
            title="",
            description="",
        )
        if selection.skill_name == "general" and all(plan.kind == "general" for plan in selection.plans):
            return _scene_signal_source_followup_selection()
        return selection

    def _build_bug_followup_selection(
        self,
        *,
        should_reanalyze: bool,
        force_rerun: bool,
        selection: "BugAnalysisSelection | None",
        reason: str,
        source: str,
    ) -> BugFollowupSelection:
        if selection is None:
            return BugFollowupSelection(
                should_reanalyze=should_reanalyze,
                force_rerun=force_rerun,
                plans=[],
                skill_name="",
                skill_label="",
                source=source,
                reason=reason,
                provider="",
            )
        return BugFollowupSelection(
            should_reanalyze=should_reanalyze,
            force_rerun=force_rerun,
            plans=selection.plans,
            skill_name=selection.skill_name,
            skill_label=selection.skill_label,
            source=selection.source,
            reason=reason,
            provider=selection.provider,
        )

    def _followup_has_explicit_bug_route(self, text: str) -> bool:
        lowered = (text or "").casefold()
        if parse_signal_request(
            text,
            signal_aliases=self.config.signal_aliases,
            command_prefixes=self.config.command_prefixes,
            signal_resolver=self.signal_resolver,
        ).signal:
            return True
        route_terms = (
            STARTUP_ROUTE_TERMS
            + STARTUP_BLOCK_ROUTE_TERMS
            + STUCK_ROUTE_TERMS
            + CRASH_ROUTE_TERMS
            + SIGNAL_ROUTE_TERMS
            + SCENE_SIGNAL_ROUTE_TERMS
            + XTHEME_ROUTE_TERMS
            + PERCEPTION_ROUTE_TERMS
            + LD_LANE_LEVEL_ROUTE_TERMS
            + PULLOVER_CHAIN_ROUTE_TERMS
        )
        return any(term.casefold() in lowered for term in route_terms)

    def _run_bug_decision_agent(self, prompt: str) -> tuple[dict[str, object] | None, str]:
        candidates = _provider_candidates(self.config.bug_analysis.provider, self.config.bug_analysis.command)
        last_error: Exception | None = None
        for provider, command_name in candidates:
            command, output_path = self._build_bug_decision_command(provider, command_name, prompt)
            if not command:
                continue
            debug_log_path = self._subprocess_debug_log_path(
                self.config.data_dir / "subprocess_debug",
                f"bug-analysis-classifier-{provider or 'agent'}",
            )
            try:
                completed = _run_tracked_process(
                    command,
                    watchdog=self.process_watchdog,
                    name="bug-analysis-classifier",
                    cwd=self._working_dir(),
                    capture_output=True,
                    text=True,
                    timeout=min(self.config.bug_analysis.timeout_seconds, 300),
                    check=False,
                    debug_log_path=debug_log_path,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                last_error = exc
                if output_path is not None:
                    output_path.unlink(missing_ok=True)
                continue
            raw = self._read_bug_decision_response(completed=completed, output_path=output_path)
            if completed.returncode != 0:
                last_error = RuntimeError(raw or completed.stderr or completed.stdout or "bug decision agent failed")
                continue
            try:
                return self._parse_bug_decision_json(raw), provider
            except ValueError as exc:
                last_error = exc
                continue
        return None, ""

    def _build_bug_decision_command(self, provider: str, command_name: str, prompt: str) -> tuple[list[str], Path | None]:
        system_prompt = (
            "你是 Lark Agent Bridge 的结构化分类器。"
            "只能根据给定输入选择 skill 和分析动作，不能调用工具，不能假装读取额外文件。"
            "你必须只输出 JSON 对象。"
        )
        if provider == "codex":
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", prefix="lark-bug-decision-", delete=False) as fh:
                output_path = Path(fh.name)
            model = (self.config.bug_analysis.model or "").strip()
            command = [
                command_name,
                "exec",
                "--skip-git-repo-check",
                "-s",
                "read-only",
                "-C",
                str(self._working_dir()),
            ]
            if model:
                command.extend(["-m", model])
            command.extend(
                [
                    "--output-last-message",
                    str(output_path),
                    f"{system_prompt}\n\n{prompt}",
                ]
            )
            return command, output_path
        if provider in {"claude", "claude-code", "claude_code"}:
            command = [
                command_name,
                "--print",
                "--output-format",
                "text",
                "--no-session-persistence",
                "--permission-mode",
                "dontAsk",
                "--tools",
                "",
                "--append-system-prompt",
                system_prompt,
                prompt,
            ]
            return command, None
        return [], None

    def _read_bug_decision_response(self, *, completed: subprocess.CompletedProcess[str], output_path: Path | None) -> str:
        try:
            if output_path is not None and output_path.exists():
                content = output_path.read_text(encoding="utf-8").strip()
                if content:
                    return content
            return completed.stdout.strip()
        finally:
            if output_path is not None:
                output_path.unlink(missing_ok=True)

    def _parse_bug_decision_json(self, raw_response: str) -> dict[str, object]:
        cleaned = raw_response.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned)
        if not cleaned.startswith("{"):
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start == -1 or end == -1 or end <= start:
                raise ValueError("no JSON object found")
            cleaned = cleaned[start : end + 1]
        parsed = json.loads(cleaned)
        if not isinstance(parsed, dict):
            raise ValueError("response must be a JSON object")
        return parsed

    def run_bug_analysis(
        self,
        request: BugRequest,
        *,
        event: LarkEvent | None = None,
        progress_callback: Callable[[dict[str, object]], None] | None = None,
    ) -> TaskResult:
        options = self.config.bug_analysis
        if not options.enabled:
            return TaskResult(
                success=True,
                message="Bug analysis agent is disabled",
                skipped=True,
                details={"mode": "bug_analysis"},
            )
        if request.error == "missing_bug_url" or not request.bug_url.strip():
            return TaskResult(
                success=False,
                message="缺少 Bug 链接：请提供 project.feishu.cn 的 bug 详情页 URL。",
                error_code="missing_bug_url",
                details={"mode": "bug_analysis"},
            )

        prompt_text = request.prompt.strip() or options.default_prompt.strip()
        if len(prompt_text) > options.max_prompt_chars:
            return TaskResult(
                success=False,
                message=f"Bug 分析描述过长，请压缩到 {options.max_prompt_chars} 字以内。",
                error_code="bug_prompt_too_long",
                details={"mode": "bug_analysis"},
            )

        context = create_job_context(self.config.data_dir, event=event)
        bridge_session_id = self._bridge_session_id(event)
        bridge_kwargs = {"bridge_session_id": bridge_session_id} if bridge_session_id else {}
        metadata_path = context.output_dir / "bug_metadata.md"
        request_text = self._request_text(raw_text=request.raw_text, prompt_text=prompt_text, bug_url=request.bug_url)
        plans = self.classify_requests(prompt_text=prompt_text, title="", description="")
        plan = plans[0]
        time_context = self._resolve_bug_time_context(
            request_text=request_text,
            title="",
            description="",
            reference_time=None,
        )
        target_time = (
            time_context.fault_time
            if time_context.has_full_datetime and _kind_accepts_target_time(plan.kind)
            else None
        )
        bug_dir = context.input_dir / f"bug_{self._bug_id(request.bug_url)}"
        html_path = context.output_dir / self._report_name(plan.kind, "html")
        json_path = context.output_dir / self._report_name(plan.kind, "json")
        analysis_dir = context.output_dir / f"{plan.kind}_analysis"
        command = self.build_command(
            plan=plan,
            input_path=bug_dir,
            html_path=html_path,
            json_path=json_path,
            analysis_dir=analysis_dir,
            target_time=target_time,
        )
        request_artifact = context.output_dir / "bug_agent_request.md"
        request_artifact.write_text(
            self._render_bug_agent_request(
                request_text=request_text,
                prompt_text=prompt_text,
                bug_url=request.bug_url,
                plans=plans,
            ),
            encoding="utf-8",
        )
        self._emit_progress(
            progress_callback,
            stage="bug_job_created",
            message="已创建 bug 分析任务",
            job_id=context.job_id,
            bug_url=request.bug_url,
            request_text=request_text,
            analysis_kinds=[item.kind for item in plans],
        )

        if self.config.dry_run:
            if [item.kind for item in plans] == ["startup", "stuck"]:
                planned_reports = (
                    f"- {context.output_dir / self._report_name('startup', 'html')}\n"
                    f"- {context.output_dir / self._report_name('stuck', 'html')}\n"
                    f"- {context.output_dir / self._combined_report_name('html')}"
                )
            else:
                planned_reports = "\n".join(
                    f"- {context.output_dir / self._report_name(item.kind, 'html')}"
                    for item in plans
                )
            self._emit_progress(
                progress_callback,
                stage="bug_dry_run_planned",
                message="dry-run 已规划 bug 分析任务",
                job_id=context.job_id,
                analysis_kinds=[item.kind for item in plans],
            )
            return TaskResult(
                success=True,
                message=(
                    "dry-run: bug 分析命令已规划\n"
                    f"metadata: {metadata_path}\n"
                    f"reports:\n{planned_reports}"
                ),
                job_id=context.job_id,
                job_dir=context.job_dir,
                command=command,
                details={
                    "mode": "bug_analysis",
                    "analysis_kind": plan.kind,
                    "analysis_kinds": [item.kind for item in plans],
                    "signal_code": plan.signal_code,
                    "user_request_text": request_text,
                    "agent_request_file": str(request_artifact),
                },
            )

        started = time.monotonic()
        try:
            self._emit_progress(progress_callback, stage="bug_check_env", message="检查 meegle 环境")
            env_status = self._run_json_command(
                [str(self._bug_fetcher_script()), "check-env"],
                timeout=60,
                **bridge_kwargs,
            )
            if not env_status.get("meegle_installed", False):
                return self._failure(
                    context=context,
                    command=command,
                    started=started,
                    message="Bug 分析前置条件缺失：本机未安装 meegle CLI。",
                    error_code="bug_analysis_missing_meegle",
                    progress_callback=progress_callback,
                )
            if not env_status.get("auth_ok", False):
                return self._failure(
                    context=context,
                    command=command,
                    started=started,
                    message="Bug 分析前置条件缺失：meegle 未登录，请先在本机完成 `meegle auth login`。",
                    error_code="bug_analysis_meegle_not_auth",
                    progress_callback=progress_callback,
                )

            self._emit_progress(progress_callback, stage="bug_resolve_url", message="解析 bug 链接")
            resolved = self._run_json_command(
                [str(self._bug_fetcher_script()), "resolve-url", request.bug_url],
                timeout=60,
                **bridge_kwargs,
            )
            project_key = str(resolved["project_key"])
            work_item_id = str(resolved["work_item_id"])
            bug_dir = self._bug_cache_dir(project_key, work_item_id)
            if bug_dir.exists() and not self._is_bug_cache_fresh(
                bug_dir,
                max_age_hours=self.config.job_retention.bug_cache_max_age_hours,
            ):
                self._remove_tree(bug_dir)
            bug_dir.mkdir(parents=True, exist_ok=True)
            self._emit_progress(
                progress_callback,
                stage="bug_fetch_data",
                message="拉取 bug 详情和字段信息",
                project_key=project_key,
                work_item_id=work_item_id,
            )
            # Parallel fetch: bug data + full work item + signal catalog pre-warm
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                future_fetched = pool.submit(
                    self._run_json_command,
                    [str(self._bug_fetcher_script()), "fetch-data", project_key, work_item_id],
                    timeout=120,
                    **bridge_kwargs,
                )
                future_full_item = pool.submit(
                    self._run_json_command,
                    ["meegle", "workitem", "get", "--project-key", project_key,
                     "--work-item-id", work_item_id, "--format", "json"],
                    timeout=120,
                    **bridge_kwargs,
                )
                # Pre-warm signal catalog in background (uses Phase 4 cache)
                pool.submit(self.signal_resolver._load_catalog)
                fetched = future_fetched.result()
                full_item = future_full_item.result()
            option_map = self._load_option_map(project_key, **bridge_kwargs)
            title = str(fetched.get("title", ""))
            description = self._bug_description(fetched)
            time_context = self._resolve_bug_time_context(
                request_text=request_text,
                title=title,
                description=description,
                reference_time=self._bug_reference_time(fetched, full_item),
            )
            decision = self._unified_classify_and_decide(
                request_text=request_text,
                prompt_text=prompt_text,
                title=title,
                description=description,
                attachments=fetched.get("attachments", []),
                time_context=time_context,
            )
            selection = decision.selection
            source_decision = decision.source_decision
            if self._needs_general_direction(selection, prompt_text=prompt_text):
                return self._general_direction_needed_result(
                    context=context,
                    selection=selection,
                    started=started,
                    request_text=request_text,
                    bug_url=request.bug_url,
                    title=title,
                    progress_callback=progress_callback,
                )
            plans = selection.plans
            if not time_context.has_full_datetime:
                return self._bug_time_clarification_result(
                    context=context,
                    started=started,
                    request_text=request_text,
                    bug_url=request.bug_url,
                    time_context=time_context,
                    status="missing_fault_time",
                    progress_callback=progress_callback,
                )
            requires_log_input = any(self._plan_requires_log_input(item) for item in plans)
            selected_input = self._select_log_input(bug_dir, fetched)
            cache_reused = False
            if self._has_bug_cache_content(bug_dir) and (selected_input is not None or not requires_log_input):
                cache_reused = True
                download = {"ok": True, "downloaded": [], "unzipped": [], "errors": [], "skipped": [], "reused": True}
                self._emit_progress(
                    progress_callback,
                    stage="bug_reuse_cache",
                    message="复用同一 bug 已缓存的附件和日志",
                    project_key=project_key,
                    work_item_id=work_item_id,
                    bug_cache_dir=str(bug_dir),
                )
            else:
                self._emit_progress(progress_callback, stage="bug_download_logs", message="下载 bug 附件和日志")
                download = self._download_bug_attachments(
                    project_key,
                    work_item_id,
                    bug_dir,
                    fetched.get("attachments", []),
                    timeout=options.timeout_seconds,
                    **bridge_kwargs,
                )
                selected_input = self._select_log_input(bug_dir, fetched)
            plan = plans[0]
            html_path = context.output_dir / self._report_name(plan.kind, "html")
            json_path = context.output_dir / self._report_name(plan.kind, "json")
            analysis_dir = context.output_dir / f"{plan.kind}_analysis"
            if requires_log_input and selected_input is None:
                attachment_lines = self._render_attachment_lines(fetched.get("attachments", []), download)
                return self._failure(
                    context=context,
                    command=command,
                    started=started,
                    message=(
                        "Bug 分析失败：当前请求包含日志分析，但未找到可用日志附件。"
                        f"\n附件结果：\n{attachment_lines}"
                    ),
                    error_code="bug_analysis_missing_log_attachment",
                    progress_callback=progress_callback,
                )
            if any(item.kind == "signal" and not item.signal_code for item in plans):
                return self._failure(
                    context=context,
                    command=command,
                    started=started,
                    message="Bug 分析失败：识别到信号链路问题，但消息和 Bug 描述里没有明确的 SignalCode/枚举名。",
                    error_code="bug_analysis_missing_signal_code",
                    progress_callback=progress_callback,
                )

            self._emit_progress(progress_callback, stage="bug_prepare_logs", message="准备日志输入")
            prepared_input = self._reuse_prepared_bug_input(selected_input) if selected_input else None
            if prepared_input is None:
                prepared_input = self._prepare_log_input(selected_input) if selected_input else None
            self._write_bug_cache_metadata(
                bug_dir,
                bug_url=request.bug_url,
                project_key=project_key,
                work_item_id=work_item_id,
                selected_input=selected_input,
                prepared_input=prepared_input,
            )
            fault_time, fault_time_note = time_context.fault_time, time_context.note
            log_coverage = self._scan_log_time_coverage(prepared_input, fault_time=fault_time) if prepared_input else None
            if log_coverage is None or not log_coverage.has_time_evidence:
                return self._bug_time_clarification_result(
                    context=context,
                    started=started,
                    request_text=request_text,
                    bug_url=request.bug_url,
                    time_context=time_context,
                    status="log_time_unknown",
                    progress_callback=progress_callback,
                    log_coverage=log_coverage,
                )
            if not log_coverage.covers_fault_time:
                return self._bug_time_clarification_result(
                    context=context,
                    started=started,
                    request_text=request_text,
                    bug_url=request.bug_url,
                    time_context=time_context,
                    status="log_not_covering_fault_time",
                    progress_callback=progress_callback,
                    log_coverage=log_coverage,
                )
            source_evidence_enabled = (
                self._should_collect_source_evidence(request_text, prompt_text)
                or any(_kind_spec(plan.kind).is_source_stage for plan in plans)
                or (
                    any(_kind_spec(p.kind).needs_source_evidence for p in plans)
                    and self._has_explicit_general_scope(prompt_text)
                )
            )
            # Start source evidence collection in background while analysis runs
            source_evidence_future: concurrent.futures.Future[Path | None] | None = None
            if source_evidence_enabled:
                _source_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
                source_evidence_future = _source_pool.submit(
                    self._write_reanalysis_source_evidence,
                    plans=plans,
                    request_text=request_text,
                    followup_text=prompt_text,
                    output_dir=context.output_dir,
                    enabled=True,
                    extra_texts=(title, description),
                )
                _source_pool.shutdown(wait=False)
            else:
                source_evidence_future = None
            # Source evidence is optional. Do not block before file-agent plans:
            # their progress/toolcall events should start even when source
            # search is slow or codegraph is cold.
            source_evidence_path: Path | None = None
            if source_evidence_future is not None:
                if any(item.kind in _SOURCE_SKILL_KINDS for item in plans):
                    source_evidence_path = context.output_dir / "bug_source_evidence.md"
                else:
                    source_evidence_path = self._resolve_source_evidence_future(source_evidence_future)
            html_paths: list[Path] = []
            report_jsons: dict[str, Path | None] = {}
            skill_file_agent_execution_result: dict[str, object] | None = None
            for current_plan in plans:
                current_html = context.output_dir / self._report_name(current_plan.kind, "html")
                current_json = context.output_dir / self._report_name(current_plan.kind, "json")
                current_analysis_dir = context.output_dir / f"{current_plan.kind}_analysis"
                input_for_plan = prepared_input
                if current_plan.kind == "startup" and prepared_input is not None:
                    input_for_plan = self._startup_analysis_input(prepared_input, fault_time)
                current_command = self.build_command(
                    plan=current_plan,
                    input_path=input_for_plan or bug_dir,
                    html_path=current_html,
                    json_path=current_json,
                    analysis_dir=current_analysis_dir,
                    target_time=fault_time if _kind_accepts_target_time(current_plan.kind) else None,
                    request_text=request_text if current_plan.kind == "xtheme" else None,
                )
                self._emit_progress(
                    progress_callback,
                    stage="bug_run_analysis",
                    message=f"执行{self._analysis_label(current_plan.kind)}",
                    plan=current_plan.kind,
                    plan_label=self._analysis_label(current_plan.kind),
                    html_path=str(current_html),
                    json_path=str(current_json),
                )
                if current_plan.kind == "general":
                    self._write_general_bug_report(
                        html_path=current_html,
                        json_path=current_json,
                        title=title,
                        description=description,
                        prompt_text=prompt_text,
                        request_text=request_text,
                        fault_time=fault_time,
                        selected_input=selected_input,
                        source_evidence_path=source_evidence_path,
                        classification_skill=selection.skill_name,
                        classification_source=selection.source,
                        classification_reason=selection.reason,
                    )
                    completed = subprocess.CompletedProcess(args=current_command, returncode=0, stdout="", stderr="")
                elif _kind_spec(current_plan.kind).is_custom_agent:
                    if current_plan.kind == "ld_lane_level":
                        current_skill_name = self._skill_name_for_kind(current_plan.kind)
                    elif _kind_spec(current_plan.kind).is_source_stage:
                        if getattr(source_decision, "source_mode", "") == "append" and not getattr(source_decision, "context_profile", ""):
                            return self._failure(
                                context=context,
                                command=current_command,
                                started=started,
                                message="Bug 分析失败：源码阶段缺少领域上下文，已停止以避免泛化扫描。",
                                error_code="source_stage_missing_context",
                                progress_callback=progress_callback,
                                details={
                                    "analysis_kind": current_plan.kind,
                                    "analysis_kinds": [item.kind for item in plans],
                                    "source_mode": getattr(source_decision, "source_mode", ""),
                                    "context_profile": getattr(source_decision, "context_profile", ""),
                                    "stage_kinds": list(getattr(source_decision, "stage_kinds", []) or []),
                                },
                            )
                        current_skill_name = self._resolve_source_stage_skill_name(selection.skill_name)
                    else:
                        current_skill_name = selection.skill_name or self._skill_name_for_kind(current_plan.kind)
                    if _kind_spec(current_plan.kind).needs_custom_executor_check and current_skill_name != "source_analysis" and self.skill_manager.custom_skill_executor_for(current_skill_name) not in {"file_agent", "pydantic_ai"}:
                        prefix = current_plan.kind if current_plan.kind in _SOURCE_SKILL_KINDS else SOURCE_STAGE_KIND
                        return self._failure(
                            context=context,
                            command=current_command,
                            started=started,
                            message=self._custom_skill_executor_not_ready_message(
                                current_skill_name,
                                selected_input=selected_input,
                            ),
                            error_code=f"{prefix}_executor_not_ready",
                            progress_callback=progress_callback,
                            details={
                                "analysis_kind": current_plan.kind,
                                "analysis_kinds": [item.kind for item in plans],
                                "analysis_skill": current_skill_name,
                                "target_time": fault_time,
                                "selected_log_input": str(selected_input or ""),
                                "prepared_log_input": str(prepared_input or ""),
                            },
                        )
                    # LD lane-level: try pydantic-ai first, then executor+direct_api (fast path)
                    if current_plan.kind == "ld_lane_level":
                        custom_result = self._run_ld_pydantic_ai_analysis(
                            skill_name=current_skill_name,
                            request_text=request_text,
                            prompt_text=prompt_text,
                            title=title,
                            description=description,
                            fault_time=fault_time,
                            selected_input=selected_input,
                            prepared_input=prepared_input,
                            html_path=current_html,
                            json_path=current_json,
                            analysis_dir=current_analysis_dir,
                            progress_callback=progress_callback,
                            prior_findings=self._summarize_prior_report_jsons(report_jsons),
                        )
                        if custom_result.get("ok"):
                            logger.info("LD pydantic-ai succeeded, skipping direct_api/file_agent")
                        else:
                            logger.warning(
                                "LD pydantic-ai failed (error=%s), trying direct_api",
                                custom_result.get("error_code", "unknown"),
                            )
                            custom_result = self._run_ld_direct_api_analysis(
                                skill_name=current_skill_name,
                                request_text=request_text,
                                prompt_text=prompt_text,
                                title=title,
                                description=description,
                                fault_time=fault_time,
                                selected_input=selected_input,
                                prepared_input=prepared_input,
                                html_path=current_html,
                                json_path=current_json,
                                analysis_dir=current_analysis_dir,
                                progress_callback=progress_callback,
                            )
                        if not custom_result.get("ok"):
                            logger.warning(
                                "LD direct_api failed (error=%s), falling back to file_agent",
                                custom_result.get("error_code", "unknown"),
                            )
                            self._emit_progress(
                                progress_callback,
                                stage="ld_direct_api_fallback",
                                message=f"LD direct_api 失败（{custom_result.get('error_code')}），切换 file_agent",
                                error_code=str(custom_result.get("error_code") or ""),
                                error_message=str(custom_result.get("message") or ""),
                            )
                            custom_result = self._run_custom_skill_agent_analysis(
                                analysis_kind=current_plan.kind,
                                analysis_label=self._analysis_label(current_plan.kind),
                                skill_name=current_skill_name,
                                request_text=request_text,
                                prompt_text=prompt_text,
                                title=title,
                                description=description,
                                fault_time=fault_time,
                                selected_input=selected_input,
                                prepared_input=prepared_input,
                                source_evidence_path=source_evidence_path,
                                html_path=current_html,
                                json_path=current_json,
                                analysis_dir=current_analysis_dir,
                                progress_callback=progress_callback,
                                timeout=self._agent_summary_timeout(
                                    options.timeout_seconds,
                                    reference_seconds=max(time.monotonic() - started, 240.0),
                                ),
                                bridge_session_id=bridge_session_id,
                                prior_findings=self._summarize_prior_report_jsons(report_jsons),
                            )
                    else:
                        # Source stage: if app-server-backed file-agent is enabled, prefer it
                        # directly and leave the later summary stage on the configured AI path.
                        if _kind_spec(current_plan.kind).is_source_stage:
                            if self._prefer_source_stage_file_agent():
                                self._emit_progress(
                                    progress_callback,
                                    stage="source_stage_app_server_preferred",
                                    message="已启用 codex app-server，源码阶段直接走 file-agent 路径",
                                )
                                execution_skill_name = (
                                    getattr(source_decision, "context_profile", "").strip()
                                    or current_skill_name
                                )
                                custom_result = self._run_custom_skill_agent_analysis(
                                    analysis_kind=current_plan.kind,
                                    analysis_label=self._analysis_label(current_plan.kind),
                                    skill_name=execution_skill_name,
                                    request_text=request_text,
                                    prompt_text=prompt_text,
                                    title=title,
                                    description=description,
                                    fault_time=fault_time,
                                    selected_input=selected_input,
                                    prepared_input=prepared_input,
                                    source_evidence_path=source_evidence_path,
                                    html_path=current_html,
                                    json_path=current_json,
                                    analysis_dir=current_analysis_dir,
                                    progress_callback=progress_callback,
                                    timeout=self._source_stage_file_agent_timeout(
                                        reference_seconds=max(time.monotonic() - started, 240.0),
                                    ),
                                    bridge_session_id=bridge_session_id,
                                    prior_findings=self._summarize_prior_report_jsons(report_jsons),
                                    context_profile=execution_skill_name,
                                    provider_override="codex",
                                    command_override=self.config.codex_app_server.command,
                                )
                            else:
                                custom_result = self._run_source_stage_pydantic_ai(
                                    analysis_kind=current_plan.kind,
                                    skill_name=current_skill_name,
                                    request_text=request_text,
                                    prompt_text=prompt_text,
                                    title=title,
                                    description=description,
                                    fault_time=fault_time,
                                    selected_input=selected_input,
                                    prepared_input=prepared_input,
                                    source_evidence_path=source_evidence_path,
                                    html_path=current_html,
                                    json_path=current_json,
                                    analysis_dir=current_analysis_dir,
                                    progress_callback=progress_callback,
                                    context_profile=getattr(source_decision, "context_profile", ""),
                                    prior_findings=self._summarize_prior_report_jsons(report_jsons),
                                )
                                if custom_result.get("ok"):
                                    logger.info("source_stage pydantic-ai succeeded, skipping file_agent")
                                else:
                                    logger.warning(
                                        "source_stage pydantic-ai failed (error=%s), falling back to file_agent",
                                        custom_result.get("error_code", "unknown"),
                                    )
                                    self._emit_progress(
                                        progress_callback,
                                        stage="source_stage_pydantic_ai_fallback",
                                        message=f"pydantic-ai 失败（{custom_result.get('error_code')}），切换 file_agent",
                                    )
                                    custom_result = self._run_custom_skill_agent_analysis(
                                        analysis_kind=current_plan.kind,
                                        analysis_label=self._analysis_label(current_plan.kind),
                                        skill_name=current_skill_name,
                                        request_text=request_text,
                                        prompt_text=prompt_text,
                                        title=title,
                                        description=description,
                                        fault_time=fault_time,
                                        selected_input=selected_input,
                                        prepared_input=prepared_input,
                                        source_evidence_path=source_evidence_path,
                                        html_path=current_html,
                                        json_path=current_json,
                                        analysis_dir=current_analysis_dir,
                                        progress_callback=progress_callback,
                                        timeout=self._source_stage_file_agent_timeout(
                                            reference_seconds=max(time.monotonic() - started, 240.0),
                                        ),
                                        bridge_session_id=bridge_session_id,
                                        prior_findings=self._summarize_prior_report_jsons(report_jsons),
                                        context_profile=getattr(source_decision, "context_profile", ""),
                                    )
                        else:
                            custom_result = self._run_custom_skill_agent_analysis(
                                analysis_kind=current_plan.kind,
                                analysis_label=self._analysis_label(current_plan.kind),
                                skill_name=current_skill_name,
                                request_text=request_text,
                                prompt_text=prompt_text,
                                title=title,
                                description=description,
                                fault_time=fault_time,
                                selected_input=selected_input,
                                prepared_input=prepared_input,
                                source_evidence_path=source_evidence_path,
                                html_path=current_html,
                                json_path=current_json,
                                analysis_dir=current_analysis_dir,
                                progress_callback=progress_callback,
                                timeout=self._agent_summary_timeout(
                                    options.timeout_seconds,
                                    reference_seconds=max(time.monotonic() - started, 240.0),
                                ),
                                bridge_session_id=bridge_session_id,
                                prior_findings=self._summarize_prior_report_jsons(report_jsons),
                                context_profile=getattr(source_decision, "context_profile", "") if _kind_spec(current_plan.kind).is_source_stage else "",
                            )
                    skill_file_agent_execution_result = custom_result
                    command = list(custom_result.get("command") or current_command)
                    current_command = command
                    if not custom_result.get("ok"):
                        return self._failure(
                            context=context,
                            command=current_command,
                            started=started,
                            message=str(custom_result.get("message") or "专用 Skill 文件 Agent 执行失败。"),
                            error_code=self._skill_file_agent_mode_error_code(
                                current_plan.kind,
                                str(custom_result.get("error_code") or "custom_skill_agent_failed"),
                                mode="bug_analysis",
                            ),
                            stdout=str(custom_result.get("stdout") or ""),
                            stderr=str(custom_result.get("stderr") or ""),
                            progress_callback=progress_callback,
                            details={
                                "analysis_kind": current_plan.kind,
                                "analysis_kinds": [item.kind for item in plans],
                                "analysis_skill": current_skill_name,
                                "target_time": fault_time,
                                "selected_log_input": str(selected_input or ""),
                                "prepared_log_input": str(prepared_input or ""),
                                "stdout_path": str(custom_result.get("stdout_path") or ""),
                                "stderr_path": str(custom_result.get("stderr_path") or ""),
                                "command_path": str(custom_result.get("command_path") or ""),
                                "context_path": str(custom_result.get("context_path") or ""),
                                "log_focus_manifest_path": str(custom_result.get("log_focus_manifest_path") or ""),
                                "focused_log_input": str(custom_result.get("focused_log_input") or ""),
                                "debug_log_path": str(custom_result.get("debug_log_path") or ""),
                                "analysis_artifact_path": str(custom_result.get("analysis_markdown_path") or ""),
                            },
                        )
                    completed = subprocess.CompletedProcess(
                        args=current_command,
                        returncode=0,
                        stdout=str(custom_result.get("stdout") or ""),
                        stderr=str(custom_result.get("stderr") or ""),
                    )
                else:
                    analysis_kwargs = {
                        "plan": current_plan,
                        "input_path": input_for_plan,
                        "html_path": current_html,
                        "json_path": current_json,
                        "analysis_dir": current_analysis_dir,
                        "timeout": options.timeout_seconds,
                        "target_time": fault_time if _kind_accepts_target_time(current_plan.kind) else None,
                        "request_text": request_text if current_plan.kind == "xtheme" else None,
                    }
                    if bridge_session_id:
                        analysis_kwargs["bridge_session_id"] = bridge_session_id
                    completed = self._run_analysis(**analysis_kwargs)
                if completed.returncode != 0:
                    return self._failure(
                        context=context,
                        command=current_command,
                        started=started,
                        message=f"Bug 分析失败：{self._analysis_label(current_plan.kind)}脚本执行失败。",
                        error_code=f"bug_analysis_{current_plan.kind}_failed",
                        stdout=completed.stdout,
                        stderr=completed.stderr,
                        progress_callback=progress_callback,
                        details={
                            "analysis_kind": current_plan.kind,
                            "analysis_kinds": [item.kind for item in plans],
                            "analysis_skill": selection.skill_name or self._skill_name_for_kind(current_plan.kind),
                            "target_time": fault_time,
                            "selected_log_input": str(selected_input or ""),
                            "prepared_log_input": str(prepared_input or ""),
                        },
                    )
                if not current_html.exists():
                    return self._failure(
                        context=context,
                        command=current_command,
                        started=started,
                        message=f"Bug 分析失败：未生成 {self._analysis_label(current_plan.kind)} HTML 报告。",
                        error_code="bug_analysis_missing_html",
                        stdout=completed.stdout,
                        stderr=completed.stderr,
                        progress_callback=progress_callback,
                        details={
                            "analysis_kind": current_plan.kind,
                            "analysis_kinds": [item.kind for item in plans],
                            "analysis_skill": selection.skill_name or self._skill_name_for_kind(current_plan.kind),
                            "target_time": fault_time,
                            "selected_log_input": str(selected_input or ""),
                            "prepared_log_input": str(prepared_input or ""),
                        },
                    )
                html_paths.append(current_html)
                report_jsons[current_plan.kind] = current_json if current_json.exists() else None
                if current_plan is plan:
                    command = current_command
                    html_path = current_html
                    json_path = current_json
                    analysis_dir = current_analysis_dir

            if source_evidence_future is not None:
                resolved_source_evidence_path = self._resolve_source_evidence_future(source_evidence_future)
                if resolved_source_evidence_path is not None:
                    source_evidence_path = resolved_source_evidence_path
                elif source_evidence_path is not None and not source_evidence_path.exists():
                    source_evidence_path = None

            self._emit_progress(progress_callback, stage="bug_build_outputs", message="整理 bug 分析结果")
            metadata_text, summary = self._build_bug_outputs(
                plans=plans,
                work_item_id=work_item_id,
                fetched=fetched,
                full_item=full_item,
                option_map=option_map,
                request_text=request_text,
                prompt_text=prompt_text,
                selected_input=selected_input,
                report_jsons=report_jsons,
                download=download,
                html_paths=html_paths,
                classification_skill=selection.skill_name,
                classification_source=selection.source,
                classification_reason=selection.reason,
                classification_provider=selection.provider,
                fault_time=fault_time,
                fault_time_note=fault_time_note,
                log_coverage=log_coverage,
            )
            metadata_path.write_text(metadata_text, encoding="utf-8")
            bug_summary_evidence_path = self._write_bug_summary_evidence(
                output_dir=context.output_dir,
                analysis_kind=plans[0].kind if plans else "general",
                report_jsons=report_jsons,
            )
            self._append_bug_summary_evidence_metadata(metadata_path, bug_summary_evidence_path)
            self._append_source_evidence_metadata(metadata_path, source_evidence_path)
            evidence_log_bundle = preserve_evidence_log_bundle(
                output_dir=context.output_dir,
                source_roots=[selected_input, prepared_input, bug_dir],
                reference_files=[metadata_path, *(path for path in report_jsons.values() if path is not None)],
                reference_texts=[summary],
            )
            self._append_evidence_log_metadata(metadata_path, evidence_log_bundle)
            combined_artifacts = self._build_combined_report_artifacts(
                plans=plans,
                prompt_text=prompt_text,
                fault_time=fault_time,
                output_dir=context.output_dir,
                html_paths=html_paths,
                report_jsons=report_jsons,
                selected_input=selected_input,
                source_evidence_path=source_evidence_path,
            )
            agent_summary_path = context.output_dir / "bug_agent_summary.md"
            agent_summary_result = self._run_bug_agent_summary(
                request_text=request_text,
                request_artifact=request_artifact,
                metadata_path=metadata_path,
                output_path=agent_summary_path,
                progress_callback=progress_callback,
                timeout=self._agent_summary_timeout(
                    options.timeout_seconds,
                    reference_seconds=max(
                        time.monotonic() - started,
                        240.0 if any(_kind_spec(i.kind).is_custom_agent for i in plans) else 0.0,
                    ),
                ),
                bridge_session_id=bridge_session_id,
            )
            self._append_agent_runtime_metadata(
                metadata_path,
                agent_summary_result=agent_summary_result,
                total_duration_seconds=time.monotonic() - started,
            )
            annotated_html_paths = list(html_paths)
            if combined_artifacts is not None:
                annotated_html_paths.append(Path(combined_artifacts["html_path"]))
            self._annotate_html_reports(
                annotated_html_paths,
                agent_summary_result=agent_summary_result,
                total_duration_seconds=time.monotonic() - started,
            )
        except subprocess.TimeoutExpired as exc:
            return self._failure(
                context=context,
                command=command,
                started=started,
                message="Bug 分析超时",
                error_code="bug_analysis_timeout",
                stdout=_coerce_process_text(exc.stdout),
                stderr=_coerce_process_text(exc.stderr),
                progress_callback=progress_callback,
            )
        except OSError as exc:
            return self._failure(
                context=context,
                command=command,
                started=started,
                message=f"Bug 分析启动失败: {exc}",
                error_code="bug_analysis_failed_to_start",
                stderr=str(exc),
                progress_callback=progress_callback,
            )
        except (KeyError, ValueError, RuntimeError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
            return self._failure(
                context=context,
                command=command,
                started=started,
                message=f"Bug 分析失败: {exc}",
                error_code="bug_analysis_failed",
                progress_callback=progress_callback,
            )

        details = {
            "mode": "bug_analysis",
            "analysis_kind": plan.kind,
            "analysis_kinds": [item.kind for item in plans],
            "analysis_skill": selection.skill_name,
            "analysis_skill_label": selection.skill_label,
            "domain_kind": getattr(source_decision, "domain_kind", "") or self._domain_kind_from_plans(plans),
            "source_mode": getattr(source_decision, "source_mode", "") or "off",
            "context_profile": getattr(source_decision, "context_profile", ""),
            "stage_kinds": list(getattr(source_decision, "stage_kinds", []) or []),
            "source_targets": list(getattr(source_decision, "targets", []) or []),
            "classification_source": selection.source,
            "classification_reason": selection.reason,
            "classification_provider": selection.provider,
            "signal_code": plan.signal_code,
            "selected_log_input": str(selected_input) if selected_input else "",
            "prepared_log_input": str(prepared_input) if prepared_input else "",
            "fault_time": fault_time,
            "fault_time_source": time_context.source,
            "fault_time_note": fault_time_note,
            "log_coverage_start": log_coverage.start_time if log_coverage else "",
            "log_coverage_end": log_coverage.end_time if log_coverage else "",
            "log_coverage_scanned_files": log_coverage.scanned_files if log_coverage else 0,
            "log_coverage_scanned_lines": log_coverage.scanned_lines if log_coverage else 0,
            "bug_dir": str(bug_dir),
            "bug_cache_dir": str(bug_dir),
            "bug_cache_reused": cache_reused,
            "bug_url": request.bug_url,
            "user_request_text": request_text,
            "agent_request_file": str(request_artifact),
            "agent_summary_file": str(agent_summary_path),
        }
        if skill_file_agent_execution_result is not None:
            details.update(
                self._skill_file_agent_execution_details(
                    str(skill_file_agent_execution_result.get("analysis_kind") or ""),
                    skill_file_agent_execution_result,
                )
            )
        if source_evidence_path is not None:
            details["source_evidence_file"] = str(source_evidence_path)
        if evidence_log_bundle is not None:
            details["evidence_log_bundle"] = str(evidence_log_bundle.get("bundle_dir") or "")
            details["evidence_log_manifest"] = str(evidence_log_bundle.get("manifest_path") or "")
            details["evidence_log_focus_logs"] = evidence_log_bundle.get("focus_logs") or []
            details["evidence_log_file_count"] = evidence_log_bundle.get("file_count") or 0
        final_message = summary
        if agent_summary_result["message"]:
            final_message = str(agent_summary_result["message"])
        self._apply_agent_runtime_details(details, agent_summary_result)
        files_to_send = [metadata_path]
        if combined_artifacts is not None:
            details["combined_report_html"] = str(combined_artifacts["html_path"])
            details["combined_report_json"] = str(combined_artifacts["json_path"])
            files_to_send = [Path(combined_artifacts["html_path"])]
        else:
            files_to_send.extend(html_paths)
        if options.upload_result_files:
            details["files_to_send"] = files_to_send
        if json_path.exists():
            details["json_report"] = str(json_path)
        self._emit_progress(
            progress_callback,
            stage="bug_completed",
            message="bug 分析完成",
            job_id=context.job_id,
            analysis_kinds=[item.kind for item in plans],
            html_reports=[str(path) for path in html_paths],
        )
        return TaskResult(
            success=True,
            message=final_message,
            job_id=context.job_id,
            job_dir=context.job_dir,
            command=command,
            duration_seconds=time.monotonic() - started,
            details=details,
        )

    def run_bug_reanalysis(
        self,
        *,
        followup_text: str,
        previous_context: object,
        previous_session: dict[str, object],
        event: LarkEvent | None = None,
        progress_callback: Callable[[dict[str, object]], None] | None = None,
        force_rerun: bool = False,
        plans_override: list["BugAnalysisPlan"] | None = None,
        classification_skill: str = "",
        classification_source: str = "",
        classification_reason: str = "",
        classification_provider: str = "",
        agent_provider_override: str = "",
        local_log_resources: list[DownloadResource] | None = None,
        bridge_session_id: str = "",
    ) -> TaskResult:
        started = time.monotonic()
        bridge_session_id = bridge_session_id.strip() or self._bridge_session_id(event)
        bridge_kwargs = {"bridge_session_id": bridge_session_id} if bridge_session_id else {}
        details = previous_session.get("details", {})
        if not isinstance(details, dict):
            details = {}
        job_id = str(previous_session.get("job_id") or "").strip()
        job_dir_value = str(previous_session.get("job_dir") or "").strip()
        if not job_id and job_dir_value:
            job_id = Path(job_dir_value).name
        if not job_id:
            return TaskResult(
                success=False,
                message="无法复用上次 Bug 分析：未找到上一轮 job_id。",
                error_code="bug_reanalysis_missing_job",
                details={"mode": "bug_reanalysis"},
            )
        job_dir = Path(job_dir_value) if job_dir_value else self.config.data_dir / "jobs" / job_id
        result_context = type("Context", (), {"job_id": job_id, "job_dir": job_dir})()
        output_dir = job_dir / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        download_retry_result: dict[str, object] | None = None
        request_text = self._original_bug_request_text(previous_context, details)
        local_log_resources = local_log_resources or []
        local_selected_input: Path | None = None
        local_prepared_input: Path | None = None
        if local_log_resources:
            for resource in local_log_resources:
                if resource.kind != "local":
                    continue
                candidate = Path(resource.value).expanduser()
                if not candidate.exists():
                    continue
                local_selected_input = candidate.resolve()
                try:
                    local_prepared_input = self._prepare_log_input(local_selected_input)
                except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                    return TaskResult(
                        success=False,
                        message=f"本地日志准备失败：{exc}",
                        job_id=job_id,
                        job_dir=job_dir,
                        duration_seconds=time.monotonic() - started,
                        error_code="bug_reanalysis_local_log_prepare_failed",
                        details={
                            "mode": "bug_reanalysis",
                            "local_log_resources": [item.value for item in local_log_resources],
                        },
                    )
                self._emit_progress(
                    progress_callback,
                    stage="bug_reanalysis_local_log_selected",
                    message="使用卡片输入中授权的本地日志文件",
                    selected_log_input=str(local_selected_input),
                    prepared_log_input=str(local_prepared_input),
                )
                break
        reference_text = "\n".join(
            [
                request_text,
                f"故障时间: {str(details.get('target_time') or details.get('fault_time') or '').strip()}",
            ]
        ).strip()
        concise_reference_text = self._trim_reanalysis_reference_text(reference_text)
        # Recover bug title from metadata so follow-ups like
        # "请根据BUG问题实际时间继续分析" can extract the correct time.
        recovered_bug_title = self._recover_bug_title_from_metadata(output_dir)
        time_context = self._resolve_bug_time_context(
            request_text=followup_text,
            title=recovered_bug_title,
            description=reference_text,
            reference_time=str(details.get("target_time") or details.get("fault_time") or "").strip(),
        )
        target_time = time_context.fault_time
        prepared_input = local_prepared_input or self._path_from_details(details, "prepared_log_input")
        selected_input = local_selected_input or self._path_from_details(details, "selected_log_input") or prepared_input
        explicit_source_followup = self._followup_explicitly_requests_source_analysis(
            request_text,
            followup_text,
        )
        reanalysis_selection: BugAnalysisSelection | None = None
        if plans_override is None and explicit_source_followup:
            candidate_selection = self._manual_followup_selection_if_explicit(
                followup_text=followup_text,
                request_text=request_text,
            )
            if candidate_selection is not None and any(plan.kind != "general" for plan in candidate_selection.plans):
                reanalysis_selection = candidate_selection
                if not classification_skill:
                    classification_skill = candidate_selection.skill_name
                if not classification_source:
                    classification_source = candidate_selection.source
                if not classification_reason:
                    classification_reason = "源码追问按原始 bug 请求重建领域上下文。"
                if not classification_provider:
                    classification_provider = candidate_selection.provider
        plans = plans_override or (
            [BugAnalysisPlan(kind=plan.kind, signal_code=plan.signal_code) for plan in reanalysis_selection.plans]
            if reanalysis_selection is not None
            else self._plans_for_reanalysis(details, request_text=request_text, followup_text=followup_text)
        )
        source_decision_skill_name = classification_skill or str(details.get("analysis_skill") or "").strip()
        if (
            plans_override is None
            and all(_kind_spec(plan.kind).is_source_stage for plan in plans)
            and not explicit_source_followup
        ):
            fallback_plans = self._fallback_non_source_plans_from_job_output(output_dir)
            if fallback_plans:
                plans = fallback_plans
                if source_decision_skill_name == "source_analysis":
                    source_decision_skill_name = ""
        source_decision = self._decide_source_analysis_request(
            request_text=request_text,
            prompt_text=followup_text,
            title="",
            description=concise_reference_text,
            plans=plans,
            skill_name=source_decision_skill_name,
        )
        plans = self._augment_plans_for_source_analysis(plans, source_decision=source_decision)
        if source_decision.requested and not classification_skill and all(_kind_spec(plan.kind).is_source_stage for plan in plans):
            classification_skill = "source_analysis"
        requires_log_input = any(self._plan_requires_log_input(plan) for plan in plans)
        if requires_log_input and not time_context.has_full_datetime:
            return self._bug_time_clarification_result(
                context=result_context,
                started=started,
                request_text=f"{request_text}\n追问/修正：{followup_text}".strip(),
                bug_url=str(details.get("bug_url") or ""),
                time_context=time_context,
                status="missing_fault_time",
                progress_callback=progress_callback,
            )
        if requires_log_input and prepared_input is None:
            recovered_selected, recovered_prepared = self._recover_cached_bug_log_input(details, request_text=request_text)
            if recovered_prepared is not None:
                selected_input = recovered_selected or recovered_prepared
                prepared_input = recovered_prepared
                self._emit_progress(
                    progress_callback,
                    stage="bug_reanalysis_recover_bug_cache",
                    message="从同一 bug cache 恢复已下载日志输入",
                    selected_log_input=str(selected_input or ""),
                    prepared_log_input=str(prepared_input),
                )
        if requires_log_input and prepared_input is None:
            retry = self._retry_bug_log_download(
                previous_session=previous_session,
                job_dir=job_dir,
                progress_callback=progress_callback,
                **bridge_kwargs,
            )
            download_retry_result = retry
            if retry.get("prepared_input") is not None:
                prepared_input = retry["prepared_input"]
                selected_input = retry.get("selected_input") or prepared_input
            else:
                failure_message = str(retry.get("message") or "无法复用上次 Bug 分析：未找到已准备好的日志输入，且重新下载日志失败。")
                failure_details = {
                    "mode": "bug_reanalysis",
                    "download_retry": retry,
                }
                return TaskResult(
                    success=False,
                    message=failure_message,
                    job_id=job_id,
                    job_dir=job_dir,
                    duration_seconds=time.monotonic() - started,
                    error_code="bug_reanalysis_missing_prepared_input",
                    details=failure_details,
                )

        log_coverage: LogCoverage | None = None
        if requires_log_input:
            log_coverage = self._scan_log_time_coverage(prepared_input, fault_time=target_time) if prepared_input else None
            if log_coverage is None or not log_coverage.has_time_evidence:
                return self._bug_time_clarification_result(
                    context=result_context,
                    started=started,
                    request_text=f"{request_text}\n追问/修正：{followup_text}".strip(),
                    bug_url=str(details.get("bug_url") or ""),
                    time_context=time_context,
                    status="log_time_unknown",
                    progress_callback=progress_callback,
                    log_coverage=log_coverage,
                )
            if not log_coverage.covers_fault_time:
                return self._bug_time_clarification_result(
                    context=result_context,
                    started=started,
                    request_text=f"{request_text}\n追问/修正：{followup_text}".strip(),
                    bug_url=str(details.get("bug_url") or ""),
                    time_context=time_context,
                    status="log_not_covering_fault_time",
                    progress_callback=progress_callback,
                    log_coverage=log_coverage,
                )

        force_rerun_kinds = self._forced_reanalysis_kinds(
            plans,
            request_text=request_text,
            followup_text=followup_text,
            force_rerun=force_rerun,
        )
        prompt_text = f"{request_text}\n追问/修正：{followup_text}".strip()
        source_stage_prompt_text = self._source_stage_followup_prompt_text(
            request_text=request_text,
            followup_text=followup_text,
        )
        source_stage_request_text = self._source_stage_followup_request_text(
            request_text=request_text,
            followup_text=followup_text,
        )
        source_evidence_path = self._write_reanalysis_source_evidence(
            plans=plans,
            request_text=request_text,
            followup_text=followup_text,
            output_dir=output_dir,
            enabled=any(_kind_spec(p.kind).needs_source_evidence for p in plans)
            or bool(force_rerun_kinds.intersection({"signal"}))
            or any(_kind_spec(plan.kind).is_source_stage for plan in plans)
            or self._should_collect_source_evidence(request_text, followup_text),
        )
        self._emit_progress(
            progress_callback,
            stage="bug_reanalysis_reuse_context",
            message="复用上一轮 bug 分析上下文，不重新拉取/下载/解密日志",
            job_id=job_id,
            prepared_log_input=str(prepared_input or ""),
            selected_log_input=str(selected_input or ""),
            target_time=target_time,
            analysis_kinds=[plan.kind for plan in plans],
        )

        html_paths: list[Path] = []
        report_jsons: dict[str, Path | None] = {}
        rerun_kinds: list[str] = []
        reused_kinds: list[str] = []
        command: list[str] | None = None
        skill_file_agent_execution_result: dict[str, object] | None = None
        try:
            for plan in plans:
                html_path = output_dir / self._report_name(plan.kind, "html")
                json_path = output_dir / self._report_name(plan.kind, "json")
                analysis_dir = output_dir / f"{plan.kind}_analysis"
                should_rerun = (
                    plan.kind == "startup"
                    or plan.kind in force_rerun_kinds
                    or not html_path.exists()
                    or not json_path.exists()
                )
                if not should_rerun:
                    self._emit_progress(
                        progress_callback,
                        stage="bug_reanalysis_reuse_report",
                        message=f"复用已生成的{self._analysis_label(plan.kind)}报告",
                        plan=plan.kind,
                        html_path=str(html_path),
                        json_path=str(json_path),
                    )
                    reused_kinds.append(plan.kind)
                    html_paths.append(html_path)
                    report_jsons[plan.kind] = json_path if json_path.exists() else None
                    continue

                input_for_plan = prepared_input
                if plan.kind == "startup":
                    input_for_plan = self._startup_analysis_input(prepared_input, target_time)
                rerun_kinds.append(plan.kind)
                command = self.build_command(
                    plan=plan,
                    input_path=input_for_plan or output_dir,
                    html_path=html_path,
                    json_path=json_path,
                    analysis_dir=analysis_dir,
                    target_time=target_time if _kind_accepts_target_time(plan.kind) else None,
                    request_text=followup_text if plan.kind == "xtheme" else None,
                )
                self._emit_progress(
                    progress_callback,
                    stage="bug_reanalysis_run_analysis",
                    message=(
                        f"基于已准备日志重新执行{self._analysis_label(plan.kind)}"
                        if input_for_plan is not None
                        else f"基于已有上下文重新执行{self._analysis_label(plan.kind)}"
                    ),
                    plan=plan.kind,
                    plan_label=self._analysis_label(plan.kind),
                    html_path=str(html_path),
                    json_path=str(json_path),
                    target_time=target_time if _kind_accepts_target_time(plan.kind) else "",
                )
                if plan.kind == "general":
                    self._write_general_bug_report(
                        html_path=html_path,
                        json_path=json_path,
                        title="",
                        description="",
                        prompt_text=followup_text,
                        request_text=request_text,
                        fault_time=target_time,
                        selected_input=selected_input,
                        source_evidence_path=source_evidence_path,
                        classification_skill=classification_skill or self._skill_name_for_kind(plan.kind),
                        classification_source=classification_source or "manual_fallback",
                        classification_reason=classification_reason or "",
                    )
                    completed = subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")
                elif _kind_spec(plan.kind).is_custom_agent:
                    if plan.kind == "ld_lane_level":
                        skill_name = self._skill_name_for_kind(plan.kind)
                    elif _kind_spec(plan.kind).is_source_stage:
                        if getattr(source_decision, "source_mode", "") == "append" and not getattr(source_decision, "context_profile", ""):
                            return TaskResult(
                                success=False,
                                message="Bug 续聊重分析失败：源码阶段缺少领域上下文，已停止以避免泛化扫描。",
                                job_id=job_id,
                                job_dir=job_dir,
                                command=command,
                                duration_seconds=time.monotonic() - started,
                                error_code="source_stage_missing_context",
                                details={
                                    "mode": "bug_reanalysis",
                                    "analysis_kind": plan.kind,
                                    "analysis_kinds": [item.kind for item in plans],
                                    "source_mode": getattr(source_decision, "source_mode", ""),
                                    "context_profile": getattr(source_decision, "context_profile", ""),
                                    "stage_kinds": list(getattr(source_decision, "stage_kinds", []) or []),
                                },
                            )
                        requested_skill = (
                            classification_skill
                            or str(details.get("analysis_skill") or "").strip()
                            or self._skill_name_for_kind(plan.kind)
                        )
                        skill_name = self._resolve_source_stage_skill_name(requested_skill)
                    else:
                        skill_name = (
                            classification_skill
                            or str(details.get("analysis_skill") or "").strip()
                            or self._skill_name_for_kind(plan.kind)
                        )
                    if _kind_spec(plan.kind).needs_custom_executor_check and skill_name != "source_analysis" and self.skill_manager.custom_skill_executor_for(skill_name) not in {"file_agent", "pydantic_ai"}:
                        prefix = plan.kind if plan.kind in _SOURCE_SKILL_KINDS else SOURCE_STAGE_KIND
                        return TaskResult(
                            success=False,
                            message=self._custom_skill_executor_not_ready_message(
                                skill_name,
                                selected_input=selected_input,
                            ),
                            job_id=job_id,
                            job_dir=job_dir,
                            command=command,
                            duration_seconds=time.monotonic() - started,
                            error_code=f"{prefix}_reanalysis_executor_not_ready",
                            details={
                                "mode": "bug_reanalysis",
                                "analysis_kind": plan.kind,
                                "analysis_skill": skill_name,
                                f"{prefix}_analysis_status": "executor_not_ready",
                                "target_time": target_time,
                                "selected_log_input": str(selected_input or ""),
                                "prepared_log_input": str(prepared_input or ""),
                            },
                        )
                    # LD lane-level reanalysis: try pydantic-ai first
                    if plan.kind == "ld_lane_level":
                        custom_result = self._run_ld_pydantic_ai_analysis(
                            skill_name=skill_name,
                            request_text=request_text,
                            prompt_text=prompt_text,
                            title="",
                            description=reference_text,
                            fault_time=target_time,
                            selected_input=selected_input,
                            prepared_input=prepared_input,
                            html_path=html_path,
                            json_path=json_path,
                            analysis_dir=analysis_dir,
                            progress_callback=progress_callback,
                            prior_findings=self._summarize_prior_report_jsons(report_jsons),
                        )
                        if custom_result.get("ok"):
                            logger.info("LD reanalysis pydantic-ai succeeded, skipping file_agent")
                        else:
                            logger.warning(
                                "LD reanalysis pydantic-ai failed (error=%s), falling back to file_agent",
                                custom_result.get("error_code", "unknown"),
                            )
                            custom_result = self._run_custom_skill_agent_analysis(
                                analysis_kind=plan.kind,
                                analysis_label=self._analysis_label(plan.kind),
                                skill_name=skill_name,
                                request_text=request_text,
                                prompt_text=prompt_text,
                                title="",
                                description=concise_reference_text,
                                fault_time=target_time,
                                selected_input=selected_input,
                                prepared_input=prepared_input,
                                source_evidence_path=source_evidence_path,
                                html_path=html_path,
                                json_path=json_path,
                                analysis_dir=analysis_dir,
                                progress_callback=progress_callback,
                                timeout=self._agent_summary_timeout(
                                    self.config.bug_analysis.timeout_seconds,
                                    reference_seconds=max(
                                        self._agent_summary_timeout_reference(previous_session) or 0.0,
                                        time.monotonic() - started,
                                        240.0,
                                    ),
                                ),
                                bridge_session_id=bridge_session_id,
                                prior_findings=self._summarize_prior_report_jsons(report_jsons),
                            )
                    else:
                        # Source stage reanalysis: prefer app-server-backed file-agent when enabled,
                        # while leaving the later summary stage on the configured AI path.
                        if _kind_spec(plan.kind).is_source_stage:
                            if self._prefer_source_stage_file_agent():
                                self._emit_progress(
                                    progress_callback,
                                    stage="source_stage_app_server_preferred",
                                    message="已启用 codex app-server，源码阶段直接走 file-agent 路径",
                                )
                                execution_skill_name = (
                                    getattr(source_decision, "context_profile", "").strip()
                                    or skill_name
                                )
                                custom_result = self._run_custom_skill_agent_analysis(
                                   analysis_kind=plan.kind,
                                   analysis_label=self._analysis_label(plan.kind),
                                   skill_name=execution_skill_name,
                                   request_text=source_stage_request_text,
                                   prompt_text=source_stage_prompt_text,
                                   title="",
                                   description=concise_reference_text,
                                   fault_time=target_time,
                                   selected_input=selected_input,
                                   prepared_input=prepared_input,
                                   source_evidence_path=source_evidence_path,
                                   html_path=html_path,
                                   json_path=json_path,
                                   analysis_dir=analysis_dir,
                                   progress_callback=progress_callback,
                                   timeout=self._source_stage_file_agent_timeout(
                                       reference_seconds=max(
                                           self._agent_summary_timeout_reference(previous_session) or 0.0,
                                           time.monotonic() - started,
                                           240.0,
                                       ),
                                   ),
                                   bridge_session_id=bridge_session_id,
                                   prior_findings=self._summarize_prior_report_jsons(report_jsons),
                                   context_profile=execution_skill_name,
                                   provider_override="codex",
                                   command_override=self.config.codex_app_server.command,
                                )
                            else:
                                custom_result = self._run_source_stage_pydantic_ai(
                                    analysis_kind=plan.kind,
                                    skill_name=skill_name,
                                    request_text=source_stage_request_text,
                                    prompt_text=source_stage_prompt_text,
                                    title="",
                                    description=concise_reference_text,
                                    fault_time=target_time,
                                    selected_input=selected_input,
                                    prepared_input=prepared_input,
                                    source_evidence_path=source_evidence_path,
                                    html_path=html_path,
                                    json_path=json_path,
                                    analysis_dir=analysis_dir,
                                    progress_callback=progress_callback,
                                    context_profile=getattr(source_decision, "context_profile", ""),
                                    prior_findings=self._summarize_prior_report_jsons(report_jsons),
                                )
                                if custom_result.get("ok"):
                                    logger.info("source_stage reanalysis pydantic-ai succeeded, skipping file_agent")
                                else:
                                    logger.warning(
                                       "source_stage reanalysis pydantic-ai failed (error=%s), falling back to file_agent",
                                       custom_result.get("error_code", "unknown"),
                                    )
                                    self._emit_progress(
                                       progress_callback,
                                       stage="source_stage_pydantic_ai_fallback",
                                       message=f"pydantic-ai 失败（{custom_result.get('error_code')}），切换 file_agent",
                                    )
                                    custom_result = self._run_custom_skill_agent_analysis(
                                       analysis_kind=plan.kind,
                                       analysis_label=self._analysis_label(plan.kind),
                                       skill_name=skill_name,
                                       request_text=source_stage_request_text,
                                       prompt_text=source_stage_prompt_text,
                                       title="",
                                       description=concise_reference_text,
                                       fault_time=target_time,
                                       selected_input=selected_input,
                                       prepared_input=prepared_input,
                                       source_evidence_path=source_evidence_path,
                                       html_path=html_path,
                                       json_path=json_path,
                                       analysis_dir=analysis_dir,
                                       progress_callback=progress_callback,
                                       timeout=self._source_stage_file_agent_timeout(
                                           reference_seconds=max(
                                               self._agent_summary_timeout_reference(previous_session) or 0.0,
                                               time.monotonic() - started,
                                               240.0,
                                           ),
                                       ),
                                       bridge_session_id=bridge_session_id,
                                       prior_findings=self._summarize_prior_report_jsons(report_jsons),
                                       context_profile=getattr(source_decision, "context_profile", ""),
                                    )
                        else:
                            custom_result = self._run_custom_skill_agent_analysis(
                                analysis_kind=plan.kind,
                                analysis_label=self._analysis_label(plan.kind),
                                skill_name=skill_name,
                                request_text=request_text,
                                        prompt_text=source_stage_prompt_text,
                                title="",
                                description=concise_reference_text,
                                fault_time=target_time,
                                selected_input=selected_input,
                                prepared_input=prepared_input,
                                source_evidence_path=source_evidence_path,
                                html_path=html_path,
                                json_path=json_path,
                                analysis_dir=analysis_dir,
                                progress_callback=progress_callback,
                                timeout=self._agent_summary_timeout(
                                   self.config.bug_analysis.timeout_seconds,
                                   reference_seconds=max(
                                       self._agent_summary_timeout_reference(previous_session) or 0.0,
                                       time.monotonic() - started,
                                       240.0,
                                   ),
                                ),
                                bridge_session_id=bridge_session_id,
                                prior_findings=self._summarize_prior_report_jsons(report_jsons),
                                context_profile=getattr(source_decision, "context_profile", "") if _kind_spec(plan.kind).is_source_stage else "",
                            )
                    skill_file_agent_execution_result = custom_result
                    command = list(custom_result.get("command") or command or [])
                    if not custom_result.get("ok"):
                        partial_markdown = str(custom_result.get("partial_markdown") or "").strip()
                        if partial_markdown and _kind_spec(plan.kind).is_source_stage:
                            self._emit_progress(
                                progress_callback,
                                stage="source_stage_partial_timeout",
                                message="源码阶段超时，但已提取到阶段性结论，直接降级回复",
                            )
                            partial_details = {
                                "mode": "bug_reanalysis",
                                "analysis_kind": plan.kind,
                                "analysis_kinds": [plan.kind],
                                "analysis_skill": skill_name,
                                "source_stage_analysis_status": "partial_timeout",
                                "agent_summary_execution_backend": "source_stage_partial",
                                "target_time": target_time,
                                "selected_log_input": str(selected_input or ""),
                                "prepared_log_input": str(prepared_input or ""),
                                "stdout_path": str(custom_result.get("stdout_path") or ""),
                                "stderr_path": str(custom_result.get("stderr_path") or ""),
                                "command_path": str(custom_result.get("command_path") or ""),
                                "context_path": str(custom_result.get("context_path") or ""),
                                "log_focus_manifest_path": str(custom_result.get("log_focus_manifest_path") or ""),
                                "focused_log_input": str(custom_result.get("focused_log_input") or ""),
                                "debug_log_path": str(custom_result.get("debug_log_path") or ""),
                                "analysis_artifact_path": str(custom_result.get("analysis_markdown_path") or ""),
                                "partial_analysis_available": True,
                            }
                            return TaskResult(
                                success=True,
                                message=partial_markdown,
                                job_id=job_id,
                                job_dir=job_dir,
                                command=command,
                                duration_seconds=time.monotonic() - started,
                                details=partial_details,
                                stdout=str(custom_result.get("stdout") or ""),
                                stderr=str(custom_result.get("stderr") or ""),
                            )
                        return TaskResult(
                            success=False,
                            message=str(custom_result.get("message") or "Bug 续聊专用 Skill 文件 Agent 执行失败。"),
                            job_id=job_id,
                            job_dir=job_dir,
                            command=command,
                            duration_seconds=time.monotonic() - started,
                            error_code=self._skill_file_agent_mode_error_code(
                                plan.kind,
                                str(custom_result.get("error_code") or "custom_skill_agent_failed"),
                                mode="bug_reanalysis",
                            ),
                            stdout=str(custom_result.get("stdout") or ""),
                            stderr=str(custom_result.get("stderr") or ""),
                            details={
                                "mode": "bug_reanalysis",
                                "analysis_kind": plan.kind,
                                "analysis_skill": skill_name,
                                f"{'custom_skill' if plan.kind == 'custom_skill' else plan.kind}_analysis_status": "failed",
                                "target_time": target_time,
                                "selected_log_input": str(selected_input or ""),
                                "prepared_log_input": str(prepared_input or ""),
                                "stdout_path": str(custom_result.get("stdout_path") or ""),
                                "stderr_path": str(custom_result.get("stderr_path") or ""),
                                "command_path": str(custom_result.get("command_path") or ""),
                                "context_path": str(custom_result.get("context_path") or ""),
                                "log_focus_manifest_path": str(custom_result.get("log_focus_manifest_path") or ""),
                                "focused_log_input": str(custom_result.get("focused_log_input") or ""),
                                "debug_log_path": str(custom_result.get("debug_log_path") or ""),
                                "analysis_artifact_path": str(custom_result.get("analysis_markdown_path") or ""),
                            },
                        )
                    completed = subprocess.CompletedProcess(
                        args=command,
                        returncode=0,
                        stdout=str(custom_result.get("stdout") or ""),
                        stderr=str(custom_result.get("stderr") or ""),
                    )
                else:
                    analysis_kwargs = {
                        "plan": plan,
                        "input_path": input_for_plan,
                        "html_path": html_path,
                        "json_path": json_path,
                        "analysis_dir": analysis_dir,
                        "timeout": self.config.bug_analysis.timeout_seconds,
                        "target_time": target_time if _kind_accepts_target_time(plan.kind) else None,
                        "request_text": followup_text if plan.kind == "xtheme" else None,
                    }
                    if bridge_session_id:
                        analysis_kwargs["bridge_session_id"] = bridge_session_id
                    completed = self._run_analysis(**analysis_kwargs)
                if completed.returncode != 0:
                    return TaskResult(
                        success=False,
                        message=f"Bug 续聊重分析失败：{self._analysis_label(plan.kind)}脚本执行失败。",
                        job_id=job_id,
                        job_dir=job_dir,
                        command=command,
                        duration_seconds=time.monotonic() - started,
                        error_code=f"bug_reanalysis_{plan.kind}_failed",
                        stdout=completed.stdout,
                        stderr=completed.stderr,
                        details={"mode": "bug_reanalysis"},
                    )
                html_paths.append(html_path)
                report_jsons[plan.kind] = json_path if json_path.exists() else None
        except subprocess.TimeoutExpired as exc:
            return TaskResult(
                success=False,
                message="Bug 续聊重分析超时",
                job_id=job_id,
                job_dir=job_dir,
                command=command,
                duration_seconds=time.monotonic() - started,
                error_code="bug_reanalysis_timeout",
                stdout=_coerce_process_text(exc.stdout),
                stderr=_coerce_process_text(exc.stderr),
                details={"mode": "bug_reanalysis"},
            )

        combined_artifacts = self._build_combined_report_artifacts(
            plans=plans,
            prompt_text=prompt_text,
            fault_time=target_time,
            output_dir=output_dir,
            html_paths=html_paths,
            report_jsons=report_jsons,
            selected_input=selected_input,
            source_evidence_path=source_evidence_path,
        )
        agent_request_path = output_dir / "bug_agent_reanalysis_request.md"
        agent_request_path.write_text(
            self._render_bug_reanalysis_request(
                request_text=request_text,
                followup_text=followup_text,
                target_time=target_time,
                plans=plans,
                history=None,
            ),
            encoding="utf-8",
        )
        agent_metadata_path = output_dir / "bug_reanalysis_metadata.md"
        previous_summary_path = None
        bug_summary_evidence_path = self._write_bug_summary_evidence(
            output_dir=output_dir,
            analysis_kind=plans[0].kind if plans else "general",
            report_jsons=report_jsons,
        )
        agent_metadata_path.write_text(
            self._render_bug_reanalysis_metadata(
                request_text=request_text,
                followup_text=followup_text,
                job_id=job_id,
                target_time=target_time,
                prepared_input=prepared_input,
                selected_input=selected_input,
                plans=plans,
                rerun_kinds=rerun_kinds,
                reused_kinds=reused_kinds,
                html_paths=html_paths,
                report_jsons=report_jsons,
                combined_artifacts=combined_artifacts,
                previous_summary_path=previous_summary_path,
                source_evidence_path=source_evidence_path,
                classification_skill=classification_skill or self._skill_name_for_kind(plans[0].kind if plans else "general"),
                classification_source=classification_source or "manual_fallback",
                classification_reason=classification_reason or "",
                classification_provider=classification_provider or "",
            ),
            encoding="utf-8",
        )
        self._append_bug_summary_evidence_metadata(agent_metadata_path, bug_summary_evidence_path)
        agent_summary_path = output_dir / "bug_agent_summary.md"
        snapshot_details = self._structured_bug_prompt_snapshot_details(
            base_details=details,
            request_text=request_text,
            plans=plans,
            target_time=target_time,
            prepared_input=prepared_input,
            selected_input=selected_input,
        )
        use_source_stage_direct_reply = (
            explicit_source_followup
            and skill_file_agent_execution_result is not None
            and bool(skill_file_agent_execution_result.get("ok"))
            and str(skill_file_agent_execution_result.get("executor") or "") == "codex_app_server"
            and str(skill_file_agent_execution_result.get("completion_state") or "") == "complete"
            and any(_kind_spec(plan.kind).is_source_stage for plan in plans)
        )
        if use_source_stage_direct_reply:
            analysis_path = skill_file_agent_execution_result.get("analysis_markdown_path")
            analysis_message = ""
            if isinstance(analysis_path, Path) and analysis_path.exists():
                analysis_message = analysis_path.read_text(encoding="utf-8", errors="replace").strip()
            agent_summary_result = {
                "message": analysis_message,
                "command": None,
                "error": "",
                "provider": "codex",
                "session_id": "",
                "resumed": False,
                "duration_seconds": 0.0,
                "usage": skill_file_agent_execution_result.get("usage") or {},
                "usage_scope": "source_stage_direct",
                "execution_backend": "source_stage_direct",
                "backend_reason": "explicit_source_stage_output",
            }
            self._emit_progress(
                progress_callback,
                stage="bug_agent_summary_completed",
                message="源码阶段已产出完整 Markdown，直接作为最终回答",
                provider="codex",
            )
        else:
            agent_summary_result = self._run_bug_agent_summary(
                request_text=request_text,
                request_artifact=agent_request_path,
                metadata_path=agent_metadata_path,
                output_path=agent_summary_path,
                progress_callback=progress_callback,
                timeout=self._agent_summary_timeout(
                    self.config.bug_analysis.timeout_seconds,
                    reference_seconds=max(
                        self._agent_summary_timeout_reference(previous_session) or 0.0,
                        time.monotonic() - started,
                    ),
                ),
                provider_session_id="",
                followup_text=followup_text,
                previous_summary_path=previous_summary_path,
                snapshot_details=snapshot_details,
                snapshot_plans=plans,
                prefer_lightweight=self._should_prefer_lightweight_bug_summary(
                    request_text=request_text,
                    followup_text=followup_text,
                    provider_session_id="",
                ),
                provider_override=agent_provider_override,
                bridge_session_id=bridge_session_id,
            )
        self._append_agent_runtime_metadata(
            agent_metadata_path,
            agent_summary_result=agent_summary_result,
            total_duration_seconds=time.monotonic() - started,
        )
        annotated_html_paths = list(html_paths)
        if combined_artifacts is not None:
            annotated_html_paths.append(Path(combined_artifacts["html_path"]))
        self._annotate_html_reports(
            annotated_html_paths,
            agent_summary_result=agent_summary_result,
            total_duration_seconds=time.monotonic() - started,
        )
        if combined_artifacts is not None:
            final_message = str(combined_artifacts["summary"])
            files_to_send = [Path(combined_artifacts["html_path"])]
        else:
            final_message = self._build_direct_analysis_summary(plans, prompt_text, html_paths)
            files_to_send = html_paths
        if agent_summary_result["message"]:
            final_message = str(agent_summary_result["message"])
        self._emit_progress(
            progress_callback,
            stage="bug_reanalysis_completed",
            message="bug 续聊重分析完成",
            job_id=job_id,
            target_time=target_time,
            analysis_kinds=[plan.kind for plan in plans],
            html_reports=[str(path) for path in html_paths],
        )
        result_details = {
            "mode": "bug_reanalysis",
            "analysis_kind": plans[0].kind if plans else "",
            "analysis_kinds": [plan.kind for plan in plans],
            "analysis_skill": classification_skill or self._skill_name_for_kind(plans[0].kind if plans else "general"),
            "analysis_skill_label": self._analysis_label(plans[0].kind) if plans else "通用问题分析",
            "domain_kind": getattr(source_decision, "domain_kind", "") or self._domain_kind_from_plans(plans),
            "source_mode": getattr(source_decision, "source_mode", "") or "off",
            "context_profile": getattr(source_decision, "context_profile", ""),
            "stage_kinds": list(getattr(source_decision, "stage_kinds", []) or []),
            "source_targets": list(getattr(source_decision, "targets", []) or []),
            "classification_source": classification_source or "manual_fallback",
            "classification_reason": classification_reason or "",
            "classification_provider": classification_provider or "",
            "selected_agent_provider": agent_provider_override,
            "selected_log_input": str(selected_input or ""),
            "prepared_log_input": str(prepared_input),
            "local_log_resources": [item.value for item in local_log_resources],
            "user_request_text": request_text,
            "followup_text": followup_text,
            "target_time": target_time,
            "log_coverage_start": log_coverage.start_time if log_coverage else "",
            "log_coverage_end": log_coverage.end_time if log_coverage else "",
            "log_coverage_scanned_files": log_coverage.scanned_files if log_coverage else 0,
            "log_coverage_scanned_lines": log_coverage.scanned_lines if log_coverage else 0,
            "rerun_analysis_kinds": rerun_kinds,
            "reused_analysis_kinds": reused_kinds,
            "agent_request_file": str(agent_request_path),
            "reanalysis_metadata_file": str(agent_metadata_path),
            "agent_summary_file": str(agent_summary_path),
            "files_to_send": files_to_send,
        }
        signal_codes = [plan.signal_code for plan in plans if plan.kind == "signal" and plan.signal_code]
        if signal_codes:
            result_details["signal_code"] = signal_codes[0]
        if source_evidence_path is not None:
            result_details["source_evidence_file"] = str(source_evidence_path)
        if download_retry_result is not None:
            result_details["download_retry"] = download_retry_result
        if combined_artifacts is not None:
            result_details["combined_report_html"] = str(combined_artifacts["html_path"])
            result_details["combined_report_json"] = str(combined_artifacts["json_path"])
        if skill_file_agent_execution_result is not None:
            result_details.update(
                self._skill_file_agent_execution_details(
                    str(skill_file_agent_execution_result.get("analysis_kind") or ""),
                    skill_file_agent_execution_result,
                )
            )
        self._apply_agent_runtime_details(result_details, agent_summary_result)
        return TaskResult(
            success=True,
            message=final_message,
            job_id=job_id,
            job_dir=job_dir,
            command=command,
            duration_seconds=time.monotonic() - started,
            details=result_details,
        )

    def run_bug_agent_followup(
        self,
        *,
        followup_text: str,
        previous_context: object,
        previous_session: dict[str, object],
        event: LarkEvent | None = None,
        progress_callback: Callable[[dict[str, object]], None] | None = None,
        resume_agent_session: bool = False,
        agent_provider_override: str = "",
        bridge_session_id: str = "",
    ) -> TaskResult:
        started = time.monotonic()
        bridge_session_id = bridge_session_id.strip() or self._bridge_session_id(event)
        details = previous_session.get("details", {})
        if not isinstance(details, dict):
            details = {}
        job_id = str(previous_session.get("job_id") or "").strip()
        job_dir_value = str(previous_session.get("job_dir") or "").strip()
        if not job_id and job_dir_value:
            job_id = Path(job_dir_value).name
        if not job_id:
            return TaskResult(
                success=False,
                message="无法延续上次 Bug 分析：未找到上一轮 job_id。",
                error_code="bug_agent_followup_missing_job",
                details={"mode": "bug_agent_followup"},
            )
        job_dir = Path(job_dir_value) if job_dir_value else self.config.data_dir / "jobs" / job_id
        output_dir = job_dir / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        request_text = str(getattr(previous_context, "request_text", "") or details.get("user_request_text") or "").strip()
        prepared_input = self._path_from_details(details, "prepared_log_input")
        selected_input = self._path_from_details(details, "selected_log_input")
        previous_summary_path = self._path_from_details(details, "agent_summary_file")
        if previous_summary_path is not None and not previous_summary_path.exists():
            previous_summary_path = None
        report_files = self._collect_bug_output_artifacts(output_dir)
        plans = self._plans_from_previous_details(details, fallback_text=request_text)
        provider_session_id = self._bug_followup_resume_session_id(details, force=resume_agent_session)
        self._emit_progress(
            progress_callback,
            stage="bug_agent_followup_prepare",
            message=(
                "复用上一轮 bug 资料并新开本地 Agent 分析追问"
                if not provider_session_id
                else "复用上一轮 bug 资料并继续原本地 Agent 会话分析追问"
            ),
            job_id=job_id,
            prepared_log_input=str(prepared_input or ""),
            selected_log_input=str(selected_input or ""),
            output_dir=str(output_dir),
            provider_session_id=provider_session_id,
            analysis_kinds=[plan.kind for plan in plans],
        )
        agent_request_path = output_dir / "bug_agent_followup_request.md"
        agent_request_path.write_text(
            self._render_bug_agent_followup_request(
                request_text=request_text,
                followup_text=followup_text,
                history=getattr(previous_context, "history", None),
            ),
            encoding="utf-8",
        )
        agent_metadata_path = output_dir / "bug_agent_followup_metadata.md"
        agent_metadata_path.write_text(
            self._render_bug_agent_followup_metadata(
                request_text=request_text,
                followup_text=followup_text,
                job_id=job_id,
                job_dir=job_dir,
                output_dir=output_dir,
                prepared_input=prepared_input,
                selected_input=selected_input,
                previous_summary_path=previous_summary_path,
                report_files=report_files,
                report_url=str(getattr(previous_context, "report_url", "") or ""),
                analysis_skill=str(details.get("analysis_skill") or ""),
            ),
            encoding="utf-8",
        )
        agent_summary_path = previous_summary_path or (output_dir / "bug_agent_summary.md")
        snapshot_details = self._structured_bug_prompt_snapshot_details(
            base_details=details,
            request_text=request_text,
            plans=plans,
            target_time=str(details.get("target_time") or details.get("fault_time") or ""),
            prepared_input=prepared_input,
            selected_input=selected_input,
        )
        agent_summary_result = self._run_bug_agent_summary(
            request_text=request_text,
            request_artifact=agent_request_path,
            metadata_path=agent_metadata_path,
            output_path=agent_summary_path,
            progress_callback=progress_callback,
            timeout=self._agent_summary_timeout(
                self.config.bug_analysis.timeout_seconds,
                reference_seconds=self._agent_summary_timeout_reference(previous_session),
            ),
            provider_session_id=provider_session_id,
            followup_text=followup_text,
            previous_summary_path=previous_summary_path,
            snapshot_details=snapshot_details,
            snapshot_plans=plans,
            prefer_lightweight=self._should_prefer_lightweight_bug_summary(
                request_text=request_text,
                followup_text=followup_text,
                provider_session_id=provider_session_id,
            ),
            provider_override=agent_provider_override,
            bridge_session_id=bridge_session_id,
        )
        self._append_agent_runtime_metadata(
            agent_metadata_path,
            agent_summary_result=agent_summary_result,
            total_duration_seconds=time.monotonic() - started,
        )
        report_html_paths = [path for path in report_files if path.suffix.lower() == ".html"]
        self._annotate_html_reports(
            report_html_paths,
            agent_summary_result=agent_summary_result,
            total_duration_seconds=time.monotonic() - started,
        )
        result_details = {
            "mode": "bug_agent_followup",
            "analysis_kinds": [plan.kind for plan in plans],
            "prepared_log_input": str(prepared_input or ""),
            "selected_log_input": str(selected_input or ""),
            "user_request_text": request_text,
            "followup_text": followup_text,
            "selected_agent_provider": agent_provider_override,
            "agent_request_file": str(agent_request_path),
            "followup_metadata_file": str(agent_metadata_path),
            "agent_summary_file": str(agent_summary_path),
        }
        self._apply_agent_runtime_details(result_details, agent_summary_result)
        if not agent_summary_result["message"]:
            error = str(agent_summary_result["error"] or "")
            if error == "agent_summary_not_configured":
                message = "Bug 续聊失败：未配置可继续会话的本地 Agent。"
            elif error:
                message = f"Bug 续聊失败：本地 Agent 未返回结果（{error}）。"
            else:
                message = "Bug 续聊失败：本地 Agent 未返回结果。"
            return TaskResult(
                success=False,
                message=message,
                job_id=job_id,
                job_dir=job_dir,
                command=list(agent_summary_result["command"]) if agent_summary_result["command"] else None,
                duration_seconds=time.monotonic() - started,
                error_code="bug_agent_followup_failed",
                details=result_details,
            )
        self._emit_progress(
            progress_callback,
            stage="bug_agent_followup_completed",
            message="Bug 续聊已由本地 Agent 完成",
            job_id=job_id,
            provider=str(agent_summary_result["provider"] or ""),
            provider_session_id=str(agent_summary_result["session_id"] or provider_session_id),
        )
        return TaskResult(
            success=True,
            message=str(agent_summary_result["message"]),
            job_id=job_id,
            job_dir=job_dir,
            command=list(agent_summary_result["command"]) if agent_summary_result["command"] else None,
            duration_seconds=time.monotonic() - started,
            details=result_details,
        )

    def _bug_followup_resume_session_id(self, details: dict[str, object], *, force: bool = False) -> str:
        if not force and not self.config.bug_analysis.resume_followup_sessions:
            return ""
        return str(details.get("agent_summary_session_id") or "").strip()

    def run_direct_analysis(
        self,
        request: DirectAnalysisRequest,
        *,
        event: LarkEvent | None = None,
        progress_callback: Callable[[dict[str, object]], None] | None = None,
        plans_override: list["BugAnalysisPlan"] | None = None,
        classification_skill: str = "",
        classification_source: str = "",
        classification_reason: str = "",
    ) -> TaskResult:
        if request.error == "missing_prompt" or not request.prompt.strip():
            return TaskResult(
                success=False,
                message="缺少分析内容：请在附件后说明要分析什么问题。",
                error_code="missing_direct_analysis_prompt",
                details={"mode": "direct_analysis"},
            )
        if not request.resources:
            return TaskResult(
                success=False,
                message="缺少日志输入：请提供飞书附件或日志 URL。",
                error_code="missing_log",
                details={"mode": "direct_analysis"},
            )

        context = create_job_context(self.config.data_dir, event=event)
        bridge_session_id = self._bridge_session_id(event)
        metadata_path = context.output_dir / "direct_analysis_metadata.md"
        request_artifact = context.output_dir / "bug_agent_request.md"
        started = time.monotonic()
        request_text = self._request_text(raw_text=request.raw_text, prompt_text=request.prompt, bug_url="")
        plans = plans_override or self.classify_requests(prompt_text=request.prompt, title="", description="")
        source_decision = self._decide_source_analysis_request(
            request_text=request_text,
            prompt_text=request.prompt,
            title="",
            description="",
            plans=plans,
            skill_name=classification_skill,
        )
        plans = self._augment_plans_for_source_analysis(plans, source_decision=source_decision)
        if source_decision.requested and not classification_skill and all(_kind_spec(plan.kind).is_source_stage for plan in plans):
            classification_skill = "source_analysis"
        request_artifact.write_text(
            self._render_bug_agent_request(
                request_text=request_text,
                prompt_text=request.prompt,
                bug_url="",
                plans=plans,
            ),
            encoding="utf-8",
        )
        self._emit_progress(
            progress_callback,
            stage="direct_job_created",
            message="已创建直传文件分析任务",
            job_id=context.job_id,
            request_text=request_text,
            resources=[item.value for item in request.resources],
        )
        time_context = self._resolve_bug_time_context(
            request_text=request_text,
            title="",
            description="",
            reference_time=self._event_reference_time_text(event),
        )
        fault_time = time_context.fault_time
        if not time_context.has_full_datetime:
            return self._bug_time_clarification_result(
                context=context,
                started=started,
                request_text=request_text,
                bug_url="",
                time_context=time_context,
                status="missing_fault_time",
                progress_callback=progress_callback,
            )
        downloader = getattr(self, "_direct_downloader", None)
        if downloader is None:
            downloader = LogDownloader(self.config, getattr(self, "_lark_client", None))
            self._direct_downloader = downloader
        try:
            self._emit_progress(progress_callback, stage="direct_download_resources", message="下载直传附件或日志")
            downloaded = downloader.download_all(
                request.resources,
                context=context,
                message_id=event.message_id if event else "",
            )
        except DownloadError as exc:
            return TaskResult(
                success=False,
                message=f"下载失败：{exc}",
                job_id=context.job_id,
                job_dir=context.job_dir,
                duration_seconds=time.monotonic() - started,
                error_code="download_failed",
                details={"mode": "direct_analysis"},
            )

        selected_input = downloaded[0].path if len(downloaded) == 1 else context.input_dir
        self._emit_progress(progress_callback, stage="direct_prepare_logs", message="准备直传日志输入")
        prepared_input = self._prepare_log_input(selected_input) if selected_input.exists() else selected_input
        html_paths: list[Path] = []
        report_jsons: dict[str, Path | None] = {}
        command: list[str] | None = None
        evidence_log_bundle: dict[str, object] | None = None
        skill_file_agent_execution_result: dict[str, object] | None = None
        log_coverage = self._scan_log_time_coverage(prepared_input, fault_time=fault_time)
        if not log_coverage.has_time_evidence:
            return self._bug_time_clarification_result(
                context=context,
                started=started,
                request_text=request_text,
                bug_url="",
                time_context=time_context,
                status="log_time_unknown",
                progress_callback=progress_callback,
                log_coverage=log_coverage,
            )
        if not log_coverage.covers_fault_time:
            return self._bug_time_clarification_result(
                context=context,
                started=started,
                request_text=request_text,
                bug_url="",
                time_context=time_context,
                status="log_not_covering_fault_time",
                progress_callback=progress_callback,
                log_coverage=log_coverage,
            )
        source_evidence_path = self._write_reanalysis_source_evidence(
            plans=plans,
            request_text=request_text,
            followup_text=request.prompt,
            output_dir=context.output_dir,
            enabled=self._should_collect_source_evidence(request_text, request.prompt)
            or any(_kind_spec(plan.kind).is_source_stage for plan in plans)
            or any(_kind_spec(p.kind).needs_source_evidence for p in plans),
        )

        # 【新增】智能日志分析：定位进程号、找出相关日志、反推线索
        log_analysis_result = None
        if prepared_input and fault_time:
            # 使用第一个 plan 进行智能分析
            first_plan = plans[0] if plans else None
            if first_plan:
                log_analysis_result = self._analyze_logs_intelligently(
                    log_dir=prepared_input,
                    problem_time=self._fault_time_to_datetime(fault_time),
                    plan=first_plan,
                )

        for current_plan in plans:
            current_html = context.output_dir / self._report_name(current_plan.kind, "html")
            current_json = context.output_dir / self._report_name(current_plan.kind, "json")
            current_analysis_dir = context.output_dir / f"{current_plan.kind}_analysis"
            input_for_plan = prepared_input
            if current_plan.kind == "startup" and prepared_input is not None:
                input_for_plan = self._startup_analysis_input(prepared_input, fault_time)
            command = self.build_command(
                plan=current_plan,
                input_path=input_for_plan,
                html_path=current_html,
                json_path=current_json,
                analysis_dir=current_analysis_dir,
                target_time=fault_time if _kind_accepts_target_time(current_plan.kind) else None,
                request_text=request.prompt if current_plan.kind == "xtheme" else None,
                log_analysis=log_analysis_result,  # 传入智能日志分析结果
            )
            try:
                self._emit_progress(
                    progress_callback,
                    stage="direct_run_analysis",
                    message=f"执行{self._analysis_label(current_plan.kind)}",
                    plan=current_plan.kind,
                    plan_label=self._analysis_label(current_plan.kind),
                )
                if current_plan.kind == "general":
                    self._write_general_bug_report(
                        html_path=current_html,
                        json_path=current_json,
                        title="",
                        description="",
                        prompt_text=request.prompt,
                        request_text=request_text,
                        fault_time=fault_time,
                        selected_input=selected_input,
                        source_evidence_path=source_evidence_path,
                        classification_skill=self._skill_name_for_kind(current_plan.kind),
                        classification_source="manual_fallback",
                        classification_reason="直传文件分析未命中专用 skill，退回通用问题分析。",
                    )
                    completed = subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")
                elif _kind_spec(current_plan.kind).is_custom_agent:
                    if current_plan.kind == "ld_lane_level":
                        current_skill_name = self._skill_name_for_kind(current_plan.kind)
                    elif _kind_spec(current_plan.kind).is_source_stage:
                        if getattr(source_decision, "source_mode", "") == "append" and not getattr(source_decision, "context_profile", ""):
                            return TaskResult(
                                success=False,
                                message="直传文件分析失败：源码阶段缺少领域上下文，已停止以避免泛化扫描。",
                                job_id=context.job_id,
                                job_dir=context.job_dir,
                                command=command,
                                duration_seconds=time.monotonic() - started,
                                error_code="source_stage_missing_context",
                                details={
                                    "mode": "direct_analysis",
                                    "analysis_kind": current_plan.kind,
                                    "analysis_kinds": [item.kind for item in plans],
                                    "source_mode": getattr(source_decision, "source_mode", ""),
                                    "context_profile": getattr(source_decision, "context_profile", ""),
                                    "stage_kinds": list(getattr(source_decision, "stage_kinds", []) or []),
                                },
                            )
                        current_skill_name = self._resolve_source_stage_skill_name(classification_skill)
                    else:
                        current_skill_name = classification_skill or self._skill_name_for_kind(current_plan.kind)
                    if _kind_spec(current_plan.kind).needs_custom_executor_check and current_skill_name != "source_analysis" and self.skill_manager.custom_skill_executor_for(current_skill_name) not in {"file_agent", "pydantic_ai"}:
                        prefix = current_plan.kind if current_plan.kind in _SOURCE_SKILL_KINDS else SOURCE_STAGE_KIND
                        return TaskResult(
                            success=False,
                            message=self._custom_skill_executor_not_ready_message(
                                current_skill_name,
                                selected_input=selected_input,
                            ),
                            job_id=context.job_id,
                            job_dir=context.job_dir,
                            command=command,
                            duration_seconds=time.monotonic() - started,
                            error_code=f"direct_{prefix}_executor_not_ready",
                            details={
                                "mode": "direct_analysis",
                                "analysis_kind": current_plan.kind,
                                "analysis_skill": current_skill_name,
                                f"{prefix}_analysis_status": "executor_not_ready",
                                "target_time": fault_time,
                                "selected_log_input": str(selected_input),
                                "prepared_log_input": str(prepared_input),
                            },
                        )
                    # Source stage: prefer app-server-backed file-agent when enabled,
                    # while keeping the later summary stage independent.
                    if _kind_spec(current_plan.kind).is_source_stage:
                        if self._prefer_source_stage_file_agent():
                            self._emit_progress(
                                progress_callback,
                                stage="source_stage_app_server_preferred",
                                message="已启用 codex app-server，源码阶段直接走 file-agent 路径",
                            )
                            execution_skill_name = (
                                getattr(source_decision, "context_profile", "").strip()
                                or current_skill_name
                            )
                            custom_result = self._run_custom_skill_agent_analysis(
                                analysis_kind=current_plan.kind,
                                analysis_label=self._analysis_label(current_plan.kind),
                                skill_name=execution_skill_name,
                                request_text=request_text,
                                prompt_text=request.prompt,
                                title="",
                                description="",
                                fault_time=fault_time,
                                selected_input=selected_input,
                                prepared_input=prepared_input,
                                source_evidence_path=source_evidence_path,
                                html_path=current_html,
                                json_path=current_json,
                                analysis_dir=current_analysis_dir,
                                progress_callback=progress_callback,
                                timeout=self._source_stage_file_agent_timeout(
                                    reference_seconds=max(time.monotonic() - started, 240.0),
                                ),
                                bridge_session_id=bridge_session_id,
                                prior_findings=self._summarize_prior_report_jsons(report_jsons),
                                context_profile=execution_skill_name,
                                provider_override="codex",
                                command_override=self.config.codex_app_server.command,
                            )
                        else:
                            custom_result = self._run_source_stage_pydantic_ai(
                                analysis_kind=current_plan.kind,
                                skill_name=current_skill_name,
                                request_text=request_text,
                                prompt_text=request.prompt,
                                title="",
                                description="",
                                fault_time=fault_time,
                                selected_input=selected_input,
                                prepared_input=prepared_input,
                                source_evidence_path=source_evidence_path,
                                html_path=current_html,
                                json_path=current_json,
                                analysis_dir=current_analysis_dir,
                                progress_callback=progress_callback,
                                context_profile=getattr(source_decision, "context_profile", ""),
                                prior_findings=self._summarize_prior_report_jsons(report_jsons),
                            )
                            if not custom_result.get("ok"):
                                logger.warning(
                                    "direct_analysis source_stage pydantic-ai failed (error=%s), falling back to file_agent",
                                    custom_result.get("error_code", "unknown"),
                                )
                                custom_result = self._run_custom_skill_agent_analysis(
                                    analysis_kind=current_plan.kind,
                                    analysis_label=self._analysis_label(current_plan.kind),
                                    skill_name=current_skill_name,
                                    request_text=request_text,
                                    prompt_text=request.prompt,
                                    title="",
                                    description="",
                                    fault_time=fault_time,
                                    selected_input=selected_input,
                                    prepared_input=prepared_input,
                                    source_evidence_path=source_evidence_path,
                                    html_path=current_html,
                                    json_path=current_json,
                                    analysis_dir=current_analysis_dir,
                                    progress_callback=progress_callback,
                                    timeout=self._source_stage_file_agent_timeout(
                                        reference_seconds=max(time.monotonic() - started, 240.0),
                                    ),
                                    bridge_session_id=bridge_session_id,
                                    prior_findings=self._summarize_prior_report_jsons(report_jsons),
                                    context_profile=getattr(source_decision, "context_profile", ""),
                                )
                    # LD lane-level: try pydantic-ai runtime first (fast path)
                    elif current_plan.kind == "ld_lane_level":
                        custom_result = self._run_ld_pydantic_ai_analysis(
                            skill_name=current_skill_name,
                            request_text=request_text,
                            prompt_text=request.prompt,
                            title="",
                            description="",
                            fault_time=fault_time,
                            selected_input=selected_input,
                            prepared_input=prepared_input,
                            html_path=current_html,
                            json_path=current_json,
                            analysis_dir=current_analysis_dir,
                            progress_callback=progress_callback,
                            prior_findings=self._summarize_prior_report_jsons(report_jsons),
                        )
                        if not custom_result.get("ok"):
                            logger.warning(
                                "direct_analysis LD pydantic-ai failed (error=%s), falling back to file_agent",
                                custom_result.get("error_code", "unknown"),
                            )
                            custom_result = self._run_custom_skill_agent_analysis(
                                analysis_kind=current_plan.kind,
                                analysis_label=self._analysis_label(current_plan.kind),
                                skill_name=current_skill_name,
                                request_text=request_text,
                                prompt_text=request.prompt,
                                title="",
                                description="",
                                fault_time=fault_time,
                                selected_input=selected_input,
                                prepared_input=prepared_input,
                                source_evidence_path=source_evidence_path,
                                html_path=current_html,
                                json_path=current_json,
                                analysis_dir=current_analysis_dir,
                                progress_callback=progress_callback,
                                timeout=self._agent_summary_timeout(
                                    self.config.bug_analysis.timeout_seconds,
                                    reference_seconds=max(time.monotonic() - started, 240.0),
                                ),
                                bridge_session_id=bridge_session_id,
                                prior_findings=self._summarize_prior_report_jsons(report_jsons),
                            )
                    else:
                        custom_result = self._run_custom_skill_agent_analysis(
                            analysis_kind=current_plan.kind,
                            analysis_label=self._analysis_label(current_plan.kind),
                            skill_name=current_skill_name,
                            request_text=request_text,
                            prompt_text=request.prompt,
                            title="",
                            description="",
                            fault_time=fault_time,
                            selected_input=selected_input,
                            prepared_input=prepared_input,
                            source_evidence_path=source_evidence_path,
                            html_path=current_html,
                            json_path=current_json,
                            analysis_dir=current_analysis_dir,
                            progress_callback=progress_callback,
                            timeout=self._agent_summary_timeout(
                                self.config.bug_analysis.timeout_seconds,
                                reference_seconds=max(time.monotonic() - started, 240.0),
                            ),
                            bridge_session_id=bridge_session_id,
                            prior_findings=self._summarize_prior_report_jsons(report_jsons),
                            context_profile=getattr(source_decision, "context_profile", "") if _kind_spec(current_plan.kind).is_source_stage else "",
                        )
                    skill_file_agent_execution_result = custom_result
                    command = list(custom_result.get("command") or command or [])
                    if not custom_result.get("ok"):
                        return TaskResult(
                            success=False,
                            message=str(custom_result.get("message") or "直传专用 Skill 文件 Agent 执行失败。"),
                            job_id=context.job_id,
                            job_dir=context.job_dir,
                            command=command,
                            duration_seconds=time.monotonic() - started,
                            error_code=self._skill_file_agent_mode_error_code(
                                current_plan.kind,
                                str(custom_result.get("error_code") or "custom_skill_agent_failed"),
                                mode="direct_analysis",
                            ),
                            stdout=str(custom_result.get("stdout") or ""),
                            stderr=str(custom_result.get("stderr") or ""),
                            details={
                                "mode": "direct_analysis",
                                "analysis_kind": current_plan.kind,
                                "analysis_skill": current_skill_name,
                                f"{'custom_skill' if current_plan.kind == 'custom_skill' else current_plan.kind}_analysis_status": "failed",
                                "target_time": fault_time,
                                "selected_log_input": str(selected_input),
                                "prepared_log_input": str(prepared_input),
                                "stdout_path": str(custom_result.get("stdout_path") or ""),
                                "stderr_path": str(custom_result.get("stderr_path") or ""),
                                "command_path": str(custom_result.get("command_path") or ""),
                                "context_path": str(custom_result.get("context_path") or ""),
                                "log_focus_manifest_path": str(custom_result.get("log_focus_manifest_path") or ""),
                                "focused_log_input": str(custom_result.get("focused_log_input") or ""),
                                "debug_log_path": str(custom_result.get("debug_log_path") or ""),
                                "analysis_artifact_path": str(custom_result.get("analysis_markdown_path") or ""),
                            },
                        )
                    completed = subprocess.CompletedProcess(
                        args=command,
                        returncode=0,
                        stdout=str(custom_result.get("stdout") or ""),
                        stderr=str(custom_result.get("stderr") or ""),
                    )
                else:
                    analysis_kwargs = {
                        "plan": current_plan,
                        "input_path": input_for_plan,
                        "html_path": current_html,
                        "json_path": current_json,
                        "analysis_dir": current_analysis_dir,
                        "timeout": self.config.bug_analysis.timeout_seconds,
                        "target_time": fault_time if _kind_accepts_target_time(current_plan.kind) else None,
                        "request_text": request.prompt if current_plan.kind == "xtheme" else None,
                    }
                    if bridge_session_id:
                        analysis_kwargs["bridge_session_id"] = bridge_session_id
                    completed = self._run_analysis(**analysis_kwargs)
            except subprocess.TimeoutExpired as exc:
                return TaskResult(
                    success=False,
                    message=f"直传文件分析超时：{self._analysis_label(current_plan.kind)}",
                    job_id=context.job_id,
                    job_dir=context.job_dir,
                    command=command,
                    duration_seconds=time.monotonic() - started,
                    error_code=f"direct_analysis_{current_plan.kind}_timeout",
                    stdout=_coerce_process_text(exc.stdout),
                    stderr=_coerce_process_text(exc.stderr),
                    details={"mode": "direct_analysis"},
                )
            if completed.returncode != 0:
                return TaskResult(
                    success=False,
                    message=f"直传文件分析失败：{self._analysis_label(current_plan.kind)}脚本执行失败。",
                    job_id=context.job_id,
                    job_dir=context.job_dir,
                    command=command,
                    duration_seconds=time.monotonic() - started,
                    error_code=f"direct_analysis_{current_plan.kind}_failed",
                    stdout=completed.stdout,
                    stderr=completed.stderr,
                    details={"mode": "direct_analysis"},
                )
            html_paths.append(current_html)
            report_jsons[current_plan.kind] = current_json if current_json.exists() else None

        summary = self._build_direct_analysis_summary(plans, request.prompt, html_paths)
        metadata_path.write_text(summary, encoding="utf-8")
        evidence_log_bundle = preserve_evidence_log_bundle(
            output_dir=context.output_dir,
            source_roots=[selected_input, prepared_input, context.input_dir],
            reference_files=[metadata_path, *(path for path in report_jsons.values() if path is not None)],
            reference_texts=[summary],
        )
        self._append_evidence_log_metadata(metadata_path, evidence_log_bundle)
        combined_artifacts = self._build_combined_report_artifacts(
            plans=plans,
            prompt_text=request.prompt,
            fault_time=fault_time,
            output_dir=context.output_dir,
            html_paths=html_paths,
            report_jsons=report_jsons,
            selected_input=selected_input,
            source_evidence_path=None,
        )
        agent_summary_result = {
            "message": "",
            "provider": "",
            "model": "",
            "session_id": "",
            "resumed": False,
            "duration_seconds": 0.0,
            "usage": {},
            "usage_scope": "",
        }
        if any(_kind_spec(plan.kind).is_agent_handled for plan in plans):
            agent_summary_path = context.output_dir / "bug_agent_summary.md"
            agent_provider_override = ""
            # source_analysis is now handled by pydantic-ai runtime; no file-agent override needed
            snapshot_details = self._structured_bug_prompt_snapshot_details(
                base_details={
                    "analysis_skill": classification_skill or self._skill_name_for_kind(plans[0].kind if plans else "general"),
                    "classification_source": classification_source or "manual_fallback",
                    "classification_reason": classification_reason or "",
                },
                request_text=request_text,
                plans=plans,
                target_time=fault_time,
                prepared_input=prepared_input,
                selected_input=selected_input,
            )
            agent_summary_result = self._run_bug_agent_summary(
                request_text=request_text,
                request_artifact=request_artifact,
                metadata_path=metadata_path,
                output_path=agent_summary_path,
                progress_callback=progress_callback,
                timeout=self._agent_summary_timeout(
                    self.config.bug_analysis.timeout_seconds,
                    reference_seconds=max(
                        time.monotonic() - started,
                        240.0 if any(_kind_spec(i.kind).is_custom_agent for i in plans) else 0.0,
                    ),
                ),
                bridge_session_id=bridge_session_id,
                snapshot_details=snapshot_details,
                snapshot_plans=plans,
                provider_override=agent_provider_override,
            )
            self._append_agent_runtime_metadata(
                metadata_path,
                agent_summary_result=agent_summary_result,
                total_duration_seconds=time.monotonic() - started,
            )
            annotated_html_paths = list(html_paths)
            if combined_artifacts is not None:
                annotated_html_paths.append(Path(combined_artifacts["html_path"]))
            self._annotate_html_reports(
                annotated_html_paths,
                agent_summary_result=agent_summary_result,
                total_duration_seconds=time.monotonic() - started,
            )
        final_message = str(combined_artifacts["summary"]) if combined_artifacts is not None else summary
        if agent_summary_result.get("message"):
            final_message = str(agent_summary_result["message"])
        self._emit_progress(
            progress_callback,
            stage="direct_completed",
            message="直传文件分析完成",
            job_id=context.job_id,
            analysis_kinds=[item.kind for item in plans],
            html_reports=[str(path) for path in html_paths],
        )
        direct_details = {
            "mode": "direct_analysis",
            "analysis_kind": plans[0].kind if plans else "general",
            "analysis_kinds": [item.kind for item in plans],
            "selected_log_input": str(selected_input),
            "prepared_log_input": str(prepared_input),
            "fault_time": fault_time,
            "domain_kind": getattr(source_decision, "domain_kind", "") or self._domain_kind_from_plans(plans),
            "source_mode": getattr(source_decision, "source_mode", "") or "off",
            "context_profile": getattr(source_decision, "context_profile", ""),
            "stage_kinds": list(getattr(source_decision, "stage_kinds", []) or []),
            "source_targets": list(getattr(source_decision, "targets", []) or []),
            "log_coverage_start": log_coverage.start_time,
            "log_coverage_end": log_coverage.end_time,
            "log_coverage_scanned_files": log_coverage.scanned_files,
            "log_coverage_scanned_lines": log_coverage.scanned_lines,
            "analysis_skill": classification_skill or self._skill_name_for_kind(plans[0].kind if plans else "general"),
            "analysis_skill_label": (
                "源码导向文件分析"
                if classification_skill == "source_analysis" or (plans and _kind_spec(plans[0].kind).is_source_stage)
                else self._analysis_label(plans[0].kind if plans else "general")
            ),
            "classification_source": classification_source or "manual_fallback",
            "classification_reason": classification_reason or "",
            "agent_request_file": str(request_artifact),
            "agent_summary_file": str(context.output_dir / "bug_agent_summary.md"),
            **(
                {
                    "combined_report_html": str(combined_artifacts["html_path"]),
                    "combined_report_json": str(combined_artifacts["json_path"]),
                }
                if combined_artifacts is not None
                else {}
            ),
            "files_to_send": (
                [metadata_path, Path(combined_artifacts["html_path"])]
                if combined_artifacts is not None
                else [metadata_path, *html_paths]
            ),
            **(
                {
                    "evidence_log_bundle": str(evidence_log_bundle.get("bundle_dir") or ""),
                    "evidence_log_manifest": str(evidence_log_bundle.get("manifest_path") or ""),
                    "evidence_log_focus_logs": evidence_log_bundle.get("focus_logs") or [],
                    "evidence_log_file_count": evidence_log_bundle.get("file_count") or 0,
                }
                if evidence_log_bundle is not None
                else {}
            ),
        }
        if skill_file_agent_execution_result is not None:
            direct_details.update(
                self._skill_file_agent_execution_details(
                    str(skill_file_agent_execution_result.get("analysis_kind") or ""),
                    skill_file_agent_execution_result,
                )
            )
        return TaskResult(
            success=True,
            message=final_message,
            job_id=context.job_id,
            job_dir=context.job_dir,
            command=command,
            duration_seconds=time.monotonic() - started,
            details=direct_details,
        )

    def classify_requests(self, *, prompt_text: str, title: str, description: str) -> list["BugAnalysisPlan"]:
        combined = "\n".join(part for part in [prompt_text, title, description] if part).strip()
        signal_request = parse_signal_request(
            combined,
            signal_aliases=self.config.signal_aliases,
            command_prefixes=self.config.command_prefixes,
            signal_resolver=self.signal_resolver,
        )
        lowered = combined.casefold()
        explicit_signal_enum = "signal_" in lowered
        explicit_signal_terms = any(term in lowered for term in SIGNAL_ROUTE_TERMS)
        startup_requested = any(term in lowered for term in STARTUP_ROUTE_TERMS)
        stuck_requested = any(term in lowered for term in STUCK_ROUTE_TERMS)
        startup_blocked = any(term in lowered for term in STARTUP_BLOCK_ROUTE_TERMS)
        candidates: list[tuple[int, str, str | None]] = []

        def add_candidate(score: int, kind: str, signal_code: str | None = None) -> None:
            candidates.append((score, kind, signal_code))

        if any(term in lowered for term in PERCEPTION_ROUTE_TERMS):
            add_candidate(120, "perception")
        if any(term in lowered for term in LD_LANE_LEVEL_ROUTE_TERMS):
            add_candidate(117, "ld_lane_level")
        if any(term in lowered for term in PULLOVER_CHAIN_ROUTE_TERMS):
            add_candidate(116, "pullover_chain")
        if any(term in lowered for term in XTHEME_ROUTE_TERMS):
            add_candidate(115, "xtheme")
        if (
            (looks_like_scene_signal_request(combined) and not (signal_request.signal and (explicit_signal_enum or explicit_signal_terms)))
            or (signal_request.signal and _is_core_scene_signal(signal_request.signal))
            or _has_strong_scene_signal_intent(lowered)
        ):
            add_candidate(110, "scene_signal")
        if (
            signal_request.signal
            and (explicit_signal_enum or explicit_signal_terms)
            and not _is_core_scene_signal(signal_request.signal)
            and not _has_strong_scene_signal_intent(lowered)
        ):
            add_candidate(100, "signal", signal_request.signal)
        if any(term in lowered for term in CRASH_ROUTE_TERMS):
            add_candidate(95, "crash")
        plans: list[BugAnalysisPlan] = []
        if startup_requested or (stuck_requested and startup_blocked):
            plans.append(BugAnalysisPlan(kind="startup"))
        if stuck_requested:
            plans.append(BugAnalysisPlan(kind="stuck"))
        if plans:
            return plans
        if candidates:
            candidates.sort(key=lambda item: item[0], reverse=True)
            _score, kind, signal_code = candidates[0]
            return [BugAnalysisPlan(kind=kind, signal_code=signal_code)]
        return [BugAnalysisPlan(kind="general")]

    def classify_request(self, *, prompt_text: str, title: str, description: str) -> "BugAnalysisPlan":
        return self.classify_requests(prompt_text=prompt_text, title=title, description=description)[0]

    def build_command(
        self,
        *,
        plan: "BugAnalysisPlan",
        input_path: Path,
        html_path: Path,
        json_path: Path,
        analysis_dir: Path,
        target_time: str | None = None,
        request_text: str | None = None,
        log_analysis: dict[str, object] | None = None,
    ) -> list[str]:
        if plan.kind == "startup":
            command = [
                sys.executable,
                str(self._startup_script()),
                str(input_path),
                "--output-dir",
                str(analysis_dir),
            ]
            if target_time:
                command.extend(["--target-time", target_time])
            return command
        if plan.kind == "stuck":
            command = [
                sys.executable,
                str(self._stuck_script()),
                str(input_path),
            ]
            if target_time:
                command.extend(["--target-time", target_time])
            return command
        if plan.kind == "perception":
            command = [
                sys.executable,
                str(self._perception_script()),
                str(input_path),
            ]
            if target_time:
                command.extend(["--target-time", target_time])
            return command
        if plan.kind == "xtheme":
            command = [
                sys.executable,
                str(self._xtheme_script()),
                str(input_path),
            ]
            if target_time:
                command.extend(["--target-time", target_time])
            if request_text:
                command.extend(["--request-text", request_text])
            return command
        if plan.kind == "scene_signal":
            command = [
                sys.executable,
                str(self._scene_signal_script()),
                "--log-path",
                str(input_path),
                "--output-dir",
                str(analysis_dir),
            ]
            if target_time:
                command.extend(["--target-time", target_time])
            # 智能日志分析：传入 PID 和日志文件列表
            if log_analysis:
                target_pid = log_analysis.get("target_pid")
                if target_pid:
                    command.extend(["--pid", str(target_pid)])
                log_files = log_analysis.get("log_files")
                if log_files and isinstance(log_files, list):
                    command.extend(["--log-files", ",".join(str(f) for f in log_files)])
            return command
        if plan.kind == "crash":
            command = [
                sys.executable,
                str(self._stuck_script()),
                str(input_path),
            ]
            if target_time:
                command.extend(["--target-time", target_time])
            return command
        if _kind_spec(plan.kind).is_agent_handled:
            return []
        command = [
            sys.executable,
            str(self._signal_script()),
            "--signal-code",
            plan.signal_code or "",
            "--output",
            str(html_path),
            "--json-output",
            str(json_path),
        ]
        if input_path.exists():
            command.extend(["--log-path", str(input_path)])
        return command

    def _working_dir(self) -> Path:
        options = self.config.bug_analysis
        return options.working_dir or self.config.workspace_root

    def _plan_requires_log_input(self, plan: "BugAnalysisPlan") -> bool:
        return plan.kind in PLAN_KIND_REGISTRY

    def _bug_fetcher_script(self) -> Path:
        return self.config.workspace_root / ".ai/skills/feishu-bug-fetcher/scripts/bug-fetcher.sh"

    def _startup_script(self) -> Path:
        return self.config.workspace_root / ".ai/skills/unity-startup-lifecycle-check/scripts/analyze_unity_startup.py"

    def _stuck_script(self) -> Path:
        return self.config.workspace_root / ".ai/skills/3d-stuck-investigate/scripts/analyze_3d_stuck.py"

    def _signal_script(self) -> Path:
        return self.config.workspace_root / ".ai/skills/signal-chain-analyzer/scripts/analyze_signal_chain.py"

    def _scene_signal_script(self) -> Path:
        return self.config.workspace_root / ".ai/skills/scene-signal-diagnosis/scripts/extract_scene_signal_events.py"

    def _perception_script(self) -> Path:
        return self.config.workspace_root / ".ai/skills/perception-data-summary/scripts/analyze_perception_data_summary.py"

    def _xtheme_script(self) -> Path:
        return self.config.workspace_root / ".ai/skills/xtheme-analyzer/scripts/analyze_xtheme.py"

    def _bug_id(self, url: str) -> str:
        match = re.search(r"/buglo/detail/(\d+)", url)
        return match.group(1) if match else "unknown_bug"

    def _path_from_details(self, details: dict[str, object], key: str) -> Path | None:
        value = details.get(key)
        if not isinstance(value, str) or not value.strip():
            return None
        path = Path(value).expanduser()
        return path if path.exists() else None

    def _plans_from_previous_details(self, details: dict[str, object], *, fallback_text: str) -> list["BugAnalysisPlan"]:
        raw_kinds = details.get("analysis_kinds")
        kinds: list[str] = []
        if isinstance(raw_kinds, list):
            kinds = [str(item) for item in raw_kinds if str(item)]
        elif isinstance(details.get("analysis_kind"), str):
            kinds = [str(details["analysis_kind"])]
        plans = [
            BugAnalysisPlan(
                kind=kind,
                signal_code=str(details.get("signal_code") or "") or None,
            )
            for kind in kinds
            if kind in PLAN_KIND_REGISTRY
        ]
        if plans:
            return plans
        return self.classify_requests(prompt_text=fallback_text, title="", description="")

    def _plans_for_reanalysis(
        self,
        details: dict[str, object],
        *,
        request_text: str,
        followup_text: str,
    ) -> list["BugAnalysisPlan"]:
        return self._plans_from_previous_details(details, fallback_text=request_text)

    def _followup_explicitly_requests_source_analysis(self, request_text: str, followup_text: str) -> bool:
        if self._source_analysis_shortcut(request_text, followup_text):
            return True
        if self._should_collect_source_evidence(request_text, followup_text):
            return True
        return False

    def _fallback_non_source_plans_from_job_output(self, output_dir: Path) -> list["BugAnalysisPlan"]:
        candidates: list[tuple[float, str]] = []
        for kind in (
            "ld_lane_level",
            "startup",
            "stuck",
            "crash",
            "scene_signal",
            "signal",
            "xtheme",
            "perception",
            "general",
            "custom_skill",
            SOURCE_CODE_SKILL_KIND,
        ):
            report_json = output_dir / self._report_name(kind, "json")
            report_html = output_dir / self._report_name(kind, "html")
            existing = report_json if report_json.exists() else report_html if report_html.exists() else None
            if existing is None:
                continue
            try:
                mtime = existing.stat().st_mtime
            except OSError:
                continue
            candidates.append((mtime, kind))
        candidates.sort(reverse=True)
        if not candidates:
            return []
        return [BugAnalysisPlan(kind=candidates[0][1])]

    def _extract_signal_code_for_reanalysis(self, text: str) -> str:
        request = parse_signal_request(
            text,
            signal_aliases=self.config.signal_aliases,
            command_prefixes=self.config.command_prefixes,
            signal_resolver=self.signal_resolver,
        )
        return request.signal or ""

    def _forced_reanalysis_kinds(
        self,
        plans: list["BugAnalysisPlan"],
        *,
        request_text: str,
        followup_text: str,
        force_rerun: bool = False,
    ) -> set[str]:
        if force_rerun:
            return {plan.kind for plan in plans}
        lowered = f"{request_text}\n{followup_text}".casefold()
        signal_followup_terms = tuple(term.casefold() for term in self.config.bug_analysis.force_reanalysis_terms)
        force: set[str] = set()
        if any(plan.kind == "signal" for plan in plans) and any(term in lowered for term in signal_followup_terms):
            force.add("signal")
        return force

    def _report_name(self, kind: str, suffix: str) -> str:
        return {
            "startup": f"bug_3d_startup_report.{suffix}",
            "stuck": f"bug_3d_stuck_report.{suffix}",
            "crash": f"bug_crash_report.{suffix}",
            "scene_signal": f"bug_scene_signal_report.{suffix}",
            "perception": f"bug_perception_data_summary.{suffix}",
            "signal": f"bug_signal_chain_report.{suffix}",
            "xtheme": f"bug_xtheme_analysis_report.{suffix}",
            "ld_lane_level": f"bug_ld_lane_level_report.{suffix}",
            "pullover_chain": f"bug_pullover_chain_report.{suffix}",
            "general": f"bug_general_analysis_report.{suffix}",
            SOURCE_STAGE_KIND: f"source_stage_report.{suffix}",
            "custom_skill": f"bug_source_code_report.{suffix}",
            SOURCE_CODE_SKILL_KIND: f"bug_source_code_report.{suffix}",
        }[kind]

    def _combined_report_name(self, suffix: str) -> str:
        return f"bug_startup_stuck_report.{suffix}"

    def _analysis_label(self, kind: str) -> str:
        return {
            "startup": "3D启动时序分析",
            "stuck": "3D卡顿分析",
            "crash": "Crash/闪退分析",
            "scene_signal": "3D场景信号分析",
            "perception": "当前感知数据总结",
            "signal": "信号链路分析",
            "xtheme": "XTheme时光主题分析",
            "ld_lane_level": "LD车道级日志分析",
            "pullover_chain": "靠边停车链路分析",
            "general": "通用问题分析",
            SOURCE_STAGE_KIND: "源码分析阶段",
            "custom_skill": "源码分析 (source_code_skill)",
            SOURCE_CODE_SKILL_KIND: "源码分析 (source_code_skill)",
        }[kind]

    def _effective_skill_name_for_plan(self, plan_kind: str, candidate_skill_name: str) -> str:
        normalized = candidate_skill_name.strip()
        default_skill = self._skill_name_for_kind(plan_kind)
        if _kind_spec(plan_kind).is_source_stage:
            if normalized and (
                normalized == "source_analysis"
                or self.skill_manager.custom_skill_executor_for(normalized) == "file_agent"
            ):
                return normalized
            return default_skill
        if plan_kind in _SOURCE_SKILL_KINDS:
            return normalized or default_skill
        return default_skill

    def _subprocess_debug_log_path(
        self,
        directory: Path,
        stem: str,
        *,
        bridge_session_id: str = "",
    ) -> Path:
        safe_stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", stem).strip("._") or "subprocess"
        if bridge_session_id:
            safe_session = re.sub(r"[^A-Za-z0-9_.-]+", "_", bridge_session_id).strip("._")
            safe_stem = f"{safe_session}.{safe_stem}"
        return directory / f"{safe_stem}.debug.log"

    def _run_json_command(
        self,
        command: list[str],
        *,
        timeout: int,
        bridge_session_id: str = "",
    ) -> dict[str, object]:
        debug_log_path = self._subprocess_debug_log_path(
            self.config.data_dir / "subprocess_debug",
            "bug-json-command",
            bridge_session_id=bridge_session_id,
        )
        completed = _run_tracked_process(
            command,
            watchdog=self.process_watchdog,
            name="bug-json-command",
            cwd=self._working_dir(),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            session_id=bridge_session_id,
            debug_log_path=debug_log_path,
        )
        if completed.returncode != 0:
            message = completed.stderr.strip() or completed.stdout.strip() or "command failed"
            raise RuntimeError(message)
        payload = json.loads(completed.stdout)
        if isinstance(payload, dict) and payload.get("ok") is False:
            raise RuntimeError(str(payload.get("error", "command returned ok=false")))
        return payload

    def _load_option_map(self, project_key: str, *, bridge_session_id: str = "") -> dict[str, str]:
        payload = self._run_json_command(
            [
                "meegle",
                "workitem",
                "meta-fields",
                "--project-key",
                project_key,
                "--work-item-type",
                "buglo",
                "--field-keys",
                "field_24095d",
                "--field-keys",
                "field_45dc84",
                "--page-num",
                "1",
                "--format",
                "json",
            ],
            timeout=120,
            bridge_session_id=bridge_session_id,
        )
        option_map: dict[str, str] = {}
        for field in payload.get("list", []):
            if not isinstance(field, dict):
                continue
            for option in field.get("option", []):
                if not isinstance(option, dict):
                    continue
                option_id = option.get("option_id")
                option_name = option.get("option_name")
                if isinstance(option_id, str) and isinstance(option_name, str):
                    option_map[option_id] = option_name
        return option_map

    def _bug_cache_root(self) -> Path:
        return Path(self.config.data_dir).expanduser().resolve() / "bug_cache"

    def _bug_cache_dir(self, project_key: str, work_item_id: str) -> Path:
        key = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{project_key}_{work_item_id}").strip("._-")
        return self._bug_cache_root() / (key or str(work_item_id))

    def _has_bug_cache_content(self, bug_dir: Path) -> bool:
        for subdir in ("attachments", "logs"):
            path = bug_dir / subdir
            if not path.exists():
                continue
            for child in path.rglob("*"):
                if child.is_file():
                    return True
        return False

    def _is_bug_cache_fresh(self, bug_dir: Path, *, max_age_hours: int, now: datetime | None = None) -> bool:
        if max_age_hours <= 0 or not bug_dir.exists():
            return False
        reference_time = now or datetime.now(timezone.utc)
        age_seconds = reference_time.timestamp() - self._latest_path_mtime(bug_dir)
        return age_seconds <= max_age_hours * 3600

    def _reuse_prepared_bug_input(self, selected_input: Path | None) -> Path | None:
        if selected_input is None:
            return None
        if selected_input.is_dir():
            return selected_input
        lower_name = selected_input.name.lower()
        if lower_name.endswith(".xp"):
            extract_dir = selected_input.with_suffix("")
            if (extract_dir / "Log").exists():
                return extract_dir / "Log"
            if extract_dir.exists():
                return extract_dir
        if lower_name.endswith(".zip") and not lower_name.endswith(".xp.zip.001"):
            extract_dir = selected_input.with_suffix("")
            if extract_dir.exists():
                return extract_dir
        return None

    def _write_bug_cache_metadata(
        self,
        bug_dir: Path,
        *,
        bug_url: str,
        project_key: str,
        work_item_id: str,
        selected_input: Path | None,
        prepared_input: Path | None,
    ) -> None:
        bug_dir.mkdir(parents=True, exist_ok=True)
        metadata_path = bug_dir / "cache.json"
        now = datetime.now(timezone.utc).isoformat()
        payload = {
            "bug_url": bug_url,
            "project_key": project_key,
            "work_item_id": work_item_id,
            "selected_log_input": str(selected_input) if selected_input else "",
            "prepared_log_input": str(prepared_input) if prepared_input else "",
            "updated_at": now,
        }
        if metadata_path.exists():
            try:
                existing = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                existing = {}
            if isinstance(existing, dict) and existing.get("created_at"):
                payload["created_at"] = str(existing.get("created_at"))
        payload.setdefault("created_at", now)
        metadata_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _recover_bug_title_from_metadata(self, output_dir: Path) -> str:
        """Recover bug title from bug_metadata.md in the job output.

        This is needed for follow-ups like "请根据BUG问题实际时间继续分析"
        where the agent needs the title to extract the correct fault time.
        """
        metadata_path = output_dir / "bug_metadata.md"
        if not metadata_path.exists():
            return ""
        try:
            text = metadata_path.read_text(encoding="utf-8")[:2000]
        except OSError:
            return ""
        match = re.search(r"[标題标题]:\s*`([^`]+)`", text)
        if match:
            return match.group(1).strip()
        match = re.search(r"[标題标题]:\s*(.+)", text)
        if match:
            return match.group(1).strip()
        return ""

    def _recover_cached_bug_log_input(
        self,
        details: dict[str, object],
        *,
        request_text: str,
    ) -> tuple[Path | None, Path | None]:
        bug_dir = self._bug_cache_dir_from_context(details, request_text=request_text)
        if bug_dir is None or not bug_dir.exists():
            return None, None
        selected_input, prepared_input = self._read_bug_cache_log_input(bug_dir)
        if prepared_input is not None:
            return selected_input or prepared_input, prepared_input
        selected_input = self._select_existing_bug_cache_input(bug_dir)
        if selected_input is None:
            return None, None
        try:
            prepared_input = self._reuse_prepared_bug_input(selected_input)
            if prepared_input is None:
                prepared_input = self._prepare_log_input(selected_input)
        except (OSError, RuntimeError, zipfile.BadZipFile):
            if selected_input.is_dir():
                prepared_input = selected_input
            else:
                return None, None
        return selected_input, prepared_input

    def _bug_cache_dir_from_context(self, details: dict[str, object], *, request_text: str) -> Path | None:
        for key in ("bug_cache_dir", "bug_dir"):
            value = details.get(key)
            if isinstance(value, str) and value.strip():
                return Path(value).expanduser()
        bug_url = str(details.get("bug_url") or "").strip()
        if not bug_url:
            match = re.search(r"https?://project\.feishu\.cn/\S+", request_text)
            bug_url = match.group(0).rstrip("`，。；;、)") if match else ""
        identity = self._bug_identity_from_url_or_text(bug_url or request_text)
        if identity is None:
            return None
        project_key, work_item_id = identity
        return self._bug_cache_dir(project_key, work_item_id)

    def _bug_identity_from_url_or_text(self, text: str) -> tuple[str, str] | None:
        match = re.search(r"project\.feishu\.cn/([^/\s]+)/buglo/detail/(\d+)", text)
        if not match:
            return None
        return match.group(1), match.group(2)

    def _read_bug_cache_log_input(self, bug_dir: Path) -> tuple[Path | None, Path | None]:
        metadata_path = bug_dir / "cache.json"
        if not metadata_path.exists():
            return None, None
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None, None
        if not isinstance(payload, dict):
            return None, None
        selected_input = self._existing_path_from_text(payload.get("selected_log_input"))
        prepared_input = self._existing_path_from_text(payload.get("prepared_log_input"))
        if prepared_input is None and selected_input is not None:
            try:
                prepared_input = self._reuse_prepared_bug_input(selected_input)
                if prepared_input is None:
                    prepared_input = self._prepare_log_input(selected_input)
            except (OSError, RuntimeError, zipfile.BadZipFile):
                prepared_input = selected_input if selected_input.is_dir() else None
        return selected_input, prepared_input

    def _existing_path_from_text(self, value: object) -> Path | None:
        if not isinstance(value, str) or not value.strip():
            return None
        path = Path(value).expanduser()
        return path if path.exists() else None

    def _select_existing_bug_cache_input(self, bug_dir: Path) -> Path | None:
        logs_dir = bug_dir / "logs"
        attachments_dir = bug_dir / "attachments"
        if self._has_meaningful_log_tree(logs_dir):
            return logs_dir
        if self._has_decoded_log_tree(attachments_dir):
            return attachments_dir
        candidates: list[Path] = []
        for root in (attachments_dir, logs_dir):
            if not root.exists():
                continue
            try:
                candidates.extend(path for path in root.rglob("*") if path.is_file())
            except OSError:
                continue
        for suffix in _BUG_LOG_INPUT_PRIORITY_SUFFIXES:
            for candidate in sorted(candidates, key=lambda item: str(item)):
                if candidate.name.lower().endswith(suffix) and self._is_usable_log_attachment(candidate):
                    return candidate
        if self._has_meaningful_log_tree(attachments_dir):
            return attachments_dir
        if self._has_meaningful_log_tree(bug_dir):
            return bug_dir
        return None

    def _has_decoded_log_tree(self, root: Path) -> bool:
        if not root.exists():
            return False
        try:
            for path in root.rglob("*"):
                if path.is_file() and self._is_log_coverage_file(path):
                    return True
        except OSError:
            return False
        return False

    def cleanup_expired_bug_cache(self, *, max_age_hours: int, now: datetime | None = None) -> int:
        if max_age_hours <= 0:
            return 0
        root = self._bug_cache_root()
        if not root.exists():
            return 0
        reference_time = now or datetime.now(timezone.utc)
        cutoff_seconds = max_age_hours * 3600
        removed = 0
        for bug_dir in root.iterdir():
            if not bug_dir.is_dir():
                continue
            age_seconds = reference_time.timestamp() - self._latest_path_mtime(bug_dir)
            if age_seconds <= cutoff_seconds:
                continue
            if self._remove_tree(bug_dir):
                removed += 1
        return removed

    def _latest_path_mtime(self, root: Path) -> float:
        latest = 0.0
        try:
            for child in root.rglob("*"):
                if not child.is_file():
                    continue
                try:
                    child_mtime = child.stat().st_mtime
                except OSError:
                    continue
                if child_mtime > latest:
                    latest = child_mtime
        except OSError:
            return latest
        return latest or root.stat().st_mtime

    def _remove_tree(self, root: Path) -> bool:
        try:
            shutil.rmtree(root)
        except (FileNotFoundError, PermissionError, OSError):
            return False
        return True

    def _download_bug_attachments(
        self,
        project_key: str,
        work_item_id: str,
        bug_dir: Path,
        attachments: object,
        *,
        timeout: int,
        bridge_session_id: str = "",
    ) -> dict[str, object]:
        attachments_dir = bug_dir / "attachments"
        logs_dir = bug_dir / "logs"
        attachments_dir.mkdir(parents=True, exist_ok=True)
        logs_dir.mkdir(parents=True, exist_ok=True)
        downloaded: list[str] = []
        unzipped: list[str] = []
        errors: list[str] = []
        error_details: list[dict[str, str]] = []
        skipped: list[str] = []
        if not isinstance(attachments, list):
            return {
                "ok": True,
                "downloaded": downloaded,
                "unzipped": unzipped,
                "errors": errors,
                "error_details": error_details,
                "skipped": skipped,
            }

        for item in attachments:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            url = str(item.get("url") or "").strip()
            if not name:
                continue
            if not self._should_download_bug_attachment(name):
                skipped.append(name)
                continue
            if not url:
                errors.append(name)
                error_details.append({"name": name, "reason": "missing_attachment_url"})
                continue
            output_path = (attachments_dir / name).expanduser().resolve()
            completed = _run_tracked_process(
                [
                    "meegle",
                    "attachment",
                    "+download",
                    url,
                    "--project-key",
                    project_key,
                    "--work-item-id",
                    work_item_id,
                    "--output",
                    str(output_path),
                    "--overwrite",
                    "--format",
                    "json",
                ],
                watchdog=self.process_watchdog,
                name="bug-attachment-download",
                cwd=self._working_dir(),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                session_id=bridge_session_id,
                debug_log_path=self._subprocess_debug_log_path(
                    attachments_dir,
                    f"download-{output_path.name}",
                    bridge_session_id=bridge_session_id,
                ),
            )
            if completed.returncode != 0:
                errors.append(name)
                error_details.append({"name": name, "reason": self._extract_process_error_message(completed)})
                continue
            downloaded.append(name)
            if output_path.name.lower().endswith(".zip") and zipfile.is_zipfile(output_path):
                if self._extract_downloaded_zip(output_path, logs_dir):
                    unzipped.append(name)
        return {
            "ok": True,
            "downloaded": downloaded,
            "unzipped": unzipped,
            "errors": errors,
            "error_details": error_details,
            "skipped": skipped,
        }

    def _extract_process_error_message(self, completed: subprocess.CompletedProcess[str]) -> str:
        candidates = [completed.stderr or "", completed.stdout or ""]
        for text in candidates:
            stripped = text.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                return stripped.splitlines()[0][:400]
            if isinstance(payload, dict):
                error = payload.get("error")
                if isinstance(error, dict):
                    code = str(error.get("code") or "").strip()
                    message = str(error.get("message") or "").strip()
                    if code and message:
                        return f"{code}: {message}"
                    if message:
                        return message
                if payload.get("ok") is False:
                    return str(payload.get("error") or "command returned ok=false")
            return stripped[:400]
        return "unknown_error"

    def _should_download_bug_attachment(self, name: str) -> bool:
        lower_name = name.strip().casefold()
        return bool(lower_name) and any(lower_name.endswith(suffix) for suffix in _BUG_ATTACHMENT_DOWNLOAD_SUFFIXES)

    def _extract_downloaded_zip(self, archive_path: Path, logs_dir: Path) -> bool:
        try:
            with zipfile.ZipFile(archive_path) as zf:
                zf.extractall(logs_dir)
        except (OSError, zipfile.BadZipFile):
            return False
        self._normalize_tree_permissions(logs_dir)
        try:
            archive_path.unlink()
        except OSError:
            pass
        return True

    def _select_log_input(self, bug_dir: Path, fetched: dict[str, object]) -> Path | None:
        attachments_dir = bug_dir / "attachments"
        logs_dir = bug_dir / "logs"
        attachments: list[Path] = []
        for item in fetched.get("attachments", []):
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if isinstance(name, str):
                candidate = attachments_dir / name
                if candidate.exists():
                    attachments.append(candidate)

        if self._has_meaningful_log_tree(logs_dir):
            return logs_dir
        for suffix in _BUG_LOG_INPUT_PRIORITY_SUFFIXES:
            for candidate in attachments:
                if candidate.name.lower().endswith(suffix) and self._is_usable_log_attachment(candidate):
                    return candidate
        return None

    def _ensure_decoded_in_place(self, prepared: Path) -> None:
        """If prepared still contains raw .alog/.xlog files, decode them in-place.
        Best-effort: failures only emit a warning, leaving _decode_raw_logs_before_analysis
        as the per-kind safety net.
        """
        try:
            if not prepared.exists():
                return
            if not self._has_raw_logs_needing_decode(prepared):
                return
        except OSError:
            return
        decoder = self.config.workspace_root / ".ai/skills/log-decoder/tools/alog_decoder.py"
        if not decoder.exists():
            logger.warning("log decoder not found: %s", decoder)
            return
        decoder_python = shutil.which("python3") or "python3"
        command = [decoder_python, str(decoder), str(prepared)]
        try:
            result = _run_tracked_process(
                command,
                watchdog=self.process_watchdog,
                name="prepare-log-decode",
                cwd=self._working_dir(),
                capture_output=True,
                text=True,
                timeout=900,
                check=False,
            )
        except Exception as exc:  # subprocess startup/timeout
            logger.warning("alog decode during prepare failed: %s", exc)
            return
        if result.returncode != 0:
            logger.warning(
                "alog decode during prepare returned %s: %s",
                result.returncode,
                (result.stderr or result.stdout or "").strip()[:400],
            )
        else:
            logger.info("stage=prepare_log_input decoded prepared=%s", prepared)

    def _prepare_log_input(self, selected_input: Path) -> Path:
        lower_name = selected_input.name.lower()
        if lower_name.endswith(".xp"):
            prepared = self._expand_xp_file(selected_input)
        elif self._is_archive_log_attachment(selected_input) and not lower_name.endswith(".xp.zip.001"):
            prepared = self._extract_log_archive(selected_input)
        elif selected_input.is_dir():
            archive = self._select_best_archive(selected_input)
            prepared = self._prepare_log_input(archive) if archive else selected_input
        else:
            prepared = selected_input
        self._ensure_decoded_in_place(prepared)
        return prepared

    def _fault_time_to_datetime(self, fault_time: str) -> datetime | None:
        return self._parse_bug_datetime(fault_time)

    def _select_best_archive(self, directory: Path) -> Path | None:
        try:
            children = sorted(directory.iterdir())
        except OSError:
            return None
        for suffix in _BUG_LOG_INPUT_PRIORITY_SUFFIXES:
            for child in children:
                if child.is_file() and child.name.lower().endswith(suffix) and self._is_usable_log_attachment(child):
                    if child.name.lower().endswith(".xp.zip.001"):
                        continue
                    return child
        return None

    def _analyze_logs_intelligently(
        self,
        log_dir: Path,
        problem_time: datetime | None,
        plan: "BugAnalysisPlan",
    ) -> dict[str, object] | None:
        """智能日志分析

        根据问题时间点定位进程号，基于进程号找出相关日志，反推日志线索

        Args:
            log_dir: 日志目录
            problem_time: 问题时间点
            plan: 分析计划

        Returns:
            智能分析结果，包含 target_pid, log_files, timeline, suggested_skill
            如果分析失败或不适用，返回 None
        """
        if not problem_time:
            logger.info("智能日志分析: 跳过（无问题时间）")
            return None

        if not self.log_analyzer:
            logger.info("智能日志分析: 跳过（分析器未初始化）")
            return None

        # 只对需要日志的分析类型启用智能分析
        if not _kind_spec(plan.kind).log_dependent:
            logger.info("智能日志分析: 跳过（类型 %s 不需要日志）", plan.kind)
            return None

        logger.info("智能日志分析: 开始, type=%s, time=%s", plan.kind, problem_time)

        try:
            # Step 1: 根据问题时间定位进程号
            target_pid = self.log_analyzer.find_target_pid(log_dir, problem_time)
            if not target_pid:
                logger.warning("智能日志分析: 未找到目标进程号")
                return None

            # Step 2: 基于进程号找出所有相关日志
            log_files = self.log_analyzer.find_all_log_files(log_dir, target_pid)
            if not log_files:
                logger.warning("智能日志分析: 未找到相关日志文件")
                return None

            # Step 3: 反推日志线索
            timeline = self.log_analyzer.reverse_trace(log_files, problem_time, target_pid)

            # Step 4: 识别问题类型（可选，用于验证）
            suggested_skill = self.log_analyzer.identify_problem_type(timeline) if timeline else plan.kind

            result = {
                "target_pid": target_pid,
                "log_files": [str(f) for f in log_files],
                "timeline": timeline,
                "suggested_skill": suggested_skill,
                "event_count": len(timeline),
            }

            logger.info(
                "智能日志分析: 完成, pid=%d, files=%d, events=%d, suggested=%s",
                target_pid,
                len(log_files),
                len(timeline),
                suggested_skill,
            )

            return result

        except Exception as e:
            logger.error("智能日志分析失败: %s", e, exc_info=True)
            return None

    def _is_archive_log_attachment(self, path: Path) -> bool:
        lower_name = path.name.lower()
        return any(lower_name.endswith(suffix) for suffix in _BUG_ARCHIVE_SUFFIXES)

    def _archive_extract_dir(self, archive_path: Path) -> Path:
        lower_name = archive_path.name.lower()
        for suffix in sorted(_BUG_ARCHIVE_SUFFIXES, key=len, reverse=True):
            if lower_name.endswith(suffix):
                return archive_path.with_name(archive_path.name[: -len(suffix)])
        return archive_path.with_suffix("")

    def _extract_log_archive(self, archive_path: Path) -> Path:
        lower_name = archive_path.name.lower()
        extract_dir = self._archive_extract_dir(archive_path)
        if lower_name.endswith(".zip"):
            if not zipfile.is_zipfile(archive_path):
                raise RuntimeError(f"日志附件不是有效 zip: {archive_path.name}")
            if not extract_dir.exists():
                with zipfile.ZipFile(archive_path) as zf:
                    zf.extractall(extract_dir)
                self._normalize_tree_permissions(extract_dir)
            return extract_dir
        if extract_dir.exists():
            return extract_dir
        extract_dir.mkdir(parents=True, exist_ok=True)
        command = self._archive_extract_command(archive_path, extract_dir)
        if command is None:
            raise RuntimeError(f"无法解压日志附件 {archive_path.name}: 未找到 bsdtar/7z/unar")
        completed = _run_tracked_process(
            command,
            watchdog=self.process_watchdog,
            name="bug-log-archive-extract",
            cwd=self._working_dir(),
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
            debug_log_path=self._subprocess_debug_log_path(
                archive_path.parent,
                f"extract-{archive_path.name}",
            ),
        )
        if completed.returncode != 0:
            shutil.rmtree(extract_dir, ignore_errors=True)
            reason = completed.stderr.strip() or completed.stdout.strip() or "archive extract failed"
            raise RuntimeError(f"日志附件解压失败 {archive_path.name}: {reason}")
        self._normalize_tree_permissions(extract_dir)
        return extract_dir

    def _archive_extract_command(self, archive_path: Path, extract_dir: Path) -> list[str] | None:
        bsdtar = shutil.which("bsdtar")
        if bsdtar:
            return [bsdtar, "-xf", str(archive_path), "-C", str(extract_dir)]
        seven_zip = shutil.which("7zz") or shutil.which("7z")
        if seven_zip:
            return [seven_zip, "x", "-y", f"-o{extract_dir}", str(archive_path)]
        unar = shutil.which("unar")
        if unar:
            return [unar, "-force-overwrite", "-output-directory", str(extract_dir), str(archive_path)]
        return None

    def _retry_bug_log_download(
        self,
        *,
        previous_session: dict[str, object],
        job_dir: Path,
        progress_callback: Callable[[dict[str, object]], None] | None,
        bridge_session_id: str = "",
    ) -> dict[str, object]:
        details = previous_session.get("details", {}) if isinstance(previous_session, dict) else {}
        if not isinstance(details, dict):
            details = {}
        bridge_kwargs = {"bridge_session_id": bridge_session_id} if bridge_session_id else {}
        bug_url = str(details.get("bug_url") or "").strip()
        if not bug_url:
            return {"ok": False, "message": "缺少 bug_url，无法重新下载日志。"}
        try:
            env_status = self._run_json_command(
                [str(self._bug_fetcher_script()), "check-env"],
                timeout=60,
                **bridge_kwargs,
            )
        except Exception as exc:
            return {"ok": False, "message": f"重新检查 meegle 环境失败：{exc}"}
        if not env_status.get("meegle_installed", False):
            return {"ok": False, "message": "重新下载日志失败：本机未安装 meegle CLI。"}
        if not env_status.get("auth_ok", False):
            host = str(env_status.get("host") or "project.feishu.cn")
            return {
                "ok": False,
                "message": f"重新下载日志失败：meegle 未登录或授权已失效（host={host}）。",
                "reason": "AUTH_REQUIRED",
            }
        try:
            resolved = self._run_json_command(
                [str(self._bug_fetcher_script()), "resolve-url", bug_url],
                timeout=60,
                **bridge_kwargs,
            )
        except Exception as exc:
            return {"ok": False, "message": f"重新解析 bug 链接失败：{exc}"}
        project_key = str(resolved.get("project_key") or "").strip()
        work_item_id = str(resolved.get("work_item_id") or "").strip()
        if not project_key or not work_item_id:
            return {"ok": False, "message": "重新下载日志失败：bug 链接未解析出 project_key/work_item_id。"}
        bug_dir = self._bug_cache_dir(project_key, work_item_id)
        bug_dir.mkdir(parents=True, exist_ok=True)
        self._emit_progress(
            progress_callback,
            stage="bug_reanalysis_retry_download",
            message="上一轮没有可复用日志，尝试重新下载 bug 附件",
            project_key=project_key,
            work_item_id=work_item_id,
            bug_cache_dir=str(bug_dir),
        )
        try:
            fetched = self._run_json_command(
                [str(self._bug_fetcher_script()), "fetch-data", project_key, work_item_id],
                timeout=120,
                **bridge_kwargs,
            )
        except Exception as exc:
            return {"ok": False, "message": f"重新拉取 bug 附件列表失败：{exc}"}
        download = self._download_bug_attachments(
            project_key,
            work_item_id,
            bug_dir,
            fetched.get("attachments", []),
            timeout=self.config.bug_analysis.timeout_seconds,
            **bridge_kwargs,
        )
        selected_input = self._select_log_input(bug_dir, fetched)
        prepared_input = self._reuse_prepared_bug_input(selected_input) if selected_input else None
        if prepared_input is None and selected_input is not None:
            prepared_input = self._prepare_log_input(selected_input)
        self._write_bug_cache_metadata(
            bug_dir,
            bug_url=bug_url,
            project_key=project_key,
            work_item_id=work_item_id,
            selected_input=selected_input,
            prepared_input=prepared_input,
        )
        if prepared_input is not None:
            return {
                "ok": True,
                "message": "已重新下载并准备日志输入。",
                "selected_input": selected_input,
                "prepared_input": prepared_input,
                "download": download,
            }
        attachment_lines = self._render_attachment_lines(fetched.get("attachments", []), download)
        return {
            "ok": False,
            "message": f"重新下载日志后仍未拿到可用日志输入。\n附件结果：\n{attachment_lines}",
            "download": download,
        }

    def _has_meaningful_log_tree(self, root: Path) -> bool:
        if not root.exists():
            return False
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            lower_name = path.name.lower()
            if lower_name in {"prop.txt", "dfx.txt"}:
                continue
            if lower_name.endswith((".alog", ".xlog", ".log", ".txt", ".xp", ".zip", ".001")):
                return True
        return False

    def _is_usable_log_attachment(self, path: Path) -> bool:
        lower_name = path.name.lower()
        if lower_name.endswith(".zip") and not lower_name.endswith(".xp.zip.001"):
            return zipfile.is_zipfile(path)
        if lower_name.endswith(".xp"):
            return not self._looks_like_non_log_xp_payload(path)
        return True

    def _looks_like_non_log_xp_payload(self, path: Path) -> bool:
        try:
            head = path.read_bytes()[:16]
        except OSError:
            return False
        if any(head.startswith(prefix) for prefix in _NON_LOG_XP_MAGIC_HEADERS):
            return True
        if len(head) >= 12 and head.startswith(b"RIFF") and head[8:12] == b"WEBP":
            return True
        if len(head) >= 8 and head[4:8] == b"ftyp":
            return True
        return False

    def _expand_xp_file(self, xp_path: Path) -> Path:
        jar_path = self.config.workspace_root / ".ai/skills/log-decoder/tools/decryptFile.jar"
        # Pass only the filename and run from the xp file's directory so that
        # DecryptFile.jar's String.replace("xp","zip") doesn't corrupt the
        # directory components of the path (e.g. "xpfailuremgmt" → "zipfailuremgmt").
        completed = _run_tracked_process(
            ["java", "-jar", str(jar_path), xp_path.name],
            watchdog=self.process_watchdog,
            name="xp-log-decrypt",
            cwd=xp_path.parent,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
            debug_log_path=self._subprocess_debug_log_path(xp_path.parent, f"decrypt-{xp_path.name}"),
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "decrypt xp failed")
        inner_zip = xp_path.with_suffix(".zip")
        if not inner_zip.exists():
            raise RuntimeError(f"xp 解密后未产出 zip: {inner_zip}")
        extract_dir = xp_path.with_suffix("")
        if not extract_dir.exists():
            with zipfile.ZipFile(inner_zip) as zf:
                zf.extractall(extract_dir)
            self._normalize_tree_permissions(extract_dir)
        return extract_dir / "Log" if (extract_dir / "Log").exists() else extract_dir

    def _normalize_tree_permissions(self, root: Path) -> None:
        try:
            root.chmod(root.stat().st_mode | stat.S_IRWXU)
        except OSError:
            return
        for path in root.rglob("*"):
            try:
                mode = path.stat().st_mode
                if path.is_dir():
                    path.chmod(mode | stat.S_IRWXU)
                else:
                    path.chmod(mode | stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                continue

    # Package whose app log should be preferred when selecting startup input.
    _STARTUP_TARGET_PACKAGE = "com.xiaopeng.montecarlo"

    def _select_startup_input(self, input_path: Path, fault_time: str) -> Path:
        if input_path.is_file():
            return input_path
        fault_dt = self._parse_fault_datetime(fault_time)
        if fault_dt is None:
            return input_path
        fault_epoch = time.mktime(fault_dt)
        all_candidates = [
            path
            for path in input_path.rglob("*")
            if path.is_file()
            and path.suffix.lower() in {".alog", ".xlog", ".log", ".txt"}
            and self._parse_log_file_datetime(path.name) is not None
        ]
        # Prefer files inside the target app package directory; fall back to
        # all candidates so the script can emit a meaningful warning itself.
        target_pkg = self._STARTUP_TARGET_PACKAGE
        montecarlo = [p for p in all_candidates if target_pkg in str(p)]
        candidates = montecarlo if montecarlo else all_candidates
        ranked: list[tuple[float, int, Path]] = []
        for candidate in candidates:
            file_dt = self._parse_log_file_datetime(candidate.name)
            if file_dt is None:
                continue
            score = abs(time.mktime(file_dt) - fault_epoch)
            ranked.append((score, self._log_file_priority(candidate), candidate))
        if not ranked:
            return input_path
        ranked.sort(key=lambda item: (item[0], item[1], str(item[2])))
        return ranked[0][2]

    def _startup_analysis_input(self, input_path: Path, fault_time: str) -> Path:
        if input_path.is_file():
            return input_path
        return self._select_startup_input(input_path, fault_time)

    def _parse_fault_datetime(self, fault_time: str) -> "time.struct_time | None":
        short_match = re.search(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)", fault_time)
        if short_match and "20" not in fault_time:
            # Bare HH:MM without a date cannot be reliably resolved —
            # using the current date would silently mis-select logs when
            # analysing historical bugs.  Return None so callers fall back
            # to the original input directory.
            return None
        match = re.search(
            r"(20\d{2})[-_/年](\d{1,2})[-_/月](\d{1,2})[日_\s-]*(\d{1,2}):(\d{2})",
            fault_time,
        )
        if not match:
            return None
        try:
            return time.strptime(
                f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d} "
                f"{int(match.group(4)):02d}:{int(match.group(5)):02d}",
                "%Y-%m-%d %H:%M",
            )
        except ValueError:
            return None

    def _parse_log_file_datetime(self, name: str) -> "time.struct_time | None":
        match = re.search(r"(20\d{2}-\d{2}-\d{2})_(\d{2})-(\d{2})(?=\D|$)", name)
        if not match:
            return None
        try:
            return time.strptime(
                f"{match.group(1)} {match.group(2)}:{match.group(3)}",
                "%Y-%m-%d %H:%M",
            )
        except ValueError:
            return None

    def _log_file_priority(self, path: Path) -> int:
        lower = path.name.lower()
        if lower.endswith((".alog.log", ".xlog.log")):
            return 0
        if lower.endswith(".log"):
            return 1
        if lower.endswith(".txt"):
            return 2
        if lower.endswith(".alog"):
            return 3
        if lower.endswith(".xlog"):
            return 4
        return 5

    def _bridge_session_id(self, event: LarkEvent | None, fallback: str = "") -> str:
        if event is None:
            return fallback.strip()
        return (event.root_id or event.message_id or event.event_id or fallback).strip()

    def _run_analysis(
        self,
        *,
        plan: "BugAnalysisPlan",
        input_path: Path | None,
        html_path: Path,
        json_path: Path,
        analysis_dir: Path,
        timeout: int,
        target_time: str | None = None,
        request_text: str | None = None,
        bridge_session_id: str = "",
    ) -> subprocess.CompletedProcess[str]:
        decode_completed = self._decode_raw_logs_before_analysis(
            plan=plan,
            input_path=input_path,
            analysis_dir=analysis_dir,
            target_time=target_time,
            bridge_session_id=bridge_session_id,
        )
        if decode_completed is not None and decode_completed.returncode != 0:
            return decode_completed
        command = self.build_command(
            plan=plan,
            input_path=input_path or self.config.workspace_root,
            html_path=html_path,
            json_path=json_path,
            analysis_dir=analysis_dir,
            target_time=target_time,
            request_text=request_text,
        )
        completed = _run_tracked_process(
            command,
            watchdog=self.process_watchdog,
            name=f"bug-analysis-{plan.kind}",
            cwd=self._working_dir(),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            session_id=bridge_session_id,
            debug_log_path=self._subprocess_debug_log_path(
                analysis_dir,
                f"{plan.kind}-analysis",
                bridge_session_id=bridge_session_id,
            ),
        )
        if completed.returncode != 0 and plan.kind in _RETRY_KINDS:
            completed = _run_tracked_process(
                command,
                watchdog=self.process_watchdog,
                name=f"bug-analysis-{plan.kind}-retry",
                cwd=self._working_dir(),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                session_id=bridge_session_id,
                debug_log_path=self._subprocess_debug_log_path(
                    analysis_dir,
                    f"{plan.kind}-analysis-retry",
                    bridge_session_id=bridge_session_id,
                ),
            )
        if completed.returncode != 0:
            return completed

        generated_html, generated_json = BugReportAdapter.locate_report(
            kind=plan.kind,
            completed_stdout=completed.stdout or "",
            analysis_dir=analysis_dir,
            html_path=html_path,
            json_path=json_path,
        )
        if generated_html and generated_html.exists() and generated_html != html_path:
            shutil.copy2(generated_html, html_path)
        if generated_json and generated_json.exists() and generated_json != json_path:
            shutil.copy2(generated_json, json_path)
        return completed

    def _decode_raw_logs_before_analysis(
        self,
        *,
        plan: "BugAnalysisPlan",
        input_path: Path | None,
        analysis_dir: Path,
        target_time: str | None,
        bridge_session_id: str = "",
    ) -> subprocess.CompletedProcess[str] | None:
        if input_path is None or not input_path.exists():
            return None
        if not self._has_raw_logs_needing_decode(input_path):
            return None
        decoder = self.config.workspace_root / ".ai/skills/log-decoder/tools/alog_decoder.py"
        if not decoder.exists():
            logger.warning("log decoder not found: %s", decoder)
            return None
        decoder_python = shutil.which("python3") or "python3"
        command = [decoder_python, str(decoder), str(input_path)]
        if not input_path.is_file():
            time_arg = self._decoder_time_arg(target_time)
            if time_arg:
                command.append(time_arg)
        return _run_tracked_process(
            command,
            watchdog=self.process_watchdog,
            name=f"bug-analysis-{plan.kind}-decode-logs",
            cwd=self._working_dir(),
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
            session_id=bridge_session_id,
            debug_log_path=self._subprocess_debug_log_path(
                analysis_dir,
                f"{plan.kind}-log-decode",
                bridge_session_id=bridge_session_id,
            ),
        )

    def _has_raw_logs_needing_decode(self, input_path: Path) -> bool:
        def needs_decode(path: Path) -> bool:
            lower = path.name.lower()
            return lower.endswith((".alog", ".xlog")) and not Path(str(path) + ".log").exists()

        if input_path.is_file():
            return needs_decode(input_path)
        try:
            return any(needs_decode(path) for path in input_path.rglob("*") if path.is_file())
        except OSError:
            return False

    def _decoder_time_arg(self, target_time: str | None) -> str:
        if not target_time:
            return ""
        parsed = self._parse_fault_datetime(target_time)
        if parsed is None:
            return ""
        return time.strftime("%Y-%m-%d %H", parsed)

    def _extract_report_path(self, output: str, pattern: str) -> Path | None:
        match = re.search(pattern, output, re.MULTILINE)
        if not match:
            return None
        return Path(match.group(1).strip())

    def _bug_description(self, fetched: dict[str, object]) -> str:
        fields = fetched.get("fields", {})
        fallback = fetched.get("description", "")
        if not isinstance(fields, dict):
            return fallback if isinstance(fallback, str) else ""
        value = fields.get("field_204366", "")
        if isinstance(value, str) and value:
            return value
        return fallback if isinstance(fallback, str) else ""

    def _bug_reference_time(self, fetched: dict[str, object], full_item: dict[str, object]) -> str:
        for value in (
            fetched.get("create_time"),
            fetched.get("created_at"),
            fetched.get("createTime"),
            self._nested_string(full_item, "work_item_attribute", "create_time"),
            self._nested_string(full_item, "work_item_attribute", "created_at"),
            self._nested_string(full_item, "data", "work_item_attribute", "create_time"),
            self._nested_string(full_item, "data", "work_item_attribute", "created_at"),
            full_item.get("create_time"),
            full_item.get("created_at"),
        ):
            text = str(value or "").strip()
            if text:
                return text
        return ""

    def _nested_string(self, payload: object, *keys: str) -> str:
        current = payload
        for key in keys:
            if not isinstance(current, dict):
                return ""
            current = current.get(key)
        return str(current or "").strip()

    def _build_bug_outputs(
        self,
        *,
        plans: list["BugAnalysisPlan"],
        work_item_id: str,
        fetched: dict[str, object],
        full_item: dict[str, object],
        option_map: dict[str, str],
        request_text: str,
        prompt_text: str,
        selected_input: Path | None,
        report_jsons: dict[str, Path | None],
        download: dict[str, object],
        html_paths: list[Path],
        classification_skill: str = "",
        classification_source: str = "",
        classification_reason: str = "",
        classification_provider: str = "",
        fault_time: str | None = None,
        fault_time_note: str | None = None,
        log_coverage: LogCoverage | None = None,
    ) -> tuple[str, str]:
        title = str(fetched.get("title", ""))
        description = self._bug_description(fetched)
        status = str(fetched.get("status", ""))
        create_time = self._bug_reference_time(fetched, full_item)
        create_by = str(fetched.get("create_by", ""))
        owner = self._extract_owner(full_item)
        bug_source = self._map_option(option_map, fetched.get("fields", {}), "field_24095d")
        found_version = self._string_field(fetched.get("fields", {}), "field_010122")
        probability = self._map_option(option_map, fetched.get("fields", {}), "field_45dc84")
        if fault_time is None or fault_time_note is None:
            extracted_fault_time, extracted_fault_time_note = self._extract_fault_time(title, description)
            fault_time = extracted_fault_time if fault_time is None else fault_time
            fault_time_note = extracted_fault_time_note if fault_time_note is None else fault_time_note
        summary_blocks: list[str] = []
        for plan in plans:
            html_path = next((path for path in html_paths if path.name == self._report_name(plan.kind, "html")), None)
            if html_path is None:
                continue
            summary_blocks.append(
                self._build_summary_from_report(
                    plan=plan,
                    report_json=report_jsons.get(plan.kind),
                    prompt_text=prompt_text,
                    fault_time=fault_time,
                    html_path=html_path,
                    selected_input=selected_input,
                )
            )
        summary = "\n\n".join(summary_blocks)
        attachment_lines = self._render_attachment_lines(fetched.get("attachments", []), download)
        analysis_lines = "\n".join(
            f"  - `{self._analysis_label(plan.kind)}` -> `{self._report_name(plan.kind, 'html')}`"
            for plan in plans
        )
        report_artifact_lines: list[str] = []
        for plan in plans:
            html_path = next((path for path in html_paths if path.name == self._report_name(plan.kind, "html")), None)
            json_path = report_jsons.get(plan.kind)
            if html_path is not None:
                report_artifact_lines.append(f"  - HTML `{self._analysis_label(plan.kind)}`: `{html_path}`")
            if json_path is not None:
                report_artifact_lines.append(f"  - JSON `{self._analysis_label(plan.kind)}`: `{json_path}`")
            analysis_md_name = self._skill_agent_analysis_markdown_name(plan.kind)
            analysis_md_path = (html_path.parent / f"{plan.kind}_analysis" / analysis_md_name) if html_path else None
            if analysis_md_path and analysis_md_path.exists():
                report_artifact_lines.append(f"  - Analysis `{self._analysis_label(plan.kind)}`: `{analysis_md_path}`")
        report_artifacts = "\n".join(report_artifact_lines) if report_artifact_lines else "  - 无"
        metadata = (
            "# Bug Metadata\n\n"
            f"- Bug ID: `{work_item_id}`\n"
            f"- 标题: `{title}`\n"
            f"- 当前状态: `{status or '未返回 / 未设置'}`\n"
            f"- 创建时间: `{create_time or '未返回 / 未设置'}`\n"
            f"- 创建人: `{create_by or '未返回 / 未设置'}`\n"
            f"- 当前负责人: `{owner}`\n"
            f"- 缺陷来源: `{bug_source}`\n"
            f"- 发现版本: `{found_version}`\n"
            f"- 发生概率: `{probability}`\n"
            f"- 分析类型:\n{analysis_lines}\n"
            f"- 命中 Skill: `{classification_skill or self._skill_name_for_kind(plans[0].kind if plans else 'general')}`\n"
            f"- 分类来源: `{classification_source or 'manual_fallback'}`\n"
            f"- 分类 Agent: `{classification_provider or '无'}`\n"
            f"- 分类理由: `{classification_reason or '未记录'}`\n"
            f"- Skill 规范:\n{self._render_skill_context_lines(classification_skill)}"
            f"- 信号代码: `{', '.join(plan.signal_code for plan in plans if plan.signal_code) or '无'}`\n"
            f"- 故障时间: `{fault_time or '未识别'}`\n"
            f"  说明: {fault_time_note}\n"
            f"- 日志覆盖范围: `{self._format_log_coverage_for_metadata(log_coverage)}`\n"
            "- 用户原始请求:\n\n```text\n"
            f"{request_text}\n"
            "```\n"
            f"- 分析请求: `{prompt_text}`\n"
            f"- 选中日志输入: `{selected_input or '无，可静态分析'}`\n"
            f"- 报告产物:\n{report_artifacts}\n"
            f"- 附件:\n{attachment_lines}\n"
            "- 缺陷描述:\n\n```text\n"
            f"{description.strip() or '(无描述)'}\n"
            "```\n\n"
            "- 本轮脚本初步摘要（仅代表本轮自动脚本输出，不代表上一轮分析结论；若与源码证据冲突，以源码与原始输入为准）:\n\n"
            f"{summary}\n"
        )
        return metadata, summary

    def _skill_context_paths(self, skill_name: str) -> list[Path]:
        normalized = skill_name.strip()
        if not normalized or normalized == "general":
            return []
        try:
            record = self.skill_manager.get_skill(normalized, include_content=False)
        except Exception:
            return []
        paths: list[Path] = []
        skill_md = Path(record.skill_md_path).expanduser() if record.skill_md_path else Path()
        if skill_md and skill_md.exists() and skill_md.is_file():
            paths.append(skill_md)
            references_dir = skill_md.parent / "references"
            if references_dir.exists():
                for path in sorted(references_dir.glob("*.md"))[:4]:
                    if path.is_file():
                        paths.append(path)
        return paths

    def _render_skill_context_lines(self, skill_name: str) -> str:
        paths = self._skill_context_paths(skill_name)
        if not paths:
            return "  - 无\n"
        return "".join(f"  - `{path}`\n" for path in paths)

    def _extract_owner(self, full_item: dict[str, object]) -> str:
        current_nodes = full_item.get("work_item_current_node", [])
        if isinstance(current_nodes, list) and current_nodes:
            owners = current_nodes[0].get("owners", []) if isinstance(current_nodes[0], dict) else []
            if isinstance(owners, list) and owners:
                owner = owners[0]
                if isinstance(owner, dict):
                    return str(owner.get("name", "未返回 / 未设置"))
        return "未返回 / 未设置"

    def _map_option(self, option_map: dict[str, str], fields: object, key: str) -> str:
        if not isinstance(fields, dict):
            return "未返回 / 未设置"
        raw = fields.get(key, "")
        if isinstance(raw, str) and raw:
            return option_map.get(raw, raw)
        return "未返回 / 未设置"

    def _string_field(self, fields: object, key: str) -> str:
        if not isinstance(fields, dict):
            return "未返回 / 未设置"
        value = fields.get(key, "")
        if isinstance(value, str) and value:
            return value
        return "未返回 / 未设置"

    def _resolve_bug_time_context(
        self,
        *,
        request_text: str,
        title: str,
        description: str,
        reference_time: str = "",
    ) -> BugTimeContext:
        sources = [
            ("user", self._strip_urls_for_time_parse(request_text)),
            ("title", title or ""),
            ("description", description or ""),
        ]
        reference_year = self._reference_year_from_text(reference_time)
        fallback_reference_date = self._reference_date_from_text(reference_time)
        candidates: list[dict[str, str]] = []
        for source, text in sources:
            candidate = self._extract_time_candidate(text, reference_year=reference_year)
            if candidate:
                candidates.append({"source": source, **candidate})

        if not candidates:
            llm_candidate = self._extract_time_via_llm(
                request_text=request_text,
                title=title,
                description=description,
                reference_time=reference_time,
            )
            if llm_candidate:
                candidates.append(llm_candidate)

        if not candidates:
            return BugTimeContext(
                fault_time="",
                source="",
                note="用户输入、标题和缺陷描述中都未识别到几月几日几点几分的问题时间。",
                has_full_datetime=False,
                candidates=[],
            )

        reference_date = self._select_reference_date(candidates) or fallback_reference_date
        override_source = self._preferred_bug_time_override_source(
            candidates,
            authoritative_date=self._authoritative_bug_time_date(candidates, fallback_reference_date),
        )
        preferred_sources = [override_source] if override_source else []
        preferred_sources.extend(source for source in ("user", "title", "description", "llm") if source != override_source)
        for preferred_source in preferred_sources:
            for candidate in candidates:
                if candidate["source"] != preferred_source:
                    continue
                fault_time = candidate["value"]
                has_full_datetime = bool(candidate.get("date"))
                if not has_full_datetime and reference_date:
                    fault_time = f"{reference_date} {candidate['time']}"
                    has_full_datetime = True
                note = self._bug_time_context_note(candidate, reference_date=reference_date, completed=has_full_datetime)
                if override_source and preferred_source == override_source:
                    label = {"title": "标题", "description": "缺陷描述", "llm": "AI识别"}.get(preferred_source, preferred_source)
                    note = f"用户输入时间与 bug 标题/创建时间明显冲突，优先采用{label}中的完整问题时间。"
                return BugTimeContext(
                    fault_time=fault_time,
                    source=preferred_source,
                    note=note,
                    has_full_datetime=has_full_datetime,
                    candidates=candidates,
                )

        return BugTimeContext(
            fault_time="",
            source="",
            note="已找到时间片段，但无法补齐到几月几日几点几分。",
            has_full_datetime=False,
            candidates=candidates,
        )

    def _authoritative_bug_time_date(
        self,
        candidates: list[dict[str, str]],
        fallback_reference_date: str,
    ) -> str:
        if fallback_reference_date:
            return fallback_reference_date
        for source in ("title", "description", "llm"):
            for candidate in candidates:
                if candidate.get("source") == source and candidate.get("date"):
                    return str(candidate["date"])
        return ""

    def _preferred_bug_time_override_source(
        self,
        candidates: list[dict[str, str]],
        *,
        authoritative_date: str,
    ) -> str:
        user_candidate = next(
            (
                candidate
                for candidate in candidates
                if candidate.get("source") == "user" and candidate.get("date")
            ),
            None,
        )
        if user_candidate is None or not authoritative_date or user_candidate.get("date") == authoritative_date:
            return ""
        user_dt = self._parse_bug_datetime(str(user_candidate.get("value") or ""))
        authoritative_dt = self._parse_bug_datetime(f"{authoritative_date} 00:00")
        if user_dt is None or authoritative_dt is None:
            return ""
        if user_dt.year == authoritative_dt.year and abs((user_dt - authoritative_dt).days) < 30:
            return ""
        for source in ("title", "description", "llm"):
            for candidate in candidates:
                if candidate.get("source") == source and candidate.get("date") == authoritative_date:
                    return source
        return ""

    def _strip_urls_for_time_parse(self, text: str) -> str:
        return re.sub(r"https?://\S+", " ", text or "")

    def _reference_year_from_text(self, text: str) -> int | None:
        match = re.search(r"\b(20\d{2})\b", text or "")
        return int(match.group(1)) if match else None

    def _reference_date_from_text(self, text: str) -> str:
        normalized = (text or "").replace("：", ":").replace("/", "-")
        match = re.search(r"\b(20\d{2})[-年](\d{1,2})[-月](\d{1,2})", normalized)
        if not match:
            return ""
        return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"

    def _extract_time_candidate(self, text: str, *, reference_year: int | None = None) -> dict[str, str] | None:
        normalized = (text or "").replace("：", ":")
        full_match = re.search(
            r"(20\d{2})[-_/年](\d{1,2})[-_/月](\d{1,2})[日_\s-]*(\d{1,2}):(\d{2})(?::(\d{2}))?",
            normalized,
        )
        if full_match:
            date = f"{int(full_match.group(1)):04d}-{int(full_match.group(2)):02d}-{int(full_match.group(3)):02d}"
            time_text = self._format_time_parts(full_match.group(4), full_match.group(5), full_match.group(6))
            return {"value": f"{date} {time_text}", "date": date, "time": time_text, "raw": full_match.group(0)}
        md_match = re.search(
            r"(?<!\d)\[?(\d{1,2})[-/](\d{1,2})\]?(?:[\]\[日_\s-]+)(\d{1,2}):(\d{2})(?::(\d{2}))?",
            normalized,
        )
        if md_match:
            year = reference_year or datetime.now().year
            date = f"{year:04d}-{int(md_match.group(1)):02d}-{int(md_match.group(2)):02d}"
            time_text = self._format_time_parts(md_match.group(3), md_match.group(4), md_match.group(5))
            return {"value": f"{date} {time_text}", "date": date, "time": time_text, "raw": md_match.group(0)}
        cn_match = re.search(
            r"(?<!\d)(\d{1,2})月(\d{1,2})日[^\d]{0,8}(\d{1,2}):(\d{2})(?::(\d{2}))?",
            normalized,
        )
        if cn_match:
            year = reference_year or datetime.now().year
            date = f"{year:04d}-{int(cn_match.group(1)):02d}-{int(cn_match.group(2)):02d}"
            time_text = self._format_time_parts(cn_match.group(3), cn_match.group(4), cn_match.group(5))
            return {"value": f"{date} {time_text}", "date": date, "time": time_text, "raw": cn_match.group(0)}
        short_match = re.search(r"(?<!\d)(\d{1,2}):(\d{2})(?::(\d{2}))?(?!\d)", normalized)
        if short_match:
            time_text = self._format_time_parts(short_match.group(1), short_match.group(2), short_match.group(3))
            return {"value": time_text, "date": "", "time": time_text, "raw": short_match.group(0)}
        return None

    def _format_time_parts(self, hour: str, minute: str, second: str | None = None) -> str:
        base = f"{int(hour):02d}:{int(minute):02d}"
        if second is not None:
            return f"{base}:{int(second):02d}"
        return base

    def _extract_time_via_llm(
        self,
        *,
        request_text: str,
        title: str,
        description: str,
        reference_time: str = "",
    ) -> dict[str, str] | None:
        """Use LLM (fast_model) to extract fault time when regex fails."""
        from .llm_client import LLMClient, LLMClientError

        client = LLMClient(self.config.ai_provider)
        if not client.is_available():
            return None

        parts: list[str] = []
        stripped = self._strip_urls_for_time_parse(request_text or "")
        if stripped.strip():
            parts.append(f"用户输入: {stripped.strip()}")
        if title:
            parts.append(f"标题: {title}")
        if description:
            parts.append(f"描述: {description}")
        if reference_time:
            parts.append(f"参考时间（创建时间）: {reference_time}")
        if not parts:
            return None

        now = datetime.now()
        system_prompt = (
            "你是一个时间提取器。从用户提供的文本中提取问题发生的精确时间。\n"
            f"当前时间: {now:%Y-%m-%d %H:%M}。\n"
            "规则:\n"
            "1. 优先取用户明确给出的时间，其次标题，最后描述。\n"
            "2. '5月19日_18点35分' 应解析为 2026-05-19 18:35。\n"
            "3. '昨天下午3点' 等相对时间，基于当前时间推算为绝对时间。\n"
            "4. 如果只有日期没有具体时间，found 设为 false。\n"
            "5. 年份缺失时用当前年份补全。\n"
            '返回 JSON: {"found": true/false, "datetime": "YYYY-MM-DD HH:MM", "source": "从哪段文字提取的", "reason": "简短说明"}\n'
            "只返回 JSON，不要其他文字。"
        )

        try:
            response = client.chat(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": "\n".join(parts)},
                ],
                model="",
                temperature=0.0,
                max_tokens=256,
                timeout_seconds=15,
                response_format={"type": "json_object"} if client._effective_api_format() == "openai" else None,
            )
        except (LLMClientError, Exception):  # noqa: BLE001
            logger.debug("LLM time extraction failed, falling back to regex-only")
            return None

        try:
            data = json.loads(response.content)
            if not data.get("found"):
                return None
            dt_str = str(data.get("datetime", ""))
            datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
            return {
                "value": dt_str,
                "date": dt_str.split(" ")[0],
                "time": dt_str.split(" ")[1],
                "raw": str(data.get("source", "")),
                "source": "llm",
                "note": str(data.get("reason", "")),
            }
        except (json.JSONDecodeError, ValueError, KeyError):
            logger.debug("LLM time extraction returned invalid JSON or format")
            return None

    def _select_reference_date(self, candidates: list[dict[str, str]]) -> str:
        for source in ("user", "title", "description", "llm"):
            for candidate in candidates:
                if candidate.get("source") == source and candidate.get("date"):
                    return str(candidate["date"])
        return ""

    def _bug_time_context_note(self, candidate: dict[str, str], *, reference_date: str, completed: bool) -> str:
        labels = {"user": "用户输入", "title": "标题", "description": "缺陷描述", "llm": "AI识别"}
        source = labels.get(candidate.get("source", ""), candidate.get("source", ""))
        if candidate.get("date"):
            return f"从{source}提取完整问题时间。"
        if completed and reference_date:
            return f"从{source}提取时分，并用 {reference_date} 补齐日期。"
        return f"从{source}只提取到时分，缺少日期。"

    def _scan_log_time_coverage(self, input_path: Path, *, fault_time: str) -> LogCoverage:
        fault_dt = self._parse_bug_datetime(fault_time)
        if fault_dt is None:
            return LogCoverage(
                has_time_evidence=False,
                covers_fault_time=False,
                reason="fault_time_not_full_datetime",
            )
        reference_year = fault_dt.year
        timestamps: list[datetime] = []
        scanned_files = 0
        scanned_lines = 0
        sample_file = ""
        for path in self._iter_log_coverage_files(input_path):
            scanned_files += 1
            file_dt = self._parse_log_file_datetime(path.name)
            if file_dt is not None:
                file_start = datetime(
                    file_dt.tm_year,
                    file_dt.tm_mon,
                    file_dt.tm_mday,
                    file_dt.tm_hour,
                    file_dt.tm_min,
                )
                timestamps.extend([file_start, file_start + timedelta(minutes=59, seconds=59)])
                sample_file = sample_file or str(path)
            try:
                with path.open("r", encoding="utf-8", errors="replace") as handle:
                    for index, line in enumerate(handle):
                        if index >= _BUG_LOG_COVERAGE_MAX_LINES_PER_FILE:
                            break
                        scanned_lines += 1
                        line_dt = self._parse_log_line_datetime(line, reference_year=reference_year)
                        if line_dt is None:
                            continue
                        timestamps.append(line_dt)
                        sample_file = sample_file or str(path)
            except OSError:
                continue
        if not timestamps:
            return LogCoverage(
                has_time_evidence=False,
                covers_fault_time=False,
                scanned_files=scanned_files,
                scanned_lines=scanned_lines,
                reason="no_log_time_found",
            )
        timestamps.sort()
        start = timestamps[0]
        end = timestamps[-1]
        window = timedelta(minutes=_BUG_LOG_COVERAGE_WINDOW_MINUTES)
        covers = start - window <= fault_dt <= end + window
        return LogCoverage(
            has_time_evidence=True,
            covers_fault_time=covers,
            start_time=self._format_bug_datetime_minute(start),
            end_time=self._format_bug_datetime_minute(end),
            scanned_files=scanned_files,
            scanned_lines=scanned_lines,
            sample_file=sample_file,
            reason="covered" if covers else "not_covering_fault_time",
        )

    def _iter_log_coverage_files(self, input_path: Path) -> list[Path]:
        if input_path.is_file():
            return [input_path] if self._is_log_coverage_file(input_path) else []
        candidates: list[Path] = []
        try:
            for path in input_path.rglob("*"):
                if len(candidates) >= _BUG_LOG_COVERAGE_MAX_FILES:
                    break
                if path.is_file() and self._is_log_coverage_file(path):
                    candidates.append(path)
        except OSError:
            return candidates
        candidates.sort(key=lambda item: str(item))
        return candidates

    def _is_log_coverage_file(self, path: Path) -> bool:
        lower_name = path.name.lower()
        return any(lower_name.endswith(suffix) for suffix in _BUG_LOG_COVERAGE_SUFFIXES)

    def _parse_log_line_datetime(self, line: str, *, reference_year: int) -> datetime | None:
        full_match = re.search(
            r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})[ T](\d{1,2}):(\d{2}):(\d{2})(?:\.\d+)?\b",
            line,
        )
        if full_match:
            return self._safe_datetime(
                int(full_match.group(1)),
                int(full_match.group(2)),
                int(full_match.group(3)),
                int(full_match.group(4)),
                int(full_match.group(5)),
                int(full_match.group(6)),
            )
        bracket_match = re.search(r"\[(20\d{2}-\d{2}-\d{2}) \+\d{4} (\d{2}:\d{2}:\d{2})\]", line)
        if bracket_match:
            try:
                return datetime.strptime(f"{bracket_match.group(1)} {bracket_match.group(2)}", "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return None
        md_match = re.search(r"(?<!\d)(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})(?:\.\d+)?", line)
        if md_match:
            return self._safe_datetime(
                reference_year,
                int(md_match.group(1)),
                int(md_match.group(2)),
                int(md_match.group(3)),
                int(md_match.group(4)),
                int(md_match.group(5)),
            )
        return None

    def _safe_datetime(self, year: int, month: int, day: int, hour: int, minute: int, second: int = 0) -> datetime | None:
        try:
            return datetime(year, month, day, hour, minute, second)
        except ValueError:
            return None

    def _parse_bug_datetime(self, value: str) -> datetime | None:
        normalized = self._normalize_fault_time_text(value)
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                return datetime.strptime(normalized, fmt)
            except ValueError:
                continue
        return None

    def _format_bug_datetime_minute(self, value: datetime) -> str:
        return value.strftime("%Y-%m-%d %H:%M")

    def _extract_fault_time(self, title: str, description: str) -> tuple[str, str]:
        match = re.search(r"(?:故障|发生|出现|问题|异常)?时间[：:]\s*(.+?)(?:\n|$)", description)
        if match:
            return self._normalize_fault_time_text(match.group(1)), "从缺陷描述提取"
        direct_match = re.search(r"(20\d{2}[-_/年]\d{1,2}[-_/月]\d{1,2}[日_\s-]*\d{1,2}:\d{2}(?::\d{2})?)", description)
        if direct_match:
            return self._normalize_fault_time_text(direct_match.group(1)), "从文本中的完整时间戳提取"
        short_match = re.search(r"(?<!\d)(\d{1,2}:\d{2})(?!\d)", description)
        if short_match:
            return self._normalize_fault_time_text(short_match.group(1)), "从文本中的时分提取"
        title_match = re.search(r"(20\d{2})年_(\d{1,2})月(\d{1,2})日_(\d{1,2}:\d{2})", title)
        if title_match:
            return (
                self._normalize_fault_time_text(
                    f"{title_match.group(1)}-{int(title_match.group(2)):02d}-{int(title_match.group(3)):02d} {title_match.group(4)}"
                ),
                "缺陷描述中未显式提供故障时间，退回使用标题中的时间戳",
            )
        return "", "缺陷描述和标题中都未识别到明确故障时间"

    def _normalize_fault_time_text(self, value: str) -> str:
        normalized = (
            value.strip()
            .replace("：", ":")
            .replace("年", "-")
            .replace("月", "-")
            .replace("日", " ")
            .replace("/", "-")
            .replace("_", " ")
        )
        normalized = re.sub(r"\s+", " ", normalized)
        full_match = re.search(
            r"(20\d{2})-(\d{1,2})-(\d{1,2})\s*(\d{1,2}):(\d{2})(?::(\d{2}))?",
            normalized,
        )
        if full_match:
            seconds = full_match.group(6)
            base = (
                f"{int(full_match.group(1)):04d}-{int(full_match.group(2)):02d}-{int(full_match.group(3)):02d} "
                f"{int(full_match.group(4)):02d}:{int(full_match.group(5)):02d}"
            )
            if seconds is not None:
                return f"{base}:{int(seconds):02d}"
            return base
        short_match = re.search(r"(?<!\d)(\d{1,2}):(\d{2})(?::(\d{2}))?(?!\d)", normalized)
        if short_match:
            seconds = short_match.group(3)
            base = f"{int(short_match.group(1)):02d}:{int(short_match.group(2)):02d}"
            if seconds is not None:
                return f"{base}:{int(seconds):02d}"
            return base
        return normalized

    def _build_direct_analysis_summary(
        self,
        plans: list["BugAnalysisPlan"],
        prompt_text: str,
        html_paths: list[Path],
    ) -> str:
        lines = ["直传文件分析完成", f"描述: {prompt_text}", "报告:"]
        for plan, html_path in zip(plans, html_paths):
            lines.append(f"- {self._analysis_label(plan.kind)}: {html_path}")
        return "\n".join(lines)

    def _render_attachment_lines(self, attachments: object, download: dict[str, object]) -> str:
        downloaded = set(str(name) for name in download.get("downloaded", []) if isinstance(name, str))
        skipped = set(str(name) for name in download.get("skipped", []) if isinstance(name, str))
        errors = set(str(name) for name in download.get("errors", []) if isinstance(name, str))
        error_detail_map = {
            str(item.get("name") or ""): str(item.get("reason") or "")
            for item in download.get("error_details", [])
            if isinstance(item, dict)
        }
        lines: list[str] = []
        if isinstance(attachments, list):
            for item in attachments:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name", ""))
                size = str(item.get("size", ""))
                status = (
                    "已下载"
                    if name in downloaded
                    else "已复用缓存日志"
                    if download.get("reused")
                    else "已跳过"
                    if name in skipped
                    else "下载失败"
                    if name in errors
                    else "未下载"
                )
                reason = error_detail_map.get(name, "")
                suffix = f" ({reason})" if reason and status == "下载失败" else ""
                lines.append(f"  - `{name}` (`{size}`) - {status}{suffix}")
        return "\n".join(lines) if lines else "  - (无附件)"

    def _build_summary_from_report(
        self,
        *,
        plan: "BugAnalysisPlan",
        report_json: Path | None,
        prompt_text: str,
        fault_time: str,
        html_path: Path,
        selected_input: Path | None,
    ) -> str:
        if report_json is None or not report_json.exists():
            log_input = selected_input.name if selected_input else "无日志，静态链路"
            return (
                "Bug 分析完成\n"
                f"类型: {self._analysis_label(plan.kind)}\n"
                f"描述: {prompt_text}\n"
                f"HTML: {html_path}\n"
                f"输入: {log_input}"
            )

        payload = json.loads(report_json.read_text(encoding="utf-8"))
        if plan.kind == "startup":
            verdict = payload.get("verdict", {}) if isinstance(payload, dict) else {}
            message = str(verdict.get("message", ""))
            sessions = payload.get("sessions", []) if isinstance(payload, dict) else []
            target_session = self._select_target_session(sessions, fault_time)
            duration_note = ""
            if target_session is not None:
                message = str(target_session.get("diagnosis", message or "启动时序报告已生成"))
                duration_note = (
                    f"\n故障时间主会话: Session {target_session.get('index', '?')} "
                    f"{target_session.get('start', '')}，状态 {target_session.get('status', '')}"
                )
            mismatch_note = self._time_match_note(fault_time, sessions)
            return (
                "Bug 分析完成\n"
                f"类型: {self._analysis_label(plan.kind)}\n"
                f"结论: {message or '启动时序报告已生成'}\n"
                f"描述: {prompt_text}\n"
                f"HTML: {html_path}\n"
                f"时间窗校验: {mismatch_note}{duration_note}"
            )

        if plan.kind in {"stuck", "crash"}:
            verdict = payload.get("verdict", {}) if isinstance(payload, dict) else {}
            sev = str(verdict.get("verdict_sev", "")).upper()
            msg = str(verdict.get("verdict_msg", ""))
            return (
                "Bug 分析完成\n"
                f"类型: {self._analysis_label(plan.kind)}\n"
                f"结论: [{sev or 'INFO'}] {msg or '已生成卡顿报告'}\n"
                f"描述: {prompt_text}\n"
                f"HTML: {html_path}"
            )

        if plan.kind == "perception":
            summary = payload.get("summary", {}) if isinstance(payload, dict) else {}
            verdict = summary.get("verdict", {}) if isinstance(summary, dict) else {}
            sev = str(verdict.get("sev", "")).upper()
            msg = str(verdict.get("msg", ""))
            return (
                "Bug 分析完成\n"
                f"类型: {self._analysis_label(plan.kind)}\n"
                f"结论: [{sev or 'INFO'}] {msg or '已生成当前感知数据总结'}\n"
                f"描述: {prompt_text}\n"
                f"HTML: {html_path}"
            )

        if plan.kind == "xtheme":
            verdict = payload.get("verdict", {}) if isinstance(payload, dict) else {}
            counts = payload.get("counts", {}) if isinstance(payload, dict) else {}
            issues = payload.get("issues", []) if isinstance(payload, dict) else []
            focus_snapshot = payload.get("focus_snapshot", []) if isinstance(payload, dict) else []
            msg = str(verdict.get("msg", "")) if isinstance(verdict, dict) else ""
            issue_lines: list[str] = []
            if isinstance(issues, list):
                for issue in issues[:3]:
                    if not isinstance(issue, dict):
                        continue
                    title = str(issue.get("title") or "").strip()
                    detail = str(issue.get("detail") or "").strip()
                    if title or detail:
                        issue_lines.append(f"- {title}: {detail}".strip())
            focus_lines: list[str] = []
            if isinstance(focus_snapshot, list):
                for item in focus_snapshot[:5]:
                    if not isinstance(item, dict):
                        continue
                    kind = str(item.get("kind") or "").strip()
                    value = str(item.get("value") or "").strip()
                    ts = str(item.get("ts") or "").strip()
                    source = str(item.get("source") or "").strip()
                    if kind or value:
                        focus_lines.append(f"- {ts} {kind}: {value} [{source}]".strip())
            counts_text = ""
            if isinstance(counts, dict) and counts:
                counts_text = ", ".join(f"{key}={value}" for key, value in counts.items())
            return (
                "Bug 分析完成\n"
                f"类型: {self._analysis_label(plan.kind)}\n"
                f"结论: {msg or '已生成 XTheme 主题链路报告'}\n"
                f"目标时间: {payload.get('target_time') or fault_time or '未识别'}\n"
                f"统计: {counts_text or '无'}\n"
                f"关键问题:\n{chr(10).join(issue_lines) if issue_lines else '- 无明确异常'}\n"
                f"问题时间证据:\n{chr(10).join(focus_lines) if focus_lines else '- 无问题时间快照'}\n"
                f"描述: {prompt_text}\n"
                f"HTML: {html_path}"
            )

        if plan.kind == "scene_signal":
            verdict = str(payload.get("verdict") or "已生成 3D 场景信号报告") if isinstance(payload, dict) else "已生成 3D 场景信号报告"
            latest_chain = payload.get("latest_sr_chain", {}) if isinstance(payload, dict) else {}
            chain_value = str(latest_chain.get("value") or "") if isinstance(latest_chain, dict) else ""
            chain_desc = str(latest_chain.get("value_desc") or "") if isinstance(latest_chain, dict) else ""
            event_count = payload.get("event_count", "") if isinstance(payload, dict) else ""
            return (
                "Bug 分析完成\n"
                f"类型: {self._analysis_label(plan.kind)}\n"
                f"结论: {verdict}\n"
                f"最终 SR 场景: {chain_value or '未命中'} {chain_desc}\n"
                f"事件数: {event_count or '未知'}\n"
                f"描述: {prompt_text}\n"
                f"HTML: {html_path}"
            )

        if _kind_spec(plan.kind).is_agent_handled:
            verdict = payload.get("verdict", {}) if isinstance(payload, dict) else {}
            default_summary = (
                "已生成 LD车道级日志分析报告"
                if plan.kind == "ld_lane_level"
                else "已生成源码分析报告"
                if _kind_spec(plan.kind).is_source_stage
                else "已生成专用 Skill 源码分析入口"
                if plan.kind in _SOURCE_SKILL_KINDS
                else "已生成通用问题分析报告"
            )
            msg = str(verdict.get("text") or payload.get("summary") or default_summary)
            source_matches = payload.get("source_matches", 0) if isinstance(payload, dict) else 0
            skill = str(payload.get("analysis_skill") or self._skill_name_for_kind(plan.kind))
            return (
                "Bug 分析完成\n"
                f"类型: {self._analysis_label(plan.kind)}\n"
                f"Skill: {skill}\n"
                f"结论: {msg}\n"
                f"源码证据: {source_matches}\n"
                f"描述: {prompt_text}\n"
                f"HTML: {html_path}"
            )

        signal = payload.get("signal", {}) if isinstance(payload, dict) else {}
        signal_code = signal.get("code", plan.signal_code or "")
        summary = str(payload.get("summary", ""))
        log_report = payload.get("log_report", {}) if isinstance(payload, dict) else {}
        scanned_files = log_report.get("scanned_files", 0) if isinstance(log_report, dict) else 0
        return (
            "Bug 分析完成\n"
            f"类型: {self._analysis_label(plan.kind)}\n"
            f"信号: {signal_code}\n"
            f"结论: {summary or '已生成信号链路报告'}\n"
            f"扫描文件: {scanned_files}\n"
            f"HTML: {html_path}"
            )

    def _format_log_coverage_for_metadata(self, log_coverage: LogCoverage | None) -> str:
        if log_coverage is None:
            return "未检查"
        if not log_coverage.has_time_evidence:
            return "未识别有效日志时间"
        status = "覆盖问题时间" if log_coverage.covers_fault_time else "未覆盖问题时间"
        return f"{log_coverage.start_time} ~ {log_coverage.end_time}（{status}）"

    def _write_general_bug_report(
        self,
        *,
        html_path: Path,
        json_path: Path,
        title: str,
        description: str,
        prompt_text: str,
        request_text: str,
        fault_time: str,
        selected_input: Path | None,
        source_evidence_path: Path | None,
        classification_skill: str = "",
        classification_source: str = "",
        classification_reason: str = "",
    ) -> None:
        source_entries = self._parse_source_evidence_entries(source_evidence_path)
        source_rows = [
            (entry["file"], f"L{entry['line']}", entry["text"])
            for entry in source_entries[:12]
        ]
        has_logs = selected_input is not None
        verdict_sev = "green" if has_logs else "yellow"
        verdict_text = (
            "未命中专用日志脚本，已按通用问题分析处理；当前结论优先基于缺陷描述、源码证据和已有上下文。"
            if source_rows
            else "未命中专用日志脚本，且当前源码检索证据有限；建议补充更明确的业务关键词或现场日志后继续收敛。"
        )
        cards = [
            ("分析方式", "静态/通用", verdict_sev, "没有把未知请求强制改写成某个固定日志脚本。"),
            ("故障时间", fault_time or "未识别", "green" if fault_time else "yellow", ""),
            ("现场日志", selected_input.name if selected_input else "无，可静态分析", "green" if has_logs else "yellow", ""),
            ("源码证据", str(len(source_rows)), "green" if source_rows else "yellow", "按业务词和源码定义做本地检索。"),
            ("命中 Skill", classification_skill or "general", "green", classification_source or "manual_fallback"),
        ]
        issues = [
            {
                "sev": verdict_sev,
                "title": "路由策略",
                "detail": "当前请求未匹配 startup/stuck/crash/scene_signal/perception/signal 等专用脚本，因此回落到通用问题分析，而不是再默认启动时序。",
            },
            {
                "sev": "yellow" if not has_logs else "green",
                "title": "现场证据",
                "detail": "没有可复用日志时，只能基于缺陷描述和源码证据给出静态判断，不直接替代现场定案。"
                if not has_logs
                else f"当前可复用日志输入：{selected_input}",
            },
            {
                "sev": "green" if source_rows else "yellow",
                "title": "源码落点",
                "detail": f"已命中 {len(source_rows)} 条源码证据，可继续围绕这些文件追踪业务链路。"
                if source_rows
                else "当前没有检索到稳定源码落点，说明问题描述还不够具体。",
            },
        ]
        summary_sections = build_structured_summary_sections(
            conclusions=[
                {"sev": verdict_sev, "title": "通用分析结论", "detail": verdict_text},
                {
                    "sev": "green" if source_rows else "yellow",
                    "title": "源码证据状态",
                    "detail": f"已命中 {len(source_rows)} 条源码证据。" if source_rows else "当前没有命中稳定源码落点。",
                },
                {
                    "sev": "green" if has_logs else "yellow",
                    "title": "现场日志状态",
                    "detail": f"当前可复用日志输入：{selected_input}" if has_logs else "当前无可复用日志，结论不能替代现场定案。",
                },
            ],
            evidence_rows=self._general_summary_evidence_rows(
                fault_time=fault_time,
                selected_input=selected_input,
                source_rows=source_rows,
                classification_skill=classification_skill or "general",
                classification_source=classification_source or "manual_fallback",
            ),
            causes=[
                {
                    "sev": "green" if source_rows else "yellow",
                    "title": "当前可解释方向",
                    "detail": "优先围绕已命中的源码落点和业务描述继续追踪。"
                    if source_rows
                    else "缺少日志和稳定源码证据时，不强行给出具体根因。",
                },
                {
                    "sev": verdict_sev,
                    "title": "路由原因",
                    "detail": classification_reason or "未命中专用日志脚本，按通用分析处理。",
                },
            ],
            confirmations=self._general_summary_confirmations(
                fault_time=fault_time,
                has_logs=has_logs,
                source_rows=source_rows,
            ),
            actions=self._general_summary_actions(has_logs=has_logs, source_rows=source_rows),
        )
        context_rows = [
            ("Bug 标题", title or "未返回 / 未设置"),
            ("分析请求", prompt_text or "未设置"),
            ("故障时间", fault_time or "未识别"),
            ("现场日志", str(selected_input) if selected_input else "无，可静态分析"),
            ("源码证据文件", str(source_evidence_path) if source_evidence_path else "未生成"),
            ("命中 Skill", classification_skill or "general"),
            ("分类来源", classification_source or "manual_fallback"),
            ("分类理由", classification_reason or "未记录"),
        ]
        flow_nodes = [
            {"tag": "REQUEST", "title": "用户问题", "meta": prompt_text or request_text, "note": "原始问题 / 追问文本"},
            {
                "tag": "SKILL",
                "title": classification_skill or "general",
                "meta": classification_source or "manual_fallback",
                "note": classification_reason or "未记录分类理由",
            },
            {
                "tag": "INPUT",
                "title": "现场日志状态",
                "meta": str(selected_input) if selected_input else "无，可静态分析",
                "note": "需要日志的 skill 会在续聊时自动重试下载。",
            },
            {
                "tag": "SOURCE",
                "title": "源码证据",
                "meta": f"{len(source_rows)} 条命中",
                "note": str(source_evidence_path) if source_evidence_path else "未生成源码证据文件",
            },
            {"tag": "OUTPUT", "title": "结论输出", "meta": verdict_text, "note": "最终结论由本地 Agent 继续归纳。"},
        ]
        raw_description = description.strip() or "(无描述)"
        raw_request = request_text.strip() or "(无请求)"
        detail_body = (
            "<div class=\"split-grid\">"
            f"{combined_bug_html.render_table([('原始请求', raw_request)], ('字段', '内容'))}"
            f"{combined_bug_html.render_table([('缺陷描述', raw_description)], ('字段', '内容'))}"
            "</div>"
        )
        composition = ReportComposition(
            title="通用问题分析",
            heading="通用问题分析",
            subtitle=f"Bug 标题：{title or '未返回 / 未设置'}",
            verdict=ReportVerdict(sev=verdict_sev, text=verdict_text),
            cards=cards,
            sections=summary_sections
            + [
                ReportSection(kind="issues", title="当前判断", items=issues, empty_text="未生成判断"),
                ReportSection(kind="flow", title="分析路径", nodes=flow_nodes, empty_text="未生成分析路径"),
                ReportSection(kind="table", title="源码证据", cols=["文件", "行号", "内容"], rows=source_rows, empty_text="未命中源码证据"),
                ReportSection(kind="table", title="分析上下文", cols=["字段", "内容"], rows=context_rows),
                ReportSection(kind="details", title="原始输入", summary="展开查看请求与缺陷描述", body_html=detail_body),
            ],
        )
        payload = {
            "mode": "general_bug_overview",
            "summary": verdict_text,
            "verdict": {"sev": verdict_sev, "text": verdict_text},
            "fault_time": fault_time,
            "selected_input": str(selected_input) if selected_input else "",
            "source_evidence_file": str(source_evidence_path) if source_evidence_path else "",
            "source_matches": len(source_rows),
            "analysis_skill": classification_skill or "general",
            "classification_source": classification_source or "manual_fallback",
            "classification_reason": classification_reason or "",
            "title": title,
            "prompt_text": prompt_text,
            "request_text": request_text,
            "description": raw_description,
        }
        html_path.write_text(
            combined_bug_html.render_report_shell(**composition_to_renderer_payload(composition)),
            encoding="utf-8",
        )
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _write_custom_skill_bug_report(
        self,
        *,
        html_path: Path,
        json_path: Path,
        title: str,
        description: str,
        prompt_text: str,
        request_text: str,
        fault_time: str,
        selected_input: Path | None,
        source_evidence_path: Path | None,
        classification_skill: str,
        classification_source: str = "",
        classification_reason: str = "",
    ) -> None:
        skill_paths = self._skill_context_paths(classification_skill)
        skill_md = next((path for path in skill_paths if path.name == "SKILL.md"), None)
        source_entries = self._parse_source_evidence_entries(source_evidence_path)
        source_rows = [(entry["file"], f"L{entry['line']}", entry["text"]) for entry in source_entries[:8]]
        has_logs = selected_input is not None
        is_source_analysis = classification_skill.strip() == "source_analysis"
        verdict_sev = "green" if has_logs and (skill_md is not None or is_source_analysis) else "yellow"
        verdict_text = (
            "已识别为源码导向文件分析，本轮将由本地 Agent 基于日志、源码证据和用户给出的源码线索整理最终结论。"
            if is_source_analysis
            else f"已命中专用 Skill `{classification_skill}`，本轮将由本地 Agent 按 Skill 规范读取源码/证据并产出最终结论。"
            if skill_md is not None
            else f"已命中专用 Skill `{classification_skill}`，但未找到 SKILL.md，当前只能保留材料索引并等待补齐 skill。"
        )
        cards = [
            ("分析方式", "源码导向文件分析 + Agent" if is_source_analysis else "专用 Skill 源码分析 + Agent", verdict_sev, "没有回落为通用问题分析。"),
            ("命中 Skill", classification_skill or "未记录", "green" if classification_skill else "yellow", classification_source or "manual_fallback"),
            ("故障时间", fault_time or "未识别", "green" if fault_time else "yellow", ""),
            ("现场日志", selected_input.name if selected_input else "无", "green" if has_logs else "yellow", str(selected_input or "")),
            ("Skill 规范", skill_md.name if skill_md else "内部源码导向规则" if is_source_analysis else "缺失", "green" if (skill_md or is_source_analysis) else "yellow", str(skill_md or "")),
            ("源码证据", str(len(source_rows)), "green" if source_rows else "yellow", "按用户诉求预检索。"),
        ]
        summary_sections = build_structured_summary_sections(
            conclusions=[
                {"sev": verdict_sev, "title": "专用 Skill 已命中", "detail": verdict_text},
                {
                    "sev": "green" if has_logs else "yellow",
                    "title": "日志输入",
                    "detail": f"当前可复用日志输入：{selected_input}" if has_logs else "当前没有可复用日志，无法执行日志型专用 Skill。",
                },
                {
                    "sev": "green" if (skill_md or is_source_analysis) else "yellow",
                    "title": "Skill 规范",
                    "detail": "当前使用内部源码导向分析路径，不要求匹配现有 SKILL.md。"
                    if is_source_analysis
                    else f"Agent 需要先读取 `{skill_md}` 并按其 Required Workflow 执行。"
                    if skill_md
                    else "缺少 SKILL.md。",
                },
            ],
            evidence_rows=[
                ("分类路由", classification_skill or "未记录", "bridge/agent", classification_source or "manual_fallback", classification_reason or "未记录"),
                ("故障时间", fault_time or "未识别", "用户输入/标题/描述", "用于限定日志窗口", ""),
                ("日志输入", str(selected_input or "无"), "附件/缓存", "专用 Skill 的主要运行材料", ""),
                ("Skill 文件", str(skill_md or "内部源码导向规则"), "workspace/.ai/skills", "Agent 分析规范入口", ""),
            ],
            causes=[
                {
                    "sev": "yellow",
                    "title": "脚本覆盖",
                    "detail": "当前路径没有 Bridge 内置业务脚本结论，最终质量依赖本地 Agent 对日志、源码证据和用户线索的综合整理。"
                    if is_source_analysis
                    else "该 Skill 当前没有 Bridge 内置 Python 执行器，因此报告主体依赖本地 Agent 按 SKILL.md 做只读分析。",
                }
            ],
            confirmations=[
                {
                    "sev": "yellow",
                    "title": "最终结论",
                    "detail": "需要等待 Agent 读取日志/源码后写入；如果 Agent 超时，报告会明确标注超时而不是伪装成通用根因。",
                }
            ],
            actions=[
                {"sev": "green", "title": "按专用 Skill 继续", "detail": "后续追问会复用当前 bug、已下载日志、Skill 规范和 Agent 会话。"}
            ],
        )
        skill_rows = [(path.name, str(path)) for path in skill_paths]
        context_rows = [
            ("Bug 标题", title or "未返回 / 未设置"),
            ("分析请求", prompt_text or "未设置"),
            ("故障时间", fault_time or "未识别"),
            ("现场日志", str(selected_input) if selected_input else "无"),
            ("源码证据文件", str(source_evidence_path) if source_evidence_path else "未生成"),
            ("分类理由", classification_reason or "未记录"),
        ]
        detail_body = (
            "<div class=\"split-grid\">"
            f"{combined_bug_html.render_table([('原始请求', request_text.strip() or '(无请求)')], ('字段', '内容'))}"
            f"{combined_bug_html.render_table([('缺陷描述', description.strip() or '(无描述)')], ('字段', '内容'))}"
            "</div>"
        )
        composition = ReportComposition(
            title="专用 Skill 源码分析",
            heading="专用 Skill 源码分析",
            subtitle=f"Bug 标题：{title or '未返回 / 未设置'}",
            verdict=ReportVerdict(sev=verdict_sev, text=verdict_text),
            cards=cards,
            sections=summary_sections
            + [
                ReportSection(kind="table", title="Skill 输入", cols=["文件", "路径"], rows=skill_rows, empty_text="未找到 Skill 文件"),
                ReportSection(kind="table", title="源码预检索", cols=["文件", "行号", "内容"], rows=source_rows, empty_text="未命中源码预检索证据"),
                ReportSection(kind="table", title="分析上下文", cols=["字段", "内容"], rows=context_rows),
                ReportSection(kind="details", title="原始输入", summary="展开查看请求与缺陷描述", body_html=detail_body),
            ],
        )
        payload = {
            "mode": "custom_skill_overview",
            "summary": verdict_text,
            "verdict": {"sev": verdict_sev, "text": verdict_text},
            "fault_time": fault_time,
            "selected_input": str(selected_input) if selected_input else "",
            "source_evidence_file": str(source_evidence_path) if source_evidence_path else "",
            "source_matches": len(source_rows),
            "analysis_skill": classification_skill,
            "classification_source": classification_source or "manual_fallback",
            "classification_reason": classification_reason or "",
            "skill_context_files": [str(path) for path in skill_paths],
            "title": title,
            "prompt_text": prompt_text,
            "request_text": request_text,
            "description": description.strip(),
        }
        html_path.write_text(
            combined_bug_html.render_report_shell(**composition_to_renderer_payload(composition)),
            encoding="utf-8",
        )
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _general_summary_evidence_rows(
        self,
        *,
        fault_time: str,
        selected_input: Path | None,
        source_rows: list[tuple[object, ...]],
        classification_skill: str,
        classification_source: str,
    ) -> list[tuple[object, ...]]:
        rows: list[tuple[object, ...]] = []
        if fault_time:
            rows.append(("故障时间", fault_time, "用户请求/缺陷描述", "已识别分析时间点", "限定后续日志和源码追踪窗口"))
        rows.append(
            (
                "现场日志",
                str(selected_input) if selected_input else "无",
                "附件/缓存",
                "已取得可复用日志输入" if selected_input else "当前没有可复用日志输入",
                "决定结论置信边界",
            )
        )
        rows.append(("分类路由", classification_skill or "general", "bridge/agent", classification_source or "manual_fallback", "决定是否调用专用 skill"))
        for file_name, line, text in source_rows[:5]:
            rows.append(("源码", f"{file_name} {line}".strip(), "业务源码", text, "支撑静态分析落点"))
        return rows[:8]

    def _general_summary_confirmations(
        self,
        *,
        fault_time: str,
        has_logs: bool,
        source_rows: list[tuple[object, ...]],
    ) -> list[dict[str, object]]:
        items: list[dict[str, object]] = []
        if not fault_time:
            items.append({"sev": "yellow", "title": "故障时间", "detail": "未识别精确时间，后续日志分析需要先补齐时间窗口。"})
        if not has_logs:
            items.append({"sev": "yellow", "title": "现场日志", "detail": "当前无可复用日志，无法验证运行时是否真的经过源码落点。"})
        if not source_rows:
            items.append({"sev": "yellow", "title": "源码落点", "detail": "源码证据不足，需要更明确的业务词、信号名、类名或调用链线索。"})
        return items

    def _general_summary_actions(
        self,
        *,
        has_logs: bool,
        source_rows: list[tuple[object, ...]],
    ) -> list[dict[str, object]]:
        actions: list[dict[str, object]] = []
        if not has_logs:
            actions.append({"sev": "yellow", "title": "补齐或重试日志", "detail": "下一轮如果命中需要日志的 skill，bridge 会优先重试下载并报告失败原因。"})
        if source_rows:
            actions.append({"sev": "green", "title": "沿源码证据追踪", "detail": "优先从命中的源码文件继续查生产者、状态更新和消费链路。"})
        actions.append({"sev": "green", "title": "续聊时保留上下文", "detail": "后续追问继续复用当前 bug、报告、源码证据和已下载日志缓存。"})
        return actions

    def _build_combined_report_artifacts(
        self,
        *,
        plans: list["BugAnalysisPlan"],
        prompt_text: str,
        fault_time: str,
        output_dir: Path,
        html_paths: list[Path],
        report_jsons: dict[str, Path | None],
        selected_input: Path | None,
        source_evidence_path: Path | None = None,
    ) -> dict[str, object] | None:
        kinds = [plan.kind for plan in plans]
        if kinds == ["startup", "stuck"]:
            startup_json_path = report_jsons.get("startup")
            stuck_json_path = report_jsons.get("stuck")
            if startup_json_path is None or stuck_json_path is None:
                return None
            if not startup_json_path.exists() or not stuck_json_path.exists():
                return None

            startup_payload = json.loads(startup_json_path.read_text(encoding="utf-8"))
            stuck_payload = json.loads(stuck_json_path.read_text(encoding="utf-8"))
            summary = self._build_combined_summary_text(startup_payload, stuck_payload, prompt_text, fault_time)
            html_path = output_dir / self._combined_report_name("html")
            json_path = output_dir / self._combined_report_name("json")
            html_path.write_text(
                self._render_combined_startup_stuck_html(
                    startup_payload=startup_payload,
                    stuck_payload=stuck_payload,
                    prompt_text=prompt_text,
                    fault_time=fault_time,
                    startup_html=next((path for path in html_paths if path.name == self._report_name("startup", "html")), None),
                    stuck_html=next((path for path in html_paths if path.name == self._report_name("stuck", "html")), None),
                    selected_input=selected_input,
                ),
                encoding="utf-8",
            )
            combined_payload = {
                "mode": "startup_stuck_combined",
                "prompt_text": prompt_text,
                "fault_time": fault_time,
                "selected_input": str(selected_input) if selected_input else "",
                "summary": summary,
                "startup": startup_payload,
                "stuck": stuck_payload,
            }
            json_path.write_text(json.dumps(combined_payload, ensure_ascii=False, indent=2), encoding="utf-8")
            return {
                "html_path": html_path,
                "json_path": json_path,
                "summary": summary,
            }

        if kinds == ["signal"]:
            signal_json_path = report_jsons.get("signal")
            if signal_json_path is None or not signal_json_path.exists():
                return None
            signal_payload = json.loads(signal_json_path.read_text(encoding="utf-8"))
            focus_scope = self._signal_focus_scope(signal_payload, fault_time)
            android_data_link = self._signal_android_data_link_checks(
                signal_payload,
                focus_scope,
                selected_input=selected_input,
                source_evidence_path=source_evidence_path,
            )
            summary = self._build_signal_overview_summary_text(
                signal_payload,
                prompt_text,
                fault_time,
                focus_scope,
                android_data_link=android_data_link,
            )
            signal_overview_payload = dict(signal_payload)
            signal_overview_payload["summary"] = self._signal_scoped_summary_text(signal_payload)
            html_path = output_dir / self._signal_overview_report_name("html")
            json_path = output_dir / self._signal_overview_report_name("json")
            html_path.write_text(
                self._render_signal_bug_overview_html(
                    signal_payload=signal_payload,
                    prompt_text=prompt_text,
                    fault_time=fault_time,
                    selected_input=selected_input,
                    source_evidence_path=source_evidence_path,
                    focus_scope=focus_scope,
                    android_data_link=android_data_link,
                ),
                encoding="utf-8",
            )
            json_path.write_text(
                json.dumps(
                    {
                        "mode": "signal_overview_combined",
                        "prompt_text": prompt_text,
                        "fault_time": fault_time,
                        "selected_input": str(selected_input) if selected_input else "",
                        "source_evidence_file": str(source_evidence_path) if source_evidence_path else "",
                        "summary": summary,
                        "focus_scope": focus_scope,
                        "android_data_link": android_data_link,
                        "signal": signal_overview_payload,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            return {
                "html_path": html_path,
                "json_path": json_path,
                "summary": summary,
            }

        return None

    def _build_combined_summary_text(
        self,
        startup_payload: dict[str, object],
        stuck_payload: dict[str, object],
        prompt_text: str,
        fault_time: str,
    ) -> str:
        startup_verdict = startup_payload.get("verdict", {}) if isinstance(startup_payload, dict) else {}
        stuck_target_verdict = stuck_payload.get("target_verdict", {}) if isinstance(stuck_payload, dict) else {}
        stuck_verdict = stuck_payload.get("verdict", {}) if isinstance(stuck_payload, dict) else {}
        focus_pid = startup_payload.get("focus_session_pid", "")
        boot_relation = startup_payload.get("boot_relation", {}) if isinstance(startup_payload, dict) else {}
        system_load = startup_payload.get("system_load", {}) if isinstance(startup_payload, dict) else {}
        startup_message = str(startup_verdict.get("message", "已生成启动分析"))
        if isinstance(stuck_target_verdict, dict) and stuck_target_verdict.get("message"):
            stuck_message = str(stuck_target_verdict.get("message"))
        else:
            stuck_message = str(stuck_verdict.get("verdict_msg", "已生成卡顿分析"))
        load_text = "未命中"
        if isinstance(system_load, dict) and system_load:
            load_text = (
                f"Total {system_load.get('total_cpu', '?')}% / "
                f"System {system_load.get('system_cpu', '?')}% / "
                f"iow {system_load.get('iow_cpu', '?')}%"
            )
        return (
            "Bug 分析完成\n"
            "类型: 3D启动卡顿综合报告\n"
            f"描述: {prompt_text}\n"
            f"故障时间: {fault_time or '未识别'}\n"
            f"主会话 PID: {focus_pid or '未识别'}\n"
            f"启动结论: {startup_message}\n"
            f"卡顿结论: {stuck_message}\n"
            f"ROM/Boot: {boot_relation.get('note', '未识别')}\n"
            f"启动时刻系统负载: {load_text}\n"
            f"HTML: {self._combined_report_name('html')}"
        )

    def _render_combined_startup_stuck_html(
        self,
        *,
        startup_payload: dict[str, object],
        stuck_payload: dict[str, object],
        prompt_text: str,
        fault_time: str,
        startup_html: Path | None,
        stuck_html: Path | None,
        selected_input: Path | None,
    ) -> str:
        composition = self._plan_startup_stuck_report(
            startup_payload=startup_payload,
            stuck_payload=stuck_payload,
            prompt_text=prompt_text,
            fault_time=fault_time,
            startup_html=startup_html,
            stuck_html=stuck_html,
            selected_input=selected_input,
        )
        return combined_bug_html.render_report_shell(**composition_to_renderer_payload(composition))

    def _combined_ig_text(self, ig_context: object, power_context: object) -> str:
        after = ig_context.get("after") if isinstance(ig_context, dict) else None
        if isinstance(after, dict) and after.get("timestamp") and after.get("value") is not None:
            return f"启动后最近 IG={after.get('value')} @ {after.get('timestamp')}"
        render_ctx = power_context.get("render_anomaly_context", {}) if isinstance(power_context, dict) else {}
        if isinstance(render_ctx, dict) and render_ctx.get("category"):
            return f"卡顿侧上下电分类={render_ctx.get('category')}"
        return "未识别到明确上下电样本"

    def _combined_stuck_context_text(
        self,
        stuck_payload: dict[str, object],
        target_context: object,
        app_pid_filter: object,
    ) -> str:
        stuck_verdict = stuck_payload.get("verdict", {}) if isinstance(stuck_payload, dict) else {}
        target_verdict = stuck_payload.get("target_verdict", {}) if isinstance(stuck_payload, dict) else {}
        pid_desc = ""
        if isinstance(app_pid_filter, dict) and app_pid_filter.get("selected_pid"):
            pid_desc = f"应用层 PID={app_pid_filter.get('selected_pid')}；"
        if isinstance(target_context, dict) and target_context.get("target"):
            pid_desc += f"目标时间窗={target_context.get('target')}；"
        message = (
            str(target_verdict.get("message"))
            if isinstance(target_verdict, dict) and target_verdict.get("message")
            else str(stuck_verdict.get("verdict_msg", "已生成卡顿报告"))
        )
        return pid_desc + message

    def _plan_startup_stuck_report(
        self,
        *,
        startup_payload: dict[str, object],
        stuck_payload: dict[str, object],
        prompt_text: str,
        fault_time: str,
        startup_html: Path | None,
        stuck_html: Path | None,
        selected_input: Path | None,
    ) -> ReportComposition:
        startup_verdict = startup_payload.get("verdict", {}) if isinstance(startup_payload, dict) else {}
        stuck_target_verdict = stuck_payload.get("target_verdict", {}) if isinstance(stuck_payload, dict) else {}
        stuck_verdict = stuck_payload.get("verdict", {}) if isinstance(stuck_payload, dict) else {}
        startup_message = str(startup_verdict.get("message", "已生成启动分析"))
        startup_sev = str(startup_verdict.get("severity", "yellow"))
        stuck_message = (
            str(stuck_target_verdict.get("message"))
            if isinstance(stuck_target_verdict, dict) and stuck_target_verdict.get("message")
            else str(stuck_verdict.get("verdict_msg", "已生成卡顿分析"))
        )
        stuck_sev = (
            str(stuck_target_verdict.get("sev", "yellow"))
            if isinstance(stuck_target_verdict, dict) and stuck_target_verdict.get("sev")
            else str(stuck_verdict.get("verdict_sev", "yellow"))
        )
        focus_pid = startup_payload.get("focus_session_pid", "")
        focus_session_index = startup_payload.get("focus_session_index", "")
        boot_relation = startup_payload.get("boot_relation", {}) if isinstance(startup_payload, dict) else {}
        system_load = startup_payload.get("system_load", {}) if isinstance(startup_payload, dict) else {}
        ig_context = startup_payload.get("ig_context", {}) if isinstance(startup_payload, dict) else {}
        power_context = stuck_payload.get("power_context", {}) if isinstance(stuck_payload, dict) else {}
        app_pid_filter = stuck_payload.get("app_pid_filter", {}) if isinstance(stuck_payload, dict) else {}
        target_context = stuck_payload.get("target_context", {}) if isinstance(stuck_payload, dict) else {}
        cards = [
            ("分析类型", "3D启动卡顿综合", "green", "启动链路与卡顿窗口合并输出"),
            ("故障时间", fault_time or "未识别", "green" if fault_time else "yellow", ""),
            ("主会话 PID", focus_pid or "未识别", "green" if focus_pid else "yellow", f"Session {focus_session_index or '?'}"),
            ("ROM 启动邻近", "是" if boot_relation.get("is_near_boot") else "否", "yellow" if boot_relation.get("is_near_boot") else "green", str(boot_relation.get("note", ""))),
            (
                "启动时刻系统负载",
                (
                    f"Total {system_load.get('total_cpu', '?')}% / iow {system_load.get('iow_cpu', '?')}%"
                    if isinstance(system_load, dict) and system_load
                    else "未命中"
                ),
                "yellow" if isinstance(system_load, dict) and int(system_load.get("total_cpu", 0) or 0) >= 70 else "green",
                (
                    f"进程 CPU {system_load.get('process_cpu', '?')}% / RSS {system_load.get('process_mem_rss_kb', '?')}KB"
                    if isinstance(system_load, dict) and system_load
                    else ""
                ),
            ),
            ("启动链路", startup_sev.upper(), startup_sev, startup_message),
            ("卡顿窗口", stuck_sev.upper(), stuck_sev, stuck_message),
        ]
        issues: list[dict[str, object]] = []
        for item in startup_verdict.get("issues", []) if isinstance(startup_verdict, dict) else []:
            if isinstance(item, dict):
                issues.append(item)
        if isinstance(stuck_target_verdict, dict) and stuck_target_verdict.get("message"):
            issues.append({"sev": stuck_sev, "title": "目标时间窗卡顿结论", "detail": stuck_message})
        elif isinstance(stuck_verdict, dict) and stuck_verdict.get("verdict_msg"):
            issues.append({"sev": stuck_sev, "title": "卡顿结论", "detail": stuck_message})
        chain_nodes = [
            {
                "sev": "green" if focus_pid else "yellow",
                "title": "目标时间锁主会话与主 PID",
                "evidence": f"故障时间 {fault_time or '未识别'} -> Session {focus_session_index or '?'} / PID {focus_pid or '未识别'}",
                "downstream": "后续启动链与卡顿证据统一围绕同一主会话展开，避免把恢复后的新进程混入。",
            },
            {
                "sev": "yellow" if boot_relation.get("is_near_boot") else "green",
                "title": "ROM 启动邻近与上下电上下文",
                "evidence": (
                    str(boot_relation.get("note", "未识别"))
                    + "；"
                    + self._combined_ig_text(ig_context, power_context)
                ),
                "downstream": "如果问题发生在整机刚启动或特殊上下电阶段，启动卡顿结论需要附带环境说明，避免误判稳定期异常。",
            },
            {
                "sev": startup_sev,
                "title": "启动链路主卡点",
                "evidence": startup_message,
                "downstream": "用于判断 Application / Surface / UnityReady / 首帧 哪一段真正断开。",
            },
            {
                "sev": stuck_sev,
                "title": "卡顿窗口系统与渲染压力",
                "evidence": self._combined_stuck_context_text(stuck_payload, target_context, app_pid_filter),
                "downstream": "补充目标时间窗内的 Watchdog / UnityRequest / 系统 iow / CPU 压力，判断是不是启动后继续卡住。",
            },
        ]
        target_rows = [
            ("故障时间", fault_time or "未识别"),
            ("主会话 PID", str(focus_pid or "未识别")),
            ("会话选择", str(startup_payload.get("focus_reason", "未记录"))),
            ("启动报告", startup_html.name if startup_html else self._report_name("startup", "html")),
            ("卡顿报告", stuck_html.name if stuck_html else self._report_name("stuck", "html")),
            ("原始日志输入", str(selected_input or "")),
        ]
        system_rows = []
        if isinstance(system_load, dict) and system_load:
            system_rows.append(
                (
                    system_load.get("timestamp", ""),
                    f"{system_load.get('total_cpu', '?')}%",
                    f"{system_load.get('user_cpu', '?')}%",
                    f"{system_load.get('system_cpu', '?')}%",
                    f"{system_load.get('iow_cpu', '?')}%",
                    f"{system_load.get('process_cpu', '?')}%",
                )
            )
        return plan_startup_stuck_report(
            prompt_text=combined_bug_html.H(prompt_text),
            fault_time=fault_time,
            startup_html_name=combined_bug_html.H(startup_html or self._report_name("startup", "html")),
            stuck_html_name=combined_bug_html.H(stuck_html or self._report_name("stuck", "html")),
            selected_input=str(selected_input or ""),
            startup_message=startup_message,
            startup_sev=startup_sev,
            stuck_message=stuck_message,
            stuck_sev=stuck_sev,
            focus_pid=str(focus_pid or ""),
            focus_session_index=str(focus_session_index or ""),
            boot_relation_is_near_boot=bool(boot_relation.get("is_near_boot")),
            boot_relation_note=str(boot_relation.get("note", "")),
            startup_load_value=(
                f"Total {system_load.get('total_cpu', '?')}% / iow {system_load.get('iow_cpu', '?')}%"
                if isinstance(system_load, dict) and system_load
                else "未命中"
            ),
            startup_load_desc=(
                f"进程 CPU {system_load.get('process_cpu', '?')}% / RSS {system_load.get('process_mem_rss_kb', '?')}KB"
                if isinstance(system_load, dict) and system_load
                else ""
            ),
            startup_load_sev="yellow" if isinstance(system_load, dict) and int(system_load.get("total_cpu", 0) or 0) >= 70 else "green",
            issues=issues,
            chain_nodes=chain_nodes,
            target_rows=target_rows,
            system_rows=system_rows,
            system_cols=["时间", "Total", "User", "System", "iow", "进程 CPU"],
        )

    def _plan_signal_report(
        self,
        *,
        signal_payload: dict[str, object],
        prompt_text: str,
        fault_time: str,
        selected_input: Path | None,
        source_evidence_path: Path | None,
        focus_scope: dict[str, object],
        android_data_link: list[dict[str, object]] | None = None,
    ) -> ReportComposition:
        signal = signal_payload.get("signal", {}) if isinstance(signal_payload, dict) else {}
        signal_name = str(signal.get("name") or signal.get("code") or "未知信号")
        signal_code = str(signal.get("code") or "")
        signal_comment = str(signal.get("comment") or "")
        package = str(focus_scope.get("package") or "未识别")
        pid = str(focus_scope.get("pid") or "未识别")
        time_window = focus_scope.get("time_window", {}) if isinstance(focus_scope, dict) else {}
        alignment = self._signal_fault_alignment(fault_time, focus_scope)
        lifecycle_nodes = self._signal_lifecycle_nodes(signal_payload, focus_scope, fault_time, source_evidence_path)
        dataflow_nodes = self._signal_dataflow_nodes(signal_payload, prompt_text, source_evidence_path)
        evidence_rows = self._signal_evidence_rows(signal_payload, focus_scope)
        boundary_issues = self._signal_boundary_issues(signal_payload, focus_scope, fault_time)
        source_rows = self._signal_source_rows(signal_payload, source_evidence_path)
        summary_text = self._signal_scoped_summary_text(signal_payload)
        visible_scope = self._signal_visible_scope_text(signal_payload, focus_scope)
        verdict_sev = alignment["sev"]
        if verdict_sev == "green" and any(issue.get("sev") == "yellow" for issue in boundary_issues):
            verdict_sev = "yellow"
        cards = [
            ("信号", signal_code or signal_name, "green", signal_name if signal_code else signal_comment),
            ("焦点进程", package, "green" if package != "未识别" else "yellow", f"PID {pid}" if pid != "未识别" else ""),
            ("日志时间窗", str(time_window.get("display") or "未识别"), "green" if time_window else "yellow", ""),
            ("现场一致性", alignment["label"], alignment["sev"], alignment["detail"]),
            ("进程内可见性", visible_scope, "green" if "已看到" in visible_scope else "yellow", ""),
            ("原始输入", str(selected_input or "无"), "green" if selected_input else "yellow", ""),
        ]
        title_suffix = signal_comment or signal_name
        composition = plan_signal_report(
            title_suffix=title_suffix,
            prompt_text=combined_bug_html.H(prompt_text),
            raw_signal_report_name=combined_bug_html.H(self._report_name("signal", "html")),
            verdict_sev=verdict_sev,
            verdict_text=alignment["headline"],
            judgement_text=alignment["judgement"],
            cards=cards,
            lifecycle_nodes=lifecycle_nodes,
            dataflow_nodes=dataflow_nodes,
            evidence_rows=evidence_rows,
            boundary_issues=boundary_issues,
            summary_text=summary_text,
            source_rows=source_rows,
            render_summary_html=lambda summary: f'<div class="insight" style="margin-top:12px">{combined_bug_html.H(summary)}</div>',
            render_source_rows_html=lambda rows: combined_bug_html.render_table(
                rows,
                ["层级", "位置", "说明"],
                empty_text="未提取到额外源码引用",
            ),
        )
        if android_data_link:
            self._signal_merge_android_conclusion(composition, android_data_link)
            self._signal_insert_sections_after(
                composition,
                title="结论摘要",
                sections=self._signal_android_data_link_sections(android_data_link),
            )
        return composition

    def _signal_overview_report_name(self, suffix: str) -> str:
        return f"bug_signal_overview_report.{suffix}"

    def _build_signal_overview_summary_text(
        self,
        signal_payload: dict[str, object],
        prompt_text: str,
        fault_time: str,
        focus_scope: dict[str, object],
        android_data_link: list[dict[str, object]] | None = None,
    ) -> str:
        signal = signal_payload.get("signal", {}) if isinstance(signal_payload, dict) else {}
        signal_name = str(signal.get("name") or signal.get("code") or "未知信号")
        signal_code = str(signal.get("code") or "")
        package = str(focus_scope.get("package") or "未识别")
        pid = str(focus_scope.get("pid") or "未识别")
        time_window = focus_scope.get("time_window", {}) if isinstance(focus_scope, dict) else {}
        start = str(time_window.get("start") or "")
        end = str(time_window.get("end") or "")
        alignment = self._signal_fault_alignment(fault_time, focus_scope)
        boundary = self._signal_boundary_issues(signal_payload, focus_scope, fault_time)
        top_issue = boundary[0]["detail"] if boundary else "已生成信号链路总览报告。"
        likely_cause = self._signal_android_likely_cause(android_data_link or [])
        if likely_cause:
            top_issue = likely_cause
        coverage_note = self._signal_coverage_note(signal_payload, focus_scope)
        time_note = "未识别"
        if start and end:
            time_note = f"{start} ~ {end}"
        elif start:
            time_note = start
        return (
            "Bug 分析完成\n"
            "类型: 信号链路总览报告\n"
            f"描述: {prompt_text}\n"
            f"信号: {signal_code or '-'} {signal_name}\n"
            f"焦点进程: {package} / PID {pid}\n"
            f"日志时间窗: {time_note}\n"
            f"现场一致性: {alignment['label']}\n"
            f"当前判断: {top_issue}\n"
            f"进程内链路: {coverage_note}\n"
            f"HTML: {self._signal_overview_report_name('html')}"
        )

    def _render_signal_bug_overview_html(
        self,
        *,
        signal_payload: dict[str, object],
        prompt_text: str,
        fault_time: str,
        selected_input: Path | None,
        source_evidence_path: Path | None,
        focus_scope: dict[str, object],
        android_data_link: list[dict[str, object]] | None = None,
    ) -> str:
        composition = self._plan_signal_report(
            signal_payload=signal_payload,
            prompt_text=prompt_text,
            fault_time=fault_time,
            selected_input=selected_input,
            source_evidence_path=source_evidence_path,
            focus_scope=focus_scope,
            android_data_link=android_data_link or [],
        )
        return combined_bug_html.render_report_shell(**composition_to_renderer_payload(composition))

    def _signal_focus_scope(self, signal_payload: dict[str, object], fault_time: str = "") -> dict[str, object]:
        evidence_items = self._signal_evidence_items(signal_payload)
        reference_date = self._signal_reference_date(evidence_items)
        target_items = [
            item
            for item in evidence_items
            if str(item.get("stage") or "") != "lifecycle" and self._signal_item_matches_target(signal_payload, item)
        ]
        scoped_items = target_items or evidence_items
        fault_dt = self._parse_bug_datetime(fault_time)
        nearest: tuple[float, tuple[str, str]] | None = None
        if fault_dt is not None:
            for item in scoped_items:
                package = self._signal_package_from_path(str(item.get("file") or ""))
                pid = self._signal_pid_from_text(str(item.get("text") or ""))
                if not package and not pid:
                    continue
                timestamp = self._signal_parse_datetime(
                    text=str(item.get("text") or ""),
                    time_text=str(item.get("time") or ""),
                    reference_date=reference_date,
                )
                if timestamp is None:
                    continue
                distance = abs((timestamp - fault_dt).total_seconds())
                key = (package, pid)
                if nearest is None or distance < nearest[0]:
                    nearest = (distance, key)
        counts: dict[tuple[str, str], int] = {}
        for item in scoped_items:
            package = self._signal_package_from_path(str(item.get("file") or ""))
            pid = self._signal_pid_from_text(str(item.get("text") or ""))
            if not package and not pid:
                continue
            counts[(package, pid)] = counts.get((package, pid), 0) + 1
        package = ""
        pid = ""
        if nearest is not None:
            package, pid = nearest[1]
        elif counts:
            (package, pid), _ = sorted(
                counts.items(),
                key=lambda item: (item[1], bool(item[0][0]), bool(item[0][1]), item[0][0], item[0][1]),
                reverse=True,
            )[0]
        time_window = self._signal_time_window(scoped_items, reference_date, package, pid)
        return {
            "package": package,
            "pid": pid,
            "reference_date": reference_date,
            "time_window": time_window,
        }

    def _signal_evidence_items(self, signal_payload: dict[str, object]) -> list[dict[str, object]]:
        items: list[dict[str, object]] = []
        lifecycle = signal_payload.get("lifecycle_report", {}) if isinstance(signal_payload, dict) else {}
        runtime = lifecycle.get("runtime", {}) if isinstance(lifecycle, dict) else {}
        for event in runtime.get("events", []) if isinstance(runtime, dict) else []:
            if not isinstance(event, dict):
                continue
            items.append(
                {
                    "stage": "lifecycle",
                    "title": str(event.get("label") or "生命周期"),
                    "file": str(event.get("file") or ""),
                    "line": event.get("line"),
                    "text": str(event.get("text") or ""),
                    "time": str(event.get("time") or ""),
                    "delta": str(event.get("delta") or ""),
                }
            )
        log_report = signal_payload.get("log_report", {}) if isinstance(signal_payload, dict) else {}
        stages = log_report.get("stages", {}) if isinstance(log_report, dict) else {}
        for stage_key, stage_payload in stages.items():
            if not isinstance(stage_payload, dict):
                continue
            title = str(stage_payload.get("title") or stage_key)
            for example in stage_payload.get("examples", []) or []:
                if not isinstance(example, dict):
                    continue
                items.append(
                    {
                        "stage": str(stage_key),
                        "title": title,
                        "code": str(example.get("code") or ""),
                        "file": str(example.get("file") or ""),
                        "line": example.get("line"),
                        "text": str(example.get("text") or ""),
                    }
                )
        return items

    def _signal_reference_date(self, evidence_items: list[dict[str, object]]) -> str:
        for item in evidence_items:
            for value in (str(item.get("text") or ""), str(item.get("file") or ""), str(item.get("time") or "")):
                match = re.search(r"(20\d{2}-\d{2}-\d{2})", value)
                if match:
                    return match.group(1)
        return ""

    def _signal_time_window(
        self,
        evidence_items: list[dict[str, object]],
        reference_date: str,
        package: str,
        pid: str,
    ) -> dict[str, object]:
        matched: list[datetime] = []
        for item in evidence_items:
            if package:
                item_package = self._signal_package_from_path(str(item.get("file") or ""))
                if item_package and item_package != package:
                    continue
            if pid:
                item_pid = self._signal_pid_from_text(str(item.get("text") or ""))
                if item_pid and item_pid != pid:
                    continue
            timestamp = self._signal_parse_datetime(
                text=str(item.get("text") or ""),
                time_text=str(item.get("time") or ""),
                reference_date=reference_date,
            )
            if timestamp is not None:
                matched.append(timestamp)
        if not matched:
            return {}
        matched.sort()
        start = matched[0]
        end = matched[-1]
        display = self._signal_format_datetime(start)
        if end != start:
            display += " ~ " + self._signal_format_datetime(end)
        return {
            "start": self._signal_format_datetime(start),
            "end": self._signal_format_datetime(end),
            "display": display,
        }

    def _signal_parse_datetime(self, *, text: str, time_text: str, reference_date: str) -> datetime | None:
        full_match = re.search(r"\[(20\d{2}-\d{2}-\d{2}) \+\d{4} (\d{2}:\d{2}:\d{2})\]", text)
        if full_match:
            try:
                return datetime.strptime(f"{full_match.group(1)} {full_match.group(2)}", "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return None
        value = time_text or text
        month_day_match = re.search(r"(\d{2})-(\d{2}) (\d{2}:\d{2}:\d{2}(?:\.\d{3})?)", value)
        if not month_day_match or not reference_date:
            return None
        dt_text = f"{reference_date[:4]}-{month_day_match.group(1)}-{month_day_match.group(2)} {month_day_match.group(3)}"
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(dt_text, fmt)
            except ValueError:
                continue
        return None

    def _signal_format_datetime(self, value: datetime) -> str:
        if value.microsecond:
            return value.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        return value.strftime("%Y-%m-%d %H:%M:%S")

    def _signal_fault_alignment(self, fault_time: str, focus_scope: dict[str, object]) -> dict[str, str]:
        time_window = focus_scope.get("time_window", {}) if isinstance(focus_scope, dict) else {}
        start = str(time_window.get("start") or "")
        if not fault_time:
            return {
                "sev": "yellow",
                "label": "未识别",
                "detail": "请求里没有可比对的故障时间。",
                "headline": "当前报告只说明可见日志内的链路状态，无法和现场时间做严格对齐。",
                "judgement": "没有可比对的故障时间，只能把这份报告当作日志样本说明，不应直接当成现场定案。",
            }
        if not start:
            return {
                "sev": "yellow",
                "label": "未知",
                "detail": "当前报告没有解析出明确日志时间窗。",
                "headline": "当前报告缺少明确日志时间窗，不能直接拿来证明现场结论。",
                "judgement": "需要补充目标时间窗日志，才能把这份链路报告和现场结论绑定起来。",
            }
        fault_hour = fault_time[:13]
        start_hour = start[:13]
        if fault_hour == start_hour:
            return {
                "sev": "green",
                "label": "一致",
                "detail": f"日志时间窗命中了请求故障小时 {fault_hour}。",
                "headline": "当前日志时间窗和请求故障时间一致，可以把下面的链路证据直接用于现场判断。",
                "judgement": "时间窗一致，这份报告可直接回答“现场这条链路当时有没有走通”。",
            }
        if fault_time[:10] == start[:10]:
            return {
                "sev": "yellow",
                "label": "同日不同小时",
                "detail": f"请求故障时间是 {fault_time}，当前日志主时间窗是 {start}。",
                "headline": "当前日志和请求是同一天，但不是同一小时，结论只能作为同版本同流程样本。",
                "judgement": "这能说明代码和样本日志里的链路行为，但还不能直接证明目标时刻的现场现象。",
            }
        return {
            "sev": "yellow",
            "label": "不一致",
            "detail": f"请求故障时间是 {fault_time}，当前日志主时间窗是 {start}。",
            "headline": "当前可用日志不是目标现场时间窗，下面的证据更适合回答“链路设计和样本运行是否走通”，不适合直接下现场定案。",
            "judgement": "这份报告最能说明的是样本日志里目标信号在当前焦点进程内走到了哪里，而不是请求里的目标时刻一定发生了什么。",
        }

    def _signal_lifecycle_nodes(
        self,
        signal_payload: dict[str, object],
        focus_scope: dict[str, object],
        fault_time: str,
        source_evidence_path: Path | None,
    ) -> list[dict[str, str]]:
        package = str(focus_scope.get("package") or "")
        pid = str(focus_scope.get("pid") or "")
        reference_date = str(focus_scope.get("reference_date") or "")
        start_dt = None
        nodes: list[dict[str, str]] = []
        for event in self._signal_runtime_events(signal_payload):
            if not self._signal_item_matches_scope(event, package, pid):
                continue
            label = str(event.get("label") or "")
            text = str(event.get("text") or "")
            if label == "进程启动":
                start_dt = self._signal_parse_datetime(text=text, time_text=str(event.get("time") or ""), reference_date=reference_date)
                nodes.append(
                    {
                        "tag": "日志",
                        "title": f"{package or '目标进程'} 启动",
                        "meta": f"{event.get('time') or ''} · PID {pid or self._signal_pid_from_text(text) or '未识别'}",
                        "note": text,
                    }
                )
                break
        for keyword, title in (
            ("injectSignalProvider", self._signal_provider_injection_title(signal_payload)),
            ("registerSignal", self._signal_registration_title(signal_payload)),
        ):
            match = self._signal_find_runtime_event(signal_payload, package, pid, keyword)
            if match is None:
                continue
            nodes.append(
                {
                    "tag": "日志",
                    "title": title,
                    "meta": self._signal_meta_from_item(match, start_dt, reference_date),
                    "note": str(match.get("text") or ""),
                }
            )
        for stage_key, title in (
            ("datacenter", self._signal_datacenter_stage_title(signal_payload)),
            ("android_business", self._signal_business_stage_title(signal_payload)),
        ):
            match = self._signal_find_stage_example(signal_payload, stage_key, package, pid, prefer_hmi=False, target_only=True)
            if match is None:
                continue
            nodes.append(
                {
                    "tag": "日志",
                    "title": title,
                    "meta": self._signal_meta_from_item(match, start_dt, reference_date),
                    "note": str(match.get("text") or ""),
                }
            )
        hmi_match = self._signal_find_stage_example(signal_payload, "android_business", package, pid, prefer_hmi=True, target_only=True)
        if hmi_match is not None:
            nodes.append(
                {
                    "tag": "日志",
                    "title": self._signal_consumer_stage_title(hmi_match),
                    "meta": self._signal_meta_from_item(hmi_match, start_dt, reference_date),
                    "note": str(hmi_match.get("text") or ""),
                }
            )
        business_entry = self._signal_select_business_entry(source_evidence_path, prompt_text=fault_time, signal_payload=signal_payload)
        if business_entry is not None:
            nodes.append(
                {
                    "tag": "源码",
                    "title": "业务判定落点",
                    "meta": f"{business_entry['file']}:{business_entry['line']}",
                    "note": business_entry["text"],
                }
            )
        return nodes[:6]

    def _signal_dataflow_nodes(
        self,
        signal_payload: dict[str, object],
        prompt_text: str,
        source_evidence_path: Path | None,
    ) -> list[dict[str, str]]:
        refs = signal_payload.get("source_references", []) if isinstance(signal_payload, dict) else []
        lifecycle = signal_payload.get("lifecycle_report", {}) if isinstance(signal_payload, dict) else {}
        lifecycle_refs = lifecycle.get("source_references", []) if isinstance(lifecycle, dict) else []
        chain_edges = signal_payload.get("chain_edges", []) if isinstance(signal_payload, dict) else []
        nodes: list[dict[str, str]] = []
        source_edge = None
        context = lifecycle.get("context", {}) if isinstance(lifecycle, dict) else {}
        if isinstance(context, dict):
            candidate = context.get("source_edge")
            if isinstance(candidate, dict):
                source_edge = candidate
        if source_edge is None:
            for edge in chain_edges:
                if isinstance(edge, dict) and str(edge.get("source") or "").startswith("CARSERVICE:"):
                    source_edge = edge
                    break
        if isinstance(source_edge, dict):
            nodes.append(
                {
                    "tag": "映射",
                    "title": str(source_edge.get("source") or "上游信号"),
                    "meta": f"{source_edge.get('file') or ''}:{source_edge.get('line') or ''}",
                    "note": str(source_edge.get("note") or ""),
                }
            )
        on_change_ref = self._signal_find_source_reference(
            lifecycle_refs,
            lambda file_text, line_text: "carvcuhelper.kt" in file_text and "onchangeevent" in line_text,
        )
        if on_change_ref is not None:
            nodes.append(
                {
                    "tag": "Helper",
                    "title": "CarVcuHelper.onChangeEvent",
                    "meta": f"{on_change_ref['file']}:{on_change_ref['line']}",
                    "note": on_change_ref["text"],
                }
            )
        data_center_edge = None
        for edge in chain_edges:
            if isinstance(edge, dict) and "datacenter" in str(edge.get("target") or "").casefold():
                data_center_edge = edge
                break
        if isinstance(data_center_edge, dict):
            nodes.append(
                {
                    "tag": "分发",
                    "title": self._signal_data_center_title(),
                    "meta": f"{data_center_edge.get('file') or ''}:{data_center_edge.get('line') or ''}",
                    "note": str(data_center_edge.get("note") or ""),
                }
            )
        consumer_ref = self._signal_select_consumer_reference(refs)
        if consumer_ref is not None:
            nodes.append(
                {
                    "tag": "业务",
                    "title": self._signal_consumer_reference_title(consumer_ref),
                    "meta": f"{consumer_ref['file']}:{consumer_ref['line']}",
                    "note": consumer_ref["text"],
                }
            )
        hmi_ref = self._signal_select_state_receiver_reference(refs)
        if hmi_ref is not None:
            nodes.append(
                {
                    "tag": "HMI",
                    "title": self._signal_consumer_reference_title(hmi_ref),
                    "meta": f"{hmi_ref['file']}:{hmi_ref['line']}",
                    "note": hmi_ref["text"],
                }
            )
        business_entry = self._signal_select_business_entry(source_evidence_path, prompt_text=prompt_text, signal_payload=signal_payload)
        if business_entry is not None:
            nodes.append(
                {
                    "tag": "落点",
                    "title": Path(business_entry["file"]).stem,
                    "meta": f"{business_entry['file']}:{business_entry['line']}",
                    "note": business_entry["text"],
                }
            )
        return nodes[:6]

    def _signal_runtime_events(self, signal_payload: dict[str, object]) -> list[dict[str, object]]:
        lifecycle = signal_payload.get("lifecycle_report", {}) if isinstance(signal_payload, dict) else {}
        runtime = lifecycle.get("runtime", {}) if isinstance(lifecycle, dict) else {}
        events = runtime.get("events", []) if isinstance(runtime, dict) else []
        return [event for event in events if isinstance(event, dict)]

    def _signal_item_matches_scope(self, item: dict[str, object], package: str, pid: str) -> bool:
        if package:
            item_package = self._signal_package_from_path(str(item.get("file") or ""))
            if item_package and item_package != package:
                return False
        if pid:
            item_pid = self._signal_pid_from_text(str(item.get("text") or ""))
            if item_pid and item_pid != pid:
                return False
        return True

    def _signal_item_matches_target(self, signal_payload: dict[str, object], item: dict[str, object]) -> bool:
        signal = signal_payload.get("signal", {}) if isinstance(signal_payload, dict) else {}
        code = str(signal.get("code") or "").strip()
        name = str(signal.get("name") or "").strip()
        item_code = str(item.get("code") or "").strip()
        if item_code:
            return bool(code and item_code == code)
        text = str(item.get("text") or "")
        return bool((code and re.search(rf"(?<!\d){re.escape(code)}(?!\d)", text)) or (name and name in text))

    def _signal_find_runtime_event(
        self,
        signal_payload: dict[str, object],
        package: str,
        pid: str,
        keyword: str,
    ) -> dict[str, object] | None:
        lowered_keyword = keyword.casefold()
        for event in self._signal_runtime_events(signal_payload):
            if not self._signal_item_matches_scope(event, package, pid):
                continue
            haystack = f"{event.get('label') or ''}\n{event.get('text') or ''}".casefold()
            if lowered_keyword in haystack:
                return event
        return None

    def _signal_find_stage_example(
        self,
        signal_payload: dict[str, object],
        stage_key: str,
        package: str,
        pid: str,
        *,
        prefer_hmi: bool,
        target_only: bool = False,
    ) -> dict[str, object] | None:
        log_report = signal_payload.get("log_report", {}) if isinstance(signal_payload, dict) else {}
        stages = log_report.get("stages", {}) if isinstance(log_report, dict) else {}
        stage = stages.get(stage_key) if isinstance(stages, dict) else None
        if not isinstance(stage, dict):
            return None
        for example in stage.get("examples", []) or []:
            if not isinstance(example, dict):
                continue
            if target_only and not self._signal_item_matches_target(signal_payload, example):
                continue
            if package:
                item_package = self._signal_package_from_path(str(example.get("file") or ""))
                if item_package and item_package != package:
                    continue
            if pid:
                item_pid = self._signal_pid_from_text(str(example.get("text") or ""))
                if item_pid and item_pid != pid:
                    continue
            text = str(example.get("text") or "")
            is_hmi = "hmi" in text.casefold() or "update battery level" in text.casefold()
            if prefer_hmi and not is_hmi:
                continue
            if not prefer_hmi and is_hmi and stage_key == "android_business":
                continue
            return example
        return None

    def _signal_meta_from_item(
        self,
        item: dict[str, object],
        start_dt: datetime | None,
        reference_date: str,
    ) -> str:
        timestamp = self._signal_parse_datetime(
            text=str(item.get("text") or ""),
            time_text=str(item.get("time") or ""),
            reference_date=reference_date,
        )
        when = str(item.get("time") or "")
        if not when and timestamp is not None:
            when = self._signal_format_datetime(timestamp)
        pid = self._signal_pid_from_text(str(item.get("text") or ""))
        tid = self._signal_tid_from_text(str(item.get("text") or ""))
        meta = when
        if start_dt is not None and timestamp is not None:
            meta = f"{when} · {self._signal_relative_text(start_dt, timestamp)}"
        if pid:
            meta += f" · PID {pid}"
        if tid:
            meta += f" / TID {tid}"
        return meta.strip(" ·")

    def _signal_relative_text(self, start_dt: datetime, current_dt: datetime) -> str:
        delta = max(0.0, (current_dt - start_dt).total_seconds())
        if delta < 1:
            return f"+{int(delta * 1000)}ms"
        return f"+{delta:.3f}s"

    def _signal_evidence_rows(
        self,
        signal_payload: dict[str, object],
        focus_scope: dict[str, object],
    ) -> list[tuple[str, str, str, str, str]]:
        package = str(focus_scope.get("package") or "")
        pid = str(focus_scope.get("pid") or "")
        reference_date = str(focus_scope.get("reference_date") or "")
        start_dt = None
        start_event = self._signal_find_runtime_event(signal_payload, package, pid, "process begin")
        if start_event is not None:
            start_dt = self._signal_parse_datetime(
                text=str(start_event.get("text") or ""),
                time_text=str(start_event.get("time") or ""),
                reference_date=reference_date,
            )
        items: list[dict[str, object]] = []
        for event in self._signal_runtime_events(signal_payload):
            if self._signal_item_matches_scope(event, package, pid):
                items.append(event)
        for stage_key in ("datacenter", "android_business", "other"):
            match = self._signal_find_stage_example(signal_payload, stage_key, package, pid, prefer_hmi=False, target_only=True)
            if match is not None:
                items.append(match)
        hmi = self._signal_find_stage_example(signal_payload, "android_business", package, pid, prefer_hmi=True, target_only=True)
        if hmi is not None:
            items.append(hmi)
        rows: list[tuple[str, str, str, str, str]] = []
        seen: set[str] = set()
        for item in items:
            text = str(item.get("text") or "")
            key = f"{item.get('line')}::{text}"
            if key in seen:
                continue
            seen.add(key)
            timestamp = self._signal_parse_datetime(
                text=text,
                time_text=str(item.get("time") or ""),
                reference_date=reference_date,
            )
            when = str(item.get("time") or (self._signal_format_datetime(timestamp) if timestamp is not None else ""))
            relative = "-"
            if start_dt is not None and timestamp is not None:
                relative = self._signal_relative_text(start_dt, timestamp)
            row_pid = self._signal_pid_from_text(text) or pid or "-"
            row_tid = self._signal_tid_from_text(text)
            pid_tid = f"PID {row_pid}"
            if row_tid:
                pid_tid += f" / TID {row_tid}"
            rows.append(
                (
                    when or "-",
                    relative,
                    pid_tid,
                    self._signal_stage_label(item),
                    self._signal_shorten(text, 120),
                )
            )
        return rows[:8]

    def _signal_android_data_link_checks(
        self,
        signal_payload: dict[str, object],
        focus_scope: dict[str, object],
        *,
        selected_input: Path | None,
        source_evidence_path: Path | None,
    ) -> list[dict[str, object]]:
        lifecycle = signal_payload.get("lifecycle_report", {}) if isinstance(signal_payload, dict) else {}
        context = lifecycle.get("context", {}) if isinstance(lifecycle, dict) else {}
        chain_edges = signal_payload.get("chain_edges", []) if isinstance(signal_payload, dict) else []
        is_android_datacenter = bool(context) or any(
            isinstance(edge, dict) and "datacenter" in str(edge.get("target") or "").casefold()
            for edge in chain_edges
        )
        if not is_android_datacenter:
            return []
        source_facts = self._signal_android_source_facts(signal_payload, source_evidence_path)
        source_facts = self._signal_merge_cached_keywords(signal_payload, source_facts)
        scan_terms = self._signal_android_scan_terms(signal_payload, source_facts)
        log_hits = self._signal_scan_log_terms(signal_payload, selected_input, scan_terms)
        self._signal_store_keyword_profile(
            signal_payload,
            source_facts,
            scan_terms=scan_terms,
            log_hits=log_hits,
            selected_input=selected_input,
            source_evidence_path=source_evidence_path,
        )
        signal = signal_payload.get("signal", {}) if isinstance(signal_payload, dict) else {}
        signal_name = str(signal.get("name") or "").strip()
        event_ids = [str(item) for item in source_facts.get("event_ids", []) if str(item).strip()]
        event_id_text = " / ".join(event_ids) or "上游事件"
        helper_class = str(context.get("helper_class") or "Helper") if isinstance(context, dict) else "Helper"
        helper_file = str(context.get("helper_file") or "") if isinstance(context, dict) else ""
        helper_full_class = self._signal_kotlin_class_from_file(helper_file, helper_class)
        controller_class = str(context.get("controller_class") or "Controller") if isinstance(context, dict) else "Controller"

        package = str(focus_scope.get("package") or "")
        pid = str(focus_scope.get("pid") or "")
        init_terms = [f"injectSignalProvider[{helper_full_class}" if helper_full_class else "", helper_class, controller_class, "injectSignalProvider["]
        init_hit = self._signal_best_log_hit(log_hits, init_terms, pid=pid, package=package)
        init_runtime = self._signal_find_runtime_event(signal_payload, package, pid, "injectSignalProvider")
        init_status = "通过" if init_hit is not None or init_runtime else "证据不足"
        init_evidence = self._signal_log_hit_text(init_runtime) or self._signal_log_hit_text(init_hit) or self._signal_runtime_evidence_text(signal_payload, "injectSignalProvider")

        register_terms = []
        for event_id in event_ids:
            register_terms.extend([f"register {event_id} indeed", f"registerRemoteListener: event:{event_id}", f"registerEventListener: event:{event_id}"])
        register_hit = self._signal_best_log_hit(log_hits, register_terms, pid=pid, package=package)
        unsupported_hit = self._signal_best_log_hit(
            log_hits,
            [f"not support id:{event_id}" for event_id in event_ids] + [f"register not support id:{event_id}" for event_id in event_ids],
            pid=pid,
            package=package,
        )
        if unsupported_hit is not None:
            register_status = "未通过"
            register_conclusion = f"上游 {event_id_text} 明确返回不支持。"
        elif register_hit is not None:
            register_status = "通过"
            register_conclusion = f"上游 {event_id_text} 已完成监听注册，未看到 not support。"
        else:
            register_status = "证据不足"
            register_conclusion = f"源码可定位到 {event_id_text}，但当前日志未证明上游监听注册成功。"

        producer_terms = [str(item) for item in source_facts.get("producer_terms", []) if str(item).strip()]
        producer_hit = self._signal_best_log_hit(log_hits, producer_terms, pid=pid, package=package)
        target_datacenter_hit = self._signal_find_stage_example(
            signal_payload,
            "datacenter",
            package,
            pid,
            prefer_hmi=False,
            target_only=True,
        )
        if producer_hit is not None:
            upstream_status = "通过"
            upstream_conclusion = f"已看到 {helper_class} 收到上游数据并进入目标信号处理。"
            upstream_evidence = self._signal_log_hit_text(producer_hit)
        else:
            upstream_status = "未通过"
            if event_ids:
                upstream_conclusion = f"未看到 {event_id_text} 回调数据进入 {helper_class}。"
            else:
                upstream_conclusion = f"未看到上游回调数据进入 {helper_class}。"
            source_ref_text = self._signal_source_fact_text(source_facts, "producer_refs")
            missing_terms = "、".join(producer_terms[:4])
            upstream_evidence = source_ref_text or "当前日志未看到上游回调和 onNextData。"
            if missing_terms:
                upstream_evidence = f"{upstream_evidence}；日志未命中关键字：{missing_terms}"
            if target_datacenter_hit is not None:
                upstream_evidence = f"{upstream_evidence}；仅看到取流/订阅：{self._signal_log_hit_text(target_datacenter_hit)}"

        business_terms = [str(item) for item in source_facts.get("consumer_terms", []) if str(item).strip()]
        business_register_hit = self._signal_best_log_hit(
            log_hits,
            [f"getSignalFlow: signalCode={signal_name}", signal_name],
            pid=pid,
            package=package,
        )
        if target_datacenter_hit is not None or business_register_hit is not None or source_facts.get("consumer_refs"):
            business_register_status = "通过"
            business_register_conclusion = "业务已注册目标信号。"
            business_register_evidence = (
                self._signal_log_hit_text(target_datacenter_hit)
                or self._signal_log_hit_text(business_register_hit)
                or self._signal_source_fact_text(source_facts, "consumer_refs")
            )
        else:
            business_register_status = "证据不足"
            business_register_conclusion = "未看到业务注册目标信号的源码或日志证据。"
            business_register_evidence = ""

        business_hit = self._signal_best_log_hit(log_hits, business_terms, pid=pid, package=package)
        business_stage_hits = self._signal_target_stage_hits(
            signal_payload,
            (signal_payload.get("log_report", {}) or {}).get("stages", {}) if isinstance(signal_payload.get("log_report", {}), dict) else {},
            "android_business",
        )
        if business_hit is not None or business_stage_hits:
            business_receive_status = "通过"
            business_receive_conclusion = "业务已收到目标信号。"
            business_receive_evidence = self._signal_log_hit_text(business_hit) or f"目标信号业务消费命中 {business_stage_hits} 条。"
        else:
            business_receive_status = "未通过"
            business_receive_conclusion = "业务未收到目标信号。"
            business_receive_evidence = (
                self._signal_source_fact_text(source_facts, "consumer_refs")
                or "源码存在消费分支，但日志未出现对应业务 update/collect 输出。"
            )

        checks = [
            self._signal_android_check(
                "1",
                "module_datacenter 初始化 / 上游 SDK 链接",
                init_status,
                f"{helper_class} / {controller_class} 初始化链路可见。" if init_status == "通过" else "未看到完整初始化链路日志。",
                init_evidence,
            ),
            self._signal_android_check(
                "2",
                "向上游注册信号 / 上游支持性",
                register_status,
                register_conclusion,
                self._signal_log_hit_text(unsupported_hit) or self._signal_log_hit_text(register_hit) or self._signal_source_fact_text(source_facts, "producer_refs"),
            ),
            self._signal_android_check(
                "3",
                "上游数据进入 DataCenter",
                upstream_status,
                upstream_conclusion,
                upstream_evidence,
            ),
            self._signal_android_check(
                "4",
                "业务注册目标信号",
                business_register_status,
                business_register_conclusion,
                business_register_evidence,
            ),
            self._signal_android_check(
                "5",
                "业务收到目标信号",
                business_receive_status,
                business_receive_conclusion,
                business_receive_evidence,
            ),
        ]
        for check in checks:
            check["source_keywords"] = source_facts.get("keywords", [])
            check["keyword_cache"] = source_facts.get("keyword_cache", {})
        return checks

    def _signal_android_check(self, step: str, checkpoint: str, status: str, conclusion: str, evidence: str) -> dict[str, object]:
        sev = "green" if status == "通过" else "red" if status == "未通过" else "yellow"
        return {
            "step": step,
            "checkpoint": checkpoint,
            "status": status,
            "sev": sev,
            "conclusion": conclusion,
            "evidence": evidence or "未提取到直接证据。",
        }

    def _signal_merge_android_conclusion(self, composition: ReportComposition, checks: list[dict[str, object]]) -> None:
        likely_cause = self._signal_android_likely_cause(checks)
        if not likely_cause:
            return
        failed = [item for item in checks if item.get("status") == "未通过"]
        item = {
            "sev": "red" if failed else "yellow",
            "title": "当前最可能卡点",
            "detail": likely_cause,
        }
        for section in composition.sections:
            if section.title == "结论摘要" and section.kind == "issues":
                section.items = [item, *section.items]
                return
        composition.sections.insert(0, ReportSection(kind="issues", title="结论摘要", items=[item]))

    def _signal_insert_sections_after(
        self,
        composition: ReportComposition,
        *,
        title: str,
        sections: list[ReportSection],
    ) -> None:
        if not sections:
            return
        for index, section in enumerate(composition.sections):
            if section.title == title:
                composition.sections[index + 1 : index + 1] = sections
                return
        composition.sections[0:0] = sections

    def _signal_android_data_link_sections(self, checks: list[dict[str, object]]) -> list[ReportSection]:
        nodes = []
        for item in checks:
            status = str(item.get("status") or "").strip()
            checkpoint = str(item.get("checkpoint") or "").strip()
            conclusion = str(item.get("conclusion") or "").strip()
            evidence = str(item.get("evidence") or "").strip()
            sev = "red" if status == "未通过" else "green" if status == "通过" else "yellow"
            step = str(item.get("step") or "").strip()
            title = f"{step}. {checkpoint}" if step else checkpoint
            nodes.append(
                {
                    "sev": sev,
                    "title": f"{title}：{status or '未识别'}",
                    "evidence": conclusion,
                    "downstream": self._signal_shorten(evidence, 260),
                }
            )
        return [
            ReportSection(
                kind="chain",
                title="Android 数据链路排查",
                description="按初始化、上游注册、上游入数、业务注册、业务接收逐段收敛；完整原始日志放入后续证据区。",
                nodes=nodes,
                empty_text="当前信号不是 Android module_datacenter 链路。",
            )
        ]

    def _signal_android_likely_cause(self, checks: list[dict[str, object]]) -> str:
        if not checks:
            return ""
        by_step = {str(item.get("step") or ""): item for item in checks}
        upstream = by_step.get("3", {})
        business_register = by_step.get("4", {})
        business_receive = by_step.get("5", {})
        if upstream.get("status") == "未通过" and business_register.get("status") == "通过":
            return (
                f"{upstream.get('conclusion') or '未看到上游数据进入 DataCenter'}"
                f" {business_receive.get('conclusion') or '业务未收到目标信号'}"
                " 结合现有证据，最可能卡点在上游事件回调没有下发有效载荷，或载荷缺少目标信号需要的 value key。"
            )
        failed = [item for item in checks if item.get("status") == "未通过"]
        if failed:
            return "；".join(str(item.get("conclusion") or item.get("checkpoint") or "") for item in failed if item)
        return ""

    def _signal_android_source_facts(
        self,
        signal_payload: dict[str, object],
        source_evidence_path: Path | None,
    ) -> dict[str, object]:
        signal = signal_payload.get("signal", {}) if isinstance(signal_payload, dict) else {}
        signal_name = str(signal.get("name") or "").strip()
        lifecycle = signal_payload.get("lifecycle_report", {}) if isinstance(signal_payload, dict) else {}
        context = lifecycle.get("context", {}) if isinstance(lifecycle, dict) else {}
        helper_file = str(context.get("helper_file") or "") if isinstance(context, dict) else ""
        facts: dict[str, object] = {
            "event_ids": [],
            "producer_terms": [],
            "consumer_terms": [],
            "producer_refs": [],
            "consumer_refs": [],
            "keywords": [],
        }
        event_ids: list[str] = []
        producer_terms: list[str] = []
        producer_refs: list[str] = []
        helper_path = self._repo_relative_path(helper_file)
        if helper_path is not None:
            helper_text = self._read_text_quiet(helper_path)
            helper_lines = helper_text.splitlines()
            consts = self._signal_kotlin_string_constants(helper_text)
            for line_no, line in enumerate(helper_lines, 1):
                if signal_name and signal_name in line:
                    matched_event = False
                    for nearby_no in range(line_no, min(len(helper_lines), line_no + 8) + 1):
                        nearby = helper_lines[nearby_no - 1]
                        match = re.search(r"\b(\d{4,})\b\s*(?:to|,)\s*SignalCode\.([A-Z0-9_]+)", nearby)
                        if match:
                            event_ids.append(match.group(1))
                            producer_refs.append(f"{helper_file}:{nearby_no} {nearby.strip()}")
                            matched_event = True
                            break
                    if not matched_event:
                        match = re.search(r"\b(\d{4,})\b\s*(?:to|,)\s*SignalCode\.([A-Z0-9_]+)", line)
                        if match:
                            event_ids.append(match.group(1))
                    producer_refs.append(f"{helper_file}:{line_no} {line.strip()}")
            for event_id in list(dict.fromkeys(event_ids)):
                callback_name = self._signal_callback_name_for_event(helper_text, event_id)
                if callback_name:
                    callback_lines = self._signal_kotlin_function_block(helper_lines, callback_name)
                    producer_lines = self._signal_kotlin_target_branch(callback_lines, signal_name)
                    for offset, line in producer_lines:
                        if signal_name and signal_name in line:
                            producer_refs.append(f"{helper_file}:{offset} {line.strip()}")
                        for token in re.findall(r"\b(?:EVENT_KEY|VALUE_KEY)_[A-Z0-9_]+\b", line):
                            producer_terms.append(token)
                            if token in consts:
                                producer_terms.append(consts[token])
                        literal = self._signal_log_literal_from_source_line(line)
                        if literal:
                            producer_terms.append(literal)
        refs = []
        refs.extend(signal_payload.get("source_references", []) if isinstance(signal_payload, dict) else [])
        refs.extend(self._parse_source_evidence_entries(source_evidence_path))
        refs.extend(self._signal_repo_signal_references(signal_name))
        consumer_refs: list[str] = []
        consumer_terms: list[str] = []
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            file_text = str(ref.get("file") or "")
            line_text = str(ref.get("text") or "")
            if signal_name and signal_name in line_text and "module_proto" not in file_text and "module_datacenter" not in file_text:
                consumer_refs.append(f"{file_text}:{ref.get('line') or ''} {line_text}".strip())
                path = self._repo_relative_path(file_text)
                if path is not None:
                    consumer_terms.extend(self._signal_consumer_log_terms(path, signal_name))
        keywords = self._unique_nonempty([*event_ids, *producer_terms, *consumer_terms, signal_name])
        facts["event_ids"] = self._unique_nonempty(event_ids)
        facts["producer_terms"] = self._unique_nonempty(producer_terms)
        facts["consumer_terms"] = self._unique_nonempty(consumer_terms)
        facts["producer_refs"] = self._unique_nonempty(producer_refs)
        facts["consumer_refs"] = self._unique_nonempty(consumer_refs)
        facts["keywords"] = keywords
        facts["source_signature"] = self._signal_keyword_source_signature(facts)
        return facts

    def _signal_merge_cached_keywords(
        self,
        signal_payload: dict[str, object],
        source_facts: dict[str, object],
    ) -> dict[str, object]:
        cache_entry = self._signal_load_keyword_profile(signal_payload)
        current_signature = str(source_facts.get("source_signature") or "")
        cache_status = "miss"
        cache_updated_at = ""
        if cache_entry:
            cached_signature = str(cache_entry.get("source_signature") or "")
            cache_updated_at = str(cache_entry.get("updated_at") or "")
            if current_signature and cached_signature == current_signature:
                cache_status = "hit"
                for key in ("event_ids", "producer_terms", "consumer_terms", "producer_refs", "consumer_refs"):
                    cached_values = cache_entry.get(key, [])
                    if isinstance(cached_values, list):
                        source_facts[key] = self._unique_nonempty([*(source_facts.get(key, []) or []), *cached_values])
                cached_keywords = cache_entry.get("keywords", [])
                source_facts["keywords"] = self._unique_nonempty([*(source_facts.get("keywords", []) or []), *(cached_keywords if isinstance(cached_keywords, list) else [])])
            elif not current_signature:
                cache_status = "fallback"
                for key in ("event_ids", "producer_terms", "consumer_terms", "producer_refs", "consumer_refs", "keywords"):
                    cached_values = cache_entry.get(key, [])
                    if isinstance(cached_values, list):
                        source_facts[key] = self._unique_nonempty([*(source_facts.get(key, []) or []), *cached_values])
                source_facts["source_signature"] = cached_signature
            else:
                cache_status = "stale_refresh"
        source_facts["keyword_cache"] = {
            "status": cache_status,
            "updated_at": cache_updated_at,
            "source_signature": str(source_facts.get("source_signature") or ""),
        }
        return source_facts

    def _signal_keyword_source_signature(self, source_facts: dict[str, object]) -> str:
        payload = {
            key: source_facts.get(key, [])
            for key in ("event_ids", "producer_refs", "consumer_refs", "producer_terms", "consumer_terms")
        }
        if not any(isinstance(value, list) and value for value in payload.values()):
            return ""
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def _signal_keyword_cache_path(self) -> Path:
        return Path(self.config.data_dir).expanduser().resolve() / "signal_keyword_cache.json"

    def _signal_keyword_cache_key(self, signal_payload: dict[str, object]) -> str:
        signal = signal_payload.get("signal", {}) if isinstance(signal_payload, dict) else {}
        code = str(signal.get("code") or "").strip()
        name = str(signal.get("name") or "").strip()
        return code or name or "unknown"

    def _signal_load_keyword_profile(self, signal_payload: dict[str, object]) -> dict[str, object] | None:
        path = self._signal_keyword_cache_path()
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        entry = payload.get(self._signal_keyword_cache_key(signal_payload))
        return entry if isinstance(entry, dict) else None

    def _signal_store_keyword_profile(
        self,
        signal_payload: dict[str, object],
        source_facts: dict[str, object],
        *,
        scan_terms: list[str],
        log_hits: dict[str, list[dict[str, object]]],
        selected_input: Path | None,
        source_evidence_path: Path | None,
    ) -> None:
        cache_key = self._signal_keyword_cache_key(signal_payload)
        if not cache_key or cache_key == "unknown":
            return
        signal = signal_payload.get("signal", {}) if isinstance(signal_payload, dict) else {}
        verified_terms = [term for term in self._unique_nonempty(scan_terms) if log_hits.get(term)]
        profile = {
            "signal_code": str(signal.get("code") or ""),
            "signal_name": str(signal.get("name") or ""),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "source_signature": str(source_facts.get("source_signature") or ""),
            "event_ids": source_facts.get("event_ids", []),
            "producer_terms": source_facts.get("producer_terms", []),
            "consumer_terms": source_facts.get("consumer_terms", []),
            "producer_refs": source_facts.get("producer_refs", []),
            "consumer_refs": source_facts.get("consumer_refs", []),
            "keywords": source_facts.get("keywords", []),
            "verified_terms": verified_terms,
            "selected_input": str(selected_input) if selected_input else "",
            "source_evidence_file": str(source_evidence_path) if source_evidence_path else "",
        }
        path = self._signal_keyword_cache_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                payload = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    payload = {}
            else:
                payload = {}
            payload[cache_key] = profile
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except (OSError, json.JSONDecodeError):
            return

    def _signal_android_scan_terms(self, signal_payload: dict[str, object], source_facts: dict[str, object]) -> list[str]:
        signal = signal_payload.get("signal", {}) if isinstance(signal_payload, dict) else {}
        signal_name = str(signal.get("name") or "").strip()
        lifecycle = signal_payload.get("lifecycle_report", {}) if isinstance(signal_payload, dict) else {}
        context = lifecycle.get("context", {}) if isinstance(lifecycle, dict) else {}
        helper_class = str(context.get("helper_class") or "").strip() if isinstance(context, dict) else ""
        helper_file = str(context.get("helper_file") or "").strip() if isinstance(context, dict) else ""
        helper_full_class = self._signal_kotlin_class_from_file(helper_file, helper_class)
        controller_class = str(context.get("controller_class") or "").strip() if isinstance(context, dict) else ""
        terms: list[str] = [
            signal_name,
            f"getSignalFlow: signalCode={signal_name}" if signal_name else "",
            f"injectSignalProvider[{helper_full_class}" if helper_full_class else "",
            helper_class,
            controller_class,
            "injectSignalProvider[",
        ]
        for event_id in source_facts.get("event_ids", []) if isinstance(source_facts, dict) else []:
            event_id_text = str(event_id)
            terms.extend(
                [
                    f"register {event_id_text} indeed",
                    f"registerRemoteListener: event:{event_id_text}",
                    f"registerEventListener: event:{event_id_text}",
                    f"not support id:{event_id_text}",
                    f"register not support id:{event_id_text}",
                ]
            )
        for key in ("producer_terms", "consumer_terms"):
            terms.extend(str(item) for item in source_facts.get(key, []) if str(item).strip())
        return self._unique_nonempty(terms)

    def _signal_scan_log_terms(
        self,
        signal_payload: dict[str, object],
        selected_input: Path | None,
        terms: list[str],
        *,
        max_hits_per_term: int = 20,
        max_files: int = 80,
        max_lines_per_file: int = 200000,
    ) -> dict[str, list[dict[str, object]]]:
        clean_terms = self._unique_nonempty(terms)
        if not clean_terms:
            return {}
        files: list[Path] = []
        for item in self._signal_evidence_items(signal_payload):
            path = Path(str(item.get("file") or "")).expanduser()
            if path.exists() and path.is_file():
                files.append(path)
        if selected_input is not None and selected_input.exists():
            files.extend(self._iter_log_coverage_files(selected_input)[:max_files])
        unique_files: list[Path] = []
        seen_files: set[str] = set()
        for path in files:
            key = str(path)
            if key in seen_files:
                continue
            seen_files.add(key)
            unique_files.append(path)
            if len(unique_files) >= max_files:
                break
        hits: dict[str, list[dict[str, object]]] = {term: [] for term in clean_terms}
        remaining = set(clean_terms)
        for path in unique_files:
            if not remaining:
                break
            try:
                with path.open("r", encoding="utf-8", errors="replace") as handle:
                    for line_no, line in enumerate(handle, 1):
                        if line_no > max_lines_per_file:
                            break
                        for term in list(remaining):
                            if term and term in line:
                                hits[term].append({"term": term, "file": str(path), "line": line_no, "text": line.rstrip()})
                                if len(hits[term]) >= max_hits_per_term:
                                    remaining.discard(term)
                        if not remaining:
                            break
            except OSError:
                continue
        return {term: items for term, items in hits.items() if items}

    def _signal_best_log_hit(
        self,
        hits: dict[str, list[dict[str, object]]],
        terms: list[str],
        *,
        pid: str = "",
        package: str = "",
    ) -> dict[str, object] | None:
        ordered_terms = self._unique_nonempty(terms)
        if pid or package:
            for term in ordered_terms:
                for item in hits.get(term, []) or []:
                    text = str(item.get("text") or "")
                    file_text = str(item.get("file") or "")
                    item_pid = self._signal_pid_from_text(text)
                    if pid and item_pid and item_pid != pid:
                        continue
                    if package and package not in file_text and package not in text:
                        item_package = self._signal_package_from_path(file_text)
                        if item_package and item_package != package:
                            continue
                    return item
        for term in ordered_terms:
            items = hits.get(term)
            if items:
                return items[0]
        return None

    def _signal_log_hit_text(self, hit: dict[str, object] | None) -> str:
        if not isinstance(hit, dict):
            return ""
        file_name = Path(str(hit.get("file") or "")).name
        line = str(hit.get("line") or "")
        text = self._signal_shorten(str(hit.get("text") or ""), 180)
        return f"{file_name}:{line} {text}".strip()

    def _signal_runtime_evidence_text(self, signal_payload: dict[str, object], keyword: str) -> str:
        event = self._signal_find_runtime_event(signal_payload, "", "", keyword)
        if event is None:
            return ""
        return self._signal_log_hit_text(event)

    def _signal_source_fact_text(self, source_facts: dict[str, object], key: str) -> str:
        values = source_facts.get(key, []) if isinstance(source_facts, dict) else []
        if not isinstance(values, list):
            return ""
        return "；".join(str(item) for item in values[:3] if str(item).strip())

    def _repo_relative_path(self, file_text: str) -> Path | None:
        if not file_text:
            return None
        path = Path(file_text)
        if path.is_absolute():
            return path if path.exists() else None
        candidate = self.config.guideengine_repo / file_text
        return candidate if candidate.exists() else None

    def _read_text_quiet(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def _signal_repo_signal_references(self, signal_name: str, *, max_refs: int = 80) -> list[dict[str, str]]:
        if not signal_name:
            return []
        repo = Path(self.config.guideengine_repo).expanduser()
        if not repo.exists():
            return []
        try:
            completed = subprocess.run(
                ["rg", "-n", "--fixed-strings", signal_name, str(repo)],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return []
        refs: list[dict[str, str]] = []
        for raw_line in completed.stdout.splitlines():
            if len(refs) >= max_refs:
                break
            match = re.match(r"(.+?):(\d+):(.*)", raw_line)
            if not match:
                continue
            path = Path(match.group(1))
            try:
                file_text = str(path.relative_to(repo))
            except ValueError:
                file_text = str(path)
            refs.append({"file": file_text, "line": match.group(2), "text": match.group(3).strip()})
        return refs

    def _signal_kotlin_class_from_file(self, file_text: str, class_name: str) -> str:
        if not file_text or not class_name:
            return ""
        normalized = file_text.replace("\\", "/")
        marker = "/src/main/java/"
        if marker in normalized:
            normalized = normalized.split(marker, 1)[1]
        elif "src/main/java/" in normalized:
            normalized = normalized.split("src/main/java/", 1)[1]
        else:
            return ""
        normalized = re.sub(r"\.(kt|java)$", "", normalized)
        dotted = normalized.replace("/", ".")
        return dotted if dotted.endswith(f".{class_name}") or dotted == class_name else ""

    def _signal_kotlin_string_constants(self, text: str) -> dict[str, str]:
        constants: dict[str, str] = {}
        for match in re.finditer(r"const\s+val\s+([A-Z0-9_]+)\s*(?::\s*String)?\s*=\s*\"([^\"]+)\"", text):
            constants[match.group(1)] = match.group(2)
        return constants

    def _signal_callback_name_for_event(self, text: str, event_id: str) -> str:
        match = re.search(rf"\b{re.escape(event_id)}\s+to\s+this::([A-Za-z0-9_]+)", text)
        return match.group(1) if match else ""

    def _signal_kotlin_function_block(self, lines: list[str], function_name: str, *, max_lines: int = 140) -> list[tuple[int, str]]:
        start_index = -1
        pattern = re.compile(rf"\bfun\s+{re.escape(function_name)}\b")
        for index, line in enumerate(lines):
            if pattern.search(line):
                start_index = index
                break
        if start_index < 0:
            return []
        block: list[tuple[int, str]] = []
        brace_depth = 0
        opened = False
        for index in range(start_index, min(len(lines), start_index + max_lines)):
            line = lines[index]
            block.append((index + 1, line))
            brace_line = re.sub(r'"(?:\\.|[^"\\])*"', '""', line)
            opens = brace_line.count("{")
            closes = brace_line.count("}")
            if opens:
                opened = True
            if opened:
                brace_depth += opens - closes
                if brace_depth <= 0 and index > start_index:
                    break
        return block

    def _signal_kotlin_target_branch(self, callback_lines: list[tuple[int, str]], signal_name: str) -> list[tuple[int, str]]:
        if not signal_name:
            return callback_lines
        target_indexes = [index for index, (_, line) in enumerate(callback_lines) if signal_name in line]
        if not target_indexes:
            return callback_lines
        result: list[tuple[int, str]] = []
        seen_lines: set[int] = set()
        for target_index in target_indexes:
            start_index = target_index
            for index in range(target_index, -1, -1):
                line = callback_lines[index][1]
                if "->" in line and re.search(r"\b(?:EVENT_KEY|VALUE_KEY)_[A-Z0-9_]+\b|\"[^\"]+\"", line):
                    start_index = index
                    break
            brace_depth = 0
            opened = False
            for index in range(start_index, len(callback_lines)):
                line_no, line = callback_lines[index]
                if line_no not in seen_lines:
                    seen_lines.add(line_no)
                    result.append((line_no, line))
                brace_line = re.sub(r'"(?:\\.|[^"\\])*"', '""', line)
                opens = brace_line.count("{")
                closes = brace_line.count("}")
                if opens:
                    opened = True
                if opened:
                    brace_depth += opens - closes
                    if brace_depth <= 0 and index > start_index:
                        break
                elif index > target_index:
                    break
        return result or callback_lines

    def _signal_consumer_log_terms(self, path: Path, signal_name: str) -> list[str]:
        text = self._read_text_quiet(path)
        if not text:
            return []
        lines = text.splitlines()
        terms: list[str] = []
        for index, line in enumerate(lines):
            if signal_name not in line:
                continue
            nearby_case = lines[max(0, index - 3) : min(len(lines), index + 4)]
            if any("->" in item for item in nearby_case):
                source_lines = [item for _, item in self._signal_kotlin_target_branch([(line_no + 1, value) for line_no, value in enumerate(lines)], signal_name)]
            elif any("getSignalFlow" in item or ".collect" in item for item in lines[index : min(len(lines), index + 16)]):
                source_lines = lines[index : min(len(lines), index + 40)]
            else:
                continue
            for nearby in source_lines:
                literal = self._signal_log_literal_from_source_line(nearby)
                if literal and self._signal_log_literal_matches_signal(literal, signal_name):
                    terms.append(literal)
        return self._unique_nonempty(terms)

    def _signal_log_literal_matches_signal(self, literal: str, signal_name: str) -> bool:
        literal_key = re.sub(r"[^a-z0-9]+", "", literal.casefold())
        if not literal_key:
            return False
        parts = [
            part.casefold()
            for part in re.split(r"[_\W]+", signal_name)
            if len(part) >= 4 and part.casefold() not in {"signal", "powercenter", "change"}
        ]
        return any(part in literal_key for part in parts)

    def _signal_log_literal_from_source_line(self, line: str) -> str:
        match = re.search(r"L\.[idwe]\([^,]+,\s*\"([^\"]+)\"", line)
        if not match:
            return ""
        literal = match.group(1).strip()
        literal = re.split(r"\$\{?|\{", literal, maxsplit=1)[0].strip()
        return literal if len(literal) >= 4 else ""

    def _unique_nonempty(self, values: list[object]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            text = str(value).strip()
            if not text or text in seen:
                continue
            seen.add(text)
            result.append(text)
        return result

    def _signal_stage_label(self, item: dict[str, object]) -> str:
        label = str(item.get("label") or item.get("title") or "")
        text = str(item.get("text") or "")
        lowered = text.casefold()
        if "injectsignalprovider" in lowered:
            return "注入 Provider"
        if "registersignal" in lowered:
            return "注册信号"
        if "getsignalflow" in lowered:
            return "DataCenter 取流"
        generic_label = self._signal_log_semantic_label(text)
        if generic_label:
            return generic_label
        return label or "日志命中"

    def _signal_boundary_issues(
        self,
        signal_payload: dict[str, object],
        focus_scope: dict[str, object],
        fault_time: str,
    ) -> list[dict[str, object]]:
        log_report = signal_payload.get("log_report", {}) if isinstance(signal_payload, dict) else {}
        stages = log_report.get("stages", {}) if isinstance(log_report, dict) else {}
        datacenter_hits = self._signal_target_stage_hits(signal_payload, stages, "datacenter")
        business_hits = self._signal_target_stage_hits(signal_payload, stages, "android_business")
        vhal_hits = self._signal_target_stage_hits(signal_payload, stages, "vhal")
        unity_hits = self._signal_target_stage_hits(signal_payload, stages, "unity_received")
        alignment = self._signal_fault_alignment(fault_time, focus_scope)
        issues = [
            {
                "sev": "green" if datacenter_hits and business_hits else "yellow",
                "title": "当前日志能证明的范围",
                "detail": self._signal_visible_scope_text(signal_payload, focus_scope),
            },
            {
                "sev": alignment["sev"],
                "title": "当前日志不能直接证明现场",
                "detail": alignment["detail"],
            },
            {
                "sev": "yellow",
                "title": "未覆盖段",
                "detail": (
                    f"VHAL/CarService 原始输入命中 {vhal_hits}，Unity 接收命中 {unity_hits}。"
                    " 现有样本更适合判断焦点进程内是否走通，不适合下跨层丢失定案。"
                ),
            },
        ]
        stats = signal_payload.get("detailed_stats", {}) if isinstance(signal_payload, dict) else {}
        signal_code = str((signal_payload.get("signal", {}) or {}).get("code") or "")
        stat = stats.get(signal_code) if isinstance(stats, dict) else None
        if isinstance(stat, dict):
            issues.append(
                {
                    "sev": "yellow",
                    "title": "为什么不再单独做“损失分析”结论",
                    "detail": (
                        f"当前 drop / unity counter 基本为 0（unity_drop_total={stat.get('unity_drop_total', 0)}，"
                        f"unity_recv_total={stat.get('unity_recv_total', 0)}），这更像“没有足够下游埋点”而不是“已经证明无损失”。"
                    ),
                }
            )
        return issues

    def _signal_visible_scope_text(self, signal_payload: dict[str, object], focus_scope: dict[str, object]) -> str:
        log_report = signal_payload.get("log_report", {}) if isinstance(signal_payload, dict) else {}
        stages = log_report.get("stages", {}) if isinstance(log_report, dict) else {}
        datacenter_hits = self._signal_target_stage_hits(signal_payload, stages, "datacenter")
        business_hits = self._signal_target_stage_hits(signal_payload, stages, "android_business")
        package = str(focus_scope.get("package") or "目标进程")
        pid = str(focus_scope.get("pid") or "未识别")
        chain = self._signal_coverage_note(signal_payload, focus_scope)
        if datacenter_hits and business_hits:
            return f"已看到 {package} / PID {pid} 内的目标信号 {chain}。"
        if datacenter_hits:
            return f"目标信号已进入 DataCenter，但业务消费证据不足（{package} / PID {pid}）。"
        return "当前样本还没有锁定到目标进程内的有效链路。"

    def _signal_coverage_note(self, signal_payload: dict[str, object], focus_scope: dict[str, object]) -> str:
        log_report = signal_payload.get("log_report", {}) if isinstance(signal_payload, dict) else {}
        stages = log_report.get("stages", {}) if isinstance(log_report, dict) else {}
        parts = ["DataCenter"]
        if self._signal_target_stage_hits(signal_payload, stages, "android_business"):
            parts.append("业务消费")
        if self._signal_has_receiver_like_evidence(signal_payload):
            parts.append("状态消费")
        return " -> ".join(parts)

    def _signal_scoped_summary_text(self, signal_payload: dict[str, object]) -> str:
        raw_summary = str(signal_payload.get("summary") or "").strip()
        log_report = signal_payload.get("log_report", {}) if isinstance(signal_payload, dict) else {}
        stages = log_report.get("stages", {}) if isinstance(log_report, dict) else {}
        signal = signal_payload.get("signal", {}) if isinstance(signal_payload, dict) else {}
        code = str(signal.get("code") or "").strip()
        name = str(signal.get("name") or "").strip()
        target = " ".join(part for part in (code, name) if part).strip() or "目标信号"
        datacenter_hits = self._signal_target_stage_hits(signal_payload, stages, "datacenter")
        business_hits = self._signal_target_stage_hits(signal_payload, stages, "android_business")
        android_unity_hits = self._signal_target_stage_hits(signal_payload, stages, "android_unity")
        unity_hits = self._signal_target_stage_hits(signal_payload, stages, "unity_received")
        if not any((datacenter_hits, business_hits, android_unity_hits, unity_hits)):
            return raw_summary
        if datacenter_hits and not any((business_hits, android_unity_hits, unity_hits)):
            return (
                f"目标信号 {target} 当前仅看到 DataCenter 取流/订阅命中，"
                "未看到目标信号业务消费、AndroidUnityProxy 或 Unity 接收证据；"
                "其它 signal 的业务日志不计入本链路。"
            )
        reached = ["DataCenter"] if datacenter_hits else []
        if business_hits:
            reached.append("业务消费")
        if android_unity_hits:
            reached.append("AndroidUnityProxy")
        if unity_hits:
            reached.append("Unity 接收")
        return f"目标信号 {target} 当前已命中 {' -> '.join(reached)}，仍需按证据边界确认未覆盖段。"

    def _signal_has_receiver_like_evidence(self, signal_payload: dict[str, object]) -> bool:
        refs = signal_payload.get("source_references", []) if isinstance(signal_payload, dict) else []
        if self._signal_select_state_receiver_reference(refs) is not None:
            return True
        log_report = signal_payload.get("log_report", {}) if isinstance(signal_payload, dict) else {}
        stages = log_report.get("stages", {}) if isinstance(log_report, dict) else {}
        stage = stages.get("android_business") if isinstance(stages, dict) else None
        if not isinstance(stage, dict):
            return False
        for example in stage.get("examples", []) or []:
            if not isinstance(example, dict):
                continue
            if not self._signal_item_matches_target(signal_payload, example):
                continue
            label = self._signal_log_semantic_label(str(example.get("text") or ""))
            if label == "状态消费":
                return True
        return False

    def _signal_provider_injection_title(self, signal_payload: dict[str, object]) -> str:
        context = (signal_payload.get("lifecycle_report", {}) or {}).get("context", {})
        helper_class = ""
        if isinstance(context, dict):
            helper_class = str(context.get("helper_class") or "").strip()
        return f"DataCenter 注入 {helper_class}" if helper_class else "DataCenter 注入 Provider"

    def _signal_registration_title(self, signal_payload: dict[str, object]) -> str:
        signal = signal_payload.get("signal", {}) if isinstance(signal_payload, dict) else {}
        code = str(signal.get("code") or "").strip()
        return f"注册 {code} 到 XData" if code else "注册信号到 XData"

    def _signal_datacenter_stage_title(self, signal_payload: dict[str, object]) -> str:
        signal = signal_payload.get("signal", {}) if isinstance(signal_payload, dict) else {}
        code = str(signal.get("code") or "").strip()
        return f"DataCenter 读取 {code} Flow" if code else "DataCenter 读取信号 Flow"

    def _signal_business_stage_title(self, signal_payload: dict[str, object]) -> str:
        return "业务侧消费到有效值"

    def _signal_consumer_stage_title(self, item: dict[str, object]) -> str:
        stage = self._signal_stage_label(item)
        if stage == "状态消费":
            return "状态消费已更新"
        if stage == "业务消费":
            return "业务消费已更新"
        return "下游状态已更新"

    def _signal_data_center_title(self) -> str:
        return "DataCenter.dispatchSignal / getSignalFlow"

    def _signal_select_consumer_reference(self, refs: object) -> dict[str, str] | None:
        if not isinstance(refs, list):
            return None
        ranked: list[tuple[tuple[int, int, int], dict[str, str]]] = []
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            file_text = str(ref.get("file") or "")
            line_text = str(ref.get("text") or "")
            lowered_file = file_text.casefold()
            lowered_line = line_text.casefold()
            if "getsignalflow" not in lowered_line and "collect {" not in lowered_line:
                continue
            score = (
                1 if any(token in lowered_file for token in ("collector", "service", "manager", "config", "viewmodel", "receiver")) else 0,
                0 if "datacenter" in lowered_file else 1,
                1 if "/business/" in lowered_file or "/manager_" in lowered_file else 0,
            )
            ranked.append(
                (
                    score,
                    {
                        "file": file_text,
                        "line": str(ref.get("line") or ""),
                        "text": line_text,
                    },
                )
            )
        if not ranked:
            return None
        ranked.sort(key=lambda item: item[0], reverse=True)
        return ranked[0][1]

    def _signal_select_state_receiver_reference(self, refs: object) -> dict[str, str] | None:
        if not isinstance(refs, list):
            return None
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            file_text = str(ref.get("file") or "")
            lowered = file_text.casefold()
            if any(token in lowered for token in ("receiver", "observer", "viewmodel", "state")):
                return {
                    "file": file_text,
                    "line": str(ref.get("line") or ""),
                    "text": str(ref.get("text") or ""),
                }
        return None

    def _signal_consumer_reference_title(self, ref: dict[str, str]) -> str:
        stem = Path(ref["file"]).stem
        text = ref["text"].casefold()
        if "receiver" in stem.casefold() or "observer" in stem.casefold():
            return f"{stem} 更新状态"
        if "viewmodel" in stem.casefold():
            return f"{stem} 消费状态"
        if "collector" in stem.casefold() or "manager" in stem.casefold() or "config" in stem.casefold():
            return f"{stem} 消费信号"
        if "collect {" in text or "getsignalflow" in text:
            return f"{stem} 消费信号"
        return stem

    def _signal_log_semantic_label(self, text: str) -> str:
        lowered = text.casefold()
        if any(token in lowered for token in ("state applied", "state updated", "update ", "receiver")):
            return "状态消费"
        if any(token in lowered for token in ("collect", "collector", "value=", "statevalue=", " it.value=")):
            return "业务消费"
        return ""

    def _signal_target_stage_hits(self, signal_payload: dict[str, object], stages: object, stage_key: str) -> int:
        if not isinstance(stages, dict):
            return 0
        stage = stages.get(stage_key)
        if not isinstance(stage, dict):
            return 0
        signal = signal_payload.get("signal", {}) if isinstance(signal_payload, dict) else {}
        code = str(signal.get("code") or "").strip()
        codes = stage.get("codes")
        if code and isinstance(codes, dict):
            value = codes.get(code)
            if isinstance(value, int):
                return value
        count = 0
        for example in stage.get("examples", []) or []:
            if isinstance(example, dict) and self._signal_item_matches_target(signal_payload, example):
                count += 1
        return count

    def _signal_source_rows(
        self,
        signal_payload: dict[str, object],
        source_evidence_path: Path | None,
    ) -> list[tuple[str, str, str]]:
        rows: list[tuple[str, str, str]] = []
        lifecycle = signal_payload.get("lifecycle_report", {}) if isinstance(signal_payload, dict) else {}
        lifecycle_refs = lifecycle.get("source_references", []) if isinstance(lifecycle, dict) else []
        for ref in lifecycle_refs[:4]:
            if not isinstance(ref, dict):
                continue
            rows.append(
                (
                    "生命周期",
                    f"{ref.get('file') or ''}:{ref.get('line') or ''}",
                    self._signal_shorten(str(ref.get("text") or ""), 90),
                )
            )
        business_entry = self._signal_select_business_entry(source_evidence_path, prompt_text="", signal_payload=signal_payload)
        if business_entry is not None:
            rows.append(
                (
                    "业务判定",
                    f"{business_entry['file']}:{business_entry['line']}",
                    self._signal_shorten(business_entry["text"], 90),
                )
            )
        return rows[:6]

    def _signal_select_business_entry(
        self,
        source_evidence_path: Path | None,
        *,
        prompt_text: str,
        signal_payload: dict[str, object] | None = None,
    ) -> dict[str, str] | None:
        entries = self._parse_source_evidence_entries(source_evidence_path)
        if not entries:
            return None
        if signal_payload:
            target_entries = [entry for entry in entries if self._signal_business_entry_matches_target(entry, signal_payload)]
            if target_entries:
                entries = target_entries
            else:
                entries = [entry for entry in entries if self._signal_business_entry_is_meaningful_fallback(entry)]
                if not entries:
                    return None
        prompt_terms = self._signal_prompt_terms(prompt_text)
        priority_tokens = ("dispatcher", "action", "viewmodel", "receiver", "fragment", "service", "scene")

        def score(entry: dict[str, str]) -> tuple[int, int, int, int]:
            haystack = f"{entry['file']}\n{entry['text']}".casefold()
            hint_rank = 0
            for index, token in enumerate(priority_tokens, start=1):
                if token in haystack:
                    hint_rank = len(priority_tokens) - index + 1
                    break
            prompt_match = any(term.casefold() in haystack for term in prompt_terms)
            is_constant_only = 1 if any(token in haystack for token in ("constants", "eventid", "strings.xml")) else 0
            return (
                1 if prompt_match else 0,
                hint_rank,
                0 if is_constant_only else 1,
                1 if "carvcuhelper" not in haystack and "datacenter" not in haystack else 0,
            )
        ranked = sorted(entries, key=score, reverse=True)
        return ranked[0] if ranked else None

    def _signal_business_entry_matches_target(self, entry: dict[str, str], signal_payload: dict[str, object]) -> bool:
        signal = signal_payload.get("signal", {}) if isinstance(signal_payload, dict) else {}
        signal_name = str(signal.get("name") or "").strip()
        signal_code = str(signal.get("code") or "").strip()
        haystack = f"{entry.get('file') or ''}\n{entry.get('text') or ''}"
        lower_file = str(entry.get("file") or "").casefold()
        if lower_file.endswith(".xml") or "/res/" in lower_file:
            return False
        if "module_proto" in lower_file or "module_datacenter" in lower_file:
            return False
        return bool((signal_name and signal_name in haystack) or (signal_code and re.search(rf"\b{re.escape(signal_code)}\b", haystack)))

    def _signal_business_entry_is_meaningful_fallback(self, entry: dict[str, str]) -> bool:
        file_text = str(entry.get("file") or "")
        line_text = str(entry.get("text") or "")
        lower_file = file_text.casefold()
        lower_line = line_text.strip().casefold()
        if lower_file.endswith(".xml") or "/res/" in lower_file:
            return False
        if "module_proto" in lower_file or "module_datacenter" in lower_file:
            return False
        if lower_line.startswith(("import ", "package ", "see ", "*", "//")):
            return False
        haystack = f"{lower_file}\n{lower_line}"
        if not any(token in haystack for token in ("collector", "receiver", "viewmodel", "dispatcher", "service", "scene", "action", "judge", "flow")):
            return False
        return any(token in lower_line for token in ("signal", "state", "flow", "collect", "register", "update", "value", "invalid"))

    def _parse_source_evidence_entries(self, source_evidence_path: Path | None) -> list[dict[str, str]]:
        if source_evidence_path is None or not source_evidence_path.exists():
            return []
        entries: list[dict[str, str]] = []
        current_file = ""
        try:
            lines = source_evidence_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        for line in lines:
            if line.startswith("## "):
                current_file = line[3:].strip()
                continue
            match = re.match(r"- L(\d+): `(.+)`", line.strip())
            if not match or not current_file:
                continue
            entries.append({"file": current_file, "line": match.group(1), "text": match.group(2)})
        return entries

    def _signal_prompt_terms(self, prompt_text: str) -> list[str]:
        raw_terms = re.findall(r"[A-Za-z_]{4,}|[\u4e00-\u9fff]{2,8}", prompt_text or "")
        ignored = {"分析", "源码", "参考", "信号链路", "主要是", "请参考"}
        terms: list[str] = []
        for term in raw_terms:
            cleaned = term.strip()
            if not cleaned or cleaned in ignored:
                continue
            if cleaned not in terms:
                terms.append(cleaned)
        return terms[:8]

    def _signal_find_source_reference(
        self,
        refs: object,
        predicate: Callable[[str, str], bool],
    ) -> dict[str, str] | None:
        if not isinstance(refs, list):
            return None
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            file_text = str(ref.get("file") or "").casefold()
            line_text = str(ref.get("text") or "").casefold()
            if predicate(file_text, line_text):
                return {
                    "file": str(ref.get("file") or ""),
                    "line": str(ref.get("line") or ""),
                    "text": str(ref.get("text") or ""),
                }
        return None

    def _signal_package_from_path(self, path_text: str) -> str:
        match = re.search(r"/app/([^/]+)/", path_text)
        if match:
            return match.group(1)
        match = re.search(r"_app_([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+){2,})_", path_text)
        if match:
            return match.group(1)
        match = re.search(r"/([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+){2,})(?:/|$)", path_text)
        return match.group(1) if match else ""

    def _signal_pid_from_text(self, text: str) -> str:
        match = re.match(r"\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+\s+(\d+)\s+", text)
        if match:
            return match.group(1)
        match = re.search(r"\[(\d+),\d+\]\[20\d{2}-\d{2}-\d{2}", text)
        return match.group(1) if match else ""

    def _signal_tid_from_text(self, text: str) -> str:
        match = re.match(r"\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+\s+\d+\s+(\d+)\s+", text)
        return match.group(1) if match else ""

    def _signal_shorten(self, text: str, limit: int) -> str:
        normalized = text.strip()
        if len(normalized) <= limit:
            return normalized
        return normalized[: limit - 1].rstrip() + "…"

    def _select_target_session(self, sessions: object, fault_time: str) -> dict[str, object] | None:
        if not isinstance(sessions, list) or not sessions:
            return None
        fault_dt = self._parse_fault_datetime(fault_time)
        if fault_dt is None:
            return None
        target_epoch = time.mktime(fault_dt)
        ranked: list[tuple[float, dict[str, object]]] = []
        for session in sessions:
            if not isinstance(session, dict):
                continue
            start = session.get("start")
            if not isinstance(start, str):
                continue
            try:
                session_epoch = time.mktime(time.strptime(start[:16], "%Y-%m-%dT%H:%M"))
            except ValueError:
                continue
            ranked.append((abs(session_epoch - target_epoch), session))
        if not ranked:
            return None
        ranked.sort(key=lambda item: item[0])
        return ranked[0][1]

    def _time_match_note(self, fault_time: str, sessions: object) -> str:
        if not fault_time:
            return "未识别故障时间，无法校验附件日志是否匹配。"
        if not isinstance(sessions, list) or not sessions:
            return "报告未产出会话，无法校验附件日志是否匹配。"
        fault_hour = fault_time[:13]
        for session in sessions:
            if not isinstance(session, dict):
                continue
            start = str(session.get("start", ""))
            if start.startswith(fault_hour):
                return f"附件日志中命中了故障小时 `{fault_hour}`。"
        starts = [str(session.get("start", "")) for session in sessions if isinstance(session, dict)]
        preview = "、".join(starts[:3]) if starts else "无"
        return f"附件日志未命中故障小时 `{fault_hour}`，实际捕获到的启动会话起点示例：{preview}"

    def _failure(
        self,
        *,
        context,
        command: list[str],
        started: float,
        message: str,
        error_code: str,
        stdout: str = "",
        stderr: str = "",
        progress_callback: Callable[[dict[str, object]], None] | None = None,
        details: dict[str, object] | None = None,
    ) -> TaskResult:
        self._emit_progress(
            progress_callback,
            stage="bug_failed",
            message=message,
            job_id=context.job_id,
            error_code=error_code,
        )
        payload_details = {"mode": "bug_analysis"}
        if isinstance(details, dict):
            payload_details.update(details)
        return TaskResult(
            success=False,
            message=message,
            job_id=context.job_id,
            job_dir=context.job_dir,
            command=command,
            duration_seconds=time.monotonic() - started,
            error_code=error_code,
            stdout=stdout,
            stderr=stderr,
            details=payload_details,
        )

    def _custom_skill_executor_not_ready_message(self, skill_name: str, *, selected_input: Path | None) -> str:
        display_name = skill_name.strip() or "source_code_skill"
        log_note = f"\n日志输入已准备：`{selected_input}`" if selected_input else ""
        route_note = ""
        try:
            record = self.skill_manager.get_skill(display_name, include_content=False)
        except Exception:
            record = None
        if record is None:
            route_note = "\n当前状态：未找到 Skill 目录或主路由记录。"
        elif record.kind not in _SOURCE_SKILL_KINDS:
            route_note = f"\n当前状态：已配置主路由，但 kind=`{record.kind or '未配置'}`，不是 source_code_skill。"
        elif not record.executor:
            route_note = "\n当前状态：已配置为 source_code_skill，但 executor 为空；需要配置 executor=`file_agent`。"
        elif record.executor != "file_agent":
            route_note = f"\n当前状态：已配置 executor=`{record.executor}`，但当前只支持 file_agent。"
        return (
            f"已命中专用 Skill `{display_name}`，但当前没有可执行源码分析器，尚未执行实际日志分析。"
            f"{route_note}{log_note}\n不会基于占位报告给出根因结论。请为该 Skill 配置文件 Agent 执行器后重试。"
        )

    def _custom_skill_agent_tools(self) -> list[str]:
        tools = list(self.config.claude_agent.allowed_tools or ["Read", "Grep", "Glob", "LS"])
        # Grep already covers repo-wide text search; add Bash only for targeted
        # read-only shell probes when file listings or one-off counts are needed.
        if "Bash" not in tools:
            tools.append("Bash")
        return tools

    def _custom_skill_agent_source_roots(self) -> list[Path]:
        roots = list(self.config.source_investigation.repo_roots or [])
        if self.config.guideengine_repo not in roots:
            roots.append(self.config.guideengine_repo)
        resolved: list[Path] = []
        for root in roots:
            try:
                candidate = root.expanduser().resolve()
            except OSError:
                continue
            if candidate.exists() and candidate not in resolved:
                resolved.append(candidate)
        return resolved

    def _sanitize_file_agent_text(self, text: str) -> str:
        sanitized = re.sub(r"https?://\S+", "", text or "")
        sanitized = re.sub(r"@[^\s，。；、]+", "", sanitized)
        sanitized = re.sub(r"\s+", " ", sanitized).strip()
        return sanitized

    def _summarize_prior_report_jsons(
        self,
        report_jsons: dict[str, Path | None],
    ) -> list[tuple[str, str, str]]:
        summaries: list[tuple[str, str, str]] = []
        for kind, json_path in report_jsons.items():
            if json_path is None or not json_path.exists():
                continue
            try:
                data = json.loads(json_path.read_text(encoding="utf-8", errors="replace"))
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(data, dict):
                continue
            verdict = data.get("verdict")
            if isinstance(verdict, dict):
                verdict_text = str(verdict.get("msg") or verdict.get("text") or "")
                sev = str(verdict.get("sev") or "")
            else:
                verdict_text = str(verdict or "")
                sev = ""
            label = self._analysis_label(kind)
            if verdict_text:
                summaries.append((label, kind, f"[{sev}] {verdict_text}" if sev else verdict_text))
        return summaries

    def _extract_text_from_agent_json(self, raw_stdout: str, analysis_markdown_path: Path) -> str:
        if not raw_stdout.strip():
            return ""
        try:
            data = json.loads(raw_stdout)
        except json.JSONDecodeError:
            text = raw_stdout.strip()
            if text:
                analysis_markdown_path.write_text(text + "\n", encoding="utf-8")
            return text
        if not isinstance(data, dict):
            text = raw_stdout.strip()
            if text:
                analysis_markdown_path.write_text(text + "\n", encoding="utf-8")
            return text
        result = str(data.get("result") or "").strip()
        if result:
            analysis_markdown_path.write_text(result + "\n", encoding="utf-8")
            return result
        text_parts: list[str] = []
        for block in data.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                text_parts.append(str(block.get("text") or ""))
        if text_parts:
            text = "\n\n".join(text_parts).strip()
            analysis_markdown_path.write_text(text + "\n", encoding="utf-8")
            return text
        return ""

    def _extract_partial_markdown_from_agent_stream(self, raw_stdout: str, *, skill_name: str) -> str:
        if not raw_stdout.strip():
            return ""
        messages: list[str] = []
        seen: set[str] = set()
        for line in raw_stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            item = payload.get("item")
            if not isinstance(item, dict) or item.get("type") != "agent_message":
                continue
            text = str(item.get("text") or "").strip()
            if len(text) < 20 or text in seen:
                continue
            seen.add(text)
            messages.append(text)
        if not messages:
            return ""
        bullets = messages[-3:]
        lines = ["## 阶段性结论（超时前）"]
        lines.extend(f"- {text}" for text in bullets)
        lines.extend(
            [
                "",
                "## 当前缺口",
                f"- 专用 Skill `{skill_name}` 文件 Agent 在整理最终关键证据前超时，未生成完整的 `source_stage_analysis.md`。",
                "",
                "## 建议动作",
                "- 优先复用以上阶段性结论继续追问，或放宽/替换当前文件 Agent 路径后重跑源码阶段。",
            ]
        )
        return "\n".join(lines).strip()

    def _file_agent_log_rules(self, analysis_kind: str) -> list[str]:
        rules = [
            "App 主日志通常位于 `data/Log/log*/app/<package>/<prefix>_YYYY-MM-DD_HH-MM.alog(.log)`，如 `main_...` 或 `user0_main_...`；前缀不固定，时间段格式固定。",
            "若同时存在解码后的 `.alog.log` / `.xlog.log` 与原始 `.alog` / `.xlog`，优先读取解码后的 `.log`。",
            "排查时优先围绕故障时间前后 1 小时内的日志，不要先全量递归扫描整个缓存树。",
            "优先读取 bridge 预先收敛后的 `log_focus/` 和 `log_focus.md`，只有证据不足时才扩展到更多日志文件。",
        ]
        return rules

    def _file_agent_search_budget_rules(self) -> list[str]:
        return [
            "不要在整个源码根目录直接执行无边界 `rg`；先用上下文、Skill、预检索证据把范围收敛到具体模块或 1-3 个候选目录。",
            "所有可能命中很多结果的 `rg` / `grep` 必须加输出预算，例如 `rg --max-count 80 ... <dir>` 或 `rg ... <dir> | head -80`。",
            "如果一次检索返回超过 80 行或明显命中无关模块，停止阅读大段输出，改用更窄关键词、`--glob`、文件名或子目录重查。",
            "日志扩展也必须先限定包名、时间窗和关键词；不要对整个日志缓存做无边界递归搜索。",
            "每轮最多保留最有价值的少量源码/日志锚点，优先读具体文件行号，再产出结论；不要把大段检索结果当作分析正文。",
        ]

    def _file_agent_focus_candidates(
        self,
        *,
        input_path: Path | None,
        fault_time: str,
        analysis_kind: str,
    ) -> list[Path]:
        if input_path is None or not input_path.exists():
            return []
        if input_path.is_file():
            return [input_path]
        fault_dt = self._parse_bug_datetime(fault_time)
        all_files = self._iter_log_coverage_files(input_path)
        if not all_files:
            return []
        selected: list[Path] = []
        decoded_aux: list[Path] = []
        for path in all_files:
            if path.name in {"prop.txt", "dfx.txt"}:
                decoded_aux.append(path)
                continue
            file_dt = self._parse_log_file_datetime(path.name)
            if fault_dt is None or file_dt is None:
                continue
            candidate_dt = datetime.fromtimestamp(time.mktime(file_dt))
            if abs((candidate_dt - fault_dt).total_seconds()) > 3600:
                continue
            selected.append(path)
        if not selected and fault_dt is not None:
            for path in all_files:
                file_dt = self._parse_log_file_datetime(path.name)
                if file_dt is None:
                    continue
                candidate_dt = datetime.fromtimestamp(time.mktime(file_dt))
                if abs((candidate_dt - fault_dt).total_seconds()) <= 3600:
                    selected.append(path)
        ranked = sorted(
            {path.resolve() for path in [*decoded_aux, *selected]},
            key=lambda item: (self._log_file_priority(item), str(item)),
        )
        return ranked[:24]

    def _build_file_agent_focus_dir(
        self,
        *,
        input_path: Path | None,
        fault_time: str,
        analysis_kind: str,
        analysis_dir: Path,
    ) -> tuple[Path | None, Path | None, list[Path]]:
        candidates = self._file_agent_focus_candidates(
            input_path=input_path,
            fault_time=fault_time,
            analysis_kind=analysis_kind,
        )
        if not candidates:
            return input_path, None, []
        focus_dir = analysis_dir / "log_focus"
        manifest_path = analysis_dir / "log_focus.md"
        focus_dir.mkdir(parents=True, exist_ok=True)
        copied: list[Path] = []
        for source in candidates:
            if input_path is not None and input_path.exists() and input_path.is_dir():
                try:
                    relative = source.relative_to(input_path)
                except ValueError:
                    relative = Path(source.name)
            else:
                relative = Path(source.name)
            dest = focus_dir / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(source, dest)
            except OSError:
                continue
            copied.append(dest)
        lines = [
            "# Log Focus",
            "",
            f"- 原始日志输入: `{input_path}`" if input_path else "- 原始日志输入: 未提供",
            f"- 聚焦目录: `{focus_dir}`",
            f"- 故障时间: `{fault_time or '未识别'}`",
            "",
            "## 已挑选文件",
        ]
        if copied:
            lines.extend(f"- `{path}`" for path in copied)
        else:
            lines.append("- 未复制出聚焦文件，继续使用原始输入目录。")
        manifest_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        return (focus_dir if copied else input_path), manifest_path, copied

    def _write_file_agent_context(
        self,
        *,
        analysis_kind: str,
        skill_name: str,
        request_text: str,
        prompt_text: str,
        title: str,
        description: str,
        fault_time: str,
        original_selected_input: Path | None,
        focused_log_input: Path | None,
        log_focus_manifest: Path | None,
        source_evidence_path: Path | None,
        analysis_dir: Path,
        analysis_markdown_path: Path,
        prior_findings: list[tuple[str, str, str]] | None = None,
    ) -> Path:
        context_path = analysis_dir / self._skill_agent_sidecar_name(analysis_kind, "context.md")
        analysis_dir.mkdir(parents=True, exist_ok=True)
        source_roots = self._custom_skill_agent_source_roots()
        sanitized_request = self._sanitize_file_agent_text(prompt_text or request_text) or "未提供"
        sanitized_description = self._sanitize_file_agent_text(description) or ""
        lines = [
            "# File Agent Context",
            "",
            "## 1. 用户请求（最重要）",
            f"- **用户请求**: {sanitized_request}",
            f"- Bug 标题: {title.strip() or '未提供'}",
            f"- 故障时间: {fault_time or '未识别'}",
        ]
        if sanitized_description and sanitized_description != sanitized_request:
            lines.append(f"- 缺陷描述: {sanitized_description[:800]}")
        lines.extend(
            [
                "",
                "## 2. 已下载好的日志路径",
                f"- 原始日志目录: `{original_selected_input}`" if original_selected_input else "- 原始日志目录: 未提供",
                f"- 本轮聚焦日志目录: `{focused_log_input}`" if focused_log_input else "- 本轮聚焦日志目录: 未生成",
                f"- 聚焦清单: `{log_focus_manifest}`" if log_focus_manifest else "- 聚焦清单: 未生成",
                "",
                "## 3. Skill 目录",
            ]
        )
        skill_paths = self._skill_context_paths(skill_name)
        if skill_paths:
            lines.extend(f"- `{path}`" for path in skill_paths)
        else:
            lines.append("- 未找到 Skill 上下文文件。")
        priority_files = self._domain_priority_files(skill_name)
        if priority_files:
            lines.extend(
                [
                    "",
                    "## 3.1 领域优先源码文件",
                    "- 先读这些文件，再决定是否扩展搜索；不要先从整个源码根做全仓扫描。",
                ]
            )
            lines.extend(f"- `{path}`" for path in priority_files)
        lines.extend(
            [
                "",
                "## 4. 日志规则",
                *[f"- {rule}" for rule in self._file_agent_log_rules(analysis_kind)],
                "",
                "## 检索预算",
                *[f"- {rule}" for rule in self._file_agent_search_budget_rules()],
                "",
                "## 5. 源码路径",
            ]
        )
        if source_roots:
            lines.extend(f"- `{path}`" for path in source_roots)
        else:
            lines.append("- 未配置源码根目录。")
        lines.append(f"- 预检索源码证据: `{source_evidence_path}`" if source_evidence_path else "- 预检索源码证据: 未生成")
        if prior_findings:
            lines.extend(
                [
                    "",
                    "## 6. 前序分析结论（已完成的其他分析，供参考）",
                ]
            )
            for label, kind, verdict in prior_findings:
                lines.append(f"- **{label}** (`{kind}`): {verdict}")
            lines.append("- 以上结论来自其他专用 Skill，请在此基础上做源码级深入分析。")
        lines.extend(
            [
                "",
                "## 输出要求",
                "- 只输出 Markdown 正文，不要输出代码块围栏，不要输出 HTML。",
                "- 必须包含这些二级标题：`## 结论摘要`、`## 关键证据`、`## 最可能原因`、`## 待确认项`、`## 建议动作`。",
                "- `## 关键证据` 不能为空，每条证据都要能回指到日志文件+时间，或源码文件+行号，或 bridge 生成的工具结果文件。",
                f"- 最终正文会由 bridge 保存到 `{analysis_markdown_path}`。",
                "",
                "## 执行约束",
                "- 本次只读分析，不可修改源码内容，不可修改任何本地文件。",
                "- 优先使用 Read/Grep/Glob/LS；只有在必要时才用 Bash 做只读命令。",
                "- Bash 只允许只读命令，例如 `rg`、`grep`、`find`、`ls`、`head`、`tail`、`sed -n`、`wc`。",
                "- 禁止无边界递归扫描整个 bug cache；先看聚焦日志目录和聚焦清单，再按需扩展。",
                "- 原始 bug 链接已经结构化，不需要重复复述链接。",
            ]
        )
        context_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        return context_path

    def _build_custom_skill_agent_command(
        self,
        *,
        analysis_kind: str = "source_code_skill",
        skill_name: str,
        request_text: str,
        prompt_text: str,
        title: str,
        description: str,
        fault_time: str,
        selected_input: Path | None,
        prepared_input: Path | None,
        source_evidence_path: Path | None,
        analysis_markdown_path: Path,
        context_path: Path | None = None,
        debug_log_path: Path | None = None,
        provider_override: str = "",
        command_override: str = "",
        prior_findings: list[tuple[str, str, str]] | None = None,
    ) -> dict[str, object]:
        provider = _normalize_provider_name(provider_override or self.config.bug_analysis.provider)
        if command_override.strip():
            command_name = command_override.strip()
        elif provider_override:
            command_name = _default_command_for_provider(provider)
        else:
            command_name = (self.config.bug_analysis.command or "").strip() or _default_command_for_provider(provider)
        if not provider or not command_name:
            detected_provider, detected_command = _detect_available_provider()
            if detected_provider and detected_command:
                provider = provider or detected_provider
                command_name = command_name or detected_command
        prompt = self._build_custom_skill_agent_prompt(
            analysis_kind=analysis_kind,
            skill_name=skill_name,
            request_text=request_text,
            prompt_text=prompt_text,
            title=title,
            description=description,
            fault_time=fault_time,
            selected_input=selected_input,
            prepared_input=prepared_input,
            source_evidence_path=source_evidence_path,
            analysis_markdown_path=analysis_markdown_path,
            context_path=context_path,
            writes_output_file=provider == "codex",
            prior_findings=prior_findings,
        )
        if not provider or not command_name:
            return {"command": [], "provider": provider, "prompt": prompt, "output_path": analysis_markdown_path}
        if provider == "codex":
            model = (self.config.bug_analysis.model or "").strip()
            working_dir = self._custom_skill_agent_cwd()
            command = [
                command_name,
                "exec",
                "--skip-git-repo-check",
                "-s",
                "read-only",
                "-C",
                str(working_dir),
            ]
            if model:
                command.extend(["-m", model])
            command.extend(["--json", "--output-last-message", str(analysis_markdown_path), prompt])
            return {
                "command": command,
                "provider": provider,
                "model": model,
                "prompt": prompt,
                "output_path": analysis_markdown_path,
                "output_mode": "tool_written_file",
                "cwd": working_dir,
            }
        if provider in {"claude", "claude-code", "claude_code"}:
            model = (self.config.claude_agent.model or "").strip()
            allowed_tools = self._custom_skill_agent_tools()
            command = [
                command_name,
                "--print",
                "--output-format",
                "json",
                "--no-session-persistence",
                "--disable-slash-commands",
                "--permission-mode",
                "bypassPermissions",
                "--tools",
                ",".join(allowed_tools),
                "--allowedTools",
                ",".join(allowed_tools),
                "--append-system-prompt",
                (
                    "你是通过飞书触发的专用 Skill 执行 Agent。"
                    "你负责读取本地日志、源码和 SKILL.md 产出执行证据，不负责跳过证据直接总结。"
                    "只读分析，不修改任何文件。输出中文 Markdown。"
                    "在所有工具调用完成后，必须输出一段完整的中文 Markdown 分析报告正文。"
                ),
            ]
            if debug_log_path is not None:
                command.extend(["--debug-file", str(debug_log_path)])
            command.extend(["--add-dir", str(analysis_markdown_path.parent)])
            for directory in self._custom_skill_agent_add_dirs(
                skill_name=skill_name,
                selected_input=selected_input,
                prepared_input=prepared_input,
                source_evidence_path=source_evidence_path,
                context_path=context_path,
            ):
                command.extend(["--add-dir", str(directory)])
            return {
                "command": command,
                "provider": provider,
                "model": model,
                "prompt": prompt,
                "output_path": analysis_markdown_path,
                "output_mode": "stdout_json",
                "cwd": analysis_markdown_path.parent,
                "input_mode": "stdin",
            }
        return {"command": [], "provider": provider, "prompt": prompt, "output_path": analysis_markdown_path}

    def _build_custom_skill_agent_prompt(
        self,
        *,
        analysis_kind: str,
        skill_name: str,
        request_text: str,
        prompt_text: str,
        title: str,
        description: str,
        fault_time: str,
        selected_input: Path | None,
        prepared_input: Path | None,
        source_evidence_path: Path | None,
        analysis_markdown_path: Path,
        context_path: Path | None,
        writes_output_file: bool,
        prior_findings: list[tuple[str, str, str]] | None = None,
    ) -> str:
        sanitized_request = self._sanitize_file_agent_text(prompt_text or request_text) or "未提供"
        lines = [
            f"用户请求: {sanitized_request}",
            f"Bug 标题: {title.strip() or '未提供'}",
            f"故障时间: {fault_time or '未识别'}",
            "",
            "请执行专用 Skill 的实际源码/证据分析，而不是做最终总结。",
            "",
            "执行入口：",
            f"- 必须先读取 `{context_path}`，里面包含日志路径、Skill 目录、源码路径和前序分析结论。" if context_path else "- 必须先读取当前分析目录中的上下文文件。",
            f"- 默认只在 `{prepared_input}` 内检索日志。" if prepared_input else "- 默认只在上下文列出的日志范围内检索。",
            f"- 命中 Skill: `{skill_name}`（分析类型：`{analysis_kind}`）。",
            "",
        ]
        priority_files = self._domain_priority_files(skill_name)
        if priority_files:
            lines.append("优先源码文件：")
            for path in priority_files:
                lines.append(f"- `{path}`")
            lines.extend(["- 先读取这些文件，不要先对整个源码仓做无边界搜索。", ""])
        if prior_findings:
            lines.append("前序分析结论（已完成的其他 Skill 分析，供参考）：")
            for label, kind, verdict in prior_findings:
                lines.append(f"- {label}: {verdict}")
            lines.extend(["", "请在以上结论基础上，做源码级深入分析，补充日志和源码证据。", ""])
        lines.extend(
            [
                "硬性要求：",
                "1. 必须先读取上下文文件、SKILL.md、可用源码证据和必要输入材料；不能只根据标题/描述直接下根因结论。",
                "2. 只读分析，不修改文件，不生成无证据结论。",
                "3. 输出中文 Markdown，不要输出代码块围栏或额外解释。",
                "4. Markdown 必须包含这些二级标题：`## 结论摘要`、`## 关键证据`、`## 最可能原因`、`## 待确认项`、`## 建议动作`。",
                "5. `## 关键证据` 必须非空，每条证据要能回指到日志/源码/工具结果；证据不足时明确写待确认，不要编造。",
                "6. 不要重复输出 bug 链接；该信息已经结构化。",
                "7. 不要无边界递归扫描整个日志树；先看上下文列出的聚焦日志目录和清单，再按需扩展。",
                "8. 分析完成后必须输出最终 Markdown 正文，不能只执行工具调用而不输出结论。",
                "",
                "检索预算：",
                *[f"- {rule}" for rule in self._file_agent_search_budget_rules()],
                "",
            ]
        )
        if writes_output_file:
            lines.extend(
                [
                    "输出路径：",
                    f"- {analysis_markdown_path.name}: `{analysis_markdown_path}`",
                    "",
                ]
            )
        else:
            lines.extend(
                [
                    "输出保存：",
                    f"- Bridge 会把你的标准输出保存到 `{analysis_markdown_path}`",
                    "- 请务必在所有工具调用完成后，输出完整的 Markdown 正文。",
                    "",
                ]
            )
        lines.extend(
            [
                "",
                f"请开始只读分析，并只输出可直接保存为 `{analysis_markdown_path.name}` 的正文内容。",
            ]
        )
        return "\n".join(lines).strip() + "\n"

    def _custom_skill_agent_add_dirs(
        self,
        *,
        skill_name: str,
        selected_input: Path | None,
        prepared_input: Path | None,
        source_evidence_path: Path | None,
        context_path: Path | None,
    ) -> list[Path]:
        dirs: list[Path] = []
        for path in [
            *self._custom_skill_agent_source_roots(),
            *self._skill_context_paths(skill_name),
            selected_input,
            prepared_input,
            source_evidence_path,
            context_path,
        ]:
            if path is None:
                continue
            candidate = path if path.is_dir() else path.parent
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            if resolved not in dirs and resolved.exists():
                dirs.append(resolved)
        return dirs

    def _custom_skill_agent_cwd(self) -> Path:
        source_roots = self._custom_skill_agent_source_roots()
        if source_roots:
            return source_roots[0]
        return self._working_dir()

    def _trim_reanalysis_reference_text(self, text: str, *, max_chars: int = 600) -> str:
        normalized = self._sanitize_file_agent_text(text)
        if not normalized:
            return ""
        if len(normalized) <= max_chars:
            return normalized
        return normalized[: max_chars - 1].rstrip() + "…"

    def _source_stage_file_agent_timeout(self, *, reference_seconds: float) -> float:
        if self._should_use_codex_app_server_for_file_agent("codex"):
            return max(
                180.0,
                min(
                    float(self.config.codex_app_server.turn_timeout_seconds),
                    float(self.config.bug_analysis.timeout_seconds),
                ),
            )
        return min(
            180.0,
            self._agent_summary_timeout(
                self.config.bug_analysis.timeout_seconds,
                reference_seconds=reference_seconds,
            ),
        )

    def _prefer_source_stage_file_agent(self) -> bool:
        return self._should_use_codex_app_server_for_file_agent("codex")

    def _source_stage_followup_prompt_text(self, *, request_text: str, followup_text: str) -> str:
        normalized_followup = followup_text.strip()
        if normalized_followup:
            return normalized_followup
        return request_text.strip()

    def _source_stage_followup_request_text(self, *, request_text: str, followup_text: str) -> str:
        if self._followup_explicitly_requests_source_analysis(request_text, followup_text):
            normalized_followup = followup_text.strip()
            if normalized_followup:
                return normalized_followup
        return request_text.strip()

    def build_codex_app_server_execution_policy(
        self, *, cwd: Path, timeout: int,
    ) -> CodexAppServerExecutionPolicy:
        options = self.config.codex_app_server
        env = build_internal_network_env(self.config.internal_network_env)
        if options.preserve_proxy_env:
            env = self._merge_codex_app_server_proxy_env(env)
        env.setdefault("RUST_LOG", "warn")
        codex_home: Path | None = None
        if options.use_minimal_home:
            codex_home = self._prepare_codex_app_server_minimal_home()
            if codex_home is not None:
                env["CODEX_HOME"] = str(codex_home)
        # Minimal home has no node_repl section, so the disable flag is only
        # meaningful (and only emitted) when running against the real home.
        emit_node_repl_flag = options.disable_node_repl and codex_home is None
        return CodexAppServerExecutionPolicy(
            env=env,
            codex_home=codex_home,
            cwd=Path(cwd),
            disable_node_repl=options.disable_node_repl,
            emit_node_repl_flag=emit_node_repl_flag,
        )

    def _prepare_codex_app_server_minimal_home(self, *, run_id: str = "") -> Path | None:
        source_home = Path.home() / ".codex"
        if not (source_home / "auth.json").exists():
            return None
        base = self.config.data_dir / "codex_app_server_home"
        template = base / "template"
        template.mkdir(parents=True, exist_ok=True)
        for name in ("auth.json", "installation_id", "models_cache.json"):
            source = source_home / name
            if not source.exists():
                continue
            try:
                shutil.copy2(source, template / name)
            except OSError:
                continue
        config_lines = ["[analytics]", "enabled = false", ""]
        model = self.config.codex_app_server.model.strip()
        if model:
            # Empty model => omit the line so Codex uses its own default.
            config_lines = [f'model = "{model}"', ""] + config_lines
        try:
            (template / "config.toml").write_text("\n".join(config_lines), encoding="utf-8")
        except OSError:
            return None
        runs_dir = base / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        self._sweep_stale_codex_app_server_runs(runs_dir)
        run_home = runs_dir / (run_id or uuid.uuid4().hex[:12])
        try:
            shutil.rmtree(run_home, ignore_errors=True)
            shutil.copytree(template, run_home)
        except OSError:
            return template
        return run_home

    def _sweep_stale_codex_app_server_runs(self, runs_dir: Path, max_age_seconds: float = 3600.0) -> None:
        now = time.time()
        try:
            children = list(runs_dir.iterdir())
        except OSError:
            return
        for child in children:
            try:
                if child.is_dir() and (now - child.stat().st_mtime) > max_age_seconds:
                    shutil.rmtree(child, ignore_errors=True)
            except OSError:
                continue

    def _merge_codex_app_server_proxy_env(self, env: dict[str, str]) -> dict[str, str]:
        merged = dict(env)
        for key in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "NO_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
            "no_proxy",
        ):
            value = os.environ.get(key)
            if value:
                merged[key] = value
        return merged

    def _domain_priority_files(self, context_profile: str) -> list[Path]:
        normalized = context_profile.strip()
        if normalized != "scene-signal-diagnosis":
            return []
        guideengine_root: Path | None = None
        napa5_root: Path | None = None
        for root in self._custom_skill_agent_source_roots():
            name = root.name.casefold()
            if name == "napa5":
                napa5_root = root
            elif "guideengine" in name or root == self.config.guideengine_repo:
                guideengine_root = root
        candidates: list[Path] = []
        if guideengine_root is not None:
            candidates.extend(
                [
                    guideengine_root / "module_display/launcher_subreality_service/src/main/java/com/xiaopeng/ainavi/scene/UnitySceneTypeService.kt",
                    guideengine_root / "module_display/launcher_subreality_service/src/main/java/com/xiaopeng/ainavi/scene/UnitySceneTypeRepository.kt",
                    guideengine_root / "module_display/launcher_subreality_service/src/main/java/com/xiaopeng/ainavi/unity/UnityAdapter.kt",
                    guideengine_root / "module_floorcenter/module_datacenter/src/main/java/com/xiaopeng/guideengine/helper/xuimanager/XuiConditionHelper.kt",
                ]
            )
        if napa5_root is not None:
            candidates.extend(
                [
                    napa5_root / "Assets/LocalModules/module-napa5-hmi/Runtime/Scripts/Common/Service/Proxy/SystemServiceProxy.cs",
                    napa5_root / "Assets/LocalModules/module-napa5-hmi/Runtime/Scripts/App/Displays/Nodes/HUSceneNode.cs",
                    napa5_root / "Assets/LocalModules/module-napa5-hmi/Runtime/Scripts/XSRSceneManager/Logic/XSRSceneStateMachine.cs",
                ]
            )
        return [path for path in candidates if path.exists()]

    def _should_use_codex_app_server_for_file_agent(self, provider: str) -> bool:
        options = self.config.codex_app_server
        return (
            options.enabled
            and options.use_for_file_agent
            and _normalize_provider_name(provider) == "codex"
        )

    def _write_app_server_event_audit(self, path: Path, events: list[dict[str, object]]) -> None:
        lines = [json.dumps(event, ensure_ascii=False) for event in events]
        path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    def _run_custom_skill_agent_via_codex_app_server(
        self,
        *,
        analysis_kind: str,
        skill_name: str,
        prompt_text: str,
        cwd: Path,
        command_path: Path,
        stdout_path: Path,
        stderr_path: Path,
        events_path: Path,
        progress_callback: Callable[[dict[str, object]], None] | None,
        timeout: int,
        bridge_session_id: str,
    ) -> dict[str, object]:
        options = self.config.codex_app_server
        ok, version_or_error = check_codex_app_server_available(options.command, options.min_version)
        if not ok:
            return {
                "ok": False,
                "error_code": "codex_app_server_unavailable",
                "message": f"Codex app-server 不可用：{version_or_error}",
                "executor": "codex_app_server",
                "provider": "codex",
                "command": [options.command, "app-server"],
                "stdout": "",
                "stderr": version_or_error,
                "usage": {},
                "thread_id": "",
                "turn_id": "",
                "events": [],
                "events_path": events_path,
                "duration_seconds": 0.0,
                "bridge_session_id": bridge_session_id,
            }

        policy = self.build_codex_app_server_execution_policy(cwd=cwd, timeout=timeout)
        subprocess_env = policy.env
        runtime = CodexAppServerRuntime(
            command=options.command,
            cwd=policy.cwd,
            startup_timeout_seconds=options.startup_timeout_seconds,
            turn_timeout_seconds=min(float(timeout), options.turn_timeout_seconds),
            post_tool_quiet_timeout_seconds=options.post_tool_quiet_timeout_seconds,
            no_event_timeout_seconds=options.no_event_timeout_seconds,
            notification_poll_seconds=options.notification_poll_seconds,
            max_event_audit=options.max_event_audit,
            sandbox_mode=options.sandbox_mode,
            disable_node_repl=policy.disable_node_repl,
            emit_node_repl_flag=policy.emit_node_repl_flag,
            disable_analytics=options.disable_analytics,
            disable_memories=options.disable_memories,
            disable_apps_feature=options.disable_apps_feature,
            disable_plugins_feature=options.disable_plugins_feature,
            disable_computer_use_feature=options.disable_computer_use_feature,
            reasoning_effort=options.reasoning_effort,
            env=subprocess_env,
        )

        def _stream_event(event: dict[str, object]) -> None:
            preview = app_server_event_preview(event)
            if not preview:
                return
            self._emit_progress(
                progress_callback,
                stage=f"{analysis_kind}_agent_analysis_stream",
                message=f"Codex app-server: {preview}",
                provider="codex",
                stream_preview=preview,
            )

        result = runtime.run_turn(prompt_text, on_event=_stream_event if progress_callback is not None else None)
        if policy.codex_home is not None and policy.codex_home.parent.name == "runs":
            shutil.rmtree(policy.codex_home, ignore_errors=True)
        stdout_path.write_text(result.stdout, encoding="utf-8")
        stderr_path.write_text(result.stderr, encoding="utf-8")
        self._write_app_server_event_audit(events_path, result.events)
        command_path.write_text(json.dumps(result.command, ensure_ascii=False, indent=2), encoding="utf-8")
        if progress_callback is not None:
            summary_preview = result.stdout.splitlines()[0] if result.stdout.strip() else ""
            self._emit_progress(
                progress_callback,
                stage=f"{analysis_kind}_agent_analysis_stream",
                message=f"Codex app-server result: {summary_preview or ('ok' if result.ok else result.error_code or 'failed')}",
                provider="codex",
                stream_preview=summary_preview,
            )
        return {
            "ok": result.ok,
            "error_code": result.error_code,
            "message": result.error,
            "final_text": result.final_text,
            "executor": "codex_app_server",
            "provider": "codex",
            "command": result.command,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "usage": normalize_token_usage(result.usage) or dict(result.usage),
            "thread_id": result.thread_id,
            "turn_id": result.turn_id,
            "events": result.events,
            "events_path": events_path,
            "duration_seconds": result.duration_seconds,
            "should_retire": result.should_retire,
            "completion_state": result.completion_state.value,
            "app_server_version": version_or_error,
        }

    def _run_custom_skill_agent_analysis(
        self,
        *,
        analysis_kind: str = "source_code_skill",
        analysis_label: str = "",
        skill_name: str,
        request_text: str,
        prompt_text: str,
        title: str,
        description: str,
        fault_time: str,
        selected_input: Path | None,
        prepared_input: Path | None,
        source_evidence_path: Path | None,
        html_path: Path,
        json_path: Path,
        analysis_dir: Path,
        progress_callback: Callable[[dict[str, object]], None] | None,
        timeout: int,
        bridge_session_id: str = "",
        prior_findings: list[tuple[str, str, str]] | None = None,
        context_profile: str = "",
        provider_override: str = "",
        command_override: str = "",
    ) -> dict[str, object]:
        started = time.monotonic()
        effective_label = analysis_label or self._analysis_label(analysis_kind)
        analysis_dir.mkdir(parents=True, exist_ok=True)
        html_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        analysis_markdown_path = analysis_dir / self._skill_agent_analysis_markdown_name(analysis_kind)
        stdout_path = analysis_dir / self._skill_agent_sidecar_name(analysis_kind, "stdout.txt")
        stderr_path = analysis_dir / self._skill_agent_sidecar_name(analysis_kind, "stderr.txt")
        command_path = analysis_dir / self._skill_agent_sidecar_name(analysis_kind, "command.txt")
        events_path = analysis_dir / self._skill_agent_sidecar_name(analysis_kind, "app_server_events.jsonl")
        context_path = analysis_dir / self._skill_agent_sidecar_name(analysis_kind, "context.md")
        log_focus_manifest_path = analysis_dir / "log_focus.md"
        debug_log_path = (
            analysis_dir / self._skill_agent_sidecar_name(analysis_kind, "debug.log")
            if self.config.bug_analysis.file_agent_debug_logs
            else None
        )

        def _fail(result: dict[str, object], *, provider_name: str = "") -> dict[str, object]:
            self._emit_progress(
                progress_callback,
                stage=f"{analysis_kind}_agent_failed",
                message=str(result.get("message") or f"{effective_label}文件 Agent 分析失败"),
                error_code=str(result.get("error_code") or ""),
                provider=provider_name,
                output_path=str(analysis_markdown_path),
                stdout_path=str(result.get("stdout_path") or stdout_path),
                stderr_path=str(result.get("stderr_path") or stderr_path),
                debug_log_path=str(result.get("debug_log_path") or debug_log_path or ""),
            )
            return result

        stale_paths = [
            analysis_markdown_path,
            html_path,
            json_path,
            stdout_path,
            stderr_path,
            command_path,
            events_path,
            context_path,
            log_focus_manifest_path,
        ]
        if debug_log_path is not None:
            stale_paths.append(debug_log_path)
        for stale_path in stale_paths:
            try:
                if stale_path.exists():
                    stale_path.unlink()
            except OSError:
                continue
        focused_log_input, focus_manifest, focused_files = self._build_file_agent_focus_dir(
            input_path=prepared_input,
            fault_time=fault_time,
            analysis_kind=analysis_kind,
            analysis_dir=analysis_dir,
        )
        context_path = self._write_file_agent_context(
            analysis_kind=analysis_kind,
            skill_name=context_profile or skill_name,
            request_text=request_text,
            prompt_text=prompt_text,
            title=title,
            description=description,
            fault_time=fault_time,
            original_selected_input=selected_input,
            focused_log_input=focused_log_input,
            log_focus_manifest=focus_manifest,
            source_evidence_path=source_evidence_path,
            analysis_dir=analysis_dir,
            analysis_markdown_path=analysis_markdown_path,
            prior_findings=prior_findings,
        )
        invocation = self._build_custom_skill_agent_command(
            analysis_kind=analysis_kind,
            skill_name=skill_name,
            request_text=request_text,
            prompt_text=prompt_text,
            title=title,
            description=description,
            fault_time=fault_time,
            selected_input=focused_log_input,
            prepared_input=focused_log_input,
            source_evidence_path=source_evidence_path,
            analysis_markdown_path=analysis_markdown_path,
            context_path=context_path,
            debug_log_path=debug_log_path,
            provider_override=provider_override,
            command_override=command_override,
            prior_findings=prior_findings,
        )
        command = list(invocation.get("command") or [])
        provider = str(invocation.get("provider") or "")
        output_mode = str(invocation.get("output_mode") or "")
        process_cwd = Path(invocation.get("cwd") or self._working_dir())
        input_mode = str(invocation.get("input_mode") or "")
        input_text = str(invocation.get("prompt") or "") if input_mode == "stdin" else None
        if command:
            command_path.write_text(json.dumps(command, ensure_ascii=False, indent=2), encoding="utf-8")
        if not command:
            return _fail({
                "ok": False,
                "error_code": "custom_skill_agent_not_configured",
                "message": f"专用 Skill `{skill_name}` 已配置 file_agent，但未配置可用 Agent 命令。",
                "command": command,
                "provider": provider,
                "analysis_markdown_path": analysis_markdown_path,
                "stdout_path": stdout_path,
                "stderr_path": stderr_path,
                "command_path": command_path,
                "events_path": events_path,
                "context_path": context_path,
                "log_focus_manifest_path": focus_manifest,
                "focused_log_input": focused_log_input,
                "focused_log_files": [str(path) for path in focused_files],
                "debug_log_path": debug_log_path,
                "stdout": "",
                "stderr": "",
                "duration_seconds": time.monotonic() - started,
            }, provider_name=provider)
        self._emit_progress(
            progress_callback,
            stage=f"{analysis_kind}_agent_analysis",
            message=f"执行{effective_label}文件 Agent 分析",
            skill=skill_name,
            provider=provider,
            output_path=str(analysis_markdown_path),
            timeout_seconds=timeout,
        )
        stdout = ""
        stderr = ""
        usage: dict[str, object] = {}
        thread_id = ""
        turn_id = ""
        executor = "file_agent"
        app_server_error_code = ""
        app_server_error_message = ""
        app_server_version = ""
        subprocess_env = build_internal_network_env(self.config.internal_network_env)
        process_debug_log_path = None if provider in {"claude", "claude-code", "claude_code"} else debug_log_path
        try:
            if self._should_use_codex_app_server_for_file_agent(provider):
                app_server_result = self._run_custom_skill_agent_via_codex_app_server(
                    analysis_kind=analysis_kind,
                    skill_name=skill_name,
                    prompt_text=str(invocation.get("prompt") or ""),
                    cwd=process_cwd,
                    command_path=command_path,
                    stdout_path=stdout_path,
                    stderr_path=stderr_path,
                    events_path=events_path,
                    progress_callback=progress_callback,
                    timeout=timeout,
                    bridge_session_id=bridge_session_id,
                )
                app_server_error_code = str(app_server_result.get("error_code") or "")
                app_server_error_message = str(app_server_result.get("message") or "")
                app_server_version = str(app_server_result.get("app_server_version") or "")
                app_server_command = list(app_server_result.get("command") or [])
                if app_server_result.get("ok"):
                    if app_server_command:
                        command = app_server_command
                    executor = "codex_app_server"
                    stdout = str(app_server_result.get("stdout") or "")
                    stderr = str(app_server_result.get("stderr") or "")
                    usage_value = app_server_result.get("usage") or {}
                    if isinstance(usage_value, dict):
                        usage = dict(usage_value)
                    thread_id = str(app_server_result.get("thread_id") or "")
                    turn_id = str(app_server_result.get("turn_id") or "")
                    final_text = str(app_server_result.get("final_text") or "").strip()
                    if final_text:
                        analysis_markdown_path.write_text(final_text + "\n", encoding="utf-8")
                    completed = subprocess.CompletedProcess(command, 0, stdout, stderr)
                elif self.config.codex_app_server.fallback_to_exec:
                    self._emit_progress(
                        progress_callback,
                        stage=f"{analysis_kind}_agent_analysis_stream",
                        message="Codex app-server 失败，回退到现有 codex exec 路径",
                        provider=provider,
                        app_server_error_code=app_server_error_code,
                    )
                    stdout = ""
                    stderr = ""
                else:
                    return {
                        "ok": False,
                        "error_code": app_server_error_code or "codex_app_server_failed",
                        "message": app_server_error_message or f"专用 Skill `{skill_name}` Codex app-server 执行失败。",
                        "command": list(app_server_result.get("command") or command),
                        "provider": provider,
                        "executor": "codex_app_server",
                        "analysis_markdown_path": analysis_markdown_path,
                        "stdout_path": stdout_path,
                        "stderr_path": stderr_path,
                        "command_path": command_path,
                        "events_path": events_path,
                        "context_path": context_path,
                        "log_focus_manifest_path": focus_manifest,
                        "focused_log_input": focused_log_input,
                        "focused_log_files": [str(path) for path in focused_files],
                        "debug_log_path": debug_log_path,
                        "stdout": str(app_server_result.get("stdout") or ""),
                        "stderr": str(app_server_result.get("stderr") or ""),
                        "duration_seconds": time.monotonic() - started,
                        "usage": usage,
                        "thread_id": thread_id,
                        "turn_id": turn_id,
                        "app_server_version": app_server_version,
                        "app_server_error_code": app_server_error_code,
                    }
            if executor == "file_agent" and output_mode == "stdout_json":
                completed = _run_tracked_process(
                    command,
                    watchdog=self.process_watchdog,
                    name=f"custom-skill-agent-analysis-{provider or 'agent'}",
                    cwd=process_cwd,
                    input=input_text,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                    session_id=bridge_session_id,
                    env=subprocess_env,
                    debug_log_path=process_debug_log_path,
                )
                raw_stdout = _coerce_process_text(completed.stdout)
                stderr = _coerce_process_text(completed.stderr)
                stdout = self._extract_text_from_agent_json(raw_stdout, analysis_markdown_path)
            elif executor == "file_agent" and output_mode == "stdout_redirect":
                with analysis_markdown_path.open("w", encoding="utf-8") as stdout_handle:
                    completed = _run_tracked_process(
                        command,
                        watchdog=self.process_watchdog,
                        name=f"custom-skill-agent-analysis-{provider or 'agent'}",
                        cwd=process_cwd,
                        input=input_text,
                        stdout=stdout_handle,
                        stderr=subprocess.PIPE,
                        text=True,
                        timeout=timeout,
                        check=False,
                        session_id=bridge_session_id,
                        env=subprocess_env,
                        debug_log_path=process_debug_log_path,
                    )
                stdout = analysis_markdown_path.read_text(encoding="utf-8", errors="replace") if analysis_markdown_path.exists() else ""
                stderr = _coerce_process_text(completed.stderr)
            elif executor == "file_agent":
                completed = _run_tracked_process(
                    command,
                    watchdog=self.process_watchdog,
                    name=f"custom-skill-agent-analysis-{provider or 'agent'}",
                    cwd=process_cwd,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                    session_id=bridge_session_id,
                    env=subprocess_env,
                    debug_log_path=process_debug_log_path,
                )
                stdout = _coerce_process_text(completed.stdout)
                stderr = _coerce_process_text(completed.stderr)
        except subprocess.TimeoutExpired as exc:
            if output_mode == "stdout_redirect" and analysis_markdown_path.exists():
                stdout = analysis_markdown_path.read_text(encoding="utf-8", errors="replace")
            else:
                stdout = _coerce_process_text(exc.stdout)
            stderr = _coerce_process_text(exc.stderr)
            stdout_path.write_text(stdout, encoding="utf-8")
            stderr_path.write_text(stderr, encoding="utf-8")
            partial_markdown = self._extract_partial_markdown_from_agent_stream(stdout, skill_name=skill_name)
            return _fail({
                "ok": False,
                "error_code": "custom_skill_agent_timeout",
                "message": f"专用 Skill `{skill_name}` 文件 Agent 执行超时，未生成可验证证据。",
                "command": command,
                "provider": provider,
                "analysis_markdown_path": analysis_markdown_path,
                "stdout_path": stdout_path,
                "stderr_path": stderr_path,
                "command_path": command_path,
                "events_path": events_path,
                "context_path": context_path,
                "log_focus_manifest_path": focus_manifest,
                "focused_log_input": focused_log_input,
                "focused_log_files": [str(path) for path in focused_files],
                "debug_log_path": debug_log_path,
                "stdout": stdout,
                "stderr": stderr,
                "duration_seconds": time.monotonic() - started,
                "timeout_seconds": timeout,
                "partial_markdown": partial_markdown,
                "partial_analysis_available": bool(partial_markdown),
            }, provider_name=provider)
        except OSError as exc:
            stdout_path.write_text("", encoding="utf-8")
            stderr_path.write_text(str(exc), encoding="utf-8")
            return _fail({
                "ok": False,
                "error_code": "custom_skill_agent_failed_to_start",
                "message": f"专用 Skill `{skill_name}` 文件 Agent 启动失败：{exc}",
                "command": command,
                "provider": provider,
                "analysis_markdown_path": analysis_markdown_path,
                "stdout_path": stdout_path,
                "stderr_path": stderr_path,
                "command_path": command_path,
                "events_path": events_path,
                "context_path": context_path,
                "log_focus_manifest_path": focus_manifest,
                "focused_log_input": focused_log_input,
                "focused_log_files": [str(path) for path in focused_files],
                "debug_log_path": debug_log_path,
                "stdout": "",
                "stderr": str(exc),
                "duration_seconds": time.monotonic() - started,
            }, provider_name=provider)
        stdout_path.write_text(stdout, encoding="utf-8")
        stderr_path.write_text(stderr, encoding="utf-8")
        if output_mode != "stdout_redirect" and not analysis_markdown_path.exists() and stdout.strip():
            analysis_markdown_path.write_text(stdout.strip() + "\n", encoding="utf-8")
        if completed.returncode != 0:
            return _fail({
                "ok": False,
                "error_code": "custom_skill_agent_failed",
                "message": f"专用 Skill `{skill_name}` 文件 Agent 执行失败，未允许进入最终总结。",
                "command": command,
                "provider": provider,
                "executor": executor,
                "analysis_markdown_path": analysis_markdown_path,
                "stdout_path": stdout_path,
                "stderr_path": stderr_path,
                "command_path": command_path,
                "events_path": events_path,
                "context_path": context_path,
                "log_focus_manifest_path": focus_manifest,
                "focused_log_input": focused_log_input,
                "focused_log_files": [str(path) for path in focused_files],
                "debug_log_path": debug_log_path,
                "stdout": stdout,
                "stderr": stderr,
                "duration_seconds": time.monotonic() - started,
            }, provider_name=provider)
        if not analysis_markdown_path.exists():
            return _fail({
                "ok": False,
                "error_code": "custom_skill_agent_missing_output",
                "message": (
                    f"专用 Skill `{skill_name}` 文件 Agent 未生成 `{analysis_markdown_path.name}`，未允许进入最终总结。"
                    f" 请检查 `{stdout_path.name}` 和 `{stderr_path.name}`。"
                ),
                "command": command,
                "provider": provider,
                "executor": executor,
                "analysis_markdown_path": analysis_markdown_path,
                "stdout_path": stdout_path,
                "stderr_path": stderr_path,
                "command_path": command_path,
                "events_path": events_path,
                "context_path": context_path,
                "log_focus_manifest_path": focus_manifest,
                "focused_log_input": focused_log_input,
                "focused_log_files": [str(path) for path in focused_files],
                "debug_log_path": debug_log_path,
                "stdout": stdout,
                "stderr": stderr,
                "duration_seconds": time.monotonic() - started,
            }, provider_name=provider)
        analysis_text = analysis_markdown_path.read_text(encoding="utf-8", errors="replace")
        if not analysis_text.strip():
            debug_hint = (
                f" 调试日志: `{debug_log_path.name}`。" if debug_log_path is not None else ""
            )
            return _fail({
                "ok": False,
                "error_code": "custom_skill_agent_empty_output",
                "message": (
                    f"专用 Skill `{skill_name}` 文件 Agent 执行完成但未输出任何正文（returncode={completed.returncode}）。"
                    f" 可能原因：模型在工具调用结束后未生成最终文本回复。"
                    f" 请检查 `{stdout_path.name}`、`{stderr_path.name}` 和{debug_hint}"
                    f" 可尝试重新触发分析。"
                ),
                "command": command,
                "provider": provider,
                "executor": executor,
                "analysis_markdown_path": analysis_markdown_path,
                "stdout_path": stdout_path,
                "stderr_path": stderr_path,
                "command_path": command_path,
                "events_path": events_path,
                "context_path": context_path,
                "debug_log_path": debug_log_path,
                "stdout": stdout,
                "stderr": stderr,
                "duration_seconds": time.monotonic() - started,
            }, provider_name=provider)
        valid, reason, evidence_count = self._validate_custom_skill_analysis(analysis_markdown_path)
        if not valid:
            return _fail({
                "ok": False,
                "error_code": "custom_skill_agent_invalid_evidence",
                "message": f"专用 Skill `{skill_name}` 文件 Agent 输出缺少有效 `## 关键证据`：{reason}。不会进入最终总结。",
                "command": command,
                "provider": provider,
                "executor": executor,
                "analysis_markdown_path": analysis_markdown_path,
                "stdout_path": stdout_path,
                "stderr_path": stderr_path,
                "command_path": command_path,
                "events_path": events_path,
                "context_path": context_path,
                "log_focus_manifest_path": focus_manifest,
                "focused_log_input": focused_log_input,
                "focused_log_files": [str(path) for path in focused_files],
                "debug_log_path": debug_log_path,
                "stdout": stdout,
                "stderr": stderr,
                "duration_seconds": time.monotonic() - started,
                "evidence_count": evidence_count,
                "validation_error": reason,
            }, provider_name=provider)
        self._write_custom_skill_agent_report(
            analysis_kind=analysis_kind,
            analysis_label=effective_label,
            html_path=html_path,
            json_path=json_path,
            analysis_markdown_path=analysis_markdown_path,
            skill_name=skill_name,
            provider=provider,
            request_text=request_text,
            prompt_text=prompt_text,
            title=title,
            description=description,
            fault_time=fault_time,
            selected_input=selected_input,
            prepared_input=prepared_input,
            source_evidence_path=source_evidence_path,
            evidence_count=evidence_count,
            duration_seconds=time.monotonic() - started,
            executor=executor,
            extra_payload={
                "usage": usage,
                "thread_id": thread_id,
                "turn_id": turn_id,
                "app_server_version": app_server_version,
            },
        )
        return {
            "ok": True,
            "error_code": "",
            "message": "",
            "command": command,
            "analysis_kind": analysis_kind,
            "provider": provider,
            "executor": executor,
            "analysis_markdown_path": analysis_markdown_path,
            "html_path": html_path,
            "json_path": json_path,
            "stdout_path": stdout_path,
            "stderr_path": stderr_path,
            "command_path": command_path,
            "events_path": events_path,
            "context_path": context_path,
            "log_focus_manifest_path": focus_manifest,
            "focused_log_input": focused_log_input,
            "focused_log_files": [str(path) for path in focused_files],
            "debug_log_path": debug_log_path,
            "stdout": stdout,
            "stderr": stderr,
            "duration_seconds": time.monotonic() - started,
            "evidence_count": evidence_count,
            "usage": usage,
            "thread_id": thread_id,
            "turn_id": turn_id,
            "app_server_version": app_server_version,
            "app_server_error_code": app_server_error_code,
            "app_server_error_message": app_server_error_message,
            "custom_skill_analysis_status": "completed",
        }

    # ------------------------------------------------------------------
    # LD lane-level: executor + direct_api path
    # ------------------------------------------------------------------

    _LD_LOG_PACKAGES = [
        "com.xiaopeng.montecarlo",
    ]
    _LD_GREP_PATTERNS = [
        r"LD:|LDConf:|CheckTileRender|CheckLDState",
        r"SetLDCenterAndRange|updateLoadTileCenter|LDEgoPosModel",
        r"MapDataHandler|dftileinfo|handle_map_data",
        r"bizCode:132002|ReceiveMsg.*132002",
        r"normal pos too large|XldNormalIfb",
        r"tileFull|XPD_LDTileCacheManager|processTileAdd",
        r"AnpXpSroverallProc|AnpBdPosProc|HOST_ICM_SD_PERIOD_DATA",
        r"NaviServiceManager|SIGNAL_X3D_SD_OVER_ALL_DATA|SIGNAL_LD_DFUNITY_DATA",
        r"OnSuperParkActive|OnEnvModeStatusChange|eParking|UpdateModelMode",
        r"scene_conf[0-9]|nearNew|LD_STATE_CHANGED",
        r"License:|NEDC:|locationState|superPark|gear:",
        r"RenderExtend|OnParse|processLoadMesh|visible ->:",
    ]

    def _ld_executor_grep_pattern(self) -> str:
        return "|".join(self._LD_GREP_PATTERNS)

    def _normalize_log_locator(self, value: str | Path) -> str:
        return str(value).replace("\\", "/")

    def _log_basename_from_locator(self, value: str | Path) -> str:
        normalized = self._normalize_log_locator(value)
        return normalized.rsplit("/", 1)[-1]

    def _parse_any_log_datetime(self, name: str) -> datetime | None:
        """Parse a datetime from a log filename.

        Supports ``main_YYYY-MM-DD_HH-MM`` and the montecarlo
        ``DIAG_D-NNN-YYYYMMDD-HHMMSS_...`` naming.
        """
        st = self._parse_log_file_datetime(name)
        if st is not None:
            try:
                return datetime.fromtimestamp(time.mktime(st))
            except (OverflowError, ValueError, OSError):
                return None
        m = re.search(r"(20\d{2})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})", name)
        if m:
            return self._safe_datetime(*(int(m.group(i)) for i in range(1, 7)))
        return None

    def _is_montecarlo_or_logd_path(self, path: Path) -> bool:
        parts = [part.casefold() for part in path.parts]
        return any(part == "logd" for part in parts) or any("montecarlo" in part for part in parts)

    def _decompress_zst(self, src: Path) -> Path | None:
        """zstd-decompress ``src`` (``*.zst``) to the path without the suffix."""
        src = src.resolve()
        dst = src.with_suffix("")
        if dst.exists():
            return dst
        zstd = shutil.which("zstd")
        if not zstd:
            logger.warning("zstd not found; cannot decompress %s", src)
            return None
        try:
            result = _run_tracked_process(
                [zstd, "-d", "-q", "-k", "-o", str(dst), str(src)],
                watchdog=self.process_watchdog,
                name="prepare-zst-decompress",
                cwd=self._working_dir(),
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except Exception as exc:
            logger.warning("zstd decompress failed for %s: %s", src, exc)
            return None
        if getattr(result, "returncode", 1) != 0 or not dst.exists():
            logger.warning("zstd decompress returned non-zero for %s", src)
            return None
        return dst

    def _ld_focus_log_candidates(self, root: Path, fault_dt: datetime | None, *, limit: int = 8) -> list[Path]:
        """Rank readable log candidates, defaulting to montecarlo/logd near the fault time.

        Montecarlo/logd logs rank first, then by time distance to the fault. Any
        selected montecarlo/logd ``.zst`` nav log is decompressed in place so the
        agent gets readable ``.log`` files covering the fault time by default.
        """
        scored: list[tuple[int, float, str, Path]] = []
        preferred: list[tuple[float, str, Path]] = []
        others: list[tuple[float, str, Path]] = []
        window_seconds = float(6 * 3600)
        try:
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                is_zst = path.name.lower().endswith(".zst")
                montecarlo_or_logd = self._is_montecarlo_or_logd_path(path)
                if not (self._is_log_coverage_file(path) or (is_zst and montecarlo_or_logd)):
                    continue
                file_dt = self._parse_any_log_datetime(path.name)
                if fault_dt is not None and file_dt is not None:
                    distance = abs((file_dt - fault_dt).total_seconds())
                elif file_dt is None:
                    distance = float(10 ** 9)
                else:
                    distance = 0.0
                if montecarlo_or_logd:
                    # Bound montecarlo/logd to the fault window so the candidate
                    # list defaults to logs covering the problem time.
                    if fault_dt is None or file_dt is None or distance <= window_seconds:
                        preferred.append((distance, str(path), path))
                elif len(others) < _BUG_LOG_COVERAGE_MAX_FILES:
                    others.append((distance, str(path), path))
        except OSError:
            return []
        preferred.sort(key=lambda item: (item[0], item[1]))
        others.sort(key=lambda item: (item[0], item[1]))
        ordered = [path for _, _, path in preferred] + [path for _, _, path in others]
        result: list[Path] = []
        for path in ordered:
            if len(result) >= limit:
                break
            if path.name.lower().endswith(".zst"):
                decoded = self._decompress_zst(path)
                if decoded is None:
                    continue
                path = decoded
            result.append(path)
        return result

    def _ld_prepared_log_metadata_path(
        self,
        *,
        prepared_input: Path | None,
        analysis_dir: Path,
        fault_time: str,
    ) -> Path | None:
        if prepared_input is None:
            return None
        analysis_dir.mkdir(parents=True, exist_ok=True)
        metadata_path = analysis_dir / "prepared_log_metadata.md"
        candidates: list[Path] = []
        seen: set[Path] = set()

        def _append(path: Path) -> None:
            if path in seen or not path.exists():
                return
            seen.add(path)
            candidates.append(path)

        _append(prepared_input)
        if prepared_input.is_file():
            lower_name = prepared_input.name.lower()
            if lower_name.endswith((".alog", ".xlog")):
                _append(prepared_input.with_name(prepared_input.name + ".log"))
            elif lower_name.endswith((".alog.log", ".xlog.log")):
                _append(prepared_input.with_suffix(""))
            fault_dt = self._parse_bug_datetime(fault_time)
            try:
                siblings = sorted(prepared_input.parent.iterdir())
            except OSError:
                siblings = []
            for sibling in siblings:
                if len(candidates) >= 8:
                    break
                if not sibling.is_file():
                    continue
                normalized = self._normalize_log_locator(sibling.name).lower()
                if not normalized.endswith((".alog", ".alog.log", ".xlog", ".xlog.log", ".log")):
                    continue
                if fault_dt is None:
                    _append(sibling)
                    continue
                basename = self._log_basename_from_locator(sibling.name)
                file_dt = self._parse_log_file_datetime(basename)
                if file_dt is None:
                    continue
                candidate_dt = datetime.fromtimestamp(time.mktime(file_dt))
                if abs((candidate_dt - fault_dt).total_seconds()) <= 7200:
                    _append(sibling)
        else:
            fault_dt = self._parse_bug_datetime(fault_time)
            for path in self._ld_focus_log_candidates(prepared_input, fault_dt, limit=8):
                _append(path)

        lines = [
            "# Prepared Log Metadata",
            "",
            f"- 故障时间: `{fault_time or '未识别'}`",
            f"- 主输入: `{prepared_input}`",
            f"- 主工作目录: `{prepared_input.parent if prepared_input.is_file() else prepared_input}`",
            "- 使用规则: 先调用 `read_prepared_log_metadata()`，首轮只允许围绕下面列出的路径检索。",
            "- 默认聚焦: 除非 Skill 另有建议，车道级/导航日志默认优先看 `com.xiaopeng.montecarlo` 与 `logd`，并以覆盖故障时间的文件为先（下方候选已按此排序，且 `.zst` 已解压为 `.log`）。",
            "- 限制: 不要扫描 `tools/lark-agent-bridge/data/bug_cache`、历史 job 输出或整个工作区；只有当这些候选文件明确不覆盖问题时间时，才允许扩到同目录相邻小时日志。",
            "",
            "## 候选日志",
        ]
        if candidates:
            lines.extend(f"- `{path}`" for path in candidates)
        else:
            lines.append("- 无可用候选文件")
        metadata_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        return metadata_path

    def _ld_executor_should_include_log_file(
        self,
        path: Path,
        *,
        locator: str | Path,
        fault_dt: "time.struct_time | None",
    ) -> bool:
        normalized = self._normalize_log_locator(locator).lower()
        if not normalized.endswith((".alog", ".alog.log", ".xlog", ".xlog.log", ".log")):
            return False
        if path.suffix == ".alog" and path.with_suffix(".alog.log").exists():
            return False
        basename = self._log_basename_from_locator(locator)
        file_dt = self._parse_log_file_datetime(basename)
        if fault_dt is None or file_dt is None:
            return True
        candidate_dt = datetime.fromtimestamp(time.mktime(file_dt))
        return abs((candidate_dt - fault_dt).total_seconds()) <= 7200

    def _ld_executor_find_log_files(
        self,
        *,
        cache_dir: Path,
        fault_time: str,
    ) -> list[Path]:
        """Find montecarlo/LD-relevant log files near fault time."""
        fault_dt = self._parse_bug_datetime(fault_time)
        results: list[Path] = []
        seen: set[Path] = set()
        logs_dir = cache_dir / "logs"

        def _append_candidate(path: Path, *, locator: str | Path) -> None:
            if path in seen or not path.is_file():
                return
            if not self._ld_executor_should_include_log_file(path, locator=locator, fault_dt=fault_dt):
                return
            seen.add(path)
            results.append(path)

        if logs_dir.exists():
            for pkg in self._LD_LOG_PACKAGES:
                for log_root in sorted(logs_dir.glob(f"data/Log/log*/app/{pkg}")):
                    for alog in sorted(log_root.glob("*.alog*")):
                        _append_candidate(alog, locator=alog.name)
            for path in sorted(logs_dir.rglob("*")):
                if not path.is_file():
                    continue
                normalized = self._normalize_log_locator(path.relative_to(logs_dir)).lower()
                if not any(pkg in normalized for pkg in self._LD_LOG_PACKAGES):
                    continue
                _append_candidate(path, locator=normalized)
        # Also check ZIP for montecarlo logs near fault time
        for zip_path in sorted((cache_dir / "attachments").glob("*.zip")) if (cache_dir / "attachments").exists() else []:
            try:
                with zipfile.ZipFile(zip_path, "r") as zf:
                    for info in zf.infolist():
                        if info.is_dir():
                            continue
                        name_lower = info.filename.lower()
                        if not any(pkg in name_lower for pkg in self._LD_LOG_PACKAGES):
                            continue
                        if not name_lower.endswith((".alog", ".alog.log", ".xlog", ".xlog.log", ".log")):
                            continue
                        basename = self._log_basename_from_locator(info.filename)
                        file_dt = self._parse_log_file_datetime(basename)
                        if fault_dt is not None and file_dt is not None:
                            candidate_dt = datetime.fromtimestamp(time.mktime(file_dt))
                            if abs((candidate_dt - fault_dt).total_seconds()) > 7200:
                                continue
                        # Extract to temp location for reading
                        extracted = cache_dir / "logs" / info.filename
                        if not extracted.exists():
                            extracted.parent.mkdir(parents=True, exist_ok=True)
                            try:
                                with zf.open(info) as src, open(extracted, "wb") as dst:
                                    shutil.copyfileobj(src, dst)
                            except OSError:
                                continue
                        results.append(extracted)
            except (zipfile.BadZipFile, OSError):
                continue
        return sorted(set(results))

    def _ld_executor_extract_evidence(
        self,
        *,
        log_files: list[Path],
        fault_time: str,
        max_lines: int = 300,
    ) -> str:
        """Extract LD-relevant lines from log files using subprocess grep for speed."""
        pattern = self._ld_executor_grep_pattern()
        fault_dt = self._parse_bug_datetime(fault_time)
        evidence_parts: list[str] = []
        total_lines = 0
        # Compute time window for grep pre-filter (fault ±3min)
        time_grep_pattern = ""
        if fault_dt is not None:
            hour = fault_dt.hour
            minute = fault_dt.minute
            time_parts: list[str] = []
            for offset in range(-3, 4):
                m = minute + offset
                h = hour + (m // 60)
                m = m % 60
                if h < 0 or h > 23:
                    continue
                time_parts.append(f" {h:02d}:{m:02d}:")
            if time_parts:
                time_grep_pattern = "|".join(time_parts)
        for log_file in log_files:
            if total_lines >= max_lines:
                break
            if not log_file.exists():
                continue
            # Skip binary files
            try:
                raw = log_file.read_bytes()[:512]
                if b"\x00" in raw:
                    continue
            except OSError:
                continue
            # Use subprocess grep for speed on large files
            # Pipeline: grep time window first (fast), then grep LD patterns
            try:
                if time_grep_pattern:
                    grep_cmd = f"grep -E {shlex.quote(time_grep_pattern)} {shlex.quote(str(log_file))} | grep -iE {shlex.quote(pattern)}"
                else:
                    grep_cmd = f"grep -iE {shlex.quote(pattern)} {shlex.quote(str(log_file))}"
                result = subprocess.run(
                    grep_cmd, shell=True, capture_output=True, text=True,
                    timeout=30, check=False,
                )
                lines = result.stdout.splitlines()
            except (subprocess.TimeoutExpired, OSError):
                continue
            remaining = max_lines - total_lines
            lines = lines[:remaining]
            if lines:
                relative_name = log_file.name
                try:
                    parts = log_file.parts
                    app_idx = next((i for i, p in enumerate(parts) if p == "app"), None)
                    if app_idx is not None and app_idx + 1 < len(parts):
                        relative_name = "/".join(parts[app_idx:])
                except (StopIteration, IndexError):
                    pass
                evidence_parts.append(
                    f"### {relative_name}\n"
                    f"匹配行数: {len(lines)}\n"
                    f"```\n" + "\n".join(lines) + "\n```\n"
                )
                total_lines += len(lines)
        if not evidence_parts:
            return (
                "## LD 日志证据提取结果\n\n"
                f"**未在 montecarlo 日志中找到 LD 相关证据行。**\n"
                f"已搜索 {len(log_files)} 个日志文件，grep 模式: `{pattern[:80]}...`\n"
                f"故障时间: {fault_time}\n\n"
                "可能原因：故障时间对应的 montecarlo 日志不在当前日志包中。\n"
            )
        return (
            f"## LD 日志证据提取结果\n\n"
            f"故障时间: {fault_time}\n"
            f"搜索文件数: {len(log_files)}\n"
            f"匹配行数: {total_lines}\n\n"
            + "\n".join(evidence_parts)
        )

    def _run_source_stage_pydantic_ai(
        self,
        *,
        analysis_kind: str,
        skill_name: str,
        request_text: str,
        prompt_text: str,
        title: str,
        description: str,
        fault_time: str,
        selected_input: Path | None,
        prepared_input: Path | None,
        source_evidence_path: Path | None,
        html_path: Path,
        json_path: Path,
        analysis_dir: Path,
        progress_callback: Callable[[dict[str, object]], None] | None,
        context_profile: str = "",
        prior_findings: list[tuple[str, str, str]] | None = None,
    ) -> dict[str, object]:
        """Run source analysis via pydantic-ai agent runtime (fast path).

        Returns custom_result-compatible dict. On failure, returns ok=False
        so the caller can fall through to subprocess.
        """
        from .agent_runtime import AgentRuntime
        from .agent_output_models import SourceAnalysisOutput

        started = time.monotonic()
        provider_tag = "pydantic_ai"
        analysis_dir.mkdir(parents=True, exist_ok=True)
        analysis_markdown_path = analysis_dir / self._skill_agent_analysis_markdown_name(analysis_kind)

        ai_opts = self.config.ai_provider
        si_opts = self.config.source_investigation
        # Use source repo roots as primary workspace (not bridge dir)
        source_roots = list(si_opts.repo_roots or [])
        if not source_roots:
            source_roots = [Path(self._working_dir())]
        primary_workspace = source_roots[0]
        extra_roots = source_roots[1:] + list(si_opts.add_dirs or [])
        # Add analysis dir so agent can read prior evidence
        if analysis_dir.exists():
            extra_roots.append(analysis_dir)
        # Report dir for reading prior analysis artifacts
        report_dir = analysis_dir.parent if analysis_dir.exists() else None
        # Log metadata
        log_metadata_path = None
        if prepared_input and (prepared_input / "log_focus.md").exists():
            log_metadata_path = prepared_input / "log_focus.md"
        elif analysis_dir and (analysis_dir / "log_focus.md").exists():
            log_metadata_path = analysis_dir / "log_focus.md"

        # Codegraph client for semantic code intelligence
        codegraph_client = None
        codegraph_roots = None
        if si_opts.codegraph_enabled:
            try:
                from ..knowledge.codegraph_client import CodeGraphClient
                cg = CodeGraphClient(
                    command=si_opts.codegraph_command,
                    timeout=si_opts.codegraph_timeout_seconds,
                )
                if cg.is_available():
                    codegraph_client = cg
                    codegraph_roots = source_roots
                    logger.info("Codegraph client available for pydantic-ai tools")
            except Exception as exc:
                logger.debug("Codegraph client init failed: %s", exc)

        runtime = AgentRuntime(ai_opts, workspace=primary_workspace)

        if not runtime.is_available():
            return {
                "ok": False,
                "error_code": "pydantic_ai_not_available",
                "message": "pydantic-ai runtime 不可用，将 fallback 到 file_agent。",
                "command": [],
                "provider": provider_tag,
                "analysis_markdown_path": analysis_markdown_path,
                "duration_seconds": time.monotonic() - started,
            }

        self._emit_progress(
            progress_callback,
            stage=f"{analysis_kind}_pydantic_ai",
            message=f"源码分析：pydantic-ai runtime（{ai_opts.primary_model}）",
            provider=provider_tag,
            model=ai_opts.primary_model,
        )

        # Build skill context
        skill_record = self.skill_manager.get_skill(context_profile or skill_name, include_content=True)
        skill_content = skill_record.content if skill_record and skill_record.content else ""

        # Build source evidence context
        source_evidence_text = ""
        if source_evidence_path and source_evidence_path.exists():
            try:
                source_evidence_text = source_evidence_path.read_text(encoding="utf-8", errors="replace")[:30000]
            except OSError:
                pass

        # Build prior findings context
        prior_context = ""
        if prior_findings:
            parts = []
            for kind, label, content in prior_findings:
                parts.append(f"### {label} ({kind})\n{content[:5000]}")
            prior_context = "\n\n".join(parts)

        system_prompt = (
            "你是一个专业的源码分析 Agent，通过飞书触发。"
            "你的任务是根据 Bug 信息、日志证据和源代码，分析问题的根因。\n\n"
            "## 可用工具\n"
            "### Shell（最高效，支持管道）\n"
            "- bash(command, workdir?): 执行只读 shell 命令，支持管道。例如：\n"
            "    bash('rg \"OnSceneChanged\" --type cs -C 3 | head -50')\n"
            "    bash('git log --oneline --follow -- UnitySceneTypeService.kt | head -20')\n"
            "    bash('find . -name \"*.kt\" | xargs grep -l \"SIGNAL_SR_SCENE_TYPE\" | head -10')\n"
            "    bash('cat SRDataManagerService.cs | sed -n \"80,140p\"')\n"
            "  允许：rg grep find ls cat head tail awk sed sort jq git-log/diff/show/blame xargs tree\n"
            "  禁止：rm mv cp curl wget sudo 及写文件\n"
            "### 文件读取\n"
            "- read_file(path, start_line?, end_line?): 读取文件内容，支持行范围分页\n"
            "- get_file_outline(path): 获取文件符号大纲（类/函数/方法列表+行号），无需读全文\n"
            "- list_dir(path): 列出目录内容\n"
            "### 搜索\n"
            "- grep(pattern, glob_filter?, context_lines?): 正则搜索，推荐用 bash('rg ...') 代替\n"
            "- glob(pattern): 按 glob 查找文件路径，如 '**/*.kt'\n"
            "### 语义代码搜索（有索引时可用）\n"
            "- search_codegraph(query, kind?): 语义符号搜索，按函数名/类名查找定义位置\n"
            "- get_callers(symbol): 查找指定函数/方法的所有调用者\n"
            "- get_code_context(task): 根据任务描述自动构建代码上下文和入口点\n"
            "### 分析辅助\n"
            "- think(thought): 记录推理思路（scratchpad），梳理复杂分析步骤\n"
            "- repo_overview(path?): 仓库概览：分支、最近提交、目录结构\n"
            "- read_report_artifact(name): 读取前序分析报告产物\n"
            "- read_prepared_log_metadata(): 读取日志元数据摘要\n\n"
            "## 工作要求\n"
            "1. 优先用 bash() 执行管道命令，效率最高（一次调用完成搜索+过滤+格式化）\n"
            "2. 复杂分析前先用 think() 写下分析计划和假设\n"
            "3. 优先用 search_codegraph/get_code_context 快速定位符号（有索引时）\n"
            "4. 对大文件先用 get_file_outline 了解结构，再用 read_file(start_line, end_line) 精读\n"
            "5. 用 bash('git log') 确认代码改动时间线\n"
            "6. 每条 evidence 必须包含具体的 file 路径和 line 号\n"
            "7. 证据不足时明确写待确认，不要编造\n"
            "8. 输出中文\n"
        )

        user_prompt = (
            f"# 源码分析请求\n\n"
            f"## Bug 信息\n"
            f"- 标题: {title}\n"
            f"- 故障时间: {fault_time}\n"
            f"- 用户请求: {request_text}\n"
            f"- 缺陷描述: {description}\n\n"
        )
        if skill_content:
            user_prompt += f"## 分析方法论\n{skill_content[:6000]}\n\n"
        if source_evidence_text:
            user_prompt += f"## 源码证据\n{source_evidence_text[:20000]}\n\n"
        if prior_context:
            user_prompt += f"## 前序分析结果\n{prior_context[:10000]}\n\n"
        user_prompt += (
            "请使用工具探索代码库，找到与此 Bug 相关的源码文件，分析根因。\n"
            "必须提供具体的代码证据（文件路径 + 行号 + 代码片段）。\n\n"
            "## 可搜索的代码仓库\n"
        )
        for root in source_roots:
            user_prompt += f"- {root.name}: {root}\n"

        try:
            # Build a progress wrapper for real-time tool call visibility
            def _tool_progress(*, stage: str, message: str, **kw: object) -> None:
                self._emit_progress(progress_callback, stage=stage, message=message, **kw)

            result = runtime.run(
                output_type=SourceAnalysisOutput,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                tools_enabled=True,
                strict_tools=True,
                extra_roots=extra_roots,
                report_dir=report_dir,
                log_metadata_path=log_metadata_path,
                progress_callback=_tool_progress,
                stream=True,
                codegraph_client=codegraph_client,
                codegraph_roots=codegraph_roots,
            )
        except Exception as exc:
            logger.warning("pydantic-ai source analysis failed: %s", exc)
            return {
                "ok": False,
                "error_code": "pydantic_ai_execution_error",
                "message": f"pydantic-ai 源码分析执行失败：{exc}",
                "command": [],
                "provider": provider_tag,
                "analysis_markdown_path": analysis_markdown_path,
                "duration_seconds": time.monotonic() - started,
            }

        if not result.ok:
            return {
                "ok": False,
                "error_code": result.error_code or "pydantic_ai_failed",
                "message": result.error or "pydantic-ai 分析失败",
                "command": [],
                "provider": provider_tag,
                "analysis_markdown_path": analysis_markdown_path,
                "duration_seconds": result.duration_seconds,
            }

        # Source analysis requires actual tool exploration
        if result.tool_calls == 0:
            logger.warning(
                "pydantic-ai source analysis completed with 0 tool calls (runtime_path=%s) — "
                "agent did not explore code, treating as failure",
                result.runtime_path,
            )
            return {
                "ok": False,
                "error_code": "pydantic_ai_no_tool_calls",
                "message": "pydantic-ai 源码分析未调用任何工具（未探索代码库），视为失败",
                "command": [],
                "provider": provider_tag,
                "analysis_markdown_path": analysis_markdown_path,
                "duration_seconds": result.duration_seconds,
                "runtime_path": result.runtime_path,
            }

        # Emit per-tool-call progress for observability
        for i, tc in enumerate(result.tool_trace[:20]):
            self._emit_progress(
                progress_callback,
                stage=f"{analysis_kind}_tool_call",
                message=f"tool[{i+1}/{result.tool_calls}]: {tc.get('tool', '?')}",
                tool=tc.get("tool", ""),
                args=tc.get("args", "")[:100],
            )

        # Write analysis markdown
        markdown = result.markdown or str(result.output)
        analysis_markdown_path.write_text(markdown + "\n", encoding="utf-8")

        # Validate evidence
        valid, reason, evidence_count = self._validate_custom_skill_analysis(analysis_markdown_path)
        if not valid:
            logger.warning("pydantic-ai source analysis output missing evidence: %s", reason)
            return {
                "ok": False,
                "error_code": "pydantic_ai_invalid_evidence",
                "message": f"pydantic-ai 源码分析输出缺少有效关键证据：{reason}",
                "command": [],
                "provider": provider_tag,
                "analysis_markdown_path": analysis_markdown_path,
                "duration_seconds": result.duration_seconds,
                "evidence_count": evidence_count,
            }

        # Write report
        self._write_custom_skill_agent_report(
            analysis_kind=analysis_kind,
            analysis_label=self._analysis_label(analysis_kind),
            html_path=html_path,
            json_path=json_path,
            analysis_markdown_path=analysis_markdown_path,
            skill_name=skill_name,
            provider=provider_tag,
            request_text=request_text,
            prompt_text=prompt_text,
            title=title,
            description=description,
            fault_time=fault_time,
            selected_input=selected_input,
            prepared_input=prepared_input,
            source_evidence_path=source_evidence_path,
            evidence_count=evidence_count,
            duration_seconds=result.duration_seconds,
            executor=provider_tag,
        )

        self._emit_progress(
            progress_callback,
            stage=f"{analysis_kind}_pydantic_ai_done",
            message=f"源码分析完成（{result.duration_seconds:.1f}s, pydantic-ai runtime）",
            provider=provider_tag,
            model=result.model,
            evidence_count=evidence_count,
            tool_calls=result.tool_calls,
            runtime_path=result.runtime_path,
        )

        return {
            "ok": True,
            "error_code": "",
            "message": "",
            "command": [],
            "analysis_kind": analysis_kind,
            "provider": provider_tag,
            "executor": provider_tag,
            "analysis_markdown_path": analysis_markdown_path,
            "html_path": html_path,
            "json_path": json_path,
            "stdout": markdown,
            "stderr": "",
            "duration_seconds": result.duration_seconds,
            "evidence_count": evidence_count,
            "runtime_path": result.runtime_path,
            "tool_calls": result.tool_calls,
            "tool_trace": result.tool_trace,
            "usage": result.usage,
            "custom_skill_analysis_status": "completed",
        }

    def _run_ld_pydantic_ai_analysis(
        self,
        *,
        skill_name: str,
        request_text: str,
        prompt_text: str,
        title: str,
        description: str,
        fault_time: str,
        selected_input: Path | None,
        prepared_input: Path | None,
        html_path: Path,
        json_path: Path,
        analysis_dir: Path,
        progress_callback: Callable[[dict[str, object]], None] | None,
        prior_findings: list[tuple[str, str, str]] | None = None,
    ) -> dict[str, object]:
        """Run LD lane-level analysis via pydantic-ai agent runtime (fast path).

        Returns custom_result-compatible dict. On failure, returns ok=False
        so the caller can fall through to direct_api or subprocess.
        """
        from .agent_runtime import AgentRuntime
        from .agent_output_models import LDLaneLevelOutput

        started = time.monotonic()
        provider_tag = "pydantic_ai"
        analysis_kind = "ld_lane_level"
        analysis_dir.mkdir(parents=True, exist_ok=True)
        analysis_markdown_path = analysis_dir / self._skill_agent_analysis_markdown_name(analysis_kind)
        log_workspace = prepared_input.parent if prepared_input is not None and prepared_input.is_file() else (prepared_input or Path(self._working_dir()))
        source_roots = self._custom_skill_agent_source_roots()
        # Lane-level root cause lives in the guideengine/Napa5 source, so run the
        # agent with the source repo as the working directory and keep the logs
        # reachable as extra roots (logs are referenced by absolute path anyway).
        primary_workspace = source_roots[0] if source_roots else log_workspace
        extra_roots: list[Path] = []
        for root in [log_workspace, *source_roots[1:], Path(self._working_dir()), analysis_dir]:
            if root and root != primary_workspace and root not in extra_roots:
                extra_roots.append(root)
        report_dir = analysis_dir.parent if analysis_dir.exists() else None
        log_metadata_path = self._ld_prepared_log_metadata_path(
            prepared_input=prepared_input,
            analysis_dir=analysis_dir,
            fault_time=fault_time,
        )

        ai_opts = self.config.ai_provider
        runtime = AgentRuntime(ai_opts, workspace=primary_workspace)

        if not runtime.is_available():
            return {
                "ok": False,
                "error_code": "pydantic_ai_not_available",
                "message": "pydantic-ai runtime 不可用，将 fallback 到 direct_api/file_agent。",
                "command": [],
                "provider": provider_tag,
                "analysis_markdown_path": analysis_markdown_path,
                "duration_seconds": time.monotonic() - started,
            }

        self._emit_progress(
            progress_callback,
            stage="ld_pydantic_ai",
            message=f"LD 车道级分析：pydantic-ai runtime（{ai_opts.primary_model}）",
            provider=provider_tag,
            model=ai_opts.primary_model,
        )

        # Build skill context
        skill_record = self.skill_manager.get_skill(skill_name, include_content=True)
        skill_content = skill_record.content if skill_record and skill_record.content else ""

        # Build prior findings context
        prior_context = ""
        if prior_findings:
            parts = []
            for kind, label, content in prior_findings:
                parts.append(f"### {label} ({kind})\n{content[:5000]}")
            prior_context = "\n\n".join(parts)

        system_prompt = (
            "你是一个专业的 LD 车道级问题分析 Agent，通过飞书触发。"
            "你的任务是根据 Bug 信息、日志证据（蒙特卡洛日志、瓦片渲染日志），分析车道级显示异常的根因。"
            "使用提供的工具（read_file, get_file_outline, grep, glob, list_dir, git_log, think）来探索日志文件和代码库。"
            "工作目录是 guideengine 源码根。当日志不覆盖故障时间、缺少 LD 状态日志或日志搜索连续 2-3 次无命中时，"
            "必须转向阅读 guideengine/Napa5 源码（如 LdActionProcess、LDDataModel.CheckLDState、Android→Unity 信号桥、tile 加载逻辑），"
            "用源码文件+行号解释 LD 车道级判定链路与可能断点，不要在日志里反复空搜。"
            "复杂分析前先用 think() 记录分析思路。"
            "对大文件先用 get_file_outline 了解结构，再用 read_file(start_line, end_line) 精读关键段落。"
            "必须输出结构化的分析结果，包含：conclusion, root_cause, evidence, montecarlo_findings, tile_render_findings, pending_items, suggested_actions。"
            "每条 evidence 必须包含具体的 file 路径和 line 号。"
            "证据不足时明确写待确认，不要编造。"
            "输出中文。"
        )

        user_prompt = (
            f"# LD 车道级分析请求\n\n"
            f"## Bug 信息\n"
            f"- 标题: {title}\n"
            f"- 故障时间: {fault_time}\n"
            f"- 用户请求: {request_text}\n"
            f"- 缺陷描述: {description}\n\n"
        )
        if skill_content:
            user_prompt += f"## 分析方法论\n{skill_content[:6000]}\n\n"
        if prior_context:
            user_prompt += f"## 前序分析结果\n{prior_context[:10000]}\n\n"
        source_roots_text = "\n".join(f"  - `{r}`" for r in source_roots) or "  - （未配置源码根）"
        user_prompt += (
            "## 检索边界\n"
            f"- 工作目录（源码根）: `{primary_workspace}`，可直接阅读 guideengine/Napa5 源码定位 LD 车道级状态机与 Android→Unity 信号桥实现。\n"
            "- 其他可读源码根:\n"
            f"{source_roots_text}\n"
            f"- 主输入日志: `{prepared_input}`，通过 read_prepared_log_metadata() 返回的绝对路径访问（日志目录已在可读根内）。\n"
            "- 必须先调用 read_prepared_log_metadata()，按元数据里的主日志和同目录候选文件检索日志证据。\n"
            "- 源码检索先收敛到 LD/车道级相关模块（如 LdActionProcess、LDDataModel.CheckLDState、tile 加载/信号桥），再按需扩展；禁止扫描 bug_cache 以外的历史 job 目录。\n\n"
            "请结合源码与日志：在源码中定位 LD 车道级关键实现（文件+行号），并用蒙特卡洛/瓦片日志证据印证根因。\n"
            "必须提供具体证据（源码文件+行号 或 日志文件+行号+内容）。"
        )

        try:
            def _tool_progress(*, stage: str, message: str, **kw: object) -> None:
                self._emit_progress(progress_callback, stage=stage, message=message, **kw)

            result = runtime.run(
                output_type=LDLaneLevelOutput,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                tools_enabled=True,
                strict_tools=True,
                extra_roots=extra_roots,
                report_dir=report_dir,
                log_metadata_path=log_metadata_path,
                progress_callback=_tool_progress,
                stream=True,
            )
        except Exception as exc:
            logger.warning("pydantic-ai LD analysis failed: %s", exc)
            return {
                "ok": False,
                "error_code": "pydantic_ai_execution_error",
                "message": f"pydantic-ai LD 分析执行失败：{exc}",
                "command": [],
                "provider": provider_tag,
                "analysis_markdown_path": analysis_markdown_path,
                "duration_seconds": time.monotonic() - started,
            }

        if not result.ok:
            return {
                "ok": False,
                "error_code": result.error_code or "pydantic_ai_failed",
                "message": result.error or "pydantic-ai LD 分析失败",
                "command": [],
                "provider": provider_tag,
                "analysis_markdown_path": analysis_markdown_path,
                "duration_seconds": result.duration_seconds,
            }

        # Write analysis markdown
        markdown = result.markdown or str(result.output)
        analysis_markdown_path.write_text(markdown + "\n", encoding="utf-8")

        # Validate evidence
        valid, reason, evidence_count = self._validate_custom_skill_analysis(analysis_markdown_path)
        if not valid:
            logger.warning("pydantic-ai LD analysis output missing evidence: %s", reason)
            return {
                "ok": False,
                "error_code": "pydantic_ai_invalid_evidence",
                "message": f"pydantic-ai LD 分析输出缺少有效关键证据：{reason}",
                "command": [],
                "provider": provider_tag,
                "analysis_markdown_path": analysis_markdown_path,
                "duration_seconds": result.duration_seconds,
                "evidence_count": evidence_count,
            }

        # Write report
        self._write_custom_skill_agent_report(
            analysis_kind=analysis_kind,
            analysis_label=self._analysis_label(analysis_kind),
            html_path=html_path,
            json_path=json_path,
            analysis_markdown_path=analysis_markdown_path,
            skill_name=skill_name,
            provider=provider_tag,
            request_text=request_text,
            prompt_text=prompt_text,
            title=title,
            description=description,
            fault_time=fault_time,
            selected_input=selected_input,
            prepared_input=prepared_input,
            source_evidence_path=None,
            evidence_count=evidence_count,
            duration_seconds=result.duration_seconds,
            executor=provider_tag,
        )

        self._emit_progress(
            progress_callback,
            stage="ld_pydantic_ai_done",
            message=f"LD 车道级分析完成（{result.duration_seconds:.1f}s, pydantic-ai runtime）",
            provider=provider_tag,
            model=result.model,
            evidence_count=evidence_count,
            tool_calls=result.tool_calls,
            runtime_path=result.runtime_path,
        )

        return {
            "ok": True,
            "error_code": "",
            "message": "",
            "command": [],
            "analysis_kind": analysis_kind,
            "provider": provider_tag,
            "executor": provider_tag,
            "analysis_markdown_path": analysis_markdown_path,
            "html_path": html_path,
            "json_path": json_path,
            "stdout": markdown,
            "stderr": "",
            "duration_seconds": result.duration_seconds,
            "evidence_count": evidence_count,
            "runtime_path": result.runtime_path,
            "tool_calls": result.tool_calls,
            "tool_trace": result.tool_trace,
            "usage": result.usage,
            "custom_skill_analysis_status": "completed",
        }

    def _run_ld_direct_api_analysis(
        self,
        *,
        skill_name: str,
        request_text: str,
        prompt_text: str,
        title: str,
        description: str,
        fault_time: str,
        selected_input: Path | None,
        prepared_input: Path | None,
        html_path: Path,
        json_path: Path,
        analysis_dir: Path,
        progress_callback: Callable[[dict[str, object]], None] | None,
    ) -> dict[str, object]:
        """Run LD lane-level analysis via executor + direct LLM API (fast path)."""
        from .llm_client import LLMClient, LLMClientError

        started = time.monotonic()
        analysis_dir.mkdir(parents=True, exist_ok=True)
        analysis_markdown_path = analysis_dir / self._skill_agent_analysis_markdown_name("ld_lane_level")
        context_path = analysis_dir / self._skill_agent_sidecar_name("ld_lane_level", "context.md")
        provider_tag = "direct_api"

        # Phase 1: Executor — extract LD evidence
        self._emit_progress(
            progress_callback,
            stage="ld_executor_extract",
            message="LD 车道级执行器：正在提取 montecarlo 日志证据",
        )
        cache_dir = self._resolve_bug_cache_dir(prepared_input or selected_input)
        log_files = self._ld_executor_find_log_files(
            cache_dir=cache_dir,
            fault_time=fault_time,
        ) if cache_dir else []
        evidence_text = self._ld_executor_extract_evidence(
            log_files=log_files,
            fault_time=fault_time,
        )
        executor_duration = time.monotonic() - started
        self._emit_progress(
            progress_callback,
            stage="ld_executor_done",
            message=f"LD 执行器完成：{len(log_files)} 文件，{executor_duration:.1f}s",
            log_file_count=len(log_files),
        )

        # Read SKILL.md
        skill_record = self.skill_manager.get_skill(skill_name, include_content=True)
        skill_content = skill_record.content if skill_record.content else ""

        # Read reference file
        reference_content = ""
        for ref_path in self._skill_context_paths(skill_name):
            for md_file in sorted(ref_path.glob("*.md")) if ref_path.is_dir() else ([ref_path] if ref_path.suffix == ".md" else []):
                try:
                    ref_text = md_file.read_text(encoding="utf-8", errors="replace")
                    if len(reference_content) + len(ref_text) < 35000:
                        reference_content += f"\n\n---\n### Reference: {md_file.name}\n{ref_text}"
                except OSError:
                    continue

        # Phase 2: Direct API call
        ai_opts = self.config.ai_provider
        client = LLMClient(ai_opts)
        if not client.is_available():
            return {
                "ok": False,
                "error_code": "ld_direct_api_not_configured",
                "message": "LD 车道级分析：direct_api 未配置（ai_provider 不可用），需要 fallback 到 file_agent。",
                "command": [],
                "provider": provider_tag,
                "analysis_markdown_path": analysis_markdown_path,
                "duration_seconds": time.monotonic() - started,
            }

        self._emit_progress(
            progress_callback,
            stage="ld_direct_api_call",
            message=f"LD 车道级分析：调用 API 进行链路分析（{ai_opts.primary_model}）",
            provider=provider_tag,
            model=ai_opts.primary_model,
        )

        system_prompt = (
            "你是一个专业的 LD 车道级日志分析 Agent。"
            "你的任务是根据 SKILL.md 的分析方法论和已预提取的日志证据，分析 LD 车道级渲染问题的根因。"
            "只读分析，不修改文件。输出中文 Markdown。"
            "必须包含这些二级标题：## 结论摘要、## 关键证据、## 最可能原因、## 待确认项、## 建议动作。"
            "## 关键证据 不能为空，每条证据要回指到具体日志行和时间。"
            "证据不足时明确写待确认，不要编造。"
        )

        user_prompt = (
            f"# LD 车道级渲染问题分析\n\n"
            f"## Bug 信息\n"
            f"- 标题: {title}\n"
            f"- 故障时间: {fault_time}\n"
            f"- 用户请求: {request_text}\n"
            f"- 缺陷描述: {description}\n\n"
        )
        if skill_content:
            # Truncate SKILL.md to key sections
            user_prompt += f"## 分析方法论 (SKILL.md)\n{skill_content[:6000]}\n\n"
        if reference_content:
            user_prompt += f"## 参考资料\n{reference_content[:25000]}\n\n"
        user_prompt += evidence_text

        # Write context for audit
        context_path.write_text(user_prompt[:5000] + "\n\n[... truncated for audit ...]\n", encoding="utf-8")

        try:
            response = client.generate_summary(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
        except LLMClientError as exc:
            logger.warning("LD direct API analysis failed: %s", exc)
            return {
                "ok": False,
                "error_code": "ld_direct_api_error",
                "message": f"LD 车道级分析 API 调用失败：{exc}",
                "command": [],
                "provider": provider_tag,
                "analysis_markdown_path": analysis_markdown_path,
                "duration_seconds": time.monotonic() - started,
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("LD direct API unexpected error: %s", exc)
            return {
                "ok": False,
                "error_code": "ld_direct_api_unexpected",
                "message": f"LD 车道级分析 API 意外错误：{exc}",
                "command": [],
                "provider": provider_tag,
                "analysis_markdown_path": analysis_markdown_path,
                "duration_seconds": time.monotonic() - started,
            }

        message = (response.content or "").strip()
        if not message:
            return {
                "ok": False,
                "error_code": "ld_direct_api_empty",
                "message": "LD 车道级分析 API 返回空响应。",
                "command": [],
                "provider": provider_tag,
                "analysis_markdown_path": analysis_markdown_path,
                "duration_seconds": time.monotonic() - started,
            }

        # Write analysis output
        analysis_markdown_path.write_text(message + "\n", encoding="utf-8")
        duration = time.monotonic() - started

        # Validate
        valid, reason, evidence_count = self._validate_custom_skill_analysis(analysis_markdown_path)
        if not valid:
            return {
                "ok": False,
                "error_code": "ld_direct_api_invalid_evidence",
                "message": f"LD 车道级 direct_api 分析输出缺少有效 `## 关键证据`：{reason}",
                "command": [],
                "provider": provider_tag,
                "analysis_markdown_path": analysis_markdown_path,
                "duration_seconds": duration,
                "evidence_count": evidence_count,
            }

        # Generate report
        self._write_custom_skill_agent_report(
            analysis_kind="ld_lane_level",
            analysis_label=self._analysis_label("ld_lane_level"),
            html_path=html_path,
            json_path=json_path,
            analysis_markdown_path=analysis_markdown_path,
            skill_name=skill_name,
            provider=provider_tag,
            request_text=request_text,
            prompt_text=prompt_text,
            title=title,
            description=description,
            fault_time=fault_time,
            selected_input=selected_input,
            prepared_input=prepared_input,
            source_evidence_path=None,
            evidence_count=evidence_count,
            duration_seconds=duration,
        )

        self._emit_progress(
            progress_callback,
            stage="ld_direct_api_done",
            message=f"LD 车道级分析完成（{duration:.1f}s, executor+direct_api）",
            provider=provider_tag,
            model=response.model or ai_opts.primary_model,
            evidence_count=evidence_count,
        )

        return {
            "ok": True,
            "error_code": "",
            "message": "",
            "command": [],
            "analysis_kind": "ld_lane_level",
            "provider": provider_tag,
            "analysis_markdown_path": analysis_markdown_path,
            "html_path": html_path,
            "json_path": json_path,
            "context_path": context_path,
            "duration_seconds": duration,
            "evidence_count": evidence_count,
            "custom_skill_analysis_status": "completed",
            "execution_backend": "ld_executor_direct_api",
        }

    def _resolve_bug_cache_dir(self, input_path: Path | None) -> Path | None:
        """Walk up from input_path to find the bug_cache/<id>/ directory."""
        if input_path is None:
            return None
        current = input_path.resolve()
        for _ in range(10):
            if current.name == "logs" and (current.parent / "attachments").exists():
                return current.parent
            if current.name.startswith("xpfailuremgmt_") or current.name.startswith("bug_"):
                return current
            if current.parent == current:
                break
            current = current.parent
        # Fallback: try to find bug_cache in known data dir
        data_dir = Path(self.config.workspace_root) / "tools" / "lark-agent-bridge" / "data" / "bug_cache"
        if not data_dir.exists():
            data_dir = Path(__file__).resolve().parent.parent.parent / "data" / "bug_cache"
        if data_dir.exists():
            # Return the most recent bug cache dir
            candidates = sorted(data_dir.iterdir(), key=lambda p: p.stat().st_mtime if p.is_dir() else 0, reverse=True)
            if candidates:
                return candidates[0]
        return None

    def _validate_custom_skill_analysis(self, analysis_markdown_path: Path) -> tuple[bool, str, int]:
        try:
            text = analysis_markdown_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False, "missing_analysis_markdown", 0
        lines = text.splitlines()
        start: int | None = None
        for index, line in enumerate(lines):
            if re.match(r"^\s*##\s+关键证据\s*$", line):
                start = index + 1
                break
        if start is None:
            return False, "missing_key_evidence_section", 0
        section_lines: list[str] = []
        for line in lines[start:]:
            if re.match(r"^\s*##\s+", line):
                break
            section_lines.append(line)
        evidence_lines = [
            line.strip()
            for line in section_lines
            if line.strip() and not line.strip().startswith("<!--")
        ]
        if not evidence_lines:
            return False, "empty_key_evidence_section", 0
        return True, "", len(evidence_lines)

    def _parse_markdown_sections(self, markdown_text: str) -> dict[str, str]:
        sections: dict[str, str] = {}
        current_title = ""
        current_lines: list[str] = []
        for raw_line in markdown_text.splitlines():
            match = re.match(r"^\s*##\s+(.+?)\s*$", raw_line)
            if match:
                if current_title:
                    sections[current_title] = "\n".join(current_lines).strip()
                current_title = match.group(1).strip()
                current_lines = []
                continue
            if current_title:
                current_lines.append(raw_line.rstrip())
        if current_title:
            sections[current_title] = "\n".join(current_lines).strip()
        return sections

    def _markdown_section_entries(self, section_text: str) -> list[str]:
        entries: list[str] = []
        current: list[str] = []
        for raw_line in section_text.splitlines():
            stripped = raw_line.strip()
            if not stripped:
                if current:
                    entries.append(" ".join(current).strip())
                    current = []
                continue
            if re.match(r"^[-*]\s+", stripped):
                if current:
                    entries.append(" ".join(current).strip())
                current = [re.sub(r"^[-*]\s+", "", stripped)]
            else:
                if current:
                    current.append(stripped)
                else:
                    current = [stripped]
        if current:
            entries.append(" ".join(current).strip())
        return [entry for entry in entries if entry]

    def _clean_markdown_inline_text(self, text: str) -> str:
        cleaned = text.strip()
        cleaned = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", cleaned)
        cleaned = re.sub(r"`([^`]+)`", r"\1", cleaned)
        cleaned = re.sub(r"\*\*([^*]+)\*\*", r"\1", cleaned)
        cleaned = re.sub(r"\*([^*]+)\*", r"\1", cleaned)
        cleaned = re.sub(r"__([^_]+)__", r"\1", cleaned)
        cleaned = re.sub(r"_([^_]+)_", r"\1", cleaned)
        return re.sub(r"\s+", " ", cleaned).strip()

    def _truncate_report_text(self, text: str, limit: int = 180) -> str:
        normalized = self._clean_markdown_inline_text(text)
        if len(normalized) <= limit:
            return normalized
        return normalized[: limit - 1].rstrip() + "…"

    def _source_stage_highlight_items(self, section_text: str, *, sev: str, limit: int = 4) -> list[dict[str, str]]:
        items: list[dict[str, str]] = []
        for entry in self._markdown_section_entries(section_text)[:limit]:
            items.append({
                "sev": sev,
                "title": self._truncate_report_text(entry, 160),
                "detail": "",
            })
        return items

    def _source_stage_evidence_rows(self, section_text: str, *, limit: int = 6) -> list[tuple[str, str]]:
        rows: list[tuple[str, str]] = []
        for entry in self._markdown_section_entries(section_text)[:limit]:
            location = "证据"
            detail = entry
            match = re.match(r"^\*\*(.+?)\*\*:\s*(.+)$", entry)
            if match:
                location = self._clean_markdown_inline_text(match.group(1))
                detail = match.group(2)
            rows.append((location, self._truncate_report_text(detail, 160)))
        return rows

    def _source_stage_report_sections(self, analysis_text: str) -> list[ReportSection]:
        sections = self._parse_markdown_sections(analysis_text)
        rendered: list[ReportSection] = []

        summary_items = self._source_stage_highlight_items(sections.get("结论摘要", ""), sev="green", limit=5)
        if summary_items:
            rendered.append(ReportSection(kind="issues", title="结论摘要", items=summary_items))

        cause_entries = self._markdown_section_entries(sections.get("最可能原因", ""))
        if cause_entries:
            rendered.append(
                ReportSection(
                    kind="text",
                    title="最可能原因",
                    text=self._truncate_report_text("\n\n".join(cause_entries[:2]), 320),
                    class_name="insight",
                )
            )

        evidence_rows = self._source_stage_evidence_rows(sections.get("关键证据", ""))
        if evidence_rows:
            rendered.append(
                ReportSection(
                    kind="table",
                    title="关键证据",
                    cols=["位置", "关键点"],
                    rows=evidence_rows,
                    empty_text="未提取到关键证据摘要",
                )
            )

        pending_items = self._source_stage_highlight_items(sections.get("待确认项", ""), sev="yellow", limit=5)
        if pending_items:
            rendered.append(ReportSection(kind="issues", title="待确认项", items=pending_items))

        action_items = self._source_stage_highlight_items(sections.get("建议动作", ""), sev="green", limit=5)
        if action_items:
            rendered.append(ReportSection(kind="issues", title="建议动作", items=action_items))

        return rendered

    def _write_custom_skill_agent_report(
        self,
        *,
        analysis_kind: str,
        analysis_label: str,
        html_path: Path,
        json_path: Path,
        analysis_markdown_path: Path,
        skill_name: str,
        provider: str,
        request_text: str,
        prompt_text: str,
        title: str,
        description: str,
        fault_time: str,
        selected_input: Path | None,
        prepared_input: Path | None,
        source_evidence_path: Path | None,
        evidence_count: int,
        duration_seconds: float,
        executor: str = "file_agent",
        extra_payload: dict[str, object] | None = None,
    ) -> None:
        analysis_text = analysis_markdown_path.read_text(encoding="utf-8", errors="replace")
        analysis_file_name = analysis_markdown_path.name
        skill_paths = self._skill_context_paths(skill_name)
        executor_label = executor or "file_agent"
        verdict_text = f"{analysis_label} `{skill_name}` 已通过 {executor_label} 执行器产出执行证据，允许进入最终总结。"
        cards = [
            ("执行器", executor_label, "green", provider or "未记录 provider"),
            ("命中 Skill", skill_name or "未记录", "green" if skill_name else "yellow", ""),
            ("关键证据", str(evidence_count), "green" if evidence_count > 0 else "red", f"来自 {analysis_file_name} 的 ## 关键证据"),
            ("故障时间", fault_time or "未识别", "green" if fault_time else "yellow", ""),
            ("日志输入", selected_input.name if selected_input else "无", "green" if selected_input else "yellow", str(selected_input or "")),
            ("执行耗时", f"{duration_seconds:.1f}s", "green", ""),
        ]
        context_rows = [
            ("Bug 标题", title or "未返回 / 未设置"),
            ("用户请求", prompt_text or request_text or "未设置"),
            ("原始请求", request_text or "未设置"),
            ("故障时间", fault_time or "未识别"),
            ("selected log input", str(selected_input or "未提供")),
            ("prepared log input", str(prepared_input or "未提供")),
            ("source evidence", str(source_evidence_path or "未生成")),
            (analysis_file_name, str(analysis_markdown_path)),
        ]
        skill_rows = [(path.name, str(path)) for path in skill_paths]
        detail_body = (
            "<pre class=\"evidence-block\">"
            + html_lib.escape(analysis_text.strip() or "(空)")
            + "</pre>"
        )
        raw_body = (
            "<div class=\"split-grid\">"
            f"{combined_bug_html.render_table([('原始请求', request_text.strip() or '(无请求)')], ('字段', '内容'))}"
            f"{combined_bug_html.render_table([('缺陷描述', description.strip() or '(无描述)')], ('字段', '内容'))}"
            "</div>"
        )
        summary_sections = self._source_stage_report_sections(analysis_text) if _kind_spec(analysis_kind).is_source_stage else []
        composition = ReportComposition(
            title=analysis_label,
            heading=analysis_label,
            subtitle=f"Bug 标题：{title or '未返回 / 未设置'}",
            verdict=ReportVerdict(sev="green", text=verdict_text),
            cards=cards,
            sections=
            summary_sections
            + [
                ReportSection(
                    kind="issues",
                    title="执行状态",
                    items=[
                        {
                            "sev": "green",
                            "title": "Execution artifact 已生成",
                            "detail": f"`{analysis_markdown_path}` 已通过 `## 关键证据` 非空校验。",
                        }
                    ],
                ),
                ReportSection(kind="table", title="Skill 上下文", cols=["文件", "路径"], rows=skill_rows, empty_text="未找到 Skill 文件"),
                ReportSection(kind="table", title="分析上下文", cols=["字段", "内容"], rows=context_rows),
                ReportSection(kind="details", title="完整分析", summary="展开查看完整分析 Markdown", body_html=detail_body),
                ReportSection(kind="details", title="原始输入", summary="展开查看请求与缺陷描述", body_html=raw_body),
            ],
        )
        status_key = f"{analysis_kind}_analysis_status"
        payload = {
            "mode": f"{analysis_kind}_agent_analysis",
            "summary": verdict_text,
            "verdict": {"sev": "green", "text": verdict_text},
            status_key: "completed",
            "analysis_kind": analysis_kind,
            "analysis_skill": skill_name,
            "executor": executor,
            "provider": provider,
            "evidence_count": evidence_count,
            "analysis_markdown": str(analysis_markdown_path),
            "fault_time": fault_time,
            "selected_input": str(selected_input) if selected_input else "",
            "prepared_input": str(prepared_input) if prepared_input else "",
            "source_evidence_file": str(source_evidence_path) if source_evidence_path else "",
            "skill_context_files": [str(path) for path in skill_paths],
            "title": title,
            "prompt_text": prompt_text,
            "request_text": request_text,
            "description": description.strip(),
            "duration_seconds": duration_seconds,
        }
        if extra_payload:
            payload.update(extra_payload)
        html_path.write_text(
            combined_bug_html.render_report_shell(**composition_to_renderer_payload(composition)),
            encoding="utf-8",
        )
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _skill_file_agent_execution_details(self, analysis_kind: str, result: dict[str, object]) -> dict[str, object]:
        prefix = analysis_kind
        status_key = f"{prefix}_analysis_status"
        executor = str(result.get("executor") or "file_agent")
        usage = result.get("usage") or {}
        total_tokens = 0
        if isinstance(usage, dict):
            total_tokens = normalize_token_usage(usage).get("total_tokens", 0)
        details = {
            f"{prefix}_executor": executor,
            status_key: str(result.get(status_key) or result.get("custom_skill_analysis_status") or "completed"),
            f"{prefix}_analysis_file": str(result.get("analysis_markdown_path") or ""),
            f"{prefix}_report_html": str(result.get("html_path") or ""),
            f"{prefix}_report_json": str(result.get("json_path") or ""),
            f"{prefix}_context_file": str(result.get("context_path") or ""),
            f"{prefix}_log_focus_manifest": str(result.get("log_focus_manifest_path") or ""),
            f"{prefix}_focused_log_input": str(result.get("focused_log_input") or ""),
            f"{prefix}_debug_log": str(result.get("debug_log_path") or ""),
            f"{prefix}_events_file": str(result.get("events_path") or ""),
            f"{prefix}_evidence_count": int(result.get("evidence_count") or 0),
            f"{prefix}_agent_provider": str(result.get("provider") or ""),
            f"{prefix}_agent_duration_seconds": result.get("duration_seconds") or 0.0,
            f"{prefix}_app_server_thread_id": str(result.get("thread_id") or ""),
            f"{prefix}_app_server_turn_id": str(result.get("turn_id") or ""),
            f"{prefix}_runtime_path": str(result.get("runtime_path") or ""),
            f"{prefix}_tool_calls": int(result.get("tool_calls") or 0),
            f"{prefix}_total_tokens": total_tokens,
        }
        if _kind_spec(analysis_kind).is_source_stage:
            stage_status = str(result.get(status_key) or result.get("custom_skill_analysis_status") or "completed")
            details.update(
                {
                    "source_stage_executor": executor,
                    "source_stage_analysis_status": stage_status,
                    "source_stage_analysis_file": str(result.get("analysis_markdown_path") or ""),
                    "source_stage_report_html": str(result.get("html_path") or ""),
                    "source_stage_report_json": str(result.get("json_path") or ""),
                    "source_stage_context_file": str(result.get("context_path") or ""),
                    "source_stage_log_focus_manifest": str(result.get("log_focus_manifest_path") or ""),
                    "source_stage_focused_log_input": str(result.get("focused_log_input") or ""),
                    "source_stage_debug_log": str(result.get("debug_log_path") or ""),
                    "source_stage_events_file": str(result.get("events_path") or ""),
                    "source_stage_evidence_count": int(result.get("evidence_count") or 0),
                    "source_stage_agent_provider": str(result.get("provider") or ""),
                    "source_stage_agent_duration_seconds": result.get("duration_seconds") or 0.0,
                    "source_stage_app_server_thread_id": str(result.get("thread_id") or ""),
                    "source_stage_app_server_turn_id": str(result.get("turn_id") or ""),
                    "source_stage_runtime_path": str(result.get("runtime_path") or ""),
                    "source_stage_tool_calls": int(result.get("tool_calls") or 0),
                    "source_stage_total_tokens": total_tokens,
                }
            )
        return details

    def _skill_agent_analysis_markdown_name(self, analysis_kind: str) -> str:
        return {
            SOURCE_STAGE_KIND: "source_stage_analysis.md",
            "ld_lane_level": "ld_lane_level_analysis.md",
            "custom_skill": "source_code_skill_analysis.md",
            SOURCE_CODE_SKILL_KIND: "source_code_skill_analysis.md",
        }.get(analysis_kind, f"{analysis_kind}_analysis.md")

    def _skill_agent_sidecar_name(self, analysis_kind: str, suffix: str) -> str:
        prefix = {
            SOURCE_STAGE_KIND: "source_stage",
            "ld_lane_level": "ld_lane_level_agent",
            "custom_skill": "source_code_skill_agent",
            SOURCE_CODE_SKILL_KIND: "source_code_skill_agent",
        }.get(analysis_kind, f"{analysis_kind}_agent")
        return f"{prefix}.{suffix}"

    def _skill_file_agent_mode_error_code(self, analysis_kind: str, base_code: str, *, mode: str) -> str:
        if analysis_kind in _SOURCE_SKILL_KINDS:
            if mode == "bug_reanalysis":
                return base_code.replace("custom_skill_agent_", "custom_skill_reanalysis_agent_", 1)
            if mode == "direct_analysis":
                return base_code.replace("custom_skill_agent_", "direct_custom_skill_agent_", 1)
            return base_code
        if mode == "bug_reanalysis":
            return base_code.replace("custom_skill_agent_", f"{analysis_kind}_reanalysis_agent_", 1)
        if mode == "direct_analysis":
            return base_code.replace("custom_skill_agent_", f"direct_{analysis_kind}_agent_", 1)
        return base_code.replace("custom_skill_agent_", f"{analysis_kind}_agent_", 1)

    def _run_bug_agent_summary(
        self,
        *,
        request_text: str,
        request_artifact: Path,
        metadata_path: Path,
        output_path: Path,
        progress_callback: Callable[[dict[str, object]], None] | None,
        timeout: int,
        provider_session_id: str = "",
        followup_text: str = "",
        previous_summary_path: Path | None = None,
        prefer_lightweight: bool = False,
        provider_override: str = "",
        command_override: str = "",
        bridge_session_id: str = "",
        snapshot_details: dict[str, object] | None = None,
        snapshot_plans: list["BugAnalysisPlan"] | None = None,
    ) -> dict[str, object]:
        explicit_provider = _normalize_provider_name(provider_override)
        if explicit_provider == "omlx":
            return self._annotate_summary_backend_result(
                self._run_bug_agent_summary_omlx_fallback(
                    request_text=request_text,
                    request_artifact=request_artifact,
                    metadata_path=metadata_path,
                    output_path=output_path,
                    followup_text=followup_text,
                    previous_summary_path=previous_summary_path,
                    progress_callback=progress_callback,
                    reason="explicit_agent",
                    allow_file_context=True,
                    snapshot_details=snapshot_details,
                    snapshot_plans=snapshot_plans,
                ),
                execution_backend="omlx",
                backend_reason="explicit_lightweight",
            )
        skip_direct_api = self._should_skip_direct_api_bug_summary(
            request_text=request_text,
            followup_text=followup_text,
            request_artifact=request_artifact,
            metadata_path=metadata_path,
            previous_summary_path=previous_summary_path,
        )
        backend_decision = choose_summary_backend(
            SummaryBackendInput(
                explicit_provider=explicit_provider,
                provider_session_id=provider_session_id,
                prefer_lightweight=False,
                ai_provider_enabled=self.config.ai_provider.enabled,
                ai_provider_base_url=self.config.ai_provider.base_url,
                ai_provider_primary_model=self.config.ai_provider.primary_model,
                skip_direct_api=skip_direct_api,
                auto_fallback_to_file_agent=self.config.bug_analysis.auto_fallback_to_file_agent,
            )
        )
        invocation = self._build_bug_agent_summary_command(
            request_text=request_text,
            request_artifact=request_artifact,
            metadata_path=metadata_path,
            output_path=output_path,
            provider_session_id=provider_session_id,
            followup_text=followup_text,
            previous_summary_path=previous_summary_path,
            provider_override=explicit_provider,
            command_override=command_override,
            snapshot_details=snapshot_details,
            snapshot_plans=snapshot_plans,
        )
        if not invocation["command"]:
            return {
                "message": "",
                "command": None,
                "error": "agent_summary_not_configured",
                "provider": "",
                "session_id": provider_session_id,
                "resumed": False,
                "usage_scope": "",
            }
        explicit_file_agent = bool(explicit_provider)
        if prefer_lightweight:
            omlx_result = self._run_bug_agent_summary_omlx_fallback(
                request_text=request_text,
                request_artifact=request_artifact,
                metadata_path=metadata_path,
                output_path=output_path,
                followup_text=followup_text,
                previous_summary_path=previous_summary_path,
                progress_callback=progress_callback,
                reason="lightweight_first",
                snapshot_details=snapshot_details,
                snapshot_plans=snapshot_plans,
            )
            if omlx_result["message"]:
                return self._annotate_summary_backend_result(
                    omlx_result,
                    execution_backend="omlx",
                    backend_reason="prefer_lightweight",
                )
            self._emit_progress(
                progress_callback,
                stage="bug_agent_summary_lightweight_unavailable",
                message="轻量总结不可用，切换主 Agent 整理最终结论",
                primary_provider=str(invocation["provider"] or ""),
                lightweight_provider="omlx",
                lightweight_error=str(omlx_result.get("error") or ""),
            )
        # --- Direct API path (fast, preferred when [ai_provider] is enabled) ---
        if (
            not explicit_file_agent
            and not provider_session_id.strip()
            and backend_decision.backend == "direct_api"
        ):
            api_result = self._run_bug_agent_summary_via_api(
                request_text=request_text,
                request_artifact=request_artifact,
                metadata_path=metadata_path,
                output_path=output_path,
                followup_text=followup_text,
                previous_summary_path=previous_summary_path,
                progress_callback=progress_callback,
                snapshot_details=snapshot_details,
                snapshot_plans=snapshot_plans,
            )
            if api_result["message"]:
                return self._annotate_summary_backend_result(
                    api_result,
                    execution_backend="direct_api",
                    backend_reason=backend_decision.reason,
                    fallback_from=backend_decision.fallback_from,
                )
            failure_decision = choose_summary_backend(
                SummaryBackendInput(
                    explicit_provider=explicit_provider,
                    provider_session_id=provider_session_id,
                    prefer_lightweight=False,
                    ai_provider_enabled=self.config.ai_provider.enabled,
                    ai_provider_base_url=self.config.ai_provider.base_url,
                    ai_provider_primary_model=self.config.ai_provider.primary_model,
                    skip_direct_api=skip_direct_api,
                    direct_api_failure=str(api_result.get("error") or "direct_api_failed"),
                    auto_fallback_to_file_agent=self.config.bug_analysis.auto_fallback_to_file_agent,
                )
            )
            if failure_decision.backend == "direct_api":
                return self._annotate_summary_backend_result(
                    api_result,
                    execution_backend="direct_api",
                    backend_reason=failure_decision.reason,
                    fallback_from=failure_decision.fallback_from,
                )
            logger.warning(
                "Direct API summary failed (error=%s), falling back to subprocess",
                api_result.get("error", "unknown"),
            )
            backend_decision = failure_decision
        result = self._run_bug_agent_summary_once(
            invocation=invocation,
            output_path=output_path,
            progress_callback=progress_callback,
            timeout=timeout,
            bridge_session_id=bridge_session_id,
        )
        if result["message"] and result["provider"]:
            return self._annotate_summary_backend_result(
                result,
                execution_backend="file_agent",
                backend_reason=backend_decision.reason,
                fallback_from=backend_decision.fallback_from,
            )
        if explicit_file_agent:
            return self._annotate_summary_backend_result(
                result,
                execution_backend="file_agent",
                backend_reason=backend_decision.reason,
                fallback_from=backend_decision.fallback_from,
            )
        if result["message"]:
            return self._annotate_summary_backend_result(
                result,
                execution_backend="file_agent",
                backend_reason=backend_decision.reason,
                fallback_from=backend_decision.fallback_from,
            )
        fallback_result = result
        if provider_session_id.strip() and str(result.get("error") or "") != "agent_summary_timeout":
            self._emit_progress(
                progress_callback,
                stage="bug_agent_summary_retry",
                message="Agent 续会话失败，退回重新读取最新产物整理结论",
                provider=invocation["provider"],
                previous_session_id=provider_session_id.strip(),
            )
            fallback_invocation = self._build_bug_agent_summary_command(
                request_text=request_text,
                request_artifact=request_artifact,
                metadata_path=metadata_path,
                output_path=output_path,
                provider_session_id="",
                followup_text=followup_text,
                previous_summary_path=previous_summary_path,
                snapshot_details=snapshot_details,
                snapshot_plans=snapshot_plans,
            )
            if fallback_invocation["command"]:
                fallback_result = self._run_bug_agent_summary_once(
                    invocation=fallback_invocation,
                    output_path=output_path,
                    progress_callback=progress_callback,
                    timeout=timeout,
                    bridge_session_id=bridge_session_id,
                )
                if fallback_result["message"] and fallback_result["provider"]:
                    return self._annotate_summary_backend_result(
                        fallback_result,
                        execution_backend="file_agent",
                        backend_reason="resume_retry_file_agent",
                    )
        if str(fallback_result.get("error") or "") == "agent_summary_timeout":
            omlx_result = self._run_bug_agent_summary_omlx_fallback(
                request_text=request_text,
                request_artifact=request_artifact,
                metadata_path=metadata_path,
                output_path=output_path,
                followup_text=followup_text,
                previous_summary_path=previous_summary_path,
                progress_callback=progress_callback,
                reason="primary_timeout",
                snapshot_details=snapshot_details,
                snapshot_plans=snapshot_plans,
            )
            if omlx_result["message"]:
                return self._annotate_summary_backend_result(
                    omlx_result,
                    execution_backend="omlx",
                    backend_reason="primary_timeout",
                    fallback_from="file_agent",
                )
            return self._annotate_summary_backend_result(
                fallback_result,
                execution_backend="file_agent",
                backend_reason=backend_decision.reason,
                fallback_from=backend_decision.fallback_from,
            )
        return self._annotate_summary_backend_result(
            fallback_result,
            execution_backend="file_agent",
            backend_reason=backend_decision.reason,
            fallback_from=backend_decision.fallback_from,
        )

    def _annotate_summary_backend_result(
        self,
        result: dict[str, object],
        *,
        execution_backend: str,
        backend_reason: str = "",
        fallback_from: str = "",
    ) -> dict[str, object]:
        annotated = dict(result)
        annotated["execution_backend"] = execution_backend
        annotated["backend_reason"] = backend_reason
        annotated["fallback_from"] = fallback_from
        return annotated

    def _agent_summary_timeout(
        self,
        operation_timeout_seconds: int,
        *,
        reference_seconds: float | None = None,
    ) -> int:
        configured = int(getattr(self.config.bug_analysis, "agent_summary_timeout_seconds", 300) or 0)
        operation_limit = max(1, min(int(operation_timeout_seconds or 0), 1800))
        if configured <= 0:
            base_timeout = operation_limit
        else:
            base_timeout = max(1, min(operation_limit, configured))
        if not isinstance(reference_seconds, (int, float)) or reference_seconds <= 0:
            return base_timeout
        reference_timeout = int(math.ceil(float(reference_seconds) * 1.25))
        return max(1, min(operation_limit, max(base_timeout, reference_timeout)))

    def _agent_summary_timeout_reference(self, previous_session: dict[str, object] | None) -> float | None:
        if not isinstance(previous_session, dict):
            return None
        candidates: list[float] = []
        for value in (previous_session.get("duration_seconds"), previous_session.get("elapsed_seconds")):
            if isinstance(value, (int, float)) and value > 0:
                candidates.append(float(value))
        details = previous_session.get("details")
        if isinstance(details, dict):
            for key in ("agent_summary_duration_seconds", "agent_summary_timeout_seconds"):
                value = details.get(key)
                if isinstance(value, (int, float)) and value > 0:
                    candidates.append(float(value))
        return max(candidates) if candidates else None

    def _run_bug_agent_summary_omlx_fallback(
        self,
        *,
        request_text: str,
        request_artifact: Path,
        metadata_path: Path,
        output_path: Path,
        followup_text: str = "",
        previous_summary_path: Path | None = None,
        progress_callback: Callable[[dict[str, object]], None] | None,
        reason: str = "primary_timeout",
        allow_file_context: bool = False,
        snapshot_details: dict[str, object] | None = None,
        snapshot_plans: list["BugAnalysisPlan"] | None = None,
    ) -> dict[str, object]:
        options = self.config.omlx_chat
        provider = "omlx"
        command = ["omlx", options.model]
        if self._bug_summary_referenced_context_files(metadata_path) and not allow_file_context:
            return {
                "message": "",
                "command": command,
                "error": "omlx_file_context_unsupported",
                "provider": provider,
                "session_id": "",
                "resumed": False,
                "usage": {},
                "usage_scope": "",
            }
        if not options.enabled:
            return {
                "message": "",
                "command": command,
                "error": "omlx_disabled",
                "provider": provider,
                "session_id": "",
                "resumed": False,
                "usage": {},
                "usage_scope": "",
            }
        prompt = self._build_omlx_bug_summary_prompt(
            request_text=request_text,
            request_artifact=request_artifact,
            metadata_path=metadata_path,
            followup_text=followup_text,
            previous_summary_path=previous_summary_path,
            include_context_file_excerpts=allow_file_context,
            snapshot_details=snapshot_details,
            snapshot_plans=snapshot_plans,
        )
        if not prompt.strip():
            return {
                "message": "",
                "command": command,
                "error": "omlx_prompt_empty",
                "provider": provider,
                "session_id": "",
                "resumed": False,
                "usage": {},
                "usage_scope": "",
            }
        self._emit_progress(
            progress_callback,
            stage=(
                "bug_agent_summary_omlx"
                if reason in {"lightweight_first", "explicit_agent"}
                else "bug_agent_summary_omlx_fallback"
            ),
            message=(
                "用户指定 OMLX 本地模型基于已生成材料重新分析"
                if reason == "explicit_agent"
                else "本地 omlx 基于已生成报告/元数据整理最终结论"
                if reason == "lightweight_first"
                else "主 Agent 超时，改用本地 omlx 基于现有材料做轻量总结"
            ),
            provider=provider,
            model=options.model,
            reason=reason,
        )
        started = time.monotonic()
        result = OmlxChatClient(self.config)._chat(
            mode="bug_agent_summary_omlx",
            system_prompt=(
                "你是本地轻量 bug 总结模型。只能基于用户提供的现有 request、metadata、报告摘录和历史摘要回答；"
                "不要声称读取了文件系统或源码；如果材料不足，要明确写出缺口。输出中文 Markdown，结论先行。"
            ),
            messages=[{"role": "user", "content": prompt}],
        )
        if result.success and result.message.strip():
            message = result.message.strip()
            try:
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(message, encoding="utf-8")
            except OSError:
                pass
            return {
                "message": message,
                "command": command,
                "error": "",
                "provider": provider,
                "model": options.model,
                "session_id": "",
                "resumed": False,
                "duration_seconds": result.duration_seconds
                if isinstance(result.duration_seconds, (int, float))
                else time.monotonic() - started,
                "usage": {},
                "usage_scope": "",
            }
        return {
            "message": "",
            "command": command,
            "error": result.error_code or result.message or "omlx_fallback_failed",
            "provider": provider,
            "model": options.model,
            "session_id": "",
            "resumed": False,
            "duration_seconds": result.duration_seconds
            if isinstance(result.duration_seconds, (int, float))
            else time.monotonic() - started,
            "usage": {},
            "usage_scope": "",
        }

    def _bug_prompt_snapshot_path(self, output_dir: Path) -> Path:
        return output_dir / "conversation_facts.json"

    def _read_bug_prompt_snapshot(self, path: Path) -> "prompt_snapshots.BugPromptSnapshot | None":
        try:
            return prompt_snapshots.read_prompt_snapshot(path)
        except (OSError, ValueError):
            return None

    def _write_bug_prompt_snapshot(self, path: Path, snapshot: "prompt_snapshots.BugPromptSnapshot") -> None:
        try:
            prompt_snapshots.write_prompt_snapshot(path, snapshot)
        except OSError:
            return

    def _snapshot_field_from_text(self, text: str, *labels: str) -> str:
        for label in labels:
            match = re.search(rf"(?m)^- {re.escape(label)}:\s*(.+?)\s*$", text)
            if not match:
                continue
            value = match.group(1).strip().strip("`").strip()
            if value:
                return value
        return ""

    def _extract_bug_url_from_text(self, *texts: str) -> str:
        for text in texts:
            if not text:
                continue
            match = re.search(r"https?://project\.feishu\.cn/\S+/buglo/detail/\d+", text)
            if match:
                return match.group(0).rstrip("`，。；;、)")
        return ""

    def _bug_prompt_snapshot_details_from_metadata(
        self,
        *,
        request_text: str,
        metadata_path: Path,
    ) -> dict[str, object]:
        try:
            metadata_text = metadata_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            metadata_text = ""
        details: dict[str, object] = {}
        bug_url = self._extract_bug_url_from_text(request_text, metadata_text)
        if bug_url:
            details["bug_url"] = bug_url
        target_time = self._snapshot_field_from_text(
            metadata_text,
            "修正后的故障时间",
            "故障时间",
            "目标时间",
        )
        if target_time and target_time != "未识别":
            details["target_time"] = target_time
        analysis_value = self._snapshot_field_from_text(metadata_text, "分析类型")
        if analysis_value and analysis_value != "无":
            kinds = [
                item.strip()
                for item in re.split(r"[，,|/]", analysis_value)
                if item.strip() and item.strip() != "无"
            ]
            if kinds:
                details["analysis_kind"] = kinds[0]
                details["analysis_kinds"] = kinds
        if "analysis_kind" not in details:
            skill_name = self._snapshot_field_from_text(metadata_text, "命中 Skill")
            route = self.skill_manager.primary_skill_map().get(skill_name) if skill_name else None
            if route is not None:
                details["analysis_kind"] = route[0]
                details["analysis_kinds"] = [route[0]]
        if "analysis_kind" not in details:
            inferred_kind = ""
            for marker, kind in (
                ("bug_3d_startup_report", "startup"),
                ("bug_3d_stuck_report", "stuck"),
                ("bug_crash_report", "crash"),
                ("bug_scene_signal_report", "scene_signal"),
                ("bug_signal_chain_report", "signal"),
                ("bug_perception_data_summary", "perception"),
                ("bug_xtheme_analysis_report", "xtheme"),
                ("bug_ld_lane_level_report", "ld_lane_level"),
                ("bug_general_analysis_report", "general"),
                ("bug_custom_skill_report", SOURCE_CODE_SKILL_KIND),
                ("bug_source_code_report", SOURCE_CODE_SKILL_KIND),
            ):
                if marker in metadata_text:
                    inferred_kind = kind
                    break
            if inferred_kind:
                details["analysis_kind"] = inferred_kind
                details["analysis_kinds"] = [inferred_kind]
        prepared_input = self._snapshot_field_from_text(
            metadata_text,
            "复用 prepared log 输入",
            "prepared log 输入",
            "prepared log input",
        )
        if prepared_input:
            details["prepared_log_input"] = prepared_input
        selected_input = self._snapshot_field_from_text(
            metadata_text,
            "上一轮选中的日志输入",
            "selected log 输入",
            "selected log input",
        )
        if selected_input:
            details["selected_log_input"] = selected_input
        report_version = self._snapshot_field_from_text(metadata_text, "report_version", "Report Version")
        if report_version:
            details["report_version"] = report_version
        return details

    def _bug_prompt_snapshot_analysis_kind(
        self,
        *,
        details: dict[str, object],
        plans_override: list["BugAnalysisPlan"] | None,
    ) -> str:
        if plans_override:
            for plan in plans_override:
                kind = str(getattr(plan, "kind", "") or "").strip()
                if kind:
                    return kind
        raw_kinds = details.get("analysis_kinds")
        if isinstance(raw_kinds, list):
            for item in raw_kinds:
                kind = str(item or "").strip()
                if kind:
                    return kind
        kind = str(details.get("analysis_kind") or "").strip()
        return kind or "general"

    def _bug_prompt_snapshot_scope_key(
        self,
        *,
        request_text: str,
        details: dict[str, object],
        output_dir: Path | None,
        existing: "prompt_snapshots.BugPromptSnapshot | None",
    ) -> str:
        if existing is not None and existing.scope_key.strip():
            return existing.scope_key
        bug_url = str(details.get("bug_url") or "").strip() or self._extract_bug_url_from_text(request_text)
        bug_id = self._bug_id(bug_url or request_text)
        scope_suffix = output_dir.parent.name if output_dir is not None else "followup"
        return f"bug:{bug_id}:{scope_suffix}"

    def _structured_bug_prompt_snapshot_details(
        self,
        *,
        base_details: dict[str, object] | None,
        request_text: str,
        plans: list["BugAnalysisPlan"] | None = None,
        target_time: str = "",
        prepared_input: Path | None = None,
        selected_input: Path | None = None,
    ) -> dict[str, object]:
        details = dict(base_details) if isinstance(base_details, dict) else {}
        bug_url = str(details.get("bug_url") or "").strip() or self._extract_bug_url_from_text(request_text)
        if bug_url:
            details["bug_url"] = bug_url
        if target_time.strip():
            details["target_time"] = target_time.strip()
        if prepared_input is not None:
            details["prepared_log_input"] = str(prepared_input)
        if selected_input is not None:
            details["selected_log_input"] = str(selected_input)
        if plans:
            details["analysis_kind"] = plans[0].kind
            details["analysis_kinds"] = [plan.kind for plan in plans]
            signal_codes = [plan.signal_code for plan in plans if plan.kind == "signal" and plan.signal_code]
            if signal_codes:
                details["signal_code"] = signal_codes[0]
        return details

    def _followup_needs_previous_summary_text(self, followup_text: str) -> bool:
        lowered = followup_text.casefold()
        compare_terms = (
            "对比上一轮",
            "对比上次",
            "上一轮结论",
            "上次结论",
            "旧结论",
            "老结论",
            "新旧结论",
        )
        if any(term in lowered for term in compare_terms):
            return True
        return (
            ("上次为什么" in lowered or "上一轮为什么" in lowered)
            and ("判断" in lowered or "结论" in lowered)
        )

    def _should_include_previous_summary_for_followup(
        self,
        *,
        followup_text: str,
        request_artifact: Path,
        metadata_path: Path,
    ) -> bool:
        if not followup_text.strip():
            return False
        names = {request_artifact.name.casefold(), metadata_path.name.casefold()}
        if any("reanalysis" in name for name in names):
            return self._followup_needs_previous_summary_text(followup_text)
        return self._followup_needs_previous_summary_text(followup_text)

    def _is_postmortem_or_conflict_followup(self, text: str) -> bool:
        lowered = text.casefold()
        terms = (
            "复盘",
            "前后结论",
            "结论冲突",
            "结论完全不一样",
            "前后不一致",
            "上次为什么",
            "上一轮为什么",
            "错判",
            "重新审视",
        )
        return any(term.casefold() in lowered for term in terms)

    def _metadata_has_structured_report_conflict(self, metadata_path: Path) -> bool:
        for item in self._bug_summary_context_items(metadata_path):
            path = Path(str(item.get("path") or ""))
            if not self._is_report_json_path(path):
                continue
            payload = self._load_structured_report_payload(path)
            if payload is None:
                continue
            focus_session = self._structured_report_focus_session(payload)
            if not isinstance(focus_session, dict):
                continue
            if self._structured_report_conflicts(payload, focus_session):
                return True
        return False

    def _should_skip_direct_api_bug_summary(
        self,
        *,
        request_text: str,
        followup_text: str,
        request_artifact: Path,
        metadata_path: Path,
        previous_summary_path: Path | None,
    ) -> bool:
        if self._should_collect_source_evidence(request_text, followup_text):
            return True
        if self._is_postmortem_or_conflict_followup(request_text) or self._is_postmortem_or_conflict_followup(followup_text):
            return True
        if previous_summary_path is not None and self._should_include_previous_summary_for_followup(
            followup_text=followup_text,
            request_artifact=request_artifact,
            metadata_path=metadata_path,
        ) and self._is_postmortem_or_conflict_followup(followup_text):
            return True
        return self._metadata_has_structured_report_conflict(metadata_path)

    def _is_report_json_path(self, path: Path) -> bool:
        lowered = path.name.casefold()
        return path.suffix.lower() == ".json" and "_report" in lowered

    def _is_report_html_path(self, path: Path) -> bool:
        lowered = path.name.casefold()
        return path.suffix.lower() == ".html" and "_report" in lowered

    def _bug_summary_context_items(
        self,
        metadata_path: Path,
        *,
        include_html_reports: bool = True,
    ) -> list[dict[str, object]]:
        items = self._bug_summary_referenced_context_files(metadata_path)[:5]
        if include_html_reports:
            return items
        has_report_json = any(self._is_report_json_path(Path(str(item.get("path") or ""))) for item in items)
        if not has_report_json:
            return items
        filtered: list[dict[str, object]] = []
        for item in items:
            path = Path(str(item.get("path") or ""))
            if self._is_report_html_path(path):
                continue
            filtered.append(item)
        return filtered

    def _bug_summary_decoded_log_paths(self, context_items: list[dict[str, object]]) -> list[str]:
        paths: list[str] = []
        seen: set[str] = set()
        for item in context_items:
            path = Path(str(item.get("path") or ""))
            if not self._is_report_json_path(path):
                continue
            payload = self._load_structured_report_payload(path)
            if payload is None:
                continue
            decoded_logs = payload.get("decoded_logs")
            if not isinstance(decoded_logs, list):
                continue
            for entry in decoded_logs:
                candidate = str(entry or "").strip()
                if not candidate or candidate in seen:
                    continue
                seen.add(candidate)
                paths.append(candidate)
                if len(paths) >= 8:
                    return paths
        return paths

    def _render_structured_report_guardrails(self, path: Path) -> str:
        if not self._is_report_json_path(path):
            return ""
        payload = self._load_structured_report_payload(path)
        if payload is None:
            return ""
        focus_session = self._structured_report_focus_session(payload)
        if not isinstance(focus_session, dict):
            return ""
        status = str(focus_session.get("status") or "").strip()
        diagnosis = str(focus_session.get("diagnosis") or "").strip()
        missing_critical = [
            str(item).strip()
            for item in focus_session.get("missing_critical") or []
            if str(item).strip()
        ]
        events = focus_session.get("events")
        last_event = events[-1] if isinstance(events, list) and events and isinstance(events[-1], dict) else None
        lines = [
            "### 结构化证据护栏",
            f"来源: {path}",
        ]
        verdict = payload.get("verdict")
        if isinstance(verdict, dict):
            verdict_message = str(verdict.get("message") or "").strip()
            if verdict_message:
                lines.append(f"- 报告顶层 verdict: {verdict_message[:220]}")
        focus_pid = payload.get("focus_session_pid")
        if focus_pid not in (None, ""):
            lines.append(f"- focus session: Session {focus_session.get('index', '?')} / PID {focus_pid}")
        if status:
            lines.append(f"- status={status}")
        if diagnosis:
            lines.append(f"- structured diagnosis: {diagnosis[:220]}")
        if missing_critical:
            lines.append("- missing_critical: " + "、".join(missing_critical[:6]))
        if isinstance(last_event, dict):
            title = str(last_event.get("title") or "").strip()
            timestamp = str(last_event.get("timestamp_text") or last_event.get("timestamp") or "").strip()
            file_path = str(last_event.get("file_path") or "").strip()
            line_no = str(last_event.get("line_no") or "").strip()
            location = f"{file_path}:{line_no}".rstrip(":") if file_path else ""
            detail = " ".join(part for part in [timestamp, title, location] if part).strip()
            if detail:
                lines.append(f"- 最后命中事件: {detail[:260]}")
        for conflict in self._structured_report_conflicts(payload, focus_session):
            lines.append(f"- 冲突: {conflict}")
        if status and status != "complete":
            lines.append(
                "- 护栏: `status` 不是 `complete` 时，不要写“启动链路完整”或“已经到达最终首帧展示”；只能描述为“当前已命中到某节点，后续关键节点未命中，链路未闭环”。"
            )
        if missing_critical:
            lines.append(
                "- 护栏: “未命中关键节点”不等于“日志在这里截止”；除非材料明确显示文件结束或时间窗截断，否则不要把缺节点改写成日志截止。"
            )
        return "\n".join(lines)

    def _load_structured_report_payload(self, path: Path) -> dict[str, object] | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _structured_report_focus_session(self, payload: dict[str, object]) -> dict[str, object] | None:
        sessions = payload.get("sessions")
        if not isinstance(sessions, list) or not sessions:
            return None
        focus_index = payload.get("focus_session_index")
        if isinstance(focus_index, int):
            for item in sessions:
                if isinstance(item, dict) and item.get("index") == focus_index:
                    return item
        first_session = sessions[0]
        return first_session if isinstance(first_session, dict) else None

    def _structured_report_conflicts(
        self,
        payload: dict[str, object],
        focus_session: dict[str, object],
    ) -> list[str]:
        conflicts: list[str] = []
        status = str(focus_session.get("status") or "").strip()
        diagnosis = str(focus_session.get("diagnosis") or "").strip()
        missing_critical = [
            str(item).strip()
            for item in focus_session.get("missing_critical") or []
            if str(item).strip()
        ]
        verdict = payload.get("verdict")
        verdict_message = ""
        if isinstance(verdict, dict):
            verdict_message = str(verdict.get("message") or "").strip()
        complete_phrases = ("启动链路完整", "最终首帧展示", "已经到达 3D 最终首帧展示")
        says_complete = any(phrase in diagnosis or phrase in verdict_message for phrase in complete_phrases)
        if status and status != "complete" and says_complete:
            conflicts.append(f"status={status} 但 report verdict/diagnosis 仍声称链路完整")
        if missing_critical and says_complete:
            conflicts.append("missing_critical 非空，但 report verdict/diagnosis 仍声称链路完整")
        return conflicts

    def _looks_like_startup_event(self, event: dict[str, object]) -> bool:
        title = str(event.get("title") or "").strip()
        return bool(title)

    def _read_same_pid_log_excerpt(
        self,
        *,
        file_path: str,
        pid: int | None,
        line_no: int,
        max_lines: int = 24,
        errors_only: bool = False,
    ) -> list[str]:
        if not file_path or pid is None or line_no <= 0:
            return []
        path = Path(file_path)
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        start = max(0, line_no - 1)
        excerpt: list[str] = []
        pid_token = f" {pid} "
        for raw in lines[start:]:
            if pid_token not in raw:
                continue
            if errors_only and not re.search(r"\s[EW]\s", raw):
                continue
            excerpt.append(raw.strip())
            if len(excerpt) >= max_lines:
                break
        return excerpt

    def _render_bug_summary_evidence_markdown(self, evidence: dict[str, object]) -> str:
        lines = [
            "# Bug Summary Evidence",
            "",
            f"- analysis_kind: `{evidence.get('analysis_kind') or ''}`",
            f"- target_time: `{evidence.get('target_time') or ''}`",
            f"- focus session: `Session {evidence.get('focus_session_index') or '?'} / PID {evidence.get('focus_session_pid') or '?'}`",
            f"- status: `{evidence.get('focus_status') or ''}`",
        ]
        conflicts = evidence.get("report_conflicts") or []
        if conflicts:
            lines.extend(["", "## Report conflicts", ""])
            for item in conflicts:
                lines.append(f"- {item}")
        missing_critical = evidence.get("missing_critical") or []
        if missing_critical:
            lines.extend(["", "## Missing critical nodes", ""])
            for item in missing_critical:
                lines.append(f"- {item}")
        last_event = evidence.get("last_matched_event") or {}
        if isinstance(last_event, dict) and last_event:
            lines.extend(["", "## Last matched lifecycle event", ""])
            for item in (
                last_event.get("timestamp_text"),
                last_event.get("title"),
                f"{last_event.get('file_path') or ''}:{last_event.get('line_no') or ''}".rstrip(":"),
                f"PID {last_event.get('pid')}" if last_event.get("pid") not in (None, "") else "",
                last_event.get("excerpt"),
            ):
                if item:
                    lines.append(f"- {item}")
        trailing = evidence.get("same_pid_trailing_log_excerpt") or []
        if trailing:
            lines.extend(["", "## Same PID trailing logs", ""])
            for item in trailing:
                lines.append(f"- {item}")
        errors = evidence.get("same_pid_error_excerpt") or []
        if errors:
            lines.extend(["", "## Same PID errors", ""])
            for item in errors:
                lines.append(f"- {item}")
        safe_assertions = evidence.get("safe_assertions") or []
        if safe_assertions:
            lines.extend(["", "## Safe assertions", ""])
            for item in safe_assertions:
                lines.append(f"- {item}")
        forbidden_assertions = evidence.get("forbidden_assertions") or []
        if forbidden_assertions:
            lines.extend(["", "## Forbidden assertions", ""])
            for item in forbidden_assertions:
                lines.append(f"- {item}")
        return "\n".join(lines).rstrip() + "\n"

    def _write_bug_summary_evidence(
        self,
        *,
        output_dir: Path,
        analysis_kind: str,
        report_jsons: dict[str, Path | None],
    ) -> Path | None:
        if analysis_kind != "startup":
            return None
        report_json = report_jsons.get("startup")
        if report_json is None or not report_json.exists():
            return None
        payload = self._load_structured_report_payload(report_json)
        if payload is None:
            return None
        focus_session = self._structured_report_focus_session(payload)
        if not isinstance(focus_session, dict):
            return None
        focus_pid = payload.get("focus_session_pid")
        try:
            normalized_pid = int(focus_pid) if focus_pid not in (None, "") else None
        except (TypeError, ValueError):
            normalized_pid = None
        missing_critical = [
            str(item).strip()
            for item in focus_session.get("missing_critical") or []
            if str(item).strip()
        ]
        events = focus_session.get("events")
        event_items = [item for item in events or [] if isinstance(item, dict) and self._looks_like_startup_event(item)]
        last_event = event_items[-1] if event_items else {}
        if normalized_pid is None and isinstance(last_event, dict):
            try:
                normalized_pid = int(last_event.get("pid")) if last_event.get("pid") not in (None, "") else None
            except (TypeError, ValueError):
                normalized_pid = None
        file_path = str(last_event.get("file_path") or "").strip() if isinstance(last_event, dict) else ""
        line_no_raw = last_event.get("line_no") if isinstance(last_event, dict) else 0
        try:
            line_no = int(line_no_raw or 0)
        except (TypeError, ValueError):
            line_no = 0
        trailing_excerpt = self._read_same_pid_log_excerpt(
            file_path=file_path,
            pid=normalized_pid,
            line_no=line_no,
            max_lines=24,
            errors_only=False,
        )
        error_excerpt = self._read_same_pid_log_excerpt(
            file_path=file_path,
            pid=normalized_pid,
            line_no=line_no,
            max_lines=12,
            errors_only=True,
        )
        conflicts = self._structured_report_conflicts(payload, focus_session)
        safe_assertions = [
            "只引用结构化报告已命中的生命周期节点。",
            "如果 `missing_critical` 非空，只能描述为“链路未闭环”。",
        ]
        if trailing_excerpt:
            safe_assertions.append("同一 PID 在最后命中事件之后仍有原始日志继续输出。")
        forbidden_assertions = [
            "启动链路完整",
            "已经到达最终首帧展示",
        ]
        if missing_critical:
            forbidden_assertions.append("日志在 preload 后截止")
        evidence = {
            "analysis_kind": analysis_kind,
            "bug_url": str(payload.get("bug_url") or ""),
            "target_time": str(payload.get("target_time") or ""),
            "focus_session_index": payload.get("focus_session_index") or focus_session.get("index"),
            "focus_session_pid": normalized_pid,
            "focus_status": str(focus_session.get("status") or ""),
            "verdict_message": str((payload.get("verdict") or {}).get("message") if isinstance(payload.get("verdict"), dict) else ""),
            "focus_diagnosis": str(focus_session.get("diagnosis") or ""),
            "report_conflicts": conflicts,
            "missing_critical": missing_critical,
            "last_matched_event": last_event if isinstance(last_event, dict) else {},
            "same_pid_trailing_log_excerpt": trailing_excerpt,
            "same_pid_error_excerpt": error_excerpt,
            "safe_assertions": safe_assertions,
            "forbidden_assertions": forbidden_assertions,
        }
        evidence_json_path = output_dir / "bug_summary_evidence.json"
        evidence_md_path = output_dir / "bug_summary_evidence.md"
        evidence_json_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        evidence_md_path.write_text(self._render_bug_summary_evidence_markdown(evidence), encoding="utf-8")
        return evidence_md_path

    def _append_bug_summary_evidence_metadata(self, metadata_path: Path, evidence_path: Path | None) -> None:
        if evidence_path is None:
            return
        json_path = evidence_path.with_suffix(".json")
        try:
            original = metadata_path.read_text(encoding="utf-8")
        except OSError:
            return
        lines = [
            "",
            "## 结构化证据包",
            "",
            f"- 结构化证据 Markdown: `{evidence_path}`",
            f"- 结构化证据 JSON: `{json_path}`",
        ]
        metadata_path.write_text(original.rstrip() + "\n" + "\n".join(lines).rstrip() + "\n", encoding="utf-8")

    def _build_or_refresh_bug_prompt_snapshot(
        self,
        *,
        details: dict[str, object],
        followup_text: str,
        request_text: str = "",
        output_dir: Path | None = None,
        metadata_path: Path | None = None,
        plans_override: list["BugAnalysisPlan"] | None = None,
    ) -> "prompt_snapshots.BugPromptSnapshot":
        snapshot_path = self._bug_prompt_snapshot_path(output_dir) if output_dir is not None else None
        existing = self._read_bug_prompt_snapshot(snapshot_path) if snapshot_path is not None and snapshot_path.exists() else None
        current_kind = self._bug_prompt_snapshot_analysis_kind(details=details, plans_override=plans_override)
        stable_fact_map: dict[str, str] = {}
        if existing is not None and existing.analysis_kind == current_kind:
            stable_fact_map = {item.label: item.value for item in existing.stable_facts if item.label and item.value}
        bug_url = str(details.get("bug_url") or "").strip() or self._extract_bug_url_from_text(request_text)
        target_time = str(details.get("target_time") or details.get("fault_time") or "").strip()
        report_version = str(details.get("report_version") or "").strip()
        prepared_input = str(details.get("prepared_log_input") or "").strip()
        selected_input = str(details.get("selected_log_input") or "").strip()
        signal_code = ""
        if plans_override:
            for plan in plans_override:
                if str(getattr(plan, "kind", "") or "").strip() != "signal":
                    continue
                signal_code = str(getattr(plan, "signal_code", "") or "").strip()
                if signal_code:
                    break
        if not signal_code and current_kind == "signal":
            signal_code = self._extract_signal_code_for_reanalysis(followup_text)
        for label, value in (
            ("bug_url", bug_url),
            ("analysis_kind", current_kind),
            ("target_time", target_time),
            ("report_version", report_version),
            ("prepared_log_input", prepared_input),
            ("selected_log_input", selected_input),
            ("signal_code", signal_code),
        ):
            if value:
                stable_fact_map[label] = value
        stable_facts = [
            prompt_snapshots.SnapshotFact(label=label, value=stable_fact_map[label])
            for label in (
                "bug_url",
                "analysis_kind",
                "target_time",
                "report_version",
                "prepared_log_input",
                "selected_log_input",
                "signal_code",
            )
            if stable_fact_map.get(label)
        ]
        evidence_refs: list[prompt_snapshots.SnapshotEvidence] = []
        if metadata_path is not None:
            evidence_refs.append(
                prompt_snapshots.SnapshotEvidence(title="Bug Follow-up Metadata", path=str(metadata_path), locator="")
            )
            for item in self._bug_summary_referenced_context_files(metadata_path)[:4]:
                title = str(item.get("title") or "").strip()
                ref_path = str(item.get("path") or "").strip()
                if title and ref_path:
                    evidence_refs.append(prompt_snapshots.SnapshotEvidence(title=title, path=ref_path, locator=""))
        elif existing is not None and existing.analysis_kind == current_kind:
            evidence_refs = list(existing.evidence_refs)
        open_questions = list(existing.open_questions) if existing is not None and existing.analysis_kind == current_kind else []
        snapshot = prompt_snapshots.BugPromptSnapshot(
            scope_key=self._bug_prompt_snapshot_scope_key(
                request_text=request_text,
                details=details,
                output_dir=output_dir,
                existing=existing,
            ),
            analysis_kind=current_kind,
            stable_facts=stable_facts,
            evidence_refs=evidence_refs,
            open_questions=open_questions,
        )
        if snapshot_path is not None:
            self._write_bug_prompt_snapshot(snapshot_path, snapshot)
        return snapshot

    def _build_bug_prompt_snapshot_prefix(
        self,
        *,
        request_text: str,
        metadata_path: Path,
        followup_text: str,
        snapshot_details: dict[str, object] | None = None,
        plans_override: list["BugAnalysisPlan"] | None = None,
    ) -> str:
        if not followup_text.strip():
            return ""
        details = self._bug_prompt_snapshot_details_from_metadata(
            request_text=request_text,
            metadata_path=metadata_path,
        )
        if snapshot_details:
            for key, value in snapshot_details.items():
                if value is None:
                    continue
                if isinstance(value, str) and not value.strip():
                    continue
                if isinstance(value, list) and not value:
                    continue
                details[key] = value
        snapshot = self._build_or_refresh_bug_prompt_snapshot(
            details=details,
            followup_text=followup_text,
            request_text=request_text,
            output_dir=metadata_path.parent,
            metadata_path=metadata_path,
            plans_override=plans_override,
        )
        return self._render_bug_prompt_snapshot_prefix(snapshot, followup_text=followup_text)

    def _render_bug_prompt_snapshot_prefix(
        self,
        snapshot: "prompt_snapshots.BugPromptSnapshot",
        *,
        followup_text: str,
    ) -> str:
        return prompt_snapshots.render_bug_snapshot_prefix(snapshot, followup_text=followup_text).rstrip() + "\n\n"

    def _build_omlx_bug_summary_prompt(
        self,
        *,
        request_text: str,
        request_artifact: Path,
        metadata_path: Path,
        followup_text: str = "",
        previous_summary_path: Path | None = None,
        include_context_file_excerpts: bool = False,
        snapshot_details: dict[str, object] | None = None,
        snapshot_plans: list["BugAnalysisPlan"] | None = None,
    ) -> str:
        budget = max(500, int(getattr(self.config.omlx_chat, "max_prompt_chars", 2000) or 2000))
        snapshot_prefix = ""
        if followup_text.strip():
            snapshot_prefix = self._build_bug_prompt_snapshot_prefix(
                request_text=request_text,
                metadata_path=metadata_path,
                followup_text=followup_text,
                snapshot_details=snapshot_details,
                plans_override=snapshot_plans,
            ).rstrip()
        sections = [
            "请基于以下已生成材料给出轻量 bug 总结。不要编造未提供的日志/源码证据。",
            "输出结构：## 结论摘要、## 关键证据、## 最可能原因、## 待确认项、## 建议动作。",
            snapshot_prefix,
            self._omlx_prompt_section("用户请求", request_text, 420),
            self._omlx_prompt_section("本次追问", followup_text, 240),
            self._omlx_prompt_section("请求文件摘录", self._read_text_excerpt(request_artifact, 500), 500),
            self._omlx_prompt_section("元数据摘录", self._read_text_excerpt(metadata_path, 900), 900),
        ]
        if previous_summary_path is not None and (
            not followup_text.strip()
            or self._should_include_previous_summary_for_followup(
                followup_text=followup_text,
                request_artifact=request_artifact,
                metadata_path=metadata_path,
            )
        ):
            sections.append(
                self._omlx_prompt_section("上一轮摘要摘录", self._read_text_excerpt(previous_summary_path, 700), 700)
            )
        for item in self._bug_summary_referenced_context_files(metadata_path)[:4]:
            path = Path(str(item["path"]))
            if include_context_file_excerpts:
                sections.append(
                    self._omlx_prompt_section(
                        str(item["title"]),
                        self._read_bug_summary_context_excerpt(path, 900),
                        900,
                    )
                )
                continue
            sections.append(
                self._omlx_prompt_section(
                    str(item["title"]),
                    f"本地路径: {item['path']}\n轻量模型不能读取本地文件；需要读取该文件时必须切换 Codex/Claude Agent。",
                    500,
                )
            )
        prompt = "\n\n".join(section for section in sections if section.strip()).strip()
        if len(prompt) > budget:
            prompt = prompt[: budget - 1].rstrip() + "…"
        return prompt

    def _read_bug_summary_context_excerpt(self, path: Path, max_chars: int) -> str:
        text = self._read_text_excerpt(path, max_chars * 2)
        if not text:
            return ""
        if path.suffix.lower() == ".html":
            text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)
            text = re.sub(r"(?s)<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) > max_chars:
            text = text[: max_chars - 1].rstrip() + "…"
        return text

    def _compact_markdown_outline_excerpt(
        self,
        path: Path,
        *,
        max_chars: int,
        max_headings: int = 6,
        max_entries_per_heading: int = 3,
        max_table_rows: int = 6,
    ) -> str:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        body = text.strip()
        if not body:
            return ""
        lines = body.splitlines()
        summary: list[str] = []
        if path.name.casefold() == "skill.md":
            frontmatter_name, frontmatter_desc = _extract_skill_frontmatter(body)
            if frontmatter_name:
                summary.append(f"skill: {frontmatter_name}")
            if frontmatter_desc:
                summary.append(f"description: {frontmatter_desc}")
        title_line = next(
            (
                self._clean_markdown_inline_text(line.lstrip("#").strip())
                for line in lines
                if re.match(r"^\s*#\s+", line)
            ),
            "",
        )
        if title_line and all(not item.endswith(title_line) for item in summary):
            summary.append(title_line)

        # Skip YAML frontmatter if present.
        start_index = 0
        if lines and lines[0].strip() == "---":
            for index in range(1, len(lines)):
                if lines[index].strip() == "---":
                    start_index = index + 1
                    break

        heading_count = 0
        index = start_index
        while index < len(lines) and heading_count < max_headings:
            raw = lines[index]
            heading_match = re.match(r"^\s*#{2,3}\s+(.+?)\s*$", raw)
            if not heading_match:
                index += 1
                continue
            heading = self._clean_markdown_inline_text(heading_match.group(1))
            if heading:
                summary.append(f"## {heading}")
                heading_count += 1
            entries: list[str] = []
            table_rows = 0
            index += 1
            while index < len(lines) and not re.match(r"^\s*#{2,3}\s+", lines[index]):
                stripped = lines[index].strip()
                if not stripped or stripped.startswith("```"):
                    index += 1
                    continue
                if re.match(r"^[-*]\s+", stripped):
                    entry = self._clean_markdown_inline_text(re.sub(r"^[-*]\s+", "", stripped))
                    if entry:
                        entries.append(entry)
                elif "|" in stripped and max_table_rows > 0:
                    cells = [self._clean_markdown_inline_text(cell) for cell in stripped.strip("|").split("|")]
                    if len(cells) >= 2 and not re.fullmatch(r"[-:\s|]+", stripped):
                        head = cells[0]
                        detail = cells[1]
                        entry = f"{head}: {detail}" if detail else head
                        if entry:
                            entries.append(entry)
                            table_rows += 1
                            if table_rows >= max_table_rows:
                                break
                elif len(entries) < max_entries_per_heading:
                    paragraph = self._clean_markdown_inline_text(stripped)
                    if paragraph:
                        entries.append(paragraph)
                if len(entries) >= max_entries_per_heading:
                    # Still advance to the next heading to keep parser aligned.
                    index += 1
                    while index < len(lines) and not re.match(r"^\s*#{2,3}\s+", lines[index]):
                        index += 1
                    break
                index += 1
            for entry in entries[:max_entries_per_heading]:
                summary.append(f"- {entry}")

        rendered = "\n".join(item for item in summary if item.strip()).strip()
        if not rendered:
            return self._read_bug_summary_context_excerpt(path, max_chars)
        if len(rendered) > max_chars:
            rendered = rendered[: max_chars - 1].rstrip() + "…"
        return rendered

    def _compact_structured_report_excerpt(self, path: Path, *, max_chars: int) -> str:
        payload = self._load_structured_report_payload(path)
        if payload is None:
            return self._read_bug_summary_context_excerpt(path, max_chars)
        lines: list[str] = []
        verdict = payload.get("verdict")
        if isinstance(verdict, dict):
            verdict_message = str(verdict.get("message") or "").strip()
            if verdict_message:
                lines.append(f"- verdict: {verdict_message}")
            issues = verdict.get("issues")
            if isinstance(issues, list) and issues:
                lines.append("- top_issues:")
                for item in issues[:4]:
                    if not isinstance(item, dict):
                        continue
                    title = self._clean_markdown_inline_text(str(item.get("title") or ""))
                    detail = self._clean_markdown_inline_text(str(item.get("detail") or ""))
                    if title or detail:
                        lines.append(f"  - {title}: {detail}".rstrip(": "))
        target_time = str(payload.get("target_time") or "").strip()
        if target_time:
            lines.append(f"- target_time: {target_time}")
        focus_session_index = payload.get("focus_session_index")
        focus_session_pid = payload.get("focus_session_pid")
        if focus_session_index not in (None, "") or focus_session_pid not in (None, ""):
            lines.append(f"- focus_session: Session {focus_session_index or '?'} / PID {focus_session_pid or '?'}")
        focus_reason = str(payload.get("focus_reason") or "").strip()
        if focus_reason:
            lines.append(f"- focus_reason: {focus_reason}")
        runtime_context = payload.get("runtime_context")
        if isinstance(runtime_context, dict):
            context_parts: list[str] = []
            for key in ("resource_type", "proto_type", "car_type", "branch", "build_time"):
                value = self._clean_markdown_inline_text(str(runtime_context.get(key) or ""))
                if value:
                    context_parts.append(f"{key}={value}")
            if context_parts:
                lines.append("- runtime_context: " + "; ".join(context_parts))
        exception_chain = payload.get("internal_exception_chain")
        if isinstance(exception_chain, dict):
            summary = self._clean_markdown_inline_text(str(exception_chain.get("summary") or ""))
            if summary:
                lines.append(f"- internal_exception_chain: {summary}")
            hits = exception_chain.get("hits")
            if isinstance(hits, list) and hits:
                lines.append("- exception_hits:")
                for item in hits[:3]:
                    if not isinstance(item, dict):
                        continue
                    label = self._clean_markdown_inline_text(str(item.get("label") or ""))
                    evidence = self._clean_markdown_inline_text(str(item.get("evidence") or ""))
                    if label or evidence:
                        lines.append(f"  - {label}: {evidence}".rstrip(": "))
        focus_session = self._structured_report_focus_session(payload)
        if isinstance(focus_session, dict):
            status = str(focus_session.get("status") or "").strip()
            if status:
                lines.append(f"- focus_status: {status}")
            diagnosis = str(focus_session.get("diagnosis") or "").strip()
            if diagnosis:
                lines.append(f"- focus_diagnosis: {diagnosis}")
            missing_critical = [
                self._clean_markdown_inline_text(str(item))
                for item in focus_session.get("missing_critical") or []
                if self._clean_markdown_inline_text(str(item))
            ]
            if missing_critical:
                lines.append("- missing_critical: " + "、".join(missing_critical[:7]))
            events = focus_session.get("events")
            last_event = events[-1] if isinstance(events, list) and events and isinstance(events[-1], dict) else None
            if isinstance(last_event, dict):
                detail = " ".join(
                    part
                    for part in [
                        str(last_event.get("timestamp_text") or last_event.get("timestamp") or "").strip(),
                        self._clean_markdown_inline_text(str(last_event.get("title") or "")),
                        f"{last_event.get('file_path') or ''}:{last_event.get('line_no') or ''}".rstrip(":"),
                    ]
                    if part
                )
                if detail:
                    lines.append(f"- last_event: {detail}")
        rendered = "\n".join(lines).strip()
        if len(rendered) > max_chars:
            rendered = rendered[: max_chars - 1].rstrip() + "…"
        return rendered

    def _compact_bug_summary_evidence_markdown_excerpt(self, path: Path, *, max_chars: int) -> str:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        sections = self._parse_markdown_sections(text)
        lines = ["# Bug Summary Evidence"]
        preferred_sections = [
            "Report conflicts",
            "Missing critical nodes",
            "Last matched lifecycle event",
            "Safe assertions",
            "Forbidden assertions",
        ]
        for title in preferred_sections:
            section_text = sections.get(title, "")
            if not section_text:
                continue
            lines.append("")
            lines.append(f"## {title}")
            for entry in self._markdown_section_entries(section_text)[:3]:
                lines.append(f"- {self._truncate_report_text(entry, 220)}")
        rendered = "\n".join(lines).strip()
        if len(rendered) > max_chars:
            rendered = rendered[: max_chars - 1].rstrip() + "…"
        return rendered

    def _direct_api_compaction_profile(
        self,
        *,
        metadata_path: Path,
        snapshot_details: dict[str, object] | None = None,
    ) -> str:
        try:
            metadata_text = metadata_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            metadata_text = ""
        details = dict(snapshot_details or {})
        if not details:
            details = self._bug_prompt_snapshot_details_from_metadata(request_text="", metadata_path=metadata_path)
        analysis_kind = str(details.get("analysis_kind") or "").strip()
        if not analysis_kind:
            analysis_kind = self._snapshot_field_from_text(metadata_text, "分析类型")
        skill_name = self._snapshot_field_from_text(metadata_text, "命中 Skill")
        for profile in _DIRECT_API_COMPACTION_PROFILES:
            if skill_name != profile.skill_name:
                continue
            if analysis_kind == profile.analysis_kind:
                return profile.name
            route = self.skill_manager.primary_skill_map().get(skill_name)
            if route is not None and str(route[0] or "").strip() == profile.analysis_kind:
                return profile.name
            if any(marker in metadata_text for marker in profile.metadata_markers):
                return profile.name
        return ""

    def _direct_api_context_excerpt_for_startup_unity_lifecycle(
        self,
        *,
        title: str,
        path: Path,
        max_chars: int,
    ) -> str:
        if self._is_report_json_path(path):
            return self._compact_structured_report_excerpt(path, max_chars=min(max_chars, 2200))
        if title == "Bug Summary Evidence":
            return self._compact_bug_summary_evidence_markdown_excerpt(path, max_chars=min(max_chars, 1600))
        if title.startswith("Matched Skill:"):
            return self._compact_markdown_outline_excerpt(
                path,
                max_chars=min(max_chars, 2200),
                max_headings=6,
                max_entries_per_heading=2,
                max_table_rows=4,
            )
        if title.startswith("Matched Skill Reference:"):
            return self._compact_markdown_outline_excerpt(
                path,
                max_chars=min(max_chars, 1600),
                max_headings=6,
                max_entries_per_heading=4,
                max_table_rows=2,
            )
        return self._read_bug_summary_context_excerpt(path, max_chars)

    def _direct_api_bug_summary_context_excerpt(
        self,
        *,
        title: str,
        path: Path,
        max_chars: int,
        compaction_profile: str,
    ) -> str:
        if not compaction_profile:
            return self._read_bug_summary_context_excerpt(path, max_chars)
        for profile in _DIRECT_API_COMPACTION_PROFILES:
            if profile.name != compaction_profile:
                continue
            handler = getattr(self, profile.handler)
            return handler(title=title, path=path, max_chars=max_chars)
        return self._read_bug_summary_context_excerpt(path, max_chars)

    def _direct_api_bug_summary_embedded_files(
        self,
        *,
        request_artifact: Path,
        metadata_path: Path,
        followup_text: str = "",
        previous_summary_path: Path | None = None,
    ) -> list[dict[str, object]]:
        files: list[dict[str, object]] = []
        if previous_summary_path is not None and (
            not followup_text.strip()
            or self._should_include_previous_summary_for_followup(
                followup_text=followup_text,
                request_artifact=request_artifact,
                metadata_path=metadata_path,
            )
        ):
            files.append({"title": "上一轮 Agent 总结", "path": str(previous_summary_path), "max_chars": 12000})
        title_request = "Bug Agent Follow-up Request" if followup_text.strip() else "Bug Agent Request"
        title_metadata = "Bug Follow-up Metadata" if followup_text.strip() else "Bug Metadata"
        files.append({"title": title_request, "path": str(request_artifact), "max_chars": 12000})
        files.append({"title": title_metadata, "path": str(metadata_path), "max_chars": 12000})
        for item in self._bug_summary_context_items(metadata_path, include_html_reports=False):
            files.append(item)
        return files

    def _omlx_prompt_section(self, title: str, text: str, max_chars: int) -> str:
        body = (text or "").strip()
        if not body:
            return ""
        if len(body) > max_chars:
            body = body[: max_chars - 1].rstrip() + "…"
        return f"### {title}\n{body}"

    def _read_text_excerpt(self, path: Path, max_chars: int) -> str:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        text = text.strip()
        if len(text) > max_chars:
            text = text[: max_chars - 1].rstrip() + "…"
        return text

    def _path_mtime(self, path: Path) -> float | None:
        try:
            return path.stat().st_mtime
        except OSError:
            return None

    def _read_fresh_agent_summary_message(self, output_path: Path, *, previous_mtime: float | None) -> str:
        current_mtime = self._path_mtime(output_path)
        if current_mtime is None:
            return ""
        if previous_mtime is not None and current_mtime <= previous_mtime:
            return ""
        try:
            return output_path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def _run_bug_summary_pydantic_ai(
        self,
        *,
        request_text: str,
        request_artifact: Path,
        metadata_path: Path,
        output_path: Path,
        followup_text: str = "",
        previous_summary_path: Path | None = None,
        progress_callback: Callable[[dict[str, object]], None] | None,
        snapshot_details: dict[str, object] | None = None,
        snapshot_plans: list["BugAnalysisPlan"] | None = None,
    ) -> dict[str, object]:
        """Run bug summary via pydantic-ai agent runtime (structured output + tools).

        Unlike direct_api which inlines all files, this uses tools so the agent
        can selectively read analysis artifacts. Falls back to direct_api on failure.
        """
        from .agent_runtime import AgentRuntime, _check_pydantic_ai

        if not _check_pydantic_ai():
            return {
                "message": "",
                "command": None,
                "error": "pydantic_ai_not_available",
                "provider": "pydantic_ai",
                "session_id": "",
                "resumed": False,
                "duration_seconds": 0.0,
                "usage": {},
                "usage_scope": "",
            }

        ai_opts = self.config.ai_provider
        started = time.monotonic()

        # Build a tool-oriented prompt: list file paths instead of inlining content.
        prompt = self._build_bug_summary_prompt_for_pydantic_ai(
            request_text=request_text,
            request_artifact=request_artifact,
            metadata_path=metadata_path,
            followup_text=followup_text,
            previous_summary_path=previous_summary_path,
            snapshot_details=snapshot_details,
            snapshot_plans=snapshot_plans,
        )
        if not prompt.strip():
            return {
                "message": "",
                "command": None,
                "error": "pydantic_ai_prompt_empty",
                "provider": "pydantic_ai",
                "session_id": "",
                "resumed": False,
                "duration_seconds": time.monotonic() - started,
                "usage": {},
                "usage_scope": "",
            }

        self._emit_progress(
            progress_callback,
            stage="bug_agent_summary_pydantic_ai",
            message="pydantic-ai Agent 整理最终结论（结构化输出 + 工具增强）",
            provider="pydantic_ai",
            model=ai_opts.primary_model,
        )

        system_prompt = (
            "你是一个通过飞书触发的 bug 分析总结 agent。\n"
            "你可以使用 read_file / grep / search_large_log / bash 工具读取分析产物文件。\n"
            "只读分析，不修改文件，不执行写入命令。\n"
            "遇到 *.alog.log、*.xlog.log 或大日志时，必须先用 search_large_log 或 bash rg 搜索关键词定位行号，再用 read_file 精读上下文。\n"
            "不要用连续 read_file 分页扫描大日志。\n"
            "必须完整响应用户原始请求中的所有诉求，输出中文 Markdown，结论先行。\n"
            "所有分析数据的路径已列在用户消息中，请根据文件类型选择合适工具读取。"
        )

        workspace = metadata_path.parent if metadata_path.exists() else Path.cwd()
        runtime = AgentRuntime(ai_opts, workspace=workspace, max_retries=1)
        result = runtime.run(
            system_prompt=system_prompt,
            user_prompt=prompt,
            tools_enabled=True,
            strict_tools=True,
        )

        duration = time.monotonic() - started
        if result.ok and (result.runtime_path != "pydantic_ai_agent" or result.tool_calls == 0):
            logger.warning(
                "pydantic-ai summary returned without tool-backed runtime "
                "(runtime_path=%s, tool_calls=%d); rejecting so caller can fallback",
                result.runtime_path,
                result.tool_calls,
            )
            return {
                "message": "",
                "command": None,
                "error": "pydantic_ai_summary_requires_tools",
                "provider": "pydantic_ai",
                "session_id": "",
                "resumed": False,
                "duration_seconds": duration,
                "usage": result.usage,
                "usage_scope": "",
                "runtime_path": result.runtime_path,
                "tool_calls": result.tool_calls,
                "tool_trace": result.tool_trace,
            }
        if result.ok and result.markdown.strip():
            message = result.markdown.strip()
            # Write to output_path for downstream consumers
            try:
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(message, encoding="utf-8")
            except OSError:
                pass
            self._emit_progress(
                progress_callback,
                stage="bug_agent_summary_completed",
                message=f"pydantic-ai Agent 已整理最终结论（{duration:.1f}s）",
                provider="pydantic_ai",
                model=result.model,
                duration_seconds=round(duration, 1),
                tool_calls=result.tool_calls,
                tool_trace=result.tool_trace[:20],
            )
            return {
                "message": message,
                "command": None,
                "error": "",
                "provider": "pydantic_ai",
                "session_id": "",
                "resumed": False,
                "duration_seconds": duration,
                "usage": result.usage,
                "usage_scope": "summary",
                "runtime_path": result.runtime_path,
                "tool_calls": result.tool_calls,
                "tool_trace": result.tool_trace,
            }

        logger.warning(
            "pydantic-ai summary failed (%.1fs, error=%s), will fallback",
            duration, result.error_code or result.error,
        )
        return {
            "message": "",
            "command": None,
            "error": result.error or "pydantic_ai_summary_failed",
            "provider": "pydantic_ai",
            "session_id": "",
            "resumed": False,
            "duration_seconds": duration,
            "usage": result.usage,
            "usage_scope": "",
        }

    def _build_bug_summary_prompt_for_pydantic_ai(
        self,
        *,
        request_text: str,
        request_artifact: Path,
        metadata_path: Path,
        followup_text: str = "",
        previous_summary_path: Path | None = None,
        snapshot_details: dict[str, object] | None = None,
        snapshot_plans: list["BugAnalysisPlan"] | None = None,
    ) -> str:
        """Build a tool-oriented summary prompt.

        Unlike _build_bug_agent_summary_prompt_for_api which inlines file content,
        this lists file paths so the pydantic-ai agent can use read_file/search tools
        tools to read them selectively.
        """
        prompt = "请基于以下分析材料完成 bug 会话的最终回答。\n"
        prompt += "材料文件路径已列出，请按文件类型选择工具：报告/Markdown 用 read_file，大日志先用 search_large_log 或 bash rg 定位行号。\n\n"

        snapshot_prefix = ""
        if followup_text.strip():
            prompt += (
                "这是续聊/追问。要求：\n"
                "1. 直接回答新问题，延续上一轮分析。\n"
                "2. 使用工具读取已有材料。\n"
                "3. 只读分析，不修改文件。\n"
                "4. 输出中文 Markdown，结论先行。\n\n"
            )
            snapshot_prefix = self._build_bug_prompt_snapshot_prefix(
                request_text=request_text,
                metadata_path=metadata_path,
                followup_text=followup_text,
                snapshot_details=snapshot_details,
                plans_override=snapshot_plans,
            )
        else:
            prompt += (
                "这是全新 bug 分析请求。要求：\n"
                "1. 完整覆盖用户请求里的所有诉求。\n"
                "2. 只读分析，不修改文件。\n"
                "3. 输出中文 Markdown，结论先行。\n\n"
            )

        prompt += (
            "输出结构：\n"
            "## 结论摘要\n- 3-5 条最重要结论，标明置信边界。\n"
            "## 关键证据\n- 每条带文件、行号、时间。\n"
            "## 最可能原因\n- 按可能性排序。\n"
            "## 待确认项\n- 真实证据缺口。\n"
            "## 建议动作\n- 下一轮可执行动作。\n\n"
        )

        if snapshot_prefix:
            prompt += snapshot_prefix

        prompt += f"### 用户原始请求\n{request_text}\n\n"
        if followup_text.strip():
            prompt += f"### 本次追问\n{followup_text.strip()}\n\n"

        # List file paths for the agent to read
        prompt += "### 可用材料文件\n"
        file_list: list[str] = []
        if request_artifact.exists():
            file_list.append(f"- Bug 请求文件: `{request_artifact}`")
        if metadata_path.exists():
            file_list.append(f"- Bug 元数据: `{metadata_path}`")
        if previous_summary_path and previous_summary_path.exists():
            file_list.append(f"- 上一轮总结: `{previous_summary_path}`")
        context_items = self._bug_summary_context_items(metadata_path, include_html_reports=False)
        for item in context_items:
            path = Path(str(item["path"]))
            title = str(item["title"])
            if path.exists():
                file_list.append(f"- {title}: `{path}`")

        if file_list:
            prompt += "\n".join(file_list) + "\n\n"
            prompt += (
                "请读取上述材料后整理回答。普通报告用 read_file；日志文件不要逐页扫描，"
                "先用 search_large_log(pattern, path=...) 或 bash(\"rg -n ...\") 搜索定位。\n\n"
            )
        else:
            prompt += "（无可用材料文件）\n"

        decoded_logs = self._bug_summary_decoded_log_paths(context_items)
        if decoded_logs:
            prompt += "### 可搜索解码日志\n"
            prompt += "\n".join(f"- `{path}`" for path in decoded_logs) + "\n\n"

        prompt += (
            "### 大日志搜索规则\n"
            "- 对 `*.alog.log`、`*.xlog.log` 或大于 1MB 的日志，先调用 `search_large_log(pattern, path=...)` 或 `bash(\"rg -n ...\")`。\n"
            "- 禁止用连续 `read_file` 分页扫描大日志；只有定位到行号后才用 `read_file(path, start_line, end_line)` 精读。\n"
            "- 普通 JSON/Markdown 报告可直接用 `read_file`。\n\n"
            "### 推荐启动排查关键词\n"
            "- `kill self for unity start dead`\n"
            "- `startCheck timeout`\n"
            "- `onUnityStartDeadTraceDump`\n"
            "- `onUnityHeartbeatSignal`\n"
            "- `X3DCB-DROP`\n"
            "- `没有注册回调函数`\n"
        )

        return prompt

    def _run_bug_agent_summary_via_api(
        self,
        *,
        request_text: str,
        request_artifact: Path,
        metadata_path: Path,
        output_path: Path,
        followup_text: str = "",
        previous_summary_path: Path | None = None,
        progress_callback: Callable[[dict[str, object]], None] | None,
        snapshot_details: dict[str, object] | None = None,
        snapshot_plans: list["BugAnalysisPlan"] | None = None,
    ) -> dict[str, object]:
        """Run bug summary via direct LLM API (fast path, no subprocess)."""
        from .llm_client import LLMClient, LLMClientError

        ai_opts = self.config.ai_provider
        provider_tag = "direct_api"
        started = time.monotonic()
        client = LLMClient(ai_opts)
        if not client.is_available():
            return {
                "message": "",
                "command": None,
                "error": "direct_api_not_configured",
                "provider": provider_tag,
                "session_id": "",
                "resumed": False,
                "duration_seconds": time.monotonic() - started,
                "usage": {},
                "usage_scope": "",
            }
        prompt = self._build_bug_agent_summary_prompt_for_api(
            request_text=request_text,
            request_artifact=request_artifact,
            metadata_path=metadata_path,
            followup_text=followup_text,
            previous_summary_path=previous_summary_path,
            snapshot_details=snapshot_details,
            snapshot_plans=snapshot_plans,
        )
        if not prompt.strip():
            return {
                "message": "",
                "command": None,
                "error": "direct_api_prompt_empty",
                "provider": provider_tag,
                "session_id": "",
                "resumed": False,
                "duration_seconds": time.monotonic() - started,
                "usage": {},
                "usage_scope": "",
            }
        embedded_files = self._direct_api_bug_summary_embedded_files(
            request_artifact=request_artifact,
            metadata_path=metadata_path,
            followup_text=followup_text,
            previous_summary_path=previous_summary_path,
        )
        prompt_file, context_file = self._write_bug_agent_summary_audit(
            {
                "provider": provider_tag,
                "session_id": "",
                "resumed": False,
                "prompt": prompt,
                "embedded_files": embedded_files,
            },
            output_path,
        )
        self._emit_progress(
            progress_callback,
            stage="bug_agent_summary_direct_api",
            message="直接调用 API 整理最终结论（快速通道）",
            provider=provider_tag,
            model=ai_opts.primary_model,
        )
        system_prompt = (
            "你是一个通过飞书触发的 bug 分析总结 agent。"
            "只读分析，不修改文件，不执行写入命令。"
            "必须完整响应用户原始请求中的所有诉求，输出中文 Markdown，结论先行。"
            "所有分析数据已内嵌在用户消息中，直接基于这些数据分析即可。"
        )
        try:
            response = client.generate_summary(
                system_prompt=system_prompt,
                user_prompt=prompt,
            )
        except LLMClientError as exc:
            logger.warning("Direct API bug summary failed: %s", exc)
            return {
                "message": "",
                "command": None,
                "error": f"direct_api_error: {exc}",
                "provider": provider_tag,
                "session_id": "",
                "resumed": False,
                "duration_seconds": time.monotonic() - started,
                "usage": {},
                "usage_scope": "",
                "prompt_file": str(prompt_file) if prompt_file is not None else "",
                "context_file": str(context_file) if context_file is not None else "",
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("Direct API bug summary unexpected error: %s", exc)
            return {
                "message": "",
                "command": None,
                "error": f"direct_api_unexpected: {exc}",
                "provider": provider_tag,
                "session_id": "",
                "resumed": False,
                "duration_seconds": time.monotonic() - started,
                "usage": {},
                "usage_scope": "",
                "prompt_file": str(prompt_file) if prompt_file is not None else "",
                "context_file": str(context_file) if context_file is not None else "",
            }
        message = (response.content or "").strip()
        if not message:
            return {
                "message": "",
                "command": None,
                "error": "direct_api_empty_response",
                "provider": provider_tag,
                "session_id": "",
                "resumed": False,
                "duration_seconds": time.monotonic() - started,
                "usage": {},
                "usage_scope": "",
                "prompt_file": str(prompt_file) if prompt_file is not None else "",
                "context_file": str(context_file) if context_file is not None else "",
            }
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(message, encoding="utf-8")
        except OSError:
            pass
        duration = time.monotonic() - started
        self._emit_progress(
            progress_callback,
            stage="bug_agent_summary_completed",
            message=f"直接 API 已整理最终结论（{duration:.1f}s）",
            provider=provider_tag,
            model=response.model or ai_opts.primary_model,
            output_path=str(output_path),
        )
        return {
            "message": message,
            "command": None,
            "error": "",
            "provider": provider_tag,
            "model": response.model or ai_opts.primary_model,
            "session_id": "",
            "resumed": False,
            "duration_seconds": duration,
            "usage": response.usage,
            "usage_scope": "direct_api",
            "prompt_file": str(prompt_file) if prompt_file is not None else "",
            "context_file": str(context_file) if context_file is not None else "",
        }

    def _build_bug_agent_summary_prompt_for_api(
        self,
        *,
        request_text: str,
        request_artifact: Path,
        metadata_path: Path,
        followup_text: str = "",
        previous_summary_path: Path | None = None,
        snapshot_details: dict[str, object] | None = None,
        snapshot_plans: list["BugAnalysisPlan"] | None = None,
    ) -> str:
        """Build summary prompt with inlined file contents for direct API calls.

        Unlike the subprocess path where codex/claude can read files via tools,
        the direct API path must embed all relevant context inline.
        """
        max_file_chars = 12000
        compaction_profile = self._direct_api_compaction_profile(
            metadata_path=metadata_path,
            snapshot_details=snapshot_details,
        )
        prompt = "请基于以下已内嵌的分析材料完成同一个 bug 会话的最终回答。\n"
        prompt += "注意：所有相关文件内容已内嵌在本消息中，无需读取本地文件。\n\n要求：\n"
        snapshot_prefix = ""
        if followup_text.strip():
            prompt += (
                "1. 这是一条续聊/追问，必须直接回答这次新问题，并延续上一轮分析。\n"
                "2. 优先复用已内嵌的元数据和报告材料，不要要求用户重新上传日志。\n"
                "3. 只读分析，不修改任何文件。\n"
                "4. 输出中文 Markdown，结论先行，再给出证据。\n"
                "5. 如果现有材料仍不足以覆盖某个诉求，要明确指出缺口，但先回答已经能确认的部分。\n"
                "6. 对启动/生命周期类报告，若结构化 JSON 显示 `status != complete` 或仍有 `missing_critical`，即使 HTML/verdict 文案更乐观，也按“链路未闭环”处理，并明确指出报告内部冲突。\n"
                "7. “未命中关键节点”不等于“日志在这里截止”；除非材料明确显示文件结束或时间窗截断，否则不要把缺节点改写成日志截止。\n\n"
            )
            snapshot_prefix = self._build_bug_prompt_snapshot_prefix(
                request_text=request_text,
                metadata_path=metadata_path,
                followup_text=followup_text,
                snapshot_details=snapshot_details,
                plans_override=snapshot_plans,
            )
        else:
            prompt += (
                "1. 本次是全新 bug 分析请求，不是续聊/修正；不要虚构\u201c上一轮分析\u201d\u201c本次修正\u201d\u201c延续上一轮\u201d这类诉求或标题。\n"
                "2. 必须完整覆盖用户原始请求里的所有诉求，不要只回答其中一部分。\n"
                "3. 只读分析，不修改任何文件。\n"
                "4. 输出中文 Markdown，结论先行；若有多个诉求，按诉求分组说明结论和证据；若只有一个诉求，只在开头说明一次，"
                "不要在每条结论或证据前重复写相同的诉求。诉求标题只能来自用户原始请求，不要自行添加不存在的诉求。\n"
                "5. 如果材料无法覆盖用户某个诉求，要明确指出缺口。\n"
                "6. 已内嵌元数据中的\u201c本轮脚本初步摘要\u201d只是当前自动脚本输出，不要把它写成\u201c上一轮结论\u201d；"
                "只有显式提供 followup/previous summary 时，才能讨论修正上一轮结论。\n"
                "7. 如果元数据或报告里已经明确给出故障时间对应的主会话 / 主 PID / focus session，"
                "请优先围绕该主会话分析，不要展开无关会话；只有在需要证明时间不匹配时才提及其他会话。\n\n"
            )
        prompt += (
            "统一输出结构：请按以下中文二级标题组织最终回答，并只填入本次 bug 自身的证据，不套用示例业务词。\n"
            "## 结论摘要\n"
            "- 先给 3 到 5 条最重要结论，必须标明置信边界。\n"
            "## 关键证据\n"
            "- 每条证据尽量带文件、行号、时间、进程/package 或源码位置。\n"
            "## 最可能原因\n"
            "- 按可能性排序，说明支持证据和缺口；证据不足时明确不要强行定根因。\n"
            "## 待确认项\n"
            "- 只列真实证据缺口，例如精确时间、日志片段、运行状态、源码链路缺口。\n"
            "## 建议动作\n"
            "- 给出下一轮可执行动作，例如补日志、重跑某个 skill、沿某个源码或日志点继续查。\n\n"
        )
        if snapshot_prefix:
            prompt += snapshot_prefix
        prompt += f"### 用户原始请求\n{request_text}\n\n"
        if followup_text.strip():
            prompt += f"### 本次追问/修正\n{followup_text.strip()}\n\n"
        if previous_summary_path is not None and (
            not followup_text.strip()
            or self._should_include_previous_summary_for_followup(
                followup_text=followup_text,
                request_artifact=request_artifact,
                metadata_path=metadata_path,
            )
        ):
            prev_text = self._read_text_excerpt(previous_summary_path, max_file_chars)
            if prev_text:
                prompt += f"### 上一轮 Agent 总结\n{prev_text}\n\n"
        request_text_content = self._read_text_excerpt(request_artifact, max_file_chars)
        if request_text_content:
            prompt += f"### Bug Agent Request 文件内容\n{request_text_content}\n\n"
        metadata_text = self._read_text_excerpt(metadata_path, max_file_chars)
        if metadata_text:
            prompt += f"### Bug Metadata 文件内容\n{metadata_text}\n\n"
        for item in self._bug_summary_context_items(metadata_path, include_html_reports=False):
            path = Path(str(item["path"]))
            title = str(item["title"])
            guardrails = self._render_structured_report_guardrails(path)
            if guardrails:
                prompt += f"{guardrails}\n\n"
            content = self._direct_api_bug_summary_context_excerpt(
                title=title,
                path=path,
                max_chars=max_file_chars,
                compaction_profile=compaction_profile,
            )
            if content:
                prompt += f"### {title}\n来源: {path}\n{content}\n\n"
        return prompt

    def _run_bug_agent_summary_once(
        self,
        *,
        invocation: dict[str, object],
        output_path: Path,
        progress_callback: Callable[[dict[str, object]], None] | None,
        timeout: int,
        bridge_session_id: str = "",
    ) -> dict[str, object]:
        command = list(invocation["command"])
        provider = str(invocation["provider"] or "")
        model = str(invocation.get("model") or "")
        session_id = str(invocation.get("session_id") or "")
        resumed = bool(invocation.get("resumed"))
        started = time.monotonic()
        previous_output_mtime = self._path_mtime(output_path)
        prompt_file, context_file = self._write_bug_agent_summary_audit(invocation, output_path)
        debug_log_path = self._subprocess_debug_log_path(
            output_path.parent,
            output_path.with_suffix("").name,
            bridge_session_id=bridge_session_id,
        )
        self._emit_progress(
            progress_callback,
            stage="bug_agent_summary",
            message=(
                "深度分析中：调用本地 Agent 继续整理最终结论"
                if resumed and provider == "codex"
                else "深度分析中：调用本地 Agent 整理最终结论"
                if provider == "codex"
                else "调用本地 Agent 继续整理最终结论"
                if resumed
                else "调用本地 Agent 整理最终结论"
            ),
            output_path=str(output_path),
            provider=provider,
            resumed=resumed,
            provider_session_id=session_id,
            timeout_seconds=timeout,
        )
        try:
            if provider == "codex" and progress_callback is not None:
                completed = self._run_bug_agent_summary_streaming_process(
                    command=command,
                    provider=provider,
                    progress_callback=progress_callback,
                    timeout=timeout,
                    bridge_session_id=bridge_session_id,
                    debug_log_path=debug_log_path,
                )
            else:
                completed = _run_tracked_process(
                    command,
                    watchdog=self.process_watchdog,
                    name=f"bug-agent-summary-{provider or 'agent'}",
                    cwd=self._working_dir(),
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                    session_id=bridge_session_id,
                    debug_log_path=debug_log_path,
                )
        except subprocess.TimeoutExpired as exc:
            fresh_message = self._read_fresh_agent_summary_message(output_path, previous_mtime=previous_output_mtime)
            if fresh_message:
                self._emit_progress(
                    progress_callback,
                    stage="bug_agent_summary_completed",
                    message=f"本地 Agent 静默超过 {timeout} 秒但已写出总结，直接采用已生成结果",
                    provider=provider,
                    output_path=str(output_path),
                    resumed=resumed,
                    provider_session_id=session_id,
                )
                usage, usage_scope = self._extract_bug_agent_usage(
                    provider,
                    _coerce_process_text(getattr(exc, "output", None)),
                    _coerce_process_text(getattr(exc, "stderr", None)),
                )
                return {
                    "message": fresh_message,
                    "command": command,
                    "error": "",
                    "provider": provider,
                    "model": model,
                    "session_id": session_id,
                    "resumed": resumed,
                    "duration_seconds": time.monotonic() - started,
                    "usage": usage,
                    "usage_scope": usage_scope,
                    "prompt_file": str(prompt_file) if prompt_file is not None else "",
                    "context_file": str(context_file) if context_file is not None else "",
                    "timeout_seconds": timeout,
                    "timed_out": True,
                }
            self._emit_progress(
                progress_callback,
                stage="bug_agent_summary_timeout",
                message=f"本地 Agent 静默超过 {timeout} 秒，准备切换轻量总结",
                provider=provider,
                resumed=resumed,
                provider_session_id=session_id,
            )
            return {
                "message": "",
                "command": command,
                "error": "agent_summary_timeout",
                "provider": provider,
                "model": model,
                "session_id": session_id,
                "resumed": resumed,
                "duration_seconds": time.monotonic() - started,
                "usage": {},
                "usage_scope": "",
                "prompt_file": str(prompt_file) if prompt_file is not None else "",
                "context_file": str(context_file) if context_file is not None else "",
                "timeout_seconds": timeout,
                "timed_out": True,
                "stderr": str(exc),
            }
        except OSError as exc:
            self._emit_progress(
                progress_callback,
                stage="bug_agent_summary_failed",
                message=f"本地 Agent 总结失败，回退到脚本摘要: {exc}",
                provider=provider,
                resumed=resumed,
                provider_session_id=session_id,
            )
            return {
                "message": "",
                "command": command,
                "error": str(exc),
                "provider": provider,
                "model": model,
                "session_id": session_id,
                "resumed": resumed,
                "duration_seconds": time.monotonic() - started,
                "usage": {},
                "usage_scope": "",
                "prompt_file": str(prompt_file) if prompt_file is not None else "",
                "context_file": str(context_file) if context_file is not None else "",
                "timeout_seconds": timeout,
            }
        if completed.returncode != 0:
            error = completed.stderr.strip() or completed.stdout.strip() or f"returncode={completed.returncode}"
            self._emit_progress(
                progress_callback,
                stage="bug_agent_summary_failed",
                message=f"本地 Agent 总结失败，回退到脚本摘要: {error}",
                provider=provider,
                resumed=resumed,
                provider_session_id=session_id,
            )
            return {
                "message": "",
                "command": command,
                "error": error,
                "provider": provider,
                "model": model,
                "session_id": session_id,
                "resumed": resumed,
                "duration_seconds": time.monotonic() - started,
                "usage": {},
                "usage_scope": "",
                "prompt_file": str(prompt_file) if prompt_file is not None else "",
                "context_file": str(context_file) if context_file is not None else "",
            }
        if output_path.exists():
            message = output_path.read_text(encoding="utf-8").strip()
        else:
            message = completed.stdout.strip()
            if message:
                try:
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    output_path.write_text(message, encoding="utf-8")
                except OSError:
                    return {
                        "message": "",
                        "command": command,
                        "error": "agent_summary_output_io_error",
                        "provider": provider,
                        "model": model,
                        "session_id": session_id,
                        "resumed": resumed,
                        "duration_seconds": time.monotonic() - started,
                        "usage": {},
                        "usage_scope": "",
                        "prompt_file": str(prompt_file) if prompt_file is not None else "",
                        "context_file": str(context_file) if context_file is not None else "",
                    }
        if not message:
            return {
                "message": "",
                "command": command,
                "error": "empty_agent_summary",
                "provider": provider,
                "model": model,
                "session_id": session_id,
                "resumed": resumed,
                "duration_seconds": time.monotonic() - started,
                "usage": {},
                "usage_scope": "",
                "prompt_file": str(prompt_file) if prompt_file is not None else "",
                "context_file": str(context_file) if context_file is not None else "",
            }
        resolved_session_id = self._extract_bug_agent_session_id(provider, completed.stdout, fallback=session_id)
        usage, usage_scope = self._extract_bug_agent_usage(provider, completed.stdout, completed.stderr)
        self._emit_progress(
            progress_callback,
            stage="bug_agent_summary_completed",
            message="本地 Agent 已整理最终结论",
            provider=provider,
            output_path=str(output_path),
            resumed=resumed,
            provider_session_id=resolved_session_id,
        )
        return {
            "message": message,
            "command": command,
            "error": "",
            "provider": provider,
            "model": model,
            "session_id": resolved_session_id,
            "resumed": resumed,
            "duration_seconds": time.monotonic() - started,
            "usage": usage,
            "usage_scope": usage_scope,
            "prompt_file": str(prompt_file) if prompt_file is not None else "",
            "context_file": str(context_file) if context_file is not None else "",
        }

    def _run_bug_agent_summary_streaming_process(
        self,
        *,
        command: list[str],
        provider: str,
        progress_callback: Callable[[dict[str, object]], None],
        timeout: int,
        bridge_session_id: str = "",
        debug_log_path: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        kwargs: dict[str, object] = {
            "cwd": self._working_dir(),
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
        }
        if sys.platform != "win32":
            kwargs["start_new_session"] = True
        process = subprocess.Popen(command, **kwargs)
        if self.process_watchdog is not None:
            self.process_watchdog.track(
                process.pid,
                f"bug-agent-summary-{provider or 'agent'}",
                max_idle_seconds=timeout,
                session_id=bridge_session_id,
            )
        started = time.monotonic()
        last_activity = started
        last_heartbeat = started
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        stdout_partial = b""
        stderr_partial = b""

        def _coerce_chunk(chunk: bytes | str | None) -> bytes:
            if chunk is None:
                return b""
            if isinstance(chunk, bytes):
                return chunk
            return chunk.encode("utf-8", errors="replace")

        def _append_chunk(
            chunk: bytes | str | None,
            *,
            partial: bytes,
            sink: list[str],
        ) -> bytes:
            data = partial + _coerce_chunk(chunk)
            while True:
                newline = data.find(b"\n")
                if newline < 0:
                    break
                sink.append(data[: newline + 1].decode("utf-8", errors="replace"))
                data = data[newline + 1 :]
            return data

        def _flush_partial(partial: bytes, sink: list[str]) -> bytes:
            if partial:
                sink.append(partial.decode("utf-8", errors="replace"))
            return b""

        try:
            if process.stdout is None or process.stderr is None:
                raise RuntimeError("subprocess streams not available (stdout/stderr must be PIPE)")
            streams = [process.stdout, process.stderr]
            while True:
                now = time.monotonic()
                if timeout and now - last_activity > timeout:
                    _safe_terminate(process.pid)
                    try:
                        stdout, stderr = process.communicate(timeout=2)
                    except subprocess.TimeoutExpired:
                        _safe_terminate(process.pid, sig=signal.SIGKILL)
                        stdout, stderr = process.communicate()
                    if stdout:
                        stdout_partial = _append_chunk(stdout, partial=stdout_partial, sink=stdout_parts)
                    if stderr:
                        stderr_partial = _append_chunk(stderr, partial=stderr_partial, sink=stderr_parts)
                    stdout_partial = _flush_partial(stdout_partial, stdout_parts)
                    stderr_partial = _flush_partial(stderr_partial, stderr_parts)
                    _write_subprocess_debug_log(
                        debug_log_path,
                        name=f"bug-agent-summary-{provider or 'agent'}",
                        command=command,
                        cwd=self._working_dir(),
                        timeout=timeout,
                        returncode=process.returncode,
                        stdout="".join(stdout_parts),
                        stderr="".join(stderr_parts),
                        error="TimeoutExpired",
                    )
                    raise subprocess.TimeoutExpired(
                        command,
                        timeout,
                        output="".join(stdout_parts),
                        stderr="".join(stderr_parts),
                    )

                if streams:
                    readable, _, _ = select.select(streams, [], [], 1.0)
                else:
                    if process.poll() is not None:
                        break
                    time.sleep(1.0)
                    readable = []
                if readable:
                    for stream in readable:
                        reader = getattr(stream, "read1", None)
                        if callable(reader):
                            chunk = reader(4096)
                        else:
                            chunk = stream.read(4096)
                        if chunk:
                            if stream is process.stdout:
                                before = len(stdout_parts)
                                stdout_partial = _append_chunk(chunk, partial=stdout_partial, sink=stdout_parts)
                                for line in stdout_parts[before:]:
                                    self._emit_agent_summary_stream_progress(
                                        progress_callback,
                                        provider=provider,
                                        line=line,
                                        elapsed_seconds=time.monotonic() - started,
                                    )
                            else:
                                stderr_partial = _append_chunk(chunk, partial=stderr_partial, sink=stderr_parts)
                            last_activity = time.monotonic()
                            if self.process_watchdog is not None:
                                self.process_watchdog.record_activity(process.pid)
                        elif stream in streams:
                            if stream is process.stdout:
                                stdout_partial = _flush_partial(stdout_partial, stdout_parts)
                            else:
                                stderr_partial = _flush_partial(stderr_partial, stderr_parts)
                            streams.remove(stream)
                elif process.poll() is not None:
                    break
                if process.poll() is not None and not streams:
                    break

                now = time.monotonic()
                if now - last_heartbeat >= 15:
                    last_heartbeat = now
                    idle_seconds = int(now - last_activity)
                    self._emit_progress(
                        progress_callback,
                        stage="bug_agent_summary_stream",
                        message=f"深度分析中，已运行 {int(now - started)} 秒，距离上次 Agent 输出 {idle_seconds} 秒",
                        provider=provider,
                        stream_preview=f"等待 Agent 输出... idle={idle_seconds}s total={int(now - started)}s",
                    )

            remaining_stdout, stderr = process.communicate(timeout=2)
            if remaining_stdout:
                stdout_partial = _append_chunk(remaining_stdout, partial=stdout_partial, sink=stdout_parts)
            if stderr:
                stderr_partial = _append_chunk(stderr, partial=stderr_partial, sink=stderr_parts)
            stdout_partial = _flush_partial(stdout_partial, stdout_parts)
            stderr_partial = _flush_partial(stderr_partial, stderr_parts)
            stdout_text = "".join(stdout_parts)
            stderr_text = "".join(stderr_parts)
            _write_subprocess_debug_log(
                debug_log_path,
                name=f"bug-agent-summary-{provider or 'agent'}",
                command=command,
                cwd=self._working_dir(),
                timeout=timeout,
                returncode=process.returncode,
                stdout=stdout_text,
                stderr=stderr_text,
            )
            return subprocess.CompletedProcess(command, process.returncode, stdout_text, stderr_text)
        finally:
            if self.process_watchdog is not None:
                self.process_watchdog.untrack(process.pid)

    def _emit_agent_summary_stream_progress(
        self,
        progress_callback: Callable[[dict[str, object]], None],
        *,
        provider: str,
        line: str,
        elapsed_seconds: float,
    ) -> None:
        preview = self._codex_stream_preview(line)
        if not preview:
            return
        self._emit_progress(
            progress_callback,
            stage="bug_agent_summary_stream",
            message=f"深度分析输出更新：{preview}",
            provider=provider,
            elapsed_seconds=round(elapsed_seconds, 1),
            stream_preview=preview,
        )

    def _codex_stream_preview(self, line: str) -> str:
        raw = line.strip()
        if not raw:
            return ""
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            return raw[:240]
        event_type = str(event.get("type") or "").strip()
        if event_type == "thread.started":
            thread_id = str(event.get("thread_id") or "").strip()
            return f"Codex 会话已创建 {thread_id}" if thread_id else "Codex 会话已创建"
        if event_type == "turn.started":
            return "Codex 已开始深度分析"
        if event_type == "turn.completed":
            usage = event.get("usage")
            if isinstance(usage, dict):
                total = usage.get("total_tokens")
                if total is None:
                    total = sum(value for value in usage.values() if isinstance(value, int))
                return f"Codex 深度分析完成，token≈{total}" if total else "Codex 深度分析完成"
            return "Codex 深度分析完成"
        if event_type in {"item.started", "item.updated", "item.completed"}:
            item = event.get("item")
            if isinstance(item, dict):
                preview = self._codex_item_preview(item, event_type=event_type)
                if preview:
                    return preview
            return "" if event_type == "item.completed" else "Codex 工具调用更新"
        return f"{event_type}: {raw[:220]}" if event_type else raw[:240]

    def _codex_item_preview(self, item: dict[str, object], *, event_type: str) -> str:
        item_type = str(item.get("type") or "").strip()
        if item_type == "command_execution":
            command = str(item.get("command") or "").strip()
            if event_type == "item.completed":
                return ""
            if command:
                return f"工具调用：执行命令 {self._compact_tool_preview(self._unwrap_shell_command(command), 200)}"
            return "工具调用：执行命令"
        if item_type in {"error", "error_message"}:
            message = str(item.get("message") or item.get("text") or item.get("error") or "").strip()
            message = self._compact_tool_preview(message, 180)
            return f"Agent 错误：{message}" if message else ""
        if item_type in {"tool_call", "function_call"}:
            name = str(
                item.get("name")
                or item.get("tool_name")
                or item.get("function_name")
                or item.get("recipient_name")
                or ""
            ).strip()
            args = item.get("arguments")
            if args is None:
                args = item.get("input")
            if args is None:
                args = item.get("parameters")
            args_text = self._compact_tool_preview(args, 120)
            label = "工具调用" if event_type == "item.started" else "工具调用更新"
            if name and args_text:
                return f"{label}：{name} {args_text}"
            if name:
                return f"{label}：{name}"
            return label
        text = str(item.get("text") or item.get("name") or "").strip()
        text = re.sub(r"\s+", " ", text)
        if text:
            return f"{item_type or 'item'}: {text[:240]}"
        if event_type == "item.completed":
            return ""
        return f"开始处理 {item_type}" if item_type else ""

    def _unwrap_shell_command(self, command: str) -> str:
        text = " ".join(command.replace("\\n", " ").split())
        try:
            parts = shlex.split(text)
        except ValueError:
            parts = []
        if len(parts) >= 3 and parts[0] in {"/bin/zsh", "/bin/bash", "zsh", "bash"} and parts[1] == "-lc":
            return " ".join(parts[2:]).strip()
        return text

    def _compact_tool_preview(self, value: object, max_chars: int) -> str:
        if isinstance(value, (dict, list)):
            text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        else:
            text = str(value or "")
        text = " ".join(text.replace("\\n", " ").split())
        if len(text) > max_chars:
            return text[: max_chars - 1].rstrip() + "…"
        return text

    def _write_bug_agent_summary_audit(
        self,
        invocation: dict[str, object],
        output_path: Path,
    ) -> tuple[Path | None, Path | None]:
        prompt = str(invocation.get("prompt") or "")
        embedded_files = invocation.get("embedded_files")
        if not isinstance(embedded_files, list):
            embedded_files = []
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            prompt_file = output_path.parent / "bug_agent_summary_prompt.md"
            context_file = output_path.parent / "bug_agent_summary_context.json"
            prompt_file.write_text(prompt, encoding="utf-8")
            manifest = {
                "provider": str(invocation.get("provider") or ""),
                "resumed": bool(invocation.get("resumed")),
                "session_id": str(invocation.get("session_id") or ""),
                "output_path": str(output_path),
                "prompt_file": str(prompt_file),
                "prompt_chars": len(prompt),
                "embedded_files": self._embedded_file_manifest(embedded_files),
            }
            context_file.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            return prompt_file, context_file
        except OSError:
            return None, None

    def _extract_bug_agent_session_id(self, provider: str, output: str, *, fallback: str = "") -> str:
        if fallback.strip():
            return fallback.strip()
        if provider != "codex":
            return ""
        for line in output.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            found = self._search_bug_agent_session_id_in_payload(payload)
            if found:
                return found
        return ""

    def _search_bug_agent_session_id_in_payload(self, payload: object) -> str:
        if isinstance(payload, dict):
            for key in ("session_id", "sessionId", "conversation_id", "conversationId", "thread_id", "threadId"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            for key in ("session", "conversation", "thread"):
                nested = payload.get(key)
                if isinstance(nested, dict):
                    value = nested.get("id")
                    if isinstance(value, str) and value.strip():
                        return value.strip()
            for value in payload.values():
                found = self._search_bug_agent_session_id_in_payload(value)
                if found:
                    return found
            return ""
        if isinstance(payload, list):
            for item in payload:
                found = self._search_bug_agent_session_id_in_payload(item)
                if found:
                    return found
        return ""

    def _extract_bug_agent_usage(self, provider: str, stdout: str, stderr: str) -> tuple[dict[str, int], str]:
        usage, scope = self._extract_usage_from_json_lines(stdout)
        if usage:
            return usage, scope
        if provider == "codex":
            usage, scope = self._extract_usage_from_json_lines(stderr)
            if usage:
                return usage, scope
        return self._extract_usage_from_text("\n".join(part for part in (stdout, stderr) if part)), "cumulative"

    def _extract_usage_from_json_lines(self, text: str) -> tuple[dict[str, int], str]:
        latest: dict[str, int] = {}
        latest_delta: dict[str, int] = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            for candidate in self._iter_usage_objects_from_keys(
                payload,
                keys=("delta_usage", "deltaUsage", "usage_delta", "usageDelta"),
            ):
                parsed = self._parse_usage_object(candidate)
                if parsed:
                    latest_delta = parsed
            for candidate in self._iter_usage_objects(payload):
                parsed = self._parse_usage_object(candidate)
                if parsed:
                    latest = parsed
        if latest_delta:
            return latest_delta, "delta"
        return latest, "cumulative" if latest else ""

    def _iter_usage_objects_from_keys(self, value: object, *, keys: tuple[str, ...]) -> list[dict[str, object]]:
        found: list[dict[str, object]] = []
        if isinstance(value, dict):
            for key in keys:
                nested = value.get(key)
                if isinstance(nested, dict):
                    found.append(nested)
            for nested in value.values():
                found.extend(self._iter_usage_objects_from_keys(nested, keys=keys))
        elif isinstance(value, list):
            for item in value:
                found.extend(self._iter_usage_objects_from_keys(item, keys=keys))
        return found

    def _iter_usage_objects(self, value: object) -> list[dict[str, object]]:
        found: list[dict[str, object]] = []
        if isinstance(value, dict):
            if any(any(alias in value for alias in aliases) for aliases in _TOKEN_USAGE_KEYS.values()):
                found.append(value)
            for nested in value.values():
                found.extend(self._iter_usage_objects(nested))
        elif isinstance(value, list):
            for item in value:
                found.extend(self._iter_usage_objects(item))
        return found

    def _parse_usage_object(self, value: object) -> dict[str, int]:
        if not isinstance(value, dict):
            return {}
        return normalize_token_usage(value)

    def _extract_usage_from_text(self, text: str) -> dict[str, int]:
        patterns = {
            "input_tokens": (r"input[_ ]tokens?\s*[:=]\s*(\d+)", r"prompt[_ ]tokens?\s*[:=]\s*(\d+)"),
            "cached_input_tokens": (
                r"cached[_ ]input[_ ]tokens?\s*[:=]\s*(\d+)",
                r"cached[_ ]prompt[_ ]tokens?\s*[:=]\s*(\d+)",
            ),
            "output_tokens": (r"output[_ ]tokens?\s*[:=]\s*(\d+)", r"completion[_ ]tokens?\s*[:=]\s*(\d+)"),
            "total_tokens": (r"total[_ ]tokens?\s*[:=]\s*(\d+)",),
        }
        usage: dict[str, int] = {}
        for target_key, candidates in patterns.items():
            for pattern in candidates:
                match = re.search(pattern, text, flags=re.IGNORECASE)
                if match:
                    usage[target_key] = int(match.group(1))
                    break
        if "total_tokens" not in usage and {"input_tokens", "output_tokens"}.issubset(usage):
            usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
        return usage

    def _emit_progress(
        self,
        progress_callback: Callable[[dict[str, object]], None] | None,
        *,
        stage: str,
        message: str,
        **details: object,
    ) -> None:
        if progress_callback is None:
            return
        payload: dict[str, object] = {"stage": stage, "message": message}
        if details:
            payload["details"] = details
        progress_callback(payload)

    def _request_text(self, *, raw_text: str, prompt_text: str, bug_url: str) -> str:
        candidate = (raw_text or "").strip()
        if candidate:
            return candidate
        if bug_url:
            if prompt_text:
                return f"{bug_url} {prompt_text}".strip()
            return bug_url
        return prompt_text.strip()

    def _apply_agent_runtime_details(self, details: dict[str, object], agent_summary_result: dict[str, object]) -> None:
        if agent_summary_result["command"]:
            details["agent_summary_command"] = list(agent_summary_result["command"])
        if agent_summary_result["error"]:
            details["agent_summary_error"] = str(agent_summary_result["error"])
        if agent_summary_result["provider"]:
            provider = str(agent_summary_result["provider"])
            details["agent_summary_provider"] = provider
            details.setdefault("provider", provider)
        model = self._agent_summary_model(agent_summary_result)
        if model:
            details["agent_summary_model"] = model
        if agent_summary_result["session_id"]:
            details["agent_summary_session_id"] = str(agent_summary_result["session_id"])
        if agent_summary_result["resumed"]:
            details["agent_summary_resumed"] = True
        if agent_summary_result.get("usage_scope"):
            details["agent_summary_usage_scope"] = str(agent_summary_result["usage_scope"])
        if agent_summary_result.get("execution_backend"):
            details["agent_summary_execution_backend"] = str(agent_summary_result["execution_backend"])
        if agent_summary_result.get("backend_reason"):
            details["agent_summary_backend_reason"] = str(agent_summary_result["backend_reason"])
        if agent_summary_result.get("fallback_from"):
            details["agent_summary_fallback_from"] = str(agent_summary_result["fallback_from"])
        if agent_summary_result.get("prompt_file"):
            details["agent_summary_prompt_file"] = str(agent_summary_result["prompt_file"])
        if agent_summary_result.get("context_file"):
            details["agent_summary_context_file"] = str(agent_summary_result["context_file"])
        if agent_summary_result.get("timeout_seconds"):
            details["agent_summary_timeout_seconds"] = agent_summary_result["timeout_seconds"]
        if agent_summary_result.get("timed_out"):
            details["agent_summary_timed_out"] = True
        tool_calls = agent_summary_result.get("tool_calls")
        if isinstance(tool_calls, int):
            details["agent_summary_tool_calls"] = tool_calls
        tool_trace = agent_summary_result.get("tool_trace")
        if isinstance(tool_trace, list):
            details["agent_summary_tool_trace"] = tool_trace[:20]
        duration = agent_summary_result.get("duration_seconds")
        if isinstance(duration, (int, float)):
            details["agent_summary_duration_seconds"] = float(duration)
        usage = agent_summary_result.get("usage")
        if isinstance(usage, dict):
            normalized_usage = normalize_token_usage(usage)
            for key in ("input_tokens", "cached_input_tokens", "output_tokens", "total_tokens"):
                value = normalized_usage.get(key)
                if isinstance(value, int):
                    details[f"agent_summary_{key}"] = value

    def _append_agent_runtime_metadata(
        self,
        metadata_path: Path,
        *,
        agent_summary_result: dict[str, object],
        total_duration_seconds: float,
    ) -> None:
        provider = str(agent_summary_result.get("provider") or "").strip()
        model = self._agent_summary_model(agent_summary_result)
        usage = agent_summary_result.get("usage")
        duration = agent_summary_result.get("duration_seconds")
        session_id = str(agent_summary_result.get("session_id") or "").strip()
        resumed = bool(agent_summary_result.get("resumed"))
        usage_scope = str(agent_summary_result.get("usage_scope") or "").strip()
        execution_backend = str(agent_summary_result.get("execution_backend") or "").strip()
        backend_reason = str(agent_summary_result.get("backend_reason") or "").strip()
        fallback_from = str(agent_summary_result.get("fallback_from") or "").strip()
        if not provider and not model and not isinstance(usage, dict) and not isinstance(duration, (int, float)):
            return
        lines = ["", "## Agent 执行信息", ""]
        lines.append(f"- Agent 类型: `{provider or '未知'}`")
        if execution_backend:
            lines.append(f"- 执行后端: `{execution_backend}`")
        if backend_reason:
            lines.append(f"- 后端选择原因: `{backend_reason}`")
        if fallback_from:
            lines.append(f"- fallback_from: `{fallback_from}`")
        if model:
            lines.append(f"- Agent 模型: `{model}`")
        if session_id:
            lines.append(f"- Agent 会话ID: `{session_id}`")
        lines.append(f"- 续会话: `{'是' if resumed else '否'}`")
        if isinstance(usage, dict):
            normalized_usage = normalize_token_usage(usage)
            input_tokens = normalized_usage.get("input_tokens")
            cached_input_tokens = normalized_usage.get("cached_input_tokens")
            output_tokens = normalized_usage.get("output_tokens")
            total_tokens = normalized_usage.get("total_tokens")
            if any(
                isinstance(value, int)
                for value in (input_tokens, cached_input_tokens, output_tokens, total_tokens)
            ):
                token_label = "本轮 Agent Token" if usage_scope == "delta" else "累计 Agent Token"
                parts = [self._format_token_millions(input_tokens)]
                if isinstance(cached_input_tokens, int):
                    parts.append(self._format_token_millions(cached_input_tokens))
                parts.extend(
                    [
                        self._format_token_millions(output_tokens),
                        self._format_token_millions(total_tokens),
                    ]
                )
                lines.append(
                    f"- {token_label}: `{' / '.join(parts)}`"
                )
        if isinstance(duration, (int, float)):
            lines.append(f"- Agent 耗时: `{float(duration):.1f} 秒`")
        tool_calls = agent_summary_result.get("tool_calls")
        if isinstance(tool_calls, int):
            lines.append(f"- Agent 工具调用数: `{tool_calls}`")
        tool_trace = agent_summary_result.get("tool_trace")
        if isinstance(tool_trace, list) and tool_trace:
            trace_items: list[str] = []
            for item in tool_trace[:5]:
                if not isinstance(item, dict):
                    continue
                tool_name = str(item.get("tool") or "?")
                args = str(item.get("args") or "").replace("\n", " ")[:120]
                trace_items.append(f"{tool_name}({args})")
            if trace_items:
                lines.append(f"- Agent 工具轨迹: `{'; '.join(trace_items)}`")
        if agent_summary_result.get("timeout_seconds"):
            lines.append(f"- Agent 总结超时: `{agent_summary_result['timeout_seconds']} 秒`")
        if agent_summary_result.get("timed_out") and agent_summary_result.get("message"):
            lines.append("- 超时处理: `已采用 Agent 写出的总结文件，未跨 provider fallback`")
        if agent_summary_result.get("prompt_file"):
            lines.append(f"- Agent Prompt: `{agent_summary_result['prompt_file']}`")
        if agent_summary_result.get("context_file"):
            lines.append(f"- Agent 输入清单: `{agent_summary_result['context_file']}`")
        lines.append(f"- 总耗时: `{total_duration_seconds:.1f} 秒`")
        try:
            original = metadata_path.read_text(encoding="utf-8")
        except OSError:
            original = ""
        metadata_path.write_text(original.rstrip() + "\n" + "\n".join(lines).rstrip() + "\n", encoding="utf-8")

    def _append_source_evidence_metadata(self, metadata_path: Path, source_evidence_path: Path | None) -> None:
        if source_evidence_path is None:
            return
        try:
            evidence = source_evidence_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            evidence = f"读取失败: {exc}"
        try:
            original = metadata_path.read_text(encoding="utf-8")
        except OSError:
            original = ""
        lines = [
            "",
            "## 源码证据",
            "",
            f"- 文件: `{source_evidence_path}`",
            "",
            "```text",
            evidence[:5000],
            "```",
        ]
        metadata_path.write_text(original.rstrip() + "\n" + "\n".join(lines).rstrip() + "\n", encoding="utf-8")

    def _append_evidence_log_metadata(self, metadata_path: Path, evidence_log_bundle: dict[str, object] | None) -> None:
        if not evidence_log_bundle:
            return
        try:
            original = metadata_path.read_text(encoding="utf-8")
        except OSError:
            original = ""
        focus_logs = evidence_log_bundle.get("focus_logs")
        if isinstance(focus_logs, list) and focus_logs:
            focus_text = ", ".join(str(item) for item in focus_logs)
        else:
            focus_text = "未识别"
        lines = [
            "",
            "## 证据日志保留包",
            "",
            f"- 目录: `{evidence_log_bundle.get('bundle_dir') or ''}`",
            f"- 清单: `{evidence_log_bundle.get('manifest_path') or ''}`",
            f"- 命中日志: `{focus_text}`",
            f"- 文件数: `{evidence_log_bundle.get('file_count') or 0}`",
        ]
        metadata_path.write_text(original.rstrip() + "\n" + "\n".join(lines).rstrip() + "\n", encoding="utf-8")

    def _should_collect_source_evidence(self, *texts: str) -> bool:
        source_terms = ("源码", "源代码", "根据源码", "基于源码")
        merged = "\n".join(texts).casefold()
        return any(term.casefold() in merged for term in source_terms) or self._source_analysis_shortcut(*texts)

    def _should_prefer_lightweight_bug_summary(
        self,
        *,
        request_text: str,
        followup_text: str,
        provider_session_id: str,
    ) -> bool:
        if provider_session_id.strip():
            return False
        # Bug summaries must be produced by a file-capable agent. The lightweight
        # local chat model cannot read report/log paths and has previously turned
        # truncated HTML/CSS excerpts into false "信息不足" conclusions.
        return False

    def _annotate_html_reports(
        self,
        html_paths: list[Path],
        *,
        agent_summary_result: dict[str, object],
        total_duration_seconds: float,
    ) -> None:
        snippet = self._build_agent_runtime_html(agent_summary_result, total_duration_seconds=total_duration_seconds)
        if not snippet:
            return
        for path in html_paths:
            if not path.exists() or path.suffix.lower() != ".html":
                continue
            try:
                html_text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            updated = self._inject_runtime_html(html_text, snippet)
            if updated == html_text:
                continue
            try:
                path.write_text(updated, encoding="utf-8")
            except OSError:
                continue

    def _build_agent_runtime_html(self, agent_summary_result: dict[str, object], *, total_duration_seconds: float) -> str:
        provider = str(agent_summary_result.get("provider") or "").strip()
        model = self._agent_summary_model(agent_summary_result)
        usage = agent_summary_result.get("usage")
        duration = agent_summary_result.get("duration_seconds")
        session_id = str(agent_summary_result.get("session_id") or "").strip()
        resumed = bool(agent_summary_result.get("resumed"))
        usage_scope = str(agent_summary_result.get("usage_scope") or "").strip()
        execution_backend = str(agent_summary_result.get("execution_backend") or "").strip()
        backend_reason = str(agent_summary_result.get("backend_reason") or "").strip()
        fallback_from = str(agent_summary_result.get("fallback_from") or "").strip()
        if not provider and not model and not isinstance(usage, dict) and not isinstance(duration, (int, float)):
            return ""
        rows = [
            ("Agent 类型", provider or "未知"),
            ("续会话", "是" if resumed else "否"),
        ]
        if execution_backend:
            rows.append(("执行后端", execution_backend))
        if backend_reason:
            rows.append(("后端选择原因", backend_reason))
        if fallback_from:
            rows.append(("Fallback From", fallback_from))
        if model:
            rows.append(("Agent 模型", model))
        if session_id:
            rows.append(("Agent 会话ID", session_id))
        normalized_usage = normalize_token_usage(usage) if isinstance(usage, dict) else {}
        if normalized_usage:
            parts = [self._format_token_millions(normalized_usage.get("input_tokens"))]
            cached_input_tokens = normalized_usage.get("cached_input_tokens")
            if isinstance(cached_input_tokens, int):
                parts.append(self._format_token_millions(cached_input_tokens))
            parts.extend(
                [
                    self._format_token_millions(normalized_usage.get("output_tokens")),
                    self._format_token_millions(normalized_usage.get("total_tokens")),
                ]
            )
            rows.append(
                (
                    "本轮 Agent Token" if usage_scope == "delta" else "累计 Agent Token",
                    " / ".join(parts),
                )
            )
        if isinstance(duration, (int, float)):
            rows.append(("Agent 耗时", f"{float(duration):.1f} 秒"))
        rows.append(("总耗时", f"{total_duration_seconds:.1f} 秒"))
        items = "".join(
            "<div class=\"lagent-runtime-item\">"
            f"<div class=\"lagent-runtime-label\">{self._escape_html(label)}</div>"
            f"<div class=\"lagent-runtime-value\">{self._escape_html(value)}</div>"
            "</div>"
            for label, value in rows
        )
        style = (
            "<style>"
            ".lagent-runtime{margin:16px 0 20px;padding:16px 18px;border:1px solid #dbe2f0;border-radius:12px;"
            "background:#f8fafc;box-shadow:0 1px 3px rgba(15,23,42,.06)}"
            ".lagent-runtime h2{margin:0 0 12px;font-size:18px;color:#0f172a}"
            ".lagent-runtime-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}"
            ".lagent-runtime-item{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:12px 14px}"
            ".lagent-runtime-label{font-size:12px;color:#64748b;margin-bottom:4px}"
            ".lagent-runtime-value{font-size:16px;font-weight:600;color:#0f172a;word-break:break-word}"
            "</style>"
        )
        return (
            f"{_RUNTIME_HTML_MARKER_START}{style}"
            "<section class=\"lagent-runtime\">"
            "<h2>Agent 运行信息</h2>"
            f"<div class=\"lagent-runtime-grid\">{items}</div>"
            "</section>"
            f"{_RUNTIME_HTML_MARKER_END}"
        )

    def _agent_summary_model(self, agent_summary_result: dict[str, object]) -> str:
        model = str(agent_summary_result.get("model") or "").strip()
        if model:
            return model
        provider = str(agent_summary_result.get("provider") or "").strip().casefold()
        if provider == "direct_api":
            return str(self.config.ai_provider.primary_model or "").strip()
        if provider == "omlx":
            return str(self.config.omlx_chat.model or "").strip()
        if provider == "codex":
            return str(self.config.bug_analysis.model or "").strip()
        if provider in {"claude", "claude-code", "claude_code"}:
            return str(self.config.claude_agent.model or "").strip()
        return ""

    def _format_token_millions(self, value: object) -> str:
        if not isinstance(value, int):
            return "-"
        if value < 1_000:
            return str(value)
        if value < 1_000_000:
            return f"{value / 1_000:.2f}K"
        return f"{value / 1_000_000:.2f}M"

    def _inject_runtime_html(self, html_text: str, runtime_html: str) -> str:
        pattern = re.compile(
            rf"{re.escape(_RUNTIME_HTML_MARKER_START)}.*?{re.escape(_RUNTIME_HTML_MARKER_END)}",
            flags=re.DOTALL,
        )
        if pattern.search(html_text):
            return pattern.sub(runtime_html, html_text, count=1)
        body_match = re.search(r"<body[^>]*>", html_text, flags=re.IGNORECASE)
        if body_match:
            index = body_match.end()
            return html_text[:index] + runtime_html + html_text[index:]
        html_match = re.search(r"<html[^>]*>", html_text, flags=re.IGNORECASE)
        if html_match:
            index = html_match.end()
            return html_text[:index] + "<body>" + runtime_html + "</body>" + html_text[index:]
        return runtime_html + html_text

    def _escape_html(self, value: object) -> str:
        return (
            str(value)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

    def _write_reanalysis_source_evidence(
        self,
        *,
        plans: list["BugAnalysisPlan"],
        request_text: str,
        followup_text: str,
        output_dir: Path,
        enabled: bool,
        extra_texts: tuple[str, ...] = (),
    ) -> Path | None:
        if not enabled:
            return None
        terms = self._source_evidence_terms(
            plans=plans,
            request_text=request_text,
            followup_text=followup_text,
            extra_texts=extra_texts,
        )
        if not terms:
            return None
        evidence_path = output_dir / "bug_source_evidence.md"
        started = time.monotonic()
        budget_seconds = min(
            self._SOURCE_EVIDENCE_TOTAL_BUDGET_SECONDS,
            5.0 * max(1, min(len(terms), 5)),
        )
        deadline = started + budget_seconds

        # Use all configured repo_roots (includes Napa5 when configured), fallback to guideengine_repo
        si_opts = self.config.source_investigation
        repo_roots = [
            p.expanduser()
            for p in (si_opts.repo_roots or [self.config.guideengine_repo])
        ]
        existing_repos = [r for r in repo_roots if r.exists()]

        lines = [
            "# Bug Source Evidence",
            "",
            f"- 源码根目录: `{', '.join(str(r) for r in (existing_repos or repo_roots))}`",
            f"- 检索词: `{', '.join(terms)}`",
            f"- 检索预算: `{budget_seconds:.1f}s`",
            "",
        ]
        if not existing_repos:
            lines.append(f"源码根目录不存在，未执行源码检索: `{', '.join(str(r) for r in repo_roots)}`")
            evidence_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return evidence_path

        all_matches: list[tuple[str, int, str]] = []
        trace_lines: list[str] = []
        for repo in existing_repos:
            if time.monotonic() >= deadline:
                trace_lines.append(f"- `{repo}`: skipped, source evidence budget exhausted")
                break
            repo_started = time.monotonic()
            # Try codegraph first (semantic symbol search)
            cg_matches = self._collect_source_evidence_with_codegraph(repo=repo, terms=terms, deadline=deadline)
            if cg_matches is not None:
                repo_prefix = f"[{repo.name}] " if len(existing_repos) > 1 else ""
                all_matches.extend((repo_prefix + path, ln, text) for path, ln, text in cg_matches)
                trace_lines.append(
                    f"- `{repo}`: codegraph, {len(cg_matches)} matches, {time.monotonic() - repo_started:.2f}s"
                )
                continue
            # Fall back to ripgrep only; avoid pure Python full-repo scans on large trees.
            matches = self._collect_source_evidence(repo=repo, terms=terms, deadline=deadline)
            if len(existing_repos) > 1:
                all_matches.extend((f"[{repo.name}] {path}", ln, text) for path, ln, text in matches)
            else:
                all_matches.extend(matches)
            trace_lines.append(
                f"- `{repo}`: rg fallback, {len(matches)} matches, {time.monotonic() - repo_started:.2f}s"
            )

        if trace_lines:
            elapsed = time.monotonic() - started
            lines.extend(["## 检索路径", "", *trace_lines, f"- 总耗时: `{elapsed:.2f}s`", ""])

        if not all_matches:
            lines.append("未检索到匹配源码。")
        else:
            current_file = ""
            for path, line_no, text in all_matches[:80]:
                if path != current_file:
                    current_file = path
                    lines.extend(["", f"## {path}"])
                lines.append(f"- L{line_no}: `{text}`")
        evidence_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        return evidence_path

    def _collect_source_evidence_with_codegraph(
        self, *, repo: Path, terms: list[str], deadline: float | None = None
    ) -> list[tuple[str, int, str]] | None:
        """Try codegraph semantic search; return None to fall through to ripgrep."""
        si_opts = self.config.source_investigation
        if not si_opts.codegraph_enabled:
            return None
        try:
            from ..knowledge.codegraph_client import CodeGraphClient
        except ImportError:
            return None
        cg = CodeGraphClient(
            command=si_opts.codegraph_command,
            timeout=min(si_opts.codegraph_timeout_seconds, self._SOURCE_EVIDENCE_CODEGRAPH_CALL_SECONDS),
        )
        if not cg.is_available():
            return None
        status_timeout = self._source_evidence_call_timeout(deadline, si_opts.codegraph_timeout_seconds)
        if status_timeout <= 0 or not cg.is_indexed(repo, timeout=status_timeout):
            return None
        matches: list[tuple[str, int, str]] = []
        seen: set[tuple[str, int]] = set()
        for term in terms[:5]:
            call_timeout = self._source_evidence_call_timeout(deadline, si_opts.codegraph_timeout_seconds)
            if call_timeout <= 0:
                break
            hits = cg.search_symbol(term, repo, limit=5, timeout=call_timeout)
            for h in hits:
                key = (h.path, h.line)
                if key in seen:
                    continue
                seen.add(key)
                matches.append((h.path, h.line, f"[{h.kind}] {h.qualified_name or h.name}"))
            call_timeout = self._source_evidence_call_timeout(deadline, si_opts.codegraph_timeout_seconds)
            if call_timeout <= 0:
                break
            callers = cg.get_callers(term, repo, limit=5, timeout=call_timeout)
            for c in callers:
                key = (c.path, c.line)
                if key in seen:
                    continue
                seen.add(key)
                matches.append((c.path, c.line, f"[caller→{term}] {c.name}"))
        return matches if matches else None

    def _source_evidence_terms(
        self,
        *,
        plans: list["BugAnalysisPlan"],
        request_text: str,
        followup_text: str,
        extra_texts: tuple[str, ...] = (),
    ) -> list[str]:
        terms: list[str] = []
        title_text = extra_texts[0] if extra_texts else ""
        description_texts = extra_texts[1:] if len(extra_texts) > 1 else ()
        for text in (request_text, followup_text, title_text):
            for term in self._explicit_source_terms_from_text(text):
                self._append_unique(terms, term)
        for plan in plans:
            if plan.kind != "signal" or not plan.signal_code:
                continue
            self._append_unique(terms, plan.signal_code)
            if plan.signal_code.startswith("SIGNAL_"):
                self._append_unique(terms, plan.signal_code.removeprefix("SIGNAL_"))
        for text in (followup_text, request_text):
            signal = self._extract_signal_code_for_reanalysis(text)
            if signal:
                self._append_unique(terms, signal)
                if signal.startswith("SIGNAL_"):
                    self._append_unique(terms, signal.removeprefix("SIGNAL_"))
        for text in (request_text, followup_text, title_text, *description_texts):
            for term in self._business_source_terms_from_text(text):
                self._append_unique(terms, term)
        return terms[:8]

    def _explicit_source_terms_from_text(self, text: str) -> list[str]:
        search_text = re.sub(r"https?://\S+", " ", text or "")
        search_text = re.sub(
            r"(?im)^\s*(?:Version|Build|Serial|ICCID|VIN)\s*[:：].*$",
            " ",
            search_text,
        )
        ignored_tokens = {"Version", "Build", "Serial", "ICCID", "VIN", "CLI"}
        terms: list[str] = []
        for match in re.finditer(
            r"(?<![A-Za-z0-9_])([A-Za-z_][A-Za-z0-9_.-]{1,120}\.(?:kt|java|cpp|cc|c|h|hpp|proto|xml))",
            search_text,
        ):
            filename = match.group(1).strip()
            self._append_unique(terms, filename)
            stem = Path(filename).stem
            if stem:
                self._append_unique(terms, stem)
        for match in re.finditer(r"\b([A-Z][A-Za-z0-9_]{4,})\b", search_text):
            token = match.group(1).strip()
            if token in ignored_tokens:
                continue
            self._append_unique(terms, token)
        return terms

    def _business_source_terms_from_text(self, text: str) -> list[str]:
        suffixes = (
            "模式",
            "功能",
            "场景",
            "页面",
            "流程",
            "链路",
            "策略",
            "状态",
            "异常",
            "失败",
            "开关",
            "服务",
            "模块",
            "信号",
            "电量",
            "电流",
            "电压",
            "百分比",
        )
        generic_prefixes = (
            "结合",
            "根据",
            "找到",
            "分析",
            "重新",
            "通过",
            "查看",
            "确认",
            "排查",
            "调查",
            "主要是",
            "为什么",
            "无法",
            "不能",
            "开启",
        )
        ascii_stopwords = {
            "http",
            "https",
            "project",
            "feishu",
            "meegle",
            "buglo",
            "detail",
            "cli",
            "bug",
            "code",
            "html",
            "report",
            "version",
            "build",
            "serial",
            "iccid",
            "vin",
            "cli",
        }
        search_text = re.sub(r"https?://\S+", " ", text or "")
        search_text = re.sub(
            r"(?im)^\s*(?:Version|Build|Serial|ICCID|VIN)\s*[:：].*$",
            " ",
            search_text,
        )
        terms: list[str] = []
        for match in re.finditer(r"(?<![A-Za-z0-9_])([A-Za-z][A-Za-z0-9_]{1,16}[\u4e00-\u9fff]{1,8})", search_text):
            term = match.group(1)
            self._append_unique(terms, term)
            ascii_part = re.match(r"[A-Za-z][A-Za-z0-9_]{1,16}", term)
            if ascii_part:
                token = ascii_part.group(0)
                chinese_part = term[len(token) :]
                for suffix in suffixes:
                    suffix_index = chinese_part.find(suffix)
                    if suffix_index >= 0:
                        compact = token + chinese_part[: suffix_index + len(suffix)]
                        if compact != term:
                            self._append_unique(terms, compact)
                if token.casefold() not in ascii_stopwords:
                    self._append_unique(terms, token)
        for match in re.finditer(r"(?<![A-Za-z0-9_])([A-Za-z][A-Za-z0-9_]{2,31})(?![A-Za-z0-9_])", search_text):
            token = match.group(1)
            if token.casefold() not in ascii_stopwords and not token.isdigit():
                self._append_unique(terms, token)
        for suffix in suffixes:
            pattern = re.compile(rf"[\u4e00-\u9fff]{{2,14}}{re.escape(suffix)}")
            for match in pattern.finditer(search_text):
                term = match.group(0)
                changed = True
                while changed:
                    changed = False
                    for prefix in generic_prefixes:
                        if term.startswith(prefix) and len(term) > len(prefix) + len(suffix):
                            term = term[len(prefix) :]
                            changed = True
                self._append_unique(terms, term)
                compact_len = len(suffix) + 2
                if len(term) > compact_len:
                    self._append_unique(terms, term[-compact_len:])
        return terms

    def _collect_source_evidence(
        self, *, repo: Path, terms: list[str], deadline: float | None = None
    ) -> list[tuple[str, int, str]]:
        if deadline is not None and time.monotonic() >= deadline:
            return []
        target_timeout = 5.0
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return []
            target_timeout = min(target_timeout, remaining)
        explicit_matches = self._collect_source_evidence_by_targets(repo=repo, terms=terms, timeout=target_timeout)
        if deadline is not None and time.monotonic() >= deadline:
            return explicit_matches
        rg_timeout = 20.0
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return explicit_matches
            rg_timeout = min(rg_timeout, remaining)
        rg_matches = self._collect_source_evidence_with_rg(repo=repo, terms=terms, timeout=rg_timeout)
        if rg_matches is not None:
            return self._merge_source_evidence_matches(explicit_matches, rg_matches)
        return explicit_matches

    def _collect_source_evidence_by_targets(
        self, *, repo: Path, terms: list[str], timeout: float = 5.0
    ) -> list[tuple[str, int, str]]:
        suffixes = {".kt", ".java", ".cpp", ".cc", ".c", ".h", ".hpp", ".proto", ".xml"}
        ignored_dirs = {".git", ".gradle", ".idea", "build", "out", ".cxx", "node_modules"}
        explicit_files = [term for term in terms if re.search(r"\.(?:kt|java|cpp|cc|c|h|hpp|proto|xml)$", term, re.I)]
        if not explicit_files:
            return []
        explicit_stems = {Path(term).stem for term in explicit_files}
        candidate_paths: list[Path] = []
        seen_paths: set[Path] = set()
        repo_resolved = repo.resolve()
        for term in explicit_files:
            candidate = (repo / term).resolve()
            try:
                candidate.relative_to(repo_resolved)
            except ValueError:
                continue
            if candidate.is_file() and candidate.suffix in suffixes and candidate not in seen_paths:
                seen_paths.add(candidate)
                candidate_paths.append(candidate)
        if timeout > 0 and shutil.which("rg") is not None:
            command = [
                "rg",
                "--files",
                "--color",
                "never",
                "--glob",
                "!.git/**",
                "--glob",
                "!.gradle/**",
                "--glob",
                "!build/**",
                "--glob",
                "!out/**",
                "--glob",
                "!.cxx/**",
            ]
            for term in explicit_files:
                command.extend(["--glob", f"**/{Path(term).name}"])
            command.append(str(repo))
            try:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                completed = None
            if completed is not None and completed.returncode in {0, 1}:
                for path_text in completed.stdout.splitlines():
                    path = Path(path_text)
                    if not path.is_absolute():
                        path = repo / path
                    try:
                        resolved = path.resolve()
                        resolved.relative_to(repo_resolved)
                    except (OSError, ValueError):
                        continue
                    if resolved.is_file() and resolved.suffix in suffixes and resolved not in seen_paths:
                        seen_paths.add(resolved)
                        candidate_paths.append(resolved)
        matches: list[tuple[str, int, str]] = []
        for path in sorted(candidate_paths):
            relative_parts = path.relative_to(repo_resolved).parts
            if any(part in ignored_dirs for part in relative_parts):
                continue
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            rel_path = str(path.relative_to(repo_resolved))
            stem = path.stem
            if path.name not in explicit_files and stem not in explicit_stems:
                continue
            for index, line in enumerate(lines, start=1):
                if stem in line or path.name in line:
                    matches.append((rel_path, index, line.strip()[:300]))
                    if len([item for item in matches if item[0] == rel_path]) >= 3:
                        break
        return matches

    def _merge_source_evidence_matches(
        self,
        primary: list[tuple[str, int, str]],
        extra: list[tuple[str, int, str]],
    ) -> list[tuple[str, int, str]]:
        merged: list[tuple[str, int, str]] = []
        seen: set[tuple[str, int, str]] = set()
        for item in [*primary, *extra]:
            if item in seen:
                continue
            seen.add(item)
            merged.append(item)
            if len(merged) >= 80:
                break
        return merged

    def _collect_source_evidence_with_rg(
        self, *, repo: Path, terms: list[str], timeout: float = 20.0
    ) -> list[tuple[str, int, str]] | None:
        if shutil.which("rg") is None:
            return None
        if timeout <= 0:
            return None
        command = [
            "rg",
            "--fixed-strings",
            "--line-number",
            "--no-heading",
            "--color",
            "never",
            "--ignore-case",
            "--max-count",
            "3",
            "--glob",
            "*.{kt,java,cpp,cc,c,h,hpp,proto,xml,md}",
            "--glob",
            "!.git/**",
            "--glob",
            "!.gradle/**",
            "--glob",
            "!build/**",
            "--glob",
            "!out/**",
            "--glob",
            "!.cxx/**",
        ]
        for term in terms:
            command.extend(["-e", term])
        command.append(str(repo))
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if completed.returncode not in {0, 1}:
            return None
        matches: list[tuple[str, int, str]] = []
        for line in completed.stdout.splitlines():
            if len(matches) >= 80:
                break
            path_text, sep, rest = line.partition(":")
            if not sep:
                continue
            line_no_text, sep, text = rest.partition(":")
            if not sep or not line_no_text.isdigit():
                continue
            try:
                rel_path = str(Path(path_text).resolve().relative_to(repo.resolve()))
            except ValueError:
                rel_path = path_text
            matches.append((rel_path, int(line_no_text), text.strip()[:300]))
        return matches

    def _collect_source_evidence_by_scan(self, *, repo: Path, terms: list[str]) -> list[tuple[str, int, str]]:
        """Legacy hook: pure Python full-repo scans are disabled for large repos."""
        return []

    def _append_unique(self, values: list[str], value: str) -> None:
        normalized = value.strip()
        if normalized and normalized not in values:
            values.append(normalized)

    def _render_bug_agent_request(
        self,
        *,
        request_text: str,
        prompt_text: str,
        bug_url: str,
        plans: list["BugAnalysisPlan"],
    ) -> str:
        plan_lines = "\n".join(f"- {self._analysis_label(plan.kind)} (`{plan.kind}`)" for plan in plans)
        return (
            "# Bug Agent Request\n\n"
            "以下内容需要完整提供给本地 Agent 作为分析输入。\n\n"
            f"- Bug URL: `{bug_url}`\n"
            f"- 提炼后的分析描述: `{prompt_text}`\n"
            f"- 计划分析类型:\n{plan_lines}\n"
            "- 用户原始请求:\n\n```text\n"
            f"{request_text}\n"
            "```\n"
        )

    def _render_bug_reanalysis_request(
        self,
        *,
        request_text: str,
        followup_text: str,
        target_time: str,
        plans: list["BugAnalysisPlan"],
        history: list[dict[str, str]] | None,
    ) -> str:
        plan_lines = "\n".join(
            f"- {self._analysis_label(plan.kind)} (`{plan.kind}`)"
            + (f" / 信号: `{plan.signal_code}`" if plan.kind == "signal" and plan.signal_code else "")
            for plan in plans
        )
        history_lines = self._compact_bug_followup_history(history)
        history_block = "\n".join(history_lines) if history_lines else "- 无"
        return (
            "# Bug Reanalysis Request\n\n"
            "这是同一个 Bug 会话里的续聊/修正，请延续上一轮分析上下文，而不是重新开启独立话题。\n\n"
            f"- 本次追问/修正: `{followup_text}`\n"
            f"- 修正后的故障时间: `{target_time or '未识别'}`\n"
            f"- 继续分析类型:\n{plan_lines}\n"
            "- 最近对话历史:\n"
            f"{history_block}\n"
            "- 用户原始请求:\n\n```text\n"
            f"{request_text}\n"
            "```\n"
        )

    def _compact_bug_followup_history(self, history: list[dict[str, str]] | None) -> list[str]:
        lines: list[str] = []
        assistant_compacted = False
        for item in history or []:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").strip()
            content = re.sub(r"\s+", " ", str(item.get("content") or "")).strip()
            if not role or not content:
                continue
            if role == "assistant":
                if assistant_compacted:
                    continue
                if len(content) > 400:
                    lines.append("- assistant: [上一轮长回答已省略；稳定事实请看 metadata 与会话事实快照]")
                    assistant_compacted = True
                    continue
                lines.append(f"- assistant: {content[:200].rstrip()}")
                assistant_compacted = True
                continue
            if len(content) > 240:
                content = content[:239].rstrip() + "…"
            lines.append(f"- {role}: {content}")
        return lines[:6]

    def _render_bug_agent_followup_request(
        self,
        *,
        request_text: str,
        followup_text: str,
        history: list[dict[str, str]] | None,
    ) -> str:
        history_lines = self._compact_bug_followup_history(history)
        history_block = "\n".join(history_lines) if history_lines else "- 无"
        return (
            "# Bug Agent Follow-up Request\n\n"
            "这是同一个 Bug 会话里的继续追问，请延续原来的 Agent 会话，"
            "优先复用已经下载/解密/分析过的日志与报告，不要重新要求用户上传材料。\n\n"
            f"- 本次追问:\n\n```text\n{followup_text}\n```\n"
            "- 会话稳定事实与证据入口: 以 metadata 与会话事实快照为准；不要在 request 中回放上一轮长摘要。\n"
            "- 最近对话历史:\n"
            f"{history_block}\n"
            "- 用户原始请求:\n\n```text\n"
            f"{request_text}\n"
            "```\n"
        )

    def _render_bug_reanalysis_metadata(
        self,
        *,
        request_text: str,
        followup_text: str,
        job_id: str,
        target_time: str,
        prepared_input: Path | None,
        selected_input: Path | None,
        plans: list["BugAnalysisPlan"],
        rerun_kinds: list[str],
        reused_kinds: list[str],
        html_paths: list[Path],
        report_jsons: dict[str, Path | None],
        combined_artifacts: dict[str, object] | None,
        previous_summary_path: Path | None,
        source_evidence_path: Path | None = None,
        classification_skill: str = "",
        classification_source: str = "",
        classification_reason: str = "",
        classification_provider: str = "",
    ) -> str:
        lines = [
            "# Bug Reanalysis Metadata",
            "",
            f"- Job ID: `{job_id}`",
            f"- 用户原始请求: `{request_text}`",
            f"- 本次追问/修正: `{followup_text}`",
            f"- 修正后的故障时间: `{target_time or '未识别'}`",
            f"- 复用 prepared log 输入: `{prepared_input or ''}`",
            f"- 上一轮选中的日志输入: `{selected_input or ''}`",
            f"- 分析类型: `{', '.join(plan.kind for plan in plans) or '无'}`",
            f"- 命中 Skill: `{classification_skill or 'general'}`",
            f"- 分类来源: `{classification_source or 'manual_fallback'}`",
            f"- 分类 Agent: `{classification_provider or '无'}`",
            f"- 分类理由: `{classification_reason or '未记录'}`",
            f"- Skill 规范:\n{self._render_skill_context_lines(classification_skill)}",
            f"- 信号目标: `{', '.join(plan.signal_code or '' for plan in plans if plan.kind == 'signal') or '无'}`",
            f"- 本次重新执行: `{', '.join(rerun_kinds) or '无'}`",
            f"- 本次直接复用: `{', '.join(reused_kinds) or '无'}`",
        ]
        if previous_summary_path is not None and self._followup_needs_previous_summary_text(followup_text):
            lines.append(f"- 上一轮 Agent 总结: `{previous_summary_path}`")
        if source_evidence_path is not None:
            lines.append(f"- 本轮源码证据: `{source_evidence_path}`")
        if combined_artifacts is not None:
            lines.extend(
                [
                    f"- 综合报告 HTML: `{combined_artifacts['html_path']}`",
                    f"- 综合报告 JSON: `{combined_artifacts['json_path']}`",
                ]
            )
        lines.append("- 最新 HTML 报告:")
        for path in html_paths:
            lines.append(f"  - `{path}`")
        lines.append("- 最新 JSON 报告:")
        for kind, path in report_jsons.items():
            lines.append(f"  - `{kind}` -> `{path or ''}`")
        if source_evidence_path is not None:
            lines.extend(["", "## 本轮源码证据摘录", ""])
            try:
                evidence = source_evidence_path.read_text(encoding="utf-8").strip()
            except OSError as exc:
                evidence = f"读取失败: {exc}"
            lines.append(evidence[:5000])
        return "\n".join(lines) + "\n"

    def _render_bug_agent_followup_metadata(
        self,
        *,
        request_text: str,
        followup_text: str,
        job_id: str,
        job_dir: Path,
        output_dir: Path,
        prepared_input: Path | None,
        selected_input: Path | None,
        previous_summary_path: Path | None,
        report_files: list[Path],
        report_url: str,
        analysis_skill: str = "",
    ) -> str:
        lines = [
            "# Bug Agent Follow-up Metadata",
            "",
            f"- Job ID: `{job_id}`",
            f"- Job 目录: `{job_dir}`",
            f"- 输出目录: `{output_dir}`",
            f"- 用户原始请求: `{request_text}`",
            f"- 本次追问: `{followup_text}`",
            f"- prepared log 输入: `{prepared_input or ''}`",
            f"- selected log 输入: `{selected_input or ''}`",
        ]
        if analysis_skill.strip():
            lines.append(f"- 命中 Skill: `{analysis_skill.strip()}`")
            lines.append(f"- Skill 规范:")
            lines.append(self._render_skill_context_lines(analysis_skill).rstrip())
        if previous_summary_path is not None and self._followup_needs_previous_summary_text(followup_text):
            lines.append(f"- 上一轮 Agent 总结: `{previous_summary_path}`")
        if report_url.strip():
            lines.append(f"- 当前已发布报告链接: `{report_url.strip()}`")
        lines.append("- 可直接读取的现有报告/产物:")
        if report_files:
            for path in report_files:
                lines.append(f"  - `{path}`")
        else:
            lines.append("  - 无")
        lines.extend(
            [
                "- 处理要求:",
                "  - 直接基于上述本地路径继续分析，不要重新要求用户上传日志。",
                "  - 若需要补充证据，优先读取 prepared log 输入与 output 目录中的现有产物。",
            ]
        )
        return "\n".join(lines) + "\n"

    def _collect_bug_output_artifacts(self, output_dir: Path) -> list[Path]:
        if not output_dir.exists():
            return []
        artifacts: list[Path] = []
        for path in sorted(output_dir.iterdir()):
            if not path.is_file():
                continue
            if path.suffix.lower() not in {".md", ".html", ".json"}:
                continue
            artifacts.append(path)
        return artifacts

    def _build_bug_agent_summary_command(
        self,
        *,
        request_text: str,
        request_artifact: Path,
        metadata_path: Path,
        output_path: Path,
        provider_session_id: str = "",
        followup_text: str = "",
        previous_summary_path: Path | None = None,
        provider_override: str = "",
        command_override: str = "",
        snapshot_details: dict[str, object] | None = None,
        snapshot_plans: list["BugAnalysisPlan"] | None = None,
    ) -> dict[str, object]:
        provider = _normalize_provider_name(provider_override or self.config.bug_analysis.provider)
        if command_override.strip():
            command_name = command_override.strip()
        elif provider_override:
            command_name = _default_command_for_provider(provider)
        else:
            command_name = (self.config.bug_analysis.command or "").strip() or _default_command_for_provider(provider)
        if not provider or not command_name:
            detected_provider, detected_command = _detect_available_provider()
            if detected_provider and detected_command:
                provider = provider or detected_provider
                command_name = command_name or detected_command
        if not provider or not command_name:
            return {"command": [], "provider": provider, "session_id": provider_session_id, "resumed": False}
        prompt = self._build_bug_agent_summary_prompt(
            request_text=request_text,
            request_artifact=request_artifact,
            metadata_path=metadata_path,
            followup_text=followup_text,
            previous_summary_path=previous_summary_path,
            snapshot_details=snapshot_details,
            snapshot_plans=snapshot_plans,
        )
        embedded_files = self._bug_agent_summary_context_files(
            followup_text=followup_text,
            request_artifact=request_artifact,
            metadata_path=metadata_path,
            previous_summary_path=previous_summary_path,
        )
        session_id = provider_session_id.strip()
        if provider == "codex":
            model = (self.config.bug_analysis.model or "").strip()
            if session_id:
                command = [
                    command_name,
                    "exec",
                    "resume",
                    "--skip-git-repo-check",
                    "--json",
                ]
                if model:
                    command.extend(["-m", model])
                command.extend(
                    [
                        "--output-last-message",
                        str(output_path),
                        session_id,
                        prompt,
                    ]
                )
            else:
                command = [
                    command_name,
                    "exec",
                    "--skip-git-repo-check",
                    "-s",
                    "read-only",
                    "-C",
                    str(self._working_dir()),
                ]
                if model:
                    command.extend(["-m", model])
                command.extend(
                    [
                        "--json",
                        "--output-last-message",
                        str(output_path),
                        prompt,
                    ]
                )
            return {
                "command": command,
                "provider": provider,
                "model": model,
                "session_id": session_id,
                "resumed": bool(session_id),
                "prompt": prompt,
                "embedded_files": embedded_files,
            }
        if provider in {"claude", "claude-code", "claude_code"}:
            model = (self.config.claude_agent.model or "").strip()
            allowed_tools = self.config.claude_agent.allowed_tools or ["Read", "Grep", "Glob", "LS"]
            session_id = session_id or str(uuid.uuid4())
            command = [
                command_name,
                "--print",
                "--output-format",
                "text",
                "--permission-mode",
                "dontAsk",
                "--allowedTools",
                ",".join(allowed_tools),
                "--append-system-prompt",
                (
                    "你是一个通过飞书触发的 bug 分析总结 agent。"
                    "只读分析，不修改文件，不执行写入命令。"
                    "必须完整响应用户原始请求中的所有诉求，输出中文 Markdown，结论先行。"
                ),
            ]
            for directory in self._bug_summary_add_dirs():
                command.extend(["--add-dir", str(directory)])
            if provider_session_id.strip():
                command.extend(["--resume", session_id])
            else:
                command.extend(["--session-id", session_id])
            command.append(prompt)
            return {
                "command": command,
                "provider": provider,
                "model": model,
                "session_id": session_id,
                "resumed": bool(provider_session_id.strip()),
                "prompt": prompt,
                "embedded_files": embedded_files,
            }
        return {"command": [], "provider": provider, "session_id": session_id, "resumed": False, "prompt": prompt, "embedded_files": embedded_files}

    def _build_bug_agent_summary_prompt(
        self,
        *,
        request_text: str,
        request_artifact: Path,
        metadata_path: Path,
        followup_text: str = "",
        previous_summary_path: Path | None = None,
        snapshot_details: dict[str, object] | None = None,
        snapshot_plans: list["BugAnalysisPlan"] | None = None,
    ) -> str:
        prompt = "请基于以下本地文件完成同一个 bug 会话的最终回答。\n要求：\n"
        snapshot_prefix = ""
        if followup_text.strip():
            prompt += (
                "1. 这是一条续聊/追问，必须直接回答这次新问题，并延续上一轮 Agent 会话。\n"
                "2. 优先复用 metadata 中已经给出的日志、报告、output 目录和历史总结，不要要求用户重新上传日志。\n"
                "3. 只读分析，不修改任何文件。\n"
                "4. 输出中文 Markdown，结论先行，再给出证据。\n"
                "5. 如果现有日志/报告仍不足以覆盖某个诉求，要明确指出缺口，但先回答已经能确认的部分。\n"
                "6. 对启动/生命周期类报告，若结构化 JSON 显示 `status != complete` 或仍有 `missing_critical`，即使 HTML/verdict 文案更乐观，也按“链路未闭环”处理，并明确指出报告内部冲突。\n"
                "7. “未命中关键节点”不等于“日志在这里截止”；除非材料明确显示文件结束或时间窗截断，否则不要把缺节点改写成日志截止。\n\n"
            )
            snapshot_prefix = self._build_bug_prompt_snapshot_prefix(
                request_text=request_text,
                metadata_path=metadata_path,
                followup_text=followup_text,
                snapshot_details=snapshot_details,
                plans_override=snapshot_plans,
            )
        else:
            prompt += (
                "1. 本次是全新 bug 分析请求，不是续聊/修正；不要虚构“上一轮分析”“本次修正”“延续上一轮”这类诉求或标题。\n"
                "2. 必须完整覆盖用户原始请求里的所有诉求，不要只回答其中一部分。\n"
                "3. 只读分析，不修改任何文件。\n"
                "4. 输出中文 Markdown，结论先行；若有多个诉求，按诉求分组说明结论和证据；若只有一个诉求，只在开头说明一次，不要在每条结论或证据前重复写相同的诉求。诉求标题只能来自用户原始请求，不要自行添加不存在的诉求。\n"
                "5. 如果脚本结果无法覆盖用户某个诉求，要明确指出缺口。\n"
                "6. metadata 中的“本轮脚本初步摘要”只是当前自动脚本输出，不要把它写成“上一轮结论”；只有显式提供 followup/previous summary 时，才能讨论修正上一轮结论。\n"
                "7. 如果 metadata 或报告里已经明确给出故障时间对应的主会话 / 主 PID / focus session，请优先围绕该主会话分析，不要展开无关会话；只有在需要证明时间不匹配时才提及其他会话。\n\n"
            )
        prompt += (
            "统一输出结构：请按以下中文二级标题组织最终回答，并只填入本次 bug 自身的证据，不套用示例业务词。\n"
            "## 结论摘要\n"
            "- 先给 3 到 5 条最重要结论，必须标明置信边界。\n"
            "## 关键证据\n"
            "- 每条证据尽量带文件、行号、时间、进程/package 或源码位置。\n"
            "## 最可能原因\n"
            "- 按可能性排序，说明支持证据和缺口；证据不足时明确不要强行定根因。\n"
            "## 待确认项\n"
            "- 只列真实证据缺口，例如精确时间、日志片段、运行状态、源码链路缺口。\n"
            "## 建议动作\n"
            "- 给出下一轮可执行动作，例如补日志、重跑某个 skill、沿某个源码或日志点继续查。\n\n"
        )
        if snapshot_prefix:
            prompt += snapshot_prefix
        prompt += f"用户原始请求：\n{request_text}\n\n"
        if followup_text.strip():
            prompt += f"本次追问/修正：\n{followup_text.strip()}\n\n"
        if previous_summary_path is not None and self._should_include_previous_summary_for_followup(
            followup_text=followup_text,
            request_artifact=request_artifact,
            metadata_path=metadata_path,
        ):
            prompt += f"上一轮 Agent 总结（也要核对，不可直接当成事实）：\n{previous_summary_path}\n\n"
        prompt += (
            "可读取路径：\n"
            f"- 工作区根目录：{self._working_dir()}\n"
            f"- 业务源码根目录：{self.config.guideengine_repo}\n\n"
            "以下是首批本地文件入口。请按需读取这些本地文件，优先读取 JSON/Markdown 结构化产物；"
            "HTML 只作为可视化报告入口，不要把 CSS/style/script 当作分析证据。"
            "如果 Bug Metadata 中列出命中的 Skill 规范，必须先读取对应 SKILL.md，并按其适用范围、执行链路和输出规则分析。"
            "只读分析，不修改文件，不要编造未看到的证据：\n\n"
        )
        if followup_text.strip():
            prompt += (
                "续聊性能约束：优先根据下面的精简上下文回答。"
                "只有精简上下文无法证明时，才读取 metadata 中列出的报告、日志或源码路径。\n\n"
            )
            for item in self._bug_agent_summary_context_files(
                followup_text=followup_text,
                request_artifact=request_artifact,
                metadata_path=metadata_path,
                previous_summary_path=previous_summary_path,
            ):
                prompt += self._render_bug_summary_context_item(item)
        else:
            for item in self._bug_agent_summary_context_files(
                followup_text=followup_text,
                request_artifact=request_artifact,
                metadata_path=metadata_path,
                previous_summary_path=previous_summary_path,
            ):
                prompt += self._render_bug_summary_context_item(item)
        return prompt

    def _bug_agent_summary_context_files(
        self,
        *,
        followup_text: str = "",
        request_artifact: Path,
        metadata_path: Path,
        previous_summary_path: Path | None = None,
    ) -> list[dict[str, object]]:
        files: list[dict[str, object]] = []
        if followup_text.strip():
            if previous_summary_path is not None and self._should_include_previous_summary_for_followup(
                followup_text=followup_text,
                request_artifact=request_artifact,
                metadata_path=metadata_path,
            ):
                files.append({"title": "上一轮 Agent 总结", "path": str(previous_summary_path), "max_chars": 0})
            files.append({"title": "Bug Agent Follow-up Request", "path": str(request_artifact), "max_chars": 0})
            files.append({"title": "Bug Follow-up Metadata", "path": str(metadata_path), "max_chars": 0})
            files.extend(self._bug_summary_referenced_context_files(metadata_path))
            return files
        if previous_summary_path is not None:
            files.append({"title": "上一轮 Agent 总结", "path": str(previous_summary_path), "max_chars": 0})
        files.append({"title": "Bug Agent Request", "path": str(request_artifact), "max_chars": 0})
        files.append({"title": "Bug Metadata", "path": str(metadata_path), "max_chars": 0})
        files.extend(self._bug_summary_referenced_context_files(metadata_path))
        return files

    def _bug_summary_referenced_context_files(self, metadata_path: Path) -> list[dict[str, object]]:
        try:
            metadata_text = metadata_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        metadata_resolved = metadata_path.expanduser().resolve()
        candidates: list[tuple[int, int, dict[str, object]]] = []
        seen: set[Path] = {metadata_resolved}
        raw_paths: list[tuple[int, str]] = []
        for index, raw in enumerate(re.findall(r"`([^`]+)`", metadata_text)):
            if re.match(r"^(?:/|~/)", raw.strip()):
                raw_paths.append((index, raw))
        offset = len(raw_paths)
        for index, raw in enumerate(re.findall(r"((?:/|~/)[^\s`]+?\.(?:md|json|html))", metadata_text)):
            raw_paths.append((offset + index, raw))
        for index, raw in raw_paths:
            path_text = raw.strip().rstrip(".,;:)）]}>，。；")
            path = Path(path_text).expanduser()
            if path.suffix.lower() not in {".md", ".json", ".html"}:
                continue
            if not path.exists() or not path.is_file():
                continue
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            title, priority, max_chars = self._bug_summary_context_file_profile(path)
            if not title:
                continue
            candidates.append(
                (
                    priority,
                    index,
                    {
                        "title": title,
                        "path": str(path),
                        "max_chars": max_chars,
                    },
                )
            )
        return [item for _priority, _index, item in sorted(candidates, key=lambda value: (value[0], value[1]))[:5]]

    def _bug_summary_context_file_profile(self, path: Path) -> tuple[str, int, int]:
        name = path.name
        lowered = name.casefold()
        if lowered == "bug_summary_evidence.md":
            return "Bug Summary Evidence", 0, 0
        if lowered == "bug_summary_evidence.json":
            return "Bug Summary Evidence JSON", 0, 0
        if lowered == "bug_source_evidence.md":
            return "Bug Source Evidence", 0, 0
        if lowered == "skill.md" and ".ai/skills" in str(path):
            return f"Matched Skill: {path.parent.name}", 0, 0
        if path.suffix.lower() == ".md" and path.parent.name == "references" and ".ai/skills" in str(path):
            return f"Matched Skill Reference: {name}", 1, 0
        if lowered.endswith("_analysis.md"):
            label = name.replace("_analysis.md", "").replace("_", " ").strip().title() or "Analysis"
            return f"Analysis Markdown: {label}", 1, 0
        if lowered.endswith(".json") and "_report" in lowered:
            return f"Report JSON: {name}", 2, 0
        if lowered.endswith(".html") and "_report" in lowered:
            return f"Report HTML: {name}", 3, 0
        return "", 9, 0

    def _render_bug_summary_context_item(self, item: dict[str, object]) -> str:
        title = str(item.get("title") or "Context File")
        path = Path(str(item.get("path") or ""))
        lowered = path.name.casefold()
        if lowered.endswith(".json") and "_report" in lowered:
            note = "结构化分析结果，必须优先读取，用于结论、时间窗、PID、证据行号。"
        elif lowered.endswith(".html") and "_report" in lowered:
            note = "可视化 HTML 报告；只有 JSON/Markdown 不足时再读取，读取时忽略 CSS/style/script。"
        elif lowered == "bug_summary_evidence.md":
            note = "结构化证据包；优先读取，用于最后命中事件、后续同 PID 原始日志、缺失关键节点和禁止结论。"
        elif lowered == "bug_summary_evidence.json":
            note = "结构化证据包 JSON；用于程序化核对字段和值，优先级高于 HTML 报告。"
        elif lowered == "bug_source_evidence.md":
            note = "源码证据文件；需要源码链路时读取。"
        elif lowered == "skill.md":
            note = "本轮命中的专用 Skill 规范；必须先读取并按其中的 Required Workflow / 适用范围执行。"
        elif path.parent.name == "references":
            note = "本轮命中 Skill 的引用资料；SKILL.md 要求读取或证据不足时必须读取。"
        elif "metadata" in lowered:
            note = "元数据索引文件；先读取它获取日志、报告、output 目录、主 PID 和故障时间等入口。"
        elif "request" in lowered:
            note = "用户请求文件；读取它确认原始诉求和本轮追问边界。"
        else:
            note = "本地上下文文件；按需读取。"
        return (
            f"## {title}\n"
            f"路径: `{path}`\n"
            f"读取要求: {note}\n\n"
        )

    def _embedded_file_manifest(self, embedded_files: list[object]) -> list[dict[str, object]]:
        manifest: list[dict[str, object]] = []
        for item in embedded_files:
            if not isinstance(item, dict):
                continue
            path = Path(str(item.get("path") or ""))
            max_chars = int(item.get("max_chars") or 0)
            entry: dict[str, object] = {
                "title": str(item.get("title") or ""),
                "path": str(path),
                "max_chars": max_chars,
                "exists": path.exists(),
            }
            if path.exists():
                try:
                    content = path.read_text(encoding="utf-8")
                    stripped = content.strip()
                    entry["source_chars"] = len(stripped)
                    entry["embedded_chars"] = min(len(stripped), max_chars) if max_chars > 0 else 0
                    entry["truncated"] = max_chars > 0 and len(stripped) > max_chars
                except OSError as exc:
                    entry["read_error"] = str(exc)
            manifest.append(entry)
        return manifest

    def _bug_summary_add_dirs(self) -> list[Path]:
        candidates = [self._working_dir(), self.config.guideengine_repo, *self.config.claude_agent.add_dirs]
        resolved: list[Path] = []
        seen: set[Path] = set()
        for candidate in candidates:
            path = Path(candidate).expanduser().resolve()
            if path in seen:
                continue
            seen.add(path)
            resolved.append(path)
        return resolved

@dataclass(slots=True)
class BugAnalysisPlan:
    kind: str
    signal_code: str | None = None


@dataclass(slots=True)
class AnalysisDecision:
    domain_kind: str
    source_mode: str
    context_profile: str = ""
    source_targets: list[str] = field(default_factory=list)
    reason: str = ""


@dataclass(slots=True)
class AnalysisStage:
    kind: str
    domain_kind: str = ""
    context_profile: str = ""
    source_targets: list[str] = field(default_factory=list)


@dataclass(slots=True)
class AnalysisPlan:
    domain_kind: str
    context_profile: str = ""
    stages: list[AnalysisStage] = field(default_factory=list)


@dataclass(slots=True)
class BugTimeContext:
    fault_time: str
    source: str
    note: str
    has_full_datetime: bool
    candidates: list[dict[str, str]]


@dataclass(slots=True)
class LogCoverage:
    has_time_evidence: bool
    covers_fault_time: bool
    start_time: str = ""
    end_time: str = ""
    scanned_files: int = 0
    scanned_lines: int = 0
    sample_file: str = ""
    reason: str = ""


@dataclass(slots=True)
class SourceAnalysisDecision:
    requested: bool
    reason: str = ""
    targets: list[str] = field(default_factory=list)
    source: str = ""
    debug_shortcut: bool = False
    domain_kind: str = "general"
    source_mode: str = "off"
    context_profile: str = ""
    stage_kinds: list[str] = field(default_factory=list)


@dataclass(slots=True)
class BugAnalysisSelection:
    plans: list["BugAnalysisPlan"]
    skill_name: str
    skill_label: str
    source: str
    reason: str = ""
    provider: str = ""


@dataclass(slots=True)
class UnifiedBugDecision:
    """Combined domain classification + source analysis decision.

    Produced by ``_unified_classify_and_decide`` to replace the previous
    two-step classify → decide_source → augment flow with a single call.
    """

    selection: "BugAnalysisSelection"
    source_decision: "SourceAnalysisDecision"


@dataclass(slots=True)
class BugFollowupSelection:
    should_reanalyze: bool
    force_rerun: bool
    plans: list["BugAnalysisPlan"]
    skill_name: str
    skill_label: str
    source: str
    reason: str = ""
    provider: str = ""
