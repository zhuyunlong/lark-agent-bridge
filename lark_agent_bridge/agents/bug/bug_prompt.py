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
            if isinstance(obj, dict) and ("node_status" in obj or "findings" in obj):
                return {
                    "node_status": obj.get("node_status") or {},
                    "findings": obj.get("findings") or [],
                }
        except Exception:
            continue
    return {}


class _BugPromptMixin:
    def _run_ld_direct_api_analysis(
        self,
        *,
        skill_name: str,
        request_text: str,
        prompt_text: str,
        title: str,
        description: str,
        fault_time: str,
        selected_input: Path | None,
        prepared_input: Path | None,
        html_path: Path,
        json_path: Path,
        analysis_dir: Path,
        progress_callback: Callable[[dict[str, object]], None] | None,
    ) -> dict[str, object]:
        """Run LD lane-level analysis via executor + direct LLM API (fast path)."""
        from ..llm_client import LLMClient, LLMClientError

        started = time.monotonic()
        analysis_dir.mkdir(parents=True, exist_ok=True)
        analysis_markdown_path = analysis_dir / self._skill_agent_analysis_markdown_name("ld_lane_level")
        context_path = analysis_dir / self._skill_agent_sidecar_name("ld_lane_level", "context.md")
        provider_tag = "direct_api"

        # Phase 1: Executor — extract LD evidence
        self._emit_progress(
            progress_callback,
            stage="ld_executor_extract",
            message="LD 车道级执行器：正在提取 montecarlo 日志证据",
        )
        cache_dir = self._resolve_bug_cache_dir(prepared_input or selected_input)
        log_files = self._ld_executor_find_log_files(
            cache_dir=cache_dir,
            fault_time=fault_time,
        ) if cache_dir else []
        evidence_text = self._ld_executor_extract_evidence(
            log_files=log_files,
            fault_time=fault_time,
        )
        executor_duration = time.monotonic() - started
        self._emit_progress(
            progress_callback,
            stage="ld_executor_done",
            message=f"LD 执行器完成：{len(log_files)} 文件，{executor_duration:.1f}s",
            log_file_count=len(log_files),
        )

        # Read SKILL.md
        skill_record = self.skill_manager.get_skill(skill_name, include_content=True)
        skill_content = skill_record.content if skill_record.content else ""

        # Read reference file
        reference_content = ""
        for ref_path in self._skill_context_paths(skill_name):
            for md_file in sorted(ref_path.glob("*.md")) if ref_path.is_dir() else ([ref_path] if ref_path.suffix == ".md" else []):
                try:
                    ref_text = md_file.read_text(encoding="utf-8", errors="replace")
                    if len(reference_content) + len(ref_text) < 35000:
                        reference_content += f"\n\n---\n### Reference: {md_file.name}\n{ref_text}"
                except OSError:
                    continue

        # Phase 2: Direct API call
        ai_opts = self.config.ai_provider
        client = LLMClient(ai_opts)
        if not client.is_available():
            return {
                "ok": False,
                "error_code": "ld_direct_api_not_configured",
                "message": "LD 车道级分析：direct_api 未配置（ai_provider 不可用），需要 fallback 到 file_agent。",
                "command": [],
                "provider": provider_tag,
                "analysis_markdown_path": analysis_markdown_path,
                "duration_seconds": time.monotonic() - started,
            }

        self._emit_progress(
            progress_callback,
            stage="ld_direct_api_call",
            message=f"LD 车道级分析：调用 API 进行链路分析（{ai_opts.primary_model}）",
            provider=provider_tag,
            model=ai_opts.primary_model,
        )

        system_prompt = (
            "你是一个专业的 LD 车道级日志分析 Agent。"
            "你的任务是根据 SKILL.md 的分析方法论和已预提取的日志证据，分析 LD 车道级渲染问题的根因。"
            "只读分析，不修改文件。输出中文 Markdown。"
            "必须包含这些二级标题：## 结论摘要、## 关键证据、## 最可能原因、## 待确认项、## 建议动作。"
            "## 关键证据 不能为空，每条证据要回指到具体日志行和时间。"
            "证据不足时明确写待确认，不要编造。"
        )

        user_prompt = (
            f"# LD 车道级渲染问题分析\n\n"
            f"## Bug 信息\n"
            f"- 标题: {title}\n"
            f"- 故障时间: {fault_time}\n"
            f"- 用户请求: {request_text}\n"
            f"- 缺陷描述: {description}\n\n"
        )
        if skill_content:
            # Truncate SKILL.md to key sections
            user_prompt += f"## 分析方法论 (SKILL.md)\n{skill_content[:6000]}\n\n"
        if reference_content:
            user_prompt += f"## 参考资料\n{reference_content[:25000]}\n\n"
        user_prompt += evidence_text

        # Write context for audit
        context_path.write_text(user_prompt[:5000] + "\n\n[... truncated for audit ...]\n", encoding="utf-8")

        try:
            response = client.generate_summary(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
        except LLMClientError as exc:
            logger.warning("LD direct API analysis failed: %s", exc)
            return {
                "ok": False,
                "error_code": "ld_direct_api_error",
                "message": f"LD 车道级分析 API 调用失败：{exc}",
                "command": [],
                "provider": provider_tag,
                "analysis_markdown_path": analysis_markdown_path,
                "duration_seconds": time.monotonic() - started,
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("LD direct API unexpected error: %s", exc)
            return {
                "ok": False,
                "error_code": "ld_direct_api_unexpected",
                "message": f"LD 车道级分析 API 意外错误：{exc}",
                "command": [],
                "provider": provider_tag,
                "analysis_markdown_path": analysis_markdown_path,
                "duration_seconds": time.monotonic() - started,
            }

        message = (response.content or "").strip()
        if not message:
            return {
                "ok": False,
                "error_code": "ld_direct_api_empty",
                "message": "LD 车道级分析 API 返回空响应。",
                "command": [],
                "provider": provider_tag,
                "analysis_markdown_path": analysis_markdown_path,
                "duration_seconds": time.monotonic() - started,
            }

        # Write analysis output
        analysis_markdown_path.write_text(message + "\n", encoding="utf-8")
        duration = time.monotonic() - started

        # Validate
        valid, reason, evidence_count = self._validate_custom_skill_analysis(analysis_markdown_path)
        if not valid:
            return {
                "ok": False,
                "error_code": "ld_direct_api_invalid_evidence",
                "message": f"LD 车道级 direct_api 分析输出缺少有效 `## 关键证据`：{reason}",
                "command": [],
                "provider": provider_tag,
                "analysis_markdown_path": analysis_markdown_path,
                "duration_seconds": duration,
                "evidence_count": evidence_count,
            }

        # Generate report
        self._write_custom_skill_agent_report(
            analysis_kind="ld_lane_level",
            analysis_label=self._analysis_label("ld_lane_level"),
            html_path=html_path,
            json_path=json_path,
            analysis_markdown_path=analysis_markdown_path,
            skill_name=skill_name,
            provider=provider_tag,
            request_text=request_text,
            prompt_text=prompt_text,
            title=title,
            description=description,
            fault_time=fault_time,
            selected_input=selected_input,
            prepared_input=prepared_input,
            source_evidence_path=None,
            evidence_count=evidence_count,
            duration_seconds=duration,
        )

        self._emit_progress(
            progress_callback,
            stage="ld_direct_api_done",
            message=f"LD 车道级分析完成（{duration:.1f}s, executor+direct_api）",
            provider=provider_tag,
            model=response.model or ai_opts.primary_model,
            evidence_count=evidence_count,
        )

        return {
            "ok": True,
            "error_code": "",
            "message": "",
            "command": [],
            "analysis_kind": "ld_lane_level",
            "provider": provider_tag,
            "analysis_markdown_path": analysis_markdown_path,
            "html_path": html_path,
            "json_path": json_path,
            "context_path": context_path,
            "duration_seconds": duration,
            "evidence_count": evidence_count,
            "custom_skill_analysis_status": "completed",
            "execution_backend": "ld_executor_direct_api",
        }

    def _resolve_bug_cache_dir(self, input_path: Path | None) -> Path | None:
        """Walk up from input_path to find the bug_cache/<id>/ directory."""
        if input_path is None:
            return None
        current = input_path.resolve()
        for _ in range(10):
            if current.name == "logs" and (current.parent / "attachments").exists():
                return current.parent
            if current.name.startswith("xpfailuremgmt_") or current.name.startswith("bug_"):
                return current
            if current.parent == current:
                break
            current = current.parent
        # Fallback: try to find bug_cache in known data dir
        data_dir = Path(self.config.workspace_root) / "tools" / "lark-agent-bridge" / "data" / "bug_cache"
        if not data_dir.exists():
            data_dir = Path(__file__).resolve().parent.parent.parent / "data" / "bug_cache"
        if data_dir.exists():
            # Return the most recent bug cache dir
            candidates = sorted(data_dir.iterdir(), key=lambda p: p.stat().st_mtime if p.is_dir() else 0, reverse=True)
            if candidates:
                return candidates[0]
        return None

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
        if "node_status" not in payload or "findings" not in payload:
            parsed = parse_node_status_block(analysis_text)
            if parsed:
                payload.setdefault("node_status", parsed.get("node_status", {}))
                payload.setdefault("findings", parsed.get("findings", []))
        if "node_status" not in payload:
            payload["node_status"] = {}
        if "findings" not in payload:
            payload["findings"] = []
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

    def _run_bug_agent_summary(
        self,
        *,
        request_text: str,
        request_artifact: Path,
        metadata_path: Path,
        output_path: Path,
        progress_callback: Callable[[dict[str, object]], None] | None,
        timeout: int,
        provider_session_id: str = "",
        followup_text: str = "",
        previous_summary_path: Path | None = None,
        prefer_lightweight: bool = False,
        provider_override: str = "",
        command_override: str = "",
        bridge_session_id: str = "",
        snapshot_details: dict[str, object] | None = None,
        snapshot_plans: list["BugAnalysisPlan"] | None = None,
    ) -> dict[str, object]:
        explicit_provider = _normalize_provider_name(provider_override)
        if explicit_provider == "omlx":
            return self._annotate_summary_backend_result(
                self._run_bug_agent_summary_omlx_fallback(
                    request_text=request_text,
                    request_artifact=request_artifact,
                    metadata_path=metadata_path,
                    output_path=output_path,
                    followup_text=followup_text,
                    previous_summary_path=previous_summary_path,
                    progress_callback=progress_callback,
                    reason="explicit_agent",
                    allow_file_context=True,
                    snapshot_details=snapshot_details,
                    snapshot_plans=snapshot_plans,
                ),
                execution_backend="omlx",
                backend_reason="explicit_lightweight",
            )
        skip_direct_api = self._should_skip_direct_api_bug_summary(
            request_text=request_text,
            followup_text=followup_text,
            request_artifact=request_artifact,
            metadata_path=metadata_path,
            previous_summary_path=previous_summary_path,
        )
        backend_decision = choose_summary_backend(
            SummaryBackendInput(
                explicit_provider=explicit_provider,
                provider_session_id=provider_session_id,
                prefer_lightweight=False,
                ai_provider_enabled=self.config.ai_provider.enabled,
                ai_provider_base_url=self.config.ai_provider.base_url,
                ai_provider_primary_model=self.config.ai_provider.primary_model,
                skip_direct_api=skip_direct_api,
                auto_fallback_to_file_agent=self.config.bug_analysis.auto_fallback_to_file_agent,
            )
        )
        invocation = self._build_bug_agent_summary_command(
            request_text=request_text,
            request_artifact=request_artifact,
            metadata_path=metadata_path,
            output_path=output_path,
            provider_session_id=provider_session_id,
            followup_text=followup_text,
            previous_summary_path=previous_summary_path,
            provider_override=explicit_provider,
            command_override=command_override,
            snapshot_details=snapshot_details,
            snapshot_plans=snapshot_plans,
        )
        if not invocation["command"]:
            return {
                "message": "",
                "command": None,
                "error": "agent_summary_not_configured",
                "provider": "",
                "session_id": provider_session_id,
                "resumed": False,
                "usage_scope": "",
            }
        explicit_file_agent = bool(explicit_provider)
        if prefer_lightweight:
            omlx_result = self._run_bug_agent_summary_omlx_fallback(
                request_text=request_text,
                request_artifact=request_artifact,
                metadata_path=metadata_path,
                output_path=output_path,
                followup_text=followup_text,
                previous_summary_path=previous_summary_path,
                progress_callback=progress_callback,
                reason="lightweight_first",
                snapshot_details=snapshot_details,
                snapshot_plans=snapshot_plans,
            )
            if omlx_result["message"]:
                return self._annotate_summary_backend_result(
                    omlx_result,
                    execution_backend="omlx",
                    backend_reason="prefer_lightweight",
                )
            self._emit_progress(
                progress_callback,
                stage="bug_agent_summary_lightweight_unavailable",
                message="轻量总结不可用，切换主 Agent 整理最终结论",
                primary_provider=str(invocation["provider"] or ""),
                lightweight_provider="omlx",
                lightweight_error=str(omlx_result.get("error") or ""),
            )
        # --- Direct API path (fast, preferred when [ai_provider] is enabled) ---
        if (
            not explicit_file_agent
            and not provider_session_id.strip()
            and backend_decision.backend == "direct_api"
        ):
            api_result = self._run_bug_agent_summary_via_api(
                request_text=request_text,
                request_artifact=request_artifact,
                metadata_path=metadata_path,
                output_path=output_path,
                followup_text=followup_text,
                previous_summary_path=previous_summary_path,
                progress_callback=progress_callback,
                snapshot_details=snapshot_details,
                snapshot_plans=snapshot_plans,
            )
            if api_result["message"]:
                return self._annotate_summary_backend_result(
                    api_result,
                    execution_backend="direct_api",
                    backend_reason=backend_decision.reason,
                    fallback_from=backend_decision.fallback_from,
                )
            failure_decision = choose_summary_backend(
                SummaryBackendInput(
                    explicit_provider=explicit_provider,
                    provider_session_id=provider_session_id,
                    prefer_lightweight=False,
                    ai_provider_enabled=self.config.ai_provider.enabled,
                    ai_provider_base_url=self.config.ai_provider.base_url,
                    ai_provider_primary_model=self.config.ai_provider.primary_model,
                    skip_direct_api=skip_direct_api,
                    direct_api_failure=str(api_result.get("error") or "direct_api_failed"),
                    auto_fallback_to_file_agent=self.config.bug_analysis.auto_fallback_to_file_agent,
                )
            )
            if failure_decision.backend == "direct_api":
                return self._annotate_summary_backend_result(
                    api_result,
                    execution_backend="direct_api",
                    backend_reason=failure_decision.reason,
                    fallback_from=failure_decision.fallback_from,
                )
            logger.warning(
                "Direct API summary failed (error=%s), falling back to subprocess",
                api_result.get("error", "unknown"),
            )
            backend_decision = failure_decision
        result = self._run_bug_agent_summary_once(
            invocation=invocation,
            output_path=output_path,
            progress_callback=progress_callback,
            timeout=timeout,
            bridge_session_id=bridge_session_id,
        )
        if result["message"] and result["provider"]:
            return self._annotate_summary_backend_result(
                result,
                execution_backend="file_agent",
                backend_reason=backend_decision.reason,
                fallback_from=backend_decision.fallback_from,
            )
        if explicit_file_agent:
            return self._annotate_summary_backend_result(
                result,
                execution_backend="file_agent",
                backend_reason=backend_decision.reason,
                fallback_from=backend_decision.fallback_from,
            )
        if result["message"]:
            return self._annotate_summary_backend_result(
                result,
                execution_backend="file_agent",
                backend_reason=backend_decision.reason,
                fallback_from=backend_decision.fallback_from,
            )
        fallback_result = result
        if provider_session_id.strip() and str(result.get("error") or "") != "agent_summary_timeout":
            self._emit_progress(
                progress_callback,
                stage="bug_agent_summary_retry",
                message="Agent 续会话失败，退回重新读取最新产物整理结论",
                provider=invocation["provider"],
                previous_session_id=provider_session_id.strip(),
            )
            fallback_invocation = self._build_bug_agent_summary_command(
                request_text=request_text,
                request_artifact=request_artifact,
                metadata_path=metadata_path,
                output_path=output_path,
                provider_session_id="",
                followup_text=followup_text,
                previous_summary_path=previous_summary_path,
                snapshot_details=snapshot_details,
                snapshot_plans=snapshot_plans,
            )
            if fallback_invocation["command"]:
                fallback_result = self._run_bug_agent_summary_once(
                    invocation=fallback_invocation,
                    output_path=output_path,
                    progress_callback=progress_callback,
                    timeout=timeout,
                    bridge_session_id=bridge_session_id,
                )
                if fallback_result["message"] and fallback_result["provider"]:
                    return self._annotate_summary_backend_result(
                        fallback_result,
                        execution_backend="file_agent",
                        backend_reason="resume_retry_file_agent",
                    )
        if str(fallback_result.get("error") or "") == "agent_summary_timeout":
            omlx_result = self._run_bug_agent_summary_omlx_fallback(
                request_text=request_text,
                request_artifact=request_artifact,
                metadata_path=metadata_path,
                output_path=output_path,
                followup_text=followup_text,
                previous_summary_path=previous_summary_path,
                progress_callback=progress_callback,
                reason="primary_timeout",
                snapshot_details=snapshot_details,
                snapshot_plans=snapshot_plans,
            )
            if omlx_result["message"]:
                return self._annotate_summary_backend_result(
                    omlx_result,
                    execution_backend="omlx",
                    backend_reason="primary_timeout",
                    fallback_from="file_agent",
                )
            return self._annotate_summary_backend_result(
                fallback_result,
                execution_backend="file_agent",
                backend_reason=backend_decision.reason,
                fallback_from=backend_decision.fallback_from,
            )
        return self._annotate_summary_backend_result(
            fallback_result,
            execution_backend="file_agent",
            backend_reason=backend_decision.reason,
            fallback_from=backend_decision.fallback_from,
        )

    def _annotate_summary_backend_result(
        self,
        result: dict[str, object],
        *,
        execution_backend: str,
        backend_reason: str = "",
        fallback_from: str = "",
    ) -> dict[str, object]:
        annotated = dict(result)
        annotated["execution_backend"] = execution_backend
        annotated["backend_reason"] = backend_reason
        annotated["fallback_from"] = fallback_from
        return annotated

    def _agent_summary_timeout(
        self,
        operation_timeout_seconds: int,
        *,
        reference_seconds: float | None = None,
    ) -> int:
        configured = int(getattr(self.config.bug_analysis, "agent_summary_timeout_seconds", 300) or 0)
        operation_limit = max(1, min(int(operation_timeout_seconds or 0), 1800))
        if configured <= 0:
            base_timeout = operation_limit
        else:
            base_timeout = max(1, min(operation_limit, configured))
        if not isinstance(reference_seconds, (int, float)) or reference_seconds <= 0:
            return base_timeout
        reference_timeout = int(math.ceil(float(reference_seconds) * 1.25))
        return max(1, min(operation_limit, max(base_timeout, reference_timeout)))

    def _agent_summary_timeout_reference(self, previous_session: dict[str, object] | None) -> float | None:
        if not isinstance(previous_session, dict):
            return None
        candidates: list[float] = []
        for value in (previous_session.get("duration_seconds"), previous_session.get("elapsed_seconds")):
            if isinstance(value, (int, float)) and value > 0:
                candidates.append(float(value))
        details = previous_session.get("details")
        if isinstance(details, dict):
            for key in ("agent_summary_duration_seconds", "agent_summary_timeout_seconds"):
                value = details.get(key)
                if isinstance(value, (int, float)) and value > 0:
                    candidates.append(float(value))
        return max(candidates) if candidates else None

    def _run_bug_agent_summary_omlx_fallback(
        self,
        *,
        request_text: str,
        request_artifact: Path,
        metadata_path: Path,
        output_path: Path,
        followup_text: str = "",
        previous_summary_path: Path | None = None,
        progress_callback: Callable[[dict[str, object]], None] | None,
        reason: str = "primary_timeout",
        allow_file_context: bool = False,
        snapshot_details: dict[str, object] | None = None,
        snapshot_plans: list["BugAnalysisPlan"] | None = None,
    ) -> dict[str, object]:
        options = self.config.omlx_chat
        provider = "omlx"
        command = ["omlx", options.model]
        if self._bug_summary_referenced_context_files(metadata_path) and not allow_file_context:
            return {
                "message": "",
                "command": command,
                "error": "omlx_file_context_unsupported",
                "provider": provider,
                "session_id": "",
                "resumed": False,
                "usage": {},
                "usage_scope": "",
            }
        if not options.enabled:
            return {
                "message": "",
                "command": command,
                "error": "omlx_disabled",
                "provider": provider,
                "session_id": "",
                "resumed": False,
                "usage": {},
                "usage_scope": "",
            }
        prompt = self._build_omlx_bug_summary_prompt(
            request_text=request_text,
            request_artifact=request_artifact,
            metadata_path=metadata_path,
            followup_text=followup_text,
            previous_summary_path=previous_summary_path,
            include_context_file_excerpts=allow_file_context,
            snapshot_details=snapshot_details,
            snapshot_plans=snapshot_plans,
        )
        if not prompt.strip():
            return {
                "message": "",
                "command": command,
                "error": "omlx_prompt_empty",
                "provider": provider,
                "session_id": "",
                "resumed": False,
                "usage": {},
                "usage_scope": "",
            }
        self._emit_progress(
            progress_callback,
            stage=(
                "bug_agent_summary_omlx"
                if reason in {"lightweight_first", "explicit_agent"}
                else "bug_agent_summary_omlx_fallback"
            ),
            message=(
                "用户指定 OMLX 本地模型基于已生成材料重新分析"
                if reason == "explicit_agent"
                else "本地 omlx 基于已生成报告/元数据整理最终结论"
                if reason == "lightweight_first"
                else "主 Agent 超时，改用本地 omlx 基于现有材料做轻量总结"
            ),
            provider=provider,
            model=options.model,
            reason=reason,
        )
        started = time.monotonic()
        result = OmlxChatClient(self.config)._chat(
            mode="bug_agent_summary_omlx",
            system_prompt=(
                "你是本地轻量 bug 总结模型。只能基于用户提供的现有 request、metadata、报告摘录和历史摘要回答；"
                "不要声称读取了文件系统或源码；如果材料不足，要明确写出缺口。输出中文 Markdown，结论先行。"
            ),
            messages=[{"role": "user", "content": prompt}],
        )
        if result.success and result.message.strip():
            message = result.message.strip()
            try:
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(message, encoding="utf-8")
            except OSError:
                pass
            return {
                "message": message,
                "command": command,
                "error": "",
                "provider": provider,
                "model": options.model,
                "session_id": "",
                "resumed": False,
                "duration_seconds": result.duration_seconds
                if isinstance(result.duration_seconds, (int, float))
                else time.monotonic() - started,
                "usage": {},
                "usage_scope": "",
            }
        return {
            "message": "",
            "command": command,
            "error": result.error_code or result.message or "omlx_fallback_failed",
            "provider": provider,
            "model": options.model,
            "session_id": "",
            "resumed": False,
            "duration_seconds": result.duration_seconds
            if isinstance(result.duration_seconds, (int, float))
            else time.monotonic() - started,
            "usage": {},
            "usage_scope": "",
        }

    def _bug_prompt_snapshot_path(self, output_dir: Path) -> Path:
        return output_dir / "conversation_facts.json"

    def _read_bug_prompt_snapshot(self, path: Path) -> "prompt_snapshots.BugPromptSnapshot | None":
        try:
            return prompt_snapshots.read_prompt_snapshot(path)
        except (OSError, ValueError):
            return None

    def _write_bug_prompt_snapshot(self, path: Path, snapshot: "prompt_snapshots.BugPromptSnapshot") -> None:
        try:
            prompt_snapshots.write_prompt_snapshot(path, snapshot)
        except OSError:
            return

    def _snapshot_field_from_text(self, text: str, *labels: str) -> str:
        for label in labels:
            match = re.search(rf"(?m)^- {re.escape(label)}:\s*(.+?)\s*$", text)
            if not match:
                continue
            value = match.group(1).strip().strip("`").strip()
            if value:
                return value
        return ""

    def _extract_bug_url_from_text(self, *texts: str) -> str:
        for text in texts:
            if not text:
                continue
            match = re.search(r"https?://project\.feishu\.cn/\S+/buglo/detail/\d+", text)
            if match:
                return match.group(0).rstrip("`，。；;、)")
        return ""

    def _bug_prompt_snapshot_details_from_metadata(
        self,
        *,
        request_text: str,
        metadata_path: Path,
    ) -> dict[str, object]:
        try:
            metadata_text = metadata_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            metadata_text = ""
        details: dict[str, object] = {}
        bug_url = self._extract_bug_url_from_text(request_text, metadata_text)
        if bug_url:
            details["bug_url"] = bug_url
        target_time = self._snapshot_field_from_text(
            metadata_text,
            "修正后的故障时间",
            "故障时间",
            "目标时间",
        )
        if target_time and target_time != "未识别":
            details["target_time"] = target_time
        analysis_value = self._snapshot_field_from_text(metadata_text, "分析类型")
        if analysis_value and analysis_value != "无":
            kinds = [
                item.strip()
                for item in re.split(r"[，,|/]", analysis_value)
                if item.strip() and item.strip() != "无"
            ]
            if kinds:
                details["analysis_kind"] = kinds[0]
                details["analysis_kinds"] = kinds
        if "analysis_kind" not in details:
            skill_name = self._snapshot_field_from_text(metadata_text, "命中 Skill")
            route = self.skill_manager.primary_skill_map().get(skill_name) if skill_name else None
            if route is not None:
                details["analysis_kind"] = route[0]
                details["analysis_kinds"] = [route[0]]
        if "analysis_kind" not in details:
            inferred_kind = ""
            for marker, kind in (
                ("bug_3d_startup_report", "startup"),
                ("bug_3d_stuck_report", "stuck"),
                ("bug_crash_report", "crash"),
                ("bug_scene_signal_report", "scene_signal"),
                ("bug_signal_chain_report", "signal"),
                ("bug_perception_data_summary", "perception"),
                ("bug_xtheme_analysis_report", "xtheme"),
                ("bug_ld_lane_level_report", "ld_lane_level"),
                ("bug_general_analysis_report", "general"),
                ("bug_custom_skill_report", SOURCE_CODE_SKILL_KIND),
                ("bug_source_code_report", SOURCE_CODE_SKILL_KIND),
            ):
                if marker in metadata_text:
                    inferred_kind = kind
                    break
            if inferred_kind:
                details["analysis_kind"] = inferred_kind
                details["analysis_kinds"] = [inferred_kind]
        prepared_input = self._snapshot_field_from_text(
            metadata_text,
            "复用 prepared log 输入",
            "prepared log 输入",
            "prepared log input",
        )
        if prepared_input:
            details["prepared_log_input"] = prepared_input
        selected_input = self._snapshot_field_from_text(
            metadata_text,
            "上一轮选中的日志输入",
            "selected log 输入",
            "selected log input",
        )
        if selected_input:
            details["selected_log_input"] = selected_input
        report_version = self._snapshot_field_from_text(metadata_text, "report_version", "Report Version")
        if report_version:
            details["report_version"] = report_version
        return details

    def _bug_prompt_snapshot_analysis_kind(
        self,
        *,
        details: dict[str, object],
        plans_override: list["BugAnalysisPlan"] | None,
    ) -> str:
        if plans_override:
            for plan in plans_override:
                kind = str(getattr(plan, "kind", "") or "").strip()
                if kind:
                    return kind
        raw_kinds = details.get("analysis_kinds")
        if isinstance(raw_kinds, list):
            for item in raw_kinds:
                kind = str(item or "").strip()
                if kind:
                    return kind
        kind = str(details.get("analysis_kind") or "").strip()
        return kind or "general"

    def _bug_prompt_snapshot_scope_key(
        self,
        *,
        request_text: str,
        details: dict[str, object],
        output_dir: Path | None,
        existing: "prompt_snapshots.BugPromptSnapshot | None",
    ) -> str:
        if existing is not None and existing.scope_key.strip():
            return existing.scope_key
        bug_url = str(details.get("bug_url") or "").strip() or self._extract_bug_url_from_text(request_text)
        bug_id = self._bug_id(bug_url or request_text)
        scope_suffix = output_dir.parent.name if output_dir is not None else "followup"
        return f"bug:{bug_id}:{scope_suffix}"

    def _structured_bug_prompt_snapshot_details(
        self,
        *,
        base_details: dict[str, object] | None,
        request_text: str,
        plans: list["BugAnalysisPlan"] | None = None,
        target_time: str = "",
        prepared_input: Path | None = None,
        selected_input: Path | None = None,
    ) -> dict[str, object]:
        details = dict(base_details) if isinstance(base_details, dict) else {}
        bug_url = str(details.get("bug_url") or "").strip() or self._extract_bug_url_from_text(request_text)
        if bug_url:
            details["bug_url"] = bug_url
        if target_time.strip():
            details["target_time"] = target_time.strip()
        if prepared_input is not None:
            details["prepared_log_input"] = str(prepared_input)
        if selected_input is not None:
            details["selected_log_input"] = str(selected_input)
        if plans:
            details["analysis_kind"] = plans[0].kind
            details["analysis_kinds"] = [plan.kind for plan in plans]
            signal_codes = [plan.signal_code for plan in plans if plan.kind == "signal" and plan.signal_code]
            if signal_codes:
                details["signal_code"] = signal_codes[0]
        return details

    def _followup_needs_previous_summary_text(self, followup_text: str) -> bool:
        lowered = followup_text.casefold()
        compare_terms = (
            "对比上一轮",
            "对比上次",
            "上一轮结论",
            "上次结论",
            "旧结论",
            "老结论",
            "新旧结论",
        )
        if any(term in lowered for term in compare_terms):
            return True
        return (
            ("上次为什么" in lowered or "上一轮为什么" in lowered)
            and ("判断" in lowered or "结论" in lowered)
        )

    def _should_include_previous_summary_for_followup(
        self,
        *,
        followup_text: str,
        request_artifact: Path,
        metadata_path: Path,
    ) -> bool:
        if not followup_text.strip():
            return False
        names = {request_artifact.name.casefold(), metadata_path.name.casefold()}
        if any("reanalysis" in name for name in names):
            return self._followup_needs_previous_summary_text(followup_text)
        return self._followup_needs_previous_summary_text(followup_text)

    def _is_postmortem_or_conflict_followup(self, text: str) -> bool:
        lowered = text.casefold()
        terms = (
            "复盘",
            "前后结论",
            "结论冲突",
            "结论完全不一样",
            "前后不一致",
            "上次为什么",
            "上一轮为什么",
            "错判",
            "重新审视",
        )
        return any(term.casefold() in lowered for term in terms)

    def _metadata_has_structured_report_conflict(self, metadata_path: Path) -> bool:
        for item in self._bug_summary_context_items(metadata_path):
            path = Path(str(item.get("path") or ""))
            if not self._is_report_json_path(path):
                continue
            payload = self._load_structured_report_payload(path)
            if payload is None:
                continue
            focus_session = self._structured_report_focus_session(payload)
            if not isinstance(focus_session, dict):
                continue
            if self._structured_report_conflicts(payload, focus_session):
                return True
        return False

    def _should_skip_direct_api_bug_summary(
        self,
        *,
        request_text: str,
        followup_text: str,
        request_artifact: Path,
        metadata_path: Path,
        previous_summary_path: Path | None,
    ) -> bool:
        if self._should_collect_source_evidence(request_text, followup_text):
            return True
        if self._is_postmortem_or_conflict_followup(request_text) or self._is_postmortem_or_conflict_followup(followup_text):
            return True
        if previous_summary_path is not None and self._should_include_previous_summary_for_followup(
            followup_text=followup_text,
            request_artifact=request_artifact,
            metadata_path=metadata_path,
        ) and self._is_postmortem_or_conflict_followup(followup_text):
            return True
        return self._metadata_has_structured_report_conflict(metadata_path)

    def _is_report_json_path(self, path: Path) -> bool:
        lowered = path.name.casefold()
        return path.suffix.lower() == ".json" and "_report" in lowered

    def _is_report_html_path(self, path: Path) -> bool:
        lowered = path.name.casefold()
        return path.suffix.lower() == ".html" and "_report" in lowered

    def _bug_summary_context_items(
        self,
        metadata_path: Path,
        *,
        include_html_reports: bool = True,
    ) -> list[dict[str, object]]:
        items = self._bug_summary_referenced_context_files(metadata_path)[:5]
        if include_html_reports:
            return items
        has_report_json = any(self._is_report_json_path(Path(str(item.get("path") or ""))) for item in items)
        if not has_report_json:
            return items
        filtered: list[dict[str, object]] = []
        for item in items:
            path = Path(str(item.get("path") or ""))
            if self._is_report_html_path(path):
                continue
            filtered.append(item)
        return filtered

    def _bug_summary_decoded_log_paths(self, context_items: list[dict[str, object]]) -> list[str]:
        paths: list[str] = []
        seen: set[str] = set()
        for item in context_items:
            path = Path(str(item.get("path") or ""))
            if not self._is_report_json_path(path):
                continue
            payload = self._load_structured_report_payload(path)
            if payload is None:
                continue
            decoded_logs = payload.get("decoded_logs")
            if not isinstance(decoded_logs, list):
                continue
            for entry in decoded_logs:
                candidate = str(entry or "").strip()
                if not candidate or candidate in seen:
                    continue
                seen.add(candidate)
                paths.append(candidate)
                if len(paths) >= 8:
                    return paths
        return paths

    def _render_structured_report_guardrails(self, path: Path) -> str:
        if not self._is_report_json_path(path):
            return ""
        payload = self._load_structured_report_payload(path)
        if payload is None:
            return ""
        focus_session = self._structured_report_focus_session(payload)
        if not isinstance(focus_session, dict):
            return ""
        status = str(focus_session.get("status") or "").strip()
        diagnosis = str(focus_session.get("diagnosis") or "").strip()
        missing_critical = [
            str(item).strip()
            for item in focus_session.get("missing_critical") or []
            if str(item).strip()
        ]
        events = focus_session.get("events")
        last_event = events[-1] if isinstance(events, list) and events and isinstance(events[-1], dict) else None
        lines = [
            "### 结构化证据护栏",
            f"来源: {path}",
        ]
        verdict = payload.get("verdict")
        if isinstance(verdict, dict):
            verdict_message = str(verdict.get("message") or "").strip()
            if verdict_message:
                lines.append(f"- 报告顶层 verdict: {verdict_message[:220]}")
        focus_pid = payload.get("focus_session_pid")
        if focus_pid not in (None, ""):
            lines.append(f"- focus session: Session {focus_session.get('index', '?')} / PID {focus_pid}")
        if status:
            lines.append(f"- status={status}")
        if diagnosis:
            lines.append(f"- structured diagnosis: {diagnosis[:220]}")
        if missing_critical:
            lines.append("- missing_critical: " + "、".join(missing_critical[:6]))
        if isinstance(last_event, dict):
            title = str(last_event.get("title") or "").strip()
            timestamp = str(last_event.get("timestamp_text") or last_event.get("timestamp") or "").strip()
            file_path = str(last_event.get("file_path") or "").strip()
            line_no = str(last_event.get("line_no") or "").strip()
            location = f"{file_path}:{line_no}".rstrip(":") if file_path else ""
            detail = " ".join(part for part in [timestamp, title, location] if part).strip()
            if detail:
                lines.append(f"- 最后命中事件: {detail[:260]}")
        for conflict in self._structured_report_conflicts(payload, focus_session):
            lines.append(f"- 冲突: {conflict}")
        if status and status != "complete":
            lines.append(
                "- 护栏: `status` 不是 `complete` 时，不要写“启动链路完整”或“已经到达最终首帧展示”；只能描述为“当前已命中到某节点，后续关键节点未命中，链路未闭环”。"
            )
        if missing_critical:
            lines.append(
                "- 护栏: “未命中关键节点”不等于“日志在这里截止”；除非材料明确显示文件结束或时间窗截断，否则不要把缺节点改写成日志截止。"
            )
        return "\n".join(lines)

    def _load_structured_report_payload(self, path: Path) -> dict[str, object] | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _structured_report_focus_session(self, payload: dict[str, object]) -> dict[str, object] | None:
        sessions = payload.get("sessions")
        if not isinstance(sessions, list) or not sessions:
            return None
        focus_index = payload.get("focus_session_index")
        if isinstance(focus_index, int):
            for item in sessions:
                if isinstance(item, dict) and item.get("index") == focus_index:
                    return item
        first_session = sessions[0]
        return first_session if isinstance(first_session, dict) else None
