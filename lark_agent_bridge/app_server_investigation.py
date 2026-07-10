"""Explicit app-server autonomous analysis for bug links or direct log uploads."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import concurrent.futures
import json
import re
import time
from typing import Callable

from .downloader import DownloadError, LogDownloader
from .models import AppServerInvestigationRequest, BridgeConfig, DownloadResource, LarkEvent, TaskResult, create_job_context
from .reporting.app_server_report_html import _first_section, render_app_server_report
from .skill_manager import SkillManager, SkillRecord
from .token_usage import normalize_token_usage
from .agents.codex_app_server_runtime import CodexAppServerTurnController


@dataclass(slots=True)
class PreparedAppServerInvestigation:
    context: object
    request_text: str
    prompt_text: str
    bug_url: str
    title: str
    description: str
    trigger_mode: str
    trigger_term: str
    source_roots: list[Path]
    fault_time: str
    fault_time_source: str
    fault_time_note: str
    selected_input: Path | None
    prepared_input: Path | None
    focused_log_input: Path | None
    log_focus_manifest: Path | None
    bridge_session_id: str
    analysis_dir: Path


class AppServerInvestigationRunner:
    def __init__(
        self,
        config: BridgeConfig,
        *,
        bug_runner: object,
        skill_manager: SkillManager,
        downloader: LogDownloader | None = None,
    ) -> None:
        self.config = config
        self.bug_runner = bug_runner
        self.skill_manager = skill_manager
        self.downloader = downloader or LogDownloader(config, getattr(bug_runner, "_lark_client", None))

    def run(
        self,
        request: AppServerInvestigationRequest,
        *,
        event: LarkEvent | None = None,
        progress_callback: Callable[[dict[str, object]], None] | None = None,
        control: CodexAppServerTurnController | None = None,
    ) -> TaskResult:
        options = self.config.bug_analysis.app_server_investigation
        if not options.enabled:
            return TaskResult(
                success=False,
                message="AI 自主分析未启用，请先在配置中打开 [bug_analysis.app_server_investigation].enabled。",
                error_code="app_server_investigation_disabled",
                details={"mode": "app_server_investigation"},
            )
        if not options.prompt_template.strip():
            return TaskResult(
                success=False,
                message="AI 自主分析缺少 prompt_template 配置，请先在配置文件中补齐提示词。",
                error_code="app_server_investigation_prompt_missing",
                details={"mode": "app_server_investigation"},
            )
        if not self.config.codex_app_server.enabled:
            return TaskResult(
                success=False,
                message="codex_app_server 未启用，无法执行自主分析模式。",
                error_code="codex_app_server_disabled",
                details={"mode": "app_server_investigation"},
            )

        if request.bug_url:
            prepared_or_result = self._prepare_bug_request(
                request,
                event=event,
                progress_callback=progress_callback,
            )
        else:
            prepared_or_result = self._prepare_direct_request(
                request,
                event=event,
                progress_callback=progress_callback,
            )
        if isinstance(prepared_or_result, TaskResult):
            return prepared_or_result
        if control is not None and control.is_cancelled:
            return self._cancelled_result(
                prepared_or_result,
                started=time.monotonic(),
                reason=control.cancel_reason,
            )
        return self._run_prepared(prepared_or_result, event=event, progress_callback=progress_callback, control=control)

    def _prepare_bug_request(
        self,
        request: AppServerInvestigationRequest,
        *,
        event: LarkEvent | None,
        progress_callback: Callable[[dict[str, object]], None] | None,
    ) -> PreparedAppServerInvestigation | TaskResult:
        context = create_job_context(self.config.data_dir, event=event)
        analysis_dir = context.output_dir / "app_server_investigation"
        analysis_dir.mkdir(parents=True, exist_ok=True)
        request_text = self.bug_runner._request_text(
            raw_text=request.raw_text,
            prompt_text=request.prompt,
            bug_url=request.bug_url,
        )
        bridge_session_id = self.bug_runner._bridge_session_id(event)
        bridge_kwargs = {"bridge_session_id": bridge_session_id} if bridge_session_id else {}
        started = time.monotonic()

        try:
            self.bug_runner._emit_progress(progress_callback, stage="app_server_bug_check_env", message="检查 meegle 环境")
            env_status = self.bug_runner._run_json_command(
                [str(self.bug_runner._bug_fetcher_script()), "check-env"],
                timeout=60,
                **bridge_kwargs,
            )
            if not env_status.get("meegle_installed", False):
                return TaskResult(
                    success=False,
                    message="自主分析前置条件缺失：本机未安装 meegle CLI。",
                    error_code="bug_analysis_missing_meegle",
                    duration_seconds=time.monotonic() - started,
                    details={"mode": "app_server_investigation"},
                )
            if not env_status.get("auth_ok", False):
                return TaskResult(
                    success=False,
                    message="自主分析前置条件缺失：meegle 未登录，请先在本机完成 `meegle auth login`。",
                    error_code="bug_analysis_meegle_not_auth",
                    duration_seconds=time.monotonic() - started,
                    details={"mode": "app_server_investigation"},
                )

            self.bug_runner._emit_progress(progress_callback, stage="app_server_bug_resolve_url", message="解析 bug 链接")
            resolved = self.bug_runner._run_json_command(
                [str(self.bug_runner._bug_fetcher_script()), "resolve-url", request.bug_url],
                timeout=60,
                **bridge_kwargs,
            )
            project_key = str(resolved["project_key"])
            work_item_id = str(resolved["work_item_id"])
            bug_dir = self.bug_runner._bug_cache_dir(project_key, work_item_id)
            if bug_dir.exists() and not self.bug_runner._is_bug_cache_fresh(
                bug_dir,
                max_age_hours=self.config.job_retention.bug_cache_max_age_hours,
            ):
                self.bug_runner._remove_tree(bug_dir)
            bug_dir.mkdir(parents=True, exist_ok=True)

            self.bug_runner._emit_progress(
                progress_callback,
                stage="app_server_bug_fetch_data",
                message="拉取 bug 详情和字段信息",
                project_key=project_key,
                work_item_id=work_item_id,
            )
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                future_fetched = pool.submit(
                    self.bug_runner._run_json_command,
                    [str(self.bug_runner._bug_fetcher_script()), "fetch-data", project_key, work_item_id],
                    timeout=120,
                    **bridge_kwargs,
                )
                future_full_item = pool.submit(
                    self.bug_runner._run_json_command,
                    ["meegle", "workitem", "get", "--project-key", project_key, "--work-item-id", work_item_id, "--format", "json"],
                    timeout=120,
                    **bridge_kwargs,
                )
                pool.submit(self.bug_runner.signal_resolver._load_catalog)
                fetched = future_fetched.result()
                full_item = future_full_item.result()

            title = str(fetched.get("title", ""))
            description = self.bug_runner._bug_description(fetched)
            time_context = self.bug_runner._resolve_bug_time_context(
                request_text=request_text,
                title=title,
                description=description,
                reference_time=self.bug_runner._bug_reference_time(fetched, full_item),
            )
            if not time_context.has_full_datetime:
                return self.bug_runner._bug_time_clarification_result(
                    context=context,
                    started=started,
                    request_text=request_text,
                    bug_url=request.bug_url,
                    time_context=time_context,
                    status="missing_fault_time",
                    progress_callback=progress_callback,
                )

            selected_input = self.bug_runner._select_log_input(bug_dir, fetched)
            if self.bug_runner._has_bug_cache_content(bug_dir) and selected_input is not None:
                self.bug_runner._emit_progress(
                    progress_callback,
                    stage="app_server_bug_reuse_cache",
                    message="复用 bug 已缓存的附件和日志",
                    bug_cache_dir=str(bug_dir),
                )
                download = {"ok": True, "reused": True}
            else:
                self.bug_runner._emit_progress(progress_callback, stage="app_server_bug_download", message="下载 bug 附件和日志")
                download = self.bug_runner._download_bug_attachments(
                    project_key,
                    work_item_id,
                    bug_dir,
                    fetched.get("attachments", []),
                    timeout=self.config.bug_analysis.timeout_seconds,
                    **bridge_kwargs,
                )
                selected_input = self.bug_runner._select_log_input(bug_dir, fetched)
            if selected_input is None:
                attachment_lines = self.bug_runner._render_attachment_lines(fetched.get("attachments", []), download)
                return TaskResult(
                    success=False,
                    message="自主分析失败：未找到可用日志附件。\n附件结果：\n" + attachment_lines,
                    error_code="bug_analysis_missing_log_attachment",
                    duration_seconds=time.monotonic() - started,
                    details={"mode": "app_server_investigation", "bug_url": request.bug_url},
                )

            prepared_input = self.bug_runner._reuse_prepared_bug_input(selected_input)
            if prepared_input is None:
                self.bug_runner._emit_progress(progress_callback, stage="app_server_bug_prepare_logs", message="准备日志输入")
                prepared_input = self.bug_runner._prepare_log_input(selected_input)
            log_coverage = self.bug_runner._scan_log_time_coverage(prepared_input, fault_time=time_context.fault_time)
            if log_coverage is None or not log_coverage.has_time_evidence:
                return self.bug_runner._bug_time_clarification_result(
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
                return self.bug_runner._bug_time_clarification_result(
                    context=context,
                    started=started,
                    request_text=request_text,
                    bug_url=request.bug_url,
                    time_context=time_context,
                    status="log_not_covering_fault_time",
                    progress_callback=progress_callback,
                    log_coverage=log_coverage,
                )

            focused_log_input, log_focus_manifest, _ = self.bug_runner._build_file_agent_focus_dir(
                input_path=prepared_input,
                fault_time=time_context.fault_time,
                analysis_kind="source_stage",
                analysis_dir=analysis_dir,
            )
            source_roots = self._source_roots()
            return PreparedAppServerInvestigation(
                context=context,
                request_text=request_text,
                prompt_text=request.prompt.strip(),
                bug_url=request.bug_url,
                title=title,
                description=description,
                trigger_mode=request.trigger_mode,
                trigger_term=request.trigger_term,
                source_roots=source_roots,
                fault_time=time_context.fault_time,
                fault_time_source=time_context.source,
                fault_time_note=time_context.note,
                selected_input=selected_input,
                prepared_input=prepared_input,
                focused_log_input=focused_log_input,
                log_focus_manifest=log_focus_manifest,
                bridge_session_id=bridge_session_id,
                analysis_dir=analysis_dir,
            )
        except Exception as exc:
            return TaskResult(
                success=False,
                message=f"AI 自主分析前置失败：{exc}",
                error_code="app_server_investigation_prepare_failed",
                duration_seconds=time.monotonic() - started,
                details={"mode": "app_server_investigation", "bug_url": request.bug_url},
            )

    def _prepare_direct_request(
        self,
        request: AppServerInvestigationRequest,
        *,
        event: LarkEvent | None,
        progress_callback: Callable[[dict[str, object]], None] | None,
    ) -> PreparedAppServerInvestigation | TaskResult:
        options = self.config.bug_analysis.app_server_investigation
        if not request.resources:
            return TaskResult(
                success=False,
                message="自主分析缺少输入：请提供 Bug 链接，或在消息里携带日志附件/文件。",
                error_code="app_server_investigation_missing_input",
                details={"mode": "app_server_investigation"},
            )
        if options.require_description_for_file_resources and not request.prompt.strip():
            return TaskResult(
                success=False,
                message="自主分析缺少问题现象描述：请在触发词后补充要调查的现象。",
                error_code="app_server_investigation_missing_prompt",
                details={"mode": "app_server_investigation"},
            )

        context = create_job_context(self.config.data_dir, event=event)
        analysis_dir = context.output_dir / "app_server_investigation"
        analysis_dir.mkdir(parents=True, exist_ok=True)
        request_text = self.bug_runner._request_text(raw_text=request.raw_text, prompt_text=request.prompt, bug_url="")
        started = time.monotonic()
        time_context = self.bug_runner._resolve_bug_time_context(
            request_text=request_text,
            title="",
            description="",
            reference_time=self.bug_runner._event_reference_time_text(event),
        )
        if options.require_time_for_file_resources and not time_context.has_full_datetime:
            return self.bug_runner._bug_time_clarification_result(
                context=context,
                started=started,
                request_text=request_text,
                bug_url="",
                time_context=time_context,
                status="missing_fault_time",
                progress_callback=progress_callback,
            )
        try:
            self.bug_runner._emit_progress(progress_callback, stage="app_server_direct_download", message="下载直传附件或日志")
            downloaded = self.downloader.download_all(
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
                details={"mode": "app_server_investigation"},
            )
        selected_input = downloaded[0].path if len(downloaded) == 1 else context.input_dir
        self.bug_runner._emit_progress(progress_callback, stage="app_server_direct_prepare_logs", message="准备直传日志输入")
        prepared_input = self.bug_runner._prepare_log_input(selected_input) if selected_input.exists() else selected_input
        if options.require_time_for_file_resources:
            log_coverage = self.bug_runner._scan_log_time_coverage(prepared_input, fault_time=time_context.fault_time)
            if not log_coverage.has_time_evidence:
                return self.bug_runner._bug_time_clarification_result(
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
                return self.bug_runner._bug_time_clarification_result(
                    context=context,
                    started=started,
                    request_text=request_text,
                    bug_url="",
                    time_context=time_context,
                    status="log_not_covering_fault_time",
                    progress_callback=progress_callback,
                    log_coverage=log_coverage,
                )
        focused_log_input, log_focus_manifest, _ = self.bug_runner._build_file_agent_focus_dir(
            input_path=prepared_input,
            fault_time=time_context.fault_time,
            analysis_kind="source_stage",
            analysis_dir=analysis_dir,
        )
        return PreparedAppServerInvestigation(
            context=context,
            request_text=request_text,
            prompt_text=request.prompt.strip(),
            bug_url="",
            title="",
            description="",
            trigger_mode=request.trigger_mode,
            trigger_term=request.trigger_term,
            source_roots=self._source_roots(),
            fault_time=time_context.fault_time,
            fault_time_source=time_context.source,
            fault_time_note=time_context.note,
            selected_input=selected_input,
            prepared_input=prepared_input,
            focused_log_input=focused_log_input,
            log_focus_manifest=log_focus_manifest,
            bridge_session_id=self.bug_runner._bridge_session_id(event),
            analysis_dir=analysis_dir,
        )

    def _run_prepared(
        self,
        prepared: PreparedAppServerInvestigation,
        *,
        event: LarkEvent | None,
        progress_callback: Callable[[dict[str, object]], None] | None,
        control: CodexAppServerTurnController | None = None,
    ) -> TaskResult:
        started = time.monotonic()
        context_path, context_json_path, context_payload = self._write_context_files(prepared)
        skill_inventory_path, skill_inventory_json_path, inventory_payload = self._write_skill_inventory_files(prepared.analysis_dir)
        output_path = prepared.analysis_dir / "app_server_investigation.md"
        html_path = prepared.context.output_dir / "app_server_investigation_report.html"
        stdout_path = prepared.analysis_dir / "app_server_investigation.stdout.txt"
        stderr_path = prepared.analysis_dir / "app_server_investigation.stderr.txt"
        command_path = prepared.analysis_dir / "app_server_investigation.command.json"
        events_path = prepared.analysis_dir / "app_server_investigation.app_server_events.jsonl"
        prompt = self._render_prompt(
            prepared,
            context_path=context_path,
            context_json_path=context_json_path,
            skill_inventory_path=skill_inventory_path,
            skill_inventory_json_path=skill_inventory_json_path,
            output_path=output_path,
        )
        self.bug_runner._emit_progress(
            progress_callback,
            stage="app_server_investigation_started",
            message="通过 Codex app-server 执行自主分析",
            bug_url=prepared.bug_url,
            output_path=str(output_path),
        )
        run_kwargs: dict[str, object] = {
            "analysis_kind": "app_server_investigation",
            "skill_name": "app_server_investigation",
            "prompt_text": prompt,
            "cwd": self._analysis_cwd(prepared),
            "command_path": command_path,
            "stdout_path": stdout_path,
            "stderr_path": stderr_path,
            "events_path": events_path,
            "progress_callback": progress_callback,
            "timeout": int(self.config.codex_app_server.turn_timeout_seconds or self.config.bug_analysis.timeout_seconds),
            "bridge_session_id": prepared.bridge_session_id,
            "model_override": self.config.bug_analysis.app_server_investigation.model,
            "reasoning_effort_override": self.config.bug_analysis.app_server_investigation.reasoning_effort,
        }
        if control is not None:
            run_kwargs["app_server_control"] = control
        result = self.bug_runner._run_custom_skill_agent_via_codex_app_server(**run_kwargs)
        usage = normalize_token_usage(result.get("usage") if isinstance(result.get("usage"), dict) else None)
        if not result.get("ok"):
            details = {
                "mode": "app_server_investigation",
                "bug_url": prepared.bug_url,
                "trigger_mode": prepared.trigger_mode,
                "trigger_term": prepared.trigger_term,
                "context_path": str(context_path),
                "skill_inventory_path": str(skill_inventory_path),
                "output_path": str(output_path),
                "app_server_version": str(result.get("app_server_version") or ""),
                "app_server_thread_id": str(result.get("thread_id") or ""),
                "app_server_turn_id": str(result.get("turn_id") or ""),
            }
            details.update(_app_server_usage_details(usage))
            return TaskResult(
                success=False,
                message=str(result.get("message") or "Codex app-server AI 自主分析失败。"),
                job_id=prepared.context.job_id,
                job_dir=prepared.context.job_dir,
                duration_seconds=time.monotonic() - started,
                error_code=str(result.get("error_code") or "codex_app_server_failed"),
                stdout=str(result.get("stdout") or ""),
                stderr=str(result.get("stderr") or ""),
                details=details,
            )
        markdown = str(result.get("final_text") or "").strip() or str(result.get("stdout") or "").strip()
        if not markdown:
            return TaskResult(
                success=False,
                message="Codex app-server 未返回可用 Markdown 正文。",
                job_id=prepared.context.job_id,
                job_dir=prepared.context.job_dir,
                duration_seconds=time.monotonic() - started,
                error_code="app_server_investigation_empty_output",
                stdout=str(result.get("stdout") or ""),
                stderr=str(result.get("stderr") or ""),
                details={"mode": "app_server_investigation", "bug_url": prepared.bug_url},
            )
        output_path.write_text(markdown.rstrip() + "\n", encoding="utf-8")
        html_path.write_text(
            self._render_html_report(
                prepared,
                markdown=markdown,
                context_path=context_path,
                skill_inventory_path=skill_inventory_path,
                context_payload=context_payload,
                inventory_payload=inventory_payload,
                usage_payload=usage,
            ),
            encoding="utf-8",
        )
        summary = _markdown_summary(markdown) or "AI 自主分析完成。"
        return TaskResult(
            success=True,
            message=summary,
            job_id=prepared.context.job_id,
            job_dir=prepared.context.job_dir,
            html_report=html_path,
            duration_seconds=time.monotonic() - started,
            stdout=str(result.get("stdout") or ""),
            stderr=str(result.get("stderr") or ""),
            command=list(result.get("command") or []) or None,
            details={
                "mode": "app_server_investigation",
                "bug_url": prepared.bug_url,
                "trigger_mode": prepared.trigger_mode,
                "trigger_term": prepared.trigger_term,
                "source_mode": "app_server_autonomous",
                "context_profile": "app_server_autonomous",
                "classification_source": "configured_app_server_investigation",
                "analysis_skill": "app_server_autonomous",
                "analysis_markdown_path": str(output_path),
                "context_path": str(context_path),
                "context_json_path": str(context_json_path),
                "skill_inventory_path": str(skill_inventory_path),
                "skill_inventory_json_path": str(skill_inventory_json_path),
                "selected_log_input": str(prepared.selected_input or ""),
                "prepared_log_input": str(prepared.prepared_input or ""),
                "focused_log_input": str(prepared.focused_log_input or ""),
                "log_focus_manifest": str(prepared.log_focus_manifest or ""),
                "fault_time": prepared.fault_time,
                "fault_time_source": prepared.fault_time_source,
                "fault_time_note": prepared.fault_time_note,
                "source_execution_backend": "codex_app_server",
                "provider": "codex",
                "app_server_version": str(result.get("app_server_version") or ""),
                "app_server_thread_id": str(result.get("thread_id") or ""),
                "app_server_turn_id": str(result.get("turn_id") or ""),
                **_app_server_usage_details(usage),
                "files_to_send": [html_path],
            },
        )

    def _cancelled_result(
        self,
        prepared: PreparedAppServerInvestigation,
        *,
        started: float,
        reason: str,
    ) -> TaskResult:
        message = f"AI 自主分析已取消：{reason or '用户要求停止当前 AI 自主分析。'}"
        return TaskResult(
            success=False,
            message=message,
            job_id=prepared.context.job_id,
            job_dir=prepared.context.job_dir,
            duration_seconds=time.monotonic() - started,
            error_code="app_server_investigation_cancelled",
            details={
                "mode": "app_server_investigation",
                "bug_url": prepared.bug_url,
                "trigger_mode": prepared.trigger_mode,
                "trigger_term": prepared.trigger_term,
                "source_mode": "app_server_autonomous",
                "context_profile": "app_server_autonomous",
                "classification_source": "configured_app_server_investigation",
            },
        )

    def _write_context_files(
        self,
        prepared: PreparedAppServerInvestigation,
    ) -> tuple[Path, Path, dict[str, object]]:
        context_path = prepared.analysis_dir / "app_server_context.md"
        context_json_path = prepared.analysis_dir / "app_server_context.json"
        payload = {
            "trigger_mode": prepared.trigger_mode,
            "trigger_term": prepared.trigger_term,
            "bug_url": prepared.bug_url,
            "user_request_text": prepared.request_text,
            "user_prompt": prepared.prompt_text,
            "bug_title": prepared.title,
            "bug_description": prepared.description,
            "fault_time": prepared.fault_time,
            "fault_time_source": prepared.fault_time_source,
            "fault_time_note": prepared.fault_time_note,
            "selected_log_input": str(prepared.selected_input or ""),
            "prepared_log_input": str(prepared.prepared_input or ""),
            "focused_log_input": str(prepared.focused_log_input or ""),
            "log_focus_manifest": str(prepared.log_focus_manifest or ""),
            "source_roots": [str(path) for path in prepared.source_roots],
        }
        context_json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        lines = [
            "# App-server Investigation Context",
            "",
            "## 1. 用户输入",
            f"- 触发模式: `{prepared.trigger_mode}`",
            f"- 触发词: `{prepared.trigger_term}`",
            f"- 用户原始请求: {prepared.request_text or '未提供'}",
            f"- 用户补充描述: {prepared.prompt_text or '未提供'}",
            "",
            "## 2. Bug / 问题背景",
            f"- Bug 链接: `{prepared.bug_url}`" if prepared.bug_url else "- Bug 链接: 未提供",
            f"- Bug 标题: {prepared.title or '未提供'}",
            f"- Bug 描述: {prepared.description or '未提供'}",
            "",
            "## 3. 时间前置结果",
            f"- 故障时间: `{prepared.fault_time or '未识别'}`",
            f"- 时间来源: `{prepared.fault_time_source or '未记录'}`",
            f"- 时间说明: {prepared.fault_time_note or '未记录'}",
            "",
            "## 4. 日志前置结果",
            f"- 原始日志目录: `{prepared.selected_input}`" if prepared.selected_input else "- 原始日志目录: 未提供",
            f"- 解密/准备后日志目录: `{prepared.prepared_input}`" if prepared.prepared_input else "- 解密/准备后日志目录: 未提供",
            f"- 本轮聚焦日志目录: `{prepared.focused_log_input}`" if prepared.focused_log_input else "- 本轮聚焦日志目录: 未生成",
            f"- 聚焦清单: `{prepared.log_focus_manifest}`" if prepared.log_focus_manifest else "- 聚焦清单: 未生成",
            "",
            "## 5. 源码范围",
        ]
        if prepared.source_roots:
            lines.extend(f"- `{path}`" for path in prepared.source_roots)
        else:
            lines.append("- 未配置源码根目录。")
        context_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        return context_path, context_json_path, payload

    def _write_skill_inventory_files(self, analysis_dir: Path) -> tuple[Path, Path, dict[str, object]]:
        inventory_path = analysis_dir / "skill_inventory.md"
        inventory_json_path = analysis_dir / "skill_inventory.json"
        records = self.skill_manager.list_skills()
        payload = {
            "skills_root": str(self.skill_manager.root_dir),
            "skills": [self._skill_payload(record) for record in records],
        }
        inventory_json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        lines = [
            "# Skill Inventory",
            "",
            f"- Skills 根目录: `{self.skill_manager.root_dir}`",
            "- 先从这里选择最合适的 skill，再按需读取对应 SKILL.md / references。",
            "",
        ]
        for record in records:
            contract = record.report_contract or {}
            lines.extend(
                [
                    f"## {record.name}",
                    f"- 标签: {record.label or record.name}",
                    f"- 角色: `{record.role}`",
                    f"- kind: `{record.kind or 'unknown'}`",
                    f"- executor: `{record.executor or 'default'}`",
                    f"- 目录: `{record.path or '(virtual)'}`",
                    f"- SKILL.md: `{record.skill_md_path or '(missing)'}`",
                    f"- 描述: {record.description or '无'}",
                    f"- 报告契约: `{json.dumps(contract, ensure_ascii=False, sort_keys=True)}`" if contract else "- 报告契约: 无",
                    "",
                ]
            )
        inventory_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        return inventory_path, inventory_json_path, payload

    def _render_prompt(
        self,
        prepared: PreparedAppServerInvestigation,
        *,
        context_path: Path,
        context_json_path: Path,
        skill_inventory_path: Path,
        skill_inventory_json_path: Path,
        output_path: Path,
    ) -> str:
        values = {
            "bug_url": prepared.bug_url,
            "request_text": prepared.request_text,
            "user_prompt": prepared.prompt_text,
            "bug_title": prepared.title,
            "bug_description": prepared.description,
            "fault_time": prepared.fault_time,
            "selected_input": str(prepared.selected_input or ""),
            "prepared_input": str(prepared.prepared_input or ""),
            "focused_log_input": str(prepared.focused_log_input or ""),
            "log_focus_manifest": str(prepared.log_focus_manifest or ""),
            "context_path": str(context_path),
            "context_json_path": str(context_json_path),
            "skill_inventory_path": str(skill_inventory_path),
            "skill_inventory_json_path": str(skill_inventory_json_path),
            "output_path": str(output_path),
            "source_roots": "\n".join(f"- {path}" for path in prepared.source_roots),
            "skills_root": str(self.skill_manager.root_dir),
        }
        try:
            return self.config.bug_analysis.app_server_investigation.prompt_template.format_map(values).strip() + "\n"
        except KeyError as exc:
            missing = str(exc).strip("'")
            raise ValueError(f"prompt_template 引用了未知占位符: {missing}") from exc

    def _render_html_report(
        self,
        prepared: PreparedAppServerInvestigation,
        *,
        markdown: str,
        context_path: Path,
        skill_inventory_path: Path,
        context_payload: dict[str, object],
        inventory_payload: dict[str, object],
        usage_payload: dict[str, object],
    ) -> str:
        sections = _markdown_sections(markdown)
        has_logs = bool(prepared.focused_log_input or prepared.prepared_input)
        token_text = _token_summary_text(
            {
                "app_server_input_tokens": usage_payload.get("input_tokens"),
                "app_server_cached_input_tokens": usage_payload.get("cached_input_tokens"),
                "app_server_output_tokens": usage_payload.get("output_tokens"),
                "app_server_total_tokens": usage_payload.get("total_tokens"),
            }
        )
        selected_skill = _detect_selected_skill(markdown, context_payload, inventory_payload)
        report_contract = _selected_skill_report_contract(inventory_payload, selected_skill)
        meta = {
            "bug_label": prepared.bug_url or (prepared.title or "直传文件"),
            "fault_time": prepared.fault_time,
            "trigger_term": prepared.trigger_term or prepared.trigger_mode,
            "selected_skill": selected_skill,
            "report_contract": report_contract,
            "has_logs": has_logs,
            "evidence_count": len(
                _section_issue_items(
                    _first_section(sections, "关键证据", "关键时间线", "已确认链路")
                )
            ),
            "token_text": token_text,
            "skill_count": len(inventory_payload.get("skills") or []),
            "context_path": context_path,
            "skill_inventory_path": skill_inventory_path,
            "context_payload": context_payload,
            "inventory_payload": inventory_payload,
            "prepared_input": prepared.prepared_input or "",
            "focused_log_input": prepared.focused_log_input or "",
            "request_text": prepared.request_text,
            "prompt_text": prepared.prompt_text,
            "description": prepared.description,
        }
        return render_app_server_report(
            title="AI 自主分析报告",
            summary_markdown=sections.get("结论摘要", ""),
            full_markdown=markdown,
            meta=meta,
        )

    def _analysis_cwd(self, prepared: PreparedAppServerInvestigation) -> Path:
        if prepared.source_roots:
            return prepared.source_roots[0]
        return self.config.workspace_root

    def _source_roots(self) -> list[Path]:
        roots = list(self.config.source_investigation.repo_roots or [])
        resolved: list[Path] = []
        for root in roots:
            try:
                candidate = root.expanduser().resolve()
            except OSError:
                continue
            if candidate.exists() and candidate not in resolved:
                resolved.append(candidate)
        return resolved

    def _skill_payload(self, record: SkillRecord) -> dict[str, object]:
        skill_md = Path(record.skill_md_path).expanduser() if record.skill_md_path else None
        references_dir = skill_md.parent / "references" if skill_md is not None else None
        references = []
        if references_dir is not None and references_dir.exists():
            references = [str(path) for path in sorted(references_dir.glob("*.md")) if path.is_file()]
        return {
            "name": record.name,
            "label": record.label,
            "description": record.description,
            "role": record.role,
            "kind": record.kind,
            "executor": record.executor,
            "path": record.path,
            "skill_md_path": record.skill_md_path,
            "references": references,
            "report_contract": record.report_contract,
        }


def _detect_selected_skill(
    markdown: str,
    context_payload: dict[str, object],
    inventory_payload: dict[str, object] | None = None,
) -> str:
    """Best-effort: which skill the app-server auto-selected (for the overview card)."""
    known = _known_skill_names(inventory_payload)
    if isinstance(context_payload, dict):
        for key in ("selected_skill", "skill", "chosen_skill"):
            value = context_payload.get(key)
            if value:
                normalized = _normalize_known_skill(str(value), known)
                if normalized:
                    return normalized
    text = markdown or ""
    patterns = (
        r"(?:primary\s+skill|主\s*skill)\s*[:：]\s*[`“\"']?([A-Za-z0-9._\-]+)",
        r"(?:命中|选择|使用)\s*skill[:：]?\s*[`“\"']?([A-Za-z0-9._\-]+)",
        r"触发\s*[`“\"']?([A-Za-z0-9._\-]+)",
    )
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("```", "`")):
            continue
        for pattern in patterns:
            match = re.search(pattern, line, re.IGNORECASE)
            if not match:
                continue
            candidate = match.group(1).strip("`'\"“”")
            normalized = _normalize_known_skill(candidate, known)
            if normalized:
                return normalized
    return ""


def _selected_skill_report_contract(inventory_payload: dict[str, object] | None, selected_skill: str) -> dict[str, object]:
    if not selected_skill or not isinstance(inventory_payload, dict):
        return {}
    skills = inventory_payload.get("skills")
    if not isinstance(skills, list):
        return {}
    selected = selected_skill.lower()
    for skill in skills:
        if not isinstance(skill, dict):
            continue
        name = str(skill.get("name") or skill.get("skill") or skill.get("id") or "").strip().lower()
        if name != selected:
            continue
        contract = skill.get("report_contract")
        return dict(contract) if isinstance(contract, dict) else {}
    return {}


def _known_skill_names(inventory_payload: dict[str, object] | None) -> dict[str, str]:
    if not isinstance(inventory_payload, dict):
        return {}
    skills = inventory_payload.get("skills")
    if not isinstance(skills, list):
        return {}
    known: dict[str, str] = {}
    for skill in skills:
        if not isinstance(skill, dict):
            continue
        for key in ("name", "skill", "id"):
            value = str(skill.get(key) or "").strip()
            if value:
                known[value.lower()] = value
    return known


def _normalize_known_skill(candidate: str, known: dict[str, str]) -> str:
    if known:
        return known.get(candidate.lower(), "")
    # Without an inventory, only trust skill-like identifiers; this avoids
    # treating business log words such as isNeedAutoFold as the selected skill.
    return candidate if re.search(r"[-_]", candidate) else ""


def _markdown_summary(text: str) -> str:
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        line = re.sub(r"^#+\s*", "", line)
        line = re.sub(r"^\d+\.\s*", "", line)
        if line in {
            "结论摘要",
            "调查方案",
            "查询路径",
            "Android 最终状态",
            "Android最终状态",
            "责任边界",
            "关键时间线",
            "关键证据",
            "已排除项",
            "已确认链路",
            "源码解释",
            "源码侧判断",
            "最可能原因",
            "待确认项",
            "建议动作",
            "建议下一步",
        }:
            continue
        return line[:1200]
    return ""


def _markdown_sections(markdown: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current = ""
    for raw_line in (markdown or "").splitlines():
        line = raw_line.rstrip()
        heading = re.match(r"^##\s+(.+?)\s*$", line)
        if heading:
            current = heading.group(1).strip()
            sections.setdefault(current, [])
            continue
        if current:
            sections[current].append(line)
    return {key: "\n".join(value).strip() for key, value in sections.items()}


def _section_issue_items(text: str) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        line = re.sub(r"^\s*(?:[-*•]|\d+\.)\s*", "", line)
        if not line:
            continue
        title, _, detail = line.partition("：")
        if not detail:
            title, _, detail = line.partition(":")
        items.append(
            {
                "sev": "yellow",
                "title": title.strip() or line,
                "detail": detail.strip(),
            }
        )
    return items


def _app_server_usage_details(usage: dict[str, int]) -> dict[str, object]:
    if not usage:
        return {}
    return {
        "app_server_usage_scope": "cumulative",
        "app_server_input_tokens": usage.get("input_tokens"),
        "app_server_cached_input_tokens": usage.get("cached_input_tokens"),
        "app_server_output_tokens": usage.get("output_tokens"),
        "app_server_total_tokens": usage.get("total_tokens"),
    }


def _token_summary_text(details: dict[str, object]) -> str:
    input_tokens = details.get("app_server_input_tokens")
    cached_input_tokens = details.get("app_server_cached_input_tokens")
    output_tokens = details.get("app_server_output_tokens")
    total_tokens = details.get("app_server_total_tokens")
    parts = []
    if isinstance(input_tokens, int):
        parts.append(f"in {input_tokens}")
    if isinstance(cached_input_tokens, int):
        parts.append(f"cache {cached_input_tokens}")
    if isinstance(output_tokens, int):
        parts.append(f"out {output_tokens}")
    if isinstance(total_tokens, int):
        parts.append(f"total {total_tokens}")
    return " / ".join(parts) if parts else "未记录"
