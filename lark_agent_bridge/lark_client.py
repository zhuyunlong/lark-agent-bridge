"""Small wrapper around lark-cli."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import shutil
import subprocess
from typing import Iterator

from .models import BridgeConfig, LarkEvent


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
                str(output),
            ]
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

    def consume_events(self) -> Iterator[LarkEvent]:
        if self.config.dry_run:
            return iter(())
        process = subprocess.Popen(
            ["lark-cli", "event", "consume", "im.message.receive_v1", "--as", "bot"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if process.stdout is None:
            return iter(())
        return self._iter_events(process)

    def _iter_events(self, process: subprocess.Popen[str]) -> Iterator[LarkEvent]:
        assert process.stdout is not None
        for line in process.stdout:
            line = line.strip()
            if not line:
                continue
            yield LarkEvent.from_dict(json.loads(line))

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
