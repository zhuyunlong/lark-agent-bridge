"""Configuration loading for the bridge."""

from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
from typing import Any
import tomllib

from .models import (
    BugAnalysisOptions,
    BridgeConfig,
    ClaudeAgentOptions,
    DownloadConfig,
    JobRetentionOptions,
    LarkOptions,
    OmlxChatOptions,
)


DEFAULT_SIGNAL_ALIASES = {
    "LD normal": "SIGNAL_X3D_LD_NORMAL_OVER_ALL_DATA",
    "normal over all": "SIGNAL_X3D_LD_NORMAL_OVER_ALL_DATA",
    "LD tile": "SIGNAL_X3D_LD_TILE_OVER_ALL_DATA",
    "tile over all": "SIGNAL_X3D_LD_TILE_OVER_ALL_DATA",
    "SD over all": "SIGNAL_X3D_SD_OVER_ALL_DATA",
}


def load_config(config_path: str | Path | None = None) -> BridgeConfig:
    path = Path(config_path).expanduser() if config_path else None
    base_dir = path.parent if path else Path.cwd()
    default_workspace_root = _default_workspace_root(base_dir)
    default_guideengine_repo = default_workspace_root / "xp/guideengine/.worktrees/os6_xpdev"
    data: dict[str, Any] = {}
    if path:
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")
        with path.open("rb") as fh:
            data = tomllib.load(fh)

    aliases = dict(DEFAULT_SIGNAL_ALIASES)
    aliases.update(_string_dict(data.get("signal_aliases", {}), "signal_aliases"))

    download_data = data.get("download") or {}
    retention_data = data.get("job_retention") or {}
    lark_data = data.get("lark") or {}
    runner_data = data.get("runner") or {}
    claude_data = data.get("claude_agent") or {}
    bug_data = data.get("bug_analysis") or {}
    omlx_data = data.get("omlx_chat") or {}

    return BridgeConfig(
        dry_run=_bool_value(os.environ.get("LARK_AGENT_BRIDGE_DRY_RUN"), bool(data.get("dry_run", True))),
        workspace_root=_resolve_path(
            os.environ.get("LARK_AGENT_BRIDGE_WORKSPACE_ROOT") or data.get("workspace_root", default_workspace_root),
            base_dir,
        ),
        guideengine_repo=_resolve_path(
            os.environ.get("LARK_AGENT_BRIDGE_GUIDEENGINE_REPO")
            or data.get("guideengine_repo", default_guideengine_repo),
            base_dir,
        ),
        data_dir=_resolve_path(data.get("data_dir", "data"), base_dir),
        allowed_chats=_env_string_list("LARK_AGENT_BRIDGE_ALLOWED_CHATS", data.get("allowed_chats", []), "allowed_chats"),
        allowed_users=_env_string_list("LARK_AGENT_BRIDGE_ALLOWED_USERS", data.get("allowed_users", []), "allowed_users"),
        command_prefixes=_string_list(data.get("command_prefixes", ["/signal"]), "command_prefixes"),
        signal_aliases=aliases,
        download=DownloadConfig(
            max_bytes=int(download_data.get("max_bytes", 5 * 1024 * 1024 * 1024)),
            timeout_seconds=int(download_data.get("timeout_seconds", 60)),
        ),
        job_retention=JobRetentionOptions(
            enabled=bool(retention_data.get("enabled", True)),
            max_age_hours=int(retention_data.get("max_age_hours", 6)),
            purge_all_on_listen_start=bool(retention_data.get("purge_all_on_listen_start", True)),
            cleanup_interval_seconds=int(retention_data.get("cleanup_interval_seconds", 60)),
        ),
        lark=LarkOptions(
            reply_in_thread=bool(lark_data.get("reply_in_thread", False)),
            mention_sender_in_group=bool(lark_data.get("mention_sender_in_group", True)),
            bot_open_id=str(
                os.environ.get("LARK_AGENT_BRIDGE_BOT_OPEN_ID") or lark_data.get("bot_open_id", "")
            ),
            bot_name=str(os.environ.get("LARK_AGENT_BRIDGE_BOT_NAME") or lark_data.get("bot_name", "")),
        ),
        claude_agent=ClaudeAgentOptions(
            enabled=bool(claude_data.get("enabled", True)),
            command=str(claude_data.get("command", "claude")),
            trigger_prefixes=_string_list(
                claude_data.get("trigger_prefixes", ["/skill", "/claude"]),
                "claude_agent.trigger_prefixes",
            ),
            working_dir=_optional_path(claude_data.get("working_dir"), base_dir),
            add_dirs=_path_list(claude_data.get("add_dirs", []), base_dir, "claude_agent.add_dirs"),
            allowed_tools=_string_list(
                claude_data.get("allowed_tools", ["Read", "Grep", "Glob", "LS"]),
                "claude_agent.allowed_tools",
            ),
            model=_optional_str(claude_data.get("model"), "claude_agent.model"),
            agent=_optional_str(claude_data.get("agent"), "claude_agent.agent"),
            permission_mode=str(claude_data.get("permission_mode", "dontAsk")),
            timeout_seconds=int(claude_data.get("timeout_seconds", 1800)),
            max_prompt_chars=int(claude_data.get("max_prompt_chars", 12000)),
            upload_result_file=bool(claude_data.get("upload_result_file", True)),
            system_prompt=str(claude_data.get("system_prompt", ClaudeAgentOptions().system_prompt)),
        ),
        bug_analysis=BugAnalysisOptions(
            enabled=bool(bug_data.get("enabled", True)),
            provider=str(bug_data.get("provider", "claude")),
            command=str(bug_data.get("command", "claude")),
            working_dir=_optional_path(bug_data.get("working_dir"), base_dir),
            timeout_seconds=int(bug_data.get("timeout_seconds", 5400)),
            max_prompt_chars=int(bug_data.get("max_prompt_chars", 16000)),
            upload_result_files=bool(bug_data.get("upload_result_files", True)),
            default_prompt=str(bug_data.get("default_prompt", BugAnalysisOptions().default_prompt)),
        ),
        omlx_chat=OmlxChatOptions(
            enabled=bool(omlx_data.get("enabled", True)),
            base_url=str(
                os.environ.get("LARK_AGENT_BRIDGE_OMLX_BASE_URL")
                or omlx_data.get("base_url", "http://127.0.0.1:8000/v1")
            ),
            model=str(
                os.environ.get("LARK_AGENT_BRIDGE_OMLX_MODEL")
                or omlx_data.get("model", "gemma-4-26b-a4b-it-4bit")
            ),
            api_key=str(os.environ.get("LARK_AGENT_BRIDGE_OMLX_API_KEY") or omlx_data.get("api_key", "")),
            timeout_seconds=int(omlx_data.get("timeout_seconds", 120)),
            max_prompt_chars=int(omlx_data.get("max_prompt_chars", 2000)),
            max_tokens=int(omlx_data.get("max_tokens", 1024)),
            temperature=float(omlx_data.get("temperature", 0.3)),
            system_prompt=str(omlx_data.get("system_prompt", OmlxChatOptions().system_prompt)),
        ),
        runner_timeout_seconds=int(runner_data.get("timeout_seconds", 900)),
    )


