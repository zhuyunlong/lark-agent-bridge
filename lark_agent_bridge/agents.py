"""Local agent integrations for Claude Code, Codex, and omlx chat."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
import zipfile
import time
import urllib.error
import urllib.request
from typing import Callable

from .downloader import DownloadError, LogDownloader
from .health import ProcessWatchdog, run_tracked_process
from .models import (
    BridgeConfig,
    BugRequest,
    ClaudeSkillRequest,
    IntentDecision,
    LarkEvent,
    PerceptionSummaryRequest,
    TaskResult,
    create_job_context,
)
from .parser import parse_signal_request
from .reporting import (
    ReportComposition,
    ReportSection,
    ReportVerdict,
    build_structured_summary_sections,
    combined_bug_html,
    composition_to_renderer_payload,
    plan_signal_report,
    plan_startup_stuck_report,
)
from .signal_resolver import SignalResolver
from .skill_registry import AUX_BUG_SKILLS, PRIMARY_BUG_SKILL_MAP, extract_skill_frontmatter


_NON_LOG_XP_MAGIC_HEADERS = (
    b"\x89PNG\r\n\x1a\n",
    b"\xff\xd8\xff",
    b"GIF87a",
    b"GIF89a",
    b"%PDF-",
)
_BUG_ATTACHMENT_DOWNLOAD_SUFFIXES = (
    ".xp.zip.001",
    ".tar.gz",
    ".tar.bz2",
    ".tar.xz",
    ".zip",
    ".7z",
    ".rar",
    ".tgz",
    ".tar",
    ".gz",
    ".bz2",
    ".xz",
    ".xp",
    ".alog",
    ".xlog",
    ".log",
    ".txt",
)
_TOKEN_USAGE_KEYS = {
    "input_tokens": ("input_tokens", "inputTokens", "prompt_tokens", "promptTokens"),
    "output_tokens": ("output_tokens", "outputTokens", "completion_tokens", "completionTokens"),
    "total_tokens": ("total_tokens", "totalTokens"),
}
_RUNTIME_HTML_MARKER_START = "<!-- LARK_AGENT_RUNTIME_START -->"
_RUNTIME_HTML_MARKER_END = "<!-- LARK_AGENT_RUNTIME_END -->"


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
        return self._chat(
            mode="omlx_chat",
            system_prompt=options.system_prompt,
            messages=[{"role": "user", "content": prompt.strip()}],
        )

    def reply_with_context(
        self,
        question: str,
        *,
        request_text: str,
        summary_text: str,
        report_excerpt: str,
        history: list[dict[str, str]] | None = None,
        report_url: str = "",
    ) -> TaskResult:
        options = self.config.omlx_chat
        cleaned_question = question.strip()
        if not cleaned_question:
            return TaskResult(
                success=False,
                message="请直接补充你想继续追问的问题。",
                error_code="analysis_followup_missing_prompt",
                details={"mode": "analysis_followup"},
            )
        if len(cleaned_question) > options.max_prompt_chars:
            return TaskResult(
                success=False,
                message=f"追问内容过长，请压缩到 {options.max_prompt_chars} 字以内。",
                error_code="analysis_followup_prompt_too_long",
                details={"mode": "analysis_followup"},
            )
        context_sections = []
        if request_text.strip():
            context_sections.append("原始请求：\n" + request_text.strip())
        if summary_text.strip():
            context_sections.append("结果摘要：\n" + summary_text.strip())
        if report_excerpt.strip():
            context_sections.append("报告摘录：\n" + report_excerpt.strip())
        if report_url.strip():
            context_sections.append("报告链接：\n" + report_url.strip())
        context_block = "\n\n".join(context_sections).strip()
        if len(context_block) > options.followup_max_context_chars:
            context_block = context_block[: options.followup_max_context_chars - 1].rstrip() + "…"
        messages: list[dict[str, str]] = []
        for item in history or []:
            role = str(item.get("role", "")).strip()
            content = str(item.get("content", "")).strip()
            if role not in {"user", "assistant"} or not content:
                continue
            messages.append({"role": role, "content": content})
        messages.append(
            {
                "role": "user",
                "content": (
                    "以下是上一轮分析结果的上下文，请只基于这些信息继续回答。\n\n"
                    f"{context_block}\n\n"
                    f"用户追问：{cleaned_question}"
                ),
            }
        )
        return self._chat(
            mode="analysis_followup",
            system_prompt=options.followup_system_prompt,
            messages=messages,
        )

    def _chat(self, *, mode: str, system_prompt: str, messages: list[dict[str, str]]) -> TaskResult:
        options = self.config.omlx_chat
        url = options.base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": options.model,
            "messages": [{"role": "system", "content": system_prompt}, *messages],
            "temperature": options.temperature,
            "max_tokens": options.max_tokens,
            "stream": False,
        }
        if self.config.dry_run:
            return TaskResult(
                success=True,
                message="dry-run: omlx chat request planned",
                details={"mode": mode, "url": url, "model": options.model},
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
                details={"mode": mode, "url": url, "model": options.model},
            )
        except urllib.error.URLError as exc:
            return TaskResult(
                success=False,
                message=f"本地 omlx 模型不可用: {exc.reason}",
                duration_seconds=time.monotonic() - started,
                error_code="omlx_unavailable",
                details={"mode": mode, "url": url, "model": options.model},
            )
        except TimeoutError as exc:
            return TaskResult(
                success=False,
                message="本地 omlx 模型请求超时",
                duration_seconds=time.monotonic() - started,
                error_code="omlx_timeout",
                stderr=str(exc),
                details={"mode": mode, "url": url, "model": options.model},
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
                details={"mode": mode, "url": url, "model": options.model},
            )

        return TaskResult(
            success=True,
            message=answer.strip(),
            duration_seconds=time.monotonic() - started,
            stdout=body,
            details={"mode": mode, "url": url, "model": options.model},
        )


class IntentAnalysisFailure(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error_code: str,
        command: list[str] | None = None,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.command = command
        self.stdout = stdout
        self.stderr = stderr


class IntentAnalysisRunner:
    ROUTES = {
        "signal",
        "claude_skill",
        "bug",
        "direct_analysis",
        "perception_summary",
        "analysis_followup",
        "chat",
        "unsupported",
    }
    FOLLOWUP_ACTIONS = {"continue_agent", "reanalysis", "context_chat", "none"}
    CONTEXT_SOURCES = {"explicit", "latest_chat", "none"}

    def __init__(self, config: BridgeConfig, process_watchdog: ProcessWatchdog | None = None) -> None:
        self.config = config
        self.process_watchdog = process_watchdog

    def is_enabled(self) -> bool:
        options = self.config.intent_analysis
        return bool(options.enabled and self._provider_candidates())

    def classify(
        self,
        *,
        event: LarkEvent,
        route_content: str,
        explicit_followup_context: object | None = None,
        latest_chat_context: object | None = None,
    ) -> IntentDecision:
        if not self.is_enabled():
            raise IntentAnalysisFailure("intent analysis is disabled", error_code="intent_analysis_disabled")
        prompt = self._build_prompt(
            event=event,
            route_content=route_content,
            explicit_followup_context=explicit_followup_context,
            latest_chat_context=latest_chat_context,
        )
        primary_command, primary_output_path = self._build_command(prompt)
        if not primary_command:
            raise IntentAnalysisFailure("intent analysis command is not configured", error_code="intent_analysis_not_configured")
        if self.config.dry_run:
            return IntentDecision(
                route="unsupported",
                reason="dry-run: intent analysis command planned but not executed",
                confidence="low",
                followup_action="none",
                context_source="none",
            )
        last_failure: IntentAnalysisFailure | None = None
        attempts: list[tuple[list[str], Path | None]] = [(primary_command, primary_output_path)]
        fallback_invocation = self._fallback_intent_invocation(prompt)
        if fallback_invocation[0]:
            attempts.append(fallback_invocation)
        for index, (command, output_path) in enumerate(attempts):
            try:
                completed = run_tracked_process(
                    command,
                    watchdog=self.process_watchdog,
                    name="intent-analysis-agent",
                    cwd=self._working_dir(),
                    capture_output=True,
                    text=True,
                    timeout=self.config.intent_analysis.timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                if output_path is not None:
                    output_path.unlink(missing_ok=True)
                last_failure = IntentAnalysisFailure(
                    "消息意图分析超时",
                    error_code="intent_analysis_timeout",
                    command=command,
                    stdout=exc.stdout or "",
                    stderr=exc.stderr or "",
                )
                if index + 1 < len(attempts):
                    continue
                raise last_failure from exc
            except OSError as exc:
                if output_path is not None:
                    output_path.unlink(missing_ok=True)
                last_failure = IntentAnalysisFailure(
                    f"消息意图分析启动失败: {exc}",
                    error_code="intent_analysis_failed_to_start",
                    command=command,
                    stderr=str(exc),
                )
                if index + 1 < len(attempts):
                    continue
                raise last_failure from exc
            raw_response = self._read_response(completed=completed, output_path=output_path)
            if completed.returncode != 0:
                last_failure = IntentAnalysisFailure(
                    "消息意图分析失败",
                    error_code="intent_analysis_failed",
                    command=command,
                    stdout=completed.stdout,
                    stderr=completed.stderr or raw_response,
                )
                if index + 1 < len(attempts):
                    continue
                raise last_failure
            try:
                decision = self._parse_decision(raw_response)
            except ValueError as exc:
                last_failure = IntentAnalysisFailure(
                    f"消息意图分析结果无法解析: {exc}",
                    error_code="intent_analysis_bad_response",
                    command=command,
                    stdout=raw_response,
                    stderr=str(exc),
                )
                if index + 1 < len(attempts):
                    continue
                raise last_failure from exc
            decision.raw_response = raw_response
            return decision
        if last_failure is not None:
            raise last_failure
        raise IntentAnalysisFailure("intent analysis command is not configured", error_code="intent_analysis_not_configured")

    def _build_prompt(
        self,
        *,
        event: LarkEvent,
        route_content: str,
        explicit_followup_context: object | None,
        latest_chat_context: object | None,
    ) -> str:
        payload = {
            "event": {
                "chat_type": event.chat_type,
                "chat_id": event.chat_id,
                "message_type": event.message_type,
                "has_reply_link": bool(event.reply_to or event.parent_id or event.root_id or event.thread_id),
            },
            "message_text": self._clip(route_content.strip(), 2000),
            "explicit_followup_context": self._context_snapshot(explicit_followup_context),
            "latest_chat_context": self._context_snapshot(latest_chat_context),
        }
        prompt = (
            "请根据下面输入，判断这条飞书消息应该走哪条桥接路径。\n"
            "只输出一个 JSON 对象，字段必须完整：\n"
            '- "route": "signal" | "claude_skill" | "bug" | "direct_analysis" | "perception_summary" | "analysis_followup" | "chat" | "unsupported"\n'
            '- "followup_action": "continue_agent" | "reanalysis" | "context_chat" | "none"\n'
            '- "context_source": "explicit" | "latest_chat" | "none"\n'
            '- "confidence": "high" | "medium" | "low"\n'
            '- "reason": 一句简短中文说明\n\n'
            "判断规则：\n"
            "1. analysis_followup 表示用户在继续同一个已有分析会话。\n"
            "2. 对 bug 续聊，如果用户是在修正时间、要求重跑、要求基于同一份已下载日志重新生成结论/报告，选 reanalysis；"
            "如果是基于现有日志/报告继续追问、补充搜索、要求继续分析，选 continue_agent。\n"
            "3. 非 bug 的历史分析追问，若只是基于已有摘要/报告继续问答，选 context_chat。\n"
            "4. 如果消息是普通闲聊、问候、解释型问题，选 chat。\n"
            "5. 如果消息是在发新的 bug 链接分析请求，选 bug；如果是带附件/URL 的日志分析请求但不是 bug 链接，选 direct_analysis；"
            "如果是信号生命周期调查，选 signal；如果是感知总结，选 perception_summary；如果是 /skill 一类代码分析，选 claude_skill。\n"
            "6. 只有在没有合适路径时才选 unsupported。\n\n"
            "输入 JSON：\n"
            f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
        )
        max_chars = max(2000, int(self.config.intent_analysis.max_prompt_chars))
        if len(prompt) <= max_chars:
            return prompt
        return prompt[: max_chars - 1].rstrip() + "…"

    def _context_snapshot(self, context: object | None) -> dict[str, object] | None:
        if context is None:
            return None
        history = getattr(context, "history", None)
        if not isinstance(history, list):
            history = []
        history_items: list[dict[str, str]] = []
        for item in history[-4:]:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").strip()
            content = str(item.get("content") or "").strip()
            if not role or not content:
                continue
            history_items.append({"role": role, "content": self._clip(content, 500)})
        return {
            "root_message_id": str(getattr(context, "root_message_id", "") or ""),
            "chat_id": str(getattr(context, "chat_id", "") or ""),
            "mode": str(getattr(context, "mode", "") or ""),
            "request_text": self._clip(str(getattr(context, "request_text", "") or ""), 1200),
            "summary_text": self._clip(str(getattr(context, "summary_text", "") or ""), 1200),
            "report_excerpt": self._clip(str(getattr(context, "report_excerpt", "") or ""), 1200),
            "report_url": self._clip(str(getattr(context, "report_url", "") or ""), 500),
            "updated_at": str(getattr(context, "updated_at", "") or ""),
            "history": history_items,
        }

    def _build_command(self, prompt: str) -> tuple[list[str], Path | None]:
        provider, command_name, _ = self._resolved_provider()
        return self._build_command_for_provider(provider, command_name, prompt)

    def _build_command_for_provider(self, provider: str, command_name: str, prompt: str) -> tuple[list[str], Path | None]:
        system_prompt = self.config.intent_analysis.system_prompt
        if provider == "codex":
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", prefix="lark-intent-", delete=False) as fh:
                output_path = Path(fh.name)
            command = [
                command_name,
                "exec",
                "--skip-git-repo-check",
                "-s",
                "read-only",
                "-C",
                str(self._working_dir()),
                "--output-last-message",
                str(output_path),
                f"{system_prompt}\n\n{prompt}",
            ]
            return command, output_path
        if provider in {"claude", "claude-code", "claude_code"}:
            command = [
                command_name,
                "--print",
                "--output-format",
                "text",
                "--no-session-persistence",
                "--permission-mode",
                "dontAsk",
                "--tools",
                "",
                "--append-system-prompt",
                system_prompt,
                prompt,
            ]
            return command, None
        return [], None

    def _read_response(self, *, completed: subprocess.CompletedProcess[str], output_path: Path | None) -> str:
        try:
            if output_path is not None and output_path.exists():
                content = output_path.read_text(encoding="utf-8").strip()
                if content:
                    return content
            return completed.stdout.strip()
        finally:
            if output_path is not None:
                output_path.unlink(missing_ok=True)

    def _parse_decision(self, raw_response: str) -> IntentDecision:
        payload = self._extract_json_payload(raw_response)
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError("response is not valid JSON") from exc
        if not isinstance(parsed, dict):
            raise ValueError("response must be a JSON object")
        route = str(parsed.get("route") or "").strip()
        followup_action = str(parsed.get("followup_action") or "none").strip()
        context_source = str(parsed.get("context_source") or "none").strip()
        confidence = str(parsed.get("confidence") or "").strip()
        reason = str(parsed.get("reason") or "").strip()
        if route not in self.ROUTES:
            raise ValueError(f"unknown route: {route}")
        if followup_action not in self.FOLLOWUP_ACTIONS:
            raise ValueError(f"unknown followup_action: {followup_action}")
        if context_source not in self.CONTEXT_SOURCES:
            raise ValueError(f"unknown context_source: {context_source}")
        return IntentDecision(
            route=route,
            followup_action=followup_action,
            context_source=context_source,
            confidence=confidence,
            reason=reason,
        )

    def _extract_json_payload(self, raw_response: str) -> str:
        cleaned = raw_response.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned)
        if cleaned.startswith("{") and cleaned.endswith("}"):
            return cleaned
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError("no JSON object found")
        return cleaned[start : end + 1]

    def _resolved_provider(self) -> tuple[str, str, Path]:
        options = self.config.intent_analysis
        provider = (options.provider or self.config.bug_analysis.provider or "").strip().casefold()
        command_name = (options.command or self.config.bug_analysis.command or "").strip()
        return provider, command_name, self._working_dir()

    def _provider_candidates(self) -> list[tuple[str, str]]:
        options = self.config.intent_analysis
        provider = (options.provider or self.config.bug_analysis.provider or "").strip().casefold()
        command_name = (options.command or self.config.bug_analysis.command or "").strip()
        return _provider_candidates(provider, command_name)

    def _fallback_intent_invocation(self, prompt: str) -> tuple[list[str], Path | None]:
        candidates = self._provider_candidates()
        if len(candidates) < 2:
            return [], None
        provider, command_name = candidates[1]
        return self._build_command_for_provider(provider, command_name, prompt)

    def _working_dir(self) -> Path:
        return self.config.intent_analysis.working_dir or self.config.bug_analysis.working_dir or self.config.workspace_root

    def _clip(self, text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        return text[: limit - 1].rstrip() + "…"


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
            completed = run_tracked_process(
                command,
                watchdog=self.process_watchdog,
                name="claude-skill-agent",
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


def _default_command_for_provider(provider: str) -> str:
    normalized = (provider or "").strip().casefold()
    if normalized == "codex":
        return "codex"
    if normalized in {"claude", "claude-code", "claude_code"}:
        return "claude"
    return ""


def _normalize_provider_name(provider: str) -> str:
    normalized = (provider or "").strip().casefold()
    if normalized in {"claude", "claude-code", "claude_code"}:
        return "claude"
    if normalized == "codex":
        return "codex"
    return normalized


def _alternate_provider(provider: str) -> str:
    normalized = _normalize_provider_name(provider)
    if normalized == "codex":
        return "claude"
    if normalized == "claude":
        return "codex"
    return ""


def _provider_candidates(provider: str, command_name: str) -> list[tuple[str, str]]:
    primary_provider = _normalize_provider_name(provider)
    primary_command = command_name.strip() or _default_command_for_provider(primary_provider)
    alternate_provider = _alternate_provider(primary_provider)
    alternate_command = _default_command_for_provider(alternate_provider)
    candidates: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for current_provider, current_command in (
        (primary_provider, primary_command),
        (alternate_provider, alternate_command),
    ):
        if not current_provider or not current_command:
            continue
        key = (current_provider, current_command)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(key)
    return candidates


_PRIMARY_BUG_SKILL_MAP = PRIMARY_BUG_SKILL_MAP
_AUX_BUG_SKILLS = AUX_BUG_SKILLS
_extract_skill_frontmatter = extract_skill_frontmatter


class BugAnalysisRunner:
    def __init__(self, config: BridgeConfig, process_watchdog: ProcessWatchdog | None = None) -> None:
        self.config = config
        self.process_watchdog = process_watchdog
        self.signal_resolver = SignalResolver(config.guideengine_repo)

    def _available_bug_skills(self) -> list[dict[str, object]]:
        skills_dir = self.config.workspace_root / ".ai/skills"
        entries: list[dict[str, object]] = []
        if not skills_dir.exists():
            for skill_name, (kind, label, requires_logs) in _PRIMARY_BUG_SKILL_MAP.items():
                if skill_name == "general":
                    continue
                entries.append(
                    {
                        "name": skill_name,
                        "kind": kind,
                        "label": label,
                        "requires_logs": requires_logs,
                        "role": "primary",
                        "description": "",
                    }
                )
            entries.append(
                {
                    "name": "general",
                    "kind": "general",
                    "label": "通用问题分析",
                    "requires_logs": False,
                    "role": "primary",
                    "description": "没有合适专用 skill 时，由 agent 直接基于现有报告、源码证据和日志上下文给出结论。",
                }
            )
            return entries

        for path in sorted(skills_dir.glob("*/SKILL.md")):
            skill_name = path.parent.name
            try:
                body = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            frontmatter_name, description = _extract_skill_frontmatter(body)
            if skill_name in _PRIMARY_BUG_SKILL_MAP:
                kind, label, requires_logs = _PRIMARY_BUG_SKILL_MAP[skill_name]
                entries.append(
                    {
                        "name": skill_name,
                        "kind": kind,
                        "label": label,
                        "requires_logs": requires_logs,
                        "role": "primary",
                        "description": description or frontmatter_name or "",
                    }
                )
            elif skill_name in _AUX_BUG_SKILLS:
                entries.append(
                    {
                        "name": skill_name,
                        "kind": "",
                        "label": frontmatter_name or skill_name,
                        "requires_logs": False,
                        "role": "auxiliary",
                        "description": description or "",
                    }
                )
        entries.append(
            {
                "name": "general",
                "kind": "general",
                "label": "通用问题分析",
                "requires_logs": False,
                "role": "primary",
                "description": "没有合适专用 skill 时，由 agent 直接基于现有报告、源码证据和日志上下文给出结论。",
            }
        )
        return entries

    def _manual_bug_selection(self, *, prompt_text: str, title: str, description: str) -> BugAnalysisSelection:
        plans = self.classify_requests(prompt_text=prompt_text, title=title, description=description)
        return self._selection_from_plans(plans, source="manual_fallback", reason="Agent 分类不可用，退回本地规则分类。")

    def _selection_from_plans(
        self,
        plans: list["BugAnalysisPlan"],
        *,
        source: str,
        reason: str,
        provider: str = "",
    ) -> BugAnalysisSelection:
        plan = plans[0] if plans else BugAnalysisPlan(kind="general")
        skill_name = self._skill_name_for_kind(plan.kind)
        return BugAnalysisSelection(
            plans=plans or [BugAnalysisPlan(kind="general")],
            skill_name=skill_name,
            skill_label=self._analysis_label(plan.kind),
            source=source,
            reason=reason,
            provider=provider,
        )

    def _skill_name_for_kind(self, kind: str) -> str:
        for skill_name, (mapped_kind, _label, _requires_logs) in _PRIMARY_BUG_SKILL_MAP.items():
            if mapped_kind == kind:
                return skill_name
        return "general"

    def _resolve_bug_plans(self, *, analysis_kind: str, signal_hint: str, combined_text: str) -> list["BugAnalysisPlan"]:
        kind = (analysis_kind or "").strip()
        if kind == "signal":
            signal_request = parse_signal_request(
                "\n".join(part for part in [signal_hint, combined_text] if part),
                signal_aliases=self.config.signal_aliases,
                command_prefixes=self.config.command_prefixes,
                signal_resolver=self.signal_resolver,
            )
            return [BugAnalysisPlan(kind="signal", signal_code=signal_request.signal)]
        if kind in {"startup", "stuck", "crash", "perception", "xtheme", "general"}:
            return [BugAnalysisPlan(kind=kind)]
        return [BugAnalysisPlan(kind="general")]

    def _classify_bug_request_with_agent(
        self,
        *,
        prompt_text: str,
        title: str,
        description: str,
        attachments: object,
    ) -> BugAnalysisSelection | None:
        primary_skills = [item for item in self._available_bug_skills() if item.get("role") == "primary"]
        aux_skills = [item for item in self._available_bug_skills() if item.get("role") == "auxiliary"]
        payload = {
            "user_prompt": prompt_text,
            "bug_title": title,
            "bug_description": description[:4000],
            "attachments": attachments if isinstance(attachments, list) else [],
            "primary_skills": primary_skills,
            "auxiliary_skills": aux_skills,
        }
        prompt = (
            "你是 Lark Agent Bridge 的 bug skill 分类器。"
            "请根据用户请求、Bug 标题、描述和当前工作区技能，选择最合适的主分析 skill。"
            "只有当没有任何专用 skill 明确匹配时，才选择 general。"
            "signal-chain-analyzer 只在用户明确要排查信号链路/信号来源/信号是否送达时使用，"
            "不要因为文本里出现 signal 字样就滥用。"
            "xtheme / 105004 / 105009 / 晨曦 / 傍晚 / 主题切换 / XuiConditionHelper 应优先考虑 xtheme-analyzer。"
            "只输出一个 JSON 对象，字段必须完整："
            '{"analysis_kind":"startup|stuck|crash|signal|perception|xtheme|general",'
            '"skill":"unity-startup-lifecycle-check|3d-stuck-investigate|signal-chain-analyzer|perception-data-summary|xtheme-analyzer|general",'
            '"signal_hint":"可为空",'
            '"reason":"一句中文理由"}'
            "\n输入 JSON：\n"
            f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
        )
        parsed, provider = self._run_bug_decision_agent(prompt)
        if parsed is None:
            return None
        kind = str(parsed.get("analysis_kind") or "").strip()
        skill = str(parsed.get("skill") or "").strip()
        reason = str(parsed.get("reason") or "").strip()
        signal_hint = str(parsed.get("signal_hint") or "").strip()
        plans = self._resolve_bug_plans(
            analysis_kind=kind,
            signal_hint=signal_hint,
            combined_text="\n".join(part for part in [prompt_text, title, description] if part),
        )
        selection = self._selection_from_plans(
            plans,
            source="agent",
            reason=reason or "Agent 已完成 bug skill 分类。",
            provider=provider,
        )
        if skill:
            selection.skill_name = skill
        return selection

    def decide_bug_followup(
        self,
        *,
        followup_text: str,
        previous_context: object,
        previous_session: dict[str, object],
    ) -> BugFollowupSelection | None:
        details = previous_session.get("details", {}) if isinstance(previous_session, dict) else {}
        if not isinstance(details, dict):
            details = {}
        request_text = str(getattr(previous_context, "request_text", "") or details.get("user_request_text") or "")
        summary_text = str(getattr(previous_context, "summary_text", "") or "")
        report_excerpt = str(getattr(previous_context, "report_excerpt", "") or "")
        prepared_log_input = str(details.get("prepared_log_input") or "")
        selected_log_input = str(details.get("selected_log_input") or "")
        primary_skills = [item for item in self._available_bug_skills() if item.get("role") == "primary"]
        payload = {
            "request_text": request_text,
            "followup_text": followup_text,
            "summary_text": summary_text[:3000],
            "report_excerpt": report_excerpt[:5000],
            "prepared_log_input": prepared_log_input,
            "selected_log_input": selected_log_input,
            "current_analysis_kinds": details.get("analysis_kinds") or [],
            "primary_skills": primary_skills,
        }
        prompt = (
            "你是 Lark Agent Bridge 的 bug 续聊决策器。"
            "请先判断当前追问能否直接基于已有分析结果回答；如果不能，再决定是否需要重新分析，"
            "并选择最合适的主分析 skill。"
            "signal-chain-analyzer 只用于明确的信号链路问题；"
            "xtheme / 105004 / 105009 / 晨曦 / 傍晚 / 主题切换 / XuiConditionHelper 优先考虑 xtheme-analyzer。"
            "如果选择的 skill 需要日志，而当前 prepared_log_input / selected_log_input 为空，请把 retry_download_if_missing 设为 true。"
            "只输出一个 JSON 对象，字段必须完整："
            '{"action":"answer_from_existing|reanalyze",'
            '"analysis_kind":"startup|stuck|crash|signal|perception|xtheme|general",'
            '"skill":"unity-startup-lifecycle-check|3d-stuck-investigate|signal-chain-analyzer|perception-data-summary|xtheme-analyzer|general",'
            '"signal_hint":"可为空",'
            '"retry_download_if_missing":true,'
            '"reason":"一句中文理由"}'
            "\n输入 JSON：\n"
            f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
        )
        parsed, provider = self._run_bug_decision_agent(prompt)
        if parsed is None:
            return None
        action = str(parsed.get("action") or "").strip()
        kind = str(parsed.get("analysis_kind") or "").strip()
        skill = str(parsed.get("skill") or "").strip()
        signal_hint = str(parsed.get("signal_hint") or "").strip()
        reason = str(parsed.get("reason") or "").strip()
        retry_download = bool(parsed.get("retry_download_if_missing"))
        plans = self._resolve_bug_plans(
            analysis_kind=kind,
            signal_hint=signal_hint,
            combined_text="\n".join(part for part in [request_text, followup_text] if part),
        )
        if any(plan.kind == "signal" and not plan.signal_code for plan in plans):
            return None
        selection = self._selection_from_plans(
            plans,
            source="agent",
            reason=reason or "Agent 已完成 bug 续聊决策。",
            provider=provider,
        )
        if skill:
            selection.skill_name = skill
        return BugFollowupSelection(
            should_reanalyze=action == "reanalyze",
            force_rerun=action == "reanalyze",
            plans=selection.plans,
            skill_name=selection.skill_name,
            skill_label=selection.skill_label,
            source=selection.source,
            reason=selection.reason,
            provider=selection.provider,
        )

    def _run_bug_decision_agent(self, prompt: str) -> tuple[dict[str, object] | None, str]:
        candidates = _provider_candidates(self.config.bug_analysis.provider, self.config.bug_analysis.command)
        last_error: Exception | None = None
        for provider, command_name in candidates:
            command, output_path = self._build_bug_decision_command(provider, command_name, prompt)
            if not command:
                continue
            try:
                completed = run_tracked_process(
                    command,
                    watchdog=self.process_watchdog,
                    name="bug-analysis-classifier",
                    cwd=self._working_dir(),
                    capture_output=True,
                    text=True,
                    timeout=min(self.config.bug_analysis.timeout_seconds, 300),
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                last_error = exc
                if output_path is not None:
                    output_path.unlink(missing_ok=True)
                continue
            raw = self._read_bug_decision_response(completed=completed, output_path=output_path)
            if completed.returncode != 0:
                last_error = RuntimeError(raw or completed.stderr or completed.stdout or "bug decision agent failed")
                continue
            try:
                return self._parse_bug_decision_json(raw), provider
            except ValueError as exc:
                last_error = exc
                continue
        return None, ""

    def _build_bug_decision_command(self, provider: str, command_name: str, prompt: str) -> tuple[list[str], Path | None]:
        system_prompt = (
            "你是 Lark Agent Bridge 的结构化分类器。"
            "只能根据给定输入选择 skill 和分析动作，不能调用工具，不能假装读取额外文件。"
            "你必须只输出 JSON 对象。"
        )
        if provider == "codex":
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", prefix="lark-bug-decision-", delete=False) as fh:
                output_path = Path(fh.name)
            command = [
                command_name,
                "exec",
                "--skip-git-repo-check",
                "-s",
                "read-only",
                "-C",
                str(self._working_dir()),
                "--output-last-message",
                str(output_path),
                f"{system_prompt}\n\n{prompt}",
            ]
            return command, output_path
        if provider in {"claude", "claude-code", "claude_code"}:
            command = [
                command_name,
                "--print",
                "--output-format",
                "text",
                "--no-session-persistence",
                "--permission-mode",
                "dontAsk",
                "--tools",
                "",
                "--append-system-prompt",
                system_prompt,
                prompt,
            ]
            return command, None
        return [], None

    def _read_bug_decision_response(self, *, completed: subprocess.CompletedProcess[str], output_path: Path | None) -> str:
        try:
            if output_path is not None and output_path.exists():
                content = output_path.read_text(encoding="utf-8").strip()
                if content:
                    return content
            return completed.stdout.strip()
        finally:
            if output_path is not None:
                output_path.unlink(missing_ok=True)

    def _parse_bug_decision_json(self, raw_response: str) -> dict[str, object]:
        cleaned = raw_response.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned)
        if not cleaned.startswith("{"):
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start == -1 or end == -1 or end <= start:
                raise ValueError("no JSON object found")
            cleaned = cleaned[start : end + 1]
        parsed = json.loads(cleaned)
        if not isinstance(parsed, dict):
            raise ValueError("response must be a JSON object")
        return parsed

    def run_bug_analysis(
        self,
        request: BugRequest,
        *,
        event: LarkEvent | None = None,
        progress_callback: Callable[[dict[str, object]], None] | None = None,
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

        prompt_text = request.prompt.strip() or options.default_prompt
        if len(prompt_text) > options.max_prompt_chars:
            return TaskResult(
                success=False,
                message=f"Bug 分析描述过长，请压缩到 {options.max_prompt_chars} 字以内。",
                error_code="bug_prompt_too_long",
                details={"mode": "bug_analysis"},
            )

        context = create_job_context(self.config.data_dir, event=event)
        metadata_path = context.output_dir / "bug_metadata.md"
        request_text = self._request_text(raw_text=request.raw_text, prompt_text=prompt_text, bug_url=request.bug_url)
        plans = self.classify_requests(prompt_text=prompt_text, title="", description="")
        plan = plans[0]
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
            target_time=None,
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
                    "signal_code": plan.signal_code,
                    "user_request_text": request_text,
                    "agent_request_file": str(request_artifact),
                },
            )

        started = time.monotonic()
        try:
            self._emit_progress(progress_callback, stage="bug_check_env", message="检查 meegle 环境")
            env_status = self._run_json_command([str(self._bug_fetcher_script()), "check-env"], timeout=60)
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

            self._emit_progress(progress_callback, stage="bug_resolve_url", message="解析 bug 链接")
            resolved = self._run_json_command(
                [str(self._bug_fetcher_script()), "resolve-url", request.bug_url],
                timeout=60,
            )
            project_key = str(resolved["project_key"])
            work_item_id = str(resolved["work_item_id"])
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
            )
            fetched = self._run_json_command(
                [str(self._bug_fetcher_script()), "fetch-data", project_key, work_item_id],
                timeout=120,
            )
            full_item = self._run_json_command(
                ["meegle", "workitem", "get", "--project-key", project_key, "--work-item-id", work_item_id, "--format", "json"],
                timeout=120,
            )
            option_map = self._load_option_map(project_key)
            title = str(fetched.get("title", ""))
            description = self._bug_description(fetched)
            selection = self._classify_bug_request_with_agent(
                prompt_text=prompt_text,
                title=title,
                description=description,
                attachments=fetched.get("attachments", []),
            )
            if selection is not None and any(plan.kind == "signal" and not plan.signal_code for plan in selection.plans):
                selection = None
            if selection is None:
                selection = self._manual_bug_selection(prompt_text=prompt_text, title=title, description=description)
            plans = selection.plans
            requires_log_input = any(self._plan_requires_log_input(item) for item in plans)
            selected_input = self._select_log_input(bug_dir, fetched)
            cache_reused = False
            if self._has_bug_cache_content(bug_dir) and (selected_input is not None or not requires_log_input):
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
                self._emit_progress(progress_callback, stage="bug_download_logs", message="下载 bug 附件和日志")
                download = self._download_bug_attachments(
                    project_key,
                    work_item_id,
                    bug_dir,
                    fetched.get("attachments", []),
                    timeout=options.timeout_seconds,
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

            self._emit_progress(progress_callback, stage="bug_prepare_logs", message="准备日志输入")
            prepared_input = self._reuse_prepared_bug_input(selected_input) if selected_input else None
            if prepared_input is None:
                prepared_input = self._prepare_log_input(selected_input) if selected_input else None
            self._write_bug_cache_metadata(
                bug_dir,
                bug_url=request.bug_url,
                project_key=project_key,
                work_item_id=work_item_id,
                selected_input=selected_input,
                prepared_input=prepared_input,
            )
            fault_time, fault_time_note = self._extract_fault_time(title, description)
            source_evidence_path = self._write_reanalysis_source_evidence(
                plans=plans,
                request_text=request_text,
                followup_text=prompt_text,
                output_dir=context.output_dir,
                enabled=self._should_collect_source_evidence(request_text, prompt_text) or any(plan.kind == "general" for plan in plans),
                extra_texts=(title, description),
            )
            html_paths: list[Path] = []
            report_jsons: dict[str, Path | None] = {}
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
                    target_time=fault_time if current_plan.kind in {"startup", "xtheme"} else None,
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
                )
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
                else:
                    completed = self._run_analysis(
                        plan=current_plan,
                        input_path=input_for_plan,
                        html_path=current_html,
                        json_path=current_json,
                        analysis_dir=current_analysis_dir,
                        timeout=options.timeout_seconds,
                        target_time=fault_time if current_plan.kind in {"startup", "xtheme"} else None,
                        request_text=request_text if current_plan.kind == "xtheme" else None,
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
                        progress_callback=progress_callback,
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
                        progress_callback=progress_callback,
                    )
                html_paths.append(current_html)
                report_jsons[current_plan.kind] = current_json if current_json.exists() else None
                if current_plan is plan:
                    command = current_command
                    html_path = current_html
                    json_path = current_json
                    analysis_dir = current_analysis_dir

            self._emit_progress(progress_callback, stage="bug_build_outputs", message="整理 bug 分析结果")
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
            )
            metadata_path.write_text(metadata_text, encoding="utf-8")
            self._append_source_evidence_metadata(metadata_path, source_evidence_path)
            combined_artifacts = self._build_combined_report_artifacts(
                plans=plans,
                prompt_text=prompt_text,
                fault_time=fault_time,
                output_dir=context.output_dir,
                html_paths=html_paths,
                report_jsons=report_jsons,
                selected_input=selected_input,
                source_evidence_path=source_evidence_path,
            )
            agent_summary_path = context.output_dir / "bug_agent_summary.md"
            agent_summary_result = self._run_bug_agent_summary(
                request_text=request_text,
                request_artifact=request_artifact,
                metadata_path=metadata_path,
                output_path=agent_summary_path,
                progress_callback=progress_callback,
                timeout=min(options.timeout_seconds, 1800),
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
                stdout=exc.stdout or "",
                stderr=exc.stderr or "",
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
            "classification_source": selection.source,
            "classification_reason": selection.reason,
            "classification_provider": selection.provider,
            "signal_code": plan.signal_code,
            "selected_log_input": str(selected_input) if selected_input else "",
            "prepared_log_input": str(prepared_input) if prepared_input else "",
            "bug_dir": str(bug_dir),
            "bug_cache_dir": str(bug_dir),
            "bug_cache_reused": cache_reused,
            "user_request_text": request_text,
            "agent_request_file": str(request_artifact),
            "agent_summary_file": str(agent_summary_path),
        }
        if source_evidence_path is not None:
            details["source_evidence_file"] = str(source_evidence_path)
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

    def run_bug_reanalysis(
        self,
        *,
        followup_text: str,
        previous_context: object,
        previous_session: dict[str, object],
        event: LarkEvent | None = None,
        progress_callback: Callable[[dict[str, object]], None] | None = None,
        force_rerun: bool = False,
        plans_override: list["BugAnalysisPlan"] | None = None,
        classification_skill: str = "",
        classification_source: str = "",
        classification_reason: str = "",
        classification_provider: str = "",
    ) -> TaskResult:
        started = time.monotonic()
        details = previous_session.get("details", {})
        if not isinstance(details, dict):
            details = {}
        job_id = str(previous_session.get("job_id") or "").strip()
        job_dir_value = str(previous_session.get("job_dir") or "").strip()
        if not job_id and job_dir_value:
            job_id = Path(job_dir_value).name
        if not job_id:
            return TaskResult(
                success=False,
                message="无法复用上次 Bug 分析：未找到上一轮 job_id。",
                error_code="bug_reanalysis_missing_job",
                details={"mode": "bug_reanalysis"},
            )
        job_dir = Path(job_dir_value) if job_dir_value else self.config.data_dir / "jobs" / job_id
        output_dir = job_dir / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        download_retry_result: dict[str, object] | None = None
        request_text = str(getattr(previous_context, "request_text", "") or details.get("user_request_text") or "")
        target_time = self._extract_followup_fault_time(
            followup_text,
            reference_text="\n".join(
                [
                    request_text,
                    str(getattr(previous_context, "summary_text", "")),
                    str(getattr(previous_context, "report_excerpt", "")),
                ]
            ),
        )
        prepared_input = self._path_from_details(details, "prepared_log_input")
        selected_input = self._path_from_details(details, "selected_log_input") or prepared_input
        plans = plans_override or self._plans_for_reanalysis(details, request_text=request_text, followup_text=followup_text)
        requires_log_input = any(self._plan_requires_log_input(plan) for plan in plans)
        if requires_log_input and prepared_input is None:
            retry = self._retry_bug_log_download(
                previous_session=previous_session,
                job_dir=job_dir,
                progress_callback=progress_callback,
            )
            download_retry_result = retry
            if retry.get("prepared_input") is not None:
                prepared_input = retry["prepared_input"]
                selected_input = retry.get("selected_input") or prepared_input
            else:
                failure_message = str(retry.get("message") or "无法复用上次 Bug 分析：未找到已准备好的日志输入，且重新下载日志失败。")
                failure_details = {
                    "mode": "bug_reanalysis",
                    "download_retry": retry,
                }
                return TaskResult(
                    success=False,
                    message=failure_message,
                    job_id=job_id,
                    job_dir=job_dir,
                    duration_seconds=time.monotonic() - started,
                    error_code="bug_reanalysis_missing_prepared_input",
                    details=failure_details,
                )

        force_rerun_kinds = self._forced_reanalysis_kinds(
            plans,
            request_text=request_text,
            followup_text=followup_text,
            force_rerun=force_rerun,
        )
        prompt_text = f"{request_text}\n追问/修正：{followup_text}".strip()
        source_evidence_path = self._write_reanalysis_source_evidence(
            plans=plans,
            request_text=request_text,
            followup_text=followup_text,
            output_dir=output_dir,
            enabled=any(plan.kind == "general" for plan in plans)
            or bool(force_rerun_kinds.intersection({"signal"}))
            or self._should_collect_source_evidence(request_text, followup_text),
        )
        self._emit_progress(
            progress_callback,
            stage="bug_reanalysis_reuse_context",
            message="复用上一轮 bug 分析上下文，不重新拉取/下载/解密日志",
            job_id=job_id,
            prepared_log_input=str(prepared_input or ""),
            selected_log_input=str(selected_input or ""),
            target_time=target_time,
            analysis_kinds=[plan.kind for plan in plans],
        )

        html_paths: list[Path] = []
        report_jsons: dict[str, Path | None] = {}
        rerun_kinds: list[str] = []
        reused_kinds: list[str] = []
        command: list[str] | None = None
        try:
            for plan in plans:
                html_path = output_dir / self._report_name(plan.kind, "html")
                json_path = output_dir / self._report_name(plan.kind, "json")
                analysis_dir = output_dir / f"{plan.kind}_analysis"
                should_rerun = (
                    plan.kind == "startup"
                    or plan.kind in force_rerun_kinds
                    or not html_path.exists()
                    or not json_path.exists()
                )
                if not should_rerun:
                    self._emit_progress(
                        progress_callback,
                        stage="bug_reanalysis_reuse_report",
                        message=f"复用已生成的{self._analysis_label(plan.kind)}报告",
                        plan=plan.kind,
                        html_path=str(html_path),
                        json_path=str(json_path),
                    )
                    reused_kinds.append(plan.kind)
                    html_paths.append(html_path)
                    report_jsons[plan.kind] = json_path if json_path.exists() else None
                    continue

                input_for_plan = prepared_input
                if plan.kind == "startup":
                    input_for_plan = self._startup_analysis_input(prepared_input, target_time)
                rerun_kinds.append(plan.kind)
                command = self.build_command(
                    plan=plan,
                    input_path=input_for_plan or output_dir,
                    html_path=html_path,
                    json_path=json_path,
                    analysis_dir=analysis_dir,
                    target_time=target_time if plan.kind in {"startup", "xtheme"} else None,
                    request_text=followup_text if plan.kind == "xtheme" else None,
                )
                self._emit_progress(
                    progress_callback,
                    stage="bug_reanalysis_run_analysis",
                    message=(
                        f"基于已准备日志重新执行{self._analysis_label(plan.kind)}"
                        if input_for_plan is not None
                        else f"基于已有上下文重新执行{self._analysis_label(plan.kind)}"
                    ),
                    plan=plan.kind,
                    plan_label=self._analysis_label(plan.kind),
                    html_path=str(html_path),
                    json_path=str(json_path),
                    target_time=target_time if plan.kind in {"startup", "xtheme"} else "",
                )
                if plan.kind == "general":
                    self._write_general_bug_report(
                        html_path=html_path,
                        json_path=json_path,
                        title="",
                        description="",
                        prompt_text=followup_text,
                        request_text=request_text,
                        fault_time=target_time,
                        selected_input=selected_input,
                        source_evidence_path=source_evidence_path,
                        classification_skill=classification_skill or self._skill_name_for_kind(plan.kind),
                        classification_source=classification_source or "manual_fallback",
                        classification_reason=classification_reason or "",
                    )
                    completed = subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")
                else:
                    completed = self._run_analysis(
                        plan=plan,
                        input_path=input_for_plan,
                        html_path=html_path,
                        json_path=json_path,
                        analysis_dir=analysis_dir,
                        timeout=self.config.bug_analysis.timeout_seconds,
                        target_time=target_time if plan.kind in {"startup", "xtheme"} else None,
                        request_text=followup_text if plan.kind == "xtheme" else None,
                    )
                if completed.returncode != 0:
                    return TaskResult(
                        success=False,
                        message=f"Bug 续聊重分析失败：{self._analysis_label(plan.kind)}脚本执行失败。",
                        job_id=job_id,
                        job_dir=job_dir,
                        command=command,
                        duration_seconds=time.monotonic() - started,
                        error_code=f"bug_reanalysis_{plan.kind}_failed",
                        stdout=completed.stdout,
                        stderr=completed.stderr,
                        details={"mode": "bug_reanalysis"},
                    )
                html_paths.append(html_path)
                report_jsons[plan.kind] = json_path if json_path.exists() else None
        except subprocess.TimeoutExpired as exc:
            return TaskResult(
                success=False,
                message="Bug 续聊重分析超时",
                job_id=job_id,
                job_dir=job_dir,
                command=command,
                duration_seconds=time.monotonic() - started,
                error_code="bug_reanalysis_timeout",
                stdout=exc.stdout or "",
                stderr=exc.stderr or "",
                details={"mode": "bug_reanalysis"},
            )

        combined_artifacts = self._build_combined_report_artifacts(
            plans=plans,
            prompt_text=prompt_text,
            fault_time=target_time,
            output_dir=output_dir,
            html_paths=html_paths,
            report_jsons=report_jsons,
            selected_input=selected_input,
            source_evidence_path=source_evidence_path,
        )
        agent_request_path = output_dir / "bug_agent_reanalysis_request.md"
        agent_request_path.write_text(
            self._render_bug_reanalysis_request(
                request_text=request_text,
                followup_text=followup_text,
                target_time=target_time,
                plans=plans,
                history=getattr(previous_context, "history", None),
            ),
            encoding="utf-8",
        )
        agent_metadata_path = output_dir / "bug_reanalysis_metadata.md"
        previous_summary_path = self._path_from_details(details, "agent_summary_file")
        agent_metadata_path.write_text(
            self._render_bug_reanalysis_metadata(
                request_text=request_text,
                followup_text=followup_text,
                job_id=job_id,
                target_time=target_time,
                prepared_input=prepared_input,
                selected_input=selected_input,
                plans=plans,
                rerun_kinds=rerun_kinds,
                reused_kinds=reused_kinds,
                html_paths=html_paths,
                report_jsons=report_jsons,
                combined_artifacts=combined_artifacts,
                previous_summary_path=previous_summary_path,
                source_evidence_path=source_evidence_path,
                classification_skill=classification_skill or self._skill_name_for_kind(plans[0].kind if plans else "general"),
                classification_source=classification_source or "manual_fallback",
                classification_reason=classification_reason or "",
                classification_provider=classification_provider or "",
            ),
            encoding="utf-8",
        )
        agent_summary_path = previous_summary_path or (output_dir / "bug_agent_summary.md")
        agent_summary_result = self._run_bug_agent_summary(
            request_text=request_text,
            request_artifact=agent_request_path,
            metadata_path=agent_metadata_path,
            output_path=agent_summary_path,
            progress_callback=progress_callback,
            timeout=min(self.config.bug_analysis.timeout_seconds, 1800),
            provider_session_id="",
            followup_text=followup_text,
            previous_summary_path=previous_summary_path,
        )
        self._append_agent_runtime_metadata(
            agent_metadata_path,
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
        if combined_artifacts is not None:
            final_message = str(combined_artifacts["summary"])
            files_to_send = [Path(combined_artifacts["html_path"])]
        else:
            final_message = self._build_direct_analysis_summary(plans, prompt_text, html_paths)
            files_to_send = html_paths
        if agent_summary_result["message"]:
            final_message = str(agent_summary_result["message"])
        self._emit_progress(
            progress_callback,
            stage="bug_reanalysis_completed",
            message="bug 续聊重分析完成",
            job_id=job_id,
            target_time=target_time,
            analysis_kinds=[plan.kind for plan in plans],
            html_reports=[str(path) for path in html_paths],
        )
        result_details = {
            "mode": "bug_reanalysis",
            "analysis_kind": plans[0].kind if plans else "",
            "analysis_kinds": [plan.kind for plan in plans],
            "analysis_skill": classification_skill or self._skill_name_for_kind(plans[0].kind if plans else "general"),
            "analysis_skill_label": self._analysis_label(plans[0].kind) if plans else "通用问题分析",
            "classification_source": classification_source or "manual_fallback",
            "classification_reason": classification_reason or "",
            "classification_provider": classification_provider or "",
            "selected_log_input": str(selected_input or ""),
            "prepared_log_input": str(prepared_input),
            "user_request_text": request_text,
            "followup_text": followup_text,
            "target_time": target_time,
            "rerun_analysis_kinds": rerun_kinds,
            "reused_analysis_kinds": reused_kinds,
            "agent_request_file": str(agent_request_path),
            "reanalysis_metadata_file": str(agent_metadata_path),
            "agent_summary_file": str(agent_summary_path),
            "files_to_send": files_to_send,
        }
        signal_codes = [plan.signal_code for plan in plans if plan.kind == "signal" and plan.signal_code]
        if signal_codes:
            result_details["signal_code"] = signal_codes[0]
        if source_evidence_path is not None:
            result_details["source_evidence_file"] = str(source_evidence_path)
        if download_retry_result is not None:
            result_details["download_retry"] = download_retry_result
        if combined_artifacts is not None:
            result_details["combined_report_html"] = str(combined_artifacts["html_path"])
            result_details["combined_report_json"] = str(combined_artifacts["json_path"])
        self._apply_agent_runtime_details(result_details, agent_summary_result)
        return TaskResult(
            success=True,
            message=final_message,
            job_id=job_id,
            job_dir=job_dir,
            command=command,
            duration_seconds=time.monotonic() - started,
            details=result_details,
        )

    def run_bug_agent_followup(
        self,
        *,
        followup_text: str,
        previous_context: object,
        previous_session: dict[str, object],
        event: LarkEvent | None = None,
        progress_callback: Callable[[dict[str, object]], None] | None = None,
        resume_agent_session: bool = False,
    ) -> TaskResult:
        started = time.monotonic()
        details = previous_session.get("details", {})
        if not isinstance(details, dict):
            details = {}
        job_id = str(previous_session.get("job_id") or "").strip()
        job_dir_value = str(previous_session.get("job_dir") or "").strip()
        if not job_id and job_dir_value:
            job_id = Path(job_dir_value).name
        if not job_id:
            return TaskResult(
                success=False,
                message="无法延续上次 Bug 分析：未找到上一轮 job_id。",
                error_code="bug_agent_followup_missing_job",
                details={"mode": "bug_agent_followup"},
            )
        job_dir = Path(job_dir_value) if job_dir_value else self.config.data_dir / "jobs" / job_id
        output_dir = job_dir / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        request_text = str(getattr(previous_context, "request_text", "") or details.get("user_request_text") or "").strip()
        prepared_input = self._path_from_details(details, "prepared_log_input")
        selected_input = self._path_from_details(details, "selected_log_input")
        previous_summary_path = self._path_from_details(details, "agent_summary_file")
        if previous_summary_path is not None and not previous_summary_path.exists():
            previous_summary_path = None
        report_files = self._collect_bug_output_artifacts(output_dir)
        plans = self._plans_from_previous_details(details, fallback_text=request_text)
        provider_session_id = self._bug_followup_resume_session_id(details, force=resume_agent_session)
        self._emit_progress(
            progress_callback,
            stage="bug_agent_followup_prepare",
            message=(
                "复用上一轮 bug 资料并新开本地 Agent 分析追问"
                if not provider_session_id
                else "复用上一轮 bug 资料并继续原本地 Agent 会话分析追问"
            ),
            job_id=job_id,
            prepared_log_input=str(prepared_input or ""),
            selected_log_input=str(selected_input or ""),
            output_dir=str(output_dir),
            provider_session_id=provider_session_id,
            analysis_kinds=[plan.kind for plan in plans],
        )
        agent_request_path = output_dir / "bug_agent_followup_request.md"
        agent_request_path.write_text(
            self._render_bug_agent_followup_request(
                request_text=request_text,
                followup_text=followup_text,
                summary_text=str(getattr(previous_context, "summary_text", "") or ""),
                report_excerpt=str(getattr(previous_context, "report_excerpt", "") or ""),
                history=getattr(previous_context, "history", None),
            ),
            encoding="utf-8",
        )
        agent_metadata_path = output_dir / "bug_agent_followup_metadata.md"
        agent_metadata_path.write_text(
            self._render_bug_agent_followup_metadata(
                request_text=request_text,
                followup_text=followup_text,
                job_id=job_id,
                job_dir=job_dir,
                output_dir=output_dir,
                prepared_input=prepared_input,
                selected_input=selected_input,
                previous_summary_path=previous_summary_path,
                report_files=report_files,
                report_url=str(getattr(previous_context, "report_url", "") or ""),
            ),
            encoding="utf-8",
        )
        agent_summary_path = previous_summary_path or (output_dir / "bug_agent_summary.md")
        agent_summary_result = self._run_bug_agent_summary(
            request_text=request_text,
            request_artifact=agent_request_path,
            metadata_path=agent_metadata_path,
            output_path=agent_summary_path,
            progress_callback=progress_callback,
            timeout=min(self.config.bug_analysis.timeout_seconds, 1800),
            provider_session_id=provider_session_id,
            followup_text=followup_text,
            previous_summary_path=previous_summary_path,
        )
        self._append_agent_runtime_metadata(
            agent_metadata_path,
            agent_summary_result=agent_summary_result,
            total_duration_seconds=time.monotonic() - started,
        )
        report_html_paths = [path for path in report_files if path.suffix.lower() == ".html"]
        self._annotate_html_reports(
            report_html_paths,
            agent_summary_result=agent_summary_result,
            total_duration_seconds=time.monotonic() - started,
        )
        result_details = {
            "mode": "bug_agent_followup",
            "analysis_kinds": [plan.kind for plan in plans],
            "prepared_log_input": str(prepared_input or ""),
            "selected_log_input": str(selected_input or ""),
            "user_request_text": request_text,
            "followup_text": followup_text,
            "agent_request_file": str(agent_request_path),
            "followup_metadata_file": str(agent_metadata_path),
            "agent_summary_file": str(agent_summary_path),
        }
        self._apply_agent_runtime_details(result_details, agent_summary_result)
        if not agent_summary_result["message"]:
            error = str(agent_summary_result["error"] or "")
            if error == "agent_summary_not_configured":
                message = "Bug 续聊失败：未配置可继续会话的本地 Agent。"
            elif error:
                message = f"Bug 续聊失败：本地 Agent 未返回结果（{error}）。"
            else:
                message = "Bug 续聊失败：本地 Agent 未返回结果。"
            return TaskResult(
                success=False,
                message=message,
                job_id=job_id,
                job_dir=job_dir,
                command=list(agent_summary_result["command"]) if agent_summary_result["command"] else None,
                duration_seconds=time.monotonic() - started,
                error_code="bug_agent_followup_failed",
                details=result_details,
            )
        self._emit_progress(
            progress_callback,
            stage="bug_agent_followup_completed",
            message="Bug 续聊已由本地 Agent 完成",
            job_id=job_id,
            provider=str(agent_summary_result["provider"] or ""),
            provider_session_id=str(agent_summary_result["session_id"] or provider_session_id),
        )
        return TaskResult(
            success=True,
            message=str(agent_summary_result["message"]),
            job_id=job_id,
            job_dir=job_dir,
            command=list(agent_summary_result["command"]) if agent_summary_result["command"] else None,
            duration_seconds=time.monotonic() - started,
            details=result_details,
        )

    def _bug_followup_resume_session_id(self, details: dict[str, object], *, force: bool = False) -> str:
        if not force and not self.config.bug_analysis.resume_followup_sessions:
            return ""
        return str(details.get("agent_summary_session_id") or "").strip()

    def run_direct_analysis(
        self,
        request: DirectAnalysisRequest,
        *,
        event: LarkEvent | None = None,
        progress_callback: Callable[[dict[str, object]], None] | None = None,
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
        metadata_path = context.output_dir / "direct_analysis_metadata.md"
        started = time.monotonic()
        request_text = self._request_text(raw_text=request.raw_text, prompt_text=request.prompt, bug_url="")
        self._emit_progress(
            progress_callback,
            stage="direct_job_created",
            message="已创建直传文件分析任务",
            job_id=context.job_id,
            request_text=request_text,
            resources=[item.value for item in request.resources],
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
        plans = self.classify_requests(prompt_text=request.prompt, title="", description="")
        html_paths: list[Path] = []
        report_jsons: dict[str, Path | None] = {}
        command: list[str] | None = None
        fault_time, _ = self._extract_fault_time("", request.prompt)
        source_evidence_path = self._write_reanalysis_source_evidence(
            plans=plans,
            request_text=request_text,
            followup_text=request.prompt,
            output_dir=context.output_dir,
            enabled=self._should_collect_source_evidence(request_text, request.prompt) or any(plan.kind == "general" for plan in plans),
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
                target_time=fault_time if current_plan.kind in {"startup", "xtheme"} else None,
                request_text=request.prompt if current_plan.kind == "xtheme" else None,
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
                else:
                    completed = self._run_analysis(
                        plan=current_plan,
                        input_path=input_for_plan,
                        html_path=current_html,
                        json_path=current_json,
                        analysis_dir=current_analysis_dir,
                        timeout=self.config.bug_analysis.timeout_seconds,
                        target_time=fault_time if current_plan.kind in {"startup", "xtheme"} else None,
                        request_text=request.prompt if current_plan.kind == "xtheme" else None,
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
        self._emit_progress(
            progress_callback,
            stage="direct_completed",
            message="直传文件分析完成",
            job_id=context.job_id,
            analysis_kinds=[item.kind for item in plans],
            html_reports=[str(path) for path in html_paths],
        )
        return TaskResult(
            success=True,
            message=str(combined_artifacts["summary"]) if combined_artifacts is not None else summary,
            job_id=context.job_id,
            job_dir=context.job_dir,
            command=command,
            duration_seconds=time.monotonic() - started,
            details={
                "mode": "direct_analysis",
                "analysis_kinds": [item.kind for item in plans],
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
            },
        )

    def classify_requests(self, *, prompt_text: str, title: str, description: str) -> list["BugAnalysisPlan"]:
        combined = "\n".join(part for part in [prompt_text, title, description] if part).strip()
        signal_request = parse_signal_request(
            combined,
            signal_aliases=self.config.signal_aliases,
            command_prefixes=self.config.command_prefixes,
            signal_resolver=self.signal_resolver,
        )
        lowered = combined.casefold()
        if any(term in lowered for term in PERCEPTION_ROUTE_TERMS):
            return [BugAnalysisPlan(kind="perception")]
        if any(term in lowered for term in XTHEME_ROUTE_TERMS):
            return [BugAnalysisPlan(kind="xtheme")]
        explicit_signal_enum = "signal_" in lowered
        explicit_signal_terms = any(term in lowered for term in SIGNAL_ROUTE_TERMS)
        if explicit_signal_enum or (explicit_signal_terms and signal_request.signal):
            return [BugAnalysisPlan(kind="signal", signal_code=signal_request.signal)]
        if any(term in lowered for term in CRASH_ROUTE_TERMS):
            return [BugAnalysisPlan(kind="crash")]
        plans: list[BugAnalysisPlan] = []
        startup_requested = any(term in lowered for term in STARTUP_ROUTE_TERMS)
        stuck_requested = any(term in lowered for term in STUCK_ROUTE_TERMS)
        startup_blocked = any(term in lowered for term in STARTUP_BLOCK_ROUTE_TERMS)
        if startup_requested or (stuck_requested and startup_blocked):
            plans.append(BugAnalysisPlan(kind="startup"))
        if stuck_requested:
            plans.append(BugAnalysisPlan(kind="stuck"))
        if plans:
            return plans
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
        if plan.kind == "crash":
            return [
                sys.executable,
                str(self._stuck_script()),
                str(input_path),
            ]
        if plan.kind == "general":
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
        return plan.kind in {"startup", "stuck", "crash", "perception", "xtheme"}

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
            if kind in {"startup", "stuck", "crash", "signal", "perception", "xtheme", "general"}
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

    def _extract_followup_fault_time(self, followup_text: str, *, reference_text: str) -> str:
        normalized = followup_text.replace("：", ":")
        full_match = re.search(
            r"(20\d{2})[-_/年](\d{1,2})[-_/月](\d{1,2})[日_\s-]*(\d{1,2}):(\d{2})",
            normalized,
        )
        if full_match:
            return (
                f"{int(full_match.group(1)):04d}-{int(full_match.group(2)):02d}-{int(full_match.group(3)):02d} "
                f"{int(full_match.group(4)):02d}:{int(full_match.group(5)):02d}"
            )
        short_match = re.search(r"(?<!\d)(\d{1,2}):(\d{2})(?:\s*分)?(?!\d)", normalized)
        if not short_match:
            return ""
        reference_date = self._extract_reference_date(reference_text)
        if reference_date:
            return f"{reference_date} {int(short_match.group(1)):02d}:{int(short_match.group(2)):02d}"
        return f"{int(short_match.group(1)):02d}:{int(short_match.group(2)):02d}"

    def _extract_reference_date(self, text: str) -> str:
        normalized = text.replace("：", ":")
        match = re.search(r"(20\d{2})[-_/年](\d{1,2})[-_/月](\d{1,2})", normalized)
        if not match:
            return ""
        return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"

    def _report_name(self, kind: str, suffix: str) -> str:
        return {
            "startup": f"bug_3d_startup_report.{suffix}",
            "stuck": f"bug_3d_stuck_report.{suffix}",
            "crash": f"bug_crash_report.{suffix}",
            "perception": f"bug_perception_data_summary.{suffix}",
            "signal": f"bug_signal_chain_report.{suffix}",
            "xtheme": f"bug_xtheme_analysis_report.{suffix}",
            "general": f"bug_general_analysis_report.{suffix}",
        }[kind]

    def _combined_report_name(self, suffix: str) -> str:
        return f"bug_startup_stuck_report.{suffix}"

    def _analysis_label(self, kind: str) -> str:
        return {
            "startup": "3D启动时序分析",
            "stuck": "3D卡顿分析",
            "crash": "Crash/闪退分析",
            "perception": "当前感知数据总结",
            "signal": "信号链路分析",
            "xtheme": "XTheme时光主题分析",
            "general": "通用问题分析",
        }[kind]

    def _run_json_command(self, command: list[str], *, timeout: int) -> dict[str, object]:
        completed = run_tracked_process(
            command,
            watchdog=self.process_watchdog,
            name="bug-json-command",
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
            completed = run_tracked_process(
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

    def _extract_process_error_message(self, completed: subprocess.CompletedProcess[str]) -> str:
        candidates = [completed.stderr or "", completed.stdout or ""]
        for text in candidates:
            stripped = text.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                return stripped.splitlines()[0][:400]
            if isinstance(payload, dict):
                error = payload.get("error")
                if isinstance(error, dict):
                    code = str(error.get("code") or "").strip()
                    message = str(error.get("message") or "").strip()
                    if code and message:
                        return f"{code}: {message}"
                    if message:
                        return message
                if payload.get("ok") is False:
                    return str(payload.get("error") or "command returned ok=false")
            return stripped[:400]
        return "unknown_error"

    def _should_download_bug_attachment(self, name: str) -> bool:
        lower_name = name.strip().casefold()
        return bool(lower_name) and any(lower_name.endswith(suffix) for suffix in _BUG_ATTACHMENT_DOWNLOAD_SUFFIXES)

    def _extract_downloaded_zip(self, archive_path: Path, logs_dir: Path) -> bool:
        try:
            with zipfile.ZipFile(archive_path) as zf:
                zf.extractall(logs_dir)
        except (OSError, zipfile.BadZipFile):
            return False
        self._normalize_tree_permissions(logs_dir)
        try:
            archive_path.unlink()
        except OSError:
            pass
        return True

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

        if self._has_meaningful_log_tree(logs_dir):
            return logs_dir
        priority_suffixes = (".xp.zip.001", ".xp", ".zip", ".alog", ".xlog", ".log", ".txt")
        for suffix in priority_suffixes:
            for candidate in attachments:
                if candidate.name.lower().endswith(suffix) and self._is_usable_log_attachment(candidate):
                    return candidate
        return None

    def _prepare_log_input(self, selected_input: Path) -> Path:
        lower_name = selected_input.name.lower()
        if lower_name.endswith(".xp"):
            return self._expand_xp_file(selected_input)
        if lower_name.endswith(".zip") and not lower_name.endswith(".xp.zip.001"):
            if not zipfile.is_zipfile(selected_input):
                raise RuntimeError(f"日志附件不是有效 zip: {selected_input.name}")
            extract_dir = selected_input.with_suffix("")
            if not extract_dir.exists():
                with zipfile.ZipFile(selected_input) as zf:
                    zf.extractall(extract_dir)
                self._normalize_tree_permissions(extract_dir)
            return extract_dir
        return selected_input

    def _retry_bug_log_download(
        self,
        *,
        previous_session: dict[str, object],
        job_dir: Path,
        progress_callback: Callable[[dict[str, object]], None] | None,
    ) -> dict[str, object]:
        details = previous_session.get("details", {}) if isinstance(previous_session, dict) else {}
        if not isinstance(details, dict):
            details = {}
        bug_url = str(details.get("bug_url") or "").strip()
        if not bug_url:
            return {"ok": False, "message": "缺少 bug_url，无法重新下载日志。"}
        try:
            env_status = self._run_json_command([str(self._bug_fetcher_script()), "check-env"], timeout=60)
        except Exception as exc:
            return {"ok": False, "message": f"重新检查 meegle 环境失败：{exc}"}
        if not env_status.get("meegle_installed", False):
            return {"ok": False, "message": "重新下载日志失败：本机未安装 meegle CLI。"}
        if not env_status.get("auth_ok", False):
            host = str(env_status.get("host") or "project.feishu.cn")
            return {
                "ok": False,
                "message": f"重新下载日志失败：meegle 未登录或授权已失效（host={host}）。",
                "reason": "AUTH_REQUIRED",
            }
        try:
            resolved = self._run_json_command([str(self._bug_fetcher_script()), "resolve-url", bug_url], timeout=60)
        except Exception as exc:
            return {"ok": False, "message": f"重新解析 bug 链接失败：{exc}"}
        project_key = str(resolved.get("project_key") or "").strip()
        work_item_id = str(resolved.get("work_item_id") or "").strip()
        if not project_key or not work_item_id:
            return {"ok": False, "message": "重新下载日志失败：bug 链接未解析出 project_key/work_item_id。"}
        bug_dir = self._bug_cache_dir(project_key, work_item_id)
        bug_dir.mkdir(parents=True, exist_ok=True)
        self._emit_progress(
            progress_callback,
            stage="bug_reanalysis_retry_download",
            message="上一轮没有可复用日志，尝试重新下载 bug 附件",
            project_key=project_key,
            work_item_id=work_item_id,
            bug_cache_dir=str(bug_dir),
        )
        try:
            fetched = self._run_json_command(
                [str(self._bug_fetcher_script()), "fetch-data", project_key, work_item_id],
                timeout=120,
            )
        except Exception as exc:
            return {"ok": False, "message": f"重新拉取 bug 附件列表失败：{exc}"}
        download = self._download_bug_attachments(
            project_key,
            work_item_id,
            bug_dir,
            fetched.get("attachments", []),
            timeout=self.config.bug_analysis.timeout_seconds,
        )
        selected_input = self._select_log_input(bug_dir, fetched)
        prepared_input = self._reuse_prepared_bug_input(selected_input) if selected_input else None
        if prepared_input is None and selected_input is not None:
            prepared_input = self._prepare_log_input(selected_input)
        self._write_bug_cache_metadata(
            bug_dir,
            bug_url=bug_url,
            project_key=project_key,
            work_item_id=work_item_id,
            selected_input=selected_input,
            prepared_input=prepared_input,
        )
        if prepared_input is not None:
            return {
                "ok": True,
                "message": "已重新下载并准备日志输入。",
                "selected_input": selected_input,
                "prepared_input": prepared_input,
                "download": download,
            }
        attachment_lines = self._render_attachment_lines(fetched.get("attachments", []), download)
        return {
            "ok": False,
            "message": f"重新下载日志后仍未拿到可用日志输入。\n附件结果：\n{attachment_lines}",
            "download": download,
        }

    def _has_meaningful_log_tree(self, root: Path) -> bool:
        if not root.exists():
            return False
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            lower_name = path.name.lower()
            if lower_name in {"prop.txt", "dfx.txt"}:
                continue
            if lower_name.endswith((".alog", ".xlog", ".log", ".txt", ".xp", ".zip", ".001")):
                return True
        return False

    def _is_usable_log_attachment(self, path: Path) -> bool:
        lower_name = path.name.lower()
        if lower_name.endswith(".zip") and not lower_name.endswith(".xp.zip.001"):
            return zipfile.is_zipfile(path)
        if lower_name.endswith(".xp"):
            return not self._looks_like_non_log_xp_payload(path)
        return True

    def _looks_like_non_log_xp_payload(self, path: Path) -> bool:
        try:
            head = path.read_bytes()[:16]
        except OSError:
            return False
        if any(head.startswith(prefix) for prefix in _NON_LOG_XP_MAGIC_HEADERS):
            return True
        if len(head) >= 12 and head.startswith(b"RIFF") and head[8:12] == b"WEBP":
            return True
        if len(head) >= 8 and head[4:8] == b"ftyp":
            return True
        return False

    def _expand_xp_file(self, xp_path: Path) -> Path:
        jar_path = self.config.workspace_root / ".ai/skills/log-decoder/tools/decryptFile.jar"
        completed = run_tracked_process(
            ["java", "-jar", str(jar_path), str(xp_path)],
            watchdog=self.process_watchdog,
            name="xp-log-decrypt",
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

    def _startup_analysis_input(self, input_path: Path, fault_time: str) -> Path:
        if input_path.is_dir():
            return input_path
        return self._select_startup_input(input_path, fault_time)

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
        target_time: str | None = None,
        request_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = self.build_command(
            plan=plan,
            input_path=input_path or self.config.workspace_root,
            html_path=html_path,
            json_path=json_path,
            analysis_dir=analysis_dir,
            target_time=target_time,
            request_text=request_text,
        )
        completed = run_tracked_process(
            command,
            watchdog=self.process_watchdog,
            name=f"bug-analysis-{plan.kind}",
            cwd=self._working_dir(),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if completed.returncode != 0 and plan.kind == "startup":
            completed = run_tracked_process(
                command,
                watchdog=self.process_watchdog,
                name=f"bug-analysis-{plan.kind}-retry",
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

        if plan.kind in {"stuck", "crash", "perception", "xtheme"}:
            generated_html = self._extract_report_path(completed.stdout, r"^\[OK\] 报告:\s*(.+)$")
            generated_json = self._extract_report_path(completed.stdout, r"^\[OK\] JSON:\s*(.+)$")
            if plan.kind == "perception":
                generated_html = self._extract_report_path(completed.stdout, r"^\[OK\] HTML:\s*(.+)$")
            if plan.kind == "xtheme":
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
        request_text: str,
        prompt_text: str,
        selected_input: Path | None,
        report_jsons: dict[str, Path | None],
        download: dict[str, object],
        html_paths: list[Path],
        classification_skill: str = "",
        classification_source: str = "",
        classification_reason: str = "",
        classification_provider: str = "",
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
            f"- 命中 Skill: `{classification_skill or self._skill_name_for_kind(plans[0].kind if plans else 'general')}`\n"
            f"- 分类来源: `{classification_source or 'manual_fallback'}`\n"
            f"- 分类 Agent: `{classification_provider or '无'}`\n"
            f"- 分类理由: `{classification_reason or '未记录'}`\n"
            f"- 信号代码: `{', '.join(plan.signal_code for plan in plans if plan.signal_code) or '无'}`\n"
            f"- 故障时间: `{fault_time or '未识别'}`\n"
            f"  说明: {fault_time_note}\n"
            "- 用户原始请求:\n\n```text\n"
            f"{request_text}\n"
            "```\n"
            f"- 分析请求: `{prompt_text}`\n"
            f"- 选中日志输入: `{selected_input or '无，可静态分析'}`\n"
            f"- 附件:\n{attachment_lines}\n"
            "- 缺陷描述:\n\n```text\n"
            f"{description.strip() or '(无描述)'}\n"
            "```\n\n"
            "- 本轮脚本初步摘要（仅代表本轮自动脚本输出，不代表上一轮分析结论；若与源码证据冲突，以源码与原始输入为准）:\n\n"
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
            return self._normalize_fault_time_text(match.group(1)), "从缺陷描述提取"
        direct_match = re.search(r"(20\d{2}[-_/年]\d{1,2}[-_/月]\d{1,2}[日_\s-]*\d{1,2}:\d{2})", description)
        if direct_match:
            return self._normalize_fault_time_text(direct_match.group(1)), "从文本中的完整时间戳提取"
        short_match = re.search(r"(?<!\d)(\d{1,2}:\d{2})(?!\d)", description)
        if short_match:
            return self._normalize_fault_time_text(short_match.group(1)), "从文本中的时分提取"
        title_match = re.search(r"(20\d{2})年_(\d{1,2})月(\d{1,2})日_(\d{1,2}:\d{2})", title)
        if title_match:
            return (
                self._normalize_fault_time_text(
                    f"{title_match.group(1)}-{int(title_match.group(2)):02d}-{int(title_match.group(3)):02d} {title_match.group(4)}"
                ),
                "缺陷描述中未显式提供故障时间，退回使用标题中的时间戳",
            )
        return "", "缺陷描述和标题中都未识别到明确故障时间"

    def _normalize_fault_time_text(self, value: str) -> str:
        normalized = (
            value.strip()
            .replace("：", ":")
            .replace("年", "-")
            .replace("月", "-")
            .replace("日", " ")
            .replace("/", "-")
            .replace("_", " ")
        )
        normalized = re.sub(r"\s+", " ", normalized)
        full_match = re.search(
            r"(20\d{2})-(\d{1,2})-(\d{1,2})\s*(\d{1,2}):(\d{2})(?::(\d{2}))?",
            normalized,
        )
        if full_match:
            seconds = full_match.group(6)
            base = (
                f"{int(full_match.group(1)):04d}-{int(full_match.group(2)):02d}-{int(full_match.group(3)):02d} "
                f"{int(full_match.group(4)):02d}:{int(full_match.group(5)):02d}"
            )
            if seconds is not None:
                return f"{base}:{int(seconds):02d}"
            return base
        short_match = re.search(r"(?<!\d)(\d{1,2}):(\d{2})(?::(\d{2}))?(?!\d)", normalized)
        if short_match:
            seconds = short_match.group(3)
            base = f"{int(short_match.group(1)):02d}:{int(short_match.group(2)):02d}"
            if seconds is not None:
                return f"{base}:{int(seconds):02d}"
            return base
        return normalized

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
        skipped = set(str(name) for name in download.get("skipped", []) if isinstance(name, str))
        errors = set(str(name) for name in download.get("errors", []) if isinstance(name, str))
        error_detail_map = {
            str(item.get("name") or ""): str(item.get("reason") or "")
            for item in download.get("error_details", [])
            if isinstance(item, dict)
        }
        lines: list[str] = []
        if isinstance(attachments, list):
            for item in attachments:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name", ""))
                size = str(item.get("size", ""))
                status = (
                    "已下载"
                    if name in downloaded
                    else "已跳过"
                    if name in skipped
                    else "下载失败"
                    if name in errors
                    else "未下载"
                )
                reason = error_detail_map.get(name, "")
                suffix = f" ({reason})" if reason and status == "下载失败" else ""
                lines.append(f"  - `{name}` (`{size}`) - {status}{suffix}")
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

        if plan.kind == "general":
            verdict = payload.get("verdict", {}) if isinstance(payload, dict) else {}
            msg = str(verdict.get("text") or payload.get("summary") or "已生成通用问题分析报告")
            source_matches = payload.get("source_matches", 0) if isinstance(payload, dict) else 0
            return (
                "Bug 分析完成\n"
                f"类型: {self._analysis_label(plan.kind)}\n"
                f"结论: {msg}\n"
                f"源码证据: {source_matches}\n"
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

    def _write_general_bug_report(
        self,
        *,
        html_path: Path,
        json_path: Path,
        title: str,
        description: str,
        prompt_text: str,
        request_text: str,
        fault_time: str,
        selected_input: Path | None,
        source_evidence_path: Path | None,
        classification_skill: str = "",
        classification_source: str = "",
        classification_reason: str = "",
    ) -> None:
        source_entries = self._parse_source_evidence_entries(source_evidence_path)
        source_rows = [
            (entry["file"], f"L{entry['line']}", entry["text"])
            for entry in source_entries[:12]
        ]
        has_logs = selected_input is not None
        verdict_sev = "green" if has_logs else "yellow"
        verdict_text = (
            "未命中专用日志脚本，已按通用问题分析处理；当前结论优先基于缺陷描述、源码证据和已有上下文。"
            if source_rows
            else "未命中专用日志脚本，且当前源码检索证据有限；建议补充更明确的业务关键词或现场日志后继续收敛。"
        )
        cards = [
            ("分析方式", "静态/通用", verdict_sev, "没有把未知请求强制改写成某个固定日志脚本。"),
            ("故障时间", fault_time or "未识别", "green" if fault_time else "yellow", ""),
            ("现场日志", selected_input.name if selected_input else "无，可静态分析", "green" if has_logs else "yellow", ""),
            ("源码证据", str(len(source_rows)), "green" if source_rows else "yellow", "按业务词和源码定义做本地检索。"),
            ("命中 Skill", classification_skill or "general", "green", classification_source or "manual_fallback"),
        ]
        issues = [
            {
                "sev": verdict_sev,
                "title": "路由策略",
                "detail": "当前请求未匹配 startup/stuck/crash/perception/signal 等专用脚本，因此回落到通用问题分析，而不是再默认启动时序。",
            },
            {
                "sev": "yellow" if not has_logs else "green",
                "title": "现场证据",
                "detail": "没有可复用日志时，只能基于缺陷描述和源码证据给出静态判断，不直接替代现场定案。"
                if not has_logs
                else f"当前可复用日志输入：{selected_input}",
            },
            {
                "sev": "green" if source_rows else "yellow",
                "title": "源码落点",
                "detail": f"已命中 {len(source_rows)} 条源码证据，可继续围绕这些文件追踪业务链路。"
                if source_rows
                else "当前没有检索到稳定源码落点，说明问题描述还不够具体。",
            },
        ]
        summary_sections = build_structured_summary_sections(
            conclusions=[
                {"sev": verdict_sev, "title": "通用分析结论", "detail": verdict_text},
                {
                    "sev": "green" if source_rows else "yellow",
                    "title": "源码证据状态",
                    "detail": f"已命中 {len(source_rows)} 条源码证据。" if source_rows else "当前没有命中稳定源码落点。",
                },
                {
                    "sev": "green" if has_logs else "yellow",
                    "title": "现场日志状态",
                    "detail": f"当前可复用日志输入：{selected_input}" if has_logs else "当前无可复用日志，结论不能替代现场定案。",
                },
            ],
            evidence_rows=self._general_summary_evidence_rows(
                fault_time=fault_time,
                selected_input=selected_input,
                source_rows=source_rows,
                classification_skill=classification_skill or "general",
                classification_source=classification_source or "manual_fallback",
            ),
            causes=[
                {
                    "sev": "green" if source_rows else "yellow",
                    "title": "当前可解释方向",
                    "detail": "优先围绕已命中的源码落点和业务描述继续追踪。"
                    if source_rows
                    else "缺少日志和稳定源码证据时，不强行给出具体根因。",
                },
                {
                    "sev": verdict_sev,
                    "title": "路由原因",
                    "detail": classification_reason or "未命中专用日志脚本，按通用分析处理。",
                },
            ],
            confirmations=self._general_summary_confirmations(
                fault_time=fault_time,
                has_logs=has_logs,
                source_rows=source_rows,
            ),
            actions=self._general_summary_actions(has_logs=has_logs, source_rows=source_rows),
        )
        context_rows = [
            ("Bug 标题", title or "未返回 / 未设置"),
            ("分析请求", prompt_text or "未设置"),
            ("故障时间", fault_time or "未识别"),
            ("现场日志", str(selected_input) if selected_input else "无，可静态分析"),
            ("源码证据文件", str(source_evidence_path) if source_evidence_path else "未生成"),
            ("命中 Skill", classification_skill or "general"),
            ("分类来源", classification_source or "manual_fallback"),
            ("分类理由", classification_reason or "未记录"),
        ]
        flow_nodes = [
            {"tag": "REQUEST", "title": "用户问题", "meta": prompt_text or request_text, "note": "原始问题 / 追问文本"},
            {
                "tag": "SKILL",
                "title": classification_skill or "general",
                "meta": classification_source or "manual_fallback",
                "note": classification_reason or "未记录分类理由",
            },
            {
                "tag": "INPUT",
                "title": "现场日志状态",
                "meta": str(selected_input) if selected_input else "无，可静态分析",
                "note": "需要日志的 skill 会在续聊时自动重试下载。",
            },
            {
                "tag": "SOURCE",
                "title": "源码证据",
                "meta": f"{len(source_rows)} 条命中",
                "note": str(source_evidence_path) if source_evidence_path else "未生成源码证据文件",
            },
            {"tag": "OUTPUT", "title": "结论输出", "meta": verdict_text, "note": "最终结论由本地 Agent 继续归纳。"},
        ]
        raw_description = description.strip() or "(无描述)"
        raw_request = request_text.strip() or "(无请求)"
        detail_body = (
            "<div class=\"split-grid\">"
            f"{combined_bug_html.render_table([('原始请求', raw_request)], ('字段', '内容'))}"
            f"{combined_bug_html.render_table([('缺陷描述', raw_description)], ('字段', '内容'))}"
            "</div>"
        )
        composition = ReportComposition(
            title="通用问题分析",
            heading="通用问题分析",
            subtitle=f"Bug 标题：{title or '未返回 / 未设置'}",
            verdict=ReportVerdict(sev=verdict_sev, text=verdict_text),
            cards=cards,
            sections=summary_sections
            + [
                ReportSection(kind="issues", title="当前判断", items=issues, empty_text="未生成判断"),
                ReportSection(kind="flow", title="分析路径", nodes=flow_nodes, empty_text="未生成分析路径"),
                ReportSection(kind="table", title="源码证据", cols=["文件", "行号", "内容"], rows=source_rows, empty_text="未命中源码证据"),
                ReportSection(kind="table", title="分析上下文", cols=["字段", "内容"], rows=context_rows),
                ReportSection(kind="details", title="原始输入", summary="展开查看请求与缺陷描述", body_html=detail_body),
            ],
        )
        payload = {
            "mode": "general_bug_overview",
            "summary": verdict_text,
            "verdict": {"sev": verdict_sev, "text": verdict_text},
            "fault_time": fault_time,
            "selected_input": str(selected_input) if selected_input else "",
            "source_evidence_file": str(source_evidence_path) if source_evidence_path else "",
            "source_matches": len(source_rows),
            "analysis_skill": classification_skill or "general",
            "classification_source": classification_source or "manual_fallback",
            "classification_reason": classification_reason or "",
            "title": title,
            "prompt_text": prompt_text,
            "request_text": request_text,
            "description": raw_description,
        }
        html_path.write_text(
            combined_bug_html.render_report_shell(**composition_to_renderer_payload(composition)),
            encoding="utf-8",
        )
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _general_summary_evidence_rows(
        self,
        *,
        fault_time: str,
        selected_input: Path | None,
        source_rows: list[tuple[object, ...]],
        classification_skill: str,
        classification_source: str,
    ) -> list[tuple[object, ...]]:
        rows: list[tuple[object, ...]] = []
        if fault_time:
            rows.append(("故障时间", fault_time, "用户请求/缺陷描述", "已识别分析时间点", "限定后续日志和源码追踪窗口"))
        rows.append(
            (
                "现场日志",
                str(selected_input) if selected_input else "无",
                "附件/缓存",
                "已取得可复用日志输入" if selected_input else "当前没有可复用日志输入",
                "决定结论置信边界",
            )
        )
        rows.append(("分类路由", classification_skill or "general", "bridge/agent", classification_source or "manual_fallback", "决定是否调用专用 skill"))
        for file_name, line, text in source_rows[:5]:
            rows.append(("源码", f"{file_name} {line}".strip(), "业务源码", text, "支撑静态分析落点"))
        return rows[:8]

    def _general_summary_confirmations(
        self,
        *,
        fault_time: str,
        has_logs: bool,
        source_rows: list[tuple[object, ...]],
    ) -> list[dict[str, object]]:
        items: list[dict[str, object]] = []
        if not fault_time:
            items.append({"sev": "yellow", "title": "故障时间", "detail": "未识别精确时间，后续日志分析需要先补齐时间窗口。"})
        if not has_logs:
            items.append({"sev": "yellow", "title": "现场日志", "detail": "当前无可复用日志，无法验证运行时是否真的经过源码落点。"})
        if not source_rows:
            items.append({"sev": "yellow", "title": "源码落点", "detail": "源码证据不足，需要更明确的业务词、信号名、类名或调用链线索。"})
        return items

    def _general_summary_actions(
        self,
        *,
        has_logs: bool,
        source_rows: list[tuple[object, ...]],
    ) -> list[dict[str, object]]:
        actions: list[dict[str, object]] = []
        if not has_logs:
            actions.append({"sev": "yellow", "title": "补齐或重试日志", "detail": "下一轮如果命中需要日志的 skill，bridge 会优先重试下载并报告失败原因。"})
        if source_rows:
            actions.append({"sev": "green", "title": "沿源码证据追踪", "detail": "优先从命中的源码文件继续查生产者、状态更新和消费链路。"})
        actions.append({"sev": "green", "title": "续聊时保留上下文", "detail": "后续追问继续复用当前 bug、报告、源码证据和已下载日志缓存。"})
        return actions

    def _build_combined_report_artifacts(
        self,
        *,
        plans: list["BugAnalysisPlan"],
        prompt_text: str,
        fault_time: str,
        output_dir: Path,
        html_paths: list[Path],
        report_jsons: dict[str, Path | None],
        selected_input: Path | None,
        source_evidence_path: Path | None = None,
    ) -> dict[str, object] | None:
        kinds = [plan.kind for plan in plans]
        if kinds == ["startup", "stuck"]:
            startup_json_path = report_jsons.get("startup")
            stuck_json_path = report_jsons.get("stuck")
            if startup_json_path is None or stuck_json_path is None:
                return None
            if not startup_json_path.exists() or not stuck_json_path.exists():
                return None

            startup_payload = json.loads(startup_json_path.read_text(encoding="utf-8"))
            stuck_payload = json.loads(stuck_json_path.read_text(encoding="utf-8"))
            summary = self._build_combined_summary_text(startup_payload, stuck_payload, prompt_text, fault_time)
            html_path = output_dir / self._combined_report_name("html")
            json_path = output_dir / self._combined_report_name("json")
            html_path.write_text(
                self._render_combined_startup_stuck_html(
                    startup_payload=startup_payload,
                    stuck_payload=stuck_payload,
                    prompt_text=prompt_text,
                    fault_time=fault_time,
                    startup_html=next((path for path in html_paths if path.name == self._report_name("startup", "html")), None),
                    stuck_html=next((path for path in html_paths if path.name == self._report_name("stuck", "html")), None),
                    selected_input=selected_input,
                ),
                encoding="utf-8",
            )
            combined_payload = {
                "mode": "startup_stuck_combined",
                "prompt_text": prompt_text,
                "fault_time": fault_time,
                "selected_input": str(selected_input) if selected_input else "",
                "summary": summary,
                "startup": startup_payload,
                "stuck": stuck_payload,
            }
            json_path.write_text(json.dumps(combined_payload, ensure_ascii=False, indent=2), encoding="utf-8")
            return {
                "html_path": html_path,
                "json_path": json_path,
                "summary": summary,
            }

        if kinds == ["signal"]:
            signal_json_path = report_jsons.get("signal")
            if signal_json_path is None or not signal_json_path.exists():
                return None
            signal_payload = json.loads(signal_json_path.read_text(encoding="utf-8"))
            focus_scope = self._signal_focus_scope(signal_payload)
            summary = self._build_signal_overview_summary_text(signal_payload, prompt_text, fault_time, focus_scope)
            html_path = output_dir / self._signal_overview_report_name("html")
            json_path = output_dir / self._signal_overview_report_name("json")
            html_path.write_text(
                self._render_signal_bug_overview_html(
                    signal_payload=signal_payload,
                    prompt_text=prompt_text,
                    fault_time=fault_time,
                    selected_input=selected_input,
                    source_evidence_path=source_evidence_path,
                    focus_scope=focus_scope,
                ),
                encoding="utf-8",
            )
            json_path.write_text(
                json.dumps(
                    {
                        "mode": "signal_overview_combined",
                        "prompt_text": prompt_text,
                        "fault_time": fault_time,
                        "selected_input": str(selected_input) if selected_input else "",
                        "source_evidence_file": str(source_evidence_path) if source_evidence_path else "",
                        "summary": summary,
                        "focus_scope": focus_scope,
                        "signal": signal_payload,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            return {
                "html_path": html_path,
                "json_path": json_path,
                "summary": summary,
            }

        return None

    def _build_combined_summary_text(
        self,
        startup_payload: dict[str, object],
        stuck_payload: dict[str, object],
        prompt_text: str,
        fault_time: str,
    ) -> str:
        startup_verdict = startup_payload.get("verdict", {}) if isinstance(startup_payload, dict) else {}
        stuck_target_verdict = stuck_payload.get("target_verdict", {}) if isinstance(stuck_payload, dict) else {}
        stuck_verdict = stuck_payload.get("verdict", {}) if isinstance(stuck_payload, dict) else {}
        focus_pid = startup_payload.get("focus_session_pid", "")
        boot_relation = startup_payload.get("boot_relation", {}) if isinstance(startup_payload, dict) else {}
        system_load = startup_payload.get("system_load", {}) if isinstance(startup_payload, dict) else {}
        startup_message = str(startup_verdict.get("message", "已生成启动分析"))
        if isinstance(stuck_target_verdict, dict) and stuck_target_verdict.get("message"):
            stuck_message = str(stuck_target_verdict.get("message"))
        else:
            stuck_message = str(stuck_verdict.get("verdict_msg", "已生成卡顿分析"))
        load_text = "未命中"
        if isinstance(system_load, dict) and system_load:
            load_text = (
                f"Total {system_load.get('total_cpu', '?')}% / "
                f"System {system_load.get('system_cpu', '?')}% / "
                f"iow {system_load.get('iow_cpu', '?')}%"
            )
        return (
            "Bug 分析完成\n"
            "类型: 3D启动卡顿综合报告\n"
            f"描述: {prompt_text}\n"
            f"故障时间: {fault_time or '未识别'}\n"
            f"主会话 PID: {focus_pid or '未识别'}\n"
            f"启动结论: {startup_message}\n"
            f"卡顿结论: {stuck_message}\n"
            f"ROM/Boot: {boot_relation.get('note', '未识别')}\n"
            f"启动时刻系统负载: {load_text}\n"
            f"HTML: {self._combined_report_name('html')}"
        )

    def _render_combined_startup_stuck_html(
        self,
        *,
        startup_payload: dict[str, object],
        stuck_payload: dict[str, object],
        prompt_text: str,
        fault_time: str,
        startup_html: Path | None,
        stuck_html: Path | None,
        selected_input: Path | None,
    ) -> str:
        composition = self._plan_startup_stuck_report(
            startup_payload=startup_payload,
            stuck_payload=stuck_payload,
            prompt_text=prompt_text,
            fault_time=fault_time,
            startup_html=startup_html,
            stuck_html=stuck_html,
            selected_input=selected_input,
        )
        return combined_bug_html.render_report_shell(**composition_to_renderer_payload(composition))

    def _combined_ig_text(self, ig_context: object, power_context: object) -> str:
        after = ig_context.get("after") if isinstance(ig_context, dict) else None
        if isinstance(after, dict) and after.get("timestamp") and after.get("value") is not None:
            return f"启动后最近 IG={after.get('value')} @ {after.get('timestamp')}"
        render_ctx = power_context.get("render_anomaly_context", {}) if isinstance(power_context, dict) else {}
        if isinstance(render_ctx, dict) and render_ctx.get("category"):
            return f"卡顿侧上下电分类={render_ctx.get('category')}"
        return "未识别到明确上下电样本"

    def _combined_stuck_context_text(
        self,
        stuck_payload: dict[str, object],
        target_context: object,
        app_pid_filter: object,
    ) -> str:
        stuck_verdict = stuck_payload.get("verdict", {}) if isinstance(stuck_payload, dict) else {}
        target_verdict = stuck_payload.get("target_verdict", {}) if isinstance(stuck_payload, dict) else {}
        pid_desc = ""
        if isinstance(app_pid_filter, dict) and app_pid_filter.get("selected_pid"):
            pid_desc = f"应用层 PID={app_pid_filter.get('selected_pid')}；"
        if isinstance(target_context, dict) and target_context.get("target"):
            pid_desc += f"目标时间窗={target_context.get('target')}；"
        message = (
            str(target_verdict.get("message"))
            if isinstance(target_verdict, dict) and target_verdict.get("message")
            else str(stuck_verdict.get("verdict_msg", "已生成卡顿报告"))
        )
        return pid_desc + message

    def _plan_startup_stuck_report(
        self,
        *,
        startup_payload: dict[str, object],
        stuck_payload: dict[str, object],
        prompt_text: str,
        fault_time: str,
        startup_html: Path | None,
        stuck_html: Path | None,
        selected_input: Path | None,
    ) -> ReportComposition:
        startup_verdict = startup_payload.get("verdict", {}) if isinstance(startup_payload, dict) else {}
        stuck_target_verdict = stuck_payload.get("target_verdict", {}) if isinstance(stuck_payload, dict) else {}
        stuck_verdict = stuck_payload.get("verdict", {}) if isinstance(stuck_payload, dict) else {}
        startup_message = str(startup_verdict.get("message", "已生成启动分析"))
        startup_sev = str(startup_verdict.get("severity", "yellow"))
        stuck_message = (
            str(stuck_target_verdict.get("message"))
            if isinstance(stuck_target_verdict, dict) and stuck_target_verdict.get("message")
            else str(stuck_verdict.get("verdict_msg", "已生成卡顿分析"))
        )
        stuck_sev = (
            str(stuck_target_verdict.get("sev", "yellow"))
            if isinstance(stuck_target_verdict, dict) and stuck_target_verdict.get("sev")
            else str(stuck_verdict.get("verdict_sev", "yellow"))
        )
        focus_pid = startup_payload.get("focus_session_pid", "")
        focus_session_index = startup_payload.get("focus_session_index", "")
        boot_relation = startup_payload.get("boot_relation", {}) if isinstance(startup_payload, dict) else {}
        system_load = startup_payload.get("system_load", {}) if isinstance(startup_payload, dict) else {}
        ig_context = startup_payload.get("ig_context", {}) if isinstance(startup_payload, dict) else {}
        power_context = stuck_payload.get("power_context", {}) if isinstance(stuck_payload, dict) else {}
        app_pid_filter = stuck_payload.get("app_pid_filter", {}) if isinstance(stuck_payload, dict) else {}
        target_context = stuck_payload.get("target_context", {}) if isinstance(stuck_payload, dict) else {}
        cards = [
            ("分析类型", "3D启动卡顿综合", "green", "启动链路与卡顿窗口合并输出"),
            ("故障时间", fault_time or "未识别", "green" if fault_time else "yellow", ""),
            ("主会话 PID", focus_pid or "未识别", "green" if focus_pid else "yellow", f"Session {focus_session_index or '?'}"),
            ("ROM 启动邻近", "是" if boot_relation.get("is_near_boot") else "否", "yellow" if boot_relation.get("is_near_boot") else "green", str(boot_relation.get("note", ""))),
            (
                "启动时刻系统负载",
                (
                    f"Total {system_load.get('total_cpu', '?')}% / iow {system_load.get('iow_cpu', '?')}%"
                    if isinstance(system_load, dict) and system_load
                    else "未命中"
                ),
                "yellow" if isinstance(system_load, dict) and int(system_load.get("total_cpu", 0) or 0) >= 70 else "green",
                (
                    f"进程 CPU {system_load.get('process_cpu', '?')}% / RSS {system_load.get('process_mem_rss_kb', '?')}KB"
                    if isinstance(system_load, dict) and system_load
                    else ""
                ),
            ),
            ("启动链路", startup_sev.upper(), startup_sev, startup_message),
            ("卡顿窗口", stuck_sev.upper(), stuck_sev, stuck_message),
        ]
        issues: list[dict[str, object]] = []
        for item in startup_verdict.get("issues", []) if isinstance(startup_verdict, dict) else []:
            if isinstance(item, dict):
                issues.append(item)
        if isinstance(stuck_target_verdict, dict) and stuck_target_verdict.get("message"):
            issues.append({"sev": stuck_sev, "title": "目标时间窗卡顿结论", "detail": stuck_message})
        elif isinstance(stuck_verdict, dict) and stuck_verdict.get("verdict_msg"):
            issues.append({"sev": stuck_sev, "title": "卡顿结论", "detail": stuck_message})
        chain_nodes = [
            {
                "sev": "green" if focus_pid else "yellow",
                "title": "目标时间锁主会话与主 PID",
                "evidence": f"故障时间 {fault_time or '未识别'} -> Session {focus_session_index or '?'} / PID {focus_pid or '未识别'}",
                "downstream": "后续启动链与卡顿证据统一围绕同一主会话展开，避免把恢复后的新进程混入。",
            },
            {
                "sev": "yellow" if boot_relation.get("is_near_boot") else "green",
                "title": "ROM 启动邻近与上下电上下文",
                "evidence": (
                    str(boot_relation.get("note", "未识别"))
                    + "；"
                    + self._combined_ig_text(ig_context, power_context)
                ),
                "downstream": "如果问题发生在整机刚启动或特殊上下电阶段，启动卡顿结论需要附带环境说明，避免误判稳定期异常。",
            },
            {
                "sev": startup_sev,
                "title": "启动链路主卡点",
                "evidence": startup_message,
                "downstream": "用于判断 Application / Surface / UnityReady / 首帧 哪一段真正断开。",
            },
            {
                "sev": stuck_sev,
                "title": "卡顿窗口系统与渲染压力",
                "evidence": self._combined_stuck_context_text(stuck_payload, target_context, app_pid_filter),
                "downstream": "补充目标时间窗内的 Watchdog / UnityRequest / 系统 iow / CPU 压力，判断是不是启动后继续卡住。",
            },
        ]
        target_rows = [
            ("故障时间", fault_time or "未识别"),
            ("主会话 PID", str(focus_pid or "未识别")),
            ("会话选择", str(startup_payload.get("focus_reason", "未记录"))),
            ("启动报告", startup_html.name if startup_html else self._report_name("startup", "html")),
            ("卡顿报告", stuck_html.name if stuck_html else self._report_name("stuck", "html")),
            ("原始日志输入", str(selected_input or "")),
        ]
        system_rows = []
        if isinstance(system_load, dict) and system_load:
            system_rows.append(
                (
                    system_load.get("timestamp", ""),
                    f"{system_load.get('total_cpu', '?')}%",
                    f"{system_load.get('user_cpu', '?')}%",
                    f"{system_load.get('system_cpu', '?')}%",
                    f"{system_load.get('iow_cpu', '?')}%",
                    f"{system_load.get('process_cpu', '?')}%",
                )
            )
        return plan_startup_stuck_report(
            prompt_text=combined_bug_html.H(prompt_text),
            fault_time=fault_time,
            startup_html_name=combined_bug_html.H(startup_html or self._report_name("startup", "html")),
            stuck_html_name=combined_bug_html.H(stuck_html or self._report_name("stuck", "html")),
            selected_input=str(selected_input or ""),
            startup_message=startup_message,
            startup_sev=startup_sev,
            stuck_message=stuck_message,
            stuck_sev=stuck_sev,
            focus_pid=str(focus_pid or ""),
            focus_session_index=str(focus_session_index or ""),
            boot_relation_is_near_boot=bool(boot_relation.get("is_near_boot")),
            boot_relation_note=str(boot_relation.get("note", "")),
            startup_load_value=(
                f"Total {system_load.get('total_cpu', '?')}% / iow {system_load.get('iow_cpu', '?')}%"
                if isinstance(system_load, dict) and system_load
                else "未命中"
            ),
            startup_load_desc=(
                f"进程 CPU {system_load.get('process_cpu', '?')}% / RSS {system_load.get('process_mem_rss_kb', '?')}KB"
                if isinstance(system_load, dict) and system_load
                else ""
            ),
            startup_load_sev="yellow" if isinstance(system_load, dict) and int(system_load.get("total_cpu", 0) or 0) >= 70 else "green",
            issues=issues,
            chain_nodes=chain_nodes,
            target_rows=target_rows,
            system_rows=system_rows,
            system_cols=["时间", "Total", "User", "System", "iow", "进程 CPU"],
        )

    def _plan_signal_report(
        self,
        *,
        signal_payload: dict[str, object],
        prompt_text: str,
        fault_time: str,
        selected_input: Path | None,
        source_evidence_path: Path | None,
        focus_scope: dict[str, object],
    ) -> ReportComposition:
        signal = signal_payload.get("signal", {}) if isinstance(signal_payload, dict) else {}
        signal_name = str(signal.get("name") or signal.get("code") or "未知信号")
        signal_code = str(signal.get("code") or "")
        signal_comment = str(signal.get("comment") or "")
        package = str(focus_scope.get("package") or "未识别")
        pid = str(focus_scope.get("pid") or "未识别")
        time_window = focus_scope.get("time_window", {}) if isinstance(focus_scope, dict) else {}
        alignment = self._signal_fault_alignment(fault_time, focus_scope)
        lifecycle_nodes = self._signal_lifecycle_nodes(signal_payload, focus_scope, fault_time, source_evidence_path)
        dataflow_nodes = self._signal_dataflow_nodes(signal_payload, prompt_text, source_evidence_path)
        evidence_rows = self._signal_evidence_rows(signal_payload, focus_scope)
        boundary_issues = self._signal_boundary_issues(signal_payload, focus_scope, fault_time)
        source_rows = self._signal_source_rows(signal_payload, source_evidence_path)
        summary_text = str(signal_payload.get("summary") or "").strip()
        visible_scope = self._signal_visible_scope_text(signal_payload, focus_scope)
        verdict_sev = alignment["sev"]
        if verdict_sev == "green" and any(issue.get("sev") == "yellow" for issue in boundary_issues):
            verdict_sev = "yellow"
        cards = [
            ("信号", signal_code or signal_name, "green", signal_name if signal_code else signal_comment),
            ("焦点进程", package, "green" if package != "未识别" else "yellow", f"PID {pid}" if pid != "未识别" else ""),
            ("日志时间窗", str(time_window.get("display") or "未识别"), "green" if time_window else "yellow", ""),
            ("现场一致性", alignment["label"], alignment["sev"], alignment["detail"]),
            ("进程内可见性", visible_scope, "green" if "已看到" in visible_scope else "yellow", ""),
            ("原始输入", str(selected_input or "无"), "green" if selected_input else "yellow", ""),
        ]
        title_suffix = signal_comment or signal_name
        sections = [
            ReportSection(kind="text", title="一句话判断", text=alignment["judgement"]),
            ReportSection(
                kind="flow",
                title="生命周期流图",
                description="只围绕当前可见的 package / PID 组织，避免把别的进程混进来。",
                nodes=lifecycle_nodes,
            ),
            ReportSection(
                kind="flow",
                title="数据流图",
                description="先看源码映射和分发，再看业务消费与最终判定落点。",
                nodes=dataflow_nodes,
            ),
            ReportSection(
                kind="table",
                title="关键日志证据",
                rows=evidence_rows,
                cols=["时间", "相对启动", "进程/线程", "阶段", "证据"],
                empty_text="未提取到可用日志证据",
            ),
            ReportSection(
                kind="issues",
                title="证据边界与未覆盖段",
                items=boundary_issues,
                empty_text="未识别明显边界问题",
            ),
        ]
        if summary_text or source_rows:
            detail_blocks: list[str] = []
            if summary_text:
                detail_blocks.append(f'<div class="insight" style="margin-top:12px">{combined_bug_html.H(summary_text)}</div>')
            if source_rows:
                detail_blocks.append(
                    combined_bug_html.render_table(
                        source_rows,
                        ["层级", "位置", "说明"],
                        empty_text="未提取到额外源码引用",
                    )
                )
            sections.append(
                ReportSection(
                    kind="details",
                    title="补充证据",
                    summary="展开脚本结论与源码引用",
                    body_html="".join(detail_blocks),
                )
            )
        return plan_signal_report(
            title_suffix=title_suffix,
            prompt_text=combined_bug_html.H(prompt_text),
            raw_signal_report_name=combined_bug_html.H(self._report_name("signal", "html")),
            verdict_sev=verdict_sev,
            verdict_text=alignment["headline"],
            judgement_text=alignment["judgement"],
            cards=cards,
            lifecycle_nodes=lifecycle_nodes,
            dataflow_nodes=dataflow_nodes,
            evidence_rows=evidence_rows,
            boundary_issues=boundary_issues,
            summary_text=summary_text,
            source_rows=source_rows,
            render_summary_html=lambda summary: f'<div class="insight" style="margin-top:12px">{combined_bug_html.H(summary)}</div>',
            render_source_rows_html=lambda rows: combined_bug_html.render_table(
                rows,
                ["层级", "位置", "说明"],
                empty_text="未提取到额外源码引用",
            ),
        )

    def _signal_overview_report_name(self, suffix: str) -> str:
        return f"bug_signal_overview_report.{suffix}"

    def _build_signal_overview_summary_text(
        self,
        signal_payload: dict[str, object],
        prompt_text: str,
        fault_time: str,
        focus_scope: dict[str, object],
    ) -> str:
        signal = signal_payload.get("signal", {}) if isinstance(signal_payload, dict) else {}
        signal_name = str(signal.get("name") or signal.get("code") or "未知信号")
        signal_code = str(signal.get("code") or "")
        package = str(focus_scope.get("package") or "未识别")
        pid = str(focus_scope.get("pid") or "未识别")
        time_window = focus_scope.get("time_window", {}) if isinstance(focus_scope, dict) else {}
        start = str(time_window.get("start") or "")
        end = str(time_window.get("end") or "")
        alignment = self._signal_fault_alignment(fault_time, focus_scope)
        boundary = self._signal_boundary_issues(signal_payload, focus_scope, fault_time)
        top_issue = boundary[0]["detail"] if boundary else "已生成信号链路总览报告。"
        coverage_note = self._signal_coverage_note(signal_payload, focus_scope)
        time_note = "未识别"
        if start and end:
            time_note = f"{start} ~ {end}"
        elif start:
            time_note = start
        return (
            "Bug 分析完成\n"
            "类型: 信号链路总览报告\n"
            f"描述: {prompt_text}\n"
            f"信号: {signal_code or '-'} {signal_name}\n"
            f"焦点进程: {package} / PID {pid}\n"
            f"日志时间窗: {time_note}\n"
            f"现场一致性: {alignment['label']}\n"
            f"当前判断: {top_issue}\n"
            f"进程内链路: {coverage_note}\n"
            f"HTML: {self._signal_overview_report_name('html')}"
        )

    def _render_signal_bug_overview_html(
        self,
        *,
        signal_payload: dict[str, object],
        prompt_text: str,
        fault_time: str,
        selected_input: Path | None,
        source_evidence_path: Path | None,
        focus_scope: dict[str, object],
    ) -> str:
        composition = self._plan_signal_report(
            signal_payload=signal_payload,
            prompt_text=prompt_text,
            fault_time=fault_time,
            selected_input=selected_input,
            source_evidence_path=source_evidence_path,
            focus_scope=focus_scope,
        )
        return combined_bug_html.render_report_shell(**composition_to_renderer_payload(composition))

    def _signal_focus_scope(self, signal_payload: dict[str, object]) -> dict[str, object]:
        evidence_items = self._signal_evidence_items(signal_payload)
        counts: dict[tuple[str, str], int] = {}
        for item in evidence_items:
            package = self._signal_package_from_path(str(item.get("file") or ""))
            pid = self._signal_pid_from_text(str(item.get("text") or ""))
            if not package and not pid:
                continue
            counts[(package, pid)] = counts.get((package, pid), 0) + 1
        package = ""
        pid = ""
        if counts:
            (package, pid), _ = sorted(
                counts.items(),
                key=lambda item: (item[1], bool(item[0][0]), bool(item[0][1]), item[0][0], item[0][1]),
                reverse=True,
            )[0]
        reference_date = self._signal_reference_date(evidence_items)
        time_window = self._signal_time_window(evidence_items, reference_date, package, pid)
        return {
            "package": package,
            "pid": pid,
            "reference_date": reference_date,
            "time_window": time_window,
        }

    def _signal_evidence_items(self, signal_payload: dict[str, object]) -> list[dict[str, object]]:
        items: list[dict[str, object]] = []
        lifecycle = signal_payload.get("lifecycle_report", {}) if isinstance(signal_payload, dict) else {}
        runtime = lifecycle.get("runtime", {}) if isinstance(lifecycle, dict) else {}
        for event in runtime.get("events", []) if isinstance(runtime, dict) else []:
            if not isinstance(event, dict):
                continue
            items.append(
                {
                    "stage": "lifecycle",
                    "title": str(event.get("label") or "生命周期"),
                    "file": str(event.get("file") or ""),
                    "line": event.get("line"),
                    "text": str(event.get("text") or ""),
                    "time": str(event.get("time") or ""),
                    "delta": str(event.get("delta") or ""),
                }
            )
        log_report = signal_payload.get("log_report", {}) if isinstance(signal_payload, dict) else {}
        stages = log_report.get("stages", {}) if isinstance(log_report, dict) else {}
        for stage_key, stage_payload in stages.items():
            if not isinstance(stage_payload, dict):
                continue
            title = str(stage_payload.get("title") or stage_key)
            for example in stage_payload.get("examples", []) or []:
                if not isinstance(example, dict):
                    continue
                items.append(
                    {
                        "stage": str(stage_key),
                        "title": title,
                        "file": str(example.get("file") or ""),
                        "line": example.get("line"),
                        "text": str(example.get("text") or ""),
                    }
                )
        return items

    def _signal_reference_date(self, evidence_items: list[dict[str, object]]) -> str:
        for item in evidence_items:
            for value in (str(item.get("text") or ""), str(item.get("file") or ""), str(item.get("time") or "")):
                match = re.search(r"(20\d{2}-\d{2}-\d{2})", value)
                if match:
                    return match.group(1)
        return ""

    def _signal_time_window(
        self,
        evidence_items: list[dict[str, object]],
        reference_date: str,
        package: str,
        pid: str,
    ) -> dict[str, object]:
        matched: list[datetime] = []
        for item in evidence_items:
            if package:
                item_package = self._signal_package_from_path(str(item.get("file") or ""))
                if item_package and item_package != package:
                    continue
            if pid:
                item_pid = self._signal_pid_from_text(str(item.get("text") or ""))
                if item_pid and item_pid != pid:
                    continue
            timestamp = self._signal_parse_datetime(
                text=str(item.get("text") or ""),
                time_text=str(item.get("time") or ""),
                reference_date=reference_date,
            )
            if timestamp is not None:
                matched.append(timestamp)
        if not matched:
            return {}
        matched.sort()
        start = matched[0]
        end = matched[-1]
        display = self._signal_format_datetime(start)
        if end != start:
            display += " ~ " + self._signal_format_datetime(end)
        return {
            "start": self._signal_format_datetime(start),
            "end": self._signal_format_datetime(end),
            "display": display,
        }

    def _signal_parse_datetime(self, *, text: str, time_text: str, reference_date: str) -> datetime | None:
        full_match = re.search(r"\[(20\d{2}-\d{2}-\d{2}) \+\d{4} (\d{2}:\d{2}:\d{2})\]", text)
        if full_match:
            try:
                return datetime.strptime(f"{full_match.group(1)} {full_match.group(2)}", "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return None
        value = time_text or text
        month_day_match = re.search(r"(\d{2})-(\d{2}) (\d{2}:\d{2}:\d{2}(?:\.\d{3})?)", value)
        if not month_day_match or not reference_date:
            return None
        dt_text = f"{reference_date[:4]}-{month_day_match.group(1)}-{month_day_match.group(2)} {month_day_match.group(3)}"
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(dt_text, fmt)
            except ValueError:
                continue
        return None

    def _signal_format_datetime(self, value: datetime) -> str:
        if value.microsecond:
            return value.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        return value.strftime("%Y-%m-%d %H:%M:%S")

    def _signal_fault_alignment(self, fault_time: str, focus_scope: dict[str, object]) -> dict[str, str]:
        time_window = focus_scope.get("time_window", {}) if isinstance(focus_scope, dict) else {}
        start = str(time_window.get("start") or "")
        if not fault_time:
            return {
                "sev": "yellow",
                "label": "未识别",
                "detail": "请求里没有可比对的故障时间。",
                "headline": "当前报告只说明可见日志内的链路状态，无法和现场时间做严格对齐。",
                "judgement": "没有可比对的故障时间，只能把这份报告当作日志样本说明，不应直接当成现场定案。",
            }
        if not start:
            return {
                "sev": "yellow",
                "label": "未知",
                "detail": "当前报告没有解析出明确日志时间窗。",
                "headline": "当前报告缺少明确日志时间窗，不能直接拿来证明现场结论。",
                "judgement": "需要补充目标时间窗日志，才能把这份链路报告和现场结论绑定起来。",
            }
        fault_hour = fault_time[:13]
        start_hour = start[:13]
        if fault_hour == start_hour:
            return {
                "sev": "green",
                "label": "一致",
                "detail": f"日志时间窗命中了请求故障小时 {fault_hour}。",
                "headline": "当前日志时间窗和请求故障时间一致，可以把下面的链路证据直接用于现场判断。",
                "judgement": "时间窗一致，这份报告可直接回答“现场这条链路当时有没有走通”。",
            }
        if fault_time[:10] == start[:10]:
            return {
                "sev": "yellow",
                "label": "同日不同小时",
                "detail": f"请求故障时间是 {fault_time}，当前日志主时间窗是 {start}。",
                "headline": "当前日志和请求是同一天，但不是同一小时，结论只能作为同版本同流程样本。",
                "judgement": "这能说明代码和样本日志里的链路行为，但还不能直接证明目标时刻的现场现象。",
            }
        return {
            "sev": "yellow",
            "label": "不一致",
            "detail": f"请求故障时间是 {fault_time}，当前日志主时间窗是 {start}。",
            "headline": "当前可用日志不是目标现场时间窗，下面的证据更适合回答“链路设计和样本运行是否走通”，不适合直接下现场定案。",
            "judgement": "这份报告最能说明的是样本日志里目标信号在当前焦点进程内走到了哪里，而不是请求里的目标时刻一定发生了什么。",
        }

    def _signal_lifecycle_nodes(
        self,
        signal_payload: dict[str, object],
        focus_scope: dict[str, object],
        fault_time: str,
        source_evidence_path: Path | None,
    ) -> list[dict[str, str]]:
        package = str(focus_scope.get("package") or "")
        pid = str(focus_scope.get("pid") or "")
        reference_date = str(focus_scope.get("reference_date") or "")
        start_dt = None
        nodes: list[dict[str, str]] = []
        for event in self._signal_runtime_events(signal_payload):
            if not self._signal_item_matches_scope(event, package, pid):
                continue
            label = str(event.get("label") or "")
            text = str(event.get("text") or "")
            if label == "进程启动":
                start_dt = self._signal_parse_datetime(text=text, time_text=str(event.get("time") or ""), reference_date=reference_date)
                nodes.append(
                    {
                        "tag": "日志",
                        "title": f"{package or '目标进程'} 启动",
                        "meta": f"{event.get('time') or ''} · PID {pid or self._signal_pid_from_text(text) or '未识别'}",
                        "note": text,
                    }
                )
                break
        for keyword, title in (
            ("injectSignalProvider", self._signal_provider_injection_title(signal_payload)),
            ("registerSignal", self._signal_registration_title(signal_payload)),
        ):
            match = self._signal_find_runtime_event(signal_payload, package, pid, keyword)
            if match is None:
                continue
            nodes.append(
                {
                    "tag": "日志",
                    "title": title,
                    "meta": self._signal_meta_from_item(match, start_dt, reference_date),
                    "note": str(match.get("text") or ""),
                }
            )
        for stage_key, title in (
            ("datacenter", self._signal_datacenter_stage_title(signal_payload)),
            ("android_business", self._signal_business_stage_title(signal_payload)),
        ):
            match = self._signal_find_stage_example(signal_payload, stage_key, package, pid, prefer_hmi=False)
            if match is None:
                continue
            nodes.append(
                {
                    "tag": "日志",
                    "title": title,
                    "meta": self._signal_meta_from_item(match, start_dt, reference_date),
                    "note": str(match.get("text") or ""),
                }
            )
        hmi_match = self._signal_find_stage_example(signal_payload, "android_business", package, pid, prefer_hmi=True)
        if hmi_match is not None:
            nodes.append(
                {
                    "tag": "日志",
                    "title": self._signal_consumer_stage_title(hmi_match),
                    "meta": self._signal_meta_from_item(hmi_match, start_dt, reference_date),
                    "note": str(hmi_match.get("text") or ""),
                }
            )
        business_entry = self._signal_select_business_entry(source_evidence_path, prompt_text=fault_time)
        if business_entry is not None:
            nodes.append(
                {
                    "tag": "源码",
                    "title": "业务判定落点",
                    "meta": f"{business_entry['file']}:{business_entry['line']}",
                    "note": business_entry["text"],
                }
            )
        return nodes[:6]

    def _signal_dataflow_nodes(
        self,
        signal_payload: dict[str, object],
        prompt_text: str,
        source_evidence_path: Path | None,
    ) -> list[dict[str, str]]:
        refs = signal_payload.get("source_references", []) if isinstance(signal_payload, dict) else []
        lifecycle = signal_payload.get("lifecycle_report", {}) if isinstance(signal_payload, dict) else {}
        lifecycle_refs = lifecycle.get("source_references", []) if isinstance(lifecycle, dict) else []
        chain_edges = signal_payload.get("chain_edges", []) if isinstance(signal_payload, dict) else []
        nodes: list[dict[str, str]] = []
        source_edge = None
        context = lifecycle.get("context", {}) if isinstance(lifecycle, dict) else {}
        if isinstance(context, dict):
            candidate = context.get("source_edge")
            if isinstance(candidate, dict):
                source_edge = candidate
        if source_edge is None:
            for edge in chain_edges:
                if isinstance(edge, dict) and str(edge.get("source") or "").startswith("CARSERVICE:"):
                    source_edge = edge
                    break
        if isinstance(source_edge, dict):
            nodes.append(
                {
                    "tag": "映射",
                    "title": str(source_edge.get("source") or "上游信号"),
                    "meta": f"{source_edge.get('file') or ''}:{source_edge.get('line') or ''}",
                    "note": str(source_edge.get("note") or ""),
                }
            )
        on_change_ref = self._signal_find_source_reference(
            lifecycle_refs,
            lambda file_text, line_text: "carvcuhelper.kt" in file_text and "onchangeevent" in line_text,
        )
        if on_change_ref is not None:
            nodes.append(
                {
                    "tag": "Helper",
                    "title": "CarVcuHelper.onChangeEvent",
                    "meta": f"{on_change_ref['file']}:{on_change_ref['line']}",
                    "note": on_change_ref["text"],
                }
            )
        data_center_edge = None
        for edge in chain_edges:
            if isinstance(edge, dict) and "datacenter" in str(edge.get("target") or "").casefold():
                data_center_edge = edge
                break
        if isinstance(data_center_edge, dict):
            nodes.append(
                {
                    "tag": "分发",
                    "title": self._signal_data_center_title(),
                    "meta": f"{data_center_edge.get('file') or ''}:{data_center_edge.get('line') or ''}",
                    "note": str(data_center_edge.get("note") or ""),
                }
            )
        consumer_ref = self._signal_select_consumer_reference(refs)
        if consumer_ref is not None:
            nodes.append(
                {
                    "tag": "业务",
                    "title": self._signal_consumer_reference_title(consumer_ref),
                    "meta": f"{consumer_ref['file']}:{consumer_ref['line']}",
                    "note": consumer_ref["text"],
                }
            )
        hmi_ref = self._signal_select_state_receiver_reference(refs)
        if hmi_ref is not None:
            nodes.append(
                {
                    "tag": "HMI",
                    "title": self._signal_consumer_reference_title(hmi_ref),
                    "meta": f"{hmi_ref['file']}:{hmi_ref['line']}",
                    "note": hmi_ref["text"],
                }
            )
        business_entry = self._signal_select_business_entry(source_evidence_path, prompt_text=prompt_text)
        if business_entry is not None:
            nodes.append(
                {
                    "tag": "落点",
                    "title": Path(business_entry["file"]).stem,
                    "meta": f"{business_entry['file']}:{business_entry['line']}",
                    "note": business_entry["text"],
                }
            )
        return nodes[:6]

    def _signal_runtime_events(self, signal_payload: dict[str, object]) -> list[dict[str, object]]:
        lifecycle = signal_payload.get("lifecycle_report", {}) if isinstance(signal_payload, dict) else {}
        runtime = lifecycle.get("runtime", {}) if isinstance(lifecycle, dict) else {}
        events = runtime.get("events", []) if isinstance(runtime, dict) else []
        return [event for event in events if isinstance(event, dict)]

    def _signal_item_matches_scope(self, item: dict[str, object], package: str, pid: str) -> bool:
        if package:
            item_package = self._signal_package_from_path(str(item.get("file") or ""))
            if item_package and item_package != package:
                return False
        if pid:
            item_pid = self._signal_pid_from_text(str(item.get("text") or ""))
            if item_pid and item_pid != pid:
                return False
        return True

    def _signal_find_runtime_event(
        self,
        signal_payload: dict[str, object],
        package: str,
        pid: str,
        keyword: str,
    ) -> dict[str, object] | None:
        lowered_keyword = keyword.casefold()
        for event in self._signal_runtime_events(signal_payload):
            if not self._signal_item_matches_scope(event, package, pid):
                continue
            haystack = f"{event.get('label') or ''}\n{event.get('text') or ''}".casefold()
            if lowered_keyword in haystack:
                return event
        return None

    def _signal_find_stage_example(
        self,
        signal_payload: dict[str, object],
        stage_key: str,
        package: str,
        pid: str,
        *,
        prefer_hmi: bool,
    ) -> dict[str, object] | None:
        log_report = signal_payload.get("log_report", {}) if isinstance(signal_payload, dict) else {}
        stages = log_report.get("stages", {}) if isinstance(log_report, dict) else {}
        stage = stages.get(stage_key) if isinstance(stages, dict) else None
        if not isinstance(stage, dict):
            return None
        for example in stage.get("examples", []) or []:
            if not isinstance(example, dict):
                continue
            if package:
                item_package = self._signal_package_from_path(str(example.get("file") or ""))
                if item_package and item_package != package:
                    continue
            if pid:
                item_pid = self._signal_pid_from_text(str(example.get("text") or ""))
                if item_pid and item_pid != pid:
                    continue
            text = str(example.get("text") or "")
            is_hmi = "hmi" in text.casefold() or "update battery level" in text.casefold()
            if prefer_hmi and not is_hmi:
                continue
            if not prefer_hmi and is_hmi and stage_key == "android_business":
                continue
            return example
        return None

    def _signal_meta_from_item(
        self,
        item: dict[str, object],
        start_dt: datetime | None,
        reference_date: str,
    ) -> str:
        timestamp = self._signal_parse_datetime(
            text=str(item.get("text") or ""),
            time_text=str(item.get("time") or ""),
            reference_date=reference_date,
        )
        when = str(item.get("time") or "")
        if not when and timestamp is not None:
            when = self._signal_format_datetime(timestamp)
        pid = self._signal_pid_from_text(str(item.get("text") or ""))
        tid = self._signal_tid_from_text(str(item.get("text") or ""))
        meta = when
        if start_dt is not None and timestamp is not None:
            meta = f"{when} · {self._signal_relative_text(start_dt, timestamp)}"
        if pid:
            meta += f" · PID {pid}"
        if tid:
            meta += f" / TID {tid}"
        return meta.strip(" ·")

    def _signal_relative_text(self, start_dt: datetime, current_dt: datetime) -> str:
        delta = max(0.0, (current_dt - start_dt).total_seconds())
        if delta < 1:
            return f"+{int(delta * 1000)}ms"
        return f"+{delta:.3f}s"

    def _signal_evidence_rows(
        self,
        signal_payload: dict[str, object],
        focus_scope: dict[str, object],
    ) -> list[tuple[str, str, str, str, str]]:
        package = str(focus_scope.get("package") or "")
        pid = str(focus_scope.get("pid") or "")
        reference_date = str(focus_scope.get("reference_date") or "")
        start_dt = None
        start_event = self._signal_find_runtime_event(signal_payload, package, pid, "process begin")
        if start_event is not None:
            start_dt = self._signal_parse_datetime(
                text=str(start_event.get("text") or ""),
                time_text=str(start_event.get("time") or ""),
                reference_date=reference_date,
            )
        items: list[dict[str, object]] = []
        for event in self._signal_runtime_events(signal_payload):
            if self._signal_item_matches_scope(event, package, pid):
                items.append(event)
        for stage_key in ("datacenter", "android_business", "other"):
            match = self._signal_find_stage_example(signal_payload, stage_key, package, pid, prefer_hmi=False)
            if match is not None:
                items.append(match)
        hmi = self._signal_find_stage_example(signal_payload, "android_business", package, pid, prefer_hmi=True)
        if hmi is not None:
            items.append(hmi)
        rows: list[tuple[str, str, str, str, str]] = []
        seen: set[str] = set()
        for item in items:
            text = str(item.get("text") or "")
            key = f"{item.get('line')}::{text}"
            if key in seen:
                continue
            seen.add(key)
            timestamp = self._signal_parse_datetime(
                text=text,
                time_text=str(item.get("time") or ""),
                reference_date=reference_date,
            )
            when = str(item.get("time") or (self._signal_format_datetime(timestamp) if timestamp is not None else ""))
            relative = "-"
            if start_dt is not None and timestamp is not None:
                relative = self._signal_relative_text(start_dt, timestamp)
            row_pid = self._signal_pid_from_text(text) or pid or "-"
            row_tid = self._signal_tid_from_text(text)
            pid_tid = f"PID {row_pid}"
            if row_tid:
                pid_tid += f" / TID {row_tid}"
            rows.append(
                (
                    when or "-",
                    relative,
                    pid_tid,
                    self._signal_stage_label(item),
                    self._signal_shorten(text, 120),
                )
            )
        return rows[:8]

    def _signal_stage_label(self, item: dict[str, object]) -> str:
        label = str(item.get("label") or item.get("title") or "")
        text = str(item.get("text") or "")
        lowered = text.casefold()
        if "injectsignalprovider" in lowered:
            return "注入 Provider"
        if "registersignal" in lowered:
            return "注册信号"
        if "getsignalflow" in lowered:
            return "DataCenter 取流"
        generic_label = self._signal_log_semantic_label(text)
        if generic_label:
            return generic_label
        return label or "日志命中"

    def _signal_boundary_issues(
        self,
        signal_payload: dict[str, object],
        focus_scope: dict[str, object],
        fault_time: str,
    ) -> list[dict[str, object]]:
        log_report = signal_payload.get("log_report", {}) if isinstance(signal_payload, dict) else {}
        stages = log_report.get("stages", {}) if isinstance(log_report, dict) else {}
        datacenter_hits = self._signal_stage_hits(stages, "datacenter")
        business_hits = self._signal_stage_hits(stages, "android_business")
        vhal_hits = self._signal_stage_hits(stages, "vhal")
        unity_hits = self._signal_stage_hits(stages, "unity_received")
        alignment = self._signal_fault_alignment(fault_time, focus_scope)
        issues = [
            {
                "sev": "green" if datacenter_hits and business_hits else "yellow",
                "title": "当前日志能证明的范围",
                "detail": self._signal_visible_scope_text(signal_payload, focus_scope),
            },
            {
                "sev": alignment["sev"],
                "title": "当前日志不能直接证明现场",
                "detail": alignment["detail"],
            },
            {
                "sev": "yellow",
                "title": "未覆盖段",
                "detail": (
                    f"VHAL/CarService 原始输入命中 {vhal_hits}，Unity 接收命中 {unity_hits}。"
                    " 现有样本更适合判断焦点进程内是否走通，不适合下跨层丢失定案。"
                ),
            },
        ]
        stats = signal_payload.get("detailed_stats", {}) if isinstance(signal_payload, dict) else {}
        signal_code = str((signal_payload.get("signal", {}) or {}).get("code") or "")
        stat = stats.get(signal_code) if isinstance(stats, dict) else None
        if isinstance(stat, dict):
            issues.append(
                {
                    "sev": "yellow",
                    "title": "为什么不再单独做“损失分析”结论",
                    "detail": (
                        f"当前 drop / unity counter 基本为 0（unity_drop_total={stat.get('unity_drop_total', 0)}，"
                        f"unity_recv_total={stat.get('unity_recv_total', 0)}），这更像“没有足够下游埋点”而不是“已经证明无损失”。"
                    ),
                }
            )
        return issues

    def _signal_visible_scope_text(self, signal_payload: dict[str, object], focus_scope: dict[str, object]) -> str:
        log_report = signal_payload.get("log_report", {}) if isinstance(signal_payload, dict) else {}
        stages = log_report.get("stages", {}) if isinstance(log_report, dict) else {}
        datacenter_hits = self._signal_stage_hits(stages, "datacenter")
        business_hits = self._signal_stage_hits(stages, "android_business")
        package = str(focus_scope.get("package") or "目标进程")
        pid = str(focus_scope.get("pid") or "未识别")
        chain = self._signal_coverage_note(signal_payload, focus_scope)
        if datacenter_hits and business_hits:
            return f"已看到 {package} / PID {pid} 内的 {chain}。"
        if datacenter_hits:
            return f"已看到 {package} / PID {pid} 内进入 DataCenter，但业务消费证据不足。"
        return "当前样本还没有锁定到目标进程内的有效链路。"

    def _signal_coverage_note(self, signal_payload: dict[str, object], focus_scope: dict[str, object]) -> str:
        log_report = signal_payload.get("log_report", {}) if isinstance(signal_payload, dict) else {}
        stages = log_report.get("stages", {}) if isinstance(log_report, dict) else {}
        parts = ["DataCenter"]
        if self._signal_stage_hits(stages, "android_business"):
            parts.append("业务消费")
        if self._signal_has_receiver_like_evidence(signal_payload):
            parts.append("状态消费")
        return " -> ".join(parts)

    def _signal_has_receiver_like_evidence(self, signal_payload: dict[str, object]) -> bool:
        refs = signal_payload.get("source_references", []) if isinstance(signal_payload, dict) else []
        if self._signal_select_state_receiver_reference(refs) is not None:
            return True
        log_report = signal_payload.get("log_report", {}) if isinstance(signal_payload, dict) else {}
        stages = log_report.get("stages", {}) if isinstance(log_report, dict) else {}
        stage = stages.get("android_business") if isinstance(stages, dict) else None
        if not isinstance(stage, dict):
            return False
        for example in stage.get("examples", []) or []:
            if not isinstance(example, dict):
                continue
            label = self._signal_log_semantic_label(str(example.get("text") or ""))
            if label == "状态消费":
                return True
        return False

    def _signal_provider_injection_title(self, signal_payload: dict[str, object]) -> str:
        context = (signal_payload.get("lifecycle_report", {}) or {}).get("context", {})
        helper_class = ""
        if isinstance(context, dict):
            helper_class = str(context.get("helper_class") or "").strip()
        return f"DataCenter 注入 {helper_class}" if helper_class else "DataCenter 注入 Provider"

    def _signal_registration_title(self, signal_payload: dict[str, object]) -> str:
        signal = signal_payload.get("signal", {}) if isinstance(signal_payload, dict) else {}
        code = str(signal.get("code") or "").strip()
        return f"注册 {code} 到 XData" if code else "注册信号到 XData"

    def _signal_datacenter_stage_title(self, signal_payload: dict[str, object]) -> str:
        signal = signal_payload.get("signal", {}) if isinstance(signal_payload, dict) else {}
        code = str(signal.get("code") or "").strip()
        return f"DataCenter 读取 {code} Flow" if code else "DataCenter 读取信号 Flow"

    def _signal_business_stage_title(self, signal_payload: dict[str, object]) -> str:
        return "业务侧消费到有效值"

    def _signal_consumer_stage_title(self, item: dict[str, object]) -> str:
        stage = self._signal_stage_label(item)
        if stage == "状态消费":
            return "状态消费已更新"
        if stage == "业务消费":
            return "业务消费已更新"
        return "下游状态已更新"

    def _signal_data_center_title(self) -> str:
        return "DataCenter.dispatchSignal / getSignalFlow"

    def _signal_select_consumer_reference(self, refs: object) -> dict[str, str] | None:
        if not isinstance(refs, list):
            return None
        ranked: list[tuple[tuple[int, int, int], dict[str, str]]] = []
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            file_text = str(ref.get("file") or "")
            line_text = str(ref.get("text") or "")
            lowered_file = file_text.casefold()
            lowered_line = line_text.casefold()
            if "getsignalflow" not in lowered_line and "collect {" not in lowered_line:
                continue
            score = (
                1 if any(token in lowered_file for token in ("collector", "service", "manager", "config", "viewmodel", "receiver")) else 0,
                0 if "datacenter" in lowered_file else 1,
                1 if "/business/" in lowered_file or "/manager_" in lowered_file else 0,
            )
            ranked.append(
                (
                    score,
                    {
                        "file": file_text,
                        "line": str(ref.get("line") or ""),
                        "text": line_text,
                    },
                )
            )
        if not ranked:
            return None
        ranked.sort(key=lambda item: item[0], reverse=True)
        return ranked[0][1]

    def _signal_select_state_receiver_reference(self, refs: object) -> dict[str, str] | None:
        if not isinstance(refs, list):
            return None
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            file_text = str(ref.get("file") or "")
            lowered = file_text.casefold()
            if any(token in lowered for token in ("receiver", "observer", "viewmodel", "state")):
                return {
                    "file": file_text,
                    "line": str(ref.get("line") or ""),
                    "text": str(ref.get("text") or ""),
                }
        return None

    def _signal_consumer_reference_title(self, ref: dict[str, str]) -> str:
        stem = Path(ref["file"]).stem
        text = ref["text"].casefold()
        if "receiver" in stem.casefold() or "observer" in stem.casefold():
            return f"{stem} 更新状态"
        if "viewmodel" in stem.casefold():
            return f"{stem} 消费状态"
        if "collector" in stem.casefold() or "manager" in stem.casefold() or "config" in stem.casefold():
            return f"{stem} 消费信号"
        if "collect {" in text or "getsignalflow" in text:
            return f"{stem} 消费信号"
        return stem

    def _signal_log_semantic_label(self, text: str) -> str:
        lowered = text.casefold()
        if any(token in lowered for token in ("state applied", "state updated", "update ", "receiver")):
            return "状态消费"
        if any(token in lowered for token in ("collect", "collector", "value=", "statevalue=", " it.value=")):
            return "业务消费"
        return ""

    def _signal_stage_hits(self, stages: object, stage_key: str) -> int:
        if not isinstance(stages, dict):
            return 0
        stage = stages.get(stage_key)
        if not isinstance(stage, dict):
            return 0
        hits = stage.get("hits")
        return int(hits) if isinstance(hits, int) else 0

    def _signal_source_rows(
        self,
        signal_payload: dict[str, object],
        source_evidence_path: Path | None,
    ) -> list[tuple[str, str, str]]:
        rows: list[tuple[str, str, str]] = []
        lifecycle = signal_payload.get("lifecycle_report", {}) if isinstance(signal_payload, dict) else {}
        lifecycle_refs = lifecycle.get("source_references", []) if isinstance(lifecycle, dict) else []
        for ref in lifecycle_refs[:4]:
            if not isinstance(ref, dict):
                continue
            rows.append(
                (
                    "生命周期",
                    f"{ref.get('file') or ''}:{ref.get('line') or ''}",
                    self._signal_shorten(str(ref.get("text") or ""), 90),
                )
            )
        business_entry = self._signal_select_business_entry(source_evidence_path, prompt_text="")
        if business_entry is not None:
            rows.append(
                (
                    "业务判定",
                    f"{business_entry['file']}:{business_entry['line']}",
                    self._signal_shorten(business_entry["text"], 90),
                )
            )
        return rows[:6]

    def _signal_select_business_entry(self, source_evidence_path: Path | None, *, prompt_text: str) -> dict[str, str] | None:
        entries = self._parse_source_evidence_entries(source_evidence_path)
        if not entries:
            return None
        prompt_terms = self._signal_prompt_terms(prompt_text)
        priority_tokens = ("dispatcher", "action", "viewmodel", "receiver", "fragment", "service", "scene")

        def score(entry: dict[str, str]) -> tuple[int, int, int, int]:
            haystack = f"{entry['file']}\n{entry['text']}".casefold()
            hint_rank = 0
            for index, token in enumerate(priority_tokens, start=1):
                if token in haystack:
                    hint_rank = len(priority_tokens) - index + 1
                    break
            prompt_match = any(term.casefold() in haystack for term in prompt_terms)
            is_constant_only = 1 if any(token in haystack for token in ("constants", "eventid", "strings.xml")) else 0
            return (
                1 if prompt_match else 0,
                hint_rank,
                0 if is_constant_only else 1,
                1 if "carvcuhelper" not in haystack and "datacenter" not in haystack else 0,
            )
        ranked = sorted(entries, key=score, reverse=True)
        return ranked[0] if ranked else None

    def _parse_source_evidence_entries(self, source_evidence_path: Path | None) -> list[dict[str, str]]:
        if source_evidence_path is None or not source_evidence_path.exists():
            return []
        entries: list[dict[str, str]] = []
        current_file = ""
        try:
            lines = source_evidence_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        for line in lines:
            if line.startswith("## "):
                current_file = line[3:].strip()
                continue
            match = re.match(r"- L(\d+): `(.+)`", line.strip())
            if not match or not current_file:
                continue
            entries.append({"file": current_file, "line": match.group(1), "text": match.group(2)})
        return entries

    def _signal_prompt_terms(self, prompt_text: str) -> list[str]:
        raw_terms = re.findall(r"[A-Za-z_]{4,}|[\u4e00-\u9fff]{2,8}", prompt_text or "")
        ignored = {"分析", "源码", "参考", "信号链路", "主要是", "请参考"}
        terms: list[str] = []
        for term in raw_terms:
            cleaned = term.strip()
            if not cleaned or cleaned in ignored:
                continue
            if cleaned not in terms:
                terms.append(cleaned)
        return terms[:8]

    def _signal_find_source_reference(
        self,
        refs: object,
        predicate: Callable[[str, str], bool],
    ) -> dict[str, str] | None:
        if not isinstance(refs, list):
            return None
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            file_text = str(ref.get("file") or "").casefold()
            line_text = str(ref.get("text") or "").casefold()
            if predicate(file_text, line_text):
                return {
                    "file": str(ref.get("file") or ""),
                    "line": str(ref.get("line") or ""),
                    "text": str(ref.get("text") or ""),
                }
        return None

    def _render_flow_line(self, nodes: list[dict[str, str]]) -> str:
        if not nodes:
            return '<p class="muted">未提取到可视化链路节点。</p>'
        chunks: list[str] = ['<div class="flow-line">']
        for index, node in enumerate(nodes):
            if index:
                chunks.append('<div class="flow-arrow-inline">→</div>')
            chunks.append(
                '<div class="flow-step">'
                f'<div class="flow-tag">{combined_bug_html.H(node.get("tag", ""))}</div>'
                f'<div class="flow-title">{combined_bug_html.H(node.get("title", ""))}</div>'
                f'<div class="flow-meta">{combined_bug_html.H(node.get("meta", ""))}</div>'
                f'<div class="flow-note">{combined_bug_html.H(node.get("note", ""))}</div>'
                '</div>'
            )
        chunks.append("</div>")
        return "".join(chunks)

    def _signal_package_from_path(self, path_text: str) -> str:
        match = re.search(r"/app/([^/]+)/", path_text)
        return match.group(1) if match else ""

    def _signal_pid_from_text(self, text: str) -> str:
        match = re.match(r"\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+\s+(\d+)\s+", text)
        if match:
            return match.group(1)
        match = re.search(r"\[(\d+),\d+\]\[20\d{2}-\d{2}-\d{2}", text)
        return match.group(1) if match else ""

    def _signal_tid_from_text(self, text: str) -> str:
        match = re.match(r"\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+\s+\d+\s+(\d+)\s+", text)
        return match.group(1) if match else ""

    def _signal_shorten(self, text: str, limit: int) -> str:
        normalized = text.strip()
        if len(normalized) <= limit:
            return normalized
        return normalized[: limit - 1].rstrip() + "…"

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
        progress_callback: Callable[[dict[str, object]], None] | None = None,
    ) -> TaskResult:
        self._emit_progress(
            progress_callback,
            stage="bug_failed",
            message=message,
            job_id=context.job_id,
            error_code=error_code,
        )
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

    def _run_bug_agent_summary(
        self,
        *,
        request_text: str,
        request_artifact: Path,
        metadata_path: Path,
        output_path: Path,
        progress_callback: Callable[[dict[str, object]], None] | None,
        timeout: int,
        provider_session_id: str = "",
        followup_text: str = "",
        previous_summary_path: Path | None = None,
    ) -> dict[str, object]:
        invocation = self._build_bug_agent_summary_command(
            request_text=request_text,
            request_artifact=request_artifact,
            metadata_path=metadata_path,
            output_path=output_path,
            provider_session_id=provider_session_id,
            followup_text=followup_text,
            previous_summary_path=previous_summary_path,
        )
        if not invocation["command"]:
            return {
                "message": "",
                "command": None,
                "error": "agent_summary_not_configured",
                "provider": "",
                "session_id": provider_session_id,
                "resumed": False,
                "usage_scope": "",
            }
        result = self._run_bug_agent_summary_once(
            invocation=invocation,
            output_path=output_path,
            progress_callback=progress_callback,
            timeout=timeout,
        )
        if result["message"] and result["provider"]:
            return result
        if result["message"]:
            return result
        fallback_result = result
        if provider_session_id.strip():
            self._emit_progress(
                progress_callback,
                stage="bug_agent_summary_retry",
                message="Agent 续会话失败，退回重新读取最新产物整理结论",
                provider=invocation["provider"],
                previous_session_id=provider_session_id.strip(),
            )
            fallback_invocation = self._build_bug_agent_summary_command(
                request_text=request_text,
                request_artifact=request_artifact,
                metadata_path=metadata_path,
                output_path=output_path,
                provider_session_id="",
                followup_text=followup_text,
                previous_summary_path=previous_summary_path,
            )
            if fallback_invocation["command"]:
                fallback_result = self._run_bug_agent_summary_once(
                    invocation=fallback_invocation,
                    output_path=output_path,
                    progress_callback=progress_callback,
                    timeout=timeout,
                )
                if fallback_result["message"] and fallback_result["provider"]:
                    return fallback_result
        provider_fallback = self._build_bug_agent_summary_fallback_command(
            request_text=request_text,
            request_artifact=request_artifact,
            metadata_path=metadata_path,
            output_path=output_path,
            followup_text=followup_text,
            previous_summary_path=previous_summary_path,
        )
        if not provider_fallback["command"]:
            return fallback_result
        self._emit_progress(
            progress_callback,
            stage="bug_agent_summary_provider_fallback",
            message="主 Agent 不可用，切换备用 Agent 继续整理结论",
            primary_provider=str(invocation["provider"] or ""),
            fallback_provider=str(provider_fallback["provider"] or ""),
        )
        return self._run_bug_agent_summary_once(
            invocation=provider_fallback,
            output_path=output_path,
            progress_callback=progress_callback,
            timeout=timeout,
        )

    def _run_bug_agent_summary_once(
        self,
        *,
        invocation: dict[str, object],
        output_path: Path,
        progress_callback: Callable[[dict[str, object]], None] | None,
        timeout: int,
    ) -> dict[str, object]:
        command = list(invocation["command"])
        provider = str(invocation["provider"] or "")
        session_id = str(invocation.get("session_id") or "")
        resumed = bool(invocation.get("resumed"))
        started = time.monotonic()
        self._emit_progress(
            progress_callback,
            stage="bug_agent_summary",
            message="调用本地 Agent 继续整理最终结论" if resumed else "调用本地 Agent 整理最终结论",
            output_path=str(output_path),
            provider=provider,
            resumed=resumed,
            provider_session_id=session_id,
        )
        try:
            completed = run_tracked_process(
                command,
                watchdog=self.process_watchdog,
                name=f"bug-agent-summary-{provider or 'agent'}",
                cwd=self._working_dir(),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            self._emit_progress(
                progress_callback,
                stage="bug_agent_summary_failed",
                message=f"本地 Agent 总结失败，回退到脚本摘要: {exc}",
                provider=provider,
                resumed=resumed,
                provider_session_id=session_id,
            )
            return {
                "message": "",
                "command": command,
                "error": str(exc),
                "provider": provider,
                "session_id": session_id,
                "resumed": resumed,
                "duration_seconds": time.monotonic() - started,
                "usage": {},
                "usage_scope": "",
            }
        if completed.returncode != 0:
            error = completed.stderr.strip() or completed.stdout.strip() or f"returncode={completed.returncode}"
            self._emit_progress(
                progress_callback,
                stage="bug_agent_summary_failed",
                message=f"本地 Agent 总结失败，回退到脚本摘要: {error}",
                provider=provider,
                resumed=resumed,
                provider_session_id=session_id,
            )
            return {
                "message": "",
                "command": command,
                "error": error,
                "provider": provider,
                "session_id": session_id,
                "resumed": resumed,
                "duration_seconds": time.monotonic() - started,
                "usage": {},
                "usage_scope": "",
            }
        if output_path.exists():
            message = output_path.read_text(encoding="utf-8").strip()
        else:
            message = completed.stdout.strip()
            if message:
                try:
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    output_path.write_text(message, encoding="utf-8")
                except OSError:
                    return {
                        "message": "",
                        "command": command,
                        "error": "agent_summary_output_io_error",
                        "provider": provider,
                        "session_id": session_id,
                        "resumed": resumed,
                        "duration_seconds": time.monotonic() - started,
                        "usage": {},
                        "usage_scope": "",
                    }
        if not message:
            return {
                "message": "",
                "command": command,
                "error": "empty_agent_summary",
                "provider": provider,
                "session_id": session_id,
                "resumed": resumed,
                "duration_seconds": time.monotonic() - started,
                "usage": {},
                "usage_scope": "",
            }
        resolved_session_id = self._extract_bug_agent_session_id(provider, completed.stdout, fallback=session_id)
        usage, usage_scope = self._extract_bug_agent_usage(provider, completed.stdout, completed.stderr)
        self._emit_progress(
            progress_callback,
            stage="bug_agent_summary_completed",
            message="本地 Agent 已整理最终结论",
            provider=provider,
            output_path=str(output_path),
            resumed=resumed,
            provider_session_id=resolved_session_id,
        )
        return {
            "message": message,
            "command": command,
            "error": "",
            "provider": provider,
            "session_id": resolved_session_id,
            "resumed": resumed,
            "duration_seconds": time.monotonic() - started,
            "usage": usage,
            "usage_scope": usage_scope,
        }

    def _extract_bug_agent_session_id(self, provider: str, output: str, *, fallback: str = "") -> str:
        if fallback.strip():
            return fallback.strip()
        if provider != "codex":
            return ""
        for line in output.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            found = self._search_bug_agent_session_id_in_payload(payload)
            if found:
                return found
        return ""

    def _search_bug_agent_session_id_in_payload(self, payload: object) -> str:
        if isinstance(payload, dict):
            for key in ("session_id", "sessionId", "conversation_id", "conversationId", "thread_id", "threadId"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            for key in ("session", "conversation", "thread"):
                nested = payload.get(key)
                if isinstance(nested, dict):
                    value = nested.get("id")
                    if isinstance(value, str) and value.strip():
                        return value.strip()
            for value in payload.values():
                found = self._search_bug_agent_session_id_in_payload(value)
                if found:
                    return found
            return ""
        if isinstance(payload, list):
            for item in payload:
                found = self._search_bug_agent_session_id_in_payload(item)
                if found:
                    return found
        return ""

    def _extract_bug_agent_usage(self, provider: str, stdout: str, stderr: str) -> tuple[dict[str, int], str]:
        usage, scope = self._extract_usage_from_json_lines(stdout)
        if usage:
            return usage, scope
        if provider == "codex":
            usage, scope = self._extract_usage_from_json_lines(stderr)
            if usage:
                return usage, scope
        return self._extract_usage_from_text("\n".join(part for part in (stdout, stderr) if part)), "cumulative"

    def _extract_usage_from_json_lines(self, text: str) -> tuple[dict[str, int], str]:
        latest: dict[str, int] = {}
        latest_delta: dict[str, int] = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            for candidate in self._iter_usage_objects_from_keys(
                payload,
                keys=("delta_usage", "deltaUsage", "usage_delta", "usageDelta"),
            ):
                parsed = self._parse_usage_object(candidate)
                if parsed:
                    latest_delta = parsed
            for candidate in self._iter_usage_objects(payload):
                parsed = self._parse_usage_object(candidate)
                if parsed:
                    latest = parsed
        if latest_delta:
            return latest_delta, "delta"
        return latest, "cumulative" if latest else ""

    def _iter_usage_objects_from_keys(self, value: object, *, keys: tuple[str, ...]) -> list[dict[str, object]]:
        found: list[dict[str, object]] = []
        if isinstance(value, dict):
            for key in keys:
                nested = value.get(key)
                if isinstance(nested, dict):
                    found.append(nested)
            for nested in value.values():
                found.extend(self._iter_usage_objects_from_keys(nested, keys=keys))
        elif isinstance(value, list):
            for item in value:
                found.extend(self._iter_usage_objects_from_keys(item, keys=keys))
        return found

    def _iter_usage_objects(self, value: object) -> list[dict[str, object]]:
        found: list[dict[str, object]] = []
        if isinstance(value, dict):
            if any(any(alias in value for alias in aliases) for aliases in _TOKEN_USAGE_KEYS.values()):
                found.append(value)
            for nested in value.values():
                found.extend(self._iter_usage_objects(nested))
        elif isinstance(value, list):
            for item in value:
                found.extend(self._iter_usage_objects(item))
        return found

    def _parse_usage_object(self, value: object) -> dict[str, int]:
        if not isinstance(value, dict):
            return {}
        usage: dict[str, int] = {}
        for target_key, aliases in _TOKEN_USAGE_KEYS.items():
            for alias in aliases:
                parsed = self._coerce_token_count(value.get(alias))
                if parsed is not None:
                    usage[target_key] = parsed
                    break
        if "total_tokens" not in usage and {"input_tokens", "output_tokens"}.issubset(usage):
            usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
        return usage

    def _extract_usage_from_text(self, text: str) -> dict[str, int]:
        patterns = {
            "input_tokens": (r"input[_ ]tokens?\s*[:=]\s*(\d+)", r"prompt[_ ]tokens?\s*[:=]\s*(\d+)"),
            "output_tokens": (r"output[_ ]tokens?\s*[:=]\s*(\d+)", r"completion[_ ]tokens?\s*[:=]\s*(\d+)"),
            "total_tokens": (r"total[_ ]tokens?\s*[:=]\s*(\d+)",),
        }
        usage: dict[str, int] = {}
        for target_key, candidates in patterns.items():
            for pattern in candidates:
                match = re.search(pattern, text, flags=re.IGNORECASE)
                if match:
                    usage[target_key] = int(match.group(1))
                    break
        if "total_tokens" not in usage and {"input_tokens", "output_tokens"}.issubset(usage):
            usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
        return usage

    def _coerce_token_count(self, value: object) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str) and value.isdigit():
            return int(value)
        return None

    def _emit_progress(
        self,
        progress_callback: Callable[[dict[str, object]], None] | None,
        *,
        stage: str,
        message: str,
        **details: object,
    ) -> None:
        if progress_callback is None:
            return
        payload: dict[str, object] = {"stage": stage, "message": message}
        if details:
            payload["details"] = details
        progress_callback(payload)

    def _request_text(self, *, raw_text: str, prompt_text: str, bug_url: str) -> str:
        candidate = (raw_text or "").strip()
        if candidate:
            return candidate
        if bug_url:
            if prompt_text:
                return f"{bug_url} {prompt_text}".strip()
            return bug_url
        return prompt_text.strip()

    def _apply_agent_runtime_details(self, details: dict[str, object], agent_summary_result: dict[str, object]) -> None:
        if agent_summary_result["command"]:
            details["agent_summary_command"] = list(agent_summary_result["command"])
        if agent_summary_result["error"]:
            details["agent_summary_error"] = str(agent_summary_result["error"])
        if agent_summary_result["provider"]:
            provider = str(agent_summary_result["provider"])
            details["agent_summary_provider"] = provider
            details.setdefault("provider", provider)
        if agent_summary_result["session_id"]:
            details["agent_summary_session_id"] = str(agent_summary_result["session_id"])
        if agent_summary_result["resumed"]:
            details["agent_summary_resumed"] = True
        if agent_summary_result.get("usage_scope"):
            details["agent_summary_usage_scope"] = str(agent_summary_result["usage_scope"])
        duration = agent_summary_result.get("duration_seconds")
        if isinstance(duration, (int, float)):
            details["agent_summary_duration_seconds"] = float(duration)
        usage = agent_summary_result.get("usage")
        if isinstance(usage, dict):
            for key in ("input_tokens", "output_tokens", "total_tokens"):
                value = usage.get(key)
                if isinstance(value, int):
                    details[f"agent_summary_{key}"] = value

    def _append_agent_runtime_metadata(
        self,
        metadata_path: Path,
        *,
        agent_summary_result: dict[str, object],
        total_duration_seconds: float,
    ) -> None:
        provider = str(agent_summary_result.get("provider") or "").strip()
        usage = agent_summary_result.get("usage")
        duration = agent_summary_result.get("duration_seconds")
        session_id = str(agent_summary_result.get("session_id") or "").strip()
        resumed = bool(agent_summary_result.get("resumed"))
        usage_scope = str(agent_summary_result.get("usage_scope") or "").strip()
        if not provider and not isinstance(usage, dict) and not isinstance(duration, (int, float)):
            return
        lines = ["", "## Agent 执行信息", ""]
        lines.append(f"- Agent 类型: `{provider or '未知'}`")
        if session_id:
            lines.append(f"- Agent 会话ID: `{session_id}`")
        lines.append(f"- 续会话: `{'是' if resumed else '否'}`")
        if isinstance(usage, dict):
            input_tokens = usage.get("input_tokens")
            output_tokens = usage.get("output_tokens")
            total_tokens = usage.get("total_tokens")
            if any(isinstance(value, int) for value in (input_tokens, output_tokens, total_tokens)):
                token_label = "本轮 Agent Token" if usage_scope == "delta" else "累计 Agent Token"
                lines.append(
                    f"- {token_label}: `{self._format_token_millions(input_tokens)} / "
                    f"{self._format_token_millions(output_tokens)} / "
                    f"{self._format_token_millions(total_tokens)}`"
                )
        if isinstance(duration, (int, float)):
            lines.append(f"- Agent 耗时: `{float(duration):.1f} 秒`")
        lines.append(f"- 总耗时: `{total_duration_seconds:.1f} 秒`")
        try:
            original = metadata_path.read_text(encoding="utf-8")
        except OSError:
            original = ""
        metadata_path.write_text(original.rstrip() + "\n" + "\n".join(lines).rstrip() + "\n", encoding="utf-8")

    def _append_source_evidence_metadata(self, metadata_path: Path, source_evidence_path: Path | None) -> None:
        if source_evidence_path is None:
            return
        try:
            evidence = source_evidence_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            evidence = f"读取失败: {exc}"
        try:
            original = metadata_path.read_text(encoding="utf-8")
        except OSError:
            original = ""
        lines = [
            "",
            "## 源码证据",
            "",
            f"- 文件: `{source_evidence_path}`",
            "",
            "```text",
            evidence[:5000],
            "```",
        ]
        metadata_path.write_text(original.rstrip() + "\n" + "\n".join(lines).rstrip() + "\n", encoding="utf-8")

    def _should_collect_source_evidence(self, *texts: str) -> bool:
        source_terms = ("源码", "源代码", "根据源码", "基于源码", "信号定义", "链路")
        merged = "\n".join(texts).casefold()
        return any(term.casefold() in merged for term in source_terms)

    def _annotate_html_reports(
        self,
        html_paths: list[Path],
        *,
        agent_summary_result: dict[str, object],
        total_duration_seconds: float,
    ) -> None:
        snippet = self._build_agent_runtime_html(agent_summary_result, total_duration_seconds=total_duration_seconds)
        if not snippet:
            return
        for path in html_paths:
            if not path.exists() or path.suffix.lower() != ".html":
                continue
            try:
                html_text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            updated = self._inject_runtime_html(html_text, snippet)
            if updated == html_text:
                continue
            try:
                path.write_text(updated, encoding="utf-8")
            except OSError:
                continue

    def _build_agent_runtime_html(self, agent_summary_result: dict[str, object], *, total_duration_seconds: float) -> str:
        provider = str(agent_summary_result.get("provider") or "").strip()
        usage = agent_summary_result.get("usage")
        duration = agent_summary_result.get("duration_seconds")
        session_id = str(agent_summary_result.get("session_id") or "").strip()
        resumed = bool(agent_summary_result.get("resumed"))
        usage_scope = str(agent_summary_result.get("usage_scope") or "").strip()
        if not provider and not isinstance(usage, dict) and not isinstance(duration, (int, float)):
            return ""
        rows = [
            ("Agent 类型", provider or "未知"),
            ("续会话", "是" if resumed else "否"),
        ]
        if session_id:
            rows.append(("Agent 会话ID", session_id))
        if isinstance(usage, dict) and any(isinstance(usage.get(key), int) for key in ("input_tokens", "output_tokens", "total_tokens")):
            rows.append(
                (
                    "本轮 Agent Token" if usage_scope == "delta" else "累计 Agent Token",
                    f"{self._format_token_millions(usage.get('input_tokens'))} / "
                    f"{self._format_token_millions(usage.get('output_tokens'))} / "
                    f"{self._format_token_millions(usage.get('total_tokens'))}",
                )
            )
        if isinstance(duration, (int, float)):
            rows.append(("Agent 耗时", f"{float(duration):.1f} 秒"))
        rows.append(("总耗时", f"{total_duration_seconds:.1f} 秒"))
        items = "".join(
            "<div class=\"lagent-runtime-item\">"
            f"<div class=\"lagent-runtime-label\">{self._escape_html(label)}</div>"
            f"<div class=\"lagent-runtime-value\">{self._escape_html(value)}</div>"
            "</div>"
            for label, value in rows
        )
        style = (
            "<style>"
            ".lagent-runtime{margin:16px 0 20px;padding:16px 18px;border:1px solid #dbe2f0;border-radius:12px;"
            "background:#f8fafc;box-shadow:0 1px 3px rgba(15,23,42,.06)}"
            ".lagent-runtime h2{margin:0 0 12px;font-size:18px;color:#0f172a}"
            ".lagent-runtime-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}"
            ".lagent-runtime-item{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:12px 14px}"
            ".lagent-runtime-label{font-size:12px;color:#64748b;margin-bottom:4px}"
            ".lagent-runtime-value{font-size:16px;font-weight:600;color:#0f172a;word-break:break-word}"
            "</style>"
        )
        return (
            f"{_RUNTIME_HTML_MARKER_START}{style}"
            "<section class=\"lagent-runtime\">"
            "<h2>Agent 运行信息</h2>"
            f"<div class=\"lagent-runtime-grid\">{items}</div>"
            "</section>"
            f"{_RUNTIME_HTML_MARKER_END}"
        )

    def _format_token_millions(self, value: object) -> str:
        if not isinstance(value, int):
            return "-"
        if value < 1_000:
            return str(value)
        if value < 1_000_000:
            return f"{value / 1_000:.2f}K"
        return f"{value / 1_000_000:.2f}M"

    def _inject_runtime_html(self, html_text: str, runtime_html: str) -> str:
        pattern = re.compile(
            rf"{re.escape(_RUNTIME_HTML_MARKER_START)}.*?{re.escape(_RUNTIME_HTML_MARKER_END)}",
            flags=re.DOTALL,
        )
        if pattern.search(html_text):
            return pattern.sub(runtime_html, html_text, count=1)
        body_match = re.search(r"<body[^>]*>", html_text, flags=re.IGNORECASE)
        if body_match:
            index = body_match.end()
            return html_text[:index] + runtime_html + html_text[index:]
        html_match = re.search(r"<html[^>]*>", html_text, flags=re.IGNORECASE)
        if html_match:
            index = html_match.end()
            return html_text[:index] + "<body>" + runtime_html + "</body>" + html_text[index:]
        return runtime_html + html_text

    def _escape_html(self, value: object) -> str:
        return (
            str(value)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

    def _write_reanalysis_source_evidence(
        self,
        *,
        plans: list["BugAnalysisPlan"],
        request_text: str,
        followup_text: str,
        output_dir: Path,
        enabled: bool,
        extra_texts: tuple[str, ...] = (),
    ) -> Path | None:
        if not enabled:
            return None
        terms = self._source_evidence_terms(
            plans=plans,
            request_text=request_text,
            followup_text=followup_text,
            extra_texts=extra_texts,
        )
        if not terms:
            return None
        evidence_path = output_dir / "bug_source_evidence.md"
        repo = self.config.guideengine_repo.expanduser()
        lines = [
            "# Bug Source Evidence",
            "",
            f"- 源码根目录: `{repo}`",
            f"- 检索词: `{', '.join(terms)}`",
            "",
        ]
        if not repo.exists():
            lines.append(f"源码根目录不存在，未执行源码检索: `{repo}`")
            evidence_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return evidence_path

        matches = self._collect_source_evidence(repo=repo, terms=terms)
        if not matches:
            lines.append("未检索到匹配源码。")
        else:
            current_file = ""
            for path, line_no, text in matches:
                if path != current_file:
                    current_file = path
                    lines.extend(["", f"## {path}"])
                lines.append(f"- L{line_no}: `{text}`")
        evidence_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        return evidence_path

    def _source_evidence_terms(
        self,
        *,
        plans: list["BugAnalysisPlan"],
        request_text: str,
        followup_text: str,
        extra_texts: tuple[str, ...] = (),
    ) -> list[str]:
        terms: list[str] = []
        for plan in plans:
            if plan.kind != "signal" or not plan.signal_code:
                continue
            self._append_unique(terms, plan.signal_code)
            if plan.signal_code.startswith("SIGNAL_"):
                self._append_unique(terms, plan.signal_code.removeprefix("SIGNAL_"))
        for text in (followup_text, request_text):
            signal = self._extract_signal_code_for_reanalysis(text)
            if signal:
                self._append_unique(terms, signal)
                if signal.startswith("SIGNAL_"):
                    self._append_unique(terms, signal.removeprefix("SIGNAL_"))
        for text in (*extra_texts, request_text, followup_text):
            for term in self._business_source_terms_from_text(text):
                self._append_unique(terms, term)
        return terms[:8]

    def _business_source_terms_from_text(self, text: str) -> list[str]:
        suffixes = (
            "模式",
            "功能",
            "场景",
            "页面",
            "流程",
            "链路",
            "策略",
            "状态",
            "异常",
            "失败",
            "开关",
            "服务",
            "模块",
            "信号",
            "电量",
            "电流",
            "电压",
            "百分比",
        )
        generic_prefixes = (
            "结合",
            "根据",
            "找到",
            "分析",
            "重新",
            "通过",
            "查看",
            "确认",
            "排查",
            "调查",
            "主要是",
            "为什么",
            "无法",
            "不能",
            "开启",
        )
        terms: list[str] = []
        for suffix in suffixes:
            pattern = re.compile(rf"[\u4e00-\u9fff]{{2,14}}{re.escape(suffix)}")
            for match in pattern.finditer(text):
                term = match.group(0)
                changed = True
                while changed:
                    changed = False
                    for prefix in generic_prefixes:
                        if term.startswith(prefix) and len(term) > len(prefix) + len(suffix):
                            term = term[len(prefix) :]
                            changed = True
                self._append_unique(terms, term)
                compact_len = len(suffix) + 2
                if len(term) > compact_len:
                    self._append_unique(terms, term[-compact_len:])
        return terms

    def _collect_source_evidence(self, *, repo: Path, terms: list[str]) -> list[tuple[str, int, str]]:
        rg_matches = self._collect_source_evidence_with_rg(repo=repo, terms=terms)
        if rg_matches is not None:
            return rg_matches
        return self._collect_source_evidence_by_scan(repo=repo, terms=terms)

    def _collect_source_evidence_with_rg(self, *, repo: Path, terms: list[str]) -> list[tuple[str, int, str]] | None:
        if shutil.which("rg") is None:
            return None
        command = [
            "rg",
            "--fixed-strings",
            "--line-number",
            "--no-heading",
            "--color",
            "never",
            "--max-count",
            "3",
            "--glob",
            "*.{kt,java,cpp,cc,c,h,hpp,proto,xml,md}",
            "--glob",
            "!.git/**",
            "--glob",
            "!.gradle/**",
            "--glob",
            "!build/**",
            "--glob",
            "!out/**",
            "--glob",
            "!.cxx/**",
        ]
        for term in terms:
            command.extend(["-e", term])
        command.append(str(repo))
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if completed.returncode not in {0, 1}:
            return None
        matches: list[tuple[str, int, str]] = []
        for line in completed.stdout.splitlines():
            if len(matches) >= 80:
                break
            path_text, sep, rest = line.partition(":")
            if not sep:
                continue
            line_no_text, sep, text = rest.partition(":")
            if not sep or not line_no_text.isdigit():
                continue
            try:
                rel_path = str(Path(path_text).resolve().relative_to(repo.resolve()))
            except ValueError:
                rel_path = path_text
            matches.append((rel_path, int(line_no_text), text.strip()[:300]))
        return matches

    def _collect_source_evidence_by_scan(self, *, repo: Path, terms: list[str]) -> list[tuple[str, int, str]]:
        suffixes = {".kt", ".java", ".cpp", ".cc", ".c", ".h", ".hpp", ".proto", ".xml", ".md"}
        ignored_dirs = {".git", ".gradle", ".idea", "build", "out", ".cxx", "node_modules"}
        matches: list[tuple[str, int, str]] = []
        max_matches = 80
        for path in sorted(repo.rglob("*")):
            if len(matches) >= max_matches:
                break
            if not path.is_file() or path.suffix not in suffixes:
                continue
            if any(part in ignored_dirs for part in path.relative_to(repo).parts):
                continue
            try:
                if path.stat().st_size > 2_000_000:
                    continue
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            rel_path = str(path.relative_to(repo))
            for index, line in enumerate(lines, start=1):
                if any(term and term in line for term in terms):
                    matches.append((rel_path, index, line.strip()[:300]))
                    if len(matches) >= max_matches:
                        break
        return matches

    def _append_unique(self, values: list[str], value: str) -> None:
        normalized = value.strip()
        if normalized and normalized not in values:
            values.append(normalized)

    def _render_bug_agent_request(
        self,
        *,
        request_text: str,
        prompt_text: str,
        bug_url: str,
        plans: list["BugAnalysisPlan"],
    ) -> str:
        plan_lines = "\n".join(f"- {self._analysis_label(plan.kind)} (`{plan.kind}`)" for plan in plans)
        return (
            "# Bug Agent Request\n\n"
            "以下内容需要完整提供给本地 Agent 作为分析输入。\n\n"
            f"- Bug URL: `{bug_url}`\n"
            f"- 提炼后的分析描述: `{prompt_text}`\n"
            f"- 计划分析类型:\n{plan_lines}\n"
            "- 用户原始请求:\n\n```text\n"
            f"{request_text}\n"
            "```\n"
        )

    def _render_bug_reanalysis_request(
        self,
        *,
        request_text: str,
        followup_text: str,
        target_time: str,
        plans: list["BugAnalysisPlan"],
        history: list[dict[str, str]] | None,
    ) -> str:
        plan_lines = "\n".join(
            f"- {self._analysis_label(plan.kind)} (`{plan.kind}`)"
            + (f" / 信号: `{plan.signal_code}`" if plan.kind == "signal" and plan.signal_code else "")
            for plan in plans
        )
        history_lines: list[str] = []
        for item in history or []:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").strip()
            content = str(item.get("content") or "").strip()
            if not role or not content:
                continue
            history_lines.append(f"- {role}: {content}")
        history_block = "\n".join(history_lines) if history_lines else "- 无"
        return (
            "# Bug Reanalysis Request\n\n"
            "这是同一个 Bug 会话里的续聊/修正，请延续上一轮分析上下文，而不是重新开启独立话题。\n\n"
            f"- 本次追问/修正: `{followup_text}`\n"
            f"- 修正后的故障时间: `{target_time or '未识别'}`\n"
            f"- 继续分析类型:\n{plan_lines}\n"
            "- 最近对话历史:\n"
            f"{history_block}\n"
            "- 用户原始请求:\n\n```text\n"
            f"{request_text}\n"
            "```\n"
        )

    def _render_bug_agent_followup_request(
        self,
        *,
        request_text: str,
        followup_text: str,
        summary_text: str,
        report_excerpt: str,
        history: list[dict[str, str]] | None,
    ) -> str:
        history_lines: list[str] = []
        for item in history or []:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").strip()
            content = str(item.get("content") or "").strip()
            if not role or not content:
                continue
            history_lines.append(f"- {role}: {content}")
        history_block = "\n".join(history_lines) if history_lines else "- 无"
        return (
            "# Bug Agent Follow-up Request\n\n"
            "这是同一个 Bug 会话里的继续追问，请延续原来的 Agent 会话，"
            "优先复用已经下载/解密/分析过的日志与报告，不要重新要求用户上传材料。\n\n"
            f"- 本次追问:\n\n```text\n{followup_text}\n```\n"
            f"- 上一轮摘要:\n\n```text\n{summary_text or '无'}\n```\n"
            f"- 上一轮报告摘录:\n\n```text\n{report_excerpt or '无'}\n```\n"
            "- 最近对话历史:\n"
            f"{history_block}\n"
            "- 用户原始请求:\n\n```text\n"
            f"{request_text}\n"
            "```\n"
        )

    def _render_bug_reanalysis_metadata(
        self,
        *,
        request_text: str,
        followup_text: str,
        job_id: str,
        target_time: str,
        prepared_input: Path | None,
        selected_input: Path | None,
        plans: list["BugAnalysisPlan"],
        rerun_kinds: list[str],
        reused_kinds: list[str],
        html_paths: list[Path],
        report_jsons: dict[str, Path | None],
        combined_artifacts: dict[str, object] | None,
        previous_summary_path: Path | None,
        source_evidence_path: Path | None = None,
        classification_skill: str = "",
        classification_source: str = "",
        classification_reason: str = "",
        classification_provider: str = "",
    ) -> str:
        lines = [
            "# Bug Reanalysis Metadata",
            "",
            f"- Job ID: `{job_id}`",
            f"- 用户原始请求: `{request_text}`",
            f"- 本次追问/修正: `{followup_text}`",
            f"- 修正后的故障时间: `{target_time or '未识别'}`",
            f"- 复用 prepared log 输入: `{prepared_input or ''}`",
            f"- 上一轮选中的日志输入: `{selected_input or ''}`",
            f"- 分析类型: `{', '.join(plan.kind for plan in plans) or '无'}`",
            f"- 命中 Skill: `{classification_skill or 'general'}`",
            f"- 分类来源: `{classification_source or 'manual_fallback'}`",
            f"- 分类 Agent: `{classification_provider or '无'}`",
            f"- 分类理由: `{classification_reason or '未记录'}`",
            f"- 信号目标: `{', '.join(plan.signal_code or '' for plan in plans if plan.kind == 'signal') or '无'}`",
            f"- 本次重新执行: `{', '.join(rerun_kinds) or '无'}`",
            f"- 本次直接复用: `{', '.join(reused_kinds) or '无'}`",
        ]
        if previous_summary_path is not None:
            lines.append(f"- 上一轮 Agent 总结: `{previous_summary_path}`")
        if source_evidence_path is not None:
            lines.append(f"- 本轮源码证据: `{source_evidence_path}`")
        if combined_artifacts is not None:
            lines.extend(
                [
                    f"- 综合报告 HTML: `{combined_artifacts['html_path']}`",
                    f"- 综合报告 JSON: `{combined_artifacts['json_path']}`",
                ]
            )
        lines.append("- 最新 HTML 报告:")
        for path in html_paths:
            lines.append(f"  - `{path}`")
        lines.append("- 最新 JSON 报告:")
        for kind, path in report_jsons.items():
            lines.append(f"  - `{kind}` -> `{path or ''}`")
        if source_evidence_path is not None:
            lines.extend(["", "## 本轮源码证据摘录", ""])
            try:
                evidence = source_evidence_path.read_text(encoding="utf-8").strip()
            except OSError as exc:
                evidence = f"读取失败: {exc}"
            lines.append(evidence[:5000])
        return "\n".join(lines) + "\n"

    def _render_bug_agent_followup_metadata(
        self,
        *,
        request_text: str,
        followup_text: str,
        job_id: str,
        job_dir: Path,
        output_dir: Path,
        prepared_input: Path | None,
        selected_input: Path | None,
        previous_summary_path: Path | None,
        report_files: list[Path],
        report_url: str,
    ) -> str:
        lines = [
            "# Bug Agent Follow-up Metadata",
            "",
            f"- Job ID: `{job_id}`",
            f"- Job 目录: `{job_dir}`",
            f"- 输出目录: `{output_dir}`",
            f"- 用户原始请求: `{request_text}`",
            f"- 本次追问: `{followup_text}`",
            f"- prepared log 输入: `{prepared_input or ''}`",
            f"- selected log 输入: `{selected_input or ''}`",
        ]
        if previous_summary_path is not None:
            lines.append(f"- 上一轮 Agent 总结: `{previous_summary_path}`")
        if report_url.strip():
            lines.append(f"- 当前已发布报告链接: `{report_url.strip()}`")
        lines.append("- 可直接读取的现有报告/产物:")
        if report_files:
            for path in report_files:
                lines.append(f"  - `{path}`")
        else:
            lines.append("  - 无")
        lines.extend(
            [
                "- 处理要求:",
                "  - 直接基于上述本地路径继续分析，不要重新要求用户上传日志。",
                "  - 若需要补充证据，优先读取 prepared log 输入与 output 目录中的现有产物。",
            ]
        )
        return "\n".join(lines) + "\n"

    def _collect_bug_output_artifacts(self, output_dir: Path) -> list[Path]:
        if not output_dir.exists():
            return []
        artifacts: list[Path] = []
        for path in sorted(output_dir.iterdir()):
            if not path.is_file():
                continue
            if path.suffix.lower() not in {".md", ".html", ".json"}:
                continue
            artifacts.append(path)
        return artifacts

    def _build_bug_agent_summary_command(
        self,
        *,
        request_text: str,
        request_artifact: Path,
        metadata_path: Path,
        output_path: Path,
        provider_session_id: str = "",
        followup_text: str = "",
        previous_summary_path: Path | None = None,
        provider_override: str = "",
        command_override: str = "",
    ) -> dict[str, object]:
        provider = _normalize_provider_name(provider_override or self.config.bug_analysis.provider)
        command_name = (command_override or self.config.bug_analysis.command).strip() or _default_command_for_provider(provider)
        if not provider or not command_name:
            return {"command": [], "provider": provider, "session_id": provider_session_id, "resumed": False}
        prompt = self._build_bug_agent_summary_prompt(
            request_text=request_text,
            request_artifact=request_artifact,
            metadata_path=metadata_path,
            followup_text=followup_text,
            previous_summary_path=previous_summary_path,
        )
        session_id = provider_session_id.strip()
        if provider == "codex":
            if session_id:
                command = [
                    command_name,
                    "exec",
                    "resume",
                    "--skip-git-repo-check",
                    "--json",
                    "--output-last-message",
                    str(output_path),
                    session_id,
                    prompt,
                ]
            else:
                command = [
                    command_name,
                    "exec",
                    "--skip-git-repo-check",
                    "-s",
                    "read-only",
                    "-C",
                    str(self._working_dir()),
                    "--json",
                    "--output-last-message",
                    str(output_path),
                    prompt,
                ]
            return {
                "command": command,
                "provider": provider,
                "session_id": session_id,
                "resumed": bool(session_id),
            }
        if provider in {"claude", "claude-code", "claude_code"}:
            allowed_tools = self.config.claude_agent.allowed_tools or ["Read", "Grep", "Glob", "LS"]
            session_id = session_id or str(uuid.uuid4())
            command = [
                command_name,
                "--print",
                "--output-format",
                "text",
                "--permission-mode",
                "dontAsk",
                "--allowedTools",
                ",".join(allowed_tools),
                "--append-system-prompt",
                (
                    "你是一个通过飞书触发的 bug 分析总结 agent。"
                    "只读分析，不修改文件，不执行写入命令。"
                    "必须完整响应用户原始请求中的所有诉求，输出中文 Markdown，结论先行。"
                ),
            ]
            for directory in self._bug_summary_add_dirs():
                command.extend(["--add-dir", str(directory)])
            if provider_session_id.strip():
                command.extend(["--resume", session_id])
            else:
                command.extend(["--session-id", session_id])
            command.append(prompt)
            return {
                "command": command,
                "provider": provider,
                "session_id": session_id,
                "resumed": bool(provider_session_id.strip()),
            }
        return {"command": [], "provider": provider, "session_id": session_id, "resumed": False}

    def _build_bug_agent_summary_fallback_command(
        self,
        *,
        request_text: str,
        request_artifact: Path,
        metadata_path: Path,
        output_path: Path,
        followup_text: str = "",
        previous_summary_path: Path | None = None,
    ) -> dict[str, object]:
        candidates = _provider_candidates(self.config.bug_analysis.provider, self.config.bug_analysis.command)
        if len(candidates) < 2:
            return {"command": [], "provider": "", "session_id": "", "resumed": False}
        provider, command_name = candidates[1]
        return self._build_bug_agent_summary_command(
            request_text=request_text,
            request_artifact=request_artifact,
            metadata_path=metadata_path,
            output_path=output_path,
            provider_session_id="",
            followup_text=followup_text,
            previous_summary_path=previous_summary_path,
            provider_override=provider,
            command_override=command_name,
        )

    def _build_bug_agent_summary_prompt(
        self,
        *,
        request_text: str,
        request_artifact: Path,
        metadata_path: Path,
        followup_text: str = "",
        previous_summary_path: Path | None = None,
    ) -> str:
        prompt = "请基于以下本地文件完成同一个 bug 会话的最终回答。\n要求：\n"
        if followup_text.strip():
            prompt += (
                "1. 这是一条续聊/追问，必须直接回答这次新问题，并延续上一轮 Agent 会话。\n"
                "2. 优先复用 metadata 中已经给出的日志、报告、output 目录和历史总结，不要要求用户重新上传日志。\n"
                "3. 只读分析，不修改任何文件。\n"
                "4. 输出中文 Markdown，结论先行，再给出证据。\n"
                "5. 如果现有日志/报告仍不足以覆盖某个诉求，要明确指出缺口，但先回答已经能确认的部分。\n\n"
            )
        else:
            prompt += (
                "1. 本次是全新 bug 分析请求，不是续聊/修正；不要虚构“上一轮分析”“本次修正”“延续上一轮”这类诉求或标题。\n"
                "2. 必须完整覆盖用户原始请求里的所有诉求，不要只回答其中一部分。\n"
                "3. 只读分析，不修改任何文件。\n"
                "4. 输出中文 Markdown，结论先行，随后按“诉求 -> 结论 -> 证据”组织；诉求标题只能来自用户原始请求，不要自行添加不存在的诉求。\n"
                "5. 如果脚本结果无法覆盖用户某个诉求，要明确指出缺口。\n"
                "6. metadata 中的“本轮脚本初步摘要”只是当前自动脚本输出，不要把它写成“上一轮结论”；只有显式提供 followup/previous summary 时，才能讨论修正上一轮结论。\n"
                "7. 如果 metadata 或报告里已经明确给出故障时间对应的主会话 / 主 PID / focus session，请优先围绕该主会话分析，不要展开无关会话；只有在需要证明时间不匹配时才提及其他会话。\n\n"
            )
        prompt += (
            "统一输出结构：请按以下中文二级标题组织最终回答，并只填入本次 bug 自身的证据，不套用示例业务词。\n"
            "## 结论摘要\n"
            "- 先给 3 到 5 条最重要结论，必须标明置信边界。\n"
            "## 关键证据\n"
            "- 每条证据尽量带文件、行号、时间、进程/package 或源码位置。\n"
            "## 最可能原因\n"
            "- 按可能性排序，说明支持证据和缺口；证据不足时明确不要强行定根因。\n"
            "## 待确认项\n"
            "- 只列真实证据缺口，例如精确时间、日志片段、运行状态、源码链路缺口。\n"
            "## 建议动作\n"
            "- 给出下一轮可执行动作，例如补日志、重跑某个 skill、沿某个源码或日志点继续查。\n\n"
        )
        prompt += f"用户原始请求：\n{request_text}\n\n"
        if followup_text.strip():
            prompt += f"本次追问/修正：\n{followup_text.strip()}\n\n"
        prompt += (
            "可读取路径：\n"
            f"- 工作区根目录：{self._working_dir()}\n"
            f"- 业务源码根目录：{self.config.guideengine_repo}\n\n"
            "以下是首批本地文件内容。你可以继续只读读取上述目录下与当前问题直接相关的源码、日志和报告，"
            "但不要修改文件，不要编造未看到的证据：\n\n"
        )
        if followup_text.strip():
            prompt += (
                "续聊性能约束：优先根据下面的精简上下文回答。"
                "只有精简上下文无法证明时，才读取 metadata 中列出的报告、日志或源码路径。\n\n"
            )
            if previous_summary_path is not None:
                prompt += self._render_embedded_file(previous_summary_path, title="上一轮 Agent 总结", max_chars=2500)
            prompt += self._render_embedded_file(request_artifact, title="Bug Agent Follow-up Request", max_chars=3500)
            prompt += self._render_embedded_file(metadata_path, title="Bug Follow-up Metadata", max_chars=3500)
        else:
            if previous_summary_path is not None:
                prompt += self._render_embedded_file(previous_summary_path, title="上一轮 Agent 总结")
            prompt += self._render_embedded_file(request_artifact, title="Bug Agent Request")
            prompt += self._render_embedded_file(metadata_path, title="Bug Metadata")
        return prompt

    def _bug_summary_add_dirs(self) -> list[Path]:
        candidates = [self._working_dir(), self.config.guideengine_repo, *self.config.claude_agent.add_dirs]
        resolved: list[Path] = []
        seen: set[Path] = set()
        for candidate in candidates:
            path = Path(candidate).expanduser().resolve()
            if path in seen:
                continue
            seen.add(path)
            resolved.append(path)
        return resolved

    def _render_embedded_file(self, path: Path, *, title: str, max_chars: int = 6000) -> str:
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            return f"## {title}\n路径: `{path}`\n读取失败: {exc}\n\n"
        normalized = content.strip()
        if len(normalized) > max_chars:
            normalized = normalized[: max_chars - 1].rstrip() + "…"
        return f"## {title}\n路径: `{path}`\n\n```text\n{normalized or '(空文件)'}\n```\n\n"


@dataclass(slots=True)
class BugAnalysisPlan:
    kind: str
    signal_code: str | None = None


@dataclass(slots=True)
class BugAnalysisSelection:
    plans: list["BugAnalysisPlan"]
    skill_name: str
    skill_label: str
    source: str
    reason: str = ""
    provider: str = ""


@dataclass(slots=True)
class BugFollowupSelection:
    should_reanalyze: bool
    force_rerun: bool
    plans: list["BugAnalysisPlan"]
    skill_name: str
    skill_label: str
    source: str
    reason: str = ""
    provider: str = ""


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

STARTUP_BLOCK_ROUTE_TERMS = (
    "打不开",
    "无法打开",
    "进不去",
    "无法进入",
    "未拉起",
    "没拉起",
    "没起来",
    "起不来",
    "黑屏只有logo",
    "黑屏只有 logo",
    "只有logo",
    "只有 logo",
    "只显示logo",
    "只显示 logo",
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

XTHEME_ROUTE_TERMS = (
    "xtheme",
    "signal_sr_xtheme",
    "105004",
    "105009",
    "时光主题",
    "时光变化",
    "晨曦",
    "傍晚",
    "黄昏",
    "日出日落",
    "主题切换",
    "xuiconditionhelper",
)


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
            completed = run_tracked_process(
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
