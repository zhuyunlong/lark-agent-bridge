"""Operation permission and approval layer for the bridge.

Classifies operations into risk levels and gates execution behind
confirmation (via interactive cards) or approval flows for higher-risk
actions.  Low-risk operations proceed automatically.

Risk levels
-----------
- **low**: Read-only, chat replies, small analyses → auto-execute.
- **medium**: Large log downloads, long-running analyses, re-analysis
  → require card confirmation from the requesting user.
- **high**: Batch operations, cross-group broadcasts, write-back to
  external systems → require explicit approval (future: approval flow).

The module is designed to be stateless per-decision: callers present
an ``OperationRequest``, get back an ``ApprovalDecision``, and act on
it.  Pending confirmations are tracked in a lightweight in-memory +
persisted store so the bridge can match incoming card button callbacks.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Risk classification
# ---------------------------------------------------------------------------

class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ApprovalStatus(str, Enum):
    AUTO_APPROVED = "auto_approved"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class OperationRequest:
    """Describes an operation that may need approval."""

    operation_type: str
    description: str
    risk_level: RiskLevel = RiskLevel.LOW
    requester_id: str = ""
    chat_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ApprovalDecision:
    """Result of evaluating an operation request."""

    status: ApprovalStatus
    request_id: str = ""
    risk_level: RiskLevel = RiskLevel.LOW
    reason: str = ""
    approved_at: float = 0.0
    expires_at: float = 0.0

    @property
    def can_proceed(self) -> bool:
        return self.status in (ApprovalStatus.AUTO_APPROVED, ApprovalStatus.APPROVED)


@dataclass
class PendingApproval:
    """A tracked pending approval waiting for user confirmation."""

    request_id: str
    operation: OperationRequest
    created_at: float = 0.0
    expires_at: float = 0.0
    status: ApprovalStatus = ApprovalStatus.PENDING
    resolved_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["operation"]["risk_level"] = self.operation.risk_level.value
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PendingApproval:
        op_data = d.get("operation", {})
        op_data["risk_level"] = RiskLevel(op_data.get("risk_level", "low"))
        op = OperationRequest(**op_data)
        return cls(
            request_id=d.get("request_id", ""),
            operation=op,
            created_at=d.get("created_at", 0.0),
            expires_at=d.get("expires_at", 0.0),
            status=ApprovalStatus(d.get("status", "pending")),
            resolved_at=d.get("resolved_at", 0.0),
        )


# ---------------------------------------------------------------------------
# Risk classification rules
# ---------------------------------------------------------------------------

# Operation type → risk level mapping
_RISK_RULES: dict[str, RiskLevel] = {
    # Low risk – auto-proceed
    "chat": RiskLevel.LOW,
    "basic_chat": RiskLevel.LOW,
    "signal_lifecycle": RiskLevel.LOW,
    "help": RiskLevel.LOW,
    "status_check": RiskLevel.LOW,

    # Medium risk – card confirmation
    "bug_analysis": RiskLevel.MEDIUM,
    "direct_analysis": RiskLevel.MEDIUM,
    "perception_summary": RiskLevel.MEDIUM,
    "claude_skill": RiskLevel.MEDIUM,
    "reanalyze": RiskLevel.MEDIUM,

    # High risk – approval flow
    "batch_archive": RiskLevel.HIGH,
    "batch_sync": RiskLevel.HIGH,
    "cross_group_broadcast": RiskLevel.HIGH,
    "purge_all": RiskLevel.HIGH,
    "writeback_external": RiskLevel.HIGH,
}


def classify_risk(operation_type: str, **hints: Any) -> RiskLevel:
    """Determine risk level for an operation.

    Parameters
    ----------
    operation_type:
        Key from the risk rules table.
    **hints:
        Additional context that can elevate risk. Supported keys:

        - ``file_count``: number of files involved
        - ``estimated_duration_seconds``: expected wall-clock time
        - ``retry_count``: how many times this has been retried
    """
    base = _RISK_RULES.get(operation_type, RiskLevel.LOW)

    # Elevate based on hints
    file_count = int(hints.get("file_count", 0))
    duration = float(hints.get("estimated_duration_seconds", 0))
    retry_count = int(hints.get("retry_count", 0))

    if base == RiskLevel.LOW:
        if file_count > 10 or duration > 300:
            return RiskLevel.MEDIUM
    if base == RiskLevel.MEDIUM:
        if retry_count > 3 or file_count > 50 or duration > 1800:
            return RiskLevel.HIGH

    return base


# ---------------------------------------------------------------------------
# Approval store (in-memory + optional persistence)
# ---------------------------------------------------------------------------

class ApprovalStore:
    """Tracks pending approval requests.

    Stores pending confirmations so the bridge can match card button
    callbacks (approve/reject) back to the original operation.
    """

    def __init__(
        self,
        state_file: str | Path | None = None,
        *,
        default_ttl_seconds: float = 300.0,
    ) -> None:
        self.state_file = Path(state_file) if state_file else None
        self.default_ttl_seconds = default_ttl_seconds
        self._pending: dict[str, PendingApproval] = {}
        self._lock = threading.RLock()
        if self.state_file:
            self._load()

    # -- public API --

    def evaluate(self, operation: OperationRequest) -> ApprovalDecision:
        """Evaluate an operation and return a decision.

        Low-risk operations are auto-approved.  Medium/high-risk create
        a pending approval and return ``PENDING``.
        """
        with self._lock:
            if operation.risk_level == RiskLevel.LOW:
                return ApprovalDecision(
                    status=ApprovalStatus.AUTO_APPROVED,
                    risk_level=RiskLevel.LOW,
                    reason="low_risk_auto_approved",
                )

            request_id = f"apr_{uuid.uuid4().hex[:12]}"
            now = time.time()
            pending = PendingApproval(
                request_id=request_id,
                operation=operation,
                created_at=now,
                expires_at=now + self.default_ttl_seconds,
            )
            self._pending[request_id] = pending
            self._persist()

            return ApprovalDecision(
                status=ApprovalStatus.PENDING,
                request_id=request_id,
                risk_level=operation.risk_level,
                reason="awaiting_confirmation",
                expires_at=pending.expires_at,
            )

    def resolve(
        self,
        request_id: str,
        *,
        approved: bool,
    ) -> ApprovalDecision:
        """Resolve a pending approval (approve or reject).

        Returns the updated decision.  If the request is expired or not
        found, returns an appropriate status.
        """
        with self._lock:
            pending = self._pending.get(request_id)
            if pending is None:
                return ApprovalDecision(
                    status=ApprovalStatus.EXPIRED,
                    request_id=request_id,
                    reason="not_found_or_expired",
                )

            if pending.status != ApprovalStatus.PENDING:
                return ApprovalDecision(
                    status=ApprovalStatus.EXPIRED,
                    request_id=request_id,
                    risk_level=pending.operation.risk_level,
                    reason=f"already_{pending.status.value}",
                )

            now = time.time()
            if now > pending.expires_at:
                pending.status = ApprovalStatus.EXPIRED
                pending.resolved_at = now
                self._persist()
                return ApprovalDecision(
                    status=ApprovalStatus.EXPIRED,
                    request_id=request_id,
                    risk_level=pending.operation.risk_level,
                    reason="expired",
                )

            pending.status = ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED
            pending.resolved_at = now
            self._persist()

            return ApprovalDecision(
                status=pending.status,
                request_id=request_id,
                risk_level=pending.operation.risk_level,
                reason="user_approved" if approved else "user_rejected",
                approved_at=now if approved else 0.0,
            )

    def get_pending(self, request_id: str) -> PendingApproval | None:
        with self._lock:
            pending = self._pending.get(request_id)
            if pending is None:
                return None
            if pending.status != ApprovalStatus.PENDING or time.time() > pending.expires_at:
                return None
            return pending

    def list_pending(self) -> list[PendingApproval]:
        """Return all currently pending (non-expired) approvals."""
        with self._lock:
            now = time.time()
            result = []
            for p in self._pending.values():
                if p.status == ApprovalStatus.PENDING and now <= p.expires_at:
                    result.append(p)
            return result

    def cleanup_expired(self) -> int:
        """Remove expired pending approvals. Returns count removed."""
        with self._lock:
            now = time.time()
            expired_ids = [
                rid
                for rid, p in self._pending.items()
                if p.status == ApprovalStatus.PENDING and now > p.expires_at
            ]
            for rid in expired_ids:
                self._pending[rid].status = ApprovalStatus.EXPIRED
                self._pending[rid].resolved_at = now
            # Remove all resolved entries older than 1 hour
            stale_ids = [
                rid
                for rid, p in self._pending.items()
                if p.status != ApprovalStatus.PENDING
                and p.resolved_at > 0
                and now - p.resolved_at > 3600
            ]
            for rid in stale_ids:
                del self._pending[rid]
            removed = len(expired_ids) + len(stale_ids)
            if removed:
                self._persist()
            return removed

    @property
    def pending_count(self) -> int:
        with self._lock:
            now = time.time()
            return sum(
                1
                for p in self._pending.values()
                if p.status == ApprovalStatus.PENDING and now <= p.expires_at
            )

    # -- persistence --

    def _persist(self) -> None:
        if not self.state_file:
            return
        with self._lock:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            data = {rid: p.to_dict() for rid, p in self._pending.items()}
            tmp = self.state_file.with_name(f".{self.state_file.name}.{uuid.uuid4().hex}.tmp")
            try:
                tmp.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                tmp.replace(self.state_file)
            finally:
                try:
                    if tmp.exists():
                        tmp.unlink()
                except OSError:
                    pass

    def _load(self) -> None:
        if not self.state_file or not self.state_file.exists():
            return
        with self._lock:
            try:
                data = json.loads(self.state_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return
            for rid, d in data.items():
                if isinstance(d, dict):
                    self._pending[rid] = PendingApproval.from_dict(d)


# ---------------------------------------------------------------------------
# Convenience helpers
# ---------------------------------------------------------------------------

def build_operation_request(
    operation_type: str,
    description: str,
    *,
    requester_id: str = "",
    chat_id: str = "",
    **hints: Any,
) -> OperationRequest:
    """Build an ``OperationRequest`` with auto-classified risk."""
    risk = classify_risk(operation_type, **hints)
    return OperationRequest(
        operation_type=operation_type,
        description=description,
        risk_level=risk,
        requester_id=requester_id,
        chat_id=chat_id,
        metadata=dict(hints),
    )
