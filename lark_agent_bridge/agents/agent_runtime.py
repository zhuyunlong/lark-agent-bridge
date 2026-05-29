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

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from ..log import get_logger
from ..models import AIProviderOptions
from .provider_capabilities import ProviderCapabilities, detect_capabilities, select_runtime_path

logger = get_logger("agent_runtime")

T = TypeVar("T")

# Anthropic prompt-cache TTLs. Instructions and tool definitions are stable for
# the whole session (and across multiple analyses), so cache them for 1h. The
# conversation breakpoint moves forward each turn and is refreshed on every read,
# so a 5m window is enough and cheaper to write.
_ANTHROPIC_STATIC_CACHE_TTL = "1h"
_ANTHROPIC_CONVERSATION_CACHE_TTL = "5m"

_PYDANTIC_AI_AVAILABLE: bool | None = None


def _extract_usage(result: Any) -> dict[str, int]:
    """Extract token usage (incl. prompt-cache tokens) from a pydantic-ai result.

    Handles both the property form (``AgentRunResult.usage``) and the legacy
    callable form, and normalises field names across pydantic-ai versions.
    """
    try:
        u = getattr(result, "usage", None)
        if callable(u):
            u = u()
        if not u:
            return {}
        return {
            "request_tokens": getattr(u, "request_tokens", 0) or getattr(u, "input_tokens", 0) or 0,
            "response_tokens": getattr(u, "response_tokens", 0) or getattr(u, "output_tokens", 0) or 0,
            "total_tokens": getattr(u, "total_tokens", 0) or 0,
            "cache_read_tokens": getattr(u, "cache_read_tokens", 0) or 0,
            "cache_write_tokens": getattr(u, "cache_write_tokens", 0) or 0,
        }
    except Exception:
        return {}



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
        strict_tools: bool = False,
        extra_roots: list[Path] | None = None,
        report_dir: Path | None = None,
        log_metadata_path: Path | None = None,
        progress_callback: Any | None = None,
        stream: bool = False,
        codegraph_client: Any | None = None,
        codegraph_roots: list[Path] | None = None,
    ) -> RuntimeResult:
        """Execute the agent and return structured result.

        Args:
            strict_tools: if True, do NOT fallback to direct_api when pydantic-ai fails.
                Use for source analysis where tool calls are mandatory.
            extra_roots: additional workspace roots for tool access.
            report_dir: job output dir for reading prior report artifacts.
            log_metadata_path: path to prepared log metadata file.
            progress_callback: optional callback(stage, message, **kw) for real-time tool progress.
            stream: if True, use run_stream() for real-time text output (calls progress_callback
                with stage="stream_text" for each chunk).
            codegraph_client: optional CodeGraphClient for semantic code intelligence tools.
            codegraph_roots: repo roots with codegraph indexes.
        """
        started = time.monotonic()

        if self._preferred_path in ("pydantic_ai_agent", "pydantic_ai_structured") and _check_pydantic_ai():
            use_tools = tools_enabled and self._preferred_path == "pydantic_ai_agent"
            # Structured outputs are more stable on the non-stream path because
            # pydantic-ai cannot retry validation failures inside run_stream().
            use_stream_path = bool(stream and output_type is None)
            runner = self._run_pydantic_ai_stream if use_stream_path else self._run_pydantic_ai
            result = runner(
                output_type=output_type,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                use_tools=use_tools,
                started=started,
                extra_roots=extra_roots,
                report_dir=report_dir,
                log_metadata_path=log_metadata_path,
                progress_callback=progress_callback,
                codegraph_client=codegraph_client,
                codegraph_roots=codegraph_roots,
            )
            if result.ok:
                return result
            if strict_tools:
                logger.warning(
                    "pydantic-ai failed (error=%s) and strict_tools=True, NOT falling back",
                    result.error_code,
                )
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
        extra_roots: list[Path] | None = None,
        report_dir: Path | None = None,
        log_metadata_path: Path | None = None,
        progress_callback: Any | None = None,
        codegraph_client: Any | None = None,
        codegraph_roots: list[Path] | None = None,
    ) -> RuntimeResult:
        """Execute via pydantic-ai Agent."""
        try:
            from pydantic_ai import Agent
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

        # Per-run tool cache to deduplicate calls within this agentic loop
        tool_cache = None
        if use_tools:
            from .agent_tools import ToolCallCache, register_tools
            tool_cache = ToolCallCache()
            register_tools(
                agent,
                self.workspace,
                extra_roots=extra_roots,
                report_dir=report_dir,
                log_metadata_path=log_metadata_path,
                progress_callback=progress_callback,
                codegraph_client=codegraph_client,
                codegraph_roots=codegraph_roots,
                tool_cache=tool_cache,
            )

        try:
            from pydantic_ai import UsageLimits
            usage_limits = UsageLimits(
                request_limit=50,
                tool_calls_limit=80,
            )
            # Use higher max_tokens for pydantic-ai since tool results consume context
            effective_max_tokens = max(self.options.summary_max_tokens, 8192)
            result = agent.run_sync(
                user_prompt,
                model_settings=self._build_model_settings(effective_max_tokens),
                usage_limits=usage_limits,
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
        usage = _extract_usage(result)

        # Extract tool trace
        tool_trace: list[dict[str, str]] = []
        tool_calls = 0
        try:
            if hasattr(result, "all_messages"):
                for msg in result.all_messages():
                    msg_kind = getattr(msg, "kind", "")
                    if msg_kind == "response" and hasattr(msg, "parts"):
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

        # Log tool cache stats if available
        cache_stats: dict[str, Any] = {}
        if tool_cache is not None:
            cache_stats = tool_cache.stats
            logger.info(
                "Tool cache stats: hits=%d misses=%d rate=%.1f%% saved_tokens≈%d",
                cache_stats.get("hits", 0),
                cache_stats.get("misses", 0),
                cache_stats.get("hit_rate", 0) * 100,
                cache_stats.get("saved_tokens_estimate", 0),
            )

        runtime_path = "pydantic_ai_agent" if use_tools else "pydantic_ai_structured"
        cache_read = usage.get("cache_read_tokens", 0)
        cache_write = usage.get("cache_write_tokens", 0)
        cacheable = cache_read + cache_write
        prompt_cache_rate = (cache_read / cacheable * 100) if cacheable else 0.0
        logger.info(
            "pydantic-ai %s completed: %.1fs, %d tool calls, %s input tokens | "
            "prompt-cache: read=%d write=%d hit_rate=%.1f%%",
            runtime_path, duration, tool_calls,
            usage.get("request_tokens", "?"),
            cache_read, cache_write, prompt_cache_rate,
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

    def _run_pydantic_ai_stream(
        self,
        *,
        output_type: type | None,
        system_prompt: str,
        user_prompt: str,
        use_tools: bool,
        started: float,
        extra_roots: list[Path] | None = None,
        report_dir: Path | None = None,
        log_metadata_path: Path | None = None,
        progress_callback: Any | None = None,
        codegraph_client: Any | None = None,
        codegraph_roots: list[Path] | None = None,
    ) -> RuntimeResult:
        """Execute via pydantic-ai Agent with streaming output.

        Uses run_stream() for real-time text generation. Tool calls
        still happen in the loop, but the final text response is streamed
        so we get partial results even for long generations.
        """
        try:
            return asyncio.run(self._run_pydantic_ai_stream_async(
                output_type=output_type,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                use_tools=use_tools,
                started=started,
                extra_roots=extra_roots,
                report_dir=report_dir,
                log_metadata_path=log_metadata_path,
                progress_callback=progress_callback,
                codegraph_client=codegraph_client,
                codegraph_roots=codegraph_roots,
            ))
        except Exception as exc:
            duration = time.monotonic() - started
            logger.warning("pydantic-ai stream failed after %.1fs: %s", duration, exc)
            return RuntimeResult(
                ok=False,
                error=str(exc),
                error_code="pydantic_ai_stream_error",
                runtime_path="pydantic_ai_agent" if use_tools else "pydantic_ai_structured",
                duration_seconds=duration,
                model=self.options.primary_model,
            )

    async def _run_pydantic_ai_stream_async(
        self,
        *,
        output_type: type | None,
        system_prompt: str,
        user_prompt: str,
        use_tools: bool,
        started: float,
        extra_roots: list[Path] | None = None,
        report_dir: Path | None = None,
        log_metadata_path: Path | None = None,
        progress_callback: Any | None = None,
        codegraph_client: Any | None = None,
        codegraph_roots: list[Path] | None = None,
    ) -> RuntimeResult:
        from pydantic_ai import Agent

        model = self._create_pydantic_model()
        if model is None:
            return RuntimeResult(
                ok=False, error="Could not create pydantic-ai model",
                error_code="model_creation_failed", runtime_path="pydantic_ai",
                duration_seconds=time.monotonic() - started,
            )

        agent_kwargs: dict[str, Any] = {
            "system_prompt": system_prompt,
            "retries": self.max_retries,
        }
        if output_type is not None:
            agent_kwargs["output_type"] = output_type

        agent = Agent(model, **agent_kwargs)

        # Per-run tool cache for the streaming path
        tool_cache = None
        if use_tools:
            from .agent_tools import ToolCallCache, register_tools
            tool_cache = ToolCallCache()
            register_tools(
                agent, self.workspace,
                extra_roots=extra_roots,
                report_dir=report_dir,
                log_metadata_path=log_metadata_path,
                progress_callback=progress_callback,
                codegraph_client=codegraph_client,
                codegraph_roots=codegraph_roots,
                tool_cache=tool_cache,
            )

        from pydantic_ai import UsageLimits
        usage_limits = UsageLimits(request_limit=50, tool_calls_limit=80)
        effective_max_tokens = max(self.options.summary_max_tokens, 8192)

        accumulated_text = ""
        chunk_count = 0
        _STREAM_PROGRESS_INTERVAL = 500  # emit progress every N chars
        is_structured = output_type is not None

        async with agent.run_stream(
            user_prompt,
            model_settings=self._build_model_settings(effective_max_tokens),
            usage_limits=usage_limits,
        ) as stream_result:
            if is_structured:
                # Structured output: can't use stream_text(); wait for
                # the full response (tool loop still runs during the stream).
                output = await stream_result.get_output()
            else:
                async for chunk in stream_result.stream_text(delta=True):
                    accumulated_text += chunk
                    chunk_count += 1
                    if progress_callback and len(accumulated_text) % _STREAM_PROGRESS_INTERVAL < len(chunk):
                        try:
                            progress_callback(
                                stage="stream_text",
                                message=f"📝 生成中... {len(accumulated_text)} chars",
                                text_length=len(accumulated_text),
                            )
                        except Exception:
                            pass
                output = accumulated_text

        duration = time.monotonic() - started

        # Extract markdown
        markdown = ""
        if hasattr(output, "to_markdown"):
            markdown = output.to_markdown()
        elif isinstance(output, str):
            markdown = output
        else:
            markdown = accumulated_text or str(output)

        # Extract usage (incl. prompt-cache tokens)
        usage = _extract_usage(stream_result)

        # Extract tool trace from messages
        tool_trace: list[dict[str, str]] = []
        tool_calls = 0
        try:
            for msg in stream_result.all_messages():
                msg_kind = getattr(msg, "kind", "")
                if msg_kind == "response" and hasattr(msg, "parts"):
                    for part in msg.parts:
                        if getattr(part, "part_kind", "") == "tool-call":
                            tool_calls += 1
                            tool_trace.append({
                                "tool": getattr(part, "tool_name", ""),
                                "args": str(getattr(part, "args", ""))[:200],
                            })
        except Exception:
            pass

        # Log tool cache stats for streaming path
        if tool_cache is not None:
            cs = tool_cache.stats
            logger.info(
                "Tool cache stats (stream): hits=%d misses=%d rate=%.1f%% saved_tokens≈%d",
                cs.get("hits", 0), cs.get("misses", 0),
                cs.get("hit_rate", 0) * 100, cs.get("saved_tokens_estimate", 0),
            )

        runtime_path = "pydantic_ai_agent" if use_tools else "pydantic_ai_structured"
        cache_read = usage.get("cache_read_tokens", 0)
        cache_write = usage.get("cache_write_tokens", 0)
        cacheable = cache_read + cache_write
        prompt_cache_rate = (cache_read / cacheable * 100) if cacheable else 0.0
        logger.info(
            "pydantic-ai stream %s completed: %.1fs, %d tool calls, %d chunks, %d chars | "
            "prompt-cache: read=%d write=%d hit_rate=%.1f%%",
            runtime_path, duration, tool_calls, chunk_count, len(accumulated_text),
            cache_read, cache_write, prompt_cache_rate,
        )

        return RuntimeResult(
            ok=True, output=output, markdown=markdown,
            runtime_path=runtime_path, model=self.options.primary_model,
            duration_seconds=duration, usage=usage,
            tool_calls=tool_calls, tool_trace=tool_trace,
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

    def _build_model_settings(self, effective_max_tokens: int) -> Any:
        """Build cache-aware ModelSettings for the active provider.

        For the Anthropic format (Claude / DeepSeek via cc-switch and other
        Anthropic-compatible gateways) we enable native prompt-cache breakpoints:
        - ``anthropic_cache_instructions``: cache the (large, stable) system prompt
        - ``anthropic_cache_tool_definitions``: cache the tool schemas
        - ``anthropic_cache``: an automatic breakpoint that moves forward as the
          conversation grows, so each agentic-loop turn re-reads the cached prefix
          (system + tools + all prior turns) instead of paying full input price.

        These three are exactly how mature agents (e.g. Claude Code) reach 90%+
        cache hit rates inside a multi-step tool loop. The OpenAI format relies on
        automatic prefix caching, helped by a fixed ``seed`` and ``store``.
        """
        base: dict[str, Any] = {
            "temperature": self.options.summary_temperature,
            "max_tokens": effective_max_tokens,
        }
        if self.capabilities.api_format == "anthropic":
            try:
                from pydantic_ai.models.anthropic import AnthropicModelSettings

                return AnthropicModelSettings(
                    **base,
                    anthropic_cache_instructions=_ANTHROPIC_STATIC_CACHE_TTL,
                    anthropic_cache_tool_definitions=_ANTHROPIC_STATIC_CACHE_TTL,
                    anthropic_cache=_ANTHROPIC_CONVERSATION_CACHE_TTL,
                )
            except ImportError:
                from pydantic_ai.settings import ModelSettings

                return ModelSettings(**base)

        from pydantic_ai.settings import ModelSettings

        base["seed"] = 42
        base["extra_body"] = {"store": True}
        return ModelSettings(**base)

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
