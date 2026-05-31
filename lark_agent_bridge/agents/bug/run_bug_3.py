from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from . import _shared


class _RunBug3Mixin:
    def _run_bug_agent_summary_once(
        self,
        *,
        invocation: dict[str, object],
        output_path: Path,
        progress_callback: Callable[[dict[str, object]], None] | None,
        timeout: int,
        bridge_session_id: str = "",
    ) -> dict[str, object]:
        command = list(invocation["command"])
        provider = str(invocation["provider"] or "")
        model = str(invocation.get("model") or "")
        session_id = str(invocation.get("session_id") or "")
        resumed = bool(invocation.get("resumed"))
        started = time.monotonic()
        previous_output_mtime = self._path_mtime(output_path)
        prompt_file, context_file = self._write_bug_agent_summary_audit(invocation, output_path)
        debug_log_path = self._subprocess_debug_log_path(
            output_path.parent,
            output_path.with_suffix("").name,
            bridge_session_id=bridge_session_id,
        )
        self._emit_progress(
            progress_callback,
            stage="bug_agent_summary",
            message=(
                "深度分析中：调用本地 Agent 继续整理最终结论"
                if resumed and provider == "codex"
                else "深度分析中：调用本地 Agent 整理最终结论"
                if provider == "codex"
                else "调用本地 Agent 继续整理最终结论"
                if resumed
                else "调用本地 Agent 整理最终结论"
            ),
            output_path=str(output_path),
            provider=provider,
            resumed=resumed,
            provider_session_id=session_id,
            timeout_seconds=timeout,
        )
        try:
            if provider == "codex" and progress_callback is not None:
                completed = self._run_bug_agent_summary_streaming_process(
                    command=command,
                    provider=provider,
                    progress_callback=progress_callback,
                    timeout=timeout,
                    bridge_session_id=bridge_session_id,
                    debug_log_path=debug_log_path,
                )
            else:
                completed = _run_tracked_process(
                    command,
                    watchdog=self.process_watchdog,
                    name=f"bug-agent-summary-{provider or 'agent'}",
                    cwd=self._working_dir(),
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                    session_id=bridge_session_id,
                    debug_log_path=debug_log_path,
                )
        except subprocess.TimeoutExpired as exc:
            fresh_message = self._read_fresh_agent_summary_message(output_path, previous_mtime=previous_output_mtime)
            if fresh_message:
                self._emit_progress(
                    progress_callback,
                    stage="bug_agent_summary_completed",
                    message=f"本地 Agent 静默超过 {timeout} 秒但已写出总结，直接采用已生成结果",
                    provider=provider,
                    output_path=str(output_path),
                    resumed=resumed,
                    provider_session_id=session_id,
                )
                usage, usage_scope = self._extract_bug_agent_usage(
                    provider,
                    _coerce_process_text(getattr(exc, "output", None)),
                    _coerce_process_text(getattr(exc, "stderr", None)),
                )
                return {
                    "message": fresh_message,
                    "command": command,
                    "error": "",
                    "provider": provider,
                    "model": model,
                    "session_id": session_id,
                    "resumed": resumed,
                    "duration_seconds": time.monotonic() - started,
                    "usage": usage,
                    "usage_scope": usage_scope,
                    "prompt_file": str(prompt_file) if prompt_file is not None else "",
                    "context_file": str(context_file) if context_file is not None else "",
                    "timeout_seconds": timeout,
                    "timed_out": True,
                }
            self._emit_progress(
                progress_callback,
                stage="bug_agent_summary_timeout",
                message=f"本地 Agent 静默超过 {timeout} 秒，准备切换轻量总结",
                provider=provider,
                resumed=resumed,
                provider_session_id=session_id,
            )
            return {
                "message": "",
                "command": command,
                "error": "agent_summary_timeout",
                "provider": provider,
                "model": model,
                "session_id": session_id,
                "resumed": resumed,
                "duration_seconds": time.monotonic() - started,
                "usage": {},
                "usage_scope": "",
                "prompt_file": str(prompt_file) if prompt_file is not None else "",
                "context_file": str(context_file) if context_file is not None else "",
                "timeout_seconds": timeout,
                "timed_out": True,
                "stderr": str(exc),
            }
        except OSError as exc:
            self._emit_progress(
                progress_callback,
                stage="bug_agent_summary_failed",
                message=f"本地 Agent 总结失败，回退到脚本摘要: {exc}",
                provider=provider,
                resumed=resumed,
                provider_session_id=session_id,
            )
            return {
                "message": "",
                "command": command,
                "error": str(exc),
                "provider": provider,
                "model": model,
                "session_id": session_id,
                "resumed": resumed,
                "duration_seconds": time.monotonic() - started,
                "usage": {},
                "usage_scope": "",
                "prompt_file": str(prompt_file) if prompt_file is not None else "",
                "context_file": str(context_file) if context_file is not None else "",
                "timeout_seconds": timeout,
            }
        if completed.returncode != 0:
            error = completed.stderr.strip() or completed.stdout.strip() or f"returncode={completed.returncode}"
            self._emit_progress(
                progress_callback,
                stage="bug_agent_summary_failed",
                message=f"本地 Agent 总结失败，回退到脚本摘要: {error}",
                provider=provider,
                resumed=resumed,
                provider_session_id=session_id,
            )
            return {
                "message": "",
                "command": command,
                "error": error,
                "provider": provider,
                "model": model,
                "session_id": session_id,
                "resumed": resumed,
                "duration_seconds": time.monotonic() - started,
                "usage": {},
                "usage_scope": "",
                "prompt_file": str(prompt_file) if prompt_file is not None else "",
                "context_file": str(context_file) if context_file is not None else "",
            }
        if output_path.exists():
            message = output_path.read_text(encoding="utf-8").strip()
        else:
            message = completed.stdout.strip()
            if message:
                try:
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    output_path.write_text(message, encoding="utf-8")
                except OSError:
                    return {
                        "message": "",
                        "command": command,
                        "error": "agent_summary_output_io_error",
                        "provider": provider,
                        "model": model,
                        "session_id": session_id,
                        "resumed": resumed,
                        "duration_seconds": time.monotonic() - started,
                        "usage": {},
                        "usage_scope": "",
                        "prompt_file": str(prompt_file) if prompt_file is not None else "",
                        "context_file": str(context_file) if context_file is not None else "",
                    }
        if not message:
            return {
                "message": "",
                "command": command,
                "error": "empty_agent_summary",
                "provider": provider,
                "model": model,
                "session_id": session_id,
                "resumed": resumed,
                "duration_seconds": time.monotonic() - started,
                "usage": {},
                "usage_scope": "",
                "prompt_file": str(prompt_file) if prompt_file is not None else "",
                "context_file": str(context_file) if context_file is not None else "",
            }
        resolved_session_id = self._extract_bug_agent_session_id(provider, completed.stdout, fallback=session_id)
        usage, usage_scope = self._extract_bug_agent_usage(provider, completed.stdout, completed.stderr)
        self._emit_progress(
            progress_callback,
            stage="bug_agent_summary_completed",
            message="本地 Agent 已整理最终结论",
            provider=provider,
            output_path=str(output_path),
            resumed=resumed,
            provider_session_id=resolved_session_id,
        )
        return {
            "message": message,
            "command": command,
            "error": "",
            "provider": provider,
            "model": model,
            "session_id": resolved_session_id,
            "resumed": resumed,
            "duration_seconds": time.monotonic() - started,
            "usage": usage,
            "usage_scope": usage_scope,
            "prompt_file": str(prompt_file) if prompt_file is not None else "",
            "context_file": str(context_file) if context_file is not None else "",
        }

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

    def _write_bug_agent_summary_audit(
        self,
        invocation: dict[str, object],
        output_path: Path,
    ) -> tuple[Path | None, Path | None]:
        prompt = str(invocation.get("prompt") or "")
        embedded_files = invocation.get("embedded_files")
        if not isinstance(embedded_files, list):
            embedded_files = []
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            prompt_file = output_path.parent / "bug_agent_summary_prompt.md"
            context_file = output_path.parent / "bug_agent_summary_context.json"
            prompt_file.write_text(prompt, encoding="utf-8")
            manifest = {
                "provider": str(invocation.get("provider") or ""),
                "resumed": bool(invocation.get("resumed")),
                "session_id": str(invocation.get("session_id") or ""),
                "output_path": str(output_path),
                "prompt_file": str(prompt_file),
                "prompt_chars": len(prompt),
                "embedded_files": self._embedded_file_manifest(embedded_files),
            }
            context_file.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            return prompt_file, context_file
        except OSError:
            return None, None

    def _extract_bug_agent_session_id(self, provider: str, output: str, *, fallback: str = "") -> str:
        if fallback.strip():
            return fallback.strip()
        if provider != "codex":
            return ""
        for line in output.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            found = self._search_bug_agent_session_id_in_payload(payload)
            if found:
                return found
        return ""

    def _search_bug_agent_session_id_in_payload(self, payload: object) -> str:
        if isinstance(payload, dict):
            for key in ("session_id", "sessionId", "conversation_id", "conversationId", "thread_id", "threadId"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            for key in ("session", "conversation", "thread"):
                nested = payload.get(key)
                if isinstance(nested, dict):
                    value = nested.get("id")
                    if isinstance(value, str) and value.strip():
                        return value.strip()
            for value in payload.values():
                found = self._search_bug_agent_session_id_in_payload(value)
                if found:
                    return found
            return ""
        if isinstance(payload, list):
            for item in payload:
                found = self._search_bug_agent_session_id_in_payload(item)
                if found:
                    return found
        return ""

    def _extract_bug_agent_usage(self, provider: str, stdout: str, stderr: str) -> tuple[dict[str, int], str]:
        usage, scope = self._extract_usage_from_json_lines(stdout)
        if usage:
            return usage, scope
        if provider == "codex":
            usage, scope = self._extract_usage_from_json_lines(stderr)
            if usage:
                return usage, scope
        return self._extract_usage_from_text("\n".join(part for part in (stdout, stderr) if part)), "cumulative"

    def _extract_usage_from_json_lines(self, text: str) -> tuple[dict[str, int], str]:
        latest: dict[str, int] = {}
        latest_delta: dict[str, int] = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            for candidate in self._iter_usage_objects_from_keys(
                payload,
                keys=("delta_usage", "deltaUsage", "usage_delta", "usageDelta"),
            ):
                parsed = self._parse_usage_object(candidate)
                if parsed:
                    latest_delta = parsed
            for candidate in self._iter_usage_objects(payload):
                parsed = self._parse_usage_object(candidate)
                if parsed:
                    latest = parsed
        if latest_delta:
            return latest_delta, "delta"
        return latest, "cumulative" if latest else ""

    def _iter_usage_objects_from_keys(self, value: object, *, keys: tuple[str, ...]) -> list[dict[str, object]]:
        found: list[dict[str, object]] = []
        if isinstance(value, dict):
            for key in keys:
                nested = value.get(key)
                if isinstance(nested, dict):
                    found.append(nested)
            for nested in value.values():
                found.extend(self._iter_usage_objects_from_keys(nested, keys=keys))
        elif isinstance(value, list):
            for item in value:
                found.extend(self._iter_usage_objects_from_keys(item, keys=keys))
        return found

    def _iter_usage_objects(self, value: object) -> list[dict[str, object]]:
        found: list[dict[str, object]] = []
        if isinstance(value, dict):
            if any(any(alias in value for alias in aliases) for aliases in _TOKEN_USAGE_KEYS.values()):
                found.append(value)
            for nested in value.values():
                found.extend(self._iter_usage_objects(nested))
        elif isinstance(value, list):
            for item in value:
                found.extend(self._iter_usage_objects(item))
        return found

    def _parse_usage_object(self, value: object) -> dict[str, int]:
        if not isinstance(value, dict):
            return {}
        return normalize_token_usage(value)

    def _extract_usage_from_text(self, text: str) -> dict[str, int]:
        patterns = {
            "input_tokens": (r"input[_ ]tokens?\s*[:=]\s*(\d+)", r"prompt[_ ]tokens?\s*[:=]\s*(\d+)"),
            "cached_input_tokens": (
                r"cached[_ ]input[_ ]tokens?\s*[:=]\s*(\d+)",
                r"cached[_ ]prompt[_ ]tokens?\s*[:=]\s*(\d+)",
            ),
            "output_tokens": (r"output[_ ]tokens?\s*[:=]\s*(\d+)", r"completion[_ ]tokens?\s*[:=]\s*(\d+)"),
            "total_tokens": (r"total[_ ]tokens?\s*[:=]\s*(\d+)",),
        }
        usage: dict[str, int] = {}
        for target_key, candidates in patterns.items():
            for pattern in candidates:
                match = re.search(pattern, text, flags=re.IGNORECASE)
                if match:
                    usage[target_key] = int(match.group(1))
                    break
        if "total_tokens" not in usage and {"input_tokens", "output_tokens"}.issubset(usage):
            usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
        return usage

    def _emit_progress(
        self,
        progress_callback: Callable[[dict[str, object]], None] | None,
        *,
        stage: str,
        message: str,
        **details: object,
    ) -> None:
        if progress_callback is None:
            return
        payload: dict[str, object] = {"stage": stage, "message": message}
        if details:
            payload["details"] = details
        progress_callback(payload)

    def _request_text(self, *, raw_text: str, prompt_text: str, bug_url: str) -> str:
        candidate = (raw_text or "").strip()
        if candidate:
            return candidate
        if bug_url:
            if prompt_text:
                return f"{bug_url} {prompt_text}".strip()
            return bug_url
        return prompt_text.strip()

    def _apply_agent_runtime_details(self, details: dict[str, object], agent_summary_result: dict[str, object]) -> None:
        if agent_summary_result["command"]:
            details["agent_summary_command"] = list(agent_summary_result["command"])
        if agent_summary_result["error"]:
            details["agent_summary_error"] = str(agent_summary_result["error"])
        if agent_summary_result["provider"]:
            provider = str(agent_summary_result["provider"])
            details["agent_summary_provider"] = provider
            details.setdefault("provider", provider)
        model = self._agent_summary_model(agent_summary_result)
        if model:
            details["agent_summary_model"] = model
        if agent_summary_result["session_id"]:
            details["agent_summary_session_id"] = str(agent_summary_result["session_id"])
        if agent_summary_result["resumed"]:
            details["agent_summary_resumed"] = True
        if agent_summary_result.get("usage_scope"):
            details["agent_summary_usage_scope"] = str(agent_summary_result["usage_scope"])
        if agent_summary_result.get("execution_backend"):
            details["agent_summary_execution_backend"] = str(agent_summary_result["execution_backend"])
        if agent_summary_result.get("backend_reason"):
            details["agent_summary_backend_reason"] = str(agent_summary_result["backend_reason"])
        if agent_summary_result.get("fallback_from"):
            details["agent_summary_fallback_from"] = str(agent_summary_result["fallback_from"])
        if agent_summary_result.get("prompt_file"):
            details["agent_summary_prompt_file"] = str(agent_summary_result["prompt_file"])
        if agent_summary_result.get("context_file"):
            details["agent_summary_context_file"] = str(agent_summary_result["context_file"])
        if agent_summary_result.get("timeout_seconds"):
            details["agent_summary_timeout_seconds"] = agent_summary_result["timeout_seconds"]
        if agent_summary_result.get("timed_out"):
            details["agent_summary_timed_out"] = True
        tool_calls = agent_summary_result.get("tool_calls")
        if isinstance(tool_calls, int):
            details["agent_summary_tool_calls"] = tool_calls
        tool_trace = agent_summary_result.get("tool_trace")
        if isinstance(tool_trace, list):
            details["agent_summary_tool_trace"] = tool_trace[:20]
        duration = agent_summary_result.get("duration_seconds")
        if isinstance(duration, (int, float)):
            details["agent_summary_duration_seconds"] = float(duration)
        usage = agent_summary_result.get("usage")
        if isinstance(usage, dict):
            normalized_usage = normalize_token_usage(usage)
            for key in ("input_tokens", "cached_input_tokens", "output_tokens", "total_tokens"):
                value = normalized_usage.get(key)
                if isinstance(value, int):
                    details[f"agent_summary_{key}"] = value

    def _append_agent_runtime_metadata(
        self,
        metadata_path: Path,
        *,
        agent_summary_result: dict[str, object],
        total_duration_seconds: float,
    ) -> None:
        provider = str(agent_summary_result.get("provider") or "").strip()
        model = self._agent_summary_model(agent_summary_result)
        usage = agent_summary_result.get("usage")
        duration = agent_summary_result.get("duration_seconds")
        session_id = str(agent_summary_result.get("session_id") or "").strip()
        resumed = bool(agent_summary_result.get("resumed"))
        usage_scope = str(agent_summary_result.get("usage_scope") or "").strip()
        execution_backend = str(agent_summary_result.get("execution_backend") or "").strip()
        backend_reason = str(agent_summary_result.get("backend_reason") or "").strip()
        fallback_from = str(agent_summary_result.get("fallback_from") or "").strip()
        if not provider and not model and not isinstance(usage, dict) and not isinstance(duration, (int, float)):
            return
        lines = ["", "## Agent 执行信息", ""]
        lines.append(f"- Agent 类型: `{provider or '未知'}`")
        if execution_backend:
            lines.append(f"- 执行后端: `{execution_backend}`")
        if backend_reason:
            lines.append(f"- 后端选择原因: `{backend_reason}`")
        if fallback_from:
            lines.append(f"- fallback_from: `{fallback_from}`")
        if model:
            lines.append(f"- Agent 模型: `{model}`")
        if session_id:
            lines.append(f"- Agent 会话ID: `{session_id}`")
        lines.append(f"- 续会话: `{'是' if resumed else '否'}`")
        if isinstance(usage, dict):
            normalized_usage = normalize_token_usage(usage)
            input_tokens = normalized_usage.get("input_tokens")
            cached_input_tokens = normalized_usage.get("cached_input_tokens")
            output_tokens = normalized_usage.get("output_tokens")
            total_tokens = normalized_usage.get("total_tokens")
            if any(
                isinstance(value, int)
                for value in (input_tokens, cached_input_tokens, output_tokens, total_tokens)
            ):
                token_label = "本轮 Agent Token" if usage_scope == "delta" else "累计 Agent Token"
                parts = [self._format_token_millions(input_tokens)]
                if isinstance(cached_input_tokens, int):
                    parts.append(self._format_token_millions(cached_input_tokens))
                parts.extend(
                    [
                        self._format_token_millions(output_tokens),
                        self._format_token_millions(total_tokens),
                    ]
                )
                lines.append(
                    f"- {token_label}: `{' / '.join(parts)}`"
                )
        if isinstance(duration, (int, float)):
            lines.append(f"- Agent 耗时: `{float(duration):.1f} 秒`")
        tool_calls = agent_summary_result.get("tool_calls")
        if isinstance(tool_calls, int):
            lines.append(f"- Agent 工具调用数: `{tool_calls}`")
        tool_trace = agent_summary_result.get("tool_trace")
        if isinstance(tool_trace, list) and tool_trace:
            trace_items: list[str] = []
            for item in tool_trace[:5]:
                if not isinstance(item, dict):
                    continue
                tool_name = str(item.get("tool") or "?")
                args = str(item.get("args") or "").replace("\n", " ")[:120]
                trace_items.append(f"{tool_name}({args})")
            if trace_items:
                lines.append(f"- Agent 工具轨迹: `{'; '.join(trace_items)}`")
        if agent_summary_result.get("timeout_seconds"):
            lines.append(f"- Agent 总结超时: `{agent_summary_result['timeout_seconds']} 秒`")
        if agent_summary_result.get("timed_out") and agent_summary_result.get("message"):
            lines.append("- 超时处理: `已采用 Agent 写出的总结文件，未跨 provider fallback`")
        if agent_summary_result.get("prompt_file"):
            lines.append(f"- Agent Prompt: `{agent_summary_result['prompt_file']}`")
        if agent_summary_result.get("context_file"):
            lines.append(f"- Agent 输入清单: `{agent_summary_result['context_file']}`")
        lines.append(f"- 总耗时: `{total_duration_seconds:.1f} 秒`")
        try:
            original = metadata_path.read_text(encoding="utf-8")
        except OSError:
            original = ""
        metadata_path.write_text(original.rstrip() + "\n" + "\n".join(lines).rstrip() + "\n", encoding="utf-8")

    def _append_source_evidence_metadata(self, metadata_path: Path, source_evidence_path: Path | None) -> None:
        if source_evidence_path is None:
            return
        try:
            evidence = source_evidence_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            evidence = f"读取失败: {exc}"
        try:
            original = metadata_path.read_text(encoding="utf-8")
        except OSError:
            original = ""
        lines = [
            "",
            "## 源码证据",
            "",
            f"- 文件: `{source_evidence_path}`",
            "",
            "```text",
            evidence[:5000],
            "```",
        ]
        metadata_path.write_text(original.rstrip() + "\n" + "\n".join(lines).rstrip() + "\n", encoding="utf-8")

    def _append_evidence_log_metadata(self, metadata_path: Path, evidence_log_bundle: dict[str, object] | None) -> None:
        if not evidence_log_bundle:
            return
        try:
            original = metadata_path.read_text(encoding="utf-8")
        except OSError:
            original = ""
        focus_logs = evidence_log_bundle.get("focus_logs")
        if isinstance(focus_logs, list) and focus_logs:
            focus_text = ", ".join(str(item) for item in focus_logs)
        else:
            focus_text = "未识别"
        lines = [
            "",
            "## 证据日志保留包",
            "",
            f"- 目录: `{evidence_log_bundle.get('bundle_dir') or ''}`",
            f"- 清单: `{evidence_log_bundle.get('manifest_path') or ''}`",
            f"- 命中日志: `{focus_text}`",
            f"- 文件数: `{evidence_log_bundle.get('file_count') or 0}`",
        ]
        metadata_path.write_text(original.rstrip() + "\n" + "\n".join(lines).rstrip() + "\n", encoding="utf-8")

    def _should_collect_source_evidence(self, *texts: str) -> bool:
        source_terms = ("源码", "源代码", "根据源码", "基于源码")
        merged = "\n".join(texts).casefold()
        return any(term.casefold() in merged for term in source_terms) or self._source_analysis_shortcut(*texts)

    def _should_prefer_lightweight_bug_summary(
        self,
        *,
        request_text: str,
        followup_text: str,
        provider_session_id: str,
    ) -> bool:
        if provider_session_id.strip():
            return False
        # Bug summaries must be produced by a file-capable agent. The lightweight
        # local chat model cannot read report/log paths and has previously turned
        # truncated HTML/CSS excerpts into false "信息不足" conclusions.
        return False

    def _annotate_html_reports(
        self,
        html_paths: list[Path],
        *,
        agent_summary_result: dict[str, object],
        total_duration_seconds: float,
    ) -> None:
        snippet = self._build_agent_runtime_html(agent_summary_result, total_duration_seconds=total_duration_seconds)
        if not snippet:
            return
        for path in html_paths:
            if not path.exists() or path.suffix.lower() != ".html":
                continue
            try:
                html_text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            updated = self._inject_runtime_html(html_text, snippet)
            if updated == html_text:
                continue
            try:
                path.write_text(updated, encoding="utf-8")
            except OSError:
                continue

    def _build_agent_runtime_html(self, agent_summary_result: dict[str, object], *, total_duration_seconds: float) -> str:
        provider = str(agent_summary_result.get("provider") or "").strip()
        model = self._agent_summary_model(agent_summary_result)
        usage = agent_summary_result.get("usage")
        duration = agent_summary_result.get("duration_seconds")
        session_id = str(agent_summary_result.get("session_id") or "").strip()
        resumed = bool(agent_summary_result.get("resumed"))
        usage_scope = str(agent_summary_result.get("usage_scope") or "").strip()
        execution_backend = str(agent_summary_result.get("execution_backend") or "").strip()
        backend_reason = str(agent_summary_result.get("backend_reason") or "").strip()
        fallback_from = str(agent_summary_result.get("fallback_from") or "").strip()
        if not provider and not model and not isinstance(usage, dict) and not isinstance(duration, (int, float)):
            return ""
        rows = [
            ("Agent 类型", provider or "未知"),
            ("续会话", "是" if resumed else "否"),
        ]
        if execution_backend:
            rows.append(("执行后端", execution_backend))
        if backend_reason:
            rows.append(("后端选择原因", backend_reason))
        if fallback_from:
            rows.append(("Fallback From", fallback_from))
        if model:
            rows.append(("Agent 模型", model))
        if session_id:
            rows.append(("Agent 会话ID", session_id))
        normalized_usage = normalize_token_usage(usage) if isinstance(usage, dict) else {}
        if normalized_usage:
            parts = [self._format_token_millions(normalized_usage.get("input_tokens"))]
            cached_input_tokens = normalized_usage.get("cached_input_tokens")
            if isinstance(cached_input_tokens, int):
                parts.append(self._format_token_millions(cached_input_tokens))
            parts.extend(
                [
                    self._format_token_millions(normalized_usage.get("output_tokens")),
                    self._format_token_millions(normalized_usage.get("total_tokens")),
                ]
            )
            rows.append(
                (
                    "本轮 Agent Token" if usage_scope == "delta" else "累计 Agent Token",
                    " / ".join(parts),
                )
            )
        if isinstance(duration, (int, float)):
            rows.append(("Agent 耗时", f"{float(duration):.1f} 秒"))
        rows.append(("总耗时", f"{total_duration_seconds:.1f} 秒"))
        items = "".join(
            "<div class=\"lagent-runtime-item\">"
            f"<div class=\"lagent-runtime-label\">{self._escape_html(label)}</div>"
            f"<div class=\"lagent-runtime-value\">{self._escape_html(value)}</div>"
            "</div>"
            for label, value in rows
        )
        style = (
            "<style>"
            ".lagent-runtime{margin:16px 0 20px;padding:16px 18px;border:1px solid #dbe2f0;border-radius:12px;"
            "background:#f8fafc;box-shadow:0 1px 3px rgba(15,23,42,.06)}"
            ".lagent-runtime h2{margin:0 0 12px;font-size:18px;color:#0f172a}"
            ".lagent-runtime-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}"
            ".lagent-runtime-item{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:12px 14px}"
            ".lagent-runtime-label{font-size:12px;color:#64748b;margin-bottom:4px}"
            ".lagent-runtime-value{font-size:16px;font-weight:600;color:#0f172a;word-break:break-word}"
            "</style>"
        )
        return (
            f"{_RUNTIME_HTML_MARKER_START}{style}"
            "<section class=\"lagent-runtime\">"
            "<h2>Agent 运行信息</h2>"
            f"<div class=\"lagent-runtime-grid\">{items}</div>"
            "</section>"
            f"{_RUNTIME_HTML_MARKER_END}"
        )

    def _agent_summary_model(self, agent_summary_result: dict[str, object]) -> str:
        model = str(agent_summary_result.get("model") or "").strip()
        if model:
            return model
        provider = str(agent_summary_result.get("provider") or "").strip().casefold()
        if provider == "direct_api":
            return str(self.config.ai_provider.primary_model or "").strip()
        if provider == "omlx":
            return str(self.config.omlx_chat.model or "").strip()
        if provider == "codex":
            return str(self.config.bug_analysis.model or "").strip()
        if provider in {"claude", "claude-code", "claude_code"}:
            return str(self.config.claude_agent.model or "").strip()
        return ""

    def _format_token_millions(self, value: object) -> str:
        if not isinstance(value, int):
            return "-"
        if value < 1_000:
            return str(value)
        if value < 1_000_000:
            return f"{value / 1_000:.2f}K"
        return f"{value / 1_000_000:.2f}M"

    def _inject_runtime_html(self, html_text: str, runtime_html: str) -> str:
        pattern = re.compile(
            rf"{re.escape(_RUNTIME_HTML_MARKER_START)}.*?{re.escape(_RUNTIME_HTML_MARKER_END)}",
            flags=re.DOTALL,
        )
        if pattern.search(html_text):
            return pattern.sub(runtime_html, html_text, count=1)
        body_match = re.search(r"<body[^>]*>", html_text, flags=re.IGNORECASE)
        if body_match:
            index = body_match.end()
            return html_text[:index] + runtime_html + html_text[index:]
        html_match = re.search(r"<html[^>]*>", html_text, flags=re.IGNORECASE)
        if html_match:
            index = html_match.end()
            return html_text[:index] + "<body>" + runtime_html + "</body>" + html_text[index:]
        return runtime_html + html_text

    def _escape_html(self, value: object) -> str:
        return (
            str(value)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

    def _write_reanalysis_source_evidence(
        self,
        *,
        plans: list["BugAnalysisPlan"],
        request_text: str,
        followup_text: str,
        output_dir: Path,
        enabled: bool,
        extra_texts: tuple[str, ...] = (),
    ) -> Path | None:
        if not enabled:
            return None
        terms = self._source_evidence_terms(
            plans=plans,
            request_text=request_text,
            followup_text=followup_text,
            extra_texts=extra_texts,
        )
        if not terms:
            return None
        evidence_path = output_dir / "bug_source_evidence.md"
        started = time.monotonic()
        budget_seconds = min(
            self._SOURCE_EVIDENCE_TOTAL_BUDGET_SECONDS,
            5.0 * max(1, min(len(terms), 5)),
        )
        deadline = started + budget_seconds

        # Use all configured repo_roots (includes Napa5 when configured), fallback to guideengine_repo
        si_opts = self.config.source_investigation
        repo_roots = [
            p.expanduser()
            for p in (si_opts.repo_roots or [self.config.guideengine_repo])
        ]
        existing_repos = [r for r in repo_roots if r.exists()]

        lines = [
            "# Bug Source Evidence",
            "",
            f"- 源码根目录: `{', '.join(str(r) for r in (existing_repos or repo_roots))}`",
            f"- 检索词: `{', '.join(terms)}`",
            f"- 检索预算: `{budget_seconds:.1f}s`",
            "",
        ]
        if not existing_repos:
            lines.append(f"源码根目录不存在，未执行源码检索: `{', '.join(str(r) for r in repo_roots)}`")
            evidence_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return evidence_path

        all_matches: list[tuple[str, int, str]] = []
        trace_lines: list[str] = []
        for repo in existing_repos:
            if time.monotonic() >= deadline:
                trace_lines.append(f"- `{repo}`: skipped, source evidence budget exhausted")
                break
            repo_started = time.monotonic()
            # Try codegraph first (semantic symbol search)
            cg_matches = self._collect_source_evidence_with_codegraph(repo=repo, terms=terms, deadline=deadline)
            if cg_matches is not None:
                repo_prefix = f"[{repo.name}] " if len(existing_repos) > 1 else ""
                all_matches.extend((repo_prefix + path, ln, text) for path, ln, text in cg_matches)
                trace_lines.append(
                    f"- `{repo}`: codegraph, {len(cg_matches)} matches, {time.monotonic() - repo_started:.2f}s"
                )
                continue
            # Fall back to ripgrep only; avoid pure Python full-repo scans on large trees.
            matches = self._collect_source_evidence(repo=repo, terms=terms, deadline=deadline)
            if len(existing_repos) > 1:
                all_matches.extend((f"[{repo.name}] {path}", ln, text) for path, ln, text in matches)
            else:
                all_matches.extend(matches)
            trace_lines.append(
                f"- `{repo}`: rg fallback, {len(matches)} matches, {time.monotonic() - repo_started:.2f}s"
            )

        if trace_lines:
            elapsed = time.monotonic() - started
            lines.extend(["## 检索路径", "", *trace_lines, f"- 总耗时: `{elapsed:.2f}s`", ""])

        if not all_matches:
            lines.append("未检索到匹配源码。")
        else:
            current_file = ""
            for path, line_no, text in all_matches[:80]:
                if path != current_file:
                    current_file = path
                    lines.extend(["", f"## {path}"])
                lines.append(f"- L{line_no}: `{text}`")
        evidence_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        return evidence_path

    def _collect_source_evidence_with_codegraph(
        self, *, repo: Path, terms: list[str], deadline: float | None = None
    ) -> list[tuple[str, int, str]] | None:
        """Try codegraph semantic search; return None to fall through to ripgrep."""
        si_opts = self.config.source_investigation
        if not si_opts.codegraph_enabled:
            return None
        try:
            from ...knowledge.codegraph_client import CodeGraphClient
        except ImportError:
            return None
        cg = CodeGraphClient(
            command=si_opts.codegraph_command,
            timeout=min(si_opts.codegraph_timeout_seconds, self._SOURCE_EVIDENCE_CODEGRAPH_CALL_SECONDS),
        )
        if not cg.is_available():
            return None
        status_timeout = self._source_evidence_call_timeout(deadline, si_opts.codegraph_timeout_seconds)
        if status_timeout <= 0 or not cg.is_indexed(repo, timeout=status_timeout):
            return None
        matches: list[tuple[str, int, str]] = []
        seen: set[tuple[str, int]] = set()
        for term in terms[:5]:
            call_timeout = self._source_evidence_call_timeout(deadline, si_opts.codegraph_timeout_seconds)
            if call_timeout <= 0:
                break
            hits = cg.search_symbol(term, repo, limit=5, timeout=call_timeout)
            for h in hits:
                key = (h.path, h.line)
                if key in seen:
                    continue
                seen.add(key)
                matches.append((h.path, h.line, f"[{h.kind}] {h.qualified_name or h.name}"))
            call_timeout = self._source_evidence_call_timeout(deadline, si_opts.codegraph_timeout_seconds)
            if call_timeout <= 0:
                break
            callers = cg.get_callers(term, repo, limit=5, timeout=call_timeout)
            for c in callers:
                key = (c.path, c.line)
                if key in seen:
                    continue
                seen.add(key)
                matches.append((c.path, c.line, f"[caller→{term}] {c.name}"))
        return matches if matches else None

    def _source_evidence_terms(
        self,
        *,
        plans: list["BugAnalysisPlan"],
        request_text: str,
        followup_text: str,
        extra_texts: tuple[str, ...] = (),
    ) -> list[str]:
        terms: list[str] = []
        title_text = extra_texts[0] if extra_texts else ""
        description_texts = extra_texts[1:] if len(extra_texts) > 1 else ()
        for text in (request_text, followup_text, title_text):
            for term in self._explicit_source_terms_from_text(text):
                self._append_unique(terms, term)
        for plan in plans:
            if plan.kind != "signal" or not plan.signal_code:
                continue
            self._append_unique(terms, plan.signal_code)
            if plan.signal_code.startswith("SIGNAL_"):
                self._append_unique(terms, plan.signal_code.removeprefix("SIGNAL_"))
        for text in (followup_text, request_text):
            signal = self._extract_signal_code_for_reanalysis(text)
            if signal:
                self._append_unique(terms, signal)
                if signal.startswith("SIGNAL_"):
                    self._append_unique(terms, signal.removeprefix("SIGNAL_"))
        for text in (request_text, followup_text, title_text, *description_texts):
            for term in self._business_source_terms_from_text(text):
                self._append_unique(terms, term)
        return terms[:8]

    def _explicit_source_terms_from_text(self, text: str) -> list[str]:
        search_text = re.sub(r"https?://\S+", " ", text or "")
        search_text = re.sub(
            r"(?im)^\s*(?:Version|Build|Serial|ICCID|VIN)\s*[:：].*$",
            " ",
            search_text,
        )
        ignored_tokens = {"Version", "Build", "Serial", "ICCID", "VIN", "CLI"}
        terms: list[str] = []
        for match in re.finditer(
            r"(?<![A-Za-z0-9_])([A-Za-z_][A-Za-z0-9_.-]{1,120}\.(?:kt|java|cpp|cc|c|h|hpp|proto|xml))",
            search_text,
        ):
            filename = match.group(1).strip()
            self._append_unique(terms, filename)
            stem = Path(filename).stem
            if stem:
                self._append_unique(terms, stem)
        for match in re.finditer(r"\b([A-Z][A-Za-z0-9_]{4,})\b", search_text):
            token = match.group(1).strip()
            if token in ignored_tokens:
                continue
            self._append_unique(terms, token)
        return terms

    def _business_source_terms_from_text(self, text: str) -> list[str]:
        suffixes = (
            "模式",
            "功能",
            "场景",
            "页面",
            "流程",
            "链路",
            "策略",
            "状态",
            "异常",
            "失败",
            "开关",
            "服务",
            "模块",
            "信号",
            "电量",
            "电流",
            "电压",
            "百分比",
        )
        generic_prefixes = (
            "结合",
            "根据",
            "找到",
            "分析",
            "重新",
            "通过",
            "查看",
            "确认",
            "排查",
            "调查",
            "主要是",
            "为什么",
            "无法",
            "不能",
            "开启",
        )
        ascii_stopwords = {
            "http",
            "https",
            "project",
            "feishu",
            "meegle",
            "buglo",
            "detail",
            "cli",
            "bug",
            "code",
            "html",
            "report",
            "version",
            "build",
            "serial",
            "iccid",
            "vin",
            "cli",
        }
        search_text = re.sub(r"https?://\S+", " ", text or "")
        search_text = re.sub(
            r"(?im)^\s*(?:Version|Build|Serial|ICCID|VIN)\s*[:：].*$",
            " ",
            search_text,
        )
        terms: list[str] = []
        for match in re.finditer(r"(?<![A-Za-z0-9_])([A-Za-z][A-Za-z0-9_]{1,16}[\u4e00-\u9fff]{1,8})", search_text):
            term = match.group(1)
            self._append_unique(terms, term)
            ascii_part = re.match(r"[A-Za-z][A-Za-z0-9_]{1,16}", term)
            if ascii_part:
                token = ascii_part.group(0)
                chinese_part = term[len(token) :]
                for suffix in suffixes:
                    suffix_index = chinese_part.find(suffix)
                    if suffix_index >= 0:
                        compact = token + chinese_part[: suffix_index + len(suffix)]
                        if compact != term:
                            self._append_unique(terms, compact)
                if token.casefold() not in ascii_stopwords:
                    self._append_unique(terms, token)
        for match in re.finditer(r"(?<![A-Za-z0-9_])([A-Za-z][A-Za-z0-9_]{2,31})(?![A-Za-z0-9_])", search_text):
            token = match.group(1)
            if token.casefold() not in ascii_stopwords and not token.isdigit():
                self._append_unique(terms, token)
        for suffix in suffixes:
            pattern = re.compile(rf"[\u4e00-\u9fff]{{2,14}}{re.escape(suffix)}")
            for match in pattern.finditer(search_text):
                term = match.group(0)
                changed = True
                while changed:
                    changed = False
                    for prefix in generic_prefixes:
                        if term.startswith(prefix) and len(term) > len(prefix) + len(suffix):
                            term = term[len(prefix) :]
                            changed = True
                self._append_unique(terms, term)
                compact_len = len(suffix) + 2
                if len(term) > compact_len:
                    self._append_unique(terms, term[-compact_len:])
        return terms

    def _collect_source_evidence(
        self, *, repo: Path, terms: list[str], deadline: float | None = None
    ) -> list[tuple[str, int, str]]:
        if deadline is not None and time.monotonic() >= deadline:
            return []
        target_timeout = 5.0
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return []
            target_timeout = min(target_timeout, remaining)
        explicit_matches = self._collect_source_evidence_by_targets(repo=repo, terms=terms, timeout=target_timeout)
        if deadline is not None and time.monotonic() >= deadline:
            return explicit_matches
        rg_timeout = 20.0
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return explicit_matches
            rg_timeout = min(rg_timeout, remaining)
        rg_matches = self._collect_source_evidence_with_rg(repo=repo, terms=terms, timeout=rg_timeout)
        if rg_matches is not None:
            return self._merge_source_evidence_matches(explicit_matches, rg_matches)
        return explicit_matches
