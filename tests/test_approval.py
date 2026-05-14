"""Tests for the approval module."""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from lark_agent_bridge.approval import (
    ApprovalStatus,
    ApprovalStore,
    OperationRequest,
    PendingApproval,
    RiskLevel,
    build_operation_request,
    classify_risk,
)


class TestClassifyRisk:
    def test_chat_is_low(self):
        assert classify_risk("chat") == RiskLevel.LOW

    def test_bug_analysis_is_medium(self):
        assert classify_risk("bug_analysis") == RiskLevel.MEDIUM

    def test_batch_archive_is_high(self):
        assert classify_risk("batch_archive") == RiskLevel.HIGH

    def test_unknown_defaults_low(self):
        assert classify_risk("some_unknown_type") == RiskLevel.LOW

    def test_low_elevated_by_file_count(self):
        assert classify_risk("chat", file_count=20) == RiskLevel.MEDIUM

    def test_low_elevated_by_duration(self):
        assert classify_risk("chat", estimated_duration_seconds=600) == RiskLevel.MEDIUM

    def test_medium_elevated_by_retry_count(self):
        assert classify_risk("bug_analysis", retry_count=5) == RiskLevel.HIGH

    def test_medium_elevated_by_large_file_count(self):
        assert classify_risk("bug_analysis", file_count=100) == RiskLevel.HIGH


class TestBuildOperationRequest:
    def test_auto_classifies_risk(self):
        op = build_operation_request("bug_analysis", "分析 bug")
        assert op.risk_level == RiskLevel.MEDIUM
        assert op.operation_type == "bug_analysis"
        assert op.description == "分析 bug"

    def test_passes_requester_and_chat(self):
        op = build_operation_request(
            "chat",
            "普通聊天",
            requester_id="ou_123",
            chat_id="oc_456",
        )
        assert op.requester_id == "ou_123"
        assert op.chat_id == "oc_456"
        assert op.risk_level == RiskLevel.LOW


class TestApprovalStore:
    def test_low_risk_auto_approved(self):
        store = ApprovalStore()
        op = OperationRequest(
            operation_type="chat",
            description="普通聊天",
            risk_level=RiskLevel.LOW,
        )
        decision = store.evaluate(op)
        assert decision.status == ApprovalStatus.AUTO_APPROVED
        assert decision.can_proceed is True
        assert store.pending_count == 0

    def test_medium_risk_creates_pending(self):
        store = ApprovalStore()
        op = OperationRequest(
            operation_type="bug_analysis",
            description="分析 bug",
            risk_level=RiskLevel.MEDIUM,
        )
        decision = store.evaluate(op)
        assert decision.status == ApprovalStatus.PENDING
        assert decision.can_proceed is False
        assert store.pending_count == 1
        assert decision.request_id.startswith("apr_")

    def test_high_risk_creates_pending(self):
        store = ApprovalStore()
        op = OperationRequest(
            operation_type="batch_archive",
            description="批量归档",
            risk_level=RiskLevel.HIGH,
        )
        decision = store.evaluate(op)
        assert decision.status == ApprovalStatus.PENDING
        assert decision.can_proceed is False

    def test_resolve_approve(self):
        store = ApprovalStore()
        op = OperationRequest("bug", "bug", risk_level=RiskLevel.MEDIUM)
        decision = store.evaluate(op)
        resolved = store.resolve(decision.request_id, approved=True)
        assert resolved.status == ApprovalStatus.APPROVED
        assert resolved.can_proceed is True

    def test_resolve_reject(self):
        store = ApprovalStore()
        op = OperationRequest("bug", "bug", risk_level=RiskLevel.MEDIUM)
        decision = store.evaluate(op)
        resolved = store.resolve(decision.request_id, approved=False)
        assert resolved.status == ApprovalStatus.REJECTED
        assert resolved.can_proceed is False

    def test_resolve_not_found(self):
        store = ApprovalStore()
        resolved = store.resolve("nonexistent", approved=True)
        assert resolved.status == ApprovalStatus.EXPIRED

    def test_resolve_expired(self):
        store = ApprovalStore(default_ttl_seconds=0.01)
        op = OperationRequest("bug", "bug", risk_level=RiskLevel.MEDIUM)
        decision = store.evaluate(op)
        time.sleep(0.02)
        resolved = store.resolve(decision.request_id, approved=True)
        assert resolved.status == ApprovalStatus.EXPIRED

    def test_list_pending(self):
        store = ApprovalStore()
        for i in range(3):
            op = OperationRequest(f"op{i}", f"desc{i}", risk_level=RiskLevel.MEDIUM)
            store.evaluate(op)
        pending = store.list_pending()
        assert len(pending) == 3

    def test_cleanup_expired(self):
        store = ApprovalStore(default_ttl_seconds=0.01)
        op = OperationRequest("bug", "bug", risk_level=RiskLevel.MEDIUM)
        store.evaluate(op)
        time.sleep(0.02)
        removed = store.cleanup_expired()
        assert removed >= 1

    def test_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "approvals.json"
            store1 = ApprovalStore(path)
            op = OperationRequest("bug", "bug", risk_level=RiskLevel.MEDIUM)
            decision = store1.evaluate(op)
            request_id = decision.request_id

            store2 = ApprovalStore(path)
            assert store2.pending_count == 1
            resolved = store2.resolve(request_id, approved=True)
            assert resolved.can_proceed is True

    def test_pending_count_excludes_expired(self):
        store = ApprovalStore(default_ttl_seconds=0.01)
        op = OperationRequest("bug", "bug", risk_level=RiskLevel.MEDIUM)
        store.evaluate(op)
        time.sleep(0.02)
        assert store.pending_count == 0


class TestPendingApproval:
    def test_to_dict_roundtrip(self):
        op = OperationRequest(
            operation_type="bug",
            description="desc",
            risk_level=RiskLevel.MEDIUM,
            requester_id="ou_1",
        )
        pending = PendingApproval(
            request_id="apr_test",
            operation=op,
            created_at=1000.0,
            expires_at=1300.0,
        )
        d = pending.to_dict()
        restored = PendingApproval.from_dict(d)
        assert restored.request_id == "apr_test"
        assert restored.operation.risk_level == RiskLevel.MEDIUM
        assert restored.operation.requester_id == "ou_1"
