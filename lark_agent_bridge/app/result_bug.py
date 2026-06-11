from __future__ import annotations

from datetime import datetime, timezone
import json
import re
from urllib.parse import quote, urlsplit, urlunsplit

from ._shared import *  # noqa: F401,F403


from .skill_clarify import _SkillClarifyMixin
from .request_exec import _RequestExecMixin
from .card_actions import _CardActionsMixin


class _ResultBugMixin(_SkillClarifyMixin, _RequestExecMixin, _CardActionsMixin):
    def _finish_progress_card(self, event: LarkEvent, result: TaskResult, *, session_id: str | None = None) -> bool:
        status = "completed" if result.success else "failed"
        note = self._progress_result_note(result) if result.success else (result.message[:MAX_ERROR_PREVIEW] if result.message else "分析失败")
        updated = self._update_progress_card(event, status=status, session_id=session_id, result=result, note=note)
        key = self._progress_card_state_key(event, session_id=session_id)
        if updated:
            with self._progress_cards_lock:
                self._progress_cards.pop(key, None)
        return updated
    def _prune_stale_progress_cards(self) -> None:
        """Remove progress card entries older than the TTL to prevent memory leaks.

        Uses ``last_active_at`` (refreshed on every update) rather than
        ``started_at`` so that long-running tasks that continuously report
        progress are never pruned prematurely.
        """
        now = datetime.now(timezone.utc)
        with self._progress_cards_lock:
            stale_keys = [
                key
                for key, state in self._progress_cards.items()
                if isinstance(state.get("last_active_at", state.get("started_at")), datetime)
                and (now - state.get("last_active_at", state["started_at"])).total_seconds() > self._progress_cards_max_age_seconds
            ]
            for key in stale_keys:
                self._progress_cards.pop(key, None)
    def _progress_result_note(self, result: TaskResult) -> str | None:
        message = (result.message or "").strip()
        if not message:
            return None
        summary = self._link_delivery_summary(message)
        if len(summary) > MAX_CARD_NOTE:
            summary = summary[:MAX_CARD_NOTE - 1].rstrip() + "…"
        return summary
    def _build_progress_card(
        self,
        event: LarkEvent,
        *,
        key: str,
        status: str,
        result: TaskResult | None = None,
        note: str | None = None,
    ) -> dict[str, object]:
        with self._progress_cards_lock:
            card_state = self._progress_cards.get(key, {})
        details = dict(card_state.get("details") if isinstance(card_state.get("details"), dict) else {})
        if result is not None:
            if status in {"completed", "failed"}:
                details.pop("当前阶段", None)
            mode = str(result.details.get("mode") or "")
            if mode:
                details["分析类型"] = self._progress_mode_label(mode)
            if result.job_id:
                details.setdefault("任务ID", result.job_id[:20])
            skill_label = str(result.details.get("analysis_skill_label") or result.details.get("analysis_skill") or "").strip()
            if skill_label:
                details["命中 Skill"] = skill_label
            classification_source = str(result.details.get("classification_source") or "").strip()
            if classification_source:
                details["分类来源"] = classification_source
            agent_model = str(result.details.get("agent_summary_model") or "").strip()
            if agent_model:
                details.setdefault("Agent 模型", agent_model)
        report_url = ""
        if result is not None:
            report_url = str(result.details.get("published_report_url") or "").strip()
        root_message_id = ""
        job_id = ""
        show_followup_actions = False
        if result is not None:
            root_message_id = str(
                result.details.get("conversation_root_message_id")
                or event.root_id
                or event.message_id
                or key
                or ""
            ).strip()
            job_id = str(result.job_id or result.details.get("job_id") or "").strip()
            show_followup_actions = bool(self._card_actions_enabled() and result.success and report_url and root_message_id)
        bug_skill_choices = (
            self._result_bug_skill_choices(result) if result is not None and self._card_actions_enabled() else []
        )
        session = self.activity_store.get_session(key) or self.activity_store.get_session(event.message_id) or {}
        progress = session.get("progress") if isinstance(session, dict) else []
        if not isinstance(progress, list):
            progress = []
        return build_status_card(
            title=str(card_state.get("title") or "分析进度"),
            status=status,
            details={str(k): str(v) for k, v in details.items() if str(v)},
            progress=progress,
            elapsed_seconds=self._progress_elapsed_seconds(card_state, result=result),
            token_usage=self._progress_token_usage(result),
            report_url=report_url or None,
            live_url=self._progress_live_url(key),
            note=note,
            job_id=job_id or None,
            root_message_id=root_message_id or None,
            show_followup_actions=show_followup_actions,
            bug_skill_choices=bug_skill_choices,
            bug_skill_choice_note=self._result_bug_skill_choice_note(result) if self._card_actions_enabled() else None,
            bug_agent_choices=self._result_bug_agent_choices(result) if self._card_actions_enabled() else [],
        )
    def _progress_live_url(self, session_id: str) -> str | None:
        if not self.config.report_server.enabled or not session_id:
            return None
        base = resolve_public_base_url(
            self.config.report_server.public_base_url,
            port=self.config.report_server.port,
            bind_host=self.config.report_server.bind_host,
        )
        parts = urlsplit(base)
        query = f"session={quote(session_id, safe='')}"
        return urlunsplit((parts.scheme, parts.netloc, "/sessions", query, ""))
    def _progress_elapsed_seconds(self, card_state: dict[str, object], *, result: TaskResult | None = None) -> float | None:
        if result is not None and isinstance(result.duration_seconds, (int, float)):
            return float(result.duration_seconds)
        started_at = card_state.get("started_at")
        if isinstance(started_at, datetime):
            return max(0.0, (datetime.now(timezone.utc) - started_at).total_seconds())
        return None
    def _progress_token_usage(self, result: TaskResult | None) -> dict[str, int] | None:
        if result is None:
            return None
        _prefix, usage = extract_first_prefixed_token_usage(result.details, ("agent_summary_", "app_server_"))
        return usage or None
    def _progress_mode_label(self, mode: str) -> str:
        labels = {
            "bug_analysis": "Bug 分析",
            "bug_clarification": "Bug 分析分诊",
            "bug_skill_confirmation": "Bug Skill 确认",
            "bug_reanalysis": "Bug 重新分析",
            "bug_agent_followup": "Bug 追问",
            "direct_analysis": "直传文件分析",
            "app_server_investigation": "AI 自主分析",
            "source_analysis": "源码分析",
            "diagram_report_followup": "图表报告",
            "perception_summary": "感知数据总结",
            "signal_lifecycle": "信号生命周期",
            "claude_skill": "Claude Code 分析",
        }
        return labels.get(mode, mode)
    def _card_message_id_from_result(self, result) -> str:
        for text in (getattr(result, "stdout", ""), getattr(result, "stderr", "")):
            message_id = self._extract_card_message_id(text)
            if message_id:
                return message_id
        return ""
    def _remember_delivery_alias_from_result(self, result, *, root_message_id: str | None) -> None:
        message_id = self._card_message_id_from_result(result)
        self._remember_conversation_alias(message_id, root_message_id)
    def _remember_conversation_alias(self, alias_message_id: str, root_message_id: str | None) -> None:
        alias = str(alias_message_id or "").strip()
        root = str(root_message_id or "").strip()
        if not alias or not root or alias == root:
            return
        self.conversation_store.remember_alias(alias_message_id=alias, root_message_id=root)
    def _extract_card_message_id(self, text: str) -> str:
        if not text.strip():
            return ""
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\b(?:open_)?message_id\b[\"'=:\s]+([A-Za-z0-9_\\-]+)", text)
            return match.group(1) if match else ""
        return self._find_message_id(payload)
    def _find_message_id(self, value) -> str:
        if isinstance(value, dict):
            for key in ("message_id", "open_message_id"):
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    return candidate.strip()
            for nested in value.values():
                found = self._find_message_id(nested)
                if found:
                    return found
        if isinstance(value, list):
            for item in value:
                found = self._find_message_id(item)
                if found:
                    return found
        return ""
