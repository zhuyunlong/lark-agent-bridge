"""Unified LLM client for direct API calls.

Replaces subprocess-based CLI invocations (codex exec / claude --print)
with direct HTTP API calls. Supports two wire protocols:

* **OpenAI format** (``api_format="openai"``) — ``/v1/chat/completions``
  via the ``openai`` Python SDK. Works with OpenAI, omlx, DeepSeek, etc.
* **Anthropic format** (``api_format="anthropic"``) — ``/v1/messages``
  via plain ``urllib.request`` (no extra SDK). Works with MiMo, DeepSeek,
  yybb, cc-switch local proxy, and Anthropic-compatible gateways.

Key design decisions:
- Preset system lets users specify just ``preset = "mimo-claude"`` in config;
  base_url/models/api_format are auto-filled from built-in preset defaults.
- Supports custom ``base_url`` and ``api_key`` per provider so users can
  point at Codex web endpoints, Claude-compatible proxies, or local models.
- Provides structured output extraction with automatic JSON retry.
- Keeps a synchronous API to match the existing codebase (no asyncio).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from ..log import get_logger
from ..models import AIProviderOptions

logger = get_logger("llm_client")


@dataclass(slots=True)
class LLMResponse:
    """Result of a single LLM API call."""

    content: str = ""
    model: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    duration_seconds: float = 0.0
    provider_tag: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def prompt_tokens(self) -> int:
        return self.usage.get("prompt_tokens", 0)

    @property
    def completion_tokens(self) -> int:
        return self.usage.get("completion_tokens", 0)

    @property
    def total_tokens(self) -> int:
        return self.usage.get("total_tokens", 0)


class LLMClientError(RuntimeError):
    """Raised when all LLM providers fail."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "llm_api_error",
        provider: str = "",
        attempts: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.provider = provider
        self.attempts = attempts or []


