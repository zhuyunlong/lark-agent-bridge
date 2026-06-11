from __future__ import annotations

from ._shared import *  # noqa: F401,F403


_FOCUS_LOOKBACK_SECONDS = 3600
_FOCUS_FORWARD_BUFFER_SECONDS = 300
_FOCUS_MAX_LINES_PER_FILE = 15000
_FOCUS_MIN_LINES_PER_FILE = 2000
_FOCUS_MAX_LOOKBACK_SECONDS = 21600
_FOCUS_EXPAND_STEP_SECONDS = 3600
_FOCUS_LOGD_PREFIXES = ("kernel", "main", "events", "crash")


class _AgentOutputMixin:
    """Agent 输出提取与失败兜底：JSON/流式 markdown 提取、失败文案、source-stage 追问与超时参数（与 CustomSkillMixin 共享 self 状态）。"""

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
