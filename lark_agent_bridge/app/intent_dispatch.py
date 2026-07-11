from __future__ import annotations

import json
from pathlib import Path
import re

from ._shared import *  # noqa: F401,F403


class _IntentDispatchMixin:
    """意图路由决策分发与上下文选择（与 LogResourcesMixin 共享 self 状态）。"""

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
                and referenced_resources
                and self._looks_like_direct_analysis_prompt(route_content)
            ):
                return self._handle_direct_analysis_intent(
                    event,
                    route_content,
                    referenced_resources=referenced_resources,
                )
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
            if (
                not parse_bug_request(route_content, bug_url_re=self.bug_url_re).triggered
                and referenced_resources
            ):
                return self._handle_direct_analysis_intent(
                    event,
                    route_content,
                    referenced_resources=referenced_resources,
                )
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
            "app_server_investigation",
            "source_analysis",
            "diagram_report_followup",
            "perception_summary",
            "signal_lifecycle",
        }
    def _threaded_reply_context_modes(self) -> set[str]:
        return {
            *self._analysis_context_modes(),
            "bug_clarification",
            "bug_skill_confirmation",
            "bug_time_clarification",
            "bug_stack_clarification",
            "knowledge_qa",
            "knowledge_probe",
            "basic_chat",
            "omlx_chat",
            "analysis_followup",
            "addr2line_resolve",
            "rom_version_lookup",
        }
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
    def _handle_bug_intent(
        self,
        event: LarkEvent,
        route_content: str,
        *,
        referenced_resources: list[DownloadResource] | None = None,
    ) -> TaskResult:
        bug_request = parse_bug_request(route_content, bug_url_re=self.bug_url_re)
        if not bug_request.triggered:
            bug_request = bug_request.__class__(bug_url="", prompt=route_content.strip(), raw_text=route_content, triggered=True, error="missing_bug_url")
        elif referenced_resources:
            bug_request = bug_request.__class__(
                bug_url=bug_request.bug_url,
                prompt=bug_request.prompt,
                raw_text=bug_request.raw_text,
                triggered=bug_request.triggered,
                error=bug_request.error,
                resources=self._merge_resources(bug_request.resources, referenced_resources),
            )
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
