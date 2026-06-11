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


class _FollowupClarifyMixin:
    """追问澄清：Bug 时间/堆栈澄清与 Skill 确认、直传分析恢复（与 ContextFromMixin 共享 self 状态）。"""

    def _bug_request_text_for_followup_context(self, followup_context, *, previous_session: dict[str, object] | None = None) -> str:
        details = previous_session.get("details", {}) if isinstance(previous_session, dict) else {}
        if not isinstance(details, dict):
            details = {}
        request_text = str(details.get("user_request_text") or getattr(followup_context, "request_text", "") or "").strip()
        for marker in ("\n\n追问/修正：", "\n追问/修正："):
            idx = request_text.find(marker)
            if idx != -1:
                return request_text[:idx].rstrip()
        return request_text
    def _fresh_bug_request_from_followup_context(self, followup_context, *, followup_text: str):
        request_text = self._bug_request_text_for_followup_context(followup_context)
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
    def _maybe_handle_bug_time_clarification_followup(self, event: LarkEvent, followup_context, route_content: str) -> TaskResult | None:
        if str(getattr(followup_context, "mode", "") or "") != "bug_time_clarification":
            return None
        previous_session = self.activity_store.get_session(followup_context.root_message_id) or {}
        request_text = self._bug_request_text_for_followup_context(followup_context, previous_session=previous_session)
        bug_request = parse_bug_request(request_text, bug_url_re=self.bug_url_re)
        if not bug_request.triggered:
            return None

        normalized_followup = self._normalize_bug_time_clarification_text(route_content)
        if not self._looks_like_bug_time_fragment(normalized_followup):
            return None
        if not self._bug_time_followup_has_full_datetime(normalized_followup):
            missing_part = "几月几日"
            if self._bug_time_followup_has_date(normalized_followup):
                missing_part = "几点几分"
            result = TaskResult(
                success=True,
                message=(
                    f"已收到时间片段 `{route_content.strip()}`，但还缺少{missing_part}。\n"
                    "请补充完整问题时间，例如：`6月8日 16:47`。"
                ),
                skipped=True,
                details={
                    "mode": "bug_time_clarification",
                    "bug_url": bug_request.bug_url,
                    "time_gate_status": "missing_fault_time",
                    "user_request_text": request_text,
                },
            )
            return self._finalize_followup_reply(event, result, followup_context, route_content)

        recovered_bug_request = self._fresh_bug_request_from_followup_context(
            followup_context,
            followup_text=normalized_followup,
        )
        if recovered_bug_request is None:
            return None
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
        return self._run_bug_request(
            event,
            recovered_bug_request,
            recovered_bug_request.raw_text,
            root_message_id=followup_context.root_message_id,
        )
    def _maybe_handle_bug_stack_clarification_followup(self, event: LarkEvent, followup_context, route_content: str) -> TaskResult | None:
        if str(getattr(followup_context, "mode", "") or "") != "bug_stack_clarification":
            return None
        previous_session = self.activity_store.get_session(followup_context.root_message_id) or {}
        request_text = self._bug_request_text_for_followup_context(followup_context, previous_session=previous_session)
        bug_request = parse_bug_request(request_text, bug_url_re=self.bug_url_re)
        if not bug_request.triggered:
            return None
        if not parse_addr2line_request(route_content).triggered:
            result = TaskResult(
                success=True,
                message=(
                    "还没识别到可反解的堆栈地址。\n"
                    "请直接粘贴 tombstone/backtrace 文本，例如：`#00 pc 0000000000123450 /system/.../libxxx.so`，"
                    "或先把 crash/tombstone 日志附件上传到 Bug 后再回复继续。"
                ),
                skipped=True,
                details={
                    "mode": "bug_stack_clarification",
                    "bug_url": bug_request.bug_url,
                    "stack_gate_status": "missing_stack_payload",
                    "user_request_text": request_text,
                },
            )
            return self._finalize_followup_reply(event, result, followup_context, route_content)
        recovered_bug_request = self._fresh_bug_request_from_followup_context(
            followup_context,
            followup_text=route_content,
        )
        if recovered_bug_request is None:
            return None
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
        return self._run_bug_request(
            event,
            recovered_bug_request,
            recovered_bug_request.raw_text,
            root_message_id=followup_context.root_message_id,
        )
    def _normalize_bug_time_clarification_text(self, text: str) -> str:
        normalized = unicodedata.normalize("NFKC", text or "")
        normalized = normalized.replace("号", "日")
        normalized = re.sub(
            r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})(?!\s*日|\d)",
            lambda match: f"{int(match.group(1))}月{int(match.group(2))}日",
            normalized,
        )
        normalized = re.sub(
            r"(?<!\d)(\d{1,2})\.(\d{1,2})(?=\D|$)",
            lambda match: f"{int(match.group(1))}-{int(match.group(2))}",
            normalized,
        )

        def replace_cn_time(match: re.Match[str]) -> str:
            period = match.group(1) or ""
            hour = int(match.group(2))
            minute = int(match.group(3))
            if period in {"下午", "晚上"} and 1 <= hour < 12:
                hour += 12
            elif period == "中午" and hour < 11:
                hour += 12
            return f"{hour:02d}:{minute:02d}"

        normalized = re.sub(
            r"(上午|早上|下午|晚上|中午)?\s*(\d{1,2})点\s*(\d{1,2})分?",
            replace_cn_time,
            normalized,
        )
        return normalized.strip()
    def _looks_like_bug_time_fragment(self, text: str) -> bool:
        return self._bug_time_followup_has_clock(text) or self._bug_time_followup_has_date(text)
    def _bug_time_followup_has_clock(self, text: str) -> bool:
        return bool(re.search(r"(?<!\d)\d{1,2}:\d{2}(?::\d{2})?(?!\d)", text or ""))
    def _bug_time_followup_has_date(self, text: str) -> bool:
        normalized = text or ""
        return bool(
            re.search(r"(?<!\d)\d{1,2}[-/]\d{1,2}(?!\d)", normalized)
            or re.search(r"(?<!\d)\d{1,2}月\d{1,2}日", normalized)
            or re.search(r"\b20\d{2}[-_/年]\d{1,2}[-_/月]\d{1,2}", normalized)
        )
    def _bug_time_followup_has_full_datetime(self, text: str) -> bool:
        resolver = getattr(self.bug_runner, "_resolve_bug_time_context", None)
        if callable(resolver):
            try:
                time_context = resolver(request_text=text, title="", description="", reference_time="")
            except Exception:
                time_context = None
            if time_context is not None:
                return bool(getattr(time_context, "has_full_datetime", False))
        return self._bug_time_text_has_full_datetime(text)
    def _bug_time_text_has_full_datetime(self, text: str) -> bool:
        normalized = text or ""
        return bool(
            re.search(
                r"\b20\d{2}[-_/年]\d{1,2}[-_/月]\d{1,2}[日_\s-]*\d{1,2}:\d{2}(?::\d{2})?\b",
                normalized,
            )
            or re.search(
                r"(?<!\d)\d{1,2}[-/]\d{1,2}(?:[\]\[日_\s-]+)\d{1,2}:\d{2}(?::\d{2})?(?!\d)",
                normalized,
            )
            or re.search(
                r"(?<!\d)\d{1,2}月\d{1,2}日[^\d]{0,8}\d{1,2}:\d{2}(?::\d{2})?(?!\d)",
                normalized,
            )
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
        request_text = self._bug_request_text_for_followup_context(followup_context, previous_session=session)
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
    def _execute_recovered_direct_analysis(
        self,
        event: LarkEvent,
        followup_context,
        *,
        followup_text: str,
        plans_override: list[BugAnalysisPlan] | None = None,
        classification_skill: str = "",
        classification_source: str = "",
        classification_reason: str = "",
    ) -> TaskResult | None:
        request = self._recovered_direct_analysis_request_from_followup_context(
            event,
            followup_context,
            followup_text=followup_text,
        )
        if request is None:
            return None
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
            plans_override=plans_override,
            classification_skill=classification_skill,
            classification_source=classification_source,
            classification_reason=classification_reason,
        )
    def _maybe_handle_direct_analysis_followup(self, event: LarkEvent, followup_context, route_content: str) -> TaskResult | None:
        request_text = str(getattr(followup_context, "request_text", "") or "").strip()
        if not request_text or self._bug_url_from_request_text(request_text):
            return None
        previous_session = self.activity_store.get_session(followup_context.root_message_id) or {}
        selected_option = self._match_direct_analysis_clarification_option(route_content, previous_session)
        if selected_option is not None:
            option_type = str(selected_option.get("type") or "").strip()
            if option_type == "skill":
                decision = self.bug_runner.selection_for_skill_name(
                    str(selected_option.get("skill_name") or "").strip(),
                    source="user_selected_reply",
                    reason="用户通过文本回复选择专用 skill。",
                )
                if decision is None:
                    return TaskResult(
                        success=False,
                        message="回复中的 Skill 选项无效，请重新选择。",
                        error_code="invalid_bug_skill_selection",
                        details={"mode": "bug_clarification"},
                    )
                return self._execute_recovered_direct_analysis(
                    event,
                    followup_context,
                    followup_text="",
                    plans_override=decision.plans,
                    classification_skill=decision.skill_name,
                    classification_source=decision.source,
                    classification_reason=decision.reason,
                )
            if option_type == "source_analysis":
                return self._execute_recovered_direct_analysis(
                    event,
                    followup_context,
                    followup_text="",
                    plans_override=[BugAnalysisPlan(kind="source_code_skill")],
                    classification_skill="source_analysis",
                    classification_source="user_selected_source_analysis",
                    classification_reason="用户明确要求直接源码分析。",
                )
        followup_action = parse_followup_action(route_content)
        if followup_action in {"retry", "continue"} and str(getattr(followup_context, "mode", "") or "") == "direct_analysis":
            return self._execute_recovered_direct_analysis(
                event,
                followup_context,
                followup_text="",
                classification_source="reply_chain_retry",
                classification_reason="用户在文件分析回复链中发起重跑。",
            )
        if str(getattr(followup_context, "mode", "") or "") in {"bug_clarification", "bug_time_clarification"}:
            return self._execute_recovered_direct_analysis(
                event,
                followup_context,
                followup_text=route_content,
                classification_source="clarification_reply",
                classification_reason="用户补充了文件分析方向，继续恢复原始文件请求执行。",
            )
        return None
    def _maybe_handle_bug_skill_confirmation_followup(
        self,
        event: LarkEvent,
        followup_context,
        route_content: str,
    ) -> TaskResult | None:
        if str(getattr(followup_context, "mode", "") or "") != "bug_skill_confirmation":
            return None
        previous_session = self.activity_store.get_session(followup_context.root_message_id) or {}
        selected_option = self._match_bug_skill_confirmation_option(route_content, previous_session)
        if selected_option is None:
            return TaskResult(
                success=True,
                skipped=True,
                message="没有识别到确认选项，请回复序号 1/2/3，或回复对应方向文本。",
                details={"mode": "bug_skill_confirmation", "needs_user_direction": True},
            )
        return self._execute_bug_skill_confirmation_choice(
            event,
            followup_context,
            previous_session,
            selected_option,
            source="user_selected_reply",
            reason="用户通过文本回复确认 bug 分析 skill。",
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
