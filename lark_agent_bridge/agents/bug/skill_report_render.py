from __future__ import annotations

from ._shared import *  # noqa: F401,F403


def parse_node_status_block(text: str) -> dict:
    """从 markdown 末尾的 ```json 围栏抽取 {node_status, findings}；失败返回 {}。"""
    import json as _json
    import re as _re

    if not text:
        return {}
    blocks = _re.findall(r"```json\s*(\{.*?\})\s*```", text, _re.DOTALL)
    for raw in reversed(blocks):
        try:
            obj = _json.loads(raw)
            if isinstance(obj, dict) and ("node_status" in obj or "findings" in obj or "verdict" in obj):
                return {
                    "node_status": obj.get("node_status") or {},
                    "findings": obj.get("findings") or [],
                    "verdict": obj.get("verdict") or {},
                }
        except Exception:
            continue
    return {}


class _SkillReportRenderMixin:
    """自定义 Skill Agent 报告：markdown/source-stage 解析、报告写出与工件命名（与 BugPromptMixin 共享 self 状态）。"""

    def _validate_custom_skill_analysis(self, analysis_markdown_path: Path) -> tuple[bool, str, int]:
        try:
            text = analysis_markdown_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False, "missing_analysis_markdown", 0
        lines = text.splitlines()
        start: int | None = None
        for index, line in enumerate(lines):
            if re.match(r"^\s*##\s+关键证据\s*$", line):
                start = index + 1
                break
        if start is None:
            return False, "missing_key_evidence_section", 0
        section_lines: list[str] = []
        for line in lines[start:]:
            if re.match(r"^\s*##\s+", line):
                break
            section_lines.append(line)
        evidence_lines = [
            line.strip()
            for line in section_lines
            if line.strip() and not line.strip().startswith("<!--")
        ]
        if not evidence_lines:
            return False, "empty_key_evidence_section", 0
        return True, "", len(evidence_lines)
    def _parse_markdown_sections(self, markdown_text: str) -> dict[str, str]:
        sections: dict[str, str] = {}
        current_title = ""
        current_lines: list[str] = []
        for raw_line in markdown_text.splitlines():
            match = re.match(r"^\s*##\s+(.+?)\s*$", raw_line)
            if match:
                if current_title:
                    sections[current_title] = "\n".join(current_lines).strip()
                current_title = match.group(1).strip()
                current_lines = []
                continue
            if current_title:
                current_lines.append(raw_line.rstrip())
        if current_title:
            sections[current_title] = "\n".join(current_lines).strip()
        return sections
    def _markdown_section_entries(self, section_text: str) -> list[str]:
        entries: list[str] = []
        current: list[str] = []
        list_prefix_pattern = r"^(?:[-*]|\d+[.)、])\s+"
        for raw_line in section_text.splitlines():
            stripped = raw_line.strip()
            if not stripped:
                if current:
                    entries.append(" ".join(current).strip())
                    current = []
                continue
            if re.match(list_prefix_pattern, stripped):
                if current:
                    entries.append(" ".join(current).strip())
                current = [re.sub(list_prefix_pattern, "", stripped)]
            else:
                if current:
                    current.append(stripped)
                else:
                    current = [stripped]
        if current:
            entries.append(" ".join(current).strip())
        return [entry for entry in entries if entry]
    def _clean_markdown_inline_text(self, text: str) -> str:
        cleaned = text.strip()
        cleaned = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", cleaned)
        cleaned = re.sub(r"`([^`]+)`", r"\1", cleaned)
        cleaned = re.sub(r"\*\*([^*]+)\*\*", r"\1", cleaned)
        cleaned = re.sub(r"\*([^*]+)\*", r"\1", cleaned)
        cleaned = re.sub(r"__([^_]+)__", r"\1", cleaned)
        cleaned = re.sub(r"_([^_]+)_", r"\1", cleaned)
        return re.sub(r"\s+", " ", cleaned).strip()
    def _truncate_report_text(self, text: str, limit: int = 180) -> str:
        normalized = self._clean_markdown_inline_text(text)
        if len(normalized) <= limit:
            return normalized
        return normalized[: limit - 1].rstrip() + "…"
    def _markdown_table_block_length(self, lines: Sequence[str], start: int) -> int:
        if start + 2 >= len(lines):
            return 0
        header = lines[start].strip()
        separator = lines[start + 1].strip()
        if not (header.startswith("|") and header.endswith("|")):
            return 0
        if not re.match(r"^\|?(?:\s*:?-{3,}:?\s*\|)+\s*:?-{3,}:?\s*\|?$", separator):
            return 0
        index = start + 2
        row_count = 0
        while index < len(lines):
            current = lines[index].strip()
            if not (current.startswith("|") and current.endswith("|")):
                break
            row_count += 1
            index += 1
        if row_count == 0:
            return 0
        return index - start
    def _parse_markdown_table_row(self, raw_line: str) -> list[str]:
        return [self._clean_markdown_inline_text(cell) for cell in raw_line.strip().strip("|").split("|")]
    def _source_stage_first_markdown_table(self, section_text: str) -> tuple[list[str], list[tuple[str, ...]]]:
        lines = section_text.splitlines()
        for index in range(len(lines)):
            block_len = self._markdown_table_block_length(lines, index)
            if not block_len:
                continue
            block = lines[index:index + block_len]
            header = self._parse_markdown_table_row(block[0])
            rows: list[tuple[str, ...]] = []
            width = len(header)
            for row_line in block[2:]:
                values = self._parse_markdown_table_row(row_line)
                if len(values) < width:
                    values.extend([""] * (width - len(values)))
                values = values[:width]
                if any(value.strip() for value in values):
                    rows.append(tuple(values))
            if header and rows:
                return header, rows
        return [], []
    def _strip_markdown_tables(self, section_text: str) -> str:
        cleaned_lines: list[str] = []
        lines = section_text.splitlines()
        index = 0
        while index < len(lines):
            block_len = self._markdown_table_block_length(lines, index)
            if block_len:
                index += block_len
                continue
            cleaned_lines.append(lines[index].rstrip())
            index += 1
        return "\n".join(cleaned_lines).strip()
    def _source_stage_entry_title_detail(self, entry: str) -> tuple[str, str]:
        raw_entry = entry.strip()
        patterns = [
            r"^\d+[.)、]?\s*\*\*(.+?)\*\*[：:，,]\s*(.+)$",
            r"^\*\*(.+?)\*\*[：:，,]\s*(.+)$",
            r"^([^：:]{1,24})[：:]\s*(.+)$",
        ]
        for pattern in patterns:
            match = re.match(pattern, raw_entry)
            if match:
                return (
                    self._clean_markdown_inline_text(match.group(1)),
                    self._clean_markdown_inline_text(match.group(2)),
                )
        return "", self._clean_markdown_inline_text(raw_entry)
    def _source_stage_issue_items(self, section_text: str, *, sev: str, limit: int = 8) -> list[dict[str, str]]:
        items: list[dict[str, str]] = []
        for entry in self._markdown_section_entries(section_text)[:limit]:
            title, detail = self._source_stage_entry_title_detail(entry)
            if title:
                items.append({
                    "sev": sev,
                    "title": self._truncate_report_text(title, 80),
                    "detail": self._truncate_report_text(detail, 360),
                })
                continue
            items.append({
                "sev": sev,
                "title": self._truncate_report_text(detail, 220),
                "detail": "",
            })
        return items
    def _source_stage_swimlane_rows(self, section_text: str) -> list[tuple[str, str, str]]:
        headers, rows = self._source_stage_first_markdown_table(section_text)
        if not headers or not rows:
            return []
        if self._clean_markdown_inline_text(headers[0]) != "泳道":
            return []
        swimlanes: list[tuple[str, str, str]] = []
        for row in rows:
            lane = row[0] if len(row) > 0 else ""
            action = row[1] if len(row) > 1 else ""
            anchor = row[2] if len(row) > 2 else ""
            if lane or action or anchor:
                swimlanes.append((lane, action, anchor))
        return swimlanes
    def _normalize_source_anchor(self, source_text: str) -> str:
        cleaned = self._clean_markdown_inline_text(source_text)
        anchors = re.findall(
            r"([A-Za-z0-9_.-]+\.[A-Za-z0-9_+-]+(?::\d+(?:[-~]\d+)?)?(?:、:\d+(?:[-~]\d+)?)*)",
            cleaned,
        )
        deduped: list[str] = []
        for anchor in anchors:
            if anchor not in deduped:
                deduped.append(anchor)
        if deduped:
            return "；".join(deduped[:3])
        return self._truncate_report_text(cleaned, 180)
    def _source_stage_evidence_rows(self, section_text: str, *, limit: int = 6) -> list[tuple[str, str, str]]:
        rows: list[tuple[str, str, str]] = []
        for entry in self._markdown_section_entries(section_text)[:limit]:
            detail_text = entry
            anchor_text = ""
            source_match = re.search(r"(?:来源|源码锚点)[：:]\s*(.+)$", entry)
            if source_match:
                detail_text = entry[:source_match.start()].strip()
                anchor_text = self._normalize_source_anchor(source_match.group(1))
            title, detail = self._source_stage_entry_title_detail(detail_text)
            location = title or "证据"
            content = detail or self._clean_markdown_inline_text(detail_text)
            rows.append(
                (
                    self._truncate_report_text(location, 80),
                    self._truncate_report_text(content, 220),
                    anchor_text or "见完整分析",
                )
            )
        return rows
    def _source_stage_report_sections(self, analysis_text: str) -> list[ReportSection]:
        sections = self._parse_markdown_sections(analysis_text)
        rendered: list[ReportSection] = []
        for title, body in sections.items():
            if not body.strip():
                continue
            if title == "结论摘要":
                summary_body = self._strip_markdown_tables(body)
                summary_items = self._source_stage_issue_items(summary_body, sev="green", limit=6)
                if summary_items:
                    rendered.append(ReportSection(kind="issues", title="结论摘要", items=summary_items))
                swimlane_rows = self._source_stage_swimlane_rows(body)
                if swimlane_rows:
                    rendered.append(
                        ReportSection(
                            kind="swimlane",
                            title="泳道图",
                            description="按发送、分发和消费路径拆开展示源码链路。",
                            cols=["泳道", "时序动作", "源码锚点"],
                            rows=swimlane_rows,
                            empty_text="未提取到结构化泳道节点",
                        )
                    )
                continue
            if title == "关键证据":
                evidence_rows = self._source_stage_evidence_rows(body, limit=12)
                if evidence_rows:
                    rendered.append(
                        ReportSection(
                            kind="table",
                            title="关键证据",
                            cols=["位置", "关键点", "源码锚点"],
                            rows=evidence_rows,
                            empty_text="未提取到关键证据摘要",
                        )
                    )
                continue
            sev = "yellow" if title == "待确认项" else "green"
            items = self._source_stage_issue_items(body, sev=sev, limit=8)
            if items:
                rendered.append(ReportSection(kind="issues", title=title, items=items))
        return rendered
    def _write_custom_skill_agent_report(
        self,
        *,
        analysis_kind: str,
        analysis_label: str,
        html_path: Path,
        json_path: Path,
        analysis_markdown_path: Path,
        skill_name: str,
        provider: str,
        request_text: str,
        prompt_text: str,
        title: str,
        description: str,
        fault_time: str,
        selected_input: Path | None,
        prepared_input: Path | None,
        source_evidence_path: Path | None,
        evidence_count: int,
        duration_seconds: float,
        executor: str = "file_agent",
        extra_payload: dict[str, object] | None = None,
        render_html: bool = True,
    ) -> None:
        analysis_text = analysis_markdown_path.read_text(encoding="utf-8", errors="replace")
        analysis_file_name = analysis_markdown_path.name
        skill_paths = self._skill_context_paths(skill_name)
        executor_label = executor or "file_agent"
        verdict_text = f"{analysis_label} `{skill_name}` 已通过 {executor_label} 执行器产出执行证据，允许进入最终总结。"
        cards = [
            ("执行器", executor_label, "green", provider or "未记录 provider"),
            ("命中 Skill", skill_name or "未记录", "green" if skill_name else "yellow", ""),
            ("关键证据", str(evidence_count), "green" if evidence_count > 0 else "red", f"来自 {analysis_file_name} 的 ## 关键证据"),
            ("故障时间", fault_time or "未识别", "green" if fault_time else "yellow", ""),
            ("日志输入", selected_input.name if selected_input else "无", "green" if selected_input else "yellow", str(selected_input or "")),
            ("执行耗时", f"{duration_seconds:.1f}s", "green", ""),
        ]
        context_rows = [
            ("Bug 标题", title or "未返回 / 未设置"),
            ("用户请求", prompt_text or request_text or "未设置"),
            ("原始请求", request_text or "未设置"),
            ("故障时间", fault_time or "未识别"),
            ("selected log input", str(selected_input or "未提供")),
            ("prepared log input", str(prepared_input or "未提供")),
            ("source evidence", str(source_evidence_path or "未生成")),
            (analysis_file_name, str(analysis_markdown_path)),
        ]
        skill_rows = [(path.name, str(path)) for path in skill_paths]
        detail_body = (
            "<pre class=\"evidence-block\">"
            + html_lib.escape(analysis_text.strip() or "(空)")
            + "</pre>"
        )
        raw_body = (
            "<div class=\"split-grid\">"
            f"{combined_bug_html.render_table([('原始请求', request_text.strip() or '(无请求)')], ('字段', '内容'))}"
            f"{combined_bug_html.render_table([('缺陷描述', description.strip() or '(无描述)')], ('字段', '内容'))}"
            "</div>"
        )
        summary_sections = self._source_stage_report_sections(analysis_text)
        composition = ReportComposition(
            title=analysis_label,
            heading=analysis_label,
            subtitle=f"Bug 标题：{title or '未返回 / 未设置'}",
            verdict=ReportVerdict(sev="green", text=verdict_text),
            cards=cards,
            sections=
            summary_sections
            + [
                ReportSection(
                    kind="issues",
                    title="执行状态",
                    items=[
                        {
                            "sev": "green",
                            "title": "Execution artifact 已生成",
                            "detail": f"`{analysis_markdown_path}` 已通过 `## 关键证据` 非空校验。",
                        }
                    ],
                ),
                ReportSection(kind="table", title="Skill 上下文", cols=["文件", "路径"], rows=skill_rows, empty_text="未找到 Skill 文件"),
                ReportSection(kind="table", title="分析上下文", cols=["字段", "内容"], rows=context_rows),
                ReportSection(kind="details", title="完整分析", summary="展开查看完整分析 Markdown", body_html=detail_body),
                ReportSection(kind="details", title="原始输入", summary="展开查看请求与缺陷描述", body_html=raw_body),
            ],
        )
        status_key = f"{analysis_kind}_analysis_status"
        payload = {
            "mode": f"{analysis_kind}_agent_analysis",
            "summary": verdict_text,
            "verdict": {"sev": "green", "text": verdict_text},
            status_key: "completed",
            "analysis_kind": analysis_kind,
            "analysis_skill": skill_name,
            "executor": executor,
            "provider": provider,
            "evidence_count": evidence_count,
            "analysis_markdown": str(analysis_markdown_path),
            "fault_time": fault_time,
            "selected_input": str(selected_input) if selected_input else "",
            "prepared_input": str(prepared_input) if prepared_input else "",
            "source_evidence_file": str(source_evidence_path) if source_evidence_path else "",
            "skill_context_files": [str(path) for path in skill_paths],
            "title": title,
            "prompt_text": prompt_text,
            "request_text": request_text,
            "description": description.strip(),
            "duration_seconds": duration_seconds,
        }
        if extra_payload:
            payload.update(extra_payload)
        # Merge structured node_status/findings: extra_payload takes priority (pydantic-ai path);
        # fall back to parsing the json fence in the analysis text (file_agent path).
        if "node_status" not in payload or "findings" not in payload or "verdict" not in payload:
            parsed = parse_node_status_block(analysis_text)
            if parsed:
                payload.setdefault("node_status", parsed.get("node_status", {}))
                payload.setdefault("findings", parsed.get("findings", []))
                payload.setdefault("verdict", parsed.get("verdict", {}))
        payload.setdefault("node_status", {})
        payload.setdefault("findings", [])
        payload.setdefault("verdict", {})
        if render_html:
            html_path.write_text(
                combined_bug_html.render_report_shell(**composition_to_renderer_payload(composition)),
                encoding="utf-8",
            )
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    def _skill_file_agent_execution_details(self, analysis_kind: str, result: dict[str, object]) -> dict[str, object]:
        prefix = analysis_kind
        status_key = f"{prefix}_analysis_status"
        executor = str(result.get("executor") or "file_agent")
        usage = result.get("usage") or {}
        total_tokens = 0
        if isinstance(usage, dict):
            total_tokens = normalize_token_usage(usage).get("total_tokens", 0)
        details = {
            f"{prefix}_executor": executor,
            status_key: str(result.get(status_key) or result.get("custom_skill_analysis_status") or "completed"),
            f"{prefix}_analysis_file": str(result.get("analysis_markdown_path") or ""),
            f"{prefix}_report_html": str(result.get("html_path") or ""),
            f"{prefix}_report_json": str(result.get("json_path") or ""),
            f"{prefix}_context_file": str(result.get("context_path") or ""),
            f"{prefix}_log_focus_manifest": str(result.get("log_focus_manifest_path") or ""),
            f"{prefix}_focused_log_input": str(result.get("focused_log_input") or ""),
            f"{prefix}_debug_log": str(result.get("debug_log_path") or ""),
            f"{prefix}_events_file": str(result.get("events_path") or ""),
            f"{prefix}_evidence_count": int(result.get("evidence_count") or 0),
            f"{prefix}_agent_provider": str(result.get("provider") or ""),
            f"{prefix}_agent_duration_seconds": result.get("duration_seconds") or 0.0,
            f"{prefix}_app_server_thread_id": str(result.get("thread_id") or ""),
            f"{prefix}_app_server_turn_id": str(result.get("turn_id") or ""),
            f"{prefix}_runtime_path": str(result.get("runtime_path") or ""),
            f"{prefix}_tool_calls": int(result.get("tool_calls") or 0),
            f"{prefix}_total_tokens": total_tokens,
        }
        if _kind_spec(analysis_kind).is_source_stage:
            stage_status = str(result.get(status_key) or result.get("custom_skill_analysis_status") or "completed")
            details.update(
                {
                    "source_stage_executor": executor,
                    "source_stage_analysis_status": stage_status,
                    "source_stage_analysis_file": str(result.get("analysis_markdown_path") or ""),
                    "source_stage_report_html": str(result.get("html_path") or ""),
                    "source_stage_report_json": str(result.get("json_path") or ""),
                    "source_stage_context_file": str(result.get("context_path") or ""),
                    "source_stage_log_focus_manifest": str(result.get("log_focus_manifest_path") or ""),
                    "source_stage_focused_log_input": str(result.get("focused_log_input") or ""),
                    "source_stage_debug_log": str(result.get("debug_log_path") or ""),
                    "source_stage_events_file": str(result.get("events_path") or ""),
                    "source_stage_evidence_count": int(result.get("evidence_count") or 0),
                    "source_stage_agent_provider": str(result.get("provider") or ""),
                    "source_stage_agent_duration_seconds": result.get("duration_seconds") or 0.0,
                    "source_stage_app_server_thread_id": str(result.get("thread_id") or ""),
                    "source_stage_app_server_turn_id": str(result.get("turn_id") or ""),
                    "source_stage_runtime_path": str(result.get("runtime_path") or ""),
                    "source_stage_tool_calls": int(result.get("tool_calls") or 0),
                    "source_stage_total_tokens": total_tokens,
                }
            )
        return details
    def _skill_agent_analysis_markdown_name(self, analysis_kind: str) -> str:
        return {
            SOURCE_STAGE_KIND: "source_stage_analysis.md",
            "ld_lane_level": "ld_lane_level_analysis.md",
            "custom_skill": "source_code_skill_analysis.md",
            SOURCE_CODE_SKILL_KIND: "source_code_skill_analysis.md",
        }.get(analysis_kind, f"{analysis_kind}_analysis.md")
    def _skill_agent_sidecar_name(self, analysis_kind: str, suffix: str) -> str:
        prefix = {
            SOURCE_STAGE_KIND: "source_stage",
            "ld_lane_level": "ld_lane_level_agent",
            "custom_skill": "source_code_skill_agent",
            SOURCE_CODE_SKILL_KIND: "source_code_skill_agent",
        }.get(analysis_kind, f"{analysis_kind}_agent")
        return f"{prefix}.{suffix}"
    def _skill_file_agent_mode_error_code(self, analysis_kind: str, base_code: str, *, mode: str) -> str:
        if analysis_kind in _SOURCE_SKILL_KINDS:
            if mode == "bug_reanalysis":
                return base_code.replace("custom_skill_agent_", "custom_skill_reanalysis_agent_", 1)
            if mode == "direct_analysis":
                return base_code.replace("custom_skill_agent_", "direct_custom_skill_agent_", 1)
            return base_code
        if mode == "bug_reanalysis":
            return base_code.replace("custom_skill_agent_", f"{analysis_kind}_reanalysis_agent_", 1)
        if mode == "direct_analysis":
            return base_code.replace("custom_skill_agent_", f"direct_{analysis_kind}_agent_", 1)
        return base_code.replace("custom_skill_agent_", f"{analysis_kind}_agent_", 1)
