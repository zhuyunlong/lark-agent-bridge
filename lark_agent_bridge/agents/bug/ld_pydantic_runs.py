from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from . import _shared


class _LdPydanticRunsMixin:
    """pydantic-ai 快路径执行：source-stage 源码分析与 LD 车道级分析（与 LdExecutorMixin 共享 self 状态）。"""

    def _run_source_stage_pydantic_ai(
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
        html_path: Path,
        json_path: Path,
        analysis_dir: Path,
        progress_callback: Callable[[dict[str, object]], None] | None,
        context_profile: str = "",
        prior_findings: list[tuple[str, str, str]] | None = None,
        render_html: bool = True,
    ) -> dict[str, object]:
        """Run source analysis via pydantic-ai agent runtime (fast path).

        Returns custom_result-compatible dict. On failure, returns ok=False
        so the caller can fall through to subprocess.
        """
        from ..agent_runtime import AgentRuntime
        from ..agent_output_models import SourceAnalysisOutput

        started = time.monotonic()
        provider_tag = "pydantic_ai"
        analysis_dir.mkdir(parents=True, exist_ok=True)
        analysis_markdown_path = analysis_dir / self._skill_agent_analysis_markdown_name(analysis_kind)

        ai_opts = self.config.ai_provider
        si_opts = self.config.source_investigation
        # Use source repo roots as primary workspace (not bridge dir)
        source_roots = list(si_opts.repo_roots or [])
        if not source_roots:
            source_roots = [Path(self._working_dir())]
        primary_workspace = source_roots[0]
        extra_roots = source_roots[1:] + list(si_opts.add_dirs or [])
        # Add analysis dir so agent can read prior evidence
        if analysis_dir.exists():
            extra_roots.append(analysis_dir)
        # Report dir for reading prior analysis artifacts
        report_dir = analysis_dir.parent if analysis_dir.exists() else None
        # Log metadata
        log_metadata_path = None
        if prepared_input and (prepared_input / "log_focus.md").exists():
            log_metadata_path = prepared_input / "log_focus.md"
        elif analysis_dir and (analysis_dir / "log_focus.md").exists():
            log_metadata_path = analysis_dir / "log_focus.md"

        # Codegraph client for semantic code intelligence
        codegraph_client = None
        codegraph_roots = None
        if si_opts.codegraph_enabled:
            try:
                from ...knowledge.codegraph_client import CodeGraphClient
                cg = CodeGraphClient(
                    command=si_opts.codegraph_command,
                    timeout=si_opts.codegraph_timeout_seconds,
                )
                if cg.is_available():
                    codegraph_client = cg
                    codegraph_roots = source_roots
                    logger.info("Codegraph client available for pydantic-ai tools")
            except Exception as exc:
                logger.debug("Codegraph client init failed: %s", exc)

        runtime = AgentRuntime(ai_opts, workspace=primary_workspace)

        if not runtime.is_available():
            return {
                "ok": False,
                "error_code": "pydantic_ai_not_available",
                "message": "pydantic-ai runtime 不可用，将 fallback 到 file_agent。",
                "command": [],
                "provider": provider_tag,
                "analysis_markdown_path": analysis_markdown_path,
                "duration_seconds": time.monotonic() - started,
            }

        self._emit_progress(
            progress_callback,
            stage=f"{analysis_kind}_pydantic_ai",
            message=f"源码分析：pydantic-ai runtime（{ai_opts.primary_model}）",
            provider=provider_tag,
            model=ai_opts.primary_model,
        )

        # Build skill context
        skill_record = self.skill_manager.get_skill(context_profile or skill_name, include_content=True)
        skill_content = skill_record.content if skill_record and skill_record.content else ""

        # Build source evidence context
        source_evidence_text = ""
        if source_evidence_path and source_evidence_path.exists():
            try:
                source_evidence_text = source_evidence_path.read_text(encoding="utf-8", errors="replace")[:30000]
            except OSError:
                pass

        # Build prior findings context
        prior_context = ""
        if prior_findings:
            parts = []
            for kind, label, content in prior_findings:
                parts.append(f"### {label} ({kind})\n{content[:5000]}")
            prior_context = "\n\n".join(parts)

        system_prompt = (
            "你是一个专业的源码分析 Agent，通过飞书触发。"
            "你的任务是根据 Bug 信息、日志证据和源代码，分析问题的根因。\n\n"
            "## 可用工具\n"
            "### Shell（最高效，支持管道）\n"
            "- bash(command, workdir?): 执行只读 shell 命令，支持管道。例如：\n"
            "    bash('rg \"OnSceneChanged\" --type cs -C 3 | head -50')\n"
            "    bash('git log --oneline --follow -- UnitySceneTypeService.kt | head -20')\n"
            "    bash('find . -name \"*.kt\" | xargs grep -l \"SIGNAL_SR_SCENE_TYPE\" | head -10')\n"
            "    bash('cat SRDataManagerService.cs | sed -n \"80,140p\"')\n"
            "  允许：rg grep find ls cat head tail awk sed sort jq git-log/diff/show/blame xargs tree\n"
            "  禁止：rm mv cp curl wget sudo 及写文件\n"
            "### 文件读取\n"
            "- read_file(path, start_line?, end_line?): 读取文件内容，支持行范围分页\n"
            "- get_file_outline(path): 获取文件符号大纲（类/函数/方法列表+行号），无需读全文\n"
            "- list_dir(path): 列出目录内容\n"
            "### 搜索\n"
            "- grep(pattern, glob_filter?, context_lines?): 正则搜索，推荐用 bash('rg ...') 代替\n"
            "- glob(pattern): 按 glob 查找文件路径，如 '**/*.kt'\n"
            "### 语义代码搜索（有索引时可用）\n"
            "- search_codegraph(query, kind?): 语义符号搜索，按函数名/类名查找定义位置\n"
            "- get_callers(symbol): 查找指定函数/方法的所有调用者\n"
            "- get_code_context(task): 根据任务描述自动构建代码上下文和入口点\n"
            "### 分析辅助\n"
            "- think(thought): 记录推理思路（scratchpad），梳理复杂分析步骤\n"
            "- repo_overview(path?): 仓库概览：分支、最近提交、目录结构\n"
            "- read_report_artifact(name): 读取前序分析报告产物\n"
            "- read_prepared_log_metadata(): 读取日志元数据摘要\n\n"
            "## 工作要求\n"
            "1. 优先用 bash() 执行管道命令，效率最高（一次调用完成搜索+过滤+格式化）\n"
            "2. 复杂分析前先用 think() 写下分析计划和假设\n"
            "3. 优先用 search_codegraph/get_code_context 快速定位符号（有索引时）\n"
            "4. 对大文件先用 get_file_outline 了解结构，再用 read_file(start_line, end_line) 精读\n"
            "5. 用 bash('git log') 确认代码改动时间线\n"
            "6. 每条 evidence 必须包含具体的 file 路径和 line 号\n"
            "7. 证据不足时明确写待确认，不要编造\n"
            "8. 输出中文\n"
            "9. 必须在 node_status 字段中，为每个分析涉及的源码文件名填写状态值（ok/suspect/broken/unknown），"
            "标注该链路节点是否打通；在 findings 中列出每个关键发现（每项含 file/severity/title）\n"
        )

        user_prompt = (
            f"# 源码分析请求\n\n"
            f"## Bug 信息\n"
            f"- 标题: {title}\n"
            f"- 故障时间: {fault_time}\n"
            f"- 用户请求: {request_text}\n"
            f"- 缺陷描述: {description}\n\n"
        )
        if skill_content:
            user_prompt += f"## 分析方法论\n{skill_content[:6000]}\n\n"
        if source_evidence_text:
            user_prompt += f"## 源码证据\n{source_evidence_text[:20000]}\n\n"
        if prior_context:
            user_prompt += f"## 前序分析结果\n{prior_context[:10000]}\n\n"
        user_prompt += (
            "请使用工具探索代码库，找到与此 Bug 相关的源码文件，分析根因。\n"
            "必须提供具体的代码证据（文件路径 + 行号 + 代码片段）。\n\n"
            "## 可搜索的代码仓库\n"
        )
        for root in source_roots:
            user_prompt += f"- {root.name}: {root}\n"

        try:
            # Build a progress wrapper for real-time tool call visibility
            def _tool_progress(*, stage: str, message: str, **kw: object) -> None:
                self._emit_progress(progress_callback, stage=stage, message=message, **kw)

            result = runtime.run(
                output_type=SourceAnalysisOutput,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                tools_enabled=True,
                strict_tools=True,
                extra_roots=extra_roots,
                report_dir=report_dir,
                log_metadata_path=log_metadata_path,
                progress_callback=_tool_progress,
                stream=True,
                codegraph_client=codegraph_client,
                codegraph_roots=codegraph_roots,
            )
        except Exception as exc:
            logger.warning("pydantic-ai source analysis failed: %s", exc)
            return {
                "ok": False,
                "error_code": "pydantic_ai_execution_error",
                "message": f"pydantic-ai 源码分析执行失败：{exc}",
                "command": [],
                "provider": provider_tag,
                "analysis_markdown_path": analysis_markdown_path,
                "duration_seconds": time.monotonic() - started,
            }

        if not result.ok:
            return {
                "ok": False,
                "error_code": result.error_code or "pydantic_ai_failed",
                "message": result.error or "pydantic-ai 分析失败",
                "command": [],
                "provider": provider_tag,
                "analysis_markdown_path": analysis_markdown_path,
                "duration_seconds": result.duration_seconds,
            }

        # Source analysis requires actual tool exploration
        if result.tool_calls == 0:
            logger.warning(
                "pydantic-ai source analysis completed with 0 tool calls (runtime_path=%s) — "
                "agent did not explore code, treating as failure",
                result.runtime_path,
            )
            return {
                "ok": False,
                "error_code": "pydantic_ai_no_tool_calls",
                "message": "pydantic-ai 源码分析未调用任何工具（未探索代码库），视为失败",
                "command": [],
                "provider": provider_tag,
                "analysis_markdown_path": analysis_markdown_path,
                "duration_seconds": result.duration_seconds,
                "runtime_path": result.runtime_path,
            }

        # Emit per-tool-call progress for observability
        for i, tc in enumerate(result.tool_trace[:20]):
            self._emit_progress(
                progress_callback,
                stage=f"{analysis_kind}_tool_call",
                message=f"tool[{i+1}/{result.tool_calls}]: {tc.get('tool', '?')}",
                tool=tc.get("tool", ""),
                args=tc.get("args", "")[:100],
            )

        # Write analysis markdown
        markdown = result.markdown or str(result.output)
        analysis_markdown_path.write_text(markdown + "\n", encoding="utf-8")

        # Validate evidence
        valid, reason, evidence_count = self._validate_custom_skill_analysis(analysis_markdown_path)
        if not valid:
            logger.warning("pydantic-ai source analysis output missing evidence: %s", reason)
            return {
                "ok": False,
                "error_code": "pydantic_ai_invalid_evidence",
                "message": f"pydantic-ai 源码分析输出缺少有效关键证据：{reason}",
                "command": [],
                "provider": provider_tag,
                "analysis_markdown_path": analysis_markdown_path,
                "duration_seconds": result.duration_seconds,
                "evidence_count": evidence_count,
            }

        # Write report
        self._write_custom_skill_agent_report(
            analysis_kind=analysis_kind,
            analysis_label=self._analysis_label(analysis_kind),
            html_path=html_path,
            json_path=json_path,
            analysis_markdown_path=analysis_markdown_path,
            skill_name=skill_name,
            provider=provider_tag,
            request_text=request_text,
            prompt_text=prompt_text,
            title=title,
            description=description,
            fault_time=fault_time,
            selected_input=selected_input,
            prepared_input=prepared_input,
            source_evidence_path=source_evidence_path,
            evidence_count=evidence_count,
            duration_seconds=result.duration_seconds,
            executor=provider_tag,
            render_html=render_html,
            extra_payload={
                "node_status": getattr(result.output, "node_status", {}),
                "findings": getattr(result.output, "findings", []),
                "verdict": (
                    {
                        "headline": getattr(result.output, "conclusion", "") or "",
                        "next_step": (getattr(result.output, "suggested_actions", None) or [""])[0],
                    }
                    if getattr(result.output, "conclusion", "")
                    else {}
                ),
            },
        )

        self._emit_progress(
            progress_callback,
            stage=f"{analysis_kind}_pydantic_ai_done",
            message=f"源码分析完成（{result.duration_seconds:.1f}s, pydantic-ai runtime）",
            provider=provider_tag,
            model=result.model,
            evidence_count=evidence_count,
            tool_calls=result.tool_calls,
            runtime_path=result.runtime_path,
        )

        return {
            "ok": True,
            "error_code": "",
            "message": "",
            "command": [],
            "analysis_kind": analysis_kind,
            "provider": provider_tag,
            "executor": provider_tag,
            "analysis_markdown_path": analysis_markdown_path,
            "html_path": html_path,
            "json_path": json_path,
            "stdout": markdown,
            "stderr": "",
            "duration_seconds": result.duration_seconds,
            "evidence_count": evidence_count,
            "runtime_path": result.runtime_path,
            "tool_calls": result.tool_calls,
            "tool_trace": result.tool_trace,
            "usage": result.usage,
            "custom_skill_analysis_status": "completed",
        }
    def _run_ld_pydantic_ai_analysis(
        self,
        *,
        skill_name: str,
        request_text: str,
        prompt_text: str,
        title: str,
        description: str,
        fault_time: str,
        selected_input: Path | None,
        prepared_input: Path | None,
        html_path: Path,
        json_path: Path,
        analysis_dir: Path,
        progress_callback: Callable[[dict[str, object]], None] | None,
        prior_findings: list[tuple[str, str, str]] | None = None,
    ) -> dict[str, object]:
        """Run LD lane-level analysis via pydantic-ai agent runtime (fast path).

        Returns custom_result-compatible dict. On failure, returns ok=False
        so the caller can fall through to direct_api or subprocess.
        """
        from ..agent_runtime import AgentRuntime
        from ..agent_output_models import LDLaneLevelOutput

        started = time.monotonic()
        provider_tag = "pydantic_ai"
        analysis_kind = "ld_lane_level"
        analysis_dir.mkdir(parents=True, exist_ok=True)
        analysis_markdown_path = analysis_dir / self._skill_agent_analysis_markdown_name(analysis_kind)
        log_workspace = prepared_input.parent if prepared_input is not None and prepared_input.is_file() else (prepared_input or Path(self._working_dir()))
        source_roots = self._custom_skill_agent_source_roots()
        # Lane-level root cause lives in the guideengine/Napa5 source, so run the
        # agent with the source repo as the working directory and keep the logs
        # reachable as extra roots (logs are referenced by absolute path anyway).
        primary_workspace = source_roots[0] if source_roots else log_workspace
        extra_roots: list[Path] = []
        for root in [log_workspace, *source_roots[1:], Path(self._working_dir()), analysis_dir]:
            if root and root != primary_workspace and root not in extra_roots:
                extra_roots.append(root)
        report_dir = analysis_dir.parent if analysis_dir.exists() else None
        log_metadata_path = self._ld_prepared_log_metadata_path(
            prepared_input=prepared_input,
            analysis_dir=analysis_dir,
            fault_time=fault_time,
        )

        ai_opts = self.config.ai_provider
        runtime = AgentRuntime(ai_opts, workspace=primary_workspace)

        if not runtime.is_available():
            return {
                "ok": False,
                "error_code": "pydantic_ai_not_available",
                "message": "pydantic-ai runtime 不可用，将 fallback 到 direct_api/file_agent。",
                "command": [],
                "provider": provider_tag,
                "analysis_markdown_path": analysis_markdown_path,
                "duration_seconds": time.monotonic() - started,
            }

        self._emit_progress(
            progress_callback,
            stage="ld_pydantic_ai",
            message=f"LD 车道级分析：pydantic-ai runtime（{ai_opts.primary_model}）",
            provider=provider_tag,
            model=ai_opts.primary_model,
        )

        # Build skill context
        skill_record = self.skill_manager.get_skill(skill_name, include_content=True)
        skill_content = skill_record.content if skill_record and skill_record.content else ""

        # Build prior findings context
        prior_context = ""
        if prior_findings:
            parts = []
            for kind, label, content in prior_findings:
                parts.append(f"### {label} ({kind})\n{content[:5000]}")
            prior_context = "\n\n".join(parts)

        system_prompt = (
            "你是一个专业的 LD 车道级问题分析 Agent，通过飞书触发。"
            "你的任务是根据 Bug 信息、日志证据（蒙特卡洛日志、瓦片渲染日志），分析车道级显示异常的根因。"
            "使用提供的工具（read_file, get_file_outline, grep, glob, list_dir, git_log, think）来探索日志文件和代码库。"
            "工作目录是 guideengine 源码根。当日志不覆盖故障时间、缺少 LD 状态日志或日志搜索连续 2-3 次无命中时，"
            "必须转向阅读 guideengine/Napa5 源码（如 LdActionProcess、LDDataModel.CheckLDState、Android→Unity 信号桥、tile 加载逻辑），"
            "用源码文件+行号解释 LD 车道级判定链路与可能断点，不要在日志里反复空搜。"
            "复杂分析前先用 think() 记录分析思路。"
            "对大文件先用 get_file_outline 了解结构，再用 read_file(start_line, end_line) 精读关键段落。"
            "必须输出结构化的分析结果，包含：conclusion, root_cause, evidence, montecarlo_findings, tile_render_findings, pending_items, suggested_actions。"
            "每条 evidence 必须包含具体的 file 路径和 line 号。"
            "证据不足时明确写待确认，不要编造。"
            "输出中文。"
        )

        user_prompt = (
            f"# LD 车道级分析请求\n\n"
            f"## Bug 信息\n"
            f"- 标题: {title}\n"
            f"- 故障时间: {fault_time}\n"
            f"- 用户请求: {request_text}\n"
            f"- 缺陷描述: {description}\n\n"
        )
        if skill_content:
            user_prompt += f"## 分析方法论\n{skill_content[:6000]}\n\n"
        if prior_context:
            user_prompt += f"## 前序分析结果\n{prior_context[:10000]}\n\n"
        source_roots_text = "\n".join(f"  - `{r}`" for r in source_roots) or "  - （未配置源码根）"
        user_prompt += (
            "## 检索边界\n"
            f"- 工作目录（源码根）: `{primary_workspace}`，可直接阅读 guideengine/Napa5 源码定位 LD 车道级状态机与 Android→Unity 信号桥实现。\n"
            "- 其他可读源码根:\n"
            f"{source_roots_text}\n"
            f"- 主输入日志: `{prepared_input}`，通过 read_prepared_log_metadata() 返回的绝对路径访问（日志目录已在可读根内）。\n"
            "- 必须先调用 read_prepared_log_metadata()，按元数据里的主日志和同目录候选文件检索日志证据。\n"
            "- 源码检索先收敛到 LD/车道级相关模块（如 LdActionProcess、LDDataModel.CheckLDState、tile 加载/信号桥），再按需扩展；禁止扫描 bug_cache 以外的历史 job 目录。\n\n"
            "请结合源码与日志：在源码中定位 LD 车道级关键实现（文件+行号），并用蒙特卡洛/瓦片日志证据印证根因。\n"
            "必须提供具体证据（源码文件+行号 或 日志文件+行号+内容）。"
        )

        try:
            def _tool_progress(*, stage: str, message: str, **kw: object) -> None:
                self._emit_progress(progress_callback, stage=stage, message=message, **kw)

            result = runtime.run(
                output_type=LDLaneLevelOutput,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                tools_enabled=True,
                strict_tools=True,
                extra_roots=extra_roots,
                report_dir=report_dir,
                log_metadata_path=log_metadata_path,
                progress_callback=_tool_progress,
                stream=True,
            )
        except Exception as exc:
            logger.warning("pydantic-ai LD analysis failed: %s", exc)
            return {
                "ok": False,
                "error_code": "pydantic_ai_execution_error",
                "message": f"pydantic-ai LD 分析执行失败：{exc}",
                "command": [],
                "provider": provider_tag,
                "analysis_markdown_path": analysis_markdown_path,
                "duration_seconds": time.monotonic() - started,
            }

        if not result.ok:
            return {
                "ok": False,
                "error_code": result.error_code or "pydantic_ai_failed",
                "message": result.error or "pydantic-ai LD 分析失败",
                "command": [],
                "provider": provider_tag,
                "analysis_markdown_path": analysis_markdown_path,
                "duration_seconds": result.duration_seconds,
            }

        # Write analysis markdown
        markdown = result.markdown or str(result.output)
        analysis_markdown_path.write_text(markdown + "\n", encoding="utf-8")

        # Validate evidence
        valid, reason, evidence_count = self._validate_custom_skill_analysis(analysis_markdown_path)
        if not valid:
            logger.warning("pydantic-ai LD analysis output missing evidence: %s", reason)
            return {
                "ok": False,
                "error_code": "pydantic_ai_invalid_evidence",
                "message": f"pydantic-ai LD 分析输出缺少有效关键证据：{reason}",
                "command": [],
                "provider": provider_tag,
                "analysis_markdown_path": analysis_markdown_path,
                "duration_seconds": result.duration_seconds,
                "evidence_count": evidence_count,
            }

        # Write report
        self._write_custom_skill_agent_report(
            analysis_kind=analysis_kind,
            analysis_label=self._analysis_label(analysis_kind),
            html_path=html_path,
            json_path=json_path,
            analysis_markdown_path=analysis_markdown_path,
            skill_name=skill_name,
            provider=provider_tag,
            request_text=request_text,
            prompt_text=prompt_text,
            title=title,
            description=description,
            fault_time=fault_time,
            selected_input=selected_input,
            prepared_input=prepared_input,
            source_evidence_path=None,
            evidence_count=evidence_count,
            duration_seconds=result.duration_seconds,
            executor=provider_tag,
        )

        self._emit_progress(
            progress_callback,
            stage="ld_pydantic_ai_done",
            message=f"LD 车道级分析完成（{result.duration_seconds:.1f}s, pydantic-ai runtime）",
            provider=provider_tag,
            model=result.model,
            evidence_count=evidence_count,
            tool_calls=result.tool_calls,
            runtime_path=result.runtime_path,
        )

        return {
            "ok": True,
            "error_code": "",
            "message": "",
            "command": [],
            "analysis_kind": analysis_kind,
            "provider": provider_tag,
            "executor": provider_tag,
            "analysis_markdown_path": analysis_markdown_path,
            "html_path": html_path,
            "json_path": json_path,
            "stdout": markdown,
            "stderr": "",
            "duration_seconds": result.duration_seconds,
            "evidence_count": evidence_count,
            "runtime_path": result.runtime_path,
            "tool_calls": result.tool_calls,
            "tool_trace": result.tool_trace,
            "usage": result.usage,
            "custom_skill_analysis_status": "completed",
        }
