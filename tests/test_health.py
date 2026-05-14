"""Tests for the health module."""

from __future__ import annotations

import os
import time

from lark_agent_bridge.health import (
    HealthMonitor,
    HealthStatus,
    ProcessWatchdog,
    TrackedProcess,
    _process_alive,
)


class TestHealthStatus:
    def test_default_healthy(self):
        status = HealthStatus()
        assert status.healthy is True
        assert status.has_issues is False
        assert status.issues == []

    def test_to_dict(self):
        status = HealthStatus(healthy=True, checked_at="2024-01-01T00:00:00Z", uptime_seconds=100.0)
        d = status.to_dict()
        assert d["healthy"] is True
        assert d["checked_at"] == "2024-01-01T00:00:00Z"
        assert d["uptime_seconds"] == 100.0
        assert d["issues"] == []

    def test_has_issues(self):
        from lark_agent_bridge.health import HealthIssue
        status = HealthStatus(issues=[HealthIssue(component="test", severity="warning", message="test")])
        assert status.has_issues is True


class TestProcessWatchdog:
    def test_track_and_untrack(self):
        wd = ProcessWatchdog(max_idle_seconds=3600)
        wd.track(os.getpid(), "self")
        assert wd.tracked_count == 1
        wd.untrack(os.getpid())
        assert wd.tracked_count == 0

    def test_record_activity(self):
        wd = ProcessWatchdog()
        wd.track(os.getpid(), "self")
        wd.record_activity(os.getpid())
        tracked = wd.list_tracked()
        assert len(tracked) == 1
        assert tracked[0]["name"] == "self"
        assert tracked[0]["alive"] is True

    def test_check_stuck_returns_empty_for_active(self):
        wd = ProcessWatchdog(max_idle_seconds=3600)
        wd.track(os.getpid(), "self")
        stuck = wd.check_stuck()
        assert stuck == []

    def test_check_stuck_detects_idle(self):
        wd = ProcessWatchdog(max_idle_seconds=60)
        wd.track(os.getpid(), "self")
        # Manually set last_activity to far in the past
        proc = wd._tracked[os.getpid()]
        proc.last_activity_at = time.time() - 120
        stuck = wd.check_stuck()
        assert len(stuck) == 1
        assert stuck[0].pid == os.getpid()

    def test_list_tracked(self):
        wd = ProcessWatchdog()
        wd.track(os.getpid(), "test-process")
        listed = wd.list_tracked()
        assert len(listed) == 1
        assert listed[0]["pid"] == os.getpid()
        assert listed[0]["name"] == "test-process"
        assert listed[0]["alive"] is True
        assert listed[0]["uptime_seconds"] >= 0

    def test_dead_process_removed_on_check(self):
        wd = ProcessWatchdog()
        wd.track(99999999, "dead-process")
        stuck = wd.check_stuck()
        assert wd.tracked_count == 0
        assert len(stuck) == 0


class TestHealthMonitor:
    def test_basic_health_check(self):
        monitor = HealthMonitor()
        status = monitor.check_health()
        assert status.healthy is True
        assert status.checked_at != ""
        assert status.uptime_seconds >= 0

    def test_event_consumer_not_running(self):
        monitor = HealthMonitor(event_consumer_pid=99999999)
        status = monitor.check_health()
        assert any(i.component == "event_consumer" for i in status.issues)
        assert not status.healthy

    def test_event_consumer_running(self):
        monitor = HealthMonitor(event_consumer_pid=os.getpid())
        status = monitor.check_health()
        consumer = status.components["event_consumer"]
        assert consumer["alive"] is True

    def test_event_lag_detection(self):
        monitor = HealthMonitor(max_event_lag_seconds=1.0)
        monitor.record_event_processed()
        time.sleep(0.01)
        # Within threshold
        status = monitor.check_health()
        lag = status.components["event_processing"]
        assert lag["lag_exceeded"] is False

    def test_event_lag_exceeded(self):
        monitor = HealthMonitor(max_event_lag_seconds=0.01)
        monitor._last_event_time = time.time() - 1.0
        status = monitor.check_health()
        lag = status.components["event_processing"]
        assert lag["lag_exceeded"] is True

    def test_disk_usage_check(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            from pathlib import Path
            monitor = HealthMonitor(data_dir=Path(tmp))
            status = monitor.check_health()
            disk = status.components["disk"]
            assert disk["available"] is True
            assert "usage_percent" in disk

    def test_stuck_process_monitoring(self):
        wd = ProcessWatchdog(max_idle_seconds=3600)
        wd.track(os.getpid(), "test")
        monitor = HealthMonitor(process_watchdog=wd)
        status = monitor.check_health()
        watchdog_status = status.components["process_watchdog"]
        assert watchdog_status["tracked"] == 1
        assert watchdog_status["stuck_count"] == 0

    def test_set_event_consumer_pid(self):
        monitor = HealthMonitor()
        monitor.set_event_consumer_pid(os.getpid())
        status = monitor.check_health()
        assert status.components["event_consumer"]["pid"] == os.getpid()
        assert status.components["event_consumer"]["alive"] is True

    def test_to_dict_serializable(self):
        import json
        monitor = HealthMonitor(event_consumer_pid=os.getpid())
        status = monitor.check_health()
        d = status.to_dict()
        serialized = json.dumps(d)
        assert isinstance(serialized, str)


class TestProcessAlive:
    def test_current_process_alive(self):
        assert _process_alive(os.getpid()) is True

    def test_nonexistent_process(self):
        assert _process_alive(99999999) is False
