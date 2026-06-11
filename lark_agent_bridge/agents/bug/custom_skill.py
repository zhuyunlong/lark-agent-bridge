from __future__ import annotations

from ._shared import *  # noqa: F401,F403


_FOCUS_LOOKBACK_SECONDS = 3600
_FOCUS_FORWARD_BUFFER_SECONDS = 300
_FOCUS_MAX_LINES_PER_FILE = 15000
_FOCUS_MIN_LINES_PER_FILE = 2000
_FOCUS_MAX_LOOKBACK_SECONDS = 21600
_FOCUS_EXPAND_STEP_SECONDS = 3600
_FOCUS_LOGD_PREFIXES = ("kernel", "main", "events", "crash")


from .log_focus import _LogFocusMixin
from .agent_command import _AgentCommandMixin
from .agent_output import _AgentOutputMixin


class _CustomSkillMixin(_LogFocusMixin, _AgentCommandMixin, _AgentOutputMixin):
    def _write_file_agent_context(
        self,
        *,
        analysis_kind: str,
        skill_name: str,
        request_text: str,
        prompt_text: str,
        title: str,
        description: str,
        fault_time: str,
        original_selected_input: Path | None,
        focused_log_input: Path | None,
        log_focus_manifest: Path | None,
        source_evidence_path: Path | None,
        analysis_dir: Path,
        analysis_markdown_path: Path,
        prior_findings: list[tuple[str, str, str]] | None = None,
    ) -> Path:
        context_path = analysis_dir / self._skill_agent_sidecar_name(analysis_kind, "context.md")
        analysis_dir.mkdir(parents=True, exist_ok=True)
        source_roots = self._custom_skill_agent_source_roots()
        sanitized_request = self._sanitize_file_agent_text(prompt_text or request_text) or "未提供"
        sanitized_description = self._sanitize_file_agent_text(description) or ""
        lines = [
            "# File Agent Context",
            "",
            "## 1. 用户请求（最重要）",
            f"- **用户请求**: {sanitized_request}",
            f"- Bug 标题: {title.strip() or '未提供'}",
            f"- 故障时间: {fault_time or '未识别'}",
        ]
        if sanitized_description and sanitized_description != sanitized_request:
            lines.append(f"- 缺陷描述: {sanitized_description[:800]}")
        lines.extend(
            [
                "",
                "## 2. 已下载好的日志路径",
                f"- 原始日志目录: `{original_selected_input}`" if original_selected_input else "- 原始日志目录: 未提供",
                f"- 本轮聚焦日志目录: `{focused_log_input}`" if focused_log_input else "- 本轮聚焦日志目录: 未生成",
                f"- 聚焦清单: `{log_focus_manifest}`" if log_focus_manifest else "- 聚焦清单: 未生成",
                "",
                "## 3. Skill 目录",
            ]
        )
        skill_paths = self._skill_context_paths(skill_name)
        if skill_paths:
            lines.extend(f"- `{path}`" for path in skill_paths)
        else:
            lines.append("- 未找到 Skill 上下文文件。")
        priority_files = self._domain_priority_files(skill_name)
        if priority_files:
            lines.extend(
                [
                    "",
                    "## 3.1 领域优先源码文件",
                    "- 先读这些文件，再决定是否扩展搜索；不要先从整个源码根做全仓扫描。",
                ]
            )
            lines.extend(f"- `{path}`" for path in priority_files)
        lines.extend(
            [
                "",
                "## 4. 日志规则",
                *[f"- {rule}" for rule in self._file_agent_log_rules(analysis_kind)],
                "",
                "## 检索预算",
                *[f"- {rule}" for rule in self._file_agent_search_budget_rules()],
                "",
                "## 5. 源码路径",
            ]
        )
        if source_roots:
            lines.extend(f"- `{path}`" for path in source_roots)
        else:
            lines.append("- 未配置源码根目录。")
        lines.append(f"- 预检索源码证据: `{source_evidence_path}`" if source_evidence_path else "- 预检索源码证据: 未生成")
        codegraph_rules = self._file_agent_codegraph_rules(source_roots)
        if codegraph_rules:
            lines.extend(
                [
                    "",
                    "## 5.1 CodeGraph 语义检索",
                    *[f"- {rule}" for rule in codegraph_rules],
                ]
            )
        if prior_findings:
            lines.extend(
                [
                    "",
                    "## 6. 前序分析结论（已完成的其他分析，供参考）",
                ]
            )
            for label, kind, verdict in prior_findings:
                lines.append(f"- **{label}** (`{kind}`): {verdict}")
            lines.append("- 以上结论来自其他专用 Skill，请在此基础上做源码级深入分析。")
        emit_node_status = _kind_spec(analysis_kind).is_source_stage
        output_rule = (
            "- 输出中文 Markdown 正文，不要输出 HTML；本轮 prompt 明确要求结构化 JSON fenced block，允许在末尾输出该 JSON 代码块。"
            if _prompt_requires_json_fence(prompt_text)
            else "- 只输出 Markdown 正文，不要输出代码块围栏，不要输出 HTML。"
        )
        output_section = [
            "",
            "## 输出要求",
            output_rule,
            "- 必须包含这些二级标题：`## 结论摘要`、`## 关键证据`、`## 最可能原因`、`## 待确认项`、`## 建议动作`。",
            "- `## 关键证据` 不能为空，每条证据都要能回指到日志文件+时间，或源码文件+行号，或 bridge 生成的工具结果文件。",
            f"- 最终正文会由 bridge 保存到 `{analysis_markdown_path}`。",
        ]
        if emit_node_status:
            output_section.append(
                "- 在正文之后追加一个 json 围栏(```json ... ```)，内容为 "
                "{\"node_status\": {\"<源码文件名>\": \"ok|suspect|broken|unknown\"}, "
                "\"findings\": [{\"file\":..., \"severity\":..., \"title\":...}], "
                "\"verdict\": {\"status\":\"ok|broken|inconclusive\", \"headline\":\"一句话结论\", \"next_step\":\"下一步建议\"}}，"
                "标注每个链路节点是否打通，并在 verdict 给出整体结论。"
            )
        lines.extend(
            output_section
            + [
                "",
                "## 执行约束",
                "- 本次只读分析，不可修改源码内容，不可修改任何本地文件。",
                "- 优先使用 Read/Grep/Glob/LS；只有在必要时才用 Bash 做只读命令。",
                "- Bash 只允许只读命令，例如 `rg`、`grep`、`find`、`ls`、`head`、`tail`、`sed -n`、`wc`。",
                "- 禁止无边界递归扫描整个 bug cache；先看聚焦日志目录和聚焦清单，再按需扩展。",
                "- 原始 bug 链接已经结构化，不需要重复复述链接。",
            ]
        )
        context_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        return context_path
    def _build_custom_skill_agent_prompt(
        self,
        *,
        analysis_kind: str,
        skill_name: str,
        request_text: str,
        prompt_text: str,
        title: str,
        description: str,
        fault_time: str,
        selected_input: Path | None,
        prepared_input: Path | None,
        source_evidence_path: Path | None,
        analysis_markdown_path: Path,
        context_path: Path | None,
        writes_output_file: bool,
        prior_findings: list[tuple[str, str, str]] | None = None,
    ) -> str:
        sanitized_request = self._sanitize_file_agent_text(prompt_text or request_text) or "未提供"
        lines = [
            f"用户请求: {sanitized_request}",
            f"Bug 标题: {title.strip() or '未提供'}",
            f"故障时间: {fault_time or '未识别'}",
            "",
            "请执行专用 Skill 的实际源码/证据分析，而不是做最终总结。",
            "",
            "执行入口：",
            f"- 必须先读取 `{context_path}`，里面包含日志路径、Skill 目录、源码路径和前序分析结论。" if context_path else "- 必须先读取当前分析目录中的上下文文件。",
            f"- 默认只在 `{prepared_input}` 内检索日志。" if prepared_input else "- 默认只在上下文列出的日志范围内检索。",
            f"- 命中 Skill: `{skill_name}`（分析类型：`{analysis_kind}`）。",
            "",
        ]
        priority_files = self._domain_priority_files(skill_name)
        if priority_files:
            lines.append("优先源码文件：")
            for path in priority_files:
                lines.append(f"- `{path}`")
            lines.extend(["- 先读取这些文件，不要先对整个源码仓做无边界搜索。", ""])
        if prior_findings:
            lines.append("前序分析结论（已完成的其他 Skill 分析，供参考）：")
            for label, kind, verdict in prior_findings:
                lines.append(f"- {label}: {verdict}")
            lines.extend(["", "请在以上结论基础上，做源码级深入分析，补充日志和源码证据。", ""])
        emit_node_status = _kind_spec(analysis_kind).is_source_stage
        output_rule = (
            "3. 输出中文 Markdown，不要输出 HTML；本轮 prompt 明确要求结构化 JSON fenced block，允许在末尾输出该 JSON 代码块。除此之外不要输出额外解释。"
            if _prompt_requires_json_fence(prompt_text)
            else "3. 输出中文 Markdown，不要输出代码块围栏或额外解释。"
        )
        node_status_rule = (
            "9. 在正文之后追加一个 json 围栏(```json ... ```)，内容为 "
            "{\"node_status\": {\"<源码文件名>\": \"ok|suspect|broken|unknown\"}, "
            "\"findings\": [{\"file\":..., \"severity\":..., \"title\":...}], "
            "\"verdict\": {\"status\":\"ok|broken|inconclusive\", \"headline\":\"一句话结论\", \"next_step\":\"下一步建议\"}}，"
            "标注每个链路节点是否打通，并在 verdict 给出整体结论。"
            if emit_node_status
            else None
        )
        lines.extend(
            [
                "硬性要求：",
                "1. 必须先读取上下文文件、SKILL.md、可用源码证据和必要输入材料；不能只根据标题/描述直接下根因结论。",
                "2. 只读分析，不修改文件，不生成无证据结论。",
                output_rule,
                "4. Markdown 必须包含这些二级标题：`## 结论摘要`、`## 关键证据`、`## 最可能原因`、`## 待确认项`、`## 建议动作`。",
                "5. `## 关键证据` 必须非空，每条证据要能回指到日志/源码/工具结果；证据不足时明确写待确认，不要编造。",
                "6. 不要重复输出 bug 链接；该信息已经结构化。",
                "7. 不要无边界递归扫描整个日志树；先看上下文列出的聚焦日志目录和清单，再按需扩展。",
                "8. 分析完成后必须输出最终 Markdown 正文，不能只执行工具调用而不输出结论。",
                *([node_status_rule] if node_status_rule else []),
                "",
                "检索预算：",
                *[f"- {rule}" for rule in self._file_agent_search_budget_rules()],
                *[f"- {rule}" for rule in self._file_agent_codegraph_rules()],
                "",
            ]
        )
        if writes_output_file:
            lines.extend(
                [
                    "输出路径：",
                    f"- {analysis_markdown_path.name}: `{analysis_markdown_path}`",
                    "",
                ]
            )
        else:
            lines.extend(
                [
                    "输出保存：",
                    f"- Bridge 会把你的标准输出保存到 `{analysis_markdown_path}`",
                    "- 请务必在所有工具调用完成后，输出完整的 Markdown 正文。",
                    "",
                ]
            )
        lines.extend(
            [
                "",
                f"请开始只读分析，并只输出可直接保存为 `{analysis_markdown_path.name}` 的正文内容。",
            ]
        )
        return "\n".join(lines).strip() + "\n"
    def build_codex_app_server_execution_policy(
        self, *, cwd: Path, timeout: int, model_override: str = "",
    ) -> CodexAppServerExecutionPolicy:
        options = self.config.codex_app_server
        env = build_internal_network_env(self.config.internal_network_env)
        if options.preserve_proxy_env:
            env = self._merge_codex_app_server_proxy_env(env)
        env.setdefault("RUST_LOG", "warn")
        codex_home: Path | None = None
        if options.use_minimal_home:
            codex_home = self._prepare_codex_app_server_minimal_home(model_override=model_override)
            if codex_home is not None:
                env["CODEX_HOME"] = str(codex_home)
        # Minimal home has no node_repl section, so the disable flag is only
        # meaningful (and only emitted) when running against the real home.
        emit_node_repl_flag = options.disable_node_repl and codex_home is None
        return CodexAppServerExecutionPolicy(
            env=env,
            codex_home=codex_home,
            cwd=Path(cwd),
            disable_node_repl=options.disable_node_repl,
            emit_node_repl_flag=emit_node_repl_flag,
        )
    def _prepare_codex_app_server_minimal_home(self, *, run_id: str = "", model_override: str = "") -> Path | None:
        source_home = Path.home() / ".codex"
        if not (source_home / "auth.json").exists():
            return None
        base = self.config.data_dir / "codex_app_server_home"
        template = base / "template"
        template.mkdir(parents=True, exist_ok=True)
        for name in ("auth.json", "installation_id", "models_cache.json"):
            source = source_home / name
            if not source.exists():
                continue
            try:
                shutil.copy2(source, template / name)
            except OSError:
                continue
        config_lines = ["[analytics]", "enabled = false", ""]
        model = (model_override or self.config.codex_app_server.model).strip()
        if model:
            # Empty model => omit the line so Codex uses its own default.
            config_lines = [f'model = "{model}"', ""] + config_lines
        config_lines.extend(self._codex_app_server_codegraph_mcp_config_lines())
        try:
            (template / "config.toml").write_text("\n".join(config_lines), encoding="utf-8")
        except OSError:
            return None
        runs_dir = base / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        self._sweep_stale_codex_app_server_runs(runs_dir)
        run_home = runs_dir / (run_id or uuid.uuid4().hex[:12])
        try:
            shutil.rmtree(run_home, ignore_errors=True)
            shutil.copytree(template, run_home)
        except OSError:
            return template
        return run_home
    def _codex_app_server_codegraph_mcp_config_lines(self) -> list[str]:
        si_opts = self.config.source_investigation
        if not si_opts.codegraph_enabled:
            return []
        roots: list[Path] = []
        for root in si_opts.repo_roots or []:
            try:
                resolved = root.expanduser().resolve()
            except OSError:
                continue
            if resolved.exists() and resolved not in roots:
                roots.append(resolved)
        if not roots:
            return []

        repo_root = Path(__file__).resolve().parents[3]
        python_path = str(repo_root)
        inherited_python_path = os.environ.get("PYTHONPATH", "").strip()
        if inherited_python_path:
            python_path = os.pathsep.join([python_path, inherited_python_path])
        command = (si_opts.codegraph_command or "codegraph").strip() or "codegraph"
        timeout = str(max(1.0, float(si_opts.codegraph_timeout_seconds or 10.0)))
        return [
            "",
            "[mcp_servers.bridge_codegraph]",
            f"command = {_toml_string(sys.executable)}",
            f"args = {_toml_array(['-m', 'lark_agent_bridge.mcp_codegraph_server'])}",
            "startup_timeout_sec = 30",
            "",
            "[mcp_servers.bridge_codegraph.env]",
            f"PYTHONPATH = {_toml_string(python_path)}",
            f"LARK_AGENT_BRIDGE_CODEGRAPH_COMMAND = {_toml_string(command)}",
            f"LARK_AGENT_BRIDGE_CODEGRAPH_TIMEOUT_SECONDS = {_toml_string(timeout)}",
            f"LARK_AGENT_BRIDGE_CODEGRAPH_ROOTS = {_toml_string(chr(10).join(str(root) for root in roots))}",
        ]
    def _merge_codex_app_server_proxy_env(self, env: dict[str, str]) -> dict[str, str]:
        merged = dict(env)
        for key in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "NO_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
            "no_proxy",
        ):
            value = os.environ.get(key)
            if value:
                merged[key] = value
        return merged
    def _should_use_codex_app_server_for_file_agent(self, provider: str) -> bool:
        options = self.config.codex_app_server
        return (
            options.enabled
            and options.use_for_file_agent
            and _normalize_provider_name(provider) == "codex"
        )
    def _write_app_server_event_audit(self, path: Path, events: list[dict[str, object]]) -> None:
        lines = [json.dumps(event, ensure_ascii=False) for event in events]
        path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    def _run_custom_skill_agent_via_codex_app_server(
        self,
        *,
        analysis_kind: str,
        skill_name: str,
        prompt_text: str,
        cwd: Path,
        command_path: Path,
        stdout_path: Path,
        stderr_path: Path,
        events_path: Path,
        progress_callback: Callable[[dict[str, object]], None] | None,
        timeout: int,
        bridge_session_id: str,
        model_override: str = "",
        reasoning_effort_override: str = "",
    ) -> dict[str, object]:
        options = self.config.codex_app_server
        ok, version_or_error = check_codex_app_server_available(options.command, options.min_version)
        if not ok:
            return {
                "ok": False,
                "error_code": "codex_app_server_unavailable",
                "message": f"Codex app-server 不可用：{version_or_error}",
                "executor": "codex_app_server",
                "provider": "codex",
                "command": [options.command, "app-server"],
                "stdout": "",
                "stderr": version_or_error,
                "usage": {},
                "thread_id": "",
                "turn_id": "",
                "events": [],
                "events_path": events_path,
                "duration_seconds": 0.0,
                "bridge_session_id": bridge_session_id,
            }

        policy = self.build_codex_app_server_execution_policy(cwd=cwd, timeout=timeout, model_override=model_override)
        subprocess_env = policy.env
        runtime = CodexAppServerRuntime(
            command=options.command,
            cwd=policy.cwd,
            startup_timeout_seconds=options.startup_timeout_seconds,
            turn_timeout_seconds=min(float(timeout), options.turn_timeout_seconds),
            post_tool_quiet_timeout_seconds=options.post_tool_quiet_timeout_seconds,
            no_event_timeout_seconds=options.no_event_timeout_seconds,
            notification_poll_seconds=options.notification_poll_seconds,
            max_event_audit=options.max_event_audit,
            sandbox_mode=options.sandbox_mode,
            disable_node_repl=policy.disable_node_repl,
            emit_node_repl_flag=policy.emit_node_repl_flag,
            disable_analytics=options.disable_analytics,
            disable_memories=options.disable_memories,
            disable_apps_feature=options.disable_apps_feature,
            disable_plugins_feature=options.disable_plugins_feature,
            disable_computer_use_feature=options.disable_computer_use_feature,
            reasoning_effort=(reasoning_effort_override or options.reasoning_effort),
            env=subprocess_env,
        )

        delta_chunks: list[str] = []
        delta_start_at = 0.0

        def _flush_delta() -> None:
            nonlocal delta_chunks, delta_start_at
            if not delta_chunks:
                return
            combined = "".join(delta_chunks).strip()
            delta_chunks = []
            delta_start_at = 0.0
            if not combined:
                return
            preview = _app_server_delta_preview(combined)
            if not preview:
                return
            self._emit_progress(
                progress_callback,
                stage=f"{analysis_kind}_agent_analysis_stream",
                message=f"Codex app-server: {preview}",
                provider="codex",
                stream_preview=preview,
                stream_kind="delta_group",
            )

        def _stream_event(event: dict[str, object]) -> None:
            nonlocal delta_start_at
            method = str(event.get("method") or "")
            params = event.get("params") or {}
            if not isinstance(params, dict):
                params = {}
            if method == "item/agentMessage/delta":
                delta = str(params.get("delta") or "")
                if delta:
                    if not delta_chunks:
                        delta_start_at = time.monotonic()
                    delta_chunks.append(delta)
                    if _app_server_delta_should_flush("".join(delta_chunks), started_at=delta_start_at):
                        _flush_delta()
                return
            _flush_delta()
            preview = app_server_event_preview(event)
            if not preview:
                return
            self._emit_progress(
                progress_callback,
                stage=f"{analysis_kind}_agent_analysis_stream",
                message=f"Codex app-server: {preview}",
                provider="codex",
                stream_preview=preview,
            )

        result = runtime.run_turn(prompt_text, on_event=_stream_event if progress_callback is not None else None)
        _flush_delta()
        if policy.codex_home is not None and policy.codex_home.parent.name == "runs":
            shutil.rmtree(policy.codex_home, ignore_errors=True)
        stdout_path.write_text(result.stdout, encoding="utf-8")
        stderr_path.write_text(result.stderr, encoding="utf-8")
        self._write_app_server_event_audit(events_path, result.events)
        command_path.write_text(json.dumps(result.command, ensure_ascii=False, indent=2), encoding="utf-8")
        if progress_callback is not None:
            summary_preview = result.stdout.splitlines()[0] if result.stdout.strip() else ""
            self._emit_progress(
                progress_callback,
                stage=f"{analysis_kind}_agent_analysis_stream",
                message=f"Codex app-server result: {summary_preview or ('ok' if result.ok else result.error_code or 'failed')}",
                provider="codex",
                stream_preview=summary_preview,
            )
        return {
            "ok": result.ok,
            "error_code": result.error_code,
            "message": result.error,
            "final_text": result.final_text,
            "executor": "codex_app_server",
            "provider": "codex",
            "command": result.command,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "usage": normalize_token_usage(result.usage) or dict(result.usage),
            "thread_id": result.thread_id,
            "turn_id": result.turn_id,
            "events": result.events,
            "events_path": events_path,
            "duration_seconds": result.duration_seconds,
            "should_retire": result.should_retire,
            "completion_state": result.completion_state.value,
            "app_server_version": version_or_error,
        }
    def _sweep_stale_codex_app_server_runs(self, runs_dir: Path, max_age_seconds: float = 3600.0) -> None:
        now = time.time()
        try:
            children = list(runs_dir.iterdir())
        except OSError:
            return
        for child in children:
            try:
                if child.is_dir() and (now - child.stat().st_mtime) > max_age_seconds:
                    shutil.rmtree(child, ignore_errors=True)
            except OSError:
                continue


def _app_server_delta_preview(text: str, *, max_chars: int = 220) -> str:
    normalized = " ".join((text or "").replace("\n", " ").split()).strip()
    if not normalized:
        return ""
    if len(normalized) > max_chars:
        normalized = normalized[: max_chars - 1].rstrip() + "..."
    return f"Codex 文本 {normalized}"


def _app_server_delta_should_flush(text: str, *, started_at: float, max_chars: int = 180) -> bool:
    if not text:
        return False
    if len(text) >= max_chars:
        return True
    if text.endswith(("\n", "。", "！", "？", ".", "!", "?")) and len(text.strip()) >= 24:
        return True
    if started_at and (time.monotonic() - started_at) >= 1.2 and len(text.strip()) >= 48:
        return True
    return False


def _prompt_requires_json_fence(prompt_text: str) -> bool:
    normalized = (prompt_text or "").casefold()
    return "```json" in normalized or ("json" in normalized and "fenced block" in normalized)


def _toml_string(value: object) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _toml_array(values: list[object]) -> str:
    return "[" + ", ".join(_toml_string(value) for value in values) + "]"
