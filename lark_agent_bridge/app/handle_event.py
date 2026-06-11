from __future__ import annotations

from datetime import datetime, timezone
import html
from pathlib import Path
import re
import threading
from typing import Callable

from ._shared import *  # noqa: F401,F403


from .routes import _RoutesMixin
from .delivery import _DeliveryMixin
from .mention import _MentionMixin
from .progress_cards import _ProgressCardsMixin


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
        self.state_store = state_store or EventStateStore(config.data_dir / "state" / "seen_events.jsonl")
        self.conversation_store = conversation_store or ConversationContextStore(
            config.data_dir / "state" / "conversation_contexts.json",
            max_history_turns=config.omlx_chat.followup_max_history_turns,
        )
        self.activity_store = activity_store or AgentActivityStore(config.data_dir / "state" / "agent_activity.json")
        self.progress_callback = progress_callback
        self._progress_cards: dict[str, dict[str, object]] = {}
        # Guards _progress_cards dict structure (get/setitem/pop/iterate) against
        # concurrent worker threads and the daemon cleanup loop. Held only around
        # dict access — never while sending cards over the network.
        self._progress_cards_lock = threading.Lock()
        self._progress_cards_max_age_seconds = 7200  # 2 hour TTL (must exceed bug_analysis timeout)
        self._progress_card_stream_update_interval_seconds = 5.0
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
            cache_ttl_seconds=config.signal_resolver.cache_ttl_seconds,
            preferred_paths=config.signal_resolver.preferred_paths or None,
            source_suffixes=config.signal_resolver.source_suffixes or None,
        )
        self.bug_url_re = build_bug_url_re(config.bug_url_domains) if config.bug_url_domains else None
        self.escalation_checker = EscalationChecker()
        self.notification_history = NotificationHistory(config.data_dir / "state" / "notification_history.json")
        self.lifecycle_store = LifecycleStore()
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

        # Phase 1: Group mention filtering.
        # Strict rule:
        #   - New chain root: event.reply_to is empty AND the message @-mentions the bot.
        #   - Continuation:  event.reply_to points at a known bot alias (an entry in
        #                    conversation_store whose context_key != root_message_id).
        #   - Anything else: ignored.
        route_content = event.content
        followup_context = None
        phase1_new_chain = False
        if event.chat_type == "group":
            stripped_at_bot = self._strip_group_chat_mention(event.content, event=event)
            direct_reply_to = self._direct_reply_to(event)
            addressed_content: str | None = None
            if direct_reply_to:
                followup_context = self._lookup_bot_alias_context(direct_reply_to)
                if followup_context is not None:
                    addressed_content = (
                        stripped_at_bot if stripped_at_bot is not None else event.content.strip()
                    )
                elif stripped_at_bot is not None:
                    addressed_content = stripped_at_bot
                    phase1_new_chain = True
            else:
                if stripped_at_bot is not None:
                    addressed_content = stripped_at_bot
                    phase1_new_chain = True
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
            app_server_investigation_request=app_server_investigation_request,
            requirement_analysis_request=requirement_analysis_request,
            source_analysis_request=source_analysis_request,
            report_followup_request=report_followup_request,
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
