"""Lightweight Feishu interaction event models."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any


_CALLBACK_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_CARD_ACTION_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")


@dataclass(slots=True)
class ReactionEvent:
    event_id: str
    message_id: str
    reaction_type: str
    operator_id: str = ""
    chat_id: str = ""
    inbound_request_id: str = ""
    event_type: str = ""
    event_create_time: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ReactionEvent":
        header = payload.get("header") or {}
        event_body = payload.get("event") or payload
        message = event_body.get("message") or event_body
        reaction_type = (
            event_body.get("reaction_type")
            or event_body.get("reaction")
            or payload.get("reaction_type")
            or ""
        )
        return cls(
            event_id=str(payload.get("event_id") or header.get("event_id") or ""),
            message_id=_safe_callback_identifier(
                payload.get("message_id") or message.get("message_id") or event_body.get("message_id") or ""
            ),
            reaction_type=_safe_card_action(_reaction_type_value(reaction_type)),
            operator_id=_safe_callback_identifier(_event_operator_id(event_body)),
            chat_id=_safe_callback_identifier(
                payload.get("chat_id") or message.get("chat_id") or event_body.get("chat_id") or ""
            ),
            inbound_request_id=_safe_callback_identifier(_payload_request_id(payload, event_body)),
            event_type=str(payload.get("event_type") or header.get("event_type") or ""),
            event_create_time=str(payload.get("event_create_time") or header.get("create_time") or ""),
            raw=payload,
        )

    @property
    def is_valid(self) -> bool:
        return bool(self.event_id and self.message_id and self.reaction_type)

    @property
    def is_created(self) -> bool:
        return self.event_type == "im.message.reaction.created_v1"

    @property
    def is_deleted(self) -> bool:
        return self.event_type == "im.message.reaction.deleted_v1"


@dataclass(slots=True)
class MessageRecalledEvent:
    event_id: str
    message_id: str
    operator_id: str = ""
    chat_id: str = ""
    inbound_request_id: str = ""
    event_type: str = ""
    event_create_time: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "MessageRecalledEvent":
        header = payload.get("header") or {}
        event_body = payload.get("event") or payload
        message = event_body.get("message") or event_body
        return cls(
            event_id=str(payload.get("event_id") or header.get("event_id") or ""),
            message_id=_safe_callback_identifier(
                payload.get("message_id") or message.get("message_id") or event_body.get("message_id") or ""
            ),
            operator_id=_safe_callback_identifier(_event_operator_id(event_body)),
            chat_id=_safe_callback_identifier(
                payload.get("chat_id") or message.get("chat_id") or event_body.get("chat_id") or ""
            ),
            inbound_request_id=_safe_callback_identifier(_payload_request_id(payload, event_body)),
            event_type=str(payload.get("event_type") or header.get("event_type") or ""),
            event_create_time=str(payload.get("event_create_time") or header.get("create_time") or ""),
            raw=payload,
        )

    @property
    def is_valid(self) -> bool:
        return bool(self.event_id and self.message_id)


def _payload_request_id(payload: dict[str, Any], event_body: dict[str, Any]) -> Any:
    return (
        payload.get("request_id")
        or payload.get("requestId")
        or event_body.get("request_id")
        or event_body.get("requestId")
        or ""
    )


def _event_operator_id(event_body: dict[str, Any]) -> str:
    operator = event_body.get("operator") or {}
    operator_id = event_body.get("operator_id") or ""
    if isinstance(operator_id, dict):
        operator_id = operator_id.get("open_id") or operator_id.get("user_id") or ""
    if not operator_id and isinstance(operator, dict):
        nested_operator_id = operator.get("operator_id") or {}
        if isinstance(nested_operator_id, dict):
            operator_id = (
                nested_operator_id.get("open_id")
                or nested_operator_id.get("user_id")
                or nested_operator_id.get("union_id")
                or ""
            )
    return str(operator_id or "")


def _reaction_type_value(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("emoji_type", "emojiType", "type", "reaction_type", "reactionType"):
            text = _reaction_type_value(value.get(key))
            if text:
                return text
        return ""
    return str(value or "").strip()


def _safe_callback_identifier(value: Any) -> str:
    text = str(value or "").strip()
    if not text or not _CALLBACK_IDENTIFIER_RE.fullmatch(text):
        return ""
    return text


def _safe_card_action(value: Any) -> str:
    text = str(value or "").strip()
    if not text or not _CARD_ACTION_RE.fullmatch(text):
        return ""
    return text
