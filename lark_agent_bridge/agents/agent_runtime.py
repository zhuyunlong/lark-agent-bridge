"""Generic pydantic-ai agent runtime for the bridge.

Provides a reusable runtime that:
1. Creates a pydantic-ai Agent with structured output_type
2. Registers bridge-owned read-only tools
3. Records tool trace, usage, retries, duration, and failure reason
4. Falls back to LLMClient direct API when pydantic-ai is unavailable

Usage:
    runtime = AgentRuntime(ai_options, workspace=Path("..."))
    result = runtime.run(
        output_type=SourceAnalysisOutput,
        system_prompt="...",
        user_prompt="...",
        tools_enabled=True,
    )
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from ..log import get_logger
from ..models import AIProviderOptions
from .provider_capabilities import ProviderCapabilities, detect_capabilities, select_runtime_path

logger = get_logger("agent_runtime")

T = TypeVar("T")

_PYDANTIC_AI_AVAILABLE: bool | None = None


def _check_pydantic_ai() -> bool:
    global _PYDANTIC_AI_AVAILABLE
    if _PYDANTIC_AI_AVAILABLE is None:
        try:
            from pydantic_ai import Agent  # noqa: F401
            _PYDANTIC_AI_AVAILABLE = True
        except ImportError:
            _PYDANTIC_AI_AVAILABLE = False
    return _PYDANTIC_AI_AVAILABLE


@dataclass(slots=True)
class RuntimeResult:
    """Result from an agent runtime execution."""

    ok: bool = False
    output: Any = None
    markdown: str = ""
    runtime_path: str = ""
    model: str = ""
    duration_seconds: float = 0.0
    usage: dict[str, int] = field(default_factory=dict)
    tool_calls: int = 0
    tool_trace: list[dict[str, str]] = field(default_factory=list)
    retries: int = 0
    error: str = ""
    error_code: str = ""


class AgentRuntime:
    """Reusable pydantic-ai agent runtime with automatic fallback.

    Lifecycle:
    1. Detect provider capabilities from AIProviderOptions
    2. Select best runtime path (pydantic_ai_agent > direct_api > subprocess)
    3. Execute with structured output validation
    4. Record detailed trace for debugging
    """

    def __init__(
        self,
        options: AIProviderOptions,
        *,
        workspace: Path | None = None,
        max_retries: int = 2,
    ) -> None:
        self.options = options
        self.workspace = workspace or Path.cwd()
        self.max_retries = max_retries
        self.capabilities = detect_capabilities(options)
        self._preferred_path = select_runtime_path(self.capabilities)
        logger.info(
            "AgentRuntime init: model=%s format=%s path=%s caps=%s",
            options.primary_model,
            self.capabilities.api_format,
            self._preferred_path,
            self.capabilities.source,
        )

    @property
    def preferred_path(self) -> str:
        return self._preferred_path

    def is_available(self) -> bool:
        """Check if any runtime path is available."""
        if self._preferred_path == "pydantic_ai_agent" and _check_pydantic_ai():
            return True
        if self._preferred_path == "pydantic_ai_structured" and _check_pydantic_ai():
            return True
        if self._preferred_path == "direct_api":
            return bool(self.options.base_url and self.options.primary_model)
        return False

    def run(
        self,
        *,
        output_type: type[T] | None = None,
        system_prompt: str,
        user_prompt: str,
        tools_enabled: bool = True,
    ) -> RuntimeResult:
        """Execute the agent and return structured result.

        Tries runtime paths in order of preference:
        1. pydantic_ai_agent (if tools + structured output supported)
        2. pydantic_ai_structured (structured output only)
        3. direct_api (raw LLMClient)
        """
        started = time.monotonic()

        if self._preferred_path in ("pydantic_ai_agent", "pydantic_ai_structured") and _check_pydantic_ai():
            use_tools = tools_enabled and self._preferred_path == "pydantic_ai_agent"
            result = self._run_pydantic_ai(
                output_type=output_type,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                use_tools=use_tools,
                started=started,
            )
            if result.ok:
                return result
            logger.warning(
                "pydantic-ai runtime failed (error=%s), trying direct_api fallback",
                result.error_code,
            )

        # Fallback to direct_api
        return self._run_direct_api(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            started=started,
        )

    def _run_pydantic_ai(
        self,
        *,
        output_type: type | None,
        system_prompt: str,
        user_prompt: str,
        use_tools: bool,
        started: float,
    ) -> RuntimeResult:
        """Execute via pydantic-ai Agent."""
        try:
            from pydantic_ai import Agent
            from pydantic_ai.settings import ModelSettings
        except ImportError:
            return RuntimeResult(
                ok=False,
                error="pydantic-ai not installed",
                error_code="pydantic_ai_not_available",
                runtime_path="pydantic_ai",
                duration_seconds=time.monotonic() - started,
            )

        model = self._create_pydantic_model()
        if model is None:
            return RuntimeResult(
                ok=False,
                error="Could not create pydantic-ai model",
                error_code="model_creation_failed",
                runtime_path="pydantic_ai",
                duration_seconds=time.monotonic() - started,
            )

        agent_kwargs: dict[str, Any] = {
            "system_prompt": system_prompt,
            "retries": self.max_retries,
        }
        if output_type is not None:
            agent_kwargs["output_type"] = output_type

        agent = Agent(model, **agent_kwargs)

        if use_tools:
            from .agent_tools import register_tools
            register_tools(agent, self.workspace)

        try:
            result = agent.run_sync(
                user_prompt,
                model_settings=ModelSettings(
                    temperature=self.options.summary_temperature,
                    max_tokens=self.options.summary_max_tokens,
                ),
            )
        except Exception as exc:
            duration = time.monotonic() - started
            logger.warning("pydantic-ai agent failed after %.1fs: %s", duration, exc)
            return RuntimeResult(
                ok=False,
                error=str(exc),
                error_code="pydantic_ai_execution_error",
                runtime_path="pydantic_ai_agent" if use_tools else "pydantic_ai_structured",
                duration_seconds=duration,
                model=self.options.primary_model,
            )

        duration = time.monotonic() - started
        output = result.output

        # Extract markdown
        markdown = ""
        if hasattr(output, "to_markdown"):
            markdown = output.to_markdown()
        elif isinstance(output, str):
            markdown = output
        else:
            try:
                markdown = str(output.model_dump()) if hasattr(output, "model_dump") else str(output)
            except Exception:
                markdown = str(output)

        # Extract usage
        usage: dict[str, int] = {}
        try:
            if hasattr(result, "usage"):
                u = result.usage()
                usage = {
                    "request_tokens": getattr(u, "request_tokens", 0) or 0,
                    "response_tokens": getattr(u, "response_tokens", 0) or 0,
                    "total_tokens": getattr(u, "total_tokens", 0) or 0,
                }
        except Exception:
            pass

        # Extract tool trace
        tool_trace: list[dict[str, str]] = []
        tool_calls = 0
        try:
            if hasattr(result, "all_messages"):
                for msg in result.all_messages():
                    msg_kind = getattr(msg, "kind", "")
                    if msg_kind == "request" and hasattr(msg, "parts"):
                        for part in msg.parts:
                            part_kind = getattr(part, "part_kind", "")
                            if part_kind == "tool-call":
                                tool_calls += 1
                                tool_trace.append({
                                    "tool": getattr(part, "tool_name", ""),
                                    "args": str(getattr(part, "args", ""))[:200],
                                })
        except Exception:
            pass

        runtime_path = "pydantic_ai_agent" if use_tools else "pydantic_ai_structured"
        logger.info(
            "pydantic-ai %s completed: %.1fs, %d tool calls, %s tokens",
            runtime_path, duration, tool_calls,
            usage.get("total_tokens", "?"),
        )

        return RuntimeResult(
            ok=True,
            output=output,
            markdown=markdown,
            runtime_path=runtime_path,
            model=self.options.primary_model,
            duration_seconds=duration,
            usage=usage,
            tool_calls=tool_calls,
            tool_trace=tool_trace,
        )

    def _run_direct_api(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        started: float,
    ) -> RuntimeResult:
        """Fallback: execute via LLMClient direct API."""
        from .llm_client import LLMClient, LLMClientError

        client = LLMClient(self.options)
        if not client.is_available():
            return RuntimeResult(
                ok=False,
                error="LLMClient not available",
                error_code="direct_api_not_available",
                runtime_path="direct_api",
                duration_seconds=time.monotonic() - started,
            )

        try:
            response = client.generate_summary(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
        except LLMClientError as exc:
            return RuntimeResult(
                ok=False,
                error=str(exc),
                error_code="direct_api_error",
                runtime_path="direct_api",
                duration_seconds=time.monotonic() - started,
                model=self.options.primary_model,
            )
        except Exception as exc:
            return RuntimeResult(
                ok=False,
                error=str(exc),
                error_code="direct_api_unexpected",
                runtime_path="direct_api",
                duration_seconds=time.monotonic() - started,
                model=self.options.primary_model,
            )

        content = (response.content or "").strip()
        if not content:
            return RuntimeResult(
                ok=False,
                error="Empty response from API",
                error_code="direct_api_empty",
                runtime_path="direct_api",
                duration_seconds=time.monotonic() - started,
                model=response.model or self.options.primary_model,
            )

        duration = time.monotonic() - started
        usage: dict[str, int] = {}
        if response.usage:
            usage = dict(response.usage)

        return RuntimeResult(
            ok=True,
            output=content,
            markdown=content,
            runtime_path="direct_api",
            model=response.model or self.options.primary_model,
            duration_seconds=duration,
            usage=usage,
        )

    def _create_pydantic_model(self) -> Any:
        """Create the appropriate pydantic-ai model."""
        api_format = self.capabilities.api_format
        model_name = self.options.primary_model
        if not model_name or not self.options.base_url:
            return None

        if api_format == "anthropic":
            return self._create_anthropic_model(model_name)
        return self._create_openai_model(model_name)

    def _create_openai_model(self, model_name: str) -> Any:
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider

        provider = OpenAIProvider(
            base_url=self.options.base_url,
            api_key=self.options.api_key or "not-set",
        )
        return OpenAIChatModel(model_name, provider=provider)

    def _create_anthropic_model(self, model_name: str) -> Any:
        from pydantic_ai.models.anthropic import AnthropicModel
        from pydantic_ai.providers.anthropic import AnthropicProvider

        provider = AnthropicProvider(
            base_url=self.options.base_url,
            api_key=self.options.api_key or "not-set",
        )
        return AnthropicModel(model_name, provider=provider)
