from __future__ import annotations

from ._shared import *  # noqa: F401,F403


from .summary_evidence import _SummaryEvidenceMixin
from .api_prompt_snapshot import _ApiPromptSnapshotMixin
from .context_excerpt import _ContextExcerptMixin


class _DirectApiMixin(_SummaryEvidenceMixin, _ApiPromptSnapshotMixin, _ContextExcerptMixin):
    def _run_bug_summary_pydantic_ai(
        self,
        *,
        request_text: str,
        request_artifact: Path,
        metadata_path: Path,
        output_path: Path,
        followup_text: str = "",
        previous_summary_path: Path | None = None,
        progress_callback: Callable[[dict[str, object]], None] | None,
        snapshot_details: dict[str, object] | None = None,
        snapshot_plans: list["BugAnalysisPlan"] | None = None,
    ) -> dict[str, object]:
        """Run bug summary via pydantic-ai agent runtime (structured output + tools).

        Unlike direct_api which inlines all files, this uses tools so the agent
        can selectively read analysis artifacts. Falls back to direct_api on failure.
        """
        from ..agent_runtime import AgentRuntime, _check_pydantic_ai

        if not _check_pydantic_ai():
            return {
                "message": "",
                "command": None,
                "error": "pydantic_ai_not_available",
                "provider": "pydantic_ai",
                "session_id": "",
                "resumed": False,
                "duration_seconds": 0.0,
                "usage": {},
                "usage_scope": "",
            }

        ai_opts = self.config.ai_provider
        started = time.monotonic()

        # Build a tool-oriented prompt: list file paths instead of inlining content.
        prompt = self._build_bug_summary_prompt_for_pydantic_ai(
            request_text=request_text,
            request_artifact=request_artifact,
            metadata_path=metadata_path,
            followup_text=followup_text,
            previous_summary_path=previous_summary_path,
            snapshot_details=snapshot_details,
            snapshot_plans=snapshot_plans,
        )
        if not prompt.strip():
            return {
                "message": "",
                "command": None,
                "error": "pydantic_ai_prompt_empty",
                "provider": "pydantic_ai",
                "session_id": "",
                "resumed": False,
                "duration_seconds": time.monotonic() - started,
                "usage": {},
                "usage_scope": "",
            }

        self._emit_progress(
            progress_callback,
            stage="bug_agent_summary_pydantic_ai",
            message="pydantic-ai Agent 整理最终结论（结构化输出 + 工具增强）",
            provider="pydantic_ai",
            model=ai_opts.primary_model,
        )

        system_prompt = (
            "你是一个通过飞书触发的 bug 分析总结 agent。\n"
            "你可以使用 read_file / grep / search_large_log / bash 工具读取分析产物文件。\n"
            "只读分析，不修改文件，不执行写入命令。\n"
            "遇到 *.alog.log、*.xlog.log 或大日志时，必须先用 search_large_log 或 bash rg 搜索关键词定位行号，再用 read_file 精读上下文。\n"
            "不要用连续 read_file 分页扫描大日志。\n"
            "必须完整响应用户原始请求中的所有诉求，输出中文 Markdown，结论先行。\n"
            "所有分析数据的路径已列在用户消息中，请根据文件类型选择合适工具读取。"
        )

        workspace = metadata_path.parent if metadata_path.exists() else Path.cwd()
        runtime = AgentRuntime(ai_opts, workspace=workspace, max_retries=1)
        result = runtime.run(
            system_prompt=system_prompt,
            user_prompt=prompt,
            tools_enabled=True,
            strict_tools=True,
        )

        duration = time.monotonic() - started
        if result.ok and (result.runtime_path != "pydantic_ai_agent" or result.tool_calls == 0):
            logger.warning(
                "pydantic-ai summary returned without tool-backed runtime "
                "(runtime_path=%s, tool_calls=%d); rejecting so caller can fallback",
                result.runtime_path,
                result.tool_calls,
            )
            return {
                "message": "",
                "command": None,
                "error": "pydantic_ai_summary_requires_tools",
                "provider": "pydantic_ai",
                "session_id": "",
                "resumed": False,
                "duration_seconds": duration,
                "usage": result.usage,
                "usage_scope": "",
                "runtime_path": result.runtime_path,
                "tool_calls": result.tool_calls,
                "tool_trace": result.tool_trace,
            }
        if result.ok and result.markdown.strip():
            message = result.markdown.strip()
            # Write to output_path for downstream consumers
            try:
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(message, encoding="utf-8")
            except OSError:
                pass
            self._emit_progress(
                progress_callback,
                stage="bug_agent_summary_completed",
                message=f"pydantic-ai Agent 已整理最终结论（{duration:.1f}s）",
                provider="pydantic_ai",
                model=result.model,
                duration_seconds=round(duration, 1),
                tool_calls=result.tool_calls,
                tool_trace=result.tool_trace[:20],
            )
            return {
                "message": message,
                "command": None,
                "error": "",
                "provider": "pydantic_ai",
                "session_id": "",
                "resumed": False,
                "duration_seconds": duration,
                "usage": result.usage,
                "usage_scope": "summary",
                "runtime_path": result.runtime_path,
                "tool_calls": result.tool_calls,
                "tool_trace": result.tool_trace,
            }

        logger.warning(
            "pydantic-ai summary failed (%.1fs, error=%s), will fallback",
            duration, result.error_code or result.error,
        )
        return {
            "message": "",
            "command": None,
            "error": result.error or "pydantic_ai_summary_failed",
            "provider": "pydantic_ai",
            "session_id": "",
            "resumed": False,
            "duration_seconds": duration,
            "usage": result.usage,
            "usage_scope": "",
        }
    def _build_bug_summary_prompt_for_pydantic_ai(
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
        """Build a tool-oriented summary prompt.

        Unlike _build_bug_agent_summary_prompt_for_api which inlines file content,
        this lists file paths so the pydantic-ai agent can use read_file/search tools
        tools to read them selectively.
        """
        prompt = "请基于以下分析材料完成 bug 会话的最终回答。\n"
        prompt += "材料文件路径已列出，请按文件类型选择工具：报告/Markdown 用 read_file，大日志先用 search_large_log 或 bash rg 定位行号。\n\n"

        snapshot_prefix = ""
        if followup_text.strip():
            prompt += (
                "这是续聊/追问。要求：\n"
                "1. 直接回答新问题，延续上一轮分析。\n"
                "2. 使用工具读取已有材料。\n"
                "3. 只读分析，不修改文件。\n"
                "4. 输出中文 Markdown，结论先行。\n\n"
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
                "这是全新 bug 分析请求。要求：\n"
                "1. 完整覆盖用户请求里的所有诉求。\n"
                "2. 只读分析，不修改文件。\n"
                "3. 输出中文 Markdown，结论先行。\n\n"
            )

        prompt += (
            "输出结构：\n"
            "## 结论摘要\n- 3-5 条最重要结论，标明置信边界。\n"
            "## 关键证据\n- 每条带文件、行号、时间。\n"
            "## 最可能原因\n- 按可能性排序。\n"
            "## 待确认项\n- 真实证据缺口。\n"
            "## 建议动作\n- 下一轮可执行动作。\n\n"
        )

        if snapshot_prefix:
            prompt += snapshot_prefix

        prompt += f"### 用户原始请求\n{request_text}\n\n"
        if followup_text.strip():
            prompt += f"### 本次追问\n{followup_text.strip()}\n\n"

        # List file paths for the agent to read
        prompt += "### 可用材料文件\n"
        file_list: list[str] = []
        if request_artifact.exists():
            file_list.append(f"- Bug 请求文件: `{request_artifact}`")
        if metadata_path.exists():
            file_list.append(f"- Bug 元数据: `{metadata_path}`")
        if previous_summary_path and previous_summary_path.exists():
            file_list.append(f"- 上一轮总结: `{previous_summary_path}`")
        context_items = self._bug_summary_context_items(metadata_path, include_html_reports=False)
        for item in context_items:
            path = Path(str(item["path"]))
            title = str(item["title"])
            if path.exists():
                file_list.append(f"- {title}: `{path}`")

        if file_list:
            prompt += "\n".join(file_list) + "\n\n"
            prompt += (
                "请读取上述材料后整理回答。普通报告用 read_file；日志文件不要逐页扫描，"
                "先用 search_large_log(pattern, path=...) 或 bash(\"rg -n ...\") 搜索定位。\n\n"
            )
        else:
            prompt += "（无可用材料文件）\n"

        decoded_logs = self._bug_summary_decoded_log_paths(context_items)
        if decoded_logs:
            prompt += "### 可搜索解码日志\n"
            prompt += "\n".join(f"- `{path}`" for path in decoded_logs) + "\n\n"

        prompt += (
            "### 大日志搜索规则\n"
            "- 对 `*.alog.log`、`*.xlog.log` 或大于 1MB 的日志，先调用 `search_large_log(pattern, path=...)` 或 `bash(\"rg -n ...\")`。\n"
            "- 禁止用连续 `read_file` 分页扫描大日志；只有定位到行号后才用 `read_file(path, start_line, end_line)` 精读。\n"
            "- 普通 JSON/Markdown 报告可直接用 `read_file`。\n\n"
            "### 推荐启动排查关键词\n"
            "- `kill self for unity start dead`\n"
            "- `startCheck timeout`\n"
            "- `onUnityStartDeadTraceDump`\n"
            "- `onUnityHeartbeatSignal`\n"
            "- `X3DCB-DROP`\n"
            "- `没有注册回调函数`\n"
        )

        return prompt
    def _run_bug_agent_summary_via_api(
        self,
        *,
        request_text: str,
        request_artifact: Path,
        metadata_path: Path,
        output_path: Path,
        followup_text: str = "",
        previous_summary_path: Path | None = None,
        progress_callback: Callable[[dict[str, object]], None] | None,
        snapshot_details: dict[str, object] | None = None,
        snapshot_plans: list["BugAnalysisPlan"] | None = None,
    ) -> dict[str, object]:
        """Run bug summary via direct LLM API (fast path, no subprocess)."""
        from ..llm_client import LLMClient, LLMClientError

        ai_opts = self.config.ai_provider
        provider_tag = "direct_api"
        started = time.monotonic()
        client = LLMClient(ai_opts)
        if not client.is_available():
            return {
                "message": "",
                "command": None,
                "error": "direct_api_not_configured",
                "provider": provider_tag,
                "session_id": "",
                "resumed": False,
                "duration_seconds": time.monotonic() - started,
                "usage": {},
                "usage_scope": "",
            }
        prompt = self._build_bug_agent_summary_prompt_for_api(
            request_text=request_text,
            request_artifact=request_artifact,
            metadata_path=metadata_path,
            followup_text=followup_text,
            previous_summary_path=previous_summary_path,
            snapshot_details=snapshot_details,
            snapshot_plans=snapshot_plans,
        )
        if not prompt.strip():
            return {
                "message": "",
                "command": None,
                "error": "direct_api_prompt_empty",
                "provider": provider_tag,
                "session_id": "",
                "resumed": False,
                "duration_seconds": time.monotonic() - started,
                "usage": {},
                "usage_scope": "",
            }
        embedded_files = self._direct_api_bug_summary_embedded_files(
            request_artifact=request_artifact,
            metadata_path=metadata_path,
            followup_text=followup_text,
            previous_summary_path=previous_summary_path,
        )
        prompt_file, context_file = self._write_bug_agent_summary_audit(
            {
                "provider": provider_tag,
                "session_id": "",
                "resumed": False,
                "prompt": prompt,
                "embedded_files": embedded_files,
            },
            output_path,
        )
        self._emit_progress(
            progress_callback,
            stage="bug_agent_summary_direct_api",
            message="直接调用 API 整理最终结论（快速通道）",
            provider=provider_tag,
            model=ai_opts.primary_model,
        )
        system_prompt = (
            "你是一个通过飞书触发的 bug 分析总结 agent。"
            "只读分析，不修改文件，不执行写入命令。"
            "必须完整响应用户原始请求中的所有诉求，输出中文 Markdown，结论先行。"
            "所有分析数据已内嵌在用户消息中，直接基于这些数据分析即可。"
        )
        try:
            response = client.generate_summary(
                system_prompt=system_prompt,
                user_prompt=prompt,
            )
        except LLMClientError as exc:
            logger.warning("Direct API bug summary failed: %s", exc)
            return {
                "message": "",
                "command": None,
                "error": f"direct_api_error: {exc}",
                "provider": provider_tag,
                "session_id": "",
                "resumed": False,
                "duration_seconds": time.monotonic() - started,
                "usage": {},
                "usage_scope": "",
                "prompt_file": str(prompt_file) if prompt_file is not None else "",
                "context_file": str(context_file) if context_file is not None else "",
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("Direct API bug summary unexpected error: %s", exc)
            return {
                "message": "",
                "command": None,
                "error": f"direct_api_unexpected: {exc}",
                "provider": provider_tag,
                "session_id": "",
                "resumed": False,
                "duration_seconds": time.monotonic() - started,
                "usage": {},
                "usage_scope": "",
                "prompt_file": str(prompt_file) if prompt_file is not None else "",
                "context_file": str(context_file) if context_file is not None else "",
            }
        message = (response.content or "").strip()
        if not message:
            return {
                "message": "",
                "command": None,
                "error": "direct_api_empty_response",
                "provider": provider_tag,
                "session_id": "",
                "resumed": False,
                "duration_seconds": time.monotonic() - started,
                "usage": {},
                "usage_scope": "",
                "prompt_file": str(prompt_file) if prompt_file is not None else "",
                "context_file": str(context_file) if context_file is not None else "",
            }
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(message, encoding="utf-8")
        except OSError:
            pass
        duration = time.monotonic() - started
        self._emit_progress(
            progress_callback,
            stage="bug_agent_summary_completed",
            message=f"直接 API 已整理最终结论（{duration:.1f}s）",
            provider=provider_tag,
            model=response.model or ai_opts.primary_model,
            output_path=str(output_path),
        )
        return {
            "message": message,
            "command": None,
            "error": "",
            "provider": provider_tag,
            "model": response.model or ai_opts.primary_model,
            "session_id": "",
            "resumed": False,
            "duration_seconds": duration,
            "usage": response.usage,
            "usage_scope": "direct_api",
            "prompt_file": str(prompt_file) if prompt_file is not None else "",
            "context_file": str(context_file) if context_file is not None else "",
        }
    def _build_bug_agent_summary_prompt_for_api(
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
        """Build summary prompt with inlined file contents for direct API calls.

        Unlike the subprocess path where codex/claude can read files via tools,
        the direct API path must embed all relevant context inline.
        """
        max_file_chars = 12000
        compaction_profile = self._direct_api_compaction_profile(
            metadata_path=metadata_path,
            snapshot_details=snapshot_details,
        )
        metadata_contract_text = self._read_text_excerpt(metadata_path, 20000)
        requires_android_boundary = (
            compaction_profile in {"xtheme_signal_boundary", "scene_signal_target_focus"}
            or "xtheme-analyzer" in metadata_contract_text
            or "scene-signal-diagnosis" in metadata_contract_text
            or "report_requires_android_unity_boundary" in metadata_contract_text
        )
        prompt = "请基于以下已内嵌的分析材料完成同一个 bug 会话的最终回答。\n"
        prompt += "注意：所有相关文件内容已内嵌在本消息中，无需读取本地文件。\n\n要求：\n"
        snapshot_prefix = ""
        if followup_text.strip():
            prompt += (
                "1. 这是一条续聊/追问，必须直接回答这次新问题，并延续上一轮分析。\n"
                "2. 优先复用已内嵌的元数据和报告材料，不要要求用户重新上传日志。\n"
                "3. 只读分析，不修改任何文件。\n"
                "4. 输出中文 Markdown，结论先行，再给出证据。\n"
                "5. 如果现有材料仍不足以覆盖某个诉求，要明确指出缺口，但先回答已经能确认的部分。\n"
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
                "1. 本次是全新 bug 分析请求，不是续聊/修正；不要虚构\u201c上一轮分析\u201d\u201c本次修正\u201d\u201c延续上一轮\u201d这类诉求或标题。\n"
                "2. 必须完整覆盖用户原始请求里的所有诉求，不要只回答其中一部分。\n"
                "3. 只读分析，不修改任何文件。\n"
                "4. 输出中文 Markdown，结论先行；若有多个诉求，按诉求分组说明结论和证据；若只有一个诉求，只在开头说明一次，"
                "不要在每条结论或证据前重复写相同的诉求。诉求标题只能来自用户原始请求，不要自行添加不存在的诉求。\n"
                "5. 如果材料无法覆盖用户某个诉求，要明确指出缺口。\n"
                "6. 已内嵌元数据中的\u201c本轮脚本初步摘要\u201d只是当前自动脚本输出，不要把它写成\u201c上一轮结论\u201d；"
                "只有显式提供 followup/previous summary 时，才能讨论修正上一轮结论。\n"
                "7. 如果元数据或报告里已经明确给出故障时间对应的主会话 / 主 PID / focus session，"
                "请优先围绕该主会话分析，不要展开无关会话；只有在需要证明时间不匹配时才提及其他会话。\n\n"
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
        if requires_android_boundary:
            prompt += (
                "本轮命中 Android/Unity 边界类主题或场景报告，最终回答必须在 `## 关键证据` 前额外输出：\n"
                "## Android 最终状态\n"
                "- 明确 Android 侧最终输入、计算结果、当前 XTheme 输出或场景信号输出是否正确；"
                "XTheme 场景必须写清 `ThemeMode`、`TimePeriod`、`SrThemeSkin`、`XThemeStrategy code`。\n"
                "## 责任边界\n"
                "- 明确到底是 Android 输入/计算/发送侧问题，还是 Android 已发送正确后需要转 Unity/3D 消费、资源、图层或显示侧补证；"
                "不要只写“已发送/未发送”。\n"
                "结论摘要第一条也必须直接回答：当前 XTheme/场景信号最终状态是什么，上游输入是什么，现象若仍存在应归到哪条链路。\n\n"
            )
        if snapshot_prefix:
            prompt += snapshot_prefix
        prompt += f"### 用户原始请求\n{request_text}\n\n"
        if followup_text.strip():
            prompt += f"### 本次追问/修正\n{followup_text.strip()}\n\n"
        if previous_summary_path is not None and (
            not followup_text.strip()
            or self._should_include_previous_summary_for_followup(
                followup_text=followup_text,
                request_artifact=request_artifact,
                metadata_path=metadata_path,
            )
        ):
            prev_text = self._read_text_excerpt(previous_summary_path, max_file_chars)
            if prev_text:
                prompt += f"### 上一轮 Agent 总结\n{prev_text}\n\n"
        request_text_content = self._read_text_excerpt(request_artifact, max_file_chars)
        if request_text_content:
            prompt += f"### Bug Agent Request 文件内容\n{request_text_content}\n\n"
        metadata_text = self._read_text_excerpt(metadata_path, max_file_chars)
        if metadata_text:
            prompt += f"### Bug Metadata 文件内容\n{metadata_text}\n\n"
        for item in self._bug_summary_context_items(metadata_path, include_html_reports=False):
            path = Path(str(item["path"]))
            title = str(item["title"])
            guardrails = self._render_structured_report_guardrails(path)
            if guardrails:
                prompt += f"{guardrails}\n\n"
            content = self._direct_api_bug_summary_context_excerpt(
                title=title,
                path=path,
                max_chars=max_file_chars,
                compaction_profile=compaction_profile,
            )
            if content:
                prompt += f"### {title}\n来源: {path}\n{content}\n\n"
        return prompt
