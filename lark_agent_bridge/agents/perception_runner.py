"""Perception summary runner."""

from __future__ import annotations

import importlib
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

from ..downloader import DownloadError, LogDownloader
from ..health import ProcessWatchdog
from ..models import BridgeConfig, LarkEvent, PerceptionSummaryRequest, TaskResult, create_job_context


def _run_tracked_process(*args, **kwargs):
    return importlib.import_module("lark_agent_bridge.agents").run_tracked_process(*args, **kwargs)


class PerceptionSummaryRunner:
    def __init__(
        self,
        config: BridgeConfig,
        lark_client=None,
        process_watchdog: ProcessWatchdog | None = None,
    ) -> None:
        self.config = config
        self.downloader = LogDownloader(config, lark_client) if lark_client is not None else None
        self.process_watchdog = process_watchdog

    def run_summary(self, request: PerceptionSummaryRequest, *, event: LarkEvent | None = None) -> TaskResult:
        if request.error == "missing_prompt" or not request.prompt.strip():
            return TaskResult(
                success=False,
                message="缺少总结内容：请说明要总结当前感知数据，或补充日志范围。",
                error_code="missing_perception_prompt",
                details={"mode": "perception_summary"},
            )

        context = create_job_context(self.config.data_dir, event=event)
        html_path = context.output_dir / "perception_data_summary.html"
        json_path = context.output_dir / "perception_data_summary.json"
        script_path = self._perception_script()
        if not request.resources:
            return TaskResult(
                success=False,
                message="缺少日志输入：请在消息中提供日志 URL 或飞书附件。",
                error_code="missing_log",
                details={"mode": "perception_summary"},
            )
        if self.config.dry_run:
            return TaskResult(
                success=True,
                message=(
                    "dry-run: 当前感知数据总结命令已规划\n"
                    f"html: {html_path}\n"
                    f"json: {json_path}"
                ),
                job_id=context.job_id,
                job_dir=context.job_dir,
                command=[sys.executable, str(script_path), "<log_path>"],
                details={
                    "mode": "perception_summary",
                    "downloads": [item.value for item in request.resources],
                    "files_to_send": [html_path],
                },
            )

        if self.downloader is None:
            return TaskResult(
                success=False,
                message="当前感知数据总结缺少 downloader 依赖。",
                job_id=context.job_id,
                job_dir=context.job_dir,
                error_code="perception_summary_missing_downloader",
                details={"mode": "perception_summary"},
            )

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
                error_code="download_failed",
                details={"mode": "perception_summary"},
            )

        input_path = downloaded[0].path if len(downloaded) == 1 else context.input_dir
        command = [sys.executable, str(script_path), str(input_path)]
        started = time.monotonic()
        try:
            completed = _run_tracked_process(
                command,
                watchdog=self.process_watchdog,
                name="perception-summary",
                cwd=self.config.workspace_root,
                capture_output=True,
                text=True,
                timeout=self.config.bug_analysis.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return TaskResult(
                success=False,
                message="当前感知数据总结超时",
                job_id=context.job_id,
                job_dir=context.job_dir,
                command=command,
                duration_seconds=time.monotonic() - started,
                error_code="perception_summary_timeout",
                stdout=exc.stdout or "",
                stderr=exc.stderr or "",
                details={"mode": "perception_summary"},
            )

        if completed.returncode != 0:
            return TaskResult(
                success=False,
                message="当前感知数据总结执行失败",
                job_id=context.job_id,
                job_dir=context.job_dir,
                command=command,
                duration_seconds=time.monotonic() - started,
                error_code="perception_summary_failed",
                stdout=completed.stdout,
                stderr=completed.stderr,
                details={"mode": "perception_summary"},
            )

        generated_html = self._extract_report_path(completed.stdout, r"^\[OK\] HTML:\s*(.+)$")
        generated_json = self._extract_report_path(completed.stdout, r"^\[OK\] JSON:\s*(.+)$")
        if generated_html and generated_html.exists():
            shutil.copy2(generated_html, html_path)
        if generated_json and generated_json.exists():
            shutil.copy2(generated_json, json_path)

        if not html_path.exists():
            return TaskResult(
                success=False,
                message="当前感知数据总结未生成 HTML 报告",
                job_id=context.job_id,
                job_dir=context.job_dir,
                command=command,
                duration_seconds=time.monotonic() - started,
                error_code="perception_summary_missing_html",
                stdout=completed.stdout,
                stderr=completed.stderr,
                details={"mode": "perception_summary"},
            )

        return TaskResult(
            success=True,
            message=(
                "当前感知数据总结完成\n"
                f"HTML: {html_path}\n"
                f"JSON: {json_path if json_path.exists() else '未生成'}\n"
                f"job: {context.job_dir}"
            ),
            job_id=context.job_id,
            job_dir=context.job_dir,
            html_report=html_path,
            json_report=json_path if json_path.exists() else None,
            command=command,
            duration_seconds=time.monotonic() - started,
            stdout=completed.stdout,
            stderr=completed.stderr,
            details={"mode": "perception_summary", "files_to_send": [html_path]},
        )

    def _perception_script(self) -> Path:
        return self.config.workspace_root / ".ai/skills/perception-data-summary/scripts/analyze_perception_data_summary.py"

    def _extract_report_path(self, output: str, pattern: str) -> Path | None:
        match = re.search(pattern, output, re.MULTILINE)
        if not match:
            return None
        return Path(match.group(1).strip())