def with_cli_overrides(config: BridgeConfig, *, dry_run: bool = False) -> BridgeConfig:
    if dry_run:
        return replace(config, dry_run=True)
    return config


def _resolve_path(value: str | Path, base_dir: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path


def _default_workspace_root(base_dir: Path) -> Path:
    if base_dir.name == "lark-agent-bridge" and base_dir.parent.name == "tools":
        return base_dir.parent.parent
    return base_dir


def _bool_value(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Invalid boolean environment value: {value}")


def _string_list(value: Any, field_name: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field_name} must be a list of strings")
    return list(value)


def _env_string_list(env_name: str, default: Any, field_name: str) -> list[str]:
    env_value = os.environ.get(env_name)
    if env_value is None:
        return _string_list(default, field_name)
    return [item.strip() for item in env_value.split(",") if item.strip()]


def _string_dict(value: Any, field_name: str) -> dict[str, str]:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        raise ValueError(f"{field_name} must be a string-to-string table")
    return dict(value)


def _optional_str(value: Any, field_name: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    return value


def _optional_path(value: Any, base_dir: Path) -> Path | None:
    if value is None or value == "":
        return None
    if not isinstance(value, (str, Path)):
        raise ValueError("claude_agent.working_dir must be a path string")
    return _resolve_path(value, base_dir)


def _path_list(value: Any, base_dir: Path, field_name: str) -> list[Path]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field_name} must be a list of path strings")
    return [_resolve_path(item, base_dir) for item in value]
