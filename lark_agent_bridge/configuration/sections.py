"""config.toml 各 section → Options 的构建器（每节一个函数，默认值与原实现一致）。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..models import (
    AIProviderOptions,
    AppServerInvestigationOptions,
    ApprovalOptions,
    BugAnalysisOptions,
    ClaudeAgentOptions,
    CodexAppServerOptions,
    DownloadConfig,
    DualAgentOptions,
    EventConsumerOptions,
    IntentAnalysisOptions,
    InternalNetworkEnvOptions,
    JobRetentionOptions,
    KnowledgeOptions,
    KnowledgeSourceOptions,
    LarkOptions,
    LocalResourceOptions,
    NotificationOptions,
    OmlxChatOptions,
    ReportServerOptions,
    RequirementAnalysisOptions,
    SignalResolverOptions,
    SourceInvestigationOptions,
    WorkflowArchiveOptions,
)
from .coercion import (
    _bool_value,
    _optional_path,
    _optional_str,
    _path_list,
    _resolve_path,
    _string_dict,
    _string_list,
)
from .presets import (
    _apply_ai_preset,
    _default_agent_command_for_provider,
    _default_agent_provider_for_api_format,
)


def _ai_provider_options(
    data: dict[str, Any],
    presets: dict[str, dict[str, Any]],
) -> AIProviderOptions:
    ai_enabled_env = os.environ.get("LARK_AGENT_BRIDGE_AI_ENABLED")
    return _apply_ai_preset(
        AIProviderOptions(
            enabled=_bool_value(ai_enabled_env, bool(data.get("enabled", False))),
            preset=str(os.environ.get("LARK_AGENT_BRIDGE_AI_PRESET") or data.get("preset", "")),
            api_format=str(data.get("api_format", "")),
            primary_model=str(
                os.environ.get("LARK_AGENT_BRIDGE_AI_PRIMARY_MODEL") or data.get("primary_model", "")
            ),
            fallback_model=str(data.get("fallback_model", "")),
            fast_model=str(data.get("fast_model", "")),
            base_url=str(os.environ.get("LARK_AGENT_BRIDGE_AI_BASE_URL") or data.get("base_url", "")),
            api_key=str(os.environ.get("LARK_AGENT_BRIDGE_AI_API_KEY") or data.get("api_key", "")),
            fallback_base_url=str(
                os.environ.get("LARK_AGENT_BRIDGE_AI_FALLBACK_BASE_URL")
                or data.get("fallback_base_url", "")
            ),
            fallback_api_key=str(
                os.environ.get("LARK_AGENT_BRIDGE_AI_FALLBACK_API_KEY")
                or data.get("fallback_api_key", "")
            ),
            intent_temperature=float(data.get("intent_temperature", 0.0)),
            intent_max_tokens=int(data.get("intent_max_tokens", 4096)),
            intent_timeout_seconds=float(data.get("intent_timeout_seconds", 30)),
            intent_max_retries=int(data.get("intent_max_retries", 2)),
            summary_temperature=float(data.get("summary_temperature", 0.3)),
            summary_max_tokens=int(data.get("summary_max_tokens", 4096)),
            summary_timeout_seconds=float(data.get("summary_timeout_seconds", 300)),
        ),
        presets=presets,
        apply_enabled=ai_enabled_env is None,
    )


class AgentBindings:
    """bug/intent/source 三个执行域的 provider+command 推导（含环境变量覆盖）。"""

    def __init__(
        self,
        ai_provider: AIProviderOptions,
        bug_data: dict[str, Any],
        intent_data: dict[str, Any],
        source_data: dict[str, Any],
    ) -> None:
        provider_override = str(os.environ.get("LARK_AGENT_BRIDGE_AGENT_PROVIDER") or "").strip()
        command_override = str(os.environ.get("LARK_AGENT_BRIDGE_AGENT_COMMAND") or "").strip()

        self.bug_provider = (provider_override or str(bug_data.get("provider", ""))).strip()
        if not self.bug_provider:
            self.bug_provider = (
                ai_provider.agent_provider
                or _default_agent_provider_for_api_format(ai_provider.api_format)
                or BugAnalysisOptions().provider
            )
        self.bug_command = (command_override or str(bug_data.get("command", ""))).strip()
        if not self.bug_command:
            self.bug_command = (
                ai_provider.agent_command
                or _default_agent_command_for_provider(self.bug_provider)
                or BugAnalysisOptions().command
            )
        self.intent_provider = (provider_override or str(intent_data.get("provider", ""))).strip()
        if not self.intent_provider:
            self.intent_provider = self.bug_provider
        self.intent_command = (command_override or str(intent_data.get("command", ""))).strip()
        if not self.intent_command:
            self.intent_command = (
                _default_agent_command_for_provider(self.intent_provider) or self.bug_command
            )
        self.source_provider = (provider_override or str(source_data.get("provider", ""))).strip()
        if not self.source_provider:
            self.source_provider = (
                ai_provider.agent_provider
                or _default_agent_provider_for_api_format(ai_provider.api_format)
                or SourceInvestigationOptions().provider
            )
        self.source_command = (command_override or str(source_data.get("command", ""))).strip()
        if not self.source_command:
            self.source_command = (
                ai_provider.agent_command
                or _default_agent_command_for_provider(self.source_provider)
                or SourceInvestigationOptions().command
            )


def _download_config(data: dict[str, Any]) -> DownloadConfig:
    return DownloadConfig(
        max_bytes=int(data.get("max_bytes", 5 * 1024 * 1024 * 1024)),
        timeout_seconds=int(data.get("timeout_seconds", 60)),
        allow_private_urls=bool(data.get("allow_private_urls", True)),
    )


def _local_resource_options(data: dict[str, Any], base_dir: Path) -> LocalResourceOptions:
    defaults = LocalResourceOptions()
    return LocalResourceOptions(
        enabled=bool(data.get("enabled", defaults.enabled)),
        require_allowed_user=bool(data.get("require_allowed_user", defaults.require_allowed_user)),
        allowed_dirs=_path_list(
            data.get("allowed_dirs", [str(path) for path in defaults.allowed_dirs]),
            base_dir,
            "local_resources.allowed_dirs",
        ),
    )


def _job_retention_options(data: dict[str, Any]) -> JobRetentionOptions:
    return JobRetentionOptions(
        enabled=bool(data.get("enabled", True)),
        max_age_hours=int(data.get("max_age_hours", 6)),
        bug_cache_max_age_hours=int(data.get("bug_cache_max_age_hours", 24)),
        purge_all_on_listen_start=bool(data.get("purge_all_on_listen_start", False)),
        cleanup_interval_seconds=int(data.get("cleanup_interval_seconds", 60)),
    )


def _event_consumer_options(data: dict[str, Any]) -> EventConsumerOptions:
    defaults = EventConsumerOptions()
    return EventConsumerOptions(
        event_key=str(data.get("event_key", defaults.event_key)),
        ready_timeout_seconds=float(data.get("ready_timeout_seconds", defaults.ready_timeout_seconds)),
        restart_on_failure=bool(data.get("restart_on_failure", defaults.restart_on_failure)),
        max_restarts=int(data.get("max_restarts", defaults.max_restarts)),
        restart_initial_delay_seconds=float(
            data.get("restart_initial_delay_seconds", defaults.restart_initial_delay_seconds)
        ),
        restart_max_delay_seconds=float(
            data.get("restart_max_delay_seconds", defaults.restart_max_delay_seconds)
        ),
        drop_stale_light_interactions=bool(
            data.get("drop_stale_light_interactions", defaults.drop_stale_light_interactions)
        ),
        stale_light_interaction_grace_seconds=float(
            data.get(
                "stale_light_interaction_grace_seconds",
                defaults.stale_light_interaction_grace_seconds,
            )
        ),
        max_concurrent_jobs=int(data.get("max_concurrent_jobs", defaults.max_concurrent_jobs)),
        max_queue_size=int(data.get("max_queue_size", defaults.max_queue_size)),
        heavy_job_timeout_seconds=float(
            data.get("heavy_job_timeout_seconds", defaults.heavy_job_timeout_seconds)
        ),
        light_inline=bool(data.get("light_inline", defaults.light_inline)),
        max_concurrent_per_chat=int(
            data.get("max_concurrent_per_chat", defaults.max_concurrent_per_chat)
        ),
    )


def _lark_options(data: dict[str, Any]) -> LarkOptions:
    return LarkOptions(
        reply_in_thread=bool(data.get("reply_in_thread", False)),
        mention_sender_in_group=bool(data.get("mention_sender_in_group", True)),
        bot_open_id=str(os.environ.get("LARK_AGENT_BRIDGE_BOT_OPEN_ID") or data.get("bot_open_id", "")),
        bot_name=str(os.environ.get("LARK_AGENT_BRIDGE_BOT_NAME") or data.get("bot_name", "")),
    )


def _internal_network_env_options(data: dict[str, Any]) -> InternalNetworkEnvOptions:
    return InternalNetworkEnvOptions(
        inherit_env=_string_list(data.get("inherit_env", []), "internal_network_env.inherit_env"),
        unset_env=_string_list(data.get("unset_env", []), "internal_network_env.unset_env"),
    )


def _claude_agent_options(data: dict[str, Any], base_dir: Path) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        enabled=bool(data.get("enabled", True)),
        command=str(data.get("command", "claude")),
        trigger_prefixes=_string_list(data.get("trigger_prefixes", []), "claude_agent.trigger_prefixes"),
        working_dir=_optional_path(data.get("working_dir"), base_dir, "claude_agent.working_dir"),
        add_dirs=_path_list(data.get("add_dirs", []), base_dir, "claude_agent.add_dirs"),
        allowed_tools=_string_list(
            data.get("allowed_tools", ["Read", "Grep", "Glob", "LS"]),
            "claude_agent.allowed_tools",
        ),
        model=_optional_str(data.get("model"), "claude_agent.model"),
        agent=_optional_str(data.get("agent"), "claude_agent.agent"),
        permission_mode=str(data.get("permission_mode", "dontAsk")),
        timeout_seconds=int(data.get("timeout_seconds", 1800)),
        max_prompt_chars=int(data.get("max_prompt_chars", 12000)),
        upload_result_file=bool(data.get("upload_result_file", True)),
        system_prompt=str(data.get("system_prompt", ClaudeAgentOptions().system_prompt)),
    )


def _app_server_investigation_options(data: dict[str, Any]) -> AppServerInvestigationOptions:
    defaults = BugAnalysisOptions().app_server_investigation
    section = data.get("app_server_investigation") or {}
    return AppServerInvestigationOptions(
        enabled=bool(section.get("enabled", defaults.enabled)),
        auto_terms=_string_list(
            section.get("auto_terms", defaults.auto_terms),
            "bug_analysis.app_server_investigation.auto_terms",
        ),
        free_terms=_string_list(
            section.get("free_terms", defaults.free_terms),
            "bug_analysis.app_server_investigation.free_terms",
        ),
        require_description_for_file_resources=bool(
            section.get(
                "require_description_for_file_resources",
                defaults.require_description_for_file_resources,
            )
        ),
        require_time_for_file_resources=bool(
            section.get("require_time_for_file_resources", defaults.require_time_for_file_resources)
        ),
        prompt_template=str(section.get("prompt_template", defaults.prompt_template)),
        model=str(section.get("model", defaults.model)),
        reasoning_effort=str(section.get("reasoning_effort", defaults.reasoning_effort)),
    )


def _bug_analysis_options(
    data: dict[str, Any],
    base_dir: Path,
    bindings: AgentBindings,
) -> BugAnalysisOptions:
    defaults = BugAnalysisOptions()
    return BugAnalysisOptions(
        enabled=bool(data.get("enabled", True)),
        provider=bindings.bug_provider,
        command=bindings.bug_command,
        model=str(data.get("model", defaults.model)),
        working_dir=_optional_path(data.get("working_dir"), base_dir, "bug_analysis.working_dir"),
        timeout_seconds=int(data.get("timeout_seconds", 5400)),
        agent_summary_timeout_seconds=int(
            data.get("agent_summary_timeout_seconds", defaults.agent_summary_timeout_seconds)
        ),
        file_agent_debug_logs=bool(data.get("file_agent_debug_logs", defaults.file_agent_debug_logs)),
        max_prompt_chars=int(data.get("max_prompt_chars", 16000)),
        upload_result_files=bool(data.get("upload_result_files", True)),
        default_prompt=str(data.get("default_prompt", defaults.default_prompt)),
        resume_followup_sessions=bool(
            data.get("resume_followup_sessions", defaults.resume_followup_sessions)
        ),
        auto_fallback_to_file_agent=bool(
            data.get("auto_fallback_to_file_agent", defaults.auto_fallback_to_file_agent)
        ),
        confirm_low_confidence_skill=bool(
            data.get("confirm_low_confidence_skill", defaults.confirm_low_confidence_skill)
        ),
        force_reanalysis_terms=_string_list(
            data.get("force_reanalysis_terms", defaults.force_reanalysis_terms),
            "bug_analysis.force_reanalysis_terms",
        ),
        app_server_investigation=_app_server_investigation_options(data),
    )


def _intent_analysis_options(
    data: dict[str, Any],
    base_dir: Path,
    bindings: AgentBindings,
) -> IntentAnalysisOptions:
    defaults = IntentAnalysisOptions()
    return IntentAnalysisOptions(
        enabled=bool(data.get("enabled", False)),
        provider=bindings.intent_provider,
        command=bindings.intent_command,
        model=str(data.get("model", defaults.model)),
        working_dir=_optional_path(data.get("working_dir"), base_dir, "intent_analysis.working_dir"),
        timeout_seconds=int(data.get("timeout_seconds", 180)),
        max_prompt_chars=int(data.get("max_prompt_chars", defaults.max_prompt_chars)),
        allow_subprocess_fallback=bool(
            data.get("allow_subprocess_fallback", defaults.allow_subprocess_fallback)
        ),
        system_prompt=str(data.get("system_prompt", defaults.system_prompt)),
    )


def _omlx_chat_options(data: dict[str, Any]) -> OmlxChatOptions:
    defaults = OmlxChatOptions()
    return OmlxChatOptions(
        enabled=bool(data.get("enabled", True)),
        base_url=str(
            os.environ.get("LARK_AGENT_BRIDGE_OMLX_BASE_URL")
            or data.get("base_url", "http://127.0.0.1:8000/v1")
        ),
        model=str(
            os.environ.get("LARK_AGENT_BRIDGE_OMLX_MODEL")
            or data.get("model", "gemma-4-26b-a4b-it-4bit")
        ),
        api_key=str(
            os.environ.get("LARK_AGENT_BRIDGE_OMLX_API_KEY")
            or data.get("api_key")
            or defaults.api_key
        ),
        timeout_seconds=int(data.get("timeout_seconds", 120)),
        max_prompt_chars=int(data.get("max_prompt_chars", 2000)),
        max_tokens=int(data.get("max_tokens", 1024)),
        temperature=float(data.get("temperature", 0.3)),
        system_prompt=str(data.get("system_prompt", defaults.system_prompt)),
        followup_max_context_chars=int(
            data.get("followup_max_context_chars", defaults.followup_max_context_chars)
        ),
        followup_max_history_turns=int(
            data.get("followup_max_history_turns", defaults.followup_max_history_turns)
        ),
        followup_system_prompt=str(
            data.get("followup_system_prompt", defaults.followup_system_prompt)
        ),
    )


def _report_server_options(data: dict[str, Any]) -> ReportServerOptions:
    return ReportServerOptions(
        enabled=bool(data.get("enabled", True)),
        bind_host=str(data.get("bind_host", "127.0.0.1")),
        port=int(data.get("port", 8765)),
        public_base_url=str(
            os.environ.get("LARK_AGENT_BRIDGE_REPORT_PUBLIC_BASE_URL")
            or data.get("public_base_url", ReportServerOptions().public_base_url)
        ),
        admin_token=str(
            os.environ.get("LARK_AGENT_BRIDGE_ADMIN_TOKEN") or data.get("admin_token", "")
        ),
    )


def _approval_options(data: dict[str, Any]) -> ApprovalOptions:
    return ApprovalOptions(enabled=bool(data.get("enabled", ApprovalOptions().enabled)))


def _codex_app_server_options(data: dict[str, Any]) -> CodexAppServerOptions:
    defaults = CodexAppServerOptions()
    return CodexAppServerOptions(
        enabled=bool(data.get("enabled", defaults.enabled)),
        command=str(data.get("command", defaults.command)),
        min_version=str(data.get("min_version", defaults.min_version)),
        use_for_file_agent=bool(data.get("use_for_file_agent", defaults.use_for_file_agent)),
        use_for_bug_summary=bool(data.get("use_for_bug_summary", defaults.use_for_bug_summary)),
        fallback_to_exec=bool(data.get("fallback_to_exec", defaults.fallback_to_exec)),
        startup_timeout_seconds=float(
            data.get("startup_timeout_seconds", defaults.startup_timeout_seconds)
        ),
        turn_timeout_seconds=float(data.get("turn_timeout_seconds", defaults.turn_timeout_seconds)),
        post_tool_quiet_timeout_seconds=float(
            data.get("post_tool_quiet_timeout_seconds", defaults.post_tool_quiet_timeout_seconds)
        ),
        notification_poll_seconds=float(
            data.get("notification_poll_seconds", defaults.notification_poll_seconds)
        ),
        no_event_timeout_seconds=float(
            data.get("no_event_timeout_seconds", defaults.no_event_timeout_seconds)
        ),
        max_event_audit=int(data.get("max_event_audit", defaults.max_event_audit)),
        sandbox_mode=str(data.get("sandbox_mode", defaults.sandbox_mode)),
        model=str(data.get("model", defaults.model)),
        disable_node_repl=bool(data.get("disable_node_repl", defaults.disable_node_repl)),
        disable_analytics=bool(data.get("disable_analytics", defaults.disable_analytics)),
        disable_memories=bool(data.get("disable_memories", defaults.disable_memories)),
        disable_apps_feature=bool(data.get("disable_apps_feature", defaults.disable_apps_feature)),
        disable_plugins_feature=bool(
            data.get("disable_plugins_feature", defaults.disable_plugins_feature)
        ),
        disable_computer_use_feature=bool(
            data.get("disable_computer_use_feature", defaults.disable_computer_use_feature)
        ),
        preserve_proxy_env=bool(data.get("preserve_proxy_env", defaults.preserve_proxy_env)),
        reasoning_effort=str(data.get("reasoning_effort", defaults.reasoning_effort)),
        use_minimal_home=bool(data.get("use_minimal_home", defaults.use_minimal_home)),
    )


def _workflow_archive_options(data: dict[str, Any]) -> WorkflowArchiveOptions:
    defaults = WorkflowArchiveOptions()
    return WorkflowArchiveOptions(
        enabled=bool(data.get("enabled", defaults.enabled)),
        base_token=str(data.get("base_token", "")),
        table_id=str(data.get("table_id", "")),
        drive_folder_token=str(data.get("drive_folder_token", "")),
        doc_parent_token=str(data.get("doc_parent_token", "")),
        base_field_map=_string_dict(
            data.get("base_field_map", defaults.base_field_map),
            "workflow_archive.base_field_map",
        ),
    )


def _notification_options(data: dict[str, Any]) -> NotificationOptions:
    defaults = NotificationOptions()
    return NotificationOptions(
        enabled=bool(data.get("enabled", defaults.enabled)),
        report_ready=bool(data.get("report_ready", defaults.report_ready)),
    )


def _dual_agent_options(data: dict[str, Any]) -> DualAgentOptions:
    return DualAgentOptions(enabled=bool(data.get("enabled", DualAgentOptions().enabled)))


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


def _knowledge_options(data: dict[str, Any], base_dir: Path) -> KnowledgeOptions:
    defaults = KnowledgeOptions()
    return KnowledgeOptions(
        enabled=bool(data.get("enabled", defaults.enabled)),
        storage=_resolve_path(data.get("storage", defaults.storage), base_dir),
        max_hits=int(data.get("max_hits", defaults.max_hits)),
        trigger_prefixes=_string_list(
            data.get("trigger_prefixes", defaults.trigger_prefixes),
            "knowledge.trigger_prefixes",
        ),
        answer_provider=str(data.get("answer_provider", defaults.answer_provider)),
        auto_probe_enabled=bool(data.get("auto_probe_enabled", defaults.auto_probe_enabled)),
        auto_probe_min_score=float(data.get("auto_probe_min_score", defaults.auto_probe_min_score)),
        auto_probe_intent_terms=_string_list(
            data.get("auto_probe_intent_terms", defaults.auto_probe_intent_terms),
            "knowledge.auto_probe_intent_terms",
        ),
        auto_probe_no_hit_terms=_string_list(
            data.get("auto_probe_no_hit_terms", defaults.auto_probe_no_hit_terms),
            "knowledge.auto_probe_no_hit_terms",
        ),
        sources=_knowledge_sources(data.get("sources", []), base_dir),
    )


def _source_investigation_options(
    data: dict[str, Any],
    base_dir: Path,
    bindings: AgentBindings,
    default_repo_roots: list[str],
) -> SourceInvestigationOptions:
    defaults = SourceInvestigationOptions()
    return SourceInvestigationOptions(
        enabled=bool(data.get("enabled", defaults.enabled)),
        provider=bindings.source_provider,
        command=bindings.source_command,
        model=str(data.get("model", defaults.model)),
        fallback_model=str(data.get("fallback_model", defaults.fallback_model)),
        timeout_seconds=int(data.get("timeout_seconds", defaults.timeout_seconds)),
        max_evidence=int(data.get("max_evidence", defaults.max_evidence)),
        repo_roots=_path_list(
            data.get("repo_roots", default_repo_roots),
            base_dir,
            "source_investigation.repo_roots",
        ),
        add_dirs=_path_list(
            data.get("add_dirs", defaults.add_dirs),
            base_dir,
            "source_investigation.add_dirs",
        ),
        priority_modules=[str(m) for m in data.get("priority_modules", [])],
        signal_priority_modules={
            str(k): [str(v) for v in vs]
            for k, vs in (data.get("signal_priority_modules") or {}).items()
            if isinstance(vs, list)
        },
        exclude_paths=[str(p) for p in data.get("exclude_paths", [])],
        code_index_enabled=bool(data.get("code_index_enabled", defaults.code_index_enabled)),
        ctags_command=str(data.get("ctags_command", defaults.ctags_command)),
        code_index_timeout_seconds=float(
            data.get("code_index_timeout_seconds", defaults.code_index_timeout_seconds)
        ),
        code_index_min_confidence=float(
            data.get("code_index_min_confidence", defaults.code_index_min_confidence)
        ),
        codegraph_enabled=bool(data.get("codegraph_enabled", defaults.codegraph_enabled)),
        codegraph_command=str(data.get("codegraph_command", defaults.codegraph_command)),
        codegraph_timeout_seconds=float(
            data.get("codegraph_timeout_seconds", defaults.codegraph_timeout_seconds)
        ),
        codegraph_min_confidence=float(
            data.get("codegraph_min_confidence", defaults.codegraph_min_confidence)
        ),
    )


def _signal_resolver_options(data: dict[str, Any]) -> SignalResolverOptions:
    section = data.get("signal_resolver", {})
    return SignalResolverOptions(
        preferred_paths=[str(p) for p in section.get("preferred_paths", [])],
        source_suffixes=[str(s) for s in section.get("source_suffixes", [])],
        cache_ttl_seconds=float(section.get("cache_ttl_seconds", 3600.0)),
    )


def _requirement_analysis_options(data: dict[str, Any]) -> RequirementAnalysisOptions:
    defaults = RequirementAnalysisOptions()
    return RequirementAnalysisOptions(
        enabled=bool(data.get("enabled", defaults.enabled)),
        timeout_seconds=int(data.get("timeout_seconds", defaults.timeout_seconds)),
        fetch_comments=bool(data.get("fetch_comments", defaults.fetch_comments)),
        fetch_wiki_body=bool(data.get("fetch_wiki_body", defaults.fetch_wiki_body)),
        max_requirement_chars=int(
            data.get("max_requirement_chars", defaults.max_requirement_chars)
        ),
        max_fact_count=int(data.get("max_fact_count", defaults.max_fact_count)),
    )
