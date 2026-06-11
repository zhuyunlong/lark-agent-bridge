from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from . import _shared


from .plan_command import _BugPlanCommandMixin
from .reanalysis_plans import _ReanalysisPlanMixin
from .cache_store import _BugCacheStoreMixin


class _BugCacheMixin(_BugPlanCommandMixin, _ReanalysisPlanMixin, _BugCacheStoreMixin):
    def run_direct_analysis(
        self,
        request: DirectAnalysisRequest,
        *,
        event: LarkEvent | None = None,
        progress_callback: Callable[[dict[str, object]], None] | None = None,
        plans_override: list["BugAnalysisPlan"] | None = None,
        classification_skill: str = "",
        classification_source: str = "",
        classification_reason: str = "",
    ) -> TaskResult:
        if request.error == "missing_prompt" or not request.prompt.strip():
            return TaskResult(
                success=False,
                message="缺少分析内容：请在附件后说明要分析什么问题。",
                error_code="missing_direct_analysis_prompt",
                details={"mode": "direct_analysis"},
            )
        if not request.resources:
            return TaskResult(
                success=False,
                message="缺少日志输入：请提供飞书附件或日志 URL。",
                error_code="missing_log",
                details={"mode": "direct_analysis"},
            )

        context = create_job_context(self.config.data_dir, event=event)
        bridge_session_id = self._bridge_session_id(event)
        metadata_path = context.output_dir / "direct_analysis_metadata.md"
        request_artifact = context.output_dir / "bug_agent_request.md"
        started = time.monotonic()
        request_text = self._request_text(raw_text=request.raw_text, prompt_text=request.prompt, bug_url="")
        plans = plans_override or self.classify_requests(prompt_text=request.prompt, title="", description="")
        source_decision = self._decide_source_analysis_request(
            request_text=request_text,
            prompt_text=request.prompt,
            title="",
            description="",
            plans=plans,
            skill_name=classification_skill,
        )
        plans = self._augment_plans_for_source_analysis(plans, source_decision=source_decision)
        if source_decision.requested and not classification_skill and all(_kind_spec(plan.kind).is_source_stage for plan in plans):
            classification_skill = "source_analysis"
        request_artifact.write_text(
            self._render_bug_agent_request(
                request_text=request_text,
                prompt_text=request.prompt,
                bug_url="",
                plans=plans,
            ),
            encoding="utf-8",
        )
        self._emit_progress(
            progress_callback,
            stage="direct_job_created",
            message="已创建直传文件分析任务",
            job_id=context.job_id,
            request_text=request_text,
            resources=[item.value for item in request.resources],
        )
        time_context = self._resolve_bug_time_context(
            request_text=request_text,
            title="",
            description="",
            reference_time=self._event_reference_time_text(event),
        )
        fault_time = time_context.fault_time
        if not time_context.has_full_datetime:
            return self._bug_time_clarification_result(
                context=context,
                started=started,
                request_text=request_text,
                bug_url="",
                time_context=time_context,
                status="missing_fault_time",
                progress_callback=progress_callback,
            )
        downloader = getattr(self, "_direct_downloader", None)
        if downloader is None:
            downloader = LogDownloader(self.config, getattr(self, "_lark_client", None))
            self._direct_downloader = downloader
        try:
            self._emit_progress(progress_callback, stage="direct_download_resources", message="下载直传附件或日志")
            downloaded = downloader.download_all(
                request.resources,
                context=context,
                message_id=event.message_id if event else "",
            )
        except DownloadError as exc:
            return TaskResult(
                success=False,
                message=f"下载失败：{exc}",
                job_id=context.job_id,
                job_dir=context.job_dir,
                duration_seconds=time.monotonic() - started,
                error_code="download_failed",
                details={"mode": "direct_analysis"},
            )

        selected_input = downloaded[0].path if len(downloaded) == 1 else context.input_dir
        self._emit_progress(progress_callback, stage="direct_prepare_logs", message="准备直传日志输入")
        prepared_input = self._prepare_log_input(selected_input) if selected_input.exists() else selected_input
        html_paths: list[Path] = []
        report_jsons: dict[str, Path | None] = {}
        command: list[str] | None = None
        evidence_log_bundle: dict[str, object] | None = None
        skill_file_agent_execution_result: dict[str, object] | None = None
        log_coverage = self._scan_log_time_coverage(prepared_input, fault_time=fault_time)
        if not log_coverage.has_time_evidence:
            return self._bug_time_clarification_result(
                context=context,
                started=started,
                request_text=request_text,
                bug_url="",
                time_context=time_context,
                status="log_time_unknown",
                progress_callback=progress_callback,
                log_coverage=log_coverage,
            )
        if not log_coverage.covers_fault_time:
            return self._bug_time_clarification_result(
                context=context,
                started=started,
                request_text=request_text,
                bug_url="",
                time_context=time_context,
                status="log_not_covering_fault_time",
                progress_callback=progress_callback,
                log_coverage=log_coverage,
            )
        source_evidence_path = self._write_reanalysis_source_evidence(
            plans=plans,
            request_text=request_text,
            followup_text=request.prompt,
            output_dir=context.output_dir,
            enabled=self._should_collect_source_evidence(request_text, request.prompt)
            or any(_kind_spec(plan.kind).is_source_stage for plan in plans)
            or any(_kind_spec(p.kind).needs_source_evidence for p in plans),
        )

        # 【新增】智能日志分析：定位进程号、找出相关日志、反推线索
        log_analysis_result = None
        if prepared_input and fault_time:
            # 使用第一个 plan 进行智能分析
            first_plan = plans[0] if plans else None
            if first_plan:
                log_analysis_result = self._analyze_logs_intelligently(
                    log_dir=prepared_input,
                    problem_time=self._fault_time_to_datetime(fault_time),
                    plan=first_plan,
                )

        for current_plan in plans:
            current_html = context.output_dir / self._report_name(current_plan.kind, "html")
            current_json = context.output_dir / self._report_name(current_plan.kind, "json")
            current_analysis_dir = context.output_dir / f"{current_plan.kind}_analysis"
            input_for_plan = prepared_input
            if current_plan.kind == "startup" and prepared_input is not None:
                input_for_plan = self._startup_analysis_input(prepared_input, fault_time)
            command = self.build_command(
                plan=current_plan,
                input_path=input_for_plan,
                html_path=current_html,
                json_path=current_json,
                analysis_dir=current_analysis_dir,
                target_time=fault_time if _kind_accepts_target_time(current_plan.kind) else None,
                request_text=request.prompt if current_plan.kind == "xtheme" else None,
                log_analysis=log_analysis_result,  # 传入智能日志分析结果
            )
            try:
                self._emit_progress(
                    progress_callback,
                    stage="direct_run_analysis",
                    message=f"执行{self._analysis_label(current_plan.kind)}",
                    plan=current_plan.kind,
                    plan_label=self._analysis_label(current_plan.kind),
                )
                if current_plan.kind == "general":
                    self._write_general_bug_report(
                        html_path=current_html,
                        json_path=current_json,
                        title="",
                        description="",
                        prompt_text=request.prompt,
                        request_text=request_text,
                        fault_time=fault_time,
                        selected_input=selected_input,
                        source_evidence_path=source_evidence_path,
                        classification_skill=self._skill_name_for_kind(current_plan.kind),
                        classification_source="manual_fallback",
                        classification_reason="直传文件分析未命中专用 skill，退回通用问题分析。",
                    )
                    completed = subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")
                elif _kind_spec(current_plan.kind).is_custom_agent:
                    if current_plan.kind == "ld_lane_level":
                        current_skill_name = self._skill_name_for_kind(current_plan.kind)
                    elif _kind_spec(current_plan.kind).is_source_stage:
                        if getattr(source_decision, "source_mode", "") == "append" and not getattr(source_decision, "context_profile", ""):
                            return TaskResult(
                                success=False,
                                message="直传文件分析失败：源码阶段缺少领域上下文，已停止以避免泛化扫描。",
                                job_id=context.job_id,
                                job_dir=context.job_dir,
                                command=command,
                                duration_seconds=time.monotonic() - started,
                                error_code="source_stage_missing_context",
                                details={
                                    "mode": "direct_analysis",
                                    "analysis_kind": current_plan.kind,
                                    "analysis_kinds": [item.kind for item in plans],
                                    "source_mode": getattr(source_decision, "source_mode", ""),
                                    "context_profile": getattr(source_decision, "context_profile", ""),
                                    "stage_kinds": list(getattr(source_decision, "stage_kinds", []) or []),
                                },
                            )
                        current_skill_name = self._resolve_source_stage_skill_name(classification_skill)
                    else:
                        current_skill_name = classification_skill or self._skill_name_for_kind(current_plan.kind)
                    if _kind_spec(current_plan.kind).needs_custom_executor_check and current_skill_name != "source_analysis" and self.skill_manager.custom_skill_executor_for(current_skill_name) not in {"file_agent", "pydantic_ai"}:
                        prefix = current_plan.kind if current_plan.kind in _SOURCE_SKILL_KINDS else SOURCE_STAGE_KIND
                        return TaskResult(
                            success=False,
                            message=self._custom_skill_executor_not_ready_message(
                                current_skill_name,
                                selected_input=selected_input,
                            ),
                            job_id=context.job_id,
                            job_dir=context.job_dir,
                            command=command,
                            duration_seconds=time.monotonic() - started,
                            error_code=f"direct_{prefix}_executor_not_ready",
                            details={
                                "mode": "direct_analysis",
                                "analysis_kind": current_plan.kind,
                                "analysis_skill": current_skill_name,
                                f"{prefix}_analysis_status": "executor_not_ready",
                                "target_time": fault_time,
                                "selected_log_input": str(selected_input),
                                "prepared_log_input": str(prepared_input),
                            },
                        )
                    # Source stage: prefer app-server-backed file-agent when enabled,
                    # while keeping the later summary stage independent.
                    if _kind_spec(current_plan.kind).is_source_stage:
                        if self._prefer_source_stage_file_agent():
                            self._emit_progress(
                                progress_callback,
                                stage="source_stage_app_server_preferred",
                                message="已启用 codex app-server，源码阶段直接走 file-agent 路径",
                            )
                            execution_skill_name = (
                                getattr(source_decision, "context_profile", "").strip()
                                or current_skill_name
                            )
                            custom_result = self._run_custom_skill_agent_analysis(
                                analysis_kind=current_plan.kind,
                                analysis_label=self._analysis_label(current_plan.kind),
                                skill_name=execution_skill_name,
                                request_text=request_text,
                                prompt_text=request.prompt,
                                title="",
                                description="",
                                fault_time=fault_time,
                                selected_input=selected_input,
                                prepared_input=prepared_input,
                                source_evidence_path=source_evidence_path,
                                html_path=current_html,
                                json_path=current_json,
                                analysis_dir=current_analysis_dir,
                                progress_callback=progress_callback,
                                timeout=self._source_stage_file_agent_timeout(
                                    reference_seconds=max(time.monotonic() - started, 240.0),
                                ),
                                bridge_session_id=bridge_session_id,
                                prior_findings=self._summarize_prior_report_jsons(report_jsons),
                                context_profile=execution_skill_name,
                                provider_override="codex",
                                command_override=self.config.codex_app_server.command,
                            )
                        else:
                            custom_result = self._run_source_stage_pydantic_ai(
                                analysis_kind=current_plan.kind,
                                skill_name=current_skill_name,
                                request_text=request_text,
                                prompt_text=request.prompt,
                                title="",
                                description="",
                                fault_time=fault_time,
                                selected_input=selected_input,
                                prepared_input=prepared_input,
                                source_evidence_path=source_evidence_path,
                                html_path=current_html,
                                json_path=current_json,
                                analysis_dir=current_analysis_dir,
                                progress_callback=progress_callback,
                                context_profile=getattr(source_decision, "context_profile", ""),
                                prior_findings=self._summarize_prior_report_jsons(report_jsons),
                            )
                            if not custom_result.get("ok"):
                                logger.warning(
                                    "direct_analysis source_stage pydantic-ai failed (error=%s), falling back to file_agent",
                                    custom_result.get("error_code", "unknown"),
                                )
                                custom_result = self._run_custom_skill_agent_analysis(
                                    analysis_kind=current_plan.kind,
                                    analysis_label=self._analysis_label(current_plan.kind),
                                    skill_name=current_skill_name,
                                    request_text=request_text,
                                    prompt_text=request.prompt,
                                    title="",
                                    description="",
                                    fault_time=fault_time,
                                    selected_input=selected_input,
                                    prepared_input=prepared_input,
                                    source_evidence_path=source_evidence_path,
                                    html_path=current_html,
                                    json_path=current_json,
                                    analysis_dir=current_analysis_dir,
                                    progress_callback=progress_callback,
                                    timeout=self._source_stage_file_agent_timeout(
                                        reference_seconds=max(time.monotonic() - started, 240.0),
                                    ),
                                    bridge_session_id=bridge_session_id,
                                    prior_findings=self._summarize_prior_report_jsons(report_jsons),
                                    context_profile=getattr(source_decision, "context_profile", ""),
                                )
                    # LD lane-level: try pydantic-ai runtime first (fast path)
                    elif current_plan.kind == "ld_lane_level":
                        custom_result = self._run_ld_pydantic_ai_analysis(
                            skill_name=current_skill_name,
                            request_text=request_text,
                            prompt_text=request.prompt,
                            title="",
                            description="",
                            fault_time=fault_time,
                            selected_input=selected_input,
                            prepared_input=prepared_input,
                            html_path=current_html,
                            json_path=current_json,
                            analysis_dir=current_analysis_dir,
                            progress_callback=progress_callback,
                            prior_findings=self._summarize_prior_report_jsons(report_jsons),
                        )
                        if not custom_result.get("ok"):
                            logger.warning(
                                "direct_analysis LD pydantic-ai failed (error=%s), falling back to file_agent",
                                custom_result.get("error_code", "unknown"),
                            )
                            custom_result = self._run_custom_skill_agent_analysis(
                                analysis_kind=current_plan.kind,
                                analysis_label=self._analysis_label(current_plan.kind),
                                skill_name=current_skill_name,
                                request_text=request_text,
                                prompt_text=request.prompt,
                                title="",
                                description="",
                                fault_time=fault_time,
                                selected_input=selected_input,
                                prepared_input=prepared_input,
                                source_evidence_path=source_evidence_path,
                                html_path=current_html,
                                json_path=current_json,
                                analysis_dir=current_analysis_dir,
                                progress_callback=progress_callback,
                                timeout=self._agent_summary_timeout(
                                    self.config.bug_analysis.timeout_seconds,
                                    reference_seconds=max(time.monotonic() - started, 240.0),
                                ),
                                bridge_session_id=bridge_session_id,
                                prior_findings=self._summarize_prior_report_jsons(report_jsons),
                            )
                    else:
                        custom_result = self._run_custom_skill_agent_analysis(
                            analysis_kind=current_plan.kind,
                            analysis_label=self._analysis_label(current_plan.kind),
                            skill_name=current_skill_name,
                            request_text=request_text,
                            prompt_text=request.prompt,
                            title="",
                            description="",
                            fault_time=fault_time,
                            selected_input=selected_input,
                            prepared_input=prepared_input,
                            source_evidence_path=source_evidence_path,
                            html_path=current_html,
                            json_path=current_json,
                            analysis_dir=current_analysis_dir,
                            progress_callback=progress_callback,
                            timeout=self._agent_summary_timeout(
                                self.config.bug_analysis.timeout_seconds,
                                reference_seconds=max(time.monotonic() - started, 240.0),
                            ),
                            bridge_session_id=bridge_session_id,
                            prior_findings=self._summarize_prior_report_jsons(report_jsons),
                            context_profile=getattr(source_decision, "context_profile", "") if _kind_spec(current_plan.kind).is_source_stage else "",
                        )
                    skill_file_agent_execution_result = custom_result
                    command = list(custom_result.get("command") or command or [])
                    if not custom_result.get("ok"):
                        return TaskResult(
                            success=False,
                            message=str(custom_result.get("message") or "直传专用 Skill 文件 Agent 执行失败。"),
                            job_id=context.job_id,
                            job_dir=context.job_dir,
                            command=command,
                            duration_seconds=time.monotonic() - started,
                            error_code=self._skill_file_agent_mode_error_code(
                                current_plan.kind,
                                str(custom_result.get("error_code") or "custom_skill_agent_failed"),
                                mode="direct_analysis",
                            ),
                            stdout=str(custom_result.get("stdout") or ""),
                            stderr=str(custom_result.get("stderr") or ""),
                            details={
                                "mode": "direct_analysis",
                                "analysis_kind": current_plan.kind,
                                "analysis_skill": current_skill_name,
                                f"{'custom_skill' if current_plan.kind == 'custom_skill' else current_plan.kind}_analysis_status": "failed",
                                "target_time": fault_time,
                                "selected_log_input": str(selected_input),
                                "prepared_log_input": str(prepared_input),
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
                        "plan": current_plan,
                        "input_path": input_for_plan,
                        "html_path": current_html,
                        "json_path": current_json,
                        "analysis_dir": current_analysis_dir,
                        "timeout": self.config.bug_analysis.timeout_seconds,
                        "target_time": fault_time if _kind_accepts_target_time(current_plan.kind) else None,
                        "request_text": request.prompt if current_plan.kind == "xtheme" else None,
                    }
                    if bridge_session_id:
                        analysis_kwargs["bridge_session_id"] = bridge_session_id
                    completed = self._run_analysis(**analysis_kwargs)
            except subprocess.TimeoutExpired as exc:
                return TaskResult(
                    success=False,
                    message=f"直传文件分析超时：{self._analysis_label(current_plan.kind)}",
                    job_id=context.job_id,
                    job_dir=context.job_dir,
                    command=command,
                    duration_seconds=time.monotonic() - started,
                    error_code=f"direct_analysis_{current_plan.kind}_timeout",
                    stdout=_coerce_process_text(exc.stdout),
                    stderr=_coerce_process_text(exc.stderr),
                    details={"mode": "direct_analysis"},
                )
            if completed.returncode != 0:
                return TaskResult(
                    success=False,
                    message=f"直传文件分析失败：{self._analysis_label(current_plan.kind)}脚本执行失败。",
                    job_id=context.job_id,
                    job_dir=context.job_dir,
                    command=command,
                    duration_seconds=time.monotonic() - started,
                    error_code=f"direct_analysis_{current_plan.kind}_failed",
                    stdout=completed.stdout,
                    stderr=completed.stderr,
                    details={"mode": "direct_analysis"},
                )
            html_paths.append(current_html)
            report_jsons[current_plan.kind] = current_json if current_json.exists() else None

        summary = self._build_direct_analysis_summary(plans, request.prompt, html_paths)
        metadata_path.write_text(summary, encoding="utf-8")
        evidence_log_bundle = preserve_evidence_log_bundle(
            output_dir=context.output_dir,
            source_roots=[selected_input, prepared_input, context.input_dir],
            reference_files=[metadata_path, *(path for path in report_jsons.values() if path is not None)],
            reference_texts=[summary],
        )
        self._append_evidence_log_metadata(metadata_path, evidence_log_bundle)
        combined_artifacts = self._build_combined_report_artifacts(
            plans=plans,
            prompt_text=request.prompt,
            fault_time=fault_time,
            output_dir=context.output_dir,
            html_paths=html_paths,
            report_jsons=report_jsons,
            selected_input=selected_input,
            source_evidence_path=None,
        )
        agent_summary_result = {
            "message": "",
            "provider": "",
            "model": "",
            "session_id": "",
            "resumed": False,
            "duration_seconds": 0.0,
            "usage": {},
            "usage_scope": "",
        }
        if any(_kind_spec(plan.kind).is_agent_handled for plan in plans):
            agent_summary_path = context.output_dir / "bug_agent_summary.md"
            agent_provider_override = ""
            # source_analysis is now handled by pydantic-ai runtime; no file-agent override needed
            snapshot_details = self._structured_bug_prompt_snapshot_details(
                base_details={
                    "analysis_skill": classification_skill or self._skill_name_for_kind(plans[0].kind if plans else "general"),
                    "classification_source": classification_source or "manual_fallback",
                    "classification_reason": classification_reason or "",
                },
                request_text=request_text,
                plans=plans,
                target_time=fault_time,
                prepared_input=prepared_input,
                selected_input=selected_input,
            )
            agent_summary_result = self._run_bug_agent_summary(
                request_text=request_text,
                request_artifact=request_artifact,
                metadata_path=metadata_path,
                output_path=agent_summary_path,
                progress_callback=progress_callback,
                timeout=self._agent_summary_timeout(
                    self.config.bug_analysis.timeout_seconds,
                    reference_seconds=max(
                        time.monotonic() - started,
                        240.0 if any(_kind_spec(i.kind).is_custom_agent for i in plans) else 0.0,
                    ),
                ),
                bridge_session_id=bridge_session_id,
                snapshot_details=snapshot_details,
                snapshot_plans=plans,
                provider_override=agent_provider_override,
            )
            self._append_agent_runtime_metadata(
                metadata_path,
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
        final_message = str(combined_artifacts["summary"]) if combined_artifacts is not None else summary
        if agent_summary_result.get("message"):
            final_message = str(agent_summary_result["message"])
        self._emit_progress(
            progress_callback,
            stage="direct_completed",
            message="直传文件分析完成",
            job_id=context.job_id,
            analysis_kinds=[item.kind for item in plans],
            html_reports=[str(path) for path in html_paths],
        )
        direct_details = {
            "mode": "direct_analysis",
            "analysis_kind": plans[0].kind if plans else "general",
            "analysis_kinds": [item.kind for item in plans],
            "selected_log_input": str(selected_input),
            "prepared_log_input": str(prepared_input),
            "fault_time": fault_time,
            "domain_kind": getattr(source_decision, "domain_kind", "") or self._domain_kind_from_plans(plans),
            "source_mode": getattr(source_decision, "source_mode", "") or "off",
            "context_profile": getattr(source_decision, "context_profile", ""),
            "stage_kinds": list(getattr(source_decision, "stage_kinds", []) or []),
            "source_targets": list(getattr(source_decision, "targets", []) or []),
            "log_coverage_start": log_coverage.start_time,
            "log_coverage_end": log_coverage.end_time,
            "log_coverage_scanned_files": log_coverage.scanned_files,
            "log_coverage_scanned_lines": log_coverage.scanned_lines,
            "analysis_skill": classification_skill or self._skill_name_for_kind(plans[0].kind if plans else "general"),
            "analysis_skill_label": (
                "源码导向文件分析"
                if classification_skill == "source_analysis" or (plans and _kind_spec(plans[0].kind).is_source_stage)
                else self._analysis_label(plans[0].kind if plans else "general")
            ),
            "classification_source": classification_source or "manual_fallback",
            "classification_reason": classification_reason or "",
            "agent_request_file": str(request_artifact),
            "agent_summary_file": str(context.output_dir / "bug_agent_summary.md"),
            **(
                {
                    "combined_report_html": str(combined_artifacts["html_path"]),
                    "combined_report_json": str(combined_artifacts["json_path"]),
                }
                if combined_artifacts is not None
                else {}
            ),
            "files_to_send": (
                [metadata_path, Path(combined_artifacts["html_path"])]
                if combined_artifacts is not None
                else [metadata_path, *html_paths]
            ),
            **(
                {
                    "evidence_log_bundle": str(evidence_log_bundle.get("bundle_dir") or ""),
                    "evidence_log_manifest": str(evidence_log_bundle.get("manifest_path") or ""),
                    "evidence_log_focus_logs": evidence_log_bundle.get("focus_logs") or [],
                    "evidence_log_file_count": evidence_log_bundle.get("file_count") or 0,
                }
                if evidence_log_bundle is not None
                else {}
            ),
        }
        if skill_file_agent_execution_result is not None:
            direct_details.update(
                self._skill_file_agent_execution_details(
                    str(skill_file_agent_execution_result.get("analysis_kind") or ""),
                    skill_file_agent_execution_result,
                )
            )
        return TaskResult(
            success=True,
            message=final_message,
            job_id=context.job_id,
            job_dir=context.job_dir,
            command=command,
            duration_seconds=time.monotonic() - started,
            details=direct_details,
        )
