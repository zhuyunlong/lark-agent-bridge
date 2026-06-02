from __future__ import annotations

import html
import json
import os
from pathlib import Path
import re
import shutil
import stat

from ._shared import *  # noqa: F401,F403


class _ContextFromMixin:
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
        direct_followup_result = self._maybe_handle_direct_analysis_followup(event, followup_context, route_content)
        if direct_followup_result is not None:
            return direct_followup_result
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

    def _answer_bug_followup_from_existing(
        self,
        route_content: str,
        followup_context,
        *,
        min_confidence: float = _FAST_EXISTING_ANSWER_MIN_CONFIDENCE,
    ) -> TaskResult | None:
        question = route_content.strip()
        if not question or self._bug_followup_requires_fresh_analysis(question):
            return None
        evidence_lines = self._rank_existing_bug_evidence(question, followup_context)
        if not evidence_lines:
            return None
        confidence, confidence_reason = self._existing_bug_answer_confidence(question, evidence_lines)
        if confidence < min_confidence:
            return None
        top_evidence = evidence_lines[:5]
        primary = top_evidence[0]
        bullets = "\n".join(f"- {line}" for line in top_evidence)
        message = (
            "## 结论摘要\n"
            f"- {primary}\n"
            "- 基于已有报告/摘要可直接回答，本轮未重新下载日志，也未重新调用本地 Agent。\n\n"
            "## 关键证据\n"
            f"{bullets}\n\n"
            "## 建议动作\n"
            "- 如果你希望重新跑日志、补源码证据或修正既有结论，请明确回复“重新分析/基于源码重跑”。"
        )
        return TaskResult(
            success=True,
            message=message,
            details={
                "mode": "bug_followup_existing_answer",
                "answer_source": "existing_context",
                "answer_confidence": confidence,
                "answer_confidence_reason": confidence_reason,
                "followup_text": route_content,
                "delivery": "reply",
            },
        )

    def _bug_followup_requires_fresh_analysis(self, text: str) -> bool:
        lowered = text.casefold()
        force_terms = tuple(term.casefold() for term in self.config.bug_analysis.force_reanalysis_terms)
        return any(term in lowered for term in force_terms)

    def _rank_existing_bug_evidence(self, question: str, followup_context) -> list[str]:
        tokens = self._fast_answer_tokens(question)
        if not tokens:
            return []
        sources: list[tuple[int, str]] = [
            (3, str(getattr(followup_context, "summary_text", "") or "")),
            (2, str(getattr(followup_context, "report_excerpt", "") or "")),
        ]
        for item in getattr(followup_context, "history", []) or []:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").casefold()
            if role == "assistant":
                sources.append((1, str(item.get("content") or "")))
        ranked: list[tuple[int, int, int, str]] = []
        seen: set[str] = set()
        for source_weight, context_text in sources:
            for index, raw_line in enumerate(context_text.splitlines()):
                line = self._clean_existing_answer_line(raw_line)
                if not line or line in seen or self._is_question_echo(line, question):
                    continue
                score = self._score_existing_answer_line(line, tokens)
                if score <= 0 and source_weight >= 2 and self._looks_like_existing_evidence(line):
                    score = 1
                if score <= 0:
                    continue
                seen.add(line)
                ranked.append((score, source_weight, -index, line))
        ranked.sort(reverse=True)
        return [line for score, _source_weight, _index, line in ranked if score >= 1][:8]

    def _existing_bug_answer_confidence(self, question: str, evidence_lines: list[str]) -> tuple[float, str]:
        evidence_text = "\n".join(evidence_lines).casefold()
        domain_terms = self._fast_answer_domain_terms(question)
        missing_domain_terms = [term for term in domain_terms if term.casefold() not in evidence_text]
        if missing_domain_terms:
            return 0.45, f"missing_domain_terms={','.join(missing_domain_terms[:4])}"
        tokens = self._fast_answer_tokens(question)
        if not tokens:
            return 0.0, "no_question_tokens"
        matched = [token for token in tokens if token.casefold() in evidence_text]
        coverage = len(matched) / max(1, len(tokens))
        if len(evidence_lines) >= 2 and coverage >= 0.35:
            return 0.9, f"coverage={coverage:.2f};domain_terms={len(domain_terms)}"
        if coverage >= 0.5:
            return 0.82, f"coverage={coverage:.2f};domain_terms={len(domain_terms)}"
        return 0.6, f"coverage={coverage:.2f};domain_terms={len(domain_terms)}"

    def _fast_answer_domain_terms(self, question: str) -> list[str]:
        terms: list[str] = []
        for match in re.finditer(r"SIGNAL_[A-Za-z0-9_]+|[A-Z][A-Z0-9]+(?:_[A-Z0-9]+)+", question):
            self._append_answerability_term(terms, match.group(0))
        for match in re.finditer(r"[A-Za-z][A-Za-z0-9_]{2,}", question):
            token = match.group(0)
            if token.casefold() not in _FAST_ANSWER_LATIN_DOMAIN_STOP_TERMS:
                self._append_answerability_term(terms, token)
        cleaned = question
        for stop in sorted(_FAST_ANSWER_DOMAIN_STOP_TERMS, key=len, reverse=True):
            cleaned = cleaned.replace(stop, " ")
        for match in re.finditer(r"[\u4e00-\u9fff]{2,12}", cleaned):
            self._append_answerability_term(terms, match.group(0))
        return terms[:8]

    def _is_question_echo(self, line: str, question: str) -> bool:
        line_norm = self._normalize_fast_answer_text(line)
        question_norm = self._normalize_fast_answer_text(question)
        if not line_norm or not question_norm:
            return False
        return line_norm == question_norm or line_norm in question_norm or question_norm in line_norm

    def _looks_like_existing_evidence(self, line: str) -> bool:
        lowered = line.casefold()
        return bool(
            re.search(r"\d{1,2}:\d{2}:\d{2}", line)
            or "`" in line
            or "/" in line
            or "true" in lowered
            or "false" in lowered
            or any(marker in line for marker in ("证据", "日志", "记录", "显示", "命中", "字段"))
        )

    def _normalize_fast_answer_text(self, text: str) -> str:
        cleaned = re.sub(r"<[^>]+>", " ", text.casefold())
        cleaned = re.sub(r"[`*_#>\[\]（）()，,。！？!?：:；;、/|\s\-–—0-9.]+", "", cleaned)
        return cleaned.strip()

    def _fast_answer_tokens(self, question: str) -> list[str]:
        normalized = re.sub(r"[`*_#>\[\]（）()，,。！？!?：:；;、/|]+", " ", question.casefold())
        tokens: set[str] = set()
        for token in re.findall(r"[a-z0-9_+\-.]{2,}|[\u4e00-\u9fff]{2,}", normalized):
            if token in _FAST_ANSWER_STOP_TOKENS:
                continue
            if re.fullmatch(r"[\u4e00-\u9fff]+", token):
                for i in range(max(1, len(token) - 1)):
                    gram = token[i : i + 2]
                    if gram and gram not in _FAST_ANSWER_STOP_TOKENS:
                        tokens.add(gram)
            else:
                tokens.add(token)
        return sorted(tokens, key=len, reverse=True)

    def _clean_existing_answer_line(self, line: str) -> str:
        cleaned = re.sub(r"<[^>]+>", " ", line.strip())
        cleaned = re.sub(r"^[\s>*#\-–—0-9.、]+", "", cleaned).strip()
        cleaned = re.sub(r"\s+", " ", cleaned)
        if not cleaned or len(cleaned) < 8:
            return ""
        if len(cleaned) > 500:
            cleaned = cleaned[:499].rstrip() + "…"
        if cleaned.count("{") + cleaned.count("}") > 2:
            return ""
        return cleaned

    def _score_existing_answer_line(self, line: str, tokens: list[str]) -> int:
        lowered = line.casefold()
        score = 0
        for token in tokens:
            if token and token in lowered:
                score += 2 if len(token) >= 3 else 1
        if any(marker in lowered for marker in ("结论", "证据", "原因", "置信", "根因")):
            score += 1
        return score

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

    def _direct_reply_to(self, event: LarkEvent) -> str:
        explicit = (event.reply_to or "").strip()
        if explicit:
            return explicit
        parent = (event.parent_id or "").strip()
        if parent:
            return parent
        if not event.message_id:
            return ""
        fetched = self.lark_client.fetch_message(event.message_id)
        if fetched.returncode != 0:
            return ""
        for message in self._extract_message_records(fetched.stdout):
            if str(message.get("message_id") or "").strip() != event.message_id:
                continue
            for key in ("reply_to", "upper_message_id"):
                value = message.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            break
        return ""

    def _lookup_bot_alias_context(self, reply_to: str) -> ConversationContext | None:
        key = (reply_to or "").strip()
        if not key:
            return None
        context = self.conversation_store.lookup(key)
        if context is None:
            return None
        if context.context_key == context.root_message_id:
            return None
        return context

    def _resolve_followup_context(self, event: LarkEvent):
        context = self.conversation_store.find(event)
        if context is not None:
            return context
        reference_ids = self._fetch_followup_reference_ids(event)
        for key in reference_ids:
            context = self.conversation_store.lookup(key)
            if context is not None:
                return context
        for key in self._followup_context_candidate_ids(event, reference_ids):
            context = self._context_from_activity_session(key, event=event)
            if context is not None:
                return context
        for key in self._followup_context_candidate_ids(event, reference_ids):
            context = self._context_from_fetched_message(key, event=event)
            if context is not None:
                return context
        return None

    def _followup_context_candidate_ids(self, event: LarkEvent, reference_ids: list[str]) -> list[str]:
        candidates = [event.reply_to, event.root_id, event.parent_id, *reference_ids]
        result: list[str] = []
        for candidate in candidates:
            normalized = str(candidate or "").strip()
            if normalized and normalized not in result:
                result.append(normalized)
        return result

    def _context_from_activity_session(self, message_id: str, *, event: LarkEvent) -> ConversationContext | None:
        session = self.activity_store.get_session(message_id)
        if not session:
            return None
        details = session.get("details", {})
        if not isinstance(details, dict):
            details = {}
        request_text = str(details.get("user_request_text") or session.get("content") or "").strip()
        bug_url = str(details.get("bug_url") or self._bug_url_from_request_text(request_text)).strip()
        mode = str(session.get("mode") or details.get("mode") or "").strip()
        if bug_url and "bug" not in mode.casefold():
            mode = "bug_analysis"
        if mode not in self._threaded_reply_context_modes():
            return None
        summary_text = str(session.get("message") or "").strip()
        report_url = str(session.get("report_url") or details.get("published_report_url") or details.get("report_url") or "")
        return ConversationContext(
            root_message_id=message_id,
            chat_id=str(session.get("chat_id") or event.chat_id),
            mode=mode,
            request_text=request_text,
            summary_text=summary_text,
            report_url=report_url,
            report_excerpt=summary_text,
            history=[],
            created_at=str(session.get("started_at") or ""),
            updated_at=str(session.get("updated_at") or session.get("finished_at") or ""),
        )

    def _context_from_fetched_message(self, message_id: str, *, event: LarkEvent) -> ConversationContext | None:
        fetched = self.lark_client.fetch_message(message_id)
        if fetched.returncode != 0:
            return None
        for message in self._extract_message_records(fetched.stdout):
            current_id = str(message.get("message_id") or "").strip()
            if current_id and current_id != message_id:
                continue
            request_text = self._message_content_text(message).strip()
            bug_request = parse_bug_request(request_text, bug_url_re=self.bug_url_re)
            if not bug_request.triggered:
                continue
            return ConversationContext(
                root_message_id=message_id,
                chat_id=str(message.get("chat_id") or event.chat_id),
                mode="bug_analysis",
                request_text=request_text,
                summary_text="",
                report_url="",
                report_excerpt="",
                history=[],
                created_at=str(message.get("create_time") or ""),
                updated_at=str(message.get("update_time") or message.get("create_time") or ""),
            )
        for message in self._extract_message_records(fetched.stdout):
            current_id = str(message.get("message_id") or "").strip()
            if current_id and current_id != message_id:
                continue
            context = self._context_from_artifact_message(message, event=event)
            if context is not None:
                if current_id:
                    self._remember_conversation_alias(current_id, context.root_message_id)
                return context
        return None

    def _context_from_artifact_message(self, message: dict[str, object], *, event: LarkEvent) -> ConversationContext | None:
        artifact_name = self._message_artifact_name(message)
        if not artifact_name:
            return None
        session = self.activity_store.find_session_by_artifact_name(
            artifact_name,
            chat_id=str(message.get("chat_id") or event.chat_id),
        )
        if not session:
            return None
        session_id = str(session.get("session_id") or "").strip()
        if not session_id:
            return None
        return self._context_from_activity_session(session_id, event=event)

    def _message_artifact_name(self, message: dict[str, object]) -> str:
        for key in ("name", "file_name", "filename"):
            value = message.get(key)
            if isinstance(value, str) and value.strip():
                return Path(value.strip()).name
        content = message.get("content")
        return self._artifact_name_from_value(content)

    def _artifact_name_from_value(self, value: object) -> str:
        if isinstance(value, dict):
            for key in ("name", "file_name", "filename"):
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    return Path(candidate.strip()).name
            for nested in value.values():
                found = self._artifact_name_from_value(nested)
                if found:
                    return found
            return ""
        if not isinstance(value, str):
            return ""
        text = value.strip()
        if not text:
            return ""
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if parsed is not None:
            found = self._artifact_name_from_value(parsed)
            if found:
                return found
        match = re.search(r'\b(?:name|file_name|filename)=["\']([^"\']+)["\']', text)
        if match:
            return Path(match.group(1).strip()).name
        return ""

    def _fetch_followup_reference_ids(self, event: LarkEvent) -> list[str]:
        pending = [value for value in [event.reply_to, event.parent_id, event.root_id] if value]
        discovered: list[str] = []
        if not pending and event.message_id:
            fetched_current = self.lark_client.fetch_message(event.message_id)
            if fetched_current.returncode == 0:
                for candidate in self._extract_message_reference_ids(fetched_current.stdout):
                    if not candidate or candidate == event.message_id:
                        continue
                    if candidate not in discovered:
                        discovered.append(candidate)
                    pending.append(candidate)
        visited: set[str] = set()
        while pending and len(visited) < 6:
            current = pending.pop(0)
            if not current or current in visited:
                continue
            visited.add(current)
            fetched = self.lark_client.fetch_message(current)
            if fetched.returncode != 0:
                continue
            for candidate in self._extract_message_reference_ids(fetched.stdout):
                if not candidate or candidate in visited:
                    continue
                if candidate not in discovered:
                    discovered.append(candidate)
                pending.append(candidate)
        return discovered

    def _extract_message_reference_ids(self, payload_text: str) -> list[str]:
        messages = self._extract_message_records(payload_text)
        ids: list[str] = []
        for message in messages:
            for key in ("message_id", "reply_to", "root_id", "parent_id", "thread_id"):
                value = message.get(key)
                if isinstance(value, str) and value.strip():
                    ids.append(value.strip())
        return ids

    def _extract_message_records(self, payload_text: str) -> list[dict[str, object]]:
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, dict):
            return []
        data = payload.get("data", {})
        if not isinstance(data, dict):
            return []
        messages = data.get("messages") or data.get("items") or data.get("message")
        if isinstance(messages, dict):
            messages = [messages]
        if not isinstance(messages, list):
            return []
        return [message for message in messages if isinstance(message, dict)]

    def _message_content_text(self, message: dict[str, object]) -> str:
        content = message.get("content")
        if content is None:
            content = message.get("text") or ""
        if isinstance(content, dict):
            value = content.get("text") or content.get("content")
            return str(value if value is not None else content)
        if not isinstance(content, str):
            return str(content)
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            return content
        if isinstance(parsed, dict):
            value = parsed.get("text") or parsed.get("content")
            if value is not None:
                return str(value)
        return content

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

    def _existing_bug_context_can_answer(self, route_content: str, followup_context) -> bool:
        question = route_content.strip()
        if not question:
            return True
        lowered = question.casefold()
        if not any(term.casefold() in lowered for term in _FOLLOWUP_INTENT_TERMS):
            return True
        context_text = "\n".join(
            [
                str(getattr(followup_context, "request_text", "") or ""),
                str(getattr(followup_context, "summary_text", "") or ""),
                str(getattr(followup_context, "report_excerpt", "") or ""),
            ]
        )
        if not context_text.strip():
            return False
        context_folded = context_text.casefold()
        missing = [
            term
            for term in self._followup_answerability_terms(question)
            if term.casefold() not in context_folded
        ]
        return not missing

    def _followup_answerability_terms(self, text: str) -> list[str]:
        terms: list[str] = []
        for match in re.finditer(r"SIGNAL_[A-Za-z0-9_]+|[A-Z][A-Z0-9]+(?:_[A-Z0-9]+)+", text):
            self._append_answerability_term(terms, match.group(0))
        for match in re.finditer(r"[A-Za-z][A-Za-z0-9_]{2,}", text):
            token = match.group(0)
            if token.upper() == "SIGNAL" or token.isdigit():
                continue
            self._append_answerability_term(terms, token)
        for match in re.finditer(r"[\u4e00-\u9fff]{2,12}", text):
            token = match.group(0)
            if token in _FOLLOWUP_STOP_TERMS:
                continue
            if any(stop in token for stop in _FOLLOWUP_STOP_TERMS) and len(token) <= 4:
                continue
            self._append_answerability_term(terms, token)
        return terms[:8]

    def _append_answerability_term(self, terms: list[str], term: str) -> None:
        normalized = term.strip(" \t\r\n，。；;,.、:：?？!！()（）[]【】")
        if not normalized:
            return
        if normalized.casefold() in {item.casefold() for item in terms}:
            return
        terms.append(normalized)

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
