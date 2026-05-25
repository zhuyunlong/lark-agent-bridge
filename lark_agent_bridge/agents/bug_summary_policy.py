"""Backend selection policy for bug summary generation."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SummaryBackendInput:
    explicit_provider: str
    provider_session_id: str
    prefer_lightweight: bool
    ai_provider_enabled: bool
    ai_provider_base_url: str
    ai_provider_primary_model: str
    skip_direct_api: bool
    direct_api_failure: str = ""
    auto_fallback_to_file_agent: bool = False


@dataclass(frozen=True, slots=True)
class SummaryBackendDecision:
    backend: str
    reason: str
    fallback_from: str = ""


def choose_summary_backend(data: SummaryBackendInput) -> SummaryBackendDecision:
    explicit_provider = data.explicit_provider.strip()
    if explicit_provider == "omlx" or data.prefer_lightweight:
        return SummaryBackendDecision("omlx", "explicit_lightweight" if explicit_provider == "omlx" else "prefer_lightweight")
    if explicit_provider:
        return SummaryBackendDecision("file_agent", "explicit_file_agent")
    if data.provider_session_id.strip():
        return SummaryBackendDecision("file_agent", "resume_file_agent_session")
    if data.direct_api_failure:
        if data.auto_fallback_to_file_agent:
            return SummaryBackendDecision("file_agent", "direct_api_failed_fallback_enabled", fallback_from="direct_api")
        return SummaryBackendDecision("direct_api", "direct_api_failed_no_fallback")
    direct_api_ready = (
        data.ai_provider_enabled
        and bool(data.ai_provider_base_url.strip())
        and bool(data.ai_provider_primary_model.strip())
    )
    if direct_api_ready and not data.skip_direct_api:
        return SummaryBackendDecision("direct_api", "direct_api_ready")
    if direct_api_ready and data.skip_direct_api:
        return SummaryBackendDecision("file_agent", "direct_api_policy_skip")
    return SummaryBackendDecision("file_agent", "direct_api_unavailable")
