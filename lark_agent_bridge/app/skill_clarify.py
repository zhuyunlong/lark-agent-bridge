from __future__ import annotations

from datetime import datetime, timezone
import json
import re
from urllib.parse import quote, urlsplit, urlunsplit

from ._shared import *  # noqa: F401,F403


class _SkillClarifyMixin:
    """Bug Skill/意图澄清：选项构建、匹配与确认执行（与 ResultBugMixin 共享 self 状态）。"""

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
        current_skill = str(result.details.get("analysis_skill") or "").strip()
        if str(result.details.get("mode") or "").strip() == "bug_skill_confirmation":
            raw_options = result.details.get("intent_options")
            if isinstance(raw_options, list):
                choices: list[dict[str, object]] = []
                for option in raw_options:
                    if not isinstance(option, dict) or str(option.get("type") or "").strip() != "skill":
                        continue
                    name = str(option.get("skill_name") or "").strip()
                    label = str(option.get("label") or name).strip()
                    if not name or not label:
                        continue
                    choices.append(
                        {
                            "name": name,
                            "label": label,
                            "description": str(option.get("description") or "").strip(),
                            "selected": bool(name == current_skill),
                        }
                    )
                if choices:
                    return choices
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
        mode = str(result.details.get("mode") or "").strip()
        if mode == "bug_skill_confirmation":
            return "当前 Skill 分类存在冲突。可以直接回复序号/方向，也可以点击下面的 Skill 按钮继续；若要两个方向都跑，请文本回复“3”或“都跑”。"
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
    def _match_bug_skill_confirmation_option(
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
        folded = normalized.casefold()
        for option in raw_options:
            if not isinstance(option, dict):
                continue
            candidates = [
                str(option.get("index") or "").strip(),
                str(option.get("label") or "").strip(),
                str(option.get("skill_name") or "").strip(),
            ]
            aliases = option.get("aliases")
            if isinstance(aliases, list):
                candidates.extend(str(item or "").strip() for item in aliases)
            if any(candidate and folded == candidate.casefold() for candidate in candidates):
                return option
        return None
    def _execute_bug_skill_confirmation_choice(
        self,
        event: LarkEvent,
        followup_context,
        previous_session: dict[str, object],
        selected_option: dict[str, object],
        *,
        source: str,
        reason: str,
    ) -> TaskResult:
        details = previous_session.get("details", {}) if isinstance(previous_session, dict) else {}
        if not isinstance(details, dict):
            details = {}
        request_text = str(
            details.get("user_request_text")
            or getattr(followup_context, "request_text", "")
            or ""
        ).strip()
        bug_url = str(details.get("bug_url") or self._bug_url_from_request_text(request_text)).strip()
        if not bug_url:
            return TaskResult(
                success=False,
                message="找不到原始 Bug 链接，请重新发送 Bug 链接和分析方向。",
                error_code="missing_bug_url_for_skill_confirmation",
                details={"mode": "bug_skill_confirmation"},
            )

        parsed = parse_bug_request(request_text, bug_url_re=self.bug_url_re)
        prompt = parsed.prompt if parsed.triggered else request_text
        option_type = str(selected_option.get("type") or "").strip()
        if option_type == "skill":
            selected = self.bug_runner.selection_for_skill_name(
                str(selected_option.get("skill_name") or "").strip(),
                source=source,
                reason=reason,
            )
            if selected is None:
                return TaskResult(
                    success=False,
                    message="回复中的 Skill 选项无效，请重新选择。",
                    error_code="invalid_bug_skill_selection",
                    details={"mode": "bug_skill_confirmation"},
                )
            plans = selected.plans
            classification_skill = selected.skill_name
            classification_reason = selected.reason
        elif option_type == "plans":
            raw_plan_kinds = selected_option.get("plan_kinds", [])
            plan_kinds = [
                str(item or "").strip()
                for item in (raw_plan_kinds if isinstance(raw_plan_kinds, list) else [])
                if str(item or "").strip()
            ]
            plans = [BugAnalysisPlan(kind=kind) for kind in plan_kinds]
            if not plans:
                return TaskResult(
                    success=False,
                    message="回复中的组合分析选项无效，请重新选择。",
                    error_code="invalid_bug_skill_confirmation_option",
                    details={"mode": "bug_skill_confirmation"},
                )
            classification_skill = str(selected_option.get("skill_name") or "startup+stuck")
            classification_reason = reason
        else:
            return TaskResult(
                success=False,
                message="回复中的选项类型无效，请重新选择。",
                error_code="invalid_bug_skill_confirmation_option",
                details={"mode": "bug_skill_confirmation"},
            )

        bug_request = BugRequest(
            bug_url=bug_url,
            prompt=prompt,
            raw_text=request_text,
            triggered=True,
        )
        return self._run_bug_request(
            event,
            bug_request,
            bug_request.raw_text,
            plans_override=plans,
            classification_skill=classification_skill,
            classification_source=source,
            classification_reason=classification_reason,
        )
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
