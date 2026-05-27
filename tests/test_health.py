"""Tests for the health module."""

from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from lark_agent_bridge.health import (
    HealthMonitor,
    HealthStatus,
    ProcessWatchdog,
    TrackedProcess,
    _process_alive,
    _safe_terminate,
    run_tracked_process,
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
        wd.track(os.getpid(), "test-process", session_id="om_1")
        listed = wd.list_tracked()
        assert len(listed) == 1
        assert listed[0]["pid"] == os.getpid()
        assert listed[0]["name"] == "test-process"
        assert listed[0]["session_id"] == "om_1"
        assert listed[0]["alive"] is True
        assert listed[0]["uptime_seconds"] >= 0

    def test_dead_process_removed_on_check(self):
        wd = ProcessWatchdog()
        wd.track(99999999, "dead-process")
        stuck = wd.check_stuck()
        assert wd.tracked_count == 0
        assert len(stuck) == 0

    def test_terminate_session_only_targets_matching_session(self):
        wd = ProcessWatchdog()
        wd.track(11111, "target", session_id="om_target")
        wd.track(22222, "other", session_id="om_other")
        with (
            mock.patch("lark_agent_bridge.health._process_alive", return_value=True),
            mock.patch("lark_agent_bridge.health._safe_terminate", return_value=True) as terminate,
        ):
            result = wd.terminate_session("om_target")

        assert len(result) == 1
        assert result[0]["pid"] == 11111
        assert result[0]["session_id"] == "om_target"
        terminate.assert_called_once_with(11111)
        assert 11111 not in wd._tracked
        assert 22222 in wd._tracked


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


class ProcessAliveTests(unittest.TestCase):
    def test_permission_error_means_process_exists_but_is_not_signalable(self):
        with mock.patch("lark_agent_bridge.health.os.kill", side_effect=PermissionError):
            self.assertTrue(_process_alive(12345))


class RunTrackedProcessTests(unittest.TestCase):
    def test_tracks_and_untracks_real_subprocess(self):
        watchdog = ProcessWatchdog(max_idle_seconds=60)

        completed = run_tracked_process(
            [sys.executable, "-c", "print('ok')"],
            watchdog=watchdog,
            name="unit-test-subprocess",
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout.strip(), "ok")
        self.assertEqual(watchdog.tracked_count, 0)

    def test_falls_back_to_subprocess_run_without_watchdog(self):
        completed = run_tracked_process(
            [sys.executable, "-c", "print('fallback')"],
            watchdog=None,
            name="fallback",
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )

        self.assertIsInstance(completed, subprocess.CompletedProcess)
        self.assertEqual(completed.stdout.strip(), "fallback")

    def test_writes_debug_log_for_subprocess_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug_path = Path(tmp) / "unit-subprocess.debug.log"

            completed = run_tracked_process(
                [sys.executable, "-c", "print('debuggable')"],
                watchdog=None,
                name="debuggable-subprocess",
                debug_log_path=debug_path,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )

            debug_text = debug_path.read_text(encoding="utf-8")

        self.assertEqual(completed.returncode, 0)
        self.assertIn("debuggable-subprocess", debug_text)
        self.assertIn("returncode: 0", debug_text)
        self.assertIn("debuggable", debug_text)

    def test_timeout_kill_race_still_untracks_process(self):
        class RaceProcess:
            pid = os.getpid()
            returncode = 0

            def __init__(self):
                self.calls = 0

            def communicate(self, input=None, timeout=None):
                self.calls += 1
                if self.calls == 1:
                    raise subprocess.TimeoutExpired(["cmd"], timeout=timeout)
                return "", ""

            def kill(self):
                raise ProcessLookupError

        watchdog = ProcessWatchdog(max_idle_seconds=60)
        process = RaceProcess()

        with (
            mock.patch("lark_agent_bridge.health.subprocess.Popen", return_value=process),
            mock.patch("lark_agent_bridge.health._safe_terminate", return_value=False),
        ):
            with self.assertRaises(subprocess.TimeoutExpired):
                run_tracked_process(
                    ["cmd"],
                    watchdog=watchdog,
                    name="race-process",
                    capture_output=True,
                    text=True,
                    timeout=1,
                    check=False,
                )

        self.assertEqual(watchdog.tracked_count, 0)

    def test_tracked_process_starts_new_session_for_process_group_cleanup(self):
        class FakeProcess:
            pid = os.getpid()
            returncode = 0

            def communicate(self, input=None, timeout=None):
                return "ok", ""

        watchdog = ProcessWatchdog(max_idle_seconds=60)
        with mock.patch("lark_agent_bridge.health.subprocess.Popen", return_value=FakeProcess()) as popen:
            completed = run_tracked_process(
                ["cmd"],
                watchdog=watchdog,
                name="group-process",
                capture_output=True,
                text=True,
                timeout=1,
                check=False,
            )

        self.assertEqual(completed.stdout, "ok")
        if os.name == "posix":
            self.assertTrue(popen.call_args.kwargs.get("start_new_session"))

    def test_input_uses_stdin_pipe_when_watchdog_enabled(self):
        class FakeProcess:
            pid = os.getpid()
            returncode = 0

            def __init__(self):
                self.received_input = None

            def communicate(self, input=None, timeout=None):
                self.received_input = input
                return "ok", ""

        watchdog = ProcessWatchdog(max_idle_seconds=60)
        process = FakeProcess()
        with mock.patch("lark_agent_bridge.health.subprocess.Popen", return_value=process) as popen:
            completed = run_tracked_process(
                ["cmd"],
                watchdog=watchdog,
                name="stdin-process",
                input="prompt from stdin",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=1,
                check=False,
            )

        self.assertEqual(completed.stdout, "ok")
        self.assertEqual(process.received_input, "prompt from stdin")
        self.assertEqual(popen.call_args.kwargs.get("stdin"), subprocess.PIPE)

    def test_timeout_terminates_process_group(self):
        class TimeoutProcess:
            pid = 12345
            returncode = None

            def __init__(self):
                self.calls = 0

            def communicate(self, input=None, timeout=None):
                self.calls += 1
                if self.calls == 1:
                    raise subprocess.TimeoutExpired(["cmd"], timeout=timeout)
                return "", ""

        watchdog = ProcessWatchdog(max_idle_seconds=60)
        with (
            mock.patch("lark_agent_bridge.health.subprocess.Popen", return_value=TimeoutProcess()),
            mock.patch("lark_agent_bridge.health._safe_terminate", return_value=True) as terminate,
        ):
            with self.assertRaises(subprocess.TimeoutExpired):
                run_tracked_process(
                    ["cmd"],
                    watchdog=watchdog,
                    name="timeout-process",
                    capture_output=True,
                    text=True,
                    timeout=1,
                    check=False,
                )

        terminate.assert_called_once_with(12345)

    def test_timeout_coerces_bytes_to_text_when_text_mode_requested(self):
        class TimeoutProcess:
            pid = 12345
            returncode = None

            def __init__(self):
                self.calls = 0

            def communicate(self, input=None, timeout=None):
                self.calls += 1
                if self.calls == 1:
                    raise subprocess.TimeoutExpired(
                        ["cmd"],
                        timeout=timeout,
                        output=b"partial stdout",
                        stderr=b"partial stderr",
                    )
                return "", ""

        watchdog = ProcessWatchdog(max_idle_seconds=60)
        with (
            mock.patch("lark_agent_bridge.health.subprocess.Popen", return_value=TimeoutProcess()),
            mock.patch("lark_agent_bridge.health._safe_terminate", return_value=True),
        ):
            with self.assertRaises(subprocess.TimeoutExpired) as exc_info:
                run_tracked_process(
                    ["cmd"],
                    watchdog=watchdog,
                    name="timeout-process",
                    capture_output=True,
                    text=True,
                    timeout=1,
                    check=False,
                )

        self.assertEqual(exc_info.exception.stdout, "partial stdout")
        self.assertEqual(exc_info.exception.stderr, "partial stderr")

    def test_safe_terminate_prefers_process_group_for_session_leader(self):
        with (
            mock.patch("lark_agent_bridge.health.os.getpgid", return_value=12345),
            mock.patch("lark_agent_bridge.health.os.killpg") as killpg,
            mock.patch("lark_agent_bridge.health.os.kill") as kill,
        ):
            self.assertTrue(_safe_terminate(12345))

        killpg.assert_called_once_with(12345, signal.SIGTERM)
        kill.assert_not_called()
