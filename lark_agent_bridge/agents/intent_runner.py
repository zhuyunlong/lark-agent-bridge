"""Intent analysis runner.

Supports three backends:
1. **Pydantic AI Agent** (best) — when pydantic-ai is installed and
   ``[ai_provider].enabled = true``, uses structured output validation
   with automatic retry on schema failures. Typical latency: 3-10 seconds.
2. **Direct API** — raw LLM HTTP call with manual JSON parsing. Typical
   latency: 3-10 seconds.
3. **Subprocess CLI** — explicit legacy route-classifier when configured,
   or optional fallback when ``allow_subprocess_fallback`` is enabled.
"""

from __future__ import annotations

import importlib
import json
import re
from pathlib import Path
import subprocess
import tempfile
import time
from typing import TYPE_CHECKING

from ..health import ProcessWatchdog, _safe_terminate
from ..log import get_logger
from ..models import BridgeConfig, IntentDecision, LarkEvent
from ._helpers import _provider_candidates
from .llm_client import LLMClient, LLMClientError
from .pydantic_agents import IntentAgent

logger = get_logger("agents")

if TYPE_CHECKING:
    from ..replay import AnalysisReplayContext, ReplayDecision


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
    REPLAY_ACTIONS = {"answer_from_existing", "reanalyze", "clarify", "new_request", "unsupported"}

    def __init__(self, config: BridgeConfig, process_watchdog: ProcessWatchdog | None = None) -> None:
        self.config = config
        self.process_watchdog = process_watchdog
        self._llm_client: LLMClient | None = None
        self._intent_agent: IntentAgent | None = None
        if config.ai_provider.enabled and config.ai_provider.base_url and config.ai_provider.primary_model:
            self._llm_client = LLMClient(config.ai_provider)
            # Try to initialize pydantic-ai agent (graceful if not installed)
            try:
                agent = IntentAgent(config.ai_provider)
                if agent.is_available():
                    self._intent_agent = agent
                    logger.info("Pydantic AI IntentAgent enabled (structured output validation)")
            except Exception as exc:
                logger.debug("Pydantic AI IntentAgent not available: %s", exc)

    def is_enabled(self) -> bool:
        if self._llm_client is not None and self._llm_client.is_available():
            return True
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

        if self.config.dry_run:
            return IntentDecision(
                route="unsupported",
                reason="dry-run: intent analysis command planned but not executed",
                confidence="low",
                followup_action="none",
                context_source="none",
            )

        # --- Path 0: Pydantic AI Agent (best — structured output + auto-retry) ---
        if self._intent_agent is not None and self._intent_agent.is_available():
            try:
                return self._classify_via_pydantic_agent(prompt)
            except Exception as exc:
                logger.warning("Pydantic AI intent classification failed, trying direct API: %s", exc)

        # --- Path 1: Direct API (fast, preferred) ---
        if self._llm_client is not None and self._llm_client.is_available():
            try:
                return self._classify_via_api(prompt)
            except (LLMClientError, ValueError) as exc:
                if not self.config.intent_analysis.allow_subprocess_fallback:
                    raise IntentAnalysisFailure(
                        f"Direct API intent classification failed: {exc}",
                        error_code="intent_analysis_api_failed",
                        stderr=str(exc),
                    ) from exc
                logger.warning("Direct API intent classification failed, trying subprocess fallback: %s", exc)

        # --- Path 2: Subprocess CLI (legacy fallback) ---
        return self._classify_via_subprocess(prompt)

    def decide_replay(self, *, context: "AnalysisReplayContext") -> "ReplayDecision":
        if not self.is_enabled():
            raise IntentAnalysisFailure("intent analysis is disabled", error_code="intent_analysis_disabled")

        prompt = self._build_replay_prompt(context=context)

        if self.config.dry_run:
            replay_module = importlib.import_module("lark_agent_bridge.replay")
            ReplayDecision = replay_module.ReplayDecision
            return ReplayDecision(
                action="clarify",
                mode=getattr(context, "mode", "unsupported"),
                reason="dry-run: replay decision command planned but not executed",
                confidence="low",
            )

        if self._llm_client is not None and self._llm_client.is_available():
            try:
                return self._decide_replay_via_api(prompt)
            except (LLMClientError, ValueError) as exc:
                if not self.config.intent_analysis.allow_subprocess_fallback:
                    raise IntentAnalysisFailure(
                        f"Direct API replay decision failed: {exc}",
                        error_code="replay_decision_api_failed",
                        stderr=str(exc),
                    ) from exc
                logger.warning("Direct API replay decision failed, trying subprocess fallback: %s", exc)

        return self._decide_replay_via_subprocess(prompt)

    def _classify_via_pydantic_agent(self, prompt: str) -> IntentDecision:
        """Classify intent via pydantic-ai Agent with structured output validation.

        This path uses Agent(output_type=IntentOutput) which:
        1. Instructs the LLM to return JSON matching IntentOutput schema
        2. Auto-validates the response with Pydantic
        3. Auto-retries with error feedback if validation fails

        No manual JSON parsing needed — pydantic-ai handles it all.
        """
        assert self._intent_agent is not None
        system_prompt = self.config.intent_analysis.system_prompt

        result = self._intent_agent.classify(
            system_prompt=system_prompt,
            user_prompt=prompt,
        )

        # Convert IntentOutput (pydantic BaseModel) to IntentDecision (dataclass)
        from .pydantic_models import IntentOutput

        output: IntentOutput = result.output  # type: ignore[assignment]
        decision = IntentDecision(
            route=output.route,
            confidence=output.confidence,
            reason=output.reason,
            followup_action=output.followup_action,
            context_source=output.context_source,
            raw_response=f"[pydantic-ai] model={result.model} duration={result.duration_seconds:.1f}s",
        )
        logger.info(
            "Intent classified via pydantic-ai: route=%s confidence=%s duration=%.1fs model=%s",
            decision.route,
            decision.confidence,
            result.duration_seconds,
            result.model,
        )
        return decision

    def _classify_via_api(self, prompt: str) -> IntentDecision:
        """Classify intent via direct LLM API call (3-10 seconds)."""
        assert self._llm_client is not None
        system_prompt = self.config.intent_analysis.system_prompt
        max_retries = self.config.ai_provider.intent_max_retries

        last_error: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                response = self._llm_client.classify_intent(
                    system_prompt=system_prompt,
                    user_prompt=prompt,
                )
                decision = self._parse_decision(response.content)
                decision.raw_response = response.content
                logger.info(
                    "Intent classified via API: route=%s confidence=%s duration=%.1fs model=%s",
                    decision.route,
                    decision.confidence,
                    response.duration_seconds,
                    response.model,
                )
                return decision
            except ValueError as exc:
                last_error = exc
                if attempt < max_retries:
                    logger.warning("Intent parse failed (attempt %d/%d): %s", attempt + 1, max_retries + 1, exc)
                    continue
                raise
            except LLMClientError:
                raise

        raise IntentAnalysisFailure(
            f"Intent classification failed after {max_retries + 1} attempts: {last_error}",
            error_code="intent_analysis_api_failed",
        )

    def _classify_via_subprocess(self, prompt: str) -> IntentDecision:
        """Classify intent via subprocess CLI call (legacy, 120-300 seconds)."""
        primary_command, primary_output_path = self._build_command(prompt)
        if not primary_command:
            raise IntentAnalysisFailure("intent analysis command is not configured", error_code="intent_analysis_not_configured")
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

    def _decide_replay_via_api(self, prompt: str) -> "ReplayDecision":
        assert self._llm_client is not None
        system_prompt = self.config.intent_analysis.system_prompt
        max_retries = self.config.ai_provider.intent_max_retries

        last_error: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                response = self._llm_client.classify_intent(
                    system_prompt=system_prompt,
                    user_prompt=prompt,
                )
                decision = self._parse_replay_decision(response.content)
                logger.info(
                    "Replay decision via API: action=%s mode=%s confidence=%s duration=%.1fs model=%s",
                    decision.action,
                    decision.mode,
                    decision.confidence,
                    response.duration_seconds,
                    response.model,
                )
                return decision
            except ValueError as exc:
                last_error = exc
                if attempt < max_retries:
                    logger.warning("Replay decision parse failed (attempt %d/%d): %s", attempt + 1, max_retries + 1, exc)
                    continue
                raise
            except LLMClientError:
                raise

        raise IntentAnalysisFailure(
            f"Replay decision failed after {max_retries + 1} attempts: {last_error}",
            error_code="replay_decision_api_failed",
        )

    def _decide_replay_via_subprocess(self, prompt: str) -> "ReplayDecision":
        primary_command, primary_output_path = self._build_command(prompt)
        if not primary_command:
            raise IntentAnalysisFailure("intent analysis command is not configured", error_code="intent_analysis_not_configured")
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
                    name="replay-decision-agent",
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
                    "分析续聊决策超时",
                    error_code="replay_decision_timeout",
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
                    f"分析续聊决策启动失败: {exc}",
                    error_code="replay_decision_failed_to_start",
                    command=command,
                    stderr=str(exc),
                )
                if index + 1 < len(attempts):
                    continue
                raise last_failure from exc
            raw_response = self._read_response(completed=completed, output_path=output_path)
            if completed.returncode != 0:
                last_failure = IntentAnalysisFailure(
                    "分析续聊决策失败",
                    error_code="replay_decision_failed",
                    command=command,
                    stdout=completed.stdout,
                    stderr=completed.stderr or raw_response,
                )
                if index + 1 < len(attempts):
                    continue
                raise last_failure
            try:
                return self._parse_replay_decision(raw_response)
            except ValueError as exc:
                last_failure = IntentAnalysisFailure(
                    f"分析续聊决策结果无法解析: {exc}",
                    error_code="replay_decision_bad_response",
                    command=command,
                    stdout=raw_response,
                    stderr=str(exc),
                )
                if index + 1 < len(attempts):
                    continue
                raise last_failure from exc
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
            "带 bug 链接或日志附件的业务现象分析应优先走 bug/direct_analysis，不能因为含“信号”二字就选 signal。"
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

    def _build_replay_prompt(self, *, context: "AnalysisReplayContext") -> str:
        payload = {
            "previous_mode": str(getattr(context, "previous_mode", "") or ""),
            "normalized_mode": str(getattr(context, "mode", "") or ""),
            "original_request_text": self._clip(str(getattr(context, "original_request_text", "") or ""), 2000),
            "current_user_text": self._clip(str(getattr(context, "current_text", "") or ""), 2000),
            "history": self._history_snapshot(getattr(context, "history", None), limit=8, clip_chars=800),
            "summary_text": self._clip(str(getattr(context, "summary_text", "") or ""), 1600),
            "report_excerpt": self._clip(str(getattr(context, "report_excerpt", "") or ""), 2000),
            "report_url": self._clip(str(getattr(context, "report_url", "") or ""), 500),
            "bug": {
                "url": self._clip(str(getattr(context, "bug_url", "") or ""), 500),
                "title": self._clip(str(getattr(context, "bug_title", "") or ""), 500),
                "description": self._clip(str(getattr(context, "bug_description", "") or ""), 2000),
            },
            "resources": self._replay_resource_snapshot(getattr(context, "resources", None)),
        }
        prompt = (
            "请判断这条飞书消息是已有分析会话的继续问答、需要重新分析、需要澄清，还是一个无关的新分析请求。\n"
            "只输出一个 JSON 对象，字段必须完整：\n"
            '- "action": "answer_from_existing" | "reanalyze" | "clarify" | "new_request" | "unsupported"\n'
            '- "mode": "bug" | "direct_analysis" | "signal_lifecycle" | "perception_summary" | "addr2line_resolve" | "rom_version_lookup" | "unsupported"\n'
            '- "confidence": "high" | "medium" | "low"\n'
            '- "reason": 一句简短中文说明\n'
            '- "analysis_kind": 可选，非 bug 默认空字符串\n'
            '- "skill_name": 可选，非 bug 默认空字符串\n'
            '- "signal_hint": 可选，信号相关时填写用户当前纠正或目标信号\n'
            '- "retry_download_if_missing": true | false\n'
            '- "normalized_request_text": 综合原始请求和当前修正后的重分析请求；如果不是重分析则可为空\n\n'
            "判断规则：\n"
            "1. 最新用户文本优先；如果它修正了时间、信号、分析方向、目标场景或补充缺失输入，选择 reanalyze。\n"
            "2. 用户明确说重新分析、重跑、重新生成报告、按新条件再查，选择 reanalyze。\n"
            "3. 只有用户是在询问既有结论、证据、报告内容、为什么这样判断，且不要求读取新资源或重跑时，选择 answer_from_existing。\n"
            "4. 只有消息明显开始一个无关分析任务时，选择 new_request。\n"
            "5. 请求重分析但关键资源既没有可用本地路径，也没有可重试的远程资源或 bug 链接时，选择 clarify。\n"
            "6. 不要选择具体 bug 分析 skill；bug 的 skill/plans 由 bug 专用决策后续决定。\n\n"
            "输入 JSON：\n"
            f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
        )
        max_chars = max(2000, int(self.config.intent_analysis.max_prompt_chars))
        if len(prompt) <= max_chars:
            return prompt
        return prompt[: max_chars - 1].rstrip() + "…"

    def _history_snapshot(self, history: object, *, limit: int, clip_chars: int) -> list[dict[str, str]]:
        if not isinstance(history, list):
            return []
        items: list[dict[str, str]] = []
        for item in history[-limit:]:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").strip()
            content = str(item.get("content") or "").strip()
            if role and content:
                items.append({"role": role, "content": self._clip(content, clip_chars)})
        return items

    def _replay_resource_snapshot(self, resources: object | None) -> dict[str, object]:
        if resources is None:
            return {
                "current": [],
                "reply_chain": [],
                "session": [],
                "local_existing": [],
                "remote": [],
                "missing_local_values": [],
            }
        return {
            "current": self._resource_descriptors(getattr(resources, "current", None)),
            "reply_chain": self._resource_descriptors(getattr(resources, "reply_chain", None)),
            "session": self._resource_descriptors(getattr(resources, "session", None)),
            "local_existing": self._resource_descriptors(getattr(resources, "local_existing", None)),
            "remote": self._resource_descriptors(getattr(resources, "remote", None)),
            "missing_local_values": [
                self._clip(str(value), 500)
                for value in (getattr(resources, "missing_local_values", None) or [])
            ],
        }

    def _resource_descriptors(self, resources: object) -> list[dict[str, str]]:
        if not isinstance(resources, list):
            return []
        descriptors: list[dict[str, str]] = []
        for item in resources[:20]:
            kind = str(getattr(item, "kind", "") or "")
            value = str(getattr(item, "value", "") or "")
            if not kind and not value:
                continue
            descriptors.append(
                {
                    "kind": self._clip(kind, 80),
                    "value": self._clip(value, 500),
                    "display_name": self._clip(str(getattr(item, "display_name", "") or ""), 200),
                    "source_message_id": self._clip(str(getattr(item, "source_message_id", "") or ""), 120),
                }
            )
        return descriptors

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

    def _parse_replay_decision(self, raw_response: str) -> "ReplayDecision":
        replay_module = importlib.import_module("lark_agent_bridge.replay")
        ReplayDecision = replay_module.ReplayDecision
        normalize_replay_action = replay_module.normalize_replay_action
        normalize_replay_mode = replay_module.normalize_replay_mode

        payload = self._extract_json_payload(raw_response)
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError("response is not valid JSON") from exc
        if not isinstance(parsed, dict):
            raise ValueError("response must be a JSON object")
        action = normalize_replay_action(str(parsed.get("action") or "").strip())
        mode = normalize_replay_mode(str(parsed.get("mode") or "unsupported").strip())
        confidence = str(parsed.get("confidence") or "medium").strip()
        reason = str(parsed.get("reason") or "").strip()
        if action not in self.REPLAY_ACTIONS:
            raise ValueError(f"unknown replay action: {action}")
        if confidence not in {"high", "medium", "low"}:
            confidence = "medium"
        return ReplayDecision(
            action=action,
            mode=mode,
            reason=reason,
            confidence=confidence,
            analysis_kind=str(parsed.get("analysis_kind") or "").strip(),
            skill_name=str(parsed.get("skill_name") or "").strip(),
            signal_hint=str(parsed.get("signal_hint") or "").strip(),
            retry_download_if_missing=bool(parsed.get("retry_download_if_missing") or False),
            normalized_request_text=str(parsed.get("normalized_request_text") or "").strip(),
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
