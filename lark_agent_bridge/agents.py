"""Local agent integrations for Claude Code, Codex, and omlx chat."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import zipfile
import time
import urllib.error
import urllib.request

from .downloader import DownloadError, LogDownloader
from .models import BridgeConfig, BugRequest, ClaudeSkillRequest, LarkEvent, PerceptionSummaryRequest, TaskResult, create_job_context
from .parser import parse_signal_request


class OmlxChatClient:
    def __init__(self, config: BridgeConfig) -> None:
        self.config = config

    def reply(self, prompt: str) -> TaskResult:
        options = self.config.omlx_chat
        if not options.enabled:
            return TaskResult(
                success=True,
                message="omlx chat is disabled",
                skipped=True,
                details={"mode": "omlx_chat"},
            )
        if len(prompt) > options.max_prompt_chars:
            return TaskResult(
                success=False,
                message=f"普通聊天内容过长，请压缩到 {options.max_prompt_chars} 字以内。",
                error_code="omlx_prompt_too_long",
                details={"mode": "omlx_chat"},
            )

        url = options.base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": options.model,
            "messages": [
                {"role": "system", "content": options.system_prompt},
                {"role": "user", "content": prompt},
            ],
            "temperature": options.temperature,
            "max_tokens": options.max_tokens,
            "stream": False,
        }
        if self.config.dry_run:
            return TaskResult(
                success=True,
                message="dry-run: omlx chat request planned",
                details={"mode": "omlx_chat", "url": url, "model": options.model},
            )

        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {options.api_key}",
            },
            method="POST",
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=options.timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            return TaskResult(
                success=False,
                message=f"本地 omlx 模型请求失败: HTTP {exc.code}",
                duration_seconds=time.monotonic() - started,
                error_code="omlx_http_error",
                stderr=body[:1000],
                details={"mode": "omlx_chat", "url": url, "model": options.model},
            )
        except urllib.error.URLError as exc:
            return TaskResult(
                success=False,
                message=f"本地 omlx 模型不可用: {exc.reason}",
                duration_seconds=time.monotonic() - started,
                error_code="omlx_unavailable",
                details={"mode": "omlx_chat", "url": url, "model": options.model},
            )
        except TimeoutError as exc:
            return TaskResult(
                success=False,
                message="本地 omlx 模型请求超时",
                duration_seconds=time.monotonic() - started,
                error_code="omlx_timeout",
                stderr=str(exc),
                details={"mode": "omlx_chat", "url": url, "model": options.model},
            )

        try:
            parsed = json.loads(body)
            answer = _extract_chat_answer(parsed)
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            return TaskResult(
                success=False,
                message="本地 omlx 模型返回格式无法解析",
                duration_seconds=time.monotonic() - started,
                error_code="omlx_bad_response",
                stdout=body[:1000],
                stderr=str(exc),
                details={"mode": "omlx_chat", "url": url, "model": options.model},
            )

        return TaskResult(
            success=True,
            message=answer.strip(),
            duration_seconds=time.monotonic() - started,
            stdout=body,
            details={"mode": "omlx_chat", "url": url, "model": options.model},
        )


class ClaudeSkillRunner:
    def __init__(self, config: BridgeConfig) -> None:
        self.config = config

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
                message="缺少分析内容：请在 /skill 后面写清楚要分析的问题。",
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
        artifact_path = context.output_dir / "claude_skill_result.md"
        prompt = self._build_prompt(request)
        command = self.build_command(prompt)
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
            completed = subprocess.run(
                command,
                cwd=self._working_dir(),
                capture_output=True,
                text=True,
                timeout=options.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
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

    def build_command(self, prompt: str) -> list[str]:
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
        command.append(prompt)
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


def _extract_chat_answer(payload: dict[str, object]) -> str:
    choices = payload["choices"]
    if not isinstance(choices, list) or not choices:
        raise KeyError("choices")
    first = choices[0]
    if not isinstance(first, dict):
        raise TypeError("choice must be an object")
    message = first.get("message")
    if isinstance(message, dict) and isinstance(message.get("content"), str):
        return message["content"]
    if isinstance(first.get("text"), str):
        return first["text"]
    raise KeyError("choices[0].message.content")


class BugAnalysisRunner:
    def __init__(self, config: BridgeConfig) -> None:
        self.config = config

    def run_bug_analysis(self, request: BugRequest, *, event: LarkEvent | None = None) -> TaskResult:
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

        prompt_text = request.prompt.strip() or options.default_prompt
        if len(prompt_text) > options.max_prompt_chars:
            return TaskResult(
                success=False,
                message=f"Bug 分析描述过长，请压缩到 {options.max_prompt_chars} 字以内。",
                error_code="bug_prompt_too_long",
                details={"mode": "bug_analysis"},
            )

        context = create_job_context(self.config.data_dir, event=event)
        bug_dir = context.input_dir / f"bug_{self._bug_id(request.bug_url)}"
        metadata_path = context.output_dir / "bug_metadata.md"
        plans = self.classify_requests(prompt_text=prompt_text, title="", description="")
        plan = plans[0]
        html_path = context.output_dir / self._report_name(plan.kind, "html")
        json_path = context.output_dir / self._report_name(plan.kind, "json")
        analysis_dir = context.output_dir / f"{plan.kind}_analysis"
        command = self.build_command(
            plan=plan,
            input_path=bug_dir,
            html_path=html_path,
            json_path=json_path,
            analysis_dir=analysis_dir,
        )

        if self.config.dry_run:
            planned_reports = "\n".join(
                f"- {context.output_dir / self._report_name(item.kind, 'html')}"
                for item in plans
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
                    "signal_code": plan.signal_code,
                },
            )

        started = time.monotonic()
        try:
            env_status = self._run_json_command([str(self._bug_fetcher_script()), "check-env"], timeout=60)
            if not env_status.get("meegle_installed", False):
                return self._failure(
                    context=context,
                    command=command,
                    started=started,
                    message="Bug 分析前置条件缺失：本机未安装 meegle CLI。",
                    error_code="bug_analysis_missing_meegle",
                )
            if not env_status.get("auth_ok", False):
                return self._failure(
                    context=context,
                    command=command,
                    started=started,
                    message="Bug 分析前置条件缺失：meegle 未登录，请先在本机完成 `meegle auth login`。",
                    error_code="bug_analysis_meegle_not_auth",
                )

            resolved = self._run_json_command(
                [str(self._bug_fetcher_script()), "resolve-url", request.bug_url],
                timeout=60,
            )
            project_key = str(resolved["project_key"])
            work_item_id = str(resolved["work_item_id"])
            fetched = self._run_json_command(
                [str(self._bug_fetcher_script()), "fetch-data", project_key, work_item_id],
                timeout=120,
            )
            full_item = self._run_json_command(
                ["meegle", "workitem", "get", "--project-key", project_key, "--work-item-id", work_item_id, "--format", "json"],
                timeout=120,
            )
            option_map = self._load_option_map(project_key)
            download = self._run_json_command(
                [str(self._bug_fetcher_script()), "download", project_key, work_item_id, str(bug_dir)],
                timeout=options.timeout_seconds,
            )

            title = str(fetched.get("title", ""))
            description = self._bug_description(fetched)
            plans = self.classify_requests(prompt_text=prompt_text, title=title, description=description)
            plan = plans[0]
            html_path = context.output_dir / self._report_name(plan.kind, "html")
            json_path = context.output_dir / self._report_name(plan.kind, "json")
            analysis_dir = context.output_dir / f"{plan.kind}_analysis"

            selected_input = self._select_log_input(bug_dir, fetched)
            if any(item.kind in {"startup", "stuck", "crash", "perception"} for item in plans) and selected_input is None:
                return self._failure(
                    context=context,
                    command=command,
                    started=started,
                    message="Bug 分析失败：当前请求包含日志分析，但未找到可用日志附件。",
                    error_code="bug_analysis_missing_log_attachment",
                )
            if any(item.kind == "signal" and not item.signal_code for item in plans):
                return self._failure(
                    context=context,
                    command=command,
                    started=started,
                    message="Bug 分析失败：识别到信号链路问题，但消息和 Bug 描述里没有明确的 SignalCode/枚举名。",
                    error_code="bug_analysis_missing_signal_code",
                )

            prepared_input = self._prepare_log_input(selected_input) if selected_input else None
            fault_time, fault_time_note = self._extract_fault_time(title, description)
            html_paths: list[Path] = []
            report_jsons: dict[str, Path | None] = {}
            for current_plan in plans:
                current_html = context.output_dir / self._report_name(current_plan.kind, "html")
                current_json = context.output_dir / self._report_name(current_plan.kind, "json")
                current_analysis_dir = context.output_dir / f"{current_plan.kind}_analysis"
                input_for_plan = prepared_input
                if current_plan.kind == "startup" and prepared_input is not None:
                    input_for_plan = self._select_startup_input(prepared_input, fault_time)
                current_command = self.build_command(
                    plan=current_plan,
                    input_path=input_for_plan or bug_dir,
                    html_path=current_html,
                    json_path=current_json,
                    analysis_dir=current_analysis_dir,
                )
                completed = self._run_analysis(
                    plan=current_plan,
                    input_path=input_for_plan,
                    html_path=current_html,
                    json_path=current_json,
                    analysis_dir=current_analysis_dir,
                    timeout=options.timeout_seconds,
                )
                if completed.returncode != 0:
                    return self._failure(
                        context=context,
                        command=current_command,
                        started=started,
                        message=f"Bug 分析失败：{self._analysis_label(current_plan.kind)}脚本执行失败。",
                        error_code=f"bug_analysis_{current_plan.kind}_failed",
                        stdout=completed.stdout,
                        stderr=completed.stderr,
                    )
                if not current_html.exists():
                    return self._failure(
                        context=context,
                        command=current_command,
                        started=started,
                        message=f"Bug 分析失败：未生成 {self._analysis_label(current_plan.kind)} HTML 报告。",
                        error_code="bug_analysis_missing_html",
                        stdout=completed.stdout,
                        stderr=completed.stderr,
                    )
                html_paths.append(current_html)
                report_jsons[current_plan.kind] = current_json if current_json.exists() else None
                if current_plan is plan:
                    command = current_command
                    html_path = current_html
                    json_path = current_json
                    analysis_dir = current_analysis_dir

            metadata_text, summary = self._build_bug_outputs(
                plans=plans,
                work_item_id=work_item_id,
                fetched=fetched,
                full_item=full_item,
                option_map=option_map,
                prompt_text=prompt_text,
                selected_input=selected_input,
                report_jsons=report_jsons,
                download=download,
                html_paths=html_paths,
            )
            metadata_path.write_text(metadata_text, encoding="utf-8")
        except subprocess.TimeoutExpired as exc:
            return self._failure(
                context=context,
                command=command,
                started=started,
                message="Bug 分析超时",
                error_code="bug_analysis_timeout",
                stdout=exc.stdout or "",
                stderr=exc.stderr or "",
            )
        except OSError as exc:
            return self._failure(
                context=context,
                command=command,
                started=started,
                message=f"Bug 分析启动失败: {exc}",
                error_code="bug_analysis_failed_to_start",
                stderr=str(exc),
            )
        except (KeyError, ValueError, RuntimeError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
            return self._failure(
                context=context,
                command=command,
                started=started,
                message=f"Bug 分析失败: {exc}",
                error_code="bug_analysis_failed",
            )

        details = {
            "mode": "bug_analysis",
            "analysis_kind": plan.kind,
            "analysis_kinds": [item.kind for item in plans],
            "signal_code": plan.signal_code,
            "selected_log_input": str(selected_input) if selected_input else "",
            "prepared_log_input": str(prepared_input) if prepared_input else "",
            "bug_dir": str(bug_dir),
        }
        if options.upload_result_files:
            details["files_to_send"] = [metadata_path, *html_paths]
        if json_path.exists():
            details["json_report"] = str(json_path)
        return TaskResult(
            success=True,
            message=summary,
            job_id=context.job_id,
            job_dir=context.job_dir,
            command=command,
            duration_seconds=time.monotonic() - started,
            details=details,
        )

    def run_direct_analysis(self, request: DirectAnalysisRequest, *, event: LarkEvent | None = None) -> TaskResult:
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
        metadata_path = context.output_dir / "direct_analysis_metadata.md"
        started = time.monotonic()
        downloader = getattr(self, "_direct_downloader", None)
        if downloader is None:
            downloader = LogDownloader(self.config, getattr(self, "_lark_client", None))
            self._direct_downloader = downloader
        try:
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
        prepared_input = self._prepare_log_input(selected_input) if selected_input.exists() else selected_input
        plans = self.classify_requests(prompt_text=request.prompt, title="", description="")
        html_paths: list[Path] = []
        report_jsons: dict[str, Path | None] = {}
        command: list[str] | None = None
        fault_time, _ = self._extract_fault_time("", request.prompt)

        for current_plan in plans:
            current_html = context.output_dir / self._report_name(current_plan.kind, "html")
            current_json = context.output_dir / self._report_name(current_plan.kind, "json")
            current_analysis_dir = context.output_dir / f"{current_plan.kind}_analysis"
            input_for_plan = prepared_input
            if current_plan.kind == "startup" and prepared_input is not None:
                input_for_plan = self._select_startup_input(prepared_input, fault_time)
            command = self.build_command(
                plan=current_plan,
                input_path=input_for_plan,
                html_path=current_html,
                json_path=current_json,
                analysis_dir=current_analysis_dir,
            )
            try:
                completed = self._run_analysis(
                    plan=current_plan,
                    input_path=input_for_plan,
                    html_path=current_html,
                    json_path=current_json,
                    analysis_dir=current_analysis_dir,
                    timeout=self.config.bug_analysis.timeout_seconds,
                )
            except subprocess.TimeoutExpired as exc:
                return TaskResult(
                    success=False,
                    message=f"直传文件分析超时：{self._analysis_label(current_plan.kind)}",
                    job_id=context.job_id,
                    job_dir=context.job_dir,
                    command=command,
                    duration_seconds=time.monotonic() - started,
                    error_code=f"direct_analysis_{current_plan.kind}_timeout",
                    stdout=exc.stdout or "",
                    stderr=exc.stderr or "",
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
        return TaskResult(
            success=True,
            message=summary,
            job_id=context.job_id,
            job_dir=context.job_dir,
            command=command,
            duration_seconds=time.monotonic() - started,
            details={
                "mode": "direct_analysis",
                "analysis_kinds": [item.kind for item in plans],
                "files_to_send": [metadata_path, *html_paths],
            },
        )

    def classify_requests(self, *, prompt_text: str, title: str, description: str) -> list["BugAnalysisPlan"]:
        combined = "\n".join(part for part in [prompt_text, title, description] if part).strip()
        signal_request = parse_signal_request(
            combined,
            signal_aliases=self.config.signal_aliases,
            command_prefixes=self.config.command_prefixes,
        )
        lowered = combined.casefold()
        if any(term in lowered for term in PERCEPTION_ROUTE_TERMS):
            return [BugAnalysisPlan(kind="perception")]
        explicit_signal_enum = "signal_" in lowered
        explicit_signal_terms = any(term in lowered for term in SIGNAL_ROUTE_TERMS)
        if explicit_signal_enum or (explicit_signal_terms and signal_request.signal):
            return [BugAnalysisPlan(kind="signal", signal_code=signal_request.signal)]
        if any(term in lowered for term in CRASH_ROUTE_TERMS):
            return [BugAnalysisPlan(kind="crash")]
        plans: list[BugAnalysisPlan] = []
        if any(term in lowered for term in STARTUP_ROUTE_TERMS):
            plans.append(BugAnalysisPlan(kind="startup"))
        if any(term in lowered for term in STUCK_ROUTE_TERMS):
            plans.append(BugAnalysisPlan(kind="stuck"))
        if plans:
            return plans
        return [BugAnalysisPlan(kind="startup")]

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
    ) -> list[str]:
        if plan.kind == "startup":
            return [
                sys.executable,
                str(self._startup_script()),
                str(input_path),
                "--output-dir",
                str(analysis_dir),
            ]
        if plan.kind == "stuck":
            return [
                sys.executable,
                str(self._stuck_script()),
                str(input_path),
            ]
        if plan.kind == "perception":
            return [
                sys.executable,
                str(self._perception_script()),
                str(input_path),
            ]
        if plan.kind == "crash":
            return [
                sys.executable,
                str(self._stuck_script()),
                str(input_path),
            ]
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

    def _bug_fetcher_script(self) -> Path:
        return self.config.workspace_root / ".ai/skills/feishu-bug-fetcher/scripts/bug-fetcher.sh"

    def _startup_script(self) -> Path:
        return self.config.workspace_root / ".ai/skills/unity-startup-lifecycle-check/scripts/analyze_unity_startup.py"

    def _stuck_script(self) -> Path:
        return self.config.workspace_root / ".ai/skills/3d-stuck-investigate/scripts/analyze_3d_stuck.py"

    def _signal_script(self) -> Path:
        return self.config.workspace_root / ".ai/skills/signal-chain-analyzer/scripts/analyze_signal_chain.py"

    def _perception_script(self) -> Path:
        return self.config.workspace_root / ".ai/skills/perception-data-summary/scripts/analyze_perception_data_summary.py"

    def _bug_id(self, url: str) -> str:
        match = re.search(r"/buglo/detail/(\d+)", url)
        return match.group(1) if match else "unknown_bug"

    def _report_name(self, kind: str, suffix: str) -> str:
        return {
            "startup": f"bug_3d_startup_report.{suffix}",
            "stuck": f"bug_3d_stuck_report.{suffix}",
            "crash": f"bug_crash_report.{suffix}",
            "perception": f"bug_perception_data_summary.{suffix}",
            "signal": f"bug_signal_chain_report.{suffix}",
        }[kind]

    def _analysis_label(self, kind: str) -> str:
        return {
            "startup": "3D启动时序分析",
            "stuck": "3D卡顿分析",
            "crash": "Crash/闪退分析",
            "perception": "当前感知数据总结",
            "signal": "信号链路分析",
        }[kind]

    def _run_json_command(self, command: list[str], *, timeout: int) -> dict[str, object]:
        completed = subprocess.run(
            command,
            cwd=self._working_dir(),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if completed.returncode != 0:
            message = completed.stderr.strip() or completed.stdout.strip() or "command failed"
            raise RuntimeError(message)
        payload = json.loads(completed.stdout)
        if isinstance(payload, dict) and payload.get("ok") is False:
            raise RuntimeError(str(payload.get("error", "command returned ok=false")))
        return payload

    def _load_option_map(self, project_key: str) -> dict[str, str]:
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

    def _select_log_input(self, bug_dir: Path, fetched: dict[str, object]) -> Path | None:
        attachments_dir = bug_dir / "attachments"
        logs_dir = bug_dir / "logs"
        attachments: list[Path] = []
        for item in fetched.get("attachments", []):
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if isinstance(name, str):
                candidate = attachments_dir / name
                if candidate.exists():
                    attachments.append(candidate)

        priority_suffixes = (".xp.zip.001", ".xp", ".zip", ".alog", ".xlog", ".log", ".txt")
        for suffix in priority_suffixes:
            for candidate in attachments:
                if candidate.name.lower().endswith(suffix):
                    return candidate
        if logs_dir.exists() and any(logs_dir.rglob("*")):
            return logs_dir
        return None

    def _prepare_log_input(self, selected_input: Path) -> Path:
        lower_name = selected_input.name.lower()
        if lower_name.endswith(".xp"):
            return self._expand_xp_file(selected_input)
        if lower_name.endswith(".zip") and not lower_name.endswith(".xp.zip.001"):
            extract_dir = selected_input.with_suffix("")
            if not extract_dir.exists():
                with zipfile.ZipFile(selected_input) as zf:
                    zf.extractall(extract_dir)
                self._normalize_tree_permissions(extract_dir)
            return extract_dir
        return selected_input

    def _expand_xp_file(self, xp_path: Path) -> Path:
        jar_path = self.config.workspace_root / ".ai/skills/log-decoder/tools/decryptFile.jar"
        completed = subprocess.run(
            ["java", "-jar", str(jar_path), str(xp_path)],
            cwd=self._working_dir(),
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "decrypt xp failed")
        inner_zip = xp_path.with_suffix(".zip")
        if not inner_zip.exists():
            raise RuntimeError(f"xp 解密后未产出 zip: {inner_zip}")
        extract_dir = xp_path.with_suffix("")
        if not extract_dir.exists():
            with zipfile.ZipFile(inner_zip) as zf:
                zf.extractall(extract_dir)
            self._normalize_tree_permissions(extract_dir)
        return extract_dir / "Log" if (extract_dir / "Log").exists() else extract_dir

    def _normalize_tree_permissions(self, root: Path) -> None:
        try:
            root.chmod(root.stat().st_mode | stat.S_IRWXU)
        except OSError:
            return
        for path in root.rglob("*"):
            try:
                mode = path.stat().st_mode
                if path.is_dir():
                    path.chmod(mode | stat.S_IRWXU)
                else:
                    path.chmod(mode | stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                continue

    def _select_startup_input(self, input_path: Path, fault_time: str) -> Path:
        if input_path.is_file():
            return input_path
        fault_dt = self._parse_fault_datetime(fault_time)
        if fault_dt is None:
            return input_path
        fault_epoch = time.mktime(fault_dt)
        candidates = [
            path
            for path in input_path.rglob("main_*")
            if path.is_file()
            and "com.xiaopeng.montecarlo" in str(path)
            and path.suffix.lower() in {".alog", ".xlog", ".log", ".txt"}
        ]
        ranked: list[tuple[float, int, Path]] = []
        for candidate in candidates:
            file_dt = self._parse_log_file_datetime(candidate.name)
            if file_dt is None:
                continue
            score = abs(time.mktime(file_dt) - fault_epoch)
            ranked.append((score, self._log_file_priority(candidate), candidate))
        if not ranked:
            return input_path
        ranked.sort(key=lambda item: (item[0], item[1], str(item[2])))
        return ranked[0][2]

    def _parse_fault_datetime(self, fault_time: str) -> "time.struct_time | None":
        short_match = re.search(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)", fault_time)
        if short_match and "20" not in fault_time:
            now = time.localtime()
            try:
                return time.strptime(
                    f"{now.tm_year:04d}-{now.tm_mon:02d}-{now.tm_mday:02d} {int(short_match.group(1)):02d}:{int(short_match.group(2)):02d}",
                    "%Y-%m-%d %H:%M",
                )
            except ValueError:
                return None
        match = re.search(
            r"(20\d{2})[-_/年](\d{1,2})[-_/月](\d{1,2})[日_\s-]*(\d{1,2}):(\d{2})",
            fault_time,
        )
        if not match:
            return None
        try:
            return time.strptime(
                f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d} "
                f"{int(match.group(4)):02d}:{int(match.group(5)):02d}",
                "%Y-%m-%d %H:%M",
            )
        except ValueError:
            return None

    def _parse_log_file_datetime(self, name: str) -> "time.struct_time | None":
        match = re.search(r"main_(20\d{2}-\d{2}-\d{2})_(\d{2})-(\d{2})", name)
        if not match:
            return None
        try:
            return time.strptime(
                f"{match.group(1)} {match.group(2)}:{match.group(3)}",
                "%Y-%m-%d %H:%M",
            )
        except ValueError:
            return None

    def _log_file_priority(self, path: Path) -> int:
        lower = path.name.lower()
        if lower.endswith(".alog"):
            return 0
        if lower.endswith(".xlog"):
            return 1
        if lower.endswith(".log"):
            return 2
        return 3

    def _run_analysis(
        self,
        *,
        plan: "BugAnalysisPlan",
        input_path: Path | None,
        html_path: Path,
        json_path: Path,
        analysis_dir: Path,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        command = self.build_command(
            plan=plan,
            input_path=input_path or self.config.workspace_root,
            html_path=html_path,
            json_path=json_path,
            analysis_dir=analysis_dir,
        )
        completed = subprocess.run(
            command,
            cwd=self._working_dir(),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if completed.returncode != 0 and plan.kind == "startup":
            completed = subprocess.run(
                command,
                cwd=self._working_dir(),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        if completed.returncode != 0:
            return completed

        if plan.kind == "startup":
            generated_html = analysis_dir / "unity_startup_lifecycle_report.html"
            generated_json = analysis_dir / "unity_startup_lifecycle_report.json"
            if generated_html.exists():
                shutil.copy2(generated_html, html_path)
            if generated_json.exists():
                shutil.copy2(generated_json, json_path)
            return completed

        if plan.kind in {"stuck", "crash", "perception"}:
            generated_html = self._extract_report_path(completed.stdout, r"^\[OK\] 报告:\s*(.+)$")
            generated_json = self._extract_report_path(completed.stdout, r"^\[OK\] JSON:\s*(.+)$")
            if plan.kind == "perception":
                generated_html = self._extract_report_path(completed.stdout, r"^\[OK\] HTML:\s*(.+)$")
            if generated_html and generated_html.exists():
                shutil.copy2(generated_html, html_path)
            if generated_json and generated_json.exists():
                shutil.copy2(generated_json, json_path)
        return completed

    def _extract_report_path(self, output: str, pattern: str) -> Path | None:
        match = re.search(pattern, output, re.MULTILINE)
        if not match:
            return None
        return Path(match.group(1).strip())

    def _bug_description(self, fetched: dict[str, object]) -> str:
        fields = fetched.get("fields", {})
        if not isinstance(fields, dict):
            return ""
        value = fields.get("field_204366", "")
        return value if isinstance(value, str) else ""

    def _build_bug_outputs(
        self,
        *,
        plans: list["BugAnalysisPlan"],
        work_item_id: str,
        fetched: dict[str, object],
        full_item: dict[str, object],
        option_map: dict[str, str],
        prompt_text: str,
        selected_input: Path | None,
        report_jsons: dict[str, Path | None],
        download: dict[str, object],
        html_paths: list[Path],
    ) -> tuple[str, str]:
        title = str(fetched.get("title", ""))
        description = self._bug_description(fetched)
        status = str(fetched.get("status", ""))
        create_time = str(fetched.get("create_time", ""))
        create_by = str(fetched.get("create_by", ""))
        owner = self._extract_owner(full_item)
        bug_source = self._map_option(option_map, fetched.get("fields", {}), "field_24095d")
        found_version = self._string_field(fetched.get("fields", {}), "field_010122")
        probability = self._map_option(option_map, fetched.get("fields", {}), "field_45dc84")
        fault_time, fault_time_note = self._extract_fault_time(title, description)
        summary_blocks: list[str] = []
        for plan in plans:
            html_path = next((path for path in html_paths if path.name == self._report_name(plan.kind, "html")), None)
            if html_path is None:
                continue
            summary_blocks.append(
                self._build_summary_from_report(
                    plan=plan,
                    report_json=report_jsons.get(plan.kind),
                    prompt_text=prompt_text,
                    fault_time=fault_time,
                    html_path=html_path,
                    selected_input=selected_input,
                )
            )
        summary = "\n\n".join(summary_blocks)
        attachment_lines = self._render_attachment_lines(fetched.get("attachments", []), download)
        analysis_lines = "\n".join(
            f"  - `{self._analysis_label(plan.kind)}` -> `{self._report_name(plan.kind, 'html')}`"
            for plan in plans
        )
        metadata = (
            "# Bug Metadata\n\n"
            f"- Bug ID: `{work_item_id}`\n"
            f"- 标题: `{title}`\n"
            f"- 当前状态: `{status or '未返回 / 未设置'}`\n"
            f"- 创建时间: `{create_time or '未返回 / 未设置'}`\n"
            f"- 创建人: `{create_by or '未返回 / 未设置'}`\n"
            f"- 当前负责人: `{owner}`\n"
            f"- 缺陷来源: `{bug_source}`\n"
            f"- 发现版本: `{found_version}`\n"
            f"- 发生概率: `{probability}`\n"
            f"- 分析类型:\n{analysis_lines}\n"
            f"- 信号代码: `{', '.join(plan.signal_code for plan in plans if plan.signal_code) or '无'}`\n"
            f"- 故障时间: `{fault_time or '未识别'}`\n"
            f"  说明: {fault_time_note}\n"
            f"- 分析请求: `{prompt_text}`\n"
            f"- 选中日志输入: `{selected_input or '无，可静态分析'}`\n"
            f"- 附件:\n{attachment_lines}\n"
            "- 缺陷描述:\n\n```text\n"
            f"{description.strip() or '(无描述)'}\n"
            "```\n\n"
            "- 关键结论:\n\n"
            f"{summary}\n"
        )
        return metadata, summary

    def _extract_owner(self, full_item: dict[str, object]) -> str:
        current_nodes = full_item.get("work_item_current_node", [])
        if isinstance(current_nodes, list) and current_nodes:
            owners = current_nodes[0].get("owners", []) if isinstance(current_nodes[0], dict) else []
            if isinstance(owners, list) and owners:
                owner = owners[0]
                if isinstance(owner, dict):
                    return str(owner.get("name", "未返回 / 未设置"))
        return "未返回 / 未设置"

    def _map_option(self, option_map: dict[str, str], fields: object, key: str) -> str:
        if not isinstance(fields, dict):
            return "未返回 / 未设置"
        raw = fields.get(key, "")
        if isinstance(raw, str) and raw:
            return option_map.get(raw, raw)
        return "未返回 / 未设置"

    def _string_field(self, fields: object, key: str) -> str:
        if not isinstance(fields, dict):
            return "未返回 / 未设置"
        value = fields.get(key, "")
        if isinstance(value, str) and value:
            return value
        return "未返回 / 未设置"

    def _extract_fault_time(self, title: str, description: str) -> tuple[str, str]:
        match = re.search(r"(?:故障|发生|出现|问题|异常)?时间[：:]\s*(.+?)(?:\n|$)", description)
        if match:
            return match.group(1).strip(), "从缺陷描述提取"
        direct_match = re.search(r"(20\d{2}[-_/年]\d{1,2}[-_/月]\d{1,2}[日_\s-]*\d{1,2}:\d{2})", description)
        if direct_match:
            return direct_match.group(1).replace("年", "-").replace("月", "-").replace("日", " "), "从文本中的完整时间戳提取"
        short_match = re.search(r"(?<!\d)(\d{1,2}:\d{2})(?!\d)", description)
        if short_match:
            return short_match.group(1), "从文本中的时分提取"
        title_match = re.search(r"(20\d{2})年_(\d{1,2})月(\d{1,2})日_(\d{1,2}:\d{2})", title)
        if title_match:
            return (
                f"{title_match.group(1)}-{int(title_match.group(2)):02d}-{int(title_match.group(3)):02d} {title_match.group(4)}",
                "缺陷描述中未显式提供故障时间，退回使用标题中的时间戳",
            )
        return "", "缺陷描述和标题中都未识别到明确故障时间"

    def _build_direct_analysis_summary(
        self,
        plans: list["BugAnalysisPlan"],
        prompt_text: str,
        html_paths: list[Path],
    ) -> str:
        lines = ["直传文件分析完成", f"描述: {prompt_text}", "报告:"]
        for plan, html_path in zip(plans, html_paths):
            lines.append(f"- {self._analysis_label(plan.kind)}: {html_path}")
        return "\n".join(lines)

    def _render_attachment_lines(self, attachments: object, download: dict[str, object]) -> str:
        downloaded = set(str(name) for name in download.get("downloaded", []) if isinstance(name, str))
        lines: list[str] = []
        if isinstance(attachments, list):
            for item in attachments:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name", ""))
                size = str(item.get("size", ""))
                status = "已下载" if name in downloaded else "未下载"
                lines.append(f"  - `{name}` (`{size}`) - {status}")
        return "\n".join(lines) if lines else "  - (无附件)"

    def _build_summary_from_report(
        self,
        *,
        plan: "BugAnalysisPlan",
        report_json: Path | None,
        prompt_text: str,
        fault_time: str,
        html_path: Path,
        selected_input: Path | None,
    ) -> str:
        if report_json is None or not report_json.exists():
            log_input = selected_input.name if selected_input else "无日志，静态链路"
            return (
                "Bug 分析完成\n"
                f"类型: {self._analysis_label(plan.kind)}\n"
                f"描述: {prompt_text}\n"
                f"HTML: {html_path}\n"
                f"输入: {log_input}"
            )

        payload = json.loads(report_json.read_text(encoding="utf-8"))
        if plan.kind == "startup":
            verdict = payload.get("verdict", {}) if isinstance(payload, dict) else {}
            message = str(verdict.get("message", ""))
            sessions = payload.get("sessions", []) if isinstance(payload, dict) else []
            target_session = self._select_target_session(sessions, fault_time)
            duration_note = ""
            if target_session is not None:
                message = str(target_session.get("diagnosis", message or "启动时序报告已生成"))
                duration_note = (
                    f"\n故障时间主会话: Session {target_session.get('index', '?')} "
                    f"{target_session.get('start', '')}，状态 {target_session.get('status', '')}"
                )
            mismatch_note = self._time_match_note(fault_time, sessions)
            return (
                "Bug 分析完成\n"
                f"类型: {self._analysis_label(plan.kind)}\n"
                f"结论: {message or '启动时序报告已生成'}\n"
                f"描述: {prompt_text}\n"
                f"HTML: {html_path}\n"
                f"时间窗校验: {mismatch_note}{duration_note}"
            )

        if plan.kind in {"stuck", "crash"}:
            verdict = payload.get("verdict", {}) if isinstance(payload, dict) else {}
            sev = str(verdict.get("verdict_sev", "")).upper()
            msg = str(verdict.get("verdict_msg", ""))
            return (
                "Bug 分析完成\n"
                f"类型: {self._analysis_label(plan.kind)}\n"
                f"结论: [{sev or 'INFO'}] {msg or '已生成卡顿报告'}\n"
                f"描述: {prompt_text}\n"
                f"HTML: {html_path}"
            )

        if plan.kind == "perception":
            summary = payload.get("summary", {}) if isinstance(payload, dict) else {}
            verdict = summary.get("verdict", {}) if isinstance(summary, dict) else {}
            sev = str(verdict.get("sev", "")).upper()
            msg = str(verdict.get("msg", ""))
            return (
                "Bug 分析完成\n"
                f"类型: {self._analysis_label(plan.kind)}\n"
                f"结论: [{sev or 'INFO'}] {msg or '已生成当前感知数据总结'}\n"
                f"描述: {prompt_text}\n"
                f"HTML: {html_path}"
            )

        signal = payload.get("signal", {}) if isinstance(payload, dict) else {}
        signal_code = signal.get("code", plan.signal_code or "")
        summary = str(payload.get("summary", ""))
        log_report = payload.get("log_report", {}) if isinstance(payload, dict) else {}
        scanned_files = log_report.get("scanned_files", 0) if isinstance(log_report, dict) else 0
        return (
            "Bug 分析完成\n"
            f"类型: {self._analysis_label(plan.kind)}\n"
            f"信号: {signal_code}\n"
            f"结论: {summary or '已生成信号链路报告'}\n"
            f"扫描文件: {scanned_files}\n"
            f"HTML: {html_path}"
        )

    def _select_target_session(self, sessions: object, fault_time: str) -> dict[str, object] | None:
        if not isinstance(sessions, list) or not sessions:
            return None
        fault_dt = self._parse_fault_datetime(fault_time)
        if fault_dt is None:
            return None
        target_epoch = time.mktime(fault_dt)
        ranked: list[tuple[float, dict[str, object]]] = []
        for session in sessions:
            if not isinstance(session, dict):
                continue
            start = session.get("start")
            if not isinstance(start, str):
                continue
            try:
                session_epoch = time.mktime(time.strptime(start[:16], "%Y-%m-%dT%H:%M"))
            except ValueError:
                continue
            ranked.append((abs(session_epoch - target_epoch), session))
        if not ranked:
            return None
        ranked.sort(key=lambda item: item[0])
        return ranked[0][1]

    def _time_match_note(self, fault_time: str, sessions: object) -> str:
        if not fault_time:
            return "未识别故障时间，无法校验附件日志是否匹配。"
        if not isinstance(sessions, list) or not sessions:
            return "报告未产出会话，无法校验附件日志是否匹配。"
        fault_hour = fault_time[:13]
        for session in sessions:
            if not isinstance(session, dict):
                continue
            start = str(session.get("start", ""))
            if start.startswith(fault_hour):
                return f"附件日志中命中了故障小时 `{fault_hour}`。"
        starts = [str(session.get("start", "")) for session in sessions if isinstance(session, dict)]
        preview = "、".join(starts[:3]) if starts else "无"
        return f"附件日志未命中故障小时 `{fault_hour}`，实际捕获到的启动会话起点示例：{preview}"

    def _failure(
        self,
        *,
        context,
        command: list[str],
        started: float,
        message: str,
        error_code: str,
        stdout: str = "",
        stderr: str = "",
    ) -> TaskResult:
        return TaskResult(
            success=False,
            message=message,
            job_id=context.job_id,
            job_dir=context.job_dir,
            command=command,
            duration_seconds=time.monotonic() - started,
            error_code=error_code,
            stdout=stdout,
            stderr=stderr,
            details={"mode": "bug_analysis"},
        )


@dataclass(slots=True)
class BugAnalysisPlan:
    kind: str
    signal_code: str | None = None


STARTUP_ROUTE_TERMS = (
    "启动",
    "时序",
    "首帧",
    "unityready",
    "readyprepare",
    "displaychanged",
    "startrender",
    "surfacecreated",
    "surfacechanged",
    "createunityplayeronmainthread",
    "onunityready",
    "unitymainfirstframereadyrendermsg",
)

STUCK_ROUTE_TERMS = (
    "卡顿",
    "卡住",
    "卡死",
    "掉帧",
    "黑屏",
    "不刷新",
    "无响应",
    "anr",
    "3d卡",
    "unity卡",
    "montecarlo卡",
)

SIGNAL_ROUTE_TERMS = (
    "信号",
    "没到unity",
    "没到 unity",
    "有没有到unity",
    "有没有到 unity",
    "数据链",
    "链路",
    "x3dcb",
    "signaldispatcher",
    "vhalhelper",
)

PERCEPTION_ROUTE_TERMS = (
    "当前感知数据",
    "感知数据总结",
    "感知数据",
    "感知统计",
    "无感知",
    "sr无感知",
    "vhalhelper",
    "mapdatahandler",
    "x3dcb",
    "xdatanativeproxy",
    "unity收到的数据统计",
)

CRASH_ROUTE_TERMS = (
    "闪退",
    "crash",
    "tombstone",
    "fatal exception",
    "异常退出",
    "崩溃",
    "sigsegv",
    "abort",
    "native crash",
)


class PerceptionSummaryRunner:
    def __init__(self, config: BridgeConfig, lark_client=None) -> None:
        self.config = config
        self.downloader = LogDownloader(config, lark_client) if lark_client is not None else None

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
            completed = subprocess.run(
                command,
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
