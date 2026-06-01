"""Repository-only source analysis orchestration."""

from __future__ import annotations

from pathlib import Path
import time
from typing import Callable

from .knowledge.source_investigation import SourceInvestigationResult, SourceInvestigationRunner
from .models import BridgeConfig, LarkEvent, SourceAnalysisRequest, TaskResult, create_job_context
from .reporting.source_report_html import render_source_analysis_report


class RepositorySourceAnalysisRunner:
    def __init__(
        self,
        config: BridgeConfig,
        *,
        source_runner: SourceInvestigationRunner | None = None,
        bug_runner: object | None = None,
    ) -> None:
        self.config = config
        self.source_runner = source_runner or SourceInvestigationRunner(config)
        self.bug_runner = bug_runner

    def run(
        self,
        request: SourceAnalysisRequest,
        event: LarkEvent | None = None,
        *,
        progress_callback: Callable[[dict[str, object]], None] | None = None,
    ) -> TaskResult:
        started = time.monotonic()
        if not request.prompt.strip():
            return TaskResult(
                success=False,
                message="源码分析请求为空。",
                error_code="missing_source_analysis_prompt",
                details={"mode": "source_analysis"},
            )

        app_server_result = self._run_via_app_server_if_configured(
            request,
            event,
            progress_callback=progress_callback,
            started=started,
        )
        if app_server_result is not None:
            return app_server_result

        context = create_job_context(self.config.data_dir, event)
        output_dir = context.output_dir
        html_path = output_dir / "source_analysis_report.html"
        self._emit(progress_callback, "source_analysis_started", "执行源码调查", target=request.target)
        result = self.source_runner.run(request.prompt, hits=[])
        duration = time.monotonic() - started
        if not result.success:
            return TaskResult(
                success=False,
                message=f"源码分析失败：{result.error or '未返回成功结果'}",
                job_id=context.job_id,
                job_dir=context.job_dir,
                command=result.command or None,
                duration_seconds=duration,
                error_code="source_analysis_failed",
                stdout=result.stdout,
                stderr=result.stderr,
                details={
                    "mode": "source_analysis",
                    "source_mode": request.source_mode,
                    "context_profile": "source_analysis",
                    "classification_source": "deterministic_source_request",
                    "source_execution_backend": "source_investigation",
                    "user_request_text": request.raw_text or request.prompt,
                    "target": request.target,
                },
            )

        html_path.write_text(
            render_source_analysis_report(
                title=f"{request.target or '源码'} 源码分析",
                request_text=request.prompt,
                answer=result.answer,
                target=request.target or result.canonical_key,
                source_evidence=result.source_evidence,
                coverage_boundary=result.coverage_boundary,
                diagram_kinds=request.diagram_kinds,
                backend="source_investigation",
                success=True,
            ),
            encoding="utf-8",
        )
        message = _summary_message(result)
        self._emit(progress_callback, "source_analysis_completed", "源码调查完成", target=request.target)
        return TaskResult(
            success=True,
            message=message,
            job_id=context.job_id,
            job_dir=context.job_dir,
            html_report=html_path,
            command=result.command or None,
            duration_seconds=duration,
            stdout=result.stdout,
            stderr=result.stderr,
            details={
                "mode": "source_analysis",
                "source_mode": request.source_mode,
                "context_profile": "source_analysis",
                "classification_source": "deterministic_source_request",
                "classification_reason": request.reason,
                "source_execution_backend": "source_investigation",
                "source_confidence": result.confidence,
                "source_canonical_key": result.canonical_key,
                "source_answer": result.answer,
                "coverage_boundary": result.coverage_boundary,
                "source_evidence": result.source_evidence,
                "target": request.target,
                "diagram_kinds": list(request.diagram_kinds),
                "user_request_text": request.raw_text or request.prompt,
                "files_to_send": [html_path],
            },
        )

    def _run_via_app_server_if_configured(
        self,
        request: SourceAnalysisRequest,
        event: LarkEvent | None,
        *,
        progress_callback: Callable[[dict[str, object]], None] | None,
        started: float,
    ) -> TaskResult | None:
        options = self.config.codex_app_server
        if not (options.enabled and options.use_for_file_agent and self.bug_runner is not None):
            return None
        runner = self.bug_runner
        execute = getattr(runner, "_run_custom_skill_agent_analysis", None)
        if not callable(execute):
            return None
        context = create_job_context(self.config.data_dir, event)
        analysis_dir = context.output_dir / "source_stage"
        html_path = context.output_dir / "source_analysis_report.html"
        json_path = context.output_dir / "source_analysis_report.json"
        self._emit(progress_callback, "source_analysis_app_server_started", "通过 Codex app-server 执行源码分析", target=request.target)
        try:
            execution = execute(
                analysis_kind="source_stage",
                analysis_label="源码分析",
                skill_name="source_analysis",
                request_text=request.raw_text or request.prompt,
                prompt_text=_source_stage_prompt(request),
                title=f"{request.target or '源码'} 源码分析",
                description="",
                fault_time="",
                selected_input=None,
                prepared_input=None,
                source_evidence_path=None,
                html_path=html_path,
                json_path=json_path,
                analysis_dir=analysis_dir,
                progress_callback=progress_callback,
                timeout=max(1, int(options.turn_timeout_seconds or self.config.source_investigation.timeout_seconds)),
                bridge_session_id=(event.message_id if event else "") or (event.event_id if event else ""),
                prior_findings=None,
                context_profile="source_analysis",
                provider_override="codex",
                command_override=options.command or "codex",
            )
        except Exception:
            return None
        if not isinstance(execution, dict) or not execution.get("ok"):
            return None
        produced_html = Path(execution.get("html_path") or html_path)
        analysis_path = execution.get("analysis_markdown_path")
        analysis_text = ""
        if isinstance(analysis_path, Path) and analysis_path.exists():
            analysis_text = analysis_path.read_text(encoding="utf-8", errors="replace").strip()
        duration = time.monotonic() - started
        self._emit(progress_callback, "source_analysis_app_server_completed", "Codex app-server 源码分析完成", target=request.target)
        return TaskResult(
            success=True,
            message=_first_line(analysis_text) or "源码分析完成，已生成 HTML 报告。",
            job_id=context.job_id,
            job_dir=context.job_dir,
            html_report=produced_html if produced_html.exists() else None,
            command=list(execution.get("command") or []) or None,
            duration_seconds=duration,
            stdout=str(execution.get("stdout") or ""),
            stderr=str(execution.get("stderr") or ""),
            details={
                "mode": "source_analysis",
                "source_mode": request.source_mode,
                "context_profile": "source_analysis",
                "classification_source": "deterministic_source_request",
                "classification_reason": request.reason,
                "source_execution_backend": str(execution.get("executor") or "file_agent"),
                "provider": str(execution.get("provider") or "codex"),
                "analysis_skill": "source_analysis",
                "analysis_skill_label": "源码分析",
                "target": request.target,
                "source_answer": analysis_text,
                "diagram_kinds": list(request.diagram_kinds),
                "user_request_text": request.raw_text or request.prompt,
                "source_analysis_file": str(analysis_path or ""),
                "app_server_version": str(execution.get("app_server_version") or ""),
                "app_server_thread_id": str(execution.get("thread_id") or ""),
                "app_server_turn_id": str(execution.get("turn_id") or ""),
                "files_to_send": [produced_html] if produced_html.exists() else [],
            },
        )

    def _emit(
        self,
        progress_callback: Callable[[dict[str, object]], None] | None,
        stage: str,
        message: str,
        **details: object,
    ) -> None:
        if progress_callback is None:
            return
        progress_callback({"stage": stage, "message": message, "details": details})


def _summary_message(result: SourceInvestigationResult) -> str:
    first = _first_line(result.answer)
    if first:
        return first
    if result.source_evidence:
        return "源码分析完成，已整理关键证据。"
    return "源码分析完成，但证据不足，已输出边界说明。"


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:1200]
    return ""


def _source_stage_prompt(request: SourceAnalysisRequest) -> str:
    diagram_note = ""
    if request.diagram_kinds:
        diagram_note = "输出中需要覆盖链路/泳道/时序关系，HTML 由 bridge 后续发布。"
    return "\n".join(
        part
        for part in (
            request.prompt.strip(),
            "",
            "这是一个无 bug 链接、无日志附件的仓库源码分析请求。",
            "请只读源码，优先定位定义、映射、注册、监听、分发、下游消费路径。",
            "结论必须包含源码文件和行号证据；证据不足时明确边界。",
            diagram_note,
        )
        if part
    )
