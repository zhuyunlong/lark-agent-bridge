"""Claude skill runner."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
import shlex
import subprocess
import sys
import time

from ..evidence_logs import preserve_evidence_log_bundle
from ..health import ProcessWatchdog
from ..log import get_logger
from ..models import BridgeConfig, ClaudeSkillRequest, LarkEvent, TaskResult, create_job_context

logger = get_logger("agents")


def _run_tracked_process(*args, **kwargs):
    return importlib.import_module("lark_agent_bridge.agents").run_tracked_process(*args, **kwargs)


class ClaudeSkillRunner:
    def __init__(self, config: BridgeConfig, process_watchdog: ProcessWatchdog | None = None) -> None:
        self.config = config
        self.process_watchdog = process_watchdog

    def run_skill_analysis(self, request: ClaudeSkillRequest, *, event: LarkEvent | None = None) -> TaskResult:
        options = self.config.claude_agent
        if not options.enabled:
            return TaskResult(
                success=True,
                message="Claude Code skill agent is disabled",
                skipped=True,
                details={"mode": "claude_skill"},
            )
        if request.error == "missing_prompt" or not request.prompt.strip():
            return TaskResult(
                success=False,
                message="缺少分析内容：请写清楚要分析的问题。",
                error_code="missing_skill_prompt",
                details={"mode": "claude_skill"},
            )
        if len(request.prompt) > options.max_prompt_chars:
            return TaskResult(
                success=False,
                message=f"Claude Code 分析内容过长，请压缩到 {options.max_prompt_chars} 字以内。",
                error_code="claude_prompt_too_long",
                details={"mode": "claude_skill"},
            )

        context = create_job_context(self.config.data_dir, event=event)
        bridge_session_id = (event.root_id or event.message_id or event.event_id).strip() if event is not None else ""
        artifact_path = context.output_dir / "claude_skill_result.md"
        prompt = self._build_prompt(request)
        command = self.build_command()
        self._write_request_file(context.job_dir / "claude_skill_request.json", request)
        if self.config.dry_run:
            return TaskResult(
                success=True,
                message=(
                    "dry-run: Claude Code skill 分析命令已规划\n"
                    f"结果文件: {artifact_path}"
                ),
                skipped=False,
                job_id=context.job_id,
                job_dir=context.job_dir,
                command=command,
                details={"mode": "claude_skill"},
            )

        started = time.monotonic()
        try:
            completed = _run_tracked_process(
                command,
                watchdog=self.process_watchdog,
                name="claude-skill-agent",
                cwd=self._working_dir(),
                capture_output=True,
                text=True,
                input=prompt,
                timeout=options.timeout_seconds,
                check=False,
                session_id=bridge_session_id,
            )
        except subprocess.TimeoutExpired as exc:
            self._write_process_logs(context.logs_dir, stdout=exc.stdout or "", stderr=exc.stderr or "")
            return TaskResult(
                success=False,
                message="Claude Code skill 分析超时",
                job_id=context.job_id,
                job_dir=context.job_dir,
                command=command,
                duration_seconds=time.monotonic() - started,
                error_code="claude_timeout",
                stdout=exc.stdout or "",
                stderr=exc.stderr or "",
                details={"mode": "claude_skill"},
            )
        except OSError as exc:
            self._write_process_logs(context.logs_dir, stderr=str(exc))
            return TaskResult(
                success=False,
                message=f"Claude Code 启动失败: {exc}",
                job_id=context.job_id,
                job_dir=context.job_dir,
                command=command,
                duration_seconds=time.monotonic() - started,
                error_code="claude_failed_to_start",
                stderr=str(exc),
                details={"mode": "claude_skill"},
            )

        if completed.returncode != 0:
            self._write_process_logs(context.logs_dir, stdout=completed.stdout, stderr=completed.stderr)
            return TaskResult(
                success=False,
                message="Claude Code skill 分析失败",
                job_id=context.job_id,
                job_dir=context.job_dir,
                command=command,
                duration_seconds=time.monotonic() - started,
                error_code="claude_failed",
                stdout=completed.stdout,
                stderr=completed.stderr,
                details={"mode": "claude_skill"},
            )

        artifact_path.write_text(completed.stdout, encoding="utf-8")
        details = {"mode": "claude_skill"}
        if options.upload_result_file:
            details["files_to_send"] = [artifact_path]
        return TaskResult(
            success=True,
            message=self._summary_message(artifact_path, completed.stdout),
            job_id=context.job_id,
            job_dir=context.job_dir,
            command=command,
            duration_seconds=time.monotonic() - started,
            stdout=completed.stdout,
            stderr=completed.stderr,
            details=details,
        )

    def build_command(self, prompt: str = "") -> list[str]:
        options = self.config.claude_agent
        command = [
            options.command,
            "--print",
            "--output-format",
            "text",
            "--no-session-persistence",
            "--permission-mode",
            options.permission_mode,
            "--append-system-prompt",
            options.system_prompt,
        ]
        if options.model:
            command.extend(["--model", options.model])
        if options.agent:
            command.extend(["--agent", options.agent])
        if options.allowed_tools:
            command.extend(["--allowedTools", ",".join(options.allowed_tools)])
        else:
            command.extend(["--tools", ""])
        for directory in self._add_dirs():
            command.extend(["--add-dir", str(directory)])
        return command

    def _build_prompt(self, request: ClaudeSkillRequest) -> str:
        return (
            "请对下面这段飞书消息做一次 Claude Code skill 分析。\n"
            "要求：\n"
            "1. 只读分析，不修改文件。\n"
            "2. 需要用到代码或文档时，只读取必要上下文。\n"
            "3. 输出中文 Markdown，结论先行。\n\n"
            f"飞书消息原文：\n{request.raw_text or request.prompt}\n\n"
            f"需要分析的问题：\n{request.prompt}\n"
        )

    def _summary_message(self, artifact_path: Path, output: str) -> str:
        excerpt = output.strip()
        if len(excerpt) > 3000:
            excerpt = excerpt[:3000].rstrip() + "\n...(结果较长，完整内容见附件)"
        return f"Claude Code skill 分析完成\n结果文件: {artifact_path}\n\n{excerpt}"

    def _working_dir(self) -> Path:
        return self.config.claude_agent.working_dir or self.config.workspace_root

    def _add_dirs(self) -> list[Path]:
        options = self.config.claude_agent
        return options.add_dirs or [self.config.workspace_root]

    def _write_request_file(self, path: Path, request: ClaudeSkillRequest) -> None:
        payload = {
            "prompt": request.prompt,
            "raw_text": request.raw_text,
            "triggered": request.triggered,
            "error": request.error,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _write_process_logs(self, logs_dir: Path, *, stdout: str = "", stderr: str = "") -> None:
        logs_dir.mkdir(parents=True, exist_ok=True)
        (logs_dir / "claude_skill.stdout.log").write_text(stdout or "", encoding="utf-8")
        (logs_dir / "claude_skill.stderr.log").write_text(stderr or "", encoding="utf-8")
