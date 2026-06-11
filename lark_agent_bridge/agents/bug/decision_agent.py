from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from . import _shared
from ...parser import parse_addr2line_request


class _BugDecisionAgentMixin:
    """Bug 决策代理：LLM 子进程分类调用与选择结果规范化（与 ResolveSourceMixin 共享 self 状态）。"""

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
            "先判断是否有更专一的 primary skill；业务差异以输入 JSON 里的 primary_skills.name/kind/description/report_contract 为准，"
            "不要在决策器里臆造或覆盖 skill 规则。"
            "通用词只能作为辅助线索：如果文本只出现 signal/信号、卡顿、主题、启动等泛词，必须结合 primary_skills 描述和用户现象确认是否真的匹配专用 skill。"
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
