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
    IntentAnalysisOptions,
    JobRetentionOptions,
    KnowledgeOptions,
    KnowledgeSourceOptions,
    LarkOptions,
    LocalResourceOptions,
    NotificationOptions,
    OmlxChatOptions,
    ReportServerOptions,
    SourceInvestigationOptions,
    WorkflowArchiveOptions,
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
    local_resource_data = data.get("local_resources") or {}
    retention_data = data.get("job_retention") or {}
    event_consumer_data = data.get("event_consumer") or {}
    lark_data = data.get("lark") or {}
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
    guideengine_repo = _resolve_path(
        os.environ.get("LARK_AGENT_BRIDGE_GUIDEENGINE_REPO")
        or data.get("guideengine_repo", default_guideengine_repo),
        base_dir,
    )

    return BridgeConfig(
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
            provider=str(bug_data.get("provider", "claude")),
            command=str(bug_data.get("command", "claude")),
            model=str(bug_data.get("model", BugAnalysisOptions().model)),
            working_dir=_optional_path(bug_data.get("working_dir"), base_dir, "bug_analysis.working_dir"),
            timeout_seconds=int(bug_data.get("timeout_seconds", 5400)),
            agent_summary_timeout_seconds=int(
                bug_data.get(
                    "agent_summary_timeout_seconds",
                    BugAnalysisOptions().agent_summary_timeout_seconds,
                )
            ),
            max_prompt_chars=int(bug_data.get("max_prompt_chars", 16000)),
            upload_result_files=bool(bug_data.get("upload_result_files", True)),
            default_prompt=str(bug_data.get("default_prompt", BugAnalysisOptions().default_prompt)),
            resume_followup_sessions=bool(
                bug_data.get("resume_followup_sessions", BugAnalysisOptions().resume_followup_sessions)
            ),
            force_reanalysis_terms=_string_list(
                bug_data.get("force_reanalysis_terms", BugAnalysisOptions().force_reanalysis_terms),
                "bug_analysis.force_reanalysis_terms",
            ),
        ),
        intent_analysis=IntentAnalysisOptions(
            enabled=bool(intent_data.get("enabled", False)),
            provider=str(intent_data.get("provider", "")),
            command=str(intent_data.get("command", "")),
            model=str(intent_data.get("model", IntentAnalysisOptions().model)),
            working_dir=_optional_path(intent_data.get("working_dir"), base_dir, "intent_analysis.working_dir"),
            timeout_seconds=int(intent_data.get("timeout_seconds", 180)),
            max_prompt_chars=int(intent_data.get("max_prompt_chars", IntentAnalysisOptions().max_prompt_chars)),
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
            api_key=str(os.environ.get("LARK_AGENT_BRIDGE_OMLX_API_KEY") or omlx_data.get("api_key", "")),
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
            provider=str(source_investigation_data.get("provider", SourceInvestigationOptions().provider)),
            command=str(source_investigation_data.get("command", SourceInvestigationOptions().command)),
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
                source_investigation_data.get("repo_roots", [str(guideengine_repo)]),
                base_dir,
                "source_investigation.repo_roots",
            ),
            add_dirs=_path_list(
                source_investigation_data.get("add_dirs", SourceInvestigationOptions().add_dirs),
                base_dir,
                "source_investigation.add_dirs",
            ),
        ),
        ai_provider=AIProviderOptions(
            enabled=bool(ai_provider_data.get("enabled", False)),
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
