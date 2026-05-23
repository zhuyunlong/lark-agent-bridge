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

from .log import get_logger

logger = get_logger("app")

from .agents import (
    Addr2LineRunner,
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
from .arbitration import arbitrate, extract_conclusion
from .approval import ApprovalStatus, ApprovalStore, build_operation_request
from .cards import (
    build_agent_reanalysis_confirmation_card,
    build_confirmation_card,
    build_followup_result_card,
    build_knowledge_answer_card,
    build_result_card,
    build_status_card,
    card_to_json,
)
from .case_store import CaseStore
from .downloader import LogDownloader
from .escalation import (
    EscalationChecker,
    NotificationHistory,
    build_report_ready_notification,
    build_status_change_notification,
)
from .health import HealthMonitor, ProcessWatchdog
from .knowledge import KnowledgeService
from .lark_client import LarkClient
from .lifecycle import AnalysisType, LifecycleStore, mode_to_analysis_type
from .models import (
    Addr2LineRequest,
    BridgeConfig,
    CardActionEvent,
    DownloadResource,
    DirectAnalysisRequest,
    IntentDecision,
    LarkEvent,
    RomVersionLookupRequest,
    SignalRequest,
    TaskResult,
    create_job_context,
)
from .parser import (
    build_basic_chat_reply,
    build_bug_url_re,
    extract_first_keyword_payload,
    find_resources,
    looks_like_direct_analysis_prompt,
    parse_followup_action,
    parse_claude_skill_request,
    parse_bug_request,
    parse_direct_analysis_request,
    parse_perception_summary_request,
    parse_addr2line_request,
    parse_rom_version_lookup_request,
    parse_signal_request,
    should_use_omlx_chat,
)
from .policy import PolicyDecision, build_policy_rejection_message, evaluate_event_policy
from .report_server import HtmlReportPublisher, ReportHttpServer, resolve_bind_host, resolve_public_base_url
from .report_version import ReportVersionStore, derive_group_key
from .runner import SignalChainRunner
from .signal_resolver import SignalResolver
from .skill_manager import SkillManager
from .state import AgentActivityStore, ConversationContext, ConversationContextStore, EventStateStore
from .handlers.signal_lifecycle import SignalLifecycleHandler
from .workflow_archive import WorkflowArchiver


CHAT_COMMAND_PREFIXES = ("/chat",)
CARD_ACTION_EVENT_KEY = "card.action.trigger"

# Truncation limits for error previews and card notes
MAX_ERROR_PREVIEW = 500
MAX_STDERR_PREVIEW = 300
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
    route_content: str
    followup_context: ConversationContext | None = None
    signal_request: SignalRequest | None = None
    bug_request: object = None
    direct_analysis_request: object = None
    perception_request: object = None
    rom_version_request: object = None
    addr2line_request: Addr2LineRequest | None = None
    referenced_resources: list[DownloadResource] = field(default_factory=list)
    latest_chat_context: ConversationContext | None = None


class BridgeApp:
    def __init__(
        self,
        config: BridgeConfig,
        *,
        lark_client: LarkClient | None = None,
        state_store: EventStateStore | None = None,
        handler: SignalLifecycleHandler | None = None,
        claude_runner: ClaudeSkillRunner | None = None,
        bug_runner: BugAnalysisRunner | None = None,
        perception_runner: PerceptionSummaryRunner | None = None,
        rom_version_runner: RomVersionLookupRunner | None = None,
        addr2line_runner: Addr2LineRunner | None = None,
        chat_client: OmlxChatClient | None = None,
        intent_runner: IntentAnalysisRunner | None = None,
        knowledge_service: KnowledgeService | None = None,
        report_publisher: HtmlReportPublisher | None = None,
        report_http_server: ReportHttpServer | None = None,
        conversation_store: ConversationContextStore | None = None,
        activity_store: AgentActivityStore | None = None,
        progress_callback: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        self.config = config
        self.lark_client = lark_client or LarkClient(config)
        self.state_store = state_store or EventStateStore(config.data_dir / "state" / "seen_events.jsonl")
        self.conversation_store = conversation_store or ConversationContextStore(
            config.data_dir / "state" / "conversation_contexts.json",
            max_history_turns=config.omlx_chat.followup_max_history_turns,
        )
        self.activity_store = activity_store or AgentActivityStore(config.data_dir / "state" / "agent_activity.json")
        self.progress_callback = progress_callback
        self._progress_cards: dict[str, dict[str, object]] = {}
        self._progress_cards_max_age_seconds = 7200  # 2 hour TTL (must exceed bug_analysis timeout)
        self.process_watchdog = ProcessWatchdog()
        self.health_monitor = HealthMonitor(data_dir=config.data_dir, process_watchdog=self.process_watchdog)
        self._restore_daemon_health_pid()
        self.case_store = CaseStore(config.data_dir / "state" / "cases.json")
        self.skill_manager = SkillManager(config)
        self.approval_store = ApprovalStore(config.data_dir / "state" / "approvals.json")
        self.version_store = ReportVersionStore(config.data_dir / "state" / "report_versions.json")
        self.workflow_archiver = WorkflowArchiver(config, self.lark_client)
        self.signal_resolver = SignalResolver(
            config.guideengine_repo,
            cache_dir=config.data_dir / "cache",
        )
        self.bug_url_re = build_bug_url_re(config.bug_url_domains) if config.bug_url_domains else None
        self.escalation_checker = EscalationChecker()
        self.notification_history = NotificationHistory(config.data_dir / "state" / "notification_history.json")
        self.lifecycle_store = LifecycleStore()
        self.report_publisher = report_publisher or HtmlReportPublisher(config)
        self.knowledge_service = knowledge_service or KnowledgeService(config)
        self.report_http_server = report_http_server or ReportHttpServer(
            config,
            activity_store=self.activity_store,
            case_store=self.case_store,
            skill_manager=self.skill_manager,
            health_monitor=self.health_monitor,
            process_watchdog=self.process_watchdog,
            conversation_store=self.conversation_store,
            version_store=self.version_store,
            knowledge_service=self.knowledge_service,
        )
        runner = SignalChainRunner(config, process_watchdog=self.process_watchdog)
        downloader = LogDownloader(config, self.lark_client)
        self.handler = handler or SignalLifecycleHandler(config, downloader, runner)
        self.claude_runner = claude_runner or ClaudeSkillRunner(config, process_watchdog=self.process_watchdog)
        self.bug_runner = bug_runner or BugAnalysisRunner(
            config,
            process_watchdog=self.process_watchdog,
            lark_client=self.lark_client,
            skill_manager=self.skill_manager,
        )
        self.perception_runner = perception_runner or PerceptionSummaryRunner(
            config,
            self.lark_client,
            process_watchdog=self.process_watchdog,
        )
        self.rom_version_runner = rom_version_runner or RomVersionLookupRunner(
            config,
            process_watchdog=self.process_watchdog,
        )
        self.addr2line_runner = addr2line_runner or Addr2LineRunner(
            config,
            lark_client=self.lark_client,
            process_watchdog=self.process_watchdog,
        )
        self.chat_client = chat_client or OmlxChatClient(config)
        self.intent_runner = intent_runner or IntentAnalysisRunner(config, process_watchdog=self.process_watchdog)

    def check(self) -> dict[str, object]:
        health = self.health_monitor.check_health()
        return {
            "dry_run": self.config.dry_run,
            "data_dir": str(self.config.data_dir),
            "guideengine_repo": str(self.config.guideengine_repo),
            "lark": self.lark_client.check_environment(),
            "report_server": {
                "enabled": self.config.report_server.enabled,
                "bind_host": resolve_bind_host(self.config.report_server.bind_host),
                "port": self.config.report_server.port,
                "public_base_url": self.report_publisher.public_base_url,
            },
            "event_consumer": {
                "event_key": self.config.event_consumer.event_key,
                "ready_timeout_seconds": self.config.event_consumer.ready_timeout_seconds,
                "restart_on_failure": self.config.event_consumer.restart_on_failure,
                "max_restarts": self.config.event_consumer.max_restarts,
                "status": self.activity_store.get_daemon_status(),
            },
            "intent_analysis": {
                "enabled": self.intent_runner.is_enabled(),
                "provider": self.config.intent_analysis.provider or self.config.bug_analysis.provider,
                "command": self.config.intent_analysis.command or self.config.bug_analysis.command,
            },
            "health": health.to_dict(),
            "approval": {
                "enabled": self.config.approval.enabled,
                "pending": len(self.approval_store.list_pending()),
            },
            "workflow_archive": {
                "enabled": self.config.workflow_archive.enabled,
                "base_configured": bool(self.config.workflow_archive.base_token and self.config.workflow_archive.table_id),
                "drive_configured": bool(self.config.workflow_archive.drive_folder_token),
                "doc_parent_token_configured": bool(self.config.workflow_archive.doc_parent_token),
            },
            "notifications": {
                "enabled": self.config.notifications.enabled,
                "report_ready": self.config.notifications.report_ready,
            },
            "dual_agent": {
                "enabled": self.config.dual_agent.enabled,
            },
            "knowledge": {
                "enabled": self.config.knowledge.enabled,
                "storage": str(self.config.knowledge.storage),
                "source_count": len(self.config.knowledge.sources),
            },
        }

    def handle_payload(self, payload: dict[str, object]) -> TaskResult:
        if self._looks_like_card_action_payload(payload):
            return self.handle_card_action_payload(payload)
        return self.handle_event_payload(payload)

    def handle_event_payload(self, payload: dict[str, object]) -> TaskResult:
        return self.handle_event(LarkEvent.from_dict(payload))

    def handle_card_action_payload(self, payload: dict[str, object]) -> TaskResult:
        return self.handle_card_action(CardActionEvent.from_dict(payload))

    def _looks_like_card_action_payload(self, payload: dict[str, object]) -> bool:
        event_body = payload.get("event") or payload
        if not isinstance(event_body, dict):
            return False
        action = event_body.get("action")
        if isinstance(action, dict):
            value = action.get("value")
            if isinstance(value, dict) and value.get("action"):
                return True
        value = payload.get("value")
        return isinstance(value, dict) and bool(value.get("action"))

    def handle_card_action(self, action_event: CardActionEvent) -> TaskResult:
        action = action_event.action.strip()
        if action in {"approve", "reject"}:
            return self._handle_approval_action(action_event, approved=(action == "approve"))
        if action == "answer_from_report":
            return self._handle_answer_from_report_action(action_event)
        if action == "reanalyze":
            return self._handle_reanalyze_action(action_event)
        if action == "continue_agent":
            return self._handle_continue_agent_action(action_event)
        if action == "select_bug_skill":
            return self._handle_select_bug_skill_action(action_event)
        if action == "select_bug_agent":
            return self._handle_select_bug_agent_action(action_event)
        if action == "confirm_bug_agent_reanalysis":
            return self._handle_confirm_bug_agent_reanalysis_action(action_event)
        if action == "cancel_bug_agent_reanalysis":
            return self._handle_cancel_bug_agent_reanalysis_action(action_event)
        if action in {"feedback_helpful", "feedback_unhelpful"}:
            return self._handle_feedback_action(action_event)
        if action == "escalate":
            return self._handle_escalate_action(action_event)
        return TaskResult(
            success=False,
            message=f"不支持的卡片操作：{action or '(empty)'}",
            error_code="unsupported_card_action",
            details={"mode": "card_action", "action": action},
        )

    def handle_event(self, event: LarkEvent) -> TaskResult:
        self.activity_store.record_event(event)
        self.health_monitor.record_event_processed()
        try:
            result = self._handle_event(event)
        except Exception as exc:
            self.activity_store.record_error(event, exc)
            raise
        self.activity_store.record_result(event, result)
        return result

    def _handle_event(self, event: LarkEvent) -> TaskResult:
        self.cleanup_expired_jobs()

        # Phase 1: Group mention filtering
        route_content = event.content
        followup_context = None
        if event.chat_type == "group":
            addressed_content = self._strip_group_chat_mention(event.content)
            if addressed_content is None:
                followup_context = self._resolve_followup_context(event)
                if followup_context is not None:
                    addressed_content = self._strip_bot_mention_anywhere(event.content)
                    if addressed_content is None and self._allows_reply_chain_without_mention(followup_context):
                        addressed_content = event.content.strip()
            if addressed_content is None:
                if not self.state_store.mark_seen(event):
                    return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)
                return TaskResult(
                    success=True,
                    message="group message not addressed to this bot",
                    skipped=True,
                    details={"mode": "not_addressed"},
                )
            route_content = addressed_content

        # Phase 2: Policy gate
        decision = evaluate_event_policy(self.config, event)
        if (
            not decision.allowed
            and decision.reason == "chat_not_allowed"
            and followup_context is None
        ):
            followup_context = self._resolve_followup_context(event)
        referenced_resources = self._fetch_referenced_message_resources(
            event,
            route_content=route_content,
            force_current_lookup=True,
        )
        signal_request = self._build_signal_request(route_content, referenced_resources)
        rom_version_request = parse_rom_version_lookup_request(route_content)
        addr2line_request = self._build_addr2line_request(route_content, event, referenced_resources)
        bug_request = parse_bug_request(route_content, bug_url_re=self.bug_url_re)
        if bug_request.triggered and signal_request.error == "missing_signal":
            signal_request = SignalRequest(
                signal="",
                resources=signal_request.resources,
                since=signal_request.since,
                raw_text=signal_request.raw_text,
                triggered=False,
            )
        direct_analysis_request = self._build_direct_analysis_request(route_content, referenced_resources, event=event)
        perception_request = self._build_perception_summary_request(route_content, referenced_resources)
        if (
            not decision.allowed
            and decision.reason == "chat_not_allowed"
            and self._allow_log_analysis_in_external_group(
                event,
                followup_context=followup_context,
                signal_request=signal_request,
                bug_request=bug_request,
                direct_analysis_request=direct_analysis_request,
                perception_request=perception_request,
                addr2line_request=addr2line_request,
            )
        ):
            decision = PolicyDecision(True, "group_log_analysis_allowed")
        if not decision.allowed:
            if not self.state_store.mark_seen(event):
                return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)
            result = TaskResult(
                success=False,
                message=build_policy_rejection_message(decision),
                error_code=decision.reason,
            )
            if not self.config.dry_run and event.chat_type in {"group", "p2p"}:
                self._send_result(event, result)
            return result

        # Phase 3: Build route context and dispatch through ordered handlers
        if followup_context is None:
            followup_context = self._resolve_followup_context(event)
        latest_chat_context = self._latest_analysis_context(
            event.chat_id,
            explicit_followup_context=followup_context,
        )
        ctx = _RouteContext(
            event=event,
            route_content=route_content,
            followup_context=followup_context,
            signal_request=signal_request,
            bug_request=bug_request,
            direct_analysis_request=direct_analysis_request,
            perception_request=perception_request,
            rom_version_request=rom_version_request,
            addr2line_request=addr2line_request,
            referenced_resources=referenced_resources,
            latest_chat_context=latest_chat_context,
        )
        return self._dispatch_route(ctx)

    def _dispatch_route(self, ctx: _RouteContext) -> TaskResult:
        """Try each route handler in priority order; first match wins."""
        # Route handlers ordered by priority (highest first).
        # Each returns TaskResult on match, or None to fall through.
        _ROUTE_HANDLERS = [
            self._route_bug_followup,       # 1. Bug followup conversation
            self._route_bug_intent,         # 2. Explicit bug analysis request
            self._route_addr2line_resolve,  # 3. Native stack address reverse lookup
            self._route_rom_version_lookup, # 4. ROM version lookup
            self._route_scene_signal,       # 5. Scene signal shortcut
            self._route_knowledge_qa,       # 6. Personal knowledge QA / ADB templates
            self._route_signal_request,     # 7. Signal lifecycle analysis
            self._route_claude_skill,       # 8. Optional configured local skill route
            self._route_bug_request,        # 9. Bug request (secondary match)
            self._route_perception,         # 10. Perception summary
            self._route_direct_analysis,    # 11. Direct file/log analysis
            self._route_followup_intent,    # 12. Followup intent keywords
            self._route_general_followup,   # 13. General followup conversation
            self._route_knowledge_probe,    # 14. Internal operation QA from knowledge before chat
            self._route_stale_light_interaction,  # 15. Replayed old lightweight messages
            self._route_basic_chat,         # 16. Deterministic help/identity replies
            self._route_omlx_chat,          # 17. OMLX chat conversation
            self._route_intent_router,      # 18. Intent fallback for unresolved tasks
        ]
        for handler in _ROUTE_HANDLERS:
            result = handler(ctx)
            if result is not None:
                return result

        # Fallback: unsupported request
        if not self.state_store.mark_seen(ctx.event):
            return TaskResult(True, f"duplicate event skipped: {ctx.event.event_id}", skipped=True)
        result = TaskResult(
            success=True,
            message="not a handled request",
            skipped=True,
            details={"mode": "unsupported"},
        )
        self._send_result(ctx.event, result)
        return result

    # --- Individual route handlers (ordered by priority) ---

    def _route_bug_followup(self, ctx: _RouteContext) -> TaskResult | None:
        if (
            ctx.followup_context is not None
            and "bug" in str(ctx.followup_context.mode).casefold()
            and ctx.signal_request is not None
            and not ctx.signal_request.triggered
        ):
            return self._handle_followup(ctx.event, ctx.route_content, ctx.followup_context)
        return None

    def _route_bug_intent(self, ctx: _RouteContext) -> TaskResult | None:
        if ctx.bug_request is not None and getattr(ctx.bug_request, "triggered", False):
            return self._handle_bug_intent(ctx.event, ctx.route_content)
        return None

    def _route_addr2line_resolve(self, ctx: _RouteContext) -> TaskResult | None:
        request = ctx.addr2line_request
        if request is None:
            return None
        if not request.triggered:
            request = self._followup_addr2line_request(ctx)
            if request is None:
                return None
        if not self.state_store.mark_seen(ctx.event):
            return TaskResult(True, f"duplicate event skipped: {ctx.event.event_id}", skipped=True)
        return self._run_addr2line_resolve_request(ctx.event, request)

    def _route_rom_version_lookup(self, ctx: _RouteContext) -> TaskResult | None:
        request = ctx.rom_version_request
        if request is None:
            return None
        if not getattr(request, "triggered", False):
            request = self._followup_rom_lookup_request(ctx)
            if request is None:
                return None
        if not self.state_store.mark_seen(ctx.event):
            return TaskResult(True, f"duplicate event skipped: {ctx.event.event_id}", skipped=True)
        return self._run_rom_version_lookup_request(ctx.event, request)

    def _route_intent_router(self, ctx: _RouteContext) -> TaskResult | None:
        if not self.intent_runner.is_enabled():
            return None
        return self._handle_intent_routed_event(
            ctx.event,
            ctx.route_content,
            explicit_followup_context=ctx.followup_context,
            latest_chat_context=ctx.latest_chat_context,
            referenced_resources=ctx.referenced_resources,
        )

    def _route_general_followup(self, ctx: _RouteContext) -> TaskResult | None:
        if ctx.followup_context is not None:
            return self._handle_followup(ctx.event, ctx.route_content, ctx.followup_context)
        return None

    def _route_stale_light_interaction(self, ctx: _RouteContext) -> TaskResult | None:
        stale = self._stale_light_interaction_details(ctx)
        if stale is None:
            return None
        if not self.state_store.mark_seen(ctx.event):
            return TaskResult(True, f"duplicate event skipped: {ctx.event.event_id}", skipped=True)
        return TaskResult(
            success=True,
            message="stale lightweight event skipped",
            skipped=True,
            details={
                "mode": "stale_light_interaction",
                **stale,
            },
        )

    def _route_scene_signal(self, ctx: _RouteContext) -> TaskResult | None:
        if (
            ctx.signal_request is not None
            and ctx.signal_request.triggered
            and not ctx.signal_request.signal
            and looks_like_scene_signal_request(ctx.route_content)
        ):
            return self._handle_direct_analysis_intent(ctx.event, ctx.route_content, referenced_resources=ctx.referenced_resources)
        return None

    def _route_signal_request(self, ctx: _RouteContext) -> TaskResult | None:
        request = ctx.signal_request
        if request is None or not request.triggered:
            return None
        if not request.resources:
            inherited_resources = self._contextual_signal_resources(
                ctx.event,
                ctx.route_content,
                explicit_followup_context=ctx.followup_context,
                latest_chat_context=ctx.latest_chat_context,
            )
            if inherited_resources:
                request = SignalRequest(
                    signal=request.signal,
                    resources=self._merge_resources(request.resources, inherited_resources),
                    since=request.since,
                    raw_text=request.raw_text,
                    triggered=request.triggered,
                    error=request.error,
                )
        if not self.state_store.mark_seen(ctx.event):
            return TaskResult(True, f"duplicate event skipped: {ctx.event.event_id}", skipped=True)
        return self._run_signal_request(ctx.event, request, ctx.route_content)

    def _route_knowledge_qa(self, ctx: _RouteContext) -> TaskResult | None:
        if not self.config.knowledge.enabled:
            return None
        if not self.knowledge_service.should_handle(ctx.route_content):
            return None
        if ctx.referenced_resources:
            return None
        if not self.state_store.mark_seen(ctx.event):
            return TaskResult(True, f"duplicate event skipped: {ctx.event.event_id}", skipped=True)
        result = self.knowledge_service.answer(ctx.route_content)
        details = dict(result.details)
        details["delivery"] = "reply"
        details["conversation_root_message_id"] = ctx.event.root_id or ctx.event.message_id
        result.details = details
        return self._deliver_result(ctx.event, result, request_text=ctx.route_content)

    def _route_knowledge_probe(self, ctx: _RouteContext) -> TaskResult | None:
        knowledge_options = self.config.knowledge
        if not knowledge_options.enabled or not knowledge_options.auto_probe_enabled:
            return None
        if ctx.referenced_resources:
            return None
        if build_basic_chat_reply(ctx.route_content, command_prefixes=self.config.command_prefixes) is not None:
            return None
        if not _looks_like_knowledge_probe_question(
            ctx.route_content,
            intent_terms=knowledge_options.auto_probe_intent_terms,
        ):
            return None
        hits = self.knowledge_service.search(ctx.route_content, limit=1)
        usable_hits = [hit for hit in hits if hit.score >= knowledge_options.auto_probe_min_score]
        if not usable_hits:
            low_confidence_answer = getattr(self.knowledge_service, "answer_low_confidence_candidates", None)
            if callable(low_confidence_answer):
                result = low_confidence_answer(ctx.route_content)
                if result is not None:
                    if not self.state_store.mark_seen(ctx.event):
                        return TaskResult(True, f"duplicate event skipped: {ctx.event.event_id}", skipped=True)
                    details = dict(result.details)
                    details["delivery"] = "reply"
                    details["conversation_root_message_id"] = ctx.event.root_id or ctx.event.message_id
                    result.details = details
                    return self._deliver_result(ctx.event, result, request_text=ctx.route_content)
            if not _contains_any_term(ctx.route_content, knowledge_options.auto_probe_no_hit_terms):
                return None
            if not self.state_store.mark_seen(ctx.event):
                return TaskResult(True, f"duplicate event skipped: {ctx.event.event_id}", skipped=True)
            result = TaskResult(
                success=False,
                message=f"知识库未命中：{ctx.route_content}\n请补充更具体的关键词，或先同步/新增对应知识。",
                error_code="knowledge_probe_no_hits",
                details={"mode": "knowledge_probe", "knowledge_hits": []},
            )
            result.details["delivery"] = "reply"
            result.details["conversation_root_message_id"] = ctx.event.root_id or ctx.event.message_id
            return self._deliver_result(ctx.event, result, request_text=ctx.route_content)
        if not self.state_store.mark_seen(ctx.event):
            return TaskResult(True, f"duplicate event skipped: {ctx.event.event_id}", skipped=True)
        result = self.knowledge_service.answer(ctx.route_content)
        details = dict(result.details)
        details["delivery"] = "reply"
        details["conversation_root_message_id"] = ctx.event.root_id or ctx.event.message_id
        result.details = details
        return self._deliver_result(ctx.event, result, request_text=ctx.route_content)

    def _route_claude_skill(self, ctx: _RouteContext) -> TaskResult | None:
        skill_request = parse_claude_skill_request(
            ctx.route_content,
            trigger_prefixes=self.config.claude_agent.trigger_prefixes,
        )
        if not skill_request.triggered:
            return None
        if not self.state_store.mark_seen(ctx.event):
            return TaskResult(True, f"duplicate event skipped: {ctx.event.event_id}", skipped=True)
        result = self.claude_runner.run_skill_analysis(skill_request, event=ctx.event)
        return self._deliver_result(ctx.event, result, request_text=skill_request.raw_text or ctx.route_content)

    def _route_bug_request(self, ctx: _RouteContext) -> TaskResult | None:
        if ctx.bug_request is None or not getattr(ctx.bug_request, "triggered", False):
            return None
        if not self.state_store.mark_seen(ctx.event):
            return TaskResult(True, f"duplicate event skipped: {ctx.event.event_id}", skipped=True)
        pending = self._maybe_request_approval(
            ctx.event,
            operation_type="bug_analysis",
            description="Bug 分析",
            route_content=ctx.route_content,
            bug_url=ctx.bug_request.bug_url,
            prompt=ctx.bug_request.prompt,
            estimated_duration_seconds=self.config.bug_analysis.timeout_seconds,
        )
        if pending is not None:
            return pending
        return self._run_bug_request(ctx.event, ctx.bug_request, ctx.route_content)

    def _route_direct_analysis(self, ctx: _RouteContext) -> TaskResult | None:
        if ctx.direct_analysis_request is None or not getattr(ctx.direct_analysis_request, "triggered", False):
            return None
        if self._should_defer_direct_analysis_to_intent(ctx):
            return None
        if not self.state_store.mark_seen(ctx.event):
            return TaskResult(True, f"duplicate event skipped: {ctx.event.event_id}", skipped=True)
        pending = self._maybe_request_approval(
            ctx.event,
            operation_type="direct_analysis",
            description="直传文件分析",
            route_content=ctx.route_content,
            file_count=len(ctx.direct_analysis_request.resources),
            prompt=ctx.direct_analysis_request.prompt,
            estimated_duration_seconds=self.config.bug_analysis.timeout_seconds,
        )
        if pending is not None:
            return pending
        return self._run_direct_analysis_request(ctx.event, ctx.direct_analysis_request, ctx.route_content)

    def _should_defer_direct_analysis_to_intent(self, ctx: _RouteContext) -> bool:
        if not self.intent_runner.is_enabled():
            return False
        if not ctx.referenced_resources:
            return False
        inline_request = parse_direct_analysis_request(ctx.route_content)
        if inline_request.triggered:
            return False
        if looks_like_direct_analysis_prompt(ctx.route_content, resources_present=False, bug_url_re=self.bug_url_re):
            return False
        return True

    def _route_perception(self, ctx: _RouteContext) -> TaskResult | None:
        if ctx.perception_request is None or not getattr(ctx.perception_request, "triggered", False):
            return None
        if not self.state_store.mark_seen(ctx.event):
            return TaskResult(True, f"duplicate event skipped: {ctx.event.event_id}", skipped=True)
        return self._run_perception_request(ctx.event, ctx.perception_request, ctx.route_content)

    def _route_followup_intent(self, ctx: _RouteContext) -> TaskResult | None:
        if not self._is_followup_intent(ctx.route_content):
            return None
        if ctx.followup_context is not None:
            return self._handle_followup(ctx.event, ctx.route_content, ctx.followup_context)
        if not self.state_store.mark_seen(ctx.event):
            return TaskResult(True, f"duplicate event skipped: {ctx.event.event_id}", skipped=True)
        result = self._missing_followup_reply_result(chat_type=ctx.event.chat_type)
        if not self.config.dry_run and ctx.event.chat_type in {"group", "p2p"}:
            self._send_result(ctx.event, result)
        return result

    def _route_omlx_chat(self, ctx: _RouteContext) -> TaskResult | None:
        chat_prompt = self._omlx_prompt(ctx.event, ctx.route_content)
        if chat_prompt is None:
            return None
        chat_reply = build_basic_chat_reply(ctx.route_content, command_prefixes=self.config.command_prefixes)
        if chat_reply is not None:
            return None  # Let _route_basic_chat handle it
        if not self.state_store.mark_seen(ctx.event):
            return TaskResult(True, f"duplicate event skipped: {ctx.event.event_id}", skipped=True)
        result = self.chat_client.reply(chat_prompt)
        return self._deliver_result(ctx.event, result, request_text=ctx.route_content)

    def _route_basic_chat(self, ctx: _RouteContext) -> TaskResult | None:
        chat_reply = build_basic_chat_reply(ctx.route_content, command_prefixes=self.config.command_prefixes)
        if chat_reply is None:
            return None
        if not self.state_store.mark_seen(ctx.event):
            return TaskResult(True, f"duplicate event skipped: {ctx.event.event_id}", skipped=True)
        result = TaskResult(
            success=True,
            message=chat_reply,
            details={"mode": "basic_chat"},
        )
        return self._deliver_result(ctx.event, result, request_text=ctx.route_content)

    def run_signal(self, *, signal: str, log_path: str | Path, since: str | None = None) -> TaskResult:
        self.cleanup_expired_jobs()
        context = create_job_context(self.config.data_dir, job_id=f"manual_{signal}")
        request = SignalRequest(
            signal=signal,
            resources=[DownloadResource(kind="local", value=str(log_path))],
            since=since,
            triggered=True,
        )
        runner = SignalChainRunner(self.config)
        result = runner.run(signal=signal, log_path=log_path, output_dir=context.output_dir, since=since)
        result.job_id = context.job_id
        result.job_dir = context.job_dir
        if result.success:
            result.message = (
                f"{'dry-run 计划' if self.config.dry_run else '处理完成'}: signal {request.signal}\n"
                f"日志路径: {log_path}\n"
                f"HTML 报告: {result.html_report}\n"
                f"JSON 报告: {result.json_report}"
            )
        return result

    def purge_all_jobs(self) -> int:
        jobs_root = self._jobs_root()
        removed = 0
        if jobs_root.exists():
            for job_dir in jobs_root.iterdir():
                if not job_dir.is_dir():
                    continue
                if self._remove_job_dir(job_dir):
                    removed += 1
        removed += self.report_publisher.purge_all_reports()
        return removed

    def cleanup_expired_jobs(self, *, now: datetime | None = None) -> int:
        retention = self.config.job_retention
        if not retention.enabled:
            return 0
        reference_time = now or datetime.now(timezone.utc)
        removed = 0
        cleanup_bug_cache = getattr(self.bug_runner, "cleanup_expired_bug_cache", None)
        if callable(cleanup_bug_cache):
            removed += cleanup_bug_cache(
                max_age_hours=retention.bug_cache_max_age_hours,
                now=reference_time,
            )
        return removed

    def run_health_maintenance(self) -> list[dict[str, object]]:
        terminated = self.process_watchdog.terminate_stuck()
        if terminated:
            self._notify_progress(
                "stuck_process_cleanup",
                f"清理卡住的 agent 子进程 {len(terminated)} 个",
                session_id="daemon",
                terminated=terminated,
            )
        return terminated

    def start_report_server(self) -> None:
        self.report_http_server.start()

    def stop_report_server(self) -> None:
        self.report_http_server.stop()

    def record_daemon_status(self, payload: dict[str, object]) -> None:
        self.activity_store.record_daemon_status(payload)
        pid = self._coerce_pid(payload.get("process_id"))
        if pid is not None:
            self.health_monitor.set_event_consumer_pid(pid)
        if self.progress_callback is not None:
            self.progress_callback(payload)

    def _restore_daemon_health_pid(self) -> None:
        pid = self._coerce_pid(self.activity_store.get_daemon_status().get("process_id"))
        if pid is not None:
            self.health_monitor.set_event_consumer_pid(pid)

    def _coerce_pid(self, value: object) -> int | None:
        try:
            return int(value) if value is not None and str(value).strip() else None
        except (TypeError, ValueError):
            return None

    def _deliver_result(
        self,
        event: LarkEvent,
        result: TaskResult,
        *,
        request_text: str,
        root_message_id: str | None = None,
    ) -> TaskResult:
        finalized = self._prepare_delivery_result(event, result, request_text=request_text, root_message_id=root_message_id)
        self._send_result(event, finalized)
        self._maybe_send_report_ready_notification(event, finalized)
        return finalized

    def _send_result(self, event: LarkEvent, result: TaskResult) -> None:
        if self.config.dry_run:
            return
        if event.chat_type not in {"group", "p2p"}:
            return
        delivery = str(result.details.get("delivery", "")).strip() or "send"
        session_id = str(result.details.get("conversation_root_message_id") or "").strip() or None

        has_progress_card = self._has_progress_card(event, session_id=session_id)
        card_sent = has_progress_card
        if not has_progress_card:
            # Try sending a structured card for results that have a report
            card_sent = self._try_send_result_card(event, result, delivery=delivery, session_id=session_id)

        if not card_sent:
            self._notify_progress(
                "reply_sending",
                "发送文字回复",
                event=event,
                session_id=session_id,
                success=result.success,
                mode=result.details.get("mode", ""),
                delivery=delivery,
            )
            if delivery == "reply" and event.message_id:
                send_result = self.lark_client.reply(event.message_id, self._reply_payload(event, result.message))
            else:
                send_result = self.lark_client.send_response(event, result.message)
            self._remember_delivery_alias_from_result(send_result, root_message_id=session_id)

        if not result.success:
            if has_progress_card:
                if not self._finish_progress_card(event, result, session_id=session_id):
                    if event.message_id:
                        self.lark_client.reply(event.message_id, self._reply_payload(event, result.message))
                    else:
                        self.lark_client.send_response(event, result.message)
            return
        for path in result.details.get("files_to_send", []):
            self._notify_progress(
                "file_uploading",
                f"上传结果文件 {Path(path).name}",
                event=event,
                session_id=session_id,
                path=str(path),
            )
            send_result = self.lark_client.send_file_response(event, Path(path))
            if send_result is None:
                continue
            if send_result.returncode != 0:
                self._notify_progress(
                    "file_upload_failed",
                    f"上传结果文件失败 {Path(path).name}",
                    event=event,
                    session_id=session_id,
                    path=str(path),
                    stderr=(send_result.stderr or send_result.stdout or "unknown error")[:MAX_ERROR_PREVIEW],
                )
                self.lark_client.send_response(
                    event,
                    f"附件发送失败：{Path(path).name}\n原因：{(send_result.stderr or send_result.stdout or 'unknown error')[:MAX_ERROR_PREVIEW]}",
                )
            else:
                self._remember_delivery_alias_from_result(send_result, root_message_id=session_id)
                self._notify_progress(
                    "file_uploaded",
                    f"上传结果文件完成 {Path(path).name}",
                    event=event,
                    session_id=session_id,
                    path=str(path),
                )

        if has_progress_card and not self._finish_progress_card(event, result, session_id=session_id):
            if delivery == "reply" and event.message_id:
                self.lark_client.reply(event.message_id, self._reply_payload(event, result.message))
            else:
                self.lark_client.send_response(event, result.message)

    def _maybe_send_report_ready_notification(self, event: LarkEvent, result: TaskResult) -> None:
        if not self.config.notifications.enabled or not self.config.notifications.report_ready:
            return
        if self.config.dry_run or not result.success:
            return
        report_url = str(result.details.get("published_report_url") or "").strip()
        if not report_url:
            return
        notification = build_report_ready_notification(
            report_url=report_url,
            summary=result.message,
            chat_id=event.chat_id,
            job_id=result.job_id or "",
        )
        if not self.notification_history.should_send(notification):
            return
        self.notification_history.record(notification)
        self.lark_client.send_response(event, f"{notification.title}\n{notification.message}")

    def _try_send_result_card(
        self,
        event: LarkEvent,
        result: TaskResult,
        *,
        delivery: str,
        session_id: str | None,
    ) -> bool:
        """Attempt to send a result card. Returns True if card was sent."""
        report_url = str(result.details.get("published_report_url") or result.details.get("report_url") or "").strip()
        mode = str(result.details.get("mode", ""))
        is_skill_clarification = mode == "bug_clarification" and bool(result.details.get("needs_user_direction"))
        # Most result cards need a published report; skill clarification and knowledge QA are card-only decision points.
        if not report_url and not is_skill_clarification and mode != "knowledge_qa":
            return False

        mode_labels = {
            "bug_analysis": "Bug 分析",
            "bug_clarification": "Bug 分析分诊",
            "bug_reanalysis": "Bug 重新分析",
            "bug_followup_existing_answer": "Bug 追问",
            "bug_agent_followup": "Bug 追问",
            "direct_analysis": "直传文件分析",
            "perception_summary": "感知数据总结",
            "signal_lifecycle": "信号生命周期",
            "claude_skill": "Claude Code 分析",
            "knowledge_qa": "知识库回答",
        }
        title = mode_labels.get(mode, mode or "分析结果")

        metadata: dict[str, str] = {}
        if mode:
            metadata["分析类型"] = title
        if result.job_id:
            metadata["任务ID"] = result.job_id[:20]
        provider = str(result.details.get("agent_summary_provider") or result.details.get("provider") or "").strip()
        if provider:
            metadata["Agent 类型"] = provider
        total_tokens = result.details.get("agent_summary_total_tokens")
        if isinstance(total_tokens, int):
            metadata["Agent Token"] = str(total_tokens)
        skill_label = str(result.details.get("analysis_skill_label") or "").strip()
        skill_name = str(result.details.get("analysis_skill") or "").strip()
        if skill_label or skill_name:
            metadata["命中 Skill"] = skill_label or skill_name
        classification_source = str(result.details.get("classification_source") or "").strip()
        if classification_source:
            metadata["分类来源"] = classification_source

        root_message_id = session_id or event.root_id or event.message_id
        card_actions_enabled = self._card_actions_enabled()
        if is_skill_clarification:
            card = build_status_card(
                title=title,
                status="completed",
                details=metadata if metadata else None,
                note=result.message,
                job_id=result.job_id,
                root_message_id=root_message_id,
                bug_skill_choices=self._result_bug_skill_choices(result) if card_actions_enabled else [],
                bug_skill_choice_note=self._result_bug_skill_choice_note(result) if card_actions_enabled else None,
            )
        elif self._result_is_bug_report_mode(result):
            card = build_status_card(
                title=title,
                status="completed" if result.success else "failed",
                details=metadata if metadata else None,
                note=result.message,
                report_url=report_url or None,
                job_id=result.job_id,
                root_message_id=root_message_id,
                elapsed_seconds=result.duration_seconds,
                token_usage=self._progress_token_usage(result),
                show_followup_actions=bool(card_actions_enabled and result.success and report_url and root_message_id),
                bug_skill_choices=self._result_bug_skill_choices(result) if card_actions_enabled else [],
                bug_skill_choice_note=self._result_bug_skill_choice_note(result) if card_actions_enabled else None,
                bug_agent_choices=self._result_bug_agent_choices(result) if card_actions_enabled else [],
            )
        elif mode in {"bug_followup_existing_answer", "bug_agent_followup", "bug_reanalysis"}:
            confidence_value = result.details.get("answer_confidence")
            answer_confidence = float(confidence_value) if isinstance(confidence_value, (int, float)) else None
            card = build_followup_result_card(
                title=title,
                summary=result.message,
                report_url=report_url or None,
                root_message_id=root_message_id,
                job_id=result.job_id,
                followup_text=str(result.details.get("followup_text") or ""),
                answer_confidence=answer_confidence,
                bug_agent_choices=self._result_bug_agent_choices(result) if card_actions_enabled else [],
            )
        elif mode == "knowledge_qa":
            hits = result.details.get("knowledge_hits")
            card = build_knowledge_answer_card(
                title=title,
                answer=result.message,
                hits=hits if isinstance(hits, list) else [],
            )
        else:
            card = build_result_card(
                title=title,
                success=result.success,
                summary=result.message,
                report_url=report_url or None,
                metadata=metadata if metadata else None,
                job_id=result.job_id,
                root_message_id=root_message_id,
                duration_seconds=result.duration_seconds,
            )
        card_json_str = card_to_json(card)

        self._notify_progress(
            "reply_sending_card",
            "发送结果卡片",
            event=event,
            session_id=session_id,
            success=result.success,
            mode=mode,
            delivery=delivery,
        )

        if delivery == "reply" and event.message_id:
            send_result = self.lark_client.reply_card(event.message_id, card_json_str)
        else:
            send_result = self.lark_client.send_card_response(event, card_json_str)

        if send_result.returncode != 0:
            self._notify_progress(
                "card_send_failed",
                "卡片发送失败，回退文字回复",
                event=event,
                session_id=session_id,
                stderr=(send_result.stderr or "")[:MAX_STDERR_PREVIEW],
            )
            return False
        self._remember_delivery_alias_from_result(send_result, root_message_id=root_message_id)
        return True

    def _omlx_prompt(self, event: LarkEvent, content: str | None = None) -> str | None:
        if not self.config.omlx_chat.enabled:
            return None
        prompt_source = event.content if content is None else content
        chat_command_prompt = self._chat_command_prompt(prompt_source)
        if chat_command_prompt is not None:
            return chat_command_prompt
        if should_use_omlx_chat(prompt_source, max_chars=self.config.omlx_chat.max_prompt_chars):
            return prompt_source
        stripped = prompt_source.strip()
        if event.chat_type != "p2p" or not stripped:
            return None
        if stripped.startswith("/"):
            return None
        return stripped

    def _chat_command_prompt(self, text: str) -> str | None:
        content = self._strip_group_chat_mention(text)
        if content is None:
            content = text.strip()
        prompt = extract_first_keyword_payload(content, CHAT_COMMAND_PREFIXES)
        if prompt is None or not prompt:
            return None
        return prompt

    def _stale_light_interaction_details(self, ctx: _RouteContext) -> dict[str, object] | None:
        options = self.config.event_consumer
        if not options.drop_stale_light_interactions:
            return None
        if ctx.followup_context is not None or ctx.referenced_resources:
            return None
        if self._has_formal_analysis_trigger(ctx):
            return None
        if build_basic_chat_reply(ctx.route_content, command_prefixes=self.config.command_prefixes) is None:
            if self._omlx_prompt(ctx.event, ctx.route_content) is None:
                return None
        event_time = self._event_created_at(ctx.event)
        ready_time = self._listener_ready_at()
        if event_time is None or ready_time is None:
            return None
        try:
            grace_seconds = max(0.0, float(options.stale_light_interaction_grace_seconds))
        except (TypeError, ValueError):
            return None
        stale_seconds = (ready_time - event_time).total_seconds()
        if stale_seconds <= grace_seconds:
            return None
        return {
            "event_create_time": event_time.isoformat(),
            "listener_ready_at": ready_time.isoformat(),
            "stale_seconds": round(stale_seconds, 3),
            "grace_seconds": grace_seconds,
        }

    def _has_formal_analysis_trigger(self, ctx: _RouteContext) -> bool:
        return any(
            bool(getattr(request, "triggered", False))
            for request in (
                ctx.bug_request,
                ctx.signal_request,
                ctx.direct_analysis_request,
                ctx.perception_request,
                ctx.rom_version_request,
            )
        )

    def _event_created_at(self, event: LarkEvent) -> datetime | None:
        return self._parse_event_time(event.create_time) or self._parse_event_time(event.timestamp)

    def _listener_ready_at(self) -> datetime | None:
        daemon_status = self.activity_store.get_daemon_status()
        if not (bool(daemon_status.get("ready")) or daemon_status.get("stage") == "event_consumer_ready"):
            return None
        return self._parse_event_time(daemon_status.get("updated_at"))

    def _parse_event_time(self, value: object) -> datetime | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            if re.fullmatch(r"\d+(?:\.\d+)?", text):
                timestamp = float(text)
                if timestamp > 10_000_000_000_000:
                    timestamp /= 1_000_000
                elif timestamp > 10_000_000_000:
                    timestamp /= 1_000
                return datetime.fromtimestamp(timestamp, timezone.utc)
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except (OverflowError, ValueError):
            return None

    def _normalize_mention_source_text(self, text: str) -> str:
        normalized = html.unescape(str(text or ""))
        normalized = re.sub(r"(?i)<br\s*/?>", " ", normalized)
        normalized = re.sub(r"(?i)</?(?:p|div|span)[^>]*>", " ", normalized)
        return normalized.strip()

    def _strip_group_chat_mention(self, text: str) -> str | None:
        content = self._normalize_mention_source_text(text)
        configured_bot = self.config.lark.bot_open_id.strip()
        if configured_bot:
            at_matches = re.findall(r'<at\s+[^>]*user_id="([^"]+)"[^>]*></at>', content)
            if configured_bot in at_matches:
                cleaned = re.sub(
                    rf'<at\s+[^>]*user_id="{re.escape(configured_bot)}"[^>]*></at>\s*',
                    " ",
                    content,
                )
                return self._normalize_mention_text(cleaned)
        else:
            at_tag = re.match(r'^<at\s+[^>]*user_id="([^"]+)"[^>]*></at>\s*(.*)$', content)
            if at_tag:
                return at_tag.group(2).strip()
        configured_name = self.config.lark.bot_name.strip()
        if configured_name:
            cleaned = self._strip_configured_bot_name_mention(content, configured_name)
            if cleaned is not None:
                return cleaned
            if configured_bot:
                return None
            return None
        if configured_bot:
            return None
        spaced_name_at = re.match(r"^@.+\s+(/chat(?:\s+.*)?)$", content)
        if spaced_name_at:
            return spaced_name_at.group(1).strip()
        plain_at = re.match(r"^@\S+\s+(.*)$", content)
        if plain_at:
            return plain_at.group(1).strip()
        return None

    def _strip_group_followup_mention(self, event: LarkEvent) -> str | None:
        if not (event.reply_to or event.parent_id or event.root_id or event.thread_id):
            return None
        return self._strip_bot_mention_anywhere(event.content)

    def _strip_bot_mention_anywhere(self, text: str) -> str | None:
        content = self._normalize_mention_source_text(text)
        configured_bot = self.config.lark.bot_open_id.strip()
        at_matches = re.findall(r'<at\s+[^>]*user_id="([^"]+)"[^>]*></at>', content)
        if at_matches and (not configured_bot or configured_bot in at_matches):
            cleaned = re.sub(r'<at\s+[^>]*></at>\s*', " ", content)
            return self._normalize_mention_text(cleaned)
        configured_name = self.config.lark.bot_name.strip()
        if configured_name:
            cleaned = self._strip_configured_bot_name_mention(content, configured_name)
            if cleaned is not None:
                return cleaned
            return None
        generic_plain = re.search(r"@\S+", content)
        if generic_plain:
            return self._normalize_mention_text(re.sub(r"@\S+", " ", content, count=1))
        return None

    def _normalize_mention_text(self, text: str) -> str:
        return re.sub(r"\s+", " ", text).strip()

    def _strip_configured_bot_name_mention(self, content: str, configured_name: str) -> str | None:
        name = configured_name.strip()
        if not name:
            return None
        tokens = [token for token in re.split(r"\s+", name) if token]
        if not tokens:
            return None
        # Feishu sometimes exposes plain mention text with spaces collapsed or
        # removed, so match configured bot names in a whitespace-tolerant way
        # without falling back to arbitrary @someone mentions.
        name_pattern = r"\s*".join(re.escape(token) for token in tokens)
        mention_pattern = re.compile(rf"(?<!\S)@{name_pattern}(?=\s|$)")
        if not mention_pattern.search(content):
            return None
        return self._normalize_mention_text(mention_pattern.sub(" ", content))

    def _jobs_root(self) -> Path:
        return self.config.data_dir / "jobs"

    def _event_progress_callback(self, event: LarkEvent, *, session_id: str | None = None) -> Callable[[dict[str, object]], None]:
        def _callback(progress: dict[str, object]) -> None:
            stage = str(progress.get("stage", "progress"))
            message = str(progress.get("message", ""))
            details = progress.get("details", {})
            if not isinstance(details, dict):
                details = {"value": details}
            details = dict(details)
            nested_session_id = details.pop("session_id", None)
            if nested_session_id and "provider_session_id" not in details:
                details["provider_session_id"] = nested_session_id
            for key in ("stage", "message", "event"):
                if key in details:
                    details[f"progress_{key}"] = details.pop(key)
            self._notify_progress(stage, message, event=event, session_id=session_id, **details)

        return _callback

    def send_status_card(
        self,
        event: LarkEvent,
        *,
        title: str,
        status: str,
        details: dict[str, str] | None = None,
        note: str | None = None,
        session_id: str | None = None,
    ) -> None:
        """Send a status progress card to the user during long operations."""
        if self.config.dry_run:
            return
        if event.chat_type not in {"group", "p2p"}:
            return
        self._prune_stale_progress_cards()
        key = self._progress_card_key(event, session_id=session_id)
        existing = self._progress_cards.get(key)
        if existing and existing.get("message_id"):
            existing["title"] = title
            existing["status"] = status
            existing["details"] = dict(details or {})
            existing["last_active_at"] = datetime.now(timezone.utc)
            self._update_progress_card(event, status=status, note=note, session_id=session_id)
            return
        self._progress_cards[key] = {
            "title": title,
            "status": status,
            "details": dict(details or {}),
            "started_at": datetime.now(timezone.utc),
            "message_id": "",
        }
        card = self._build_progress_card(event, key=key, status=status, note=note)
        card_json_str = card_to_json(card)
        if event.message_id:
            send_result = self.lark_client.reply_card(event.message_id, card_json_str)
        else:
            send_result = self.lark_client.send_card_response(event, card_json_str)
        card_message_id = self._card_message_id_from_result(send_result)
        if card_message_id:
            self._progress_cards[key]["message_id"] = card_message_id
            self._remember_conversation_alias(card_message_id, key)
        else:
            self._progress_cards.pop(key, None)
            self._notify_progress(
                "status_card_send_failed",
                "进度卡发送失败，退回文字确认",
                event=event,
                title=title,
                status=status,
                stderr=(getattr(send_result, "stderr", "") or getattr(send_result, "stdout", ""))[:MAX_STDERR_PREVIEW],
            )
            fallback_text = f"已收到，{title}处理中。"
            if note:
                fallback_text = f"{fallback_text}\n{note}"
            if event.message_id:
                self.lark_client.reply(event.message_id, self._reply_payload(event, fallback_text))
            else:
                self.lark_client.send_response(event, fallback_text)

    def _progress_card_key(self, event: LarkEvent | None, *, session_id: str | None = None) -> str:
        if session_id:
            return session_id
        if event is None:
            return ""
        return event.message_id or event.event_id

    def _progress_card_state_key(self, event: LarkEvent, *, session_id: str | None = None) -> str:
        candidates = [
            self._progress_card_key(event, session_id=session_id),
            event.message_id,
            event.event_id,
        ]
        for candidate in candidates:
            if candidate and candidate in self._progress_cards:
                return candidate
        return candidates[0] if candidates else ""

    def _has_progress_card(self, event: LarkEvent, *, session_id: str | None = None) -> bool:
        key = self._progress_card_state_key(event, session_id=session_id)
        card_state = self._progress_cards.get(key)
        return bool(card_state and card_state.get("message_id"))

    def _update_progress_card(
        self,
        event: LarkEvent,
        *,
        status: str = "analyzing",
        session_id: str | None = None,
        result: TaskResult | None = None,
        note: str | None = None,
    ) -> bool:
        key = self._progress_card_state_key(event, session_id=session_id)
        card_state = self._progress_cards.get(key)
        if not card_state:
            return False
        message_id = str(card_state.get("message_id") or "")
        if not message_id:
            return False
        card = self._build_progress_card(event, key=key, status=status, result=result, note=note)
        send_result = self.lark_client.update_card(message_id, card_to_json(card))
        return send_result.returncode == 0

    def _finish_progress_card(self, event: LarkEvent, result: TaskResult, *, session_id: str | None = None) -> bool:
        status = "completed" if result.success else "failed"
        note = self._progress_result_note(result) if result.success else (result.message[:MAX_ERROR_PREVIEW] if result.message else "分析失败")
        updated = self._update_progress_card(event, status=status, session_id=session_id, result=result, note=note)
        key = self._progress_card_state_key(event, session_id=session_id)
        if updated:
            self._progress_cards.pop(key, None)
        return updated

    def _prune_stale_progress_cards(self) -> None:
        """Remove progress card entries older than the TTL to prevent memory leaks.

        Uses ``last_active_at`` (refreshed on every update) rather than
        ``started_at`` so that long-running tasks that continuously report
        progress are never pruned prematurely.
        """
        now = datetime.now(timezone.utc)
        stale_keys = [
            key
            for key, state in self._progress_cards.items()
            if isinstance(state.get("last_active_at", state.get("started_at")), datetime)
            and (now - state.get("last_active_at", state["started_at"])).total_seconds() > self._progress_cards_max_age_seconds
        ]
        for key in stale_keys:
            self._progress_cards.pop(key, None)

    def _progress_result_note(self, result: TaskResult) -> str | None:
        message = (result.message or "").strip()
        if not message:
            return None
        summary = self._link_delivery_summary(message)
        if len(summary) > MAX_CARD_NOTE:
            summary = summary[:MAX_CARD_NOTE - 1].rstrip() + "…"
        return summary

    def _result_is_bug_report_mode(self, result: TaskResult | None) -> bool:
        if result is None:
            return False
        mode = str(result.details.get("mode") or "")
        return mode in {
            "bug_analysis",
            "bug_reanalysis",
            "bug_agent_followup",
            "bug_followup_existing_answer",
        }

    def _card_actions_enabled(self) -> bool:
        event_keys = getattr(self.config.event_consumer, "event_keys", None)
        if isinstance(event_keys, (list, tuple, set)):
            normalized = {str(item).strip() for item in event_keys}
            return CARD_ACTION_EVENT_KEY in normalized
        return str(self.config.event_consumer.event_key).strip() == CARD_ACTION_EVENT_KEY

    def _result_bug_skill_choices(self, result: TaskResult | None) -> list[dict[str, object]]:
        if result is None:
            return []
        if not result.success and not result.details.get("needs_user_direction"):
            return []
        if not (result.details.get("needs_user_direction") or self._result_is_bug_report_mode(result)):
            return []
        raw = result.details.get("supported_bug_skills")
        if not isinstance(raw, list):
            provider = getattr(self.bug_runner, "supported_primary_bug_skills", None)
            if callable(provider):
                try:
                    raw = provider()
                except Exception:
                    raw = []
        if not isinstance(raw, list):
            return []
        current_skill = str(result.details.get("analysis_skill") or "").strip()
        choices: list[dict[str, object]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name or name == "general":
                continue
            choices.append(
                {
                    "name": name,
                    "label": str(item.get("label") or name).strip(),
                    "description": str(item.get("description") or "").strip(),
                    "selected": bool(name and name == current_skill),
                }
            )
        return choices

    def _result_bug_skill_choice_note(self, result: TaskResult | None) -> str | None:
        if result is None:
            return None
        if result.details.get("needs_user_direction"):
            return "当前未自动命中专用 Skill。可以先补充分析要求，再从 Skill 按钮选一个方向继续。"
        skill_label = str(result.details.get("analysis_skill_label") or result.details.get("analysis_skill") or "").strip()
        if skill_label:
            return f"当前命中：{skill_label}。如果意图不正确，可以先补充要求，再从 Skill 按钮改选一个方向基于已有日志重新分析。"
        return "如果当前意图不正确，可以先补充要求，再从 Skill 按钮改选一个方向基于已有日志重新分析。"

    def _result_bug_agent_choices(self, result: TaskResult | None) -> list[dict[str, object]]:
        if result is None or not result.success or not self._result_is_bug_report_mode(result):
            return []
        current_provider = self._normalize_bug_agent_provider(
            str(result.details.get("agent_summary_provider") or result.details.get("provider") or "")
        )
        choices: list[dict[str, object]] = []
        for provider in ("codex", "claude", "omlx"):
            choice = self._bug_agent_choice(provider)
            if choice is None:
                continue
            if provider == current_provider:
                continue
            choices.append(choice)
        return choices

    def _bug_agent_choice(self, provider: str) -> dict[str, object] | None:
        normalized = self._normalize_bug_agent_provider(provider)
        if normalized == "codex":
            return {
                "provider": "codex",
                "label": "换 Codex 重分析",
                "confirm_label": "Codex Agent",
            }
        if normalized == "claude":
            if not self.config.claude_agent.enabled:
                return None
            return {
                "provider": "claude",
                "label": "换 Claude 重分析",
                "confirm_label": "Claude Agent",
            }
        if normalized == "omlx":
            if not self.config.omlx_chat.enabled:
                return None
            return {
                "provider": "omlx",
                "label": "用 OMLX 本地模型",
                "confirm_label": "OMLX 本地模型",
            }
        return None

    def _normalize_bug_agent_provider(self, provider: str) -> str:
        normalized = (provider or "").strip().casefold()
        if normalized in {"claude-code", "claude_code"}:
            return "claude"
        if normalized in {"codex", "claude", "omlx"}:
            return normalized
        return ""

    def _build_progress_card(
        self,
        event: LarkEvent,
        *,
        key: str,
        status: str,
        result: TaskResult | None = None,
        note: str | None = None,
    ) -> dict[str, object]:
        card_state = self._progress_cards.get(key, {})
        details = dict(card_state.get("details") if isinstance(card_state.get("details"), dict) else {})
        if result is not None:
            mode = str(result.details.get("mode") or "")
            if mode:
                details.setdefault("分析类型", self._progress_mode_label(mode))
            if result.job_id:
                details.setdefault("任务ID", result.job_id[:20])
            skill_label = str(result.details.get("analysis_skill_label") or result.details.get("analysis_skill") or "").strip()
            if skill_label:
                details.setdefault("命中 Skill", skill_label)
            classification_source = str(result.details.get("classification_source") or "").strip()
            if classification_source:
                details.setdefault("分类来源", classification_source)
        report_url = ""
        if result is not None:
            report_url = str(result.details.get("published_report_url") or "").strip()
        root_message_id = ""
        job_id = ""
        show_followup_actions = False
        if result is not None:
            root_message_id = str(
                result.details.get("conversation_root_message_id")
                or event.root_id
                or event.message_id
                or key
                or ""
            ).strip()
            job_id = str(result.job_id or result.details.get("job_id") or "").strip()
            show_followup_actions = bool(self._card_actions_enabled() and result.success and report_url and root_message_id)
        bug_skill_choices = (
            self._result_bug_skill_choices(result) if result is not None and self._card_actions_enabled() else []
        )
        session = self.activity_store.get_session(key) or self.activity_store.get_session(event.message_id) or {}
        progress = session.get("progress") if isinstance(session, dict) else []
        if not isinstance(progress, list):
            progress = []
        return build_status_card(
            title=str(card_state.get("title") or "分析进度"),
            status=status,
            details={str(k): str(v) for k, v in details.items() if str(v)},
            progress=progress,
            elapsed_seconds=self._progress_elapsed_seconds(card_state, result=result),
            token_usage=self._progress_token_usage(result),
            report_url=report_url or None,
            live_url=self._progress_live_url(key),
            note=note,
            job_id=job_id or None,
            root_message_id=root_message_id or None,
            show_followup_actions=show_followup_actions,
            bug_skill_choices=bug_skill_choices,
            bug_skill_choice_note=self._result_bug_skill_choice_note(result) if self._card_actions_enabled() else None,
            bug_agent_choices=self._result_bug_agent_choices(result) if self._card_actions_enabled() else [],
        )

    def _progress_live_url(self, session_id: str) -> str | None:
        if not self.config.report_server.enabled or not session_id:
            return None
        base = resolve_public_base_url(
            self.config.report_server.public_base_url,
            port=self.config.report_server.port,
            bind_host=self.config.report_server.bind_host,
        )
        parts = urlsplit(base)
        query = f"session={quote(session_id, safe='')}"
        return urlunsplit((parts.scheme, parts.netloc, "/sessions", query, ""))

    def _progress_elapsed_seconds(self, card_state: dict[str, object], *, result: TaskResult | None = None) -> float | None:
        if result is not None and isinstance(result.duration_seconds, (int, float)):
            return float(result.duration_seconds)
        started_at = card_state.get("started_at")
        if isinstance(started_at, datetime):
            return max(0.0, (datetime.now(timezone.utc) - started_at).total_seconds())
        return None

    def _progress_token_usage(self, result: TaskResult | None) -> dict[str, int] | None:
        if result is None:
            return None
        usage: dict[str, int] = {}
        for detail_key, usage_key in (
            ("agent_summary_input_tokens", "input_tokens"),
            ("agent_summary_output_tokens", "output_tokens"),
            ("agent_summary_total_tokens", "total_tokens"),
        ):
            value = result.details.get(detail_key)
            if isinstance(value, int):
                usage[usage_key] = value
        return usage or None

    def _progress_mode_label(self, mode: str) -> str:
        labels = {
            "bug_analysis": "Bug 分析",
            "bug_clarification": "Bug 分析分诊",
            "bug_reanalysis": "Bug 重新分析",
            "bug_agent_followup": "Bug 追问",
            "direct_analysis": "直传文件分析",
            "perception_summary": "感知数据总结",
            "signal_lifecycle": "信号生命周期",
            "claude_skill": "Claude Code 分析",
        }
        return labels.get(mode, mode)

    def _card_message_id_from_result(self, result) -> str:
        for text in (getattr(result, "stdout", ""), getattr(result, "stderr", "")):
            message_id = self._extract_card_message_id(text)
            if message_id:
                return message_id
        return ""

    def _remember_delivery_alias_from_result(self, result, *, root_message_id: str | None) -> None:
        message_id = self._card_message_id_from_result(result)
        self._remember_conversation_alias(message_id, root_message_id)

    def _remember_conversation_alias(self, alias_message_id: str, root_message_id: str | None) -> None:
        alias = str(alias_message_id or "").strip()
        root = str(root_message_id or "").strip()
        if not alias or not root or alias == root:
            return
        self.conversation_store.remember_alias(alias_message_id=alias, root_message_id=root)

    def _extract_card_message_id(self, text: str) -> str:
        if not text.strip():
            return ""
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\b(?:open_)?message_id\b[\"'=:\s]+([A-Za-z0-9_\\-]+)", text)
            return match.group(1) if match else ""
        return self._find_message_id(payload)

    def _find_message_id(self, value) -> str:
        if isinstance(value, dict):
            for key in ("message_id", "open_message_id"):
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    return candidate.strip()
            for nested in value.values():
                found = self._find_message_id(nested)
                if found:
                    return found
        if isinstance(value, list):
            for item in value:
                found = self._find_message_id(item)
                if found:
                    return found
        return ""

    def check_approval(
        self,
        event: LarkEvent,
        operation_type: str,
        description: str,
        **hints: object,
    ) -> tuple[bool, str]:
        """Check if an operation is approved to proceed.

        Returns ``(can_proceed, request_id)``.  If the operation needs
        confirmation, sends a confirmation card and returns ``False``.
        """
        op = build_operation_request(
            operation_type,
            description,
            requester_id=event.sender_id,
            chat_id=event.chat_id,
            **hints,
        )
        decision = self.approval_store.evaluate(op)
        if decision.can_proceed:
            return True, decision.request_id

        # Send confirmation card to the user
        if not self.config.dry_run and event.chat_type in {"group", "p2p"}:
            card = build_confirmation_card(
                title=f"操作确认：{description}",
                description=f"即将执行 **{description}**，该操作风险等级为 **{op.risk_level.value}**。请确认是否继续。",
                risk_level=op.risk_level.value,
                action_id=decision.request_id,
                metadata={"操作类型": operation_type, **{k: str(v) for k, v in hints.items()}},
            )
            card_json_str = card_to_json(card)
            self.lark_client.send_card_response(event, card_json_str)

        return False, decision.request_id

    def resolve_approval(self, request_id: str, *, approved: bool) -> bool:
        """Resolve a pending approval. Returns whether it was approved."""
        decision = self.approval_store.resolve(request_id, approved=approved)
        return decision.can_proceed

    def _run_signal_request(self, event: LarkEvent, request: SignalRequest, route_content: str) -> TaskResult:
        self._notify_progress(
            "signal_request_received",
            "收到信号生命周期分析请求",
            event=event,
            signal=request.signal or "",
            raw_text=request.raw_text,
            resource_count=len(request.resources),
            resources=self._resource_descriptors(request.resources),
        )
        self.send_status_card(
            event,
            title="信号生命周期",
            status="analyzing",
            details={"信号": request.signal or "未指定", "资源数": str(len(request.resources))},
            note="分析进行中，请稍候…",
        )
        result = self.handler.handle(request, event=event)
        return self._deliver_result(event, result, request_text=request.raw_text or route_content)

    def _run_bug_request(self, event: LarkEvent, bug_request, route_content: str) -> TaskResult:
        self._notify_progress(
            "bug_request_received",
            "收到 bug 分析请求",
            event=event,
            bug_url=bug_request.bug_url,
            prompt=bug_request.prompt,
            raw_text=bug_request.raw_text,
        )
        self.send_status_card(
            event,
            title="Bug 分析",
            status="analyzing",
            details={"Bug 链接": bug_request.bug_url[:60], "分析提示": bug_request.prompt or "默认"},
            note="分析进行中，请稍候…",
        )
        result = self.bug_runner.run_bug_analysis(
            bug_request,
            event=event,
            progress_callback=self._event_progress_callback(event),
        )
        self._ensure_result_bug_url(result, bug_request.bug_url)
        return self._deliver_result(event, result, request_text=bug_request.raw_text or route_content)

    def _run_perception_request(self, event: LarkEvent, perception_request, route_content: str) -> TaskResult:
        self._notify_progress(
            "perception_summary_request_received",
            "收到感知数据总结请求",
            event=event,
            prompt=perception_request.prompt,
            raw_text=perception_request.raw_text,
        )
        self.send_status_card(
            event,
            title="感知数据总结",
            status="analyzing",
            details={"提示": perception_request.prompt or "默认"},
            note="分析进行中，请稍候…",
        )
        result = self.perception_runner.run_summary(perception_request, event=event)
        return self._deliver_result(event, result, request_text=perception_request.raw_text or route_content)

    def _run_direct_analysis_request(self, event: LarkEvent, direct_analysis_request, route_content: str) -> TaskResult:
        self._notify_progress(
            "direct_analysis_request_received",
            "收到直传文件分析请求",
            event=event,
            prompt=direct_analysis_request.prompt,
            raw_text=direct_analysis_request.raw_text,
            resources=[item.value for item in direct_analysis_request.resources],
        )
        self.send_status_card(
            event,
            title="文件分析",
            status="analyzing",
            details={"文件数": str(len(direct_analysis_request.resources))},
            note="分析进行中，请稍候…",
        )
        result = self.bug_runner.run_direct_analysis(
            direct_analysis_request,
            event=event,
            progress_callback=self._event_progress_callback(event),
        )
        return self._deliver_result(event, result, request_text=direct_analysis_request.raw_text or route_content)

    def _run_rom_version_lookup_request(self, event: LarkEvent, rom_version_request) -> TaskResult:
        self._notify_progress(
            "rom_version_lookup_received",
            "收到 ROM 版本查询请求",
            event=event,
            rom_version=rom_version_request.rom_version,
            prompt=rom_version_request.prompt,
            raw_text=rom_version_request.raw_text,
        )
        result = self.rom_version_runner.run_lookup(rom_version_request, event=event)
        return self._deliver_result(event, result, request_text=rom_version_request.raw_text)

    def _run_addr2line_resolve_request(self, event: LarkEvent, request: Addr2LineRequest) -> TaskResult:
        self._notify_progress(
            "addr2line_resolve_received",
            "收到地址反解请求",
            event=event,
            target=request.target,
            rom_version=request.rom_version,
            napa_version=request.napa_version,
            apk_version=request.apk_version,
            raw_text=request.raw_text,
        )
        request = self._resolve_addr2line_navigation_version(event, request)
        result = self.addr2line_runner.run_resolve(request, event=event)
        return self._deliver_result(event, result, request_text=request.raw_text)

    def _resolve_addr2line_navigation_version(self, event: LarkEvent, request: Addr2LineRequest) -> Addr2LineRequest:
        if request.apk_version or request.napa_version or not request.rom_version:
            return request
        lookup_request = parse_rom_version_lookup_request(f"{request.rom_version} 查导航版本")
        if not lookup_request.triggered or not lookup_request.rom_version:
            return request
        self._notify_progress(
            "addr2line_symbol_version_lookup",
            "查询 ROM 对应导航符号表版本",
            event=event,
            rom_version=request.rom_version,
        )
        lookup_result = self.rom_version_runner.run_lookup(lookup_request, event=event)
        navigation_version = self._navigation_version_from_lookup_result(lookup_result)
        if not navigation_version:
            return request
        self._notify_progress(
            "addr2line_symbol_version_resolved",
            "已匹配导航符号表版本",
            event=event,
            rom_version=request.rom_version,
            apk_version=navigation_version,
        )
        return Addr2LineRequest(
            addr_text=request.addr_text,
            resources=request.resources,
            raw_text=request.raw_text,
            rom_version=request.rom_version,
            napa_version=request.napa_version,
            apk_version=navigation_version,
            target=request.target,
            prompt=request.prompt,
            triggered=request.triggered,
            error=None if request.error == "missing_symbol_version" else request.error,
        )

    def _maybe_request_approval(
        self,
        event: LarkEvent,
        *,
        operation_type: str,
        description: str,
        route_content: str,
        **hints: object,
    ) -> TaskResult | None:
        if not self.config.approval.enabled:
            return None
        op = build_operation_request(
            operation_type,
            description,
            requester_id=event.sender_id,
            chat_id=event.chat_id,
            **hints,
        )
        op.metadata.update(
            {
                "event_payload": self._event_payload(event),
                "route_content": route_content,
            }
        )
        decision = self.approval_store.evaluate(op)
        if decision.can_proceed:
            return None
        if not self.config.dry_run and event.chat_type in {"group", "p2p"}:
            card = build_confirmation_card(
                title=f"操作确认：{description}",
                description=f"即将执行 **{description}**，该操作风险等级为 **{op.risk_level.value}**。请确认是否继续。",
                risk_level=op.risk_level.value,
                action_id=decision.request_id,
                metadata={"操作类型": operation_type, **{k: str(v) for k, v in hints.items()}},
            )
            self.lark_client.send_card_response(event, card_to_json(card))
        return TaskResult(
            success=False,
            message=f"{description} 等待确认后执行。",
            error_code="approval_pending",
            details={
                "mode": "approval",
                "operation_type": operation_type,
                "approval_request_id": decision.request_id,
                "risk_level": op.risk_level.value,
            },
        )

    def _handle_approval_action(self, action_event: CardActionEvent, *, approved: bool) -> TaskResult:
        if not action_event.request_id:
            return TaskResult(
                success=False,
                message="卡片回调缺少审批 request_id。",
                error_code="missing_approval_request_id",
                details={"mode": "card_action", "action": action_event.action},
            )
        pending = self.approval_store.get_pending(action_event.request_id)
        if pending is None:
            return TaskResult(
                success=False,
                message="审批请求已过期或不存在。",
                error_code="approval_not_available",
                details={"mode": "approval", "approval_request_id": action_event.request_id},
            )
        decision = self.approval_store.resolve(action_event.request_id, approved=approved)
        if decision.status == ApprovalStatus.REJECTED:
            return TaskResult(
                success=False,
                message="已取消执行。",
                error_code="approval_rejected",
                details={"mode": "approval", "approval_request_id": action_event.request_id},
            )
        if not decision.can_proceed:
            return TaskResult(
                success=False,
                message="审批请求已过期或不存在。",
                error_code="approval_not_available",
                details={"mode": "approval", "approval_request_id": action_event.request_id},
            )
        return self._execute_approved_operation(pending.operation)

    def _execute_approved_operation(self, operation) -> TaskResult:
        metadata = operation.metadata or {}
        event_payload = metadata.get("event_payload")
        if not isinstance(event_payload, dict):
            return TaskResult(
                success=False,
                message="审批请求缺少原始事件信息，无法继续执行。",
                error_code="approval_missing_event_payload",
                details={"mode": "approval", "operation_type": operation.operation_type},
            )
        event = LarkEvent.from_dict(event_payload)
        route_content = str(metadata.get("route_content") or "")
        self.activity_store.record_event(event, content=route_content)
        if operation.operation_type == "bug_analysis":
            request = parse_bug_request(route_content, bug_url_re=self.bug_url_re)
            result = self._run_bug_request(event, request, route_content)
        elif operation.operation_type == "direct_analysis":
            referenced_resources = self._fetch_referenced_message_resources(event, route_content=route_content)
            request = self._build_direct_analysis_request(route_content, referenced_resources, event=event)
            result = self._run_direct_analysis_request(event, request, route_content)
        elif operation.operation_type == "reanalyze":
            result = self._execute_approved_reanalysis(event, route_content, metadata)
        else:
            result = TaskResult(
                success=False,
                message=f"审批已通过，但暂不支持执行操作类型：{operation.operation_type}",
                error_code="unsupported_approved_operation",
                details={"mode": "approval", "operation_type": operation.operation_type},
            )
        self.activity_store.record_result(event, result)
        return result

    def _execute_approved_reanalysis(self, event: LarkEvent, route_content: str, metadata: dict[str, object]) -> TaskResult:
        root_message_id = str(metadata.get("root_message_id") or "")
        followup_context = self.conversation_store.lookup(root_message_id) if root_message_id else self._resolve_followup_context(event)
        if followup_context is None:
            return self._missing_followup_reply_result(mode="approval", chat_type=event.chat_type)
        previous_session = self.activity_store.get_session(followup_context.root_message_id) or {}
        if not previous_session and metadata.get("job_id"):
            previous_session = self.activity_store.find_session_by_job_id(str(metadata.get("job_id") or "")) or {}
        selected_skill = str(metadata.get("selected_skill") or "").strip()
        selected_agent_provider = self._normalize_bug_agent_provider(
            str(metadata.get("selected_agent_provider") or metadata.get("agent_provider") or "")
        )
        if (metadata.get("selected_agent_provider") or metadata.get("agent_provider")) and not selected_agent_provider:
            return TaskResult(
                success=False,
                message="指定的 Agent 无效或不支持，无法重新分析。",
                error_code="invalid_bug_agent_selection",
                details={
                    "mode": "approval",
                    "agent_provider": str(
                        metadata.get("selected_agent_provider") or metadata.get("agent_provider") or ""
                    ),
                },
            )
        selected_skill_decision = (
            self.bug_runner.selection_for_skill_name(
                selected_skill,
                source="user_selected_card",
                reason="用户在分诊卡片中选择专用 skill。",
            )
            if selected_skill
            else None
        )
        if selected_skill_decision is not None:
            reanalysis_decision = BugFollowupSelection(
                should_reanalyze=True,
                force_rerun=True,
                plans=selected_skill_decision.plans,
                skill_name=selected_skill_decision.skill_name,
                skill_label=selected_skill_decision.skill_label,
                source=selected_skill_decision.source,
                reason=selected_skill_decision.reason,
                provider=selected_skill_decision.provider,
            )
        else:
            reanalysis_decision = self._bug_reanalysis_decision(route_content, followup_context)
        result = self.bug_runner.run_bug_reanalysis(
            followup_text=route_content,
            previous_context=followup_context,
            previous_session=previous_session,
            event=event,
            progress_callback=self._event_progress_callback(event, session_id=followup_context.root_message_id),
            force_rerun=reanalysis_decision.force_rerun,
            plans_override=reanalysis_decision.plans,
            classification_skill=reanalysis_decision.skill_name,
            classification_source=reanalysis_decision.source,
            classification_reason=reanalysis_decision.reason,
            classification_provider=reanalysis_decision.provider,
            agent_provider_override=selected_agent_provider,
            local_log_resources=self._authorized_local_download_resources(event, route_content),
            bridge_session_id=followup_context.root_message_id,
        )
        self._ensure_result_bug_url(result, self._bug_url_from_session(previous_session))
        return self._deliver_result(
            event,
            result,
            request_text=f"{followup_context.request_text}\n\n追问/修正：{route_content}",
            root_message_id=followup_context.root_message_id,
        )

    def _card_followup_context(self, action_event: CardActionEvent):
        root_message_id = action_event.root_message_id or action_event.message_id
        followup_context = self.conversation_store.lookup(root_message_id) if root_message_id else None
        previous_session: dict[str, object] = {}
        if followup_context is None and action_event.job_id:
            previous_session = self.activity_store.find_session_by_job_id(action_event.job_id) or {}
            root_message_id = str(previous_session.get("session_id") or root_message_id or "")
            followup_context = self.conversation_store.lookup(root_message_id) if root_message_id else None
        if followup_context is not None and not previous_session:
            previous_session = self.activity_store.get_session(followup_context.root_message_id) or {}
        if followup_context is not None and not previous_session and action_event.job_id:
            previous_session = self.activity_store.find_session_by_job_id(action_event.job_id) or {}
        return root_message_id, followup_context, previous_session

    def _missing_card_followup_prompt_result(
        self,
        action_event: CardActionEvent,
        *,
        root_message_id: str = "",
        fallback_chat_id: str = "",
        fallback_chat_type: str = "",
    ) -> TaskResult:
        event = self._event_from_card_action(
            action_event,
            root_message_id=root_message_id,
            fallback_chat_id=fallback_chat_id,
            fallback_chat_type=fallback_chat_type,
        )
        message = (
            "请先在卡片输入框填写追问/重跑提示词，再点击按钮；"
            "例如：基于当前报告回答“生命周期卡在哪里”，或“基于已有日志重新分析 3D 生命周期”。"
        )
        if not self.config.dry_run and event.chat_type in {"group", "p2p"}:
            if event.message_id:
                self.lark_client.reply(event.message_id, self._reply_payload(event, message))
            else:
                self.lark_client.send_response(event, message)
        return TaskResult(
            success=False,
            message=message,
            error_code="missing_card_followup_prompt",
            details={"mode": "card_action", "action": action_event.action},
        )

    def _card_action_chat_id(self, action_event: CardActionEvent, followup_context: ConversationContext | None) -> str:
        return str(action_event.chat_id or getattr(followup_context, "chat_id", "") or "").strip()

    def _handle_answer_from_report_action(self, action_event: CardActionEvent) -> TaskResult:
        _root_message_id, followup_context, previous_session = self._card_followup_context(action_event)
        if followup_context is None:
            return TaskResult(
                success=False,
                message="找不到可回答的历史上下文，请回复原分析消息后再重试。",
                error_code="missing_followup_context",
                details={"mode": "card_action", "action": "answer_from_report"},
            )
        chat_id = self._card_action_chat_id(action_event, followup_context)
        if not chat_id:
            return TaskResult(
                success=False,
                message="卡片回调缺少 chat_id，无法基于报告回答。",
                error_code="invalid_card_action_context",
                details={"mode": "card_action", "action": "answer_from_report"},
            )
        route_content = action_event.followup_text.strip()
        if not route_content:
            return self._missing_card_followup_prompt_result(
                action_event,
                root_message_id=followup_context.root_message_id,
                fallback_chat_id=chat_id,
                fallback_chat_type=str(previous_session.get("chat_type") or ""),
            )
        event = self._event_from_card_action(
            action_event,
            root_message_id=followup_context.root_message_id,
            fallback_chat_id=chat_id,
            fallback_chat_type=str(previous_session.get("chat_type") or ""),
        )
        if not self.state_store.mark_seen(event):
            return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)
        result = self._answer_bug_followup_from_existing(route_content, followup_context, min_confidence=0.0)
        if result is None:
            result = TaskResult(
                success=False,
                message="当前报告/上下文无法直接回答这个问题，请点击“基于已有日志重新分析”，或直接回复新的提示词。",
                error_code="existing_context_answer_unavailable",
                details={
                    "mode": "bug_followup_existing_answer",
                    "answer_source": "existing_context",
                    "followup_text": route_content,
                    "delivery": "reply",
                },
            )
        return self._finalize_followup_reply(event, result, followup_context, route_content)

    def _handle_continue_agent_action(self, action_event: CardActionEvent) -> TaskResult:
        _root_message_id, followup_context, previous_session = self._card_followup_context(action_event)
        if followup_context is None:
            return TaskResult(
                success=False,
                message="找不到可继续的历史上下文，请回复原分析消息后再重试。",
                error_code="missing_followup_context",
                details={"mode": "card_action", "action": "continue_agent"},
            )
        chat_id = self._card_action_chat_id(action_event, followup_context)
        if not chat_id:
            return TaskResult(
                success=False,
                message="卡片回调缺少 chat_id，无法继续原 Agent。",
                error_code="invalid_card_action_context",
                details={"mode": "card_action", "action": "continue_agent"},
            )
        route_content = action_event.followup_text.strip()
        if not route_content:
            return self._missing_card_followup_prompt_result(
                action_event,
                root_message_id=followup_context.root_message_id,
                fallback_chat_id=chat_id,
                fallback_chat_type=str(previous_session.get("chat_type") or ""),
            )
        event = self._event_from_card_action(
            action_event,
            root_message_id=followup_context.root_message_id,
            fallback_chat_id=chat_id,
            fallback_chat_type=str(previous_session.get("chat_type") or ""),
        )
        if not self.state_store.mark_seen(event):
            return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)
        result = self.bug_runner.run_bug_agent_followup(
            followup_text=route_content,
            previous_context=followup_context,
            previous_session=previous_session,
            event=event,
            progress_callback=self._event_progress_callback(event, session_id=followup_context.root_message_id),
            resume_agent_session=True,
            bridge_session_id=followup_context.root_message_id,
        )
        return self._finalize_followup_reply(event, result, followup_context, route_content)

    def _handle_select_bug_skill_action(self, action_event: CardActionEvent) -> TaskResult:
        selected = self.bug_runner.selection_for_skill_name(
            action_event.skill_name,
            source="user_selected_card",
            reason="用户在卡片中选择专用 skill。",
        )
        if selected is None:
            return TaskResult(
                success=False,
                message="卡片回调中的 skill 无效或不支持，请重新选择。",
                error_code="invalid_bug_skill_selection",
                details={
                    "mode": "card_action",
                    "action": "select_bug_skill",
                    "skill_name": action_event.skill_name,
                },
            )
        _root_message_id, followup_context, previous_session = self._card_followup_context(action_event)
        if followup_context is None:
            return TaskResult(
                success=False,
                message="找不到可继续的 bug 分诊上下文，请回复原分析消息后再重试。",
                error_code="missing_followup_context",
                details={"mode": "card_action", "action": "select_bug_skill"},
            )
        chat_id = self._card_action_chat_id(action_event, followup_context)
        if not chat_id:
            return TaskResult(
                success=False,
                message="卡片回调缺少 chat_id，无法按所选 skill 继续分析。",
                error_code="invalid_card_action_context",
                details={"mode": "card_action", "action": "select_bug_skill"},
            )
        if not previous_session:
            previous_session = self.activity_store.get_session(followup_context.root_message_id) or {}
        prompt = action_event.followup_text.strip()
        route_content = f"按专用 skill「{selected.skill_label}」继续分析"
        if prompt:
            route_content = f"{route_content}：{prompt}"
        event = self._event_from_card_action(
            action_event,
            root_message_id=followup_context.root_message_id,
            fallback_chat_id=chat_id,
            fallback_chat_type=str(previous_session.get("chat_type") or ""),
        )
        if not self.state_store.mark_seen(event):
            return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)
        pending = self._maybe_request_approval(
            event,
            operation_type="reanalyze",
            description=f"按 {selected.skill_label} 继续分析",
            route_content=route_content,
            root_message_id=followup_context.root_message_id,
            job_id=action_event.job_id,
            selected_skill=selected.skill_name,
            retry_count=1,
            estimated_duration_seconds=self.config.bug_analysis.timeout_seconds,
        )
        if pending is not None:
            return pending
        return self._execute_approved_reanalysis(
            event,
            route_content,
            {
                "root_message_id": followup_context.root_message_id,
                "job_id": action_event.job_id,
                "selected_skill": selected.skill_name,
            },
        )

    def _handle_select_bug_agent_action(self, action_event: CardActionEvent) -> TaskResult:
        provider = self._normalize_bug_agent_provider(action_event.agent_provider)
        choice = self._bug_agent_choice(provider)
        if choice is None:
            return TaskResult(
                success=False,
                message="卡片回调中的 Agent 无效或当前未启用，请重新选择。",
                error_code="invalid_bug_agent_selection",
                details={
                    "mode": "card_action",
                    "action": "select_bug_agent",
                    "agent_provider": action_event.agent_provider,
                },
            )
        _root_message_id, followup_context, previous_session = self._card_followup_context(action_event)
        if followup_context is None:
            return TaskResult(
                success=False,
                message="找不到可重新分析的 bug 上下文，请回复原分析消息后再重试。",
                error_code="missing_reanalysis_context",
                details={"mode": "card_action", "action": "select_bug_agent"},
            )
        chat_id = self._card_action_chat_id(action_event, followup_context)
        if not chat_id:
            return TaskResult(
                success=False,
                message="卡片回调缺少 chat_id，无法确认换 Agent 重分析。",
                error_code="invalid_card_action_context",
                details={"mode": "card_action", "action": "select_bug_agent"},
            )
        event = self._event_from_card_action(
            action_event,
            root_message_id=followup_context.root_message_id,
            fallback_chat_id=chat_id,
            fallback_chat_type=str(previous_session.get("chat_type") or ""),
        )
        if not self.state_store.mark_seen(event):
            return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)
        agent_label = str(choice.get("confirm_label") or choice.get("label") or provider)
        card = build_agent_reanalysis_confirmation_card(
            agent_label=agent_label,
            agent_provider=provider,
            job_id=action_event.job_id,
            root_message_id=followup_context.root_message_id,
            followup_text=action_event.followup_text,
        )
        if not self.config.dry_run and event.chat_type in {"group", "p2p"}:
            card_json = card_to_json(card)
            if event.message_id:
                self.lark_client.reply_card(event.message_id, card_json)
            else:
                self.lark_client.send_card_response(event, card_json)
        return TaskResult(
            success=True,
            message=f"已选择 {agent_label}，等待确认后重新分析。",
            details={
                "mode": "card_action",
                "action": "select_bug_agent",
                "agent_provider": provider,
                "root_message_id": followup_context.root_message_id,
                "job_id": action_event.job_id,
            },
        )

    def _handle_confirm_bug_agent_reanalysis_action(self, action_event: CardActionEvent) -> TaskResult:
        provider = self._normalize_bug_agent_provider(action_event.agent_provider)
        choice = self._bug_agent_choice(provider)
        if choice is None:
            return TaskResult(
                success=False,
                message="确认卡中的 Agent 无效或当前未启用，请重新从结果卡片选择。",
                error_code="invalid_bug_agent_selection",
                details={
                    "mode": "card_action",
                    "action": "confirm_bug_agent_reanalysis",
                    "agent_provider": action_event.agent_provider,
                },
            )
        _root_message_id, followup_context, previous_session = self._card_followup_context(action_event)
        if followup_context is None:
            return TaskResult(
                success=False,
                message="找不到可重新分析的 bug 上下文，请回复原分析消息后再重试。",
                error_code="missing_reanalysis_context",
                details={"mode": "card_action", "action": "confirm_bug_agent_reanalysis"},
            )
        chat_id = self._card_action_chat_id(action_event, followup_context)
        if not chat_id:
            return TaskResult(
                success=False,
                message="卡片回调缺少 chat_id，无法换 Agent 重分析。",
                error_code="invalid_card_action_context",
                details={"mode": "card_action", "action": "confirm_bug_agent_reanalysis"},
            )
        event = self._event_from_card_action(
            action_event,
            root_message_id=followup_context.root_message_id,
            fallback_chat_id=chat_id,
            fallback_chat_type=str(previous_session.get("chat_type") or ""),
        )
        if not self.state_store.mark_seen(event):
            return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)
        agent_label = str(choice.get("confirm_label") or choice.get("label") or provider)
        route_content = action_event.followup_text.strip()
        if not route_content:
            route_content = f"换用 {agent_label} 基于已有日志/报告重新分析"
        self._notify_progress(
            "bug_agent_switch_confirmed",
            "用户确认换 Agent 重新分析",
            event=event,
            session_id=followup_context.root_message_id,
            agent_provider=provider,
            agent_label=agent_label,
            job_id=action_event.job_id,
        )
        return self._execute_approved_reanalysis(
            event,
            route_content,
            {
                "root_message_id": followup_context.root_message_id,
                "job_id": action_event.job_id,
                "selected_agent_provider": provider,
            },
        )

    def _handle_cancel_bug_agent_reanalysis_action(self, action_event: CardActionEvent) -> TaskResult:
        _root_message_id, followup_context, previous_session = self._card_followup_context(action_event)
        chat_id = self._card_action_chat_id(action_event, followup_context)
        event = self._event_from_card_action(
            action_event,
            root_message_id=str(getattr(followup_context, "root_message_id", "") or action_event.root_message_id),
            fallback_chat_id=chat_id,
            fallback_chat_type=str(previous_session.get("chat_type") or ""),
        )
        if not self.state_store.mark_seen(event):
            return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)
        message = "已取消换 Agent 重分析。"
        if not self.config.dry_run and event.chat_type in {"group", "p2p"}:
            if event.message_id:
                self.lark_client.reply(event.message_id, self._reply_payload(event, message))
            elif event.chat_id:
                self.lark_client.send_response(event, message)
        return TaskResult(
            success=True,
            message=message,
            details={
                "mode": "card_action",
                "action": "cancel_bug_agent_reanalysis",
                "agent_provider": self._normalize_bug_agent_provider(action_event.agent_provider),
            },
        )

    def _handle_feedback_action(self, action_event: CardActionEvent) -> TaskResult:
        root_message_id, followup_context, previous_session = self._card_followup_context(action_event)
        chat_id = self._card_action_chat_id(action_event, followup_context)
        if not chat_id:
            return TaskResult(
                success=False,
                message="卡片回调缺少 chat_id，无法记录反馈。",
                error_code="invalid_card_action_context",
                details={"mode": "card_action", "action": action_event.action},
            )
        event = self._event_from_card_action(
            action_event,
            root_message_id=str(getattr(followup_context, "root_message_id", "") or root_message_id),
            fallback_chat_id=chat_id,
            fallback_chat_type=str(previous_session.get("chat_type") or ""),
        )
        if not self.state_store.mark_seen(event):
            return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)
        helpful = action_event.action == "feedback_helpful"
        message = "已记录反馈：有用。" if helpful else "已记录反馈：不准。你可以点“基于已有日志重新分析”，或直接回复新的提示词。"
        self._notify_progress(
            "followup_feedback_recorded",
            "记录追问结果反馈",
            event=event,
            session_id=str(getattr(followup_context, "root_message_id", "") or root_message_id or ""),
            feedback="helpful" if helpful else "unhelpful",
            job_id=action_event.job_id,
            root_message_id=str(getattr(followup_context, "root_message_id", "") or root_message_id or ""),
            followup_text=action_event.followup_text,
            card_message_id=event.message_id,
        )
        if not self.config.dry_run and event.chat_type in {"group", "p2p"}:
            if event.message_id:
                self.lark_client.reply(event.message_id, self._reply_payload(event, message))
            else:
                self.lark_client.send_response(event, message)
        return TaskResult(
            success=True,
            message=message,
            details={
                "mode": "card_action",
                "action": action_event.action,
                "feedback": "helpful" if helpful else "unhelpful",
                "job_id": action_event.job_id,
                "root_message_id": str(getattr(followup_context, "root_message_id", "") or root_message_id or ""),
            },
        )

    def _handle_reanalyze_action(self, action_event: CardActionEvent) -> TaskResult:
        root_message_id = action_event.root_message_id or action_event.message_id
        followup_context = self.conversation_store.lookup(root_message_id) if root_message_id else None
        previous_session: dict[str, object] = {}
        if followup_context is None and action_event.job_id:
            previous_session = self.activity_store.find_session_by_job_id(action_event.job_id) or {}
            root_message_id = str(previous_session.get("session_id") or "")
            followup_context = self.conversation_store.lookup(root_message_id) if root_message_id else None
        if followup_context is None:
            return TaskResult(
                success=False,
                message="找不到可重新分析的历史上下文，请回复原分析消息后再重试。",
                error_code="missing_reanalysis_context",
                details={"mode": "card_action", "action": "reanalyze"},
            )
        chat_id = self._card_action_chat_id(action_event, followup_context)
        if not chat_id:
            return TaskResult(
                success=False,
                message="卡片回调缺少 chat_id，无法重新分析。",
                error_code="invalid_card_action_context",
                details={"mode": "card_action", "action": "reanalyze"},
            )
        if not previous_session:
            previous_session = self.activity_store.get_session(followup_context.root_message_id) or {}
        if not previous_session and action_event.job_id:
            previous_session = self.activity_store.find_session_by_job_id(action_event.job_id) or {}
        route_content = action_event.followup_text.strip()
        if not route_content:
            return self._missing_card_followup_prompt_result(
                action_event,
                root_message_id=followup_context.root_message_id,
                fallback_chat_id=chat_id,
                fallback_chat_type=str(previous_session.get("chat_type") or ""),
            )
        event = self._event_from_card_action(
            action_event,
            root_message_id=followup_context.root_message_id,
            fallback_chat_id=chat_id,
            fallback_chat_type=str(previous_session.get("chat_type") or ""),
        )
        if not self.state_store.mark_seen(event):
            return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)
        pending = self._maybe_request_approval(
            event,
            operation_type="reanalyze",
            description="重新分析",
            route_content=route_content,
            root_message_id=followup_context.root_message_id,
            job_id=action_event.job_id,
            retry_count=1,
            estimated_duration_seconds=self.config.bug_analysis.timeout_seconds,
        )
        if pending is not None:
            return pending
        return self._execute_approved_reanalysis(
            event,
            route_content,
            {"root_message_id": followup_context.root_message_id, "job_id": action_event.job_id},
        )

    def _handle_escalate_action(self, action_event: CardActionEvent) -> TaskResult:
        if not action_event.chat_id:
            return TaskResult(
                success=False,
                message="卡片回调缺少 chat_id，无法升级人工。",
                error_code="invalid_card_action_context",
                details={"mode": "card_action", "action": "escalate"},
            )
        root_message_id = action_event.root_message_id or action_event.message_id
        session = self.activity_store.find_session_by_job_id(action_event.job_id) if action_event.job_id else None
        event = self._event_from_card_action(
            action_event,
            root_message_id=root_message_id,
            fallback_chat_type=str((session or {}).get("chat_type") or ""),
        )
        message = f"已收到人工升级请求。job_id={action_event.job_id or '-'}"
        if not self.config.dry_run and event.chat_type in {"group", "p2p"}:
            self.lark_client.send_response(event, message)
        return TaskResult(
            success=True,
            message=message,
            details={
                "mode": "card_action",
                "action": "escalate",
                "job_id": action_event.job_id,
                "root_message_id": root_message_id,
            },
        )

    def _event_payload(self, event: LarkEvent) -> dict[str, object]:
        if event.raw:
            return event.raw
        return {
            "event_id": event.event_id,
            "message_id": event.message_id,
            "chat_id": event.chat_id,
            "chat_type": event.chat_type,
            "sender_id": event.sender_id,
            "message_type": event.message_type,
            "content": event.content,
            "create_time": event.create_time,
            "timestamp": event.timestamp,
            "reply_to": event.reply_to,
            "parent_id": event.parent_id,
            "root_id": event.root_id,
            "thread_id": event.thread_id,
        }

    def _event_from_card_action(
        self,
        action_event: CardActionEvent,
        *,
        root_message_id: str = "",
        fallback_chat_id: str = "",
        fallback_chat_type: str = "",
    ) -> LarkEvent:
        chat_type = action_event.chat_type or fallback_chat_type or "unknown"
        return LarkEvent(
            event_id=action_event.event_id or f"card_{action_event.action}_{action_event.request_id or action_event.job_id}",
            message_id=action_event.message_id,
            chat_id=action_event.chat_id or fallback_chat_id,
            chat_type=chat_type,
            sender_id=action_event.operator_id,
            message_type="interactive",
            content=action_event.action,
            root_id=root_message_id,
            raw=action_event.raw,
        )

    def _notify_progress(
        self,
        stage: str,
        message: str,
        *,
        event: LarkEvent | None = None,
        session_id: str | None = None,
        **details: object,
    ) -> None:
        payload: dict[str, object] = {
            "type": "progress",
            "stage": stage,
            "message": message,
        }
        if session_id:
            payload["session_id"] = session_id
        if event is not None:
            payload.update(
                {
                    "event_id": event.event_id,
                    "message_id": event.message_id,
                    "chat_id": event.chat_id,
                    "chat_type": event.chat_type,
                }
            )
        progress_details = dict(details)
        progress_details.setdefault("executor", self._progress_executor(stage, progress_details))
        payload["details"] = progress_details
        self.activity_store.record_progress(payload)
        if event is not None and event.chat_type in {"group", "p2p"}:
            self._update_progress_card(event, session_id=session_id)
        if self.progress_callback is None:
            return
        self.progress_callback(payload)

    def _progress_executor(self, stage: str, details: dict[str, object]) -> str:
        for key in ("executor", "executed_by", "actor", "runner"):
            value = str(details.get(key) or "").strip()
            if value:
                return value
        provider = str(
            details.get("provider")
            or details.get("classification_provider")
            or details.get("agent_provider")
            or ""
        ).strip()
        normalized_stage = stage.casefold()
        if normalized_stage.startswith("intent_"):
            return f"意图 Agent({provider})" if provider else "意图 Agent"
        if "agent" in normalized_stage or "run_analysis" in normalized_stage:
            return f"本地 Agent({provider})" if provider else "本地 Agent"
        if any(
            token in normalized_stage
            for token in (
                "download",
                "fetch",
                "resolve_url",
                "check_env",
                "retry_download",
                "auth",
            )
        ):
            return "飞书/Meegle CLI"
        if any(
            token in normalized_stage
            for token in (
                "file_",
                "reply",
                "status_card",
                "report",
                "publish",
                "archive",
                "delivery",
            )
        ):
            return "Bridge 发布器"
        return "Bridge 编排器"

    def _prepare_delivery_result(
        self,
        event: LarkEvent,
        result: TaskResult,
        *,
        request_text: str,
        root_message_id: str | None = None,
    ) -> TaskResult:
        if not result.success:
            return result
        self._apply_dual_agent_arbitration(result)
        published = self.report_publisher.publish_result(result)
        if published is None:
            mode = str(result.details.get("mode", "") or "").strip()
            if result.success and (
                result.details.get("needs_user_direction") or mode in self._threaded_reply_context_modes()
            ):
                details = dict(result.details)
                context_root_message_id = root_message_id or event.root_id or event.message_id
                details.setdefault("delivery", "reply")
                details.setdefault("conversation_root_message_id", context_root_message_id)
                bug_url = str(details.get("bug_url") or self._bug_url_from_request_text(request_text))
                if bug_url:
                    details["bug_url"] = bug_url
                result.details = details
                self.conversation_store.remember(
                    root_message_id=context_root_message_id,
                    chat_id=event.chat_id,
                    mode=str(details.get("mode", "")),
                    request_text=request_text.strip() or self._fallback_request_text(result),
                    summary_text=result.message,
                    report_url="",
                    report_excerpt="",
                )
                self._remember_progress_card_aliases(event, context_root_message_id)
            return result
        summary_text = self._link_delivery_summary(result.message)
        result.message = f"{summary_text}\n\n报告链接：{published.url}"
        details = dict(result.details)
        details["delivery"] = "reply"
        details["published_report_url"] = published.url
        details["published_report_index"] = str(published.index_path)
        context_root_message_id = root_message_id or event.root_id or event.message_id
        details["conversation_root_message_id"] = context_root_message_id
        if event.chat_type == "group":
            details["files_to_send"] = [Path(path) for path in published.source_report_paths]
        else:
            details.pop("files_to_send", None)
        result.details = details
        self.conversation_store.remember(
            root_message_id=context_root_message_id,
            chat_id=event.chat_id,
            mode=str(details.get("mode", "")),
            request_text=request_text.strip() or self._fallback_request_text(result),
            summary_text=summary_text,
            report_url=published.url,
            report_excerpt=published.context_excerpt,
        )
        self._remember_progress_card_aliases(event, context_root_message_id)
        bug_url = str(details.get("bug_url") or self._bug_url_from_request_text(request_text))
        if bug_url and not details.get("bug_url"):
            details["bug_url"] = bug_url
            result.details = details
        # Auto-archive as a case for the case library
        self.case_store.save_from_result(
            result,
            event=event,
            request_text=request_text,
            bug_url=bug_url,
        )
        # Track report version
        group_key = derive_group_key(
            bug_url=bug_url,
            case_id=result.job_id or "",
            root_message_id=context_root_message_id,
        )
        version = self.version_store.add_version(
            group_key,
            job_id=result.job_id or "",
            report_url=published.url,
            summary=summary_text,
            provider=str(details.get("provider", "")),
            mode=str(details.get("mode", "")),
            duration_seconds=result.duration_seconds or 0.0,
            label=request_text[:80] if request_text else "",
        )
        details["report_version"] = version.version
        details["report_group_key"] = group_key
        result.details = details
        if self.config.workflow_archive.enabled:
            try:
                archive = self.workflow_archiver.archive(result, event=event, request_text=request_text)
            except Exception as exc:
                archive = {
                    "enabled": True,
                    "skipped": False,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                }
            details = dict(result.details)
            details["workflow_archive"] = archive
            result.details = details
        return result

    def _remember_progress_card_aliases(self, event: LarkEvent, root_message_id: str) -> None:
        for key in (root_message_id, event.message_id, event.event_id):
            card_state = self._progress_cards.get(str(key or ""))
            if not isinstance(card_state, dict):
                continue
            message_id = str(card_state.get("message_id") or "").strip()
            self._remember_conversation_alias(message_id, root_message_id)

    def _apply_dual_agent_arbitration(self, result: TaskResult) -> None:
        if not self.config.dual_agent.enabled:
            return
        details = dict(result.details)
        secondary_summary = str(
            details.get("secondary_agent_summary")
            or details.get("agent_secondary_summary")
            or ""
        ).strip()
        if not secondary_summary:
            return
        primary_provider = str(
            details.get("agent_summary_provider")
            or details.get("provider")
            or "primary"
        )
        secondary_provider = str(details.get("secondary_agent_provider") or "secondary")
        arbitration = arbitrate(
            extract_conclusion(result.message, provider=primary_provider),
            extract_conclusion(secondary_summary, provider=secondary_provider),
        )
        details["arbitration"] = arbitration.to_dict()
        result.details = details
        result.message = f"{result.message}\n\n双 Agent 裁决：\n{arbitration.summary_text()}"

    def _ensure_result_bug_url(self, result: TaskResult, bug_url: str) -> None:
        normalized = bug_url.strip()
        if not normalized:
            return
        details = dict(result.details)
        details.setdefault("bug_url", normalized)
        result.details = details

    def _bug_url_from_session(self, session: dict[str, object]) -> str:
        details = session.get("details", {}) if isinstance(session, dict) else {}
        if isinstance(details, dict):
            return str(details.get("bug_url") or "")
        return ""

    def _bug_url_from_request_text(self, request_text: str) -> str:
        request = parse_bug_request(request_text, bug_url_re=self.bug_url_re)
        return request.bug_url if request.triggered else ""

    def _handle_intent_routed_event(
        self,
        event: LarkEvent,
        route_content: str,
        *,
        explicit_followup_context,
        latest_chat_context,
        referenced_resources: list[DownloadResource] | None = None,
    ) -> TaskResult | None:
        self._notify_progress(
            "intent_analysis_started",
            "调用本地 Agent 判断消息意图",
            event=event,
            has_explicit_followup_context=explicit_followup_context is not None,
            has_latest_chat_context=latest_chat_context is not None,
        )
        try:
            decision = self.intent_runner.classify(
                event=event,
                route_content=route_content,
                explicit_followup_context=explicit_followup_context,
                latest_chat_context=latest_chat_context,
            )
        except IntentAnalysisFailure as exc:
            self._notify_progress(
                "intent_analysis_failed",
                str(exc),
                event=event,
                error_code=exc.error_code,
                stderr=(exc.stderr or exc.stdout or "")[:MAX_ERROR_PREVIEW],
            )
            if not self.state_store.mark_seen(event):
                return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)
            result = TaskResult(
                success=False,
                message=str(exc),
                command=exc.command,
                error_code=exc.error_code,
                stdout=exc.stdout,
                stderr=exc.stderr,
                details={"mode": "intent_analysis"},
            )
            if not self.config.dry_run and event.chat_type in {"group", "p2p"}:
                self._send_result(event, result)
            return result
        self._notify_progress(
            "intent_analysis_completed",
            "本地 Agent 已完成消息意图判断",
            event=event,
            route=decision.route,
            followup_action=decision.followup_action,
            context_source=decision.context_source,
            confidence=decision.confidence,
            reason=decision.reason,
        )
        return self._dispatch_intent_decision(
            event,
            route_content,
            decision,
            explicit_followup_context=explicit_followup_context,
            latest_chat_context=latest_chat_context,
            referenced_resources=referenced_resources,
        )

    def _dispatch_intent_decision(
        self,
        event: LarkEvent,
        route_content: str,
        decision: IntentDecision,
        *,
        explicit_followup_context,
        latest_chat_context,
        referenced_resources: list[DownloadResource] | None = None,
    ) -> TaskResult | None:
        route = decision.route
        referenced_resources = referenced_resources or []
        if route == "analysis_followup":
            if (
                explicit_followup_context is None
                and decision.context_source == "latest_chat"
                and looks_like_scene_signal_request(route_content)
                and not self._is_followup_intent(route_content)
            ):
                return self._handle_direct_analysis_intent(
                    event,
                    route_content,
                    referenced_resources=referenced_resources,
                )
            followup_context = self._choose_followup_context(
                decision,
                explicit_followup_context=explicit_followup_context,
                latest_chat_context=latest_chat_context,
            )
            if followup_context is None:
                if not self.state_store.mark_seen(event):
                    return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)
                result = self._missing_followup_reply_result(mode="intent_analysis", chat_type=event.chat_type)
                if not self.config.dry_run and event.chat_type in {"group", "p2p"}:
                    self._send_result(event, result)
                return result
            return self._handle_followup(
                event,
                route_content,
                followup_context,
                followup_action=decision.followup_action,
            )
        if route == "signal":
            if looks_like_scene_signal_request(route_content):
                inline_signal = parse_signal_request(
                    route_content,
                    signal_aliases=self.config.signal_aliases,
                    command_prefixes=self.config.command_prefixes,
                    signal_resolver=self.signal_resolver,
                )
                if not inline_signal.signal:
                    return self._handle_direct_analysis_intent(
                        event,
                        route_content,
                        referenced_resources=referenced_resources,
                    )
            return self._handle_signal_intent(
                event,
                route_content,
                referenced_resources=referenced_resources,
                explicit_followup_context=explicit_followup_context,
                latest_chat_context=latest_chat_context,
            )
        if route == "bug":
            return self._handle_bug_intent(event, route_content)
        if route == "direct_analysis":
            return self._handle_direct_analysis_intent(event, route_content, referenced_resources=referenced_resources)
        if route == "perception_summary":
            return self._handle_perception_intent(event, route_content, referenced_resources=referenced_resources)
        if route == "chat":
            if referenced_resources:
                return self._handle_direct_analysis_intent(event, route_content, referenced_resources=referenced_resources)
            return self._handle_chat_intent(event, route_content)
        if route == "unsupported":
            if referenced_resources:
                return self._handle_direct_analysis_intent(event, route_content, referenced_resources=referenced_resources)
            if not self.state_store.mark_seen(event):
                return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)
            if self._is_followup_intent(route_content):
                result = self._missing_followup_reply_result(mode="intent_analysis", chat_type=event.chat_type)
            else:
                result = TaskResult(
                    success=True,
                    message="not a handled request",
                    skipped=True,
                    details={"mode": "unsupported"},
                )
            self._send_result(event, result)
            return result
        return None

    def _choose_followup_context(self, decision: IntentDecision, *, explicit_followup_context, latest_chat_context):
        _ = decision
        _ = latest_chat_context
        return explicit_followup_context

    def _analysis_context_modes(self) -> set[str]:
        return {
            "bug_analysis",
            "bug_reanalysis",
            "bug_agent_followup",
            "direct_analysis",
            "perception_summary",
            "signal_lifecycle",
        }

    def _threaded_reply_context_modes(self) -> set[str]:
        return {
            *self._analysis_context_modes(),
            "bug_clarification",
            "bug_time_clarification",
            "knowledge_qa",
            "knowledge_probe",
            "basic_chat",
            "omlx_chat",
            "analysis_followup",
            "addr2line_resolve",
            "rom_version_lookup",
        }

    def _allows_reply_chain_without_mention(self, followup_context) -> bool:
        return str(getattr(followup_context, "mode", "") or "").strip() in self._threaded_reply_context_modes()

    def _latest_analysis_context(self, chat_id: str, *, explicit_followup_context=None):
        latest = self.conversation_store.latest_for_chat(chat_id, modes=self._analysis_context_modes())
        if latest is None:
            return None
        if explicit_followup_context is not None and latest.root_message_id == explicit_followup_context.root_message_id:
            return None
        return latest

    def _missing_followup_reply_result(self, *, mode: str = "followup_guard", chat_type: str = "") -> TaskResult:
        if chat_type == "p2p":
            message = "若要延续上一次分析，请直接回复对应那条分析消息。"
        else:
            message = "若要延续上一次分析，请回复对应那条分析消息；群聊里还需要 @机器人。"
        return TaskResult(
            success=False,
            message=message,
            error_code="missing_followup_reply",
            details={"mode": mode},
        )

    def _allow_log_analysis_in_external_group(
        self,
        event: LarkEvent,
        *,
        followup_context,
        signal_request: SignalRequest,
        bug_request,
        direct_analysis_request,
        perception_request,
        addr2line_request,
    ) -> bool:
        if event.chat_type != "group":
            return False
        if not self.config.allowed_chats:
            return False
        if event.chat_id in self.config.allowed_chats:
            return False
        if followup_context is not None:
            return True
        return bool(
            signal_request.triggered
            or bug_request.triggered
            or direct_analysis_request.triggered
            or perception_request.triggered
            or (addr2line_request is not None and addr2line_request.triggered)
        )

    def _build_signal_request(self, route_content: str, referenced_resources: list[DownloadResource]) -> SignalRequest:
        request = parse_signal_request(
            route_content,
            signal_aliases=self.config.signal_aliases,
            command_prefixes=self.config.command_prefixes,
            signal_resolver=self.signal_resolver,
        )
        if not referenced_resources:
            return request
        return SignalRequest(
            signal=request.signal,
            resources=self._merge_resources(request.resources, referenced_resources),
            since=request.since,
            raw_text=request.raw_text,
            triggered=request.triggered,
            error=request.error,
        )

    def _build_addr2line_request(
        self,
        route_content: str,
        event: LarkEvent,
        referenced_resources: list[DownloadResource] | None = None,
    ) -> Addr2LineRequest:
        resources = referenced_resources or []
        if not resources:
            probe = parse_addr2line_request(route_content, allow_missing_address=True)
            if probe.triggered:
                resources = self._reference_chain_log_resources(event)
        request = parse_addr2line_request(route_content, allow_missing_address=bool(resources))
        if request.triggered and resources and not request.resources:
            request = Addr2LineRequest(
                addr_text=request.addr_text,
                resources=resources,
                raw_text=request.raw_text,
                rom_version=request.rom_version,
                napa_version=request.napa_version,
                apk_version=request.apk_version,
                target=request.target,
                prompt=request.prompt,
                triggered=request.triggered,
                error=request.error,
            )
        if not request.triggered:
            return request
        if request.rom_version and not request.apk_version and not request.napa_version:
            recent_apk = self._recent_navigation_version_for_chat(event, request.rom_version)
            if recent_apk:
                request = Addr2LineRequest(
                    addr_text=request.addr_text,
                    resources=request.resources,
                    raw_text=request.raw_text,
                    rom_version=request.rom_version,
                    napa_version=request.napa_version,
                    apk_version=recent_apk,
                    target=request.target,
                    prompt=request.prompt,
                    triggered=request.triggered,
                    error=None if request.error == "missing_symbol_version" else request.error,
                )
        if request.rom_version or request.napa_version or request.apk_version:
            return request
        inherited_rom = self._recent_rom_version_for_chat(event)
        if not inherited_rom:
            return request
        inherited_apk = self._recent_navigation_version_for_chat(event, inherited_rom)
        return Addr2LineRequest(
            addr_text=request.addr_text,
            resources=request.resources,
            raw_text=request.raw_text,
            rom_version=inherited_rom,
            napa_version=request.napa_version,
            apk_version=inherited_apk or request.apk_version,
            target=request.target,
            prompt=request.prompt,
            triggered=True,
            error=None if request.error == "missing_symbol_version" else request.error,
        )

    def _followup_addr2line_request(self, ctx: _RouteContext) -> Addr2LineRequest | None:
        followup_context = ctx.followup_context
        if followup_context is None or str(getattr(followup_context, "mode", "") or "") != "addr2line_resolve":
            return None
        if parse_followup_action(ctx.route_content) != "retry":
            return None
        previous_request = parse_addr2line_request(
            str(getattr(followup_context, "request_text", "") or ""),
            allow_missing_address=True,
        )
        resources = self._reference_chain_log_resources(ctx.event) or self._log_resources_from_context(followup_context)
        session = self.activity_store.get_session(followup_context.root_message_id) or {}
        session_details = session.get("details") if isinstance(session.get("details"), dict) else {}
        session_rom = str(session_details.get("rom_version") or "")
        session_symbol = str(session_details.get("symbol_version") or "")
        session_symbol_kind = str(session_details.get("symbol_version_kind") or "")
        rom_version = previous_request.rom_version or session_rom or self._recent_rom_version_for_chat(ctx.event)
        apk_version = previous_request.apk_version
        napa_version = previous_request.napa_version
        if session_symbol:
            if session_symbol_kind == "apk" and not apk_version:
                apk_version = session_symbol
            elif session_symbol_kind == "napa" and not napa_version:
                napa_version = session_symbol
            elif session_symbol_kind == "rom" and not rom_version:
                rom_version = session_symbol
        if not apk_version and rom_version:
            apk_version = self._recent_navigation_version_for_chat(ctx.event, rom_version) or apk_version
        return Addr2LineRequest(
            addr_text=previous_request.addr_text,
            resources=resources,
            raw_text=ctx.route_content,
            rom_version=rom_version,
            napa_version=napa_version,
            apk_version=apk_version,
            target=previous_request.target or "auto",
            prompt=ctx.route_content.strip(),
            triggered=True,
        )

    def _followup_rom_lookup_request(self, ctx: _RouteContext) -> RomVersionLookupRequest | None:
        followup_context = ctx.followup_context
        if followup_context is None or str(getattr(followup_context, "mode", "") or "") != "rom_version_lookup":
            return None
        if parse_followup_action(ctx.route_content) != "retry":
            return None
        previous_request = parse_rom_version_lookup_request(str(getattr(followup_context, "request_text", "") or ""))
        rom_version = previous_request.rom_version or self._recent_rom_version_for_chat(ctx.event)
        if not rom_version:
            return None
        return RomVersionLookupRequest(
            rom_version=rom_version,
            prompt=ctx.route_content.strip(),
            raw_text=ctx.route_content,
            triggered=True,
        )

    def _recent_rom_version_for_chat(self, event: LarkEvent) -> str:
        session = self._recent_rom_lookup_session_for_chat(event)
        if not session:
            return ""
        candidate = parse_rom_version_lookup_request(str(session.get("content") or ""))
        return candidate.rom_version

    def _recent_navigation_version_for_chat(self, event: LarkEvent, rom_version: str = "") -> str:
        session = self._recent_rom_lookup_session_for_chat(event, rom_version=rom_version)
        if not session:
            return ""
        details = session.get("details")
        if not isinstance(details, dict):
            return ""
        required = details.get("required_outputs")
        if not isinstance(required, dict):
            return ""
        return self._navigation_version_from_required_outputs(required)

    def _navigation_version_from_lookup_result(self, result: TaskResult) -> str:
        if not result.success or not isinstance(result.details, dict):
            return ""
        required = result.details.get("required_outputs")
        if not isinstance(required, dict):
            return ""
        return self._navigation_version_from_required_outputs(required)

    def _navigation_version_from_required_outputs(self, required: dict[str, object]) -> str:
        navigation_version = str(required.get("navigation_version") or "").strip()
        if self._looks_like_apk_version(navigation_version):
            return navigation_version
        symbol_url = str(required.get("symbol_table_url") or "").strip()
        match = re.search(r"/(V\d+\.\d+\.\d+(?:\.\d+)?_\d{14}(?:\.\d+)?_[A-Za-z0-9]+)/?$", symbol_url)
        return match.group(1) if match else ""

    def _recent_rom_lookup_session_for_chat(self, event: LarkEvent, rom_version: str = "") -> dict[str, object] | None:
        chat_id = event.chat_id.strip()
        if not chat_id:
            return None
        for session in self.activity_store.list_sessions(limit=30, include_hidden=True):
            if str(session.get("chat_id") or "") != chat_id:
                continue
            if str(session.get("session_id") or "") == event.message_id:
                continue
            if str(session.get("mode") or "") != "rom_version_lookup":
                continue
            if str(session.get("status") or "") != "succeeded":
                continue
            candidate = parse_rom_version_lookup_request(str(session.get("content") or ""))
            if candidate.rom_version:
                if rom_version and candidate.rom_version != rom_version:
                    continue
                session_id = str(session.get("session_id") or "").strip()
                return self.activity_store.get_session(session_id) or session
        return None

    def _looks_like_apk_version(self, value: str) -> bool:
        return re.match(r"^V\d+\.\d+\.\d+(?:\.\d+)?_\d{14}(?:\.\d+)?_[A-Za-z0-9]+$", value) is not None

    def _resource_descriptors(self, resources: list[DownloadResource]) -> list[dict[str, str]]:
        return [
            {
                "kind": item.kind,
                "value": item.value,
                "source_message_id": item.source_message_id,
            }
            for item in resources
        ]

    def _log_resources_from_session(self, session: dict[str, object] | None) -> list[DownloadResource]:
        if not session:
            return []
        resources: list[DownloadResource] = []
        details = session.get("details")
        if not isinstance(details, dict):
            details = {}
        for key in ("prepared_log_input", "selected_log_input"):
            value = str(details.get(key) or "").strip()
            if not value:
                continue
            candidate = Path(value).expanduser()
            if candidate.exists():
                resources.append(DownloadResource(kind="local", value=str(candidate.resolve())))
        downloads = details.get("downloads")
        if isinstance(downloads, list):
            for item in downloads:
                if not isinstance(item, dict):
                    continue
                path = str(item.get("path") or "").strip()
                if path:
                    candidate = Path(path).expanduser()
                    if candidate.exists():
                        resources.append(DownloadResource(kind="local", value=str(candidate.resolve())))
                        continue
                kind = str(item.get("kind") or "").strip()
                value = str(item.get("value") or "").strip()
                if kind and value:
                    resources.append(DownloadResource(kind=kind, value=value))
        raw_resources = details.get("resources")
        if isinstance(raw_resources, list):
            for item in raw_resources:
                if not isinstance(item, dict):
                    continue
                kind = str(item.get("kind") or "").strip()
                value = str(item.get("value") or "").strip()
                source_message_id = str(item.get("source_message_id") or "").strip()
                if kind and value:
                    resources.append(DownloadResource(kind=kind, value=value, source_message_id=source_message_id))
        return self._merge_resources([], resources)

    def _log_resources_from_context(self, context) -> list[DownloadResource]:
        if context is None:
            return []
        return self._log_resources_from_session(self.activity_store.get_session(context.root_message_id))

    def _should_inherit_signal_resources(self, event: LarkEvent, route_content: str) -> bool:
        if event.reply_to or event.parent_id or event.root_id or event.thread_id:
            return True
        if self._is_followup_intent(route_content):
            return True
        lowered = route_content.casefold()
        return any(
            term in lowered
            for term in (
                "基于日志",
                "用日志",
                "看日志",
                "日志",
                "那就",
                "继续",
                "刚才",
                "上次",
                "上轮",
                "上一",
                "之前",
                "已有",
                "这个",
                "这些",
                "同样",
            )
        )

    def _fetch_resources_from_session_message(self, session: dict[str, object]) -> list[DownloadResource]:
        message_id = str(session.get("message_id") or session.get("session_id") or "").strip()
        if not message_id:
            return []
        synthetic_event = LarkEvent(
            event_id=str(session.get("event_id") or ""),
            message_id=message_id,
            chat_id=str(session.get("chat_id") or ""),
            chat_type=str(session.get("chat_type") or ""),
            sender_id=str(session.get("sender_id") or ""),
            message_type="text",
            content=str(session.get("content") or ""),
            reply_to=str(session.get("reply_to") or ""),
            parent_id=str(session.get("parent_id") or ""),
            root_id=str(session.get("root_id") or ""),
            thread_id=str(session.get("thread_id") or ""),
        )
        resources = self._fetch_referenced_message_resources(
            synthetic_event,
            route_content=synthetic_event.content,
            force_current_lookup=True,
        )
        fetched = self.lark_client.fetch_message(message_id)
        if fetched.returncode == 0:
            resources = self._merge_resources(
                resources,
                self._extract_resources_from_message_payload(fetched.stdout, fallback_message_id=message_id),
            )
        return resources

    def _reference_chain_log_resources(self, event: LarkEvent) -> list[DownloadResource]:
        reference_ids = self._fetch_followup_reference_ids(event)
        for message_id in self._followup_context_candidate_ids(event, reference_ids):
            resources = self._log_resources_from_reference_id(message_id)
            if resources:
                return resources
        return []

    def _log_resources_from_reference_id(self, message_id: str) -> list[DownloadResource]:
        normalized = message_id.strip()
        if not normalized:
            return []
        session = self.activity_store.get_session(normalized)
        resources = self._log_resources_from_session(session)
        if resources:
            return resources
        if session:
            resources = self._fetch_resources_from_session_message(session)
            if resources:
                return resources
        context = self.conversation_store.lookup(normalized)
        resources = self._log_resources_from_context(context)
        if resources:
            return resources
        fetched = self.lark_client.fetch_message(normalized)
        if fetched.returncode == 0:
            resources = self._extract_resources_from_message_payload(fetched.stdout, fallback_message_id=normalized)
            if resources:
                return resources
        return []

    def _contextual_signal_resources(
        self,
        event: LarkEvent,
        route_content: str,
        *,
        explicit_followup_context,
        latest_chat_context,
    ) -> list[DownloadResource]:
        resources = self._reference_chain_log_resources(event)
        if resources:
            return resources
        if not self._should_inherit_signal_resources(event, route_content):
            return []
        resources = self._log_resources_from_context(explicit_followup_context)
        if resources:
            return resources
        _ = latest_chat_context
        return []

    def _build_perception_summary_request(self, route_content: str, referenced_resources: list[DownloadResource]):
        request = parse_perception_summary_request(route_content)
        merged_resources = self._merge_resources(request.resources, referenced_resources)
        if request.triggered:
            return request.__class__(
                prompt=request.prompt,
                resources=merged_resources,
                raw_text=request.raw_text,
                triggered=True,
                error=request.error,
            )
        if not referenced_resources:
            return request
        hinted = f"{route_content.strip()} {' '.join(item.value for item in referenced_resources)}".strip()
        hinted_request = parse_perception_summary_request(hinted)
        if not hinted_request.triggered:
            return request
        return request.__class__(
            prompt=route_content.strip(),
            resources=merged_resources,
            raw_text=route_content,
            triggered=True,
            error=None if route_content.strip() else "missing_prompt",
        )

    def _build_direct_analysis_request(
        self,
        route_content: str,
        referenced_resources: list[DownloadResource],
        *,
        event: LarkEvent | None = None,
    ):
        request = parse_direct_analysis_request(route_content)
        local_resources = self._authorized_local_download_resources(event, route_content)
        merged_resources = self._merge_resources(request.resources, [*referenced_resources, *local_resources])
        if request.triggered:
            return request.__class__(
                prompt=request.prompt,
                resources=merged_resources,
                raw_text=request.raw_text,
                triggered=True,
                error=request.error,
            )
        if local_resources and self._looks_like_direct_analysis_prompt(route_content):
            return request.__class__(
                prompt=route_content.strip(),
                resources=merged_resources,
                raw_text=route_content,
                triggered=True,
                error=None if route_content.strip() else "missing_prompt",
            )
        if not referenced_resources:
            return request
        hinted = f"{route_content.strip()} {' '.join(item.value for item in referenced_resources)}".strip()
        hinted_request = parse_direct_analysis_request(hinted)
        if not hinted_request.triggered:
            prompt = route_content.strip()
            if not prompt or not self._looks_like_direct_analysis_prompt(route_content):
                return request
            return request.__class__(
                prompt=prompt,
                resources=merged_resources,
                raw_text=route_content,
                triggered=True,
                error=None,
            )
        return request.__class__(
            prompt=route_content.strip(),
            resources=merged_resources,
            raw_text=route_content,
            triggered=True,
            error=None if route_content.strip() else "missing_prompt",
        )

    def _authorized_local_download_resources(self, event: LarkEvent | None, route_content: str) -> list[DownloadResource]:
        options = self.config.local_resources
        if not options.enabled or event is None:
            return []
        if options.require_allowed_user and event.sender_id not in set(self.config.allowed_users):
            return []
        lowered_content = route_content.casefold()
        if not any(term.casefold() in lowered_content for term in LOCAL_DOWNLOAD_AUTH_TERMS):
            return []
        resources: list[DownloadResource] = []
        seen: set[Path] = set()
        for file_name in LOCAL_RESOURCE_NAME_RE.findall(route_content):
            safe_name = Path(file_name).name
            if safe_name != file_name:
                continue
            for base_dir in options.allowed_dirs:
                candidate = (Path(base_dir).expanduser() / safe_name).resolve()
                if candidate in seen:
                    continue
                if not self._is_path_under_allowed_local_dir(candidate, options.allowed_dirs):
                    continue
                if not candidate.is_file():
                    continue
                seen.add(candidate)
                resources.append(DownloadResource(kind="local", value=str(candidate)))
        return resources

    def _is_path_under_allowed_local_dir(self, path: Path, allowed_dirs: list[Path]) -> bool:
        try:
            resolved_path = path.expanduser().resolve()
        except OSError:
            return False
        for base_dir in allowed_dirs:
            try:
                resolved_base = Path(base_dir).expanduser().resolve()
            except OSError:
                continue
            if resolved_path == resolved_base or resolved_base in resolved_path.parents:
                return True
        return False

    def _merge_resources(
        self,
        primary: list[DownloadResource],
        extra: list[DownloadResource],
    ) -> list[DownloadResource]:
        merged: list[DownloadResource] = []
        seen: set[tuple[str, str, str]] = set()
        for item in [*primary, *extra]:
            key = (item.kind, item.value, item.source_message_id.strip())
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
        return merged

    def _fetch_referenced_message_resources(
        self,
        event: LarkEvent,
        *,
        route_content: str,
        force_current_lookup: bool = False,
    ) -> list[DownloadResource]:
        resources: list[DownloadResource] = []
        candidate_ids = self._candidate_reference_message_ids(
            event,
            route_content=route_content,
            force_current_lookup=force_current_lookup,
        )
        for message_id in candidate_ids:
            fetched = self.lark_client.fetch_message(message_id)
            if fetched.returncode != 0:
                continue
            resources = self._merge_resources(
                resources,
                self._extract_resources_from_message_payload(fetched.stdout, fallback_message_id=message_id),
            )
        if resources:
            return resources
        seen = set(candidate_ids)
        reference_ids = self._fetch_followup_reference_ids(event)
        for message_id in self._followup_context_candidate_ids(event, reference_ids):
            if message_id in seen:
                continue
            seen.add(message_id)
            fetched = self.lark_client.fetch_message(message_id)
            if fetched.returncode != 0:
                continue
            resources = self._merge_resources(
                resources,
                self._extract_resources_from_message_payload(fetched.stdout, fallback_message_id=message_id),
            )
        return resources

    def _candidate_reference_message_ids(
        self,
        event: LarkEvent,
        *,
        route_content: str,
        force_current_lookup: bool = False,
    ) -> list[str]:
        candidates = [value for value in [event.reply_to, event.parent_id, event.root_id] if value]
        should_lookup_current = force_current_lookup or self._should_lookup_current_message_for_resources(event, route_content)
        if not candidates and should_lookup_current and event.message_id:
            fetched_current = self.lark_client.fetch_message(event.message_id)
            if fetched_current.returncode == 0:
                candidates.extend(
                    candidate
                    for candidate in self._extract_message_reference_ids(fetched_current.stdout)
                    if candidate and candidate != event.message_id
                )
        unique: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            value = candidate.strip()
            if not value or value in seen:
                continue
            seen.add(value)
            unique.append(value)
        return unique[:3]

    def _should_lookup_current_message_for_resources(self, event: LarkEvent, route_content: str) -> bool:
        if event.reply_to or event.parent_id or event.root_id:
            return True
        inline_direct = parse_direct_analysis_request(route_content)
        if inline_direct.triggered:
            return not inline_direct.resources
        if self._looks_like_direct_analysis_prompt(route_content):
            return True
        inline_perception = parse_perception_summary_request(route_content)
        if inline_perception.triggered:
            return not inline_perception.resources
        inline_signal = parse_signal_request(
            route_content,
            signal_aliases=self.config.signal_aliases,
            command_prefixes=self.config.command_prefixes,
            signal_resolver=self.signal_resolver,
        )
        if inline_signal.triggered:
            return not inline_signal.resources
        return False

    def _looks_like_direct_analysis_prompt(self, route_content: str) -> bool:
        return looks_like_direct_analysis_prompt(route_content, resources_present=True, bug_url_re=self.bug_url_re)

    def _extract_resources_from_message_payload(self, payload_text: str, *, fallback_message_id: str = "") -> list[DownloadResource]:
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, dict):
            return []
        data = payload.get("data", {})
        if not isinstance(data, dict):
            return []
        messages = data.get("messages")
        if isinstance(messages, dict):
            messages = [messages]
        if not isinstance(messages, list):
            return []
        resources: list[DownloadResource] = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            message_id = str(message.get("message_id") or fallback_message_id).strip()
            extracted = self._extract_resources_from_message_value(message, source_message_id=message_id)
            resources = self._merge_resources(resources, extracted)
        return resources

    def _extract_resources_from_message_value(self, value: object, *, source_message_id: str) -> list[DownloadResource]:
        resources: list[DownloadResource] = []
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                parsed = None
            if parsed is not None and parsed is not value:
                return self._extract_resources_from_message_value(parsed, source_message_id=source_message_id)
            return [
                item
                for item in find_resources(value, source_message_id=source_message_id)
                if item.kind in {"file", "folder", "image"}
            ]
        if isinstance(value, dict):
            for key, nested in value.items():
                if key == "file_key" and isinstance(nested, str) and nested.strip():
                    resources = self._merge_resources(
                        resources,
                        [DownloadResource(kind="file", value=nested.strip(), source_message_id=source_message_id)],
                    )
                    continue
                if key == "image_key" and isinstance(nested, str) and nested.strip():
                    resources = self._merge_resources(
                        resources,
                        [DownloadResource(kind="image", value=nested.strip(), source_message_id=source_message_id)],
                    )
                    continue
                if key in {"folder_token", "folderToken"} and isinstance(nested, str) and nested.strip():
                    resources = self._merge_resources(
                        resources,
                        [DownloadResource(kind="folder", value=nested.strip(), source_message_id=source_message_id)],
                    )
                    continue
                resources = self._merge_resources(
                    resources,
                    self._extract_resources_from_message_value(nested, source_message_id=source_message_id),
                )
            return resources
        if isinstance(value, list):
            for item in value:
                resources = self._merge_resources(
                    resources,
                    self._extract_resources_from_message_value(item, source_message_id=source_message_id),
                )
        return resources

    def _handle_signal_intent(
        self,
        event: LarkEvent,
        route_content: str,
        *,
        referenced_resources: list[DownloadResource] | None = None,
        explicit_followup_context=None,
        latest_chat_context=None,
    ) -> TaskResult:
        request = parse_signal_request(
            route_content,
            signal_aliases=self.config.signal_aliases,
            command_prefixes=self.config.command_prefixes,
            signal_resolver=self.signal_resolver,
        )
        if referenced_resources:
            request = SignalRequest(
                signal=request.signal,
                resources=self._merge_resources(request.resources, referenced_resources),
                since=request.since,
                raw_text=request.raw_text,
                triggered=request.triggered,
                error=request.error,
            )
        if request.triggered and not request.resources:
            inherited_resources = self._contextual_signal_resources(
                event,
                route_content,
                explicit_followup_context=explicit_followup_context,
                latest_chat_context=latest_chat_context,
            )
            if inherited_resources:
                request = SignalRequest(
                    signal=request.signal,
                    resources=self._merge_resources(request.resources, inherited_resources),
                    since=request.since,
                    raw_text=request.raw_text,
                    triggered=request.triggered,
                    error=request.error,
                )
        if not request.triggered:
            request = SignalRequest(
                signal=None,
                resources=referenced_resources or [],
                raw_text=route_content,
                triggered=True,
                error="missing_signal",
            )
        if not self.state_store.mark_seen(event):
            return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)
        return self._run_signal_request(event, request, route_content)

    def _handle_skill_intent(self, event: LarkEvent, route_content: str) -> TaskResult:
        skill_request = parse_claude_skill_request(
            route_content,
            trigger_prefixes=self.config.claude_agent.trigger_prefixes,
        )
        if not skill_request.triggered:
            skill_request = skill_request.__class__(prompt=route_content.strip(), raw_text=route_content, triggered=True)
        if not self.state_store.mark_seen(event):
            return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)
        result = self.claude_runner.run_skill_analysis(skill_request, event=event)
        return self._deliver_result(event, result, request_text=skill_request.raw_text or route_content)

    def _handle_bug_intent(self, event: LarkEvent, route_content: str) -> TaskResult:
        bug_request = parse_bug_request(route_content, bug_url_re=self.bug_url_re)
        if not bug_request.triggered:
            bug_request = bug_request.__class__(bug_url="", prompt=route_content.strip(), raw_text=route_content, triggered=True, error="missing_bug_url")
        if not self.state_store.mark_seen(event):
            return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)
        pending = self._maybe_request_approval(
            event,
            operation_type="bug_analysis",
            description="Bug 分析",
            route_content=route_content,
            bug_url=bug_request.bug_url,
            prompt=bug_request.prompt,
            estimated_duration_seconds=self.config.bug_analysis.timeout_seconds,
        )
        if pending is not None:
            return pending
        return self._run_bug_request(event, bug_request, route_content)

    def _handle_direct_analysis_intent(
        self,
        event: LarkEvent,
        route_content: str,
        *,
        referenced_resources: list[DownloadResource] | None = None,
    ) -> TaskResult:
        referenced_resources = referenced_resources or []
        direct_analysis_request = self._build_direct_analysis_request(route_content, referenced_resources, event=event)
        if not direct_analysis_request.triggered:
            direct_analysis_request = direct_analysis_request.__class__(
                prompt=route_content.strip(),
                resources=referenced_resources,
                raw_text=route_content,
                triggered=True,
                error=None if referenced_resources else "missing_log",
            )
        if not self.state_store.mark_seen(event):
            return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)
        pending = self._maybe_request_approval(
            event,
            operation_type="direct_analysis",
            description="直传文件分析",
            route_content=route_content,
            file_count=len(direct_analysis_request.resources),
            prompt=direct_analysis_request.prompt,
            estimated_duration_seconds=self.config.bug_analysis.timeout_seconds,
        )
        if pending is not None:
            return pending
        return self._run_direct_analysis_request(event, direct_analysis_request, route_content)

    def _handle_perception_intent(
        self,
        event: LarkEvent,
        route_content: str,
        *,
        referenced_resources: list[DownloadResource] | None = None,
    ) -> TaskResult:
        perception_request = self._build_perception_summary_request(route_content, referenced_resources or [])
        if not perception_request.triggered:
            perception_request = perception_request.__class__(
                prompt=route_content.strip(),
                resources=referenced_resources or [],
                raw_text=route_content,
                triggered=True,
            )
        if not self.state_store.mark_seen(event):
            return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)
        return self._run_perception_request(event, perception_request, route_content)

    def _handle_chat_intent(self, event: LarkEvent, route_content: str) -> TaskResult:
        chat_reply = build_basic_chat_reply(route_content, command_prefixes=self.config.command_prefixes)
        chat_prompt = self._omlx_prompt(event, route_content)
        if chat_reply is None and chat_prompt is not None:
            if not self.state_store.mark_seen(event):
                return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)
            result = self.chat_client.reply(chat_prompt)
            return self._deliver_result(event, result, request_text=route_content)
        if chat_reply is not None:
            if not self.state_store.mark_seen(event):
                return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)
            result = TaskResult(
                success=True,
                message=chat_reply,
                details={"mode": "basic_chat"},
            )
            return self._deliver_result(event, result, request_text=route_content)
        return None

    def _handle_followup(
        self,
        event: LarkEvent,
        route_content: str,
        followup_context,
        *,
        followup_action: str | None = None,
    ) -> TaskResult:
        if not self.state_store.mark_seen(event):
            return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)
        had_previous_session_before_followup = (
            self.activity_store.get_session(followup_context.root_message_id) is not None
        )
        if "bug" in str(followup_context.mode).casefold():
            existing_answer = self._answer_bug_followup_from_existing(route_content, followup_context)
            if existing_answer is not None:
                return self._finalize_followup_reply(event, existing_answer, followup_context, route_content)
            self._send_followup_ack(
                event,
                "已收到，正在基于上次 bug 会话处理；能复用已有日志/报告会优先复用，需要时才重跑。",
                root_message_id=followup_context.root_message_id,
            )
            self._notify_progress(
                "bug_followup_decision_started",
                "判断续聊是否需要重分析",
                event=event,
                session_id=followup_context.root_message_id,
                followup_text=route_content,
            )
        action = (followup_action or "").strip()
        reanalysis_decision = self._bug_reanalysis_decision(route_content, followup_context)
        if "bug" in str(followup_context.mode).casefold():
            self._notify_progress(
                "bug_followup_decision_completed",
                "续聊处理路径已确定",
                event=event,
                session_id=followup_context.root_message_id,
                should_reanalyze=reanalysis_decision.should_reanalyze,
                force_rerun=reanalysis_decision.force_rerun,
                skill_name=reanalysis_decision.skill_name,
                reason=reanalysis_decision.reason,
                provider=reanalysis_decision.provider,
            )
        should_reanalyze = action == "reanalysis" or (not action and reanalysis_decision.should_reanalyze)
        if should_reanalyze:
            stored_previous_session = self.activity_store.get_session(followup_context.root_message_id) or {}
            previous_session = self._session_with_followup_bug_metadata(stored_previous_session, followup_context)
            recovered_bug_request = self._fresh_bug_request_from_followup_context(
                followup_context,
                followup_text=route_content,
            )
            if (
                recovered_bug_request is not None
                and not had_previous_session_before_followup
                and not self._followup_context_has_analysis_artifacts(followup_context)
            ):
                pending = self._maybe_request_approval(
                    event,
                    operation_type="bug_analysis",
                    description="Bug 分析",
                    route_content=recovered_bug_request.raw_text,
                    bug_url=recovered_bug_request.bug_url,
                    prompt=recovered_bug_request.prompt,
                    estimated_duration_seconds=self.config.bug_analysis.timeout_seconds,
                )
                if pending is not None:
                    return pending
                self._notify_progress(
                    "bug_followup_recovered_as_new_bug",
                    "从被回复消息恢复 Bug 链接，按新的 Bug 分析重新执行",
                    event=event,
                    session_id=followup_context.root_message_id,
                    bug_url=recovered_bug_request.bug_url,
                    followup_text=route_content,
                )
                return self._run_bug_request(event, recovered_bug_request, recovered_bug_request.raw_text)
            if not str(previous_session.get("job_id") or "").strip():
                recovered_direct_request = self._recovered_direct_analysis_request_from_followup_context(
                    event,
                    followup_context,
                    followup_text=route_content,
                )
                if recovered_direct_request is not None:
                    pending = self._maybe_request_approval(
                        event,
                        operation_type="direct_analysis",
                        description="直传文件分析",
                        route_content=recovered_direct_request.raw_text,
                        file_count=len(recovered_direct_request.resources),
                        prompt=recovered_direct_request.prompt,
                        estimated_duration_seconds=self.config.bug_analysis.timeout_seconds,
                    )
                    if pending is not None:
                        return pending
                    self._notify_progress(
                        "bug_followup_recovered_as_direct_analysis",
                        "从回复链恢复原文件和意图，按直传文件分析重新执行",
                        event=event,
                        session_id=followup_context.root_message_id,
                        followup_text=route_content,
                        recovered_prompt=recovered_direct_request.prompt,
                        recovered_resources=[item.value for item in recovered_direct_request.resources],
                    )
                    return self._run_direct_analysis_request(
                        event,
                        recovered_direct_request,
                        recovered_direct_request.raw_text,
                    )
            pending = self._maybe_request_approval(
                event,
                operation_type="reanalyze",
                description="重新分析",
                route_content=route_content,
                root_message_id=followup_context.root_message_id,
                retry_count=1,
                estimated_duration_seconds=self.config.bug_analysis.timeout_seconds,
            )
            if pending is not None:
                return pending
            result = self.bug_runner.run_bug_reanalysis(
                followup_text=route_content,
                previous_context=followup_context,
                previous_session=previous_session,
                event=event,
                progress_callback=self._event_progress_callback(event, session_id=followup_context.root_message_id),
                force_rerun=action == "reanalysis" or reanalysis_decision.force_rerun,
                plans_override=reanalysis_decision.plans,
                classification_skill=reanalysis_decision.skill_name,
                classification_source=reanalysis_decision.source,
                classification_reason=reanalysis_decision.reason,
                classification_provider=reanalysis_decision.provider,
                bridge_session_id=followup_context.root_message_id,
            )
            self._ensure_result_bug_url(result, self._bug_url_from_session(previous_session))
            finalized = self._deliver_result(
                event,
                result,
                request_text=f"{followup_context.request_text}\n\n追问/修正：{route_content}",
                root_message_id=followup_context.root_message_id,
            )
            if finalized.success:
                self.conversation_store.append_exchange(
                    followup_context.root_message_id,
                    user_text=route_content,
                    assistant_text=finalized.message,
                )
            return finalized
        if "bug" in str(followup_context.mode).casefold():
            previous_session = self.activity_store.get_session(followup_context.root_message_id) or {}
            previous_session = self._session_with_followup_bug_metadata(previous_session, followup_context)
            result = self.bug_runner.run_bug_agent_followup(
                followup_text=route_content,
                previous_context=followup_context,
                previous_session=previous_session,
                event=event,
                progress_callback=self._event_progress_callback(event, session_id=followup_context.root_message_id),
                resume_agent_session=action == "continue_agent",
                bridge_session_id=followup_context.root_message_id,
            )
            return self._finalize_followup_reply(event, result, followup_context, route_content)
        result = self.chat_client.reply_with_context(
            route_content,
            request_text=followup_context.request_text,
            summary_text=followup_context.summary_text,
            report_excerpt=followup_context.report_excerpt,
            history=followup_context.history,
            report_url=followup_context.report_url,
        )
        return self._finalize_followup_reply(event, result, followup_context, route_content)

    def _fresh_bug_request_from_followup_context(self, followup_context, *, followup_text: str):
        request_text = str(getattr(followup_context, "request_text", "") or "").strip()
        if not request_text:
            return None
        bug_request = parse_bug_request(request_text, bug_url_re=self.bug_url_re)
        if not bug_request.triggered:
            return None
        followup = followup_text.strip()
        prompt_parts = [part for part in (bug_request.prompt.strip(), f"追问/修正：{followup}" if followup else "") if part]
        raw_parts = [part for part in (request_text, f"追问/修正：{followup}" if followup else "") if part]
        return bug_request.__class__(
            bug_url=bug_request.bug_url,
            prompt="\n".join(prompt_parts),
            raw_text="\n\n".join(raw_parts),
            triggered=True,
            error=None,
        )

    def _followup_context_has_analysis_artifacts(self, followup_context) -> bool:
        return bool(
            str(getattr(followup_context, "summary_text", "") or "").strip()
            or str(getattr(followup_context, "report_url", "") or "").strip()
            or str(getattr(followup_context, "report_excerpt", "") or "").strip()
            or getattr(followup_context, "history", None)
        )

    def _session_with_followup_bug_metadata(self, previous_session: dict[str, object], followup_context) -> dict[str, object]:
        session = dict(previous_session)
        details = session.get("details", {})
        if not isinstance(details, dict):
            details = {}
        else:
            details = dict(details)
        request_text = str(getattr(followup_context, "request_text", "") or "").strip()
        bug_url = str(details.get("bug_url") or self._bug_url_from_request_text(request_text)).strip()
        if bug_url:
            details["bug_url"] = bug_url
        if request_text and not str(details.get("user_request_text") or "").strip():
            details["user_request_text"] = request_text
        session["details"] = details
        return session

    def _recovered_direct_analysis_request_from_followup_context(
        self,
        event: LarkEvent,
        followup_context,
        *,
        followup_text: str,
    ) -> DirectAnalysisRequest | None:
        request_text = str(getattr(followup_context, "request_text", "") or "").strip()
        if not request_text or self._bug_url_from_request_text(request_text):
            return None
        resources = self._reference_chain_log_resources(event)
        if not resources:
            return None
        route_content = request_text
        cleaned_followup = followup_text.strip()
        followup_action = parse_followup_action(cleaned_followup)
        if cleaned_followup and followup_action not in {"retry", "continue"}:
            route_content = f"{request_text}\n\n追问/修正：{cleaned_followup}"
        request = self._build_direct_analysis_request(route_content, resources, event=event)
        if request.triggered:
            return request
        prompt = route_content.strip()
        if not prompt:
            return None
        return DirectAnalysisRequest(
            prompt=prompt,
            resources=resources,
            raw_text=route_content,
            triggered=True,
            error=None,
        )

    def _send_followup_ack(self, event: LarkEvent, message: str, *, root_message_id: str | None = None) -> None:
        if self.config.dry_run or event.chat_type not in {"group", "p2p"}:
            return
        session_id = str(root_message_id or "").strip() or None
        try:
            self.send_status_card(
                event,
                title="请求处理中",
                status="analyzing",
                details={"当前阶段": "续聊判断", "分析类型": "Bug 追问"},
                note=message,
                session_id=session_id,
            )
            if session_id:
                self._remember_conversation_alias(event.message_id, session_id)
        except Exception:
            if event.message_id:
                try:
                    self.lark_client.reply(event.message_id, self._reply_payload(event, message))
                except Exception as reply_exc:
                    logger.error("failed to send followup ack for %s: %s", event.message_id, reply_exc)
                    return

    def _answer_bug_followup_from_existing(
        self,
        route_content: str,
        followup_context,
        *,
        min_confidence: float = _FAST_EXISTING_ANSWER_MIN_CONFIDENCE,
    ) -> TaskResult | None:
        question = route_content.strip()
        if not question or self._bug_followup_requires_fresh_analysis(question):
            return None
        evidence_lines = self._rank_existing_bug_evidence(question, followup_context)
        if not evidence_lines:
            return None
        confidence, confidence_reason = self._existing_bug_answer_confidence(question, evidence_lines)
        if confidence < min_confidence:
            return None
        top_evidence = evidence_lines[:5]
        primary = top_evidence[0]
        bullets = "\n".join(f"- {line}" for line in top_evidence)
        message = (
            "## 结论摘要\n"
            f"- {primary}\n"
            "- 基于已有报告/摘要可直接回答，本轮未重新下载日志，也未重新调用本地 Agent。\n\n"
            "## 关键证据\n"
            f"{bullets}\n\n"
            "## 建议动作\n"
            "- 如果你希望重新跑日志、补源码证据或修正既有结论，请明确回复“重新分析/基于源码重跑”。"
        )
        return TaskResult(
            success=True,
            message=message,
            details={
                "mode": "bug_followup_existing_answer",
                "answer_source": "existing_context",
                "answer_confidence": confidence,
                "answer_confidence_reason": confidence_reason,
                "followup_text": route_content,
                "delivery": "reply",
            },
        )

    def _bug_followup_requires_fresh_analysis(self, text: str) -> bool:
        lowered = text.casefold()
        force_terms = tuple(term.casefold() for term in self.config.bug_analysis.force_reanalysis_terms)
        return any(term in lowered for term in force_terms)

    def _rank_existing_bug_evidence(self, question: str, followup_context) -> list[str]:
        tokens = self._fast_answer_tokens(question)
        if not tokens:
            return []
        sources: list[tuple[int, str]] = [
            (3, str(getattr(followup_context, "summary_text", "") or "")),
            (2, str(getattr(followup_context, "report_excerpt", "") or "")),
        ]
        for item in getattr(followup_context, "history", []) or []:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").casefold()
            if role == "assistant":
                sources.append((1, str(item.get("content") or "")))
        ranked: list[tuple[int, int, int, str]] = []
        seen: set[str] = set()
        for source_weight, context_text in sources:
            for index, raw_line in enumerate(context_text.splitlines()):
                line = self._clean_existing_answer_line(raw_line)
                if not line or line in seen or self._is_question_echo(line, question):
                    continue
                score = self._score_existing_answer_line(line, tokens)
                if score <= 0 and source_weight >= 2 and self._looks_like_existing_evidence(line):
                    score = 1
                if score <= 0:
                    continue
                seen.add(line)
                ranked.append((score, source_weight, -index, line))
        ranked.sort(reverse=True)
        return [line for score, _source_weight, _index, line in ranked if score >= 1][:8]

    def _existing_bug_answer_confidence(self, question: str, evidence_lines: list[str]) -> tuple[float, str]:
        evidence_text = "\n".join(evidence_lines).casefold()
        domain_terms = self._fast_answer_domain_terms(question)
        missing_domain_terms = [term for term in domain_terms if term.casefold() not in evidence_text]
        if missing_domain_terms:
            return 0.45, f"missing_domain_terms={','.join(missing_domain_terms[:4])}"
        tokens = self._fast_answer_tokens(question)
        if not tokens:
            return 0.0, "no_question_tokens"
        matched = [token for token in tokens if token.casefold() in evidence_text]
        coverage = len(matched) / max(1, len(tokens))
        if len(evidence_lines) >= 2 and coverage >= 0.35:
            return 0.9, f"coverage={coverage:.2f};domain_terms={len(domain_terms)}"
        if coverage >= 0.5:
            return 0.82, f"coverage={coverage:.2f};domain_terms={len(domain_terms)}"
        return 0.6, f"coverage={coverage:.2f};domain_terms={len(domain_terms)}"

    def _fast_answer_domain_terms(self, question: str) -> list[str]:
        terms: list[str] = []
        for match in re.finditer(r"SIGNAL_[A-Za-z0-9_]+|[A-Z][A-Z0-9]+(?:_[A-Z0-9]+)+", question):
            self._append_answerability_term(terms, match.group(0))
        for match in re.finditer(r"[A-Za-z][A-Za-z0-9_]{2,}", question):
            token = match.group(0)
            if token.casefold() not in _FAST_ANSWER_LATIN_DOMAIN_STOP_TERMS:
                self._append_answerability_term(terms, token)
        cleaned = question
        for stop in sorted(_FAST_ANSWER_DOMAIN_STOP_TERMS, key=len, reverse=True):
            cleaned = cleaned.replace(stop, " ")
        for match in re.finditer(r"[\u4e00-\u9fff]{2,12}", cleaned):
            self._append_answerability_term(terms, match.group(0))
        return terms[:8]

    def _is_question_echo(self, line: str, question: str) -> bool:
        line_norm = self._normalize_fast_answer_text(line)
        question_norm = self._normalize_fast_answer_text(question)
        if not line_norm or not question_norm:
            return False
        return line_norm == question_norm or line_norm in question_norm or question_norm in line_norm

    def _looks_like_existing_evidence(self, line: str) -> bool:
        lowered = line.casefold()
        return bool(
            re.search(r"\d{1,2}:\d{2}:\d{2}", line)
            or "`" in line
            or "/" in line
            or "true" in lowered
            or "false" in lowered
            or any(marker in line for marker in ("证据", "日志", "记录", "显示", "命中", "字段"))
        )

    def _normalize_fast_answer_text(self, text: str) -> str:
        cleaned = re.sub(r"<[^>]+>", " ", text.casefold())
        cleaned = re.sub(r"[`*_#>\[\]（）()，,。！？!?：:；;、/|\s\-–—0-9.]+", "", cleaned)
        return cleaned.strip()

    def _fast_answer_tokens(self, question: str) -> list[str]:
        normalized = re.sub(r"[`*_#>\[\]（）()，,。！？!?：:；;、/|]+", " ", question.casefold())
        tokens: set[str] = set()
        for token in re.findall(r"[a-z0-9_+\-.]{2,}|[\u4e00-\u9fff]{2,}", normalized):
            if token in _FAST_ANSWER_STOP_TOKENS:
                continue
            if re.fullmatch(r"[\u4e00-\u9fff]+", token):
                for i in range(max(1, len(token) - 1)):
                    gram = token[i : i + 2]
                    if gram and gram not in _FAST_ANSWER_STOP_TOKENS:
                        tokens.add(gram)
            else:
                tokens.add(token)
        return sorted(tokens, key=len, reverse=True)

    def _clean_existing_answer_line(self, line: str) -> str:
        cleaned = re.sub(r"<[^>]+>", " ", line.strip())
        cleaned = re.sub(r"^[\s>*#\-–—0-9.、]+", "", cleaned).strip()
        cleaned = re.sub(r"\s+", " ", cleaned)
        if not cleaned or len(cleaned) < 8:
            return ""
        if len(cleaned) > 500:
            cleaned = cleaned[:499].rstrip() + "…"
        if cleaned.count("{") + cleaned.count("}") > 2:
            return ""
        return cleaned

    def _score_existing_answer_line(self, line: str, tokens: list[str]) -> int:
        lowered = line.casefold()
        score = 0
        for token in tokens:
            if token and token in lowered:
                score += 2 if len(token) >= 3 else 1
        if any(marker in lowered for marker in ("结论", "证据", "原因", "置信", "根因")):
            score += 1
        return score

    def _finalize_followup_reply(self, event: LarkEvent, result: TaskResult, followup_context, route_content: str) -> TaskResult:
        result.details["delivery"] = "reply"
        result.details["conversation_root_message_id"] = followup_context.root_message_id
        result.details.setdefault("followup_text", route_content)
        is_bug_followup = "bug" in str(followup_context.mode).casefold()
        if followup_context.report_url:
            result.details.setdefault("published_report_url", followup_context.report_url)
            result.details.setdefault("report_url", followup_context.report_url)
        if result.success and followup_context.report_url and followup_context.report_url not in result.message:
            if result.message.strip():
                result.message = f"{result.message}\n\n报告链接：{followup_context.report_url}"
            else:
                result.message = f"报告链接：{followup_context.report_url}"
        if result.success and not is_bug_followup:
            self.conversation_store.remember(
                root_message_id=followup_context.root_message_id,
                chat_id=followup_context.chat_id,
                mode=str(result.details.get("mode") or followup_context.mode or ""),
                request_text=followup_context.request_text,
                summary_text=result.message,
                report_url=followup_context.report_url,
                report_excerpt=str(getattr(followup_context, "report_excerpt", "") or ""),
            )
            self.conversation_store.rewrite_branch(
                followup_context.root_message_id,
                base_history=list(getattr(followup_context, "history", []) or []),
                user_text=route_content,
                assistant_text=result.message,
            )
        if result.success and is_bug_followup:
            self.conversation_store.append_exchange(
                followup_context.root_message_id,
                user_text=route_content,
                assistant_text=result.message,
            )
        self._send_result(event, result)
        return result

    def _link_delivery_summary(self, message: str) -> str:
        lines = []
        for raw_line in (message or "").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            lowered = line.casefold()
            if lowered.startswith(("html", "json", "reports:", "metadata:", "job:", "日志路径:", "耗时:", "结果文件:")):
                continue
            if "/jobs/" in line or "\\jobs\\" in line:
                continue
            lines.append(line)
        summary = "\n".join(lines[:8]).strip()
        if len(summary) > 1200:
            summary = summary[:1199].rstrip() + "…"
        return summary or "分析完成"

    def _fallback_request_text(self, result: TaskResult) -> str:
        for key in ("user_request_text", "prompt"):
            value = result.details.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return result.message.splitlines()[0].strip() if result.message else ""

    def _reply_payload(self, event: LarkEvent, text: str) -> str:
        if event.chat_type == "group" and self.config.lark.mention_sender_in_group and event.sender_id:
            return f'<at user_id="{event.sender_id}"></at> {text}'
        return text

    def _resolve_followup_context(self, event: LarkEvent):
        context = self.conversation_store.find(event)
        if context is not None:
            return context
        reference_ids = self._fetch_followup_reference_ids(event)
        for key in reference_ids:
            context = self.conversation_store.lookup(key)
            if context is not None:
                return context
        for key in self._followup_context_candidate_ids(event, reference_ids):
            context = self._context_from_activity_session(key, event=event)
            if context is not None:
                return context
        for key in self._followup_context_candidate_ids(event, reference_ids):
            context = self._context_from_fetched_message(key, event=event)
            if context is not None:
                return context
        return None

    def _followup_context_candidate_ids(self, event: LarkEvent, reference_ids: list[str]) -> list[str]:
        candidates = [event.reply_to, event.root_id, event.parent_id, *reference_ids]
        result: list[str] = []
        for candidate in candidates:
            normalized = str(candidate or "").strip()
            if normalized and normalized not in result:
                result.append(normalized)
        return result

    def _context_from_activity_session(self, message_id: str, *, event: LarkEvent) -> ConversationContext | None:
        session = self.activity_store.get_session(message_id)
        if not session:
            return None
        details = session.get("details", {})
        if not isinstance(details, dict):
            details = {}
        request_text = str(details.get("user_request_text") or session.get("content") or "").strip()
        bug_url = str(details.get("bug_url") or self._bug_url_from_request_text(request_text)).strip()
        mode = str(session.get("mode") or details.get("mode") or "").strip()
        if bug_url and "bug" not in mode.casefold():
            mode = "bug_analysis"
        if mode not in self._threaded_reply_context_modes():
            return None
        summary_text = str(session.get("message") or "").strip()
        report_url = str(session.get("report_url") or details.get("published_report_url") or details.get("report_url") or "")
        return ConversationContext(
            root_message_id=message_id,
            chat_id=str(session.get("chat_id") or event.chat_id),
            mode=mode,
            request_text=request_text,
            summary_text=summary_text,
            report_url=report_url,
            report_excerpt=summary_text,
            history=[],
            created_at=str(session.get("started_at") or ""),
            updated_at=str(session.get("updated_at") or session.get("finished_at") or ""),
        )

    def _context_from_fetched_message(self, message_id: str, *, event: LarkEvent) -> ConversationContext | None:
        fetched = self.lark_client.fetch_message(message_id)
        if fetched.returncode != 0:
            return None
        for message in self._extract_message_records(fetched.stdout):
            current_id = str(message.get("message_id") or "").strip()
            if current_id and current_id != message_id:
                continue
            request_text = self._message_content_text(message).strip()
            bug_request = parse_bug_request(request_text, bug_url_re=self.bug_url_re)
            if not bug_request.triggered:
                continue
            return ConversationContext(
                root_message_id=message_id,
                chat_id=str(message.get("chat_id") or event.chat_id),
                mode="bug_analysis",
                request_text=request_text,
                summary_text="",
                report_url="",
                report_excerpt="",
                history=[],
                created_at=str(message.get("create_time") or ""),
                updated_at=str(message.get("update_time") or message.get("create_time") or ""),
            )
        for message in self._extract_message_records(fetched.stdout):
            current_id = str(message.get("message_id") or "").strip()
            if current_id and current_id != message_id:
                continue
            context = self._context_from_artifact_message(message, event=event)
            if context is not None:
                if current_id:
                    self._remember_conversation_alias(current_id, context.root_message_id)
                return context
        return None

    def _context_from_artifact_message(self, message: dict[str, object], *, event: LarkEvent) -> ConversationContext | None:
        artifact_name = self._message_artifact_name(message)
        if not artifact_name:
            return None
        session = self.activity_store.find_session_by_artifact_name(
            artifact_name,
            chat_id=str(message.get("chat_id") or event.chat_id),
        )
        if not session:
            return None
        session_id = str(session.get("session_id") or "").strip()
        if not session_id:
            return None
        return self._context_from_activity_session(session_id, event=event)

    def _message_artifact_name(self, message: dict[str, object]) -> str:
        for key in ("name", "file_name", "filename"):
            value = message.get(key)
            if isinstance(value, str) and value.strip():
                return Path(value.strip()).name
        content = message.get("content")
        return self._artifact_name_from_value(content)

    def _artifact_name_from_value(self, value: object) -> str:
        if isinstance(value, dict):
            for key in ("name", "file_name", "filename"):
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    return Path(candidate.strip()).name
            for nested in value.values():
                found = self._artifact_name_from_value(nested)
                if found:
                    return found
            return ""
        if not isinstance(value, str):
            return ""
        text = value.strip()
        if not text:
            return ""
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if parsed is not None:
            found = self._artifact_name_from_value(parsed)
            if found:
                return found
        match = re.search(r'\b(?:name|file_name|filename)=["\']([^"\']+)["\']', text)
        if match:
            return Path(match.group(1).strip()).name
        return ""

    def _fetch_followup_reference_ids(self, event: LarkEvent) -> list[str]:
        pending = [value for value in [event.reply_to, event.parent_id, event.root_id] if value]
        discovered: list[str] = []
        if not pending and event.message_id:
            fetched_current = self.lark_client.fetch_message(event.message_id)
            if fetched_current.returncode == 0:
                for candidate in self._extract_message_reference_ids(fetched_current.stdout):
                    if not candidate or candidate == event.message_id:
                        continue
                    if candidate not in discovered:
                        discovered.append(candidate)
                    pending.append(candidate)
        visited: set[str] = set()
        while pending and len(visited) < 6:
            current = pending.pop(0)
            if not current or current in visited:
                continue
            visited.add(current)
            fetched = self.lark_client.fetch_message(current)
            if fetched.returncode != 0:
                continue
            for candidate in self._extract_message_reference_ids(fetched.stdout):
                if not candidate or candidate in visited:
                    continue
                if candidate not in discovered:
                    discovered.append(candidate)
                pending.append(candidate)
        return discovered

    def _extract_message_reference_ids(self, payload_text: str) -> list[str]:
        messages = self._extract_message_records(payload_text)
        ids: list[str] = []
        for message in messages:
            for key in ("message_id", "reply_to", "root_id", "parent_id", "thread_id"):
                value = message.get(key)
                if isinstance(value, str) and value.strip():
                    ids.append(value.strip())
        return ids

    def _extract_message_records(self, payload_text: str) -> list[dict[str, object]]:
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, dict):
            return []
        data = payload.get("data", {})
        if not isinstance(data, dict):
            return []
        messages = data.get("messages") or data.get("items") or data.get("message")
        if isinstance(messages, dict):
            messages = [messages]
        if not isinstance(messages, list):
            return []
        return [message for message in messages if isinstance(message, dict)]

    def _message_content_text(self, message: dict[str, object]) -> str:
        content = message.get("content")
        if content is None:
            content = message.get("text") or ""
        if isinstance(content, dict):
            value = content.get("text") or content.get("content")
            return str(value if value is not None else content)
        if not isinstance(content, str):
            return str(content)
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            return content
        if isinstance(parsed, dict):
            value = parsed.get("text") or parsed.get("content")
            if value is not None:
                return str(value)
        return content

    def _is_contextual_followup(self, event: LarkEvent, route_content: str) -> bool:
        if not route_content.strip():
            return False
        return bool(event.reply_to or event.parent_id or event.root_id or event.thread_id)

    def _is_followup_intent(self, route_content: str) -> bool:
        action = parse_followup_action(route_content)
        if action in {"retry", "continue"}:
            return True
        lowered = route_content.casefold()
        return any(
            term in lowered
            for term in (
                "修正",
                "修复问题时间",
                "更正",
                "改成",
                "修改",
                "重新分析",
                "重新跑",
                "重跑",
                "再分析",
                "上次",
                "上一条",
                "这个报告",
                "这份报告",
                "问题时间",
                "故障时间",
                "时间点",
                "用你之前下载",
                "之前下载",
                "下载下来",
                "之前的日志",
                "日志搜索",
                "logd",
                "关键字",
                "卡顿skill",
                "卡顿 skill",
                "系统卡顿报告",
            )
        )

    def _is_bug_reanalysis_followup(self, route_content: str, followup_context) -> bool:
        return self._bug_reanalysis_decision(route_content, followup_context).should_reanalyze

    def _bug_reanalysis_decision(self, route_content: str, followup_context) -> _BugReanalysisDecision:
        if "bug" not in str(followup_context.mode).casefold():
            return _BugReanalysisDecision(False, False)
        previous_session = self.activity_store.get_session(followup_context.root_message_id) or {}
        agent_decision = self.bug_runner.decide_bug_followup(
            followup_text=route_content,
            previous_context=followup_context,
            previous_session=previous_session,
        )
        if agent_decision is not None:
            return _BugReanalysisDecision(
                agent_decision.should_reanalyze,
                agent_decision.force_rerun,
                plans=agent_decision.plans or None,
                skill_name=agent_decision.skill_name,
                skill_label=agent_decision.skill_label,
                source=agent_decision.source,
                reason=agent_decision.reason,
                provider=agent_decision.provider,
            )
        if not self._existing_bug_context_can_answer(route_content, followup_context):
            return _BugReanalysisDecision(
                True,
                True,
                plans=None,
                skill_name="",
                skill_label="",
                source="minimal_fallback",
                reason="现有上下文不足以直接回答，沿用上一轮分析类型重分析。",
            )
        return _BugReanalysisDecision(False, False)

    def _existing_bug_context_can_answer(self, route_content: str, followup_context) -> bool:
        question = route_content.strip()
        if not question:
            return True
        lowered = question.casefold()
        if not any(term.casefold() in lowered for term in _FOLLOWUP_INTENT_TERMS):
            return True
        context_text = "\n".join(
            [
                str(getattr(followup_context, "request_text", "") or ""),
                str(getattr(followup_context, "summary_text", "") or ""),
                str(getattr(followup_context, "report_excerpt", "") or ""),
            ]
        )
        if not context_text.strip():
            return False
        context_folded = context_text.casefold()
        missing = [
            term
            for term in self._followup_answerability_terms(question)
            if term.casefold() not in context_folded
        ]
        return not missing

    def _followup_answerability_terms(self, text: str) -> list[str]:
        terms: list[str] = []
        for match in re.finditer(r"SIGNAL_[A-Za-z0-9_]+|[A-Z][A-Z0-9]+(?:_[A-Z0-9]+)+", text):
            self._append_answerability_term(terms, match.group(0))
        for match in re.finditer(r"[A-Za-z][A-Za-z0-9_]{2,}", text):
            token = match.group(0)
            if token.upper() == "SIGNAL" or token.isdigit():
                continue
            self._append_answerability_term(terms, token)
        for match in re.finditer(r"[\u4e00-\u9fff]{2,12}", text):
            token = match.group(0)
            if token in _FOLLOWUP_STOP_TERMS:
                continue
            if any(stop in token for stop in _FOLLOWUP_STOP_TERMS) and len(token) <= 4:
                continue
            self._append_answerability_term(terms, token)
        return terms[:8]

    def _append_answerability_term(self, terms: list[str], term: str) -> None:
        normalized = term.strip(" \t\r\n，。；;,.、:：?？!！()（）[]【】")
        if not normalized:
            return
        if normalized.casefold() in {item.casefold() for item in terms}:
            return
        terms.append(normalized)

    def _latest_job_mtime(self, job_dir: Path) -> float:
        latest = 0.0
        try:
            for child in job_dir.rglob("*"):
                if not child.is_file():
                    continue
                try:
                    child_mtime = child.stat().st_mtime
                except OSError:
                    continue
                if child_mtime > latest:
                    latest = child_mtime
        except OSError:
            latest = 0.0
        if latest == 0.0:
            try:
                latest = job_dir.stat().st_mtime
            except OSError:
                latest = 0.0
        return latest

    def _remove_job_dir(self, job_dir: Path) -> bool:
        try:
            shutil.rmtree(job_dir, onerror=self._handle_rmtree_error)
            return True
        except OSError:
            return False

    def _handle_rmtree_error(self, func, path, exc_info) -> None:
        try:
            os.chmod(path, stat.S_IRWXU)
        except OSError:
            pass
        func(path)
