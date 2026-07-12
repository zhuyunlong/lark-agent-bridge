"""Normalized event input shared by conversation-aware routes."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal
import unicodedata

from .models import DownloadResource, LarkEvent
from .parsing.chat import parse_followup_action
from .parsing.terms import FOLLOWUP_CONTINUE_TERMS, FOLLOWUP_RETRY_TERMS


ConversationAction = Literal["new", "retry", "continue", "ask", "unknown"]
ResourceSelection = Literal[
    "current_message",
    "reply_chain",
    "session",
    "bug_attachment",
    "none",
]


@dataclass(frozen=True, slots=True)
class ResolvedConversationResources:
    current_message: tuple[DownloadResource, ...]
    reply_chain: tuple[DownloadResource, ...]
    session: tuple[DownloadResource, ...]
    bug_attachments: tuple[DownloadResource, ...]
    preferred_resources: tuple[DownloadResource, ...]
    selected_source: ResourceSelection
    source_message_ids: tuple[str, ...]


def resolve_conversation_resources(
    *,
    current_message: list[DownloadResource],
    reply_chain: list[DownloadResource],
    session: list[DownloadResource],
    bug_attachments: list[DownloadResource],
) -> ResolvedConversationResources:
    groups = (
        ("current_message", current_message),
        ("reply_chain", reply_chain),
        ("session", session),
        ("bug_attachment", bug_attachments),
    )
    selected_source: ResourceSelection = "none"
    preferred: tuple[DownloadResource, ...] = ()
    for source, resources in groups:
        if resources:
            selected_source = source
            preferred = tuple(resources)
            break
    source_message_ids: list[str] = []
    for _, resources in groups:
        for resource in resources:
            message_id = resource.source_message_id.strip()
            if message_id and message_id not in source_message_ids:
                source_message_ids.append(message_id)
    return ResolvedConversationResources(
        current_message=tuple(current_message),
        reply_chain=tuple(reply_chain),
        session=tuple(session),
        bug_attachments=tuple(bug_attachments),
        preferred_resources=preferred,
        selected_source=selected_source,
        source_message_ids=tuple(source_message_ids),
    )


@dataclass(frozen=True, slots=True)
class ResolvedConversationInput:
    event: LarkEvent
    raw_text: str
    route_text: str
    direct_reply_to: str
    conversation_root_message_id: str
    followup_context: object | None
    referenced_resources: tuple[DownloadResource, ...]
    resources: ResolvedConversationResources
    followup_action: ConversationAction
    followup_payload: str
    is_new_chain: bool
    route_text_source: str
    context_source: str
    resource_source_message_ids: tuple[str, ...]


def build_resolved_conversation_input(
    *,
    event: LarkEvent,
    route_text: str,
    direct_reply_to: str,
    followup_context: object | None,
    referenced_resources: list[DownloadResource],
    is_new_chain: bool,
    route_text_source: str,
    context_source: str,
    conversation_root_message_id: str = "",
    resource_view: ResolvedConversationResources | None = None,
) -> ResolvedConversationInput:
    normalized_route_text = (route_text or "").strip()
    if followup_context is None:
        action: ConversationAction = "new"
    else:
        parsed_action = parse_followup_action(normalized_route_text)
        action = parsed_action if parsed_action in {"retry", "continue", "ask", "unknown"} else "unknown"
    payload = normalized_route_text
    if action in {"retry", "continue"} and is_pure_followup_control(normalized_route_text):
        payload = ""
    root_message_id = str(
        conversation_root_message_id
        or getattr(followup_context, "root_message_id", "")
        or event.root_id
        or event.message_id
        or ""
    ).strip()
    resolved_resources = resource_view or resolve_conversation_resources(
        current_message=[],
        reply_chain=referenced_resources,
        session=[],
        bug_attachments=[],
    )
    return ResolvedConversationInput(
        event=event,
        raw_text=event.content,
        route_text=normalized_route_text,
        direct_reply_to=(direct_reply_to or "").strip(),
        conversation_root_message_id=root_message_id,
        followup_context=followup_context,
        referenced_resources=tuple(referenced_resources),
        resources=resolved_resources,
        followup_action=action,
        followup_payload=payload,
        is_new_chain=is_new_chain,
        route_text_source=route_text_source,
        context_source=context_source,
        resource_source_message_ids=resolved_resources.source_message_ids,
    )


def is_pure_followup_control(text: str) -> bool:
    cleaned = unicodedata.normalize("NFKC", text or "").strip()
    compact = re.sub(r"[\s，。；;：:、!?！？]+", "", cleaned).casefold()
    if not compact:
        return False
    action = parse_followup_action(cleaned)
    terms = FOLLOWUP_RETRY_TERMS if action == "retry" else FOLLOWUP_CONTINUE_TERMS if action == "continue" else ()
    normalized_terms = {
        re.sub(r"[\s，。；;：:、!?！？]+", "", term).casefold()
        for term in terms
        if term
    }
    if compact in normalized_terms:
        return True
    if action == "retry":
        return compact in {
            "重新分析",
            "重新分析下",
            "重新分析一下",
            "重新分析一次",
            "重新分析一遍",
            "再分析一次",
            "再分析一遍",
        }
    if action == "continue":
        return compact in {"继续自主分析", "继续自主分析下", "继续自主分析一下"}
    return False
