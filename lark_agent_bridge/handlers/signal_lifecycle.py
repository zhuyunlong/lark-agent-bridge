"""Signal lifecycle handler."""

from __future__ import annotations

import json
import time

from ..downloader import DownloadError, LogDownloader
from ..models import BridgeConfig, LarkEvent, SignalRequest, TaskResult, create_job_context
from ..runner import SignalChainRunner


class SignalLifecycleHandler:
    def __init__(
        self,
        config: BridgeConfig,
        downloader: LogDownloader,
        runner: SignalChainRunner,
    ) -> None:
        self.config = config
        self.downloader = downloader
        self.runner = runner

    def handle(self, request: SignalRequest, *, event: LarkEvent | None = None) -> TaskResult:
        started = time.monotonic()
        if not request.signal:
            return TaskResult(
                success=False,
                message="缺少 signal：请提供 132002 或 SIGNAL_... 形式的信号。",
                error_code="missing_signal",
            )
        if not request.resources:
            return TaskResult(
                success=False,
                message="缺少日志输入：请在消息中提供日志 URL 或飞书附件。",
                error_code="missing_log",
            )

        context = create_job_context(self.config.data_dir, event=event)
        self._write_job_file(context.job_dir / "job.json", request, event)
        try:
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
            )

        log_path = downloaded[0].path if len(downloaded) == 1 else context.input_dir
        runner_result = self.runner.run(
            signal=request.signal,
            log_path=log_path,
            output_dir=context.output_dir,
            since=request.since,
        )
        duration = time.monotonic() - started
        downloads = [
            {
                "kind": item.resource.kind,
                "value": item.resource.value,
                "path": str(item.path),
                "dry_run": item.dry_run,
                "command": item.command,
            }
            for item in downloaded
        ]
        if not runner_result.success:
            return TaskResult(
                success=False,
                message=(
                    f"runner 失败：{runner_result.message}\n"
                    f"job: {context.job_dir}\n"
                    f"stderr: {runner_result.stderr[:500]}"
                ),
                job_id=context.job_id,
                job_dir=context.job_dir,
                html_report=runner_result.html_report,
                json_report=runner_result.json_report,
                command=runner_result.command,
                duration_seconds=duration,
                error_code=runner_result.error_code,
                stdout=runner_result.stdout,
                stderr=runner_result.stderr,
                details={"downloads": downloads},
            )

        message = self._summary_message(request, context, runner_result, downloads, duration)
        return TaskResult(
            success=True,
            message=message,
            job_id=context.job_id,
            job_dir=context.job_dir,
            html_report=runner_result.html_report,
            json_report=runner_result.json_report,
            command=runner_result.command,
            duration_seconds=duration,
            details={"downloads": downloads},
        )

    def _summary_message(
        self,
        request: SignalRequest,
        context,
        runner_result: TaskResult,
        downloads: list[dict[str, object]],
        duration: float,
    ) -> str:
        mode = "dry-run 计划" if self.config.dry_run else "处理完成"
        first_source = downloads[0]["value"] if downloads else ""
        lines = [
            f"{mode}: signal {request.signal}",
            f"日志来源: {first_source}",
            f"HTML 报告: {runner_result.html_report}",
            f"JSON 报告: {runner_result.json_report}",
            f"job: {context.job_dir}",
            f"耗时: {duration:.2f}s",
        ]
        if request.since:
            lines.insert(2, f"时间范围: {request.since}")
        return "\n".join(lines)

    def _write_job_file(self, path, request: SignalRequest, event: LarkEvent | None) -> None:
        payload = {
            "signal": request.signal,
            "since": request.since,
            "resources": [{"kind": item.kind, "value": item.value} for item in request.resources],
            "event_id": event.event_id if event else None,
            "message_id": event.message_id if event else None,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

