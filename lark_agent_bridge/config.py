"""Configuration loading for the bridge."""

from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
from typing import Any
import tomllib

from .models import (
    AIProviderOptions,
    BugAnalysisOptions,
    BridgeConfig,
    ApprovalOptions,
    ClaudeAgentOptions,
    DownloadConfig,
    DualAgentOptions,
    EventConsumerOptions,
    InternalNetworkEnvOptions,
    IntentAnalysisOptions,
    JobRetentionOptions,
    KnowledgeOptions,
    KnowledgeSourceOptions,
    LarkOptions,
    LocalResourceOptions,
    NotificationOptions,
    OmlxChatOptions,
    ReportServerOptions,
    SignalResolverOptions,
    SourceInvestigationOptions,
    WorkflowArchiveOptions,
)
from .profile_registry import PROFILE_REGISTRY_PATH, load_profile_specs

try:
    from .log import get_logger
except Exception:  # pragma: no cover - logging is optional at config load time
    class _FallbackLogger:
        def warning(self, *_args: Any, **_kwargs: Any) -> None: ...
        def info(self, *_args: Any, **_kwargs: Any) -> None: ...
        def debug(self, *_args: Any, **_kwargs: Any) -> None: ...

    def get_logger(_name: str):  # type: ignore[override]
        return _FallbackLogger()

_logger = get_logger("config")


TEMP_PATH_MARKERS = ("/var/folders/", "/tmp/", "/private/var/folders/", "/private/tmp/")


def _is_under_tool_dir(path: Path) -> bool:
    """True when ``path`` is (or lives inside) the lark-agent-bridge tool itself."""
    try:
        resolved = path.resolve()
    except OSError:
        resolved = Path(path).expanduser()
    return resolved.name == "lark-agent-bridge" and resolved.parent.name == "tools"


def _is_temp_path(path: Path) -> bool:
    try:
        text = str(path.resolve())
    except OSError:
        text = str(path)
    return any(marker in text for marker in TEMP_PATH_MARKERS)


def _warn_suspicious_paths(config: "BridgeConfig") -> None:
    """Emit warnings when workspace_root / guideengine_repo look misconfigured.

    Targets the failure mode where subprocess prompts receive either a sandbox
    tempdir as workspace_root or the tool's own directory as guideengine_repo.
    """
    workspace = config.workspace_root
    repo = config.guideengine_repo

    if _is_temp_path(workspace):
        _logger.warning(
            "workspace_root=%s is a sandbox/temp path; subprocess skill scripts will not be found. "
            "Set LARK_AGENT_BRIDGE_WORKSPACE_ROOT or workspace_root in config.toml.",
            workspace,
        )
    elif _is_under_tool_dir(workspace):
        _logger.warning(
            "workspace_root=%s points at the lark-agent-bridge tool itself; set an explicit "
            "LARK_AGENT_BRIDGE_WORKSPACE_ROOT or workspace_root in config.toml.",
            workspace,
        )

    if _is_under_tool_dir(repo):
        _logger.warning(
            "guideengine_repo=%s points at the lark-agent-bridge tool itself; "
            "set LARK_AGENT_BRIDGE_GUIDEENGINE_REPO or guideengine_repo in config.toml.",
            repo,
        )
    elif _is_temp_path(repo):
        _logger.warning(
            "guideengine_repo=%s is a sandbox/temp path; source analysis will see no code. "
            "Set LARK_AGENT_BRIDGE_GUIDEENGINE_REPO or guideengine_repo in config.toml.",
            repo,
        )
    elif not repo.exists():
        _logger.warning(
            "guideengine_repo=%s does not exist; source investigation will fail. "
            "Set LARK_AGENT_BRIDGE_GUIDEENGINE_REPO or guideengine_repo in config.toml.",
            repo,
        )
    elif repo == workspace:
        _logger.warning(
            "guideengine_repo=%s equals workspace_root; source analysis needs an explicit code repo.",
            repo,
        )


