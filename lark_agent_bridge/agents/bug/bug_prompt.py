from __future__ import annotations

from ._shared import *  # noqa: F401,F403


from .skill_report_render import _SkillReportRenderMixin, parse_node_status_block  # noqa: F401
from .summary_backend import _SummaryBackendMixin
from .prompt_snapshot import _PromptSnapshotMixin
from .summary_context import _SummaryContextMixin


class _BugPromptMixin(_SkillReportRenderMixin, _SummaryBackendMixin, _PromptSnapshotMixin, _SummaryContextMixin):
    def _run_ld_direct_api_analysis(
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
    ) -> dict[str, object]:
        """Run LD lane-level analysis via executor + direct LLM API (fast path)."""
        from ..llm_client import LLMClient, LLMClientError

        started = time.monotonic()
        analysis_dir.mkdir(parents=True, exist_ok=True)
        analysis_markdown_path = analysis_dir / self._skill_agent_analysis_markdown_name("ld_lane_level")
        context_path = analysis_dir / self._skill_agent_sidecar_name("ld_lane_level", "context.md")
        provider_tag = "direct_api"

        # Phase 1: Executor — extract LD evidence
        self._emit_progress(
            progress_callback,
            stage="ld_executor_extract",
            message="LD 车道级执行器：正在提取 montecarlo 日志证据",
        )
        cache_dir = self._resolve_bug_cache_dir(prepared_input or selected_input)
        log_files = self._ld_executor_find_log_files(
            cache_dir=cache_dir,
            fault_time=fault_time,
        ) if cache_dir else []
        evidence_text = self._ld_executor_extract_evidence(
            log_files=log_files,
            fault_time=fault_time,
        )
        executor_duration = time.monotonic() - started
        self._emit_progress(
            progress_callback,
            stage="ld_executor_done",
            message=f"LD 执行器完成：{len(log_files)} 文件，{executor_duration:.1f}s",
            log_file_count=len(log_files),
        )

        # Read SKILL.md
        skill_record = self.skill_manager.get_skill(skill_name, include_content=True)
        skill_content = skill_record.content if skill_record.content else ""

        # Read reference file
        reference_content = ""
        for ref_path in self._skill_context_paths(skill_name):
            for md_file in sorted(ref_path.glob("*.md")) if ref_path.is_dir() else ([ref_path] if ref_path.suffix == ".md" else []):
                try:
                    ref_text = md_file.read_text(encoding="utf-8", errors="replace")
                    if len(reference_content) + len(ref_text) < 35000:
                        reference_content += f"\n\n---\n### Reference: {md_file.name}\n{ref_text}"
                except OSError:
                    continue

        # Phase 2: Direct API call
        ai_opts = self.config.ai_provider
        client = LLMClient(ai_opts)
        if not client.is_available():
            return {
                "ok": False,
                "error_code": "ld_direct_api_not_configured",
                "message": "LD 车道级分析：direct_api 未配置（ai_provider 不可用），需要 fallback 到 file_agent。",
                "command": [],
                "provider": provider_tag,
                "analysis_markdown_path": analysis_markdown_path,
                "duration_seconds": time.monotonic() - started,
            }

        self._emit_progress(
            progress_callback,
            stage="ld_direct_api_call",
            message=f"LD 车道级分析：调用 API 进行链路分析（{ai_opts.primary_model}）",
            provider=provider_tag,
            model=ai_opts.primary_model,
        )

        system_prompt = (
            "你是一个专业的 LD 车道级日志分析 Agent。"
            "你的任务是根据 SKILL.md 的分析方法论和已预提取的日志证据，分析 LD 车道级渲染问题的根因。"
            "只读分析，不修改文件。输出中文 Markdown。"
            "必须包含这些二级标题：## 结论摘要、## 关键证据、## 最可能原因、## 待确认项、## 建议动作。"
            "## 关键证据 不能为空，每条证据要回指到具体日志行和时间。"
            "证据不足时明确写待确认，不要编造。"
        )

        user_prompt = (
            f"# LD 车道级渲染问题分析\n\n"
            f"## Bug 信息\n"
            f"- 标题: {title}\n"
            f"- 故障时间: {fault_time}\n"
            f"- 用户请求: {request_text}\n"
            f"- 缺陷描述: {description}\n\n"
        )
        if skill_content:
            # Truncate SKILL.md to key sections
            user_prompt += f"## 分析方法论 (SKILL.md)\n{skill_content[:6000]}\n\n"
        if reference_content:
            user_prompt += f"## 参考资料\n{reference_content[:25000]}\n\n"
        user_prompt += evidence_text

        # Write context for audit
        context_path.write_text(user_prompt[:5000] + "\n\n[... truncated for audit ...]\n", encoding="utf-8")

        try:
            response = client.generate_summary(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
        except LLMClientError as exc:
            logger.warning("LD direct API analysis failed: %s", exc)
            return {
                "ok": False,
                "error_code": "ld_direct_api_error",
                "message": f"LD 车道级分析 API 调用失败：{exc}",
                "command": [],
                "provider": provider_tag,
                "analysis_markdown_path": analysis_markdown_path,
                "duration_seconds": time.monotonic() - started,
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("LD direct API unexpected error: %s", exc)
            return {
                "ok": False,
                "error_code": "ld_direct_api_unexpected",
                "message": f"LD 车道级分析 API 意外错误：{exc}",
                "command": [],
                "provider": provider_tag,
                "analysis_markdown_path": analysis_markdown_path,
                "duration_seconds": time.monotonic() - started,
            }

        message = (response.content or "").strip()
        if not message:
            return {
                "ok": False,
                "error_code": "ld_direct_api_empty",
                "message": "LD 车道级分析 API 返回空响应。",
                "command": [],
                "provider": provider_tag,
                "analysis_markdown_path": analysis_markdown_path,
                "duration_seconds": time.monotonic() - started,
            }

        # Write analysis output
        analysis_markdown_path.write_text(message + "\n", encoding="utf-8")
        duration = time.monotonic() - started

        # Validate
        valid, reason, evidence_count = self._validate_custom_skill_analysis(analysis_markdown_path)
        if not valid:
            return {
                "ok": False,
                "error_code": "ld_direct_api_invalid_evidence",
                "message": f"LD 车道级 direct_api 分析输出缺少有效 `## 关键证据`：{reason}",
                "command": [],
                "provider": provider_tag,
                "analysis_markdown_path": analysis_markdown_path,
                "duration_seconds": duration,
                "evidence_count": evidence_count,
            }

        # Generate report
        self._write_custom_skill_agent_report(
            analysis_kind="ld_lane_level",
            analysis_label=self._analysis_label("ld_lane_level"),
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
            duration_seconds=duration,
        )

        self._emit_progress(
            progress_callback,
            stage="ld_direct_api_done",
            message=f"LD 车道级分析完成（{duration:.1f}s, executor+direct_api）",
            provider=provider_tag,
            model=response.model or ai_opts.primary_model,
            evidence_count=evidence_count,
        )

        return {
            "ok": True,
            "error_code": "",
            "message": "",
            "command": [],
            "analysis_kind": "ld_lane_level",
            "provider": provider_tag,
            "analysis_markdown_path": analysis_markdown_path,
            "html_path": html_path,
            "json_path": json_path,
            "context_path": context_path,
            "duration_seconds": duration,
            "evidence_count": evidence_count,
            "custom_skill_analysis_status": "completed",
            "execution_backend": "ld_executor_direct_api",
        }
    def _resolve_bug_cache_dir(self, input_path: Path | None) -> Path | None:
        """Walk up from input_path to find the bug_cache/<id>/ directory."""
        if input_path is None:
            return None
        current = input_path.resolve()
        for _ in range(10):
            if current.name == "logs" and (current.parent / "attachments").exists():
                return current.parent
            if current.name.startswith("xpfailuremgmt_") or current.name.startswith("bug_"):
                return current
            if current.parent == current:
                break
            current = current.parent
        # Fallback: try to find bug_cache in known data dir
        data_dir = Path(self.config.workspace_root) / "tools" / "lark-agent-bridge" / "data" / "bug_cache"
        if not data_dir.exists():
            data_dir = Path(__file__).resolve().parent.parent.parent / "data" / "bug_cache"
        if data_dir.exists():
            # Return the most recent bug cache dir
            candidates = sorted(data_dir.iterdir(), key=lambda p: p.stat().st_mtime if p.is_dir() else 0, reverse=True)
            if candidates:
                return candidates[0]
        return None
