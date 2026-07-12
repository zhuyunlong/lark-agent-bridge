"""Application orchestration for the bridge."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import html
import json
import os
from pathlib import Path
import re
import shutil
import stat
from typing import Callable
from urllib.parse import quote, urlsplit, urlunsplit

from ..log import get_logger

logger = get_logger("app")

from ..agents import (
    Addr2LineRunner,
    BugAnalysisPlan,
    BugAnalysisRunner,
    BugFollowupSelection,
    ClaudeSkillRunner,
    IntentAnalysisFailure,
    IntentAnalysisRunner,
    OmlxChatClient,
    PerceptionSummaryRunner,
    RomVersionLookupRunner,
    looks_like_scene_signal_request,
)
from ..agents.codex_app_server_runtime import CodexAppServerTurnController
from ..arbitration import arbitrate, extract_conclusion
from ..approval import ApprovalStatus, ApprovalStore, build_operation_request
from ..cards import (
    build_agent_reanalysis_confirmation_card,
    build_confirmation_card,
    build_followup_result_card,
    build_knowledge_answer_card,
    build_result_card,
    build_status_card,
    card_to_json,
)
from ..case_store import CaseStore
from ..conversation_input import ResolvedConversationInput
from ..downloader import LogDownloader
from ..escalation import (
    EscalationChecker,
    NotificationHistory,
    build_report_ready_notification,
    build_status_change_notification,
)
from ..health import HealthMonitor, ProcessWatchdog
from ..knowledge import KnowledgeService
from ..lark_client import LarkClient
from ..lifecycle import AnalysisType, LifecycleStore, mode_to_analysis_type
from ..models import (
    AppServerInvestigationRequest,
    Addr2LineRequest,
    BridgeConfig,
    BotMenuEvent,
    BugRequest,
    CardActionEvent,
    DownloadResource,
    DirectAnalysisRequest,
    IntentDecision,
    LarkEvent,
    MessageRecalledEvent,
    ReactionEvent,
    RequirementAnalysisRequest,
    ReportFollowupRequest,
    RomVersionLookupRequest,
    SignalRequest,
    SourceAnalysisRequest,
    TaskResult,
    create_job_context,
)
from ..parser import (
    parse_app_server_investigation_request,
    build_basic_chat_reply,
    build_bug_url_re,
    extract_first_keyword_payload,
    find_resources,
    looks_like_direct_analysis_prompt,
    parse_report_followup_request,
    parse_followup_action,
    parse_claude_skill_request,
    parse_bug_request,
    parse_direct_analysis_request,
    parse_perception_summary_request,
    parse_requirement_analysis_request,
    parse_addr2line_request,
    parse_rom_version_lookup_request,
    parse_signal_request,
    parse_source_analysis_request,
    should_use_omlx_chat,
)
from ..policy import PolicyDecision, build_policy_rejection_message, evaluate_event_policy
from ..report_server import HtmlReportPublisher, ReportHttpServer, resolve_bind_host, resolve_public_base_url
from ..reporting.source_report_html import render_context_diagram_report
from ..report_version import ReportVersionStore, derive_group_key
from ..replay import (
    AnalysisReplayContext,
    ReplayDecision,
    ReplayPlan,
    ReplayResourceBundle,
    normalize_replay_mode,
    serialize_resource_status,
)
from ..runner import SignalChainRunner
from ..signal_resolver import SignalResolver
from ..skill_manager import SkillManager
from ..app_server_investigation import AppServerInvestigationRunner
from ..requirement_analysis import RequirementAnalysisRunner
from ..source_analysis import RepositorySourceAnalysisRunner
from ..state import AgentActivityStore, ConversationContext, ConversationContextStore, EventStateStore
from ..token_usage import extract_first_prefixed_token_usage, extract_prefixed_token_usage
from ..handlers.signal_lifecycle import SignalLifecycleHandler
from ..workflow_archive import WorkflowArchiver


CHAT_COMMAND_PREFIXES = ("/chat",)
CARD_ACTION_EVENT_KEY = "card.action.trigger"

# Truncation limits for error previews and card notes
MAX_ERROR_PREVIEW = 500
MAX_STDERR_PREVIEW = 300
# Progress stages whose updates are throttled to one card refresh per interval.
_THROTTLED_PROGRESS_STAGE_SUFFIXES = ("_agent_analysis_stream", "_summary_stream")
MAX_CARD_NOTE = 700
LOCAL_DOWNLOAD_AUTH_TERMS = (
    "下载目录",
    "Downloads",
    "downloads",
    "服务器下载目录",
    "本机下载目录",
    "本地下载目录",
)
LOCAL_RESOURCE_NAME_RE = re.compile(
    r"(?<![A-Za-z0-9_./~-])"
    r"([A-Za-z0-9][A-Za-z0-9._-]{0,180}\.(?:zip|7z|tar\.gz|tgz|gz|xz|alog|xlog|log|txt))"
    r"(?![A-Za-z0-9_./~-])",
    re.IGNORECASE,
)


def _contains_any_term(text: str, terms: list[str] | tuple[str, ...]) -> bool:
    lowered = text.casefold()
    return any(term and term.casefold() in lowered for term in terms)


def _looks_like_knowledge_probe_question(text: str, *, intent_terms: list[str]) -> bool:
    cleaned = (text or "").strip()
    if not cleaned:
        return False
    return _contains_any_term(cleaned, intent_terms)


class _BugReanalysisDecision:
    def __init__(
        self,
        should_reanalyze: bool,
        force_rerun: bool = False,
        *,
        plans=None,
        skill_name: str = "",
        skill_label: str = "",
        source: str = "",
        reason: str = "",
        provider: str = "",
    ) -> None:
        self.should_reanalyze = should_reanalyze
        self.force_rerun = force_rerun
        self.plans = plans
        self.skill_name = skill_name
        self.skill_label = skill_label
        self.source = source
        self.reason = reason
        self.provider = provider


_FOLLOWUP_INTENT_TERMS = (
    "基于源码",
    "根据源码",
    "源码",
    "源代码",
    "信号定义",
    "定义",
    "链路",
)

_FOLLOWUP_STOP_TERMS = (
    "这个",
    "这个结论",
    "为什么",
    "怎么",
    "如何",
    "是否",
    "是不是",
    "有没有",
    "请",
    "帮我",
    "继续",
    "刚才",
    "之前",
    "一下",
    "里面",
    "相关",
    "分析",
    "报告",
    "问题",
    "结论",
)

@dataclass
class _IntentPreflightDecision:
    title: str
    intent_label: str
    confidence_label: str
    strategy_label: str
    reason: str
    execute: bool = True
    clarification_result: TaskResult | None = None
    plans_override: list[BugAnalysisPlan] | None = None
    classification_skill: str = ""
    classification_source: str = "preflight_rules"
    classification_reason: str = ""
    source_targets: list[str] | None = None
    source_mode: str = ""

_FAST_EXISTING_ANSWER_MIN_CONFIDENCE = 0.8
_FAST_ANSWER_DOMAIN_STOP_TERMS = {
    "问题",
    "时刻",
    "故障",
    "时间",
    "报告",
    "结论",
    "分析",
    "这个",
    "那个",
    "是否",
    "是不是",
    "为什么",
    "怎么",
    "如何",
    "还是",
    "什么",
    "一下",
    "请问",
    "帮我",
    "看下",
    "看看",
    "继续",
    "之前",
    "已有",
    "当前",
    "主题",
    "状态",
    "白天",
    "白昼",
    "黑夜",
    "夜间",
    "上午",
    "下午",
    "早晨",
    "傍晚",
    "黄昏",
    "是",
    "的",
}
# Alias: previously a smaller set, now unified with _FAST_ANSWER_DOMAIN_STOP_TERMS
_FAST_ANSWER_STOP_TOKENS = _FAST_ANSWER_DOMAIN_STOP_TERMS
_FAST_ANSWER_LATIN_DOMAIN_STOP_TERMS = {
    "the",
    "and",
    "or",
    "for",
    "with",
    "status",
    "state",
    "theme",
    "day",
    "night",
    "true",
    "false",
}


@dataclass
class _RouteContext:
    """Pre-computed state shared across route handlers during event dispatch."""

    event: LarkEvent
    conversation_input: ResolvedConversationInput
    signal_request: SignalRequest | None = None
    bug_request: object = None
    direct_analysis_request: object = None
    app_server_investigation_request: AppServerInvestigationRequest | None = None
    requirement_analysis_request: RequirementAnalysisRequest | None = None
    source_analysis_request: SourceAnalysisRequest | None = None
    report_followup_request: ReportFollowupRequest | None = None
    perception_request: object = None
    rom_version_request: object = None
    addr2line_request: Addr2LineRequest | None = None
    latest_chat_context: ConversationContext | None = None

    @property
    def route_content(self) -> str:
        return self.conversation_input.route_text

    @property
    def followup_context(self):
        return self.conversation_input.followup_context

    @property
    def referenced_resources(self) -> list[DownloadResource]:
        return list(self.conversation_input.referenced_resources)


@dataclass
class RouteCandidate:
    route: str
    band: str
    score: int
    reason: str
    blocked_by: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "route": self.route,
            "band": self.band,
            "score": self.score,
            "reason": self.reason,
            "blocked_by": list(self.blocked_by),
        }


__all__ = [
    'get_logger',
    'logger',
    'Addr2LineRunner',
    'BugAnalysisPlan',
    'BugAnalysisRunner',
    'BugFollowupSelection',
    'ClaudeSkillRunner',
    'IntentAnalysisFailure',
    'IntentAnalysisRunner',
    'OmlxChatClient',
    'PerceptionSummaryRunner',
    'RomVersionLookupRunner',
    'looks_like_scene_signal_request',
    'arbitrate',
    'extract_conclusion',
    'ApprovalStatus',
    'ApprovalStore',
    'build_operation_request',
    'build_agent_reanalysis_confirmation_card',
    'build_confirmation_card',
    'build_followup_result_card',
    'build_knowledge_answer_card',
    'build_result_card',
    'build_status_card',
    'card_to_json',
    'CaseStore',
    'ResolvedConversationInput',
    'LogDownloader',
    'EscalationChecker',
    'NotificationHistory',
    'build_report_ready_notification',
    'build_status_change_notification',
    'HealthMonitor',
    'ProcessWatchdog',
    'KnowledgeService',
    'LarkClient',
    'AnalysisType',
    'LifecycleStore',
    'mode_to_analysis_type',
    'Addr2LineRequest',
    'AppServerInvestigationRequest',
    'BridgeConfig',
    'BotMenuEvent',
    'BugRequest',
    'CardActionEvent',
    'DownloadResource',
    'DirectAnalysisRequest',
    'IntentDecision',
    'LarkEvent',
    'MessageRecalledEvent',
    'ReactionEvent',
    'RequirementAnalysisRequest',
    'ReportFollowupRequest',
    'RomVersionLookupRequest',
    'SignalRequest',
    'SourceAnalysisRequest',
    'TaskResult',
    'create_job_context',
    'build_basic_chat_reply',
    'parse_app_server_investigation_request',
    'build_bug_url_re',
    'extract_first_keyword_payload',
    'find_resources',
    'looks_like_direct_analysis_prompt',
    'parse_report_followup_request',
    'parse_followup_action',
    'parse_claude_skill_request',
    'parse_bug_request',
    'parse_direct_analysis_request',
    'parse_perception_summary_request',
    'parse_requirement_analysis_request',
    'parse_addr2line_request',
    'parse_rom_version_lookup_request',
    'parse_signal_request',
    'parse_source_analysis_request',
    'should_use_omlx_chat',
    'PolicyDecision',
    'build_policy_rejection_message',
    'evaluate_event_policy',
    'HtmlReportPublisher',
    'ReportHttpServer',
    'resolve_bind_host',
    'resolve_public_base_url',
    'render_context_diagram_report',
    'ReportVersionStore',
    'derive_group_key',
    'serialize_resource_status',
    'normalize_replay_mode',
    'ReplayResourceBundle',
    'ReplayPlan',
    'ReplayDecision',
    'AnalysisReplayContext',
    'SignalChainRunner',
    'SignalResolver',
    'SkillManager',
    'AppServerInvestigationRunner',
    'RequirementAnalysisRunner',
    'RepositorySourceAnalysisRunner',
    'AgentActivityStore',
    'ConversationContext',
    'ConversationContextStore',
    'EventStateStore',
    'CodexAppServerTurnController',
    'extract_first_prefixed_token_usage',
    'extract_prefixed_token_usage',
    'SignalLifecycleHandler',
    'WorkflowArchiver',
    'CHAT_COMMAND_PREFIXES',
    'CARD_ACTION_EVENT_KEY',
    'MAX_ERROR_PREVIEW',
    'MAX_STDERR_PREVIEW',
    '_THROTTLED_PROGRESS_STAGE_SUFFIXES',
    'MAX_CARD_NOTE',
    'LOCAL_DOWNLOAD_AUTH_TERMS',
    'LOCAL_RESOURCE_NAME_RE',
    '_contains_any_term',
    '_looks_like_knowledge_probe_question',
    '_BugReanalysisDecision',
    '_FOLLOWUP_INTENT_TERMS',
    '_FOLLOWUP_STOP_TERMS',
    '_IntentPreflightDecision',
    '_FAST_EXISTING_ANSWER_MIN_CONFIDENCE',
    '_FAST_ANSWER_DOMAIN_STOP_TERMS',
    '_FAST_ANSWER_STOP_TOKENS',
    '_FAST_ANSWER_LATIN_DOMAIN_STOP_TERMS',
    '_RouteContext',
    'RouteCandidate',
]
