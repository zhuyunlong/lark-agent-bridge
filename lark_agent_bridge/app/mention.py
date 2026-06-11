from __future__ import annotations

from datetime import datetime, timezone
import html
from pathlib import Path
import re
import threading
from typing import Callable

from ._shared import *  # noqa: F401,F403


class _MentionMixin:
    """群聊 @bot 提及解析与剥离（与 HandleEventMixin 共享 self 状态）。"""

    def _normalize_mention_source_text(self, text: str) -> str:
        normalized = html.unescape(str(text or ""))
        normalized = re.sub(r"(?i)<br\s*/?>", " ", normalized)
        normalized = re.sub(r"(?i)</?(?:p|div|span)[^>]*>", " ", normalized)
        return normalized.strip()
    def _strip_group_chat_mention(self, text: str, *, event: LarkEvent | None = None) -> str | None:
        content = self._normalize_mention_source_text(text)
        configured_bot = self.config.lark.bot_open_id.strip()
        if configured_bot:
            at_matches = re.findall(r'<at\s+[^>]*user_id="([^"]+)"[^>]*></at>', content)
            if configured_bot in at_matches:
                cleaned = re.sub(
                    rf'<at\s+[^>]*user_id="{re.escape(configured_bot)}"[^>]*></at>\s*',
                    " ",
                    content,
                )
                return self._normalize_mention_text(cleaned)
        else:
            at_tag = re.match(r'^<at\s+[^>]*user_id="([^"]+)"[^>]*></at>\s*(.*)$', content)
            if at_tag:
                return at_tag.group(2).strip()
        configured_name = self.config.lark.bot_name.strip()
        if configured_name:
            cleaned = self._strip_configured_bot_name_mention(content, configured_name)
            if cleaned is not None:
                return cleaned
            if configured_bot:
                return None
            return None
        if event is not None:
            cleaned = self._strip_runtime_bot_mention(content, event)
            if cleaned is not None:
                return cleaned
            return None
        if configured_bot:
            return None
        spaced_name_at = re.match(r"^@.+\s+(/chat(?:\s+.*)?)$", content)
        if spaced_name_at:
            return spaced_name_at.group(1).strip()
        plain_at = re.match(r"^@\S+\s+(.*)$", content)
        if plain_at:
            return plain_at.group(1).strip()
        return None
    def _strip_bot_mention_anywhere(self, text: str) -> str | None:
        content = self._normalize_mention_source_text(text)
        configured_bot = self.config.lark.bot_open_id.strip()
        at_matches = re.findall(r'<at\s+[^>]*user_id="([^"]+)"[^>]*></at>', content)
        if at_matches and (not configured_bot or configured_bot in at_matches):
            cleaned = re.sub(r'<at\s+[^>]*></at>\s*', " ", content)
            return self._normalize_mention_text(cleaned)
        configured_name = self.config.lark.bot_name.strip()
        if configured_name:
            cleaned = self._strip_configured_bot_name_mention(content, configured_name)
            if cleaned is not None:
                return cleaned
            return None
        generic_plain = re.search(r"@\S+", content)
        if generic_plain:
            return self._normalize_mention_text(re.sub(r"@\S+", " ", content, count=1))
        return None
    def _normalize_mention_text(self, text: str) -> str:
        return re.sub(r"\s+", " ", text).strip()
    def _strip_configured_bot_name_mention(self, content: str, configured_name: str) -> str | None:
        name = configured_name.strip()
        if not name:
            return None
        tokens = [token for token in re.split(r"\s+", name) if token]
        if not tokens:
            return None
        # Feishu sometimes exposes plain mention text with spaces collapsed or
        # removed, so match configured bot names in a whitespace-tolerant way
        # without falling back to arbitrary @someone mentions.
        name_pattern = r"\s*".join(re.escape(token) for token in tokens)
        mention_pattern = re.compile(rf"(?<!\S)@{name_pattern}(?=\s|$)")
        if not mention_pattern.search(content):
            return None
        return self._normalize_mention_text(mention_pattern.sub(" ", content))
    def _strip_runtime_bot_mention(self, content: str, event: LarkEvent) -> str | None:
        for name in self._runtime_bot_mention_names(event):
            cleaned = self._strip_configured_bot_name_mention(content, name)
            if cleaned is not None:
                return cleaned
        return None
    def _runtime_bot_mention_names(self, event: LarkEvent) -> list[str]:
        names = self._bot_mention_names_from_payload(event.raw)
        if names or not event.message_id:
            return names
        fetched = self.lark_client.fetch_message(event.message_id)
        if fetched.returncode != 0:
            return []
        return self._bot_mention_names_from_payload_text(fetched.stdout, message_id=event.message_id)
    def _bot_mention_names_from_payload(self, payload: object) -> list[str]:
        if not isinstance(payload, dict):
            return []
        messages: list[dict[str, object]] = []
        direct_message = payload.get("message")
        if isinstance(direct_message, dict):
            messages.append(direct_message)
        event_body = payload.get("event")
        if isinstance(event_body, dict):
            nested_message = event_body.get("message")
            if isinstance(nested_message, dict):
                messages.append(nested_message)
        if not messages:
            return []
        return self._bot_mention_names_from_messages(messages)
    def _bot_mention_names_from_payload_text(self, payload_text: str, *, message_id: str = "") -> list[str]:
        messages = self._extract_message_records(payload_text)
        if message_id:
            filtered = []
            for message in messages:
                current_id = str(message.get("message_id") or "").strip()
                if not current_id or current_id == message_id:
                    filtered.append(message)
            messages = filtered
        return self._bot_mention_names_from_messages(messages)
    def _bot_mention_names_from_messages(self, messages: list[dict[str, object]]) -> list[str]:
        names: list[str] = []
        for message in messages:
            mentions = message.get("mentions")
            if not isinstance(mentions, list):
                continue
            for mention in mentions:
                if not isinstance(mention, dict) or not self._is_bot_mention_metadata(mention):
                    continue
                name = str(mention.get("name") or "").strip()
                if name and name not in names:
                    names.append(name)
        return names
    def _is_bot_mention_metadata(self, mention: dict[str, object]) -> bool:
        mention_id = str(
            mention.get("id")
            or mention.get("open_id")
            or mention.get("user_id")
            or mention.get("union_id")
            or ""
        ).strip()
        if mention_id:
            configured_bot = self.config.lark.bot_open_id.strip()
            if configured_bot and mention_id == configured_bot:
                return True
            if mention_id.startswith("cli_"):
                return True
        mention_type = str(mention.get("type") or mention.get("id_type") or "").strip().casefold()
        return mention_type in {"bot", "app_id"}