DEFAULT_SIGNAL_ALIASES = {
    "LD normal": "SIGNAL_X3D_LD_NORMAL_OVER_ALL_DATA",
    "normal over all": "SIGNAL_X3D_LD_NORMAL_OVER_ALL_DATA",
    "LD tile": "SIGNAL_X3D_LD_TILE_OVER_ALL_DATA",
    "tile over all": "SIGNAL_X3D_LD_TILE_OVER_ALL_DATA",
    "SD over all": "SIGNAL_X3D_SD_OVER_ALL_DATA",
}

# ---------------------------------------------------------------------------
# Built-in AI provider presets
# ---------------------------------------------------------------------------
# Setting ``preset = "..."`` in [ai_provider] auto-fills base_url, model names
# and api_format.  You only need to supply ``api_key`` (or set the env var
# LARK_AGENT_BRIDGE_AI_API_KEY).  Individual fields override the preset.
#
# Built-in presets are loaded from config/presets.toml.
# User-defined [provider_presets.<name>] sections in config.toml are merged
# on top, allowing updates without touching source code.
# ---------------------------------------------------------------------------

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

    download_data = data.get("download") or {}
    local_resource_data = data.get("local_resources") or {}
    retention_data = data.get("job_retention") or {}
    event_consumer_data = data.get("event_consumer") or {}
    lark_data = data.get("lark") or {}
    internal_network_env_data = data.get("internal_network_env") or {}
    runner_data = data.get("runner") or {}
    claude_data = data.get("claude_agent") or {}
    bug_data = data.get("bug_analysis") or {}
    source_investigation_data = data.get("source_investigation") or {}
    intent_data = data.get("intent_analysis") or {}
    omlx_data = data.get("omlx_chat") or {}
    report_server_data = data.get("report_server") or {}
    approval_data = data.get("approval") or {}
    workflow_archive_data = data.get("workflow_archive") or {}
    notifications_data = data.get("notifications") or {}
    dual_agent_data = data.get("dual_agent") or {}
    knowledge_data = data.get("knowledge") or {}
    ai_provider_data = data.get("ai_provider") or {}
    user_presets_data = data.get("provider_presets") or {}
    provider_presets = _load_provider_presets(user_presets_data if isinstance(user_presets_data, dict) else None)
    agent_provider_override = str(os.environ.get("LARK_AGENT_BRIDGE_AGENT_PROVIDER") or "").strip()
    agent_command_override = str(os.environ.get("LARK_AGENT_BRIDGE_AGENT_COMMAND") or "").strip()
    ai_enabled_env = os.environ.get("LARK_AGENT_BRIDGE_AI_ENABLED")
    ai_provider = _apply_ai_preset(
        AIProviderOptions(
            enabled=_bool_value(
                ai_enabled_env,
                bool(ai_provider_data.get("enabled", False)),
            ),
            preset=str(
                os.environ.get("LARK_AGENT_BRIDGE_AI_PRESET")
                or ai_provider_data.get("preset", "")
            ),
            api_format=str(ai_provider_data.get("api_format", "")),
            primary_model=str(
                os.environ.get("LARK_AGENT_BRIDGE_AI_PRIMARY_MODEL")
                or ai_provider_data.get("primary_model", "")
            ),
            fallback_model=str(ai_provider_data.get("fallback_model", "")),
            fast_model=str(ai_provider_data.get("fast_model", "")),
            base_url=str(
                os.environ.get("LARK_AGENT_BRIDGE_AI_BASE_URL")
                or ai_provider_data.get("base_url", "")
            ),
            api_key=str(
                os.environ.get("LARK_AGENT_BRIDGE_AI_API_KEY")
                or ai_provider_data.get("api_key", "")
            ),
            fallback_base_url=str(
                os.environ.get("LARK_AGENT_BRIDGE_AI_FALLBACK_BASE_URL")
                or ai_provider_data.get("fallback_base_url", "")
            ),
            fallback_api_key=str(
                os.environ.get("LARK_AGENT_BRIDGE_AI_FALLBACK_API_KEY")
                or ai_provider_data.get("fallback_api_key", "")
            ),
            intent_temperature=float(ai_provider_data.get("intent_temperature", 0.0)),
            intent_max_tokens=int(ai_provider_data.get("intent_max_tokens", 1024)),
            intent_timeout_seconds=float(ai_provider_data.get("intent_timeout_seconds", 30)),
            intent_max_retries=int(ai_provider_data.get("intent_max_retries", 2)),
            summary_temperature=float(ai_provider_data.get("summary_temperature", 0.3)),
            summary_max_tokens=int(ai_provider_data.get("summary_max_tokens", 4096)),
            summary_timeout_seconds=float(ai_provider_data.get("summary_timeout_seconds", 120)),
        ),
        presets=provider_presets,
        apply_enabled=ai_enabled_env is None,
    )
    bug_provider = (agent_provider_override or str(bug_data.get("provider", ""))).strip()
    if not bug_provider:
        bug_provider = (
            ai_provider.agent_provider
            or _default_agent_provider_for_api_format(ai_provider.api_format)
            or BugAnalysisOptions().provider
        )
    bug_command = (agent_command_override or str(bug_data.get("command", ""))).strip()
    if not bug_command:
        bug_command = ai_provider.agent_command or _default_agent_command_for_provider(bug_provider) or BugAnalysisOptions().command
    intent_provider = (agent_provider_override or str(intent_data.get("provider", ""))).strip()
    if not intent_provider:
        intent_provider = bug_provider
    intent_command = (agent_command_override or str(intent_data.get("command", ""))).strip()
    if not intent_command:
        intent_command = _default_agent_command_for_provider(intent_provider) or bug_command
    source_provider = (agent_provider_override or str(source_investigation_data.get("provider", ""))).strip()
    if not source_provider:
        source_provider = (
            ai_provider.agent_provider
            or _default_agent_provider_for_api_format(ai_provider.api_format)
            or SourceInvestigationOptions().provider
        )
    source_command = (agent_command_override or str(source_investigation_data.get("command", ""))).strip()
    if not source_command:
        source_command = (
            ai_provider.agent_command
            or _default_agent_command_for_provider(source_provider)
            or SourceInvestigationOptions().command
        )
    guideengine_repo = _resolve_path(
        os.environ.get("LARK_AGENT_BRIDGE_GUIDEENGINE_REPO")
        or data.get("guideengine_repo", default_guideengine_repo),
        base_dir,
    )

    config = BridgeConfig(
        dry_run=_bool_value(os.environ.get("LARK_AGENT_BRIDGE_DRY_RUN"), bool(data.get("dry_run", True))),
        workspace_root=_resolve_path(
            os.environ.get("LARK_AGENT_BRIDGE_WORKSPACE_ROOT") or data.get("workspace_root", default_workspace_root),
            base_dir,
        ),
        guideengine_repo=guideengine_repo,
        data_dir=_resolve_path(data.get("data_dir", "data"), base_dir),
        allowed_chats=_env_string_list("LARK_AGENT_BRIDGE_ALLOWED_CHATS", data.get("allowed_chats", []), "allowed_chats"),
        allowed_users=_env_string_list("LARK_AGENT_BRIDGE_ALLOWED_USERS", data.get("allowed_users", []), "allowed_users"),
        command_prefixes=_string_list(data.get("command_prefixes", []), "command_prefixes"),
        signal_aliases=aliases,
        bug_url_domains=_string_list(data.get("bug_url_domains", []), "bug_url_domains"),
        download=DownloadConfig(
            max_bytes=int(download_data.get("max_bytes", 5 * 1024 * 1024 * 1024)),
            timeout_seconds=int(download_data.get("timeout_seconds", 60)),
            allow_private_urls=bool(download_data.get("allow_private_urls", True)),
        ),
        local_resources=LocalResourceOptions(
            enabled=bool(local_resource_data.get("enabled", LocalResourceOptions().enabled)),
            require_allowed_user=bool(
                local_resource_data.get("require_allowed_user", LocalResourceOptions().require_allowed_user)
            ),
            allowed_dirs=_path_list(
                local_resource_data.get(
                    "allowed_dirs",
                    [str(path) for path in LocalResourceOptions().allowed_dirs],
                ),
                base_dir,
                "local_resources.allowed_dirs",
            ),
        ),
        job_retention=JobRetentionOptions(
            enabled=bool(retention_data.get("enabled", True)),
            max_age_hours=int(retention_data.get("max_age_hours", 6)),
            bug_cache_max_age_hours=int(retention_data.get("bug_cache_max_age_hours", 24)),
            purge_all_on_listen_start=bool(retention_data.get("purge_all_on_listen_start", False)),
            cleanup_interval_seconds=int(retention_data.get("cleanup_interval_seconds", 60)),
        ),
        event_consumer=EventConsumerOptions(
            event_key=str(event_consumer_data.get("event_key", EventConsumerOptions().event_key)),
            ready_timeout_seconds=float(
                event_consumer_data.get("ready_timeout_seconds", EventConsumerOptions().ready_timeout_seconds)
            ),
            restart_on_failure=bool(
                event_consumer_data.get("restart_on_failure", EventConsumerOptions().restart_on_failure)
            ),
            max_restarts=int(event_consumer_data.get("max_restarts", EventConsumerOptions().max_restarts)),
            restart_initial_delay_seconds=float(
                event_consumer_data.get(
                    "restart_initial_delay_seconds",
                    EventConsumerOptions().restart_initial_delay_seconds,
                )
            ),
            restart_max_delay_seconds=float(
                event_consumer_data.get(
                    "restart_max_delay_seconds",
                    EventConsumerOptions().restart_max_delay_seconds,
                )
            ),
            drop_stale_light_interactions=bool(
                event_consumer_data.get(
                    "drop_stale_light_interactions",
                    EventConsumerOptions().drop_stale_light_interactions,
                )
            ),
            stale_light_interaction_grace_seconds=float(
                event_consumer_data.get(
                    "stale_light_interaction_grace_seconds",
                    EventConsumerOptions().stale_light_interaction_grace_seconds,
                )
            ),
        ),
        lark=LarkOptions(
            reply_in_thread=bool(lark_data.get("reply_in_thread", False)),
            mention_sender_in_group=bool(lark_data.get("mention_sender_in_group", True)),
            bot_open_id=str(
                os.environ.get("LARK_AGENT_BRIDGE_BOT_OPEN_ID") or lark_data.get("bot_open_id", "")
            ),
            bot_name=str(os.environ.get("LARK_AGENT_BRIDGE_BOT_NAME") or lark_data.get("bot_name", "")),
        ),
        internal_network_env=InternalNetworkEnvOptions(
            inherit_env=_string_list(
                internal_network_env_data.get("inherit_env", []),
                "internal_network_env.inherit_env",
            ),
            unset_env=_string_list(
                internal_network_env_data.get("unset_env", []),
                "internal_network_env.unset_env",
            ),
        ),
        claude_agent=ClaudeAgentOptions(
            enabled=bool(claude_data.get("enabled", True)),
            command=str(claude_data.get("command", "claude")),
            trigger_prefixes=_string_list(
                claude_data.get("trigger_prefixes", []),
                "claude_agent.trigger_prefixes",
            ),
            working_dir=_optional_path(claude_data.get("working_dir"), base_dir, "claude_agent.working_dir"),
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
            provider=bug_provider,
            command=bug_command,
            model=str(bug_data.get("model", BugAnalysisOptions().model)),
            working_dir=_optional_path(bug_data.get("working_dir"), base_dir, "bug_analysis.working_dir"),
            timeout_seconds=int(bug_data.get("timeout_seconds", 5400)),
            agent_summary_timeout_seconds=int(
                bug_data.get(
                    "agent_summary_timeout_seconds",
                    BugAnalysisOptions().agent_summary_timeout_seconds,
                )
            ),
            file_agent_debug_logs=bool(
                bug_data.get("file_agent_debug_logs", BugAnalysisOptions().file_agent_debug_logs)
            ),
            max_prompt_chars=int(bug_data.get("max_prompt_chars", 16000)),
            upload_result_files=bool(bug_data.get("upload_result_files", True)),
            default_prompt=str(bug_data.get("default_prompt", BugAnalysisOptions().default_prompt)),
            resume_followup_sessions=bool(
                bug_data.get("resume_followup_sessions", BugAnalysisOptions().resume_followup_sessions)
            ),
            auto_fallback_to_file_agent=bool(
                bug_data.get("auto_fallback_to_file_agent", BugAnalysisOptions().auto_fallback_to_file_agent)
            ),
            force_reanalysis_terms=_string_list(
                bug_data.get("force_reanalysis_terms", BugAnalysisOptions().force_reanalysis_terms),
                "bug_analysis.force_reanalysis_terms",
            ),
        ),
        intent_analysis=IntentAnalysisOptions(
            enabled=bool(intent_data.get("enabled", False)),
            provider=intent_provider,
            command=intent_command,
            model=str(intent_data.get("model", IntentAnalysisOptions().model)),
            working_dir=_optional_path(intent_data.get("working_dir"), base_dir, "intent_analysis.working_dir"),
            timeout_seconds=int(intent_data.get("timeout_seconds", 180)),
            max_prompt_chars=int(intent_data.get("max_prompt_chars", IntentAnalysisOptions().max_prompt_chars)),
            allow_subprocess_fallback=bool(
                intent_data.get(
                    "allow_subprocess_fallback",
                    IntentAnalysisOptions().allow_subprocess_fallback,
                )
            ),
            system_prompt=str(intent_data.get("system_prompt", IntentAnalysisOptions().system_prompt)),
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
            api_key=str(
                os.environ.get("LARK_AGENT_BRIDGE_OMLX_API_KEY")
                or omlx_data.get("api_key")
                or OmlxChatOptions().api_key
            ),
            timeout_seconds=int(omlx_data.get("timeout_seconds", 120)),
            max_prompt_chars=int(omlx_data.get("max_prompt_chars", 2000)),
            max_tokens=int(omlx_data.get("max_tokens", 1024)),
            temperature=float(omlx_data.get("temperature", 0.3)),
            system_prompt=str(omlx_data.get("system_prompt", OmlxChatOptions().system_prompt)),
            followup_max_context_chars=int(
                omlx_data.get("followup_max_context_chars", OmlxChatOptions().followup_max_context_chars)
            ),
            followup_max_history_turns=int(
                omlx_data.get("followup_max_history_turns", OmlxChatOptions().followup_max_history_turns)
            ),
            followup_system_prompt=str(
                omlx_data.get("followup_system_prompt", OmlxChatOptions().followup_system_prompt)
            ),
        ),
        report_server=ReportServerOptions(
            enabled=bool(report_server_data.get("enabled", True)),
            bind_host=str(report_server_data.get("bind_host", "127.0.0.1")),
            port=int(report_server_data.get("port", 8765)),
            public_base_url=str(
                os.environ.get("LARK_AGENT_BRIDGE_REPORT_PUBLIC_BASE_URL")
                or report_server_data.get("public_base_url", ReportServerOptions().public_base_url)
            ),
            admin_token=str(
                os.environ.get("LARK_AGENT_BRIDGE_ADMIN_TOKEN")
                or report_server_data.get("admin_token", "")
            ),
        ),
        approval=ApprovalOptions(
            enabled=bool(approval_data.get("enabled", ApprovalOptions().enabled)),
        ),
        workflow_archive=WorkflowArchiveOptions(
            enabled=bool(workflow_archive_data.get("enabled", WorkflowArchiveOptions().enabled)),
            base_token=str(workflow_archive_data.get("base_token", "")),
            table_id=str(workflow_archive_data.get("table_id", "")),
            drive_folder_token=str(workflow_archive_data.get("drive_folder_token", "")),
            doc_parent_token=str(workflow_archive_data.get("doc_parent_token", "")),
            base_field_map=_string_dict(
                workflow_archive_data.get("base_field_map", WorkflowArchiveOptions().base_field_map),
                "workflow_archive.base_field_map",
            ),
        ),
        notifications=NotificationOptions(
            enabled=bool(notifications_data.get("enabled", NotificationOptions().enabled)),
            report_ready=bool(notifications_data.get("report_ready", NotificationOptions().report_ready)),
        ),
        dual_agent=DualAgentOptions(
            enabled=bool(dual_agent_data.get("enabled", DualAgentOptions().enabled)),
        ),
        knowledge=KnowledgeOptions(
            enabled=bool(knowledge_data.get("enabled", KnowledgeOptions().enabled)),
            storage=_resolve_path(
                knowledge_data.get("storage", KnowledgeOptions().storage),
                base_dir,
            ),
            max_hits=int(knowledge_data.get("max_hits", KnowledgeOptions().max_hits)),
            trigger_prefixes=_string_list(
                knowledge_data.get("trigger_prefixes", KnowledgeOptions().trigger_prefixes),
                "knowledge.trigger_prefixes",
            ),
            answer_provider=str(knowledge_data.get("answer_provider", KnowledgeOptions().answer_provider)),
            auto_probe_enabled=bool(
                knowledge_data.get("auto_probe_enabled", KnowledgeOptions().auto_probe_enabled)
            ),
            auto_probe_min_score=float(
                knowledge_data.get("auto_probe_min_score", KnowledgeOptions().auto_probe_min_score)
            ),
            auto_probe_intent_terms=_string_list(
                knowledge_data.get("auto_probe_intent_terms", KnowledgeOptions().auto_probe_intent_terms),
                "knowledge.auto_probe_intent_terms",
            ),
            auto_probe_no_hit_terms=_string_list(
                knowledge_data.get("auto_probe_no_hit_terms", KnowledgeOptions().auto_probe_no_hit_terms),
                "knowledge.auto_probe_no_hit_terms",
            ),
            sources=_knowledge_sources(knowledge_data.get("sources", []), base_dir),
        ),
        source_investigation=SourceInvestigationOptions(
            enabled=bool(source_investigation_data.get("enabled", SourceInvestigationOptions().enabled)),
            provider=source_provider,
            command=source_command,
            model=str(source_investigation_data.get("model", SourceInvestigationOptions().model)),
            fallback_model=str(
                source_investigation_data.get(
                    "fallback_model",
                    SourceInvestigationOptions().fallback_model,
                )
            ),
            timeout_seconds=int(
                source_investigation_data.get(
                    "timeout_seconds",
                    SourceInvestigationOptions().timeout_seconds,
                )
            ),
            max_evidence=int(source_investigation_data.get("max_evidence", SourceInvestigationOptions().max_evidence)),
            repo_roots=_path_list(
                source_investigation_data.get("repo_roots", _default_repo_roots(default_guideengine_repo, default_napa5_repo)),
                base_dir,
                "source_investigation.repo_roots",
            ),
            add_dirs=_path_list(
                source_investigation_data.get("add_dirs", SourceInvestigationOptions().add_dirs),
                base_dir,
                "source_investigation.add_dirs",
            ),
            priority_modules=[
                str(m) for m in source_investigation_data.get("priority_modules", [])
            ],
            signal_priority_modules={
                str(k): [str(v) for v in vs]
                for k, vs in (source_investigation_data.get("signal_priority_modules") or {}).items()
                if isinstance(vs, list)
            },
            exclude_paths=[
                str(p) for p in source_investigation_data.get("exclude_paths", [])
            ],
            code_index_enabled=bool(
                source_investigation_data.get("code_index_enabled", SourceInvestigationOptions().code_index_enabled)
            ),
            ctags_command=str(
                source_investigation_data.get("ctags_command", SourceInvestigationOptions().ctags_command)
            ),
            code_index_timeout_seconds=float(
                source_investigation_data.get(
                    "code_index_timeout_seconds",
                    SourceInvestigationOptions().code_index_timeout_seconds,
                )
            ),
            code_index_min_confidence=float(
                source_investigation_data.get(
                    "code_index_min_confidence",
                    SourceInvestigationOptions().code_index_min_confidence,
                )
            ),
            codegraph_enabled=bool(
                source_investigation_data.get("codegraph_enabled", SourceInvestigationOptions().codegraph_enabled)
            ),
            codegraph_command=str(
                source_investigation_data.get("codegraph_command", SourceInvestigationOptions().codegraph_command)
            ),
            codegraph_timeout_seconds=float(
                source_investigation_data.get(
                    "codegraph_timeout_seconds",
                    SourceInvestigationOptions().codegraph_timeout_seconds,
                )
            ),
            codegraph_min_confidence=float(
                source_investigation_data.get(
                    "codegraph_min_confidence",
                    SourceInvestigationOptions().codegraph_min_confidence,
                )
            ),
        ),
        signal_resolver=SignalResolverOptions(
            preferred_paths=[
                str(p) for p in (data.get("signal_resolver", {}).get("preferred_paths", []))
            ],
            source_suffixes=[
                str(s) for s in (data.get("signal_resolver", {}).get("source_suffixes", []))
            ],
            cache_ttl_seconds=float(
                data.get("signal_resolver", {}).get("cache_ttl_seconds", 3600.0)
            ),
        ),
        ai_provider=ai_provider,
        runner_timeout_seconds=int(runner_data.get("timeout_seconds", 900)),
    )
    _warn_suspicious_paths(config)
    return config


