"""Pydantic AI agent wrapper for intent classification.

Architecture:
- Uses pydantic-ai Agent with structured output_type for guaranteed schema compliance
- Falls back to raw LLM client if pydantic-ai is not available (graceful degradation)
- Maps to existing preset/config system (AIProviderOptions)
- Provides synchronous API (run_sync) to match existing codebase patterns

Provider mapping:
- api_format="openai" → OpenAIProvider + OpenAIChatModel
- api_format="anthropic" → AnthropicProvider + AnthropicModel

Key design:
- IntentAgent: fast classification with auto-retry on schema validation failure
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ..log import get_logger
from ..models import AIProviderOptions
from .pydantic_models import AgentDeps, IntentOutput

logger = get_logger("pydantic_agents")

# Sentinel for lazy import success check
_PYDANTIC_AI_AVAILABLE: bool | None = None


def _check_pydantic_ai() -> bool:
    """Lazy-check if pydantic-ai is importable."""
    global _PYDANTIC_AI_AVAILABLE
    if _PYDANTIC_AI_AVAILABLE is None:
        try:
            from pydantic_ai import Agent  # noqa: F401

            _PYDANTIC_AI_AVAILABLE = True
        except ImportError:
            _PYDANTIC_AI_AVAILABLE = False
    return _PYDANTIC_AI_AVAILABLE


@dataclass(slots=True)
class AgentResult:
    """Result from a pydantic-ai agent call."""

    output: IntentOutput | str
    model: str = ""
    duration_seconds: float = 0.0
    usage: dict[str, int] = field(default_factory=dict)
    retries: int = 0
    provider_tag: str = ""


class IntentAgent:
    """Pydantic-AI powered intent classifier with structured output validation.

    Uses Agent(output_type=IntentOutput) so pydantic-ai:
    1. Instructs the model to return JSON matching IntentOutput schema
    2. Validates the response against the Pydantic model
    3. Automatically retries with error feedback if validation fails

    This replaces the manual _parse_decision() + _extract_json_payload() logic
    (40+ lines) with zero-code validation.
    """

    def __init__(self, options: AIProviderOptions) -> None:
        self.options = options
        self._agent: Any = None
        self._model: Any = None
        self._setup()

    def _setup(self) -> None:
        """Create the pydantic-ai Agent with the configured provider."""
        if not _check_pydantic_ai():
            logger.info("pydantic-ai not available; IntentAgent will be disabled")
            return

        from pydantic_ai import Agent

        model = self._create_model(use_fast=True)
        if model is None:
            logger.warning("Could not create model for IntentAgent")
            return

        self._model = model
        self._agent = Agent(
            model,
            output_type=IntentOutput,
            system_prompt=self._default_system_prompt(),
            retries=self.options.intent_max_retries,
        )
        logger.info("IntentAgent initialized: model=%s format=%s", self._model_name(use_fast=True), self.options.api_format)

    def _create_model(self, *, use_fast: bool = False) -> Any:
        """Create the appropriate pydantic-ai model based on api_format."""
        api_format = self._effective_format()
        model_name = (self.options.fast_model if use_fast else self.options.primary_model) or self.options.primary_model
        if not model_name or not self.options.base_url:
            return None

        if api_format == "anthropic":
            return self._create_anthropic_model(model_name)
        return self._create_openai_model(model_name)

    def _create_openai_model(self, model_name: str) -> Any:
        """Create OpenAI-compatible model."""
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider

        provider = OpenAIProvider(
            base_url=self.options.base_url,
            api_key=self.options.api_key or "not-set",
        )
        return OpenAIChatModel(model_name, provider=provider)

    def _create_anthropic_model(self, model_name: str) -> Any:
        """Create Anthropic-compatible model."""
        from pydantic_ai.models.anthropic import AnthropicModel
        from pydantic_ai.providers.anthropic import AnthropicProvider

        provider = AnthropicProvider(
            base_url=self.options.base_url,
            api_key=self.options.api_key or "not-set",
        )
        return AnthropicModel(model_name, provider=provider)

    def _effective_format(self) -> str:
        """Determine API format from config or URL heuristics."""
        if self.options.api_format in {"openai", "anthropic"}:
            return self.options.api_format
        url = self.options.base_url or ""
        if "/anthropic" in url or "mimo" in url or "yybb" in url:
            return "anthropic"
        return "openai"

    def _model_name(self, *, use_fast: bool = False) -> str:
        if use_fast and self.options.fast_model:
            return self.options.fast_model
        return self.options.primary_model or "unknown"

    def is_available(self) -> bool:
        """Check if the agent is properly configured and ready."""
        return self._agent is not None

    def classify(self, *, system_prompt: str, user_prompt: str) -> AgentResult:
        """Classify intent with structured output validation.

        Returns AgentResult with output as IntentOutput (validated).
        Raises RuntimeError if agent is not available.
        """
        if not self.is_available():
            raise RuntimeError("IntentAgent is not available (pydantic-ai not installed or no model configured)")

        from pydantic_ai import Agent
        from pydantic_ai.settings import ModelSettings

        # Override system prompt if provided
        agent: Agent[None, IntentOutput] = self._agent
        if system_prompt and system_prompt != self._default_system_prompt():
            agent = Agent(
                self._model,
                output_type=IntentOutput,
                system_prompt=system_prompt,
                retries=self.options.intent_max_retries,
            )

        started = time.monotonic()
        try:
            result = agent.run_sync(
                user_prompt,
                model_settings=ModelSettings(
                    temperature=self.options.intent_temperature,
                    max_tokens=self.options.intent_max_tokens,
                ),
            )
            duration = time.monotonic() - started

            # Extract usage if available
            usage: dict[str, int] = {}
            if hasattr(result, "usage") and result.usage:
                u = result.usage()
                usage = {
                    "prompt_tokens": getattr(u, "request_tokens", 0) or 0,
                    "completion_tokens": getattr(u, "response_tokens", 0) or 0,
                    "total_tokens": getattr(u, "total_tokens", 0) or 0,
                }

            logger.info(
                "IntentAgent classified: route=%s confidence=%s duration=%.1fs",
                result.output.route,
                result.output.confidence,
                duration,
            )

            return AgentResult(
                output=result.output,
                model=self._model_name(use_fast=True),
                duration_seconds=duration,
                usage=usage,
                provider_tag="pydantic-ai",
            )
        except Exception as exc:
            duration = time.monotonic() - started
            logger.warning("IntentAgent classification failed after %.1fs: %s", duration, exc)
            raise

    @staticmethod
    def _default_system_prompt() -> str:
        return (
            "You are an intent classifier for a Feishu (Lark) Bot bridge. "
            "Classify the user message into one of the defined routes. "
            "Always respond with a valid JSON object matching the required schema."
        )
