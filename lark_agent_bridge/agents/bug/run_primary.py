from __future__ import annotations

from ._shared import *  # noqa: F401,F403


class _RunPrimaryMixin:
    def run_bug_analysis(
        self,
        request: BugRequest,
        *,
        event: LarkEvent | None = None,
        progress_callback: Callable[[dict[str, object]], None] | None = None,
        plans_override: list["BugAnalysisPlan"] | None = None,
        classification_skill: str = "",
        classification_source: str = "",
        classification_reason: str = "",
        classification_provider: str = "",
    ) -> TaskResult:
        options = self.config.bug_analysis
        if not options.enabled:
            return TaskResult(
                success=True,
                message="Bug analysis agent is disabled",
                skipped=True,
                details={"mode": "bug_analysis"},
            )
        if request.error == "missing_bug_url" or not request.bug_url.strip():
            return TaskResult(
                success=False,
                message="缺少 Bug 链接：请提供 project.feishu.cn 的 bug 详情页 URL。",
                error_code="missing_bug_url",
                details={"mode": "bug_analysis"},
            )

        prompt_text = request.prompt.strip() or options.default_prompt.strip()
        if len(prompt_text) > options.max_prompt_chars:
            return TaskResult(
                success=False,
                message=f"Bug 分析描述过长，请压缩到 {options.max_prompt_chars} 字以内。",
                error_code="bug_prompt_too_long",
                details={"mode": "bug_analysis"},
            )

        context = create_job_context(self.config.data_dir, event=event)
        bridge_session_id = self._bridge_session_id(event)
        bridge_kwargs = {"bridge_session_id": bridge_session_id} if bridge_session_id else {}
        metadata_path = context.output_dir / "bug_metadata.md"
        request_text = self._request_text(raw_text=request.raw_text, prompt_text=prompt_text, bug_url=request.bug_url)
        plans = (
            list(plans_override)
            if plans_override is not None
            else self.classify_requests(prompt_text=prompt_text, title="", description="")
        )
        if not plans:
            plans = [BugAnalysisPlan(kind="general")]
        if plans_override is not None:
            selection = self._selection_from_plans(
                plans,
                source=classification_source or "user_selected_reply",
                reason=classification_reason or "用户确认后按指定 Bug skill 执行。",
                provider=classification_provider,
            )
            if classification_skill:
                selection.skill_name = classification_skill
                selection.skill_label = self._skill_label_for_name(
                    classification_skill,
                    plans[0].kind if plans else "general",
                )
        else:
            selection = self._selection_from_plans(
                plans,
                source="preflight_rules",
                reason="",
            )
        plan = plans[0]
        time_context = self._resolve_bug_time_context(
            request_text=request_text,
            title="",
            description="",
            reference_time=None,
        )
        target_time = (
            time_context.fault_time
            if time_context.has_full_datetime and _kind_accepts_target_time(plan.kind)
            else None
        )
        bug_dir = context.input_dir / f"bug_{self._bug_id(request.bug_url)}"
        html_path = context.output_dir / self._report_name(plan.kind, "html")
        json_path = context.output_dir / self._report_name(plan.kind, "json")
        analysis_dir = context.output_dir / f"{plan.kind}_analysis"
        command = self.build_command(
            plan=plan,
            input_path=bug_dir,
            html_path=html_path,
            json_path=json_path,
            analysis_dir=analysis_dir,
            target_time=target_time,
        )
        request_artifact = context.output_dir / "bug_agent_request.md"
        request_artifact.write_text(
            self._render_bug_agent_request(
                request_text=request_text,
                prompt_text=prompt_text,
                bug_url=request.bug_url,
                plans=plans,
            ),
            encoding="utf-8",
        )
        self._emit_progress(
            progress_callback,
            stage="bug_job_created",
            message="已创建 bug 分析任务",
            job_id=context.job_id,
            bug_url=request.bug_url,
            request_text=request_text,
            analysis_kinds=[item.kind for item in plans],
        )

        if self.config.dry_run:
            if [item.kind for item in plans] == ["startup", "stuck"]:
                planned_reports = (
                    f"- {context.output_dir / self._report_name('startup', 'html')}\n"
                    f"- {context.output_dir / self._report_name('stuck', 'html')}\n"
                    f"- {context.output_dir / self._combined_report_name('html')}"
                )
            else:
                planned_reports = "\n".join(
                    f"- {context.output_dir / self._report_name(item.kind, 'html')}"
                    for item in plans
                )
            self._emit_progress(
                progress_callback,
                stage="bug_dry_run_planned",
                message="dry-run 已规划 bug 分析任务",
                job_id=context.job_id,
                analysis_kinds=[item.kind for item in plans],
            )
            return TaskResult(
                success=True,
                message=(
                    "dry-run: bug 分析命令已规划\n"
                    f"metadata: {metadata_path}\n"
                    f"reports:\n{planned_reports}"
                ),
                job_id=context.job_id,
                job_dir=context.job_dir,
                command=command,
                details={
                    "mode": "bug_analysis",
                    "analysis_kind": plan.kind,
                    "analysis_kinds": [item.kind for item in plans],
                    "analysis_skill": selection.skill_name,
                    "analysis_skill_label": selection.skill_label,
                    "classification_source": selection.source,
                    "classification_reason": selection.reason,
                    "classification_provider": selection.provider,
                    "signal_code": plan.signal_code,
                    "user_request_text": request_text,
                    "agent_request_file": str(request_artifact),
                },
            )

        started = time.monotonic()
        try:
            _check_env_command = [str(self._bug_fetcher_script()), "check-env"]
            self._emit_progress(progress_callback, stage="bug_check_env",
                message="检查 meegle 环境", command=" ".join(_check_env_command))
            _check_env_started = time.monotonic()
            env_status = self._run_json_command(_check_env_command, timeout=60, **bridge_kwargs)
            self._emit_progress(progress_callback, stage="bug_check_env_completed",
                message=(f"meegle 环境检查完成（{time.monotonic() - _check_env_started:.1f}s, "
                    f"installed={bool(env_status.get('meegle_installed'))}, auth_ok={bool(env_status.get('auth_ok'))}）"),
                duration_seconds=round(time.monotonic() - _check_env_started, 2),
                meegle_installed=bool(env_status.get("meegle_installed", False)),
                auth_ok=bool(env_status.get("auth_ok", False)))
            if not env_status.get("meegle_installed", False):
                return self._failure(
                    context=context,
                    command=command,
                    started=started,
                    message="Bug 分析前置条件缺失：本机未安装 meegle CLI。",
                    error_code="bug_analysis_missing_meegle",
                    progress_callback=progress_callback,
                )
            if not env_status.get("auth_ok", False):
                return self._failure(
                    context=context,
                    command=command,
                    started=started,
                    message="Bug 分析前置条件缺失：meegle 未登录，请先在本机完成 `meegle auth login`。",
                    error_code="bug_analysis_meegle_not_auth",
                    progress_callback=progress_callback,
                )

            _resolve_url_command = [str(self._bug_fetcher_script()), "resolve-url", request.bug_url]
            self._emit_progress(progress_callback, stage="bug_resolve_url",
                message="解析 bug 链接", command=" ".join(_resolve_url_command), bug_url=request.bug_url)
            _resolve_url_started = time.monotonic()
            resolved = self._run_json_command(_resolve_url_command, timeout=60, **bridge_kwargs)
            project_key = str(resolved["project_key"])
            work_item_id = str(resolved["work_item_id"])
            self._emit_progress(progress_callback, stage="bug_resolve_url_completed",
                message=(f"已解析 bug 链接（{time.monotonic() - _resolve_url_started:.1f}s, "
                    f"project_key={project_key}, work_item_id={work_item_id}）"),
                duration_seconds=round(time.monotonic() - _resolve_url_started, 2),
                project_key=project_key, work_item_id=work_item_id)
            bug_dir = self._bug_cache_dir(project_key, work_item_id)
            if bug_dir.exists() and not self._is_bug_cache_fresh(
                bug_dir,
                max_age_hours=self.config.job_retention.bug_cache_max_age_hours,
            ):
                self._remove_tree(bug_dir)
            bug_dir.mkdir(parents=True, exist_ok=True)
            self._emit_progress(
                progress_callback,
                stage="bug_fetch_data",
                message="拉取 bug 详情和字段信息",
                project_key=project_key,
                work_item_id=work_item_id,
                commands=[
                    f"{self._bug_fetcher_script()} fetch-data {project_key} {work_item_id}",
                    f"meegle workitem get --project-key {project_key} --work-item-id {work_item_id} --format json",
                ],
            )
            _fetch_data_started = time.monotonic()
            # Parallel fetch: bug data + full work item + signal catalog pre-warm
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                future_fetched = pool.submit(
                    self._run_json_command,
                    [str(self._bug_fetcher_script()), "fetch-data", project_key, work_item_id],
                    timeout=120,
                    **bridge_kwargs,
                )
                future_full_item = pool.submit(
                    self._run_json_command,
                    ["meegle", "workitem", "get", "--project-key", project_key,
                     "--work-item-id", work_item_id, "--format", "json"],
                    timeout=120,
                    **bridge_kwargs,
                )
                # Pre-warm signal catalog in background (uses Phase 4 cache)
                pool.submit(self.signal_resolver._load_catalog)
                fetched = future_fetched.result()
                full_item = future_full_item.result()
            option_map = self._load_option_map(project_key, **bridge_kwargs)
            title = str(fetched.get("title", ""))
            description = self._bug_description(fetched)
            _fetch_attachments = fetched.get("attachments", []) if isinstance(fetched, dict) else []
            self._emit_progress(
                progress_callback,
                stage="bug_fetch_data_completed",
                message=(
                    f"已拉取 bug 详情（{time.monotonic() - _fetch_data_started:.1f}s, "
                    f"title 长度={len(title)}, description 长度={len(description)}, "
                    f"附件 {len(_fetch_attachments) if isinstance(_fetch_attachments, list) else 0} 个）"
                ),
                duration_seconds=round(time.monotonic() - _fetch_data_started, 2),
                title=title[:120],
                description_chars=len(description),
                attachment_count=(len(_fetch_attachments) if isinstance(_fetch_attachments, list) else 0),
                attachment_names=[
                    str(item.get("name") or "")
                    for item in (_fetch_attachments if isinstance(_fetch_attachments, list) else [])
                    if isinstance(item, dict)
                ][:8],
            )
            stack_payload_text = self._bug_stack_payload_text(fetched, description)
            time_context = self._resolve_bug_time_context(
                request_text=request_text,
                title=title,
                description=description,
                reference_time=self._bug_reference_time(fetched, full_item),
            )
            if plans_override is not None:
                if selection.source not in {"user_selected_reply", "user_selected_card"}:
                    selection = self._downgrade_lifecycle_stuck_conflict(
                        selection,
                        prompt_text=prompt_text,
                        title=title,
                        description=description,
                    )
                source_decision = self._decide_source_analysis_request(
                    request_text=request_text,
                    prompt_text=prompt_text,
                    title=title,
                    description=description,
                    plans=plans,
                    skill_name=selection.skill_name,
                )
                selection.plans = self._augment_plans_for_source_analysis(
                    selection.plans,
                    source_decision=source_decision,
                )
                if self._needs_skill_confirmation(selection):
                    return self._skill_confirmation_needed_result(
                        context=context,
                        selection=selection,
                        started=started,
                        request_text=request_text,
                        bug_url=request.bug_url,
                        title=title,
                        progress_callback=progress_callback,
                    )
            else:
                decision = self._unified_classify_and_decide(
                    request_text=request_text,
                    prompt_text=prompt_text,
                    title=title,
                    description=description,
                    attachments=fetched.get("attachments", []),
                    time_context=time_context,
                    stack_payload_text=stack_payload_text,
                )
                selection = decision.selection
                source_decision = decision.source_decision
                if self._needs_general_direction(selection, prompt_text=prompt_text):
                    return self._general_direction_needed_result(
                        context=context,
                        selection=selection,
                        started=started,
                        request_text=request_text,
                        bug_url=request.bug_url,
                        title=title,
                        progress_callback=progress_callback,
                    )
                if self._needs_skill_confirmation(selection):
                    return self._skill_confirmation_needed_result(
                        context=context,
                        selection=selection,
                        started=started,
                        request_text=request_text,
                        bug_url=request.bug_url,
                        title=title,
                        progress_callback=progress_callback,
                    )
            plans = selection.plans
            if (
                (
                    any(_kind_spec(item.kind).is_source_stage for item in plans)
                    or self._stack_reverse_preflight_can_override(selection)
                )
                and self._has_stack_reverse_lookup_intent(prompt_text)
                and not self._has_stack_reverse_lookup_payload(request_text, prompt_text, stack_payload_text)
                and not fetched.get("attachments")
            ):
                return self._bug_stack_clarification_result(
                    context=context,
                    started=started,
                    request_text=request_text,
                    bug_url=request.bug_url,
                    progress_callback=progress_callback,
                )
            requires_log_input = any(self._plan_requires_log_input(item) for item in plans)
            if requires_log_input and not time_context.has_full_datetime:
                return self._bug_time_clarification_result(
                    context=context,
                    started=started,
                    request_text=request_text,
                    bug_url=request.bug_url,
                    time_context=time_context,
                    status="missing_fault_time",
                    progress_callback=progress_callback,
                )
            explicit_selected_input: Path | None = None
            if request.resources:
                try:
                    explicit_selected_input = self._download_explicit_bug_resources(
                        request.resources,
                        context=context,
                        event=event,
                        progress_callback=progress_callback,
                    )
                except DownloadError as exc:
                    return self._failure(
                        context=context,
                        command=command,
                        started=started,
                        message=f"Bug 分析群聊文件下载失败：{exc}",
                        error_code="download_failed",
                        progress_callback=progress_callback,
                    )
            selected_input = explicit_selected_input or self._select_log_input(bug_dir, fetched)
            cache_reused = False
            if explicit_selected_input is not None:
                download = {
                    "ok": True,
                    "downloaded": [str(explicit_selected_input)],
                    "unzipped": [],
                    "errors": [],
                    "skipped": [],
                    "explicit_resource": True,
                }
            elif self._has_bug_cache_content(bug_dir) and (selected_input is not None or not requires_log_input):
                cache_reused = True
                download = {"ok": True, "downloaded": [], "unzipped": [], "errors": [], "skipped": [], "reused": True}
                self._emit_progress(
                    progress_callback,
                    stage="bug_reuse_cache",
                    message="复用同一 bug 已缓存的附件和日志",
                    project_key=project_key,
                    work_item_id=work_item_id,
                    bug_cache_dir=str(bug_dir),
                )
            else:
                _download_attachments = fetched.get("attachments", []) if isinstance(fetched, dict) else []
                _attachment_names = [
                    str(item.get("name") or "")
                    for item in (_download_attachments if isinstance(_download_attachments, list) else [])
                    if isinstance(item, dict)
                ]
                self._emit_progress(
                    progress_callback,
                    stage="bug_download_logs",
                    message=(
                        f"开始下载 bug 附件和日志（共 {len(_attachment_names)} 个）"
                    ),
                    attachment_count=len(_attachment_names),
                    attachment_names=_attachment_names[:8],
                    bug_cache_dir=str(bug_dir),
                )
                _download_started = time.monotonic()
                download = self._download_bug_attachments(
                    project_key,
                    work_item_id,
                    bug_dir,
                    fetched.get("attachments", []),
                    timeout=options.timeout_seconds,
                    **bridge_kwargs,
                )
                _downloaded_list = download.get("downloaded", []) if isinstance(download, dict) else []
                _unzipped_list = download.get("unzipped", []) if isinstance(download, dict) else []
                _errors_list = download.get("errors", []) if isinstance(download, dict) else []
                _skipped_list = download.get("skipped", []) if isinstance(download, dict) else []
                self._emit_progress(
                    progress_callback,
                    stage="bug_download_logs_completed",
                    message=(
                        f"附件下载完成（{time.monotonic() - _download_started:.1f}s, "
                        f"成功 {len(_downloaded_list)}, 解压 {len(_unzipped_list)}, "
                        f"失败 {len(_errors_list)}, 跳过 {len(_skipped_list)}）"
                    ),
                    duration_seconds=round(time.monotonic() - _download_started, 2),
                    downloaded=list(_downloaded_list)[:8],
                    downloaded_count=len(_downloaded_list),
                    unzipped=list(_unzipped_list)[:8],
                    unzipped_count=len(_unzipped_list),
                    errors=list(_errors_list)[:8],
                    error_count=len(_errors_list),
                    skipped=list(_skipped_list)[:8],
                    skipped_count=len(_skipped_list),
                    bug_cache_dir=str(bug_dir),
                )
                selected_input = self._select_log_input(bug_dir, fetched)
            plan = plans[0]
            html_path = context.output_dir / self._report_name(plan.kind, "html")
            json_path = context.output_dir / self._report_name(plan.kind, "json")
            analysis_dir = context.output_dir / f"{plan.kind}_analysis"
            if requires_log_input and selected_input is None:
                attachment_lines = self._render_attachment_lines(fetched.get("attachments", []), download)
                return self._failure(
                    context=context,
                    command=command,
                    started=started,
                    message=(
                        "Bug 分析失败：当前请求包含日志分析，但未找到可用日志附件。"
                        f"\n附件结果：\n{attachment_lines}"
                    ),
                    error_code="bug_analysis_missing_log_attachment",
                    progress_callback=progress_callback,
                )
            if any(item.kind == "signal" and not item.signal_code for item in plans):
                return self._failure(
                    context=context,
                    command=command,
                    started=started,
                    message="Bug 分析失败：识别到信号链路问题，但消息和 Bug 描述里没有明确的 SignalCode/枚举名。",
                    error_code="bug_analysis_missing_signal_code",
                    progress_callback=progress_callback,
                )

            _prepare_logs_started = time.monotonic()
            _selected_input_path = str(selected_input) if selected_input else ""
            _selected_input_kind = ""
            if selected_input is not None:
                if selected_input.is_dir():
                    _selected_input_kind = "directory"
                else:
                    _selected_input_kind = (selected_input.suffix or "").lstrip(".") or "file"
            self._emit_progress(
                progress_callback,
                stage="bug_prepare_logs",
                message=(
                    f"准备日志输入（{_selected_input_kind or '无'}: {Path(_selected_input_path).name if _selected_input_path else 'N/A'}）"
                ),
                selected_input=_selected_input_path,
                selected_input_kind=_selected_input_kind,
            )
            prepared_input = self._reuse_prepared_bug_input(selected_input) if selected_input else None
            _reused_prepared = prepared_input is not None
            if prepared_input is None:
                prepared_input = self._prepare_log_input(selected_input) if selected_input else None
            _prepared_input_path = str(prepared_input) if prepared_input else ""
            self._emit_progress(
                progress_callback,
                stage="bug_prepare_logs_completed",
                message=(
                    f"日志输入已准备（{time.monotonic() - _prepare_logs_started:.1f}s, "
                    f"{'缓存命中' if _reused_prepared else '现解码'}）"
                ),
                duration_seconds=round(time.monotonic() - _prepare_logs_started, 2),
                selected_input=_selected_input_path,
                prepared_input=_prepared_input_path,
                reused_prepared=_reused_prepared,
            )
            if explicit_selected_input is None:
                self._write_bug_cache_metadata(
                    bug_dir,
                    bug_url=request.bug_url,
                    project_key=project_key,
                    work_item_id=work_item_id,
                    selected_input=selected_input,
                    prepared_input=prepared_input,
                )
            fault_time, fault_time_note = time_context.fault_time, time_context.note
            log_coverage: LogCoverage | None = None
            if requires_log_input:
                log_coverage = self._scan_log_time_coverage(prepared_input, fault_time=fault_time) if prepared_input else None
                if log_coverage is None or not log_coverage.has_time_evidence:
                    return self._bug_time_clarification_result(
                        context=context,
                        started=started,
                        request_text=request_text,
                        bug_url=request.bug_url,
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
                        bug_url=request.bug_url,
                        time_context=time_context,
                        status="log_not_covering_fault_time",
                        progress_callback=progress_callback,
                        log_coverage=log_coverage,
                    )
            source_evidence_enabled = (
                self._should_collect_source_evidence(request_text, prompt_text)
                or any(_kind_spec(plan.kind).is_source_stage for plan in plans)
                or (
                    any(_kind_spec(p.kind).needs_source_evidence for p in plans)
                    and self._has_explicit_general_scope(prompt_text)
                )
            )
            # Start source evidence collection in background while analysis runs
            source_evidence_future: concurrent.futures.Future[Path | None] | None = None
            if source_evidence_enabled:
                _source_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
                source_evidence_future = _source_pool.submit(
                    self._write_reanalysis_source_evidence,
                    plans=plans,
                    request_text=request_text,
                    followup_text=prompt_text,
                    output_dir=context.output_dir,
                    enabled=True,
                    extra_texts=(title, description),
                )
                _source_pool.shutdown(wait=False)
            else:
                source_evidence_future = None
            # Source evidence is optional. Do not block before file-agent plans:
            # their progress/toolcall events should start even when source
            # search is slow or codegraph is cold.
            source_evidence_path: Path | None = None
            if source_evidence_future is not None:
                if any(item.kind in _SOURCE_SKILL_KINDS for item in plans):
                    source_evidence_path = context.output_dir / "bug_source_evidence.md"
                else:
                    source_evidence_path = self._resolve_source_evidence_future(source_evidence_future)
            html_paths: list[Path] = []
            report_jsons: dict[str, Path | None] = {}
            skill_file_agent_execution_result: dict[str, object] | None = None
            kinds = [p.kind for p in plans]
            will_merge = "source_stage" in kinds and "signal" in kinds
            for current_plan in plans:
                current_html = context.output_dir / self._report_name(current_plan.kind, "html")
                current_json = context.output_dir / self._report_name(current_plan.kind, "json")
                current_analysis_dir = context.output_dir / f"{current_plan.kind}_analysis"
                input_for_plan = prepared_input
                if current_plan.kind == "startup" and prepared_input is not None:
                    input_for_plan = self._startup_analysis_input(prepared_input, fault_time)
                current_command = self.build_command(
                    plan=current_plan,
                    input_path=input_for_plan or bug_dir,
                    html_path=current_html,
                    json_path=current_json,
                    analysis_dir=current_analysis_dir,
                    target_time=fault_time if _kind_accepts_target_time(current_plan.kind) else None,
                    request_text=request_text if current_plan.kind == "xtheme" else None,
                )
                self._emit_progress(
                    progress_callback,
                    stage="bug_run_analysis",
                    message=f"执行{self._analysis_label(current_plan.kind)}",
                    plan=current_plan.kind,
                    plan_label=self._analysis_label(current_plan.kind),
                    html_path=str(current_html),
                    json_path=str(current_json),
                    command=" ".join(str(part) for part in (current_command or []))[:600],
                    input_path=str(input_for_plan or bug_dir),
                )
                _run_analysis_started = time.monotonic()
                if current_plan.kind == "general":
                    self._write_general_bug_report(
                        html_path=current_html,
                        json_path=current_json,
                        title=title,
                        description=description,
                        prompt_text=prompt_text,
                        request_text=request_text,
                        fault_time=fault_time,
                        selected_input=selected_input,
                        source_evidence_path=source_evidence_path,
                        classification_skill=selection.skill_name,
                        classification_source=selection.source,
                        classification_reason=selection.reason,
                    )
                    completed = subprocess.CompletedProcess(args=current_command, returncode=0, stdout="", stderr="")
                elif _kind_spec(current_plan.kind).is_custom_agent:
                    if current_plan.kind == "ld_lane_level":
                        current_skill_name = self._skill_name_for_kind(current_plan.kind)
                    elif _kind_spec(current_plan.kind).is_source_stage:
                        if getattr(source_decision, "source_mode", "") == "append" and not getattr(source_decision, "context_profile", ""):
                            return self._failure(
                                context=context,
                                command=current_command,
                                started=started,
                                message="Bug 分析失败：源码阶段缺少领域上下文，已停止以避免泛化扫描。",
                                error_code="source_stage_missing_context",
                                progress_callback=progress_callback,
                                details={
                                    "analysis_kind": current_plan.kind,
                                    "analysis_kinds": [item.kind for item in plans],
                                    "source_mode": getattr(source_decision, "source_mode", ""),
                                    "context_profile": getattr(source_decision, "context_profile", ""),
                                    "stage_kinds": list(getattr(source_decision, "stage_kinds", []) or []),
                                },
                            )
                        current_skill_name = self._resolve_source_stage_skill_name(selection.skill_name)
                    else:
                        current_skill_name = selection.skill_name or self._skill_name_for_kind(current_plan.kind)
                    if _kind_spec(current_plan.kind).needs_custom_executor_check and current_skill_name != "source_analysis" and self.skill_manager.custom_skill_executor_for(current_skill_name) not in {"file_agent", "pydantic_ai"}:
                        prefix = current_plan.kind if current_plan.kind in _SOURCE_SKILL_KINDS else SOURCE_STAGE_KIND
                        return self._failure(
                            context=context,
                            command=current_command,
                            started=started,
                            message=self._custom_skill_executor_not_ready_message(
                                current_skill_name,
                                selected_input=selected_input,
                            ),
                            error_code=f"{prefix}_executor_not_ready",
                            progress_callback=progress_callback,
                            details={
                                "analysis_kind": current_plan.kind,
                                "analysis_kinds": [item.kind for item in plans],
                                "analysis_skill": current_skill_name,
                                "target_time": fault_time,
                                "selected_log_input": str(selected_input or ""),
                                "prepared_log_input": str(prepared_input or ""),
                            },
                        )
                    # LD lane-level: try pydantic-ai first, then executor+direct_api (fast path)
                    if current_plan.kind == "ld_lane_level":
                        custom_result = self._run_ld_pydantic_ai_analysis(
                            skill_name=current_skill_name,
                            request_text=request_text,
                            prompt_text=prompt_text,
                            title=title,
                            description=description,
                            fault_time=fault_time,
                            selected_input=selected_input,
                            prepared_input=prepared_input,
                            html_path=current_html,
                            json_path=current_json,
                            analysis_dir=current_analysis_dir,
                            progress_callback=progress_callback,
                            prior_findings=self._summarize_prior_report_jsons(report_jsons),
                        )
                        if custom_result.get("ok"):
                            logger.info("LD pydantic-ai succeeded, skipping direct_api/file_agent")
                        else:
                            logger.warning(
                                "LD pydantic-ai failed (error=%s), trying direct_api",
                                custom_result.get("error_code", "unknown"),
                            )
                            custom_result = self._run_ld_direct_api_analysis(
                                skill_name=current_skill_name,
                                request_text=request_text,
                                prompt_text=prompt_text,
                                title=title,
                                description=description,
                                fault_time=fault_time,
                                selected_input=selected_input,
                                prepared_input=prepared_input,
                                html_path=current_html,
                                json_path=current_json,
                                analysis_dir=current_analysis_dir,
                                progress_callback=progress_callback,
                            )
                        if not custom_result.get("ok"):
                            logger.warning(
                                "LD direct_api failed (error=%s), falling back to file_agent",
                                custom_result.get("error_code", "unknown"),
                            )
                            self._emit_progress(
                                progress_callback,
                                stage="ld_direct_api_fallback",
                                message=f"LD direct_api 失败（{custom_result.get('error_code')}），切换 file_agent",
                                error_code=str(custom_result.get("error_code") or ""),
                            )
                            custom_result = self._run_custom_skill_agent_analysis(
                                analysis_kind=current_plan.kind,
                                analysis_label=self._analysis_label(current_plan.kind),
                                skill_name=current_skill_name,
                                request_text=request_text,
                                prompt_text=prompt_text,
                                title=title,
                                description=description,
                                fault_time=fault_time,
                                selected_input=selected_input,
                                prepared_input=prepared_input,
                                source_evidence_path=source_evidence_path,
                                html_path=current_html,
                                json_path=current_json,
                                analysis_dir=current_analysis_dir,
                                progress_callback=progress_callback,
                                timeout=self._agent_summary_timeout(
                                    options.timeout_seconds,
                                    reference_seconds=max(time.monotonic() - started, 240.0),
                                ),
                                bridge_session_id=bridge_session_id,
                                prior_findings=self._summarize_prior_report_jsons(report_jsons),
                            )
                    else:
                        # Source stage: if app-server-backed file-agent is enabled, prefer it
                        # directly and leave the later summary stage on the configured AI path.
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
                                    prompt_text=prompt_text,
                                    title=title,
                                    description=description,
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
                                    render_html=not will_merge,
                                )
                            else:
                                custom_result = self._run_source_stage_pydantic_ai(
                                    analysis_kind=current_plan.kind,
                                    skill_name=current_skill_name,
                                    request_text=request_text,
                                    prompt_text=prompt_text,
                                    title=title,
                                    description=description,
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
                                    render_html=not will_merge,
                                )
                                if custom_result.get("ok"):
                                    logger.info("source_stage pydantic-ai succeeded, skipping file_agent")
                                else:
                                    logger.warning(
                                        "source_stage pydantic-ai failed (error=%s), falling back to file_agent",
                                        custom_result.get("error_code", "unknown"),
                                    )
                                    self._emit_progress(
                                        progress_callback,
                                        stage="source_stage_pydantic_ai_fallback",
                                        message=f"pydantic-ai 失败（{custom_result.get('error_code')}），切换 file_agent",
                                    )
                                    custom_result = self._run_custom_skill_agent_analysis(
                                        analysis_kind=current_plan.kind,
                                        analysis_label=self._analysis_label(current_plan.kind),
                                        skill_name=current_skill_name,
                                        request_text=request_text,
                                        prompt_text=prompt_text,
                                        title=title,
                                        description=description,
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
                                        render_html=not will_merge,
                                    )
                        else:
                            custom_result = self._run_custom_skill_agent_analysis(
                                analysis_kind=current_plan.kind,
                                analysis_label=self._analysis_label(current_plan.kind),
                                skill_name=current_skill_name,
                                request_text=request_text,
                                prompt_text=prompt_text,
                                title=title,
                                description=description,
                                fault_time=fault_time,
                                selected_input=selected_input,
                                prepared_input=prepared_input,
                                source_evidence_path=source_evidence_path,
                                html_path=current_html,
                                json_path=current_json,
                                analysis_dir=current_analysis_dir,
                                progress_callback=progress_callback,
                                timeout=self._agent_summary_timeout(
                                    options.timeout_seconds,
                                    reference_seconds=max(time.monotonic() - started, 240.0),
                                ),
                                bridge_session_id=bridge_session_id,
                                prior_findings=self._summarize_prior_report_jsons(report_jsons),
                                context_profile=getattr(source_decision, "context_profile", "") if _kind_spec(current_plan.kind).is_source_stage else "",
                            )
                    skill_file_agent_execution_result = custom_result
                    command = list(custom_result.get("command") or current_command)
                    current_command = command
                    if not custom_result.get("ok"):
                        return self._failure(
                            context=context,
                            command=current_command,
                            started=started,
                            message=str(custom_result.get("message") or "专用 Skill 文件 Agent 执行失败。"),
                            error_code=self._skill_file_agent_mode_error_code(
                                current_plan.kind,
                                str(custom_result.get("error_code") or "custom_skill_agent_failed"),
                                mode="bug_analysis",
                            ),
                            stdout=str(custom_result.get("stdout") or ""),
                            stderr=str(custom_result.get("stderr") or ""),
                            progress_callback=progress_callback,
                            details={
                                "analysis_kind": current_plan.kind,
                                "analysis_kinds": [item.kind for item in plans],
                                "analysis_skill": current_skill_name,
                                "target_time": fault_time,
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
                        args=current_command,
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
                        "timeout": options.timeout_seconds,
                        "target_time": fault_time if _kind_accepts_target_time(current_plan.kind) else None,
                        "request_text": request_text if current_plan.kind == "xtheme" else None,
                    }
                    if bridge_session_id:
                        analysis_kwargs["bridge_session_id"] = bridge_session_id
                    completed = self._run_analysis(**analysis_kwargs)
                if completed.returncode != 0:
                    return self._failure(
                        context=context,
                        command=current_command,
                        started=started,
                        message=f"Bug 分析失败：{self._analysis_label(current_plan.kind)}脚本执行失败。",
                        error_code=f"bug_analysis_{current_plan.kind}_failed",
                        stdout=completed.stdout,
                        stderr=completed.stderr,
                        progress_callback=progress_callback,
                        details={
                            "analysis_kind": current_plan.kind,
                            "analysis_kinds": [item.kind for item in plans],
                            "analysis_skill": selection.skill_name or self._skill_name_for_kind(current_plan.kind),
                            "target_time": fault_time,
                            "selected_log_input": str(selected_input or ""),
                            "prepared_log_input": str(prepared_input or ""),
                        },
                    )
                if not current_html.exists() and not (
                    will_merge and current_plan.kind in ("signal", "source_stage")
                ):
                    return self._failure(
                        context=context,
                        command=current_command,
                        started=started,
                        message=f"Bug 分析失败：未生成 {self._analysis_label(current_plan.kind)} HTML 报告。",
                        error_code="bug_analysis_missing_html",
                        stdout=completed.stdout,
                        stderr=completed.stderr,
                        progress_callback=progress_callback,
                        details={
                            "analysis_kind": current_plan.kind,
                            "analysis_kinds": [item.kind for item in plans],
                            "analysis_skill": selection.skill_name or self._skill_name_for_kind(current_plan.kind),
                            "target_time": fault_time,
                            "selected_log_input": str(selected_input or ""),
                            "prepared_log_input": str(prepared_input or ""),
                        },
                    )
                if not (will_merge and current_plan.kind in ("signal", "source_stage")):
                    html_paths.append(current_html)
                report_jsons[current_plan.kind] = current_json if current_json.exists() else None
                if current_plan is plan:
                    command = current_command
                    html_path = current_html
                    json_path = current_json
                    analysis_dir = current_analysis_dir
                _run_analysis_duration = time.monotonic() - _run_analysis_started
                _completed_returncode = getattr(completed, "returncode", 0) if "completed" in locals() else 0
                self._emit_progress(
                    progress_callback,
                    stage="bug_run_analysis_completed",
                    message=(
                        f"{self._analysis_label(current_plan.kind)}执行完成"
                        f"（{_run_analysis_duration:.1f}s, returncode={_completed_returncode}）"
                    ),
                    plan=current_plan.kind,
                    plan_label=self._analysis_label(current_plan.kind),
                    duration_seconds=round(_run_analysis_duration, 2),
                    returncode=_completed_returncode,
                    html_path=str(current_html),
                    html_exists=current_html.exists(),
                    json_path=str(current_json),
                    json_exists=current_json.exists(),
                )

            if source_evidence_future is not None:
                resolved_source_evidence_path = self._resolve_source_evidence_future(source_evidence_future)
                if resolved_source_evidence_path is not None:
                    source_evidence_path = resolved_source_evidence_path
                elif source_evidence_path is not None and not source_evidence_path.exists():
                    source_evidence_path = None

            self._emit_progress(
                progress_callback,
                stage="bug_build_outputs",
                message="整理 bug 分析结果",
                plan_count=len(plans),
                report_json_count=sum(1 for v in report_jsons.values() if v is not None),
                html_path_count=len(html_paths),
            )
            _build_outputs_started = time.monotonic()
            metadata_text, summary = self._build_bug_outputs(
                plans=plans,
                work_item_id=work_item_id,
                fetched=fetched,
                full_item=full_item,
                option_map=option_map,
                request_text=request_text,
                prompt_text=prompt_text,
                selected_input=selected_input,
                report_jsons=report_jsons,
                download=download,
                html_paths=html_paths,
                classification_skill=selection.skill_name,
                classification_source=selection.source,
                classification_reason=selection.reason,
                classification_provider=selection.provider,
                fault_time=fault_time,
                fault_time_note=fault_time_note,
                log_coverage=log_coverage,
            )
            metadata_path.write_text(metadata_text, encoding="utf-8")
            bug_summary_evidence_path = self._write_bug_summary_evidence(
                output_dir=context.output_dir,
                analysis_kind=plans[0].kind if plans else "general",
                report_jsons=report_jsons,
            )
            self._append_bug_summary_evidence_metadata(metadata_path, bug_summary_evidence_path)
            self._append_source_evidence_metadata(metadata_path, source_evidence_path)
            evidence_log_bundle = preserve_evidence_log_bundle(
                output_dir=context.output_dir,
                source_roots=[selected_input, prepared_input, bug_dir],
                reference_files=[metadata_path, *(path for path in report_jsons.values() if path is not None)],
                reference_texts=[summary],
            )
            self._append_evidence_log_metadata(metadata_path, evidence_log_bundle)
            combined_artifacts = self._build_combined_report_artifacts(
                plans=plans,
                prompt_text=prompt_text,
                fault_time=fault_time,
                output_dir=context.output_dir,
                html_paths=html_paths,
                report_jsons=report_jsons,
                selected_input=selected_input,
                source_evidence_path=source_evidence_path,
                intent=resolve_effective_intent(
                    getattr(source_decision, "intent", ""),
                    has_logs=selected_input is not None,
                ),
            )
            self._emit_progress(
                progress_callback,
                stage="bug_build_outputs_completed",
                message=(
                    f"bug 分析结果已整理完成（{time.monotonic() - _build_outputs_started:.1f}s）"
                ),
                duration_seconds=round(time.monotonic() - _build_outputs_started, 2),
                metadata_path=str(metadata_path),
                metadata_chars=len(metadata_text),
                summary_chars=len(summary),
                bug_summary_evidence_path=str(bug_summary_evidence_path or ""),
                evidence_log_count=int((evidence_log_bundle or {}).get("file_count") or 0) if isinstance(evidence_log_bundle, dict) else 0,
                combined_html_path=str((combined_artifacts or {}).get("html_path") or ""),
                combined_json_path=str((combined_artifacts or {}).get("json_path") or ""),
            )
            agent_summary_path = context.output_dir / "bug_agent_summary.md"
            agent_summary_result = self._run_bug_agent_summary(
                request_text=request_text,
                request_artifact=request_artifact,
                metadata_path=metadata_path,
                output_path=agent_summary_path,
                progress_callback=progress_callback,
                timeout=self._agent_summary_timeout(
                    options.timeout_seconds,
                    reference_seconds=max(
                        time.monotonic() - started,
                        240.0 if any(_kind_spec(i.kind).is_custom_agent for i in plans) else 0.0,
                    ),
                ),
                bridge_session_id=bridge_session_id,
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
        except subprocess.TimeoutExpired as exc:
            return self._failure(
                context=context,
                command=command,
                started=started,
                message="Bug 分析超时",
                error_code="bug_analysis_timeout",
                stdout=_coerce_process_text(exc.stdout),
                stderr=_coerce_process_text(exc.stderr),
                progress_callback=progress_callback,
            )
        except OSError as exc:
            return self._failure(
                context=context,
                command=command,
                started=started,
                message=f"Bug 分析启动失败: {exc}",
                error_code="bug_analysis_failed_to_start",
                stderr=str(exc),
                progress_callback=progress_callback,
            )
        except (KeyError, ValueError, RuntimeError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
            return self._failure(
                context=context,
                command=command,
                started=started,
                message=f"Bug 分析失败: {exc}",
                error_code="bug_analysis_failed",
                progress_callback=progress_callback,
            )

        details = {
            "mode": "bug_analysis",
            "analysis_kind": plan.kind,
            "analysis_kinds": [item.kind for item in plans],
            "analysis_skill": selection.skill_name,
            "analysis_skill_label": selection.skill_label,
            "domain_kind": getattr(source_decision, "domain_kind", "") or self._domain_kind_from_plans(plans),
            "source_mode": getattr(source_decision, "source_mode", "") or "off",
            "context_profile": getattr(source_decision, "context_profile", ""),
            "stage_kinds": list(getattr(source_decision, "stage_kinds", []) or []),
            "source_targets": list(getattr(source_decision, "targets", []) or []),
            "classification_source": selection.source,
            "classification_reason": selection.reason,
            "classification_provider": selection.provider,
            "signal_code": plan.signal_code,
            "selected_log_input": str(selected_input) if selected_input else "",
            "prepared_log_input": str(prepared_input) if prepared_input else "",
            "log_input_source": "replied_resource" if explicit_selected_input is not None else "bug_attachment",
            "fault_time": fault_time,
            "fault_time_source": time_context.source,
            "fault_time_note": fault_time_note,
            "log_coverage_start": log_coverage.start_time if log_coverage else "",
            "log_coverage_end": log_coverage.end_time if log_coverage else "",
            "log_coverage_scanned_files": log_coverage.scanned_files if log_coverage else 0,
            "log_coverage_scanned_lines": log_coverage.scanned_lines if log_coverage else 0,
            "bug_dir": str(bug_dir),
            "bug_cache_dir": str(bug_dir),
            "bug_cache_reused": cache_reused,
            "bug_url": request.bug_url,
            "user_request_text": request_text,
            "agent_request_file": str(request_artifact),
            "agent_summary_file": str(agent_summary_path),
        }
        if skill_file_agent_execution_result is not None:
            details.update(
                self._skill_file_agent_execution_details(
                    str(skill_file_agent_execution_result.get("analysis_kind") or ""),
                    skill_file_agent_execution_result,
                )
            )
        if source_evidence_path is not None:
            details["source_evidence_file"] = str(source_evidence_path)
        if evidence_log_bundle is not None:
            details["evidence_log_bundle"] = str(evidence_log_bundle.get("bundle_dir") or "")
            details["evidence_log_manifest"] = str(evidence_log_bundle.get("manifest_path") or "")
            details["evidence_log_focus_logs"] = evidence_log_bundle.get("focus_logs") or []
            details["evidence_log_file_count"] = evidence_log_bundle.get("file_count") or 0
        final_message = summary
        if agent_summary_result["message"]:
            final_message = str(agent_summary_result["message"])
        self._apply_agent_runtime_details(details, agent_summary_result)
        files_to_send = [metadata_path]
        if combined_artifacts is not None:
            details["combined_report_html"] = str(combined_artifacts["html_path"])
            details["combined_report_json"] = str(combined_artifacts["json_path"])
            files_to_send = [Path(combined_artifacts["html_path"])]
        else:
            files_to_send.extend(html_paths)
        if options.upload_result_files:
            details["files_to_send"] = files_to_send
        if json_path.exists():
            details["json_report"] = str(json_path)
        self._emit_progress(
            progress_callback,
            stage="bug_completed",
            message="bug 分析完成",
            job_id=context.job_id,
            analysis_kinds=[item.kind for item in plans],
            html_reports=[str(path) for path in html_paths],
        )
        return TaskResult(
            success=True,
            message=final_message,
            job_id=context.job_id,
            job_dir=context.job_dir,
            command=command,
            duration_seconds=time.monotonic() - started,
            details=details,
        )
