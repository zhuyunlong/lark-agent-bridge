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
import threading
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
    session_id: str = ""


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
        self._lock = threading.Lock()

    def track(
        self,
        pid: int,
        name: str,
        *,
        max_idle_seconds: float | None = None,
        session_id: str = "",
    ) -> None:
        """Start tracking a subprocess."""
        now = time.time()
        with self._lock:
            self._tracked[pid] = TrackedProcess(
                pid=pid,
                name=name,
                started_at=now,
                last_activity_at=now,
                max_idle_seconds=max_idle_seconds if max_idle_seconds is not None else self.max_idle_seconds,
                session_id=session_id.strip(),
            )

    def record_activity(self, pid: int) -> None:
        """Record that the tracked process showed activity."""
        with self._lock:
            proc = self._tracked.get(pid)
        if proc is not None:
            proc.last_activity_at = time.time()

    def untrack(self, pid: int) -> None:
        """Stop tracking a subprocess."""
        with self._lock:
            self._tracked.pop(pid, None)

    def check_stuck(self) -> list[TrackedProcess]:
        """Return list of processes that appear stuck (idle too long)."""
        now = time.time()
        with self._lock:
            snapshot = list(self._tracked.values())
        stuck: list[TrackedProcess] = []
        for proc in snapshot:
            if not _process_alive(proc.pid):
                with self._lock:
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
                with self._lock:
                    self._tracked.pop(proc.pid, None)
        return results

    def terminate_session(self, session_id: str) -> list[dict[str, Any]]:
        """Terminate tracked subprocesses associated with a bridge session."""
        normalized = session_id.strip()
        if not normalized:
            return []
        with self._lock:
            snapshot = list(self._tracked.values())
        results: list[dict[str, Any]] = []
        for proc in snapshot:
            if proc.session_id != normalized:
                continue
            alive = _process_alive(proc.pid)
            terminated = _safe_terminate(proc.pid) if alive else False
            results.append(
                {
                    "pid": proc.pid,
                    "name": proc.name,
                    "session_id": proc.session_id,
                    "uptime_seconds": time.time() - proc.started_at,
                    "idle_seconds": time.time() - proc.last_activity_at,
                    "alive": alive,
                    "terminated": terminated,
                }
            )
            if terminated or not alive:
                with self._lock:
                    self._tracked.pop(proc.pid, None)
        return results

    @property
    def tracked_count(self) -> int:
        with self._lock:
            return len(self._tracked)

    def list_tracked(self) -> list[dict[str, Any]]:
        """Return info about all tracked processes."""
        now = time.time()
        with self._lock:
            snapshot = list(self._tracked.values())
        return [
            {
                "pid": proc.pid,
                "name": proc.name,
                "session_id": proc.session_id,
                "uptime_seconds": now - proc.started_at,
                "idle_seconds": now - proc.last_activity_at,
                "alive": _process_alive(proc.pid),
            }
            for proc in snapshot
        ]


def _subprocess_text_mode_requested(kwargs: dict[str, Any]) -> bool:
    return bool(
        kwargs.get("text")
        or kwargs.get("universal_newlines")
        or kwargs.get("encoding") is not None
        or kwargs.get("errors") is not None
    )


def _coerce_subprocess_stream(
    value: Any,
    *,
    text_mode: bool,
    encoding: str | None,
    errors: str | None,
) -> Any:
    if value is None or not text_mode or not isinstance(value, bytes):
        return value
    return value.decode(encoding or "utf-8", errors=errors or "replace")


def _debug_stream_preview(value: Any, *, limit: int = 12000) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = str(value)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n... <truncated {len(text) - limit} chars>"


