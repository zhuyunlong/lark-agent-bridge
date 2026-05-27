"""Local agent integrations for Claude Code, Codex, and omlx chat."""

from __future__ import annotations

from ..downloader import DownloadError, LogDownloader
from ..evidence_logs import preserve_evidence_log_bundle
from ..health import ProcessWatchdog, run_tracked_process
from ..log import get_logger
from ..models import (
    Addr2LineRequest,
    BridgeConfig,
    BugRequest,
    ClaudeSkillRequest,
    DownloadResource,
    IntentDecision,
    LarkEvent,
    PerceptionSummaryRequest,
    RomVersionLookupRequest,
    TaskResult,
    create_job_context,
)
from ..parser import parse_signal_request
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
from .addr2line_runner import Addr2LineRunner
from .bug_runner import (
    BugAnalysisPlan,
    BugAnalysisRunner,
    BugAnalysisSelection,
    BugFollowupSelection,
    BugTimeContext,
    LogCoverage,
    UnifiedBugDecision,
)
from .claude_runner import ClaudeSkillRunner
from .intent_runner import IntentAnalysisFailure, IntentAnalysisRunner
from .llm_client import LLMClient, LLMClientError, LLMResponse
from .omlx_client import OmlxChatClient
from .perception_runner import PerceptionSummaryRunner
from .rom_version_runner import RomVersionLookupRunner
from .routing_terms import (
    CORE_SCENE_SIGNALS,
    CRASH_ROUTE_TERMS,
    PERCEPTION_ROUTE_TERMS,
    SCENE_SIGNAL_CONTEXT_TERMS,
    SCENE_SIGNAL_HINT_TERMS,
    SCENE_SIGNAL_ROUTE_TERMS,
    SIGNAL_ROUTE_TERMS,
    STARTUP_BLOCK_ROUTE_TERMS,
    STARTUP_ROUTE_TERMS,
    STRONG_SCENE_SIGNAL_INTENT_TERMS,
    STUCK_ROUTE_TERMS,
    XTHEME_ROUTE_TERMS,
    looks_like_scene_signal_request,
)

logger = get_logger("agents")

__all__ = [
    "AUX_BUG_SKILLS",
    "Addr2LineRequest",
    "Addr2LineRunner",
    "BridgeConfig",
    "BugAnalysisPlan",
    "BugAnalysisRunner",
    "BugAnalysisSelection",
    "BugFollowupSelection",
    "BugRequest",
    "BugTimeContext",
    "CRASH_ROUTE_TERMS",
    "CORE_SCENE_SIGNALS",
    "ClaudeSkillRequest",
    "ClaudeSkillRunner",
    "DownloadError",
    "DownloadResource",
    "IntentAnalysisFailure",
    "IntentAnalysisRunner",
    "IntentDecision",
    "LarkEvent",
    "LLMClient",
    "LLMClientError",
    "LLMResponse",
    "LogCoverage",
    "LogDownloader",
    "OmlxChatClient",
    "PRIMARY_BUG_SKILL_MAP",
    "PERCEPTION_ROUTE_TERMS",
    "PerceptionSummaryRequest",
    "PerceptionSummaryRunner",
    "ProcessWatchdog",
    "ReportComposition",
    "ReportSection",
    "ReportVerdict",
    "RomVersionLookupRequest",
    "RomVersionLookupRunner",
    "SCENE_SIGNAL_CONTEXT_TERMS",
    "SCENE_SIGNAL_HINT_TERMS",
    "SCENE_SIGNAL_ROUTE_TERMS",
    "SIGNAL_ROUTE_TERMS",
    "STARTUP_BLOCK_ROUTE_TERMS",
    "STARTUP_ROUTE_TERMS",
    "STRONG_SCENE_SIGNAL_INTENT_TERMS",
    "STUCK_ROUTE_TERMS",
    "SignalResolver",
    "TaskResult",
    "UnifiedBugDecision",
    "XTHEME_ROUTE_TERMS",
    "build_structured_summary_sections",
    "combined_bug_html",
    "composition_to_renderer_payload",
    "create_job_context",
    "extract_skill_frontmatter",
    "get_logger",
    "logger",
    "looks_like_scene_signal_request",
    "parse_signal_request",
    "plan_signal_report",
    "plan_startup_stuck_report",
    "preserve_evidence_log_bundle",
    "run_tracked_process",
]
