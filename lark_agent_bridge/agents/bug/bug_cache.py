from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from . import _shared


class _BugCacheMixin:
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

    def classify_requests(self, *, prompt_text: str, title: str, description: str) -> list["BugAnalysisPlan"]:
        combined = "\n".join(part for part in [prompt_text, title, description] if part).strip()
        # Signal extraction uses only prompt_text to avoid picking up signal names
        # that appear in bug descriptions/logs but are not what the user wants to investigate.
        signal_request = parse_signal_request(
            prompt_text,
            signal_aliases=self.config.signal_aliases,
            command_prefixes=self.config.command_prefixes,
            signal_resolver=self.signal_resolver,
        )
        prompt_lowered = prompt_text.casefold()
        lowered = combined.casefold()
        explicit_signal_enum = "signal_" in prompt_lowered
        explicit_signal_terms = any(term in prompt_lowered for term in SIGNAL_ROUTE_TERMS)
        startup_requested = any(term in lowered for term in STARTUP_ROUTE_TERMS)
        stuck_requested = any(term in lowered for term in STUCK_ROUTE_TERMS)
        startup_blocked = any(term in lowered for term in STARTUP_BLOCK_ROUTE_TERMS)
        candidates: list[tuple[int, str, str | None]] = []

        def add_candidate(score: int, kind: str, signal_code: str | None = None) -> None:
            candidates.append((score, kind, signal_code))

        if any(term in lowered for term in PERCEPTION_ROUTE_TERMS):
            add_candidate(120, "perception")
        if any(term in lowered for term in LD_LANE_LEVEL_ROUTE_TERMS):
            add_candidate(117, "ld_lane_level")
        if any(term in lowered for term in PULLOVER_CHAIN_ROUTE_TERMS):
            add_candidate(116, "pullover_chain")
        if any(term in lowered for term in XTHEME_ROUTE_TERMS):
            add_candidate(115, "xtheme")
        if (
            (looks_like_scene_signal_request(combined) and not (signal_request.signal and (explicit_signal_enum or explicit_signal_terms)))
            or (signal_request.signal and _is_core_scene_signal(signal_request.signal))
            or _has_strong_scene_signal_intent(lowered)
        ):
            add_candidate(110, "scene_signal")
        if (
            signal_request.signal
            and (explicit_signal_enum or explicit_signal_terms)
            and not _is_core_scene_signal(signal_request.signal)
            and not _has_strong_scene_signal_intent(lowered)
        ):
            add_candidate(100, "signal", signal_request.signal)
        if any(term in lowered for term in CRASH_ROUTE_TERMS):
            add_candidate(95, "crash")
        plans: list[BugAnalysisPlan] = []
        if startup_requested or (stuck_requested and startup_blocked):
            plans.append(BugAnalysisPlan(kind="startup"))
        if stuck_requested:
            plans.append(BugAnalysisPlan(kind="stuck"))
        if plans:
            return plans
        if candidates:
            candidates.sort(key=lambda item: item[0], reverse=True)
            _score, kind, signal_code = candidates[0]
            return [BugAnalysisPlan(kind=kind, signal_code=signal_code)]
        return [BugAnalysisPlan(kind="general")]

    def classify_request(self, *, prompt_text: str, title: str, description: str) -> "BugAnalysisPlan":
        return self.classify_requests(prompt_text=prompt_text, title=title, description=description)[0]

    def build_command(
        self,
        *,
        plan: "BugAnalysisPlan",
        input_path: Path,
        html_path: Path,
        json_path: Path,
        analysis_dir: Path,
        target_time: str | None = None,
        request_text: str | None = None,
        log_analysis: dict[str, object] | None = None,
    ) -> list[str]:
        if plan.kind == "startup":
            command = [
                sys.executable,
                str(self._startup_script()),
                str(input_path),
                "--output-dir",
                str(analysis_dir),
            ]
            if target_time:
                command.extend(["--target-time", target_time])
            return command
        if plan.kind == "stuck":
            command = [
                sys.executable,
                str(self._stuck_script()),
                str(input_path),
            ]
            if target_time:
                command.extend(["--target-time", target_time])
            return command
        if plan.kind == "perception":
            command = [
                sys.executable,
                str(self._perception_script()),
                str(input_path),
            ]
            if target_time:
                command.extend(["--target-time", target_time])
            return command
        if plan.kind == "xtheme":
            command = [
                sys.executable,
                str(self._xtheme_script()),
                str(input_path),
            ]
            if target_time:
                command.extend(["--target-time", target_time])
            if request_text:
                command.extend(["--request-text", request_text])
            return command
        if plan.kind == "scene_signal":
            command = [
                sys.executable,
                str(self._scene_signal_script()),
                "--log-path",
                str(input_path),
                "--output-dir",
                str(analysis_dir),
            ]
            if target_time:
                command.extend(["--target-time", target_time])
            # 智能日志分析：传入 PID 和日志文件列表
            if log_analysis:
                target_pid = log_analysis.get("target_pid")
                if target_pid:
                    command.extend(["--pid", str(target_pid)])
                log_files = log_analysis.get("log_files")
                if log_files and isinstance(log_files, list):
                    command.extend(["--log-files", ",".join(str(f) for f in log_files)])
            return command
        if plan.kind == "crash":
            command = [
                sys.executable,
                str(self._stuck_script()),
                str(input_path),
            ]
            if target_time:
                command.extend(["--target-time", target_time])
            return command
        if _kind_spec(plan.kind).is_agent_handled:
            return []
        command = [
            sys.executable,
            str(self._signal_script()),
            "--signal-code",
            plan.signal_code or "",
            "--output",
            str(html_path),
            "--json-output",
            str(json_path),
        ]
        if input_path.exists():
            command.extend(["--log-path", str(input_path)])
        return command

    def _working_dir(self) -> Path:
        options = self.config.bug_analysis
        return options.working_dir or self.config.workspace_root

    def _plan_requires_log_input(self, plan: "BugAnalysisPlan") -> bool:
        return plan.kind in PLAN_KIND_REGISTRY

    def _bug_fetcher_script(self) -> Path:
        return self.config.workspace_root / ".ai/skills/feishu-bug-fetcher/scripts/bug-fetcher.sh"

    def _startup_script(self) -> Path:
        return self.config.workspace_root / ".ai/skills/unity-startup-lifecycle-check/scripts/analyze_unity_startup.py"

    def _stuck_script(self) -> Path:
        return self.config.workspace_root / ".ai/skills/3d-stuck-investigate/scripts/analyze_3d_stuck.py"

    def _signal_script(self) -> Path:
        return self.config.workspace_root / ".ai/skills/signal-chain-analyzer/scripts/analyze_signal_chain.py"

    def _scene_signal_script(self) -> Path:
        return self.config.workspace_root / ".ai/skills/scene-signal-diagnosis/scripts/extract_scene_signal_events.py"

    def _perception_script(self) -> Path:
        return self.config.workspace_root / ".ai/skills/perception-data-summary/scripts/analyze_perception_data_summary.py"

    def _xtheme_script(self) -> Path:
        return self.config.workspace_root / ".ai/skills/xtheme-analyzer/scripts/analyze_xtheme.py"

    def _bug_id(self, url: str) -> str:
        match = re.search(r"/buglo/detail/(\d+)", url)
        return match.group(1) if match else "unknown_bug"

    def _path_from_details(self, details: dict[str, object], key: str) -> Path | None:
        value = details.get(key)
        if not isinstance(value, str) or not value.strip():
            return None
        path = Path(value).expanduser()
        return path if path.exists() else None

    def _plans_from_previous_details(self, details: dict[str, object], *, fallback_text: str) -> list["BugAnalysisPlan"]:
        raw_kinds = details.get("analysis_kinds")
        kinds: list[str] = []
        if isinstance(raw_kinds, list):
            kinds = [str(item) for item in raw_kinds if str(item)]
        elif isinstance(details.get("analysis_kind"), str):
            kinds = [str(details["analysis_kind"])]
        plans = [
            BugAnalysisPlan(
                kind=kind,
                signal_code=str(details.get("signal_code") or "") or None,
            )
            for kind in kinds
            if kind in PLAN_KIND_REGISTRY
        ]
        if plans:
            return plans
        return self.classify_requests(prompt_text=fallback_text, title="", description="")

    def _plans_for_reanalysis(
        self,
        details: dict[str, object],
        *,
        request_text: str,
        followup_text: str,
    ) -> list["BugAnalysisPlan"]:
        return self._plans_from_previous_details(details, fallback_text=request_text)

    def _followup_explicitly_requests_source_analysis(self, request_text: str, followup_text: str) -> bool:
        if self._source_analysis_shortcut(request_text, followup_text):
            return True
        if self._should_collect_source_evidence(request_text, followup_text):
            return True
        return False

    def _fallback_non_source_plans_from_job_output(self, output_dir: Path) -> list["BugAnalysisPlan"]:
        candidates: list[tuple[float, str]] = []
        for kind in (
            "ld_lane_level",
            "startup",
            "stuck",
            "crash",
            "scene_signal",
            "signal",
            "xtheme",
            "perception",
            "general",
            "custom_skill",
            SOURCE_CODE_SKILL_KIND,
        ):
            report_json = output_dir / self._report_name(kind, "json")
            report_html = output_dir / self._report_name(kind, "html")
            existing = report_json if report_json.exists() else report_html if report_html.exists() else None
            if existing is None:
                continue
            try:
                mtime = existing.stat().st_mtime
            except OSError:
                continue
            candidates.append((mtime, kind))
        candidates.sort(reverse=True)
        if not candidates:
            return []
        return [BugAnalysisPlan(kind=candidates[0][1])]

    def _extract_signal_code_for_reanalysis(self, text: str) -> str:
        request = parse_signal_request(
            text,
            signal_aliases=self.config.signal_aliases,
            command_prefixes=self.config.command_prefixes,
            signal_resolver=self.signal_resolver,
        )
        return request.signal or ""

    def _forced_reanalysis_kinds(
        self,
        plans: list["BugAnalysisPlan"],
        *,
        request_text: str,
        followup_text: str,
        force_rerun: bool = False,
    ) -> set[str]:
        if force_rerun:
            return {plan.kind for plan in plans}
        lowered = f"{request_text}\n{followup_text}".casefold()
        signal_followup_terms = tuple(term.casefold() for term in self.config.bug_analysis.force_reanalysis_terms)
        force: set[str] = set()
        if any(plan.kind == "signal" for plan in plans) and any(term in lowered for term in signal_followup_terms):
            force.add("signal")
        return force

    def _report_name(self, kind: str, suffix: str) -> str:
        return {
            "startup": f"bug_3d_startup_report.{suffix}",
            "stuck": f"bug_3d_stuck_report.{suffix}",
            "crash": f"bug_crash_report.{suffix}",
            "scene_signal": f"bug_scene_signal_report.{suffix}",
            "perception": f"bug_perception_data_summary.{suffix}",
            "signal": f"bug_signal_chain_report.{suffix}",
            "xtheme": f"bug_xtheme_analysis_report.{suffix}",
            "ld_lane_level": f"bug_ld_lane_level_report.{suffix}",
            "pullover_chain": f"bug_pullover_chain_report.{suffix}",
            "general": f"bug_general_analysis_report.{suffix}",
            SOURCE_STAGE_KIND: f"source_stage_report.{suffix}",
            "custom_skill": f"bug_source_code_report.{suffix}",
            SOURCE_CODE_SKILL_KIND: f"bug_source_code_report.{suffix}",
        }[kind]

    def _combined_report_name(self, suffix: str) -> str:
        return f"bug_startup_stuck_report.{suffix}"

    def _analysis_label(self, kind: str) -> str:
        return {
            "startup": "3D启动时序分析",
            "stuck": "3D卡顿分析",
            "crash": "Crash/闪退分析",
            "scene_signal": "3D场景信号分析",
            "perception": "当前感知数据总结",
            "signal": "信号链路分析",
            "xtheme": "XTheme时光主题分析",
            "ld_lane_level": "LD车道级日志分析",
            "pullover_chain": "靠边停车链路分析",
            "general": "通用问题分析",
            SOURCE_STAGE_KIND: "源码分析阶段",
            "custom_skill": "源码分析 (source_code_skill)",
            SOURCE_CODE_SKILL_KIND: "源码分析 (source_code_skill)",
        }[kind]

    def _effective_skill_name_for_plan(self, plan_kind: str, candidate_skill_name: str) -> str:
        normalized = candidate_skill_name.strip()
        default_skill = self._skill_name_for_kind(plan_kind)
        if _kind_spec(plan_kind).is_source_stage:
            if normalized and (
                normalized == "source_analysis"
                or self.skill_manager.custom_skill_executor_for(normalized) == "file_agent"
            ):
                return normalized
            return default_skill
        if plan_kind in _SOURCE_SKILL_KINDS:
            return normalized or default_skill
        return default_skill

    def _subprocess_debug_log_path(
        self,
        directory: Path,
        stem: str,
        *,
        bridge_session_id: str = "",
    ) -> Path:
        safe_stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", stem).strip("._") or "subprocess"
        if bridge_session_id:
            safe_session = re.sub(r"[^A-Za-z0-9_.-]+", "_", bridge_session_id).strip("._")
            safe_stem = f"{safe_session}.{safe_stem}"
        return directory / f"{safe_stem}.debug.log"

    def _run_json_command(
        self,
        command: list[str],
        *,
        timeout: int,
        bridge_session_id: str = "",
    ) -> dict[str, object]:
        debug_log_path = self._subprocess_debug_log_path(
            self.config.data_dir / "subprocess_debug",
            "bug-json-command",
            bridge_session_id=bridge_session_id,
        )
        completed = _run_tracked_process(
            command,
            watchdog=self.process_watchdog,
            name="bug-json-command",
            cwd=self._working_dir(),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            session_id=bridge_session_id,
            debug_log_path=debug_log_path,
        )
        if completed.returncode != 0:
            message = completed.stderr.strip() or completed.stdout.strip() or "command failed"
            raise RuntimeError(message)
        payload = json.loads(completed.stdout)
        if isinstance(payload, dict) and payload.get("ok") is False:
            raise RuntimeError(str(payload.get("error", "command returned ok=false")))
        return payload

    def _load_option_map(self, project_key: str, *, bridge_session_id: str = "") -> dict[str, str]:
        payload = self._run_json_command(
            [
                "meegle",
                "workitem",
                "meta-fields",
                "--project-key",
                project_key,
                "--work-item-type",
                "buglo",
                "--field-keys",
                "field_24095d",
                "--field-keys",
                "field_45dc84",
                "--page-num",
                "1",
                "--format",
                "json",
            ],
            timeout=120,
            bridge_session_id=bridge_session_id,
        )
        option_map: dict[str, str] = {}
        for field in payload.get("list", []):
            if not isinstance(field, dict):
                continue
            for option in field.get("option", []):
                if not isinstance(option, dict):
                    continue
                option_id = option.get("option_id")
                option_name = option.get("option_name")
                if isinstance(option_id, str) and isinstance(option_name, str):
                    option_map[option_id] = option_name
        return option_map

    def _bug_cache_root(self) -> Path:
        return Path(self.config.data_dir).expanduser().resolve() / "bug_cache"

    def _bug_cache_dir(self, project_key: str, work_item_id: str) -> Path:
        key = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{project_key}_{work_item_id}").strip("._-")
        return self._bug_cache_root() / (key or str(work_item_id))

    def _has_bug_cache_content(self, bug_dir: Path) -> bool:
        for subdir in ("attachments", "logs"):
            path = bug_dir / subdir
            if not path.exists():
                continue
            for child in path.rglob("*"):
                if child.is_file():
                    return True
        return False

    def _is_bug_cache_fresh(self, bug_dir: Path, *, max_age_hours: int, now: datetime | None = None) -> bool:
        if max_age_hours <= 0 or not bug_dir.exists():
            return False
        reference_time = now or datetime.now(timezone.utc)
        age_seconds = reference_time.timestamp() - self._latest_path_mtime(bug_dir)
        return age_seconds <= max_age_hours * 3600

    def _reuse_prepared_bug_input(self, selected_input: Path | None) -> Path | None:
        if selected_input is None:
            return None
        if selected_input.is_dir():
            return selected_input
        lower_name = selected_input.name.lower()
        if lower_name.endswith(".xp"):
            extract_dir = selected_input.with_suffix("")
            if (extract_dir / "Log").exists():
                return extract_dir / "Log"
            if extract_dir.exists():
                return extract_dir
        if lower_name.endswith(".zip") and not lower_name.endswith(".xp.zip.001"):
            extract_dir = selected_input.with_suffix("")
            if extract_dir.exists():
                return extract_dir
        return None

    def _write_bug_cache_metadata(
        self,
        bug_dir: Path,
        *,
        bug_url: str,
        project_key: str,
        work_item_id: str,
        selected_input: Path | None,
        prepared_input: Path | None,
    ) -> None:
        bug_dir.mkdir(parents=True, exist_ok=True)
        metadata_path = bug_dir / "cache.json"
        now = datetime.now(timezone.utc).isoformat()
        payload = {
            "bug_url": bug_url,
            "project_key": project_key,
            "work_item_id": work_item_id,
            "selected_log_input": str(selected_input) if selected_input else "",
            "prepared_log_input": str(prepared_input) if prepared_input else "",
            "updated_at": now,
        }
        if metadata_path.exists():
            try:
                existing = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                existing = {}
            if isinstance(existing, dict) and existing.get("created_at"):
                payload["created_at"] = str(existing.get("created_at"))
        payload.setdefault("created_at", now)
        metadata_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _recover_bug_title_from_metadata(self, output_dir: Path) -> str:
        """Recover bug title from bug_metadata.md in the job output.

        This is needed for follow-ups like "请根据BUG问题实际时间继续分析"
        where the agent needs the title to extract the correct fault time.
        """
        metadata_path = output_dir / "bug_metadata.md"
        if not metadata_path.exists():
            return ""
        try:
            text = metadata_path.read_text(encoding="utf-8")[:2000]
        except OSError:
            return ""
        match = re.search(r"[标題标题]:\s*`([^`]+)`", text)
        if match:
            return match.group(1).strip()
        match = re.search(r"[标題标题]:\s*(.+)", text)
        if match:
            return match.group(1).strip()
        return ""

    def _recover_cached_bug_log_input(
        self,
        details: dict[str, object],
        *,
        request_text: str,
    ) -> tuple[Path | None, Path | None]:
        bug_dir = self._bug_cache_dir_from_context(details, request_text=request_text)
        if bug_dir is None or not bug_dir.exists():
            return None, None
        selected_input, prepared_input = self._read_bug_cache_log_input(bug_dir)
        if prepared_input is not None:
            return selected_input or prepared_input, prepared_input
        selected_input = self._select_existing_bug_cache_input(bug_dir)
        if selected_input is None:
            return None, None
        try:
            prepared_input = self._reuse_prepared_bug_input(selected_input)
            if prepared_input is None:
                prepared_input = self._prepare_log_input(selected_input)
        except (OSError, RuntimeError, zipfile.BadZipFile):
            if selected_input.is_dir():
                prepared_input = selected_input
            else:
                return None, None
        return selected_input, prepared_input

    def _bug_cache_dir_from_context(self, details: dict[str, object], *, request_text: str) -> Path | None:
        for key in ("bug_cache_dir", "bug_dir"):
            value = details.get(key)
            if isinstance(value, str) and value.strip():
                return Path(value).expanduser()
        bug_url = str(details.get("bug_url") or "").strip()
        if not bug_url:
            match = re.search(r"https?://project\.feishu\.cn/\S+", request_text)
            bug_url = match.group(0).rstrip("`，。；;、)") if match else ""
        identity = self._bug_identity_from_url_or_text(bug_url or request_text)
        if identity is None:
            return None
        project_key, work_item_id = identity
        return self._bug_cache_dir(project_key, work_item_id)

    def _bug_identity_from_url_or_text(self, text: str) -> tuple[str, str] | None:
        match = re.search(r"project\.feishu\.cn/([^/\s]+)/buglo/detail/(\d+)", text)
        if not match:
            return None
        return match.group(1), match.group(2)

    def _read_bug_cache_log_input(self, bug_dir: Path) -> tuple[Path | None, Path | None]:
        metadata_path = bug_dir / "cache.json"
        if not metadata_path.exists():
            return None, None
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None, None
        if not isinstance(payload, dict):
            return None, None
        selected_input = self._existing_path_from_text(payload.get("selected_log_input"))
        prepared_input = self._existing_path_from_text(payload.get("prepared_log_input"))
        if prepared_input is None and selected_input is not None:
            try:
                prepared_input = self._reuse_prepared_bug_input(selected_input)
                if prepared_input is None:
                    prepared_input = self._prepare_log_input(selected_input)
            except (OSError, RuntimeError, zipfile.BadZipFile):
                prepared_input = selected_input if selected_input.is_dir() else None
        return selected_input, prepared_input

    def _existing_path_from_text(self, value: object) -> Path | None:
        if not isinstance(value, str) or not value.strip():
            return None
        path = Path(value).expanduser()
        return path if path.exists() else None

    def _select_existing_bug_cache_input(self, bug_dir: Path) -> Path | None:
        logs_dir = bug_dir / "logs"
        attachments_dir = bug_dir / "attachments"
        if self._has_meaningful_log_tree(logs_dir):
            return logs_dir
        if self._has_decoded_log_tree(attachments_dir):
            return attachments_dir
        candidates: list[Path] = []
        for root in (attachments_dir, logs_dir):
            if not root.exists():
                continue
            try:
                candidates.extend(path for path in root.rglob("*") if path.is_file())
            except OSError:
                continue
        for suffix in _BUG_LOG_INPUT_PRIORITY_SUFFIXES:
            for candidate in sorted(candidates, key=lambda item: str(item)):
                if candidate.name.lower().endswith(suffix) and self._is_usable_log_attachment(candidate):
                    return candidate
        if self._has_meaningful_log_tree(attachments_dir):
            return attachments_dir
        if self._has_meaningful_log_tree(bug_dir):
            return bug_dir
        return None

    def _has_decoded_log_tree(self, root: Path) -> bool:
        if not root.exists():
            return False
        try:
            for path in root.rglob("*"):
                if path.is_file() and self._is_log_coverage_file(path):
                    return True
        except OSError:
            return False
        return False

    def cleanup_expired_bug_cache(self, *, max_age_hours: int, now: datetime | None = None) -> int:
        if max_age_hours <= 0:
            return 0
        root = self._bug_cache_root()
        if not root.exists():
            return 0
        reference_time = now or datetime.now(timezone.utc)
        cutoff_seconds = max_age_hours * 3600
        removed = 0
        for bug_dir in root.iterdir():
            if not bug_dir.is_dir():
                continue
            age_seconds = reference_time.timestamp() - self._latest_path_mtime(bug_dir)
            if age_seconds <= cutoff_seconds:
                continue
            if self._remove_tree(bug_dir):
                removed += 1
        return removed

    def _latest_path_mtime(self, root: Path) -> float:
        latest = 0.0
        try:
            for child in root.rglob("*"):
                if not child.is_file():
                    continue
                try:
                    child_mtime = child.stat().st_mtime
                except OSError:
                    continue
                if child_mtime > latest:
                    latest = child_mtime
        except OSError:
            return latest
        return latest or root.stat().st_mtime

    def _remove_tree(self, root: Path) -> bool:
        try:
            shutil.rmtree(root)
        except (FileNotFoundError, PermissionError, OSError):
            return False
        return True

    def _download_bug_attachments(
        self,
        project_key: str,
        work_item_id: str,
        bug_dir: Path,
        attachments: object,
        *,
        timeout: int,
        bridge_session_id: str = "",
    ) -> dict[str, object]:
        attachments_dir = bug_dir / "attachments"
        logs_dir = bug_dir / "logs"
        attachments_dir.mkdir(parents=True, exist_ok=True)
        logs_dir.mkdir(parents=True, exist_ok=True)
        downloaded: list[str] = []
        unzipped: list[str] = []
        errors: list[str] = []
        error_details: list[dict[str, str]] = []
        skipped: list[str] = []
        if not isinstance(attachments, list):
            return {
                "ok": True,
                "downloaded": downloaded,
                "unzipped": unzipped,
                "errors": errors,
                "error_details": error_details,
                "skipped": skipped,
            }

        for item in attachments:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            url = str(item.get("url") or "").strip()
            if not name:
                continue
            if not self._should_download_bug_attachment(name):
                skipped.append(name)
                continue
            if not url:
                errors.append(name)
                error_details.append({"name": name, "reason": "missing_attachment_url"})
                continue
            output_path = (attachments_dir / name).expanduser().resolve()
            completed = _run_tracked_process(
                [
                    "meegle",
                    "attachment",
                    "+download",
                    url,
                    "--project-key",
                    project_key,
                    "--work-item-id",
                    work_item_id,
                    "--output",
                    str(output_path),
                    "--overwrite",
                    "--format",
                    "json",
                ],
                watchdog=self.process_watchdog,
                name="bug-attachment-download",
                cwd=self._working_dir(),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                session_id=bridge_session_id,
                debug_log_path=self._subprocess_debug_log_path(
                    attachments_dir,
                    f"download-{output_path.name}",
                    bridge_session_id=bridge_session_id,
                ),
            )
            if completed.returncode != 0:
                errors.append(name)
                error_details.append({"name": name, "reason": self._extract_process_error_message(completed)})
                continue
            downloaded.append(name)
            if output_path.name.lower().endswith(".zip") and zipfile.is_zipfile(output_path):
                if self._extract_downloaded_zip(output_path, logs_dir):
                    unzipped.append(name)
        return {
            "ok": True,
            "downloaded": downloaded,
            "unzipped": unzipped,
            "errors": errors,
            "error_details": error_details,
            "skipped": skipped,
        }
