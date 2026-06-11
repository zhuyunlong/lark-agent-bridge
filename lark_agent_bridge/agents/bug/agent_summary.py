from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from . import _shared


from .summary_stream import _SummaryStreamMixin
from .summary_usage import _SummaryUsageMixin
from .runtime_metadata import _RuntimeMetadataMixin
from .source_evidence import _SourceEvidenceMixin


class _AgentSummaryMixin(_SummaryStreamMixin, _SummaryUsageMixin, _RuntimeMetadataMixin, _SourceEvidenceMixin):
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
