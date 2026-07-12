from __future__ import annotations

from pathlib import Path
import re

from ._shared import *  # noqa: F401,F403


class _ReplayFlowMixin:
    def _route_analysis_replay_decision(self, ctx: _RouteContext) -> TaskResult | None:
        if ctx.followup_context is None:
            return None
        replay_context = self._build_analysis_replay_context(
            ctx.event,
            ctx.route_content,
            ctx.followup_context,
            referenced_resources=ctx.referenced_resources,
            followup_action=ctx.conversation_input.followup_action,
            followup_payload=ctx.conversation_input.followup_payload,
        )
        # Keep bug/addr2line/rom on their existing paths until their adapters are
        # migrated. Direct/signal/perception are handled here because their old
        # replay shortcuts could drop prepared local logs.
        if replay_context.mode not in {"direct_analysis", "signal_lifecycle", "perception_summary"}:
            return None
        decision = self._decide_analysis_replay(replay_context)
        if decision.action in {"new_request", "unsupported"}:
            return None
        if decision.action == "answer_from_existing":
            return self._handle_followup(
                ctx.event,
                ctx.route_content,
                ctx.followup_context,
                conversation_action=ctx.conversation_input.followup_action,
                conversation_payload=ctx.conversation_input.followup_payload,
            )
        if decision.action == "reanalyze":
            return self._execute_analysis_replay_plan(ReplayPlan(decision=decision, context=replay_context), ctx.event)
        if decision.action == "clarify":
            if not self.state_store.mark_seen(ctx.event):
                return TaskResult(True, f"duplicate event skipped: {ctx.event.event_id}", skipped=True)
            return TaskResult(
                success=False,
                message="无法确认是否要基于上一轮日志重新分析，请明确回复“重新分析”或补充新的分析条件。",
                error_code="analysis_replay_clarification_required",
                details={
                    "mode": "analysis_replay",
                    "previous_mode": replay_context.previous_mode,
                    "resource_status": serialize_resource_status(replay_context.resources),
                },
            )
        return None

    def _build_analysis_replay_context(
        self,
        event: LarkEvent,
        route_content: str,
        followup_context,
        *,
        referenced_resources: list[DownloadResource] | None = None,
        followup_action: str = "unknown",
        followup_payload: str | None = None,
    ) -> AnalysisReplayContext:
        previous_session = self.activity_store.get_session(followup_context.root_message_id) or {}
        details = previous_session.get("details") if isinstance(previous_session.get("details"), dict) else {}
        original_request_text = str(
            details.get("user_request_text")
            or getattr(followup_context, "request_text", "")
            or previous_session.get("content")
            or ""
        ).strip()
        bug_url = str(details.get("bug_url") or self._bug_url_from_request_text(original_request_text)).strip()
        bug_title, bug_description = self._bug_metadata_from_replay_session(previous_session)
        reply_chain_resources = self._reference_chain_log_resources(event)
        session_resources = self._log_resources_from_session(previous_session)
        resources = ReplayResourceBundle.from_candidates(
            current=referenced_resources or [],
            reply_chain=reply_chain_resources,
            session=session_resources,
        )
        return AnalysisReplayContext(
            root_message_id=str(getattr(followup_context, "root_message_id", "") or ""),
            chat_id=str(getattr(followup_context, "chat_id", "") or event.chat_id),
            mode=normalize_replay_mode(str(getattr(followup_context, "mode", "") or "")),
            previous_mode=str(getattr(followup_context, "mode", "") or ""),
            original_request_text=original_request_text,
            current_text=route_content.strip(),
            history=list(getattr(followup_context, "history", []) or []),
            followup_action=followup_action,
            followup_payload=(route_content.strip() if followup_payload is None else followup_payload),
            summary_text=str(getattr(followup_context, "summary_text", "") or ""),
            report_excerpt=str(getattr(followup_context, "report_excerpt", "") or ""),
            report_url=str(getattr(followup_context, "report_url", "") or ""),
            bug_url=bug_url,
            bug_title=bug_title,
            bug_description=bug_description,
            previous_session=previous_session,
            resources=resources,
        )

    def _bug_metadata_from_replay_session(self, previous_session: dict[str, object]) -> tuple[str, str]:
        job_dir_value = str(previous_session.get("job_dir") or "").strip()
        if not job_dir_value:
            return "", ""
        metadata_path = Path(job_dir_value).expanduser() / "output" / "bug_metadata.md"
        if not metadata_path.exists():
            return "", ""
        try:
            text = metadata_path.read_text(encoding="utf-8")[:12000]
        except OSError:
            return "", ""
        title = ""
        match = re.search(r"[标題标题]:\s*`([^`]+)`", text)
        if match:
            title = match.group(1).strip()
        else:
            match = re.search(r"[标題标题]:\s*(.+)", text)
            if match:
                title = match.group(1).strip()
        description = ""
        desc_match = re.search(r"- 缺陷描述:\s*```text\s*(.*?)\s*```", text, re.S)
        if desc_match:
            description = desc_match.group(1).strip()
        return title, description

    def _decide_analysis_replay(self, context: AnalysisReplayContext) -> ReplayDecision:
        if self.intent_runner.is_enabled() and hasattr(self.intent_runner, "decide_replay"):
            try:
                decision = self.intent_runner.decide_replay(context=context)
                if decision.action != "unsupported":
                    return decision
            except IntentAnalysisFailure as exc:
                self._notify_progress(
                    "analysis_replay_decision_failed",
                    "续聊重分析意图识别失败，使用本地兜底规则",
                    session_id=context.root_message_id,
                    error_code=exc.error_code,
                    stderr=(exc.stderr or exc.stdout or "")[:MAX_ERROR_PREVIEW],
                )
            except Exception as exc:
                logger.warning("analysis replay decision failed, using fallback: %s", exc)

        current_text = context.current_text.strip()
        if self._replay_text_requests_reanalysis(current_text, context):
            return ReplayDecision(
                action="reanalyze",
                mode=context.mode,
                reason="本地规则识别到重新分析或修正意图。",
                confidence="high",
                signal_hint=self._signal_hint_from_replay_text(current_text, context) if context.mode == "signal_lifecycle" else "",
                normalized_request_text=self._normalized_replay_request_text(context),
            )
        if current_text:
            return ReplayDecision(
                action="answer_from_existing",
                mode=context.mode,
                reason="本地规则识别为基于已有报告的追问。",
                confidence="medium",
            )
        return ReplayDecision(
            action="clarify",
            mode=context.mode,
            reason="续聊内容为空，无法判断处理路径。",
            confidence="low",
        )

    def _replay_text_requests_reanalysis(self, current_text: str, context: AnalysisReplayContext) -> bool:
        action = context.followup_action
        if action == "retry":
            return True
        if action == "continue" and context.mode == "direct_analysis":
            return True
        lowered = current_text.casefold()
        if any(term in lowered for term in ("重新分析", "重新跑", "重跑", "再分析", "重新执行", "重试", "再跑一次")):
            return True
        if any(term in lowered for term in ("修正", "更正", "改成", "修改", "问题时间", "故障时间", "时间点")):
            return True
        if context.mode == "signal_lifecycle":
            request = parse_signal_request(
                current_text,
                signal_aliases=self.config.signal_aliases,
                command_prefixes=self.config.command_prefixes,
                signal_resolver=self.signal_resolver,
            )
            return bool(request.signal)
        if context.mode == "perception_summary":
            return parse_perception_summary_request(current_text).triggered and self._is_followup_intent(current_text)
        return False

    def _signal_hint_from_replay_text(self, current_text: str, context: AnalysisReplayContext) -> str:
        current_request = parse_signal_request(
            current_text,
            signal_aliases=self.config.signal_aliases,
            command_prefixes=self.config.command_prefixes,
            signal_resolver=self.signal_resolver,
        )
        if current_request.signal:
            return current_request.signal
        original_request = parse_signal_request(
            context.original_request_text,
            signal_aliases=self.config.signal_aliases,
            command_prefixes=self.config.command_prefixes,
            signal_resolver=self.signal_resolver,
        )
        return original_request.signal or ""

    def _normalized_replay_request_text(self, context: AnalysisReplayContext) -> str:
        if context.followup_action in {"retry", "continue"} and not context.followup_payload:
            return context.original_request_text
        if not context.current_text:
            return context.original_request_text
        if context.current_text and context.current_text not in context.original_request_text:
            return f"{context.original_request_text}\n\n追问/修正：{context.current_text}".strip()
        return context.original_request_text

    def _execute_analysis_replay_plan(self, plan: ReplayPlan, event: LarkEvent) -> TaskResult:
        if plan.decision.mode == "direct_analysis":
            return self._execute_direct_analysis_replay_plan(plan, event)
        if plan.decision.mode == "signal_lifecycle":
            return self._execute_signal_replay_plan(plan, event)
        if plan.decision.mode == "perception_summary":
            return self._execute_perception_replay_plan(plan, event)
        return TaskResult(
            success=False,
            message="当前分析类型暂未接入统一重分析执行器。",
            error_code="analysis_replay_mode_not_supported",
            details={"mode": "analysis_replay", "previous_mode": plan.context.previous_mode},
        )

    def _execute_direct_analysis_replay_plan(self, plan: ReplayPlan, event: LarkEvent) -> TaskResult:
        context = plan.context
        request_text = plan.decision.normalized_request_text or self._normalized_replay_request_text(context)
        resources = context.resources.best_effort
        request = self._build_direct_analysis_request(request_text, resources, event=event)
        if not request.triggered:
            request = DirectAnalysisRequest(
                prompt=request_text.strip(),
                resources=resources,
                raw_text=request_text,
                triggered=True,
                error=None if resources else "missing_log",
            )
        if not request.resources:
            if not self.state_store.mark_seen(event):
                return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)
            return TaskResult(
                success=False,
                message="缺少日志输入：上一轮没有可复用的本地日志，也没有可重新下载的资源。",
                error_code="missing_log",
                details={
                    "mode": "direct_analysis",
                    "resource_status": serialize_resource_status(context.resources),
                },
            )
        self._notify_progress(
            "direct_analysis_followup_replay",
            "基于续聊上下文重新执行直传文件分析",
            event=event,
            session_id=context.root_message_id,
            resource_count=len(request.resources),
        )
        if not self.state_store.mark_seen(event):
            return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)
        pending = self._maybe_request_approval(
            event,
            operation_type="direct_analysis",
            description="直传文件分析",
            route_content=request.raw_text,
            file_count=len(request.resources),
            prompt=request.prompt,
            estimated_duration_seconds=self.config.bug_analysis.timeout_seconds,
        )
        if pending is not None:
            return pending
        return self._run_direct_analysis_request(
            event,
            request,
            request.raw_text,
            classification_source="analysis_replay",
            classification_reason=plan.decision.reason,
            root_message_id=context.root_message_id,
        )

    def _execute_signal_replay_plan(self, plan: ReplayPlan, event: LarkEvent) -> TaskResult:
        context = plan.context
        signal_text = "\n".join(
            part
            for part in (
                plan.decision.signal_hint,
                context.current_text,
                plan.decision.normalized_request_text,
                context.original_request_text,
            )
            if str(part or "").strip()
        )
        request = parse_signal_request(
            signal_text,
            signal_aliases=self.config.signal_aliases,
            command_prefixes=self.config.command_prefixes,
            signal_resolver=self.signal_resolver,
        )
        resources = self._merge_resources(request.resources, context.resources.best_effort)
        if not resources:
            if not self.state_store.mark_seen(event):
                return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)
            return TaskResult(
                success=False,
                message="缺少日志输入：上一轮没有可复用的本地日志，也没有可重新下载的资源。",
                error_code="missing_log",
                details={
                    "mode": "signal_lifecycle",
                    "resource_status": serialize_resource_status(context.resources),
                },
            )
        request = SignalRequest(
            signal=request.signal,
            resources=resources,
            since=request.since,
            raw_text=plan.decision.normalized_request_text or signal_text,
            triggered=True,
            error=request.error,
        )
        self._notify_progress(
            "signal_followup_replay",
            "基于续聊上下文重新执行信号生命周期分析",
            event=event,
            session_id=context.root_message_id,
            signal=request.signal or "",
            resource_count=len(request.resources),
        )
        if not self.state_store.mark_seen(event):
            return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)
        return self._run_signal_request(event, request, request.raw_text or signal_text)

    def _execute_perception_replay_plan(self, plan: ReplayPlan, event: LarkEvent) -> TaskResult:
        context = plan.context
        if context.followup_action == "retry" and not context.followup_payload:
            request_text = context.original_request_text
        else:
            request_text = plan.decision.normalized_request_text or self._normalized_replay_request_text(context)
        request = parse_perception_summary_request(request_text)
        if not request.triggered:
            request = parse_perception_summary_request(context.current_text)
        resources = self._merge_resources(request.resources, context.resources.best_effort)
        if not resources:
            if not self.state_store.mark_seen(event):
                return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)
            return TaskResult(
                success=False,
                message="缺少日志输入：上一轮没有可复用的本地日志，也没有可重新下载的资源。",
                error_code="missing_log",
                details={
                    "mode": "perception_summary",
                    "resource_status": serialize_resource_status(context.resources),
                },
            )
        request = request.__class__(
            prompt=request.prompt or request_text.strip() or context.current_text.strip(),
            resources=resources,
            raw_text=request.raw_text or request_text,
            triggered=True,
            error=request.error,
        )
        self._notify_progress(
            "perception_followup_replay",
            "基于续聊上下文重新执行感知数据总结",
            event=event,
            session_id=context.root_message_id,
            resource_count=len(request.resources),
        )
        if not self.state_store.mark_seen(event):
            return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)
        return self._run_perception_request(event, request, request.raw_text or request_text)
