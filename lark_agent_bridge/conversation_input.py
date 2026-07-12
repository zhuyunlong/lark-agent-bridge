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
    action, payload = resolve_followup_action_payload(
        normalized_route_text,
        has_followup_context=followup_context is not None,
    )
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


def resolve_followup_action_payload(
    text: str,
    *,
    has_followup_context: bool,
) -> tuple[ConversationAction, str]:
    normalized = (text or "").strip()
    if not has_followup_context:
        return "new", normalized
    parsed_action = parse_followup_action(normalized)
    if parsed_action == "ask" and re.search(r"(?:重新|再)分析", normalized):
        parsed_action = "retry"
    action: ConversationAction = (
        parsed_action if parsed_action in {"retry", "continue", "ask", "unknown"} else "unknown"
    )
    if action in {"retry", "continue"} and is_pure_followup_control(normalized):
        return action, ""
    return action, normalized


def conversation_input_snapshot(resolved: ResolvedConversationInput) -> dict[str, object]:
    def descriptors(resources: tuple[DownloadResource, ...]) -> list[dict[str, str]]:
        return [
            {
                "kind": item.kind,
                "value": item.value,
                "source_message_id": item.source_message_id,
                "display_name": item.display_name,
            }
            for item in resources
        ]

    resource_view = resolved.resources
    return {
        "version": 1,
        "route_text": resolved.route_text,
        "direct_reply_to": resolved.direct_reply_to,
        "conversation_root_message_id": resolved.conversation_root_message_id,
        "followup_action": resolved.followup_action,
        "followup_payload": resolved.followup_payload,
        "is_new_chain": resolved.is_new_chain,
        "route_text_source": resolved.route_text_source,
        "context_source": resolved.context_source,
        "resources": {
            "selected_source": resource_view.selected_source,
            "current_message": descriptors(resource_view.current_message),
            "reply_chain": descriptors(resource_view.reply_chain),
            "session": descriptors(resource_view.session),
            "bug_attachments": descriptors(resource_view.bug_attachments),
            "preferred": descriptors(resource_view.preferred_resources),
        },
    }


def resource_view_from_snapshot(snapshot: dict[str, object]) -> ResolvedConversationResources | None:
    payload: object = snapshot.get("conversation_input") or snapshot
    if not isinstance(payload, dict) or payload.get("version") != 1:
        return None
    resources = payload.get("resources")
    if not isinstance(resources, dict):
        return None

    def load_group(name: str) -> list[DownloadResource]:
        raw_group = resources.get(name)
        if not isinstance(raw_group, list):
            return []
        loaded: list[DownloadResource] = []
        for item in raw_group:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or "").strip()
            value = str(item.get("value") or "").strip()
            if not kind or not value:
                continue
            loaded.append(
                DownloadResource(
                    kind=kind,
                    value=value,
                    source_message_id=str(item.get("source_message_id") or "").strip(),
                    display_name=str(item.get("display_name") or "").strip(),
                )
            )
        return loaded

    return resolve_conversation_resources(
        current_message=load_group("current_message"),
        reply_chain=load_group("reply_chain"),
        session=load_group("session"),
        bug_attachments=load_group("bug_attachments"),
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
