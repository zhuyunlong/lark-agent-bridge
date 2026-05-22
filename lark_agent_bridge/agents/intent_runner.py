"""Intent analysis runner."""

from __future__ import annotations

import importlib
import json
import math
from pathlib import Path
import shlex
import subprocess
import tempfile
import time
import uuid

from ..health import ProcessWatchdog, _safe_terminate
from ..log import get_logger
from ..models import BridgeConfig, IntentDecision, LarkEvent
from ._helpers import _provider_candidates

logger = get_logger("agents")


def _run_tracked_process(*args, **kwargs):
    return importlib.import_module("lark_agent_bridge.agents").run_tracked_process(*args, **kwargs)


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
                completed = _run_tracked_process(
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
            '- "route": "signal" | "bug" | "direct_analysis" | "perception_summary" | "analysis_followup" | "chat" | "unsupported"\n'
            '- "followup_action": "continue_agent" | "reanalysis" | "context_chat" | "none"\n'
            '- "context_source": "explicit" | "latest_chat" | "none"\n'
            '- "confidence": "high" | "medium" | "low"\n'
            '- "reason": 一句简短中文说明\n\n'
            "判断规则：\n"
            "1. analysis_followup 表示用户在继续同一个已有分析会话。\n"
            "   群聊里如果没有 has_reply_link，不能仅因 latest_chat_context 主题相似就选 analysis_followup；"
            "看起来像新的分析命令时应选择对应的新请求路径。\n"
            "2. 对 bug 续聊，如果用户是在修正时间、要求重跑、要求基于同一份已下载日志重新生成结论/报告，选 reanalysis；"
            "如果是基于现有日志/报告继续追问、补充搜索、要求继续分析，选 continue_agent。\n"
            "3. 非 bug 的历史分析追问，若只是基于已有摘要/报告继续问答，选 context_chat。\n"
            "4. 如果消息是普通闲聊、问候、解释型问题，选 chat。\n"
            "5. 如果消息是在发新的 bug 链接分析请求，选 bug；如果是带附件/URL 的日志分析请求但不是 bug 链接，选 direct_analysis；"
            "如果是信号生命周期调查，只有在用户明确给出单个 SignalCode / SIGNAL_... 并询问信号来源、是否送达或链路时才选 signal；"
            "3D场景信号、SceneType、上电P、临停P、特殊场景等属于更专一的场景信号分析，带 bug 链接选 bug，带附件/URL 日志选 direct_analysis，不能因为含“信号”二字就选 signal。"
            "如果是感知总结，选 perception_summary；如果是代码/仓库分析但没有明确日志、Bug 或已配置入口，选 unsupported。\n"
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
            model = (self.config.intent_analysis.model or self.config.bug_analysis.model or "").strip()
            command = [
                command_name,
                "exec",
                "--skip-git-repo-check",
                "-s",
                "read-only",
                "-C",
                str(self._working_dir()),
            ]
            if model:
                command.extend(["-m", model])
            command.extend(
                [
                    "--output-last-message",
                    str(output_path),
                    f"{system_prompt}\n\n{prompt}",
                ]
            )
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