def _write_subprocess_debug_log(
    debug_log_path: str | os.PathLike[str] | None,
    *,
    name: str,
    command: list[str],
    cwd: Any,
    returncode: int | None,
    stdout: Any = None,
    stderr: Any = None,
    timeout: float | None = None,
    error: str = "",
) -> None:
    if debug_log_path is None:
        return
    path = Path(debug_log_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Subprocess Debug Log",
        f"name: {name}",
        "command:",
        "  " + " ".join(str(item) for item in command),
        f"cwd: {cwd or ''}",
        f"timeout: {timeout if timeout is not None else ''}",
        f"returncode: {returncode if returncode is not None else ''}",
    ]
    if error:
        lines.append(f"error: {error}")
    lines.extend(
        [
            "",
            "## stdout",
            _debug_stream_preview(stdout),
            "",
            "## stderr",
            _debug_stream_preview(stderr),
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def run_tracked_process(
    command: list[str],
    *,
    watchdog: ProcessWatchdog | None,
    name: str,
    max_idle_seconds: float | None = None,
    session_id: str = "",
    debug_log_path: str | os.PathLike[str] | None = None,
    **kwargs: Any,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess and register its PID with the watchdog when available.

    Falls back to ``subprocess.run`` when no watchdog is supplied so existing
    tests and standalone runners keep their previous behavior.
    """
    text_mode = _subprocess_text_mode_requested(kwargs)
    encoding = kwargs.get("encoding")
    errors = kwargs.get("errors")
    cwd = kwargs.get("cwd")

    if watchdog is None:
        try:
            completed = subprocess.run(command, **kwargs)
        except subprocess.TimeoutExpired as exc:
            output = _coerce_subprocess_stream(
                getattr(exc, "output", None),
                text_mode=text_mode,
                encoding=encoding,
                errors=errors,
            )
            stderr = _coerce_subprocess_stream(
                exc.stderr,
                text_mode=text_mode,
                encoding=encoding,
                errors=errors,
            )
            _write_subprocess_debug_log(
                debug_log_path,
                name=name,
                command=command,
                cwd=cwd,
                timeout=exc.timeout,
                returncode=None,
                stdout=output,
                stderr=stderr,
                error="TimeoutExpired",
            )
            raise subprocess.TimeoutExpired(
                exc.cmd,
                exc.timeout,
                output=output,
                stderr=stderr,
            ) from exc
        result = subprocess.CompletedProcess(
            completed.args,
            completed.returncode,
            _coerce_subprocess_stream(
                completed.stdout,
                text_mode=text_mode,
                encoding=encoding,
                errors=errors,
            ),
            _coerce_subprocess_stream(
                completed.stderr,
                text_mode=text_mode,
                encoding=encoding,
                errors=errors,
            ),
        )
        _write_subprocess_debug_log(
            debug_log_path,
            name=name,
            command=command,
            cwd=cwd,
            timeout=kwargs.get("timeout"),
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )
        return result

    timeout = kwargs.pop("timeout", None)
    check = bool(kwargs.pop("check", False))
    capture_output = bool(kwargs.pop("capture_output", False))
    input_data = kwargs.pop("input", None)

    if capture_output:
        if kwargs.get("stdout") is not None or kwargs.get("stderr") is not None:
            raise ValueError("stdout and stderr arguments may not be used with capture_output")
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    if input_data is not None and "stdin" not in kwargs:
        kwargs["stdin"] = subprocess.PIPE

    if os.name == "posix" and "start_new_session" not in kwargs:
        kwargs["start_new_session"] = True

    process = subprocess.Popen(command, **kwargs)
    watchdog.track(
        process.pid,
        name,
        max_idle_seconds=max_idle_seconds if max_idle_seconds is not None else timeout,
        session_id=session_id,
    )
    try:
        try:
            stdout, stderr = process.communicate(input=input_data, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            try:
                _safe_terminate(process.pid)
            except ProcessLookupError:
                pass
            try:
                stdout, stderr = process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                _safe_terminate(process.pid, sig=signal.SIGKILL)
                stdout, stderr = process.communicate()
            output = _coerce_subprocess_stream(
                exc.output if exc.output is not None else stdout,
                text_mode=text_mode,
                encoding=encoding,
                errors=errors,
            )
            stderr_text = _coerce_subprocess_stream(
                exc.stderr if exc.stderr is not None else stderr,
                text_mode=text_mode,
                encoding=encoding,
                errors=errors,
            )
            _write_subprocess_debug_log(
                debug_log_path,
                name=name,
                command=command,
                cwd=cwd,
                timeout=timeout,
                returncode=process.returncode,
                stdout=output,
                stderr=stderr_text,
                error="TimeoutExpired",
            )
            raise subprocess.TimeoutExpired(
                command,
                timeout,
                output=output,
                stderr=stderr_text,
            ) from exc
    finally:
        watchdog.untrack(process.pid)

    completed = subprocess.CompletedProcess(
        command,
        process.returncode,
        _coerce_subprocess_stream(
            stdout,
            text_mode=text_mode,
            encoding=encoding,
            errors=errors,
        ),
        _coerce_subprocess_stream(
            stderr,
            text_mode=text_mode,
            encoding=encoding,
            errors=errors,
        ),
    )
    _write_subprocess_debug_log(
        debug_log_path,
        name=name,
        command=command,
        cwd=cwd,
        timeout=timeout,
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )
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
        # Guards the small mutable fields (_event_consumer_pid, _last_event_time)
        # touched concurrently by worker threads (record_event_processed) and the
        # health endpoint (check_health). check_health itself stays lock-free; the
        # leaf readers below grab the lock so there is no nested acquisition.
        self._lock = threading.Lock()

    def set_event_consumer_pid(self, pid: int | None) -> None:
        with self._lock:
            self._event_consumer_pid = pid

    def record_event_processed(self) -> None:
        """Record that an event was just processed."""
        with self._lock:
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
        with self._lock:
            pid = self._event_consumer_pid
        if pid is None:
            return {"pid": None, "alive": True, "note": "no PID tracked"}
        alive = _process_alive(pid)
        return {"pid": pid, "alive": alive}

    def _check_event_lag(self, now: float) -> dict[str, Any]:
        with self._lock:
            last_event_time = self._last_event_time
        if last_event_time is None:
            return {"last_event_time": None, "lag_seconds": 0, "lag_exceeded": False}
        lag = now - last_event_time
        return {
            "last_event_time": last_event_time,
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


def _safe_terminate(pid: int, *, sig: signal.Signals = signal.SIGTERM) -> bool:
    """Send a signal to a process or its process group when it is a leader."""
    try:
        if os.name == "posix":
            try:
                pgid = os.getpgid(pid)
            except OSError:
                pgid = None
            if pgid == pid:
                os.killpg(pgid, sig)
                return True
        os.kill(pid, sig)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False
