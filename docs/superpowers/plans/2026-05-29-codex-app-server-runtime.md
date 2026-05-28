# Codex App Server Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an optional `codex app-server` runtime to `lark-agent-bridge` so selected Codex-backed file-agent/source-analysis work can stream real-time tool progress, support interrupt/retire semantics, and keep `codex exec` as a stable fallback.

**Architecture:** Keep current `codex exec --json --output-last-message` paths as the default. Introduce a separate app-server runtime module with a wire-level JSON-RPC client, a per-job session wrapper, and a projection layer that maps Codex notifications into the bridge's existing `progress_callback` and output-file contract. Integrate it first into source/file-agent analysis, then optionally into bug-summary follow-ups once the per-job path is stable.

**Tech Stack:** Python stdlib `subprocess`, `threading`, `queue`, `json`, dataclasses, existing `BridgeConfig`, `BugAnalysisRunner`, `ProcessWatchdog`, pytest/unittest.

---

## Context

Current baseline:

- `codex exec --json` is already used for bug summaries and source/file-agent work.
- `bug_runner._run_bug_agent_summary_streaming_process()` streams `codex exec --json` stdout into progress events.
- `_run_custom_skill_agent_analysis()` still runs file-agent work through `_run_tracked_process()` and only parses stdout/output after process completion.
- `knowledge/source_investigation.py` falls back to `codex exec` after local/direct-API paths.
- `codex app-server` is a different runtime model: long-lived JSON-RPC over stdio, `initialize -> thread/start -> turn/start`, streamed `item/*` notifications, server-initiated approval requests, `turn/interrupt`, and retire/restart behavior after wedged turns.

Non-goals for the first implementation:

- Do not remove `codex exec`.
- Do not route all Codex work through app-server.
- Do not add write-capable behavior by default.
- Do not expose app-server in user-visible route names; keep it as runtime plumbing.
- Do not persist a global long-lived Codex daemon in Phase 1.

## File Structure

Create:

- `lark_agent_bridge/agents/codex_app_server_runtime.py`
  - Wire-level `CodexAppServerClient`
  - Per-job `CodexAppServerRuntime`
  - `CodexAppServerResult`
  - event preview/projection helpers
  - app-server availability/version helper

- `tests/test_codex_app_server_runtime.py`
  - JSON-RPC client unit tests with fake process streams
  - event projection tests
  - runtime timeout/retire/interrupt tests

Modify:

- `lark_agent_bridge/models.py`
  - Add `CodexAppServerOptions`
  - Add `BridgeConfig.codex_app_server`

- `lark_agent_bridge/config.py`
  - Parse `[codex_app_server]`
  - Keep defaults disabled

- `tests/test_config.py`
  - Assert config defaults and custom values

- `lark_agent_bridge/agents/bug_runner.py`
  - Add app-server branch inside `_run_custom_skill_agent_analysis()`
  - Keep fallback to current `_run_tracked_process()` path
  - Persist app-server stdout/stderr/event audit sidecars

- `tests/test_agents.py`
  - Add focused tests for source/file-agent app-server selection, fallback, output writing, and progress events

- `config/config.example.toml`
  - Document disabled default block

- `docs/configuration.md`
  - Explain runtime distinction and rollout guidance

Later phase modify:

- `lark_agent_bridge/state.py` or `lark_agent_bridge/app.py`
  - Only if we add long-lived session cache and admin termination integration after Phase 1.

## Runtime Contract

The new runtime must expose this bridge-facing shape:

```python
@dataclass(slots=True)
class CodexAppServerResult:
    ok: bool
    final_text: str = ""
    error: str = ""
    error_code: str = ""
    command: list[str] = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    thread_id: str = ""
    turn_id: str = ""
    duration_seconds: float = 0.0
    events: list[dict[str, object]] = field(default_factory=list)
    should_retire: bool = False
```

Integration rule:

- The runtime returns Markdown in `final_text`.
- The bridge writes `final_text` to `analysis_markdown_path`.
- The Codex subprocess is not responsible for writing bridge output files.
- If app-server fails before a valid final text, the caller falls back to existing `codex exec` unless `fallback_to_exec=false`.

## Configuration Shape

Add a new top-level section:

```toml
[codex_app_server]
enabled = false
command = "codex"
min_version = "0.125.0"
use_for_file_agent = false
use_for_bug_summary = false
fallback_to_exec = true
startup_timeout_seconds = 15
turn_timeout_seconds = 600
post_tool_quiet_timeout_seconds = 90
notification_poll_seconds = 0.25
max_event_audit = 200
sandbox_mode = "read-only"
```

Default behavior remains unchanged because `enabled=false` and both `use_for_*` switches are false.

## Task 1: Config Model And Loader

**Files:**
- Modify: `lark_agent_bridge/models.py`
- Modify: `lark_agent_bridge/config.py`
- Modify: `tests/test_config.py`
- Modify: `config/config.example.toml`
- Modify: `docs/configuration.md`

- [ ] **Step 1: Write config defaults test**

Add to `tests/test_config.py`:

```python
def test_codex_app_server_options_default_disabled(self):
    config = load_config()

    self.assertFalse(config.codex_app_server.enabled)
    self.assertEqual(config.codex_app_server.command, "codex")
    self.assertEqual(config.codex_app_server.min_version, "0.125.0")
    self.assertFalse(config.codex_app_server.use_for_file_agent)
    self.assertFalse(config.codex_app_server.use_for_bug_summary)
    self.assertTrue(config.codex_app_server.fallback_to_exec)
    self.assertEqual(config.codex_app_server.startup_timeout_seconds, 15)
    self.assertEqual(config.codex_app_server.turn_timeout_seconds, 600)
    self.assertEqual(config.codex_app_server.post_tool_quiet_timeout_seconds, 90)
    self.assertEqual(config.codex_app_server.notification_poll_seconds, 0.25)
    self.assertEqual(config.codex_app_server.max_event_audit, 200)
    self.assertEqual(config.codex_app_server.sandbox_mode, "read-only")
```

- [ ] **Step 2: Run default test and verify failure**

Run:

```bash
PYTHONPATH=. pytest -q tests/test_config.py::TestConfig::test_codex_app_server_options_default_disabled
```

Expected: fail with `AttributeError: 'BridgeConfig' object has no attribute 'codex_app_server'`.

- [ ] **Step 3: Add dataclass and BridgeConfig field**

In `lark_agent_bridge/models.py`, add this near other option dataclasses:

```python
@dataclass(slots=True)
class CodexAppServerOptions:
    enabled: bool = False
    command: str = "codex"
    min_version: str = "0.125.0"
    use_for_file_agent: bool = False
    use_for_bug_summary: bool = False
    fallback_to_exec: bool = True
    startup_timeout_seconds: float = 15.0
    turn_timeout_seconds: float = 600.0
    post_tool_quiet_timeout_seconds: float = 90.0
    notification_poll_seconds: float = 0.25
    max_event_audit: int = 200
    sandbox_mode: str = "read-only"
```

Then add to `BridgeConfig`:

```python
codex_app_server: CodexAppServerOptions = field(default_factory=CodexAppServerOptions)
```

- [ ] **Step 4: Parse config section**

In `lark_agent_bridge/config.py`, import `CodexAppServerOptions`.

At the top of `load_config()`, add:

```python
codex_app_server_data = data.get("codex_app_server") or {}
```

Inside the `BridgeConfig(...)` constructor, add:

