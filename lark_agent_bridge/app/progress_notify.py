from __future__ import annotations

import json
from pathlib import Path
import re

from ._shared import *  # noqa: F401,F403


class _ProgressNotifyMixin:
    """进度通知推送与交付结果预处理（与 LogResourcesMixin 共享 self 状态）。"""

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
