"""Profile registry loaded from the centralized TOML config directory."""

from __future__ import annotations

import argparse
from pathlib import Path
import shlex
import sys
from typing import Any
import tomllib


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = PROJECT_ROOT / "config"
PROFILE_REGISTRY_PATH = CONFIG_DIR / "presets.toml"
RESERVED_SECTIONS = {"defaults"}


def load_profile_registry(path: str | Path | None = None) -> dict[str, Any]:
    target = Path(path) if path else PROFILE_REGISTRY_PATH
    with target.open("rb") as fh:
        return tomllib.load(fh)


def load_profile_specs(path: str | Path | None = None) -> dict[str, dict[str, Any]]:
    data = load_profile_registry(path)
    profiles: dict[str, dict[str, Any]] = {}
    for name, values in data.items():
        if name in RESERVED_SECTIONS:
            continue
        if isinstance(values, dict):
            spec = dict(values)
            _validate_profile(name, spec)
            profiles[name] = spec
    return profiles


def default_profile(path: str | Path | None = None) -> str:
    data = load_profile_registry(path)
    defaults = data.get("defaults", {})
    if not isinstance(defaults, dict):
        raise ValueError("presets.toml [defaults] must be a table")
    profile = str(defaults.get("profile", "")).strip()
    if not profile:
        raise ValueError("presets.toml [defaults].profile is required")
    return profile


def resolve_profile(name: str | None, path: str | Path | None = None) -> tuple[str, dict[str, Any]]:
    requested = (name or "").strip()
    profile_name = default_profile(path) if requested in {"", "default"} else requested
    profiles = load_profile_specs(path)
    if profile_name not in profiles:
        supported = ", ".join(profiles)
        raise ValueError(f"Unsupported profile: {profile_name}. Supported profiles: {supported}")
    return profile_name, profiles[profile_name]


def shell_exports(profile_name: str | None, path: str | Path | None = None) -> str:
    resolved_name, spec = resolve_profile(profile_name, path)
    ai_enabled = _bool_field(spec, "ai_enabled", True)
    lines = [
        f"PROFILE={shlex.quote(resolved_name)}",
        f"PROFILE_TYPE={shlex.quote(str(spec.get('type', '')))}",
        f"PROFILE_REQUIRES_API_KEY={shlex.quote(str(_bool_field(spec, 'requires_api_key', False)).lower())}",
        f"PROFILE_PRECONDITION={shlex.quote(str(spec.get('precondition', '')))}",
        f"export LARK_AGENT_BRIDGE_AI_ENABLED={shlex.quote(str(ai_enabled).lower())}",
        f"export LARK_AGENT_BRIDGE_AGENT_PROVIDER={shlex.quote(str(spec.get('agent_provider', '')))}",
        f"export LARK_AGENT_BRIDGE_AGENT_COMMAND={shlex.quote(str(spec.get('agent_command', '')))}",
    ]
    if ai_enabled:
        lines.append(f"export LARK_AGENT_BRIDGE_AI_PRESET={shlex.quote(resolved_name)}")
    else:
        lines.append("unset LARK_AGENT_BRIDGE_AI_PRESET")
    return "\n".join(lines)


def _validate_profile(name: str, spec: dict[str, Any]) -> None:
    api_format = str(spec.get("api_format", "")).strip().casefold()
    agent_provider = str(spec.get("agent_provider", "")).strip().casefold()
    if api_format == "openai" and agent_provider != "codex":
        raise ValueError(f"profile {name} uses openai api_format but agent_provider is not codex")
    if api_format == "anthropic" and agent_provider != "claude":
        raise ValueError(f"profile {name} uses anthropic api_format but agent_provider is not claude")


def _bool_field(spec: dict[str, Any], key: str, default: bool) -> bool:
    value = spec.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return bool(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m lark_agent_bridge.profile_registry")
    parser.add_argument("profile", nargs="?", default="default")
    parser.add_argument("--shell", action="store_true", help="Print shell assignments for run.sh.")
    parser.add_argument("--list", action="store_true", help="List supported profiles.")
    parser.add_argument("--path", help="Override registry path.")
    args = parser.parse_args(argv)
    try:
        if args.list:
            for name in load_profile_specs(args.path):
                print(name)
            return 0
        if args.shell:
            print(shell_exports(args.profile, args.path))
            return 0
        name, spec = resolve_profile(args.profile, args.path)
        print({"name": name, **spec})
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
