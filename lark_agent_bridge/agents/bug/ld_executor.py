from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from . import _shared


from .ld_log_evidence import _LdLogEvidenceMixin
from .ld_pydantic_runs import _LdPydanticRunsMixin


class _LdExecutorMixin(_LdLogEvidenceMixin, _LdPydanticRunsMixin):
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
