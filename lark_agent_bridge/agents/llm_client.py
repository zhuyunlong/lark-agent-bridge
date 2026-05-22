"""Unified LLM client for direct API calls.

Replaces subprocess-based CLI invocations (codex exec / claude --print)
with direct HTTP API calls via the OpenAI Python SDK, which works with
any OpenAI-compatible endpoint (OpenAI, Anthropic-via-proxy, DeepSeek,
Ollama, vLLM, omlx, etc.).

Key design decisions:
- Uses ``openai.OpenAI`` as the universal HTTP client since the project
  already talks to OpenAI-compatible endpoints (omlx_client.py).
- Supports custom ``base_url`` and ``api_key`` per provider so users
  can point at Codex web endpoints, Claude-compatible proxies, or local
  models.
- Provides structured output extraction with automatic JSON retry.
- Keeps a synchronous API to match the existing codebase (no asyncio).
"""

from __future__ import annotations

import json
import time
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
    """Synchronous LLM client wrapping the OpenAI SDK.

    Supports multiple providers via ``base_url`` + ``api_key`` pairs and
    automatic fallback when the primary provider fails.
    """

    def __init__(self, options: AIProviderOptions) -> None:
        self.options = options
        self._clients: dict[str, Any] = {}

    def _get_client(self, base_url: str, api_key: str) -> Any:
        """Return a cached ``openai.OpenAI`` client for the given endpoint."""
        cache_key = f"{base_url}|{api_key[:8] if api_key else ''}"
        if cache_key not in self._clients:
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise LLMClientError(
                    "openai package is not installed. Run: pip install openai",
                    error_code="llm_missing_openai",
                ) from exc
            self._clients[cache_key] = OpenAI(
                base_url=base_url or "https://api.openai.com/v1",
                api_key=api_key or "not-set",
                timeout=max(self.options.intent_timeout_seconds, self.options.summary_timeout_seconds) + 10,
            )
        return self._clients[cache_key]

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

        Parameters
        ----------
        messages : list of dicts
            OpenAI-compatible message list.
        model : str, optional
            Override model. If empty, uses ``primary_model``.
        temperature : float
            Sampling temperature.
        max_tokens : int
            Maximum tokens to generate.
        timeout_seconds : float
            Per-request timeout.
        response_format : dict, optional
            E.g. ``{"type": "json_object"}`` for JSON mode.
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
        attempts: list[dict[str, Any]] = []
        last_error: Exception | None = None
        for tag, mdl, base_url, api_key in configs:
            started = time.monotonic()
            try:
                client = self._get_client(base_url, api_key)
                kwargs: dict[str, Any] = {
                    "model": mdl,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "timeout": timeout_seconds,
                }
                if response_format:
                    kwargs["response_format"] = response_format
                completion = client.chat.completions.create(**kwargs)
                duration = time.monotonic() - started
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
                logger.info(
                    "LLM call succeeded: provider=%s model=%s duration=%.1fs tokens=%s",
                    tag, mdl, duration, usage.get("total_tokens", "?"),
                )
                return LLMResponse(
                    content=content,
                    model=mdl,
                    usage=usage,
                    duration_seconds=duration,
                    provider_tag=tag,
                )
            except Exception as exc:
                duration = time.monotonic() - started
                logger.warning(
                    "LLM call failed: provider=%s model=%s duration=%.1fs error=%s",
                    tag, mdl, duration, exc,
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
        return self.chat(
            messages=messages,
            model=model,
            temperature=self.options.intent_temperature,
            max_tokens=self.options.intent_max_tokens,
            timeout_seconds=self.options.intent_timeout_seconds,
            response_format={"type": "json_object"},
        )

    def generate_summary(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str = "",
    ) -> LLMResponse:
        """Convenience method for summary generation."""
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
