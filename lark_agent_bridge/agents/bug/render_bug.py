from __future__ import annotations

from ._shared import *  # noqa: F401,F403


class _RenderBugMixin:
    def _collect_source_evidence_by_targets(
        self, *, repo: Path, terms: list[str], timeout: float = 5.0
    ) -> list[tuple[str, int, str]]:
        suffixes = {".kt", ".java", ".cpp", ".cc", ".c", ".h", ".hpp", ".proto", ".xml"}
        ignored_dirs = {".git", ".gradle", ".idea", "build", "out", ".cxx", "node_modules"}
        explicit_files = [term for term in terms if re.search(r"\.(?:kt|java|cpp|cc|c|h|hpp|proto|xml)$", term, re.I)]
        if not explicit_files:
            return []
        explicit_stems = {Path(term).stem for term in explicit_files}
        candidate_paths: list[Path] = []
        seen_paths: set[Path] = set()
        repo_resolved = repo.resolve()
        for term in explicit_files:
            candidate = (repo / term).resolve()
            try:
                candidate.relative_to(repo_resolved)
            except ValueError:
                continue
            if candidate.is_file() and candidate.suffix in suffixes and candidate not in seen_paths:
                seen_paths.add(candidate)
                candidate_paths.append(candidate)
        if timeout > 0 and shutil.which("rg") is not None:
            command = [
                "rg",
                "--files",
                "--color",
                "never",
                "--glob",
                "!.git/**",
                "--glob",
                "!.gradle/**",
                "--glob",
                "!build/**",
                "--glob",
                "!out/**",
                "--glob",
                "!.cxx/**",
            ]
            for term in explicit_files:
                command.extend(["--glob", f"**/{Path(term).name}"])
            command.append(str(repo))
            try:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                completed = None
            if completed is not None and completed.returncode in {0, 1}:
                for path_text in completed.stdout.splitlines():
                    path = Path(path_text)
                    if not path.is_absolute():
                        path = repo / path
                    try:
                        resolved = path.resolve()
                        resolved.relative_to(repo_resolved)
                    except (OSError, ValueError):
                        continue
                    if resolved.is_file() and resolved.suffix in suffixes and resolved not in seen_paths:
                        seen_paths.add(resolved)
                        candidate_paths.append(resolved)
        matches: list[tuple[str, int, str]] = []
        for path in sorted(candidate_paths):
            relative_parts = path.relative_to(repo_resolved).parts
            if any(part in ignored_dirs for part in relative_parts):
                continue
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            rel_path = str(path.relative_to(repo_resolved))
            stem = path.stem
            if path.name not in explicit_files and stem not in explicit_stems:
                continue
            for index, line in enumerate(lines, start=1):
                if stem in line or path.name in line:
                    matches.append((rel_path, index, line.strip()[:300]))
                    if len([item for item in matches if item[0] == rel_path]) >= 3:
                        break
        return matches

    def _merge_source_evidence_matches(
        self,
        primary: list[tuple[str, int, str]],
        extra: list[tuple[str, int, str]],
    ) -> list[tuple[str, int, str]]:
        merged: list[tuple[str, int, str]] = []
        seen: set[tuple[str, int, str]] = set()
        for item in [*primary, *extra]:
            if item in seen:
                continue
            seen.add(item)
            merged.append(item)
            if len(merged) >= 80:
                break
        return merged

    def _collect_source_evidence_with_rg(
        self, *, repo: Path, terms: list[str], timeout: float = 20.0
    ) -> list[tuple[str, int, str]] | None:
        if shutil.which("rg") is None:
            return None
        if timeout <= 0:
            return None
        command = [
            "rg",
            "--fixed-strings",
            "--line-number",
            "--no-heading",
            "--color",
            "never",
            "--ignore-case",
            "--max-count",
            "3",
            "--glob",
            "*.{kt,java,cpp,cc,c,h,hpp,proto,xml,md}",
            "--glob",
            "!.git/**",
            "--glob",
            "!.gradle/**",
            "--glob",
            "!build/**",
            "--glob",
            "!out/**",
            "--glob",
            "!.cxx/**",
        ]
        for term in terms:
            command.extend(["-e", term])
        command.append(str(repo))
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if completed.returncode not in {0, 1}:
            return None
        matches: list[tuple[str, int, str]] = []
        for line in completed.stdout.splitlines():
            if len(matches) >= 80:
                break
            path_text, sep, rest = line.partition(":")
            if not sep:
                continue
            line_no_text, sep, text = rest.partition(":")
            if not sep or not line_no_text.isdigit():
                continue
            try:
                rel_path = str(Path(path_text).resolve().relative_to(repo.resolve()))
            except ValueError:
                rel_path = path_text
            matches.append((rel_path, int(line_no_text), text.strip()[:300]))
        return matches

    def _collect_source_evidence_by_scan(self, *, repo: Path, terms: list[str]) -> list[tuple[str, int, str]]:
        """Legacy hook: pure Python full-repo scans are disabled for large repos."""
        return []

    def _append_unique(self, values: list[str], value: str) -> None:
        normalized = value.strip()
        if normalized and normalized not in values:
            values.append(normalized)

    def _render_bug_agent_request(
        self,
        *,
        request_text: str,
        prompt_text: str,
        bug_url: str,
        plans: list["BugAnalysisPlan"],
    ) -> str:
        plan_lines = "\n".join(f"- {self._analysis_label(plan.kind)} (`{plan.kind}`)" for plan in plans)
        return (
            "# Bug Agent Request\n\n"
            "以下内容需要完整提供给本地 Agent 作为分析输入。\n\n"
            f"- Bug URL: `{bug_url}`\n"
            f"- 提炼后的分析描述: `{prompt_text}`\n"
            f"- 计划分析类型:\n{plan_lines}\n"
            "- 用户原始请求:\n\n```text\n"
            f"{request_text}\n"
            "```\n"
        )

    def _render_bug_reanalysis_request(
        self,
        *,
        request_text: str,
        followup_text: str,
        target_time: str,
        plans: list["BugAnalysisPlan"],
        history: list[dict[str, str]] | None,
    ) -> str:
        plan_lines = "\n".join(
            f"- {self._analysis_label(plan.kind)} (`{plan.kind}`)"
            + (f" / 信号: `{plan.signal_code}`" if plan.kind == "signal" and plan.signal_code else "")
            for plan in plans
        )
        history_lines = self._compact_bug_followup_history(history)
        history_block = "\n".join(history_lines) if history_lines else "- 无"
        return (
            "# Bug Reanalysis Request\n\n"
            "这是同一个 Bug 会话里的续聊/修正，请延续上一轮分析上下文，而不是重新开启独立话题。\n\n"
            f"- 本次追问/修正: `{followup_text}`\n"
            f"- 修正后的故障时间: `{target_time or '未识别'}`\n"
            f"- 继续分析类型:\n{plan_lines}\n"
            "- 最近对话历史:\n"
            f"{history_block}\n"
            "- 用户原始请求:\n\n```text\n"
            f"{request_text}\n"
            "```\n"
        )

    def _compact_bug_followup_history(self, history: list[dict[str, str]] | None) -> list[str]:
        lines: list[str] = []
        assistant_compacted = False
        for item in history or []:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").strip()
            content = re.sub(r"\s+", " ", str(item.get("content") or "")).strip()
            if not role or not content:
                continue
            if role == "assistant":
                if assistant_compacted:
                    continue
                if len(content) > 400:
                    lines.append("- assistant: [上一轮长回答已省略；稳定事实请看 metadata 与会话事实快照]")
                    assistant_compacted = True
                    continue
                lines.append(f"- assistant: {content[:200].rstrip()}")
                assistant_compacted = True
                continue
            if len(content) > 240:
                content = content[:239].rstrip() + "…"
            lines.append(f"- {role}: {content}")
        return lines[:6]

    def _render_bug_agent_followup_request(
        self,
        *,
        request_text: str,
        followup_text: str,
        history: list[dict[str, str]] | None,
    ) -> str:
        history_lines = self._compact_bug_followup_history(history)
        history_block = "\n".join(history_lines) if history_lines else "- 无"
        return (
            "# Bug Agent Follow-up Request\n\n"
            "这是同一个 Bug 会话里的继续追问，请延续原来的 Agent 会话，"
            "优先复用已经下载/解密/分析过的日志与报告，不要重新要求用户上传材料。\n\n"
            f"- 本次追问:\n\n```text\n{followup_text}\n```\n"
            "- 会话稳定事实与证据入口: 以 metadata 与会话事实快照为准；不要在 request 中回放上一轮长摘要。\n"
            "- 最近对话历史:\n"
            f"{history_block}\n"
            "- 用户原始请求:\n\n```text\n"
            f"{request_text}\n"
            "```\n"
        )

    def _render_bug_reanalysis_metadata(
        self,
        *,
        request_text: str,
        followup_text: str,
        job_id: str,
        target_time: str,
        prepared_input: Path | None,
        selected_input: Path | None,
        plans: list["BugAnalysisPlan"],
        rerun_kinds: list[str],
        reused_kinds: list[str],
        html_paths: list[Path],
        report_jsons: dict[str, Path | None],
        combined_artifacts: dict[str, object] | None,
        previous_summary_path: Path | None,
        source_evidence_path: Path | None = None,
        classification_skill: str = "",
        classification_source: str = "",
        classification_reason: str = "",
        classification_provider: str = "",
    ) -> str:
        lines = [
            "# Bug Reanalysis Metadata",
            "",
            f"- Job ID: `{job_id}`",
            f"- 用户原始请求: `{request_text}`",
            f"- 本次追问/修正: `{followup_text}`",
            f"- 修正后的故障时间: `{target_time or '未识别'}`",
            f"- 复用 prepared log 输入: `{prepared_input or ''}`",
            f"- 上一轮选中的日志输入: `{selected_input or ''}`",
            f"- 分析类型: `{', '.join(plan.kind for plan in plans) or '无'}`",
            f"- 命中 Skill: `{classification_skill or 'general'}`",
            f"- 分类来源: `{classification_source or 'manual_fallback'}`",
            f"- 分类 Agent: `{classification_provider or '无'}`",
            f"- 分类理由: `{classification_reason or '未记录'}`",
            f"- Skill 规范:\n{self._render_skill_context_lines(classification_skill)}",
            f"- 信号目标: `{', '.join(plan.signal_code or '' for plan in plans if plan.kind == 'signal') or '无'}`",
            f"- 本次重新执行: `{', '.join(rerun_kinds) or '无'}`",
            f"- 本次直接复用: `{', '.join(reused_kinds) or '无'}`",
        ]
        if previous_summary_path is not None and self._followup_needs_previous_summary_text(followup_text):
            lines.append(f"- 上一轮 Agent 总结: `{previous_summary_path}`")
        if source_evidence_path is not None:
            lines.append(f"- 本轮源码证据: `{source_evidence_path}`")
        if combined_artifacts is not None:
            lines.extend(
                [
                    f"- 综合报告 HTML: `{combined_artifacts['html_path']}`",
                    f"- 综合报告 JSON: `{combined_artifacts['json_path']}`",
                ]
            )
        lines.append("- 最新 HTML 报告:")
        for path in html_paths:
            lines.append(f"  - `{path}`")
        lines.append("- 最新 JSON 报告:")
        for kind, path in report_jsons.items():
            lines.append(f"  - `{kind}` -> `{path or ''}`")
        if source_evidence_path is not None:
            lines.extend(["", "## 本轮源码证据摘录", ""])
            try:
                evidence = source_evidence_path.read_text(encoding="utf-8").strip()
            except OSError as exc:
                evidence = f"读取失败: {exc}"
            lines.append(evidence[:5000])
        return "\n".join(lines) + "\n"

    def _render_bug_agent_followup_metadata(
        self,
        *,
        request_text: str,
        followup_text: str,
        job_id: str,
        job_dir: Path,
        output_dir: Path,
        prepared_input: Path | None,
        selected_input: Path | None,
        previous_summary_path: Path | None,
        report_files: list[Path],
        report_url: str,
        analysis_skill: str = "",
    ) -> str:
        lines = [
            "# Bug Agent Follow-up Metadata",
            "",
            f"- Job ID: `{job_id}`",
            f"- Job 目录: `{job_dir}`",
            f"- 输出目录: `{output_dir}`",
            f"- 用户原始请求: `{request_text}`",
            f"- 本次追问: `{followup_text}`",
            f"- prepared log 输入: `{prepared_input or ''}`",
            f"- selected log 输入: `{selected_input or ''}`",
        ]
        if analysis_skill.strip():
            lines.append(f"- 命中 Skill: `{analysis_skill.strip()}`")
            lines.append(f"- Skill 规范:")
            lines.append(self._render_skill_context_lines(analysis_skill).rstrip())
        if previous_summary_path is not None and self._followup_needs_previous_summary_text(followup_text):
            lines.append(f"- 上一轮 Agent 总结: `{previous_summary_path}`")
        if report_url.strip():
            lines.append(f"- 当前已发布报告链接: `{report_url.strip()}`")
        lines.append("- 可直接读取的现有报告/产物:")
        if report_files:
            for path in report_files:
                lines.append(f"  - `{path}`")
        else:
            lines.append("  - 无")
        lines.extend(
            [
                "- 处理要求:",
                "  - 直接基于上述本地路径继续分析，不要重新要求用户上传日志。",
                "  - 若需要补充证据，优先读取 prepared log 输入与 output 目录中的现有产物。",
            ]
        )
        return "\n".join(lines) + "\n"

    def _collect_bug_output_artifacts(self, output_dir: Path) -> list[Path]:
        if not output_dir.exists():
            return []
        artifacts: list[Path] = []
        for path in sorted(output_dir.iterdir()):
            if not path.is_file():
                continue
            if path.suffix.lower() not in {".md", ".html", ".json"}:
                continue
            artifacts.append(path)
        return artifacts

    def _build_bug_agent_summary_command(
        self,
        *,
        request_text: str,
        request_artifact: Path,
        metadata_path: Path,
        output_path: Path,
        provider_session_id: str = "",
        followup_text: str = "",
        previous_summary_path: Path | None = None,
        provider_override: str = "",
        command_override: str = "",
        snapshot_details: dict[str, object] | None = None,
        snapshot_plans: list["BugAnalysisPlan"] | None = None,
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
        if not provider or not command_name:
            return {"command": [], "provider": provider, "session_id": provider_session_id, "resumed": False}
        prompt = self._build_bug_agent_summary_prompt(
            request_text=request_text,
            request_artifact=request_artifact,
            metadata_path=metadata_path,
            followup_text=followup_text,
            previous_summary_path=previous_summary_path,
            snapshot_details=snapshot_details,
            snapshot_plans=snapshot_plans,
        )
        embedded_files = self._bug_agent_summary_context_files(
            followup_text=followup_text,
            request_artifact=request_artifact,
            metadata_path=metadata_path,
            previous_summary_path=previous_summary_path,
        )
        session_id = provider_session_id.strip()
        if provider == "codex":
            model = (self.config.bug_analysis.model or "").strip()
            if session_id:
                command = [
                    command_name,
                    "exec",
                    "resume",
                    "--skip-git-repo-check",
                    "--json",
                ]
                if model:
                    command.extend(["-m", model])
                command.extend(
                    [
                        "--output-last-message",
                        str(output_path),
                        session_id,
                        prompt,
                    ]
                )
            else:
                command = [
                    command_name,
                    "exec",
                    "--skip-git-repo-check",
                    "-s",
                    "read-only",
                    "-C",
                    str(self._working_dir()),
                ]
                if model:
                    command.extend(["-m", model])
                command.extend(
                    [
                        "--json",
                        "--output-last-message",
                        str(output_path),
                        prompt,
                    ]
                )
            return {
                "command": command,
                "provider": provider,
                "model": model,
                "session_id": session_id,
                "resumed": bool(session_id),
                "prompt": prompt,
                "embedded_files": embedded_files,
            }
        if provider in {"claude", "claude-code", "claude_code"}:
            model = (self.config.claude_agent.model or "").strip()
            allowed_tools = self.config.claude_agent.allowed_tools or ["Read", "Grep", "Glob", "LS"]
            session_id = session_id or str(uuid.uuid4())
            command = [
                command_name,
                "--print",
                "--output-format",
                "text",
                "--permission-mode",
                "dontAsk",
                "--allowedTools",
                ",".join(allowed_tools),
                "--append-system-prompt",
                (
                    "你是一个通过飞书触发的 bug 分析总结 agent。"
                    "只读分析，不修改文件，不执行写入命令。"
                    "必须完整响应用户原始请求中的所有诉求，输出中文 Markdown，结论先行。"
                ),
            ]
            for directory in self._bug_summary_add_dirs():
                command.extend(["--add-dir", str(directory)])
            if provider_session_id.strip():
                command.extend(["--resume", session_id])
            else:
                command.extend(["--session-id", session_id])
            command.append(prompt)
            return {
                "command": command,
                "provider": provider,
                "model": model,
                "session_id": session_id,
                "resumed": bool(provider_session_id.strip()),
                "prompt": prompt,
                "embedded_files": embedded_files,
            }
        return {"command": [], "provider": provider, "session_id": session_id, "resumed": False, "prompt": prompt, "embedded_files": embedded_files}

    def _build_bug_agent_summary_prompt(
        self,
        *,
        request_text: str,
        request_artifact: Path,
        metadata_path: Path,
        followup_text: str = "",
        previous_summary_path: Path | None = None,
        snapshot_details: dict[str, object] | None = None,
        snapshot_plans: list["BugAnalysisPlan"] | None = None,
    ) -> str:
        prompt = "请基于以下本地文件完成同一个 bug 会话的最终回答。\n要求：\n"
        snapshot_prefix = ""
        if followup_text.strip():
            prompt += (
                "1. 这是一条续聊/追问，必须直接回答这次新问题，并延续上一轮 Agent 会话。\n"
                "2. 优先复用 metadata 中已经给出的日志、报告、output 目录和历史总结，不要要求用户重新上传日志。\n"
                "3. 只读分析，不修改任何文件。\n"
                "4. 输出中文 Markdown，结论先行，再给出证据。\n"
                "5. 如果现有日志/报告仍不足以覆盖某个诉求，要明确指出缺口，但先回答已经能确认的部分。\n"
                "6. 对启动/生命周期类报告，若结构化 JSON 显示 `status != complete` 或仍有 `missing_critical`，即使 HTML/verdict 文案更乐观，也按“链路未闭环”处理，并明确指出报告内部冲突。\n"
                "7. “未命中关键节点”不等于“日志在这里截止”；除非材料明确显示文件结束或时间窗截断，否则不要把缺节点改写成日志截止。\n\n"
            )
            snapshot_prefix = self._build_bug_prompt_snapshot_prefix(
                request_text=request_text,
                metadata_path=metadata_path,
                followup_text=followup_text,
                snapshot_details=snapshot_details,
                plans_override=snapshot_plans,
            )
        else:
            prompt += (
                "1. 本次是全新 bug 分析请求，不是续聊/修正；不要虚构“上一轮分析”“本次修正”“延续上一轮”这类诉求或标题。\n"
                "2. 必须完整覆盖用户原始请求里的所有诉求，不要只回答其中一部分。\n"
                "3. 只读分析，不修改任何文件。\n"
                "4. 输出中文 Markdown，结论先行；若有多个诉求，按诉求分组说明结论和证据；若只有一个诉求，只在开头说明一次，不要在每条结论或证据前重复写相同的诉求。诉求标题只能来自用户原始请求，不要自行添加不存在的诉求。\n"
                "5. 如果脚本结果无法覆盖用户某个诉求，要明确指出缺口。\n"
                "6. metadata 中的“本轮脚本初步摘要”只是当前自动脚本输出，不要把它写成“上一轮结论”；只有显式提供 followup/previous summary 时，才能讨论修正上一轮结论。\n"
                "7. 如果 metadata 或报告里已经明确给出故障时间对应的主会话 / 主 PID / focus session，请优先围绕该主会话分析，不要展开无关会话；只有在需要证明时间不匹配时才提及其他会话。\n\n"
            )
        prompt += (
            "统一输出结构：请按以下中文二级标题组织最终回答，并只填入本次 bug 自身的证据，不套用示例业务词。\n"
            "## 结论摘要\n"
            "- 先给 3 到 5 条最重要结论，必须标明置信边界。\n"
            "## 关键证据\n"
            "- 每条证据尽量带文件、行号、时间、进程/package 或源码位置。\n"
            "## 最可能原因\n"
            "- 按可能性排序，说明支持证据和缺口；证据不足时明确不要强行定根因。\n"
            "## 待确认项\n"
            "- 只列真实证据缺口，例如精确时间、日志片段、运行状态、源码链路缺口。\n"
            "## 建议动作\n"
            "- 给出下一轮可执行动作，例如补日志、重跑某个 skill、沿某个源码或日志点继续查。\n\n"
        )
        if snapshot_prefix:
            prompt += snapshot_prefix
        prompt += f"用户原始请求：\n{request_text}\n\n"
        if followup_text.strip():
            prompt += f"本次追问/修正：\n{followup_text.strip()}\n\n"
        if previous_summary_path is not None and self._should_include_previous_summary_for_followup(
            followup_text=followup_text,
            request_artifact=request_artifact,
            metadata_path=metadata_path,
        ):
            prompt += f"上一轮 Agent 总结（也要核对，不可直接当成事实）：\n{previous_summary_path}\n\n"
        prompt += (
            "可读取路径：\n"
            f"- 工作区根目录：{self._working_dir()}\n"
            f"- 业务源码根目录：{self.config.guideengine_repo}\n\n"
            "以下是首批本地文件入口。请按需读取这些本地文件，优先读取 JSON/Markdown 结构化产物；"
            "HTML 只作为可视化报告入口，不要把 CSS/style/script 当作分析证据。"
            "如果 Bug Metadata 中列出命中的 Skill 规范，必须先读取对应 SKILL.md，并按其适用范围、执行链路和输出规则分析。"
            "只读分析，不修改文件，不要编造未看到的证据：\n\n"
        )
        if followup_text.strip():
            prompt += (
                "续聊性能约束：优先根据下面的精简上下文回答。"
                "只有精简上下文无法证明时，才读取 metadata 中列出的报告、日志或源码路径。\n\n"
            )
            for item in self._bug_agent_summary_context_files(
                followup_text=followup_text,
                request_artifact=request_artifact,
                metadata_path=metadata_path,
                previous_summary_path=previous_summary_path,
            ):
                prompt += self._render_bug_summary_context_item(item)
        else:
            for item in self._bug_agent_summary_context_files(
                followup_text=followup_text,
                request_artifact=request_artifact,
                metadata_path=metadata_path,
                previous_summary_path=previous_summary_path,
            ):
                prompt += self._render_bug_summary_context_item(item)
        return prompt

    def _bug_agent_summary_context_files(
        self,
        *,
        followup_text: str = "",
        request_artifact: Path,
        metadata_path: Path,
        previous_summary_path: Path | None = None,
    ) -> list[dict[str, object]]:
        files: list[dict[str, object]] = []
        if followup_text.strip():
            if previous_summary_path is not None and self._should_include_previous_summary_for_followup(
                followup_text=followup_text,
                request_artifact=request_artifact,
                metadata_path=metadata_path,
            ):
                files.append({"title": "上一轮 Agent 总结", "path": str(previous_summary_path), "max_chars": 0})
            files.append({"title": "Bug Agent Follow-up Request", "path": str(request_artifact), "max_chars": 0})
            files.append({"title": "Bug Follow-up Metadata", "path": str(metadata_path), "max_chars": 0})
            files.extend(self._bug_summary_referenced_context_files(metadata_path))
            return files
        if previous_summary_path is not None:
            files.append({"title": "上一轮 Agent 总结", "path": str(previous_summary_path), "max_chars": 0})
        files.append({"title": "Bug Agent Request", "path": str(request_artifact), "max_chars": 0})
        files.append({"title": "Bug Metadata", "path": str(metadata_path), "max_chars": 0})
        files.extend(self._bug_summary_referenced_context_files(metadata_path))
        return files

    def _bug_summary_referenced_context_files(self, metadata_path: Path) -> list[dict[str, object]]:
        try:
            metadata_text = metadata_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        metadata_resolved = metadata_path.expanduser().resolve()
        candidates: list[tuple[int, int, dict[str, object]]] = []
        seen: set[Path] = {metadata_resolved}
        raw_paths: list[tuple[int, str]] = []
        for index, raw in enumerate(re.findall(r"`([^`]+)`", metadata_text)):
            if re.match(r"^(?:/|~/)", raw.strip()):
                raw_paths.append((index, raw))
        offset = len(raw_paths)
        for index, raw in enumerate(re.findall(r"((?:/|~/)[^\s`]+?\.(?:md|json|html))", metadata_text)):
            raw_paths.append((offset + index, raw))
        for index, raw in raw_paths:
            path_text = raw.strip().rstrip(".,;:)）]}>，。；")
            path = Path(path_text).expanduser()
            if path.suffix.lower() not in {".md", ".json", ".html"}:
                continue
            if not path.exists() or not path.is_file():
                continue
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            title, priority, max_chars = self._bug_summary_context_file_profile(path)
            if not title:
                continue
            candidates.append(
                (
                    priority,
                    index,
                    {
                        "title": title,
                        "path": str(path),
                        "max_chars": max_chars,
                    },
                )
            )
        return [item for _priority, _index, item in sorted(candidates, key=lambda value: (value[0], value[1]))[:5]]

    def _bug_summary_context_file_profile(self, path: Path) -> tuple[str, int, int]:
        name = path.name
        lowered = name.casefold()
        if lowered == "bug_summary_evidence.md":
            return "Bug Summary Evidence", 0, 0
        if lowered == "bug_summary_evidence.json":
            return "Bug Summary Evidence JSON", 0, 0
        if lowered == "bug_source_evidence.md":
            return "Bug Source Evidence", 0, 0
        if lowered == "skill.md" and ".ai/skills" in str(path):
            return f"Matched Skill: {path.parent.name}", 0, 0
        if path.suffix.lower() == ".md" and path.parent.name == "references" and ".ai/skills" in str(path):
            return f"Matched Skill Reference: {name}", 1, 0
        if lowered.endswith("_analysis.md"):
            label = name.replace("_analysis.md", "").replace("_", " ").strip().title() or "Analysis"
            return f"Analysis Markdown: {label}", 1, 0
        if lowered.endswith(".json") and "_report" in lowered:
            return f"Report JSON: {name}", 2, 0
        if lowered.endswith(".html") and "_report" in lowered:
            return f"Report HTML: {name}", 3, 0
        return "", 9, 0

    def _render_bug_summary_context_item(self, item: dict[str, object]) -> str:
        title = str(item.get("title") or "Context File")
        path = Path(str(item.get("path") or ""))
        lowered = path.name.casefold()
        if lowered.endswith(".json") and "_report" in lowered:
            note = "结构化分析结果，必须优先读取，用于结论、时间窗、PID、证据行号。"
        elif lowered.endswith(".html") and "_report" in lowered:
            note = "可视化 HTML 报告；只有 JSON/Markdown 不足时再读取，读取时忽略 CSS/style/script。"
        elif lowered == "bug_summary_evidence.md":
            note = "结构化证据包；优先读取，用于最后命中事件、后续同 PID 原始日志、缺失关键节点和禁止结论。"
        elif lowered == "bug_summary_evidence.json":
            note = "结构化证据包 JSON；用于程序化核对字段和值，优先级高于 HTML 报告。"
        elif lowered == "bug_source_evidence.md":
            note = "源码证据文件；需要源码链路时读取。"
        elif lowered == "skill.md":
            note = "本轮命中的专用 Skill 规范；必须先读取并按其中的 Required Workflow / 适用范围执行。"
        elif path.parent.name == "references":
            note = "本轮命中 Skill 的引用资料；SKILL.md 要求读取或证据不足时必须读取。"
        elif "metadata" in lowered:
            note = "元数据索引文件；先读取它获取日志、报告、output 目录、主 PID 和故障时间等入口。"
        elif "request" in lowered:
            note = "用户请求文件；读取它确认原始诉求和本轮追问边界。"
        else:
            note = "本地上下文文件；按需读取。"
        return (
            f"## {title}\n"
            f"路径: `{path}`\n"
            f"读取要求: {note}\n\n"
        )

    def _embedded_file_manifest(self, embedded_files: list[object]) -> list[dict[str, object]]:
        manifest: list[dict[str, object]] = []
        for item in embedded_files:
            if not isinstance(item, dict):
                continue
            path = Path(str(item.get("path") or ""))
            max_chars = int(item.get("max_chars") or 0)
            entry: dict[str, object] = {
                "title": str(item.get("title") or ""),
                "path": str(path),
                "max_chars": max_chars,
                "exists": path.exists(),
            }
            if path.exists():
                try:
                    content = path.read_text(encoding="utf-8")
                    stripped = content.strip()
                    entry["source_chars"] = len(stripped)
                    entry["embedded_chars"] = min(len(stripped), max_chars) if max_chars > 0 else 0
                    entry["truncated"] = max_chars > 0 and len(stripped) > max_chars
                except OSError as exc:
                    entry["read_error"] = str(exc)
            manifest.append(entry)
        return manifest

    def _bug_summary_add_dirs(self) -> list[Path]:
        candidates = [self._working_dir(), self.config.guideengine_repo, *self.config.claude_agent.add_dirs]
        resolved: list[Path] = []
        seen: set[Path] = set()
        for candidate in candidates:
            path = Path(candidate).expanduser().resolve()
            if path in seen:
                continue
            seen.add(path)
            resolved.append(path)
        return resolved
