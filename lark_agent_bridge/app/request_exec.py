from __future__ import annotations

from datetime import datetime, timezone
import json
import re
from urllib.parse import quote, urlsplit, urlunsplit

from ._shared import *  # noqa: F401,F403


class _RequestExecMixin:
    """各业务请求执行入口与审批流（与 ResultBugMixin 共享 self 状态）。"""

    def check_approval(
        self,
        event: LarkEvent,
        operation_type: str,
        description: str,
        **hints: object,
    ) -> tuple[bool, str]:
        """Check if an operation is approved to proceed.

        Returns ``(can_proceed, request_id)``.  If the operation needs
        confirmation, sends a confirmation card and returns ``False``.
        """
        op = build_operation_request(
            operation_type,
            description,
            requester_id=event.sender_id,
            chat_id=event.chat_id,
            **hints,
        )
        decision = self.approval_store.evaluate(op)
        if decision.can_proceed:
            return True, decision.request_id

        # Send confirmation card to the user
        if not self.config.dry_run and event.chat_type in {"group", "p2p"}:
            card = build_confirmation_card(
                title=f"操作确认：{description}",
                description=f"即将执行 **{description}**，该操作风险等级为 **{op.risk_level.value}**。请确认是否继续。",
                risk_level=op.risk_level.value,
                action_id=decision.request_id,
                metadata={"操作类型": operation_type, **{k: str(v) for k, v in hints.items()}},
            )
            card_json_str = card_to_json(card)
            self.lark_client.send_card_response(event, card_json_str)

        return False, decision.request_id
    def resolve_approval(self, request_id: str, *, approved: bool) -> bool:
        """Resolve a pending approval. Returns whether it was approved."""
        decision = self.approval_store.resolve(request_id, approved=approved)
        return decision.can_proceed
    def _run_signal_request(self, event: LarkEvent, request: SignalRequest, route_content: str) -> TaskResult:
        self._send_intent_preflight_card(
            event,
            _IntentPreflightDecision(
                title="信号生命周期",
                intent_label="信号生命周期分析",
                confidence_label="高置信度",
                strategy_label="自动执行",
                reason="消息已明确命中信号生命周期分析入口。",
            ),
        )
        self._notify_progress(
            "signal_request_received",
            "收到信号生命周期分析请求",
            event=event,
            signal=request.signal or "",
            raw_text=request.raw_text,
            resource_count=len(request.resources),
            resources=self._resource_descriptors(request.resources),
        )
        self.send_status_card(
            event,
            title="信号生命周期",
            status="analyzing",
            details={"信号": request.signal or "未指定", "资源数": str(len(request.resources))},
            note="分析进行中，请稍候…",
        )
        result = self.handler.handle(request, event=event)
        return self._deliver_result(event, result, request_text=request.raw_text or route_content)
    def _run_bug_request(
        self,
        event: LarkEvent,
        bug_request,
        route_content: str,
        *,
        plans_override: list[BugAnalysisPlan] | None = None,
        classification_skill: str = "",
        classification_source: str = "",
        classification_reason: str = "",
        classification_provider: str = "",
        root_message_id: str | None = None,
    ) -> TaskResult:
        self._send_intent_preflight_card(
            event,
            _IntentPreflightDecision(
                title="Bug 分析",
                intent_label="Bug 分析",
                confidence_label="高置信度",
                strategy_label="自动执行",
                reason="消息包含显式 Bug 链接，将按 Bug 分析路径执行。",
            ),
        )
        self._notify_progress(
            "bug_request_received",
            "收到 bug 分析请求",
            event=event,
            session_id=root_message_id,
            bug_url=bug_request.bug_url,
            prompt=bug_request.prompt,
            raw_text=bug_request.raw_text,
        )
        self.send_status_card(
            event,
            title="Bug 分析",
            status="analyzing",
            details={"Bug 链接": bug_request.bug_url[:60], "分析提示": bug_request.prompt or "默认"},
            note="分析进行中，请稍候…",
            session_id=root_message_id,
        )
        runner_kwargs: dict[str, object] = {
            "event": event,
            "progress_callback": self._event_progress_callback(event, session_id=root_message_id),
        }
        if plans_override is not None:
            runner_kwargs["plans_override"] = plans_override
        if classification_skill:
            runner_kwargs["classification_skill"] = classification_skill
        if classification_source:
            runner_kwargs["classification_source"] = classification_source
        if classification_reason:
            runner_kwargs["classification_reason"] = classification_reason
        if classification_provider:
            runner_kwargs["classification_provider"] = classification_provider
        result = self.bug_runner.run_bug_analysis(bug_request, **runner_kwargs)
        self._ensure_result_bug_url(result, bug_request.bug_url)
        return self._deliver_result(
            event,
            result,
            request_text=bug_request.raw_text or route_content,
            root_message_id=root_message_id,
        )
    def _run_perception_request(self, event: LarkEvent, perception_request, route_content: str) -> TaskResult:
        self._send_intent_preflight_card(
            event,
            _IntentPreflightDecision(
                title="感知数据总结",
                intent_label="感知数据总结",
                confidence_label="高置信度",
                strategy_label="自动执行",
                reason="消息已明确命中感知数据总结入口。",
            ),
        )
        self._notify_progress(
            "perception_summary_request_received",
            "收到感知数据总结请求",
            event=event,
            prompt=perception_request.prompt,
            raw_text=perception_request.raw_text,
        )
        self.send_status_card(
            event,
            title="感知数据总结",
            status="analyzing",
            details={"提示": perception_request.prompt or "默认"},
            note="分析进行中，请稍候…",
        )
        result = self.perception_runner.run_summary(perception_request, event=event)
        return self._deliver_result(event, result, request_text=perception_request.raw_text or route_content)
    def _run_direct_analysis_request(
        self,
        event: LarkEvent,
        direct_analysis_request,
        route_content: str,
        *,
        plans_override: list[BugAnalysisPlan] | None = None,
        classification_skill: str = "",
        classification_source: str = "",
        classification_reason: str = "",
    ) -> TaskResult:
        self._notify_progress(
            "direct_analysis_request_received",
            "收到直传文件分析请求",
            event=event,
            prompt=direct_analysis_request.prompt,
            raw_text=direct_analysis_request.raw_text,
            resources=[item.value for item in direct_analysis_request.resources],
        )
        self.send_status_card(
            event,
            title="文件分析",
            status="analyzing",
            details={"文件数": str(len(direct_analysis_request.resources))},
            note="分析进行中，请稍候…",
        )
        result = self.bug_runner.run_direct_analysis(
            direct_analysis_request,
            event=event,
            progress_callback=self._event_progress_callback(event),
            plans_override=plans_override,
            classification_skill=classification_skill,
            classification_source=classification_source,
            classification_reason=classification_reason,
        )
        return self._deliver_result(event, result, request_text=direct_analysis_request.raw_text or route_content)
    def _run_app_server_investigation_request(
        self,
        event: LarkEvent,
        request,
        route_content: str,
    ) -> TaskResult:
        self._send_intent_preflight_card(
            event,
            _IntentPreflightDecision(
                title="AI 自主分析",
                intent_label="AI 自主分析",
                confidence_label="高置信度",
                strategy_label="自动执行",
                reason="消息命中显式自主分析触发词，将由 Codex app-server 自行选择 skill 并执行只读分析。",
            ),
        )
        self._notify_progress(
            "app_server_investigation_request_received",
            "收到 AI 自主分析请求",
            event=event,
            bug_url=request.bug_url,
            prompt=request.prompt,
            raw_text=request.raw_text,
            resources=[item.value for item in request.resources],
            trigger_mode=request.trigger_mode,
            trigger_term=request.trigger_term,
        )
        details = {"触发词": request.trigger_term or request.trigger_mode or "未知"}
        if request.bug_url:
            details["Bug 链接"] = request.bug_url[:60]
        if request.resources:
            details["文件数"] = str(len(request.resources))
        self.send_status_card(
            event,
            title="AI 自主分析",
            status="analyzing",
            details=details,
            note="分析进行中，请稍候…",
        )
        result = self.app_server_investigation_runner.run(
            request,
            event=event,
            progress_callback=self._event_progress_callback(event),
        )
        self._ensure_result_bug_url(result, request.bug_url)
        return self._deliver_result(event, result, request_text=request.raw_text or route_content)
    def _run_rom_version_lookup_request(self, event: LarkEvent, rom_version_request) -> TaskResult:
        self._notify_progress(
            "rom_version_lookup_received",
            "收到 ROM 版本查询请求",
            event=event,
            rom_version=rom_version_request.rom_version,
            prompt=rom_version_request.prompt,
            raw_text=rom_version_request.raw_text,
        )
        result = self.rom_version_runner.run_lookup(rom_version_request, event=event)
        return self._deliver_result(event, result, request_text=rom_version_request.raw_text)
    def _run_addr2line_resolve_request(self, event: LarkEvent, request: Addr2LineRequest) -> TaskResult:
        self._notify_progress(
            "addr2line_resolve_received",
            "收到地址反解请求",
            event=event,
            target=request.target,
            rom_version=request.rom_version,
            napa_version=request.napa_version,
            apk_version=request.apk_version,
            raw_text=request.raw_text,
        )
        resolved_request = self._resolve_addr2line_navigation_version(event, request)
        if isinstance(resolved_request, TaskResult):
            return self._deliver_result(event, resolved_request, request_text=request.raw_text)
        request = resolved_request
        result = self.addr2line_runner.run_resolve(request, event=event)
        return self._deliver_result(event, result, request_text=request.raw_text)
    def _resolve_addr2line_navigation_version(self, event: LarkEvent, request: Addr2LineRequest) -> Addr2LineRequest | TaskResult:
        if request.apk_version or request.napa_version or not request.rom_version:
            return request
        lookup_request = parse_rom_version_lookup_request(f"{request.rom_version} 查导航版本")
        if not lookup_request.triggered or not lookup_request.rom_version:
            return request
        self._notify_progress(
            "addr2line_symbol_version_lookup",
            "查询 ROM 对应导航符号表版本",
            event=event,
            rom_version=request.rom_version,
        )
        lookup_result = self.rom_version_runner.run_lookup(lookup_request, event=event)
        if not lookup_result.success:
            return TaskResult(
                success=False,
                message=(
                    "ROM 查询失败，无法确定导航符号表版本；"
                    "当前不会再按日期近似猜测符号表，请先修复 ROM 查询或直接提供 APK 版本后重试。\n"
                    f"{lookup_result.message}"
                ),
                error_code="addr2line_symbol_version_lookup_failed",
                stdout=lookup_result.stdout,
                stderr=lookup_result.stderr,
                details={
                    "mode": "addr2line_resolve",
                    "rom_version": request.rom_version,
                    "lookup_error_code": lookup_result.error_code,
                },
            )
        required_outputs = self._required_outputs_from_lookup_result(lookup_result)
        navigation_version = self._navigation_version_from_required_outputs(required_outputs)
        if not navigation_version:
            return TaskResult(
                success=False,
                message=(
                    "ROM 查询没有返回导航版本，无法确定导航符号表版本；"
                    "当前不会再按日期近似猜测符号表，请先修复 ROM 查询或直接提供 APK 版本后重试。"
                ),
                error_code="addr2line_symbol_version_lookup_failed",
                stdout=lookup_result.stdout,
                stderr=lookup_result.stderr,
                details={
                    "mode": "addr2line_resolve",
                    "rom_version": request.rom_version,
                    "lookup_error_code": lookup_result.error_code,
                },
            )
        self._notify_progress(
            "addr2line_symbol_version_resolved",
            "已匹配导航符号表版本",
            event=event,
            rom_version=request.rom_version,
            apk_version=navigation_version,
            symbol_table_url=str(required_outputs.get("symbol_table_url") or "").strip(),
            napa5_download_url=str(required_outputs.get("napa5_download_url") or "").strip(),
        )
        return Addr2LineRequest(
            addr_text=request.addr_text,
            resources=request.resources,
            raw_text=request.raw_text,
            rom_version=request.rom_version,
            napa_version=request.napa_version,
            apk_version=navigation_version,
            symbol_table_url=str(required_outputs.get("symbol_table_url") or "").strip(),
            napa5_download_url=str(required_outputs.get("napa5_download_url") or "").strip(),
            log_folder=request.log_folder,
            fault_time=request.fault_time,
            target=request.target,
            prompt=request.prompt,
            triggered=request.triggered,
            error=None if request.error == "missing_symbol_version" else request.error,
        )
    def _maybe_request_approval(
        self,
        event: LarkEvent,
        *,
        operation_type: str,
        description: str,
        route_content: str,
        **hints: object,
    ) -> TaskResult | None:
        if not self.config.approval.enabled:
            return None
        op = build_operation_request(
            operation_type,
            description,
            requester_id=event.sender_id,
            chat_id=event.chat_id,
            **hints,
        )
        op.metadata.update(
            {
                "event_payload": self._event_payload(event),
                "route_content": route_content,
            }
        )
        decision = self.approval_store.evaluate(op)
        if decision.can_proceed:
            return None
        if not self.config.dry_run and event.chat_type in {"group", "p2p"}:
            card = build_confirmation_card(
                title=f"操作确认：{description}",
                description=f"即将执行 **{description}**，该操作风险等级为 **{op.risk_level.value}**。请确认是否继续。",
                risk_level=op.risk_level.value,
                action_id=decision.request_id,
                metadata={"操作类型": operation_type, **{k: str(v) for k, v in hints.items()}},
            )
            self.lark_client.send_card_response(event, card_to_json(card))
        return TaskResult(
            success=False,
            message=f"{description} 等待确认后执行。",
            error_code="approval_pending",
            details={
                "mode": "approval",
                "operation_type": operation_type,
                "approval_request_id": decision.request_id,
                "risk_level": op.risk_level.value,
            },
        )
