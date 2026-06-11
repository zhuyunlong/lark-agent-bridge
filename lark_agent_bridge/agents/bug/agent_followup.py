from __future__ import annotations

from ._shared import *  # noqa: F401,F403


class _BugAgentFollowupMixin:
    """Bug 追问续聊：复用上轮 agent 会话直接回答追问（与 RunReanalysisMixin 共享 self 状态）。"""

    def run_bug_agent_followup(
        self,
        *,
        followup_text: str,
        previous_context: object,
        previous_session: dict[str, object],
        event: LarkEvent | None = None,
        progress_callback: Callable[[dict[str, object]], None] | None = None,
        resume_agent_session: bool = False,
        agent_provider_override: str = "",
        bridge_session_id: str = "",
    ) -> TaskResult:
        started = time.monotonic()
        bridge_session_id = bridge_session_id.strip() or self._bridge_session_id(event)
        details = previous_session.get("details", {})
        if not isinstance(details, dict):
            details = {}
        job_id = str(previous_session.get("job_id") or "").strip()
        job_dir_value = str(previous_session.get("job_dir") or "").strip()
        if not job_id and job_dir_value:
            job_id = Path(job_dir_value).name
        if not job_id:
            return TaskResult(
                success=False,
                message="无法延续上次 Bug 分析：未找到上一轮 job_id。",
                error_code="bug_agent_followup_missing_job",
                details={"mode": "bug_agent_followup"},
            )
        job_dir = Path(job_dir_value) if job_dir_value else self.config.data_dir / "jobs" / job_id
        output_dir = job_dir / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        request_text = str(getattr(previous_context, "request_text", "") or details.get("user_request_text") or "").strip()
        prepared_input = self._path_from_details(details, "prepared_log_input")
        selected_input = self._path_from_details(details, "selected_log_input")
        previous_summary_path = self._path_from_details(details, "agent_summary_file")
        if previous_summary_path is not None and not previous_summary_path.exists():
            previous_summary_path = None
        report_files = self._collect_bug_output_artifacts(output_dir)
        plans = self._plans_from_previous_details(details, fallback_text=request_text)
        provider_session_id = self._bug_followup_resume_session_id(details, force=resume_agent_session)
        self._emit_progress(
            progress_callback,
            stage="bug_agent_followup_prepare",
            message=(
                "复用上一轮 bug 资料并新开本地 Agent 分析追问"
                if not provider_session_id
                else "复用上一轮 bug 资料并继续原本地 Agent 会话分析追问"
            ),
            job_id=job_id,
            prepared_log_input=str(prepared_input or ""),
            selected_log_input=str(selected_input or ""),
            output_dir=str(output_dir),
            provider_session_id=provider_session_id,
            analysis_kinds=[plan.kind for plan in plans],
        )
        agent_request_path = output_dir / "bug_agent_followup_request.md"
        agent_request_path.write_text(
            self._render_bug_agent_followup_request(
                request_text=request_text,
                followup_text=followup_text,
                history=getattr(previous_context, "history", None),
            ),
            encoding="utf-8",
        )
        agent_metadata_path = output_dir / "bug_agent_followup_metadata.md"
        agent_metadata_path.write_text(
            self._render_bug_agent_followup_metadata(
                request_text=request_text,
                followup_text=followup_text,
                job_id=job_id,
                job_dir=job_dir,
                output_dir=output_dir,
                prepared_input=prepared_input,
                selected_input=selected_input,
                previous_summary_path=previous_summary_path,
                report_files=report_files,
                report_url=str(getattr(previous_context, "report_url", "") or ""),
                analysis_skill=str(details.get("analysis_skill") or ""),
            ),
            encoding="utf-8",
        )
        agent_summary_path = previous_summary_path or (output_dir / "bug_agent_summary.md")
        snapshot_details = self._structured_bug_prompt_snapshot_details(
            base_details=details,
            request_text=request_text,
            plans=plans,
            target_time=str(details.get("target_time") or details.get("fault_time") or ""),
            prepared_input=prepared_input,
            selected_input=selected_input,
        )
        agent_summary_result = self._run_bug_agent_summary(
            request_text=request_text,
            request_artifact=agent_request_path,
            metadata_path=agent_metadata_path,
            output_path=agent_summary_path,
            progress_callback=progress_callback,
            timeout=self._agent_summary_timeout(
                self.config.bug_analysis.timeout_seconds,
                reference_seconds=self._agent_summary_timeout_reference(previous_session),
            ),
            provider_session_id=provider_session_id,
            followup_text=followup_text,
            previous_summary_path=previous_summary_path,
            snapshot_details=snapshot_details,
            snapshot_plans=plans,
            prefer_lightweight=self._should_prefer_lightweight_bug_summary(
                request_text=request_text,
                followup_text=followup_text,
                provider_session_id=provider_session_id,
            ),
            provider_override=agent_provider_override,
            bridge_session_id=bridge_session_id,
        )
        self._append_agent_runtime_metadata(
            agent_metadata_path,
            agent_summary_result=agent_summary_result,
            total_duration_seconds=time.monotonic() - started,
        )
        report_html_paths = [path for path in report_files if path.suffix.lower() == ".html"]
        self._annotate_html_reports(
            report_html_paths,
            agent_summary_result=agent_summary_result,
            total_duration_seconds=time.monotonic() - started,
        )
        result_details = {
            "mode": "bug_agent_followup",
            "analysis_kinds": [plan.kind for plan in plans],
            "prepared_log_input": str(prepared_input or ""),
            "selected_log_input": str(selected_input or ""),
            "user_request_text": request_text,
            "followup_text": followup_text,
            "selected_agent_provider": agent_provider_override,
            "agent_request_file": str(agent_request_path),
            "followup_metadata_file": str(agent_metadata_path),
            "agent_summary_file": str(agent_summary_path),
        }
        self._apply_agent_runtime_details(result_details, agent_summary_result)
        if not agent_summary_result["message"]:
            error = str(agent_summary_result["error"] or "")
            if error == "agent_summary_not_configured":
                message = "Bug 续聊失败：未配置可继续会话的本地 Agent。"
            elif error:
                message = f"Bug 续聊失败：本地 Agent 未返回结果（{error}）。"
            else:
                message = "Bug 续聊失败：本地 Agent 未返回结果。"
            return TaskResult(
                success=False,
                message=message,
                job_id=job_id,
                job_dir=job_dir,
                command=list(agent_summary_result["command"]) if agent_summary_result["command"] else None,
                duration_seconds=time.monotonic() - started,
                error_code="bug_agent_followup_failed",
                details=result_details,
            )
        self._emit_progress(
            progress_callback,
            stage="bug_agent_followup_completed",
            message="Bug 续聊已由本地 Agent 完成",
            job_id=job_id,
            provider=str(agent_summary_result["provider"] or ""),
            provider_session_id=str(agent_summary_result["session_id"] or provider_session_id),
        )
        return TaskResult(
            success=True,
            message=str(agent_summary_result["message"]),
            job_id=job_id,
            job_dir=job_dir,
            command=list(agent_summary_result["command"]) if agent_summary_result["command"] else None,
            duration_seconds=time.monotonic() - started,
            details=result_details,
        )
    def _bug_followup_resume_session_id(self, details: dict[str, object], *, force: bool = False) -> str:
        if not force and not self.config.bug_analysis.resume_followup_sessions:
            return ""
        return str(details.get("agent_summary_session_id") or "").strip()
