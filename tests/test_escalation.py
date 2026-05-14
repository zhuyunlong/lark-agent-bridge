"""Tests for the escalation module."""

from __future__ import annotations

import time

from lark_agent_bridge.escalation import (
    EscalationChecker,
    EscalationLevel,
    EscalationRule,
    NotificationHistory,
    PushNotification,
    PushReason,
    build_manual_escalation_notification,
    build_missing_material_notification,
    build_report_ready_notification,
    build_status_change_notification,
)


class TestEscalationChecker:
    def test_no_timeout_below_threshold(self):
        checker = EscalationChecker()
        notifications = checker.check_timeout(elapsed_seconds=60)
        assert len(notifications) == 0

    def test_warning_at_5_minutes(self):
        checker = EscalationChecker()
        notifications = checker.check_timeout(elapsed_seconds=350)
        assert len(notifications) >= 1
        levels = [n.level for n in notifications]
        assert EscalationLevel.WARNING in levels

    def test_escalate_at_15_minutes(self):
        checker = EscalationChecker()
        notifications = checker.check_timeout(elapsed_seconds=1000)
        levels = [n.level for n in notifications]
        assert EscalationLevel.ESCALATE in levels

    def test_retry_limit(self):
        checker = EscalationChecker()
        notifications = checker.check_retries(retry_count=5)
        assert len(notifications) >= 1
        assert notifications[0].reason == PushReason.RETRY_LIMIT

    def test_no_retry_below_limit(self):
        checker = EscalationChecker()
        notifications = checker.check_retries(retry_count=1)
        assert len(notifications) == 0

    def test_custom_rules(self):
        rules = [
            EscalationRule(
                name="custom",
                timeout_seconds=10,
                level=EscalationLevel.CRITICAL,
                message_template="Timed out at {elapsed:.0f}s",
            )
        ]
        checker = EscalationChecker(rules)
        notifications = checker.check_timeout(elapsed_seconds=15)
        assert len(notifications) == 1
        assert notifications[0].level == EscalationLevel.CRITICAL

    def test_metadata_in_notification(self):
        checker = EscalationChecker()
        notifications = checker.check_timeout(
            elapsed_seconds=1000,
            job_id="j1",
            chat_id="oc_1",
            user_id="ou_1",
        )
        for n in notifications:
            assert n.metadata.get("job_id") == "j1"
            assert n.target_chat_id == "oc_1"


class TestNotificationBuilders:
    def test_status_change(self):
        n = build_status_change_notification(
            old_status="analyzing",
            new_status="completed",
            job_id="j1",
            chat_id="oc_1",
        )
        assert n.reason == PushReason.STATUS_CHANGE
        assert "completed" in n.title
        assert n.target_chat_id == "oc_1"

    def test_missing_material(self):
        n = build_missing_material_notification(
            missing_fields=["日志文件", "故障时间"],
            chat_id="oc_1",
        )
        assert n.reason == PushReason.MISSING_MATERIAL
        assert "日志文件" in n.message
        assert "故障时间" in n.message

    def test_report_ready(self):
        n = build_report_ready_notification(
            report_url="http://report/1",
            chat_id="oc_1",
        )
        assert n.reason == PushReason.REPORT_READY
        assert "report/1" in n.message

    def test_manual_escalation(self):
        n = build_manual_escalation_notification(
            reason="用户不满意结果",
            chat_id="oc_1",
        )
        assert n.reason == PushReason.MANUAL_ESCALATION
        assert n.level == EscalationLevel.CRITICAL


class TestNotificationHistory:
    def test_record_and_count(self):
        history = NotificationHistory()
        n = PushNotification(
            reason=PushReason.STATUS_CHANGE,
            level=EscalationLevel.INFO,
            title="test",
            message="test",
            created_at=time.time(),
        )
        history.record(n)
        assert history.count == 1

    def test_dedup_blocks_duplicate(self):
        history = NotificationHistory(dedup_window_seconds=60)
        n = PushNotification(
            reason=PushReason.STATUS_CHANGE,
            level=EscalationLevel.INFO,
            title="test",
            message="test",
            target_chat_id="oc_1",
            metadata={"job_id": "j1"},
            created_at=time.time(),
        )
        assert history.should_send(n) is True
        history.record(n)
        assert history.should_send(n) is False

    def test_dedup_allows_different_job(self):
        history = NotificationHistory(dedup_window_seconds=60)
        n1 = PushNotification(
            reason=PushReason.STATUS_CHANGE,
            level=EscalationLevel.INFO,
            title="test",
            message="test",
            metadata={"job_id": "j1"},
            created_at=time.time(),
        )
        n2 = PushNotification(
            reason=PushReason.STATUS_CHANGE,
            level=EscalationLevel.INFO,
            title="test",
            message="test",
            metadata={"job_id": "j2"},
            created_at=time.time(),
        )
        history.record(n1)
        assert history.should_send(n2) is True

    def test_max_size_enforced(self):
        history = NotificationHistory(max_size=10)
        for i in range(20):
            history.record(
                PushNotification(
                    reason=PushReason.STATUS_CHANGE,
                    level=EscalationLevel.INFO,
                    title=f"test{i}",
                    message="test",
                    metadata={"job_id": f"j{i}"},
                    created_at=time.time(),
                )
            )
        assert history.count == 10

    def test_recent(self):
        history = NotificationHistory()
        for i in range(5):
            history.record(
                PushNotification(
                    reason=PushReason.STATUS_CHANGE,
                    level=EscalationLevel.INFO,
                    title=f"t{i}",
                    message="test",
                    created_at=time.time(),
                )
            )
        recent = history.recent(limit=3)
        assert len(recent) == 3
        assert recent[0].title == "t4"

    def test_to_dict(self):
        n = PushNotification(
            reason=PushReason.REPORT_READY,
            level=EscalationLevel.INFO,
            title="test",
            message="msg",
            created_at=1000.0,
        )
        d = n.to_dict()
        assert d["reason"] == "report_ready"
        assert d["level"] == "info"
