"""AI provider 预设：内置 presets.toml + 用户覆盖 + secrets 合并与应用。"""

from __future__ import annotations

from dataclasses import replace
import tomllib
from typing import Any

from ..models import AIProviderOptions
from ..profile_registry import PROFILE_REGISTRY_PATH, load_profile_specs
from .coercion import _bool_like

_BUILTIN_PRESETS_PATH = PROFILE_REGISTRY_PATH


def _load_provider_presets(user_presets: dict[str, dict[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
    """Load built-in presets from presets.toml, then merge user overrides and secrets."""
    presets: dict[str, dict[str, Any]] = {}
    if _BUILTIN_PRESETS_PATH.exists():
        presets.update(load_profile_specs(_BUILTIN_PRESETS_PATH))
    if user_presets:
        for name, values in user_presets.items():
            if isinstance(values, dict):
                merged = dict(presets.get(name, {}))
                merged.update(dict(values))
                presets[name] = merged
    secrets_path = _BUILTIN_PRESETS_PATH.parent / "secrets.toml"
    if secrets_path.exists():
        with secrets_path.open("rb") as fh:
            secrets = tomllib.load(fh)
        for name, secret in secrets.items():
            if name in presets and isinstance(secret, dict) and secret.get("api_key"):
                presets[name]["api_key"] = secret["api_key"]
    return presets


def _apply_ai_preset(
    opts: AIProviderOptions,
    presets: dict[str, dict[str, Any]] | None = None,
    apply_enabled: bool = True,
) -> AIProviderOptions:
    """Fill missing fields from a named preset. User-set values take precedence."""
    preset_name = opts.preset.strip().lower()
    if not preset_name:
        return opts
    effective_presets = presets if presets is not None else _load_provider_presets()
    preset = effective_presets.get(preset_name)
    if not preset:
        return opts
    overrides: dict[str, Any] = {}
    if not opts.base_url:
        overrides["base_url"] = preset.get("base_url", "")
    if not opts.primary_model:
        overrides["primary_model"] = preset.get("primary_model", "")
    if not opts.fast_model and preset.get("fast_model"):
        overrides["fast_model"] = preset["fast_model"]
    if not opts.fallback_model and preset.get("fallback_model"):
        overrides["fallback_model"] = preset["fallback_model"]
    if not opts.api_format and preset.get("api_format"):
        overrides["api_format"] = preset["api_format"]
    if not opts.api_key and preset.get("api_key"):
        overrides["api_key"] = preset["api_key"]
    if apply_enabled and "ai_enabled" in preset:
        overrides["enabled"] = _bool_like(preset.get("ai_enabled"), opts.enabled)
    if not opts.profile_type and preset.get("type"):
        overrides["profile_type"] = str(preset.get("type", ""))
    if not opts.agent_provider and preset.get("agent_provider"):
        overrides["agent_provider"] = str(preset.get("agent_provider", ""))
    if not opts.agent_command and preset.get("agent_command"):
        overrides["agent_command"] = str(preset.get("agent_command", ""))
    if preset.get("requires_api_key") is not None:
        overrides["requires_api_key"] = _bool_like(preset.get("requires_api_key"), opts.requires_api_key)
    if not opts.precondition and preset.get("precondition"):
        overrides["precondition"] = str(preset.get("precondition", ""))
    if not overrides:
        return opts
    return replace(opts, **overrides)


def _default_agent_provider_for_api_format(api_format: str) -> str:
    normalized = api_format.strip().casefold()
    if normalized == "openai":
        return "codex"
    if normalized == "anthropic":
        return "claude"
    return ""


def _default_agent_command_for_provider(provider: str) -> str:
    normalized = provider.strip().casefold()
    if normalized == "codex":
        return "codex"
    if normalized in {"claude", "claude-code", "claude_code"}:
        return "claude"
    return ""
