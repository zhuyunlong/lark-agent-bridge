"""Local agent integrations for Claude Code, Codex, and omlx chat."""

from __future__ import annotations

from dataclasses import dataclass
import importlib.util
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

    def __init__(self, config: BridgeConfig) -> None:
        self.config = config

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
                completed = subprocess.run(
                    command,
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


class BugAnalysisRunner:
    def __init__(self, config: BridgeConfig) -> None:
        self.config = config

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
        bug_dir = context.input_dir / f"bug_{self._bug_id(request.bug_url)}"
        metadata_path = context.output_dir / "bug_metadata.md"
        request_text = self._request_text(raw_text=request.raw_text, prompt_text=prompt_text, bug_url=request.bug_url)
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
            self._emit_progress(progress_callback, stage="bug_download_logs", message="下载 bug 附件和日志")
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
                    input_for_plan = self._startup_analysis_input(prepared_input, fault_time)
                current_command = self.build_command(
                    plan=current_plan,
                    input_path=input_for_plan or bug_dir,
                    html_path=current_html,
                    json_path=current_json,
                    analysis_dir=current_analysis_dir,
                    target_time=fault_time if current_plan.kind == "startup" else None,
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
                completed = self._run_analysis(
                    plan=current_plan,
                    input_path=input_for_plan,
                    html_path=current_html,
                    json_path=current_json,
                    analysis_dir=current_analysis_dir,
                    timeout=options.timeout_seconds,
                    target_time=fault_time if current_plan.kind == "startup" else None,
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
            )
            metadata_path.write_text(metadata_text, encoding="utf-8")
            combined_artifacts = self._build_combined_report_artifacts(
                plans=plans,
                prompt_text=prompt_text,
                fault_time=fault_time,
                output_dir=context.output_dir,
                html_paths=html_paths,
                report_jsons=report_jsons,
                selected_input=selected_input,
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
            "signal_code": plan.signal_code,
            "selected_log_input": str(selected_input) if selected_input else "",
            "prepared_log_input": str(prepared_input) if prepared_input else "",
            "bug_dir": str(bug_dir),
            "user_request_text": request_text,
            "agent_request_file": str(request_artifact),
            "agent_summary_file": str(agent_summary_path),
        }
        final_message = summary
        if agent_summary_result["message"]:
            final_message = str(agent_summary_result["message"])
        if agent_summary_result["command"]:
            details["agent_summary_command"] = list(agent_summary_result["command"])
        if agent_summary_result["error"]:
            details["agent_summary_error"] = str(agent_summary_result["error"])
        if agent_summary_result["provider"]:
            details["agent_summary_provider"] = str(agent_summary_result["provider"])
        if agent_summary_result["session_id"]:
            details["agent_summary_session_id"] = str(agent_summary_result["session_id"])
        if agent_summary_result["resumed"]:
            details["agent_summary_resumed"] = True
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
        if prepared_input is None:
            return TaskResult(
                success=False,
                message="无法复用上次 Bug 分析：未找到已准备好的日志输入，不能安全地跳过下载/解密步骤。",
                job_id=job_id,
                job_dir=job_dir,
                duration_seconds=time.monotonic() - started,
                error_code="bug_reanalysis_missing_prepared_input",
                details={"mode": "bug_reanalysis"},
            )

        plans = self._plans_from_previous_details(details, fallback_text=request_text)
        prompt_text = f"{request_text}\n追问/修正：{followup_text}".strip()
        self._emit_progress(
            progress_callback,
            stage="bug_reanalysis_reuse_context",
            message="复用上一轮 bug 分析上下文，不重新拉取/下载/解密日志",
            job_id=job_id,
            prepared_log_input=str(prepared_input),
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
                should_rerun = plan.kind == "startup" or not html_path.exists() or not json_path.exists()
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
                    input_path=input_for_plan,
                    html_path=html_path,
                    json_path=json_path,
                    analysis_dir=analysis_dir,
                    target_time=target_time if plan.kind == "startup" else None,
                )
                self._emit_progress(
                    progress_callback,
                    stage="bug_reanalysis_run_analysis",
                    message=f"基于已准备日志重新执行{self._analysis_label(plan.kind)}",
                    plan=plan.kind,
                    plan_label=self._analysis_label(plan.kind),
                    html_path=str(html_path),
                    json_path=str(json_path),
                    target_time=target_time if plan.kind == "startup" else "",
                )
                completed = self._run_analysis(
                    plan=plan,
                    input_path=input_for_plan,
                    html_path=html_path,
                    json_path=json_path,
                    analysis_dir=analysis_dir,
                    timeout=self.config.bug_analysis.timeout_seconds,
                    target_time=target_time if plan.kind == "startup" else None,
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
            provider_session_id=str(details.get("agent_summary_session_id") or ""),
            followup_text=followup_text,
            previous_summary_path=previous_summary_path,
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
        if combined_artifacts is not None:
            result_details["combined_report_html"] = str(combined_artifacts["html_path"])
            result_details["combined_report_json"] = str(combined_artifacts["json_path"])
        if agent_summary_result["command"]:
            result_details["agent_summary_command"] = list(agent_summary_result["command"])
        if agent_summary_result["error"]:
            result_details["agent_summary_error"] = str(agent_summary_result["error"])
        if agent_summary_result["provider"]:
            result_details["agent_summary_provider"] = str(agent_summary_result["provider"])
        if agent_summary_result["session_id"]:
            result_details["agent_summary_session_id"] = str(agent_summary_result["session_id"])
        if agent_summary_result["resumed"]:
            result_details["agent_summary_resumed"] = True
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
        provider_session_id = str(details.get("agent_summary_session_id") or "").strip()
        self._emit_progress(
            progress_callback,
            stage="bug_agent_followup_prepare",
            message="复用上一轮 bug 会话，继续调用本地 Agent 分析追问",
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
        if agent_summary_result["command"]:
            result_details["agent_summary_command"] = list(agent_summary_result["command"])
        if agent_summary_result["error"]:
            result_details["agent_summary_error"] = str(agent_summary_result["error"])
        if agent_summary_result["provider"]:
            result_details["agent_summary_provider"] = str(agent_summary_result["provider"])
        if agent_summary_result["session_id"]:
            result_details["agent_summary_session_id"] = str(agent_summary_result["session_id"])
        if agent_summary_result["resumed"]:
            result_details["agent_summary_resumed"] = True
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
                target_time=fault_time if current_plan.kind == "startup" else None,
            )
            try:
                self._emit_progress(
                    progress_callback,
                    stage="direct_run_analysis",
                    message=f"执行{self._analysis_label(current_plan.kind)}",
                    plan=current_plan.kind,
                    plan_label=self._analysis_label(current_plan.kind),
                )
                completed = self._run_analysis(
                    plan=current_plan,
                    input_path=input_for_plan,
                    html_path=current_html,
                    json_path=current_json,
                    analysis_dir=current_analysis_dir,
                    timeout=self.config.bug_analysis.timeout_seconds,
                    target_time=fault_time if current_plan.kind == "startup" else None,
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
        startup_requested = any(term in lowered for term in STARTUP_ROUTE_TERMS)
        stuck_requested = any(term in lowered for term in STUCK_ROUTE_TERMS)
        startup_blocked = any(term in lowered for term in STARTUP_BLOCK_ROUTE_TERMS)
        if startup_requested or (stuck_requested and startup_blocked):
            plans.append(BugAnalysisPlan(kind="startup"))
        if stuck_requested:
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
        target_time: str | None = None,
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
            if kind in {"startup", "stuck", "crash", "signal", "perception"}
        ]
        if plans:
            return plans
        return self.classify_requests(prompt_text=fallback_text, title="", description="")

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
        return True

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
    ) -> subprocess.CompletedProcess[str]:
        command = self.build_command(
            plan=plan,
            input_path=input_path or self.config.workspace_root,
            html_path=html_path,
            json_path=json_path,
            analysis_dir=analysis_dir,
            target_time=target_time,
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
        request_text: str,
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
            "- 用户原始请求:\n\n```text\n"
            f"{request_text}\n"
            "```\n"
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
    ) -> dict[str, object] | None:
        kinds = [plan.kind for plan in plans]
        if kinds != ["startup", "stuck"]:
            return None
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
        report_html = self._load_shared_report_html()
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
                    f"montecarlo CPU {system_load.get('process_cpu', '?')}% / RSS {system_load.get('process_mem_rss_kb', '?')}KB"
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
        body_html = (
            '<div class="container">'
            '<h1>🎮 3D启动卡顿综合报告</h1>'
            f'<div class="sub">分析请求：<code>{report_html.H(prompt_text)}</code><br/>'
            f'启动报告：<code>{report_html.H(startup_html or self._report_name("startup", "html"))}</code> · '
            f'卡顿报告：<code>{report_html.H(stuck_html or self._report_name("stuck", "html"))}</code></div>'
            f'<div class="verdict v-{report_html.H(startup_sev if startup_sev == "red" else stuck_sev)}">🎯 {report_html.H(startup_message)}；{report_html.H(stuck_message)}</div>'
            f'<div class="cards">{report_html.render_cards(cards)}</div>'
            f'<div class="section"><h2>📋 异常摘要</h2>{report_html.render_issue_list(issues)}</div>'
            f'{report_html.render_chain(chain_nodes, title="🧠 启动卡顿综合链路", description="先锁主会话，再串 ROM/上下电/启动链/卡顿窗口系统压力。")}'
            f'<div class="section"><h2>T. 目标时间窗与主会话</h2>{report_html.render_table(target_rows, ["项目", "内容"])}</div>'
            f'<div class="section"><h2>P. 启动时刻系统负载</h2>{report_html.render_table(system_rows, ["时间", "Total", "User", "System", "iow", "montecarlo CPU"], empty_text="未命中 DFX-SystemMonitor 同时间窗样本")}</div>'
            '<div class="section"><h2>A. 单报告说明</h2>'
            '<p class="muted">底层仍保留单独的 startup / stuck 报告，当前综合报告只负责把“启动链路异常”和“卡顿窗口证据”收口成一份可上传的 HTML。</p>'
            '</div>'
            '</div>'
        )
        return report_html.render_document("3D启动卡顿综合报告", body_html)

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

    def _load_shared_report_html(self):
        module_path = self.config.workspace_root / ".ai/skills/sr-skill-common/report_html.py"
        spec = importlib.util.spec_from_file_location("lark_bridge_report_html", module_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"无法加载共享 HTML 模板: {module_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

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
            completed = subprocess.run(
                command,
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
            }
        if output_path.exists():
            message = output_path.read_text(encoding="utf-8").strip()
        else:
            message = completed.stdout.strip()
            if message:
                output_path.write_text(message, encoding="utf-8")
        if not message:
            return {
                "message": "",
                "command": command,
                "error": "empty_agent_summary",
                "provider": provider,
                "session_id": session_id,
                "resumed": resumed,
            }
        resolved_session_id = self._extract_bug_agent_session_id(provider, completed.stdout, fallback=session_id)
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
        plan_lines = "\n".join(f"- {self._analysis_label(plan.kind)} (`{plan.kind}`)" for plan in plans)
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
        prepared_input: Path,
        selected_input: Path | None,
        plans: list["BugAnalysisPlan"],
        rerun_kinds: list[str],
        reused_kinds: list[str],
        html_paths: list[Path],
        report_jsons: dict[str, Path | None],
        combined_artifacts: dict[str, object] | None,
        previous_summary_path: Path | None,
    ) -> str:
        lines = [
            "# Bug Reanalysis Metadata",
            "",
            f"- Job ID: `{job_id}`",
            f"- 用户原始请求: `{request_text}`",
            f"- 本次追问/修正: `{followup_text}`",
            f"- 修正后的故障时间: `{target_time or '未识别'}`",
            f"- 复用 prepared log 输入: `{prepared_input}`",
            f"- 上一轮选中的日志输入: `{selected_input or ''}`",
            f"- 分析类型: `{', '.join(plan.kind for plan in plans) or '无'}`",
            f"- 本次重新执行: `{', '.join(rerun_kinds) or '无'}`",
            f"- 本次直接复用: `{', '.join(reused_kinds) or '无'}`",
        ]
        if previous_summary_path is not None:
            lines.append(f"- 上一轮 Agent 总结: `{previous_summary_path}`")
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
            session_id = session_id or str(uuid.uuid4())
            command = [
                command_name,
                "--print",
                "--output-format",
                "text",
                "--permission-mode",
                "dontAsk",
                "--tools",
                "",
                "--append-system-prompt",
                (
                    "你是一个通过飞书触发的 bug 分析总结 agent。"
                    "只读分析，不修改文件，不执行写入命令。"
                    "必须完整响应用户原始请求中的所有诉求，输出中文 Markdown，结论先行。"
                ),
            ]
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
                "1. 必须完整覆盖用户原始请求里的所有诉求，不要只回答其中一部分。\n"
                "2. 只读分析，不修改任何文件。\n"
                "3. 输出中文 Markdown，结论先行，随后按“诉求 -> 结论 -> 证据”组织。\n"
                "4. 如果脚本结果无法覆盖用户某个诉求，要明确指出缺口。\n"
                "5. 如果这是续聊/修正，请延续上一轮分析上下文，明确本次修正改动了什么结论。\n"
                "6. 如果 metadata 或报告里已经明确给出故障时间对应的主会话 / 主 PID / focus session，请优先围绕该主会话分析，不要展开无关会话；只有在需要证明时间不匹配时才提及其他会话。\n\n"
            )
        prompt += f"用户原始请求：\n{request_text}\n\n"
        if followup_text.strip():
            prompt += f"本次追问/修正：\n{followup_text.strip()}\n\n"
        prompt += "以下是本地文件内容，请直接基于这些内容回答，不要假设还能读取其他未列出的文件：\n\n"
        if previous_summary_path is not None:
            prompt += self._render_embedded_file(previous_summary_path, title="上一轮 Agent 总结")
        prompt += self._render_embedded_file(request_artifact, title="Bug Agent Request")
        prompt += self._render_embedded_file(metadata_path, title="Bug Metadata")
        return prompt

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
