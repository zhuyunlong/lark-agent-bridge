"""Unified object model for bug analysis lifecycle.

Provides a single ``AnalysisLifecycle`` object that tracks a bug
analysis from initial request to final conclusion, unifying all
analysis types (signal, bug, direct, perception, claude skill, chat)
under one state-machine driven model.

States
------
``created → queued → downloading → analyzing → completed / failed``

Each state transition is recorded with a timestamp, enabling full
audit trails and status queries.

This module does NOT replace the existing per-type request dataclasses.
It sits on top of them as a higher-level abstraction for lifecycle
tracking, status queries, and cross-type operations.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# Analysis types and states
# ---------------------------------------------------------------------------

class AnalysisType(str, Enum):
    SIGNAL = "signal_lifecycle"
    BUG = "bug_analysis"
    DIRECT = "direct_analysis"
    PERCEPTION = "perception_summary"
    CLAUDE_SKILL = "claude_skill"
    CHAT = "chat"
    UNKNOWN = "unknown"


class AnalysisState(str, Enum):
    CREATED = "created"
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    ANALYZING = "analyzing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# Valid state transitions
_TRANSITIONS: dict[AnalysisState, set[AnalysisState]] = {
    AnalysisState.CREATED: {AnalysisState.QUEUED, AnalysisState.CANCELLED},
    AnalysisState.QUEUED: {AnalysisState.DOWNLOADING, AnalysisState.ANALYZING, AnalysisState.CANCELLED, AnalysisState.FAILED},
    AnalysisState.DOWNLOADING: {AnalysisState.ANALYZING, AnalysisState.FAILED, AnalysisState.CANCELLED},
    AnalysisState.ANALYZING: {AnalysisState.COMPLETED, AnalysisState.FAILED, AnalysisState.CANCELLED},
    AnalysisState.COMPLETED: set(),
    AnalysisState.FAILED: {AnalysisState.QUEUED},  # allow retry
    AnalysisState.CANCELLED: set(),
}


# ---------------------------------------------------------------------------
# State transition record
# ---------------------------------------------------------------------------

@dataclass
class StateTransition:
    """Records a single state transition."""

    from_state: str
    to_state: str
    timestamp: float = 0.0
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "from_state": self.from_state,
            "to_state": self.to_state,
            "timestamp": self.timestamp,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> StateTransition:
        return cls(
            from_state=d.get("from_state", ""),
            to_state=d.get("to_state", ""),
            timestamp=d.get("timestamp", 0.0),
            reason=d.get("reason", ""),
        )


# ---------------------------------------------------------------------------
# Analysis lifecycle
# ---------------------------------------------------------------------------

@dataclass
class AnalysisLifecycle:
    """Unified lifecycle model for all analysis types.

    Tracks state, metadata, results, and full transition history for
    a single analysis job.
    """

    lifecycle_id: str = ""
    analysis_type: AnalysisType = AnalysisType.UNKNOWN
    state: AnalysisState = AnalysisState.CREATED
    request_text: str = ""
    requester_id: str = ""
    chat_id: str = ""
    message_id: str = ""

    # Result tracking
    job_id: str = ""
    report_url: str = ""
    report_version: int = 0
    conclusion: str = ""
    provider: str = ""
    duration_seconds: float = 0.0
    error_message: str = ""

    # Metadata
    created_at: float = 0.0
    updated_at: float = 0.0
    retry_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    transitions: list[StateTransition] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.lifecycle_id:
            self.lifecycle_id = f"lc_{uuid.uuid4().hex[:12]}"
        if not self.created_at:
            self.created_at = time.time()
        if not self.updated_at:
            self.updated_at = self.created_at
        # Per-object lock: a lifecycle is transitioned by the producer thread
        # (QUEUED) then a worker thread (ANALYZING/COMPLETED/FAILED), and read by
        # the health endpoint. Guards the multi-field mutation in transition_to
        # (state + updated_at + transitions.append) so it is atomic. RLock so
        # mark_*/retry (which call transition_to) stay safe under nesting.
        self._lock = threading.RLock()

    @property
    def is_terminal(self) -> bool:
        """Whether the lifecycle is in a terminal state."""
        return self.state in (
            AnalysisState.COMPLETED,
            AnalysisState.FAILED,
            AnalysisState.CANCELLED,
        )

    @property
    def elapsed_seconds(self) -> float:
        """Seconds since creation."""
        return time.time() - self.created_at

    def can_transition_to(self, target: AnalysisState) -> bool:
        """Check if transition to target state is valid."""
        allowed = _TRANSITIONS.get(self.state, set())
        return target in allowed

    def transition_to(
        self,
        target: AnalysisState,
        *,
        reason: str = "",
    ) -> StateTransition:
        """Transition to a new state.

        Raises ``ValueError`` if the transition is invalid.
        """
        with self._lock:
            if not self.can_transition_to(target):
                raise ValueError(
                    f"Invalid transition: {self.state.value} → {target.value}"
                )
            now = time.time()
            transition = StateTransition(
                from_state=self.state.value,
                to_state=target.value,
                timestamp=now,
                reason=reason,
            )
            self.transitions.append(transition)
            self.state = target
            self.updated_at = now
            return transition

    def mark_completed(
        self,
        *,
        conclusion: str = "",
        report_url: str = "",
        duration_seconds: float = 0.0,
        provider: str = "",
    ) -> StateTransition:
        """Convenience: transition to COMPLETED and set result fields."""
        self.conclusion = conclusion
        self.report_url = report_url
        self.duration_seconds = duration_seconds
        self.provider = provider
        return self.transition_to(AnalysisState.COMPLETED, reason="analysis_complete")

    def mark_failed(self, *, error_message: str = "") -> StateTransition:
        """Convenience: transition to FAILED."""
        self.error_message = error_message
        return self.transition_to(AnalysisState.FAILED, reason=error_message or "failed")

    def retry(self) -> StateTransition:
        """Re-queue a failed analysis for retry."""
        self.retry_count += 1
        return self.transition_to(AnalysisState.QUEUED, reason=f"retry_{self.retry_count}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "lifecycle_id": self.lifecycle_id,
            "analysis_type": self.analysis_type.value,
            "state": self.state.value,
            "request_text": self.request_text,
            "requester_id": self.requester_id,
            "chat_id": self.chat_id,
            "message_id": self.message_id,
            "job_id": self.job_id,
            "report_url": self.report_url,
            "report_version": self.report_version,
            "conclusion": self.conclusion,
            "provider": self.provider,
            "duration_seconds": self.duration_seconds,
            "error_message": self.error_message,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "retry_count": self.retry_count,
            "metadata": self.metadata,
            "transitions": [t.to_dict() for t in self.transitions],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AnalysisLifecycle:
        transitions = [StateTransition.from_dict(t) for t in d.get("transitions", [])]
        return cls(
            lifecycle_id=d.get("lifecycle_id", ""),
            analysis_type=AnalysisType(d.get("analysis_type", "unknown")),
            state=AnalysisState(d.get("state", "created")),
            request_text=d.get("request_text", ""),
            requester_id=d.get("requester_id", ""),
            chat_id=d.get("chat_id", ""),
            message_id=d.get("message_id", ""),
            job_id=d.get("job_id", ""),
            report_url=d.get("report_url", ""),
            report_version=d.get("report_version", 0),
            conclusion=d.get("conclusion", ""),
            provider=d.get("provider", ""),
            duration_seconds=d.get("duration_seconds", 0.0),
            error_message=d.get("error_message", ""),
            created_at=d.get("created_at", 0.0),
            updated_at=d.get("updated_at", 0.0),
            retry_count=d.get("retry_count", 0),
            metadata=d.get("metadata", {}),
            transitions=transitions,
        )


# ---------------------------------------------------------------------------
# Lifecycle store
# ---------------------------------------------------------------------------

class LifecycleStore:
    """In-memory store for active analysis lifecycles.

    Designed for runtime tracking; persists to JSON for crash recovery.
    """

    def __init__(self, *, max_active: int = 500) -> None:
        self.max_active = max(10, max_active)
        self._lifecycles: dict[str, AnalysisLifecycle] = {}
        # RLock: create() calls _enforce_limit() internally. Guards the
        # _lifecycles dict against concurrent dispatcher/worker/health threads.
        self._lock = threading.RLock()

    def create(
        self,
        analysis_type: AnalysisType,
        *,
        request_text: str = "",
        requester_id: str = "",
        chat_id: str = "",
        message_id: str = "",
        **metadata: Any,
    ) -> AnalysisLifecycle:
        """Create a new analysis lifecycle in CREATED state."""
        with self._lock:
            lc = AnalysisLifecycle(
                analysis_type=analysis_type,
                request_text=request_text,
                requester_id=requester_id,
                chat_id=chat_id,
                message_id=message_id,
                metadata=dict(metadata),
            )
            self._lifecycles[lc.lifecycle_id] = lc
            self._enforce_limit()
            return lc

    def get(self, lifecycle_id: str) -> AnalysisLifecycle | None:
        with self._lock:
            return self._lifecycles.get(lifecycle_id)

    def find_by_job_id(self, job_id: str) -> AnalysisLifecycle | None:
        """Find a lifecycle by its job_id."""
        with self._lock:
            for lc in self._lifecycles.values():
                if lc.job_id == job_id:
                    return lc
            return None

    def find_by_message_id(self, message_id: str) -> AnalysisLifecycle | None:
        """Find a lifecycle by the originating message_id."""
        with self._lock:
            for lc in self._lifecycles.values():
                if lc.message_id == message_id:
                    return lc
            return None

    def list_active(self) -> list[AnalysisLifecycle]:
        """Return all non-terminal lifecycles."""
        with self._lock:
            return [lc for lc in self._lifecycles.values() if not lc.is_terminal]

    def list_recent(self, *, limit: int = 20) -> list[AnalysisLifecycle]:
        """Return most recently updated lifecycles."""
        with self._lock:
            sorted_lcs = sorted(
                self._lifecycles.values(),
                key=lambda lc: lc.updated_at,
                reverse=True,
            )
            return sorted_lcs[:limit]

    def remove(self, lifecycle_id: str) -> bool:
        with self._lock:
            if lifecycle_id in self._lifecycles:
                del self._lifecycles[lifecycle_id]
                return True
            return False

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._lifecycles)

    @property
    def active_count(self) -> int:
        with self._lock:
            return sum(1 for lc in self._lifecycles.values() if not lc.is_terminal)

    def cleanup_terminal(self, *, max_age_seconds: float = 3600) -> int:
        """Remove terminal lifecycles older than max_age."""
        with self._lock:
            now = time.time()
            to_remove = [
                lid
                for lid, lc in self._lifecycles.items()
                if lc.is_terminal and now - lc.updated_at > max_age_seconds
            ]
            for lid in to_remove:
                del self._lifecycles[lid]
            return len(to_remove)

    def _enforce_limit(self) -> None:
        # Private helper — caller must hold self._lock (only invoked from
        # create(), which does). Mirrors CaseStore._enforce_limit's contract.
        if len(self._lifecycles) <= self.max_active:
            return
        # Remove oldest terminal first
        terminal = sorted(
            [(lid, lc) for lid, lc in self._lifecycles.items() if lc.is_terminal],
            key=lambda x: x[1].updated_at,
        )
        while len(self._lifecycles) > self.max_active and terminal:
            lid, _ = terminal.pop(0)
            del self._lifecycles[lid]


# ---------------------------------------------------------------------------
# Helper: map mode string to AnalysisType
# ---------------------------------------------------------------------------

def mode_to_analysis_type(mode: str) -> AnalysisType:
    """Convert a mode string to AnalysisType enum."""
    mapping = {
        "signal_lifecycle": AnalysisType.SIGNAL,
        "bug_analysis": AnalysisType.BUG,
        "direct_analysis": AnalysisType.DIRECT,
        "perception_summary": AnalysisType.PERCEPTION,
        "claude_skill": AnalysisType.CLAUDE_SKILL,
        "chat": AnalysisType.CHAT,
        "basic_chat": AnalysisType.CHAT,
        "omlx_chat": AnalysisType.CHAT,
    }
    return mapping.get(mode, AnalysisType.UNKNOWN)
