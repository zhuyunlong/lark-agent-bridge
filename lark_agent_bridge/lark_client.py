"""Small wrapper around lark-cli."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
from typing import Callable, Iterator

from .models import BridgeConfig, LarkEvent


class EventConsumerError(RuntimeError):
    """Raised when the lark event consumer cannot start or exits unexpectedly."""


@dataclass(slots=True)
class CommandResult:
    command: list[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""
    dry_run: bool = False
    cwd: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "command": self.command,
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "dry_run": self.dry_run,
            "cwd": self.cwd,
        }


@dataclass(slots=True)
class _ConsumerRun:
    command: list[str]
    process: subprocess.Popen[str]
    ready_event: threading.Event
    stderr_thread: threading.Thread
    stderr_tail: list[str]
    ready_line: str = ""
    exit_reason: str = ""
    last_error: str = ""


StatusCallback = Callable[[dict[str, object]], None]

_READY_MARKER_RE = re.compile(r"^\[event\]\s+ready\s+event_key=(?P<event_key>\S+)")
_EXIT_REASON_RE = re.compile(r"\(reason:\s*(?P<reason>[^)]+)\)")


class LarkClient:
    def __init__(self, config: BridgeConfig) -> None:
        self.config = config

    def check_environment(self) -> dict[str, object]:
        path = shutil.which("lark-cli")
        result: dict[str, object] = {"lark_cli_path": path, "available": bool(path)}
        if not path:
            result["message"] = "lark-cli not found on PATH"
            return result
        result["version"] = self._run(["lark-cli", "--version"], timeout=10).to_dict()
        result["auth_status"] = _summarize_auth_status(self._run(["lark-cli", "auth", "status"], timeout=10))
        return result

    def fetch_message(self, message_id: str) -> CommandResult:
        return self._run_or_plan(
            ["lark-cli", "im", "+messages-mget", "--as", "bot", "--message-ids", message_id, "--format", "json"]
        )

    def download_resource(
        self,
        *,
        message_id: str,
        file_key: str,
        resource_type: str,
        output: str | Path,
    ) -> CommandResult:
        output_path = Path(output)
        output_cwd: Path | None = None
        output_arg = str(output_path)
        if output_path.is_absolute():
            output_cwd = output_path.parent
            output_arg = f"./{output_path.name}"
        return self._run_or_plan(
            [
                "lark-cli",
                "im",
                "+messages-resources-download",
                "--as",
                "bot",
                "--message-id",
                message_id,
                "--file-key",
                file_key,
                "--type",
                resource_type,
                "--output",
                output_arg,
            ],
            cwd=output_cwd,
        )

    def download_drive_folder(self, *, folder_token: str, output_dir: str | Path) -> CommandResult:
        output_path = Path(output_dir)
        output_cwd: Path | None = None
        local_dir_arg = str(output_path)
        if output_path.is_absolute():
            output_cwd = output_path.parent
            local_dir_arg = f"./{output_path.name}"
        return self._run_or_plan(
            [
                "lark-cli",
                "drive",
                "+pull",
                "--as",
                "bot",
                "--folder-token",
                folder_token,
                "--local-dir",
                local_dir_arg,
                "--if-exists",
                "smart",
                "--on-duplicate-remote",
                "rename",
            ],
            cwd=output_cwd,
        )

    def reply(self, message_id: str, text: str, *, markdown: bool = False) -> CommandResult:
        command = [
            "lark-cli",
            "im",
            "+messages-reply",
            "--as",
            "bot",
            "--message-id",
            message_id,
            "--text",
            text,
        ]
        if markdown:
            command.append("--markdown")
        if self.config.lark.reply_in_thread:
            command.append("--reply-in-thread")
        return self._run_or_plan(command)

    def reply_card(self, message_id: str, card_json: str) -> CommandResult:
        """Reply to a message with an interactive card."""
        command = [
            "lark-cli",
            "im",
            "+messages-reply",
            "--as",
            "bot",
            "--message-id",
            message_id,
            "--msg-type",
            "interactive",
            "--content",
            card_json,
        ]
        if self.config.lark.reply_in_thread:
            command.append("--reply-in-thread")
        return self._run_or_plan(command)

    def update_card(self, message_id: str, card_json: str) -> CommandResult:
        """Update an existing interactive card message."""
        data = json.dumps(
            {"msg_type": "interactive", "content": card_json},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        command = [
            "lark-cli",
            "api",
            "PATCH",
            f"/open-apis/im/v1/messages/{message_id}",
            "--as",
            "bot",
            "--data",
            data,
        ]
        return self._run_or_plan(command)

    def send_response(self, event: LarkEvent, text: str, *, markdown: bool = False) -> CommandResult:
        if event.chat_type == "group":
            payload = text
            if self.config.lark.mention_sender_in_group and event.sender_id:
                payload = f'<at user_id="{event.sender_id}"></at> {text}'
            return self._send_message(chat_id=event.chat_id, text=payload, markdown=markdown)

        if event.chat_type == "p2p":
            if event.sender_id:
                return self._send_message(user_id=event.sender_id, text=text, markdown=markdown)
            return self._send_message(chat_id=event.chat_id, text=text, markdown=markdown)

        return CommandResult(
            command=[],
            returncode=2,
            stderr=f"unsupported chat type: {event.chat_type}",
        )

    def send_card_response(self, event: LarkEvent, card_json: str) -> CommandResult:
        """Send an interactive card to the event source (group or p2p)."""
        if event.chat_type == "group":
            return self._send_card(chat_id=event.chat_id, card_json=card_json)

        if event.chat_type == "p2p":
            if event.sender_id:
                return self._send_card(user_id=event.sender_id, card_json=card_json)
            return self._send_card(chat_id=event.chat_id, card_json=card_json)

        return CommandResult(
            command=[],
            returncode=2,
            stderr=f"unsupported chat type: {event.chat_type}",
        )

    def send_file_response(self, event: LarkEvent, path: str | Path) -> CommandResult:
        if event.chat_type == "group":
            return self._send_file(chat_id=event.chat_id, path=path)

        if event.chat_type == "p2p":
            if event.sender_id:
                return self._send_file(user_id=event.sender_id, path=path)
            return self._send_file(chat_id=event.chat_id, path=path)

        return CommandResult(
            command=[],
            returncode=2,
            stderr=f"unsupported chat type: {event.chat_type}",
        )

    def create_doc(self, *, content: str, parent_token: str = "") -> CommandResult:
        command = [
            "lark-cli",
            "docs",
            "+create",
            "--api-version",
            "v2",
            "--as",
            "bot",
            "--content",
            content,
        ]
        if parent_token.strip():
            command.extend(["--parent-token", parent_token.strip()])
        return self._run_or_plan(command)

    def upload_drive_file(self, *, path: str | Path, folder_token: str = "", name: str = "") -> CommandResult:
        file_path = Path(path).expanduser().resolve()
        command = [
            "lark-cli",
            "drive",
            "+upload",
            "--as",
            "bot",
            "--file",
            str(file_path),
        ]
        if folder_token.strip():
            command.extend(["--folder-token", folder_token.strip()])
        if name.strip():
            command.extend(["--name", name.strip()])
        return self._run_or_plan(command)

    def upsert_base_record(
        self,
        *,
        base_token: str,
        table_id: str,
        fields: dict[str, object],
        record_id: str = "",
    ) -> CommandResult:
        command = [
            "lark-cli",
            "base",
            "+record-upsert",
            "--as",
            "bot",
            "--base-token",
            base_token,
            "--table-id",
            table_id,
            "--json",
            json.dumps(fields, ensure_ascii=False, separators=(",", ":")),
        ]
        if record_id.strip():
            command.extend(["--record-id", record_id.strip()])
        return self._run_or_plan(command)

    def consume_payloads(self, *, status_callback: StatusCallback | None = None) -> Iterator[dict[str, object]]:
        if self.config.dry_run:
            return iter(())
        return self._consume_events_with_restart(status_callback=status_callback)

    def consume_events(self, *, status_callback: StatusCallback | None = None) -> Iterator[LarkEvent]:
        return (LarkEvent.from_dict(payload) for payload in self.consume_payloads(status_callback=status_callback))

    def _consume_events_with_restart(
        self,
        *,
        status_callback: StatusCallback | None = None,
    ) -> Iterator[dict[str, object]]:
        restart_count = 0
        while True:
            run = self._start_event_consumer(status_callback=status_callback, restart_count=restart_count)
            if not self._wait_for_ready(run, status_callback=status_callback, restart_count=restart_count):
                error = self._consumer_startup_error(run)
                self._emit_consumer_status(
                    status_callback,
                    "event_consumer_startup_failed",
                    error,
                    run=run,
                    restart_count=restart_count,
                    ready=False,
                )
                self._terminate_consumer(run.process)
                if self._can_restart(restart_count):
                    self._sleep_before_restart(status_callback, run=run, restart_count=restart_count, reason=error)
                    restart_count += 1
                    continue
                raise EventConsumerError(error)

            event_count = 0
            assert run.process.stdout is not None
            try:
                for line in run.process.stdout:
                    line = line.strip()
                    if not line:
                        continue
                    event_count += 1
                    parsed = json.loads(line)
                    if isinstance(parsed, dict):
                        yield parsed
            finally:
                self._terminate_consumer(run.process)

            returncode = run.process.wait()
            run.stderr_thread.join(timeout=0.2)
            self._emit_consumer_status(
                status_callback,
                "event_consumer_exited",
                self._consumer_exit_message(run, returncode, event_count),
                run=run,
                restart_count=restart_count,
                returncode=returncode,
                exit_reason=run.exit_reason,
                events=event_count,
            )
            if returncode == 0:
                return
            error = self._consumer_exit_error(run, returncode)
            if self._can_restart(restart_count):
                self._sleep_before_restart(status_callback, run=run, restart_count=restart_count, reason=error)
                restart_count += 1
                continue
            raise EventConsumerError(error)

    def _start_event_consumer(
        self,
        *,
        status_callback: StatusCallback | None,
        restart_count: int,
    ) -> _ConsumerRun:
        event_key = self.config.event_consumer.event_key
        command = ["lark-cli", "event", "consume", event_key, "--as", "bot"]
        self._emit_consumer_status(
            status_callback,
            "event_consumer_starting",
            f"starting lark event consumer for {event_key}",
            event_key=event_key,
            command=command,
            restart_count=restart_count,
        )
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        run = _ConsumerRun(
            command=command,
            process=process,
            ready_event=threading.Event(),
            stderr_thread=threading.Thread(),
            stderr_tail=[],
        )
        thread = threading.Thread(
            target=self._read_consumer_stderr,
            args=(run, status_callback, restart_count),
            name=f"lark-event-stderr-{event_key}",
            daemon=True,
        )
        run.stderr_thread = thread
        thread.start()
        return run

    def _read_consumer_stderr(
        self,
        run: _ConsumerRun,
        status_callback: StatusCallback | None,
        restart_count: int,
    ) -> None:
        if run.process.stderr is None:
            return
        event_key = self.config.event_consumer.event_key
        for raw_line in run.process.stderr:
            line = raw_line.strip()
            if not line:
                continue
            run.stderr_tail.append(line)
            del run.stderr_tail[:-20]
            ready_match = _READY_MARKER_RE.search(line)
            if ready_match:
                run.ready_line = line
                run.ready_event.set()
                self._emit_consumer_status(
                    status_callback,
                    "event_consumer_ready",
                    f"lark event consumer ready for {ready_match.group('event_key')}",
                    run=run,
                    restart_count=restart_count,
                    event_key=ready_match.group("event_key"),
                    ready=True,
                )
                continue
            exit_match = _EXIT_REASON_RE.search(line)
            if exit_match:
                run.exit_reason = exit_match.group("reason").strip()
                continue
            if line.lower().startswith("error:"):
                run.last_error = line
                self._emit_consumer_status(
                    status_callback,
                    "event_consumer_stderr_error",
                    line,
                    run=run,
                    restart_count=restart_count,
                    event_key=event_key,
                )

    def _wait_for_ready(
        self,
        run: _ConsumerRun,
        *,
        status_callback: StatusCallback | None,
        restart_count: int,
    ) -> bool:
        timeout = max(0.0, float(self.config.event_consumer.ready_timeout_seconds))
        ready = run.ready_event.wait(timeout)
        if ready:
            return True
        if run.process.poll() is None:
            self._emit_consumer_status(
                status_callback,
                "event_consumer_ready_timeout",
                f"lark event consumer did not emit ready marker within {timeout:g}s",
                run=run,
                restart_count=restart_count,
                ready=False,
            )
        return False

    def _can_restart(self, restart_count: int) -> bool:
        options = self.config.event_consumer
        if not options.restart_on_failure:
            return False
        return options.max_restarts <= 0 or restart_count < options.max_restarts

    def _sleep_before_restart(
        self,
        status_callback: StatusCallback | None,
        *,
        run: _ConsumerRun,
        restart_count: int,
        reason: str,
    ) -> None:
        options = self.config.event_consumer
        delay = min(
            float(options.restart_initial_delay_seconds) * (2**restart_count),
            float(options.restart_max_delay_seconds),
        )
        delay = max(0.0, delay)
        self._emit_consumer_status(
            status_callback,
            "event_consumer_restarting",
            f"restarting lark event consumer after {delay:g}s: {reason}",
            run=run,
            restart_count=restart_count,
            restart_delay_seconds=delay,
            reason=reason,
        )
        if delay > 0:
            time.sleep(delay)

    def _consumer_startup_error(self, run: _ConsumerRun) -> str:
        if run.last_error:
            return run.last_error
        if run.stderr_tail:
            return run.stderr_tail[-1]
        return "lark event consumer exited before ready marker"

    def _consumer_exit_message(self, run: _ConsumerRun, returncode: int, event_count: int) -> str:
        reason = f" reason={run.exit_reason}" if run.exit_reason else ""
        return f"lark event consumer exited returncode={returncode}{reason} events={event_count}"

    def _consumer_exit_error(self, run: _ConsumerRun, returncode: int) -> str:
        if run.last_error:
            return run.last_error
        if run.stderr_tail:
            return run.stderr_tail[-1]
        return f"lark event consumer exited unexpectedly with returncode {returncode}"

    def _terminate_consumer(self, process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            process.terminate()
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            return

    def _emit_consumer_status(
        self,
        status_callback: StatusCallback | None,
        stage: str,
        message: str,
        *,
        run: _ConsumerRun | None = None,
        **details: object,
    ) -> None:
        if status_callback is None:
            return
        payload: dict[str, object] = {
            "type": "event_consumer",
            "stage": stage,
            "message": message,
        }
        payload.update(details)
        payload.setdefault("event_key", self.config.event_consumer.event_key)
        if run is not None:
            payload.setdefault("command", run.command)
            payload.setdefault("process_id", getattr(run.process, "pid", None))
            if run.stderr_tail:
                payload.setdefault("stderr_tail", list(run.stderr_tail))
        status_callback(payload)

    def _run_or_plan(self, command: list[str], *, timeout: int = 60, cwd: Path | None = None) -> CommandResult:
        if self.config.dry_run:
            return CommandResult(command=command, returncode=0, dry_run=True, cwd=str(cwd or ""))
        return self._run(command, timeout=timeout, cwd=cwd)

    def _send_message(
        self,
        *,
        text: str,
        chat_id: str | None = None,
        user_id: str | None = None,
        markdown: bool = False,
    ) -> CommandResult:
        command = ["lark-cli", "im", "+messages-send", "--as", "bot"]
        if chat_id:
            command.extend(["--chat-id", chat_id])
        elif user_id:
            command.extend(["--user-id", user_id])
        else:
            return CommandResult(command=command, returncode=2, stderr="chat_id or user_id is required")
        command.extend(["--markdown" if markdown else "--text", text])
        return self._run_or_plan(command)

    def _send_file(
        self,
        *,
        path: str | Path,
        chat_id: str | None = None,
        user_id: str | None = None,
    ) -> CommandResult:
        file_path = Path(path).expanduser().resolve()
        if not file_path.is_file():
            return CommandResult(command=[], returncode=2, stderr=f"file not found: {file_path}")
        command = ["lark-cli", "im", "+messages-send", "--as", "bot"]
        if chat_id:
            command.extend(["--chat-id", chat_id])
        elif user_id:
            command.extend(["--user-id", user_id])
        else:
            return CommandResult(command=command, returncode=2, stderr="chat_id or user_id is required")
        command.extend(["--file", f"./{file_path.name}"])
        return self._run_or_plan(command, cwd=file_path.parent)

    def _send_card(
        self,
        *,
        card_json: str,
        chat_id: str | None = None,
        user_id: str | None = None,
    ) -> CommandResult:
        command = ["lark-cli", "im", "+messages-send", "--as", "bot"]
        if chat_id:
            command.extend(["--chat-id", chat_id])
        elif user_id:
            command.extend(["--user-id", user_id])
        else:
            return CommandResult(command=command, returncode=2, stderr="chat_id or user_id is required")
        command.extend(["--msg-type", "interactive", "--content", card_json])
        return self._run_or_plan(command)

    def _run(self, command: list[str], *, timeout: int, cwd: Path | None = None) -> CommandResult:
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                cwd=str(cwd) if cwd else None,
            )
        except FileNotFoundError as exc:
            return CommandResult(command=command, returncode=127, stderr=str(exc), cwd=str(cwd or ""))
        except subprocess.TimeoutExpired as exc:
            return CommandResult(
                command=command,
                returncode=124,
                stdout=exc.stdout or "",
                stderr=exc.stderr or "",
                cwd=str(cwd or ""),
            )
        return CommandResult(
            command=command,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            cwd=str(cwd or ""),
        )


def _summarize_auth_status(result: CommandResult) -> dict[str, object]:
    summary: dict[str, object] = {
        "command": result.command,
        "returncode": result.returncode,
        "dry_run": result.dry_run,
    }
    if result.returncode != 0:
        summary["stderr"] = result.stderr[:500]
        return summary
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        summary["stdout"] = result.stdout[:500]
        return summary
    for key in ("identity", "defaultAs", "tokenStatus", "expiresAt", "refreshExpiresAt", "userOpenId", "userName"):
        if key in payload:
            summary[key] = payload[key]
    return summary
