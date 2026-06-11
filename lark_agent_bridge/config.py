"""Configuration loading for the bridge (facade over configuration/)."""

from __future__ import annotations

from .configuration import DEFAULT_SIGNAL_ALIASES, load_config, with_cli_overrides
from .configuration.coercion import (
    _bool_like,
    _bool_value,
    _env_string_list,
    _optional_path,
    _optional_str,
    _path_list,
    _resolve_path,
    _string_dict,
    _string_list,
)
from .configuration.paths import (
    TEMP_PATH_MARKERS,
    _default_repo_roots,
    _default_workspace_root,
    _is_temp_path,
    _is_under_tool_dir,
    _warn_suspicious_paths,
)
from .configuration.presets import (
    _apply_ai_preset,
    _default_agent_command_for_provider,
    _default_agent_provider_for_api_format,
    _load_provider_presets,
)

__all__ = [
    "DEFAULT_SIGNAL_ALIASES",
    "TEMP_PATH_MARKERS",
    "load_config",
    "with_cli_overrides",
]
