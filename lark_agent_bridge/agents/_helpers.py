"""Shared helpers for agent runners."""

from __future__ import annotations

def _extract_chat_answer(payload: dict[str, object]) -> str:
    choices = payload["choices"]
    if not isinstance(choices, list) or not choices:
        raise KeyError("choices")
    first = choices[0]
    if not isinstance(first, dict):
        raise TypeError("choice must be an object")
    message = first.get("message")
    if isinstance(message, dict) and isinstance(message.get("content"), str):
        return message["content"]
    if isinstance(first.get("text"), str):
        return first["text"]
    raise KeyError("choices[0].message.content")


def _default_command_for_provider(provider: str) -> str:
    normalized = (provider or "").strip().casefold()
    if normalized == "codex":
        return "codex"
    if normalized in {"claude", "claude-code", "claude_code"}:
        return "claude"
    return ""


def _normalize_provider_name(provider: str) -> str:
    normalized = (provider or "").strip().casefold()
    if normalized in {"claude", "claude-code", "claude_code"}:
        return "claude"
    if normalized == "codex":
        return "codex"
    return normalized


def _alternate_provider(provider: str) -> str:
    normalized = _normalize_provider_name(provider)
    if normalized == "codex":
        return "claude"
    if normalized == "claude":
        return "codex"
    return ""


def _provider_candidates(provider: str, command_name: str) -> list[tuple[str, str]]:
    primary_provider = _normalize_provider_name(provider)
    primary_command = command_name.strip() or _default_command_for_provider(primary_provider)
    alternate_provider = _alternate_provider(primary_provider)
    alternate_command = _default_command_for_provider(alternate_provider)
    candidates: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for current_provider, current_command in (
        (primary_provider, primary_command),
        (alternate_provider, alternate_command),
    ):
        if not current_provider or not current_command:
            continue
        key = (current_provider, current_command)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(key)
    return candidates
