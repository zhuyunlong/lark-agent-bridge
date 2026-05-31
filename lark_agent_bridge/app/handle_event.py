from __future__ import annotations

from ._shared import *  # noqa: F401,F403


class _HandleEventMixin:
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
            self._route_knowledge_qa,       # 10. Personal knowledge QA / ADB templates
            self._route_source_analysis,    # 11. Repository-only source analysis
            self._route_signal_request,     # 12. Signal lifecycle analysis
            self._route_claude_skill,       # 13. Optional configured local skill route
            self._route_bug_request,        # 14. Bug request (secondary match)
            self._route_perception,         # 15. Perception summary
            self._route_direct_analysis,    # 16. Direct file/log analysis
            self._route_followup_intent,    # 17. Followup intent keywords
            self._route_general_followup,   # 18. General followup conversation
            self._route_knowledge_probe,    # 19. Internal operation QA from knowledge before chat
            self._route_stale_light_interaction,  # 20. Replayed old lightweight messages
            self._route_basic_chat,         # 21. Deterministic help/identity replies
            self._route_omlx_chat,          # 22. OMLX chat conversation
            self._route_intent_router,      # 23. Intent fallback for unresolved tasks
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

    def _route_app_server_investigation(self, ctx: _RouteContext) -> TaskResult | None:
        request = ctx.app_server_investigation_request
        if request is None or not request.triggered:
            return None
        if not self.state_store.mark_seen(ctx.event):
            return TaskResult(True, f"duplicate event skipped: {ctx.event.event_id}", skipped=True)
        return self._run_app_server_investigation_request(ctx.event, request, ctx.route_content)

    def _route_report_followup(self, ctx: _RouteContext) -> TaskResult | None:
        request = ctx.report_followup_request
        if ctx.followup_context is None or request is None or not request.triggered:
            return None
        if parse_followup_action(request.prompt) in {"retry", "continue"}:
            return None
        if not self.state_store.mark_seen(ctx.event):
            return TaskResult(True, f"duplicate event skipped: {ctx.event.event_id}", skipped=True)
        job = create_job_context(self.config.data_dir, ctx.event)
        html_path = job.output_dir / "diagram_report_followup.html"
        html_path.write_text(
            render_context_diagram_report(
                title="上下文图表报告",
                request_text=ctx.followup_context.request_text,
                followup_text=request.prompt,
                summary_text=ctx.followup_context.summary_text,
                report_excerpt=ctx.followup_context.report_excerpt,
                history=ctx.followup_context.history,
                diagram_kinds=request.diagram_kinds,
                source_mode=ctx.followup_context.source_mode,
                context_profile=ctx.followup_context.context_profile,
            ),
            encoding="utf-8",
        )
        result = TaskResult(
            success=True,
            message="已基于上一轮分析上下文生成 HTML 图表报告。",
            job_id=job.job_id,
            job_dir=job.job_dir,
            html_report=html_path,
            details={
                "mode": "diagram_report_followup",
                "source_mode": ctx.followup_context.source_mode,
                "context_profile": ctx.followup_context.context_profile,
                "classification_source": "deterministic_report_followup",
                "diagram_kinds": list(request.diagram_kinds),
                "followup_text": request.prompt,
                "user_request_text": ctx.followup_context.request_text,
                "files_to_send": [html_path],
            },
        )
        return self._deliver_result(
            ctx.event,
            result,
            request_text=ctx.followup_context.request_text,
            root_message_id=ctx.followup_context.root_message_id,
        )

    def _route_source_analysis(self, ctx: _RouteContext) -> TaskResult | None:
        request = ctx.source_analysis_request
        if request is None or not request.triggered:
            return None
        if ctx.referenced_resources:
            return None
        if ctx.bug_request is not None and getattr(ctx.bug_request, "triggered", False):
            return None
        if ctx.direct_analysis_request is not None and getattr(ctx.direct_analysis_request, "triggered", False):
            return None
        if not self.state_store.mark_seen(ctx.event):
            return TaskResult(True, f"duplicate event skipped: {ctx.event.event_id}", skipped=True)
        self._notify_progress(
            "source_analysis_request_received",
            "收到源码分析请求",
            event=ctx.event,
            mode="source_analysis",
            target=request.target,
        )
        def _source_progress(payload: dict[str, object]) -> None:
            details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
            progress_details = dict(details)
            progress_details.setdefault("mode", "source_analysis")
            self._notify_progress(
                str(payload.get("stage") or "source_analysis"),
                str(payload.get("message") or "源码分析"),
                event=ctx.event,
                **progress_details,
            )

        result = self.source_analysis_runner.run(
            request,
            ctx.event,
            progress_callback=_source_progress,
        )
        return self._deliver_result(ctx.event, result, request_text=request.raw_text or request.prompt)

    def _route_bug_followup(self, ctx: _RouteContext) -> TaskResult | None:
        if ctx.followup_context is None or "bug" not in str(ctx.followup_context.mode).casefold():
            return None
        if ctx.signal_request is None or not ctx.signal_request.triggered:
            return self._handle_followup(ctx.event, ctx.route_content, ctx.followup_context)
        if (
            self._bug_followup_requires_fresh_analysis(ctx.route_content)
            and self._is_bug_reanalysis_followup(ctx.route_content, ctx.followup_context)
        ):
            return self._handle_followup(ctx.event, ctx.route_content, ctx.followup_context)
        return None

    def _route_direct_analysis_followup(self, ctx: _RouteContext) -> TaskResult | None:
        if ctx.followup_context is None:
            return None
        return self._maybe_handle_direct_analysis_followup(ctx.event, ctx.followup_context, ctx.route_content)

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
        if (
            not request.signal
            and ctx.direct_analysis_request is not None
            and bool(ctx.direct_analysis_request.resources)
            and self._looks_like_direct_analysis_prompt(ctx.route_content)
        ):
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
        return self._handle_direct_analysis_intent(
            ctx.event,
            ctx.route_content,
            referenced_resources=ctx.referenced_resources,
        )

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
        # When a message still carries file/folder resources but the prompt looks
        # like a correction/follow-up ("问题时间", "重新分析"), let the intent
        # router decide first instead of forcing the generic followup guard.
        if (
            ctx.followup_context is None
            and self.intent_runner.is_enabled()
            and ctx.direct_analysis_request is not None
            and bool(ctx.direct_analysis_request.resources)
        ):
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
            "app_server_investigation": "AI 自主分析",
            "source_analysis": "源码分析",
            "diagram_report_followup": "图表报告",
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
        model = str(result.details.get("agent_summary_model") or "").strip()
        if model:
            metadata["Agent 模型"] = model
        usage_prefix, usage = extract_first_prefixed_token_usage(result.details, ("agent_summary_", "app_server_"))
        total_tokens = usage.get("total_tokens")
        if isinstance(total_tokens, int):
            metadata["AI Token" if usage_prefix == "app_server_" else "Agent Token"] = str(total_tokens)
        skill_label = str(result.details.get("analysis_skill_label") or "").strip()
        skill_name = str(result.details.get("analysis_skill") or "").strip()
        if skill_label or skill_name:
            metadata["命中 Skill"] = skill_label or skill_name
        classification_source = str(result.details.get("classification_source") or "").strip()
        if classification_source:
            metadata["分类来源"] = classification_source
        source_mode = str(result.details.get("source_mode") or "").strip()
        if source_mode and source_mode != "off":
            metadata["源码模式"] = source_mode

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

    def _strip_group_chat_mention(self, text: str, *, event: LarkEvent | None = None) -> str | None:
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
        if event is not None:
            cleaned = self._strip_runtime_bot_mention(content, event)
            if cleaned is not None:
                return cleaned
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

    def _strip_runtime_bot_mention(self, content: str, event: LarkEvent) -> str | None:
        for name in self._runtime_bot_mention_names(event):
            cleaned = self._strip_configured_bot_name_mention(content, name)
            if cleaned is not None:
                return cleaned
        return None

    def _runtime_bot_mention_names(self, event: LarkEvent) -> list[str]:
        names = self._bot_mention_names_from_payload(event.raw)
        if names or not event.message_id:
            return names
        fetched = self.lark_client.fetch_message(event.message_id)
        if fetched.returncode != 0:
            return []
        return self._bot_mention_names_from_payload_text(fetched.stdout, message_id=event.message_id)

    def _bot_mention_names_from_payload(self, payload: object) -> list[str]:
        if not isinstance(payload, dict):
            return []
        messages: list[dict[str, object]] = []
        direct_message = payload.get("message")
        if isinstance(direct_message, dict):
            messages.append(direct_message)
        event_body = payload.get("event")
        if isinstance(event_body, dict):
            nested_message = event_body.get("message")
            if isinstance(nested_message, dict):
                messages.append(nested_message)
        if not messages:
            return []
        return self._bot_mention_names_from_messages(messages)

    def _bot_mention_names_from_payload_text(self, payload_text: str, *, message_id: str = "") -> list[str]:
        messages = self._extract_message_records(payload_text)
        if message_id:
            filtered = []
            for message in messages:
                current_id = str(message.get("message_id") or "").strip()
                if not current_id or current_id == message_id:
                    filtered.append(message)
            messages = filtered
        return self._bot_mention_names_from_messages(messages)

    def _bot_mention_names_from_messages(self, messages: list[dict[str, object]]) -> list[str]:
        names: list[str] = []
        for message in messages:
            mentions = message.get("mentions")
            if not isinstance(mentions, list):
                continue
            for mention in mentions:
                if not isinstance(mention, dict) or not self._is_bot_mention_metadata(mention):
                    continue
                name = str(mention.get("name") or "").strip()
                if name and name not in names:
                    names.append(name)
        return names

    def _is_bot_mention_metadata(self, mention: dict[str, object]) -> bool:
        mention_id = str(
            mention.get("id")
            or mention.get("open_id")
            or mention.get("user_id")
            or mention.get("union_id")
            or ""
        ).strip()
        if mention_id:
            configured_bot = self.config.lark.bot_open_id.strip()
            if configured_bot and mention_id == configured_bot:
                return True
            if mention_id.startswith("cli_"):
                return True
        mention_type = str(mention.get("type") or mention.get("id_type") or "").strip().casefold()
        return mention_type in {"bot", "app_id"}

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
            existing["details"] = {
                **dict(existing.get("details") or {}),
                **dict(details or {}),
            }
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

    def _should_update_progress_card_from_progress(
        self,
        event: LarkEvent,
        *,
        stage: str,
        session_id: str | None = None,
    ) -> bool:
        key = self._progress_card_state_key(event, session_id=session_id)
        card_state = self._progress_cards.get(key)
        if not card_state or not card_state.get("message_id"):
            return False
        now = datetime.now(timezone.utc)
        card_state["last_active_at"] = now
        # Throttle only the known high-frequency streaming families (Codex
        # app-server deltas + agent summary stream), not every "*_stream" stage.
        if not stage.endswith(_THROTTLED_PROGRESS_STAGE_SUFFIXES):
            return True
        last_update = card_state.get("last_card_update_at")
        if isinstance(last_update, datetime):
            if (now - last_update).total_seconds() < self._progress_card_stream_update_interval_seconds:
                return False
        return True

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
        try:
            card = self._build_progress_card(event, key=key, status=status, result=result, note=note)
            send_result = self.lark_client.update_card(message_id, card_to_json(card))
        except Exception as exc:
            logger.debug("failed to update progress card %s: %s", message_id, exc, exc_info=True)
            self._record_progress_card_update_failure(event, session_id=session_id, error=exc)
            return False
        if send_result.returncode == 0:
            now = datetime.now(timezone.utc)
            card_state["last_card_update_at"] = now
            card_state["last_active_at"] = now
        return send_result.returncode == 0
