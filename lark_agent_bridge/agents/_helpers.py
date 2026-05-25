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


def _provider_candidates(provider: str, command_name: str) -> list[tuple[str, str]]:
    primary_provider = _normalize_provider_name(provider)
    primary_command = command_name.strip() or _default_command_for_provider(primary_provider)
    candidates: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    if primary_provider and primary_command:
        key = (primary_provider, primary_command)
        if key not in seen:
            seen.add(key)
            candidates.append(key)
    return candidates
