"""Codex app-server runtime helpers for bridge-managed analysis jobs."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import json
import queue
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable


_MIN_CODEX_VERSION = (0, 125, 0)
_APPROVAL_METHODS = {
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "item/permissions/requestApproval",
}
_TOOL_ITEM_TYPES = {"commandExecution", "fileChange", "dynamicToolCall", "mcpToolCall"}
_PARTIAL_ERROR_CODES = {"codex_app_server_no_event_timeout", "codex_app_server_turn_timeout"}


class CompletionState(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"


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
    usage: dict[str, object] = field(default_factory=dict)
    completion_state: CompletionState = CompletionState.FAILED


@dataclass(slots=True)
class CodexAppServerError(RuntimeError):
    code: int
    message: str
    data: object | None = None

    def __str__(self) -> str:
        return f"codex app-server error {self.code}: {self.message}"


@dataclass(slots=True)
class _PendingRequest:
    response_queue: queue.Queue[dict[str, object]]


def parse_codex_version(output: str) -> tuple[int, int, int] | None:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", output or "")
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def check_codex_app_server_available(command: str, min_version: str) -> tuple[bool, str]:
    required = parse_codex_version(min_version)
    if required is None:
        required = _MIN_CODEX_VERSION
    try:
        completed = subprocess.run(
            [command, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except FileNotFoundError:
        return False, f"codex CLI not found at {command!r}"
    except subprocess.TimeoutExpired:
        return False, "codex --version timed out"
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        return False, f"codex --version exited {completed.returncode}: {stderr}"
    version = parse_codex_version(completed.stdout)
    if version is None:
        return False, f"could not parse codex version from {completed.stdout!r}"
    if version < required:
        return False, f"codex {'.'.join(map(str, version))} is older than required {'.'.join(map(str, required))}"
    return True, ".".join(map(str, version))


def app_server_event_preview(event: dict[str, object]) -> str:
    method = str(event.get("method") or "")
    params = event.get("params") or {}
    if not isinstance(params, dict):
        params = {}
    if method == "thread/started":
        return "Codex thread started"
    if method == "turn/started":
        return "Codex turn started"
    if method == "turn/completed":
        turn = params.get("turn") or {}
        if isinstance(turn, dict):
            status = str(turn.get("status") or "completed")
            duration_ms = turn.get("durationMs")
            if isinstance(duration_ms, (int, float)):
                return f"Codex turn completed status={status} duration_ms={int(duration_ms)}"
            return f"Codex turn completed status={status}"
        return "Codex turn completed"
    if method == "thread/tokenUsage/updated":
        token_usage = params.get("tokenUsage") or {}
        if isinstance(token_usage, dict):
            total = token_usage.get("total") or {}
            if isinstance(total, dict):
                total_tokens = total.get("totalTokens")
                input_tokens = total.get("inputTokens")
                output_tokens = total.get("outputTokens")
                if isinstance(total_tokens, int):
                    if isinstance(input_tokens, int) and isinstance(output_tokens, int):
                        return (
                            "Codex token usage "
                            f"token≈{total_tokens} input={input_tokens} output={output_tokens}"
                        )
                    return f"Codex token usage token≈{total_tokens}"
        return ""
    if method == "error":
        error = params.get("error") or {}
        if isinstance(error, dict):
            message = _compact_text(str(error.get("message") or ""), max_chars=160)
            details = _compact_text(str(error.get("additionalDetails") or ""), max_chars=120)
            if message and details:
                return f"Codex error {message} {details}"
            if message:
                return f"Codex error {message}"
        return "Codex error"
    if method == "warning":
        message = _compact_text(str(params.get("message") or ""), max_chars=180)
        return f"Codex warning {message}" if message else "Codex warning"
    if method == "mcpServer/startupStatus/updated":
        name = str(params.get("name") or "")
        status = str(params.get("status") or "")
        if name and status:
            return f"MCP {name} {status}"
        return ""
    if method in {"hook/started", "hook/completed"}:
        run = params.get("run") or {}
        if isinstance(run, dict):
            event_name = str(run.get("eventName") or "")
            status = str(run.get("status") or "")
            if event_name:
                return f"Hook {event_name} {status or method.split('/', 1)[1]}"
        return ""
    if method in {"item/started", "item/completed"}:
        item = params.get("item") or {}
        if not isinstance(item, dict):
            return ""
        item_type = str(item.get("type") or "")
        if item_type == "commandExecution":
            command = _unwrap_shell_command(str(item.get("command") or ""))
            if command:
                return f"Codex command {command}"
            return "Codex command"
        if item_type == "agentMessage":
            phase = str(item.get("phase") or "")
            text = _compact_text(str(item.get("text") or ""), max_chars=180)
            if text:
                if phase:
                    return f"Codex {phase} {text}"
                return f"Codex message {text}"
            return ""
        if item_type == "reasoning":
            return ""
        if item_type == "dynamicToolCall":
            tool = str(item.get("tool") or "")
            return f"Codex tool {tool}" if tool else "Codex tool call"
        if item_type == "mcpToolCall":
            server = str(item.get("server") or "mcp")
            tool = str(item.get("tool") or "")
            return f"Codex MCP {server}.{tool}" if tool else f"Codex MCP {server}"
        if item_type == "fileChange":
            return "Codex file change request"
    if method == "item/agentMessage/delta":
        return ""
    return ""


class CodexAppServerClient:
    def __init__(
        self,
        *,
        command: str,
        sandbox_mode: str,
        disable_node_repl: bool = True,
        emit_node_repl_flag: bool = True,
        disable_analytics: bool = True,
        disable_memories: bool = True,
        disable_apps_feature: bool = True,
        disable_plugins_feature: bool = True,
        disable_computer_use_feature: bool = True,
        reasoning_effort: str = "",
        env: dict[str, str] | None = None,
        cwd: Path | None = None,
    ) -> None:
        spawn_env = dict(env or {})
        spawn_env.setdefault("RUST_LOG", "warn")
        self._command = _build_app_server_command(
            command=command,
            sandbox_mode=sandbox_mode,
            disable_node_repl=disable_node_repl,
            emit_node_repl_flag=emit_node_repl_flag,
            disable_analytics=disable_analytics,
            disable_memories=disable_memories,
            disable_apps_feature=disable_apps_feature,
            disable_plugins_feature=disable_plugins_feature,
            disable_computer_use_feature=disable_computer_use_feature,
            reasoning_effort=reasoning_effort,
        )
        self._process = subprocess.Popen(
            self._command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            cwd=str(cwd) if cwd is not None else None,
            env=spawn_env,
        )
        self._next_id = 1
        self._pending: dict[int, _PendingRequest] = {}
        self._pending_lock = threading.Lock()
        self._notifications: queue.Queue[dict[str, object]] = queue.Queue()
        self._server_requests: queue.Queue[dict[str, object]] = queue.Queue()
        self._stderr_lines: list[str] = []
        self._stderr_lock = threading.Lock()
        self._closed = False
        self._stdout_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()

    @property
    def spawn_command(self) -> list[str]:
        return list(self._command)

    def initialize(self, *, timeout: float) -> dict[str, object]:
        result = self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "lark-agent-bridge",
                    "title": "Lark Agent Bridge",
                    "version": "0.1",
                },
                "capabilities": {},
            },
            timeout=timeout,
        )
        self.notify("initialized", {})
        return result

    def request(
        self,
        method: str,
        params: dict[str, object] | None,
        *,
        timeout: float,
    ) -> dict[str, object]:
        request_id = self._next_request_id()
        response_queue: queue.Queue[dict[str, object]] = queue.Queue(maxsize=1)
        with self._pending_lock:
            self._pending[request_id] = _PendingRequest(response_queue=response_queue)
        self._send({"id": request_id, "method": method, "params": params or {}})
        try:
            response = response_queue.get(timeout=timeout)
        except queue.Empty as exc:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise TimeoutError(f"codex app-server method {method!r} timed out after {timeout}s") from exc
        if "error" in response:
            error = response.get("error") or {}
            if not isinstance(error, dict):
                error = {"message": str(error)}
            raise CodexAppServerError(
                code=int(error.get("code", -1)),
                message=str(error.get("message") or ""),
                data=error.get("data"),
            )
        result = response.get("result") or {}
        if not isinstance(result, dict):
            return {"value": result}
        return result

    def notify(self, method: str, params: dict[str, object] | None = None) -> None:
        self._send({"method": method, "params": params or {}})

    def respond(self, request_id: object, result: dict[str, object]) -> None:
        self._send({"id": request_id, "result": result})

    def respond_error(self, request_id: object, code: int, message: str) -> None:
        self._send({"id": request_id, "error": {"code": code, "message": message}})

    def take_notification(self, *, timeout: float) -> dict[str, object] | None:
        try:
            if timeout <= 0:
                return self._notifications.get_nowait()
            return self._notifications.get(timeout=timeout)
        except queue.Empty:
            return None

    def take_server_request(self, *, timeout: float) -> dict[str, object] | None:
        try:
            if timeout <= 0:
                return self._server_requests.get_nowait()
            return self._server_requests.get(timeout=timeout)
        except queue.Empty:
            return None

    def stderr_tail(self, n: int = 20) -> list[str]:
        with self._stderr_lock:
            return list(self._stderr_lines[-n:])

    def stderr_line_count(self) -> int:
        with self._stderr_lock:
            return len(self._stderr_lines)

    def is_alive(self) -> bool:
        return self._process.poll() is None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._process.stdin is not None and not self._process.stdin.closed:
                self._process.stdin.close()
        except OSError:
            pass
        try:
            self._process.terminate()
            self._process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait(timeout=1)

    def _next_request_id(self) -> int:
        request_id = self._next_id
        self._next_id += 1
        return request_id

    def _send(self, payload: dict[str, object]) -> None:
        if self._closed:
            raise RuntimeError("codex app-server client is closed")
        if self._process.stdin is None:
            raise RuntimeError("codex app-server stdin is unavailable")
        encoded = (json.dumps(payload) + "\n").encode("utf-8")
        self._process.stdin.write(encoded)
        self._process.stdin.flush()

    def _read_stdout(self) -> None:
        if self._process.stdout is None:
            return
        try:
            for raw_line in iter(self._process.stdout.readline, b""):
                if not raw_line:
                    break
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    with self._stderr_lock:
                        self._stderr_lines.append(f"<non-json stdout> {line[:200]}")
                    continue
                self._dispatch(message)
        except Exception as exc:  # pragma: no cover - defensive reader guard
            with self._stderr_lock:
                self._stderr_lines.append(f"<stdout reader error> {exc}")

    def _dispatch(self, message: dict[str, object]) -> None:
        if "id" in message and ("result" in message or "error" in message):
            with self._pending_lock:
                pending = self._pending.pop(int(message["id"]), None)
            if pending is not None:
                pending.response_queue.put_nowait(message)
            return
        if "id" in message and "method" in message:
            self._server_requests.put(message)
            return
        if "method" in message:
            self._notifications.put(message)

    def _read_stderr(self) -> None:
        if self._process.stderr is None:
            return
        try:
            for raw_line in iter(self._process.stderr.readline, b""):
                if not raw_line:
                    break
                line = raw_line.decode("utf-8", errors="replace").rstrip()
                with self._stderr_lock:
                    self._stderr_lines.append(line)
                    if len(self._stderr_lines) > 500:
                        self._stderr_lines = self._stderr_lines[-500:]
        except Exception:  # pragma: no cover - defensive reader guard
            return


class CodexAppServerRuntime:
    def __init__(
        self,
        *,
        command: str,
        cwd: str | Path,
        startup_timeout_seconds: float,
        turn_timeout_seconds: float,
        post_tool_quiet_timeout_seconds: float,
        notification_poll_seconds: float,
        max_event_audit: int,
        sandbox_mode: str,
        no_event_timeout_seconds: float = 0.0,
        disable_node_repl: bool = True,
        emit_node_repl_flag: bool = True,
        disable_analytics: bool = True,
        disable_memories: bool = True,
        disable_apps_feature: bool = True,
        disable_plugins_feature: bool = True,
        disable_computer_use_feature: bool = True,
        reasoning_effort: str = "",
        env: dict[str, str] | None = None,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.command = command
        self.cwd = Path(cwd)
        self.startup_timeout_seconds = startup_timeout_seconds
        self.turn_timeout_seconds = turn_timeout_seconds
        self.post_tool_quiet_timeout_seconds = post_tool_quiet_timeout_seconds
        self.no_event_timeout_seconds = no_event_timeout_seconds
        self.notification_poll_seconds = notification_poll_seconds
        self.max_event_audit = max_event_audit
        self.sandbox_mode = sandbox_mode
        self.disable_node_repl = disable_node_repl
        self.emit_node_repl_flag = emit_node_repl_flag
        self.disable_analytics = disable_analytics
        self.disable_memories = disable_memories
        self.disable_apps_feature = disable_apps_feature
        self.disable_plugins_feature = disable_plugins_feature
        self.disable_computer_use_feature = disable_computer_use_feature
        self.reasoning_effort = reasoning_effort
        self.env = dict(env or {})
        self.client_factory = client_factory or CodexAppServerClient

    def run_turn(
        self,
        prompt: str,
        *,
        on_event: Callable[[dict[str, object]], None] | None = None,
    ) -> CodexAppServerResult:
        started = time.monotonic()
        client = self.client_factory(
            command=self.command,
            sandbox_mode=self.sandbox_mode,
            disable_node_repl=self.disable_node_repl,
            emit_node_repl_flag=self.emit_node_repl_flag,
            disable_analytics=self.disable_analytics,
            disable_memories=self.disable_memories,
            disable_apps_feature=self.disable_apps_feature,
            disable_plugins_feature=self.disable_plugins_feature,
            disable_computer_use_feature=self.disable_computer_use_feature,
            reasoning_effort=self.reasoning_effort,
            env=self.env,
            cwd=self.cwd,
        )
        spawn_command = list(getattr(client, "spawn_command", [self.command, "app-server"]))
        thread_id = ""
        turn_id = ""
        final_text = ""
        final_answer_text = ""
        agent_message_phases: dict[str, str] = {}
        agent_message_buffers: dict[str, list[str]] = {}
        usage: dict[str, object] = {}
        preview_lines: list[str] = []
        events: list[dict[str, object]] = []
        stderr_text = ""
        last_progress_at = time.monotonic()
        stderr_seen = 0
        no_event_timeout = (
            self.no_event_timeout_seconds
            if self.no_event_timeout_seconds > 0
            else self.post_tool_quiet_timeout_seconds
        )
        turn_completed = False
        result_error = ""
        result_error_code = ""
        should_retire = False

        try:
            client.initialize(timeout=self.startup_timeout_seconds)
            thread_result = client.request(
                "thread/start",
                {"cwd": str(self.cwd)},
                timeout=self.startup_timeout_seconds,
            )
            thread_id = _extract_thread_id(thread_result)
            turn_result = client.request(
                "turn/start",
                {
                    "threadId": thread_id,
                    "input": [{"type": "text", "text": prompt}],
                },
                timeout=self.startup_timeout_seconds,
            )
            turn_id = _extract_turn_id(turn_result)
            deadline = time.monotonic() + self.turn_timeout_seconds
            last_progress_at = time.monotonic()

            while time.monotonic() < deadline:
                if not client.is_alive():
                    result_error = "codex app-server subprocess exited unexpectedly"
                    result_error_code = "codex_app_server_exited"
                    should_retire = True
                    break

                now = time.monotonic()
                # Any new stderr line counts as progress (e.g. silent reasoning
                # that still logs), so long reasoning is not killed as a stall.
                stderr_count = client.stderr_line_count()
                if stderr_count > stderr_seen:
                    stderr_seen = stderr_count
                    last_progress_at = now
                if no_event_timeout > 0 and (now - last_progress_at) > no_event_timeout:
                    self._interrupt_turn(client, thread_id=thread_id, turn_id=turn_id)
                    result_error = "codex app-server produced no progress before timeout"
                    result_error_code = "codex_app_server_no_event_timeout"
                    should_retire = True
                    break

                server_request = client.take_server_request(timeout=0.0)
                if server_request is not None:
                    self._handle_server_request(client, server_request)
                    last_progress_at = time.monotonic()
                    continue

                event = client.take_notification(timeout=self.notification_poll_seconds)
                if event is None:
                    continue
                last_progress_at = time.monotonic()

                if len(events) < self.max_event_audit:
                    events.append(event)
                if on_event is not None:
                    on_event(event)
                preview = app_server_event_preview(event)
                if preview:
                    preview_lines.append(preview)

                method = str(event.get("method") or "")
                params = event.get("params") or {}
                if not isinstance(params, dict):
                    params = {}
                item = params.get("item") or {}
                if not isinstance(item, dict):
                    item = {}

                if method == "item/started" and str(item.get("type") or "") == "agentMessage":
                    item_id = str(item.get("id") or "")
                    if item_id:
                        agent_message_phases[item_id] = str(item.get("phase") or "")
                        initial_text = str(item.get("text") or "")
                        if initial_text:
                            agent_message_buffers[item_id] = [initial_text]
                        else:
                            agent_message_buffers.setdefault(item_id, [])
                elif method == "item/agentMessage/delta":
                    item_id = str(params.get("itemId") or "")
                    delta = str(params.get("delta") or "")
                    if item_id and delta:
                        agent_message_buffers.setdefault(item_id, []).append(delta)
                        phase = agent_message_phases.get(item_id, "")
                        buffered_text = "".join(agent_message_buffers.get(item_id, []))
                        if phase == "final_answer":
                            final_answer_text = buffered_text
                        elif buffered_text:
                            final_text = buffered_text

                if method == "thread/tokenUsage/updated":
                    token_usage = params.get("tokenUsage") or {}
                    if isinstance(token_usage, dict):
                        total = token_usage.get("total") or {}
                        if isinstance(total, dict):
                            usage = dict(total)
                elif method == "item/completed":
                    item_type = str(item.get("type") or "")
                    if item_type == "agentMessage":
                        item_id = str(item.get("id") or "")
                        phase = str(item.get("phase") or agent_message_phases.get(item_id, "") or "")
                        text = str(item.get("text") or "")
                        if not text and item_id:
                            text = "".join(agent_message_buffers.get(item_id, []))
                        if text:
                            final_text = text
                            if phase == "final_answer":
                                final_answer_text = text
                elif method == "turn/completed":
                    turn = params.get("turn") or {}
                    turn_completed = True
                    status = str(turn.get("status") or "completed") if isinstance(turn, dict) else "completed"
                    if status != "completed":
                        error = turn.get("error") if isinstance(turn, dict) else None
                        result_error = str(error or f"codex app-server turn status={status}")
                        result_error_code = "codex_app_server_turn_failed"
                    break
            if not turn_completed and not result_error_code:
                self._interrupt_turn(client, thread_id=thread_id, turn_id=turn_id)
                result_error = f"codex app-server turn timed out after {self.turn_timeout_seconds}s"
                result_error_code = "codex_app_server_turn_timeout"
                should_retire = True

            stderr_text = "\n".join(client.stderr_tail(40))
            chosen_text = final_answer_text or final_text
            if not result_error_code and not chosen_text.strip():
                result_error = "codex app-server completed without final text"
                result_error_code = "codex_app_server_empty_output"

            if not result_error_code:
                completion_state = CompletionState.COMPLETE
            elif result_error_code in _PARTIAL_ERROR_CODES:
                completion_state = CompletionState.PARTIAL
            else:
                completion_state = CompletionState.FAILED

            return CodexAppServerResult(
                ok=completion_state == CompletionState.COMPLETE,
                final_text=chosen_text,
                error=result_error,
                error_code=result_error_code,
                command=spawn_command,
                stdout="\n".join(preview_lines),
                stderr=stderr_text,
                thread_id=thread_id,
                turn_id=turn_id,
                duration_seconds=time.monotonic() - started,
                events=events,
                should_retire=should_retire,
                usage=usage,
                completion_state=completion_state,
            )
        finally:
            try:
                client.close()
            except Exception:
                pass

    def _handle_server_request(self, client: Any, request: dict[str, object]) -> None:
        method = str(request.get("method") or "")
        request_id = request.get("id")
        if method in _APPROVAL_METHODS:
            client.respond(request_id, {"decision": "decline"})
            return
        if method == "mcpServer/elicitation/request":
            client.respond(request_id, {"action": "decline", "content": None, "_meta": None})
            return
        client.respond_error(request_id, -32601, f"Unsupported method: {method}")

    def _interrupt_turn(self, client: Any, *, thread_id: str, turn_id: str) -> None:
        if not thread_id or not turn_id:
            return
        try:
            client.request(
                "turn/interrupt",
                {"threadId": thread_id, "turnId": turn_id},
                timeout=5.0,
            )
        except Exception:
            return


def _extract_thread_id(result: dict[str, object]) -> str:
    thread = result.get("thread") or {}
    if isinstance(thread, dict):
        thread_id = str(thread.get("id") or thread.get("sessionId") or "")
        if thread_id:
            return thread_id
    return str(result.get("threadId") or result.get("sessionId") or "")


def _extract_turn_id(result: dict[str, object]) -> str:
    turn = result.get("turn") or {}
    if isinstance(turn, dict):
        return str(turn.get("id") or "")
    return str(result.get("turnId") or "")


def _unwrap_shell_command(command: str) -> str:
    text = " ".join(command.replace("\\n", " ").split())
    match = re.match(r"^(?:/bin/)?(?:zsh|bash) -lc \"(.*)\"$", text)
    if not match:
        return text
    return match.group(1)


def _compact_text(text: str, *, max_chars: int) -> str:
    compact = " ".join(text.split())
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 1].rstrip() + "..."


def _build_app_server_command(
    *,
    command: str,
    sandbox_mode: str,
    disable_node_repl: bool,
    disable_analytics: bool,
    disable_memories: bool,
    disable_apps_feature: bool,
    disable_plugins_feature: bool,
    disable_computer_use_feature: bool,
    reasoning_effort: str,
    emit_node_repl_flag: bool = True,
) -> list[str]:
    cli = [command, "app-server", "-c", f'sandbox_mode="{sandbox_mode}"']
    if disable_analytics:
        cli.extend(["-c", "analytics.enabled=false"])
    if disable_apps_feature:
        cli.extend(["--disable", "apps"])
    if disable_plugins_feature:
        cli.extend(["--disable", "plugins"])
    if disable_computer_use_feature:
        cli.extend(["--disable", "computer_use"])
    normalized_effort = reasoning_effort.strip()
    if normalized_effort:
        cli.extend(["-c", f'model_reasoning_effort="{normalized_effort}"'])
    if disable_node_repl and emit_node_repl_flag:
        cli.extend(["-c", "mcp_servers.node_repl.enabled=false"])
    if disable_memories:
        cli.extend(["--disable", "memories"])
    return cli
