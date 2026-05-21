"""OMLX chat client integration."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from ..log import get_logger
from ..models import BridgeConfig, TaskResult
from ._helpers import _extract_chat_answer

logger = get_logger("agents")


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

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if options.api_key:
            headers["Authorization"] = f"Bearer {options.api_key}"
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        started = time.monotonic()
        evidence_log_bundle: dict[str, object] | None = None
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