```python
codex_app_server=CodexAppServerOptions(
    enabled=bool(codex_app_server_data.get("enabled", CodexAppServerOptions().enabled)),
    command=str(codex_app_server_data.get("command", CodexAppServerOptions().command)),
    min_version=str(codex_app_server_data.get("min_version", CodexAppServerOptions().min_version)),
    use_for_file_agent=bool(
        codex_app_server_data.get("use_for_file_agent", CodexAppServerOptions().use_for_file_agent)
    ),
    use_for_bug_summary=bool(
        codex_app_server_data.get("use_for_bug_summary", CodexAppServerOptions().use_for_bug_summary)
    ),
    fallback_to_exec=bool(
        codex_app_server_data.get("fallback_to_exec", CodexAppServerOptions().fallback_to_exec)
    ),
    startup_timeout_seconds=float(
        codex_app_server_data.get(
            "startup_timeout_seconds",
            CodexAppServerOptions().startup_timeout_seconds,
        )
    ),
    turn_timeout_seconds=float(
        codex_app_server_data.get(
            "turn_timeout_seconds",
            CodexAppServerOptions().turn_timeout_seconds,
        )
    ),
    post_tool_quiet_timeout_seconds=float(
        codex_app_server_data.get(
            "post_tool_quiet_timeout_seconds",
            CodexAppServerOptions().post_tool_quiet_timeout_seconds,
        )
    ),
    notification_poll_seconds=float(
        codex_app_server_data.get(
            "notification_poll_seconds",
            CodexAppServerOptions().notification_poll_seconds,
        )
    ),
    max_event_audit=int(
        codex_app_server_data.get("max_event_audit", CodexAppServerOptions().max_event_audit)
    ),
    sandbox_mode=str(
        codex_app_server_data.get("sandbox_mode", CodexAppServerOptions().sandbox_mode)
    ),
),
```

- [ ] **Step 5: Add custom config test**

Add to `tests/test_config.py`:

```python
def test_load_codex_app_server_options(self):
    with tempfile.TemporaryDirectory() as tmp:
        config_path = Path(tmp) / "config.toml"
        config_path.write_text(
            """
[codex_app_server]
enabled = true
command = "custom-codex"
min_version = "0.134.0"
use_for_file_agent = true
use_for_bug_summary = true
fallback_to_exec = false
startup_timeout_seconds = 11
turn_timeout_seconds = 222
post_tool_quiet_timeout_seconds = 33
notification_poll_seconds = 0.5
max_event_audit = 17
sandbox_mode = "workspace-write"
""",
            encoding="utf-8",
        )

        config = load_config(config_path)

    self.assertTrue(config.codex_app_server.enabled)
    self.assertEqual(config.codex_app_server.command, "custom-codex")
    self.assertEqual(config.codex_app_server.min_version, "0.134.0")
    self.assertTrue(config.codex_app_server.use_for_file_agent)
    self.assertTrue(config.codex_app_server.use_for_bug_summary)
    self.assertFalse(config.codex_app_server.fallback_to_exec)
    self.assertEqual(config.codex_app_server.startup_timeout_seconds, 11.0)
    self.assertEqual(config.codex_app_server.turn_timeout_seconds, 222.0)
    self.assertEqual(config.codex_app_server.post_tool_quiet_timeout_seconds, 33.0)
    self.assertEqual(config.codex_app_server.notification_poll_seconds, 0.5)
    self.assertEqual(config.codex_app_server.max_event_audit, 17)
    self.assertEqual(config.codex_app_server.sandbox_mode, "workspace-write")
```

- [ ] **Step 6: Run config tests**

Run:

```bash
PYTHONPATH=. pytest -q tests/test_config.py -k "codex_app_server or source_investigation or bug_analysis_command_defaults"
```

Expected: all selected tests pass.

- [ ] **Step 7: Document disabled config**

Append to `config/config.example.toml`:

```toml
[codex_app_server]
enabled = false
command = "codex"
min_version = "0.125.0"
use_for_file_agent = false
use_for_bug_summary = false
fallback_to_exec = true
startup_timeout_seconds = 15
turn_timeout_seconds = 600
post_tool_quiet_timeout_seconds = 90
notification_poll_seconds = 0.25
max_event_audit = 200
sandbox_mode = "read-only"
```

Add a short section to `docs/configuration.md` after the source investigation Codex CLI section:

