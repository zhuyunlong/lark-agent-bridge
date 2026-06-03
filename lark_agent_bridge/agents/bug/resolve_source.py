from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from . import _shared


class _ResolveSourceMixin:
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

    def _domain_kind_from_plans(self, plans: list["BugAnalysisPlan"]) -> str:
        for plan in plans:
            if plan.kind != "general" and not _kind_spec(plan.kind).is_source_stage:
                return plan.kind
        return "general"

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

    # ------------------------------------------------------------------
    # Unified classification + source-decision (Phase 2)
    # ------------------------------------------------------------------

    def _unified_classify_and_decide(
        self,
        *,
        request_text: str,
        prompt_text: str,
        title: str,
        description: str,
        attachments: object = (),
        time_context: "BugTimeContext | None" = None,
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

    def _needs_general_direction(
        self,
        selection: "BugAnalysisSelection",
        *,
        prompt_text: str,
    ) -> bool:
        if selection.skill_name != "general":
            return False
        if any(plan.kind != "general" for plan in selection.plans):
            return False
        return not self._has_explicit_general_scope(prompt_text)

    def _has_explicit_general_scope(self, prompt_text: str) -> bool:
        prompt = self._normalize_general_prompt(prompt_text)
        if not prompt:
            return False
        if prompt.casefold() in _GENERIC_BUG_PROMPT_TERMS:
            return False
        return any(pattern.search(prompt) for pattern in _GENERAL_SCOPE_PATTERNS)

    def _normalize_general_prompt(self, prompt_text: str) -> str:
        prompt = re.sub(r"https?://\S+", " ", prompt_text or "")
        prompt = re.sub(r"\s+", " ", prompt).strip(" \t\r\n，。；;、:：")
        return prompt

    def _general_direction_needed_result(
        self,
        *,
        context,
        selection: "BugAnalysisSelection",
        started: float,
        request_text: str,
        bug_url: str,
        title: str,
        progress_callback: Callable[[dict[str, object]], None] | None,
    ) -> TaskResult:
        self._emit_progress(
            progress_callback,
            stage="bug_need_analysis_direction",
            message="未命中专用预设，等待用户补充明确分析方向",
            job_id=context.job_id,
            bug_url=bug_url,
            title=title,
            classification_skill=selection.skill_name,
            classification_source=selection.source,
            classification_reason=selection.reason,
        )
        message = (
            "当前没有命中专用分析预设，且请求中缺少明确分析方向。\n"
            "为了避免盲扫源码/日志后给出不可靠结论，我没有继续自动分析。\n\n"
            "请补充一个可约束的方向，例如：\n"
            "- 启动 / 卡顿 / Crash / 感知数据 / 主题切换 / 场景信号\n"
            "- 具体 SignalCode、枚举名、类名、函数名、进程号、包名或日志关键词\n"
            "- 只检查日志材料是否完整，或指定要看的时间窗口\n\n"
            "如果仍没有明确方向，目前能力不足以给出可靠根因。"
        )
        return TaskResult(
            success=True,
            message=message,
            skipped=True,
            job_id=context.job_id,
            job_dir=context.job_dir,
            duration_seconds=time.monotonic() - started,
            details={
                "mode": "bug_clarification",
                "analysis_kind": "general",
                "analysis_kinds": ["general"],
                "analysis_skill": "general",
                "analysis_skill_label": "通用问题分诊",
                "classification_source": selection.source,
                "classification_reason": selection.reason,
                "classification_provider": selection.provider,
                "bug_url": bug_url,
                "user_request_text": request_text,
                "needs_user_direction": True,
                "supported_bug_skills": self.supported_primary_bug_skills(),
            },
        )

    def _bug_time_clarification_result(
        self,
        *,
        context,
        started: float,
        request_text: str,
        bug_url: str,
        time_context: BugTimeContext,
        status: str,
        progress_callback: Callable[[dict[str, object]], None] | None,
        log_coverage: LogCoverage | None = None,
    ) -> TaskResult:
        self._emit_progress(
            progress_callback,
            stage="bug_time_gate_blocked",
            message="问题时间或日志覆盖不满足分析前置条件",
            job_id=context.job_id,
            bug_url=bug_url,
            time_gate_status=status,
            fault_time=time_context.fault_time,
            time_source=time_context.source,
            log_start=log_coverage.start_time if log_coverage else "",
            log_end=log_coverage.end_time if log_coverage else "",
        )
        if status == "missing_fault_time":
            message = (
                "缺少明确问题时间：请补充几月几日 几点几分，越精确越好。\n"
                "我已检查用户输入、Bug 标题和描述，但没有找到可用于定位日志的完整时间点。\n"
                "补充示例：`问题时间 2026-05-11 23:12:30，分析3D卡顿`。"
            )
        elif status == "log_time_unknown":
            message = (
                f"已识别问题时间 `{time_context.fault_time}`，但当前日志无法解析出有效时间范围。\n"
                "请补充包含该时间点附近的已解密文本日志，或确认附件是否为正确日志包。"
            )
        else:
            coverage_text = (
                f"{log_coverage.start_time} ~ {log_coverage.end_time}"
                if log_coverage and log_coverage.start_time
                else "未识别"
            )
            message = (
                f"日志时间范围未覆盖问题时间 `{time_context.fault_time}`。\n"
                f"当前日志覆盖范围：`{coverage_text}`。\n"
                "请补充覆盖该问题时间前后约 10 分钟的日志，或修正问题时间后再继续分析。"
            )
        return TaskResult(
            success=True,
            message=message,
            skipped=True,
            job_id=context.job_id,
            job_dir=context.job_dir,
            duration_seconds=time.monotonic() - started,
            details={
                "mode": "bug_time_clarification",
                "bug_url": bug_url,
                "user_request_text": request_text,
                "time_gate_status": status,
                "fault_time": time_context.fault_time,
                "fault_time_source": time_context.source,
                "fault_time_note": time_context.note,
                "log_coverage_start": log_coverage.start_time if log_coverage else "",
                "log_coverage_end": log_coverage.end_time if log_coverage else "",
                "log_coverage_scanned_files": log_coverage.scanned_files if log_coverage else 0,
                "log_coverage_scanned_lines": log_coverage.scanned_lines if log_coverage else 0,
            },
        )

    def _bug_time_context_payload(self, time_context: BugTimeContext | None) -> dict[str, object]:
        if time_context is None:
            return {}
        return {
            "fault_time": time_context.fault_time,
            "source": time_context.source,
            "note": time_context.note,
            "has_full_datetime": time_context.has_full_datetime,
            "candidates": time_context.candidates,
        }

    def _event_reference_time_text(self, event: LarkEvent | None) -> str:
        if event is None:
            return datetime.now().astimezone().isoformat(timespec="seconds")
        for raw in (event.create_time, event.timestamp):
            text = str(raw or "").strip()
            if not text:
                continue
            if text.isdigit():
                try:
                    return datetime.fromtimestamp(int(text) / 1000, tz=timezone.utc).astimezone().isoformat(
                        timespec="seconds"
                    )
                except (OverflowError, OSError, ValueError):
                    continue
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone().isoformat(timespec="seconds")
        return datetime.now().astimezone().isoformat(timespec="seconds")

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

    def _classify_bug_request_with_agent(
        self,
        *,
        prompt_text: str,
        title: str,
        description: str,
        attachments: object,
        time_context: BugTimeContext | None = None,
    ) -> BugAnalysisSelection | None:
        primary_skills = [item for item in self._available_bug_skills() if item.get("role") == "primary"]
        aux_skills = [item for item in self._available_bug_skills() if item.get("role") == "auxiliary"]
        primary_skill_names = [str(item.get("name") or "") for item in primary_skills if item.get("name")]
        analysis_kinds = sorted({str(item.get("kind") or "general") for item in primary_skills if item.get("kind")})
        payload = {
            "user_prompt": prompt_text,
            "bug_title": title,
            "bug_description": description[:4000],
            "attachments": attachments if isinstance(attachments, list) else [],
            "problem_time": self._bug_time_context_payload(time_context),
            "primary_skills": primary_skills,
            "auxiliary_skills": aux_skills,
        }
        prompt = (
            "你是 Lark Agent Bridge 的 bug skill 分类器。"
            "请根据用户请求、Bug 标题、描述和当前工作区技能，选择最合适的主分析 skill。"
            "在选择 skill 前必须先参考 problem_time；如果 has_full_datetime=false，表示问题时间不足，后续应先要求补充时间而不是继续分析。"
            "只有当没有任何专用 skill 明确匹配时，才选择 general。"
            "general 不是让系统盲扫源码给根因，而是表示需要分诊、材料检查或要求用户补充更明确方向。"
            "先判断是否有更专一的 primary skill；signal-chain-analyzer 优先级最低，只在用户明确要排查某个具体 SignalCode / SIGNAL_... 的通用信号链路、信号来源或是否送达时使用，"
            "不要因为文本里出现 signal/信号 字样就滥用。"
            "3D场景信号 / SceneType / 上电P / 临停P / 特殊场景 / 小憩 / 露营 / 洗车 / 充电场景 / 放电场景 / 场景选择 / 离车舒享 / 行车场景 / 泊车场景 优先考虑 scene-signal-diagnosis。"
            "xtheme / 105004 / 105009 / 晨曦 / 傍晚 / 主题切换 / XuiConditionHelper 应优先考虑 xtheme-analyzer。"
            "车道级 / 车道级进不去 / 车道级渲染 / LD / lane-level / LDConf / CheckLDState / tile / 瓦片 优先考虑车道级（ld-lane-level）类 skill。"
            "启动 / 时序 / 首帧 / UnityReady / displayChanged / startRender 优先考虑启动类 skill；卡顿 / 卡死 / 掉帧 / 黑屏 / ANR / 不刷新 优先考虑卡顿类 skill；闪退 / crash / tombstone / FATAL EXCEPTION / SIGSEGV / 异常退出 优先考虑 crash 类 skill；当前感知数据总结 优先考虑感知类 skill。"
            "若用户已明确点名某 skill 或给出明确方向，直接采用并给 high 置信度；若多个预设都相近、信息不足或只能模糊归类，给 low 置信度并在 reason 里说明分诊依据。"
            "只输出一个 JSON 对象，字段必须完整："
            f'{{"analysis_kind":"{"|".join(analysis_kinds or ["general"])}",'
            f'"skill":"{"|".join(primary_skill_names or ["general"])}",'
            '"signal_hint":"可为空",'
            '"confidence":"high|medium|low",'
            '"reason":"一句中文理由"}'
            "\n输入 JSON：\n"
            f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
        )
        parsed, provider = self._run_bug_decision_agent(prompt)
        if parsed is None:
            return None
        kind = str(parsed.get("analysis_kind") or "").strip()
        skill = str(parsed.get("skill") or "").strip()
        reason = str(parsed.get("reason") or "").strip()
        signal_hint = str(parsed.get("signal_hint") or "").strip()
        confidence = str(parsed.get("confidence") or "").strip().lower()
        if confidence not in {"high", "medium", "low"}:
            confidence = ""
        primary_skill_map = self.skill_manager.primary_skill_map()
        if skill in primary_skill_map:
            kind = primary_skill_map[skill][0] or kind
        plans = self._resolve_bug_plans(
            analysis_kind=kind,
            signal_hint=signal_hint,
            combined_text="\n".join(part for part in [prompt_text, title, description] if part),
        )
        selection = self._selection_from_plans(
            plans,
            source="agent",
            reason=reason or "Agent 已完成 bug skill 分类。",
            provider=provider,
        )
        if skill:
            selection.skill_name = skill
            selection.skill_label = self._skill_label_for_name(skill, plans[0].kind if plans else "general")
        selection.confidence = confidence
        return selection

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
            "先判断是否有更专一的 primary skill；signal-chain-analyzer 优先级最低，只用于明确给出具体 SignalCode / SIGNAL_... 的通用信号链路问题；"
            "3D场景信号 / SceneType / 上电P / 临停P / 特殊场景 / 小憩 / 露营 / 洗车 / 充电场景 / 放电场景 / 场景选择 / 离车舒享 / 行车场景 / 泊车场景 优先考虑 scene-signal-diagnosis。"
            "xtheme / 105004 / 105009 / 晨曦 / 傍晚 / 主题切换 / XuiConditionHelper 优先考虑 xtheme-analyzer。"
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

    def _run_bug_decision_agent(self, prompt: str) -> tuple[dict[str, object] | None, str]:
        candidates = _provider_candidates(self.config.bug_analysis.provider, self.config.bug_analysis.command)
        last_error: Exception | None = None
        for provider, command_name in candidates:
            command, output_path = self._build_bug_decision_command(provider, command_name, prompt)
            if not command:
                continue
            debug_log_path = self._subprocess_debug_log_path(
                self.config.data_dir / "subprocess_debug",
                f"bug-analysis-classifier-{provider or 'agent'}",
            )
            try:
                completed = _run_tracked_process(
                    command,
                    watchdog=self.process_watchdog,
                    name="bug-analysis-classifier",
                    cwd=self._working_dir(),
                    capture_output=True,
                    text=True,
                    timeout=min(self.config.bug_analysis.timeout_seconds, 300),
                    check=False,
                    debug_log_path=debug_log_path,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                last_error = exc
                if output_path is not None:
                    output_path.unlink(missing_ok=True)
                continue
            raw = self._read_bug_decision_response(completed=completed, output_path=output_path)
            if completed.returncode != 0:
                last_error = RuntimeError(raw or completed.stderr or completed.stdout or "bug decision agent failed")
                continue
            try:
                return self._parse_bug_decision_json(raw), provider
            except ValueError as exc:
                last_error = exc
                continue
        return None, ""

    def _build_bug_decision_command(self, provider: str, command_name: str, prompt: str) -> tuple[list[str], Path | None]:
        system_prompt = (
            "你是 Lark Agent Bridge 的结构化分类器。"
            "只能根据给定输入选择 skill 和分析动作，不能调用工具，不能假装读取额外文件。"
            "你必须只输出 JSON 对象。"
        )
        if provider == "codex":
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", prefix="lark-bug-decision-", delete=False) as fh:
                output_path = Path(fh.name)
            model = (self.config.bug_analysis.model or "").strip()
            command = [
                command_name,
                "exec",
                "--skip-git-repo-check",
                "-s",
                "read-only",
                "-C",
                str(self._working_dir()),
            ]
            if model:
                command.extend(["-m", model])
            command.extend(
                [
                    "--output-last-message",
                    str(output_path),
                    f"{system_prompt}\n\n{prompt}",
                ]
            )
            return command, output_path
        if provider in {"claude", "claude-code", "claude_code"}:
            command = [
                command_name,
                "--print",
                "--output-format",
                "text",
                "--no-session-persistence",
                "--permission-mode",
                "dontAsk",
                "--tools",
                "",
                "--append-system-prompt",
                system_prompt,
                prompt,
            ]
            return command, None
        return [], None

    def _read_bug_decision_response(self, *, completed: subprocess.CompletedProcess[str], output_path: Path | None) -> str:
        try:
            if output_path is not None and output_path.exists():
                content = output_path.read_text(encoding="utf-8").strip()
                if content:
                    return content
            return completed.stdout.strip()
        finally:
            if output_path is not None:
                output_path.unlink(missing_ok=True)

    def _parse_bug_decision_json(self, raw_response: str) -> dict[str, object]:
        cleaned = raw_response.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned)
        if not cleaned.startswith("{"):
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start == -1 or end == -1 or end <= start:
                raise ValueError("no JSON object found")
            cleaned = cleaned[start : end + 1]
        parsed = json.loads(cleaned)
        if not isinstance(parsed, dict):
            raise ValueError("response must be a JSON object")
        return parsed

    def _prompt_has_explicit_signal_target(self, prompt_text: str) -> bool:
        signal_request = parse_signal_request(
            prompt_text,
            signal_aliases=self.config.signal_aliases,
            command_prefixes=self.config.command_prefixes,
            signal_resolver=self.signal_resolver,
        )
        return bool(signal_request.signal)

    def _normalize_agent_bug_selection(
        self,
        selection: "BugAnalysisSelection | None",
        *,
        prompt_text: str,
    ) -> "BugAnalysisSelection | None":
        if selection is None:
            return None
        if not any(plan.kind == "signal" for plan in selection.plans):
            return selection
        if self._prompt_has_explicit_signal_target(prompt_text):
            return selection
        return None

    def _has_explicit_3d_lifecycle_intent(self, text: str) -> bool:
        lowered = (text or "").casefold().replace(" ", "")
        return any(
            term in lowered
            for term in (
                "3d生命周期",
                "unity生命周期",
                "surface生命周期",
                "sr生命周期",
            )
        )

    def _downgrade_lifecycle_stuck_conflict(
        self,
        selection: "BugAnalysisSelection",
        *,
        prompt_text: str,
        title: str,
        description: str,
    ) -> "BugAnalysisSelection":
        if not self._has_explicit_3d_lifecycle_intent(prompt_text):
            return selection
        has_lifecycle_or_stuck_plan = any(plan.kind in {"startup", "stuck"} for plan in selection.plans)
        if not has_lifecycle_or_stuck_plan:
            return selection
        if not any(term in f"{title}\n{description}".casefold() for term in ("黑屏", "不显示", "卡顿", "卡住")):
            return selection
        selection.confidence = "low"
        reason = selection.reason.strip()
        suffix = "用户明确要求 3D 生命周期，但标题/描述也包含黑屏/不显示/卡顿，存在 lifecycle 与 stuck skill 冲突，需要用户确认。"
        selection.reason = f"{reason}；{suffix}" if reason else suffix
        return selection

    def _bug_skill_confirmation_options(
        self,
        selection: "BugAnalysisSelection",
        *,
        request_text: str = "",
    ) -> list[dict[str, object]]:
        if self._has_explicit_3d_lifecycle_intent(request_text):
            options: list[dict[str, object]] = []

            def add_skill(index: int, skill_name: str, label: str, aliases: list[str]) -> None:
                options.append(
                    {
                        "index": index,
                        "type": "skill",
                        "skill_name": skill_name,
                        "label": label,
                        "aliases": aliases,
                    }
                )

            add_skill(
                1,
                "unity-startup-lifecycle-check",
                "3D启动/Surface生命周期分析",
                ["3D启动时序分析", "生命周期", "3D生命周期", "启动时序", "Surface生命周期"],
            )
            add_skill(
                2,
                "3d-stuck-investigate",
                "3D卡顿/黑屏渲染分析",
                ["3D卡顿分析", "卡顿", "黑屏", "渲染黑屏"],
            )
            options.append(
                {
                    "index": 3,
                    "type": "plans",
                    "label": "两个方向都跑",
                    "plan_kinds": ["startup", "stuck"],
                    "skill_name": "startup+stuck",
                    "aliases": ["都跑", "两个都跑", "全部", "两个方向都跑", "启动和卡顿"],
                }
            )
            return options

        label = selection.skill_label or selection.skill_name
        return [
            {
                "index": 1,
                "type": "skill",
                "skill_name": selection.skill_name,
                "label": label,
                "aliases": [label],
            }
        ]

    def _needs_skill_confirmation(self, selection: "BugAnalysisSelection") -> bool:
        """Gate a low-confidence specific-skill pick before the expensive run."""
        if not self.config.bug_analysis.confirm_low_confidence_skill:
            return False
        if selection.confidence != "low":
            return False
        if selection.skill_name in {"", "general"}:
            return False  # general is already handled by _needs_general_direction
        return not any(plan.kind == "general" for plan in selection.plans)

    def _skill_confirmation_needed_result(
        self,
        *,
        context,
        selection: "BugAnalysisSelection",
        started: float,
        request_text: str,
        bug_url: str,
        title: str,
        progress_callback: Callable[[dict[str, object]], None] | None,
    ) -> TaskResult:
        self._emit_progress(
            progress_callback,
            stage="bug_skill_confirmation_needed",
            message="Skill 分类置信度偏低，先请用户确认分析方向",
            job_id=context.job_id,
            bug_url=bug_url,
            title=title,
            classification_skill=selection.skill_name,
            classification_source=selection.source,
            classification_reason=selection.reason,
        )
        label = selection.skill_label or selection.skill_name
        options = self._bug_skill_confirmation_options(selection, request_text=request_text)
        option_lines = "\n".join(
            f"{option['index']}. {option['label']}" for option in options
        )
        message = (
            f"我初步判定使用 **{label}** 分析"
            + (f"（理由：{selection.reason}）" if selection.reason else "")
            + "，但置信度偏低、可能选错预设。\n"
            "为避免错跑较久的分析，先请你确认方向。\n"
            "请直接回复下面任一选项的序号，或直接回复对应文本：\n"
            f"{option_lines}"
        )
        return TaskResult(
            success=True,
            message=message,
            skipped=True,
            job_id=context.job_id,
            job_dir=context.job_dir,
            duration_seconds=time.monotonic() - started,
            details={
                "mode": "bug_skill_confirmation",
                "analysis_kind": selection.plans[0].kind if selection.plans else "general",
                "analysis_kinds": [item.kind for item in selection.plans],
                "analysis_skill": selection.skill_name,
                "analysis_skill_label": label,
                "classification_source": selection.source,
                "classification_reason": selection.reason,
                "classification_provider": selection.provider,
                "classification_confidence": selection.confidence,
                "bug_url": bug_url,
                "user_request_text": request_text,
                "intent_options": options,
                "needs_user_direction": True,
                "supported_bug_skills": self.supported_primary_bug_skills(),
            },
        )
