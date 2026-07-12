"""Resolve one event's address and reply-chain identity exactly once."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..models import LarkEvent
from ..state import ConversationContext


class MessageFetchCache:
    """Event-scoped cache for Feishu message payloads, including misses."""

    def __init__(self, lark_client: object) -> None:
        self._lark_client = lark_client
        self._payloads: dict[str, str | None] = {}

    def payload(self, message_id: str) -> str | None:
        key = (message_id or "").strip()
        if not key:
            return None
        if key not in self._payloads:
            fetched = self._lark_client.fetch_message(key)
            self._payloads[key] = fetched.stdout if fetched.returncode == 0 else None
        return self._payloads[key]


@dataclass(frozen=True, slots=True)
class ConversationResolution:
    addressed: bool
    route_text: str
    direct_reply_to: str
    followup_context: ConversationContext | None
    conversation_root_message_id: str
    is_new_chain: bool
    route_text_source: str
    context_source: str
    message_cache: MessageFetchCache


class ConversationResolver:
    def __init__(self, app: Any) -> None:
        self._app = app

    def resolve(self, event: LarkEvent) -> ConversationResolution:
        cache = MessageFetchCache(self._app.lark_client)
        route_text = event.content
        route_text_source = "event_content"
        context_source = "none"
        direct_reply_to = self._app._direct_reply_to(event, message_cache=cache)
        followup_context: ConversationContext | None = None
        addressed = True

        if event.chat_type == "group":
            stripped_at_bot = self._app._strip_group_chat_mention(
                event.content,
                event=event,
                message_cache=cache,
            )
            addressed_text: str | None = None
            if direct_reply_to:
                followup_context = self._app._lookup_bot_alias_context(direct_reply_to)
                if followup_context is not None:
                    context_source = "bot_alias"
                    addressed_text = (
                        stripped_at_bot if stripped_at_bot is not None else event.content.strip()
                    )
                    route_text_source = (
                        "group_mention" if stripped_at_bot is not None else "bot_alias_reply"
                    )
                elif stripped_at_bot is not None:
                    addressed_text = stripped_at_bot
                    route_text_source = "group_mention"
            elif stripped_at_bot is not None:
                addressed_text = stripped_at_bot
                route_text_source = "group_mention"
            if addressed_text is None:
                addressed = False
            else:
                route_text = addressed_text

        if addressed and followup_context is None:
            followup_context = self._app._resolve_followup_context(
                event,
                message_cache=cache,
            )
            if followup_context is not None:
                context_source = "reply_chain"

        root_message_id = str(
            getattr(followup_context, "root_message_id", "")
            or event.root_id
            or event.message_id
            or ""
        ).strip()
        return ConversationResolution(
            addressed=addressed,
            route_text=route_text,
            direct_reply_to=direct_reply_to,
            followup_context=followup_context,
            conversation_root_message_id=root_message_id,
            is_new_chain=followup_context is None,
            route_text_source=route_text_source,
            context_source=context_source,
            message_cache=cache,
        )