def with_cli_overrides(config: BridgeConfig, *, dry_run: bool = False) -> BridgeConfig:
    if dry_run:
        return replace(config, dry_run=True)
    return config


def _resolve_path(value: str | Path, base_dir: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def _default_workspace_root(base_dir: Path) -> Path:
    if base_dir.name == "lark-agent-bridge" and base_dir.parent.name == "tools":
        return base_dir.parent.parent
    return base_dir


def _default_repo_roots(guideengine: Path, napa5: Path) -> list[str]:
    """Build default repo_roots list, including only paths that exist."""
    roots = [str(guideengine)]
    if napa5.exists():
        roots.append(str(napa5))
    return roots


def _bool_value(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Invalid boolean environment value: {value}")


def _bool_like(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return _bool_value(value, default)
    return bool(value)


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


def _optional_path(value: Any, base_dir: Path, field_name: str = "path") -> Path | None:
    if value is None or value == "":
        return None
    if not isinstance(value, (str, Path)):
        raise ValueError(f"{field_name} must be a path string")
    return _resolve_path(value, base_dir)


def _path_list(value: Any, base_dir: Path, field_name: str) -> list[Path]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field_name} must be a list of path strings")
    return [_resolve_path(item, base_dir) for item in value]


def _knowledge_sources(value: Any, base_dir: Path) -> list[KnowledgeSourceOptions]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise ValueError("knowledge.sources must be a list of tables")
    sources: list[KnowledgeSourceOptions] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"knowledge.sources[{index}] must be a table")
        source_id = str(item.get("id") or "").strip()
        source_type = str(item.get("type") or "").strip()
        if not source_id:
            raise ValueError(f"knowledge.sources[{index}].id is required")
        if not source_type:
            raise ValueError(f"knowledge.sources[{index}].type is required")
        raw_path = item.get("path") or ""
        path = str(_resolve_path(raw_path, base_dir)) if raw_path else ""
        sources.append(
            KnowledgeSourceOptions(
                id=source_id,
                type=source_type,
                path=path,
                url=str(item.get("url") or "").strip(),
                title=str(item.get("title") or "").strip(),
                table_id=str(item.get("table_id") or "").strip(),
                view_id=str(item.get("view_id") or "").strip(),
            )
        )
    return sources
