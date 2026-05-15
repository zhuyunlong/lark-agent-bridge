"""Daemon and process health governance.

Provides health check infrastructure, stuck-process detection, event backlog
monitoring, and auto-recovery for the bridge's long-running components.

Key components
--------------
- **HealthMonitor** – periodic health check that inspects the event consumer
  process, agent subprocesses, event processing latency, and disk usage.
- **ProcessWatchdog** – detects stuck agent subprocesses (no output for a
  configurable duration) and terminates them to free resources.
- **HealthStatus** – structured snapshot of the system's health for API
  exposure and alerting.

Usage example::

    from lark_agent_bridge.health import HealthMonitor

    monitor = HealthMonitor(config, activity_store=activity_store)
    status = monitor.check_health()
    if status.has_issues:
        for issue in status.issues:
            log.warning("health issue: %s", issue)
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Health status model
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class HealthIssue:
    """A single health concern."""
    component: str
    severity: str  # "warning" | "critical"
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class HealthStatus:
    """Snapshot of overall system health."""
    healthy: bool = True
    checked_at: str = ""
    uptime_seconds: float = 0.0
    issues: list[HealthIssue] = field(default_factory=list)
    components: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def has_issues(self) -> bool:
        return len(self.issues) > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "healthy": self.healthy,
            "checked_at": self.checked_at,
            "uptime_seconds": self.uptime_seconds,
            "issues": [
                {
                    "component": issue.component,
                    "severity": issue.severity,
                    "message": issue.message,
                    "details": issue.details,
                }
                for issue in self.issues
            ],
            "components": self.components,
        }


# ---------------------------------------------------------------------------
# Process watchdog
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class TrackedProcess:
    """A subprocess being monitored by the watchdog."""
    pid: int
    name: str
    started_at: float
    last_activity_at: float
    max_idle_seconds: float = 3600.0


class ProcessWatchdog:
    """Detects and terminates stuck subprocesses.

    Parameters
    ----------
    max_idle_seconds:
        Default maximum seconds a process can be idle (no recorded activity)
        before being considered stuck.
    """

    def __init__(self, *, max_idle_seconds: float = 3600.0) -> None:
        self.max_idle_seconds = max(60.0, float(max_idle_seconds))
        self._tracked: dict[int, TrackedProcess] = {}

    def track(self, pid: int, name: str, *, max_idle_seconds: float | None = None) -> None:
        """Start tracking a subprocess."""
        now = time.time()
        self._tracked[pid] = TrackedProcess(
            pid=pid,
            name=name,
            started_at=now,
            last_activity_at=now,
            max_idle_seconds=max_idle_seconds if max_idle_seconds is not None else self.max_idle_seconds,
        )

    def record_activity(self, pid: int) -> None:
        """Record that the tracked process showed activity."""
        proc = self._tracked.get(pid)
        if proc is not None:
            proc.last_activity_at = time.time()

    def untrack(self, pid: int) -> None:
        """Stop tracking a subprocess."""
        self._tracked.pop(pid, None)

    def check_stuck(self) -> list[TrackedProcess]:
        """Return list of processes that appear stuck (idle too long)."""
        now = time.time()
        stuck: list[TrackedProcess] = []
        for proc in list(self._tracked.values()):
            if not _process_alive(proc.pid):
                self._tracked.pop(proc.pid, None)
                continue
            idle_seconds = now - proc.last_activity_at
            if idle_seconds > proc.max_idle_seconds:
                stuck.append(proc)
        return stuck

    def terminate_stuck(self) -> list[dict[str, Any]]:
        """Terminate all stuck processes and return details."""
        results: list[dict[str, Any]] = []
        for proc in self.check_stuck():
            terminated = _safe_terminate(proc.pid)
            results.append({
                "pid": proc.pid,
                "name": proc.name,
                "idle_seconds": time.time() - proc.last_activity_at,
                "terminated": terminated,
            })
            if terminated:
                self._tracked.pop(proc.pid, None)
        return results

    @property
    def tracked_count(self) -> int:
        return len(self._tracked)

    def list_tracked(self) -> list[dict[str, Any]]:
        """Return info about all tracked processes."""
        now = time.time()
        return [
            {
                "pid": proc.pid,
                "name": proc.name,
                "uptime_seconds": now - proc.started_at,
                "idle_seconds": now - proc.last_activity_at,
                "alive": _process_alive(proc.pid),
            }
            for proc in self._tracked.values()
        ]


def run_tracked_process(
    command: list[str],
    *,
    watchdog: ProcessWatchdog | None,
    name: str,
    max_idle_seconds: float | None = None,
    **kwargs: Any,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess and register its PID with the watchdog when available.

    Falls back to ``subprocess.run`` when no watchdog is supplied so existing
    tests and standalone runners keep their previous behavior.
    """
    if watchdog is None:
        return subprocess.run(command, **kwargs)

    timeout = kwargs.pop("timeout", None)
    check = bool(kwargs.pop("check", False))
    capture_output = bool(kwargs.pop("capture_output", False))
    input_data = kwargs.pop("input", None)

    if capture_output:
        if kwargs.get("stdout") is not None or kwargs.get("stderr") is not None:
            raise ValueError("stdout and stderr arguments may not be used with capture_output")
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE

    process = subprocess.Popen(command, **kwargs)
    watchdog.track(
        process.pid,
        name,
        max_idle_seconds=max_idle_seconds if max_idle_seconds is not None else timeout,
    )
    try:
        try:
            stdout, stderr = process.communicate(input=input_data, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate()
            raise subprocess.TimeoutExpired(
                command,
                timeout,
                output=exc.output if exc.output is not None else stdout,
                stderr=exc.stderr if exc.stderr is not None else stderr,
            ) from exc
    finally:
        watchdog.untrack(process.pid)

    completed = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    if check and completed.returncode:
        raise subprocess.CalledProcessError(
            completed.returncode,
            command,
            output=stdout,
            stderr=stderr,
        )
    return completed


# ---------------------------------------------------------------------------
# Health monitor
# ---------------------------------------------------------------------------

class HealthMonitor:
    """Aggregates health checks for all bridge components.

    Parameters
    ----------
    data_dir:
        Path to the bridge data directory.
    event_consumer_pid:
        Optional PID of the lark event consumer process.
    process_watchdog:
        Optional watchdog for agent subprocesses.
    max_event_lag_seconds:
        Maximum acceptable event processing delay.
    max_disk_usage_percent:
        Maximum acceptable disk usage percentage.
    """

    def __init__(
        self,
        *,
        data_dir: Path | None = None,
        event_consumer_pid: int | None = None,
        process_watchdog: ProcessWatchdog | None = None,
        max_event_lag_seconds: float = 300.0,
        max_disk_usage_percent: float = 90.0,
    ) -> None:
        self._start_time = time.time()
        self._data_dir = data_dir
        self._event_consumer_pid = event_consumer_pid
        self._watchdog = process_watchdog
        self._max_event_lag_seconds = max_event_lag_seconds
        self._max_disk_usage_percent = max_disk_usage_percent
        self._last_event_time: float | None = None

    def set_event_consumer_pid(self, pid: int | None) -> None:
        self._event_consumer_pid = pid

    def record_event_processed(self) -> None:
        """Record that an event was just processed."""
        self._last_event_time = time.time()

    def check_health(self) -> HealthStatus:
        """Run all health checks and return a status snapshot."""
        now = time.time()
        status = HealthStatus(
            healthy=True,
            checked_at=datetime.now(timezone.utc).isoformat(),
            uptime_seconds=now - self._start_time,
        )

        # Check event consumer
        consumer_status = self._check_event_consumer()
        status.components["event_consumer"] = consumer_status
        if not consumer_status.get("alive", True):
            status.issues.append(HealthIssue(
                component="event_consumer",
                severity="critical",
                message="Event consumer process is not running",
                details=consumer_status,
            ))

        # Check event processing lag
        lag_status = self._check_event_lag(now)
        status.components["event_processing"] = lag_status
        if lag_status.get("lag_exceeded", False):
            status.issues.append(HealthIssue(
                component="event_processing",
                severity="warning",
                message=f"No events processed for {lag_status.get('lag_seconds', 0):.0f}s",
                details=lag_status,
            ))

        # Check stuck processes
        if self._watchdog:
            stuck_status = self._check_stuck_processes()
            status.components["process_watchdog"] = stuck_status
            if stuck_status.get("stuck_count", 0) > 0:
                status.issues.append(HealthIssue(
                    component="process_watchdog",
                    severity="warning",
                    message=f"{stuck_status['stuck_count']} stuck process(es) detected",
                    details=stuck_status,
                ))

        # Check disk usage
        if self._data_dir:
            disk_status = self._check_disk_usage()
            status.components["disk"] = disk_status
            if disk_status.get("usage_exceeded", False):
                status.issues.append(HealthIssue(
                    component="disk",
                    severity="warning",
                    message=f"Disk usage at {disk_status.get('usage_percent', 0):.1f}%",
                    details=disk_status,
                ))

        status.healthy = not any(i.severity == "critical" for i in status.issues)
        return status

    def _check_event_consumer(self) -> dict[str, Any]:
        if self._event_consumer_pid is None:
            return {"pid": None, "alive": True, "note": "no PID tracked"}
        alive = _process_alive(self._event_consumer_pid)
        return {"pid": self._event_consumer_pid, "alive": alive}

    def _check_event_lag(self, now: float) -> dict[str, Any]:
        if self._last_event_time is None:
            return {"last_event_time": None, "lag_seconds": 0, "lag_exceeded": False}
        lag = now - self._last_event_time
        return {
            "last_event_time": self._last_event_time,
            "lag_seconds": lag,
            "lag_exceeded": lag > self._max_event_lag_seconds,
        }

    def _check_stuck_processes(self) -> dict[str, Any]:
        if self._watchdog is None:
            return {"tracked": 0, "stuck_count": 0}
        stuck = self._watchdog.check_stuck()
        return {
            "tracked": self._watchdog.tracked_count,
            "stuck_count": len(stuck),
            "stuck_pids": [p.pid for p in stuck],
        }

    def _check_disk_usage(self) -> dict[str, Any]:
        if self._data_dir is None:
            return {"available": True}
        try:
            stat_result = os.statvfs(str(self._data_dir))
            total = stat_result.f_blocks * stat_result.f_frsize
            free = stat_result.f_bavail * stat_result.f_frsize
            if total == 0:
                return {"available": True, "total_bytes": 0}
            usage_percent = ((total - free) / total) * 100
            return {
                "available": True,
                "total_bytes": total,
                "free_bytes": free,
                "usage_percent": usage_percent,
                "usage_exceeded": usage_percent > self._max_disk_usage_percent,
            }
        except OSError:
            return {"available": False}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _process_alive(pid: int) -> bool:
    """Check if a process is alive using kill(0)."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _safe_terminate(pid: int) -> bool:
    """Send SIGTERM to a process. Returns True if signal was sent."""
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False
