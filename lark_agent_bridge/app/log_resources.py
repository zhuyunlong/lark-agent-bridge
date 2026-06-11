from __future__ import annotations

import json
from pathlib import Path
import re

from ._shared import *  # noqa: F401,F403


from .progress_notify import _ProgressNotifyMixin
from .intent_dispatch import _IntentDispatchMixin
from .request_build import _RequestBuildMixin
from .version_lookup import _VersionLookupMixin
from .resources import _ResourcesMixin


class _LogResourcesMixin(_ProgressNotifyMixin, _IntentDispatchMixin, _RequestBuildMixin, _VersionLookupMixin, _ResourcesMixin):
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
