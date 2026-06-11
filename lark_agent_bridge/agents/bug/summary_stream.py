from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from . import _shared


class _SummaryStreamMixin:
    """Agent 总结流式执行：子进程流式读取、stream 进度与工具调用预览（与 AgentSummaryMixin 共享 self 状态）。"""

    def _run_bug_agent_summary_streaming_process(
        self,
        *,
        command: list[str],
        provider: str,
        progress_callback: Callable[[dict[str, object]], None],
        timeout: int,
        bridge_session_id: str = "",
        debug_log_path: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        kwargs: dict[str, object] = {
            "cwd": self._working_dir(),
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
        }
        if sys.platform != "win32":
            kwargs["start_new_session"] = True
        process = subprocess.Popen(command, **kwargs)
        if self.process_watchdog is not None:
            self.process_watchdog.track(
                process.pid,
                f"bug-agent-summary-{provider or 'agent'}",
                max_idle_seconds=timeout,
                session_id=bridge_session_id,
            )
        started = time.monotonic()
        last_activity = started
        last_heartbeat = started
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        stdout_partial = b""
        stderr_partial = b""

        def _coerce_chunk(chunk: bytes | str | None) -> bytes:
            if chunk is None:
                return b""
            if isinstance(chunk, bytes):
                return chunk
            return chunk.encode("utf-8", errors="replace")

        def _append_chunk(
            chunk: bytes | str | None,
            *,
            partial: bytes,
            sink: list[str],
        ) -> bytes:
            data = partial + _coerce_chunk(chunk)
            while True:
                newline = data.find(b"\n")
                if newline < 0:
                    break
                sink.append(data[: newline + 1].decode("utf-8", errors="replace"))
                data = data[newline + 1 :]
            return data

        def _flush_partial(partial: bytes, sink: list[str]) -> bytes:
            if partial:
                sink.append(partial.decode("utf-8", errors="replace"))
            return b""

        try:
            if process.stdout is None or process.stderr is None:
                raise RuntimeError("subprocess streams not available (stdout/stderr must be PIPE)")
            streams = [process.stdout, process.stderr]
            while True:
                now = time.monotonic()
                if timeout and now - last_activity > timeout:
                    _safe_terminate(process.pid)
                    try:
                        stdout, stderr = process.communicate(timeout=2)
                    except subprocess.TimeoutExpired:
                        _safe_terminate(process.pid, sig=signal.SIGKILL)
                        stdout, stderr = process.communicate()
                    if stdout:
                        stdout_partial = _append_chunk(stdout, partial=stdout_partial, sink=stdout_parts)
                    if stderr:
                        stderr_partial = _append_chunk(stderr, partial=stderr_partial, sink=stderr_parts)
                    stdout_partial = _flush_partial(stdout_partial, stdout_parts)
                    stderr_partial = _flush_partial(stderr_partial, stderr_parts)
                    _write_subprocess_debug_log(
                        debug_log_path,
                        name=f"bug-agent-summary-{provider or 'agent'}",
                        command=command,
                        cwd=self._working_dir(),
                        timeout=timeout,
                        returncode=process.returncode,
                        stdout="".join(stdout_parts),
                        stderr="".join(stderr_parts),
                        error="TimeoutExpired",
                    )
                    raise subprocess.TimeoutExpired(
                        command,
                        timeout,
                        output="".join(stdout_parts),
                        stderr="".join(stderr_parts),
                    )

                if streams:
                    readable, _, _ = select.select(streams, [], [], 1.0)
                else:
                    if process.poll() is not None:
                        break
                    time.sleep(1.0)
                    readable = []
                if readable:
                    for stream in readable:
                        reader = getattr(stream, "read1", None)
                        if callable(reader):
                            chunk = reader(4096)
                        else:
                            chunk = stream.read(4096)
                        if chunk:
                            if stream is process.stdout:
                                before = len(stdout_parts)
                                stdout_partial = _append_chunk(chunk, partial=stdout_partial, sink=stdout_parts)
                                for line in stdout_parts[before:]:
                                    self._emit_agent_summary_stream_progress(
                                        progress_callback,
                                        provider=provider,
                                        line=line,
                                        elapsed_seconds=time.monotonic() - started,
                                    )
                            else:
                                stderr_partial = _append_chunk(chunk, partial=stderr_partial, sink=stderr_parts)
                            last_activity = time.monotonic()
                            if self.process_watchdog is not None:
                                self.process_watchdog.record_activity(process.pid)
                        elif stream in streams:
                            if stream is process.stdout:
                                stdout_partial = _flush_partial(stdout_partial, stdout_parts)
                            else:
                                stderr_partial = _flush_partial(stderr_partial, stderr_parts)
                            streams.remove(stream)
                elif process.poll() is not None:
                    break
                if process.poll() is not None and not streams:
                    break

                now = time.monotonic()
                if now - last_heartbeat >= 15:
                    last_heartbeat = now
                    idle_seconds = int(now - last_activity)
                    self._emit_progress(
                        progress_callback,
                        stage="bug_agent_summary_stream",
                        message=f"深度分析中，已运行 {int(now - started)} 秒，距离上次 Agent 输出 {idle_seconds} 秒",
                        provider=provider,
                        stream_preview=f"等待 Agent 输出... idle={idle_seconds}s total={int(now - started)}s",
                    )

            remaining_stdout, stderr = process.communicate(timeout=2)
            if remaining_stdout:
                stdout_partial = _append_chunk(remaining_stdout, partial=stdout_partial, sink=stdout_parts)
            if stderr:
                stderr_partial = _append_chunk(stderr, partial=stderr_partial, sink=stderr_parts)
            stdout_partial = _flush_partial(stdout_partial, stdout_parts)
            stderr_partial = _flush_partial(stderr_partial, stderr_parts)
            stdout_text = "".join(stdout_parts)
            stderr_text = "".join(stderr_parts)
            _write_subprocess_debug_log(
                debug_log_path,
                name=f"bug-agent-summary-{provider or 'agent'}",
                command=command,
                cwd=self._working_dir(),
                timeout=timeout,
                returncode=process.returncode,
                stdout=stdout_text,
                stderr=stderr_text,
            )
            return subprocess.CompletedProcess(command, process.returncode, stdout_text, stderr_text)
        finally:
            if self.process_watchdog is not None:
                self.process_watchdog.untrack(process.pid)
    def _emit_agent_summary_stream_progress(
        self,
        progress_callback: Callable[[dict[str, object]], None],
        *,
        provider: str,
        line: str,
        elapsed_seconds: float,
    ) -> None:
        preview = self._codex_stream_preview(line)
        if not preview:
            return
        self._emit_progress(
            progress_callback,
            stage="bug_agent_summary_stream",
            message=f"深度分析输出更新：{preview}",
            provider=provider,
            elapsed_seconds=round(elapsed_seconds, 1),
            stream_preview=preview,
        )
    def _codex_stream_preview(self, line: str) -> str:
        raw = line.strip()
        if not raw:
            return ""
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            return raw[:240]
        event_type = str(event.get("type") or "").strip()
        if event_type == "thread.started":
            thread_id = str(event.get("thread_id") or "").strip()
            return f"Codex 会话已创建 {thread_id}" if thread_id else "Codex 会话已创建"
        if event_type == "turn.started":
            return "Codex 已开始深度分析"
        if event_type == "turn.completed":
            usage = event.get("usage")
            if isinstance(usage, dict):
                total = usage.get("total_tokens")
                if total is None:
                    total = sum(value for value in usage.values() if isinstance(value, int))
                return f"Codex 深度分析完成，token≈{total}" if total else "Codex 深度分析完成"
            return "Codex 深度分析完成"
        if event_type in {"item.started", "item.updated", "item.completed"}:
            item = event.get("item")
            if isinstance(item, dict):
                preview = self._codex_item_preview(item, event_type=event_type)
                if preview:
                    return preview
            return "" if event_type == "item.completed" else "Codex 工具调用更新"
        return f"{event_type}: {raw[:220]}" if event_type else raw[:240]
    def _codex_item_preview(self, item: dict[str, object], *, event_type: str) -> str:
        item_type = str(item.get("type") or "").strip()
        if item_type == "command_execution":
            command = str(item.get("command") or "").strip()
            if event_type == "item.completed":
                return ""
            if command:
                return f"工具调用：执行命令 {self._compact_tool_preview(self._unwrap_shell_command(command), 200)}"
            return "工具调用：执行命令"
        if item_type in {"error", "error_message"}:
            message = str(item.get("message") or item.get("text") or item.get("error") or "").strip()
            message = self._compact_tool_preview(message, 180)
            return f"Agent 错误：{message}" if message else ""
        if item_type in {"tool_call", "function_call"}:
            name = str(
                item.get("name")
                or item.get("tool_name")
                or item.get("function_name")
                or item.get("recipient_name")
                or ""
            ).strip()
            args = item.get("arguments")
            if args is None:
                args = item.get("input")
            if args is None:
                args = item.get("parameters")
            args_text = self._compact_tool_preview(args, 120)
            label = "工具调用" if event_type == "item.started" else "工具调用更新"
            if name and args_text:
                return f"{label}：{name} {args_text}"
            if name:
                return f"{label}：{name}"
            return label
        text = str(item.get("text") or item.get("name") or "").strip()
        text = re.sub(r"\s+", " ", text)
        if text:
            return f"{item_type or 'item'}: {text[:240]}"
        if event_type == "item.completed":
            return ""
        return f"开始处理 {item_type}" if item_type else ""
    def _unwrap_shell_command(self, command: str) -> str:
        text = " ".join(command.replace("\\n", " ").split())
        try:
            parts = shlex.split(text)
        except ValueError:
            parts = []
        if len(parts) >= 3 and parts[0] in {"/bin/zsh", "/bin/bash", "zsh", "bash"} and parts[1] == "-lc":
            return " ".join(parts[2:]).strip()
        return text
    def _compact_tool_preview(self, value: object, max_chars: int) -> str:
        if isinstance(value, (dict, list)):
            text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        else:
            text = str(value or "")
        text = " ".join(text.replace("\\n", " ").split())
        if len(text) > max_chars:
            return text[: max_chars - 1].rstrip() + "…"
        return text
