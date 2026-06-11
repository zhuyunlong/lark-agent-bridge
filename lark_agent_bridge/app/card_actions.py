from __future__ import annotations

from datetime import datetime, timezone
import json
import re
from urllib.parse import quote, urlsplit, urlunsplit

from ._shared import *  # noqa: F401,F403


class _CardActionsMixin:
    """卡片交互动作处理（审批/重析/选择/反馈等）（与 ResultBugMixin 共享 self 状态）。"""

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
            request_text=self._bug_request_text_for_followup_context(
                followup_context,
                previous_session=previous_session,
            ),
            root_message_id=followup_context.root_message_id,
        )
    def _card_followup_context(self, action_event: CardActionEvent):
        root_message_id = action_event.root_message_id or action_event.message_id
        followup_context = self.conversation_store.lookup(root_message_id) if root_message_id else None
        previous_session: dict[str, object] = {}
        if followup_context is None and root_message_id:
            event = self._event_from_card_action(
                action_event,
                root_message_id=root_message_id,
                fallback_chat_id=action_event.chat_id,
                fallback_chat_type=action_event.chat_type,
            )
            followup_context = self._context_from_activity_session(root_message_id, event=event)
        if followup_context is None and action_event.job_id:
            previous_session = self.activity_store.find_session_by_job_id(action_event.job_id) or {}
            root_message_id = str(previous_session.get("session_id") or root_message_id or "")
            followup_context = self.conversation_store.lookup(root_message_id) if root_message_id else None
            if followup_context is None and root_message_id:
                event = self._event_from_card_action(
                    action_event,
                    root_message_id=root_message_id,
                    fallback_chat_id=action_event.chat_id,
                    fallback_chat_type=action_event.chat_type,
                )
                followup_context = self._context_from_activity_session(root_message_id, event=event)
        if followup_context is not None and not previous_session:
            previous_session = self.activity_store.get_session(followup_context.root_message_id) or {}
        if followup_context is not None and not previous_session and action_event.job_id:
            previous_session = self.activity_store.find_session_by_job_id(action_event.job_id) or {}
        return root_message_id, followup_context, previous_session
    def _chat_type_from_previous_session(self, previous_session: dict[str, object]) -> str:
        return str(previous_session.get("chat_type") or "")
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
        if not previous_session:
            previous_session = self.activity_store.get_session(followup_context.root_message_id) or {}
        chat_id = self._card_action_chat_id(action_event, followup_context)
        if not chat_id:
            return TaskResult(
                success=False,
                message="卡片回调缺少 chat_id，无法按所选 skill 继续分析。",
                error_code="invalid_card_action_context",
                details={"mode": "card_action", "action": "select_bug_skill"},
            )
        if str(getattr(followup_context, "mode", "") or "") == "bug_skill_confirmation":
            event = self._event_from_card_action(
                action_event,
                root_message_id=followup_context.root_message_id,
                fallback_chat_id=chat_id,
                fallback_chat_type=self._chat_type_from_previous_session(previous_session),
            )
            if not self.state_store.mark_seen(event):
                return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)
            return self._execute_bug_skill_confirmation_choice(
                event,
                followup_context,
                previous_session,
                {
                    "type": "skill",
                    "skill_name": selected.skill_name,
                    "label": selected.skill_label,
                },
                source="user_selected_card",
                reason="用户通过卡片按钮确认 bug 分析 skill。",
            )
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
