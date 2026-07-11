from __future__ import annotations

from datetime import datetime, timezone
import html
from pathlib import Path
import re
import threading
from typing import Callable

from ._shared import *  # noqa: F401,F403


class _RoutesMixin:
    """路由矩阵：各业务域 _route_* 与 _would_route_* 判定（与 HandleEventMixin 共享 self 状态）。"""

    def _route_arbitrated_business_routes(self, ctx: _RouteContext) -> TaskResult | None:
        candidates = self._collect_arbitrated_route_candidates(ctx)
        if not candidates:
            return None
        winner = self._choose_arbitrated_route(ctx, candidates)
        if winner is None:
            return None
        result = self._invoke_arbitrated_route(ctx, winner.route)
        if result is None:
            return None
        details = result.details if isinstance(result.details, dict) else {}
        details = dict(details)
        details.setdefault("route_candidates", [item.to_dict() for item in candidates])
        details.setdefault("route_winner", winner.route)
        details.setdefault("route_winner_reason", winner.reason)
        result.details = details
        return result
    def _collect_arbitrated_route_candidates(self, ctx: _RouteContext) -> list[RouteCandidate]:
        candidates: list[RouteCandidate] = []
        if self._would_route_knowledge_qa(ctx):
            candidates.append(
                RouteCandidate(
                    route="knowledge_qa",
                    band="explicit" if self._has_explicit_knowledge_trigger(ctx.route_content) else "heuristic",
                    score=95 if self._has_explicit_knowledge_trigger(ctx.route_content) else 40,
                    reason=(
                        "explicit_knowledge_trigger"
                        if self._has_explicit_knowledge_trigger(ctx.route_content)
                        else "knowledge_heuristic_should_handle"
                    ),
                )
            )
        if self._would_route_source_analysis(ctx):
            blocked_by: list[str] = []
            if self._would_route_requirement_analysis(ctx):
                blocked_by.append("requirement_analysis")
            candidates.append(
                RouteCandidate(
                    route="source_analysis",
                    band="explicit",
                    score=90,
                    reason="explicit_source_analysis_request",
                    blocked_by=blocked_by,
                )
            )
        if self._would_route_bug_request(ctx):
            blocked_by = []
            if self._would_route_requirement_analysis(ctx):
                blocked_by.append("requirement_analysis")
            if self._would_route_signal_request(ctx):
                blocked_by.append("signal_request")
            if self._would_route_claude_skill(ctx):
                blocked_by.append("claude_skill")
            candidates.append(
                RouteCandidate(
                    route="bug_request",
                    band="explicit",
                    score=85,
                    reason="explicit_bug_request",
                    blocked_by=blocked_by,
                )
            )
        if self._would_route_direct_analysis(ctx):
            blocked_by = []
            if self._would_route_requirement_analysis(ctx):
                blocked_by.append("requirement_analysis")
            if self._would_route_signal_request(ctx):
                blocked_by.append("signal_request")
            if self._would_route_claude_skill(ctx):
                blocked_by.append("claude_skill")
            if self._would_route_perception(ctx):
                blocked_by.append("perception_summary")
            candidates.append(
                RouteCandidate(
                    route="direct_analysis",
                    band="explicit",
                    score=80,
                    reason="explicit_direct_analysis_request",
                    blocked_by=blocked_by,
                )
            )
        return candidates
    def _choose_arbitrated_route(self, ctx: _RouteContext, candidates: list[RouteCandidate]) -> RouteCandidate | None:
        del ctx
        viable = [candidate for candidate in candidates if not candidate.blocked_by]
        if not viable:
            return None
        band_rank = {
            "hard_gate": 5,
            "explicit": 4,
            "contextual": 3,
            "heuristic": 2,
            "fallback": 1,
        }
        return max(viable, key=lambda item: (band_rank.get(item.band, 0), item.score))
    def _invoke_arbitrated_route(self, ctx: _RouteContext, route: str) -> TaskResult | None:
        if route == "knowledge_qa":
            return self._route_knowledge_qa(ctx)
        if route == "source_analysis":
            return self._route_source_analysis(ctx)
        if route == "bug_request":
            return self._route_bug_request(ctx)
        if route == "direct_analysis":
            return self._route_direct_analysis(ctx)
        return None
    def _would_route_requirement_analysis(self, ctx: _RouteContext) -> bool:
        request = ctx.requirement_analysis_request
        if request is None or not request.triggered:
            return False
        if not self.config.requirement_analysis.enabled:
            return False
        return not (ctx.bug_request is not None and getattr(ctx.bug_request, "triggered", False))
    def _would_route_source_analysis(self, ctx: _RouteContext) -> bool:
        request = ctx.source_analysis_request
        if request is None or not request.triggered:
            return False
        if ctx.referenced_resources:
            return False
        if ctx.bug_request is not None and getattr(ctx.bug_request, "triggered", False):
            return False
        if ctx.direct_analysis_request is not None and getattr(ctx.direct_analysis_request, "triggered", False):
            return False
        return True
    def _would_route_signal_request(self, ctx: _RouteContext) -> bool:
        request = ctx.signal_request
        if request is None or not request.triggered:
            return False
        if (
            not request.signal
            and ctx.direct_analysis_request is not None
            and bool(ctx.direct_analysis_request.resources)
            and self._looks_like_direct_analysis_prompt(ctx.route_content)
        ):
            return False
        return True
    def _would_route_knowledge_qa(self, ctx: _RouteContext) -> bool:
        if not self.config.knowledge.enabled:
            return False
        if ctx.referenced_resources:
            return False
        return self.knowledge_service.should_handle(ctx.route_content)
    def _would_route_bug_request(self, ctx: _RouteContext) -> bool:
        return bool(ctx.bug_request is not None and getattr(ctx.bug_request, "triggered", False))
    def _would_route_direct_analysis(self, ctx: _RouteContext) -> bool:
        if ctx.direct_analysis_request is None or not getattr(ctx.direct_analysis_request, "triggered", False):
            return False
        return not self._should_defer_direct_analysis_to_intent(ctx)
    def _would_route_perception(self, ctx: _RouteContext) -> bool:
        return bool(ctx.perception_request is not None and getattr(ctx.perception_request, "triggered", False))
    def _would_route_claude_skill(self, ctx: _RouteContext) -> bool:
        skill_request = parse_claude_skill_request(
            ctx.route_content,
            trigger_prefixes=self.config.claude_agent.trigger_prefixes,
        )
        return bool(skill_request.triggered)
    def _route_app_server_investigation(self, ctx: _RouteContext) -> TaskResult | None:
        request = ctx.app_server_investigation_request
        if request is None or not request.triggered:
            return None
        # 续聊场景：裸触发「自主分析」没带 bug 链接也没带附件时，从回复上下文或
        # 本群最近一次分析继承 bug 链接，避免误判为「缺少输入」。
        if not request.bug_url and not request.resources:
            inherited_bug_url = self._contextual_app_server_bug_url(
                explicit_followup_context=ctx.followup_context,
                latest_chat_context=ctx.latest_chat_context,
            )
            if inherited_bug_url:
                request = request.__class__(
                    prompt=request.prompt,
                    bug_url=inherited_bug_url,
                    resources=request.resources,
                    raw_text=request.raw_text,
                    triggered=True,
                    error=request.error,
                    trigger_mode=request.trigger_mode,
                    trigger_term=request.trigger_term,
                )
        if not self.state_store.mark_seen(ctx.event):
            return TaskResult(True, f"duplicate event skipped: {ctx.event.event_id}", skipped=True)
        return self._run_app_server_investigation_request(
            ctx.event,
            request,
            ctx.route_content,
            root_message_id=(ctx.followup_context.root_message_id if ctx.followup_context is not None else None),
        )
    def _route_report_followup(self, ctx: _RouteContext) -> TaskResult | None:
        request = ctx.report_followup_request
        if ctx.followup_context is None or request is None or not request.triggered:
            return None
        if parse_followup_action(request.prompt) in {"retry", "continue"}:
            return None
        if not self.state_store.mark_seen(ctx.event):
            return TaskResult(True, f"duplicate event skipped: {ctx.event.event_id}", skipped=True)
        job = create_job_context(self.config.data_dir, ctx.event)
        html_path = job.output_dir / "diagram_report_followup.html"
        html_path.write_text(
            render_context_diagram_report(
                title="上下文图表报告",
                request_text=ctx.followup_context.request_text,
                followup_text=request.prompt,
                summary_text=ctx.followup_context.summary_text,
                report_excerpt=ctx.followup_context.report_excerpt,
                history=ctx.followup_context.history,
                diagram_kinds=request.diagram_kinds,
                source_mode=ctx.followup_context.source_mode,
                context_profile=ctx.followup_context.context_profile,
            ),
            encoding="utf-8",
        )
        result = TaskResult(
            success=True,
            message="已基于上一轮分析上下文生成 HTML 图表报告。",
            job_id=job.job_id,
            job_dir=job.job_dir,
            html_report=html_path,
            details={
                "mode": "diagram_report_followup",
                "source_mode": ctx.followup_context.source_mode,
                "context_profile": ctx.followup_context.context_profile,
                "classification_source": "deterministic_report_followup",
                "diagram_kinds": list(request.diagram_kinds),
                "followup_text": request.prompt,
                "user_request_text": ctx.followup_context.request_text,
                "files_to_send": [html_path],
            },
        )
        return self._deliver_result(
            ctx.event,
            result,
            request_text=ctx.followup_context.request_text,
            root_message_id=ctx.followup_context.root_message_id,
        )
    def _route_source_analysis(self, ctx: _RouteContext) -> TaskResult | None:
        request = ctx.source_analysis_request
        if request is None or not request.triggered:
            return None
        if ctx.referenced_resources:
            return None
        if ctx.bug_request is not None and getattr(ctx.bug_request, "triggered", False):
            return None
        if ctx.direct_analysis_request is not None and getattr(ctx.direct_analysis_request, "triggered", False):
            return None
        if not self.state_store.mark_seen(ctx.event):
            return TaskResult(True, f"duplicate event skipped: {ctx.event.event_id}", skipped=True)
        self._notify_progress(
            "source_analysis_request_received",
            "收到源码分析请求",
            event=ctx.event,
            mode="source_analysis",
            target=request.target,
        )
        def _source_progress(payload: dict[str, object]) -> None:
            details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
            progress_details = dict(details)
            progress_details.setdefault("mode", "source_analysis")
            self._notify_progress(
                str(payload.get("stage") or "source_analysis"),
                str(payload.get("message") or "源码分析"),
                event=ctx.event,
                **progress_details,
            )

        result = self.source_analysis_runner.run(
            request,
            ctx.event,
            progress_callback=_source_progress,
        )
        return self._deliver_result(ctx.event, result, request_text=request.raw_text or request.prompt)
    def _route_requirement_analysis(self, ctx: _RouteContext) -> TaskResult | None:
        request = ctx.requirement_analysis_request
        if request is None or not request.triggered:
            return None
        if not self.config.requirement_analysis.enabled:
            return None
        if ctx.bug_request is not None and getattr(ctx.bug_request, "triggered", False):
            return None
        if not self.state_store.mark_seen(ctx.event):
            return TaskResult(True, f"duplicate event skipped: {ctx.event.event_id}", skipped=True)
        self._notify_progress(
            "requirement_analysis_request_received",
            "收到需求源码分析请求",
            event=ctx.event,
            mode="requirement_analysis",
            work_item_id=request.workitem.work_item_id,
        )

        def _requirement_progress(payload: dict[str, object]) -> None:
            details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
            progress_details = dict(details)
            progress_details.setdefault("mode", "requirement_analysis")
            self._notify_progress(
                str(payload.get("stage") or "requirement_analysis"),
                str(payload.get("message") or "需求源码分析"),
                event=ctx.event,
                **progress_details,
            )

        result = self.requirement_analysis_runner.run(request, ctx.event, progress_callback=_requirement_progress)
        return self._deliver_result(ctx.event, result, request_text=request.raw_text or request.prompt)
    def _route_bug_followup(self, ctx: _RouteContext) -> TaskResult | None:
        if ctx.followup_context is None or "bug" not in str(ctx.followup_context.mode).casefold():
            return None
        if ctx.signal_request is None or not ctx.signal_request.triggered:
            return self._handle_followup(ctx.event, ctx.route_content, ctx.followup_context)
        if (
            self._bug_followup_requires_fresh_analysis(ctx.route_content)
            and self._is_bug_reanalysis_followup(ctx.route_content, ctx.followup_context)
        ):
            return self._handle_followup(ctx.event, ctx.route_content, ctx.followup_context)
        return None
    def _route_direct_analysis_followup(self, ctx: _RouteContext) -> TaskResult | None:
        if ctx.followup_context is None:
            return None
        return self._maybe_handle_direct_analysis_followup(ctx.event, ctx.followup_context, ctx.route_content)
    def _route_bug_intent(self, ctx: _RouteContext) -> TaskResult | None:
        if ctx.bug_request is not None and getattr(ctx.bug_request, "triggered", False):
            return self._handle_bug_intent(
                ctx.event,
                ctx.route_content,
                referenced_resources=ctx.referenced_resources,
            )
        return None
    def _route_addr2line_resolve(self, ctx: _RouteContext) -> TaskResult | None:
        request = ctx.addr2line_request
        if request is None:
            return None
        if not request.triggered:
            request = self._followup_addr2line_request(ctx)
            if request is None:
                return None
        if not self.state_store.mark_seen(ctx.event):
            return TaskResult(True, f"duplicate event skipped: {ctx.event.event_id}", skipped=True)
        return self._run_addr2line_resolve_request(ctx.event, request)
    def _route_rom_version_lookup(self, ctx: _RouteContext) -> TaskResult | None:
        request = ctx.rom_version_request
        if request is None:
            return None
        if not getattr(request, "triggered", False):
            request = self._followup_rom_lookup_request(ctx)
            if request is None:
                return None
        if not self.state_store.mark_seen(ctx.event):
            return TaskResult(True, f"duplicate event skipped: {ctx.event.event_id}", skipped=True)
        return self._run_rom_version_lookup_request(ctx.event, request)
    def _route_intent_router(self, ctx: _RouteContext) -> TaskResult | None:
        if not self.intent_runner.is_enabled():
            return None
        return self._handle_intent_routed_event(
            ctx.event,
            ctx.route_content,
            explicit_followup_context=ctx.followup_context,
            latest_chat_context=ctx.latest_chat_context,
            referenced_resources=ctx.referenced_resources,
        )
    def _route_general_followup(self, ctx: _RouteContext) -> TaskResult | None:
        if ctx.followup_context is not None:
            return self._handle_followup(ctx.event, ctx.route_content, ctx.followup_context)
        return None
    def _route_stale_light_interaction(self, ctx: _RouteContext) -> TaskResult | None:
        stale = self._stale_light_interaction_details(ctx)
        if stale is None:
            return None
        if not self.state_store.mark_seen(ctx.event):
            return TaskResult(True, f"duplicate event skipped: {ctx.event.event_id}", skipped=True)
        return TaskResult(
            success=True,
            message="stale lightweight event skipped",
            skipped=True,
            details={
                "mode": "stale_light_interaction",
                **stale,
            },
        )
    def _route_scene_signal(self, ctx: _RouteContext) -> TaskResult | None:
        if (
            ctx.signal_request is not None
            and ctx.signal_request.triggered
            and not ctx.signal_request.signal
            and looks_like_scene_signal_request(ctx.route_content)
        ):
            return self._handle_direct_analysis_intent(ctx.event, ctx.route_content, referenced_resources=ctx.referenced_resources)
        return None
    def _route_signal_request(self, ctx: _RouteContext) -> TaskResult | None:
        request = ctx.signal_request
        if request is None or not request.triggered:
            return None
        if (
            not request.signal
            and ctx.direct_analysis_request is not None
            and bool(ctx.direct_analysis_request.resources)
            and self._looks_like_direct_analysis_prompt(ctx.route_content)
        ):
            return None
        if not request.resources:
            inherited_resources = self._contextual_signal_resources(
                ctx.event,
                ctx.route_content,
                explicit_followup_context=ctx.followup_context,
                latest_chat_context=ctx.latest_chat_context,
            )
            if inherited_resources:
                request = SignalRequest(
                    signal=request.signal,
                    resources=self._merge_resources(request.resources, inherited_resources),
                    since=request.since,
                    raw_text=request.raw_text,
                    triggered=request.triggered,
                    error=request.error,
                )
        if not self.state_store.mark_seen(ctx.event):
            return TaskResult(True, f"duplicate event skipped: {ctx.event.event_id}", skipped=True)
        return self._run_signal_request(ctx.event, request, ctx.route_content)
    def _has_explicit_knowledge_trigger(self, text: str) -> bool:
        cleaned = (text or "").strip()
        for prefix in self.config.knowledge.trigger_prefixes:
            if prefix and cleaned.startswith(prefix):
                return True
        return "知识库" in cleaned or "查知识" in cleaned
    def _route_knowledge_qa(self, ctx: _RouteContext) -> TaskResult | None:
        if not self.config.knowledge.enabled:
            return None
        if not self.knowledge_service.should_handle(ctx.route_content):
            return None
        if ctx.referenced_resources:
            return None
        if not self.state_store.mark_seen(ctx.event):
            return TaskResult(True, f"duplicate event skipped: {ctx.event.event_id}", skipped=True)
        result = self.knowledge_service.answer(ctx.route_content)
        details = dict(result.details)
        details["delivery"] = "reply"
        details["conversation_root_message_id"] = ctx.event.root_id or ctx.event.message_id
        result.details = details
        return self._deliver_result(ctx.event, result, request_text=ctx.route_content)
    def _route_knowledge_probe(self, ctx: _RouteContext) -> TaskResult | None:
        knowledge_options = self.config.knowledge
        if not knowledge_options.enabled or not knowledge_options.auto_probe_enabled:
            return None
        if ctx.referenced_resources:
            return None
        if build_basic_chat_reply(ctx.route_content, command_prefixes=self.config.command_prefixes) is not None:
            return None
        if not _looks_like_knowledge_probe_question(
            ctx.route_content,
            intent_terms=knowledge_options.auto_probe_intent_terms,
        ):
            return None
        hits = self.knowledge_service.search(ctx.route_content, limit=1)
        usable_hits = [hit for hit in hits if hit.score >= knowledge_options.auto_probe_min_score]
        if not usable_hits:
            low_confidence_answer = getattr(self.knowledge_service, "answer_low_confidence_candidates", None)
            if callable(low_confidence_answer):
                result = low_confidence_answer(ctx.route_content)
                if result is not None:
                    if not self.state_store.mark_seen(ctx.event):
                        return TaskResult(True, f"duplicate event skipped: {ctx.event.event_id}", skipped=True)
                    details = dict(result.details)
                    details["delivery"] = "reply"
                    details["conversation_root_message_id"] = ctx.event.root_id or ctx.event.message_id
                    result.details = details
                    return self._deliver_result(ctx.event, result, request_text=ctx.route_content)
            if not _contains_any_term(ctx.route_content, knowledge_options.auto_probe_no_hit_terms):
                return None
            if not self.state_store.mark_seen(ctx.event):
                return TaskResult(True, f"duplicate event skipped: {ctx.event.event_id}", skipped=True)
            result = TaskResult(
                success=False,
                message=f"知识库未命中：{ctx.route_content}\n请补充更具体的关键词，或先同步/新增对应知识。",
                error_code="knowledge_probe_no_hits",
                details={"mode": "knowledge_probe", "knowledge_hits": []},
            )
            result.details["delivery"] = "reply"
            result.details["conversation_root_message_id"] = ctx.event.root_id or ctx.event.message_id
            return self._deliver_result(ctx.event, result, request_text=ctx.route_content)
        if not self.state_store.mark_seen(ctx.event):
            return TaskResult(True, f"duplicate event skipped: {ctx.event.event_id}", skipped=True)
        result = self.knowledge_service.answer(ctx.route_content)
        details = dict(result.details)
        details["delivery"] = "reply"
        details["conversation_root_message_id"] = ctx.event.root_id or ctx.event.message_id
        result.details = details
        return self._deliver_result(ctx.event, result, request_text=ctx.route_content)
    def _route_claude_skill(self, ctx: _RouteContext) -> TaskResult | None:
        skill_request = parse_claude_skill_request(
            ctx.route_content,
            trigger_prefixes=self.config.claude_agent.trigger_prefixes,
        )
        if not skill_request.triggered:
            return None
        if not self.state_store.mark_seen(ctx.event):
            return TaskResult(True, f"duplicate event skipped: {ctx.event.event_id}", skipped=True)
        result = self.claude_runner.run_skill_analysis(skill_request, event=ctx.event)
        return self._deliver_result(ctx.event, result, request_text=skill_request.raw_text or ctx.route_content)
    def _route_bug_request(self, ctx: _RouteContext) -> TaskResult | None:
        if ctx.bug_request is None or not getattr(ctx.bug_request, "triggered", False):
            return None
        if not self.state_store.mark_seen(ctx.event):
            return TaskResult(True, f"duplicate event skipped: {ctx.event.event_id}", skipped=True)
        pending = self._maybe_request_approval(
            ctx.event,
            operation_type="bug_analysis",
            description="Bug 分析",
            route_content=ctx.route_content,
            bug_url=ctx.bug_request.bug_url,
            prompt=ctx.bug_request.prompt,
            estimated_duration_seconds=self.config.bug_analysis.timeout_seconds,
        )
        if pending is not None:
            return pending
        return self._run_bug_request(ctx.event, ctx.bug_request, ctx.route_content)
    def _route_direct_analysis(self, ctx: _RouteContext) -> TaskResult | None:
        if ctx.direct_analysis_request is None or not getattr(ctx.direct_analysis_request, "triggered", False):
            return None
        if self._should_defer_direct_analysis_to_intent(ctx):
            return None
        return self._handle_direct_analysis_intent(
            ctx.event,
            ctx.route_content,
            referenced_resources=ctx.referenced_resources,
        )
    def _should_defer_direct_analysis_to_intent(self, ctx: _RouteContext) -> bool:
        if not self.intent_runner.is_enabled():
            return False
        if not ctx.referenced_resources:
            return False
        inline_request = parse_direct_analysis_request(ctx.route_content)
        if inline_request.triggered:
            return False
        if looks_like_direct_analysis_prompt(ctx.route_content, resources_present=False, bug_url_re=self.bug_url_re):
            return False
        return True
    def _route_perception(self, ctx: _RouteContext) -> TaskResult | None:
        if ctx.perception_request is None or not getattr(ctx.perception_request, "triggered", False):
            return None
        if not self.state_store.mark_seen(ctx.event):
            return TaskResult(True, f"duplicate event skipped: {ctx.event.event_id}", skipped=True)
        return self._run_perception_request(ctx.event, ctx.perception_request, ctx.route_content)
    def _route_followup_intent(self, ctx: _RouteContext) -> TaskResult | None:
        if not self._is_followup_intent(ctx.route_content):
            return None
        # When a message still carries file/folder resources but the prompt looks
        # like a correction/follow-up ("问题时间", "重新分析"), let the intent
        # router decide first instead of forcing the generic followup guard.
        if (
            ctx.followup_context is None
            and self.intent_runner.is_enabled()
            and ctx.direct_analysis_request is not None
            and bool(ctx.direct_analysis_request.resources)
        ):
            return None
        if ctx.followup_context is not None:
            return self._handle_followup(ctx.event, ctx.route_content, ctx.followup_context)
        if not self.state_store.mark_seen(ctx.event):
            return TaskResult(True, f"duplicate event skipped: {ctx.event.event_id}", skipped=True)
        result = self._missing_followup_reply_result(chat_type=ctx.event.chat_type)
        if not self.config.dry_run and ctx.event.chat_type in {"group", "p2p"}:
            self._send_result(ctx.event, result)
        return result
    def _route_omlx_chat(self, ctx: _RouteContext) -> TaskResult | None:
        chat_prompt = self._omlx_prompt(ctx.event, ctx.route_content)
        if chat_prompt is None:
            return None
        chat_reply = build_basic_chat_reply(ctx.route_content, command_prefixes=self.config.command_prefixes)
        if chat_reply is not None:
            return None  # Let _route_basic_chat handle it
        if not self.state_store.mark_seen(ctx.event):
            return TaskResult(True, f"duplicate event skipped: {ctx.event.event_id}", skipped=True)
        result = self.chat_client.reply(chat_prompt)
        return self._deliver_result(ctx.event, result, request_text=ctx.route_content)
    def _route_basic_chat(self, ctx: _RouteContext) -> TaskResult | None:
        chat_reply = build_basic_chat_reply(ctx.route_content, command_prefixes=self.config.command_prefixes)
        if chat_reply is None:
            return None
        if not self.state_store.mark_seen(ctx.event):
            return TaskResult(True, f"duplicate event skipped: {ctx.event.event_id}", skipped=True)
        result = TaskResult(
            success=True,
            message=chat_reply,
            details={"mode": "basic_chat"},
        )
        return self._deliver_result(ctx.event, result, request_text=ctx.route_content)
    def _omlx_prompt(self, event: LarkEvent, content: str | None = None) -> str | None:
        if not self.config.omlx_chat.enabled:
            return None
        prompt_source = event.content if content is None else content
        chat_command_prompt = self._chat_command_prompt(prompt_source)
        if chat_command_prompt is not None:
            return chat_command_prompt
        if should_use_omlx_chat(prompt_source, max_chars=self.config.omlx_chat.max_prompt_chars):
            return prompt_source
        stripped = prompt_source.strip()
        if event.chat_type != "p2p" or not stripped:
            return None
        if stripped.startswith("/"):
            return None
        return stripped
    def _chat_command_prompt(self, text: str) -> str | None:
        content = self._strip_group_chat_mention(text)
        if content is None:
            content = text.strip()
        prompt = extract_first_keyword_payload(content, CHAT_COMMAND_PREFIXES)
        if prompt is None or not prompt:
            return None
        return prompt
    def _stale_light_interaction_details(self, ctx: _RouteContext) -> dict[str, object] | None:
        options = self.config.event_consumer
        if not options.drop_stale_light_interactions:
            return None
        if ctx.followup_context is not None or ctx.referenced_resources:
            return None
        if self._has_formal_analysis_trigger(ctx):
            return None
        if build_basic_chat_reply(ctx.route_content, command_prefixes=self.config.command_prefixes) is None:
            if self._omlx_prompt(ctx.event, ctx.route_content) is None:
                return None
        event_time = self._event_created_at(ctx.event)
        ready_time = self._listener_ready_at()
        if event_time is None or ready_time is None:
            return None
        try:
            grace_seconds = max(0.0, float(options.stale_light_interaction_grace_seconds))
        except (TypeError, ValueError):
            return None
        stale_seconds = (ready_time - event_time).total_seconds()
        if stale_seconds <= grace_seconds:
            return None
        return {
            "event_create_time": event_time.isoformat(),
            "listener_ready_at": ready_time.isoformat(),
            "stale_seconds": round(stale_seconds, 3),
            "grace_seconds": grace_seconds,
        }
    def _has_formal_analysis_trigger(self, ctx: _RouteContext) -> bool:
        return any(
            bool(getattr(request, "triggered", False))
            for request in (
                ctx.bug_request,
                ctx.signal_request,
                ctx.direct_analysis_request,
                ctx.perception_request,
                ctx.rom_version_request,
                ctx.requirement_analysis_request,
            )
        )
