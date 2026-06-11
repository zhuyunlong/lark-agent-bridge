from __future__ import annotations

import html
import json
import os
from pathlib import Path
import re
import shutil
import stat
import unicodedata

from ._shared import *  # noqa: F401,F403


from .followup_clarify import _FollowupClarifyMixin
from .existing_answer import _ExistingAnswerMixin
from .context_lookup import _ContextLookupMixin


class _ContextFromMixin(_FollowupClarifyMixin, _ExistingAnswerMixin, _ContextLookupMixin):
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
        preflight = self._direct_analysis_preflight(direct_analysis_request)
        if not preflight.execute:
            if not self.state_store.mark_seen(event):
                return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)
            result = preflight.clarification_result
            if result is None:
                return TaskResult(
                    success=False,
                    message="意图分析失败，无法确定后续执行路径。",
                    error_code="intent_preflight_failed",
                    details={"mode": "intent_preflight"},
                )
            return self._deliver_result(event, result, request_text=direct_analysis_request.raw_text or route_content)
        self._send_intent_preflight_card(event, preflight)
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
        return self._run_direct_analysis_request(
            event,
            direct_analysis_request,
            route_content,
            plans_override=preflight.plans_override,
            classification_skill=preflight.classification_skill,
            classification_source=preflight.classification_source,
            classification_reason=preflight.classification_reason or preflight.reason,
        )
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
        bug_skill_confirmation_result = self._maybe_handle_bug_skill_confirmation_followup(
            event,
            followup_context,
            route_content,
        )
        if bug_skill_confirmation_result is not None:
            result_mode = str(bug_skill_confirmation_result.details.get("mode") or "")
            if result_mode == "bug_skill_confirmation" and (
                bug_skill_confirmation_result.skipped or not bug_skill_confirmation_result.success
            ):
                return self._finalize_followup_reply(
                    event,
                    bug_skill_confirmation_result,
                    followup_context,
                    route_content,
                )
            return bug_skill_confirmation_result
        direct_followup_result = self._maybe_handle_direct_analysis_followup(event, followup_context, route_content)
        if direct_followup_result is not None:
            return direct_followup_result
        bug_time_clarification_result = self._maybe_handle_bug_time_clarification_followup(
            event,
            followup_context,
            route_content,
        )
        if bug_time_clarification_result is not None:
            return bug_time_clarification_result
        bug_stack_clarification_result = self._maybe_handle_bug_stack_clarification_followup(
            event,
            followup_context,
            route_content,
        )
        if bug_stack_clarification_result is not None:
            return bug_stack_clarification_result
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
            previous_bug_url = self._bug_url_from_session(previous_session)
            if not previous_bug_url:
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
                request_text=self._bug_request_text_for_followup_context(
                    followup_context,
                    previous_session=previous_session,
                ),
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
                source_mode=str(result.details.get("source_mode", "")),
                context_profile=str(result.details.get("context_profile", "")),
                classification_source=str(result.details.get("classification_source", "")),
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
