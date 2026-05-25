"""Pydantic-based structured output models for AI agent responses.

These models provide:
- Type-safe validation with Literal types (LLM must return valid values)
- Automatic JSON parsing and validation (replaces 40+ lines of manual parsing)
- Clear error messages on validation failure (enables auto-retry)
- Backward compatibility with existing dataclass-based IntentDecision

Design decisions:
- Uses pydantic v2 BaseModel (not dataclass) for validator support
- Keeps field names identical to existing IntentDecision for drop-in compat
- Adds from_llm_response() classmethod for JSON extraction from raw LLM output
- Provides to_dataclass() for bridging to existing code that expects dataclass

Note: This module requires `pydantic>=2.0`. If pydantic is not installed,
importing this module will raise ImportError. Callers should handle gracefully.
"""

from __future__ import annotations

import re
from typing import Any, Literal

try:
    from pydantic import BaseModel, Field, field_validator

    PYDANTIC_AVAILABLE = True
except ImportError:
    # Provide stub for type checking without runtime pydantic
    PYDANTIC_AVAILABLE = False

    class BaseModel:  # type: ignore[no-redef]
        """Stub BaseModel when pydantic is not installed."""

        def __init_subclass__(cls, **kwargs: Any) -> None:
            pass

        def __init__(self, **kwargs: Any) -> None:
            for k, v in kwargs.items():
                setattr(self, k, v)

        def model_dump(self, **kwargs: Any) -> dict[str, Any]:
            return vars(self)

        @classmethod
        def model_validate_json(cls, data: str) -> Any:
            import json as _json

            return cls(**_json.loads(data))

    def Field(**kwargs: Any) -> Any:  # type: ignore[no-redef]
        return kwargs.get("default", None)

    def field_validator(*args: Any, **kwargs: Any) -> Any:  # type: ignore[no-redef]
        def decorator(fn: Any) -> Any:
            return classmethod(fn)
        return decorator


# ---------------------------------------------------------------------------
# Intent Classification Models
# ---------------------------------------------------------------------------

# Valid values (must match IntentRunner.ROUTES etc.)
RouteType = Literal[
    "signal",
    "bug",
    "direct_analysis",
    "perception_summary",
    "analysis_followup",
    "chat",
    "unsupported",
]

FollowupAction = Literal["continue_agent", "reanalysis", "context_chat", "none"]
ContextSource = Literal["explicit", "latest_chat", "none"]
Confidence = Literal["high", "medium", "low"]


class IntentOutput(BaseModel):
    """Structured output for intent classification.

    When used as pydantic-ai Agent output_type, the LLM is forced to return
    a JSON object matching this schema. Validation failures trigger automatic
    retry with the error message fed back to the model.
    """

    route: RouteType = Field(description="The classified message route/intent")
    confidence: Confidence = Field(default="low", description="Classification confidence level")
    reason: str = Field(default="", description="Brief Chinese explanation of the classification")
    followup_action: FollowupAction = Field(default="none", description="Action for follow-up messages")
    context_source: ContextSource = Field(default="none", description="Where follow-up context comes from")

    @field_validator("reason")
    @classmethod
    def reason_not_too_long(cls, v: str) -> str:
        if len(v) > 200:
            return v[:200]
        return v

    @classmethod
    def from_llm_response(cls, raw: str) -> "IntentOutput":
        """Parse an IntentOutput from raw LLM text response.

        Handles:
        - Clean JSON objects
        - JSON wrapped in markdown code blocks
        - JSON embedded in natural language text
        """
        payload = _extract_json(raw)
        return cls.model_validate_json(payload)

    def to_dict(self) -> dict[str, str]:
        """Convert to plain dict for serialization."""
        return self.model_dump(mode="python")


# ---------------------------------------------------------------------------
# Dependency Context (for pydantic-ai RunContext)
# ---------------------------------------------------------------------------


class AgentDeps(BaseModel):
    """Dependencies injected into pydantic-ai agents via RunContext.

    This decouples agents from BridgeApp/BridgeConfig, making them
    testable in isolation.
    """

    model_config = {"arbitrary_types_allowed": True}

    # Provider config
    base_url: str = ""
    api_key: str = ""
    api_format: str = "openai"  # "openai" or "anthropic"
    model_name: str = ""
    fast_model_name: str = ""
    temperature: float = 0.0
    max_tokens: int = 1024
    timeout_seconds: float = 30.0

    # Feature flags
    json_mode: bool = True
    max_retries: int = 2

    # Context for the current request
    message_text: str = ""
    chat_type: str = ""
    sender_id: str = ""
    extra_context: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _extract_json(raw: str) -> str:
    """Extract a JSON object from raw LLM text.

    Handles markdown code blocks, leading/trailing text, etc.
    """
    cleaned = raw.strip()
    # Remove markdown code fences
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    # If it's already a clean JSON object
    cleaned = cleaned.strip()
    if cleaned.startswith("{") and cleaned.endswith("}"):
        return cleaned
    # Try to find embedded JSON
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"No JSON object found in response: {cleaned[:100]}...")
    return cleaned[start : end + 1]
