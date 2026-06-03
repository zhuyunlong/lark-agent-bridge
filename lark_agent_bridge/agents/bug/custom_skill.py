from __future__ import annotations

from ._shared import *  # noqa: F401,F403


class _CustomSkillMixin:
    def _time_match_note(self, fault_time: str, sessions: object) -> str:
        if not fault_time:
            return "未识别故障时间，无法校验附件日志是否匹配。"
        if not isinstance(sessions, list) or not sessions:
            return "报告未产出会话，无法校验附件日志是否匹配。"
        fault_hour = fault_time[:13]
        for session in sessions:
            if not isinstance(session, dict):
                continue
            start = str(session.get("start", ""))
            if start.startswith(fault_hour):
                return f"附件日志中命中了故障小时 `{fault_hour}`。"
        starts = [str(session.get("start", "")) for session in sessions if isinstance(session, dict)]
        preview = "、".join(starts[:3]) if starts else "无"
        return f"附件日志未命中故障小时 `{fault_hour}`，实际捕获到的启动会话起点示例：{preview}"

    def _failure(
        self,
        *,
        context,
        command: list[str],
        started: float,
        message: str,
        error_code: str,
        stdout: str = "",
        stderr: str = "",
        progress_callback: Callable[[dict[str, object]], None] | None = None,
        details: dict[str, object] | None = None,
    ) -> TaskResult:
        self._emit_progress(
            progress_callback,
            stage="bug_failed",
            message=message,
            job_id=context.job_id,
            error_code=error_code,
        )
        payload_details = {"mode": "bug_analysis"}
        if isinstance(details, dict):
            payload_details.update(details)
        return TaskResult(
            success=False,
            message=message,
            job_id=context.job_id,
            job_dir=context.job_dir,
            command=command,
            duration_seconds=time.monotonic() - started,
            error_code=error_code,
            stdout=stdout,
            stderr=stderr,
            details=payload_details,
        )

    def _custom_skill_executor_not_ready_message(self, skill_name: str, *, selected_input: Path | None) -> str:
        display_name = skill_name.strip() or "source_code_skill"
        log_note = f"\n日志输入已准备：`{selected_input}`" if selected_input else ""
        route_note = ""
        try:
            record = self.skill_manager.get_skill(display_name, include_content=False)
        except Exception:
            record = None
        if record is None:
            route_note = "\n当前状态：未找到 Skill 目录或主路由记录。"
        elif record.kind not in _SOURCE_SKILL_KINDS:
            route_note = f"\n当前状态：已配置主路由，但 kind=`{record.kind or '未配置'}`，不是 source_code_skill。"
        elif not record.executor:
            route_note = "\n当前状态：已配置为 source_code_skill，但 executor 为空；需要配置 executor=`file_agent`。"
        elif record.executor != "file_agent":
            route_note = f"\n当前状态：已配置 executor=`{record.executor}`，但当前只支持 file_agent。"
        return (
            f"已命中专用 Skill `{display_name}`，但当前没有可执行源码分析器，尚未执行实际日志分析。"
            f"{route_note}{log_note}\n不会基于占位报告给出根因结论。请为该 Skill 配置文件 Agent 执行器后重试。"
        )

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

    def _sanitize_file_agent_text(self, text: str) -> str:
        sanitized = re.sub(r"https?://\S+", "", text or "")
        sanitized = re.sub(r"@[^\s，。；、]+", "", sanitized)
        sanitized = re.sub(r"\s+", " ", sanitized).strip()
        return sanitized

    def _summarize_prior_report_jsons(
        self,
        report_jsons: dict[str, Path | None],
    ) -> list[tuple[str, str, str]]:
        summaries: list[tuple[str, str, str]] = []
        for kind, json_path in report_jsons.items():
            if json_path is None or not json_path.exists():
                continue
            try:
                data = json.loads(json_path.read_text(encoding="utf-8", errors="replace"))
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(data, dict):
                continue
            verdict = data.get("verdict")
            if isinstance(verdict, dict):
                verdict_text = str(verdict.get("msg") or verdict.get("text") or "")
                sev = str(verdict.get("sev") or "")
            else:
                verdict_text = str(verdict or "")
                sev = ""
            label = self._analysis_label(kind)
            if verdict_text:
                summaries.append((label, kind, f"[{sev}] {verdict_text}" if sev else verdict_text))
        return summaries

    def _extract_text_from_agent_json(self, raw_stdout: str, analysis_markdown_path: Path) -> str:
        if not raw_stdout.strip():
            return ""
        try:
            data = json.loads(raw_stdout)
        except json.JSONDecodeError:
            text = raw_stdout.strip()
            if text:
                analysis_markdown_path.write_text(text + "\n", encoding="utf-8")
            return text
        if not isinstance(data, dict):
            text = raw_stdout.strip()
            if text:
                analysis_markdown_path.write_text(text + "\n", encoding="utf-8")
            return text
        result = str(data.get("result") or "").strip()
        if result:
            analysis_markdown_path.write_text(result + "\n", encoding="utf-8")
            return result
        text_parts: list[str] = []
        for block in data.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                text_parts.append(str(block.get("text") or ""))
        if text_parts:
            text = "\n\n".join(text_parts).strip()
            analysis_markdown_path.write_text(text + "\n", encoding="utf-8")
            return text
        return ""

    def _extract_partial_markdown_from_agent_stream(self, raw_stdout: str, *, skill_name: str) -> str:
        if not raw_stdout.strip():
            return ""
        messages: list[str] = []
        seen: set[str] = set()
        for line in raw_stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            item = payload.get("item")
            if not isinstance(item, dict) or item.get("type") != "agent_message":
                continue
            text = str(item.get("text") or "").strip()
            if len(text) < 20 or text in seen:
                continue
            seen.add(text)
            messages.append(text)
        if not messages:
            return ""
        bullets = messages[-3:]
        lines = ["## 阶段性结论（超时前）"]
        lines.extend(f"- {text}" for text in bullets)
        lines.extend(
            [
                "",
                "## 当前缺口",
                f"- 专用 Skill `{skill_name}` 文件 Agent 在整理最终关键证据前超时，未生成完整的 `source_stage_analysis.md`。",
                "",
                "## 建议动作",
                "- 优先复用以上阶段性结论继续追问，或放宽/替换当前文件 Agent 路径后重跑源码阶段。",
            ]
        )
        return "\n".join(lines).strip()

    def _file_agent_log_rules(self, analysis_kind: str) -> list[str]:
        rules = [
            "App 主日志通常位于 `data/Log/log*/app/<package>/<prefix>_YYYY-MM-DD_HH-MM.alog(.log)`，如 `main_...` 或 `user0_main_...`；前缀不固定，时间段格式固定。",
            "若同时存在解码后的 `.alog.log` / `.xlog.log` 与原始 `.alog` / `.xlog`，优先读取解码后的 `.log`。",
            "排查时优先围绕故障时间前后 1 小时内的日志，不要先全量递归扫描整个缓存树。",
            "优先读取 bridge 预先收敛后的 `log_focus/` 和 `log_focus.md`，只有证据不足时才扩展到更多日志文件。",
        ]
        return rules

    def _file_agent_search_budget_rules(self) -> list[str]:
        return [
            "不要在整个源码根目录直接执行无边界 `rg`；先用上下文、Skill、预检索证据把范围收敛到具体模块或 1-3 个候选目录。",
            "所有可能命中很多结果的 `rg` / `grep` 必须加输出预算，例如 `rg --max-count 80 ... <dir>` 或 `rg ... <dir> | head -80`。",
            "如果一次检索返回超过 80 行或明显命中无关模块，停止阅读大段输出，改用更窄关键词、`--glob`、文件名或子目录重查。",
            "日志扩展也必须先限定包名、时间窗和关键词；不要对整个日志缓存做无边界递归搜索。",
            "每轮最多保留最有价值的少量源码/日志锚点，优先读具体文件行号，再产出结论；不要把大段检索结果当作分析正文。",
        ]

    def _file_agent_focus_candidates(
        self,
        *,
        input_path: Path | None,
        fault_time: str,
        analysis_kind: str,
    ) -> list[Path]:
        if input_path is None or not input_path.exists():
            return []
        if input_path.is_file():
            return [input_path]
        fault_dt = self._parse_bug_datetime(fault_time)
        all_files = self._iter_log_coverage_files(input_path)
        if not all_files:
            return []
        selected: list[Path] = []
        decoded_aux: list[Path] = []
        for path in all_files:
            if path.name in {"prop.txt", "dfx.txt"}:
                decoded_aux.append(path)
                continue
            file_dt = self._parse_log_file_datetime(path.name)
            if fault_dt is None or file_dt is None:
                continue
            candidate_dt = datetime.fromtimestamp(time.mktime(file_dt))
            if abs((candidate_dt - fault_dt).total_seconds()) > 3600:
                continue
            selected.append(path)
        if not selected and fault_dt is not None:
            for path in all_files:
                file_dt = self._parse_log_file_datetime(path.name)
                if file_dt is None:
                    continue
                candidate_dt = datetime.fromtimestamp(time.mktime(file_dt))
                if abs((candidate_dt - fault_dt).total_seconds()) <= 3600:
                    selected.append(path)
        ranked = sorted(
            {path.resolve() for path in [*decoded_aux, *selected]},
            key=lambda item: (self._log_file_priority(item), str(item)),
        )
        return ranked[:24]

    def _build_file_agent_focus_dir(
        self,
        *,
        input_path: Path | None,
        fault_time: str,
        analysis_kind: str,
        analysis_dir: Path,
    ) -> tuple[Path | None, Path | None, list[Path]]:
        candidates = self._file_agent_focus_candidates(
            input_path=input_path,
            fault_time=fault_time,
            analysis_kind=analysis_kind,
        )
        if not candidates:
            return input_path, None, []
        focus_dir = analysis_dir / "log_focus"
        manifest_path = analysis_dir / "log_focus.md"
        focus_dir.mkdir(parents=True, exist_ok=True)
        copied: list[Path] = []
        for source in candidates:
            if input_path is not None and input_path.exists() and input_path.is_dir():
                try:
                    relative = source.relative_to(input_path)
                except ValueError:
                    relative = Path(source.name)
            else:
                relative = Path(source.name)
            dest = focus_dir / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(source, dest)
            except OSError:
                continue
            copied.append(dest)
        lines = [
            "# Log Focus",
            "",
            f"- 原始日志输入: `{input_path}`" if input_path else "- 原始日志输入: 未提供",
            f"- 聚焦目录: `{focus_dir}`",
            f"- 故障时间: `{fault_time or '未识别'}`",
            "",
            "## 已挑选文件",
        ]
        if copied:
            lines.extend(f"- `{path}`" for path in copied)
        else:
            lines.append("- 未复制出聚焦文件，继续使用原始输入目录。")
        manifest_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        return (focus_dir if copied else input_path), manifest_path, copied

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
                "\"findings\": [{\"file\":..., \"severity\":..., \"title\":...}]}，"
                "标注每个链路节点是否打通。"
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
            "\"findings\": [{\"file\":..., \"severity\":..., \"title\":...}]}，"
            "标注每个链路节点是否打通。"
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

    def _trim_reanalysis_reference_text(self, text: str, *, max_chars: int = 600) -> str:
        normalized = self._sanitize_file_agent_text(text)
        if not normalized:
            return ""
        if len(normalized) <= max_chars:
            return normalized
        return normalized[: max_chars - 1].rstrip() + "…"

    def _source_stage_file_agent_timeout(self, *, reference_seconds: float) -> float:
        if self._should_use_codex_app_server_for_file_agent("codex"):
            return max(
                180.0,
                min(
                    float(self.config.codex_app_server.turn_timeout_seconds),
                    float(self.config.bug_analysis.timeout_seconds),
                ),
            )
        return min(
            180.0,
            self._agent_summary_timeout(
                self.config.bug_analysis.timeout_seconds,
                reference_seconds=reference_seconds,
            ),
        )

    def _prefer_source_stage_file_agent(self) -> bool:
        return self._should_use_codex_app_server_for_file_agent("codex")

    def _source_stage_followup_prompt_text(self, *, request_text: str, followup_text: str) -> str:
        normalized_followup = followup_text.strip()
        if normalized_followup:
            return normalized_followup
        return request_text.strip()

    def _source_stage_followup_request_text(self, *, request_text: str, followup_text: str) -> str:
        if self._followup_explicitly_requests_source_analysis(request_text, followup_text):
            normalized_followup = followup_text.strip()
            if normalized_followup:
                return normalized_followup
        return request_text.strip()

    def build_codex_app_server_execution_policy(
        self, *, cwd: Path, timeout: int,
    ) -> CodexAppServerExecutionPolicy:
        options = self.config.codex_app_server
        env = build_internal_network_env(self.config.internal_network_env)
        if options.preserve_proxy_env:
            env = self._merge_codex_app_server_proxy_env(env)
        env.setdefault("RUST_LOG", "warn")
        codex_home: Path | None = None
        if options.use_minimal_home:
            codex_home = self._prepare_codex_app_server_minimal_home()
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

    def _prepare_codex_app_server_minimal_home(self, *, run_id: str = "") -> Path | None:
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
        model = self.config.codex_app_server.model.strip()
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

        policy = self.build_codex_app_server_execution_policy(cwd=cwd, timeout=timeout)
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
            reasoning_effort=options.reasoning_effort,
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
