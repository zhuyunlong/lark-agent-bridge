from __future__ import annotations

from ._shared import *  # noqa: F401,F403


class _ContextExcerptMixin:
    """上下文摘录压缩：markdown 大纲/结构化报告/证据摘录、压缩档位与嵌入文件挑选（与 DirectApiMixin 共享 self 状态）。"""

    def _read_bug_summary_context_excerpt(self, path: Path, max_chars: int) -> str:
        text = self._read_text_excerpt(path, max_chars * 2)
        if not text:
            return ""
        if path.suffix.lower() == ".html":
            text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)
            text = re.sub(r"(?s)<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) > max_chars:
            text = text[: max_chars - 1].rstrip() + "…"
        return text
    def _compact_markdown_outline_excerpt(
        self,
        path: Path,
        *,
        max_chars: int,
        max_headings: int = 6,
        max_entries_per_heading: int = 3,
        max_table_rows: int = 6,
    ) -> str:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        body = text.strip()
        if not body:
            return ""
        lines = body.splitlines()
        summary: list[str] = []
        if path.name.casefold() == "skill.md":
            frontmatter_name, frontmatter_desc = _extract_skill_frontmatter(body)
            if frontmatter_name:
                summary.append(f"skill: {frontmatter_name}")
            if frontmatter_desc:
                summary.append(f"description: {frontmatter_desc}")
        title_line = next(
            (
                self._clean_markdown_inline_text(line.lstrip("#").strip())
                for line in lines
                if re.match(r"^\s*#\s+", line)
            ),
            "",
        )
        if title_line and all(not item.endswith(title_line) for item in summary):
            summary.append(title_line)

        # Skip YAML frontmatter if present.
        start_index = 0
        if lines and lines[0].strip() == "---":
            for index in range(1, len(lines)):
                if lines[index].strip() == "---":
                    start_index = index + 1
                    break

        heading_count = 0
        index = start_index
        while index < len(lines) and heading_count < max_headings:
            raw = lines[index]
            heading_match = re.match(r"^\s*#{2,3}\s+(.+?)\s*$", raw)
            if not heading_match:
                index += 1
                continue
            heading = self._clean_markdown_inline_text(heading_match.group(1))
            if heading:
                summary.append(f"## {heading}")
                heading_count += 1
            entries: list[str] = []
            table_rows = 0
            index += 1
            while index < len(lines) and not re.match(r"^\s*#{2,3}\s+", lines[index]):
                stripped = lines[index].strip()
                if not stripped or stripped.startswith("```"):
                    index += 1
                    continue
                if re.match(r"^[-*]\s+", stripped):
                    entry = self._clean_markdown_inline_text(re.sub(r"^[-*]\s+", "", stripped))
                    if entry:
                        entries.append(entry)
                elif "|" in stripped and max_table_rows > 0:
                    cells = [self._clean_markdown_inline_text(cell) for cell in stripped.strip("|").split("|")]
                    if len(cells) >= 2 and not re.fullmatch(r"[-:\s|]+", stripped):
                        head = cells[0]
                        detail = cells[1]
                        entry = f"{head}: {detail}" if detail else head
                        if entry:
                            entries.append(entry)
                            table_rows += 1
                            if table_rows >= max_table_rows:
                                break
                elif len(entries) < max_entries_per_heading:
                    paragraph = self._clean_markdown_inline_text(stripped)
                    if paragraph:
                        entries.append(paragraph)
                if len(entries) >= max_entries_per_heading:
                    # Still advance to the next heading to keep parser aligned.
                    index += 1
                    while index < len(lines) and not re.match(r"^\s*#{2,3}\s+", lines[index]):
                        index += 1
                    break
                index += 1
            for entry in entries[:max_entries_per_heading]:
                summary.append(f"- {entry}")

        rendered = "\n".join(item for item in summary if item.strip()).strip()
        if not rendered:
            return self._read_bug_summary_context_excerpt(path, max_chars)
        if len(rendered) > max_chars:
            rendered = rendered[: max_chars - 1].rstrip() + "…"
        return rendered
    def _compact_structured_report_excerpt(self, path: Path, *, max_chars: int) -> str:
        payload = self._load_structured_report_payload(path)
        if payload is None:
            return self._read_bug_summary_context_excerpt(path, max_chars)
        lines: list[str] = []
        verdict = payload.get("verdict")
        if isinstance(verdict, dict):
            verdict_message = str(verdict.get("message") or "").strip()
            if verdict_message:
                lines.append(f"- verdict: {verdict_message}")
            issues = verdict.get("issues")
            if isinstance(issues, list) and issues:
                lines.append("- top_issues:")
                for item in issues[:4]:
                    if not isinstance(item, dict):
                        continue
                    title = self._clean_markdown_inline_text(str(item.get("title") or ""))
                    detail = self._clean_markdown_inline_text(str(item.get("detail") or ""))
                    if title or detail:
                        lines.append(f"  - {title}: {detail}".rstrip(": "))
        elif isinstance(verdict, str):
            verdict_message = self._clean_markdown_inline_text(verdict)
            if verdict_message:
                lines.append(f"- verdict: {verdict_message}")
        target_time = str(payload.get("target_time") or "").strip()
        if target_time:
            lines.append(f"- target_time: {target_time}")
        target_focus = payload.get("target_focus")
        if isinstance(target_focus, dict):
            headline = self._clean_markdown_inline_text(str(target_focus.get("headline") or ""))
            summary = self._clean_markdown_inline_text(str(target_focus.get("summary") or ""))
            inferred = self._clean_markdown_inline_text(str(target_focus.get("inferred_state_summary") or ""))
            if headline:
                lines.append(f"- target_focus: {headline}")
            elif summary:
                lines.append(f"- target_focus: {summary}")
            if inferred:
                lines.append(f"- target_state: {inferred}")
        latest_chain = payload.get("latest_sr_chain")
        if isinstance(latest_chain, dict) and not isinstance(target_focus, dict):
            android_event = latest_chain.get("android_event")
            chain_time = ""
            if isinstance(android_event, dict):
                chain_time = str(android_event.get("time") or "").strip()
            chain_value = self._clean_markdown_inline_text(str(latest_chain.get("value_desc") or latest_chain.get("value") or ""))
            if chain_time or chain_value:
                lines.append(f"- latest_sr_chain: {chain_time or '未命中'} {chain_value}".rstrip())
        focus_session_index = payload.get("focus_session_index")
        focus_session_pid = payload.get("focus_session_pid")
        if focus_session_index not in (None, "") or focus_session_pid not in (None, ""):
            lines.append(f"- focus_session: Session {focus_session_index or '?'} / PID {focus_session_pid or '?'}")
        focus_reason = str(payload.get("focus_reason") or "").strip()
        if focus_reason:
            lines.append(f"- focus_reason: {focus_reason}")
        runtime_context = payload.get("runtime_context")
        if isinstance(runtime_context, dict):
            context_parts: list[str] = []
            for key in ("resource_type", "proto_type", "car_type", "branch", "build_time"):
                value = self._clean_markdown_inline_text(str(runtime_context.get(key) or ""))
                if value:
                    context_parts.append(f"{key}={value}")
            if context_parts:
                lines.append("- runtime_context: " + "; ".join(context_parts))
        exception_chain = payload.get("internal_exception_chain")
        if isinstance(exception_chain, dict):
            summary = self._clean_markdown_inline_text(str(exception_chain.get("summary") or ""))
            if summary:
                lines.append(f"- internal_exception_chain: {summary}")
            hits = exception_chain.get("hits")
            if isinstance(hits, list) and hits:
                lines.append("- exception_hits:")
                for item in hits[:3]:
                    if not isinstance(item, dict):
                        continue
                    label = self._clean_markdown_inline_text(str(item.get("label") or ""))
                    evidence = self._clean_markdown_inline_text(str(item.get("evidence") or ""))
                    if label or evidence:
                        lines.append(f"  - {label}: {evidence}".rstrip(": "))
        focus_session = self._structured_report_focus_session(payload)
        if isinstance(focus_session, dict):
            status = str(focus_session.get("status") or "").strip()
            if status:
                lines.append(f"- focus_status: {status}")
            diagnosis = str(focus_session.get("diagnosis") or "").strip()
            if diagnosis:
                lines.append(f"- focus_diagnosis: {diagnosis}")
            missing_critical = [
                self._clean_markdown_inline_text(str(item))
                for item in focus_session.get("missing_critical") or []
                if self._clean_markdown_inline_text(str(item))
            ]
            if missing_critical:
                lines.append("- missing_critical: " + "、".join(missing_critical[:7]))
            events = focus_session.get("events")
            last_event = events[-1] if isinstance(events, list) and events and isinstance(events[-1], dict) else None
            if isinstance(last_event, dict):
                detail = " ".join(
                    part
                    for part in [
                        str(last_event.get("timestamp_text") or last_event.get("timestamp") or "").strip(),
                        self._clean_markdown_inline_text(str(last_event.get("title") or "")),
                        f"{last_event.get('file_path') or ''}:{last_event.get('line_no') or ''}".rstrip(":"),
                    ]
                    if part
                )
                if detail:
                    lines.append(f"- last_event: {detail}")
        rendered = "\n".join(lines).strip()
        if len(rendered) > max_chars:
            rendered = rendered[: max_chars - 1].rstrip() + "…"
        return rendered
    def _compact_bug_summary_evidence_markdown_excerpt(self, path: Path, *, max_chars: int) -> str:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        sections = self._parse_markdown_sections(text)
        lines = ["# Bug Summary Evidence"]
        preferred_sections = [
            "Report conflicts",
            "Missing critical nodes",
            "Last matched lifecycle event",
            "Safe assertions",
            "Forbidden assertions",
        ]
        for title in preferred_sections:
            section_text = sections.get(title, "")
            if not section_text:
                continue
            lines.append("")
            lines.append(f"## {title}")
            for entry in self._markdown_section_entries(section_text)[:3]:
                lines.append(f"- {self._truncate_report_text(entry, 220)}")
        rendered = "\n".join(lines).strip()
        if len(rendered) > max_chars:
            rendered = rendered[: max_chars - 1].rstrip() + "…"
        return rendered
    def _direct_api_compaction_profile(
        self,
        *,
        metadata_path: Path,
        snapshot_details: dict[str, object] | None = None,
    ) -> str:
        try:
            metadata_text = metadata_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            metadata_text = ""
        details = dict(snapshot_details or {})
        if not details:
            details = self._bug_prompt_snapshot_details_from_metadata(request_text="", metadata_path=metadata_path)
        analysis_kind = str(details.get("analysis_kind") or "").strip()
        if not analysis_kind:
            analysis_kind = self._snapshot_field_from_text(metadata_text, "分析类型")
        skill_name = self._snapshot_field_from_text(metadata_text, "命中 Skill")
        for profile in _DIRECT_API_COMPACTION_PROFILES:
            if skill_name != profile.skill_name:
                continue
            if analysis_kind == profile.analysis_kind:
                return profile.name
            route = self.skill_manager.primary_skill_map().get(skill_name)
            if route is not None and str(route[0] or "").strip() == profile.analysis_kind:
                return profile.name
            if any(marker in metadata_text for marker in profile.metadata_markers):
                return profile.name
        return ""
    def _direct_api_context_excerpt_for_startup_unity_lifecycle(
        self,
        *,
        title: str,
        path: Path,
        max_chars: int,
    ) -> str:
        if self._is_report_json_path(path):
            return self._compact_structured_report_excerpt(path, max_chars=min(max_chars, 2200))
        if title == "Bug Summary Evidence":
            return self._compact_bug_summary_evidence_markdown_excerpt(path, max_chars=min(max_chars, 1600))
        if title.startswith("Matched Skill:"):
            return self._compact_markdown_outline_excerpt(
                path,
                max_chars=min(max_chars, 2200),
                max_headings=6,
                max_entries_per_heading=2,
                max_table_rows=4,
            )
        if title.startswith("Matched Skill Reference:"):
            return self._compact_markdown_outline_excerpt(
                path,
                max_chars=min(max_chars, 1600),
                max_headings=6,
                max_entries_per_heading=4,
                max_table_rows=2,
            )
        return self._read_bug_summary_context_excerpt(path, max_chars)
    def _direct_api_context_excerpt_for_scene_signal_target_focus(
        self,
        *,
        title: str,
        path: Path,
        max_chars: int,
    ) -> str:
        if self._is_report_json_path(path):
            return self._compact_structured_report_excerpt(path, max_chars=min(max_chars, 2200))
        if title.startswith("Matched Skill:") or title.startswith("Matched Skill Reference:"):
            return self._compact_markdown_outline_excerpt(
                path,
                max_chars=min(max_chars, 1600),
                max_headings=6,
                max_entries_per_heading=3,
                max_table_rows=3,
            )
        return self._read_bug_summary_context_excerpt(path, max_chars)

    def _compact_xtheme_report_excerpt(self, path: Path, *, max_chars: int) -> str:
        payload = self._load_structured_report_payload(path)
        if payload is None:
            return self._read_bug_summary_context_excerpt(path, max_chars)

        lines: list[str] = ["# XTheme Report Focus"]
        verdict = payload.get("verdict")
        if isinstance(verdict, dict):
            msg = self._clean_markdown_inline_text(str(verdict.get("msg") or verdict.get("message") or ""))
            sev = self._clean_markdown_inline_text(str(verdict.get("sev") or ""))
            if msg:
                prefix = f"[{sev}] " if sev else ""
                lines.append(f"- verdict: {prefix}{msg}")
        target_time = self._clean_markdown_inline_text(str(payload.get("target_time") or ""))
        if target_time:
            lines.append(f"- target_time: {target_time}")
        counts = payload.get("counts")
        if isinstance(counts, dict) and counts:
            lines.append("- counts: " + ", ".join(f"{key}={value}" for key, value in counts.items()))

        focus_snapshot = payload.get("focus_snapshot")
        if isinstance(focus_snapshot, list):
            upstream: list[str] = []
            calculations: list[str] = []
            outputs: list[str] = []
            for item in focus_snapshot:
                if not isinstance(item, dict):
                    continue
                kind = self._clean_markdown_inline_text(str(item.get("kind") or ""))
                value = self._clean_markdown_inline_text(str(item.get("value") or ""))
                ts = self._clean_markdown_inline_text(str(item.get("ts") or ""))
                source = self._clean_markdown_inline_text(str(item.get("source") or ""))
                if not (kind or value):
                    continue
                entry = " ".join(part for part in (ts, kind, value) if part).strip()
                if source:
                    entry += f" [{source}]"
                if "XThemeStrategy" in kind:
                    outputs.append(entry)
                elif "calculateTimeInfo" in kind:
                    calculations.append(entry)
                else:
                    upstream.append(entry)
            if outputs:
                lines.append("## 当前 XTheme 输出")
                lines.extend(f"- {entry}" for entry in outputs[:4])
            if upstream:
                lines.append("## 上游输入")
                lines.extend(f"- {entry}" for entry in upstream[:8])
            if calculations:
                lines.append("## 计算结果")
                lines.extend(f"- {entry}" for entry in calculations[:4])

        issues = payload.get("issues")
        if isinstance(issues, list) and issues:
            lines.append("## 脚本判定问题")
            for item in issues[:4]:
                if not isinstance(item, dict):
                    continue
                title_text = self._clean_markdown_inline_text(str(item.get("title") or ""))
                detail = self._clean_markdown_inline_text(str(item.get("detail") or ""))
                if title_text or detail:
                    lines.append(f"- {title_text}: {detail}".rstrip(": "))

        theme_switches = payload.get("theme_switches")
        if isinstance(theme_switches, list) and theme_switches:
            lines.append("## 问题前后主题切换")
            for item in theme_switches[-8:]:
                if not isinstance(item, dict):
                    continue
                ts = self._clean_markdown_inline_text(str(item.get("ts") or ""))
                from_mode = item.get("from")
                to_mode = item.get("to")
                source = self._clean_markdown_inline_text(
                    f"{item.get('file') or ''}:{item.get('line') or ''}".rstrip(":")
                )
                detail = f"{ts} {from_mode}->{to_mode}".strip()
                if source:
                    detail += f" [{source}]"
                lines.append(f"- {detail}")

        rendered = "\n".join(lines).strip()
        if len(rendered) > max_chars:
            rendered = rendered[: max_chars - 1].rstrip() + "…"
        return rendered

    def _direct_api_context_excerpt_for_xtheme_signal_boundary(
        self,
        *,
        title: str,
        path: Path,
        max_chars: int,
    ) -> str:
        if self._is_report_json_path(path):
            return self._compact_xtheme_report_excerpt(path, max_chars=min(max_chars, 2600))
        if title.startswith("Matched Skill:") or title.startswith("Matched Skill Reference:"):
            return self._compact_markdown_outline_excerpt(
                path,
                max_chars=min(max_chars, 1800),
                max_headings=6,
                max_entries_per_heading=3,
                max_table_rows=4,
            )
        return self._read_bug_summary_context_excerpt(path, max_chars)
    def _direct_api_bug_summary_context_excerpt(
        self,
        *,
        title: str,
        path: Path,
        max_chars: int,
        compaction_profile: str,
    ) -> str:
        if not compaction_profile:
            return self._read_bug_summary_context_excerpt(path, max_chars)
        for profile in _DIRECT_API_COMPACTION_PROFILES:
            if profile.name != compaction_profile:
                continue
            handler = getattr(self, profile.handler)
            return handler(title=title, path=path, max_chars=max_chars)
        return self._read_bug_summary_context_excerpt(path, max_chars)
    def _direct_api_bug_summary_embedded_files(
        self,
        *,
        request_artifact: Path,
        metadata_path: Path,
        followup_text: str = "",
        previous_summary_path: Path | None = None,
    ) -> list[dict[str, object]]:
        files: list[dict[str, object]] = []
        if previous_summary_path is not None and (
            not followup_text.strip()
            or self._should_include_previous_summary_for_followup(
                followup_text=followup_text,
                request_artifact=request_artifact,
                metadata_path=metadata_path,
            )
        ):
            files.append({"title": "上一轮 Agent 总结", "path": str(previous_summary_path), "max_chars": 12000})
        title_request = "Bug Agent Follow-up Request" if followup_text.strip() else "Bug Agent Request"
        title_metadata = "Bug Follow-up Metadata" if followup_text.strip() else "Bug Metadata"
        files.append({"title": title_request, "path": str(request_artifact), "max_chars": 12000})
        files.append({"title": title_metadata, "path": str(metadata_path), "max_chars": 12000})
        for item in self._bug_summary_context_items(metadata_path, include_html_reports=False):
            files.append(item)
        return files
    def _omlx_prompt_section(self, title: str, text: str, max_chars: int) -> str:
        body = (text or "").strip()
        if not body:
            return ""
        if len(body) > max_chars:
            body = body[: max_chars - 1].rstrip() + "…"
        return f"### {title}\n{body}"
    def _read_text_excerpt(self, path: Path, max_chars: int) -> str:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        text = text.strip()
        if len(text) > max_chars:
            text = text[: max_chars - 1].rstrip() + "…"
        return text
    def _path_mtime(self, path: Path) -> float | None:
        try:
            return path.stat().st_mtime
        except OSError:
            return None
    def _read_fresh_agent_summary_message(self, output_path: Path, *, previous_mtime: float | None) -> str:
        current_mtime = self._path_mtime(output_path)
        if current_mtime is None:
            return ""
        if previous_mtime is not None and current_mtime <= previous_mtime:
            return ""
        try:
            return output_path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
