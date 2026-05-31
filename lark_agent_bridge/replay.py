"""Pure replay data models and resource normalization helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .models import DownloadResource

ReplayMode = Literal[
    "bug",
    "direct_analysis",
    "signal_lifecycle",
    "perception_summary",
    "addr2line_resolve",
    "rom_version_lookup",
    "unsupported",
]

ReplayAction = Literal[
    "answer_from_existing",
    "reanalyze",
    "clarify",
    "new_request",
    "unsupported",
]


def normalize_replay_mode(mode: str) -> ReplayMode:
    value = (mode or "").strip().casefold()
    if "bug" in value:
        return "bug"
    if value == "direct_analysis":
        return "direct_analysis"
    if value == "signal_lifecycle":
        return "signal_lifecycle"
    if value == "perception_summary":
        return "perception_summary"
    if value == "addr2line_resolve":
        return "addr2line_resolve"
    if value == "rom_version_lookup":
        return "rom_version_lookup"
    return "unsupported"


def normalize_replay_action(action: str) -> ReplayAction:
    value = (action or "").strip().casefold().replace("-", "_")
    if value == "answer_from_existing":
        return "answer_from_existing"
    if value == "reanalyze":
        return "reanalyze"
    if value == "clarify":
        return "clarify"
    if value == "new_request":
        return "new_request"
    return "unsupported"


@dataclass(slots=True)
class ReplayResourceBundle:
    current: list[DownloadResource] = field(default_factory=list)
    reply_chain: list[DownloadResource] = field(default_factory=list)
    session: list[DownloadResource] = field(default_factory=list)
    local_existing: list[DownloadResource] = field(default_factory=list)
    remote: list[DownloadResource] = field(default_factory=list)
    missing_local_values: list[str] = field(default_factory=list)

    @classmethod
    def from_candidates(
        cls,
        *,
        current: list[DownloadResource],
        reply_chain: list[DownloadResource],
        session: list[DownloadResource],
    ) -> "ReplayResourceBundle":
        ordered = [*current, *reply_chain, *session]
        local_existing: list[DownloadResource] = []
        remote: list[DownloadResource] = []
        missing_local_values: list[str] = []
        seen: set[tuple[str, str]] = set()

        for item in ordered:
            if item.kind == "local":
                path = Path(item.value).expanduser()
                if path.exists():
                    resolved = str(path.resolve())
                    key = ("local", resolved)
                    if key in seen:
                        continue
                    seen.add(key)
                    local_existing.append(
                        DownloadResource(
                            kind="local",
                            value=resolved,
                            source_message_id=item.source_message_id,
                            display_name=item.display_name,
                        )
                    )
                else:
                    key = ("local", item.value)
                    if key in seen:
                        continue
                    seen.add(key)
                    missing_local_values.append(item.value)
                continue

            key = (item.kind, item.value)
            if key in seen:
                continue
            seen.add(key)
            remote.append(item)

        return cls(
            current=current,
            reply_chain=reply_chain,
            session=session,
            local_existing=local_existing,
            remote=remote,
            missing_local_values=missing_local_values,
        )

    @property
    def best_effort(self) -> list[DownloadResource]:
        return [*self.local_existing, *self.remote]


@dataclass(slots=True)
class AnalysisReplayContext:
    root_message_id: str
    chat_id: str
    mode: ReplayMode
    previous_mode: str
    original_request_text: str
    current_text: str
    history: list[dict[str, str]]
    summary_text: str = ""
    report_excerpt: str = ""
    report_url: str = ""
    bug_url: str = ""
    bug_title: str = ""
    bug_description: str = ""
    previous_session: dict[str, object] = field(default_factory=dict)
    resources: ReplayResourceBundle = field(default_factory=ReplayResourceBundle)


@dataclass(slots=True)
class ReplayDecision:
    action: ReplayAction
    mode: ReplayMode
    reason: str
    confidence: str = "medium"
    analysis_kind: str = ""
    skill_name: str = ""
    signal_hint: str = ""
    retry_download_if_missing: bool = False
    normalized_request_text: str = ""


@dataclass(slots=True)
class ReplayPlan:
    decision: ReplayDecision
    context: AnalysisReplayContext


def resource_descriptor(resource: DownloadResource) -> dict[str, str]:
    return {
        "kind": resource.kind,
        "value": resource.value,
        "source_message_id": resource.source_message_id,
        "display_name": resource.display_name,
    }


def serialize_resource_status(bundle: ReplayResourceBundle) -> dict[str, object]:
    return {
        "local_existing": [resource_descriptor(item) for item in bundle.local_existing],
        "remote": [resource_descriptor(item) for item in bundle.remote],
        "missing_local_values": list(bundle.missing_local_values),
    }
