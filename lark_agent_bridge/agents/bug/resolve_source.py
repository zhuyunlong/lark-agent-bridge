from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from . import _shared
from ...parser import parse_addr2line_request


from .followup_route import _BugFollowupRouteMixin
from .decision_agent import _BugDecisionAgentMixin
from .clarify_confirm import _BugClarifyConfirmMixin


class _ResolveSourceMixin(_BugFollowupRouteMixin, _BugDecisionAgentMixin, _BugClarifyConfirmMixin):
    def __init__(
        self,
        config: BridgeConfig,
        process_watchdog: ProcessWatchdog | None = None,
        lark_client: object | None = None,
        skill_manager: SkillManager | None = None,
    ) -> None:
        self.config = config
        self.process_watchdog = process_watchdog
        self.signal_resolver = SignalResolver(
            config.guideengine_repo,
            cache_dir=config.data_dir / "cache",
            cache_ttl_seconds=config.signal_resolver.cache_ttl_seconds,
            preferred_paths=config.signal_resolver.preferred_paths or None,
            source_suffixes=config.signal_resolver.source_suffixes or None,
        )
        self._lark_client = lark_client
        self.skill_manager = skill_manager or SkillManager(config)

        # 智能日志分析器（内部工具）
        smart_config = getattr(config, 'smart_log_analysis', None)
        if smart_config and getattr(smart_config, 'enabled', False):
            self.log_analyzer = SmartLogAnalyzer(
                process_name=getattr(smart_config, 'process_name', 'com.xiaopeng.montecarlo'),
                time_window_minutes=getattr(smart_config, 'time_window_minutes', 1),
                max_reverse_lines=getattr(smart_config, 'max_reverse_lines', 1000),
                workspace_root=config.workspace_root,
            )
        else:
            # 默认配置
            self.log_analyzer = SmartLogAnalyzer(
                process_name='com.xiaopeng.montecarlo',
                time_window_minutes=1,
                max_reverse_lines=1000,
                workspace_root=config.workspace_root,
            )
    def _resolve_source_evidence_future(self, future: object) -> Path | None:
        try:
            return future.result(timeout=self._SOURCE_EVIDENCE_WAIT_SECONDS)  # type: ignore[attr-defined]
        except Exception:
            return None
    def _source_evidence_call_timeout(self, deadline: float | None, configured_timeout: float) -> float:
        timeout = min(configured_timeout, self._SOURCE_EVIDENCE_CODEGRAPH_CALL_SECONDS)
        if deadline is None:
            return timeout
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return 0.0
        return min(timeout, remaining)
    def _available_bug_skills(self) -> list[dict[str, object]]:
        skills_dir = self.config.workspace_root / ".ai/skills"
        entries: list[dict[str, object]] = []
        primary_skill_map = self.skill_manager.primary_skill_map()
        auxiliary_skill_names = self.skill_manager.auxiliary_skill_names()
        if not skills_dir.exists():
            for skill_name, (kind, label, requires_logs) in primary_skill_map.items():
                if skill_name == "general":
                    continue
                entries.append(
                    {
                        "name": skill_name,
                        "kind": kind,
                        "label": label,
                        "requires_logs": requires_logs,
                        "role": "primary",
                        "description": "",
                    }
                )
            entries.append(
                {
                    "name": "general",
                    "kind": "general",
                    "label": "通用问题分析",
                    "requires_logs": False,
                    "role": "primary",
                    "description": "没有合适专用 skill 时只做分诊和材料检查；缺少明确方向时要求用户补充，不盲扫源码给根因。",
                }
            )
            return entries

        for path in sorted(skills_dir.glob("*/SKILL.md")):
            skill_name = path.parent.name
            try:
                body = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            frontmatter_name, description = _extract_skill_frontmatter(body)
            if skill_name in primary_skill_map:
                kind, label, requires_logs = primary_skill_map[skill_name]
                entries.append(
                    {
                        "name": skill_name,
                        "kind": kind,
                        "label": label,
                        "requires_logs": requires_logs,
                        "role": "primary",
                        "description": description or frontmatter_name or "",
                    }
                )
            elif skill_name in auxiliary_skill_names:
                entries.append(
                    {
                        "name": skill_name,
                        "kind": "",
                        "label": frontmatter_name or skill_name,
                        "requires_logs": False,
                        "role": "auxiliary",
                        "description": description or "",
                    }
                )
        entries.append(
            {
                "name": "general",
                "kind": "general",
                "label": "通用问题分析",
                "requires_logs": False,
                "role": "primary",
                "description": "没有合适专用 skill 时只做分诊和材料检查；缺少明确方向时要求用户补充，不盲扫源码给根因。",
            }
        )
        return entries
    def supported_primary_bug_skills(self) -> list[dict[str, object]]:
        return [
            record.to_dict(include_content=False)
            for record in self.skill_manager.list_skills()
            if record.role == "primary" and record.name != "general" and record.selectable_in_report_card
        ]
    def selection_for_skill_name(
        self,
        skill_name: str,
        *,
        source: str,
        reason: str = "",
        provider: str = "",
    ) -> "BugAnalysisSelection | None":
        normalized = skill_name.strip()
        primary_skill_map = self.skill_manager.primary_skill_map()
        if normalized == "general" or normalized not in primary_skill_map:
            return None
        kind, label, _requires_logs = primary_skill_map[normalized]
        return BugAnalysisSelection(
            plans=[BugAnalysisPlan(kind=kind)],
            skill_name=normalized,
            skill_label=label,
            source=source,
            reason=reason or f"用户选择专用 skill：{label}",
            provider=provider,
        )
    def _manual_bug_selection(self, *, prompt_text: str, title: str, description: str) -> BugAnalysisSelection:
        plans = self.classify_requests(prompt_text=prompt_text, title=title, description=description)
        return self._selection_from_plans(plans, source="manual_fallback", reason="Agent 分类不可用，退回本地规则分类。")
    def _selection_from_plans(
        self,
        plans: list["BugAnalysisPlan"],
        *,
        source: str,
        reason: str,
        provider: str = "",
    ) -> BugAnalysisSelection:
        plan = plans[0] if plans else BugAnalysisPlan(kind="general")
        skill_name = self._skill_name_for_kind(plan.kind)
        return BugAnalysisSelection(
            plans=plans or [BugAnalysisPlan(kind="general")],
            skill_name=skill_name,
            skill_label=self._analysis_label(plan.kind),
            source=source,
            reason=reason,
            provider=provider,
        )
    def _source_analysis_shortcut(self, *texts: str) -> bool:
        merged = "\n".join(str(text or "") for text in texts).strip()
        if not merged:
            return False
        return re.search(r"(^|[\s@])debug(\b|[\s:：_-])", merged, re.I) is not None
    def _source_analysis_targets_from_texts(self, *texts: str) -> list[str]:
        terms: list[str] = []
        for text in texts:
            for term in self._explicit_source_terms_from_text(str(text or "")):
                self._append_unique(terms, term)
        return terms[:12]
    def _has_stack_reverse_lookup_intent(self, prompt_text: str) -> bool:
        prompt = str(prompt_text or "").strip()
        if not prompt:
            return False
        return parse_addr2line_request(prompt, allow_missing_address=True).triggered
    def _has_stack_reverse_lookup_payload(self, *texts: str) -> bool:
        merged = "\n".join(str(text or "") for text in texts if str(text or "").strip())
        if not merged:
            return False
        return parse_addr2line_request(merged).triggered
    def _domain_kind_from_plans(self, plans: list["BugAnalysisPlan"]) -> str:
        for plan in plans:
            if plan.kind != "general" and not _kind_spec(plan.kind).is_source_stage:
                return plan.kind
        return "general"
    def _stack_reverse_preflight_can_override(self, selection: "BugAnalysisSelection") -> bool:
        if selection.skill_name.strip() in {"", "general"}:
            return True
        if selection.source in {"manual_fallback", "heuristic", "fallback"}:
            return all(plan.kind in {"general", "crash"} for plan in selection.plans)
        return False
    def _context_profile_for_domain(self, domain_kind: str, skill_name: str) -> str:
        normalized = skill_name.strip()
        if normalized and normalized not in {"general", "source_analysis"}:
            return normalized
        if domain_kind and domain_kind != "general":
            candidate = self._skill_name_for_kind(domain_kind)
            if candidate != "general":
                return candidate
        return ""
    def _has_explicit_source_request(self, *, request_text: str, prompt_text: str, skill_name: str) -> bool:
        if skill_name.strip() == "source_analysis":
            return True
        if self._source_analysis_shortcut(prompt_text, request_text):
            return True
        if self._should_collect_source_evidence(prompt_text, request_text):
            return True
        return False
    def _build_analysis_decision(
        self,
        *,
        request_text: str,
        prompt_text: str,
        title: str,
        description: str,
        plans: list["BugAnalysisPlan"],
        skill_name: str,
    ) -> "AnalysisDecision":
        domain_kind = self._domain_kind_from_plans(plans)
        context_profile = self._context_profile_for_domain(domain_kind, skill_name)
        source_targets = self._source_analysis_targets_from_texts(prompt_text, request_text)
        explicit_source = self._has_explicit_source_request(
            request_text=request_text,
            prompt_text=prompt_text,
            skill_name=skill_name,
        )
        source_mode = "off"
        reason = "未检测到明确源码诉求，默认只执行领域分析。"
        if explicit_source:
            if domain_kind == "general":
                source_mode = "standalone"
                reason = "用户明确要求源码分析，且未命中稳定领域 skill，执行独立源码阶段。"
            else:
                source_mode = "append"
                reason = "用户明确要求源码分析，在领域分析后追加源码阶段。"
        return AnalysisDecision(
            domain_kind=domain_kind,
            source_mode=source_mode,
            context_profile=context_profile,
            source_targets=source_targets,
            reason=reason,
        )
    def _build_analysis_plan(self, decision: "AnalysisDecision") -> "AnalysisPlan":
        stages: list[AnalysisStage] = []
        if decision.source_mode in {"off", "append"}:
            stages.append(
                AnalysisStage(
                    kind="domain",
                    domain_kind=decision.domain_kind,
                    context_profile=decision.context_profile,
                    source_targets=list(decision.source_targets),
                )
            )
        if decision.source_mode in {"append", "standalone"}:
            stages.append(
                AnalysisStage(
                    kind="source",
                    domain_kind=decision.domain_kind,
                    context_profile=decision.context_profile,
                    source_targets=list(decision.source_targets),
                )
            )
        stages.append(
            AnalysisStage(
                kind="summary",
                domain_kind=decision.domain_kind,
                context_profile=decision.context_profile,
                source_targets=list(decision.source_targets),
            )
        )
        return AnalysisPlan(
            domain_kind=decision.domain_kind,
            context_profile=decision.context_profile,
            stages=stages,
        )
    def _decide_source_analysis_request(
        self,
        *,
        request_text: str,
        prompt_text: str,
        title: str,
        description: str,
        plans: list["BugAnalysisPlan"],
        skill_name: str,
    ) -> SourceAnalysisDecision:
        analysis_decision = self._build_analysis_decision(
            request_text=request_text,
            prompt_text=prompt_text,
            title=title,
            description=description,
            plans=plans,
            skill_name=skill_name,
        )
        analysis_plan = self._build_analysis_plan(analysis_decision)
        requested = analysis_decision.source_mode in {"append", "standalone"}
        return SourceAnalysisDecision(
            requested=requested,
            reason=analysis_decision.reason,
            targets=list(analysis_decision.source_targets),
            source="stage_rules",
            debug_shortcut=self._source_analysis_shortcut(prompt_text, request_text),
            domain_kind=analysis_decision.domain_kind,
            source_mode=analysis_decision.source_mode,
            context_profile=analysis_decision.context_profile,
            stage_kinds=[stage.kind for stage in analysis_plan.stages],
            intent=infer_intent_from_text(f"{prompt_text} {request_text}"),
        )
    def _augment_plans_for_source_analysis(
        self,
        plans: list["BugAnalysisPlan"],
        *,
        source_decision: SourceAnalysisDecision,
    ) -> list["BugAnalysisPlan"]:
        normalized = [BugAnalysisPlan(kind=plan.kind, signal_code=plan.signal_code) for plan in plans]
        if not source_decision.requested:
            return normalized
        if all(plan.kind == "general" for plan in normalized):
            return [BugAnalysisPlan(kind=SOURCE_STAGE_KIND)]
        if any(_kind_spec(plan.kind).is_source_stage for plan in normalized):
            return normalized
        return [*normalized, BugAnalysisPlan(kind=SOURCE_STAGE_KIND)]
    def _unified_classify_and_decide(
        self,
        *,
        request_text: str,
        prompt_text: str,
        title: str,
        description: str,
        attachments: object = (),
        time_context: "BugTimeContext | None" = None,
        stack_payload_text: str = "",
    ) -> "UnifiedBugDecision":
        """Single entry-point that replaces the old two-step flow.

        1. Agent classification → ``BugAnalysisSelection``
        2. Source analysis decision → ``SourceAnalysisDecision``
        3. Plan augmentation (append ``source_stage`` if needed)
        4. Skill-name normalization (rename to ``source_analysis`` when applicable)

        The result is a ``UnifiedBugDecision`` with both sub-decisions.
        """
        # --- step 1: domain classification ---
        selection = self._classify_bug_request_with_agent(
            prompt_text=prompt_text,
            title=title,
            description=description,
            attachments=attachments,
            time_context=time_context,
        )
        selection = self._normalize_agent_bug_selection(selection, prompt_text=prompt_text)
        if selection is not None:
            selection = self._downgrade_lifecycle_stuck_conflict(
                selection,
                prompt_text=prompt_text,
                title=title,
                description=description,
            )
        if selection is not None and any(
            plan.kind == "signal" and not plan.signal_code for plan in selection.plans
        ):
            selection = None
        if selection is None:
            selection = self._manual_bug_selection(
                prompt_text=prompt_text, title=title, description=description,
            )
        payload_source = stack_payload_text or description
        if (
            self._stack_reverse_preflight_can_override(selection)
            and self._has_stack_reverse_lookup_intent(prompt_text)
            and self._has_stack_reverse_lookup_payload(
                request_text,
                prompt_text,
                payload_source,
            )
        ):
            selection = self._selection_from_plans(
                [BugAnalysisPlan(kind=SOURCE_STAGE_KIND)],
                source="preflight_rules",
                reason="用户明确要求反解堆栈，且 Bug 文本包含可用堆栈，执行源码/堆栈分析阶段。",
            )
            selection.skill_name = "source_analysis"
            selection.skill_label = self._analysis_label(SOURCE_STAGE_KIND)

        # --- step 2: source analysis decision ---
        source_decision = self._decide_source_analysis_request(
            request_text=request_text,
            prompt_text=prompt_text,
            title=title,
            description=description,
            plans=selection.plans,
            skill_name=selection.skill_name,
        )

        # --- step 3: augment plans ---
        selection.plans = self._augment_plans_for_source_analysis(
            selection.plans, source_decision=source_decision,
        )

        # --- step 4: normalize skill name ---
        if (
            source_decision.requested
            and all(_kind_spec(plan.kind).is_source_stage for plan in selection.plans)
            and selection.skill_name.strip() in {"", "general"}
        ):
            selection.skill_name = "source_analysis"
            selection.skill_label = self._analysis_label(SOURCE_STAGE_KIND)
            if source_decision.reason:
                selection.reason = source_decision.reason

        return UnifiedBugDecision(selection=selection, source_decision=source_decision)
    def classify_and_decide(
        self,
        *,
        request_text: str,
        prompt_text: str,
        title: str = "",
        description: str = "",
        plans: "list[BugAnalysisPlan] | None" = None,
    ) -> "UnifiedBugDecision":
        """Public API for external callers (e.g. ``app.py`` preflight).

        When *plans* is provided the classification step is skipped
        and only the source-analysis decision is performed.
        """
        if plans is not None:
            first_plan = plans[0] if plans else BugAnalysisPlan(kind="general")
            skill_name = (
                self._skill_name_for_kind(first_plan.kind)
                if first_plan.kind != "general"
                else ""
            )
            selection = self._selection_from_plans(
                plans,
                source="preflight_rules",
                reason="",
            )
            if skill_name:
                selection.skill_name = skill_name
                selection.skill_label = self._skill_label_for_name(
                    skill_name, first_plan.kind,
                )
            source_decision = self._decide_source_analysis_request(
                request_text=request_text,
                prompt_text=prompt_text,
                title=title,
                description=description,
                plans=plans,
                skill_name=skill_name,
            )
            selection.plans = self._augment_plans_for_source_analysis(
                selection.plans, source_decision=source_decision,
            )
            if (
                source_decision.requested
                and all(_kind_spec(plan.kind).is_source_stage for plan in selection.plans)
                and selection.skill_name.strip() in {"", "general"}
            ):
                selection.skill_name = "source_analysis"
                selection.skill_label = self._analysis_label(SOURCE_STAGE_KIND)
                if source_decision.reason:
                    selection.reason = source_decision.reason
            return UnifiedBugDecision(selection=selection, source_decision=source_decision)

        return self._unified_classify_and_decide(
            request_text=request_text,
            prompt_text=prompt_text,
            title=title,
            description=description,
        )
    def _skill_name_for_kind(self, kind: str) -> str:
        if _kind_spec(kind).is_source_stage:
            return "source_analysis"
        for skill_name, (mapped_kind, _label, _requires_logs) in self.skill_manager.primary_skill_map().items():
            if mapped_kind == kind:
                return skill_name
        return "general"
    def _resolve_source_stage_skill_name(self, requested_skill: str) -> str:
        normalized = str(requested_skill or "").strip()
        fallback = self._skill_name_for_kind(SOURCE_STAGE_KIND)
        if not normalized or normalized == fallback:
            return fallback
        executor = self.skill_manager.custom_skill_executor_for(normalized)
        if executor in {"file_agent", "pydantic_ai"}:
            # Explicitly configured executor → use this skill
            return normalized
        # executor == "" for two reasons:
        #   (a) builtin/non-routed skill (scene-signal-diagnosis, signal-chain-analyzer, etc.)
        #       → use fallback "source_analysis" so pydantic-ai runs without custom routing
        #   (b) explicitly registered as primary source skill but no executor configured
        #       → return name so executor_not_ready check fires downstream
        # Distinguish by checking if primary_skill_map kind is a source skill kind.
        entry = self.skill_manager.primary_skill_map().get(normalized)
        if entry is not None and entry[0] in _SOURCE_SKILL_KINDS:
            return normalized  # explicitly configured as source skill → trigger executor_not_ready
        return fallback
    def _skill_label_for_name(self, skill_name: str, fallback_kind: str = "general") -> str:
        if skill_name == "startup+stuck":
            return "3D启动卡顿综合分析"
        route = self.skill_manager.primary_skill_map().get(skill_name)
        if route is not None:
            return route[1]
        return self._analysis_label(fallback_kind)
    def _resolve_bug_plans(self, *, analysis_kind: str, signal_hint: str, combined_text: str) -> list["BugAnalysisPlan"]:
        kind = (analysis_kind or "").strip()
        if kind == "signal":
            signal_request = parse_signal_request(
                "\n".join(part for part in [signal_hint, combined_text] if part),
                signal_aliases=self.config.signal_aliases,
                command_prefixes=self.config.command_prefixes,
                signal_resolver=self.signal_resolver,
            )
            return [BugAnalysisPlan(kind="signal", signal_code=signal_request.signal)]
        if kind in PLAN_KIND_REGISTRY:
            return [BugAnalysisPlan(kind=kind)]
        return [BugAnalysisPlan(kind="general")]
