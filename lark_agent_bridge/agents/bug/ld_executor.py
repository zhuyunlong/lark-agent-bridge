from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from . import _shared


class _LdExecutorMixin:
    def _run_custom_skill_agent_analysis(
        self,
        *,
        analysis_kind: str = "source_code_skill",
        analysis_label: str = "",
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
        timeout: int,
        bridge_session_id: str = "",
        prior_findings: list[tuple[str, str, str]] | None = None,
        context_profile: str = "",
        provider_override: str = "",
        command_override: str = "",
        render_html: bool = True,
    ) -> dict[str, object]:
        started = time.monotonic()
        effective_label = analysis_label or self._analysis_label(analysis_kind)
        analysis_dir.mkdir(parents=True, exist_ok=True)
        html_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        analysis_markdown_path = analysis_dir / self._skill_agent_analysis_markdown_name(analysis_kind)
        stdout_path = analysis_dir / self._skill_agent_sidecar_name(analysis_kind, "stdout.txt")
        stderr_path = analysis_dir / self._skill_agent_sidecar_name(analysis_kind, "stderr.txt")
        command_path = analysis_dir / self._skill_agent_sidecar_name(analysis_kind, "command.txt")
        events_path = analysis_dir / self._skill_agent_sidecar_name(analysis_kind, "app_server_events.jsonl")
        context_path = analysis_dir / self._skill_agent_sidecar_name(analysis_kind, "context.md")
        log_focus_manifest_path = analysis_dir / "log_focus.md"
        debug_log_path = (
            analysis_dir / self._skill_agent_sidecar_name(analysis_kind, "debug.log")
            if self.config.bug_analysis.file_agent_debug_logs
            else None
        )

        def _fail(result: dict[str, object], *, provider_name: str = "") -> dict[str, object]:
            self._emit_progress(
                progress_callback,
                stage=f"{analysis_kind}_agent_failed",
                message=str(result.get("message") or f"{effective_label}文件 Agent 分析失败"),
                error_code=str(result.get("error_code") or ""),
                provider=provider_name,
                output_path=str(analysis_markdown_path),
                stdout_path=str(result.get("stdout_path") or stdout_path),
                stderr_path=str(result.get("stderr_path") or stderr_path),
                debug_log_path=str(result.get("debug_log_path") or debug_log_path or ""),
            )
            return result

        stale_paths = [
            analysis_markdown_path,
            html_path,
            json_path,
            stdout_path,
            stderr_path,
            command_path,
            events_path,
            context_path,
            log_focus_manifest_path,
        ]
        if debug_log_path is not None:
            stale_paths.append(debug_log_path)
        for stale_path in stale_paths:
            try:
                if stale_path.exists():
                    stale_path.unlink()
            except OSError:
                continue
        focused_log_input, focus_manifest, focused_files = self._build_file_agent_focus_dir(
            input_path=prepared_input,
            fault_time=fault_time,
            analysis_kind=analysis_kind,
            analysis_dir=analysis_dir,
        )
        context_path = self._write_file_agent_context(
            analysis_kind=analysis_kind,
            skill_name=context_profile or skill_name,
            request_text=request_text,
            prompt_text=prompt_text,
            title=title,
            description=description,
            fault_time=fault_time,
            original_selected_input=selected_input,
            focused_log_input=focused_log_input,
            log_focus_manifest=focus_manifest,
            source_evidence_path=source_evidence_path,
            analysis_dir=analysis_dir,
            analysis_markdown_path=analysis_markdown_path,
            prior_findings=prior_findings,
        )
        invocation = self._build_custom_skill_agent_command(
            analysis_kind=analysis_kind,
            skill_name=skill_name,
            request_text=request_text,
            prompt_text=prompt_text,
            title=title,
            description=description,
            fault_time=fault_time,
            selected_input=focused_log_input,
            prepared_input=focused_log_input,
            source_evidence_path=source_evidence_path,
            analysis_markdown_path=analysis_markdown_path,
            context_path=context_path,
            debug_log_path=debug_log_path,
            provider_override=provider_override,
            command_override=command_override,
            prior_findings=prior_findings,
        )
        command = list(invocation.get("command") or [])
        provider = str(invocation.get("provider") or "")
        output_mode = str(invocation.get("output_mode") or "")
        process_cwd = Path(invocation.get("cwd") or self._working_dir())
        input_mode = str(invocation.get("input_mode") or "")
        input_text = str(invocation.get("prompt") or "") if input_mode == "stdin" else None
        if command:
            command_path.write_text(json.dumps(command, ensure_ascii=False, indent=2), encoding="utf-8")
        if not command:
            return _fail({
                "ok": False,
                "error_code": "custom_skill_agent_not_configured",
                "message": f"专用 Skill `{skill_name}` 已配置 file_agent，但未配置可用 Agent 命令。",
                "command": command,
                "provider": provider,
                "analysis_markdown_path": analysis_markdown_path,
                "stdout_path": stdout_path,
                "stderr_path": stderr_path,
                "command_path": command_path,
                "events_path": events_path,
                "context_path": context_path,
                "log_focus_manifest_path": focus_manifest,
                "focused_log_input": focused_log_input,
                "focused_log_files": [str(path) for path in focused_files],
                "debug_log_path": debug_log_path,
                "stdout": "",
                "stderr": "",
                "duration_seconds": time.monotonic() - started,
            }, provider_name=provider)
        self._emit_progress(
            progress_callback,
            stage=f"{analysis_kind}_agent_analysis",
            message=f"执行{effective_label}文件 Agent 分析",
            skill=skill_name,
            provider=provider,
            output_path=str(analysis_markdown_path),
            timeout_seconds=timeout,
        )
        stdout = ""
        stderr = ""
        usage: dict[str, object] = {}
        thread_id = ""
        turn_id = ""
        executor = "file_agent"
        app_server_error_code = ""
        app_server_error_message = ""
        app_server_version = ""
        app_server_completion_state = ""
        subprocess_env = build_internal_network_env(self.config.internal_network_env)
        process_debug_log_path = None if provider in {"claude", "claude-code", "claude_code"} else debug_log_path
        try:
            if self._should_use_codex_app_server_for_file_agent(provider):
                app_server_result = self._run_custom_skill_agent_via_codex_app_server(
                    analysis_kind=analysis_kind,
                    skill_name=skill_name,
                    prompt_text=str(invocation.get("prompt") or ""),
                    cwd=process_cwd,
                    command_path=command_path,
                    stdout_path=stdout_path,
                    stderr_path=stderr_path,
                    events_path=events_path,
                    progress_callback=progress_callback,
                    timeout=timeout,
                    bridge_session_id=bridge_session_id,
                )
                app_server_error_code = str(app_server_result.get("error_code") or "")
                app_server_error_message = str(app_server_result.get("message") or "")
                app_server_version = str(app_server_result.get("app_server_version") or "")
                app_server_completion_state = str(app_server_result.get("completion_state") or "")
                app_server_command = list(app_server_result.get("command") or [])
                if app_server_result.get("ok"):
                    if app_server_command:
                        command = app_server_command
                    executor = "codex_app_server"
                    stdout = str(app_server_result.get("stdout") or "")
                    stderr = str(app_server_result.get("stderr") or "")
                    usage_value = app_server_result.get("usage") or {}
                    if isinstance(usage_value, dict):
                        usage = dict(usage_value)
                    thread_id = str(app_server_result.get("thread_id") or "")
                    turn_id = str(app_server_result.get("turn_id") or "")
                    final_text = str(app_server_result.get("final_text") or "").strip()
                    if final_text:
                        analysis_markdown_path.write_text(final_text + "\n", encoding="utf-8")
                    completed = subprocess.CompletedProcess(command, 0, stdout, stderr)
                elif self.config.codex_app_server.fallback_to_exec:
                    self._emit_progress(
                        progress_callback,
                        stage=f"{analysis_kind}_agent_analysis_stream",
                        message="Codex app-server 失败，回退到现有 codex exec 路径",
                        provider=provider,
                        app_server_error_code=app_server_error_code,
                    )
                    stdout = ""
                    stderr = ""
                else:
                    return {
                        "ok": False,
                        "error_code": app_server_error_code or "codex_app_server_failed",
                        "message": app_server_error_message or f"专用 Skill `{skill_name}` Codex app-server 执行失败。",
                        "command": list(app_server_result.get("command") or command),
                        "provider": provider,
                        "executor": "codex_app_server",
                        "analysis_markdown_path": analysis_markdown_path,
                        "stdout_path": stdout_path,
                        "stderr_path": stderr_path,
                        "command_path": command_path,
                        "events_path": events_path,
                        "context_path": context_path,
                        "log_focus_manifest_path": focus_manifest,
                        "focused_log_input": focused_log_input,
                        "focused_log_files": [str(path) for path in focused_files],
                        "debug_log_path": debug_log_path,
                        "stdout": str(app_server_result.get("stdout") or ""),
                        "stderr": str(app_server_result.get("stderr") or ""),
                        "duration_seconds": time.monotonic() - started,
                        "usage": usage,
                        "thread_id": thread_id,
                        "turn_id": turn_id,
                        "app_server_version": app_server_version,
                        "app_server_error_code": app_server_error_code,
                    }
            if executor == "file_agent" and output_mode == "stdout_json":
                completed = _run_tracked_process(
                    command,
                    watchdog=self.process_watchdog,
                    name=f"custom-skill-agent-analysis-{provider or 'agent'}",
                    cwd=process_cwd,
                    input=input_text,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                    session_id=bridge_session_id,
                    env=subprocess_env,
                    debug_log_path=process_debug_log_path,
                )
                raw_stdout = _coerce_process_text(completed.stdout)
                stderr = _coerce_process_text(completed.stderr)
                stdout = self._extract_text_from_agent_json(raw_stdout, analysis_markdown_path)
            elif executor == "file_agent" and output_mode == "stdout_redirect":
                with analysis_markdown_path.open("w", encoding="utf-8") as stdout_handle:
                    completed = _run_tracked_process(
                        command,
                        watchdog=self.process_watchdog,
                        name=f"custom-skill-agent-analysis-{provider or 'agent'}",
                        cwd=process_cwd,
                        input=input_text,
                        stdout=stdout_handle,
                        stderr=subprocess.PIPE,
                        text=True,
                        timeout=timeout,
                        check=False,
                        session_id=bridge_session_id,
                        env=subprocess_env,
                        debug_log_path=process_debug_log_path,
                    )
                stdout = analysis_markdown_path.read_text(encoding="utf-8", errors="replace") if analysis_markdown_path.exists() else ""
                stderr = _coerce_process_text(completed.stderr)
            elif executor == "file_agent":
                completed = _run_tracked_process(
                    command,
                    watchdog=self.process_watchdog,
                    name=f"custom-skill-agent-analysis-{provider or 'agent'}",
                    cwd=process_cwd,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                    session_id=bridge_session_id,
                    env=subprocess_env,
                    debug_log_path=process_debug_log_path,
                )
                stdout = _coerce_process_text(completed.stdout)
                stderr = _coerce_process_text(completed.stderr)
        except subprocess.TimeoutExpired as exc:
            if output_mode == "stdout_redirect" and analysis_markdown_path.exists():
                stdout = analysis_markdown_path.read_text(encoding="utf-8", errors="replace")
            else:
                stdout = _coerce_process_text(exc.stdout)
            stderr = _coerce_process_text(exc.stderr)
            stdout_path.write_text(stdout, encoding="utf-8")
            stderr_path.write_text(stderr, encoding="utf-8")
            partial_markdown = self._extract_partial_markdown_from_agent_stream(stdout, skill_name=skill_name)
            return _fail({
                "ok": False,
                "error_code": "custom_skill_agent_timeout",
                "message": f"专用 Skill `{skill_name}` 文件 Agent 执行超时，未生成可验证证据。",
                "command": command,
                "provider": provider,
                "analysis_markdown_path": analysis_markdown_path,
                "stdout_path": stdout_path,
                "stderr_path": stderr_path,
                "command_path": command_path,
                "events_path": events_path,
                "context_path": context_path,
                "log_focus_manifest_path": focus_manifest,
                "focused_log_input": focused_log_input,
                "focused_log_files": [str(path) for path in focused_files],
                "debug_log_path": debug_log_path,
                "stdout": stdout,
                "stderr": stderr,
                "duration_seconds": time.monotonic() - started,
                "timeout_seconds": timeout,
                "partial_markdown": partial_markdown,
                "partial_analysis_available": bool(partial_markdown),
            }, provider_name=provider)
        except OSError as exc:
            stdout_path.write_text("", encoding="utf-8")
            stderr_path.write_text(str(exc), encoding="utf-8")
            return _fail({
                "ok": False,
                "error_code": "custom_skill_agent_failed_to_start",
                "message": f"专用 Skill `{skill_name}` 文件 Agent 启动失败：{exc}",
                "command": command,
                "provider": provider,
                "analysis_markdown_path": analysis_markdown_path,
                "stdout_path": stdout_path,
                "stderr_path": stderr_path,
                "command_path": command_path,
                "events_path": events_path,
                "context_path": context_path,
                "log_focus_manifest_path": focus_manifest,
                "focused_log_input": focused_log_input,
                "focused_log_files": [str(path) for path in focused_files],
                "debug_log_path": debug_log_path,
                "stdout": "",
                "stderr": str(exc),
                "duration_seconds": time.monotonic() - started,
            }, provider_name=provider)
        stdout_path.write_text(stdout, encoding="utf-8")
        stderr_path.write_text(stderr, encoding="utf-8")
        if output_mode != "stdout_redirect" and not analysis_markdown_path.exists() and stdout.strip():
            analysis_markdown_path.write_text(stdout.strip() + "\n", encoding="utf-8")
        if completed.returncode != 0:
            return _fail({
                "ok": False,
                "error_code": "custom_skill_agent_failed",
                "message": f"专用 Skill `{skill_name}` 文件 Agent 执行失败，未允许进入最终总结。",
                "command": command,
                "provider": provider,
                "executor": executor,
                "analysis_markdown_path": analysis_markdown_path,
                "stdout_path": stdout_path,
                "stderr_path": stderr_path,
                "command_path": command_path,
                "events_path": events_path,
                "context_path": context_path,
                "log_focus_manifest_path": focus_manifest,
                "focused_log_input": focused_log_input,
                "focused_log_files": [str(path) for path in focused_files],
                "debug_log_path": debug_log_path,
                "stdout": stdout,
                "stderr": stderr,
                "duration_seconds": time.monotonic() - started,
            }, provider_name=provider)
        if not analysis_markdown_path.exists():
            return _fail({
                "ok": False,
                "error_code": "custom_skill_agent_missing_output",
                "message": (
                    f"专用 Skill `{skill_name}` 文件 Agent 未生成 `{analysis_markdown_path.name}`，未允许进入最终总结。"
                    f" 请检查 `{stdout_path.name}` 和 `{stderr_path.name}`。"
                ),
                "command": command,
                "provider": provider,
                "executor": executor,
                "analysis_markdown_path": analysis_markdown_path,
                "stdout_path": stdout_path,
                "stderr_path": stderr_path,
                "command_path": command_path,
                "events_path": events_path,
                "context_path": context_path,
                "log_focus_manifest_path": focus_manifest,
                "focused_log_input": focused_log_input,
                "focused_log_files": [str(path) for path in focused_files],
                "debug_log_path": debug_log_path,
                "stdout": stdout,
                "stderr": stderr,
                "duration_seconds": time.monotonic() - started,
            }, provider_name=provider)
        analysis_text = analysis_markdown_path.read_text(encoding="utf-8", errors="replace")
        if not analysis_text.strip():
            debug_hint = (
                f" 调试日志: `{debug_log_path.name}`。" if debug_log_path is not None else ""
            )
            return _fail({
                "ok": False,
                "error_code": "custom_skill_agent_empty_output",
                "message": (
                    f"专用 Skill `{skill_name}` 文件 Agent 执行完成但未输出任何正文（returncode={completed.returncode}）。"
                    f" 可能原因：模型在工具调用结束后未生成最终文本回复。"
                    f" 请检查 `{stdout_path.name}`、`{stderr_path.name}` 和{debug_hint}"
                    f" 可尝试重新触发分析。"
                ),
                "command": command,
                "provider": provider,
                "executor": executor,
                "analysis_markdown_path": analysis_markdown_path,
                "stdout_path": stdout_path,
                "stderr_path": stderr_path,
                "command_path": command_path,
                "events_path": events_path,
                "context_path": context_path,
                "debug_log_path": debug_log_path,
                "stdout": stdout,
                "stderr": stderr,
                "duration_seconds": time.monotonic() - started,
            }, provider_name=provider)
        valid, reason, evidence_count = self._validate_custom_skill_analysis(analysis_markdown_path)
        if not valid:
            return _fail({
                "ok": False,
                "error_code": "custom_skill_agent_invalid_evidence",
                "message": f"专用 Skill `{skill_name}` 文件 Agent 输出缺少有效 `## 关键证据`：{reason}。不会进入最终总结。",
                "command": command,
                "provider": provider,
                "executor": executor,
                "analysis_markdown_path": analysis_markdown_path,
                "stdout_path": stdout_path,
                "stderr_path": stderr_path,
                "command_path": command_path,
                "events_path": events_path,
                "context_path": context_path,
                "log_focus_manifest_path": focus_manifest,
                "focused_log_input": focused_log_input,
                "focused_log_files": [str(path) for path in focused_files],
                "debug_log_path": debug_log_path,
                "stdout": stdout,
                "stderr": stderr,
                "duration_seconds": time.monotonic() - started,
                "evidence_count": evidence_count,
                "validation_error": reason,
            }, provider_name=provider)
        self._write_custom_skill_agent_report(
            analysis_kind=analysis_kind,
            analysis_label=effective_label,
            html_path=html_path,
            json_path=json_path,
            analysis_markdown_path=analysis_markdown_path,
            skill_name=skill_name,
            provider=provider,
            request_text=request_text,
            prompt_text=prompt_text,
            title=title,
            description=description,
            fault_time=fault_time,
            selected_input=selected_input,
            prepared_input=prepared_input,
            source_evidence_path=source_evidence_path,
            evidence_count=evidence_count,
            duration_seconds=time.monotonic() - started,
            executor=executor,
            extra_payload={
                "usage": usage,
                "thread_id": thread_id,
                "turn_id": turn_id,
                "app_server_version": app_server_version,
            },
            render_html=render_html,
        )
        return {
            "ok": True,
            "error_code": "",
            "message": "",
            "command": command,
            "analysis_kind": analysis_kind,
            "provider": provider,
            "executor": executor,
            "analysis_markdown_path": analysis_markdown_path,
            "html_path": html_path,
            "json_path": json_path,
            "stdout_path": stdout_path,
            "stderr_path": stderr_path,
            "command_path": command_path,
            "events_path": events_path,
            "context_path": context_path,
            "log_focus_manifest_path": focus_manifest,
            "focused_log_input": focused_log_input,
            "focused_log_files": [str(path) for path in focused_files],
            "debug_log_path": debug_log_path,
            "stdout": stdout,
            "stderr": stderr,
            "duration_seconds": time.monotonic() - started,
            "evidence_count": evidence_count,
            "usage": usage,
            "thread_id": thread_id,
            "turn_id": turn_id,
            "app_server_version": app_server_version,
            "completion_state": app_server_completion_state if executor == "codex_app_server" else "",
            "app_server_error_code": app_server_error_code,
            "app_server_error_message": app_server_error_message,
            "custom_skill_analysis_status": "completed",
        }

    # ------------------------------------------------------------------
    # LD lane-level: executor + direct_api path
    # ------------------------------------------------------------------

    _LD_LOG_PACKAGES = [
        "com.xiaopeng.montecarlo",
    ]
    _LD_GREP_PATTERNS = [
        r"LD:|LDConf:|CheckTileRender|CheckLDState",
        r"SetLDCenterAndRange|updateLoadTileCenter|LDEgoPosModel",
        r"MapDataHandler|dftileinfo|handle_map_data",
        r"bizCode:132002|ReceiveMsg.*132002",
        r"normal pos too large|XldNormalIfb",
        r"tileFull|XPD_LDTileCacheManager|processTileAdd",
        r"AnpXpSroverallProc|AnpBdPosProc|HOST_ICM_SD_PERIOD_DATA",
        r"NaviServiceManager|SIGNAL_X3D_SD_OVER_ALL_DATA|SIGNAL_LD_DFUNITY_DATA",
        r"OnSuperParkActive|OnEnvModeStatusChange|eParking|UpdateModelMode",
        r"scene_conf[0-9]|nearNew|LD_STATE_CHANGED",
        r"License:|NEDC:|locationState|superPark|gear:",
        r"RenderExtend|OnParse|processLoadMesh|visible ->:",
    ]

    def _ld_executor_grep_pattern(self) -> str:
        return "|".join(self._LD_GREP_PATTERNS)

    def _normalize_log_locator(self, value: str | Path) -> str:
        return str(value).replace("\\", "/")

    def _log_basename_from_locator(self, value: str | Path) -> str:
        normalized = self._normalize_log_locator(value)
        return normalized.rsplit("/", 1)[-1]

    def _ld_prepared_log_metadata_path(
        self,
        *,
        prepared_input: Path | None,
        analysis_dir: Path,
        fault_time: str,
    ) -> Path | None:
        if prepared_input is None:
            return None
        analysis_dir.mkdir(parents=True, exist_ok=True)
        metadata_path = analysis_dir / "prepared_log_metadata.md"
        candidates: list[Path] = []
        seen: set[Path] = set()

        def _append(path: Path) -> None:
            if path in seen or not path.exists():
                return
            seen.add(path)
            candidates.append(path)

        _append(prepared_input)
        if prepared_input.is_file():
            lower_name = prepared_input.name.lower()
            if lower_name.endswith((".alog", ".xlog")):
                _append(prepared_input.with_name(prepared_input.name + ".log"))
            elif lower_name.endswith((".alog.log", ".xlog.log")):
                _append(prepared_input.with_suffix(""))
            fault_dt = self._parse_bug_datetime(fault_time)
            try:
                siblings = sorted(prepared_input.parent.iterdir())
            except OSError:
                siblings = []
            for sibling in siblings:
                if len(candidates) >= 8:
                    break
                if not sibling.is_file():
                    continue
                normalized = self._normalize_log_locator(sibling.name).lower()
                if not normalized.endswith((".alog", ".alog.log", ".xlog", ".xlog.log", ".log")):
                    continue
                if fault_dt is None:
                    _append(sibling)
                    continue
                basename = self._log_basename_from_locator(sibling.name)
                file_dt = self._parse_log_file_datetime(basename)
                if file_dt is None:
                    continue
                candidate_dt = datetime.fromtimestamp(time.mktime(file_dt))
                if abs((candidate_dt - fault_dt).total_seconds()) <= 7200:
                    _append(sibling)
        else:
            fault_dt = self._parse_bug_datetime(fault_time)
            for path in self._ld_focus_log_candidates(prepared_input, fault_dt, limit=8):
                _append(path)

        lines = [
            "# Prepared Log Metadata",
            "",
            f"- 故障时间: `{fault_time or '未识别'}`",
            f"- 主输入: `{prepared_input}`",
            f"- 主工作目录: `{prepared_input.parent if prepared_input.is_file() else prepared_input}`",
            "- 使用规则: 先调用 `read_prepared_log_metadata()`，首轮只允许围绕下面列出的路径检索。",
            "- 默认聚焦: 除非 Skill 另有建议，车道级/导航日志默认优先看 `com.xiaopeng.montecarlo` 与 `logd`，并以覆盖故障时间的文件为先（下方候选已按此排序，且 `.zst` 已解压为 `.log`）。",
            "- 限制: 不要扫描 `tools/lark-agent-bridge/data/bug_cache`、历史 job 输出或整个工作区；只有当这些候选文件明确不覆盖问题时间时，才允许扩到同目录相邻小时日志。",
            "",
            "## 候选日志",
        ]
        if candidates:
            lines.extend(f"- `{path}`" for path in candidates)
        else:
            lines.append("- 无可用候选文件")
        metadata_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        return metadata_path

    def _ld_executor_should_include_log_file(
        self,
        path: Path,
        *,
        locator: str | Path,
        fault_dt: "time.struct_time | None",
    ) -> bool:
        normalized = self._normalize_log_locator(locator).lower()
        if not normalized.endswith((".alog", ".alog.log", ".xlog", ".xlog.log", ".log")):
            return False
        if path.suffix == ".alog" and path.with_suffix(".alog.log").exists():
            return False
        basename = self._log_basename_from_locator(locator)
        file_dt = self._parse_log_file_datetime(basename)
        if fault_dt is None or file_dt is None:
            return True
        candidate_dt = datetime.fromtimestamp(time.mktime(file_dt))
        return abs((candidate_dt - fault_dt).total_seconds()) <= 7200

    def _ld_executor_find_log_files(
        self,
        *,
        cache_dir: Path,
        fault_time: str,
    ) -> list[Path]:
        """Find montecarlo/LD-relevant log files near fault time."""
        fault_dt = self._parse_bug_datetime(fault_time)
        results: list[Path] = []
        seen: set[Path] = set()
        logs_dir = cache_dir / "logs"

        def _append_candidate(path: Path, *, locator: str | Path) -> None:
            if path in seen or not path.is_file():
                return
            if not self._ld_executor_should_include_log_file(path, locator=locator, fault_dt=fault_dt):
                return
            seen.add(path)
            results.append(path)

        if logs_dir.exists():
            for pkg in self._LD_LOG_PACKAGES:
                for log_root in sorted(logs_dir.glob(f"data/Log/log*/app/{pkg}")):
                    for alog in sorted(log_root.glob("*.alog*")):
                        _append_candidate(alog, locator=alog.name)
            for path in sorted(logs_dir.rglob("*")):
                if not path.is_file():
                    continue
                normalized = self._normalize_log_locator(path.relative_to(logs_dir)).lower()
                if not any(pkg in normalized for pkg in self._LD_LOG_PACKAGES):
                    continue
                _append_candidate(path, locator=normalized)
        # Also check ZIP for montecarlo logs near fault time
        for zip_path in sorted((cache_dir / "attachments").glob("*.zip")) if (cache_dir / "attachments").exists() else []:
            try:
                with zipfile.ZipFile(zip_path, "r") as zf:
                    for info in zf.infolist():
                        if info.is_dir():
                            continue
                        name_lower = info.filename.lower()
                        if not any(pkg in name_lower for pkg in self._LD_LOG_PACKAGES):
                            continue
                        if not name_lower.endswith((".alog", ".alog.log", ".xlog", ".xlog.log", ".log")):
                            continue
                        basename = self._log_basename_from_locator(info.filename)
                        file_dt = self._parse_log_file_datetime(basename)
                        if fault_dt is not None and file_dt is not None:
                            candidate_dt = datetime.fromtimestamp(time.mktime(file_dt))
                            if abs((candidate_dt - fault_dt).total_seconds()) > 7200:
                                continue
                        # Extract to temp location for reading
                        extracted = cache_dir / "logs" / info.filename
                        if not extracted.exists():
                            extracted.parent.mkdir(parents=True, exist_ok=True)
                            try:
                                with zf.open(info) as src, open(extracted, "wb") as dst:
                                    shutil.copyfileobj(src, dst)
                            except OSError:
                                continue
                        results.append(extracted)
            except (zipfile.BadZipFile, OSError):
                continue
        return sorted(set(results))

    def _ld_executor_extract_evidence(
        self,
        *,
        log_files: list[Path],
        fault_time: str,
        max_lines: int = 300,
    ) -> str:
        """Extract LD-relevant lines from log files using subprocess grep for speed."""
        pattern = self._ld_executor_grep_pattern()
        fault_dt = self._parse_bug_datetime(fault_time)
        evidence_parts: list[str] = []
        total_lines = 0
        # Compute time window for grep pre-filter (fault ±3min)
        time_grep_pattern = ""
        if fault_dt is not None:
            hour = fault_dt.hour
            minute = fault_dt.minute
            time_parts: list[str] = []
            for offset in range(-3, 4):
                m = minute + offset
                h = hour + (m // 60)
                m = m % 60
                if h < 0 or h > 23:
                    continue
                time_parts.append(f" {h:02d}:{m:02d}:")
            if time_parts:
                time_grep_pattern = "|".join(time_parts)
        for log_file in log_files:
            if total_lines >= max_lines:
                break
            if not log_file.exists():
                continue
            # Skip binary files
            try:
                raw = log_file.read_bytes()[:512]
                if b"\x00" in raw:
                    continue
            except OSError:
                continue
            # Use subprocess grep for speed on large files
            # Pipeline: grep time window first (fast), then grep LD patterns
            try:
                if time_grep_pattern:
                    grep_cmd = f"grep -E {shlex.quote(time_grep_pattern)} {shlex.quote(str(log_file))} | grep -iE {shlex.quote(pattern)}"
                else:
                    grep_cmd = f"grep -iE {shlex.quote(pattern)} {shlex.quote(str(log_file))}"
                result = subprocess.run(
                    grep_cmd, shell=True, capture_output=True, text=True,
                    timeout=30, check=False,
                )
                lines = result.stdout.splitlines()
            except (subprocess.TimeoutExpired, OSError):
                continue
            remaining = max_lines - total_lines
            lines = lines[:remaining]
            if lines:
                relative_name = log_file.name
                try:
                    parts = log_file.parts
                    app_idx = next((i for i, p in enumerate(parts) if p == "app"), None)
                    if app_idx is not None and app_idx + 1 < len(parts):
                        relative_name = "/".join(parts[app_idx:])
                except (StopIteration, IndexError):
                    pass
                evidence_parts.append(
                    f"### {relative_name}\n"
                    f"匹配行数: {len(lines)}\n"
                    f"```\n" + "\n".join(lines) + "\n```\n"
                )
                total_lines += len(lines)
        if not evidence_parts:
            return (
                "## LD 日志证据提取结果\n\n"
                f"**未在 montecarlo 日志中找到 LD 相关证据行。**\n"
                f"已搜索 {len(log_files)} 个日志文件，grep 模式: `{pattern[:80]}...`\n"
                f"故障时间: {fault_time}\n\n"
                "可能原因：故障时间对应的 montecarlo 日志不在当前日志包中。\n"
            )
        return (
            f"## LD 日志证据提取结果\n\n"
            f"故障时间: {fault_time}\n"
            f"搜索文件数: {len(log_files)}\n"
            f"匹配行数: {total_lines}\n\n"
            + "\n".join(evidence_parts)
        )

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

    def _parse_any_log_datetime(self, name: str) -> datetime | None:
        """Parse a datetime from a log filename.

        Supports ``main_YYYY-MM-DD_HH-MM`` and the montecarlo
        ``DIAG_D-NNN-YYYYMMDD-HHMMSS_...`` naming.
        """
        st = self._parse_log_file_datetime(name)
        if st is not None:
            try:
                return datetime.fromtimestamp(time.mktime(st))
            except (OverflowError, ValueError, OSError):
                return None
        m = re.search(r"(20\d{2})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})", name)
        if m:
            return self._safe_datetime(*(int(m.group(i)) for i in range(1, 7)))
        return None

    def _is_montecarlo_or_logd_path(self, path: Path) -> bool:
        parts = [part.casefold() for part in path.parts]
        return any(part == "logd" for part in parts) or any("montecarlo" in part for part in parts)

    def _decompress_zst(self, src: Path) -> Path | None:
        """zstd-decompress ``src`` (``*.zst``) to the path without the suffix."""
        src = src.resolve()
        dst = src.with_suffix("")
        if dst.exists():
            return dst
        zstd = shutil.which("zstd")
        if not zstd:
            logger.warning("zstd not found; cannot decompress %s", src)
            return None
        try:
            result = _run_tracked_process(
                [zstd, "-d", "-q", "-k", "-o", str(dst), str(src)],
                watchdog=self.process_watchdog,
                name="prepare-zst-decompress",
                cwd=self._working_dir(),
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except Exception as exc:
            logger.warning("zstd decompress failed for %s: %s", src, exc)
            return None
        if getattr(result, "returncode", 1) != 0 or not dst.exists():
            logger.warning("zstd decompress returned non-zero for %s", src)
            return None
        return dst

    def _ld_focus_log_candidates(self, root: Path, fault_dt: datetime | None, *, limit: int = 8) -> list[Path]:
        """Rank readable log candidates, defaulting to montecarlo/logd near the fault time.

        Montecarlo/logd logs rank first, then by time distance to the fault. Any
        selected montecarlo/logd ``.zst`` nav log is decompressed in place so the
        agent gets readable ``.log`` files covering the fault time by default.
        """
        scored: list[tuple[int, float, str, Path]] = []
        preferred: list[tuple[float, str, Path]] = []
        others: list[tuple[float, str, Path]] = []
        window_seconds = float(6 * 3600)
        try:
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                is_zst = path.name.lower().endswith(".zst")
                montecarlo_or_logd = self._is_montecarlo_or_logd_path(path)
                if not (self._is_log_coverage_file(path) or (is_zst and montecarlo_or_logd)):
                    continue
                file_dt = self._parse_any_log_datetime(path.name)
                if fault_dt is not None and file_dt is not None:
                    distance = abs((file_dt - fault_dt).total_seconds())
                elif file_dt is None:
                    distance = float(10 ** 9)
                else:
                    distance = 0.0
                if montecarlo_or_logd:
                    # Bound montecarlo/logd to the fault window so the candidate
                    # list defaults to logs covering the problem time.
                    if fault_dt is None or file_dt is None or distance <= window_seconds:
                        preferred.append((distance, str(path), path))
                elif len(others) < _BUG_LOG_COVERAGE_MAX_FILES:
                    others.append((distance, str(path), path))
        except OSError:
            return []
        preferred.sort(key=lambda item: (item[0], item[1]))
        others.sort(key=lambda item: (item[0], item[1]))
        ordered = [path for _, _, path in preferred] + [path for _, _, path in others]
        result: list[Path] = []
        for path in ordered:
            if len(result) >= limit:
                break
            if path.name.lower().endswith(".zst"):
                decoded = self._decompress_zst(path)
                if decoded is None:
                    continue
                path = decoded
            result.append(path)
        return result
