"""配置装配主干：TOML + 环境变量 → BridgeConfig。"""

from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
from typing import Any
import tomllib

from ..models import BridgeConfig
from .coercion import (
    _bool_value,
    _env_string_list,
    _resolve_path,
    _string_dict,
    _string_list,
)
from .paths import _default_repo_roots, _default_workspace_root, _warn_suspicious_paths
from .presets import _load_provider_presets
from . import sections

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
    default_napa5_repo = default_workspace_root / "xp/Napa5"
    data: dict[str, Any] = {}
    if path:
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")
        with path.open("rb") as fh:
            data = tomllib.load(fh)

    aliases = dict(DEFAULT_SIGNAL_ALIASES)
    aliases.update(_string_dict(data.get("signal_aliases", {}), "signal_aliases"))

    bug_data = data.get("bug_analysis") or {}
    intent_data = data.get("intent_analysis") or {}
    source_investigation_data = data.get("source_investigation") or {}
    user_presets_data = data.get("provider_presets") or {}
    provider_presets = _load_provider_presets(
        user_presets_data if isinstance(user_presets_data, dict) else None
    )
    ai_provider = sections._ai_provider_options(data.get("ai_provider") or {}, provider_presets)
    bindings = sections.AgentBindings(ai_provider, bug_data, intent_data, source_investigation_data)
    guideengine_repo = _resolve_path(
        os.environ.get("LARK_AGENT_BRIDGE_GUIDEENGINE_REPO")
        or data.get("guideengine_repo", default_guideengine_repo),
        base_dir,
    )

    config = BridgeConfig(
        dry_run=_bool_value(os.environ.get("LARK_AGENT_BRIDGE_DRY_RUN"), bool(data.get("dry_run", True))),
        workspace_root=_resolve_path(
            os.environ.get("LARK_AGENT_BRIDGE_WORKSPACE_ROOT")
            or data.get("workspace_root", default_workspace_root),
            base_dir,
        ),
        guideengine_repo=guideengine_repo,
        data_dir=_resolve_path(data.get("data_dir", "data"), base_dir),
        allowed_chats=_env_string_list(
            "LARK_AGENT_BRIDGE_ALLOWED_CHATS", data.get("allowed_chats", []), "allowed_chats"
        ),
        allowed_users=_env_string_list(
            "LARK_AGENT_BRIDGE_ALLOWED_USERS", data.get("allowed_users", []), "allowed_users"
        ),
        command_prefixes=_string_list(data.get("command_prefixes", []), "command_prefixes"),
        signal_aliases=aliases,
        bug_url_domains=_string_list(data.get("bug_url_domains", []), "bug_url_domains"),
        download=sections._download_config(data.get("download") or {}),
        local_resources=sections._local_resource_options(data.get("local_resources") or {}, base_dir),
        job_retention=sections._job_retention_options(data.get("job_retention") or {}),
        event_consumer=sections._event_consumer_options(data.get("event_consumer") or {}),
        lark=sections._lark_options(data.get("lark") or {}),
        internal_network_env=sections._internal_network_env_options(
            data.get("internal_network_env") or {}
        ),
        claude_agent=sections._claude_agent_options(data.get("claude_agent") or {}, base_dir),
        bug_analysis=sections._bug_analysis_options(bug_data, base_dir, bindings),
        intent_analysis=sections._intent_analysis_options(intent_data, base_dir, bindings),
        omlx_chat=sections._omlx_chat_options(data.get("omlx_chat") or {}),
        report_server=sections._report_server_options(data.get("report_server") or {}),
        approval=sections._approval_options(data.get("approval") or {}),
        codex_app_server=sections._codex_app_server_options(data.get("codex_app_server") or {}),
        workflow_archive=sections._workflow_archive_options(data.get("workflow_archive") or {}),
        notifications=sections._notification_options(data.get("notifications") or {}),
        dual_agent=sections._dual_agent_options(data.get("dual_agent") or {}),
        knowledge=sections._knowledge_options(data.get("knowledge") or {}, base_dir),
        source_investigation=sections._source_investigation_options(
            source_investigation_data,
            base_dir,
            bindings,
            _default_repo_roots(default_guideengine_repo, default_napa5_repo),
        ),
        signal_resolver=sections._signal_resolver_options(data),
        health=sections._health_options(data.get("health") or {}),
        auth=sections._auth_options(data.get("auth") or {}),
        state=sections._state_options(data.get("state") or {}),
        ai_provider=ai_provider,
        requirement_analysis=sections._requirement_analysis_options(
            data.get("requirement_analysis") or {}
        ),
        runner_timeout_seconds=int((data.get("runner") or {}).get("timeout_seconds", 900)),
    )
    _warn_suspicious_paths(config)
    return config


def with_cli_overrides(config: BridgeConfig, *, dry_run: bool = False) -> BridgeConfig:
    if dry_run:
        return replace(config, dry_run=True)
    return config
