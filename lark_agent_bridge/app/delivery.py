from __future__ import annotations

from datetime import datetime, timezone
import html
from pathlib import Path
import re
import threading
from typing import Callable

from ._shared import *  # noqa: F401,F403


class _DeliveryMixin:
    """结果交付：发送文本/卡片回复与报告就绪通知（与 HandleEventMixin 共享 self 状态）。"""

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
            # Always thread the bot's answer to the triggering message so the
            # conversation chain stays walkable: a later "reply to the bot"
            # can follow reply_to back to this request (and its replied-to
            # input). Only fall back to a standalone send when no message_id.
            if event.message_id:
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
        needs_user_direction = bool(result.details.get("needs_user_direction"))
        intent_options = result.details.get("intent_options")
        is_skill_clarification = (
            (mode == "bug_clarification" and needs_user_direction)
            or (
                mode == "bug_skill_confirmation"
                and needs_user_direction
                and isinstance(intent_options, list)
                and bool(intent_options)
            )
        )
        # Most result cards need a published report; skill clarification and knowledge QA are card-only decision points.
        if not report_url and not is_skill_clarification and mode != "knowledge_qa":
            return False

        mode_labels = {
            "bug_analysis": "Bug 分析",
            "bug_clarification": "Bug 分析分诊",
            "bug_skill_confirmation": "Bug Skill 确认",
            "bug_reanalysis": "Bug 重新分析",
            "bug_followup_existing_answer": "Bug 追问",
            "bug_agent_followup": "Bug 追问",
            "direct_analysis": "直传文件分析",
            "app_server_investigation": "AI 自主分析",
            "requirement_analysis": "需求源码分析",
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
