"""Provider capability detection for pydantic-ai runtime.

Determines which pydantic-ai features a given AI provider supports based on
its api_format and model name.  This avoids runtime trial-and-error by
consulting a static capability matrix that can be overridden in config.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..log import get_logger
from ..models import AIProviderOptions

logger = get_logger("provider_capabilities")


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    """What a given provider endpoint actually supports."""

    supports_structured_output: bool = False
    supports_function_tools: bool = False
    supports_multi_step_tools: bool = False
    supports_stream_events: bool = False
    supports_message_history: bool = False
    api_format: str = ""
    model: str = ""
    source: str = "probe"


# Known capability profiles keyed by (api_format, model_prefix) patterns.
_KNOWN_PROFILES: list[tuple[str, str, ProviderCapabilities]] = [
    # OpenAI-compatible endpoints (GPT-4+, DeepSeek)
    ("openai", "", ProviderCapabilities(
        supports_structured_output=True,
        supports_function_tools=True,
        supports_multi_step_tools=True,
        supports_stream_events=True,
        supports_message_history=True,
        api_format="openai",
        source="known_profile",
    )),
    # Anthropic-compatible endpoints (Claude)
    ("anthropic", "", ProviderCapabilities(
        supports_structured_output=True,
        supports_function_tools=True,
        supports_multi_step_tools=True,
        supports_stream_events=True,
        supports_message_history=True,
        api_format="anthropic",
        source="known_profile",
    )),
]

# Models known NOT to support tools
_NO_TOOL_MODELS = frozenset({
    "gemma",
    "llama",
    "qwen",
    "phi",
})


def detect_capabilities(options: AIProviderOptions) -> ProviderCapabilities:
    """Detect provider capabilities from AIProviderOptions.

    Uses api_format and model name to determine what features are available.
    Returns a conservative estimate — never over-promises.
    """
    api_format = _effective_format(options)
    model = options.primary_model or ""
    model_lower = model.lower()

    # Check if model is known to lack tool support
    no_tools = any(prefix in model_lower for prefix in _NO_TOOL_MODELS)

    if no_tools:
        return ProviderCapabilities(
            supports_structured_output=True,
            supports_function_tools=False,
            supports_multi_step_tools=False,
            supports_stream_events=False,
            supports_message_history=True,
            api_format=api_format,
            model=model,
            source="no_tool_model",
        )

    if not api_format:
        return ProviderCapabilities(
            api_format="",
            model=model,
            source="unknown_format",
        )

    # Match against known profiles
    for fmt, _prefix, profile in _KNOWN_PROFILES:
        if fmt == api_format:
            return ProviderCapabilities(
                supports_structured_output=profile.supports_structured_output,
                supports_function_tools=profile.supports_function_tools,
                supports_multi_step_tools=profile.supports_multi_step_tools,
                supports_stream_events=profile.supports_stream_events,
                supports_message_history=profile.supports_message_history,
                api_format=api_format,
                model=model,
                source=profile.source,
            )

    return ProviderCapabilities(
        supports_structured_output=True,
        supports_function_tools=False,
        supports_multi_step_tools=False,
        api_format=api_format,
        model=model,
        source="conservative_default",
    )


def select_runtime_path(caps: ProviderCapabilities) -> str:
    """Choose the best runtime path based on capabilities.

    Returns one of:
    - "pydantic_ai_agent"     - full agent with tools and structured output
    - "pydantic_ai_structured" - structured output only, no tools
    - "direct_api"            - raw LLMClient call
    - "subprocess"            - CLI subprocess fallback
    """
    if caps.supports_function_tools and caps.supports_structured_output:
        return "pydantic_ai_agent"
    if caps.supports_structured_output:
        return "pydantic_ai_structured"
    if caps.api_format:
        return "direct_api"
    return "subprocess"


def _effective_format(options: AIProviderOptions) -> str:
    """Determine API format from config or URL heuristics."""
    if options.api_format in {"openai", "anthropic"}:
        return options.api_format
    url = options.base_url or ""
    if "/anthropic" in url:
        return "anthropic"
    if url:
        return "openai"
    return ""
