from __future__ import annotations

import json
from pathlib import Path
import re

from ._shared import *  # noqa: F401,F403


class _LogResourcesMixin:
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

    def _notify_progress(
        self,
        stage: str,
        message: str,
        *,
        event: LarkEvent | None = None,
        session_id: str | None = None,
        **details: object,
    ) -> None:
        payload: dict[str, object] = {
            "type": "progress",
            "stage": stage,
            "message": message,
        }
        if session_id:
            payload["session_id"] = session_id
        if event is not None:
            payload.update(
                {
                    "event_id": event.event_id,
                    "message_id": event.message_id,
                    "chat_id": event.chat_id,
                    "chat_type": event.chat_type,
                }
            )
        progress_details = dict(details)
        progress_details.setdefault("executor", self._progress_executor(stage, progress_details))
        payload["details"] = progress_details
        self.activity_store.record_progress(payload)
        if event is not None and event.chat_type in {"group", "p2p"}:
            try:
                should_update_card = self._should_update_progress_card_from_progress(
                    event,
                    stage=stage,
                    session_id=session_id,
                )
            except Exception as exc:
                logger.debug(
                    "failed to inspect progress card state for %s: %s",
                    event.message_id,
                    exc,
                    exc_info=True,
                )
                self._record_progress_card_update_failure(event, session_id=session_id, error=exc)
                should_update_card = False
            if should_update_card:
                self._update_progress_card(event, session_id=session_id)
        if self.progress_callback is None:
            return
        self.progress_callback(payload)

    def _record_progress_card_update_failure(
        self,
        event: LarkEvent | None,
        *,
        session_id: str | None = None,
        error: BaseException,
    ) -> None:
        payload: dict[str, object] = {
            "type": "progress",
            "stage": "progress_card_update_failed",
            "message": "进度卡更新失败，后台流程继续",
            "details": {
                "error_type": type(error).__name__,
                "error": str(error)[:MAX_ERROR_PREVIEW],
                "executor": "Bridge 发布器",
            },
        }
        if session_id:
            payload["session_id"] = session_id
        if event is not None:
            payload.update(
                {
                    "event_id": event.event_id,
                    "message_id": event.message_id,
                    "chat_id": event.chat_id,
                    "chat_type": event.chat_type,
                }
            )
        self.activity_store.record_progress(payload)
        if self.progress_callback is not None:
            try:
                self.progress_callback(payload)
            except Exception as exc:
                logger.debug("failed to emit progress-card failure event: %s", exc, exc_info=True)

    def _progress_executor(self, stage: str, details: dict[str, object]) -> str:
        for key in ("executor", "executed_by", "actor", "runner"):
            value = str(details.get(key) or "").strip()
            if value:
                return value
        provider = str(
            details.get("provider")
            or details.get("classification_provider")
            or details.get("agent_provider")
            or ""
        ).strip()
        normalized_stage = stage.casefold()
        if normalized_stage.startswith("intent_"):
            return f"意图 Agent({provider})" if provider else "意图 Agent"
        if "agent" in normalized_stage or "run_analysis" in normalized_stage:
            return f"本地 Agent({provider})" if provider else "本地 Agent"
        if any(
            token in normalized_stage
            for token in (
                "download",
                "fetch",
                "resolve_url",
                "check_env",
                "retry_download",
                "auth",
            )
        ):
            return "飞书/Meegle CLI"
        if any(
            token in normalized_stage
            for token in (
                "file_",
                "reply",
                "status_card",
                "report",
                "publish",
                "archive",
                "delivery",
            )
        ):
            return "Bridge 发布器"
        return "Bridge 编排器"

    def _prepare_delivery_result(
        self,
        event: LarkEvent,
        result: TaskResult,
        *,
        request_text: str,
        root_message_id: str | None = None,
    ) -> TaskResult:
        context_root_message_id = root_message_id or event.root_id or event.message_id
        if not result.success:
            mode = str(result.details.get("mode", "") or "").strip()
            if context_root_message_id and mode not in {"not_addressed", "stale_light_interaction"}:
                details = dict(result.details)
                details.setdefault("delivery", "reply")
                details.setdefault("conversation_root_message_id", context_root_message_id)
                result.details = details
                self.conversation_store.remember(
                    root_message_id=context_root_message_id,
                    chat_id=event.chat_id,
                    mode=mode,
                    request_text=request_text.strip() or self._fallback_request_text(result),
                    summary_text=result.message,
                    report_url="",
                    report_excerpt="",
                    source_mode=str(details.get("source_mode", "")),
                    context_profile=str(details.get("context_profile", "")),
                    classification_source=str(details.get("classification_source", "")),
                )
                self._remember_progress_card_aliases(event, context_root_message_id)
            return result
        self._apply_dual_agent_arbitration(result)
        bug_url = str(result.details.get("bug_url") or self._bug_url_from_request_text(request_text))
        group_key = derive_group_key(
            bug_url=bug_url,
            case_id=result.job_id or "",
            root_message_id=context_root_message_id,
        )
        next_report_version = self.version_store.peek_next_version(group_key)
        published = self.report_publisher.publish_result(result, version=next_report_version)
        if published is None:
            mode = str(result.details.get("mode", "") or "").strip()
            if result.success and (
                result.details.get("needs_user_direction") or mode in self._threaded_reply_context_modes()
            ):
                details = dict(result.details)
                details.setdefault("delivery", "reply")
                details.setdefault("conversation_root_message_id", context_root_message_id)
                if bug_url:
                    details["bug_url"] = bug_url
                result.details = details
                self.conversation_store.remember(
                    root_message_id=context_root_message_id,
                    chat_id=event.chat_id,
                    mode=str(details.get("mode", "")),
                    request_text=request_text.strip() or self._fallback_request_text(result),
                    summary_text=result.message,
                    report_url="",
                    report_excerpt="",
                    source_mode=str(details.get("source_mode", "")),
                    context_profile=str(details.get("context_profile", "")),
                    classification_source=str(details.get("classification_source", "")),
                )
                self._remember_progress_card_aliases(event, context_root_message_id)
            return result
        summary_text = self._link_delivery_summary(result.message)
        result.message = f"{summary_text}\n\n报告链接：{published.url}"
        details = dict(result.details)
        details["delivery"] = "reply"
        details["published_report_url"] = published.url
        details["published_report_index"] = str(published.index_path)
        details["published_report_dir"] = str(published.directory)
        details["conversation_root_message_id"] = context_root_message_id
        if event.chat_type == "group":
            details["files_to_send"] = [Path(path) for path in published.source_report_paths]
        else:
            details.pop("files_to_send", None)
        result.details = details
        self.conversation_store.remember(
            root_message_id=context_root_message_id,
            chat_id=event.chat_id,
            mode=str(details.get("mode", "")),
            request_text=request_text.strip() or self._fallback_request_text(result),
            summary_text=summary_text,
            report_url=published.url,
            report_excerpt=published.context_excerpt,
            source_mode=str(details.get("source_mode", "")),
            context_profile=str(details.get("context_profile", "")),
            classification_source=str(details.get("classification_source", "")),
        )
        self._remember_progress_card_aliases(event, context_root_message_id)
        if bug_url and not details.get("bug_url"):
            details["bug_url"] = bug_url
            result.details = details
        # Auto-archive as a case for the case library
        self.case_store.save_from_result(
            result,
            event=event,
            request_text=request_text,
            bug_url=bug_url,
        )
        # Track report version
        version = self.version_store.add_version(
            group_key,
            version=next_report_version,
            job_id=result.job_id or "",
            report_url=published.url,
            summary=summary_text,
            provider=str(details.get("provider", "")),
            mode=str(details.get("mode", "")),
            duration_seconds=result.duration_seconds or 0.0,
            label=request_text[:80] if request_text else "",
        )
        details["report_version"] = version.version
        details["report_group_key"] = group_key
        result.details = details
        if self.config.workflow_archive.enabled:
            try:
                archive = self.workflow_archiver.archive(result, event=event, request_text=request_text)
            except Exception as exc:
                archive = {
                    "enabled": True,
                    "skipped": False,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                }
            details = dict(result.details)
            details["workflow_archive"] = archive
            result.details = details
        return result

    def _remember_progress_card_aliases(self, event: LarkEvent, root_message_id: str) -> None:
        for key in (root_message_id, event.message_id, event.event_id):
            with self._progress_cards_lock:
                card_state = self._progress_cards.get(str(key or ""))
            if not isinstance(card_state, dict):
                continue
            message_id = str(card_state.get("message_id") or "").strip()
            self._remember_conversation_alias(message_id, root_message_id)

    def _apply_dual_agent_arbitration(self, result: TaskResult) -> None:
        if not self.config.dual_agent.enabled:
            return
        details = dict(result.details)
        secondary_summary = str(
            details.get("secondary_agent_summary")
            or details.get("agent_secondary_summary")
            or ""
        ).strip()
        if not secondary_summary:
            return
        primary_provider = str(
            details.get("agent_summary_provider")
            or details.get("provider")
            or "primary"
        )
        secondary_provider = str(details.get("secondary_agent_provider") or "secondary")
        arbitration = arbitrate(
            extract_conclusion(result.message, provider=primary_provider),
            extract_conclusion(secondary_summary, provider=secondary_provider),
        )
        details["arbitration"] = arbitration.to_dict()
        result.details = details
        result.message = f"{result.message}\n\n双 Agent 裁决：\n{arbitration.summary_text()}"

    def _ensure_result_bug_url(self, result: TaskResult, bug_url: str) -> None:
        normalized = bug_url.strip()
        if not normalized:
            return
        details = dict(result.details)
        details.setdefault("bug_url", normalized)
        result.details = details

    def _bug_url_from_session(self, session: dict[str, object]) -> str:
        details = session.get("details", {}) if isinstance(session, dict) else {}
        if isinstance(details, dict):
            return str(details.get("bug_url") or "")
        return ""

    def _bug_url_from_request_text(self, request_text: str) -> str:
        request = parse_bug_request(request_text, bug_url_re=self.bug_url_re)
        return request.bug_url if request.triggered else ""

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

    def _build_signal_request(self, route_content: str, referenced_resources: list[DownloadResource]) -> SignalRequest:
        request = parse_signal_request(
            route_content,
            signal_aliases=self.config.signal_aliases,
            command_prefixes=self.config.command_prefixes,
            signal_resolver=self.signal_resolver,
        )
        if not referenced_resources:
            return request
        return SignalRequest(
            signal=request.signal,
            resources=self._merge_resources(request.resources, referenced_resources),
            since=request.since,
            raw_text=request.raw_text,
            triggered=request.triggered,
            error=request.error,
        )

    def _build_addr2line_request(
        self,
        route_content: str,
        event: LarkEvent,
        referenced_resources: list[DownloadResource] | None = None,
    ) -> Addr2LineRequest:
        resources = referenced_resources or []
        if not resources:
            probe = parse_addr2line_request(route_content, allow_missing_address=True)
            if probe.triggered:
                resources = self._reference_chain_log_resources(event)
        request = parse_addr2line_request(route_content, allow_missing_address=bool(resources))
        if request.triggered and resources and not request.resources:
            request = Addr2LineRequest(
                addr_text=request.addr_text,
                resources=resources,
                raw_text=request.raw_text,
                rom_version=request.rom_version,
                napa_version=request.napa_version,
                apk_version=request.apk_version,
                symbol_table_url=request.symbol_table_url,
                napa5_download_url=request.napa5_download_url,
                log_folder=request.log_folder,
                fault_time=request.fault_time,
                target=request.target,
                prompt=request.prompt,
                triggered=request.triggered,
                error=request.error,
            )
        if not request.triggered:
            return request
        if request.rom_version and not request.apk_version and not request.napa_version:
            recent_required = self._recent_required_outputs_for_chat(event, request.rom_version)
            recent_apk = self._navigation_version_from_required_outputs(recent_required)
            if recent_apk:
                request = Addr2LineRequest(
                    addr_text=request.addr_text,
                    resources=request.resources,
                    raw_text=request.raw_text,
                    rom_version=request.rom_version,
                    napa_version=request.napa_version,
                    apk_version=recent_apk,
                    symbol_table_url=request.symbol_table_url or str(recent_required.get("symbol_table_url") or "").strip(),
                    napa5_download_url=request.napa5_download_url or str(recent_required.get("napa5_download_url") or "").strip(),
                    log_folder=request.log_folder,
                    fault_time=request.fault_time,
                    target=request.target,
                    prompt=request.prompt,
                    triggered=request.triggered,
                    error=None if request.error == "missing_symbol_version" else request.error,
                )
        if request.rom_version or request.napa_version or request.apk_version:
            return request
        inherited_rom = self._recent_rom_version_for_chat(event)
        if not inherited_rom:
            return request
        inherited_required = self._recent_required_outputs_for_chat(event, inherited_rom)
        inherited_apk = self._navigation_version_from_required_outputs(inherited_required)
        return Addr2LineRequest(
            addr_text=request.addr_text,
            resources=request.resources,
            raw_text=request.raw_text,
            rom_version=inherited_rom,
            napa_version=request.napa_version,
            apk_version=inherited_apk or request.apk_version,
            symbol_table_url=request.symbol_table_url or str(inherited_required.get("symbol_table_url") or "").strip(),
            napa5_download_url=request.napa5_download_url or str(inherited_required.get("napa5_download_url") or "").strip(),
            log_folder=request.log_folder,
            fault_time=request.fault_time,
            target=request.target,
            prompt=request.prompt,
            triggered=True,
            error=None if request.error == "missing_symbol_version" else request.error,
        )

    def _followup_addr2line_request(self, ctx: _RouteContext) -> Addr2LineRequest | None:
        followup_context = ctx.followup_context
        if followup_context is None or str(getattr(followup_context, "mode", "") or "") != "addr2line_resolve":
            return None
        if parse_followup_action(ctx.route_content) != "retry":
            return None
        previous_request = parse_addr2line_request(
            str(getattr(followup_context, "request_text", "") or ""),
            allow_missing_address=True,
        )
        resources = self._reference_chain_log_resources(ctx.event) or self._log_resources_from_context(followup_context)
        session = self.activity_store.get_session(followup_context.root_message_id) or {}
        session_details = session.get("details") if isinstance(session.get("details"), dict) else {}
        session_rom = str(session_details.get("rom_version") or "")
        session_symbol = str(session_details.get("symbol_version") or "")
        session_symbol_kind = str(session_details.get("symbol_version_kind") or "")
        rom_version = previous_request.rom_version or session_rom or self._recent_rom_version_for_chat(ctx.event)
        apk_version = previous_request.apk_version
        napa_version = previous_request.napa_version
        if session_symbol:
            if session_symbol_kind == "apk" and not apk_version:
                apk_version = session_symbol
            elif session_symbol_kind == "napa" and not napa_version:
                napa_version = session_symbol
            elif session_symbol_kind == "rom" and not rom_version:
                rom_version = session_symbol
        if not apk_version and rom_version:
            apk_version = self._recent_navigation_version_for_chat(ctx.event, rom_version) or apk_version
        recent_required = self._recent_required_outputs_for_chat(ctx.event, rom_version) if rom_version else {}
        return Addr2LineRequest(
            addr_text=previous_request.addr_text,
            resources=resources,
            raw_text=ctx.route_content,
            rom_version=rom_version,
            napa_version=napa_version,
            apk_version=apk_version,
            symbol_table_url=previous_request.symbol_table_url or str(recent_required.get("symbol_table_url") or "").strip(),
            napa5_download_url=previous_request.napa5_download_url or str(recent_required.get("napa5_download_url") or "").strip(),
            log_folder=previous_request.log_folder,
            fault_time=previous_request.fault_time,
            target=previous_request.target or "auto",
            prompt=ctx.route_content.strip(),
            triggered=True,
        )

    def _followup_rom_lookup_request(self, ctx: _RouteContext) -> RomVersionLookupRequest | None:
        followup_context = ctx.followup_context
        if followup_context is None or str(getattr(followup_context, "mode", "") or "") != "rom_version_lookup":
            return None
        if parse_followup_action(ctx.route_content) != "retry":
            return None
        previous_request = parse_rom_version_lookup_request(str(getattr(followup_context, "request_text", "") or ""))
        rom_version = previous_request.rom_version or self._recent_rom_version_for_chat(ctx.event)
        if not rom_version:
            return None
        return RomVersionLookupRequest(
            rom_version=rom_version,
            prompt=ctx.route_content.strip(),
            raw_text=ctx.route_content,
            triggered=True,
        )

    def _recent_rom_version_for_chat(self, event: LarkEvent) -> str:
        session = self._recent_rom_lookup_session_for_chat(event)
        if not session:
            return ""
        candidate = parse_rom_version_lookup_request(str(session.get("content") or ""))
        return candidate.rom_version

    def _recent_navigation_version_for_chat(self, event: LarkEvent, rom_version: str = "") -> str:
        return self._navigation_version_from_required_outputs(self._recent_required_outputs_for_chat(event, rom_version=rom_version))

    def _recent_required_outputs_for_chat(self, event: LarkEvent, rom_version: str = "") -> dict[str, object]:
        session = self._recent_rom_lookup_session_for_chat(event, rom_version=rom_version)
        if not session:
            return {}
        details = session.get("details")
        if not isinstance(details, dict):
            return {}
        required = details.get("required_outputs")
        if not isinstance(required, dict):
            return {}
        return required

    def _navigation_version_from_lookup_result(self, result: TaskResult) -> str:
        return self._navigation_version_from_required_outputs(self._required_outputs_from_lookup_result(result))

    def _required_outputs_from_lookup_result(self, result: TaskResult) -> dict[str, object]:
        if not result.success or not isinstance(result.details, dict):
            return {}
        required = result.details.get("required_outputs")
        if not isinstance(required, dict):
            return {}
        return required

    def _navigation_version_from_required_outputs(self, required: dict[str, object]) -> str:
        navigation_version = str(required.get("navigation_version") or "").strip()
        if self._looks_like_apk_version(navigation_version):
            return navigation_version
        symbol_url = str(required.get("symbol_table_url") or "").strip()
        match = re.search(r"/(V\d+\.\d+\.\d+(?:\.\d+)?_\d{14}(?:\.\d+)?_[A-Za-z0-9]+)/?$", symbol_url)
        return match.group(1) if match else ""

    def _recent_rom_lookup_session_for_chat(self, event: LarkEvent, rom_version: str = "") -> dict[str, object] | None:
        chat_id = event.chat_id.strip()
        if not chat_id:
            return None
        for session in self.activity_store.list_sessions(limit=30, include_hidden=True):
            if str(session.get("chat_id") or "") != chat_id:
                continue
            if str(session.get("session_id") or "") == event.message_id:
                continue
            if str(session.get("mode") or "") != "rom_version_lookup":
                continue
            if str(session.get("status") or "") != "succeeded":
                continue
            candidate = parse_rom_version_lookup_request(str(session.get("content") or ""))
            if candidate.rom_version:
                if rom_version and candidate.rom_version != rom_version:
                    continue
                session_id = str(session.get("session_id") or "").strip()
                return self.activity_store.get_session(session_id) or session
        return None

    def _looks_like_apk_version(self, value: str) -> bool:
        return re.match(r"^V\d+\.\d+\.\d+(?:\.\d+)?_\d{14}(?:\.\d+)?_[A-Za-z0-9]+$", value) is not None

    def _resource_descriptors(self, resources: list[DownloadResource]) -> list[dict[str, str]]:
        return [
            {
                "kind": item.kind,
                "value": item.value,
                "source_message_id": item.source_message_id,
            }
            for item in resources
        ]

    def _log_resources_from_session(self, session: dict[str, object] | None) -> list[DownloadResource]:
        if not session:
            return []
        resources: list[DownloadResource] = []
        details = session.get("details")
        if not isinstance(details, dict):
            details = {}
        for key in ("prepared_log_input", "selected_log_input"):
            value = str(details.get(key) or "").strip()
            if not value:
                continue
            candidate = Path(value).expanduser()
            if candidate.exists():
                resources.append(DownloadResource(kind="local", value=str(candidate.resolve())))
        downloads = details.get("downloads")
        if isinstance(downloads, list):
            for item in downloads:
                if not isinstance(item, dict):
                    continue
                path = str(item.get("path") or "").strip()
                if path:
                    candidate = Path(path).expanduser()
                    if candidate.exists():
                        resources.append(DownloadResource(kind="local", value=str(candidate.resolve())))
                        continue
                kind = str(item.get("kind") or "").strip()
                value = str(item.get("value") or "").strip()
                if kind and value:
                    resources.append(DownloadResource(kind=kind, value=value))
        raw_resources = details.get("resources")
        if isinstance(raw_resources, list):
            for item in raw_resources:
                if not isinstance(item, dict):
                    continue
                kind = str(item.get("kind") or "").strip()
                value = str(item.get("value") or "").strip()
                source_message_id = str(item.get("source_message_id") or "").strip()
                if kind and value:
                    resources.append(DownloadResource(kind=kind, value=value, source_message_id=source_message_id))
        return self._merge_resources([], resources)

    def _log_resources_from_context(self, context) -> list[DownloadResource]:
        if context is None:
            return []
        return self._log_resources_from_session(self.activity_store.get_session(context.root_message_id))

    def _should_inherit_signal_resources(self, event: LarkEvent, route_content: str) -> bool:
        if event.reply_to or event.parent_id or event.root_id or event.thread_id:
            return True
        if self._is_followup_intent(route_content):
            return True
        lowered = route_content.casefold()
        return any(
            term in lowered
            for term in (
                "基于日志",
                "用日志",
                "看日志",
                "日志",
                "那就",
                "继续",
                "刚才",
                "上次",
                "上轮",
                "上一",
                "之前",
                "已有",
                "这个",
                "这些",
                "同样",
            )
        )

    def _fetch_resources_from_session_message(self, session: dict[str, object]) -> list[DownloadResource]:
        message_id = str(session.get("message_id") or session.get("session_id") or "").strip()
        if not message_id:
            return []
        synthetic_event = LarkEvent(
            event_id=str(session.get("event_id") or ""),
            message_id=message_id,
            chat_id=str(session.get("chat_id") or ""),
            chat_type=str(session.get("chat_type") or ""),
            sender_id=str(session.get("sender_id") or ""),
            message_type="text",
            content=str(session.get("content") or ""),
            reply_to=str(session.get("reply_to") or ""),
            parent_id=str(session.get("parent_id") or ""),
            root_id=str(session.get("root_id") or ""),
            thread_id=str(session.get("thread_id") or ""),
        )
        resources = self._fetch_referenced_message_resources(
            synthetic_event,
            route_content=synthetic_event.content,
            force_current_lookup=True,
        )
        fetched = self.lark_client.fetch_message(message_id)
        if fetched.returncode == 0:
            resources = self._merge_resources(
                resources,
                self._extract_resources_from_message_payload(fetched.stdout, fallback_message_id=message_id),
            )
        return resources

    def _reference_chain_log_resources(self, event: LarkEvent) -> list[DownloadResource]:
        reference_ids = self._fetch_followup_reference_ids(event)
        for message_id in self._followup_context_candidate_ids(event, reference_ids):
            resources = self._log_resources_from_reference_id(message_id)
            if resources:
                return resources
        return []

    def _log_resources_from_reference_id(self, message_id: str) -> list[DownloadResource]:
        normalized = message_id.strip()
        if not normalized:
            return []
        session = self.activity_store.get_session(normalized)
        resources = self._log_resources_from_session(session)
        if resources:
            return resources
        if session:
            resources = self._fetch_resources_from_session_message(session)
            if resources:
                return resources
        context = self.conversation_store.lookup(normalized)
        resources = self._log_resources_from_context(context)
        if resources:
            return resources
        fetched = self.lark_client.fetch_message(normalized)
        if fetched.returncode == 0:
            resources = self._extract_resources_from_message_payload(fetched.stdout, fallback_message_id=normalized)
            if resources:
                return resources
        return []

    def _contextual_signal_resources(
        self,
        event: LarkEvent,
        route_content: str,
        *,
        explicit_followup_context,
        latest_chat_context,
    ) -> list[DownloadResource]:
        resources = self._reference_chain_log_resources(event)
        if resources:
            return resources
        if not self._should_inherit_signal_resources(event, route_content):
            return []
        resources = self._log_resources_from_context(explicit_followup_context)
        if resources:
            return resources
        _ = latest_chat_context
        return []

    def _contextual_app_server_bug_url(self, *, explicit_followup_context, latest_chat_context) -> str:
        """续聊「自主分析」缺少 bug 链接时，从回复上下文或本群最近一次分析继承。"""
        for context in (explicit_followup_context, latest_chat_context):
            if context is None:
                continue
            bug_url = self._bug_url_from_request_text(getattr(context, "request_text", "") or "")
            if bug_url:
                return bug_url
        return ""

    def _build_perception_summary_request(self, route_content: str, referenced_resources: list[DownloadResource]):
        request = parse_perception_summary_request(route_content)
        merged_resources = self._merge_resources(request.resources, referenced_resources)
        if request.triggered:
            return request.__class__(
                prompt=request.prompt,
                resources=merged_resources,
                raw_text=request.raw_text,
                triggered=True,
                error=request.error,
            )
        if not referenced_resources:
            return request
        hinted = f"{route_content.strip()} {' '.join(item.value for item in referenced_resources)}".strip()
        hinted_request = parse_perception_summary_request(hinted)
        if not hinted_request.triggered:
            return request
        return request.__class__(
            prompt=route_content.strip(),
            resources=merged_resources,
            raw_text=route_content,
            triggered=True,
            error=None if route_content.strip() else "missing_prompt",
        )

    def _build_direct_analysis_request(
        self,
        route_content: str,
        referenced_resources: list[DownloadResource],
        *,
        event: LarkEvent | None = None,
    ):
        request = parse_direct_analysis_request(route_content)
        local_resources = self._authorized_local_download_resources(event, route_content)
        merged_resources = self._merge_resources(request.resources, [*referenced_resources, *local_resources])
        if request.triggered:
            return request.__class__(
                prompt=request.prompt,
                resources=merged_resources,
                raw_text=request.raw_text,
                triggered=True,
                error=request.error,
            )
        if local_resources and self._looks_like_direct_analysis_prompt(route_content):
            return request.__class__(
                prompt=route_content.strip(),
                resources=merged_resources,
                raw_text=route_content,
                triggered=True,
                error=None if route_content.strip() else "missing_prompt",
            )
        if not referenced_resources:
            return request
        hinted = f"{route_content.strip()} {' '.join(item.value for item in referenced_resources)}".strip()
        hinted_request = parse_direct_analysis_request(hinted)
        if not hinted_request.triggered:
            prompt = route_content.strip()
            if not prompt or not self._looks_like_direct_analysis_prompt(route_content):
                return request
            return request.__class__(
                prompt=prompt,
                resources=merged_resources,
                raw_text=route_content,
                triggered=True,
                error=None,
            )
        return request.__class__(
            prompt=route_content.strip(),
            resources=merged_resources,
            raw_text=route_content,
            triggered=True,
            error=None if route_content.strip() else "missing_prompt",
        )

    def _build_app_server_investigation_request(
        self,
        route_content: str,
        referenced_resources: list[DownloadResource],
        *,
        event: LarkEvent | None = None,
    ):
        request = parse_app_server_investigation_request(
            route_content,
            bug_url_re=self.bug_url_re,
            auto_terms=self.config.bug_analysis.app_server_investigation.auto_terms,
            free_terms=self.config.bug_analysis.app_server_investigation.free_terms,
        )
        if not request.triggered:
            return request
        local_resources = self._authorized_local_download_resources(event, route_content)
        merged_resources = self._merge_resources(request.resources, [*referenced_resources, *local_resources])
        return request.__class__(
            prompt=request.prompt,
            bug_url=request.bug_url,
            resources=merged_resources,
            raw_text=request.raw_text,
            triggered=True,
            error=request.error,
            trigger_mode=request.trigger_mode,
            trigger_term=request.trigger_term,
        )

    def _authorized_local_download_resources(self, event: LarkEvent | None, route_content: str) -> list[DownloadResource]:
        options = self.config.local_resources
        if not options.enabled or event is None:
            return []
        if options.require_allowed_user and event.sender_id not in set(self.config.allowed_users):
            return []
        lowered_content = route_content.casefold()
        if not any(term.casefold() in lowered_content for term in LOCAL_DOWNLOAD_AUTH_TERMS):
            return []
        resources: list[DownloadResource] = []
        seen: set[Path] = set()
        for file_name in LOCAL_RESOURCE_NAME_RE.findall(route_content):
            safe_name = Path(file_name).name
            if safe_name != file_name:
                continue
            for base_dir in options.allowed_dirs:
                candidate = (Path(base_dir).expanduser() / safe_name).resolve()
                if candidate in seen:
                    continue
                if not self._is_path_under_allowed_local_dir(candidate, options.allowed_dirs):
                    continue
                if not candidate.is_file():
                    continue
                seen.add(candidate)
                resources.append(DownloadResource(kind="local", value=str(candidate)))
        return resources

    def _is_path_under_allowed_local_dir(self, path: Path, allowed_dirs: list[Path]) -> bool:
        try:
            resolved_path = path.expanduser().resolve()
        except OSError:
            return False
        for base_dir in allowed_dirs:
            try:
                resolved_base = Path(base_dir).expanduser().resolve()
            except OSError:
                continue
            if resolved_path == resolved_base or resolved_base in resolved_path.parents:
                return True
        return False

    def _merge_resources(
        self,
        primary: list[DownloadResource],
        extra: list[DownloadResource],
    ) -> list[DownloadResource]:
        merged: list[DownloadResource] = []
        seen: dict[tuple[str, str, str], int] = {}
        for item in [*primary, *extra]:
            key = (item.kind, item.value, item.source_message_id.strip())
            existing_index = seen.get(key)
            if existing_index is not None:
                if not merged[existing_index].display_name and item.display_name:
                    merged[existing_index] = item
                continue
            seen[key] = len(merged)
            merged.append(item)
        return merged

    def _extract_resource_display_name(self, value: dict[str, object]) -> str:
        for key in ("file_name", "fileName", "filename", "name"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return ""

    def _fetch_referenced_message_resources(
        self,
        event: LarkEvent,
        *,
        route_content: str,
        force_current_lookup: bool = False,
    ) -> list[DownloadResource]:
        resources: list[DownloadResource] = []
        candidate_ids = self._candidate_reference_message_ids(
            event,
            route_content=route_content,
            force_current_lookup=force_current_lookup,
        )
        for message_id in candidate_ids:
            fetched = self.lark_client.fetch_message(message_id)
            if fetched.returncode != 0:
                continue
            resources = self._merge_resources(
                resources,
                self._extract_resources_from_message_payload(fetched.stdout, fallback_message_id=message_id),
            )
        if resources:
            return resources
        seen = set(candidate_ids)
        reference_ids = self._fetch_followup_reference_ids(event)
        for message_id in self._followup_context_candidate_ids(event, reference_ids):
            if message_id in seen:
                continue
            seen.add(message_id)
            fetched = self.lark_client.fetch_message(message_id)
            if fetched.returncode != 0:
                continue
            resources = self._merge_resources(
                resources,
                self._extract_resources_from_message_payload(fetched.stdout, fallback_message_id=message_id),
            )
        return resources

    def _candidate_reference_message_ids(
        self,
        event: LarkEvent,
        *,
        route_content: str,
        force_current_lookup: bool = False,
    ) -> list[str]:
        candidates = [value for value in [event.reply_to, event.parent_id, event.root_id] if value]
        should_lookup_current = force_current_lookup or self._should_lookup_current_message_for_resources(event, route_content)
        if not candidates and should_lookup_current and event.message_id:
            fetched_current = self.lark_client.fetch_message(event.message_id)
            if fetched_current.returncode == 0:
                candidates.extend(
                    candidate
                    for candidate in self._extract_message_reference_ids(fetched_current.stdout)
                    if candidate and candidate != event.message_id
                )
        unique: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            value = candidate.strip()
            if not value or value in seen:
                continue
            seen.add(value)
            unique.append(value)
        return unique[:3]

    def _should_lookup_current_message_for_resources(self, event: LarkEvent, route_content: str) -> bool:
        if event.reply_to or event.parent_id or event.root_id:
            return True
        inline_app_server = parse_app_server_investigation_request(
            route_content,
            bug_url_re=self.bug_url_re,
            auto_terms=self.config.bug_analysis.app_server_investigation.auto_terms,
            free_terms=self.config.bug_analysis.app_server_investigation.free_terms,
        )
        if inline_app_server.triggered:
            return not inline_app_server.resources and not inline_app_server.bug_url
        inline_direct = parse_direct_analysis_request(route_content)
        if inline_direct.triggered:
            return not inline_direct.resources
        if self._looks_like_direct_analysis_prompt(route_content):
            return True
        inline_perception = parse_perception_summary_request(route_content)
        if inline_perception.triggered:
            return not inline_perception.resources
        inline_signal = parse_signal_request(
            route_content,
            signal_aliases=self.config.signal_aliases,
            command_prefixes=self.config.command_prefixes,
            signal_resolver=self.signal_resolver,
        )
        if inline_signal.triggered:
            return not inline_signal.resources
        return False

    def _looks_like_direct_analysis_prompt(self, route_content: str) -> bool:
        return looks_like_direct_analysis_prompt(route_content, resources_present=True, bug_url_re=self.bug_url_re)

    def _extract_resources_from_message_payload(self, payload_text: str, *, fallback_message_id: str = "") -> list[DownloadResource]:
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, dict):
            return []
        data = payload.get("data", {})
        if not isinstance(data, dict):
            return []
        messages = data.get("messages")
        if isinstance(messages, dict):
            messages = [messages]
        if not isinstance(messages, list):
            return []
        resources: list[DownloadResource] = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            message_id = str(message.get("message_id") or fallback_message_id).strip()
            extracted = self._extract_resources_from_message_value(message, source_message_id=message_id)
            resources = self._merge_resources(resources, extracted)
        return resources

    def _extract_resources_from_message_value(self, value: object, *, source_message_id: str) -> list[DownloadResource]:
        resources: list[DownloadResource] = []
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                parsed = None
            if parsed is not None and parsed is not value:
                return self._extract_resources_from_message_value(parsed, source_message_id=source_message_id)
            return [
                item
                for item in find_resources(value, source_message_id=source_message_id)
                if item.kind in {"file", "folder", "image"}
            ]
        if isinstance(value, dict):
            file_display_name = self._extract_resource_display_name(value)
            for key, nested in value.items():
                if key == "file_key" and isinstance(nested, str) and nested.strip():
                    resources = self._merge_resources(
                        resources,
                        [
                            DownloadResource(
                                kind="file",
                                value=nested.strip(),
                                source_message_id=source_message_id,
                                display_name=file_display_name,
                            )
                        ],
                    )
                    continue
                if key == "image_key" and isinstance(nested, str) and nested.strip():
                    resources = self._merge_resources(
                        resources,
                        [DownloadResource(kind="image", value=nested.strip(), source_message_id=source_message_id)],
                    )
                    continue
                if key in {"folder_token", "folderToken"} and isinstance(nested, str) and nested.strip():
                    resources = self._merge_resources(
                        resources,
                        [DownloadResource(kind="folder", value=nested.strip(), source_message_id=source_message_id)],
                    )
                    continue
                resources = self._merge_resources(
                    resources,
                    self._extract_resources_from_message_value(nested, source_message_id=source_message_id),
                )
            return resources
        if isinstance(value, list):
            for item in value:
                resources = self._merge_resources(
                    resources,
                    self._extract_resources_from_message_value(item, source_message_id=source_message_id),
                )
        return resources

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

    def _handle_bug_intent(self, event: LarkEvent, route_content: str) -> TaskResult:
        bug_request = parse_bug_request(route_content, bug_url_re=self.bug_url_re)
        if not bug_request.triggered:
            bug_request = bug_request.__class__(bug_url="", prompt=route_content.strip(), raw_text=route_content, triggered=True, error="missing_bug_url")
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
