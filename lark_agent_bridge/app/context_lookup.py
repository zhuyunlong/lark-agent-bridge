from __future__ import annotations

import html
import json
import os
from pathlib import Path
import re
import shutil
import stat
from typing import TYPE_CHECKING
import unicodedata

from ._shared import *  # noqa: F401,F403

if TYPE_CHECKING:
    from .conversation_resolver import MessageFetchCache


class _ContextLookupMixin:
    """会话上下文多级查找（活动会话/引用消息/产物消息）（与 ContextFromMixin 共享 self 状态）。"""

    def _direct_reply_to(
        self,
        event: LarkEvent,
        *,
        message_cache: MessageFetchCache | None = None,
    ) -> str:
        explicit = (event.reply_to or "").strip()
        if explicit:
            return explicit
        parent = (event.parent_id or "").strip()
        if parent:
            return parent
        if not event.message_id:
            return ""
        payload = self._message_payload(event.message_id, message_cache=message_cache)
        if payload is None:
            return ""
        for message in self._extract_message_records(payload):
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
    def _resolve_followup_context(
        self,
        event: LarkEvent,
        *,
        message_cache: MessageFetchCache | None = None,
    ):
        context = self.conversation_store.find(event)
        if context is not None:
            return context
        reference_ids = self._fetch_followup_reference_ids(event, message_cache=message_cache)
        for key in reference_ids:
            context = self.conversation_store.lookup(key)
            if context is not None:
                return context
        for key in self._followup_context_candidate_ids(event, reference_ids):
            context = self._context_from_activity_session(key, event=event)
            if context is not None:
                return context
        for key in self._followup_context_candidate_ids(event, reference_ids):
            context = self._context_from_fetched_message(
                key,
                event=event,
                message_cache=message_cache,
            )
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
    def _context_from_fetched_message(
        self,
        message_id: str,
        *,
        event: LarkEvent,
        message_cache: MessageFetchCache | None = None,
    ) -> ConversationContext | None:
        payload = self._message_payload(message_id, message_cache=message_cache)
        if payload is None:
            return None
        for message in self._extract_message_records(payload):
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
        for message in self._extract_message_records(payload):
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
    def _fetch_followup_reference_ids(
        self,
        event: LarkEvent,
        *,
        message_cache: MessageFetchCache | None = None,
    ) -> list[str]:
        pending = [value for value in [event.reply_to, event.parent_id, event.root_id] if value]
        discovered: list[str] = []
        if not pending and event.message_id:
            current_payload = self._message_payload(event.message_id, message_cache=message_cache)
            if current_payload is not None:
                for candidate in self._extract_message_reference_ids(current_payload):
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
            payload = self._message_payload(current, message_cache=message_cache)
            if payload is None:
                continue
            for candidate in self._extract_message_reference_ids(payload):
                if not candidate or candidate in visited:
                    continue
                if candidate not in discovered:
                    discovered.append(candidate)
                pending.append(candidate)
        return discovered
    def _message_payload(
        self,
        message_id: str,
        *,
        message_cache: MessageFetchCache | None = None,
    ) -> str | None:
        if message_cache is not None:
            return message_cache.payload(message_id)
        fetched = self.lark_client.fetch_message(message_id)
        return fetched.stdout if fetched.returncode == 0 else None
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
