from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from . import _shared
from ...parser import parse_addr2line_request


class _BugFollowupRouteMixin:
    """Bug 追问路由决策：判定是否重分析与目标技能选择（与 ResolveSourceMixin 共享 self 状态）。"""

    def _strip_bug_followup_suffix(self, request_text: str) -> str:
        text = request_text.strip()
        for marker in ("\n\n追问/修正：", "\n追问/修正："):
            idx = text.find(marker)
            if idx != -1:
                return text[:idx].rstrip()
        return text
    def _original_bug_request_text(self, previous_context: object, details: dict[str, object]) -> str:
        original = str(details.get("user_request_text") or "").strip()
        if original:
            return original
        request_text = str(getattr(previous_context, "request_text", "") or "").strip()
        return self._strip_bug_followup_suffix(request_text)
    def decide_bug_followup(
        self,
        *,
        followup_text: str,
        previous_context: object,
        previous_session: dict[str, object],
    ) -> BugFollowupSelection | None:
        details = previous_session.get("details", {}) if isinstance(previous_session, dict) else {}
        if not isinstance(details, dict):
            details = {}
        request_text = self._original_bug_request_text(previous_context, details)
        summary_text = str(getattr(previous_context, "summary_text", "") or "")
        report_excerpt = str(getattr(previous_context, "report_excerpt", "") or "")
        prepared_log_input = str(details.get("prepared_log_input") or "")
        selected_log_input = str(details.get("selected_log_input") or "")
        primary_skills = [item for item in self._available_bug_skills() if item.get("role") == "primary"]
        primary_skill_names = [str(item.get("name") or "") for item in primary_skills if item.get("name")]
        analysis_kinds = sorted({str(item.get("kind") or "general") for item in primary_skills if item.get("kind")})
        payload = {
            "request_text": request_text,
            "followup_text": followup_text,
            "summary_text": summary_text[:3000],
            "report_excerpt": report_excerpt[:5000],
            "prepared_log_input": prepared_log_input,
            "selected_log_input": selected_log_input,
            "current_analysis_kinds": details.get("analysis_kinds") or [],
            "primary_skills": primary_skills,
        }
        deterministic = self._deterministic_bug_followup_selection(
            followup_text=followup_text,
            request_text=request_text,
        )
        if deterministic is not None:
            return deterministic
        prompt = (
            "你是 Lark Agent Bridge 的 bug 续聊决策器。"
            "请先判断当前追问能否直接基于已有分析结果回答；如果不能，再决定是否需要重新分析，"
            "并选择最合适的主分析 skill。"
            "如果没有专用 skill 明确匹配，general 只代表分诊或澄清，不要把泛泛请求改写成源码根因分析。"
            "先判断是否有更专一的 primary skill；业务差异以输入 JSON 里的 primary_skills.name/kind/description/report_contract 为准，"
            "不要在决策器里臆造或覆盖 skill 规则。"
            "通用词只能作为辅助线索：如果文本只出现 signal/信号、卡顿、主题、启动等泛词，必须结合 primary_skills 描述和追问意图确认是否真的需要重新分析。"
            "如果选择的 skill 需要日志，而当前 prepared_log_input / selected_log_input 为空，请把 retry_download_if_missing 设为 true。"
            "只输出一个 JSON 对象，字段必须完整："
            '{"action":"answer_from_existing|reanalyze",'
            f'"analysis_kind":"{"|".join(analysis_kinds or ["general"])}",'
            f'"skill":"{"|".join(primary_skill_names or ["general"])}",'
            '"signal_hint":"可为空",'
            '"retry_download_if_missing":true,'
            '"reason":"一句中文理由"}'
            "\n输入 JSON：\n"
            f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
        )
        parsed, provider = self._run_bug_decision_agent(prompt)
        if parsed is None:
            return None
        action = str(parsed.get("action") or "").strip()
        kind = str(parsed.get("analysis_kind") or "").strip()
        skill = str(parsed.get("skill") or "").strip()
        signal_hint = str(parsed.get("signal_hint") or "").strip()
        reason = str(parsed.get("reason") or "").strip()
        retry_download = bool(parsed.get("retry_download_if_missing"))
        primary_skill_map = self.skill_manager.primary_skill_map()
        if skill in primary_skill_map:
            kind = primary_skill_map[skill][0] or kind
        plans = self._resolve_bug_plans(
            analysis_kind=kind,
            signal_hint=signal_hint,
            combined_text="\n".join(part for part in [request_text, followup_text] if part),
        )
        if any(plan.kind == "signal" and not plan.signal_code for plan in plans):
            return None
        selection = self._selection_from_plans(
            plans,
            source="agent",
            reason=reason or "Agent 已完成 bug 续聊决策。",
            provider=provider,
        )
        if skill:
            selection.skill_name = skill
            selection.skill_label = self._skill_label_for_name(skill, plans[0].kind if plans else "general")
        return BugFollowupSelection(
            should_reanalyze=action == "reanalyze",
            force_rerun=action == "reanalyze",
            plans=selection.plans,
            skill_name=selection.skill_name,
            skill_label=selection.skill_label,
            source=selection.source,
            reason=selection.reason,
            provider=selection.provider,
        )
    def _deterministic_bug_followup_selection(
        self,
        *,
        followup_text: str,
        request_text: str,
    ) -> BugFollowupSelection | None:
        lowered = followup_text.casefold()
        followup_action = parse_followup_action(followup_text)
        force_terms = tuple(term.casefold() for term in self.config.bug_analysis.force_reanalysis_terms)
        selection = self._manual_followup_selection_if_explicit(
            followup_text=followup_text,
            request_text=request_text,
        )
        if any(term in lowered for term in force_terms):
            return self._build_bug_followup_selection(
                should_reanalyze=True,
                force_rerun=True,
                selection=selection,
                reason=(
                    "命中本地强制重分析词，并识别到新的分析方向。"
                    if selection is not None
                    else "命中本地强制重分析词，沿用上一轮分析类型重新执行。"
                ),
                source="deterministic_fallback",
            )
        if followup_action == "retry":
            return self._build_bug_followup_selection(
                should_reanalyze=True,
                force_rerun=True,
                selection=selection,
                reason=(
                    "命中统一续跑动作，并识别到新的分析方向。"
                    if selection is not None
                    else "命中统一续跑动作，沿用上一轮分析类型重新执行。"
                ),
                source="deterministic_fallback",
            )
        if parse_signal_request(
            followup_text,
            signal_aliases=self.config.signal_aliases,
            command_prefixes=self.config.command_prefixes,
            signal_resolver=self.signal_resolver,
        ).signal:
            return self._build_bug_followup_selection(
                should_reanalyze=True,
                force_rerun=True,
                selection=selection,
                reason="本地回退识别到明确信号请求，触发重分析。",
                source="deterministic_fallback",
            )
        has_correction = any(term in lowered for term in ("修正", "修复问题时间", "更正", "修改", "改成"))
        has_time = re.search(r"(?<!\d)\d{1,2}[:：]\d{2}(?:\s*分)?(?!\d)", followup_text) is not None
        if has_correction and has_time:
            return self._build_bug_followup_selection(
                should_reanalyze=True,
                force_rerun=True,
                selection=selection,
                reason=(
                    "本地回退识别到时间修正和新的分析方向，触发重分析。"
                    if selection is not None
                    else "本地回退识别到时间修正，沿用上一轮分析类型重分析。"
                ),
                source="deterministic_fallback",
            )
        return None
    def _manual_followup_selection_if_explicit(
        self,
        *,
        followup_text: str,
        request_text: str,
    ) -> "BugAnalysisSelection | None":
        lowered_followup = (followup_text or "").casefold()
        combined_lowered = f"{request_text}\n{followup_text}".casefold()

        def _scene_signal_source_followup_selection() -> "BugAnalysisSelection | None":
            if (
                self._followup_explicitly_requests_source_analysis(request_text, followup_text)
                and ("信号" in followup_text or "signal" in lowered_followup)
                and (
                    _has_strong_scene_signal_intent(combined_lowered)
                    or looks_like_scene_signal_request(request_text)
                )
            ):
                return self.selection_for_skill_name(
                    "scene-signal-diagnosis",
                    source="deterministic_fallback",
                    reason="源码追问命中场景信号上下文。",
                )
            return None

        if self._has_explicit_3d_lifecycle_intent(followup_text):
            return self.selection_for_skill_name(
                "unity-startup-lifecycle-check",
                source="deterministic_fallback",
                reason="用户在续聊中明确要求重新分析 3D 生命周期。",
            )
        if not self._followup_has_explicit_bug_route(followup_text):
            return _scene_signal_source_followup_selection()
        selection = self._manual_bug_selection(
            prompt_text=followup_text,
            title="",
            description="",
        )
        if selection.skill_name == "general" and all(plan.kind == "general" for plan in selection.plans):
            return _scene_signal_source_followup_selection()
        return selection
    def _build_bug_followup_selection(
        self,
        *,
        should_reanalyze: bool,
        force_rerun: bool,
        selection: "BugAnalysisSelection | None",
        reason: str,
        source: str,
    ) -> BugFollowupSelection:
        if selection is None:
            return BugFollowupSelection(
                should_reanalyze=should_reanalyze,
                force_rerun=force_rerun,
                plans=[],
                skill_name="",
                skill_label="",
                source=source,
                reason=reason,
                provider="",
            )
        return BugFollowupSelection(
            should_reanalyze=should_reanalyze,
            force_rerun=force_rerun,
            plans=selection.plans,
            skill_name=selection.skill_name,
            skill_label=selection.skill_label,
            source=selection.source,
            reason=reason,
            provider=selection.provider,
        )
    def _followup_has_explicit_bug_route(self, text: str) -> bool:
        lowered = (text or "").casefold()
        if parse_signal_request(
            text,
            signal_aliases=self.config.signal_aliases,
            command_prefixes=self.config.command_prefixes,
            signal_resolver=self.signal_resolver,
        ).signal:
            return True
        route_terms = (
            STARTUP_ROUTE_TERMS
            + STARTUP_BLOCK_ROUTE_TERMS
            + STUCK_ROUTE_TERMS
            + CRASH_ROUTE_TERMS
            + SIGNAL_ROUTE_TERMS
            + SCENE_SIGNAL_ROUTE_TERMS
            + XTHEME_ROUTE_TERMS
            + PERCEPTION_ROUTE_TERMS
            + LD_LANE_LEVEL_ROUTE_TERMS
            + PULLOVER_CHAIN_ROUTE_TERMS
        )
        return any(term.casefold() in lowered for term in route_terms)
