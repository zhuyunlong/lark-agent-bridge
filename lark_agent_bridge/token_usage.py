from __future__ import annotations

from collections.abc import Mapping


TOKEN_USAGE_ALIASES: dict[str, tuple[str, ...]] = {
    "input_tokens": (
        "input_tokens",
        "inputTokens",
        "prompt_tokens",
        "promptTokens",
        "request_tokens",
        "requestTokens",
    ),
    "cached_input_tokens": (
        "cached_input_tokens",
        "cachedInputTokens",
        "cached_prompt_tokens",
        "cachedPromptTokens",
        "cached_request_tokens",
        "cachedRequestTokens",
    ),
    "output_tokens": (
        "output_tokens",
        "outputTokens",
        "completion_tokens",
        "completionTokens",
        "response_tokens",
        "responseTokens",
    ),
    "total_tokens": ("total_tokens", "totalTokens"),
}


def normalize_token_usage(value: Mapping[str, object] | None) -> dict[str, int]:
    if not isinstance(value, Mapping):
        return {}
    usage: dict[str, int] = {}
    for target_key, aliases in TOKEN_USAGE_ALIASES.items():
        for alias in aliases:
            parsed = coerce_token_count(value.get(alias))
            if parsed is not None:
                usage[target_key] = parsed
                break
    if "total_tokens" not in usage and {"input_tokens", "output_tokens"}.issubset(usage):
        usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
    return usage


def extract_prefixed_token_usage(value: Mapping[str, object] | None, prefix: str) -> dict[str, int]:
    if not isinstance(value, Mapping):
        return {}
    usage: dict[str, int] = {}
    for target_key, aliases in TOKEN_USAGE_ALIASES.items():
        for alias in aliases:
            parsed = coerce_token_count(value.get(f"{prefix}{alias}"))
            if parsed is not None:
                usage[target_key] = parsed
                break
    if "total_tokens" not in usage and {"input_tokens", "output_tokens"}.issubset(usage):
        usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
    return usage


def extract_first_prefixed_token_usage(value: Mapping[str, object] | None, prefixes: tuple[str, ...] | list[str]) -> tuple[str, dict[str, int]]:
    if not isinstance(value, Mapping):
        return "", {}
    for prefix in prefixes:
        normalized = str(prefix or "")
        if not normalized:
            continue
        usage = extract_prefixed_token_usage(value, normalized)
        if usage:
            return normalized, usage
    return "", {}


def coerce_token_count(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            return int(stripped)
    return None
