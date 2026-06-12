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

from ...downloader import DownloadError, LogDownloader
from ...evidence_logs import preserve_evidence_log_bundle
from ...health import ProcessWatchdog, _safe_terminate, _write_subprocess_debug_log
from ...log import get_logger
from ...log_analyzer import SmartLogAnalyzer
from ...models import (
    BridgeConfig,
    BugRequest,
    DownloadResource,
    LarkEvent,
    TaskResult,
    create_job_context,
)
from ...network_env import build_internal_network_env
from ...parser import parse_followup_action, parse_signal_request
from ...reporting import (
    ReportComposition,
    ReportSection,
    ReportVerdict,
    build_structured_summary_sections,
    combined_bug_html,
    composition_to_renderer_payload,
    plan_signal_report,
    plan_startup_stuck_report,
)
from ...signal_resolver import SignalResolver
from ...skill_registry import AUX_BUG_SKILLS, PRIMARY_BUG_SKILL_MAP, extract_skill_frontmatter
from ...skill_manager import SkillManager
from ...token_usage import TOKEN_USAGE_ALIASES, normalize_token_usage
from ... import prompt_snapshots
from .._helpers import (
    _default_command_for_provider,
    _detect_available_provider,
    _normalize_provider_name,
    _provider_candidates,
)
from ..bug_summary_policy import SummaryBackendInput, choose_summary_backend
from ..codex_app_server_runtime import (
    CodexAppServerRuntime,
    app_server_event_preview,
    check_codex_app_server_available,
)
from ..omlx_client import OmlxChatClient
from ..routing_terms import (
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
    DirectApiCompactionProfileSpec(
        name="scene_signal_target_focus",
        analysis_kind="scene_signal",
        skill_name="scene-signal-diagnosis",
        handler="_direct_api_context_excerpt_for_scene_signal_target_focus",
        metadata_markers=("bug_scene_signal_report",),
    ),
    DirectApiCompactionProfileSpec(
        name="xtheme_signal_boundary",
        analysis_kind="xtheme",
        skill_name="xtheme-analyzer",
        handler="_direct_api_context_excerpt_for_xtheme_signal_boundary",
        metadata_markers=("bug_xtheme_analysis_report",),
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


_DIAGNOSE_TERMS: tuple[str, ...] = (
    "收不到", "没收到", "看不到", "不显示", "没显示", "不展示", "没展示",
    "为什么没", "为啥没", "为何没", "黑屏", "卡住", "卡死", "闪退", "崩溃",
    "掉帧", "不刷新", "报错", "异常", "ANR", "无法", "失败", "没反应", "丢失", "不生效",
)
_CONSULT_TERMS: tuple[str, ...] = (
    "了解", "怎么接入", "如何接入", "涉及哪些模块", "涉及哪些", "链路怎么走",
    "怎么走", "数据怎么来", "从哪来", "从哪里来", "怎么分发", "如何分发",
    "注册在哪", "在哪注册", "梳理", "讲解", "说明", "介绍", "怎么实现", "如何实现",
)


def infer_intent_from_text(text: str) -> str:
    """纯文本启发式意图初判：diagnose / consult / ""（不确定）。"""
    blob = text or ""
    if any(term in blob for term in _DIAGNOSE_TERMS):
        return "diagnose"
    if any(term in blob for term in _CONSULT_TERMS):
        return "consult"
    return ""


def resolve_effective_intent(text_intent: str, *, has_logs: bool) -> str:
    """最终裁决：显式初判优先；不确定时按是否有日志兜底（有日志→诊断，无→咨询）。"""
    if text_intent in {"consult", "diagnose"}:
        return text_intent
    return "diagnose" if has_logs else "consult"


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
    intent: str = ""


@dataclass(slots=True)
class BugAnalysisSelection:
    plans: list["BugAnalysisPlan"]
    skill_name: str
    skill_label: str
    source: str
    reason: str = ""
    provider: str = ""
    confidence: str = ""


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


__all__ = [
    'infer_intent_from_text',
    'resolve_effective_intent',
    'concurrent',
    'dataclass',
    'field',
    'datetime',
    'timedelta',
    'timezone',
    'hashlib',
    'html_lib',
    'importlib',
    'json',
    'math',
    'os',
    're',
    'select',
    'shlex',
    'signal',
    'Path',
    'shutil',
    'stat',
    'subprocess',
    'sys',
    'tempfile',
    'time',
    'uuid',
    'zipfile',
    'Callable',
    'urllib',
    'DownloadError',
    'LogDownloader',
    'preserve_evidence_log_bundle',
    'ProcessWatchdog',
    '_safe_terminate',
    '_write_subprocess_debug_log',
    'get_logger',
    'SmartLogAnalyzer',
    'BridgeConfig',
    'BugRequest',
    'DownloadResource',
    'LarkEvent',
    'TaskResult',
    'create_job_context',
    'build_internal_network_env',
    'parse_followup_action',
    'parse_signal_request',
    'ReportComposition',
    'ReportSection',
    'ReportVerdict',
    'build_structured_summary_sections',
    'combined_bug_html',
    'composition_to_renderer_payload',
    'plan_signal_report',
    'plan_startup_stuck_report',
    'SignalResolver',
    'AUX_BUG_SKILLS',
    'PRIMARY_BUG_SKILL_MAP',
    'extract_skill_frontmatter',
    'SkillManager',
    'TOKEN_USAGE_ALIASES',
    'normalize_token_usage',
    'prompt_snapshots',
    '_default_command_for_provider',
    '_detect_available_provider',
    '_normalize_provider_name',
    '_provider_candidates',
    'SummaryBackendInput',
    'choose_summary_backend',
    'CodexAppServerRuntime',
    'app_server_event_preview',
    'check_codex_app_server_available',
    'OmlxChatClient',
    'CRASH_ROUTE_TERMS',
    'LD_LANE_LEVEL_ROUTE_TERMS',
    'PERCEPTION_ROUTE_TERMS',
    'PULLOVER_CHAIN_ROUTE_TERMS',
    'SCENE_SIGNAL_ROUTE_TERMS',
    'SIGNAL_ROUTE_TERMS',
    'STARTUP_BLOCK_ROUTE_TERMS',
    'STARTUP_ROUTE_TERMS',
    'STUCK_ROUTE_TERMS',
    'XTHEME_ROUTE_TERMS',
    '_has_strong_scene_signal_intent',
    '_is_core_scene_signal',
    'looks_like_scene_signal_request',
    'logger',
    'SOURCE_STAGE_KIND',
    'SOURCE_CODE_SKILL_KIND',
    'SOURCE_STAGE_KINDS',
    'PlanKindSpec',
    'DirectApiCompactionProfileSpec',
    'PLAN_KIND_REGISTRY',
    '_SOURCE_SKILL_KINDS',
    '_DIRECT_API_COMPACTION_PROFILES',
    'ALL_PLAN_KINDS',
    '_kind_spec',
    '_SCRIPT_ACCEPTS_TARGET_TIME',
    '_RETRY_KINDS',
    '_kind_accepts_target_time',
    'NormalizedVerdict',
    'BugReportAdapter',
    '_run_tracked_process',
    '_coerce_process_text',
    '_NON_LOG_XP_MAGIC_HEADERS',
    '_BUG_ATTACHMENT_DOWNLOAD_SUFFIXES',
    '_BUG_ARCHIVE_SUFFIXES',
    '_BUG_LOG_INPUT_PRIORITY_SUFFIXES',
    '_TOKEN_USAGE_KEYS',
    '_RUNTIME_HTML_MARKER_START',
    '_RUNTIME_HTML_MARKER_END',
    '_BUG_LOG_COVERAGE_WINDOW_MINUTES',
    '_BUG_LOG_COVERAGE_MAX_FILES',
    '_BUG_LOG_COVERAGE_MAX_LINES_PER_FILE',
    '_BUG_LOG_COVERAGE_SUFFIXES',
    '_GENERIC_BUG_PROMPT_TERMS',
    '_GENERAL_SCOPE_PATTERNS',
    '_PRIMARY_BUG_SKILL_MAP',
    '_AUX_BUG_SKILLS',
    '_extract_skill_frontmatter',
    'CodexAppServerExecutionPolicy',
    'BugAnalysisPlan',
    'AnalysisDecision',
    'AnalysisStage',
    'AnalysisPlan',
    'BugTimeContext',
    'LogCoverage',
    'SourceAnalysisDecision',
    'BugAnalysisSelection',
    'UnifiedBugDecision',
    'BugFollowupSelection',
]
