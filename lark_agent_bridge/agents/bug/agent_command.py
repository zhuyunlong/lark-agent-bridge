from __future__ import annotations

from ._shared import *  # noqa: F401,F403


_FOCUS_LOOKBACK_SECONDS = 3600
_FOCUS_FORWARD_BUFFER_SECONDS = 300
_FOCUS_MAX_LINES_PER_FILE = 15000
_FOCUS_MIN_LINES_PER_FILE = 2000
_FOCUS_MAX_LOOKBACK_SECONDS = 21600
_FOCUS_EXPAND_STEP_SECONDS = 3600
_FOCUS_LOGD_PREFIXES = ("kernel", "main", "events", "crash")


class _AgentCommandMixin:
    """文件 Agent 命令与运行环境：命令行组装、工具/源码根、检索与 codegraph 规则、附加目录与工作目录（与 CustomSkillMixin 共享 self 状态）。"""

    def _custom_skill_agent_tools(self) -> list[str]:
        tools = list(self.config.claude_agent.allowed_tools or ["Read", "Grep", "Glob", "LS"])
        # Grep already covers repo-wide text search; add Bash only for targeted
        # read-only shell probes when file listings or one-off counts are needed.
        if "Bash" not in tools:
            tools.append("Bash")
        return tools
    def _custom_skill_agent_source_roots(self) -> list[Path]:
        roots = list(self.config.source_investigation.repo_roots or [])
        if self.config.guideengine_repo not in roots:
            roots.append(self.config.guideengine_repo)
        resolved: list[Path] = []
        for root in roots:
            try:
                candidate = root.expanduser().resolve()
            except OSError:
                continue
            if candidate.exists() and candidate not in resolved:
                resolved.append(candidate)
        return resolved
    def _file_agent_log_rules(self, analysis_kind: str) -> list[str]:
        rules = [
            "App 主日志通常位于 `data/Log/log*/app/<package>/<prefix>_YYYY-MM-DD_HH-MM.alog(.log)`，如 `main_...` 或 `user0_main_...`；前缀不固定，时间段格式固定。",
            "若同时存在解码后的 `.alog.log` / `.xlog.log` 与原始 `.alog` / `.xlog`，优先读取解码后的 `.log`。",
            "排查时优先围绕故障时间前后 1 小时内的日志，不要先全量递归扫描整个缓存树。",
            "优先读取 bridge 预先收敛后的 `log_focus/` 和 `log_focus.md`，只有证据不足时才扩展到更多日志文件。",
            "故障时间是分析门槛：故障时刻之后的日志只能用于了解后续表现，不得作为根因判断或源码归因的证据；归因必须基于故障时刻及之前的状态。",
            "log_focus/ 内的日志已按故障时间窗裁剪过，行数有限是正常的；不要因为「行数少」就回到原始日志目录重新全量搜索。",
        ]
        return rules
    def _file_agent_search_budget_rules(self) -> list[str]:
        return [
            "不要在整个源码根目录直接执行无边界 `rg`；先用上下文、Skill、预检索证据把范围收敛到具体模块或 1-3 个候选目录。",
            "所有可能命中很多结果的 `rg` / `grep` 必须加输出预算，例如 `rg --max-count 80 ... <dir>` 或 `rg ... <dir> | head -80`。",
            "如果一次检索返回超过 80 行或明显命中无关模块，停止阅读大段输出，改用更窄关键词、`--glob`、文件名或子目录重查。",
            "日志扩展也必须先限定包名、时间窗和关键词；不要对整个日志缓存做无边界递归搜索。",
            "每轮最多保留最有价值的少量源码/日志锚点，优先读具体文件行号，再产出结论；不要把大段检索结果当作分析正文。",
            "只在 `log_focus/` 目录内检索日志；非必要不要回读原始 `bug_cache/.../logs` 全量日志，尤其禁止对原始 `logd/main.txt` / `main.txt.01` 做无时间窗的 `rg` / `sed`。",
            "源码定位禁止 `rg --files <整个源码根>` 全树枚举；先读领域优先文件，再按符号名精确 `rg --max-count`。",
        ]
    def _build_custom_skill_agent_command(
        self,
        *,
        analysis_kind: str = "source_code_skill",
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
        context_path: Path | None = None,
        debug_log_path: Path | None = None,
        provider_override: str = "",
        command_override: str = "",
        prior_findings: list[tuple[str, str, str]] | None = None,
    ) -> dict[str, object]:
        provider = _normalize_provider_name(provider_override or self.config.bug_analysis.provider)
        if command_override.strip():
            command_name = command_override.strip()
        elif provider_override:
            command_name = _default_command_for_provider(provider)
        else:
            command_name = (self.config.bug_analysis.command or "").strip() or _default_command_for_provider(provider)
        if not provider or not command_name:
            detected_provider, detected_command = _detect_available_provider()
            if detected_provider and detected_command:
                provider = provider or detected_provider
                command_name = command_name or detected_command
        prompt = self._build_custom_skill_agent_prompt(
            analysis_kind=analysis_kind,
            skill_name=skill_name,
            request_text=request_text,
            prompt_text=prompt_text,
            title=title,
            description=description,
            fault_time=fault_time,
            selected_input=selected_input,
            prepared_input=prepared_input,
            source_evidence_path=source_evidence_path,
            analysis_markdown_path=analysis_markdown_path,
            context_path=context_path,
            writes_output_file=provider == "codex",
            prior_findings=prior_findings,
        )
        if not provider or not command_name:
            return {"command": [], "provider": provider, "prompt": prompt, "output_path": analysis_markdown_path}
        if provider == "codex":
            model = (self.config.bug_analysis.model or "").strip()
            working_dir = self._custom_skill_agent_cwd()
            command = [
                command_name,
                "exec",
                "--skip-git-repo-check",
                "-s",
                "read-only",
                "-C",
                str(working_dir),
            ]
            if model:
                command.extend(["-m", model])
            command.extend(["--json", "--output-last-message", str(analysis_markdown_path), prompt])
            return {
                "command": command,
                "provider": provider,
                "model": model,
                "prompt": prompt,
                "output_path": analysis_markdown_path,
                "output_mode": "tool_written_file",
                "cwd": working_dir,
            }
        if provider in {"claude", "claude-code", "claude_code"}:
            model = (self.config.claude_agent.model or "").strip()
            allowed_tools = self._custom_skill_agent_tools()
            command = [
                command_name,
                "--print",
                "--output-format",
                "json",
                "--no-session-persistence",
                "--disable-slash-commands",
                "--permission-mode",
                "bypassPermissions",
                "--tools",
                ",".join(allowed_tools),
                "--allowedTools",
                ",".join(allowed_tools),
                "--append-system-prompt",
                (
                    "你是通过飞书触发的专用 Skill 执行 Agent。"
                    "你负责读取本地日志、源码和 SKILL.md 产出执行证据，不负责跳过证据直接总结。"
                    "只读分析，不修改任何文件。输出中文 Markdown。"
                    "在所有工具调用完成后，必须输出一段完整的中文 Markdown 分析报告正文。"
                ),
            ]
            if debug_log_path is not None:
                command.extend(["--debug-file", str(debug_log_path)])
            command.extend(["--add-dir", str(analysis_markdown_path.parent)])
            for directory in self._custom_skill_agent_add_dirs(
                skill_name=skill_name,
                selected_input=selected_input,
                prepared_input=prepared_input,
                source_evidence_path=source_evidence_path,
                context_path=context_path,
            ):
                command.extend(["--add-dir", str(directory)])
            return {
                "command": command,
                "provider": provider,
                "model": model,
                "prompt": prompt,
                "output_path": analysis_markdown_path,
                "output_mode": "stdout_json",
                "cwd": analysis_markdown_path.parent,
                "input_mode": "stdin",
            }
        return {"command": [], "provider": provider, "prompt": prompt, "output_path": analysis_markdown_path}
    def _custom_skill_agent_add_dirs(
        self,
        *,
        skill_name: str,
        selected_input: Path | None,
        prepared_input: Path | None,
        source_evidence_path: Path | None,
        context_path: Path | None,
    ) -> list[Path]:
        dirs: list[Path] = []
        for path in [
            *self._custom_skill_agent_source_roots(),
            *self._skill_context_paths(skill_name),
            selected_input,
            prepared_input,
            source_evidence_path,
            context_path,
        ]:
            if path is None:
                continue
            candidate = path if path.is_dir() else path.parent
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            if resolved not in dirs and resolved.exists():
                dirs.append(resolved)
        return dirs
    def _custom_skill_agent_cwd(self) -> Path:
        source_roots = self._custom_skill_agent_source_roots()
        if source_roots:
            return source_roots[0]
        return self._working_dir()
    def _domain_priority_files(self, context_profile: str) -> list[Path]:
        normalized = context_profile.strip()
        if normalized != "scene-signal-diagnosis":
            return []
        guideengine_root: Path | None = None
        napa5_root: Path | None = None
        for root in self._custom_skill_agent_source_roots():
            name = root.name.casefold()
            if name == "napa5":
                napa5_root = root
            elif "guideengine" in name or root == self.config.guideengine_repo:
                guideengine_root = root
        candidates: list[Path] = []
        if guideengine_root is not None:
            candidates.extend(
                [
                    guideengine_root / "module_display/launcher_subreality_service/src/main/java/com/xiaopeng/ainavi/scene/UnitySceneTypeService.kt",
                    guideengine_root / "module_display/launcher_subreality_service/src/main/java/com/xiaopeng/ainavi/scene/UnitySceneTypeRepository.kt",
                    guideengine_root / "module_display/launcher_subreality_service/src/main/java/com/xiaopeng/ainavi/unity/UnityAdapter.kt",
                    guideengine_root / "module_floorcenter/module_datacenter/src/main/java/com/xiaopeng/guideengine/helper/xuimanager/XuiConditionHelper.kt",
                ]
            )
        if napa5_root is not None:
            candidates.extend(
                [
                    napa5_root / "Assets/LocalModules/module-napa5-hmi/Runtime/Scripts/Common/Service/Proxy/SystemServiceProxy.cs",
                    napa5_root / "Assets/LocalModules/module-napa5-hmi/Runtime/Scripts/App/Displays/Nodes/HUSceneNode.cs",
                    napa5_root / "Assets/LocalModules/module-napa5-hmi/Runtime/Scripts/XSRSceneManager/Logic/XSRSceneStateMachine.cs",
                ]
            )
        return [path for path in candidates if path.exists()]
    def _file_agent_codegraph_rules(self, source_roots: list[Path] | None = None) -> list[str]:
        si_opts = self.config.source_investigation
        if not si_opts.codegraph_enabled:
            return []
        command = (si_opts.codegraph_command or "codegraph").strip() or "codegraph"
        roots = source_roots if source_roots is not None else self._custom_skill_agent_source_roots()
        roots_hint = "、".join(f"`{path}`" for path in roots[:3]) if roots else "`<源码根>`"
        return [
            f"可用源码根: {roots_hint}；若可见 `bridge_codegraph` MCP 工具，优先调用 `codegraph_status`、`search_codegraph`、`get_callers`、`get_code_context`。",
            f"若 MCP 工具不可用，再用 CLI fallback: `{command} status <源码根>`；sandbox 下 status 失败只表示 CLI 受限，不代表宿主索引一定不可用。",
            f"CLI 按符号/信号名定位入口: `{command} query \"<类名/函数名/信号名>\" --path <源码根> --json --limit 20`。",
            f"CLI 追调用方: `{command} callers \"<符号名>\" --path <源码根> --json --limit 20`。",
            f"CLI 需要全局入口点时: `{command} context \"<分析目标>\" --path <源码根> --format json --no-code --max-nodes 30`。",
            "CodeGraph 只用于收敛候选文件和调用链；最终证据仍要回到具体源码文件+行号，不要把完整 JSON 大段贴入结论。",
        ]
