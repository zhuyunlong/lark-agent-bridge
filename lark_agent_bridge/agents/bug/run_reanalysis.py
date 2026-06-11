from __future__ import annotations

from ._shared import *  # noqa: F401,F403


from .agent_followup import _BugAgentFollowupMixin


class _RunReanalysisMixin(_BugAgentFollowupMixin):
    def run_bug_reanalysis(
        self,
        *,
        followup_text: str,
        previous_context: object,
        previous_session: dict[str, object],
        event: LarkEvent | None = None,
        progress_callback: Callable[[dict[str, object]], None] | None = None,
        force_rerun: bool = False,
        plans_override: list["BugAnalysisPlan"] | None = None,
        classification_skill: str = "",
        classification_source: str = "",
        classification_reason: str = "",
        classification_provider: str = "",
        agent_provider_override: str = "",
        local_log_resources: list[DownloadResource] | None = None,
        bridge_session_id: str = "",
    ) -> TaskResult:
        started = time.monotonic()
        bridge_session_id = bridge_session_id.strip() or self._bridge_session_id(event)
        bridge_kwargs = {"bridge_session_id": bridge_session_id} if bridge_session_id else {}
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
                message="无法复用上次 Bug 分析：未找到上一轮 job_id。",
                error_code="bug_reanalysis_missing_job",
                details={"mode": "bug_reanalysis"},
            )
        job_dir = Path(job_dir_value) if job_dir_value else self.config.data_dir / "jobs" / job_id
        result_context = type("Context", (), {"job_id": job_id, "job_dir": job_dir})()
        output_dir = job_dir / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        download_retry_result: dict[str, object] | None = None
        request_text = self._original_bug_request_text(previous_context, details)
        local_log_resources = local_log_resources or []
        local_selected_input: Path | None = None
        local_prepared_input: Path | None = None
        if local_log_resources:
            for resource in local_log_resources:
                if resource.kind != "local":
                    continue
                candidate = Path(resource.value).expanduser()
                if not candidate.exists():
                    continue
                local_selected_input = candidate.resolve()
                try:
                    local_prepared_input = self._prepare_log_input(local_selected_input)
                except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                    return TaskResult(
                        success=False,
                        message=f"本地日志准备失败：{exc}",
                        job_id=job_id,
                        job_dir=job_dir,
                        duration_seconds=time.monotonic() - started,
                        error_code="bug_reanalysis_local_log_prepare_failed",
                        details={
                            "mode": "bug_reanalysis",
                            "local_log_resources": [item.value for item in local_log_resources],
                        },
                    )
                self._emit_progress(
                    progress_callback,
                    stage="bug_reanalysis_local_log_selected",
                    message="使用卡片输入中授权的本地日志文件",
                    selected_log_input=str(local_selected_input),
                    prepared_log_input=str(local_prepared_input),
                )
                break
        reference_text = "\n".join(
            [
                request_text,
                f"故障时间: {str(details.get('target_time') or details.get('fault_time') or '').strip()}",
            ]
        ).strip()
        concise_reference_text = self._trim_reanalysis_reference_text(reference_text)
        # Recover bug title from metadata so follow-ups like
        # "请根据BUG问题实际时间继续分析" can extract the correct time.
        recovered_bug_title = self._recover_bug_title_from_metadata(output_dir)
        time_context = self._resolve_bug_time_context(
            request_text=followup_text,
            title=recovered_bug_title,
            description=reference_text,
            reference_time=str(details.get("target_time") or details.get("fault_time") or "").strip(),
        )
        target_time = time_context.fault_time
        prepared_input = local_prepared_input or self._path_from_details(details, "prepared_log_input")
        selected_input = local_selected_input or self._path_from_details(details, "selected_log_input") or prepared_input
        explicit_source_followup = self._followup_explicitly_requests_source_analysis(
            request_text,
            followup_text,
        )
        reanalysis_selection: BugAnalysisSelection | None = None
        if plans_override is None and explicit_source_followup:
            candidate_selection = self._manual_followup_selection_if_explicit(
                followup_text=followup_text,
                request_text=request_text,
            )
            if candidate_selection is not None and any(plan.kind != "general" for plan in candidate_selection.plans):
                reanalysis_selection = candidate_selection
                if not classification_skill:
                    classification_skill = candidate_selection.skill_name
                if not classification_source:
                    classification_source = candidate_selection.source
                if not classification_reason:
                    classification_reason = "源码追问按原始 bug 请求重建领域上下文。"
                if not classification_provider:
                    classification_provider = candidate_selection.provider
        plans = plans_override or (
            [BugAnalysisPlan(kind=plan.kind, signal_code=plan.signal_code) for plan in reanalysis_selection.plans]
            if reanalysis_selection is not None
            else self._plans_for_reanalysis(details, request_text=request_text, followup_text=followup_text)
        )
        source_decision_skill_name = classification_skill or str(details.get("analysis_skill") or "").strip()
        if (
            plans_override is None
            and all(_kind_spec(plan.kind).is_source_stage for plan in plans)
            and not explicit_source_followup
        ):
            fallback_plans = self._fallback_non_source_plans_from_job_output(output_dir)
            if fallback_plans:
                plans = fallback_plans
                if source_decision_skill_name == "source_analysis":
                    source_decision_skill_name = ""
        source_decision = self._decide_source_analysis_request(
            request_text=request_text,
            prompt_text=followup_text,
            title="",
            description=concise_reference_text,
            plans=plans,
            skill_name=source_decision_skill_name,
        )
        plans = self._augment_plans_for_source_analysis(plans, source_decision=source_decision)
        if source_decision.requested and not classification_skill and all(_kind_spec(plan.kind).is_source_stage for plan in plans):
            classification_skill = "source_analysis"
        requires_log_input = any(self._plan_requires_log_input(plan) for plan in plans)
        if requires_log_input and not time_context.has_full_datetime:
            return self._bug_time_clarification_result(
                context=result_context,
                started=started,
                request_text=f"{request_text}\n追问/修正：{followup_text}".strip(),
                bug_url=str(details.get("bug_url") or ""),
                time_context=time_context,
                status="missing_fault_time",
                progress_callback=progress_callback,
            )
        if requires_log_input and prepared_input is None:
            recovered_selected, recovered_prepared = self._recover_cached_bug_log_input(details, request_text=request_text)
            if recovered_prepared is not None:
                selected_input = recovered_selected or recovered_prepared
                prepared_input = recovered_prepared
                self._emit_progress(
                    progress_callback,
                    stage="bug_reanalysis_recover_bug_cache",
                    message="从同一 bug cache 恢复已下载日志输入",
                    selected_log_input=str(selected_input or ""),
                    prepared_log_input=str(prepared_input),
                )
        if requires_log_input and prepared_input is None:
            retry = self._retry_bug_log_download(
                previous_session=previous_session,
                job_dir=job_dir,
                progress_callback=progress_callback,
                **bridge_kwargs,
            )
            download_retry_result = retry
            if retry.get("prepared_input") is not None:
                prepared_input = retry["prepared_input"]
                selected_input = retry.get("selected_input") or prepared_input
            else:
                failure_message = str(retry.get("message") or "无法复用上次 Bug 分析：未找到已准备好的日志输入，且重新下载日志失败。")
                failure_details = {
                    "mode": "bug_reanalysis",
                    "download_retry": retry,
                }
                return TaskResult(
                    success=False,
                    message=failure_message,
                    job_id=job_id,
                    job_dir=job_dir,
                    duration_seconds=time.monotonic() - started,
                    error_code="bug_reanalysis_missing_prepared_input",
                    details=failure_details,
                )

        log_coverage: LogCoverage | None = None
        if requires_log_input:
            log_coverage = self._scan_log_time_coverage(prepared_input, fault_time=target_time) if prepared_input else None
            if log_coverage is None or not log_coverage.has_time_evidence:
                return self._bug_time_clarification_result(
                    context=result_context,
                    started=started,
                    request_text=f"{request_text}\n追问/修正：{followup_text}".strip(),
                    bug_url=str(details.get("bug_url") or ""),
                    time_context=time_context,
                    status="log_time_unknown",
                    progress_callback=progress_callback,
                    log_coverage=log_coverage,
                )
            if not log_coverage.covers_fault_time:
                return self._bug_time_clarification_result(
                    context=result_context,
                    started=started,
                    request_text=f"{request_text}\n追问/修正：{followup_text}".strip(),
                    bug_url=str(details.get("bug_url") or ""),
                    time_context=time_context,
                    status="log_not_covering_fault_time",
                    progress_callback=progress_callback,
                    log_coverage=log_coverage,
                )

        force_rerun_kinds = self._forced_reanalysis_kinds(
            plans,
            request_text=request_text,
            followup_text=followup_text,
            force_rerun=force_rerun,
        )
        prompt_text = f"{request_text}\n追问/修正：{followup_text}".strip()
        source_stage_prompt_text = self._source_stage_followup_prompt_text(
            request_text=request_text,
            followup_text=followup_text,
        )
        source_stage_request_text = self._source_stage_followup_request_text(
            request_text=request_text,
            followup_text=followup_text,
        )
        source_evidence_path = self._write_reanalysis_source_evidence(
            plans=plans,
            request_text=request_text,
            followup_text=followup_text,
            output_dir=output_dir,
            enabled=any(_kind_spec(p.kind).needs_source_evidence for p in plans)
            or bool(force_rerun_kinds.intersection({"signal"}))
            or any(_kind_spec(plan.kind).is_source_stage for plan in plans)
            or self._should_collect_source_evidence(request_text, followup_text),
        )
        self._emit_progress(
            progress_callback,
            stage="bug_reanalysis_reuse_context",
            message="复用上一轮 bug 分析上下文，不重新拉取/下载/解密日志",
            job_id=job_id,
            prepared_log_input=str(prepared_input or ""),
            selected_log_input=str(selected_input or ""),
            target_time=target_time,
            analysis_kinds=[plan.kind for plan in plans],
        )

        html_paths: list[Path] = []
        report_jsons: dict[str, Path | None] = {}
        rerun_kinds: list[str] = []
        reused_kinds: list[str] = []
        command: list[str] | None = None
        skill_file_agent_execution_result: dict[str, object] | None = None
        try:
            for plan in plans:
                html_path = output_dir / self._report_name(plan.kind, "html")
                json_path = output_dir / self._report_name(plan.kind, "json")
                analysis_dir = output_dir / f"{plan.kind}_analysis"
                should_rerun = (
                    plan.kind == "startup"
                    or plan.kind in force_rerun_kinds
                    or not html_path.exists()
                    or not json_path.exists()
                )
                if not should_rerun:
                    self._emit_progress(
                        progress_callback,
                        stage="bug_reanalysis_reuse_report",
                        message=f"复用已生成的{self._analysis_label(plan.kind)}报告",
                        plan=plan.kind,
                        html_path=str(html_path),
                        json_path=str(json_path),
                    )
                    reused_kinds.append(plan.kind)
                    html_paths.append(html_path)
                    report_jsons[plan.kind] = json_path if json_path.exists() else None
                    continue

                input_for_plan = prepared_input
                if plan.kind == "startup":
                    input_for_plan = self._startup_analysis_input(prepared_input, target_time)
                rerun_kinds.append(plan.kind)
                command = self.build_command(
                    plan=plan,
                    input_path=input_for_plan or output_dir,
                    html_path=html_path,
                    json_path=json_path,
                    analysis_dir=analysis_dir,
                    target_time=target_time if _kind_accepts_target_time(plan.kind) else None,
                    request_text=followup_text if plan.kind == "xtheme" else None,
                )
                self._emit_progress(
                    progress_callback,
                    stage="bug_reanalysis_run_analysis",
                    message=(
                        f"基于已准备日志重新执行{self._analysis_label(plan.kind)}"
                        if input_for_plan is not None
                        else f"基于已有上下文重新执行{self._analysis_label(plan.kind)}"
                    ),
                    plan=plan.kind,
                    plan_label=self._analysis_label(plan.kind),
                    html_path=str(html_path),
                    json_path=str(json_path),
                    target_time=target_time if _kind_accepts_target_time(plan.kind) else "",
                )
                if plan.kind == "general":
                    self._write_general_bug_report(
                        html_path=html_path,
                        json_path=json_path,
                        title="",
                        description="",
                        prompt_text=followup_text,
                        request_text=request_text,
                        fault_time=target_time,
                        selected_input=selected_input,
                        source_evidence_path=source_evidence_path,
                        classification_skill=classification_skill or self._skill_name_for_kind(plan.kind),
                        classification_source=classification_source or "manual_fallback",
                        classification_reason=classification_reason or "",
                    )
                    completed = subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")
                elif _kind_spec(plan.kind).is_custom_agent:
                    if plan.kind == "ld_lane_level":
                        skill_name = self._skill_name_for_kind(plan.kind)
                    elif _kind_spec(plan.kind).is_source_stage:
                        if getattr(source_decision, "source_mode", "") == "append" and not getattr(source_decision, "context_profile", ""):
                            return TaskResult(
                                success=False,
                                message="Bug 续聊重分析失败：源码阶段缺少领域上下文，已停止以避免泛化扫描。",
                                job_id=job_id,
                                job_dir=job_dir,
                                command=command,
                                duration_seconds=time.monotonic() - started,
                                error_code="source_stage_missing_context",
                                details={
                                    "mode": "bug_reanalysis",
                                    "analysis_kind": plan.kind,
                                    "analysis_kinds": [item.kind for item in plans],
                                    "source_mode": getattr(source_decision, "source_mode", ""),
                                    "context_profile": getattr(source_decision, "context_profile", ""),
                                    "stage_kinds": list(getattr(source_decision, "stage_kinds", []) or []),
                                },
                            )
                        requested_skill = (
                            classification_skill
                            or str(details.get("analysis_skill") or "").strip()
                            or self._skill_name_for_kind(plan.kind)
                        )
                        skill_name = self._resolve_source_stage_skill_name(requested_skill)
                    else:
                        skill_name = (
                            classification_skill
                            or str(details.get("analysis_skill") or "").strip()
                            or self._skill_name_for_kind(plan.kind)
                        )
                    if _kind_spec(plan.kind).needs_custom_executor_check and skill_name != "source_analysis" and self.skill_manager.custom_skill_executor_for(skill_name) not in {"file_agent", "pydantic_ai"}:
                        prefix = plan.kind if plan.kind in _SOURCE_SKILL_KINDS else SOURCE_STAGE_KIND
                        return TaskResult(
                            success=False,
                            message=self._custom_skill_executor_not_ready_message(
                                skill_name,
                                selected_input=selected_input,
                            ),
                            job_id=job_id,
                            job_dir=job_dir,
                            command=command,
                            duration_seconds=time.monotonic() - started,
                            error_code=f"{prefix}_reanalysis_executor_not_ready",
                            details={
                                "mode": "bug_reanalysis",
                                "analysis_kind": plan.kind,
                                "analysis_skill": skill_name,
                                f"{prefix}_analysis_status": "executor_not_ready",
                                "target_time": target_time,
                                "selected_log_input": str(selected_input or ""),
                                "prepared_log_input": str(prepared_input or ""),
                            },
                        )
                    # LD lane-level reanalysis: try pydantic-ai first
                    if plan.kind == "ld_lane_level":
                        custom_result = self._run_ld_pydantic_ai_analysis(
                            skill_name=skill_name,
                            request_text=request_text,
                            prompt_text=prompt_text,
                            title="",
                            description=reference_text,
                            fault_time=target_time,
                            selected_input=selected_input,
                            prepared_input=prepared_input,
                            html_path=html_path,
                            json_path=json_path,
                            analysis_dir=analysis_dir,
                            progress_callback=progress_callback,
                            prior_findings=self._summarize_prior_report_jsons(report_jsons),
                        )
                        if custom_result.get("ok"):
                            logger.info("LD reanalysis pydantic-ai succeeded, skipping file_agent")
                        else:
                            logger.warning(
                                "LD reanalysis pydantic-ai failed (error=%s), falling back to file_agent",
                                custom_result.get("error_code", "unknown"),
                            )
                            custom_result = self._run_custom_skill_agent_analysis(
                                analysis_kind=plan.kind,
                                analysis_label=self._analysis_label(plan.kind),
                                skill_name=skill_name,
                                request_text=request_text,
                                prompt_text=prompt_text,
                                title="",
                                description=concise_reference_text,
                                fault_time=target_time,
                                selected_input=selected_input,
                                prepared_input=prepared_input,
                                source_evidence_path=source_evidence_path,
                                html_path=html_path,
                                json_path=json_path,
                                analysis_dir=analysis_dir,
                                progress_callback=progress_callback,
                                timeout=self._agent_summary_timeout(
                                    self.config.bug_analysis.timeout_seconds,
                                    reference_seconds=max(
                                        self._agent_summary_timeout_reference(previous_session) or 0.0,
                                        time.monotonic() - started,
                                        240.0,
                                    ),
                                ),
                                bridge_session_id=bridge_session_id,
                                prior_findings=self._summarize_prior_report_jsons(report_jsons),
                            )
                    else:
                        # Source stage reanalysis: prefer app-server-backed file-agent when enabled,
                        # while leaving the later summary stage on the configured AI path.
                        if _kind_spec(plan.kind).is_source_stage:
                            if self._prefer_source_stage_file_agent():
                                self._emit_progress(
                                    progress_callback,
                                    stage="source_stage_app_server_preferred",
                                    message="已启用 codex app-server，源码阶段直接走 file-agent 路径",
                                )
                                execution_skill_name = (
                                    getattr(source_decision, "context_profile", "").strip()
                                    or skill_name
                                )
                                custom_result = self._run_custom_skill_agent_analysis(
                                   analysis_kind=plan.kind,
                                   analysis_label=self._analysis_label(plan.kind),
                                   skill_name=execution_skill_name,
                                   request_text=source_stage_request_text,
                                   prompt_text=source_stage_prompt_text,
                                   title="",
                                   description=concise_reference_text,
                                   fault_time=target_time,
                                   selected_input=selected_input,
                                   prepared_input=prepared_input,
                                   source_evidence_path=source_evidence_path,
                                   html_path=html_path,
                                   json_path=json_path,
                                   analysis_dir=analysis_dir,
                                   progress_callback=progress_callback,
                                   timeout=self._source_stage_file_agent_timeout(
                                       reference_seconds=max(
                                           self._agent_summary_timeout_reference(previous_session) or 0.0,
                                           time.monotonic() - started,
                                           240.0,
                                       ),
                                   ),
                                   bridge_session_id=bridge_session_id,
                                   prior_findings=self._summarize_prior_report_jsons(report_jsons),
                                   context_profile=execution_skill_name,
                                   provider_override="codex",
                                   command_override=self.config.codex_app_server.command,
                                )
                            else:
                                custom_result = self._run_source_stage_pydantic_ai(
                                    analysis_kind=plan.kind,
                                    skill_name=skill_name,
                                    request_text=source_stage_request_text,
                                    prompt_text=source_stage_prompt_text,
                                    title="",
                                    description=concise_reference_text,
                                    fault_time=target_time,
                                    selected_input=selected_input,
                                    prepared_input=prepared_input,
                                    source_evidence_path=source_evidence_path,
                                    html_path=html_path,
                                    json_path=json_path,
                                    analysis_dir=analysis_dir,
                                    progress_callback=progress_callback,
                                    context_profile=getattr(source_decision, "context_profile", ""),
                                    prior_findings=self._summarize_prior_report_jsons(report_jsons),
                                )
                                if custom_result.get("ok"):
                                    logger.info("source_stage reanalysis pydantic-ai succeeded, skipping file_agent")
                                else:
                                    logger.warning(
                                       "source_stage reanalysis pydantic-ai failed (error=%s), falling back to file_agent",
                                       custom_result.get("error_code", "unknown"),
                                    )
                                    self._emit_progress(
                                       progress_callback,
                                       stage="source_stage_pydantic_ai_fallback",
                                       message=f"pydantic-ai 失败（{custom_result.get('error_code')}），切换 file_agent",
                                    )
                                    custom_result = self._run_custom_skill_agent_analysis(
                                       analysis_kind=plan.kind,
                                       analysis_label=self._analysis_label(plan.kind),
                                       skill_name=skill_name,
                                       request_text=source_stage_request_text,
                                       prompt_text=source_stage_prompt_text,
                                       title="",
                                       description=concise_reference_text,
                                       fault_time=target_time,
                                       selected_input=selected_input,
                                       prepared_input=prepared_input,
                                       source_evidence_path=source_evidence_path,
                                       html_path=html_path,
                                       json_path=json_path,
                                       analysis_dir=analysis_dir,
                                       progress_callback=progress_callback,
                                       timeout=self._source_stage_file_agent_timeout(
                                           reference_seconds=max(
                                               self._agent_summary_timeout_reference(previous_session) or 0.0,
                                               time.monotonic() - started,
                                               240.0,
                                           ),
                                       ),
                                       bridge_session_id=bridge_session_id,
                                       prior_findings=self._summarize_prior_report_jsons(report_jsons),
                                       context_profile=getattr(source_decision, "context_profile", ""),
                                    )
                        else:
                            custom_result = self._run_custom_skill_agent_analysis(
                                analysis_kind=plan.kind,
                                analysis_label=self._analysis_label(plan.kind),
                                skill_name=skill_name,
                                request_text=request_text,
                                        prompt_text=source_stage_prompt_text,
                                title="",
                                description=concise_reference_text,
                                fault_time=target_time,
                                selected_input=selected_input,
                                prepared_input=prepared_input,
                                source_evidence_path=source_evidence_path,
                                html_path=html_path,
                                json_path=json_path,
                                analysis_dir=analysis_dir,
                                progress_callback=progress_callback,
                                timeout=self._agent_summary_timeout(
                                   self.config.bug_analysis.timeout_seconds,
                                   reference_seconds=max(
                                       self._agent_summary_timeout_reference(previous_session) or 0.0,
                                       time.monotonic() - started,
                                       240.0,
                                   ),
                                ),
                                bridge_session_id=bridge_session_id,
                                prior_findings=self._summarize_prior_report_jsons(report_jsons),
                                context_profile=getattr(source_decision, "context_profile", "") if _kind_spec(plan.kind).is_source_stage else "",
                            )
                    skill_file_agent_execution_result = custom_result
                    command = list(custom_result.get("command") or command or [])
                    if not custom_result.get("ok"):
                        partial_markdown = str(custom_result.get("partial_markdown") or "").strip()
                        if partial_markdown and _kind_spec(plan.kind).is_source_stage:
                            self._emit_progress(
                                progress_callback,
                                stage="source_stage_partial_timeout",
                                message="源码阶段超时，但已提取到阶段性结论，直接降级回复",
                            )
                            partial_details = {
                                "mode": "bug_reanalysis",
                                "analysis_kind": plan.kind,
                                "analysis_kinds": [plan.kind],
                                "analysis_skill": skill_name,
                                "source_stage_analysis_status": "partial_timeout",
                                "agent_summary_execution_backend": "source_stage_partial",
                                "target_time": target_time,
                                "selected_log_input": str(selected_input or ""),
                                "prepared_log_input": str(prepared_input or ""),
                                "stdout_path": str(custom_result.get("stdout_path") or ""),
                                "stderr_path": str(custom_result.get("stderr_path") or ""),
                                "command_path": str(custom_result.get("command_path") or ""),
                                "context_path": str(custom_result.get("context_path") or ""),
                                "log_focus_manifest_path": str(custom_result.get("log_focus_manifest_path") or ""),
                                "focused_log_input": str(custom_result.get("focused_log_input") or ""),
                                "debug_log_path": str(custom_result.get("debug_log_path") or ""),
                                "analysis_artifact_path": str(custom_result.get("analysis_markdown_path") or ""),
                                "partial_analysis_available": True,
                            }
                            return TaskResult(
                                success=True,
                                message=partial_markdown,
                                job_id=job_id,
                                job_dir=job_dir,
                                command=command,
                                duration_seconds=time.monotonic() - started,
                                details=partial_details,
                                stdout=str(custom_result.get("stdout") or ""),
                                stderr=str(custom_result.get("stderr") or ""),
                            )
                        return TaskResult(
                            success=False,
                            message=str(custom_result.get("message") or "Bug 续聊专用 Skill 文件 Agent 执行失败。"),
                            job_id=job_id,
                            job_dir=job_dir,
                            command=command,
                            duration_seconds=time.monotonic() - started,
                            error_code=self._skill_file_agent_mode_error_code(
                                plan.kind,
                                str(custom_result.get("error_code") or "custom_skill_agent_failed"),
                                mode="bug_reanalysis",
                            ),
                            stdout=str(custom_result.get("stdout") or ""),
                            stderr=str(custom_result.get("stderr") or ""),
                            details={
                                "mode": "bug_reanalysis",
                                "analysis_kind": plan.kind,
                                "analysis_skill": skill_name,
                                f"{'custom_skill' if plan.kind == 'custom_skill' else plan.kind}_analysis_status": "failed",
                                "target_time": target_time,
                                "selected_log_input": str(selected_input or ""),
                                "prepared_log_input": str(prepared_input or ""),
                                "stdout_path": str(custom_result.get("stdout_path") or ""),
                                "stderr_path": str(custom_result.get("stderr_path") or ""),
                                "command_path": str(custom_result.get("command_path") or ""),
                                "context_path": str(custom_result.get("context_path") or ""),
                                "log_focus_manifest_path": str(custom_result.get("log_focus_manifest_path") or ""),
                                "focused_log_input": str(custom_result.get("focused_log_input") or ""),
                                "debug_log_path": str(custom_result.get("debug_log_path") or ""),
                                "analysis_artifact_path": str(custom_result.get("analysis_markdown_path") or ""),
                            },
                        )
                    completed = subprocess.CompletedProcess(
                        args=command,
                        returncode=0,
                        stdout=str(custom_result.get("stdout") or ""),
                        stderr=str(custom_result.get("stderr") or ""),
                    )
                else:
                    analysis_kwargs = {
                        "plan": plan,
                        "input_path": input_for_plan,
                        "html_path": html_path,
                        "json_path": json_path,
                        "analysis_dir": analysis_dir,
                        "timeout": self.config.bug_analysis.timeout_seconds,
                        "target_time": target_time if _kind_accepts_target_time(plan.kind) else None,
                        "request_text": followup_text if plan.kind == "xtheme" else None,
                    }
                    if bridge_session_id:
                        analysis_kwargs["bridge_session_id"] = bridge_session_id
                    completed = self._run_analysis(**analysis_kwargs)
                if completed.returncode != 0:
                    return TaskResult(
                        success=False,
                        message=f"Bug 续聊重分析失败：{self._analysis_label(plan.kind)}脚本执行失败。",
                        job_id=job_id,
                        job_dir=job_dir,
                        command=command,
                        duration_seconds=time.monotonic() - started,
                        error_code=f"bug_reanalysis_{plan.kind}_failed",
                        stdout=completed.stdout,
                        stderr=completed.stderr,
                        details={"mode": "bug_reanalysis"},
                    )
                html_paths.append(html_path)
                report_jsons[plan.kind] = json_path if json_path.exists() else None
        except subprocess.TimeoutExpired as exc:
            return TaskResult(
                success=False,
                message="Bug 续聊重分析超时",
                job_id=job_id,
                job_dir=job_dir,
                command=command,
                duration_seconds=time.monotonic() - started,
                error_code="bug_reanalysis_timeout",
                stdout=_coerce_process_text(exc.stdout),
                stderr=_coerce_process_text(exc.stderr),
                details={"mode": "bug_reanalysis"},
            )

        combined_artifacts = self._build_combined_report_artifacts(
            plans=plans,
            prompt_text=prompt_text,
            fault_time=target_time,
            output_dir=output_dir,
            html_paths=html_paths,
            report_jsons=report_jsons,
            selected_input=selected_input,
            source_evidence_path=source_evidence_path,
        )
        agent_request_path = output_dir / "bug_agent_reanalysis_request.md"
        agent_request_path.write_text(
            self._render_bug_reanalysis_request(
                request_text=request_text,
                followup_text=followup_text,
                target_time=target_time,
                plans=plans,
                history=None,
            ),
            encoding="utf-8",
        )
        agent_metadata_path = output_dir / "bug_reanalysis_metadata.md"
        previous_summary_path = None
        bug_summary_evidence_path = self._write_bug_summary_evidence(
            output_dir=output_dir,
            analysis_kind=plans[0].kind if plans else "general",
            report_jsons=report_jsons,
        )
        agent_metadata_path.write_text(
            self._render_bug_reanalysis_metadata(
                request_text=request_text,
                followup_text=followup_text,
                job_id=job_id,
                target_time=target_time,
                prepared_input=prepared_input,
                selected_input=selected_input,
                plans=plans,
                rerun_kinds=rerun_kinds,
                reused_kinds=reused_kinds,
                html_paths=html_paths,
                report_jsons=report_jsons,
                combined_artifacts=combined_artifacts,
                previous_summary_path=previous_summary_path,
                source_evidence_path=source_evidence_path,
                classification_skill=classification_skill or self._skill_name_for_kind(plans[0].kind if plans else "general"),
                classification_source=classification_source or "manual_fallback",
                classification_reason=classification_reason or "",
                classification_provider=classification_provider or "",
            ),
            encoding="utf-8",
        )
        self._append_bug_summary_evidence_metadata(agent_metadata_path, bug_summary_evidence_path)
        agent_summary_path = output_dir / "bug_agent_summary.md"
        snapshot_details = self._structured_bug_prompt_snapshot_details(
            base_details=details,
            request_text=request_text,
            plans=plans,
            target_time=target_time,
            prepared_input=prepared_input,
            selected_input=selected_input,
        )
        use_source_stage_direct_reply = (
            explicit_source_followup
            and skill_file_agent_execution_result is not None
            and bool(skill_file_agent_execution_result.get("ok"))
            and str(skill_file_agent_execution_result.get("executor") or "") == "codex_app_server"
            and str(skill_file_agent_execution_result.get("completion_state") or "") == "complete"
            and any(_kind_spec(plan.kind).is_source_stage for plan in plans)
        )
        if use_source_stage_direct_reply:
            analysis_path = skill_file_agent_execution_result.get("analysis_markdown_path")
            analysis_message = ""
            if isinstance(analysis_path, Path) and analysis_path.exists():
                analysis_message = analysis_path.read_text(encoding="utf-8", errors="replace").strip()
            agent_summary_result = {
                "message": analysis_message,
                "command": None,
                "error": "",
                "provider": "codex",
                "session_id": "",
                "resumed": False,
                "duration_seconds": 0.0,
                "usage": skill_file_agent_execution_result.get("usage") or {},
                "usage_scope": "source_stage_direct",
                "execution_backend": "source_stage_direct",
                "backend_reason": "explicit_source_stage_output",
            }
            self._emit_progress(
                progress_callback,
                stage="bug_agent_summary_completed",
                message="源码阶段已产出完整 Markdown，直接作为最终回答",
                provider="codex",
            )
        else:
            agent_summary_result = self._run_bug_agent_summary(
                request_text=request_text,
                request_artifact=agent_request_path,
                metadata_path=agent_metadata_path,
                output_path=agent_summary_path,
                progress_callback=progress_callback,
                timeout=self._agent_summary_timeout(
                    self.config.bug_analysis.timeout_seconds,
                    reference_seconds=max(
                        self._agent_summary_timeout_reference(previous_session) or 0.0,
                        time.monotonic() - started,
                    ),
                ),
                provider_session_id="",
                followup_text=followup_text,
                previous_summary_path=previous_summary_path,
                snapshot_details=snapshot_details,
                snapshot_plans=plans,
                prefer_lightweight=self._should_prefer_lightweight_bug_summary(
                    request_text=request_text,
                    followup_text=followup_text,
                    provider_session_id="",
                ),
                provider_override=agent_provider_override,
                bridge_session_id=bridge_session_id,
            )
        self._append_agent_runtime_metadata(
            agent_metadata_path,
            agent_summary_result=agent_summary_result,
            total_duration_seconds=time.monotonic() - started,
        )
        annotated_html_paths = list(html_paths)
        if combined_artifacts is not None:
            annotated_html_paths.append(Path(combined_artifacts["html_path"]))
        self._annotate_html_reports(
            annotated_html_paths,
            agent_summary_result=agent_summary_result,
            total_duration_seconds=time.monotonic() - started,
        )
        if combined_artifacts is not None:
            final_message = str(combined_artifacts["summary"])
            files_to_send = [Path(combined_artifacts["html_path"])]
        else:
            final_message = self._build_direct_analysis_summary(plans, prompt_text, html_paths)
            files_to_send = html_paths
        if agent_summary_result["message"]:
            final_message = str(agent_summary_result["message"])
        self._emit_progress(
            progress_callback,
            stage="bug_reanalysis_completed",
            message="bug 续聊重分析完成",
            job_id=job_id,
            target_time=target_time,
            analysis_kinds=[plan.kind for plan in plans],
            html_reports=[str(path) for path in html_paths],
        )
        result_details = {
            "mode": "bug_reanalysis",
            "analysis_kind": plans[0].kind if plans else "",
            "analysis_kinds": [plan.kind for plan in plans],
            "analysis_skill": classification_skill or self._skill_name_for_kind(plans[0].kind if plans else "general"),
            "analysis_skill_label": self._analysis_label(plans[0].kind) if plans else "通用问题分析",
            "domain_kind": getattr(source_decision, "domain_kind", "") or self._domain_kind_from_plans(plans),
            "source_mode": getattr(source_decision, "source_mode", "") or "off",
            "context_profile": getattr(source_decision, "context_profile", ""),
            "stage_kinds": list(getattr(source_decision, "stage_kinds", []) or []),
            "source_targets": list(getattr(source_decision, "targets", []) or []),
            "classification_source": classification_source or "manual_fallback",
            "classification_reason": classification_reason or "",
            "classification_provider": classification_provider or "",
            "selected_agent_provider": agent_provider_override,
            "selected_log_input": str(selected_input or ""),
            "prepared_log_input": str(prepared_input),
            "local_log_resources": [item.value for item in local_log_resources],
            "user_request_text": request_text,
            "followup_text": followup_text,
            "target_time": target_time,
            "log_coverage_start": log_coverage.start_time if log_coverage else "",
            "log_coverage_end": log_coverage.end_time if log_coverage else "",
            "log_coverage_scanned_files": log_coverage.scanned_files if log_coverage else 0,
            "log_coverage_scanned_lines": log_coverage.scanned_lines if log_coverage else 0,
            "rerun_analysis_kinds": rerun_kinds,
            "reused_analysis_kinds": reused_kinds,
            "agent_request_file": str(agent_request_path),
            "reanalysis_metadata_file": str(agent_metadata_path),
            "agent_summary_file": str(agent_summary_path),
            "files_to_send": files_to_send,
        }
        signal_codes = [plan.signal_code for plan in plans if plan.kind == "signal" and plan.signal_code]
        if signal_codes:
            result_details["signal_code"] = signal_codes[0]
        if source_evidence_path is not None:
            result_details["source_evidence_file"] = str(source_evidence_path)
        if download_retry_result is not None:
            result_details["download_retry"] = download_retry_result
        if combined_artifacts is not None:
            result_details["combined_report_html"] = str(combined_artifacts["html_path"])
            result_details["combined_report_json"] = str(combined_artifacts["json_path"])
        if skill_file_agent_execution_result is not None:
            result_details.update(
                self._skill_file_agent_execution_details(
                    str(skill_file_agent_execution_result.get("analysis_kind") or ""),
                    skill_file_agent_execution_result,
                )
            )
        self._apply_agent_runtime_details(result_details, agent_summary_result)
        return TaskResult(
            success=True,
            message=final_message,
            job_id=job_id,
            job_dir=job_dir,
            command=command,
            duration_seconds=time.monotonic() - started,
            details=result_details,
        )
