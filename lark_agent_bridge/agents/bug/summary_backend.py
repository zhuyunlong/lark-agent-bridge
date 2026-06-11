from __future__ import annotations

from ._shared import *  # noqa: F401,F403


class _SummaryBackendMixin:
    """Bug Agent 总结执行后端：主后端调度、超时计算与 OMLX 兜底（与 BugPromptMixin 共享 self 状态）。"""

    def _run_bug_agent_summary(
        self,
        *,
        request_text: str,
        request_artifact: Path,
        metadata_path: Path,
        output_path: Path,
        progress_callback: Callable[[dict[str, object]], None] | None,
        timeout: int,
        provider_session_id: str = "",
        followup_text: str = "",
        previous_summary_path: Path | None = None,
        prefer_lightweight: bool = False,
        provider_override: str = "",
        command_override: str = "",
        bridge_session_id: str = "",
        snapshot_details: dict[str, object] | None = None,
        snapshot_plans: list["BugAnalysisPlan"] | None = None,
    ) -> dict[str, object]:
        explicit_provider = _normalize_provider_name(provider_override)
        if explicit_provider == "omlx":
            return self._annotate_summary_backend_result(
                self._run_bug_agent_summary_omlx_fallback(
                    request_text=request_text,
                    request_artifact=request_artifact,
                    metadata_path=metadata_path,
                    output_path=output_path,
                    followup_text=followup_text,
                    previous_summary_path=previous_summary_path,
                    progress_callback=progress_callback,
                    reason="explicit_agent",
                    allow_file_context=True,
                    snapshot_details=snapshot_details,
                    snapshot_plans=snapshot_plans,
                ),
                execution_backend="omlx",
                backend_reason="explicit_lightweight",
            )
        skip_direct_api = self._should_skip_direct_api_bug_summary(
            request_text=request_text,
            followup_text=followup_text,
            request_artifact=request_artifact,
            metadata_path=metadata_path,
            previous_summary_path=previous_summary_path,
        )
        backend_decision = choose_summary_backend(
            SummaryBackendInput(
                explicit_provider=explicit_provider,
                provider_session_id=provider_session_id,
                prefer_lightweight=False,
                ai_provider_enabled=self.config.ai_provider.enabled,
                ai_provider_base_url=self.config.ai_provider.base_url,
                ai_provider_primary_model=self.config.ai_provider.primary_model,
                skip_direct_api=skip_direct_api,
                auto_fallback_to_file_agent=self.config.bug_analysis.auto_fallback_to_file_agent,
            )
        )
        invocation = self._build_bug_agent_summary_command(
            request_text=request_text,
            request_artifact=request_artifact,
            metadata_path=metadata_path,
            output_path=output_path,
            provider_session_id=provider_session_id,
            followup_text=followup_text,
            previous_summary_path=previous_summary_path,
            provider_override=explicit_provider,
            command_override=command_override,
            snapshot_details=snapshot_details,
            snapshot_plans=snapshot_plans,
        )
        if not invocation["command"]:
            return {
                "message": "",
                "command": None,
                "error": "agent_summary_not_configured",
                "provider": "",
                "session_id": provider_session_id,
                "resumed": False,
                "usage_scope": "",
            }
        explicit_file_agent = bool(explicit_provider)
        if prefer_lightweight:
            omlx_result = self._run_bug_agent_summary_omlx_fallback(
                request_text=request_text,
                request_artifact=request_artifact,
                metadata_path=metadata_path,
                output_path=output_path,
                followup_text=followup_text,
                previous_summary_path=previous_summary_path,
                progress_callback=progress_callback,
                reason="lightweight_first",
                snapshot_details=snapshot_details,
                snapshot_plans=snapshot_plans,
            )
            if omlx_result["message"]:
                return self._annotate_summary_backend_result(
                    omlx_result,
                    execution_backend="omlx",
                    backend_reason="prefer_lightweight",
                )
            self._emit_progress(
                progress_callback,
                stage="bug_agent_summary_lightweight_unavailable",
                message="轻量总结不可用，切换主 Agent 整理最终结论",
                primary_provider=str(invocation["provider"] or ""),
                lightweight_provider="omlx",
                lightweight_error=str(omlx_result.get("error") or ""),
            )
        # --- Direct API path (fast, preferred when [ai_provider] is enabled) ---
        if (
            not explicit_file_agent
            and not provider_session_id.strip()
            and backend_decision.backend == "direct_api"
        ):
            api_result = self._run_bug_agent_summary_via_api(
                request_text=request_text,
                request_artifact=request_artifact,
                metadata_path=metadata_path,
                output_path=output_path,
                followup_text=followup_text,
                previous_summary_path=previous_summary_path,
                progress_callback=progress_callback,
                snapshot_details=snapshot_details,
                snapshot_plans=snapshot_plans,
            )
            if api_result["message"]:
                return self._annotate_summary_backend_result(
                    api_result,
                    execution_backend="direct_api",
                    backend_reason=backend_decision.reason,
                    fallback_from=backend_decision.fallback_from,
                )
            failure_decision = choose_summary_backend(
                SummaryBackendInput(
                    explicit_provider=explicit_provider,
                    provider_session_id=provider_session_id,
                    prefer_lightweight=False,
                    ai_provider_enabled=self.config.ai_provider.enabled,
                    ai_provider_base_url=self.config.ai_provider.base_url,
                    ai_provider_primary_model=self.config.ai_provider.primary_model,
                    skip_direct_api=skip_direct_api,
                    direct_api_failure=str(api_result.get("error") or "direct_api_failed"),
                    auto_fallback_to_file_agent=self.config.bug_analysis.auto_fallback_to_file_agent,
                )
            )
            if failure_decision.backend == "direct_api":
                return self._annotate_summary_backend_result(
                    api_result,
                    execution_backend="direct_api",
                    backend_reason=failure_decision.reason,
                    fallback_from=failure_decision.fallback_from,
                )
            logger.warning(
                "Direct API summary failed (error=%s), falling back to subprocess",
                api_result.get("error", "unknown"),
            )
            backend_decision = failure_decision
        result = self._run_bug_agent_summary_once(
            invocation=invocation,
            output_path=output_path,
            progress_callback=progress_callback,
            timeout=timeout,
            bridge_session_id=bridge_session_id,
        )
        if result["message"] and result["provider"]:
            return self._annotate_summary_backend_result(
                result,
                execution_backend="file_agent",
                backend_reason=backend_decision.reason,
                fallback_from=backend_decision.fallback_from,
            )
        if explicit_file_agent:
            return self._annotate_summary_backend_result(
                result,
                execution_backend="file_agent",
                backend_reason=backend_decision.reason,
                fallback_from=backend_decision.fallback_from,
            )
        if result["message"]:
            return self._annotate_summary_backend_result(
                result,
                execution_backend="file_agent",
                backend_reason=backend_decision.reason,
                fallback_from=backend_decision.fallback_from,
            )
        fallback_result = result
        if provider_session_id.strip() and str(result.get("error") or "") != "agent_summary_timeout":
            self._emit_progress(
                progress_callback,
                stage="bug_agent_summary_retry",
                message="Agent 续会话失败，退回重新读取最新产物整理结论",
                provider=invocation["provider"],
                previous_session_id=provider_session_id.strip(),
            )
            fallback_invocation = self._build_bug_agent_summary_command(
                request_text=request_text,
                request_artifact=request_artifact,
                metadata_path=metadata_path,
                output_path=output_path,
                provider_session_id="",
                followup_text=followup_text,
                previous_summary_path=previous_summary_path,
                snapshot_details=snapshot_details,
                snapshot_plans=snapshot_plans,
            )
            if fallback_invocation["command"]:
                fallback_result = self._run_bug_agent_summary_once(
                    invocation=fallback_invocation,
                    output_path=output_path,
                    progress_callback=progress_callback,
                    timeout=timeout,
                    bridge_session_id=bridge_session_id,
                )
                if fallback_result["message"] and fallback_result["provider"]:
                    return self._annotate_summary_backend_result(
                        fallback_result,
                        execution_backend="file_agent",
                        backend_reason="resume_retry_file_agent",
                    )
        if str(fallback_result.get("error") or "") == "agent_summary_timeout":
            omlx_result = self._run_bug_agent_summary_omlx_fallback(
                request_text=request_text,
                request_artifact=request_artifact,
                metadata_path=metadata_path,
                output_path=output_path,
                followup_text=followup_text,
                previous_summary_path=previous_summary_path,
                progress_callback=progress_callback,
                reason="primary_timeout",
                snapshot_details=snapshot_details,
                snapshot_plans=snapshot_plans,
            )
            if omlx_result["message"]:
                return self._annotate_summary_backend_result(
                    omlx_result,
                    execution_backend="omlx",
                    backend_reason="primary_timeout",
                    fallback_from="file_agent",
                )
            return self._annotate_summary_backend_result(
                fallback_result,
                execution_backend="file_agent",
                backend_reason=backend_decision.reason,
                fallback_from=backend_decision.fallback_from,
            )
        return self._annotate_summary_backend_result(
            fallback_result,
            execution_backend="file_agent",
            backend_reason=backend_decision.reason,
            fallback_from=backend_decision.fallback_from,
        )
    def _annotate_summary_backend_result(
        self,
        result: dict[str, object],
        *,
        execution_backend: str,
        backend_reason: str = "",
        fallback_from: str = "",
    ) -> dict[str, object]:
        annotated = dict(result)
        annotated["execution_backend"] = execution_backend
        annotated["backend_reason"] = backend_reason
        annotated["fallback_from"] = fallback_from
        return annotated
    def _agent_summary_timeout(
        self,
        operation_timeout_seconds: int,
        *,
        reference_seconds: float | None = None,
    ) -> int:
        configured = int(getattr(self.config.bug_analysis, "agent_summary_timeout_seconds", 300) or 0)
        operation_limit = max(1, min(int(operation_timeout_seconds or 0), 1800))
        if configured <= 0:
            base_timeout = operation_limit
        else:
            base_timeout = max(1, min(operation_limit, configured))
        if not isinstance(reference_seconds, (int, float)) or reference_seconds <= 0:
            return base_timeout
        reference_timeout = int(math.ceil(float(reference_seconds) * 1.25))
        return max(1, min(operation_limit, max(base_timeout, reference_timeout)))
    def _agent_summary_timeout_reference(self, previous_session: dict[str, object] | None) -> float | None:
        if not isinstance(previous_session, dict):
            return None
        candidates: list[float] = []
        for value in (previous_session.get("duration_seconds"), previous_session.get("elapsed_seconds")):
            if isinstance(value, (int, float)) and value > 0:
                candidates.append(float(value))
        details = previous_session.get("details")
        if isinstance(details, dict):
            for key in ("agent_summary_duration_seconds", "agent_summary_timeout_seconds"):
                value = details.get(key)
                if isinstance(value, (int, float)) and value > 0:
                    candidates.append(float(value))
        return max(candidates) if candidates else None
    def _run_bug_agent_summary_omlx_fallback(
        self,
        *,
        request_text: str,
        request_artifact: Path,
        metadata_path: Path,
        output_path: Path,
        followup_text: str = "",
        previous_summary_path: Path | None = None,
        progress_callback: Callable[[dict[str, object]], None] | None,
        reason: str = "primary_timeout",
        allow_file_context: bool = False,
        snapshot_details: dict[str, object] | None = None,
        snapshot_plans: list["BugAnalysisPlan"] | None = None,
    ) -> dict[str, object]:
        options = self.config.omlx_chat
        provider = "omlx"
        command = ["omlx", options.model]
        if self._bug_summary_referenced_context_files(metadata_path) and not allow_file_context:
            return {
                "message": "",
                "command": command,
                "error": "omlx_file_context_unsupported",
                "provider": provider,
                "session_id": "",
                "resumed": False,
                "usage": {},
                "usage_scope": "",
            }
        if not options.enabled:
            return {
                "message": "",
                "command": command,
                "error": "omlx_disabled",
                "provider": provider,
                "session_id": "",
                "resumed": False,
                "usage": {},
                "usage_scope": "",
            }
        prompt = self._build_omlx_bug_summary_prompt(
            request_text=request_text,
            request_artifact=request_artifact,
            metadata_path=metadata_path,
            followup_text=followup_text,
            previous_summary_path=previous_summary_path,
            include_context_file_excerpts=allow_file_context,
            snapshot_details=snapshot_details,
            snapshot_plans=snapshot_plans,
        )
        if not prompt.strip():
            return {
                "message": "",
                "command": command,
                "error": "omlx_prompt_empty",
                "provider": provider,
                "session_id": "",
                "resumed": False,
                "usage": {},
                "usage_scope": "",
            }
        self._emit_progress(
            progress_callback,
            stage=(
                "bug_agent_summary_omlx"
                if reason in {"lightweight_first", "explicit_agent"}
                else "bug_agent_summary_omlx_fallback"
            ),
            message=(
                "用户指定 OMLX 本地模型基于已生成材料重新分析"
                if reason == "explicit_agent"
                else "本地 omlx 基于已生成报告/元数据整理最终结论"
                if reason == "lightweight_first"
                else "主 Agent 超时，改用本地 omlx 基于现有材料做轻量总结"
            ),
            provider=provider,
            model=options.model,
            reason=reason,
        )
        started = time.monotonic()
        result = OmlxChatClient(self.config)._chat(
            mode="bug_agent_summary_omlx",
            system_prompt=(
                "你是本地轻量 bug 总结模型。只能基于用户提供的现有 request、metadata、报告摘录和历史摘要回答；"
                "不要声称读取了文件系统或源码；如果材料不足，要明确写出缺口。输出中文 Markdown，结论先行。"
            ),
            messages=[{"role": "user", "content": prompt}],
        )
        if result.success and result.message.strip():
            message = result.message.strip()
            try:
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(message, encoding="utf-8")
            except OSError:
                pass
            return {
                "message": message,
                "command": command,
                "error": "",
                "provider": provider,
                "model": options.model,
                "session_id": "",
                "resumed": False,
                "duration_seconds": result.duration_seconds
                if isinstance(result.duration_seconds, (int, float))
                else time.monotonic() - started,
                "usage": {},
                "usage_scope": "",
            }
        return {
            "message": "",
            "command": command,
            "error": result.error_code or result.message or "omlx_fallback_failed",
            "provider": provider,
            "model": options.model,
            "session_id": "",
            "resumed": False,
            "duration_seconds": result.duration_seconds
            if isinstance(result.duration_seconds, (int, float))
            else time.monotonic() - started,
            "usage": {},
            "usage_scope": "",
        }
