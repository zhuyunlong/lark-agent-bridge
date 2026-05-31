from __future__ import annotations

from ._shared import *  # noqa: F401,F403


class _ResultBugMixin:
    def _finish_progress_card(self, event: LarkEvent, result: TaskResult, *, session_id: str | None = None) -> bool:
        status = "completed" if result.success else "failed"
        note = self._progress_result_note(result) if result.success else (result.message[:MAX_ERROR_PREVIEW] if result.message else "分析失败")
        updated = self._update_progress_card(event, status=status, session_id=session_id, result=result, note=note)
        key = self._progress_card_state_key(event, session_id=session_id)
        if updated:
            self._progress_cards.pop(key, None)
        return updated

    def _prune_stale_progress_cards(self) -> None:
        """Remove progress card entries older than the TTL to prevent memory leaks.

        Uses ``last_active_at`` (refreshed on every update) rather than
        ``started_at`` so that long-running tasks that continuously report
        progress are never pruned prematurely.
        """
        now = datetime.now(timezone.utc)
        stale_keys = [
            key
            for key, state in self._progress_cards.items()
            if isinstance(state.get("last_active_at", state.get("started_at")), datetime)
            and (now - state.get("last_active_at", state["started_at"])).total_seconds() > self._progress_cards_max_age_seconds
        ]
        for key in stale_keys:
            self._progress_cards.pop(key, None)

    def _progress_result_note(self, result: TaskResult) -> str | None:
        message = (result.message or "").strip()
        if not message:
            return None
        summary = self._link_delivery_summary(message)
        if len(summary) > MAX_CARD_NOTE:
            summary = summary[:MAX_CARD_NOTE - 1].rstrip() + "…"
        return summary

    def _result_is_bug_report_mode(self, result: TaskResult | None) -> bool:
        if result is None:
            return False
        mode = str(result.details.get("mode") or "")
        return mode in {
            "bug_analysis",
            "bug_reanalysis",
            "bug_agent_followup",
            "bug_followup_existing_answer",
        }

    def _card_actions_enabled(self) -> bool:
        event_keys = getattr(self.config.event_consumer, "event_keys", None)
        if isinstance(event_keys, (list, tuple, set)):
            normalized = {str(item).strip() for item in event_keys}
            return CARD_ACTION_EVENT_KEY in normalized
        return str(self.config.event_consumer.event_key).strip() == CARD_ACTION_EVENT_KEY

    def _result_bug_skill_choices(self, result: TaskResult | None) -> list[dict[str, object]]:
        if result is None:
            return []
        if not result.success and not result.details.get("needs_user_direction"):
            return []
        if not (result.details.get("needs_user_direction") or self._result_is_bug_report_mode(result)):
            return []
        raw = result.details.get("supported_bug_skills")
        if not isinstance(raw, list):
            provider = getattr(self.bug_runner, "supported_primary_bug_skills", None)
            if callable(provider):
                try:
                    raw = provider()
                except Exception:
                    raw = []
        if not isinstance(raw, list):
            return []
        current_skill = str(result.details.get("analysis_skill") or "").strip()
        choices: list[dict[str, object]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name or name == "general":
                continue
            choices.append(
                {
                    "name": name,
                    "label": str(item.get("label") or name).strip(),
                    "description": str(item.get("description") or "").strip(),
                    "selected": bool(name and name == current_skill),
                }
            )
        return choices

    def _result_bug_skill_choice_note(self, result: TaskResult | None) -> str | None:
        if result is None:
            return None
        if result.details.get("needs_user_direction"):
            return "当前未自动命中专用 Skill。可以先补充分析要求，再从 Skill 按钮选一个方向继续。"
        skill_label = str(result.details.get("analysis_skill_label") or result.details.get("analysis_skill") or "").strip()
        if skill_label:
            return f"当前命中：{skill_label}。如果意图不正确，可以先补充要求，再从 Skill 按钮改选一个方向基于已有日志重新分析。"
        return "如果当前意图不正确，可以先补充要求，再从 Skill 按钮改选一个方向基于已有日志重新分析。"

    def _analysis_label_for_plan_kind(self, kind: str) -> str:
        labeler = getattr(self.bug_runner, "_analysis_label", None)
        if callable(labeler):
            try:
                return str(labeler(kind))
            except Exception:
                pass
        return kind or "通用问题分析"

    def _build_direct_analysis_clarification_options(self) -> list[dict[str, object]]:
        options: list[dict[str, object]] = []
        provider = getattr(self.bug_runner, "supported_primary_bug_skills", None)
        raw_skills = provider() if callable(provider) else []
        index = 1
        for item in raw_skills:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            label = str(item.get("label") or name).strip()
            if not name or not label or name == "general":
                continue
            options.append(
                {
                    "index": index,
                    "type": "skill",
                    "skill_name": name,
                    "label": label,
                }
            )
            index += 1
        options.append(
            {
                "index": index,
                "type": "source_analysis",
                "label": "直接源码分析",
            }
        )
        return options

    def _direct_analysis_needs_user_direction(self, prompt: str) -> bool:
        normalized = prompt.strip()
        if not normalized:
            return False
        has_time = bool(
            re.search(r"\b20\d{2}-\d{2}-\d{2}\b", normalized)
            or re.search(r"\b\d{1,2}:\d{2}\b", normalized)
            or "时间点" in normalized
            or "问题时间" in normalized
        )
        if not has_time:
            return False
        broad_terms = ("生命周期", "3D", "场景", "启动", "卡顿", "黑屏", "闪退")
        return any(term in normalized for term in broad_terms)

    def _render_direct_analysis_clarification_message(self, *, prompt: str, options: list[dict[str, object]]) -> str:
        lines = [
            "意图分析结果：当前已识别为文件型分析请求，但自动路由置信度不足，暂不直接执行。",
            "",
            f"原始请求：{prompt.strip()}",
            "",
            "请直接回复下面任一选项的序号，或直接回复对应文本：",
        ]
        for option in options:
            lines.append(f"{option['index']}. {option['label']}")
        lines.extend(
            [
                "",
                "也可以直接补充更明确的方向，例如：",
                "- 根据源码分析 SR 生命周期",
                "- 重点看 displayChanged",
                "- 按当前感知数据总结",
            ]
        )
        return "\n".join(lines)

    def _match_direct_analysis_clarification_option(
        self,
        followup_text: str,
        previous_session: dict[str, object],
    ) -> dict[str, object] | None:
        details = previous_session.get("details", {})
        if not isinstance(details, dict):
            return None
        raw_options = details.get("intent_options")
        if not isinstance(raw_options, list):
            return None
        normalized = followup_text.strip()
        if not normalized:
            return None
        for option in raw_options:
            if not isinstance(option, dict):
                continue
            index = str(option.get("index") or "").strip()
            label = str(option.get("label") or "").strip()
            skill_name = str(option.get("skill_name") or "").strip()
            if normalized == index:
                return option
            if label and normalized.casefold() == label.casefold():
                return option
            if skill_name and normalized.casefold() == skill_name.casefold():
                return option
        if normalized in {"直接源码分析", "源码分析"}:
            for option in raw_options:
                if isinstance(option, dict) and str(option.get("type") or "") == "source_analysis":
                    return option
        return None

    def _direct_analysis_preflight(self, request: DirectAnalysisRequest) -> _IntentPreflightDecision:
        prompt = request.prompt.strip()
        if not request.resources:
            return _IntentPreflightDecision(
                title="文件分析",
                intent_label="文件分析",
                confidence_label="高置信度",
                strategy_label="自动执行",
                reason="消息已包含可执行的文件分析资源。",
            )
        plans = None  # let classify_and_decide handle classification internally
        decision = self.bug_runner.classify_and_decide(
            request_text=request.raw_text or prompt,
            prompt_text=prompt,
        )
        selection = decision.selection
        source_decision = decision.source_decision
        first_plan = selection.plans[0] if selection.plans else BugAnalysisPlan(kind="general")
        domain_kind = getattr(source_decision, "domain_kind", first_plan.kind)
        # Use plans from the unified decision — do not re-construct plans here.
        resolved_plans = selection.plans
        # Extract source targets and mode from the decision for downstream context.
        _src_targets = getattr(source_decision, "source_targets", None)
        _src_mode = getattr(source_decision, "source_mode", "") or ""
        if source_decision.requested:
            if domain_kind not in {"general", "source_stage", "source_code_skill"}:
                plan_label = self._analysis_label_for_plan_kind(domain_kind)
                return _IntentPreflightDecision(
                    title="文件分析",
                    intent_label=f"{plan_label} + 源码分析",
                    confidence_label="高置信度",
                    strategy_label="自动执行",
                    reason=source_decision.reason or "已识别源码分析诉求，将在专用分析后继续执行源码分析。",
                    plans_override=resolved_plans,
                    classification_skill=selection.skill_name,
                    classification_reason=source_decision.reason or f"已命中专用分析方向：{plan_label}，并识别到源码分析诉求。",
                    source_targets=_src_targets,
                    source_mode=_src_mode,
                )
            return _IntentPreflightDecision(
                title="文件分析",
                intent_label="源码导向文件分析",
                confidence_label="高置信度",
                strategy_label="自动执行",
                reason=source_decision.reason or "已识别到源码分析诉求，将基于日志和源码证据直接执行文件分析。",
                plans_override=resolved_plans,
                classification_skill="source_analysis",
                classification_reason=source_decision.reason or "文件请求命中源码分析意图，优先按源码导向分析执行。",
                source_targets=_src_targets,
                source_mode=_src_mode,
            )
        if domain_kind not in {"general", "source_stage", "source_code_skill"}:
            plan_label = self._analysis_label_for_plan_kind(domain_kind)
            return _IntentPreflightDecision(
                title="文件分析",
                intent_label=f"文件型Bug分析（{plan_label}）",
                confidence_label="高置信度",
                strategy_label="自动执行",
                reason=f"已命中专用分析方向：{plan_label}。",
                plans_override=selection.plans,
                classification_skill=selection.skill_name,
                classification_reason=f"文件请求已稳定命中专用分析方向：{plan_label}。",
            )
        if not self._direct_analysis_needs_user_direction(prompt):
            return _IntentPreflightDecision(
                title="文件分析",
                intent_label="文件型分析",
                confidence_label="高置信度",
                strategy_label="自动执行",
                reason="消息已给出附件资源和可执行请求，按文件分析直接执行。",
                classification_skill="general",
                classification_reason="文件请求未命中专用方向，但范围不足以要求额外分诊，直接执行通用文件分析。",
            )
        options = self._build_direct_analysis_clarification_options()
        message = self._render_direct_analysis_clarification_message(prompt=prompt, options=options)
        clarification = TaskResult(
            success=True,
            skipped=True,
            message=message,
            details={
                "mode": "bug_clarification",
                "analysis_kind": "general",
                "analysis_kinds": ["general"],
                "analysis_skill": "general",
                "analysis_skill_label": "通用问题分诊",
                "classification_source": "preflight_rules",
                "classification_reason": "文件请求未命中稳定专用方向，需用户补充约束后继续。",
                "needs_user_direction": True,
                "supported_bug_skills": getattr(self.bug_runner, "supported_primary_bug_skills", lambda: [])(),
                "intent_options": options,
                "user_request_text": prompt,
            },
        )
        return _IntentPreflightDecision(
            title="文件分析分诊",
            intent_label="文件型分析待补充方向",
            confidence_label="低置信度",
            strategy_label="等待用户补充",
            reason="当前只有文件和泛化现象，未稳定命中专用分析方向。",
            execute=False,
            clarification_result=clarification,
        )

    def _send_intent_preflight_card(self, event: LarkEvent, decision: _IntentPreflightDecision) -> None:
        self.send_status_card(
            event,
            title=decision.title,
            status="queued",
            details={
                "意图分析": decision.intent_label,
                "置信度": decision.confidence_label,
                "执行策略": decision.strategy_label,
                "分类来源": decision.classification_source or "preflight_rules",
            },
            note=f"意图分析：{decision.reason}",
        )

    def _result_bug_agent_choices(self, result: TaskResult | None) -> list[dict[str, object]]:
        if result is None or not result.success or not self._result_is_bug_report_mode(result):
            return []
        current_provider = self._normalize_bug_agent_provider(
            str(result.details.get("agent_summary_provider") or result.details.get("provider") or "")
        )
        choices: list[dict[str, object]] = []
        for provider in ("codex", "claude", "omlx"):
            choice = self._bug_agent_choice(provider)
            if choice is None:
                continue
            if provider == current_provider:
                continue
            choices.append(choice)
        return choices

    def _bug_agent_choice(self, provider: str) -> dict[str, object] | None:
        normalized = self._normalize_bug_agent_provider(provider)
        if normalized == "codex":
            return {
                "provider": "codex",
                "label": "换 Codex 重分析",
                "confirm_label": "Codex Agent",
            }
        if normalized == "claude":
            if not self.config.claude_agent.enabled:
                return None
            return {
                "provider": "claude",
                "label": "换 Claude 重分析",
                "confirm_label": "Claude Agent",
            }
        if normalized == "omlx":
            if not self.config.omlx_chat.enabled:
                return None
            return {
                "provider": "omlx",
                "label": "用 OMLX 本地模型",
                "confirm_label": "OMLX 本地模型",
            }
        return None

    def _normalize_bug_agent_provider(self, provider: str) -> str:
        normalized = (provider or "").strip().casefold()
        if normalized in {"claude-code", "claude_code"}:
            return "claude"
        if normalized in {"codex", "claude", "omlx"}:
            return normalized
        return ""

    def _build_progress_card(
        self,
        event: LarkEvent,
        *,
        key: str,
        status: str,
        result: TaskResult | None = None,
        note: str | None = None,
    ) -> dict[str, object]:
        card_state = self._progress_cards.get(key, {})
        details = dict(card_state.get("details") if isinstance(card_state.get("details"), dict) else {})
        if result is not None:
            if status in {"completed", "failed"}:
                details.pop("当前阶段", None)
            mode = str(result.details.get("mode") or "")
            if mode:
                details["分析类型"] = self._progress_mode_label(mode)
            if result.job_id:
                details.setdefault("任务ID", result.job_id[:20])
            skill_label = str(result.details.get("analysis_skill_label") or result.details.get("analysis_skill") or "").strip()
            if skill_label:
                details.setdefault("命中 Skill", skill_label)
            classification_source = str(result.details.get("classification_source") or "").strip()
            if classification_source:
                details.setdefault("分类来源", classification_source)
            agent_model = str(result.details.get("agent_summary_model") or "").strip()
            if agent_model:
                details.setdefault("Agent 模型", agent_model)
        report_url = ""
        if result is not None:
            report_url = str(result.details.get("published_report_url") or "").strip()
        root_message_id = ""
        job_id = ""
        show_followup_actions = False
        if result is not None:
            root_message_id = str(
                result.details.get("conversation_root_message_id")
                or event.root_id
                or event.message_id
                or key
                or ""
            ).strip()
            job_id = str(result.job_id or result.details.get("job_id") or "").strip()
            show_followup_actions = bool(self._card_actions_enabled() and result.success and report_url and root_message_id)
        bug_skill_choices = (
            self._result_bug_skill_choices(result) if result is not None and self._card_actions_enabled() else []
        )
        session = self.activity_store.get_session(key) or self.activity_store.get_session(event.message_id) or {}
        progress = session.get("progress") if isinstance(session, dict) else []
        if not isinstance(progress, list):
            progress = []
        return build_status_card(
            title=str(card_state.get("title") or "分析进度"),
            status=status,
            details={str(k): str(v) for k, v in details.items() if str(v)},
            progress=progress,
            elapsed_seconds=self._progress_elapsed_seconds(card_state, result=result),
            token_usage=self._progress_token_usage(result),
            report_url=report_url or None,
            live_url=self._progress_live_url(key),
            note=note,
            job_id=job_id or None,
            root_message_id=root_message_id or None,
            show_followup_actions=show_followup_actions,
            bug_skill_choices=bug_skill_choices,
            bug_skill_choice_note=self._result_bug_skill_choice_note(result) if self._card_actions_enabled() else None,
            bug_agent_choices=self._result_bug_agent_choices(result) if self._card_actions_enabled() else [],
        )

    def _progress_live_url(self, session_id: str) -> str | None:
        if not self.config.report_server.enabled or not session_id:
            return None
        base = resolve_public_base_url(
            self.config.report_server.public_base_url,
            port=self.config.report_server.port,
            bind_host=self.config.report_server.bind_host,
        )
        parts = urlsplit(base)
        query = f"session={quote(session_id, safe='')}"
        return urlunsplit((parts.scheme, parts.netloc, "/sessions", query, ""))

    def _progress_elapsed_seconds(self, card_state: dict[str, object], *, result: TaskResult | None = None) -> float | None:
        if result is not None and isinstance(result.duration_seconds, (int, float)):
            return float(result.duration_seconds)
        started_at = card_state.get("started_at")
        if isinstance(started_at, datetime):
            return max(0.0, (datetime.now(timezone.utc) - started_at).total_seconds())
        return None

    def _progress_token_usage(self, result: TaskResult | None) -> dict[str, int] | None:
        if result is None:
            return None
        _prefix, usage = extract_first_prefixed_token_usage(result.details, ("agent_summary_", "app_server_"))
        return usage or None

    def _progress_mode_label(self, mode: str) -> str:
        labels = {
            "bug_analysis": "Bug 分析",
            "bug_clarification": "Bug 分析分诊",
            "bug_reanalysis": "Bug 重新分析",
            "bug_agent_followup": "Bug 追问",
            "direct_analysis": "直传文件分析",
            "app_server_investigation": "AI 自主分析",
            "source_analysis": "源码分析",
            "diagram_report_followup": "图表报告",
            "perception_summary": "感知数据总结",
            "signal_lifecycle": "信号生命周期",
            "claude_skill": "Claude Code 分析",
        }
        return labels.get(mode, mode)

    def _card_message_id_from_result(self, result) -> str:
        for text in (getattr(result, "stdout", ""), getattr(result, "stderr", "")):
            message_id = self._extract_card_message_id(text)
            if message_id:
                return message_id
        return ""

    def _remember_delivery_alias_from_result(self, result, *, root_message_id: str | None) -> None:
        message_id = self._card_message_id_from_result(result)
        self._remember_conversation_alias(message_id, root_message_id)

    def _remember_conversation_alias(self, alias_message_id: str, root_message_id: str | None) -> None:
        alias = str(alias_message_id or "").strip()
        root = str(root_message_id or "").strip()
        if not alias or not root or alias == root:
            return
        self.conversation_store.remember_alias(alias_message_id=alias, root_message_id=root)

    def _extract_card_message_id(self, text: str) -> str:
        if not text.strip():
            return ""
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\b(?:open_)?message_id\b[\"'=:\s]+([A-Za-z0-9_\\-]+)", text)
            return match.group(1) if match else ""
        return self._find_message_id(payload)

    def _find_message_id(self, value) -> str:
        if isinstance(value, dict):
            for key in ("message_id", "open_message_id"):
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    return candidate.strip()
            for nested in value.values():
                found = self._find_message_id(nested)
                if found:
                    return found
        if isinstance(value, list):
            for item in value:
                found = self._find_message_id(item)
                if found:
                    return found
        return ""

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

    def _run_bug_request(self, event: LarkEvent, bug_request, route_content: str) -> TaskResult:
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
        )
        result = self.bug_runner.run_bug_analysis(
            bug_request,
            event=event,
            progress_callback=self._event_progress_callback(event),
        )
        self._ensure_result_bug_url(result, bug_request.bug_url)
        return self._deliver_result(event, result, request_text=bug_request.raw_text or route_content)

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
        navigation_version = self._navigation_version_from_lookup_result(lookup_result)
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
        )
        return Addr2LineRequest(
            addr_text=request.addr_text,
            resources=request.resources,
            raw_text=request.raw_text,
            rom_version=request.rom_version,
            napa_version=request.napa_version,
            apk_version=navigation_version,
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

    def _handle_approval_action(self, action_event: CardActionEvent, *, approved: bool) -> TaskResult:
        if not action_event.request_id:
            return TaskResult(
                success=False,
                message="卡片回调缺少审批 request_id。",
                error_code="missing_approval_request_id",
                details={"mode": "card_action", "action": action_event.action},
            )
        pending = self.approval_store.get_pending(action_event.request_id)
        if pending is None:
            return TaskResult(
                success=False,
                message="审批请求已过期或不存在。",
                error_code="approval_not_available",
                details={"mode": "approval", "approval_request_id": action_event.request_id},
            )
        decision = self.approval_store.resolve(action_event.request_id, approved=approved)
        if decision.status == ApprovalStatus.REJECTED:
            return TaskResult(
                success=False,
                message="已取消执行。",
                error_code="approval_rejected",
                details={"mode": "approval", "approval_request_id": action_event.request_id},
            )
        if not decision.can_proceed:
            return TaskResult(
                success=False,
                message="审批请求已过期或不存在。",
                error_code="approval_not_available",
                details={"mode": "approval", "approval_request_id": action_event.request_id},
            )
        return self._execute_approved_operation(pending.operation)

    def _execute_approved_operation(self, operation) -> TaskResult:
        metadata = operation.metadata or {}
        event_payload = metadata.get("event_payload")
        if not isinstance(event_payload, dict):
            return TaskResult(
                success=False,
                message="审批请求缺少原始事件信息，无法继续执行。",
                error_code="approval_missing_event_payload",
                details={"mode": "approval", "operation_type": operation.operation_type},
            )
        event = LarkEvent.from_dict(event_payload)
        route_content = str(metadata.get("route_content") or "")
        self.activity_store.record_event(event, content=route_content)
        if operation.operation_type == "bug_analysis":
            request = parse_bug_request(route_content, bug_url_re=self.bug_url_re)
            result = self._run_bug_request(event, request, route_content)
        elif operation.operation_type == "direct_analysis":
            referenced_resources = self._fetch_referenced_message_resources(event, route_content=route_content)
            request = self._build_direct_analysis_request(route_content, referenced_resources, event=event)
            result = self._run_direct_analysis_request(event, request, route_content)
        elif operation.operation_type == "reanalyze":
            result = self._execute_approved_reanalysis(event, route_content, metadata)
        else:
            result = TaskResult(
                success=False,
                message=f"审批已通过，但暂不支持执行操作类型：{operation.operation_type}",
                error_code="unsupported_approved_operation",
                details={"mode": "approval", "operation_type": operation.operation_type},
            )
        self.activity_store.record_result(event, result)
        return result

    def _execute_approved_reanalysis(self, event: LarkEvent, route_content: str, metadata: dict[str, object]) -> TaskResult:
        root_message_id = str(metadata.get("root_message_id") or "")
        followup_context = self.conversation_store.lookup(root_message_id) if root_message_id else self._resolve_followup_context(event)
        if followup_context is None:
            return self._missing_followup_reply_result(mode="approval", chat_type=event.chat_type)
        previous_session = self.activity_store.get_session(followup_context.root_message_id) or {}
        if not previous_session and metadata.get("job_id"):
            previous_session = self.activity_store.find_session_by_job_id(str(metadata.get("job_id") or "")) or {}
        selected_skill = str(metadata.get("selected_skill") or "").strip()
        selected_agent_provider = self._normalize_bug_agent_provider(
            str(metadata.get("selected_agent_provider") or metadata.get("agent_provider") or "")
        )
        if (metadata.get("selected_agent_provider") or metadata.get("agent_provider")) and not selected_agent_provider:
            return TaskResult(
                success=False,
                message="指定的 Agent 无效或不支持，无法重新分析。",
                error_code="invalid_bug_agent_selection",
                details={
                    "mode": "approval",
                    "agent_provider": str(
                        metadata.get("selected_agent_provider") or metadata.get("agent_provider") or ""
                    ),
                },
            )
        selected_skill_decision = (
            self.bug_runner.selection_for_skill_name(
                selected_skill,
                source="user_selected_card",
                reason="用户在分诊卡片中选择专用 skill。",
            )
            if selected_skill
            else None
        )
        if selected_skill_decision is not None:
            reanalysis_decision = BugFollowupSelection(
                should_reanalyze=True,
                force_rerun=True,
                plans=selected_skill_decision.plans,
                skill_name=selected_skill_decision.skill_name,
                skill_label=selected_skill_decision.skill_label,
                source=selected_skill_decision.source,
                reason=selected_skill_decision.reason,
                provider=selected_skill_decision.provider,
            )
        else:
            reanalysis_decision = self._bug_reanalysis_decision(route_content, followup_context)
        result = self.bug_runner.run_bug_reanalysis(
            followup_text=route_content,
            previous_context=followup_context,
            previous_session=previous_session,
            event=event,
            progress_callback=self._event_progress_callback(event, session_id=followup_context.root_message_id),
            force_rerun=reanalysis_decision.force_rerun,
            plans_override=reanalysis_decision.plans,
            classification_skill=reanalysis_decision.skill_name,
            classification_source=reanalysis_decision.source,
            classification_reason=reanalysis_decision.reason,
            classification_provider=reanalysis_decision.provider,
            agent_provider_override=selected_agent_provider,
            local_log_resources=self._authorized_local_download_resources(event, route_content),
            bridge_session_id=followup_context.root_message_id,
        )
        self._ensure_result_bug_url(result, self._bug_url_from_session(previous_session))
        return self._deliver_result(
            event,
            result,
            request_text=self._bug_request_text_for_followup_context(
                followup_context,
                previous_session=previous_session,
            ),
            root_message_id=followup_context.root_message_id,
        )

    def _card_followup_context(self, action_event: CardActionEvent):
        root_message_id = action_event.root_message_id or action_event.message_id
        followup_context = self.conversation_store.lookup(root_message_id) if root_message_id else None
        previous_session: dict[str, object] = {}
        if followup_context is None and action_event.job_id:
            previous_session = self.activity_store.find_session_by_job_id(action_event.job_id) or {}
            root_message_id = str(previous_session.get("session_id") or root_message_id or "")
            followup_context = self.conversation_store.lookup(root_message_id) if root_message_id else None
        if followup_context is not None and not previous_session:
            previous_session = self.activity_store.get_session(followup_context.root_message_id) or {}
        if followup_context is not None and not previous_session and action_event.job_id:
            previous_session = self.activity_store.find_session_by_job_id(action_event.job_id) or {}
        return root_message_id, followup_context, previous_session

    def _missing_card_followup_prompt_result(
        self,
        action_event: CardActionEvent,
        *,
        root_message_id: str = "",
        fallback_chat_id: str = "",
        fallback_chat_type: str = "",
    ) -> TaskResult:
        event = self._event_from_card_action(
            action_event,
            root_message_id=root_message_id,
            fallback_chat_id=fallback_chat_id,
            fallback_chat_type=fallback_chat_type,
        )
        message = (
            "请先在卡片输入框填写追问/重跑提示词，再点击按钮；"
            "例如：基于当前报告回答“生命周期卡在哪里”，或“基于已有日志重新分析 3D 生命周期”。"
        )
        if not self.config.dry_run and event.chat_type in {"group", "p2p"}:
            if event.message_id:
                self.lark_client.reply(event.message_id, self._reply_payload(event, message))
            else:
                self.lark_client.send_response(event, message)
        return TaskResult(
            success=False,
            message=message,
            error_code="missing_card_followup_prompt",
            details={"mode": "card_action", "action": action_event.action},
        )

    def _card_action_chat_id(self, action_event: CardActionEvent, followup_context: ConversationContext | None) -> str:
        return str(action_event.chat_id or getattr(followup_context, "chat_id", "") or "").strip()

    def _handle_answer_from_report_action(self, action_event: CardActionEvent) -> TaskResult:
        _root_message_id, followup_context, previous_session = self._card_followup_context(action_event)
        if followup_context is None:
            return TaskResult(
                success=False,
                message="找不到可回答的历史上下文，请回复原分析消息后再重试。",
                error_code="missing_followup_context",
                details={"mode": "card_action", "action": "answer_from_report"},
            )
        chat_id = self._card_action_chat_id(action_event, followup_context)
        if not chat_id:
            return TaskResult(
                success=False,
                message="卡片回调缺少 chat_id，无法基于报告回答。",
                error_code="invalid_card_action_context",
                details={"mode": "card_action", "action": "answer_from_report"},
            )
        route_content = action_event.followup_text.strip()
        if not route_content:
            return self._missing_card_followup_prompt_result(
                action_event,
                root_message_id=followup_context.root_message_id,
                fallback_chat_id=chat_id,
                fallback_chat_type=str(previous_session.get("chat_type") or ""),
            )
        event = self._event_from_card_action(
            action_event,
            root_message_id=followup_context.root_message_id,
            fallback_chat_id=chat_id,
            fallback_chat_type=str(previous_session.get("chat_type") or ""),
        )
        if not self.state_store.mark_seen(event):
            return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)
        result = self._answer_bug_followup_from_existing(route_content, followup_context, min_confidence=0.0)
        if result is None:
            result = TaskResult(
                success=False,
                message="当前报告/上下文无法直接回答这个问题，请点击“基于已有日志重新分析”，或直接回复新的提示词。",
                error_code="existing_context_answer_unavailable",
                details={
                    "mode": "bug_followup_existing_answer",
                    "answer_source": "existing_context",
                    "followup_text": route_content,
                    "delivery": "reply",
                },
            )
        return self._finalize_followup_reply(event, result, followup_context, route_content)

    def _handle_continue_agent_action(self, action_event: CardActionEvent) -> TaskResult:
        _root_message_id, followup_context, previous_session = self._card_followup_context(action_event)
        if followup_context is None:
            return TaskResult(
                success=False,
                message="找不到可继续的历史上下文，请回复原分析消息后再重试。",
                error_code="missing_followup_context",
                details={"mode": "card_action", "action": "continue_agent"},
            )
        chat_id = self._card_action_chat_id(action_event, followup_context)
        if not chat_id:
            return TaskResult(
                success=False,
                message="卡片回调缺少 chat_id，无法继续原 Agent。",
                error_code="invalid_card_action_context",
                details={"mode": "card_action", "action": "continue_agent"},
            )
        route_content = action_event.followup_text.strip()
        if not route_content:
            return self._missing_card_followup_prompt_result(
                action_event,
                root_message_id=followup_context.root_message_id,
                fallback_chat_id=chat_id,
                fallback_chat_type=str(previous_session.get("chat_type") or ""),
            )
        event = self._event_from_card_action(
            action_event,
            root_message_id=followup_context.root_message_id,
            fallback_chat_id=chat_id,
            fallback_chat_type=str(previous_session.get("chat_type") or ""),
        )
        if not self.state_store.mark_seen(event):
            return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)
        result = self.bug_runner.run_bug_agent_followup(
            followup_text=route_content,
            previous_context=followup_context,
            previous_session=previous_session,
            event=event,
            progress_callback=self._event_progress_callback(event, session_id=followup_context.root_message_id),
            resume_agent_session=True,
            bridge_session_id=followup_context.root_message_id,
        )
        return self._finalize_followup_reply(event, result, followup_context, route_content)

    def _handle_select_bug_skill_action(self, action_event: CardActionEvent) -> TaskResult:
        selected = self.bug_runner.selection_for_skill_name(
            action_event.skill_name,
            source="user_selected_card",
            reason="用户在卡片中选择专用 skill。",
        )
        if selected is None:
            return TaskResult(
                success=False,
                message="卡片回调中的 skill 无效或不支持，请重新选择。",
                error_code="invalid_bug_skill_selection",
                details={
                    "mode": "card_action",
                    "action": "select_bug_skill",
                    "skill_name": action_event.skill_name,
                },
            )
        _root_message_id, followup_context, previous_session = self._card_followup_context(action_event)
        if followup_context is None:
            return TaskResult(
                success=False,
                message="找不到可继续的 bug 分诊上下文，请回复原分析消息后再重试。",
                error_code="missing_followup_context",
                details={"mode": "card_action", "action": "select_bug_skill"},
            )
        chat_id = self._card_action_chat_id(action_event, followup_context)
        if not chat_id:
            return TaskResult(
                success=False,
                message="卡片回调缺少 chat_id，无法按所选 skill 继续分析。",
                error_code="invalid_card_action_context",
                details={"mode": "card_action", "action": "select_bug_skill"},
            )
        if not previous_session:
            previous_session = self.activity_store.get_session(followup_context.root_message_id) or {}
        prompt = action_event.followup_text.strip()
        route_content = f"按专用 skill「{selected.skill_label}」继续分析"
        if prompt:
            route_content = f"{route_content}：{prompt}"
        event = self._event_from_card_action(
            action_event,
            root_message_id=followup_context.root_message_id,
            fallback_chat_id=chat_id,
            fallback_chat_type=str(previous_session.get("chat_type") or ""),
        )
        if not self.state_store.mark_seen(event):
            return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)
        pending = self._maybe_request_approval(
            event,
            operation_type="reanalyze",
            description=f"按 {selected.skill_label} 继续分析",
            route_content=route_content,
            root_message_id=followup_context.root_message_id,
            job_id=action_event.job_id,
            selected_skill=selected.skill_name,
            retry_count=1,
            estimated_duration_seconds=self.config.bug_analysis.timeout_seconds,
        )
        if pending is not None:
            return pending
        return self._execute_approved_reanalysis(
            event,
            route_content,
            {
                "root_message_id": followup_context.root_message_id,
                "job_id": action_event.job_id,
                "selected_skill": selected.skill_name,
            },
        )

    def _handle_select_bug_agent_action(self, action_event: CardActionEvent) -> TaskResult:
        provider = self._normalize_bug_agent_provider(action_event.agent_provider)
        choice = self._bug_agent_choice(provider)
        if choice is None:
            return TaskResult(
                success=False,
                message="卡片回调中的 Agent 无效或当前未启用，请重新选择。",
                error_code="invalid_bug_agent_selection",
                details={
                    "mode": "card_action",
                    "action": "select_bug_agent",
                    "agent_provider": action_event.agent_provider,
                },
            )
        _root_message_id, followup_context, previous_session = self._card_followup_context(action_event)
        if followup_context is None:
            return TaskResult(
                success=False,
                message="找不到可重新分析的 bug 上下文，请回复原分析消息后再重试。",
                error_code="missing_reanalysis_context",
                details={"mode": "card_action", "action": "select_bug_agent"},
            )
        chat_id = self._card_action_chat_id(action_event, followup_context)
        if not chat_id:
            return TaskResult(
                success=False,
                message="卡片回调缺少 chat_id，无法确认换 Agent 重分析。",
                error_code="invalid_card_action_context",
                details={"mode": "card_action", "action": "select_bug_agent"},
            )
        event = self._event_from_card_action(
            action_event,
            root_message_id=followup_context.root_message_id,
            fallback_chat_id=chat_id,
            fallback_chat_type=str(previous_session.get("chat_type") or ""),
        )
        if not self.state_store.mark_seen(event):
            return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)
        agent_label = str(choice.get("confirm_label") or choice.get("label") or provider)
        card = build_agent_reanalysis_confirmation_card(
            agent_label=agent_label,
            agent_provider=provider,
            job_id=action_event.job_id,
            root_message_id=followup_context.root_message_id,
            followup_text=action_event.followup_text,
        )
        if not self.config.dry_run and event.chat_type in {"group", "p2p"}:
            card_json = card_to_json(card)
            if event.message_id:
                self.lark_client.reply_card(event.message_id, card_json)
            else:
                self.lark_client.send_card_response(event, card_json)
        return TaskResult(
            success=True,
            message=f"已选择 {agent_label}，等待确认后重新分析。",
            details={
                "mode": "card_action",
                "action": "select_bug_agent",
                "agent_provider": provider,
                "root_message_id": followup_context.root_message_id,
                "job_id": action_event.job_id,
            },
        )

    def _handle_confirm_bug_agent_reanalysis_action(self, action_event: CardActionEvent) -> TaskResult:
        provider = self._normalize_bug_agent_provider(action_event.agent_provider)
        choice = self._bug_agent_choice(provider)
        if choice is None:
            return TaskResult(
                success=False,
                message="确认卡中的 Agent 无效或当前未启用，请重新从结果卡片选择。",
                error_code="invalid_bug_agent_selection",
                details={
                    "mode": "card_action",
                    "action": "confirm_bug_agent_reanalysis",
                    "agent_provider": action_event.agent_provider,
                },
            )
        _root_message_id, followup_context, previous_session = self._card_followup_context(action_event)
        if followup_context is None:
            return TaskResult(
                success=False,
                message="找不到可重新分析的 bug 上下文，请回复原分析消息后再重试。",
                error_code="missing_reanalysis_context",
                details={"mode": "card_action", "action": "confirm_bug_agent_reanalysis"},
            )
        chat_id = self._card_action_chat_id(action_event, followup_context)
        if not chat_id:
            return TaskResult(
                success=False,
                message="卡片回调缺少 chat_id，无法换 Agent 重分析。",
                error_code="invalid_card_action_context",
                details={"mode": "card_action", "action": "confirm_bug_agent_reanalysis"},
            )
        event = self._event_from_card_action(
            action_event,
            root_message_id=followup_context.root_message_id,
            fallback_chat_id=chat_id,
            fallback_chat_type=str(previous_session.get("chat_type") or ""),
        )
        if not self.state_store.mark_seen(event):
            return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)
        agent_label = str(choice.get("confirm_label") or choice.get("label") or provider)
        route_content = action_event.followup_text.strip()
        if not route_content:
            route_content = f"换用 {agent_label} 基于已有日志/报告重新分析"
        self._notify_progress(
            "bug_agent_switch_confirmed",
            "用户确认换 Agent 重新分析",
            event=event,
            session_id=followup_context.root_message_id,
            agent_provider=provider,
            agent_label=agent_label,
            job_id=action_event.job_id,
        )
        return self._execute_approved_reanalysis(
            event,
            route_content,
            {
                "root_message_id": followup_context.root_message_id,
                "job_id": action_event.job_id,
                "selected_agent_provider": provider,
            },
        )

    def _handle_cancel_bug_agent_reanalysis_action(self, action_event: CardActionEvent) -> TaskResult:
        _root_message_id, followup_context, previous_session = self._card_followup_context(action_event)
        chat_id = self._card_action_chat_id(action_event, followup_context)
        event = self._event_from_card_action(
            action_event,
            root_message_id=str(getattr(followup_context, "root_message_id", "") or action_event.root_message_id),
            fallback_chat_id=chat_id,
            fallback_chat_type=str(previous_session.get("chat_type") or ""),
        )
        if not self.state_store.mark_seen(event):
            return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)
        message = "已取消换 Agent 重分析。"
        if not self.config.dry_run and event.chat_type in {"group", "p2p"}:
            if event.message_id:
                self.lark_client.reply(event.message_id, self._reply_payload(event, message))
            elif event.chat_id:
                self.lark_client.send_response(event, message)
        return TaskResult(
            success=True,
            message=message,
            details={
                "mode": "card_action",
                "action": "cancel_bug_agent_reanalysis",
                "agent_provider": self._normalize_bug_agent_provider(action_event.agent_provider),
            },
        )

    def _handle_feedback_action(self, action_event: CardActionEvent) -> TaskResult:
        root_message_id, followup_context, previous_session = self._card_followup_context(action_event)
        chat_id = self._card_action_chat_id(action_event, followup_context)
        if not chat_id:
            return TaskResult(
                success=False,
                message="卡片回调缺少 chat_id，无法记录反馈。",
                error_code="invalid_card_action_context",
                details={"mode": "card_action", "action": action_event.action},
            )
        event = self._event_from_card_action(
            action_event,
            root_message_id=str(getattr(followup_context, "root_message_id", "") or root_message_id),
            fallback_chat_id=chat_id,
            fallback_chat_type=str(previous_session.get("chat_type") or ""),
        )
        if not self.state_store.mark_seen(event):
            return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)
        helpful = action_event.action == "feedback_helpful"
        message = "已记录反馈：有用。" if helpful else "已记录反馈：不准。你可以点“基于已有日志重新分析”，或直接回复新的提示词。"
        self._notify_progress(
            "followup_feedback_recorded",
            "记录追问结果反馈",
            event=event,
            session_id=str(getattr(followup_context, "root_message_id", "") or root_message_id or ""),
            feedback="helpful" if helpful else "unhelpful",
            job_id=action_event.job_id,
            root_message_id=str(getattr(followup_context, "root_message_id", "") or root_message_id or ""),
            followup_text=action_event.followup_text,
            card_message_id=event.message_id,
        )
        if not self.config.dry_run and event.chat_type in {"group", "p2p"}:
            if event.message_id:
                self.lark_client.reply(event.message_id, self._reply_payload(event, message))
            else:
                self.lark_client.send_response(event, message)
        return TaskResult(
            success=True,
            message=message,
            details={
                "mode": "card_action",
                "action": action_event.action,
                "feedback": "helpful" if helpful else "unhelpful",
                "job_id": action_event.job_id,
                "root_message_id": str(getattr(followup_context, "root_message_id", "") or root_message_id or ""),
            },
        )