```markdown
### Optional Codex app-server runtime

`[codex_app_server]` is disabled by default. When enabled, the bridge can run selected Codex file-agent work through `codex app-server` instead of `codex exec`. This is not a drop-in synonym: app-server is a long-lived JSON-RPC protocol with streamed `item/*` notifications and explicit interrupt/retire handling, while `codex exec --json` is a one-shot JSONL subprocess.

Keep `fallback_to_exec = true` during rollout. Start with `use_for_file_agent = true`; leave `use_for_bug_summary = false` until file-agent progress and timeout behavior are verified in a real Feishu run.
```

- [ ] **Step 8: Commit**

```bash
git add lark_agent_bridge/models.py lark_agent_bridge/config.py tests/test_config.py config/config.example.toml docs/configuration.md
git commit -m "config: add optional codex app-server runtime settings"
```

## Task 2: Wire-Level JSON-RPC Client

**Files:**
- Create: `lark_agent_bridge/agents/codex_app_server_runtime.py`
- Create: `tests/test_codex_app_server_runtime.py`

- [ ] **Step 1: Write version parsing tests**

Create `tests/test_codex_app_server_runtime.py` with:

```python
import io
import json
import queue
import subprocess
import threading
import time
import unittest
from unittest import mock

from lark_agent_bridge.agents.codex_app_server_runtime import (
    CodexAppServerClient,
    CodexAppServerError,
    parse_codex_version,
)


class TestCodexAppServerVersion(unittest.TestCase):
    def test_parse_codex_version(self):
        self.assertEqual(parse_codex_version("codex-cli 0.134.0"), (0, 134, 0))
        self.assertEqual(parse_codex_version("codex 1.2.3 extra"), (1, 2, 3))
        self.assertIsNone(parse_codex_version("not a version"))
        self.assertIsNone(parse_codex_version(""))
```

- [ ] **Step 2: Run version test and verify failure**

Run:

```bash
PYTHONPATH=. pytest -q tests/test_codex_app_server_runtime.py::TestCodexAppServerVersion::test_parse_codex_version
```

Expected: fail with `ModuleNotFoundError` or import error.

- [ ] **Step 3: Implement module skeleton and version parser**

Create `lark_agent_bridge/agents/codex_app_server_runtime.py`:

```python
"""Codex app-server runtime for optional bridge file-agent execution."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import queue
import re
import subprocess
import threading
import time
from typing import Any


@dataclass(slots=True)
class CodexAppServerError(RuntimeError):
    code: int
    message: str
    data: Any = None

    def __str__(self) -> str:
        return f"codex app-server error {self.code}: {self.message}"


@dataclass(slots=True)
class _PendingRequest:
    method: str
    queue: queue.Queue
    sent_at: float = field(default_factory=time.monotonic)


def parse_codex_version(output: str | None) -> tuple[int, int, int] | None:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", output or "")
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))
```

- [ ] **Step 4: Run version test**

Run:

```bash
PYTHONPATH=. pytest -q tests/test_codex_app_server_runtime.py::TestCodexAppServerVersion::test_parse_codex_version
```

Expected: pass.

- [ ] **Step 5: Write client send/dispatch tests**

Append to `tests/test_codex_app_server_runtime.py`:

```python
class _FakePipe:
    def __init__(self, initial: bytes = b""):
        self._read = io.BytesIO(initial)
        self.written = bytearray()
        self.closed = False

    def readline(self):
        return self._read.readline()

    def write(self, data):
        self.written.extend(data)
        return len(data)

    def flush(self):
        return None

    def close(self):
        self.closed = True


class _FakeProcess:
    def __init__(self, stdout: bytes = b"", stderr: bytes = b""):
        self.stdin = _FakePipe()
        self.stdout = _FakePipe(stdout)
        self.stderr = _FakePipe(stderr)
        self.returncode = None
        self.pid = 12345
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode or 0


class TestCodexAppServerClient(unittest.TestCase):
    def test_request_writes_json_rpc_and_returns_result(self):
        proc = _FakeProcess(
            stdout=b'{"id":1,"result":{"ok":true}}\\n'
        )
        with mock.patch("subprocess.Popen", return_value=proc):
            client = CodexAppServerClient(codex_bin="codex")
            result = client.request("initialize", {"clientInfo": {"name": "test"}}, timeout=1)

        self.assertEqual(result, {"ok": True})
        sent = proc.stdin.written.decode("utf-8")
        self.assertIn('"id": 1', sent)
        self.assertIn('"method": "initialize"', sent)

    def test_request_raises_on_rpc_error(self):
        proc = _FakeProcess(
            stdout=b'{"id":1,"error":{"code":-32603,"message":"boom"}}\\n'
        )
        with mock.patch("subprocess.Popen", return_value=proc):
            client = CodexAppServerClient(codex_bin="codex")
            with self.assertRaises(CodexAppServerError) as caught:
                client.request("initialize", {}, timeout=1)

        self.assertEqual(caught.exception.code, -32603)
        self.assertEqual(caught.exception.message, "boom")

    def test_notification_and_server_request_are_queued(self):
        proc = _FakeProcess(
            stdout=(
                b'{"method":"item/completed","params":{"item":{"type":"agentMessage","text":"hi"}}}\\n'
                b'{"id":9,"method":"item/permissions/requestApproval","params":{}}\\n'
            )
        )
        with mock.patch("subprocess.Popen", return_value=proc):
            client = CodexAppServerClient(codex_bin="codex")
            time.sleep(0.05)

        self.assertEqual(client.take_notification(timeout=1)["method"], "item/completed")
        self.assertEqual(client.take_server_request(timeout=1)["method"], "item/permissions/requestApproval")
```

- [ ] **Step 6: Implement client**

Append to `lark_agent_bridge/agents/codex_app_server_runtime.py`:

```python
class CodexAppServerClient:
    def __init__(
        self,
        *,
        codex_bin: str = "codex",
        cwd: Path | None = None,
        sandbox_mode: str = "read-only",
        extra_env: dict[str, str] | None = None,
    ) -> None:
        self._next_id = 1
        self._pending: dict[int, _PendingRequest] = {}
        self._pending_lock = threading.Lock()
        self._notifications: queue.Queue = queue.Queue()
        self._server_requests: queue.Queue = queue.Queue()
        self._stderr_lines: list[str] = []
        self._stderr_lock = threading.Lock()
        self._closed = False
        env = None
        if extra_env:
            import os
            env = os.environ.copy()
            env.update(extra_env)
        args = [codex_bin, "app-server", "-c", f'sandbox_mode="{sandbox_mode}"']
        self._proc = subprocess.Popen(
            args,
            cwd=str(cwd) if cwd else None,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            env=env,
        )
        self._stdout_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()

    def request(self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 30.0) -> dict[str, Any]:
        request_id = self._take_id()
        response_queue: queue.Queue = queue.Queue(maxsize=1)
        with self._pending_lock:
            self._pending[request_id] = _PendingRequest(method=method, queue=response_queue)
        self._send({"id": request_id, "method": method, "params": params or {}})
        try:
            message = response_queue.get(timeout=timeout)
        except queue.Empty as exc:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise TimeoutError(f"codex app-server method {method!r} timed out after {timeout}s") from exc
        if "error" in message:
            error = message["error"] or {}
            raise CodexAppServerError(
                code=int(error.get("code", -1)),
                message=str(error.get("message", "")),
                data=error.get("data"),
            )
        return dict(message.get("result") or {})

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._send({"method": method, "params": params or {}})

    def respond(self, request_id: Any, result: dict[str, Any]) -> None:
        self._send({"id": request_id, "result": result})

    def respond_error(self, request_id: Any, *, code: int, message: str) -> None:
        self._send({"id": request_id, "error": {"code": code, "message": message}})

    def take_notification(self, *, timeout: float = 0.0) -> dict[str, Any] | None:
        try:
            if timeout <= 0:
                return self._notifications.get_nowait()
            return self._notifications.get(timeout=timeout)
        except queue.Empty:
            return None

    def take_server_request(self, *, timeout: float = 0.0) -> dict[str, Any] | None:
        try:
            if timeout <= 0:
                return self._server_requests.get_nowait()
            return self._server_requests.get(timeout=timeout)
        except queue.Empty:
            return None

    def stderr_tail(self, count: int = 20) -> list[str]:
        with self._stderr_lock:
            return list(self._stderr_lines[-count:])

    def is_alive(self) -> bool:
        return self._proc.poll() is None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._proc.stdin is not None:
                self._proc.stdin.close()
        except Exception:
            pass
        try:
            self._proc.terminate()
            self._proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait(timeout=1)

    def _take_id(self) -> int:
        request_id = self._next_id
        self._next_id += 1
        return request_id

    def _send(self, obj: dict[str, Any]) -> None:
        if self._closed:
            raise RuntimeError("codex app-server client is closed")
        if self._proc.stdin is None:
            raise RuntimeError("codex app-server stdin is unavailable")
        data = json.dumps(obj, ensure_ascii=False) + "\n"
        self._proc.stdin.write(data.encode("utf-8"))
        self._proc.stdin.flush()

    def _read_stdout(self) -> None:
        if self._proc.stdout is None:
            return
        for raw_line in iter(self._proc.stdout.readline, b""):
            if not raw_line:
                break
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                message = json.loads(raw_line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                with self._stderr_lock:
                    self._stderr_lines.append(f"<non-json stdout> {raw_line[:200]!r}")
                continue
            self._dispatch(message)

    def _dispatch(self, message: dict[str, Any]) -> None:
        if "id" in message and ("result" in message or "error" in message) and "method" not in message:
            with self._pending_lock:
                pending = self._pending.pop(int(message["id"]), None)
            if pending is not None:
                pending.queue.put_nowait(message)
            return
        if "id" in message and "method" in message:
            self._server_requests.put(message)
            return
        if "method" in message:
            self._notifications.put(message)

    def _read_stderr(self) -> None:
        if self._proc.stderr is None:
            return
        for raw_line in iter(self._proc.stderr.readline, b""):
            if not raw_line:
                break
            with self._stderr_lock:
                self._stderr_lines.append(raw_line.decode("utf-8", errors="replace").rstrip())
                if len(self._stderr_lines) > 500:
                    self._stderr_lines = self._stderr_lines[-500:]
```

- [ ] **Step 7: Run client tests**

Run:

```bash
PYTHONPATH=. pytest -q tests/test_codex_app_server_runtime.py::TestCodexAppServerClient
```

Expected: pass.

- [ ] **Step 8: Commit**

```bash
git add lark_agent_bridge/agents/codex_app_server_runtime.py tests/test_codex_app_server_runtime.py
git commit -m "runtime: add codex app-server json-rpc client"
```

## Task 3: Runtime Session And Event Projection

**Files:**
- Modify: `lark_agent_bridge/agents/codex_app_server_runtime.py`
- Modify: `tests/test_codex_app_server_runtime.py`

- [ ] **Step 1: Write event preview tests**

Append:

```python
from lark_agent_bridge.agents.codex_app_server_runtime import codex_app_server_event_preview


class TestCodexAppServerEventPreview(unittest.TestCase):
    def test_agent_message_preview(self):
        event = {
            "method": "item/completed",
            "params": {"item": {"type": "agentMessage", "text": "结论 A\n证据 B"}},
        }
        self.assertEqual(codex_app_server_event_preview(event), "Codex 输出：结论 A")

    def test_command_preview(self):
        event = {
            "method": "item/started",
            "params": {"item": {"type": "commandExecution", "command": "rg DataCenter .", "cwd": "/repo"}},
        }
        self.assertEqual(codex_app_server_event_preview(event), "工具调用：rg DataCenter .")

    def test_turn_completed_preview(self):
        event = {"method": "turn/completed", "params": {"turn": {"status": "completed"}}}
        self.assertEqual(codex_app_server_event_preview(event), "Codex app-server 分析完成")
```

- [ ] **Step 2: Implement preview helper**

Add:

```python
def _compact(text: str, limit: int = 180) -> str:
    text = " ".join((text or "").strip().split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def codex_app_server_event_preview(event: dict[str, Any]) -> str:
    method = str(event.get("method") or "")
    params = event.get("params") if isinstance(event.get("params"), dict) else {}
    item = params.get("item") if isinstance(params.get("item"), dict) else {}
    item_type = str(item.get("type") or "")
    if method == "turn/completed":
        return "Codex app-server 分析完成"
    if item_type == "agentMessage":
        text = str(item.get("text") or "").strip().splitlines()
        first = text[0].strip() if text else ""
        return f"Codex 输出：{_compact(first, 160)}" if first else "Codex 输出更新"
    if item_type == "commandExecution":
        command = str(item.get("command") or "").strip()
        return f"工具调用：{_compact(command, 160)}" if command else "工具调用：执行命令"
    if item_type == "fileChange":
        return "Codex 请求文件变更"
    if item_type in {"mcpToolCall", "dynamicToolCall"}:
        tool = str(item.get("tool") or item.get("name") or "tool")
        return f"工具调用：{_compact(tool, 160)}"
    return ""
```

- [ ] **Step 3: Write runtime turn test with fake client**

Append:

```python
from lark_agent_bridge.agents.codex_app_server_runtime import (
    CodexAppServerResult,
    CodexAppServerRuntime,
)


class _FakeClientForTurn:
    def __init__(self):
        self.requests = []
        self.responses = []
        self.closed = False
        self.notifications = queue.Queue()
        self.server_requests = queue.Queue()
        self.notifications.put({
            "method": "item/completed",
            "params": {"item": {"type": "commandExecution", "command": "rg DataCenter .", "aggregatedOutput": "ok"}},
        })
        self.notifications.put({
            "method": "item/completed",
            "params": {"item": {"type": "agentMessage", "text": "## 结论摘要\nok"}},
        })
        self.notifications.put({"method": "turn/completed", "params": {"turn": {"status": "completed"}}})

    def request(self, method, params=None, *, timeout=30):
        self.requests.append((method, params or {}))
        if method == "initialize":
            return {"userAgent": "codex-test"}
        if method == "thread/start":
            return {"thread": {"id": "thread_1"}}
        if method == "turn/start":
            return {"turn": {"id": "turn_1"}}
        return {}

    def notify(self, method, params=None):
        return None

    def respond(self, request_id, result):
        self.responses.append((request_id, result))

    def respond_error(self, request_id, *, code, message):
        self.responses.append((request_id, {"error": {"code": code, "message": message}}))

    def take_notification(self, *, timeout=0):
        try:
            return self.notifications.get_nowait()
        except queue.Empty:
            return None

    def take_server_request(self, *, timeout=0):
        try:
            return self.server_requests.get_nowait()
        except queue.Empty:
            return None

    def is_alive(self):
        return True

    def stderr_tail(self, count=20):
        return []

    def close(self):
        self.closed = True


class TestCodexAppServerRuntime(unittest.TestCase):
    def test_run_turn_collects_final_text_and_progress(self):
        fake = _FakeClientForTurn()
        progress = []
        runtime = CodexAppServerRuntime(
            codex_bin="codex",
            cwd=Path.cwd(),
            client_factory=lambda **kwargs: fake,
        )

        result = runtime.run_turn(
            user_input="分析",
            progress_callback=lambda payload: progress.append(payload),
            turn_timeout_seconds=3,
            notification_poll_seconds=0.01,
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.final_text, "## 结论摘要\nok")
        self.assertEqual(result.thread_id, "thread_1")
        self.assertEqual(result.turn_id, "turn_1")
        self.assertTrue(any("工具调用" in p["message"] for p in progress))
        self.assertTrue(any("Codex 输出" in p["message"] for p in progress))
```

- [ ] **Step 4: Implement runtime result and run_turn**

Add:

```python
@dataclass(slots=True)
class CodexAppServerResult:
    ok: bool
    final_text: str = ""
    error: str = ""
    error_code: str = ""
    command: list[str] = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    thread_id: str = ""
    turn_id: str = ""
    duration_seconds: float = 0.0
    events: list[dict[str, object]] = field(default_factory=list)
    should_retire: bool = False


class CodexAppServerRuntime:
    def __init__(
        self,
        *,
        codex_bin: str,
        cwd: Path,
        sandbox_mode: str = "read-only",
        client_factory=CodexAppServerClient,
        max_event_audit: int = 200,
    ) -> None:
        self.codex_bin = codex_bin
        self.cwd = cwd
        self.sandbox_mode = sandbox_mode
        self.client_factory = client_factory
        self.max_event_audit = max_event_audit
        self._client: Any = None
        self._thread_id = ""

    def run_turn(
        self,
        *,
        user_input: str,
        progress_callback=None,
        startup_timeout_seconds: float = 15.0,
        turn_timeout_seconds: float = 600.0,
        post_tool_quiet_timeout_seconds: float = 90.0,
        notification_poll_seconds: float = 0.25,
    ) -> CodexAppServerResult:
        started = time.monotonic()
        events: list[dict[str, object]] = []
        final_text = ""
        turn_id = ""
        last_tool_at: float | None = None
        try:
            client = self._ensure_client(startup_timeout_seconds=startup_timeout_seconds)
            turn = client.request(
                "turn/start",
                {"threadId": self._thread_id, "input": [{"type": "text", "text": user_input}]},
                timeout=10,
            )
            turn_id = str((turn.get("turn") or {}).get("id") or "")
            deadline = time.monotonic() + turn_timeout_seconds
            while time.monotonic() < deadline:
                server_request = client.take_server_request(timeout=0)
                if server_request is not None:
                    self._handle_server_request(client, server_request)
                    last_tool_at = None
                    continue
                if last_tool_at is not None and time.monotonic() - last_tool_at > post_tool_quiet_timeout_seconds:
                    self._interrupt(client, turn_id)
                    stderr = "\n".join(client.stderr_tail(40))
                    return CodexAppServerResult(
                        ok=False,
                        error=f"codex app-server silent after tool result for {post_tool_quiet_timeout_seconds:.0f}s",
                        error_code="codex_app_server_post_tool_timeout",
                        stderr=stderr,
                        thread_id=self._thread_id,
                        turn_id=turn_id,
                        duration_seconds=time.monotonic() - started,
                        events=events,
                        should_retire=True,
                    )
                if not client.is_alive():
                    stderr = "\n".join(client.stderr_tail(40))
                    return CodexAppServerResult(
                        ok=False,
                        error=stderr or "codex app-server exited unexpectedly",
                        error_code="codex_app_server_exited",
                        stderr=stderr,
                        thread_id=self._thread_id,
                        turn_id=turn_id,
                        duration_seconds=time.monotonic() - started,
                        events=events,
                        should_retire=True,
                    )
                note = client.take_notification(timeout=notification_poll_seconds)
                if note is None:
                    continue
                events.append(note)
                if len(events) > self.max_event_audit:
                    events = events[-self.max_event_audit :]
                preview = codex_app_server_event_preview(note)
                if preview and progress_callback is not None:
                    progress_callback(
                        {
                            "stage": "codex_app_server_stream",
                            "message": preview,
                            "thread_id": self._thread_id,
                            "turn_id": turn_id,
                        }
                    )
                item = ((note.get("params") or {}).get("item") or {}) if isinstance(note.get("params"), dict) else {}
                if item.get("type") == "commandExecution":
                    last_tool_at = time.monotonic()
                elif item.get("type") == "agentMessage":
                    final_text = str(item.get("text") or final_text)
                    last_tool_at = None
                if note.get("method") == "turn/completed":
                    return CodexAppServerResult(
                        ok=bool(final_text.strip()),
                        final_text=final_text,
                        error="" if final_text.strip() else "codex app-server completed without final text",
                        error_code="" if final_text.strip() else "codex_app_server_empty_final_text",
                        stderr="\n".join(client.stderr_tail(40)),
                        thread_id=self._thread_id,
                        turn_id=turn_id,
                        duration_seconds=time.monotonic() - started,
                        events=events,
                    )
            self._interrupt(client, turn_id)
            stderr = "\n".join(client.stderr_tail(40))
            return CodexAppServerResult(
                ok=False,
                error=f"codex app-server turn timed out after {turn_timeout_seconds:.0f}s",
                error_code="codex_app_server_turn_timeout",
                stderr=stderr,
                thread_id=self._thread_id,
                turn_id=turn_id,
                duration_seconds=time.monotonic() - started,
                events=events,
                should_retire=True,
            )
        except Exception as exc:
            stderr = "\n".join(self._client.stderr_tail(40)) if self._client is not None else ""
            return CodexAppServerResult(
                ok=False,
                error=str(exc),
                error_code="codex_app_server_error",
                stderr=stderr,
                thread_id=self._thread_id,
                turn_id=turn_id,
                duration_seconds=time.monotonic() - started,
                events=events,
                should_retire=True,
            )

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
            self._thread_id = ""

    def _ensure_client(self, *, startup_timeout_seconds: float) -> Any:
        if self._client is None:
            self._client = self.client_factory(
                codex_bin=self.codex_bin,
                cwd=self.cwd,
                sandbox_mode=self.sandbox_mode,
            )
            self._client.request(
                "initialize",
                {"clientInfo": {"name": "lark-agent-bridge", "title": "Lark Agent Bridge", "version": "0"}},
                timeout=startup_timeout_seconds,
            )
            self._client.notify("initialized")
            thread = self._client.request("thread/start", {"cwd": str(self.cwd)}, timeout=startup_timeout_seconds)
            thread_obj = thread.get("thread") or {}
            self._thread_id = str(
                thread_obj.get("id")
                or thread_obj.get("sessionId")
                or thread.get("threadId")
                or thread.get("sessionId")
                or ""
            )
            if not self._thread_id:
                raise RuntimeError("codex app-server thread/start returned no thread id")
        return self._client

    def _interrupt(self, client: Any, turn_id: str) -> None:
        if not self._thread_id or not turn_id:
            return
        try:
            client.request("turn/interrupt", {"threadId": self._thread_id, "turnId": turn_id}, timeout=5)
        except Exception:
            pass

    def _handle_server_request(self, client: Any, request: dict[str, Any]) -> None:
        method = str(request.get("method") or "")
        request_id = request.get("id")
        if method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
            "item/permissions/requestApproval",
        }:
            client.respond(request_id, {"decision": "decline"})
            return
        client.respond_error(request_id, code=-32601, message=f"Unsupported method: {method}")
```

- [ ] **Step 5: Run runtime tests**

Run:

```bash
PYTHONPATH=. pytest -q tests/test_codex_app_server_runtime.py
```

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add lark_agent_bridge/agents/codex_app_server_runtime.py tests/test_codex_app_server_runtime.py
git commit -m "runtime: project codex app-server turn events"
```

## Task 4: Integrate File-Agent Path Behind Config Flag

**Files:**
- Modify: `lark_agent_bridge/agents/bug_runner.py`
- Modify: `tests/test_agents.py`

- [ ] **Step 1: Write selection test**

Add to `tests/test_agents.py` near existing custom-skill/file-agent tests:

```python
def test_bug_analysis_custom_skill_uses_codex_app_server_when_enabled(self):
    with tempfile.TemporaryDirectory() as tmp:
        config = _make_config(Path(tmp))
        config.bug_analysis.provider = "codex"
        config.bug_analysis.command = "codex"
        config.codex_app_server.enabled = True
        config.codex_app_server.use_for_file_agent = True
        runner = BugAnalysisRunner(config)
        analysis_dir = Path(tmp) / "analysis"
        html_path = Path(tmp) / "report.html"
        json_path = Path(tmp) / "report.json"

        fake_result = {
            "ok": True,
            "message": "## 结论摘要\napp-server ok",
            "stdout": "",
            "stderr": "",
            "thread_id": "thread_1",
            "turn_id": "turn_1",
            "duration_seconds": 1.0,
            "events": [],
        }

        with mock.patch.object(runner, "_run_custom_skill_agent_via_codex_app_server", return_value=fake_result) as app_server:
            result = runner._run_custom_skill_agent_analysis(
                analysis_kind="source_stage",
                skill_name="source_analysis",
                request_text="查源码",
                prompt_text="查源码",
                title="bug",
                description="desc",
                fault_time="",
                selected_input=None,
                prepared_input=None,
                source_evidence_path=None,
                html_path=html_path,
                json_path=json_path,
                analysis_dir=analysis_dir,
                progress_callback=None,
                timeout=30,
            )

    self.assertTrue(result["ok"])
    self.assertEqual(result["provider"], "codex_app_server")
    app_server.assert_called_once()
```

If `_make_config` is not available in the local test class, use the existing helper used by nearby tests. Do not create a second helper with a conflicting shape.

- [ ] **Step 2: Add integration branch**

In `_run_custom_skill_agent_analysis()` after `invocation` is built and before `_run_tracked_process()`, add:

```python
        if (
            provider == "codex"
            and self.config.codex_app_server.enabled
            and self.config.codex_app_server.use_for_file_agent
        ):
            app_server_result = self._run_custom_skill_agent_via_codex_app_server(
                prompt=str(invocation.get("prompt") or ""),
                cwd=process_cwd,
                analysis_markdown_path=analysis_markdown_path,
                progress_callback=progress_callback,
                timeout=timeout,
            )
            if app_server_result.get("ok"):
                stdout = str(app_server_result.get("stdout") or "")
                stderr = str(app_server_result.get("stderr") or "")
                stdout_path.write_text(stdout, encoding="utf-8")
                stderr_path.write_text(stderr, encoding="utf-8")
                completed = subprocess.CompletedProcess(
                    args=["codex", "app-server"],
                    returncode=0,
                    stdout=stdout,
                    stderr=stderr,
                )
                provider = "codex_app_server"
            elif not self.config.codex_app_server.fallback_to_exec:
                stdout = str(app_server_result.get("stdout") or "")
                stderr = str(app_server_result.get("stderr") or "")
                stdout_path.write_text(stdout, encoding="utf-8")
                stderr_path.write_text(stderr, encoding="utf-8")
                return {
                    "ok": False,
                    "error_code": str(app_server_result.get("error_code") or "codex_app_server_failed"),
                    "message": str(app_server_result.get("error") or "Codex app-server failed"),
                    "command": ["codex", "app-server"],
                    "provider": "codex_app_server",
                    "analysis_markdown_path": analysis_markdown_path,
                    "stdout_path": stdout_path,
                    "stderr_path": stderr_path,
                    "command_path": command_path,
                    "context_path": context_path,
                    "log_focus_manifest_path": focus_manifest,
                    "focused_log_input": focused_log_input,
                    "focused_log_files": [str(path) for path in focused_files],
                    "debug_log_path": debug_log_path,
                    "stdout": stdout,
                    "stderr": stderr,
                    "duration_seconds": time.monotonic() - started,
                }
            else:
                self._emit_progress(
                    progress_callback,
                    stage=f"{analysis_kind}_agent_analysis",
                    message="Codex app-server 失败，回退到 codex exec",
                    provider="codex_app_server",
                    error=str(app_server_result.get("error") or ""),
                )
```

This step intentionally leaves the existing `_run_tracked_process()` block in place for fallback. After a successful app-server result, skip the subprocess block by restructuring the existing try block into:

```python
        completed = None
        if provider != "codex_app_server":
            try:
                ...
            except subprocess.TimeoutExpired as exc:
                ...
```

- [ ] **Step 3: Add helper method**

Add to `BugAnalysisRunner` near subprocess helpers:

```python
    def _run_custom_skill_agent_via_codex_app_server(
        self,
        *,
        prompt: str,
        cwd: Path,
        analysis_markdown_path: Path,
        progress_callback: Callable[[dict[str, object]], None] | None,
        timeout: int,
    ) -> dict[str, object]:
        from .codex_app_server_runtime import CodexAppServerRuntime

        opts = self.config.codex_app_server
        runtime = CodexAppServerRuntime(
            codex_bin=opts.command or "codex",
            cwd=cwd,
            sandbox_mode=opts.sandbox_mode,
            max_event_audit=opts.max_event_audit,
        )
        try:
            result = runtime.run_turn(
                user_input=prompt,
                progress_callback=progress_callback,
                startup_timeout_seconds=opts.startup_timeout_seconds,
                turn_timeout_seconds=min(float(timeout), opts.turn_timeout_seconds),
                post_tool_quiet_timeout_seconds=opts.post_tool_quiet_timeout_seconds,
                notification_poll_seconds=opts.notification_poll_seconds,
            )
        finally:
            runtime.close()
        if result.ok and result.final_text.strip():
            analysis_markdown_path.write_text(result.final_text.strip() + "\n", encoding="utf-8")
        return {
            "ok": result.ok,
            "message": result.final_text,
            "error": result.error,
            "error_code": result.error_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "thread_id": result.thread_id,
            "turn_id": result.turn_id,
            "duration_seconds": result.duration_seconds,
            "events": result.events,
            "should_retire": result.should_retire,
        }
```

- [ ] **Step 4: Persist app-server event audit**

In the helper, after `result = runtime.run_turn(...)`, write:

```python
        events_path = analysis_markdown_path.with_name(
            analysis_markdown_path.stem + "_codex_app_server_events.jsonl"
        )
        try:
            events_path.write_text(
                "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in result.events),
                encoding="utf-8",
            )
        except OSError:
            pass
```

Then include `"events_path": str(events_path)` in the returned dict.

- [ ] **Step 5: Write fallback test**

Add:

```python
def test_bug_analysis_custom_skill_app_server_failure_falls_back_to_exec(self):
    with tempfile.TemporaryDirectory() as tmp:
        config = _make_config(Path(tmp))
        config.bug_analysis.provider = "codex"
        config.bug_analysis.command = "codex"
        config.codex_app_server.enabled = True
        config.codex_app_server.use_for_file_agent = True
        config.codex_app_server.fallback_to_exec = True
        runner = BugAnalysisRunner(config)
        analysis_dir = Path(tmp) / "analysis"

        with (
            mock.patch.object(
                runner,
                "_run_custom_skill_agent_via_codex_app_server",
                return_value={"ok": False, "error": "boom", "error_code": "codex_app_server_error"},
            ),
            mock.patch("lark_agent_bridge.agents.bug_runner._run_tracked_process") as tracked,
        ):
            tracked.return_value = subprocess.CompletedProcess(
                args=["codex", "exec"],
                returncode=0,
                stdout=json.dumps({"result": "## 结论摘要\nexec ok\n\n## 关键证据\n- evidence\n\n## 最可能原因\nok\n\n## 待确认项\n- none\n\n## 建议动作\n- check"}, ensure_ascii=False),
                stderr="",
            )
            result = runner._run_custom_skill_agent_analysis(
                analysis_kind="source_stage",
                skill_name="source_analysis",
                request_text="查源码",
                prompt_text="查源码",
                title="bug",
                description="desc",
                fault_time="",
                selected_input=None,
                prepared_input=None,
                source_evidence_path=None,
                html_path=Path(tmp) / "report.html",
                json_path=Path(tmp) / "report.json",
                analysis_dir=analysis_dir,
                progress_callback=None,
                timeout=30,
            )

    self.assertTrue(result["ok"])
    tracked.assert_called_once()
```

Use the existing custom-skill validation requirements in nearby tests. If the validation helper requires `## 关键证据`, keep that heading non-empty as shown.

- [ ] **Step 6: Run focused tests**

Run:

```bash
PYTHONPATH=. pytest -q tests/test_agents.py -k "app_server or custom_skill_file_agent or source_stage"
```

Expected: all selected tests pass.

- [ ] **Step 7: Commit**

```bash
git add lark_agent_bridge/agents/bug_runner.py tests/test_agents.py
git commit -m "runtime: use codex app-server for optional file-agent analysis"
```

## Task 5: Runtime Availability And Version Gate

**Files:**
- Modify: `lark_agent_bridge/agents/codex_app_server_runtime.py`
- Modify: `lark_agent_bridge/agents/bug_runner.py`
- Modify: `tests/test_codex_app_server_runtime.py`
- Modify: `tests/test_agents.py`

- [ ] **Step 1: Write binary check tests**

Add:

```python
from lark_agent_bridge.agents.codex_app_server_runtime import check_codex_app_server_available


class TestCodexAppServerAvailability(unittest.TestCase):
    def test_missing_binary_is_not_available(self):
        ok, message = check_codex_app_server_available("/definitely/missing/codex", "0.125.0")
        self.assertFalse(ok)
        self.assertIn("not found", message.lower())

    def test_old_version_is_not_available(self):
        completed = subprocess.CompletedProcess(["codex", "--version"], 0, "codex-cli 0.1.0\n", "")
        with mock.patch("subprocess.run", return_value=completed):
            ok, message = check_codex_app_server_available("codex", "0.125.0")
        self.assertFalse(ok)
        self.assertIn("older", message.lower())

    def test_new_version_is_available(self):
        completed = subprocess.CompletedProcess(["codex", "--version"], 0, "codex-cli 0.134.0\n", "")
        with mock.patch("subprocess.run", return_value=completed):
            ok, message = check_codex_app_server_available("codex", "0.125.0")
        self.assertTrue(ok)
        self.assertEqual(message, "0.134.0")
```

- [ ] **Step 2: Implement version gate**

Add:

```python
def _version_tuple(value: str) -> tuple[int, int, int]:
    parsed = parse_codex_version(value)
    if parsed is None:
        return (0, 0, 0)
    return parsed


def check_codex_app_server_available(codex_bin: str, min_version: str) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            [codex_bin, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except FileNotFoundError:
        return False, f"codex CLI not found: {codex_bin}"
    except subprocess.TimeoutExpired:
        return False, "codex --version timed out"
    if completed.returncode != 0:
        return False, (completed.stderr or completed.stdout or f"codex exited {completed.returncode}").strip()
    version = parse_codex_version(completed.stdout)
    if version is None:
        return False, f"could not parse codex version from {completed.stdout!r}"
    required = _version_tuple(min_version)
    if version < required:
        return False, f"codex {'.'.join(map(str, version))} is older than required {min_version}"
    return True, ".".join(map(str, version))
```

- [ ] **Step 3: Gate before app-server path**

In `_run_custom_skill_agent_analysis()` before calling `_run_custom_skill_agent_via_codex_app_server`, add:

```python
            from .codex_app_server_runtime import check_codex_app_server_available

            available, availability_message = check_codex_app_server_available(
                self.config.codex_app_server.command or "codex",
                self.config.codex_app_server.min_version,
            )
            if not available:
                app_server_result = {
                    "ok": False,
                    "error": availability_message,
                    "error_code": "codex_app_server_unavailable",
                    "stdout": "",
                    "stderr": availability_message,
                }
            else:
                app_server_result = self._run_custom_skill_agent_via_codex_app_server(...)
```

When inserting this, keep the existing `fallback_to_exec` branch unchanged.

- [ ] **Step 4: Add unavailable fallback test**

Patch `check_codex_app_server_available` to return false and assert `_run_tracked_process` is called when `fallback_to_exec=true`.

```python
with (
    mock.patch(
        "lark_agent_bridge.agents.codex_app_server_runtime.check_codex_app_server_available",
        return_value=(False, "missing"),
    ),
    mock.patch("lark_agent_bridge.agents.bug_runner._run_tracked_process") as tracked,
):
    ...
```

- [ ] **Step 5: Run tests**

Run:

```bash
PYTHONPATH=. pytest -q tests/test_codex_app_server_runtime.py tests/test_agents.py -k "app_server"
```

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add lark_agent_bridge/agents/codex_app_server_runtime.py lark_agent_bridge/agents/bug_runner.py tests/test_codex_app_server_runtime.py tests/test_agents.py
git commit -m "runtime: gate codex app-server by cli availability"
```

## Task 6: Progress, Audit, And Failure Semantics

**Files:**
- Modify: `lark_agent_bridge/agents/codex_app_server_runtime.py`
- Modify: `lark_agent_bridge/agents/bug_runner.py`
- Modify: `tests/test_codex_app_server_runtime.py`
- Modify: `tests/test_agents.py`

- [ ] **Step 1: Add progress schema test**

Add a test that captures progress payloads and asserts these keys exist:

```python
self.assertIn("stage", progress[0])
self.assertIn("message", progress[0])
self.assertIn("thread_id", progress[0])
self.assertIn("turn_id", progress[0])
```

Expected stage values:

- `codex_app_server_stream`
- `source_stage_agent_analysis` for the outer bug runner stage

- [ ] **Step 2: Add redacted stderr tail helper**

Implement:

```python
_SENSITIVE_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._-]{12,}", re.IGNORECASE),
)


def redact_codex_diagnostics(text: str) -> str:
    redacted = text or ""
    for pattern in _SENSITIVE_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted
```

Use it before returning `stderr` in runtime failures:

```python
stderr = redact_codex_diagnostics("\n".join(client.stderr_tail(40)))
```

- [ ] **Step 3: Test redaction**

Add:

```python
from lark_agent_bridge.agents.codex_app_server_runtime import redact_codex_diagnostics


def test_redact_codex_diagnostics(self):
    text = "Authorization: Bearer abcdefghijklmnop\nkey=sk-1234567890abcdef"
    redacted = redact_codex_diagnostics(text)
    self.assertNotIn("abcdefghijklmnop", redacted)
    self.assertNotIn("sk-1234567890abcdef", redacted)
    self.assertIn("[REDACTED]", redacted)
```

- [ ] **Step 4: Ensure app-server failures appear in analysis sidecars**

When `_run_custom_skill_agent_via_codex_app_server()` returns failure, always write:

```python
stdout_path.write_text(stdout, encoding="utf-8")
stderr_path.write_text(stderr, encoding="utf-8")
```

and include `thread_id`, `turn_id`, `events_path`, and `should_retire` in the returned details.

- [ ] **Step 5: Run tests**

Run:

```bash
PYTHONPATH=. pytest -q tests/test_codex_app_server_runtime.py tests/test_agents.py -k "app_server or redaction"
```

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add lark_agent_bridge/agents/codex_app_server_runtime.py lark_agent_bridge/agents/bug_runner.py tests/test_codex_app_server_runtime.py tests/test_agents.py
git commit -m "runtime: add codex app-server progress and diagnostics"
```

## Task 7: Optional Bug Summary Runtime Switch

**Files:**
- Modify: `lark_agent_bridge/agents/bug_runner.py`
- Modify: `tests/test_agents.py`
- Modify: `docs/configuration.md`

This task should start only after file-agent path has passed a real Feishu smoke test.

- [ ] **Step 1: Add test that default summary path does not use app-server**

Configure:

```python
config.codex_app_server.enabled = True
config.codex_app_server.use_for_file_agent = True
config.codex_app_server.use_for_bug_summary = False
```

Patch `_run_bug_agent_summary_streaming_process` and assert it is still called for provider `codex`.

- [ ] **Step 2: Add test for summary app-server when enabled**

Configure:

```python
config.codex_app_server.enabled = True
config.codex_app_server.use_for_bug_summary = True
```

Patch a new helper `_run_bug_agent_summary_via_codex_app_server()` to return:

```python
{
    "message": "summary via app-server",
    "command": ["codex", "app-server"],
    "error": "",
    "provider": "codex_app_server",
    "session_id": "thread_1",
    "resumed": False,
    "duration_seconds": 1.0,
    "usage": {},
}
```

Assert `_run_bug_agent_summary()` returns provider `codex_app_server` and message `summary via app-server`.

- [ ] **Step 3: Implement summary helper**

Use the same runtime class:

```python
    def _run_bug_agent_summary_via_codex_app_server(
        self,
        *,
        invocation: dict[str, object],
        output_path: Path,
        progress_callback: Callable[[dict[str, object]], None] | None,
        timeout: int,
    ) -> dict[str, object]:
        prompt = str(invocation.get("prompt") or "")
        opts = self.config.codex_app_server
        from .codex_app_server_runtime import CodexAppServerRuntime

        runtime = CodexAppServerRuntime(
            codex_bin=opts.command or "codex",
            cwd=self._working_dir(),
            sandbox_mode=opts.sandbox_mode,
            max_event_audit=opts.max_event_audit,
        )
        try:
            result = runtime.run_turn(
                user_input=prompt,
                progress_callback=progress_callback,
                startup_timeout_seconds=opts.startup_timeout_seconds,
                turn_timeout_seconds=min(float(timeout), opts.turn_timeout_seconds),
                post_tool_quiet_timeout_seconds=opts.post_tool_quiet_timeout_seconds,
                notification_poll_seconds=opts.notification_poll_seconds,
            )
        finally:
            runtime.close()
        if result.ok:
            output_path.write_text(result.final_text.strip() + "\n", encoding="utf-8")
        return {
            "message": result.final_text,
            "command": ["codex", "app-server"],
            "error": result.error,
            "provider": "codex_app_server",
            "session_id": result.thread_id,
            "resumed": False,
            "duration_seconds": result.duration_seconds,
            "usage": {},
        }
```

- [ ] **Step 4: Call helper only when explicitly enabled**

At the start of `_run_bug_agent_summary_once()` or `_run_bug_agent_summary()`, choose app-server only when:

```python
provider == "codex"
and self.config.codex_app_server.enabled
and self.config.codex_app_server.use_for_bug_summary
```

If helper fails and `fallback_to_exec=true`, emit progress and continue to current `codex exec` path.

- [ ] **Step 5: Run summary tests**

Run:

```bash
PYTHONPATH=. pytest -q tests/test_agents.py -k "bug_agent_summary and app_server"
```

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add lark_agent_bridge/agents/bug_runner.py tests/test_agents.py docs/configuration.md
git commit -m "runtime: add optional codex app-server summary path"
```

## Task 8: Manual And Feishu-In-The-Loop Verification

**Files:**
- No code changes unless verification finds a bug.
- Read: `data/state/agent_activity.json`
- Inspect: generated job output directory

- [ ] **Step 1: Local CLI smoke test**

Run a narrow unit/integration selection:

```bash
PYTHONPATH=. pytest -q tests/test_config.py -k codex_app_server
PYTHONPATH=. pytest -q tests/test_codex_app_server_runtime.py
PYTHONPATH=. pytest -q tests/test_agents.py -k "app_server or custom_skill_file_agent or source_stage"
```

Expected: all pass.

- [ ] **Step 2: Live app-server handshake check**

Run:

```bash
codex --version
codex app-server --help
```

Expected:

- `codex-cli` version is at least configured `min_version`.
- help shows `--listen <URL>` and default `stdio://`.

- [ ] **Step 3: Enable only file-agent path in local config**

Use a temporary config or local uncommitted config edit:

```toml
[codex_app_server]
enabled = true
use_for_file_agent = true
use_for_bug_summary = false
fallback_to_exec = true
sandbox_mode = "read-only"
```

Do not commit local secrets or machine-specific config.

- [ ] **Step 4: Trigger one source-stage request**

Preferred Feishu test group if user does not name another group:

```text
oc_d977fe30a92c7ac81e3e6b543d99ef5b
```

Send a source-analysis-explicit request against a known bug or direct log input. The request must explicitly ask for source/code evidence so ordinary domain-only bug requests are not polluted.

- [ ] **Step 5: Verify runtime state**

Inspect latest session:

```bash
python - <<'PY'
import json
from pathlib import Path
p = Path("data/state/agent_activity.json")
data = json.loads(p.read_text())
sessions = list((data.get("sessions") or {}).values())
sessions.sort(key=lambda x: x.get("updated_at", ""))
latest = sessions[-1]
print(json.dumps({
    "session_id": latest.get("session_id"),
    "status": latest.get("status"),
    "job_id": latest.get("job_id"),
    "mode": latest.get("mode"),
    "details": latest.get("details", {}),
    "progress_tail": latest.get("progress", [])[-5:],
}, ensure_ascii=False, indent=2))
PY
```

Expected:

- status is `succeeded`, or if failed, error code is app-server-specific and fallback behavior is visible.
- progress contains `codex_app_server_stream` when app-server was used.
- output directory contains `*_codex_app_server_events.jsonl`.
- `analysis_markdown_path` has non-empty `## 关键证据`.

- [ ] **Step 6: Verify ordinary bug request is unchanged**

Send or simulate an ordinary non-source bug request. Expected:

- No app-server source-stage artifact unless source request is explicit.
- Existing domain analysis and summary behavior are unchanged.

- [ ] **Step 7: Document result**

Append a short verification note to `docs/configuration.md` or a new rollout note only if the test changes operating guidance. Do not paste sensitive chat IDs, tokens, cookies, or full logs.

## Task 9: Cleanup And Rollout Decision

**Files:**
- Modify only if verification reveals concrete issues.

- [ ] **Step 1: Review git diff**

Run:

```bash
git status --short
git diff -- lark_agent_bridge/agents/codex_app_server_runtime.py lark_agent_bridge/agents/bug_runner.py lark_agent_bridge/models.py lark_agent_bridge/config.py tests/test_codex_app_server_runtime.py tests/test_agents.py tests/test_config.py docs/configuration.md config/config.example.toml
```

Expected:

- No secrets.
- No unrelated refactors.
- `codex exec` fallback remains intact.
- New runtime is disabled by default.

- [ ] **Step 2: Full focused regression**

Run:

```bash
PYTHONPATH=. pytest -q tests/test_config.py tests/test_codex_app_server_runtime.py tests/test_agent_runtime.py tests/test_agents.py -k "codex or app_server or source_stage or custom_skill or bug_agent_summary"
```

Expected: pass. If selection is too broad/slow, run the failing subset first and record exact skipped boundary.

- [ ] **Step 3: Rollout choice**

Use this submit rule:

- Submit if config tests, runtime tests, focused bug-runner tests, and one Feishu/source-stage smoke pass.
- Keep behind config and do not enable by default.
- Do not enable `use_for_bug_summary` until file-agent app-server has at least one successful live source-stage run and one fallback run.

- [ ] **Step 4: Commit final verification note**

If the branch is being committed:

```bash
git add .
git commit -m "runtime: integrate optional codex app-server execution"
```

If only some tasks were implemented, commit only completed task files with the task-specific commit messages above.

## Self-Review

Spec coverage:

- Hermes-style app-server usage is represented as JSON-RPC client, session runtime, event projection, approval denial, interrupt, timeout, and retire semantics.
- Current bridge constraints are covered: default `codex exec` remains; file-agent path is first; bug summary path is optional later.
- Feishu/runtime verification is included and checks `agent_activity.json`, progress, and output artifacts.

Placeholder scan:

- No planned code step depends on unnamed helper except existing local test helpers in `tests/test_agents.py`; the plan instructs to reuse the existing helper in that file.
- No production behavior is described as "later" without a task; later-phase bug summary is Task 7.

Type consistency:

- Config field name is consistently `codex_app_server`.
- Runtime result field names match `CodexAppServerResult`.
- Progress stage is consistently `codex_app_server_stream`.

## Execution Handoff

Plan complete. Recommended execution order is Tasks 1-6 first, then one real Feishu smoke test, then Task 7 only if the first rollout behaves correctly.
