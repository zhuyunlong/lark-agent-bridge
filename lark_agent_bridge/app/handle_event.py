from __future__ import annotations

from datetime import datetime, timezone
import html
from pathlib import Path
import re
import threading
import time
from typing import Callable

from ._shared import *  # noqa: F401,F403
from .conversation_resolver import ConversationResolver
from ..conversation_input import build_resolved_conversation_input, conversation_input_snapshot


from .routes import _RoutesMixin
from .delivery import _DeliveryMixin
from .mention import _MentionMixin
from .progress_cards import _ProgressCardsMixin


_INBOUND_INTERACTION_DEDUPE_TTL_SECONDS = 600.0
_BOT_MENU_HELP_KEYS = {"menu", "help"}
_REACTION_CREATED_EVENT_TYPE = "im.message.reaction.created_v1"
_REACTION_DELETED_EVENT_TYPE = "im.message.reaction.deleted_v1"
_MESSAGE_RECALLED_EVENT_TYPE = "im.message.recalled_v1"
_APP_SERVER_CONTINUE_REACTION_TYPES = {"thumbsup", "like"}
_APP_SERVER_REACTION_STEER_PROMPT = "用户通过飞书点赞表示认可当前方向，请继续深入。"


class _HandleEventMixin(_RoutesMixin, _DeliveryMixin, _MentionMixin, _ProgressCardsMixin):
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
        app_server_investigation_runner: AppServerInvestigationRunner | None = None,
        requirement_analysis_runner: RequirementAnalysisRunner | None = None,
        source_analysis_runner: RepositorySourceAnalysisRunner | None = None,
        report_publisher: HtmlReportPublisher | None = None,
        report_http_server: ReportHttpServer | None = None,
        conversation_store: ConversationContextStore | None = None,
        activity_store: AgentActivityStore | None = None,
        progress_callback: Callable[[dict[str, object]], None] | None = None,
        warmup_codegraph: bool = True,
    ) -> None:
        self.config = config
        self.lark_client = lark_client or LarkClient(config)
        self.state_store = state_store or EventStateStore(
            config.data_dir / "state" / "seen_events.jsonl",
            max_seen_events=config.state.max_seen_events,
        )
        self.conversation_store = conversation_store or ConversationContextStore(
            config.data_dir / "state" / "conversation_contexts.json",
            max_history_turns=config.omlx_chat.followup_max_history_turns,
        )
        self.activity_store = activity_store or AgentActivityStore(
            config.data_dir / "state" / "agent_activity.json",
            max_progress_events=config.state.max_progress_events,
        )
        self.progress_callback = progress_callback
        self._progress_cards: dict[str, dict[str, object]] = {}
        # Guards _progress_cards dict structure (get/setitem/pop/iterate) against
        # concurrent worker threads and the daemon cleanup loop. Held only around
        # dict access — never while sending cards over the network.
        self._progress_cards_lock = threading.Lock()
        self._app_server_controls: dict[str, CodexAppServerTurnController] = {}
        self._app_server_controls_lock = threading.Lock()
        self._recent_inbound_interactions: dict[str, float] = {}
        self._recent_inbound_interactions_lock = threading.Lock()
        self._progress_cards_max_age_seconds = config.state.progress_card_max_age_seconds
        self._progress_card_stream_update_interval_seconds = 5.0
        self.process_watchdog = ProcessWatchdog(
            max_idle_seconds=config.health.watchdog_max_idle_seconds,
        )
        self.health_monitor = HealthMonitor(
            data_dir=config.data_dir,
            process_watchdog=self.process_watchdog,
            max_event_lag_seconds=config.health.max_event_lag_seconds,
            max_disk_usage_percent=config.health.max_disk_usage_percent,
        )
        self._restore_daemon_health_pid()
        self.case_store = CaseStore(config.data_dir / "state" / "cases.json")
        self.skill_manager = SkillManager(config)
        self.approval_store = ApprovalStore(config.data_dir / "state" / "approvals.json")
        self.version_store = ReportVersionStore(config.data_dir / "state" / "report_versions.json")
        self.workflow_archiver = WorkflowArchiver(config, self.lark_client)
        self.signal_resolver = SignalResolver(
            config.guideengine_repo,
            cache_dir=config.data_dir / "cache",
            cache_ttl_seconds=config.signal_resolver.cache_ttl_seconds,
            preferred_paths=config.signal_resolver.preferred_paths or None,
            source_suffixes=config.signal_resolver.source_suffixes or None,
        )
        self.bug_url_re = build_bug_url_re(config.bug_url_domains) if config.bug_url_domains else None
        self.escalation_checker = EscalationChecker()
        self.notification_history = NotificationHistory(config.data_dir / "state" / "notification_history.json")
        self.lifecycle_store = LifecycleStore(max_active=config.state.lifecycle_max_active)
        self.report_publisher = report_publisher or HtmlReportPublisher(config)
        self.knowledge_service = knowledge_service or KnowledgeService(
            config,
            warmup_codegraph=warmup_codegraph,
        )
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
            lifecycle_store=self.lifecycle_store,
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
        self.app_server_investigation_runner = (
            app_server_investigation_runner
            or AppServerInvestigationRunner(
                config,
                bug_runner=self.bug_runner,
                skill_manager=self.skill_manager,
            )
        )
        self.source_analysis_runner = source_analysis_runner or RepositorySourceAnalysisRunner(
            config,
            bug_runner=self.bug_runner,
        )
        self.requirement_analysis_runner = requirement_analysis_runner or RequirementAnalysisRunner(
            config,
            source_analysis_runner=self.source_analysis_runner,
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
            rom_version_runner=self.rom_version_runner,
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
        if self._looks_like_bot_menu_payload(payload):
            return self.handle_bot_menu_payload(payload)
        if self._looks_like_reaction_payload(payload):
            return self.handle_reaction_payload(payload)
        if self._looks_like_message_recalled_payload(payload):
            return self.handle_message_recalled_payload(payload)
        return self.handle_event_payload(payload)
    def handle_event_payload(self, payload: dict[str, object]) -> TaskResult:
        return self.handle_event(LarkEvent.from_dict(payload))
    def handle_card_action_payload(self, payload: dict[str, object]) -> TaskResult:
        return self.handle_card_action(CardActionEvent.from_dict(payload))
    def handle_reaction_payload(self, payload: dict[str, object]) -> TaskResult:
        return self.handle_reaction(ReactionEvent.from_dict(payload))
    def handle_message_recalled_payload(self, payload: dict[str, object]) -> TaskResult:
        return self.handle_message_recalled(MessageRecalledEvent.from_dict(payload))
    def handle_bot_menu_payload(self, payload: dict[str, object]) -> TaskResult:
        menu_event = BotMenuEvent.from_dict(payload)
        if not menu_event.is_valid:
            return TaskResult(
                success=False,
                message="飞书菜单事件缺少有效的 menu key 或操作者。",
                error_code="invalid_bot_menu_event",
                details={"mode": "bot_menu"},
            )
        duplicate_key = self._mark_inbound_interaction_seen(
            inbound_request_id=menu_event.inbound_request_id,
            event_id=menu_event.event_id,
        )
        if duplicate_key:
            return self._duplicate_inbound_result(duplicate_key)
        menu_key = menu_event.menu_key.strip().casefold()
        if menu_key in _BOT_MENU_HELP_KEYS:
            return self.handle_event(menu_event.to_lark_event(content="help"))
        if menu_key == "stop":
            return self._handle_bot_menu_stop(menu_event)
        return TaskResult(
            success=False,
            message=f"暂不支持的飞书菜单：{menu_event.menu_key}",
            error_code="unsupported_bot_menu",
            details={"mode": "bot_menu", "menu_key": menu_event.menu_key},
        )
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
    def _looks_like_bot_menu_payload(self, payload: dict[str, object]) -> bool:
        event_body = payload.get("event") or payload
        if not isinstance(event_body, dict):
            return False
        header = payload.get("header") or {}
        event_type = ""
        if isinstance(header, dict):
            event_type = str(header.get("event_type") or "")
        event_type = str(payload.get("event_type") or event_type or event_body.get("event_type") or "").strip()
        if event_type == "application.bot.menu_v6":
            return True
        return bool(event_body.get("event_key") or event_body.get("menu_key")) and bool(event_body.get("operator") or event_body.get("operator_id"))
    def _looks_like_reaction_payload(self, payload: dict[str, object]) -> bool:
        return self._payload_event_type(payload) in {
            _REACTION_CREATED_EVENT_TYPE,
            _REACTION_DELETED_EVENT_TYPE,
        }
    def _looks_like_message_recalled_payload(self, payload: dict[str, object]) -> bool:
        return self._payload_event_type(payload) == _MESSAGE_RECALLED_EVENT_TYPE
    def _payload_event_type(self, payload: dict[str, object]) -> str:
        event_body = payload.get("event") or payload
        if not isinstance(event_body, dict):
            event_body = {}
        header = payload.get("header") or {}
        if not isinstance(header, dict):
            header = {}
        return str(payload.get("event_type") or header.get("event_type") or event_body.get("event_type") or "").strip()
    def _mark_inbound_interaction_seen(self, *, inbound_request_id: str = "", event_id: str = "") -> str:
        key = self._inbound_interaction_dedupe_key(
            inbound_request_id=inbound_request_id,
            event_id=event_id,
        )
        if not key:
            return ""
        now = time.monotonic()
        with self._recent_inbound_interactions_lock:
            expired = [
                existing_key
                for existing_key, expires_at in self._recent_inbound_interactions.items()
                if expires_at <= now
            ]
            for existing_key in expired:
                self._recent_inbound_interactions.pop(existing_key, None)
            if self._recent_inbound_interactions.get(key, 0.0) > now:
                return key
            self._recent_inbound_interactions[key] = now + _INBOUND_INTERACTION_DEDUPE_TTL_SECONDS
        return ""
    def _inbound_interaction_dedupe_key(self, *, inbound_request_id: str = "", event_id: str = "") -> str:
        inbound_request_id = str(inbound_request_id or "").strip()
        if inbound_request_id:
            return f"request:{inbound_request_id}"
        event_id = str(event_id or "").strip()
        if event_id:
            return f"event:{event_id}"
        return ""
    def _duplicate_inbound_result(self, dedupe_key: str) -> TaskResult:
        return TaskResult(
            success=True,
            message=f"duplicate inbound interaction skipped: {dedupe_key}",
            skipped=True,
            details={"mode": "duplicate_inbound", "dedupe_key": dedupe_key},
        )
    def handle_card_action(self, action_event: CardActionEvent) -> TaskResult:
        duplicate_key = self._mark_inbound_interaction_seen(
            inbound_request_id=action_event.inbound_request_id,
            event_id=action_event.event_id,
        )
        if duplicate_key:
            return self._duplicate_inbound_result(duplicate_key)
        stale_result = self._stale_card_action_result(action_event)
        if stale_result is not None:
            return stale_result
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
    def _stale_card_action_result(self, action_event: CardActionEvent) -> TaskResult | None:
        expected = self._expected_card_action_stamp(action_event)
        if not expected:
            return None
        expected_lifecycle = str(expected.get("card_lifecycle_id") or "").strip()
        expected_revision = self._int_card_action_stamp(expected.get("action_revision"))
        received_lifecycle = action_event.card_lifecycle_id.strip()
        received_revision = action_event.action_revision
        if expected_lifecycle and received_lifecycle and expected_lifecycle != received_lifecycle:
            return self._stale_card_action_skip(
                action_event,
                reason="card_lifecycle_mismatch",
                expected_lifecycle=expected_lifecycle,
                expected_revision=expected_revision,
            )
        if expected_revision > 0 and received_revision > 0 and expected_revision != received_revision:
            return self._stale_card_action_skip(
                action_event,
                reason="action_revision_mismatch",
                expected_lifecycle=expected_lifecycle,
                expected_revision=expected_revision,
            )
        return None
    def _expected_card_action_stamp(self, action_event: CardActionEvent) -> dict[str, object]:
        candidates = [
            action_event.root_message_id,
            action_event.message_id,
            action_event.open_message_id,
        ]
        with self._progress_cards_lock:
            for candidate in candidates:
                state = self._progress_cards.get(str(candidate or "").strip())
                if isinstance(state, dict):
                    stamp = self._card_action_stamp_from_mapping(state)
                    if stamp:
                        return stamp
            wanted = {str(candidate or "").strip() for candidate in candidates if str(candidate or "").strip()}
            for state in self._progress_cards.values():
                if not isinstance(state, dict):
                    continue
                if str(state.get("message_id") or "").strip() in wanted:
                    stamp = self._card_action_stamp_from_mapping(state)
                    if stamp:
                        return stamp
        for candidate in candidates:
            session = self.activity_store.get_session(str(candidate or "").strip())
            if not isinstance(session, dict):
                continue
            stamp = self._card_action_stamp_from_mapping(session)
            if stamp:
                return stamp
        return {}
    def _card_action_stamp_from_mapping(self, payload: dict[str, object]) -> dict[str, object]:
        details = payload.get("details")
        if not isinstance(details, dict):
            details = {}
        lifecycle = str(
            details.get("card_lifecycle_id")
            or details.get("card_daemon_lifecycle_id")
            or payload.get("card_lifecycle_id")
            or payload.get("card_daemon_lifecycle_id")
            or ""
        ).strip()
        revision = self._int_card_action_stamp(
            details.get("action_revision")
            or details.get("card_action_revision")
            or payload.get("action_revision")
            or payload.get("card_action_revision")
        )
        if not lifecycle and revision <= 0:
            return {}
        return {"card_lifecycle_id": lifecycle, "action_revision": revision}
    def _int_card_action_stamp(self, value: object) -> int:
        if isinstance(value, bool):
            return 0
        if isinstance(value, int):
            return value if value > 0 else 0
        text = str(value or "").strip()
        if not text.isdigit():
            return 0
        return int(text)
    def _stale_card_action_skip(
        self,
        action_event: CardActionEvent,
        *,
        reason: str,
        expected_lifecycle: str,
        expected_revision: int,
    ) -> TaskResult:
        return TaskResult(
            success=True,
            message="stale card action skipped",
            error_code="stale_card_action",
            skipped=True,
            details={
                "mode": "stale_card_action",
                "action": action_event.action,
                "reason": reason,
                "expected_card_lifecycle_id": expected_lifecycle,
                "received_card_lifecycle_id": action_event.card_lifecycle_id,
                "expected_action_revision": expected_revision,
                "received_action_revision": action_event.action_revision,
            },
        )
    def handle_reaction(self, reaction_event: ReactionEvent) -> TaskResult:
        self.health_monitor.record_event_processed()
        if not reaction_event.is_valid:
            return TaskResult(
                success=False,
                message="飞书 reaction 事件缺少有效的消息或 reaction 类型。",
                error_code="invalid_reaction_event",
                details={"mode": "reaction"},
            )
        duplicate_key = self._mark_inbound_interaction_seen(
            inbound_request_id=reaction_event.inbound_request_id,
            event_id=reaction_event.event_id,
        )
        if duplicate_key:
            return self._duplicate_inbound_result(duplicate_key)
        if reaction_event.is_deleted:
            return TaskResult(
                success=True,
                message="reaction delete ignored",
                skipped=True,
                details={
                    "mode": "reaction",
                    "action": "ignored",
                    "reason": "reaction_deleted",
                    "message_id": reaction_event.message_id,
                },
            )
        root_message_id = self._app_server_control_root_for_reply(reaction_event.message_id)
        if not root_message_id:
            return TaskResult(
                success=True,
                message="reaction ignored because no active app-server control matched",
                skipped=True,
                details={
                    "mode": "reaction",
                    "action": "ignored",
                    "reason": "no_active_app_server_control",
                    "message_id": reaction_event.message_id,
                },
            )
        if reaction_event.reaction_type.casefold() not in _APP_SERVER_CONTINUE_REACTION_TYPES:
            return TaskResult(
                success=True,
                message="reaction type ignored for app-server control",
                skipped=True,
                details={
                    "mode": "app_server_reaction",
                    "action": "ignored",
                    "reason": "unsupported_reaction_type",
                    "reaction_type": reaction_event.reaction_type,
                    "app_server_control_root_message_id": root_message_id,
                },
            )
        with self._app_server_controls_lock:
            control = self._app_server_controls.get(root_message_id)
        if control is None:
            return TaskResult(
                success=True,
                message="reaction ignored because app-server control is no longer active",
                skipped=True,
                details={
                    "mode": "app_server_reaction",
                    "action": "ignored",
                    "reason": "app_server_control_missing",
                    "app_server_control_root_message_id": root_message_id,
                },
            )
        if not control.steer(
            _APP_SERVER_REACTION_STEER_PROMPT,
            source_message_id=f"reaction:{reaction_event.event_id}",
            actor_id=reaction_event.operator_id,
        ):
            return TaskResult(
                success=False,
                message="reaction steer was rejected by the running app-server control.",
                error_code="app_server_control_rejected",
                details={
                    "mode": "app_server_reaction",
                    "action": "steer",
                    "app_server_control_root_message_id": root_message_id,
                },
            )
        self.activity_store.record_progress(
            {
                "session_id": root_message_id,
                "stage": "app_server_investigation_reaction_received",
                "message": "收到飞书点赞 reaction，已作为运行中认可信号并入 AI 自主分析。",
                "details": {
                    "executor": "飞书 reaction",
                    "reaction_type": reaction_event.reaction_type,
                    "source_message_id": reaction_event.message_id,
                    "operator_id": reaction_event.operator_id,
                },
            }
        )
        return TaskResult(
            success=True,
            message="app-server reaction steer accepted",
            skipped=True,
            details={
                "mode": "app_server_reaction",
                "action": "steer",
                "app_server_control_root_message_id": root_message_id,
                "reaction_type": reaction_event.reaction_type,
            },
        )
    def handle_message_recalled(self, recalled_event: MessageRecalledEvent) -> TaskResult:
        self.health_monitor.record_event_processed()
        if not recalled_event.is_valid:
            return TaskResult(
                success=False,
                message="飞书撤回事件缺少有效的消息 ID。",
                error_code="invalid_message_recalled_event",
                details={"mode": "message_recalled"},
            )
        duplicate_key = self._mark_inbound_interaction_seen(
            inbound_request_id=recalled_event.inbound_request_id,
            event_id=recalled_event.event_id,
        )
        if duplicate_key:
            return self._duplicate_inbound_result(duplicate_key)
        root_message_id = recalled_event.message_id
        with self._app_server_controls_lock:
            control = self._app_server_controls.get(root_message_id)
        if control is None:
            return TaskResult(
                success=True,
                message="message recall ignored because no active app-server root matched",
                skipped=True,
                details={
                    "mode": "message_recalled",
                    "action": "ignored",
                    "reason": "no_active_app_server_root",
                    "message_id": recalled_event.message_id,
                },
            )
        reason = "用户撤回了触发 AI 自主分析的飞书消息。"
        if not control.cancel(
            reason,
            source_message_id=f"recall:{recalled_event.event_id}",
            actor_id=recalled_event.operator_id,
        ):
            return TaskResult(
                success=False,
                message="撤回停止请求未被当前 AI 自主分析接收。",
                error_code="app_server_control_rejected",
                details={
                    "mode": "message_recalled",
                    "action": "cancel",
                    "app_server_control_root_message_id": root_message_id,
                },
            )
        self.activity_store.cancel_session(
            root_message_id,
            reason=reason,
            stage="app_server_investigation_cancel_requested",
            executor="飞书撤回",
            error_code="cancelled_by_user",
        )
        return TaskResult(
            success=True,
            message="message recall cancel accepted",
            skipped=True,
            details={
                "mode": "message_recalled",
                "action": "cancel",
                "app_server_control_root_message_id": root_message_id,
            },
        )
    def _handle_bot_menu_stop(self, menu_event: BotMenuEvent) -> TaskResult:
        event = menu_event.to_lark_event(content="停止")
        self.activity_store.record_event(event)
        self.health_monitor.record_event_processed()
        with self._app_server_controls_lock:
            active_controls = list(self._app_server_controls.items())
        if not active_controls:
            result = TaskResult(
                success=True,
                message="当前没有正在运行的 AI 自主分析。",
                skipped=True,
                details={"mode": "bot_menu", "action": "stop", "status": "noop"},
            )
            self.activity_store.record_result(event, result)
            if not self.config.dry_run:
                self._send_result(event, result)
            return result
        if len(active_controls) > 1:
            result = TaskResult(
                success=False,
                message="当前有多个 AI 自主分析正在运行，请回复对应进度卡片发送“停止”。",
                error_code="ambiguous_app_server_control",
                details={"mode": "bot_menu", "action": "stop", "active_count": len(active_controls)},
            )
            self.activity_store.record_result(event, result)
            if not self.config.dry_run:
                self._send_result(event, result)
            return result
        root_message_id, control = active_controls[0]
        reason = "用户通过飞书菜单停止当前 AI 自主分析。"
        if not control.cancel(reason, source_message_id=f"menu:{menu_event.event_id}", actor_id=menu_event.operator_id):
            result = TaskResult(
                success=False,
                message="停止请求未被当前 AI 自主分析接收。",
                error_code="app_server_control_rejected",
                details={"mode": "bot_menu", "action": "stop", "app_server_control_root_message_id": root_message_id},
            )
            self.activity_store.record_result(event, result)
            if not self.config.dry_run:
                self._send_result(event, result)
            return result
        self.activity_store.cancel_session(
            root_message_id,
            reason=reason,
            stage="app_server_investigation_cancel_requested",
            executor="飞书菜单",
            error_code="cancelled_by_user",
        )
        result = TaskResult(
            success=True,
            message="已收到，正在停止当前 AI 自主分析。",
            skipped=True,
            details={"mode": "bot_menu", "action": "stop", "app_server_control_root_message_id": root_message_id},
        )
        self.activity_store.record_result(event, result)
        if not self.config.dry_run:
            self._send_result(event, result)
        return result
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

        resolution = ConversationResolver(self).resolve(event)
        if not resolution.addressed:
            if not self.state_store.mark_seen(event):
                return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)
            return TaskResult(
                success=True,
                message="group message not addressed to this bot",
                skipped=True,
                details={"mode": "not_addressed"},
            )
        route_content = resolution.route_text
        followup_context = resolution.followup_context
        direct_reply_to = resolution.direct_reply_to

        # Phase 2: Policy gate
        decision = evaluate_event_policy(self.config, event)
        control_direct_reply_to = (
            direct_reply_to
            if event.chat_type == "group"
            else (event.reply_to or event.parent_id or event.root_id or "")
        )
        if decision.allowed:
            control_result = self._maybe_handle_app_server_control_event(
                event,
                route_content=route_content,
                direct_reply_to=control_direct_reply_to,
            )
            if control_result is not None:
                return control_result
        resource_view = self._resolve_conversation_resource_view(
            event,
            route_content=route_content,
            followup_context=followup_context,
            message_cache=resolution.message_cache,
        )
        referenced_resources = list(resource_view.preferred_resources)
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
        app_server_investigation_request = self._build_app_server_investigation_request(
            route_content,
            referenced_resources,
            event=event,
        )
        requirement_analysis_request = parse_requirement_analysis_request(route_content, bug_url_re=self.bug_url_re)
        source_analysis_request = parse_source_analysis_request(route_content, bug_url_re=self.bug_url_re)
        report_followup_request = parse_report_followup_request(route_content)
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

        control_result = self._maybe_handle_app_server_control_event(
            event,
            route_content=route_content,
            direct_reply_to=control_direct_reply_to,
        )
        if control_result is not None:
            return control_result

        # Phase 3: Build route context and dispatch through ordered handlers
        latest_chat_context = self._latest_analysis_context(
            event.chat_id,
            explicit_followup_context=followup_context,
        )
        conversation_input = build_resolved_conversation_input(
            event=event,
            route_text=route_content,
            direct_reply_to=direct_reply_to,
            followup_context=followup_context,
            referenced_resources=referenced_resources,
            is_new_chain=resolution.is_new_chain,
            route_text_source=resolution.route_text_source,
            context_source=resolution.context_source,
            conversation_root_message_id=resolution.conversation_root_message_id,
            resource_view=resource_view,
        )
        self.activity_store.record_conversation_input(
            conversation_input.conversation_root_message_id,
            event,
            conversation_input_snapshot(conversation_input),
        )
        ctx = _RouteContext(
            event=event,
            route_content=route_content,
            followup_context=followup_context,
            signal_request=signal_request,
            bug_request=bug_request,
            direct_analysis_request=direct_analysis_request,
            app_server_investigation_request=app_server_investigation_request,
            requirement_analysis_request=requirement_analysis_request,
            source_analysis_request=source_analysis_request,
            report_followup_request=report_followup_request,
            perception_request=perception_request,
            rom_version_request=rom_version_request,
            addr2line_request=addr2line_request,
            referenced_resources=referenced_resources,
            latest_chat_context=latest_chat_context,
            conversation_input=conversation_input,
        )
        return self._dispatch_route(ctx)
    def _dispatch_route(self, ctx: _RouteContext) -> TaskResult:
        """Try each route handler in priority order; first match wins."""
        # Route handlers ordered by priority (highest first).
        # Each returns TaskResult on match, or None to fall through.
        _ROUTE_HANDLERS = [
            self._route_analysis_replay_decision,  # 1. Follow-up replay decision before fresh routes
            self._route_report_followup,    # 2. Explicit HTML/diagram report followup
            self._route_app_server_investigation,  # 3. Explicit app-server autonomous analysis
            self._route_bug_followup,       # 4. Bug followup conversation
            self._route_direct_analysis_followup,  # 5. File-analysis followup / retry
            self._route_bug_intent,         # 6. Explicit bug analysis request
            self._route_addr2line_resolve,  # 7. Native stack address reverse lookup
            self._route_rom_version_lookup, # 8. ROM version lookup
            self._route_scene_signal,       # 9. Scene signal shortcut
            self._route_arbitrated_business_routes,  # 10. Scoped candidate arbitration
            self._route_requirement_analysis,  # 11. Feishu Project requirement + source analysis
            self._route_signal_request,     # 12. Signal lifecycle analysis
            self._route_claude_skill,       # 13. Optional configured local skill route
            self._route_perception,         # 14. Perception summary
            self._route_followup_intent,    # 18. Followup intent keywords
            self._route_general_followup,   # 19. General followup conversation
            self._route_knowledge_probe,    # 20. Internal operation QA from knowledge before chat
            self._route_stale_light_interaction,  # 21. Replayed old lightweight messages
            self._route_basic_chat,         # 22. Deterministic help/identity replies
            self._route_omlx_chat,          # 23. OMLX chat conversation
            self._route_intent_router,      # 24. Intent fallback for unresolved tasks
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
    def _jobs_root(self) -> Path:
        return self.config.data_dir / "jobs"
