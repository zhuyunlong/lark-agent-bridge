"""Policy guard for incoming bridge events."""

from __future__ import annotations

from dataclasses import dataclass

from .models import BridgeConfig, LarkEvent


@dataclass(slots=True)
class PolicyDecision:
    allowed: bool
    reason: str = "allowed"


def evaluate_event_policy(config: BridgeConfig, event: LarkEvent) -> PolicyDecision:
    if event.chat_type not in {"group", "p2p"}:
        return PolicyDecision(False, f"unsupported_chat_type:{event.chat_type or 'unknown'}")
    if event.chat_type == "p2p":
        return PolicyDecision(True, "p2p_allowed")
    if config.allowed_users and event.sender_id in config.allowed_users:
        return PolicyDecision(True, "allowed_user_bypass")
    if config.allowed_chats and event.chat_id not in config.allowed_chats:
        return PolicyDecision(False, "chat_not_allowed")
    return PolicyDecision(True)


def build_policy_rejection_message(decision: PolicyDecision) -> str:
    if decision.reason == "chat_not_allowed":
        return "当前群未加入允许列表，暂不支持在这个群里使用。你可以改用已放行的群，或直接私聊我。"
    if decision.reason.startswith("unsupported_chat_type:"):
        return "当前只支持群聊和私聊消息。"
    return f"当前请求未通过策略校验：{decision.reason}"
