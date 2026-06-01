from __future__ import annotations

from ._shared import *  # noqa: F401,F403


class _GeneralSummaryMixin:
    def _build_summary_from_report(
        self,
        *,
        plan: "BugAnalysisPlan",
        report_json: Path | None,
        prompt_text: str,
        fault_time: str,
        html_path: Path,
        selected_input: Path | None,
    ) -> str:
        if report_json is None or not report_json.exists():
            log_input = selected_input.name if selected_input else "无日志，静态链路"
            return (
                "Bug 分析完成\n"
                f"类型: {self._analysis_label(plan.kind)}\n"
                f"描述: {prompt_text}\n"
                f"HTML: {html_path}\n"
                f"输入: {log_input}"
            )

        payload = json.loads(report_json.read_text(encoding="utf-8"))
        if plan.kind == "startup":
            verdict = payload.get("verdict", {}) if isinstance(payload, dict) else {}
            message = str(verdict.get("message", ""))
            sessions = payload.get("sessions", []) if isinstance(payload, dict) else []
            target_session = self._select_target_session(sessions, fault_time)
            duration_note = ""
            if target_session is not None:
                message = str(target_session.get("diagnosis", message or "启动时序报告已生成"))
                duration_note = (
                    f"\n故障时间主会话: Session {target_session.get('index', '?')} "
                    f"{target_session.get('start', '')}，状态 {target_session.get('status', '')}"
                )
            mismatch_note = self._time_match_note(fault_time, sessions)
            return (
                "Bug 分析完成\n"
                f"类型: {self._analysis_label(plan.kind)}\n"
                f"结论: {message or '启动时序报告已生成'}\n"
                f"描述: {prompt_text}\n"
                f"HTML: {html_path}\n"
                f"时间窗校验: {mismatch_note}{duration_note}"
            )

        if plan.kind in {"stuck", "crash"}:
            verdict = payload.get("verdict", {}) if isinstance(payload, dict) else {}
            sev = str(verdict.get("verdict_sev", "")).upper()
            msg = str(verdict.get("verdict_msg", ""))
            return (
                "Bug 分析完成\n"
                f"类型: {self._analysis_label(plan.kind)}\n"
                f"结论: [{sev or 'INFO'}] {msg or '已生成卡顿报告'}\n"
                f"描述: {prompt_text}\n"
                f"HTML: {html_path}"
            )

        if plan.kind == "perception":
            summary = payload.get("summary", {}) if isinstance(payload, dict) else {}
            verdict = summary.get("verdict", {}) if isinstance(summary, dict) else {}
            sev = str(verdict.get("sev", "")).upper()
            msg = str(verdict.get("msg", ""))
            return (
                "Bug 分析完成\n"
                f"类型: {self._analysis_label(plan.kind)}\n"
                f"结论: [{sev or 'INFO'}] {msg or '已生成当前感知数据总结'}\n"
                f"描述: {prompt_text}\n"
                f"HTML: {html_path}"
            )

        if plan.kind == "xtheme":
            verdict = payload.get("verdict", {}) if isinstance(payload, dict) else {}
            counts = payload.get("counts", {}) if isinstance(payload, dict) else {}
            issues = payload.get("issues", []) if isinstance(payload, dict) else []
            focus_snapshot = payload.get("focus_snapshot", []) if isinstance(payload, dict) else []
            msg = str(verdict.get("msg", "")) if isinstance(verdict, dict) else ""
            issue_lines: list[str] = []
            if isinstance(issues, list):
                for issue in issues[:3]:
                    if not isinstance(issue, dict):
                        continue
                    title = str(issue.get("title") or "").strip()
                    detail = str(issue.get("detail") or "").strip()
                    if title or detail:
                        issue_lines.append(f"- {title}: {detail}".strip())
            focus_lines: list[str] = []
            if isinstance(focus_snapshot, list):
                for item in focus_snapshot[:5]:
                    if not isinstance(item, dict):
                        continue
                    kind = str(item.get("kind") or "").strip()
                    value = str(item.get("value") or "").strip()
                    ts = str(item.get("ts") or "").strip()
                    source = str(item.get("source") or "").strip()
                    if kind or value:
                        focus_lines.append(f"- {ts} {kind}: {value} [{source}]".strip())
            counts_text = ""
            if isinstance(counts, dict) and counts:
                counts_text = ", ".join(f"{key}={value}" for key, value in counts.items())
            return (
                "Bug 分析完成\n"
                f"类型: {self._analysis_label(plan.kind)}\n"
                f"结论: {msg or '已生成 XTheme 主题链路报告'}\n"
                f"目标时间: {payload.get('target_time') or fault_time or '未识别'}\n"
                f"统计: {counts_text or '无'}\n"
                f"关键问题:\n{chr(10).join(issue_lines) if issue_lines else '- 无明确异常'}\n"
                f"问题时间证据:\n{chr(10).join(focus_lines) if focus_lines else '- 无问题时间快照'}\n"
                f"描述: {prompt_text}\n"
                f"HTML: {html_path}"
            )

        if plan.kind == "scene_signal":
            verdict = str(payload.get("verdict") or "已生成 3D 场景信号报告") if isinstance(payload, dict) else "已生成 3D 场景信号报告"
            target_focus = payload.get("target_focus", {}) if isinstance(payload, dict) else {}
            event_count = payload.get("event_count", "") if isinstance(payload, dict) else ""
            target_summary = str(target_focus.get("summary") or "").strip() if isinstance(target_focus, dict) else ""
            target_time = str(payload.get("target_time") or fault_time or "未识别") if isinstance(payload, dict) else (fault_time or "未识别")
            inferred_state = str(target_focus.get("inferred_state_summary") or "").strip() if isinstance(target_focus, dict) else ""
            prior_state_time = str(target_focus.get("latest_prior_state_time") or "").strip() if isinstance(target_focus, dict) else ""
            return (
                "Bug 分析完成\n"
                f"类型: {self._analysis_label(plan.kind)}\n"
                f"结论: {target_summary or verdict}\n"
                f"目标时间: {target_time}\n"
                f"脚本 verdict: {verdict}\n"
                f"目标时间前最近状态时间: {prior_state_time or '未命中'}\n"
                f"目标时间前最近状态: {inferred_state or '未命中'}\n"
                f"事件数: {event_count or '未知'}\n"
                f"描述: {prompt_text}\n"
                f"HTML: {html_path}"
            )

        if _kind_spec(plan.kind).is_agent_handled:
            verdict = payload.get("verdict", {}) if isinstance(payload, dict) else {}
            default_summary = (
                "已生成 LD车道级日志分析报告"
                if plan.kind == "ld_lane_level"
                else "已生成源码分析报告"
                if _kind_spec(plan.kind).is_source_stage
                else "已生成专用 Skill 源码分析入口"
                if plan.kind in _SOURCE_SKILL_KINDS
                else "已生成通用问题分析报告"
            )
            msg = str(verdict.get("text") or payload.get("summary") or default_summary)
            source_matches = payload.get("source_matches", 0) if isinstance(payload, dict) else 0
            skill = str(payload.get("analysis_skill") or self._skill_name_for_kind(plan.kind))
            return (
                "Bug 分析完成\n"
                f"类型: {self._analysis_label(plan.kind)}\n"
                f"Skill: {skill}\n"
                f"结论: {msg}\n"
                f"源码证据: {source_matches}\n"
                f"描述: {prompt_text}\n"
                f"HTML: {html_path}"
            )

        signal = payload.get("signal", {}) if isinstance(payload, dict) else {}
        signal_code = signal.get("code", plan.signal_code or "")
        summary = str(payload.get("summary", ""))
        log_report = payload.get("log_report", {}) if isinstance(payload, dict) else {}
        scanned_files = log_report.get("scanned_files", 0) if isinstance(log_report, dict) else 0
        return (
            "Bug 分析完成\n"
            f"类型: {self._analysis_label(plan.kind)}\n"
            f"信号: {signal_code}\n"
            f"结论: {summary or '已生成信号链路报告'}\n"
            f"扫描文件: {scanned_files}\n"
            f"HTML: {html_path}"
            )

    def _format_log_coverage_for_metadata(self, log_coverage: LogCoverage | None) -> str:
        if log_coverage is None:
            return "未检查"
        if not log_coverage.has_time_evidence:
            return "未识别有效日志时间"
        status = "覆盖问题时间" if log_coverage.covers_fault_time else "未覆盖问题时间"
        return f"{log_coverage.start_time} ~ {log_coverage.end_time}（{status}）"

    def _write_general_bug_report(
        self,
        *,
        html_path: Path,
        json_path: Path,
        title: str,
        description: str,
        prompt_text: str,
        request_text: str,
        fault_time: str,
        selected_input: Path | None,
        source_evidence_path: Path | None,
        classification_skill: str = "",
        classification_source: str = "",
        classification_reason: str = "",
    ) -> None:
        source_entries = self._parse_source_evidence_entries(source_evidence_path)
        source_rows = [
            (entry["file"], f"L{entry['line']}", entry["text"])
            for entry in source_entries[:12]
        ]
        has_logs = selected_input is not None
        verdict_sev = "green" if has_logs else "yellow"
        verdict_text = (
            "未命中专用日志脚本，已按通用问题分析处理；当前结论优先基于缺陷描述、源码证据和已有上下文。"
            if source_rows
            else "未命中专用日志脚本，且当前源码检索证据有限；建议补充更明确的业务关键词或现场日志后继续收敛。"
        )
        cards = [
            ("分析方式", "静态/通用", verdict_sev, "没有把未知请求强制改写成某个固定日志脚本。"),
            ("故障时间", fault_time or "未识别", "green" if fault_time else "yellow", ""),
            ("现场日志", selected_input.name if selected_input else "无，可静态分析", "green" if has_logs else "yellow", ""),
            ("源码证据", str(len(source_rows)), "green" if source_rows else "yellow", "按业务词和源码定义做本地检索。"),
            ("命中 Skill", classification_skill or "general", "green", classification_source or "manual_fallback"),
        ]
        issues = [
            {
                "sev": verdict_sev,
                "title": "路由策略",
                "detail": "当前请求未匹配 startup/stuck/crash/scene_signal/perception/signal 等专用脚本，因此回落到通用问题分析，而不是再默认启动时序。",
            },
            {
                "sev": "yellow" if not has_logs else "green",
                "title": "现场证据",
                "detail": "没有可复用日志时，只能基于缺陷描述和源码证据给出静态判断，不直接替代现场定案。"
                if not has_logs
                else f"当前可复用日志输入：{selected_input}",
            },
            {
                "sev": "green" if source_rows else "yellow",
                "title": "源码落点",
                "detail": f"已命中 {len(source_rows)} 条源码证据，可继续围绕这些文件追踪业务链路。"
                if source_rows
                else "当前没有检索到稳定源码落点，说明问题描述还不够具体。",
            },
        ]
        summary_sections = build_structured_summary_sections(
            conclusions=[
                {"sev": verdict_sev, "title": "通用分析结论", "detail": verdict_text},
                {
                    "sev": "green" if source_rows else "yellow",
                    "title": "源码证据状态",
                    "detail": f"已命中 {len(source_rows)} 条源码证据。" if source_rows else "当前没有命中稳定源码落点。",
                },
                {
                    "sev": "green" if has_logs else "yellow",
                    "title": "现场日志状态",
                    "detail": f"当前可复用日志输入：{selected_input}" if has_logs else "当前无可复用日志，结论不能替代现场定案。",
                },
            ],
            evidence_rows=self._general_summary_evidence_rows(
                fault_time=fault_time,
                selected_input=selected_input,
                source_rows=source_rows,
                classification_skill=classification_skill or "general",
                classification_source=classification_source or "manual_fallback",
            ),
            causes=[
                {
                    "sev": "green" if source_rows else "yellow",
                    "title": "当前可解释方向",
                    "detail": "优先围绕已命中的源码落点和业务描述继续追踪。"
                    if source_rows
                    else "缺少日志和稳定源码证据时，不强行给出具体根因。",
                },
                {
                    "sev": verdict_sev,
                    "title": "路由原因",
                    "detail": classification_reason or "未命中专用日志脚本，按通用分析处理。",
                },
            ],
            confirmations=self._general_summary_confirmations(
                fault_time=fault_time,
                has_logs=has_logs,
                source_rows=source_rows,
            ),
            actions=self._general_summary_actions(has_logs=has_logs, source_rows=source_rows),
        )
        context_rows = [
            ("Bug 标题", title or "未返回 / 未设置"),
            ("分析请求", prompt_text or "未设置"),
            ("故障时间", fault_time or "未识别"),
            ("现场日志", str(selected_input) if selected_input else "无，可静态分析"),
            ("源码证据文件", str(source_evidence_path) if source_evidence_path else "未生成"),
            ("命中 Skill", classification_skill or "general"),
            ("分类来源", classification_source or "manual_fallback"),
            ("分类理由", classification_reason or "未记录"),
        ]
        flow_nodes = [
            {"tag": "REQUEST", "title": "用户问题", "meta": prompt_text or request_text, "note": "原始问题 / 追问文本"},
            {
                "tag": "SKILL",
                "title": classification_skill or "general",
                "meta": classification_source or "manual_fallback",
                "note": classification_reason or "未记录分类理由",
            },
            {
                "tag": "INPUT",
                "title": "现场日志状态",
                "meta": str(selected_input) if selected_input else "无，可静态分析",
                "note": "需要日志的 skill 会在续聊时自动重试下载。",
            },
            {
                "tag": "SOURCE",
                "title": "源码证据",
                "meta": f"{len(source_rows)} 条命中",
                "note": str(source_evidence_path) if source_evidence_path else "未生成源码证据文件",
            },
            {"tag": "OUTPUT", "title": "结论输出", "meta": verdict_text, "note": "最终结论由本地 Agent 继续归纳。"},
        ]
        raw_description = description.strip() or "(无描述)"
        raw_request = request_text.strip() or "(无请求)"
        detail_body = (
            "<div class=\"split-grid\">"
            f"{combined_bug_html.render_table([('原始请求', raw_request)], ('字段', '内容'))}"
            f"{combined_bug_html.render_table([('缺陷描述', raw_description)], ('字段', '内容'))}"
            "</div>"
        )
        composition = ReportComposition(
            title="通用问题分析",
            heading="通用问题分析",
            subtitle=f"Bug 标题：{title or '未返回 / 未设置'}",
            verdict=ReportVerdict(sev=verdict_sev, text=verdict_text),
            cards=cards,
            sections=summary_sections
            + [
                ReportSection(kind="issues", title="当前判断", items=issues, empty_text="未生成判断"),
                ReportSection(kind="flow", title="分析路径", nodes=flow_nodes, empty_text="未生成分析路径"),
                ReportSection(kind="table", title="源码证据", cols=["文件", "行号", "内容"], rows=source_rows, empty_text="未命中源码证据"),
                ReportSection(kind="table", title="分析上下文", cols=["字段", "内容"], rows=context_rows),
                ReportSection(kind="details", title="原始输入", summary="展开查看请求与缺陷描述", body_html=detail_body),
            ],
        )
        payload = {
            "mode": "general_bug_overview",
            "summary": verdict_text,
            "verdict": {"sev": verdict_sev, "text": verdict_text},
            "fault_time": fault_time,
            "selected_input": str(selected_input) if selected_input else "",
            "source_evidence_file": str(source_evidence_path) if source_evidence_path else "",
            "source_matches": len(source_rows),
            "analysis_skill": classification_skill or "general",
            "classification_source": classification_source or "manual_fallback",
            "classification_reason": classification_reason or "",
            "title": title,
            "prompt_text": prompt_text,
            "request_text": request_text,
            "description": raw_description,
        }
        html_path.write_text(
            combined_bug_html.render_report_shell(**composition_to_renderer_payload(composition)),
            encoding="utf-8",
        )
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _write_custom_skill_bug_report(
        self,
        *,
        html_path: Path,
        json_path: Path,
        title: str,
        description: str,
        prompt_text: str,
        request_text: str,
        fault_time: str,
        selected_input: Path | None,
        source_evidence_path: Path | None,
        classification_skill: str,
        classification_source: str = "",
        classification_reason: str = "",
    ) -> None:
        skill_paths = self._skill_context_paths(classification_skill)
        skill_md = next((path for path in skill_paths if path.name == "SKILL.md"), None)
        source_entries = self._parse_source_evidence_entries(source_evidence_path)
        source_rows = [(entry["file"], f"L{entry['line']}", entry["text"]) for entry in source_entries[:8]]
        has_logs = selected_input is not None
        is_source_analysis = classification_skill.strip() == "source_analysis"
        verdict_sev = "green" if has_logs and (skill_md is not None or is_source_analysis) else "yellow"
        verdict_text = (
            "已识别为源码导向文件分析，本轮将由本地 Agent 基于日志、源码证据和用户给出的源码线索整理最终结论。"
            if is_source_analysis
            else f"已命中专用 Skill `{classification_skill}`，本轮将由本地 Agent 按 Skill 规范读取源码/证据并产出最终结论。"
            if skill_md is not None
            else f"已命中专用 Skill `{classification_skill}`，但未找到 SKILL.md，当前只能保留材料索引并等待补齐 skill。"
        )
        cards = [
            ("分析方式", "源码导向文件分析 + Agent" if is_source_analysis else "专用 Skill 源码分析 + Agent", verdict_sev, "没有回落为通用问题分析。"),
            ("命中 Skill", classification_skill or "未记录", "green" if classification_skill else "yellow", classification_source or "manual_fallback"),
            ("故障时间", fault_time or "未识别", "green" if fault_time else "yellow", ""),
            ("现场日志", selected_input.name if selected_input else "无", "green" if has_logs else "yellow", str(selected_input or "")),
            ("Skill 规范", skill_md.name if skill_md else "内部源码导向规则" if is_source_analysis else "缺失", "green" if (skill_md or is_source_analysis) else "yellow", str(skill_md or "")),
            ("源码证据", str(len(source_rows)), "green" if source_rows else "yellow", "按用户诉求预检索。"),
        ]
        summary_sections = build_structured_summary_sections(
            conclusions=[
                {"sev": verdict_sev, "title": "专用 Skill 已命中", "detail": verdict_text},
                {
                    "sev": "green" if has_logs else "yellow",
                    "title": "日志输入",
                    "detail": f"当前可复用日志输入：{selected_input}" if has_logs else "当前没有可复用日志，无法执行日志型专用 Skill。",
                },
                {
                    "sev": "green" if (skill_md or is_source_analysis) else "yellow",
                    "title": "Skill 规范",
                    "detail": "当前使用内部源码导向分析路径，不要求匹配现有 SKILL.md。"
                    if is_source_analysis
                    else f"Agent 需要先读取 `{skill_md}` 并按其 Required Workflow 执行。"
                    if skill_md
                    else "缺少 SKILL.md。",
                },
            ],
            evidence_rows=[
                ("分类路由", classification_skill or "未记录", "bridge/agent", classification_source or "manual_fallback", classification_reason or "未记录"),
                ("故障时间", fault_time or "未识别", "用户输入/标题/描述", "用于限定日志窗口", ""),
                ("日志输入", str(selected_input or "无"), "附件/缓存", "专用 Skill 的主要运行材料", ""),
                ("Skill 文件", str(skill_md or "内部源码导向规则"), "workspace/.ai/skills", "Agent 分析规范入口", ""),
            ],
            causes=[
                {
                    "sev": "yellow",
                    "title": "脚本覆盖",
                    "detail": "当前路径没有 Bridge 内置业务脚本结论，最终质量依赖本地 Agent 对日志、源码证据和用户线索的综合整理。"
                    if is_source_analysis
                    else "该 Skill 当前没有 Bridge 内置 Python 执行器，因此报告主体依赖本地 Agent 按 SKILL.md 做只读分析。",
                }
            ],
            confirmations=[
                {
                    "sev": "yellow",
                    "title": "最终结论",
                    "detail": "需要等待 Agent 读取日志/源码后写入；如果 Agent 超时，报告会明确标注超时而不是伪装成通用根因。",
                }
            ],
            actions=[
                {"sev": "green", "title": "按专用 Skill 继续", "detail": "后续追问会复用当前 bug、已下载日志、Skill 规范和 Agent 会话。"}
            ],
        )
        skill_rows = [(path.name, str(path)) for path in skill_paths]
        context_rows = [
            ("Bug 标题", title or "未返回 / 未设置"),
            ("分析请求", prompt_text or "未设置"),
            ("故障时间", fault_time or "未识别"),
            ("现场日志", str(selected_input) if selected_input else "无"),
            ("源码证据文件", str(source_evidence_path) if source_evidence_path else "未生成"),
            ("分类理由", classification_reason or "未记录"),
        ]
        detail_body = (
            "<div class=\"split-grid\">"
            f"{combined_bug_html.render_table([('原始请求', request_text.strip() or '(无请求)')], ('字段', '内容'))}"
            f"{combined_bug_html.render_table([('缺陷描述', description.strip() or '(无描述)')], ('字段', '内容'))}"
            "</div>"
        )
        composition = ReportComposition(
            title="专用 Skill 源码分析",
            heading="专用 Skill 源码分析",
            subtitle=f"Bug 标题：{title or '未返回 / 未设置'}",
            verdict=ReportVerdict(sev=verdict_sev, text=verdict_text),
            cards=cards,
            sections=summary_sections
            + [
                ReportSection(kind="table", title="Skill 输入", cols=["文件", "路径"], rows=skill_rows, empty_text="未找到 Skill 文件"),
                ReportSection(kind="table", title="源码预检索", cols=["文件", "行号", "内容"], rows=source_rows, empty_text="未命中源码预检索证据"),
                ReportSection(kind="table", title="分析上下文", cols=["字段", "内容"], rows=context_rows),
                ReportSection(kind="details", title="原始输入", summary="展开查看请求与缺陷描述", body_html=detail_body),
            ],
        )
        payload = {
            "mode": "custom_skill_overview",
            "summary": verdict_text,
            "verdict": {"sev": verdict_sev, "text": verdict_text},
            "fault_time": fault_time,
            "selected_input": str(selected_input) if selected_input else "",
            "source_evidence_file": str(source_evidence_path) if source_evidence_path else "",
            "source_matches": len(source_rows),
            "analysis_skill": classification_skill,
            "classification_source": classification_source or "manual_fallback",
            "classification_reason": classification_reason or "",
            "skill_context_files": [str(path) for path in skill_paths],
            "title": title,
            "prompt_text": prompt_text,
            "request_text": request_text,
            "description": description.strip(),
        }
        html_path.write_text(
            combined_bug_html.render_report_shell(**composition_to_renderer_payload(composition)),
            encoding="utf-8",
        )
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _general_summary_evidence_rows(
        self,
        *,
        fault_time: str,
        selected_input: Path | None,
        source_rows: list[tuple[object, ...]],
        classification_skill: str,
        classification_source: str,
    ) -> list[tuple[object, ...]]:
        rows: list[tuple[object, ...]] = []
        if fault_time:
            rows.append(("故障时间", fault_time, "用户请求/缺陷描述", "已识别分析时间点", "限定后续日志和源码追踪窗口"))
        rows.append(
            (
                "现场日志",
                str(selected_input) if selected_input else "无",
                "附件/缓存",
                "已取得可复用日志输入" if selected_input else "当前没有可复用日志输入",
                "决定结论置信边界",
            )
        )
        rows.append(("分类路由", classification_skill or "general", "bridge/agent", classification_source or "manual_fallback", "决定是否调用专用 skill"))
        for file_name, line, text in source_rows[:5]:
            rows.append(("源码", f"{file_name} {line}".strip(), "业务源码", text, "支撑静态分析落点"))
        return rows[:8]

    def _general_summary_confirmations(
        self,
        *,
        fault_time: str,
        has_logs: bool,
        source_rows: list[tuple[object, ...]],
    ) -> list[dict[str, object]]:
        items: list[dict[str, object]] = []
        if not fault_time:
            items.append({"sev": "yellow", "title": "故障时间", "detail": "未识别精确时间，后续日志分析需要先补齐时间窗口。"})
        if not has_logs:
            items.append({"sev": "yellow", "title": "现场日志", "detail": "当前无可复用日志，无法验证运行时是否真的经过源码落点。"})
        if not source_rows:
            items.append({"sev": "yellow", "title": "源码落点", "detail": "源码证据不足，需要更明确的业务词、信号名、类名或调用链线索。"})
        return items

    def _general_summary_actions(
        self,
        *,
        has_logs: bool,
        source_rows: list[tuple[object, ...]],
    ) -> list[dict[str, object]]:
        actions: list[dict[str, object]] = []
        if not has_logs:
            actions.append({"sev": "yellow", "title": "补齐或重试日志", "detail": "下一轮如果命中需要日志的 skill，bridge 会优先重试下载并报告失败原因。"})
        if source_rows:
            actions.append({"sev": "green", "title": "沿源码证据追踪", "detail": "优先从命中的源码文件继续查生产者、状态更新和消费链路。"})
        actions.append({"sev": "green", "title": "续聊时保留上下文", "detail": "后续追问继续复用当前 bug、报告、源码证据和已下载日志缓存。"})
        return actions

    def _build_combined_report_artifacts(
        self,
        *,
        plans: list["BugAnalysisPlan"],
        prompt_text: str,
        fault_time: str,
        output_dir: Path,
        html_paths: list[Path],
        report_jsons: dict[str, Path | None],
        selected_input: Path | None,
        source_evidence_path: Path | None = None,
    ) -> dict[str, object] | None:
        kinds = [plan.kind for plan in plans]
        if kinds == ["startup", "stuck"]:
            startup_json_path = report_jsons.get("startup")
            stuck_json_path = report_jsons.get("stuck")
            if startup_json_path is None or stuck_json_path is None:
                return None
            if not startup_json_path.exists() or not stuck_json_path.exists():
                return None

            startup_payload = json.loads(startup_json_path.read_text(encoding="utf-8"))
            stuck_payload = json.loads(stuck_json_path.read_text(encoding="utf-8"))
            summary = self._build_combined_summary_text(startup_payload, stuck_payload, prompt_text, fault_time)
            html_path = output_dir / self._combined_report_name("html")
            json_path = output_dir / self._combined_report_name("json")
            html_path.write_text(
                self._render_combined_startup_stuck_html(
                    startup_payload=startup_payload,
                    stuck_payload=stuck_payload,
                    prompt_text=prompt_text,
                    fault_time=fault_time,
                    startup_html=next((path for path in html_paths if path.name == self._report_name("startup", "html")), None),
                    stuck_html=next((path for path in html_paths if path.name == self._report_name("stuck", "html")), None),
                    selected_input=selected_input,
                ),
                encoding="utf-8",
            )
            combined_payload = {
                "mode": "startup_stuck_combined",
                "prompt_text": prompt_text,
                "fault_time": fault_time,
                "selected_input": str(selected_input) if selected_input else "",
                "summary": summary,
                "startup": startup_payload,
                "stuck": stuck_payload,
            }
            json_path.write_text(json.dumps(combined_payload, ensure_ascii=False, indent=2), encoding="utf-8")
            return {
                "html_path": html_path,
                "json_path": json_path,
                "summary": summary,
            }

        if kinds == ["signal"]:
            signal_json_path = report_jsons.get("signal")
            if signal_json_path is None or not signal_json_path.exists():
                return None
            signal_payload = json.loads(signal_json_path.read_text(encoding="utf-8"))
            focus_scope = self._signal_focus_scope(signal_payload, fault_time)
            android_data_link = self._signal_android_data_link_checks(
                signal_payload,
                focus_scope,
                selected_input=selected_input,
                source_evidence_path=source_evidence_path,
            )
            summary = self._build_signal_overview_summary_text(
                signal_payload,
                prompt_text,
                fault_time,
                focus_scope,
                android_data_link=android_data_link,
            )
            signal_overview_payload = dict(signal_payload)
            signal_overview_payload["summary"] = self._signal_scoped_summary_text(signal_payload)
            html_path = output_dir / self._signal_overview_report_name("html")
            json_path = output_dir / self._signal_overview_report_name("json")
            html_path.write_text(
                self._render_signal_bug_overview_html(
                    signal_payload=signal_payload,
                    prompt_text=prompt_text,
                    fault_time=fault_time,
                    selected_input=selected_input,
                    source_evidence_path=source_evidence_path,
                    focus_scope=focus_scope,
                    android_data_link=android_data_link,
                ),
                encoding="utf-8",
            )
            json_path.write_text(
                json.dumps(
                    {
                        "mode": "signal_overview_combined",
                        "prompt_text": prompt_text,
                        "fault_time": fault_time,
                        "selected_input": str(selected_input) if selected_input else "",
                        "source_evidence_file": str(source_evidence_path) if source_evidence_path else "",
                        "summary": summary,
                        "focus_scope": focus_scope,
                        "android_data_link": android_data_link,
                        "signal": signal_overview_payload,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            return {
                "html_path": html_path,
                "json_path": json_path,
                "summary": summary,
            }

        return None

    def _build_combined_summary_text(
        self,
        startup_payload: dict[str, object],
        stuck_payload: dict[str, object],
        prompt_text: str,
        fault_time: str,
    ) -> str:
        startup_verdict = startup_payload.get("verdict", {}) if isinstance(startup_payload, dict) else {}
        stuck_target_verdict = stuck_payload.get("target_verdict", {}) if isinstance(stuck_payload, dict) else {}
        stuck_verdict = stuck_payload.get("verdict", {}) if isinstance(stuck_payload, dict) else {}
        focus_pid = startup_payload.get("focus_session_pid", "")
        boot_relation = startup_payload.get("boot_relation", {}) if isinstance(startup_payload, dict) else {}
        system_load = startup_payload.get("system_load", {}) if isinstance(startup_payload, dict) else {}
        startup_message = str(startup_verdict.get("message", "已生成启动分析"))
        if isinstance(stuck_target_verdict, dict) and stuck_target_verdict.get("message"):
            stuck_message = str(stuck_target_verdict.get("message"))
        else:
            stuck_message = str(stuck_verdict.get("verdict_msg", "已生成卡顿分析"))
        load_text = "未命中"
        if isinstance(system_load, dict) and system_load:
            load_text = (
                f"Total {system_load.get('total_cpu', '?')}% / "
                f"System {system_load.get('system_cpu', '?')}% / "
                f"iow {system_load.get('iow_cpu', '?')}%"
            )
        return (
            "Bug 分析完成\n"
            "类型: 3D启动卡顿综合报告\n"
            f"描述: {prompt_text}\n"
            f"故障时间: {fault_time or '未识别'}\n"
            f"主会话 PID: {focus_pid or '未识别'}\n"
            f"启动结论: {startup_message}\n"
            f"卡顿结论: {stuck_message}\n"
            f"ROM/Boot: {boot_relation.get('note', '未识别')}\n"
            f"启动时刻系统负载: {load_text}\n"
            f"HTML: {self._combined_report_name('html')}"
        )

    def _render_combined_startup_stuck_html(
        self,
        *,
        startup_payload: dict[str, object],
        stuck_payload: dict[str, object],
        prompt_text: str,
        fault_time: str,
        startup_html: Path | None,
        stuck_html: Path | None,
        selected_input: Path | None,
    ) -> str:
        composition = self._plan_startup_stuck_report(
            startup_payload=startup_payload,
            stuck_payload=stuck_payload,
            prompt_text=prompt_text,
            fault_time=fault_time,
            startup_html=startup_html,
            stuck_html=stuck_html,
            selected_input=selected_input,
        )
        return combined_bug_html.render_report_shell(**composition_to_renderer_payload(composition))

    def _combined_ig_text(self, ig_context: object, power_context: object) -> str:
        after = ig_context.get("after") if isinstance(ig_context, dict) else None
        if isinstance(after, dict) and after.get("timestamp") and after.get("value") is not None:
            return f"启动后最近 IG={after.get('value')} @ {after.get('timestamp')}"
        render_ctx = power_context.get("render_anomaly_context", {}) if isinstance(power_context, dict) else {}
        if isinstance(render_ctx, dict) and render_ctx.get("category"):
            return f"卡顿侧上下电分类={render_ctx.get('category')}"
        return "未识别到明确上下电样本"

    def _combined_stuck_context_text(
        self,
        stuck_payload: dict[str, object],
        target_context: object,
        app_pid_filter: object,
    ) -> str:
        stuck_verdict = stuck_payload.get("verdict", {}) if isinstance(stuck_payload, dict) else {}
        target_verdict = stuck_payload.get("target_verdict", {}) if isinstance(stuck_payload, dict) else {}
        pid_desc = ""
        if isinstance(app_pid_filter, dict) and app_pid_filter.get("selected_pid"):
            pid_desc = f"应用层 PID={app_pid_filter.get('selected_pid')}；"
        if isinstance(target_context, dict) and target_context.get("target"):
            pid_desc += f"目标时间窗={target_context.get('target')}；"
        message = (
            str(target_verdict.get("message"))
            if isinstance(target_verdict, dict) and target_verdict.get("message")
            else str(stuck_verdict.get("verdict_msg", "已生成卡顿报告"))
        )
        return pid_desc + message

    def _plan_startup_stuck_report(
        self,
        *,
        startup_payload: dict[str, object],
        stuck_payload: dict[str, object],
        prompt_text: str,
        fault_time: str,
        startup_html: Path | None,
        stuck_html: Path | None,
        selected_input: Path | None,
    ) -> ReportComposition:
        startup_verdict = startup_payload.get("verdict", {}) if isinstance(startup_payload, dict) else {}
        stuck_target_verdict = stuck_payload.get("target_verdict", {}) if isinstance(stuck_payload, dict) else {}
        stuck_verdict = stuck_payload.get("verdict", {}) if isinstance(stuck_payload, dict) else {}
        startup_message = str(startup_verdict.get("message", "已生成启动分析"))
        startup_sev = str(startup_verdict.get("severity", "yellow"))
        stuck_message = (
            str(stuck_target_verdict.get("message"))
            if isinstance(stuck_target_verdict, dict) and stuck_target_verdict.get("message")
            else str(stuck_verdict.get("verdict_msg", "已生成卡顿分析"))
        )
        stuck_sev = (
            str(stuck_target_verdict.get("sev", "yellow"))
            if isinstance(stuck_target_verdict, dict) and stuck_target_verdict.get("sev")
            else str(stuck_verdict.get("verdict_sev", "yellow"))
        )
        focus_pid = startup_payload.get("focus_session_pid", "")
        focus_session_index = startup_payload.get("focus_session_index", "")
        boot_relation = startup_payload.get("boot_relation", {}) if isinstance(startup_payload, dict) else {}
        system_load = startup_payload.get("system_load", {}) if isinstance(startup_payload, dict) else {}
        ig_context = startup_payload.get("ig_context", {}) if isinstance(startup_payload, dict) else {}
        power_context = stuck_payload.get("power_context", {}) if isinstance(stuck_payload, dict) else {}
        app_pid_filter = stuck_payload.get("app_pid_filter", {}) if isinstance(stuck_payload, dict) else {}
        target_context = stuck_payload.get("target_context", {}) if isinstance(stuck_payload, dict) else {}
        cards = [
            ("分析类型", "3D启动卡顿综合", "green", "启动链路与卡顿窗口合并输出"),
            ("故障时间", fault_time or "未识别", "green" if fault_time else "yellow", ""),
            ("主会话 PID", focus_pid or "未识别", "green" if focus_pid else "yellow", f"Session {focus_session_index or '?'}"),
            ("ROM 启动邻近", "是" if boot_relation.get("is_near_boot") else "否", "yellow" if boot_relation.get("is_near_boot") else "green", str(boot_relation.get("note", ""))),
            (
                "启动时刻系统负载",
                (
                    f"Total {system_load.get('total_cpu', '?')}% / iow {system_load.get('iow_cpu', '?')}%"
                    if isinstance(system_load, dict) and system_load
                    else "未命中"
                ),
                "yellow" if isinstance(system_load, dict) and int(system_load.get("total_cpu", 0) or 0) >= 70 else "green",
                (
                    f"进程 CPU {system_load.get('process_cpu', '?')}% / RSS {system_load.get('process_mem_rss_kb', '?')}KB"
                    if isinstance(system_load, dict) and system_load
                    else ""
                ),
            ),
            ("启动链路", startup_sev.upper(), startup_sev, startup_message),
            ("卡顿窗口", stuck_sev.upper(), stuck_sev, stuck_message),
        ]
        issues: list[dict[str, object]] = []
        for item in startup_verdict.get("issues", []) if isinstance(startup_verdict, dict) else []:
            if isinstance(item, dict):
                issues.append(item)
        if isinstance(stuck_target_verdict, dict) and stuck_target_verdict.get("message"):
            issues.append({"sev": stuck_sev, "title": "目标时间窗卡顿结论", "detail": stuck_message})
        elif isinstance(stuck_verdict, dict) and stuck_verdict.get("verdict_msg"):
            issues.append({"sev": stuck_sev, "title": "卡顿结论", "detail": stuck_message})
        chain_nodes = [
            {
                "sev": "green" if focus_pid else "yellow",
                "title": "目标时间锁主会话与主 PID",
                "evidence": f"故障时间 {fault_time or '未识别'} -> Session {focus_session_index or '?'} / PID {focus_pid or '未识别'}",
                "downstream": "后续启动链与卡顿证据统一围绕同一主会话展开，避免把恢复后的新进程混入。",
            },
            {
                "sev": "yellow" if boot_relation.get("is_near_boot") else "green",
                "title": "ROM 启动邻近与上下电上下文",
                "evidence": (
                    str(boot_relation.get("note", "未识别"))
                    + "；"
                    + self._combined_ig_text(ig_context, power_context)
                ),
                "downstream": "如果问题发生在整机刚启动或特殊上下电阶段，启动卡顿结论需要附带环境说明，避免误判稳定期异常。",
            },
            {
                "sev": startup_sev,
                "title": "启动链路主卡点",
                "evidence": startup_message,
                "downstream": "用于判断 Application / Surface / UnityReady / 首帧 哪一段真正断开。",
            },
            {
                "sev": stuck_sev,
                "title": "卡顿窗口系统与渲染压力",
                "evidence": self._combined_stuck_context_text(stuck_payload, target_context, app_pid_filter),
                "downstream": "补充目标时间窗内的 Watchdog / UnityRequest / 系统 iow / CPU 压力，判断是不是启动后继续卡住。",
            },
        ]
        target_rows = [
            ("故障时间", fault_time or "未识别"),
            ("主会话 PID", str(focus_pid or "未识别")),
            ("会话选择", str(startup_payload.get("focus_reason", "未记录"))),
            ("启动报告", startup_html.name if startup_html else self._report_name("startup", "html")),
            ("卡顿报告", stuck_html.name if stuck_html else self._report_name("stuck", "html")),
            ("原始日志输入", str(selected_input or "")),
        ]
        system_rows = []
        if isinstance(system_load, dict) and system_load:
            system_rows.append(
                (
                    system_load.get("timestamp", ""),
                    f"{system_load.get('total_cpu', '?')}%",
                    f"{system_load.get('user_cpu', '?')}%",
                    f"{system_load.get('system_cpu', '?')}%",
                    f"{system_load.get('iow_cpu', '?')}%",
                    f"{system_load.get('process_cpu', '?')}%",
                )
            )
        return plan_startup_stuck_report(
            prompt_text=combined_bug_html.H(prompt_text),
            fault_time=fault_time,
            startup_html_name=combined_bug_html.H(startup_html or self._report_name("startup", "html")),
            stuck_html_name=combined_bug_html.H(stuck_html or self._report_name("stuck", "html")),
            selected_input=str(selected_input or ""),
            startup_message=startup_message,
            startup_sev=startup_sev,
            stuck_message=stuck_message,
            stuck_sev=stuck_sev,
            focus_pid=str(focus_pid or ""),
            focus_session_index=str(focus_session_index or ""),
            boot_relation_is_near_boot=bool(boot_relation.get("is_near_boot")),
            boot_relation_note=str(boot_relation.get("note", "")),
            startup_load_value=(
                f"Total {system_load.get('total_cpu', '?')}% / iow {system_load.get('iow_cpu', '?')}%"
                if isinstance(system_load, dict) and system_load
                else "未命中"
            ),
            startup_load_desc=(
                f"进程 CPU {system_load.get('process_cpu', '?')}% / RSS {system_load.get('process_mem_rss_kb', '?')}KB"
                if isinstance(system_load, dict) and system_load
                else ""
            ),
            startup_load_sev="yellow" if isinstance(system_load, dict) and int(system_load.get("total_cpu", 0) or 0) >= 70 else "green",
            issues=issues,
            chain_nodes=chain_nodes,
            target_rows=target_rows,
            system_rows=system_rows,
            system_cols=["时间", "Total", "User", "System", "iow", "进程 CPU"],
        )

    def _plan_signal_report(
        self,
        *,
        signal_payload: dict[str, object],
        prompt_text: str,
        fault_time: str,
        selected_input: Path | None,
        source_evidence_path: Path | None,
        focus_scope: dict[str, object],
        android_data_link: list[dict[str, object]] | None = None,
    ) -> ReportComposition:
        signal = signal_payload.get("signal", {}) if isinstance(signal_payload, dict) else {}
        signal_name = str(signal.get("name") or signal.get("code") or "未知信号")
        signal_code = str(signal.get("code") or "")
        signal_comment = str(signal.get("comment") or "")
        package = str(focus_scope.get("package") or "未识别")
        pid = str(focus_scope.get("pid") or "未识别")
        time_window = focus_scope.get("time_window", {}) if isinstance(focus_scope, dict) else {}
        alignment = self._signal_fault_alignment(fault_time, focus_scope)
        lifecycle_nodes = self._signal_lifecycle_nodes(signal_payload, focus_scope, fault_time, source_evidence_path)
        dataflow_nodes = self._signal_dataflow_nodes(signal_payload, prompt_text, source_evidence_path)
        evidence_rows = self._signal_evidence_rows(signal_payload, focus_scope)
        boundary_issues = self._signal_boundary_issues(signal_payload, focus_scope, fault_time)
        source_rows = self._signal_source_rows(signal_payload, source_evidence_path)
        summary_text = self._signal_scoped_summary_text(signal_payload)
        visible_scope = self._signal_visible_scope_text(signal_payload, focus_scope)
        verdict_sev = alignment["sev"]
        if verdict_sev == "green" and any(issue.get("sev") == "yellow" for issue in boundary_issues):
            verdict_sev = "yellow"
        cards = [
            ("信号", signal_code or signal_name, "green", signal_name if signal_code else signal_comment),
            ("焦点进程", package, "green" if package != "未识别" else "yellow", f"PID {pid}" if pid != "未识别" else ""),
            ("日志时间窗", str(time_window.get("display") or "未识别"), "green" if time_window else "yellow", ""),
            ("现场一致性", alignment["label"], alignment["sev"], alignment["detail"]),
            ("进程内可见性", visible_scope, "green" if "已看到" in visible_scope else "yellow", ""),
            ("原始输入", str(selected_input or "无"), "green" if selected_input else "yellow", ""),
        ]
        title_suffix = signal_comment or signal_name
        composition = plan_signal_report(
            title_suffix=title_suffix,
            prompt_text=combined_bug_html.H(prompt_text),
            raw_signal_report_name=combined_bug_html.H(self._report_name("signal", "html")),
            verdict_sev=verdict_sev,
            verdict_text=alignment["headline"],
            judgement_text=alignment["judgement"],
            cards=cards,
            lifecycle_nodes=lifecycle_nodes,
            dataflow_nodes=dataflow_nodes,
            evidence_rows=evidence_rows,
            boundary_issues=boundary_issues,
            summary_text=summary_text,
            source_rows=source_rows,
            render_summary_html=lambda summary: f'<div class="insight" style="margin-top:12px">{combined_bug_html.H(summary)}</div>',
            render_source_rows_html=lambda rows: combined_bug_html.render_table(
                rows,
                ["层级", "位置", "说明"],
                empty_text="未提取到额外源码引用",
            ),
        )
        if android_data_link:
            self._signal_merge_android_conclusion(composition, android_data_link)
            self._signal_insert_sections_after(
                composition,
                title="结论摘要",
                sections=self._signal_android_data_link_sections(android_data_link),
            )
        return composition

    def _signal_overview_report_name(self, suffix: str) -> str:
        return f"bug_signal_overview_report.{suffix}"

    def _build_signal_overview_summary_text(
        self,
        signal_payload: dict[str, object],
        prompt_text: str,
        fault_time: str,
        focus_scope: dict[str, object],
        android_data_link: list[dict[str, object]] | None = None,
    ) -> str:
        signal = signal_payload.get("signal", {}) if isinstance(signal_payload, dict) else {}
        signal_name = str(signal.get("name") or signal.get("code") or "未知信号")
        signal_code = str(signal.get("code") or "")
        package = str(focus_scope.get("package") or "未识别")
        pid = str(focus_scope.get("pid") or "未识别")
        time_window = focus_scope.get("time_window", {}) if isinstance(focus_scope, dict) else {}
        start = str(time_window.get("start") or "")
        end = str(time_window.get("end") or "")
        alignment = self._signal_fault_alignment(fault_time, focus_scope)
        boundary = self._signal_boundary_issues(signal_payload, focus_scope, fault_time)
        top_issue = boundary[0]["detail"] if boundary else "已生成信号链路总览报告。"
        likely_cause = self._signal_android_likely_cause(android_data_link or [])
        if likely_cause:
            top_issue = likely_cause
        coverage_note = self._signal_coverage_note(signal_payload, focus_scope)
        time_note = "未识别"
        if start and end:
            time_note = f"{start} ~ {end}"
        elif start:
            time_note = start
        return (
            "Bug 分析完成\n"
            "类型: 信号链路总览报告\n"
            f"描述: {prompt_text}\n"
            f"信号: {signal_code or '-'} {signal_name}\n"
            f"焦点进程: {package} / PID {pid}\n"
            f"日志时间窗: {time_note}\n"
            f"现场一致性: {alignment['label']}\n"
            f"当前判断: {top_issue}\n"
            f"进程内链路: {coverage_note}\n"
            f"HTML: {self._signal_overview_report_name('html')}"
        )

    def _render_signal_bug_overview_html(
        self,
        *,
        signal_payload: dict[str, object],
        prompt_text: str,
        fault_time: str,
        selected_input: Path | None,
        source_evidence_path: Path | None,
        focus_scope: dict[str, object],
        android_data_link: list[dict[str, object]] | None = None,
    ) -> str:
        composition = self._plan_signal_report(
            signal_payload=signal_payload,
            prompt_text=prompt_text,
            fault_time=fault_time,
            selected_input=selected_input,
            source_evidence_path=source_evidence_path,
            focus_scope=focus_scope,
            android_data_link=android_data_link or [],
        )
        return combined_bug_html.render_report_shell(**composition_to_renderer_payload(composition))

    def _signal_focus_scope(self, signal_payload: dict[str, object], fault_time: str = "") -> dict[str, object]:
        evidence_items = self._signal_evidence_items(signal_payload)
        reference_date = self._signal_reference_date(evidence_items)
        target_items = [
            item
            for item in evidence_items
            if str(item.get("stage") or "") != "lifecycle" and self._signal_item_matches_target(signal_payload, item)
        ]
        scoped_items = target_items or evidence_items
        fault_dt = self._parse_bug_datetime(fault_time)
        nearest: tuple[float, tuple[str, str]] | None = None
        if fault_dt is not None:
            for item in scoped_items:
                package = self._signal_package_from_path(str(item.get("file") or ""))
                pid = self._signal_pid_from_text(str(item.get("text") or ""))
                if not package and not pid:
                    continue
                timestamp = self._signal_parse_datetime(
                    text=str(item.get("text") or ""),
                    time_text=str(item.get("time") or ""),
                    reference_date=reference_date,
                )
                if timestamp is None:
                    continue
                distance = abs((timestamp - fault_dt).total_seconds())
                key = (package, pid)
                if nearest is None or distance < nearest[0]:
                    nearest = (distance, key)
        counts: dict[tuple[str, str], int] = {}
        for item in scoped_items:
            package = self._signal_package_from_path(str(item.get("file") or ""))
            pid = self._signal_pid_from_text(str(item.get("text") or ""))
            if not package and not pid:
                continue
            counts[(package, pid)] = counts.get((package, pid), 0) + 1
        package = ""
        pid = ""
        if nearest is not None:
            package, pid = nearest[1]
        elif counts:
            (package, pid), _ = sorted(
                counts.items(),
                key=lambda item: (item[1], bool(item[0][0]), bool(item[0][1]), item[0][0], item[0][1]),
                reverse=True,
            )[0]
        time_window = self._signal_time_window(scoped_items, reference_date, package, pid)
        return {
            "package": package,
            "pid": pid,
            "reference_date": reference_date,
            "time_window": time_window,
        }

    def _signal_evidence_items(self, signal_payload: dict[str, object]) -> list[dict[str, object]]:
        items: list[dict[str, object]] = []
        lifecycle = signal_payload.get("lifecycle_report", {}) if isinstance(signal_payload, dict) else {}
        runtime = lifecycle.get("runtime", {}) if isinstance(lifecycle, dict) else {}
        for event in runtime.get("events", []) if isinstance(runtime, dict) else []:
            if not isinstance(event, dict):
                continue
            items.append(
                {
                    "stage": "lifecycle",
                    "title": str(event.get("label") or "生命周期"),
                    "file": str(event.get("file") or ""),
                    "line": event.get("line"),
                    "text": str(event.get("text") or ""),
                    "time": str(event.get("time") or ""),
                    "delta": str(event.get("delta") or ""),
                }
            )
        log_report = signal_payload.get("log_report", {}) if isinstance(signal_payload, dict) else {}
        stages = log_report.get("stages", {}) if isinstance(log_report, dict) else {}
        for stage_key, stage_payload in stages.items():
            if not isinstance(stage_payload, dict):
                continue
            title = str(stage_payload.get("title") or stage_key)
            for example in stage_payload.get("examples", []) or []:
                if not isinstance(example, dict):
                    continue
                items.append(
                    {
                        "stage": str(stage_key),
                        "title": title,
                        "code": str(example.get("code") or ""),
                        "file": str(example.get("file") or ""),
                        "line": example.get("line"),
                        "text": str(example.get("text") or ""),
                    }
                )
        return items

    def _signal_reference_date(self, evidence_items: list[dict[str, object]]) -> str:
        for item in evidence_items:
            for value in (str(item.get("text") or ""), str(item.get("file") or ""), str(item.get("time") or "")):
                match = re.search(r"(20\d{2}-\d{2}-\d{2})", value)
                if match:
                    return match.group(1)
        return ""

    def _signal_time_window(
        self,
        evidence_items: list[dict[str, object]],
        reference_date: str,
        package: str,
        pid: str,
    ) -> dict[str, object]:
        matched: list[datetime] = []
        for item in evidence_items:
            if package:
                item_package = self._signal_package_from_path(str(item.get("file") or ""))
                if item_package and item_package != package:
                    continue
            if pid:
                item_pid = self._signal_pid_from_text(str(item.get("text") or ""))
                if item_pid and item_pid != pid:
                    continue
            timestamp = self._signal_parse_datetime(
                text=str(item.get("text") or ""),
                time_text=str(item.get("time") or ""),
                reference_date=reference_date,
            )
            if timestamp is not None:
                matched.append(timestamp)
        if not matched:
            return {}
        matched.sort()
        start = matched[0]
        end = matched[-1]
        display = self._signal_format_datetime(start)
        if end != start:
            display += " ~ " + self._signal_format_datetime(end)
        return {
            "start": self._signal_format_datetime(start),
            "end": self._signal_format_datetime(end),
            "display": display,
        }

    def _signal_parse_datetime(self, *, text: str, time_text: str, reference_date: str) -> datetime | None:
        full_match = re.search(r"\[(20\d{2}-\d{2}-\d{2}) \+\d{4} (\d{2}:\d{2}:\d{2})\]", text)
        if full_match:
            try:
                return datetime.strptime(f"{full_match.group(1)} {full_match.group(2)}", "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return None
        value = time_text or text
        month_day_match = re.search(r"(\d{2})-(\d{2}) (\d{2}:\d{2}:\d{2}(?:\.\d{3})?)", value)
        if not month_day_match or not reference_date:
            return None
        dt_text = f"{reference_date[:4]}-{month_day_match.group(1)}-{month_day_match.group(2)} {month_day_match.group(3)}"
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(dt_text, fmt)
            except ValueError:
                continue
        return None

    def _signal_format_datetime(self, value: datetime) -> str:
        if value.microsecond:
            return value.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        return value.strftime("%Y-%m-%d %H:%M:%S")

    def _signal_fault_alignment(self, fault_time: str, focus_scope: dict[str, object]) -> dict[str, str]:
        time_window = focus_scope.get("time_window", {}) if isinstance(focus_scope, dict) else {}
        start = str(time_window.get("start") or "")
        if not fault_time:
            return {
                "sev": "yellow",
                "label": "未识别",
                "detail": "请求里没有可比对的故障时间。",
                "headline": "当前报告只说明可见日志内的链路状态，无法和现场时间做严格对齐。",
                "judgement": "没有可比对的故障时间，只能把这份报告当作日志样本说明，不应直接当成现场定案。",
            }
        if not start:
            return {
                "sev": "yellow",
                "label": "未知",
                "detail": "当前报告没有解析出明确日志时间窗。",
                "headline": "当前报告缺少明确日志时间窗，不能直接拿来证明现场结论。",
                "judgement": "需要补充目标时间窗日志，才能把这份链路报告和现场结论绑定起来。",
            }
        fault_hour = fault_time[:13]
        start_hour = start[:13]
        if fault_hour == start_hour:
            return {
                "sev": "green",
                "label": "一致",
                "detail": f"日志时间窗命中了请求故障小时 {fault_hour}。",
                "headline": "当前日志时间窗和请求故障时间一致，可以把下面的链路证据直接用于现场判断。",
                "judgement": "时间窗一致，这份报告可直接回答“现场这条链路当时有没有走通”。",
            }
        if fault_time[:10] == start[:10]:
            return {
                "sev": "yellow",
                "label": "同日不同小时",
                "detail": f"请求故障时间是 {fault_time}，当前日志主时间窗是 {start}。",
                "headline": "当前日志和请求是同一天，但不是同一小时，结论只能作为同版本同流程样本。",
                "judgement": "这能说明代码和样本日志里的链路行为，但还不能直接证明目标时刻的现场现象。",
            }
        return {
            "sev": "yellow",
            "label": "不一致",
            "detail": f"请求故障时间是 {fault_time}，当前日志主时间窗是 {start}。",
            "headline": "当前可用日志不是目标现场时间窗，下面的证据更适合回答“链路设计和样本运行是否走通”，不适合直接下现场定案。",
            "judgement": "这份报告最能说明的是样本日志里目标信号在当前焦点进程内走到了哪里，而不是请求里的目标时刻一定发生了什么。",
        }

    def _signal_lifecycle_nodes(
        self,
        signal_payload: dict[str, object],
        focus_scope: dict[str, object],
        fault_time: str,
        source_evidence_path: Path | None,
    ) -> list[dict[str, str]]:
        package = str(focus_scope.get("package") or "")
        pid = str(focus_scope.get("pid") or "")
        reference_date = str(focus_scope.get("reference_date") or "")
        start_dt = None
        nodes: list[dict[str, str]] = []
        for event in self._signal_runtime_events(signal_payload):
            if not self._signal_item_matches_scope(event, package, pid):
                continue
            label = str(event.get("label") or "")
            text = str(event.get("text") or "")
            if label == "进程启动":
                start_dt = self._signal_parse_datetime(text=text, time_text=str(event.get("time") or ""), reference_date=reference_date)
                nodes.append(
                    {
                        "tag": "日志",
                        "title": f"{package or '目标进程'} 启动",
                        "meta": f"{event.get('time') or ''} · PID {pid or self._signal_pid_from_text(text) or '未识别'}",
                        "note": text,
                    }
                )
                break
        for keyword, title in (
            ("injectSignalProvider", self._signal_provider_injection_title(signal_payload)),
            ("registerSignal", self._signal_registration_title(signal_payload)),
        ):
            match = self._signal_find_runtime_event(signal_payload, package, pid, keyword)
            if match is None:
                continue
            nodes.append(
                {
                    "tag": "日志",
                    "title": title,
                    "meta": self._signal_meta_from_item(match, start_dt, reference_date),
                    "note": str(match.get("text") or ""),
                }
            )
        for stage_key, title in (
            ("datacenter", self._signal_datacenter_stage_title(signal_payload)),
            ("android_business", self._signal_business_stage_title(signal_payload)),
        ):
            match = self._signal_find_stage_example(signal_payload, stage_key, package, pid, prefer_hmi=False, target_only=True)
            if match is None:
                continue
            nodes.append(
                {
                    "tag": "日志",
                    "title": title,
                    "meta": self._signal_meta_from_item(match, start_dt, reference_date),
                    "note": str(match.get("text") or ""),
                }
            )
        hmi_match = self._signal_find_stage_example(signal_payload, "android_business", package, pid, prefer_hmi=True, target_only=True)
        if hmi_match is not None:
            nodes.append(
                {
                    "tag": "日志",
                    "title": self._signal_consumer_stage_title(hmi_match),
                    "meta": self._signal_meta_from_item(hmi_match, start_dt, reference_date),
                    "note": str(hmi_match.get("text") or ""),
                }
            )
        business_entry = self._signal_select_business_entry(source_evidence_path, prompt_text=fault_time, signal_payload=signal_payload)
        if business_entry is not None:
            nodes.append(
                {
                    "tag": "源码",
                    "title": "业务判定落点",
                    "meta": f"{business_entry['file']}:{business_entry['line']}",
                    "note": business_entry["text"],
                }
            )
        return nodes[:6]

    def _signal_dataflow_nodes(
        self,
        signal_payload: dict[str, object],
        prompt_text: str,
        source_evidence_path: Path | None,
    ) -> list[dict[str, str]]:
        refs = signal_payload.get("source_references", []) if isinstance(signal_payload, dict) else []
        lifecycle = signal_payload.get("lifecycle_report", {}) if isinstance(signal_payload, dict) else {}
        lifecycle_refs = lifecycle.get("source_references", []) if isinstance(lifecycle, dict) else []
        chain_edges = signal_payload.get("chain_edges", []) if isinstance(signal_payload, dict) else []
        nodes: list[dict[str, str]] = []
        source_edge = None
        context = lifecycle.get("context", {}) if isinstance(lifecycle, dict) else {}
        if isinstance(context, dict):
            candidate = context.get("source_edge")
            if isinstance(candidate, dict):
                source_edge = candidate
        if source_edge is None:
            for edge in chain_edges:
                if isinstance(edge, dict) and str(edge.get("source") or "").startswith("CARSERVICE:"):
                    source_edge = edge
                    break
        if isinstance(source_edge, dict):
            nodes.append(
                {
                    "tag": "映射",
                    "title": str(source_edge.get("source") or "上游信号"),
                    "meta": f"{source_edge.get('file') or ''}:{source_edge.get('line') or ''}",
                    "note": str(source_edge.get("note") or ""),
                }
            )
        on_change_ref = self._signal_find_source_reference(
            lifecycle_refs,
            lambda file_text, line_text: "carvcuhelper.kt" in file_text and "onchangeevent" in line_text,
        )
        if on_change_ref is not None:
            nodes.append(
                {
                    "tag": "Helper",
                    "title": "CarVcuHelper.onChangeEvent",
                    "meta": f"{on_change_ref['file']}:{on_change_ref['line']}",
                    "note": on_change_ref["text"],
                }
            )
        data_center_edge = None
        for edge in chain_edges:
            if isinstance(edge, dict) and "datacenter" in str(edge.get("target") or "").casefold():
                data_center_edge = edge
                break
        if isinstance(data_center_edge, dict):
            nodes.append(
                {
                    "tag": "分发",
                    "title": self._signal_data_center_title(),
                    "meta": f"{data_center_edge.get('file') or ''}:{data_center_edge.get('line') or ''}",
                    "note": str(data_center_edge.get("note") or ""),
                }
            )
        consumer_ref = self._signal_select_consumer_reference(refs)
        if consumer_ref is not None:
            nodes.append(
                {
                    "tag": "业务",
                    "title": self._signal_consumer_reference_title(consumer_ref),
                    "meta": f"{consumer_ref['file']}:{consumer_ref['line']}",
                    "note": consumer_ref["text"],
                }
            )
        hmi_ref = self._signal_select_state_receiver_reference(refs)
        if hmi_ref is not None:
            nodes.append(
                {
                    "tag": "HMI",
                    "title": self._signal_consumer_reference_title(hmi_ref),
                    "meta": f"{hmi_ref['file']}:{hmi_ref['line']}",
                    "note": hmi_ref["text"],
                }
            )
        business_entry = self._signal_select_business_entry(source_evidence_path, prompt_text=prompt_text, signal_payload=signal_payload)
        if business_entry is not None:
            nodes.append(
                {
                    "tag": "落点",
                    "title": Path(business_entry["file"]).stem,
                    "meta": f"{business_entry['file']}:{business_entry['line']}",
                    "note": business_entry["text"],
                }
            )
        return nodes[:6]
