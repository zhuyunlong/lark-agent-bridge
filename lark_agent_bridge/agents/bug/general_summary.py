from __future__ import annotations

from ._shared import *  # noqa: F401,F403


from .combined_report import _CombinedReportMixin
from .signal_report import _SignalReportMixin


class _GeneralSummaryMixin(_CombinedReportMixin, _SignalReportMixin):
    def _xtheme_focus_entry(self, item: dict[str, object]) -> tuple[str, str]:
        kind = str(item.get("kind") or "").strip()
        value = str(item.get("value") or "").strip()
        ts = str(item.get("ts") or "").strip()
        source = str(item.get("source") or "").strip()
        body = " ".join(part for part in (ts, kind, value) if part).strip()
        if source:
            body += f" [{source}]"
        return kind, body

    def _xtheme_focus_sections(self, payload: dict[str, object]) -> dict[str, str]:
        focus_snapshot = payload.get("focus_snapshot", [])
        upstream: list[str] = []
        calculations: list[str] = []
        outputs: list[str] = []
        if isinstance(focus_snapshot, list):
            for item in focus_snapshot:
                if not isinstance(item, dict):
                    continue
                kind, entry = self._xtheme_focus_entry(item)
                if not entry:
                    continue
                if "XThemeStrategy" in kind:
                    outputs.append(entry)
                elif "calculateTimeInfo" in kind:
                    calculations.append(entry)
                else:
                    upstream.append(entry)

        android_lines = []
        if calculations:
            android_lines.append(f"- 计算结果: {calculations[0]}")
        if outputs:
            android_lines.append(f"- 当前 XTheme 输出: {outputs[0]}")
        if not android_lines:
            android_lines.append("- 未命中问题时刻前的 calculateTimeInfo/XThemeStrategy 关键快照")

        boundary = self._xtheme_boundary_summary(calculations, outputs)
        return {
            "android": "\n".join(android_lines),
            "boundary": boundary,
            "upstream": "\n".join(f"- {line}" for line in upstream[:6]) or "- 未命中 ThemeHelper/UI mode/日出日落输入快照",
            "output": "\n".join(f"- {line}" for line in outputs[:4]) or "- 未命中 XThemeStrategy 发送证据",
        }

    def _xtheme_boundary_summary(self, calculations: list[str], outputs: list[str]) -> str:
        calc_text = " ".join(calculations)
        output_text = " ".join(outputs)
        if (
            "Final=TIME_DAY" in calc_text
            and "ThemeMode=1" in output_text
            and ("TimePeriod=3" in output_text or "TIME_NIGHT" in output_text)
        ):
            return (
                "- Android 侧最终已通过 XThemeStrategy 发送 Night/TIME_NIGHT；"
                "已知 Android 问题窗口是最终发送前曾计算/发送 Day/TIME_DAY。"
                "若用户画面异常持续到最终发送之后，责任边界需要转 Unity/3D 消费、资源或图层证据。"
            )
        if outputs:
            return (
                "- Android 侧最终状态以 XThemeStrategy 输出为准；"
                "若该输出与上游输入不一致，归 Android ThemeHelper/XuiConditionHelper/XThemeStrategy 链路；"
                "若输出正确但画面仍异常，转 Unity/3D 消费或显示侧补证。"
            )
        return "- Android 侧未命中最终 XThemeStrategy 发送证据，当前不能把责任转给 Unity/3D。"

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
            sections = self._xtheme_focus_sections(payload) if isinstance(payload, dict) else {}
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
                f"Android 最终状态:\n{sections.get('android', '- 未整理')}\n"
                f"责任边界:\n{sections.get('boundary', '- 未整理')}\n"
                f"上游输入:\n{sections.get('upstream', '- 未整理')}\n"
                f"XTheme 输出:\n{sections.get('output', '- 未整理')}\n"
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