class LLMClient:
    """Synchronous LLM client supporting OpenAI and Anthropic wire formats.

    Supports multiple providers via ``base_url`` + ``api_key`` pairs and
    automatic fallback when the primary provider fails.

    When ``options.preset`` is set, ``base_url``, ``primary_model``,
    ``fast_model``, and ``api_format`` are pre-filled from built-in presets
    so you only need to supply ``api_key``.
    """

    def __init__(self, options: AIProviderOptions) -> None:
        self.options = options
        self._openai_clients: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # OpenAI-format path (uses openai SDK)
    # ------------------------------------------------------------------

    def _get_openai_client(self, base_url: str, api_key: str) -> Any:
        """Return a cached ``openai.OpenAI`` client for the given endpoint."""
        cache_key = f"{base_url}|{api_key[:8] if api_key else ''}"
        if cache_key not in self._openai_clients:
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise LLMClientError(
                    "openai package is not installed. Run: pip install openai",
                    error_code="llm_missing_openai",
                ) from exc
            self._openai_clients[cache_key] = OpenAI(
                base_url=base_url or "https://api.openai.com/v1",
                api_key=api_key or "not-set",
                timeout=max(self.options.intent_timeout_seconds, self.options.summary_timeout_seconds) + 10,
            )
        return self._openai_clients[cache_key]

    def _call_openai_api(
        self,
        *,
        messages: list[dict[str, str]],
        model: str,
        base_url: str,
        api_key: str,
        max_tokens: int,
        temperature: float,
        timeout_seconds: float,
        response_format: dict[str, str] | None = None,
    ) -> tuple[str, dict[str, int], str]:
        """Call OpenAI-compatible /v1/chat/completions. Returns (content, usage, model)."""
        client = self._get_openai_client(base_url, api_key)
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "timeout": timeout_seconds,
        }
        if response_format:
            kwargs["response_format"] = response_format
        completion = client.chat.completions.create(**kwargs)
        content = ""
        if completion.choices:
            content = completion.choices[0].message.content or ""
        usage: dict[str, int] = {}
        if completion.usage:
            usage = {
                "prompt_tokens": completion.usage.prompt_tokens or 0,
                "completion_tokens": completion.usage.completion_tokens or 0,
                "total_tokens": completion.usage.total_tokens or 0,
            }
        resolved_model = getattr(completion, "model", model) or model
        return content, usage, resolved_model

    # ------------------------------------------------------------------
    # Anthropic-format path (pure urllib, no extra SDK)
    # ------------------------------------------------------------------

    def _call_anthropic_api(
        self,
        *,
        messages: list[dict[str, str]],
        model: str,
        base_url: str,
        api_key: str,
        max_tokens: int,
        temperature: float,
        timeout_seconds: float,
    ) -> tuple[str, dict[str, int], str]:
        """Call Anthropic-compatible /v1/messages endpoint.

        Handles both the ``ANTHROPIC_BASE_URL`` style (e.g.
        ``https://token-plan-cn.xiaomimimo.com/anthropic``) and plain base
        URLs (e.g. ``https://yybb.codes``).  Returns (content, usage, model).
        """
        # Separate system message from the conversation messages
        system_content = ""
        user_messages: list[dict[str, str]] = []
        for msg in messages:
            if msg["role"] == "system":
                system_content = msg["content"]
            else:
                user_messages.append({"role": msg["role"], "content": msg["content"]})

        # Build the endpoint URL
        url = base_url.rstrip("/")
        if url.endswith("/v1"):
            url += "/messages"
        else:
            url += "/v1/messages"

        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": user_messages,
        }
        if system_content:
            payload["system"] = system_content
        if temperature > 0:
            payload["temperature"] = temperature

        data = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "x-api-key": api_key or "not-set",
            "anthropic-version": "2023-06-01",
        }
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
                result = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise LLMClientError(
                f"Anthropic API HTTP {exc.code}: {body[:300]}",
                error_code="anthropic_http_error",
            ) from exc

        # Parse Anthropic response format
        content_list = result.get("content") or []
        content = ""
        if isinstance(content_list, list):
            for block in content_list:
                if isinstance(block, dict) and block.get("type") == "text":
                    content += block.get("text", "")
        elif isinstance(content_list, str):
            content = content_list

        usage_raw = result.get("usage") or {}
        input_tokens = int(usage_raw.get("input_tokens", 0))
        output_tokens = int(usage_raw.get("output_tokens", 0))
        usage = {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        }
        resolved_model = result.get("model", model) or model
        return content, usage, resolved_model

    # ------------------------------------------------------------------
    # Provider config helpers
    # ------------------------------------------------------------------

    def _effective_api_format(self) -> str:
        """Return the wire format to use, inferring from base_url if not explicit."""
        if self.options.api_format in {"openai", "anthropic"}:
            return self.options.api_format
        url = self.options.base_url or ""
        if "/anthropic" in url or "mimo" in url or "yybb" in url:
            return "anthropic"
        return "openai"

    def _provider_configs(self) -> list[tuple[str, str, str, str]]:
        """Return (tag, model, base_url, api_key) for primary + fallback."""
        configs: list[tuple[str, str, str, str]] = []
        if self.options.primary_model and self.options.base_url:
            configs.append((
                "primary",
                self.options.primary_model,
                self.options.base_url,
                self.options.api_key,
            ))
        if self.options.fallback_model and self.options.fallback_base_url:
            configs.append((
                "fallback",
                self.options.fallback_model,
                self.options.fallback_base_url,
                self.options.fallback_api_key or self.options.api_key,
            ))
        elif self.options.fallback_model and self.options.base_url:
            configs.append((
                "fallback",
                self.options.fallback_model,
                self.options.base_url,
                self.options.api_key,
            ))
        return configs

    def _fast_config(self) -> tuple[str, str, str, str] | None:
        """Return config for the fast/cheap model if configured."""
        model = self.options.fast_model or self.options.primary_model
        if not model or not self.options.base_url:
            return None
        return ("fast", model, self.options.base_url, self.options.api_key)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def chat(
        self,
        *,
        messages: list[dict[str, str]],
        model: str = "",
        temperature: float = 0.0,
        max_tokens: int = 1024,
        timeout_seconds: float = 30,
        response_format: dict[str, str] | None = None,
    ) -> LLMResponse:
        """Send a chat completion request with automatic provider fallback.

        Dispatches to the Anthropic or OpenAI wire format based on
        ``api_format`` (or auto-detected from ``base_url``).

        Parameters
        ----------
        messages : list of dicts
            OpenAI-compatible message list (system/user/assistant roles).
        model : str, optional
            Override model. If empty, uses ``primary_model``.
        temperature : float
            Sampling temperature.
        max_tokens : int
            Maximum tokens to generate.
        timeout_seconds : float
            Per-request timeout.
        response_format : dict, optional
            E.g. ``{"type": "json_object"}`` for JSON mode (OpenAI only).
        """
        configs = self._provider_configs()
        if model:
            if self.options.base_url:
                configs = [("override", model, self.options.base_url, self.options.api_key)]
            else:
                configs = [("override", model, "https://api.openai.com/v1", self.options.api_key)]
        if not configs:
            raise LLMClientError(
                "No AI provider configured. Set [ai_provider] base_url and primary_model in config.",
                error_code="llm_not_configured",
            )
        api_format = self._effective_api_format()
        attempts: list[dict[str, Any]] = []
        last_error: Exception | None = None
        for tag, mdl, base_url, api_key in configs:
            started = time.monotonic()
            try:
                if api_format == "anthropic":
                    content, usage, resolved_model = self._call_anthropic_api(
                        messages=messages,
                        model=mdl,
                        base_url=base_url,
                        api_key=api_key,
                        max_tokens=max_tokens,
                        temperature=temperature,
                        timeout_seconds=timeout_seconds,
                    )
                else:
                    content, usage, resolved_model = self._call_openai_api(
                        messages=messages,
                        model=mdl,
                        base_url=base_url,
                        api_key=api_key,
                        max_tokens=max_tokens,
                        temperature=temperature,
                        timeout_seconds=timeout_seconds,
                        response_format=response_format,
                    )
                duration = time.monotonic() - started
                logger.info(
                    "LLM call succeeded: format=%s provider=%s model=%s duration=%.1fs tokens=%s",
                    api_format, tag, resolved_model, duration, usage.get("total_tokens", "?"),
                )
                return LLMResponse(
                    content=content,
                    model=resolved_model,
                    usage=usage,
                    duration_seconds=duration,
                    provider_tag=tag,
                )
            except Exception as exc:
                duration = time.monotonic() - started
                logger.warning(
                    "LLM call failed: format=%s provider=%s model=%s duration=%.1fs error=%s",
                    api_format, tag, mdl, duration, exc,
                )
                attempts.append({
                    "provider": tag,
                    "model": mdl,
                    "base_url": base_url,
                    "error": str(exc),
                    "duration_seconds": duration,
                })
                last_error = exc
                continue
        raise LLMClientError(
            f"All LLM providers failed: {last_error}",
            error_code="llm_all_providers_failed",
            attempts=attempts,
        )

    def classify_intent(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
    ) -> LLMResponse:
        """Convenience method for intent classification with JSON mode."""
        fast = self._fast_config()
        model = fast[1] if fast else ""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        # JSON mode only works reliably with OpenAI format
        response_format: dict[str, str] | None = None
        if self._effective_api_format() == "openai":
            response_format = {"type": "json_object"}
        return self.chat(
            messages=messages,
            model=model,
            temperature=self.options.intent_temperature,
            max_tokens=self.options.intent_max_tokens,
            timeout_seconds=self.options.intent_timeout_seconds,
            response_format=response_format,
        )

    def generate_summary(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str = "",
    ) -> LLMResponse:
        """Convenience method for long-form summary generation."""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        return self.chat(
            messages=messages,
            model=model,
            temperature=self.options.summary_temperature,
            max_tokens=self.options.summary_max_tokens,
            timeout_seconds=self.options.summary_timeout_seconds,
        )

    def is_available(self) -> bool:
        """Check if at least one provider is configured."""
        return bool(self.options.enabled and self.options.base_url and self.options.primary_model)
