from __future__ import annotations

from ._shared import *  # noqa: F401,F403


class _DirectApiMixin:
    def _structured_report_conflicts(
        self,
        payload: dict[str, object],
        focus_session: dict[str, object],
    ) -> list[str]:
        conflicts: list[str] = []
        status = str(focus_session.get("status") or "").strip()
        diagnosis = str(focus_session.get("diagnosis") or "").strip()
        missing_critical = [
            str(item).strip()
            for item in focus_session.get("missing_critical") or []
            if str(item).strip()
        ]
        verdict = payload.get("verdict")
        verdict_message = ""
        if isinstance(verdict, dict):
            verdict_message = str(verdict.get("message") or "").strip()
        complete_phrases = ("启动链路完整", "最终首帧展示", "已经到达 3D 最终首帧展示")
        says_complete = any(phrase in diagnosis or phrase in verdict_message for phrase in complete_phrases)
        if status and status != "complete" and says_complete:
            conflicts.append(f"status={status} 但 report verdict/diagnosis 仍声称链路完整")
        if missing_critical and says_complete:
            conflicts.append("missing_critical 非空，但 report verdict/diagnosis 仍声称链路完整")
        return conflicts

    def _looks_like_startup_event(self, event: dict[str, object]) -> bool:
        title = str(event.get("title") or "").strip()
        return bool(title)

    def _read_same_pid_log_excerpt(
        self,
        *,
        file_path: str,
        pid: int | None,
        line_no: int,
        max_lines: int = 24,
        errors_only: bool = False,
    ) -> list[str]:
        if not file_path or pid is None or line_no <= 0:
            return []
        path = Path(file_path)
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        start = max(0, line_no - 1)
        excerpt: list[str] = []
        pid_token = f" {pid} "
        for raw in lines[start:]:
            if pid_token not in raw:
                continue
            if errors_only and not re.search(r"\s[EW]\s", raw):
                continue
            excerpt.append(raw.strip())
            if len(excerpt) >= max_lines:
                break
        return excerpt

    def _render_bug_summary_evidence_markdown(self, evidence: dict[str, object]) -> str:
        lines = [
            "# Bug Summary Evidence",
            "",
            f"- analysis_kind: `{evidence.get('analysis_kind') or ''}`",
            f"- target_time: `{evidence.get('target_time') or ''}`",
            f"- focus session: `Session {evidence.get('focus_session_index') or '?'} / PID {evidence.get('focus_session_pid') or '?'}`",
            f"- status: `{evidence.get('focus_status') or ''}`",
        ]
        conflicts = evidence.get("report_conflicts") or []
        if conflicts:
            lines.extend(["", "## Report conflicts", ""])
            for item in conflicts:
                lines.append(f"- {item}")
        missing_critical = evidence.get("missing_critical") or []
        if missing_critical:
            lines.extend(["", "## Missing critical nodes", ""])
            for item in missing_critical:
                lines.append(f"- {item}")
        last_event = evidence.get("last_matched_event") or {}
        if isinstance(last_event, dict) and last_event:
            lines.extend(["", "## Last matched lifecycle event", ""])
            for item in (
                last_event.get("timestamp_text"),
                last_event.get("title"),
                f"{last_event.get('file_path') or ''}:{last_event.get('line_no') or ''}".rstrip(":"),
                f"PID {last_event.get('pid')}" if last_event.get("pid") not in (None, "") else "",
                last_event.get("excerpt"),
            ):
                if item:
                    lines.append(f"- {item}")
        trailing = evidence.get("same_pid_trailing_log_excerpt") or []
        if trailing:
            lines.extend(["", "## Same PID trailing logs", ""])
            for item in trailing:
                lines.append(f"- {item}")
        errors = evidence.get("same_pid_error_excerpt") or []
        if errors:
            lines.extend(["", "## Same PID errors", ""])
            for item in errors:
                lines.append(f"- {item}")
        safe_assertions = evidence.get("safe_assertions") or []
        if safe_assertions:
            lines.extend(["", "## Safe assertions", ""])
            for item in safe_assertions:
                lines.append(f"- {item}")
        forbidden_assertions = evidence.get("forbidden_assertions") or []
        if forbidden_assertions:
            lines.extend(["", "## Forbidden assertions", ""])
            for item in forbidden_assertions:
                lines.append(f"- {item}")
        return "\n".join(lines).rstrip() + "\n"

    def _write_bug_summary_evidence(
        self,
        *,
        output_dir: Path,
        analysis_kind: str,
        report_jsons: dict[str, Path | None],
    ) -> Path | None:
        if analysis_kind != "startup":
            return None
        report_json = report_jsons.get("startup")
        if report_json is None or not report_json.exists():
            return None
        payload = self._load_structured_report_payload(report_json)
        if payload is None:
            return None
        focus_session = self._structured_report_focus_session(payload)
        if not isinstance(focus_session, dict):
            return None
        focus_pid = payload.get("focus_session_pid")
        try:
            normalized_pid = int(focus_pid) if focus_pid not in (None, "") else None
        except (TypeError, ValueError):
            normalized_pid = None
        missing_critical = [
            str(item).strip()
            for item in focus_session.get("missing_critical") or []
            if str(item).strip()
        ]
        events = focus_session.get("events")
        event_items = [item for item in events or [] if isinstance(item, dict) and self._looks_like_startup_event(item)]
        last_event = event_items[-1] if event_items else {}
        if normalized_pid is None and isinstance(last_event, dict):
            try:
                normalized_pid = int(last_event.get("pid")) if last_event.get("pid") not in (None, "") else None
            except (TypeError, ValueError):
                normalized_pid = None
        file_path = str(last_event.get("file_path") or "").strip() if isinstance(last_event, dict) else ""
        line_no_raw = last_event.get("line_no") if isinstance(last_event, dict) else 0
        try:
            line_no = int(line_no_raw or 0)
        except (TypeError, ValueError):
            line_no = 0
        trailing_excerpt = self._read_same_pid_log_excerpt(
            file_path=file_path,
            pid=normalized_pid,
            line_no=line_no,
            max_lines=24,
            errors_only=False,
        )
        error_excerpt = self._read_same_pid_log_excerpt(
            file_path=file_path,
            pid=normalized_pid,
            line_no=line_no,
            max_lines=12,
            errors_only=True,
        )
        conflicts = self._structured_report_conflicts(payload, focus_session)
        safe_assertions = [
            "只引用结构化报告已命中的生命周期节点。",
            "如果 `missing_critical` 非空，只能描述为“链路未闭环”。",
        ]
        if trailing_excerpt:
            safe_assertions.append("同一 PID 在最后命中事件之后仍有原始日志继续输出。")
        forbidden_assertions = [
            "启动链路完整",
            "已经到达最终首帧展示",
        ]
        if missing_critical:
            forbidden_assertions.append("日志在 preload 后截止")
        evidence = {
            "analysis_kind": analysis_kind,
            "bug_url": str(payload.get("bug_url") or ""),
            "target_time": str(payload.get("target_time") or ""),
            "focus_session_index": payload.get("focus_session_index") or focus_session.get("index"),
            "focus_session_pid": normalized_pid,
            "focus_status": str(focus_session.get("status") or ""),
            "verdict_message": str((payload.get("verdict") or {}).get("message") if isinstance(payload.get("verdict"), dict) else ""),
            "focus_diagnosis": str(focus_session.get("diagnosis") or ""),
            "report_conflicts": conflicts,
            "missing_critical": missing_critical,
            "last_matched_event": last_event if isinstance(last_event, dict) else {},
            "same_pid_trailing_log_excerpt": trailing_excerpt,
            "same_pid_error_excerpt": error_excerpt,
            "safe_assertions": safe_assertions,
            "forbidden_assertions": forbidden_assertions,
        }
        evidence_json_path = output_dir / "bug_summary_evidence.json"
        evidence_md_path = output_dir / "bug_summary_evidence.md"
        evidence_json_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        evidence_md_path.write_text(self._render_bug_summary_evidence_markdown(evidence), encoding="utf-8")
        return evidence_md_path

    def _append_bug_summary_evidence_metadata(self, metadata_path: Path, evidence_path: Path | None) -> None:
        if evidence_path is None:
            return
        json_path = evidence_path.with_suffix(".json")
        try:
            original = metadata_path.read_text(encoding="utf-8")
        except OSError:
            return
        lines = [
            "",
            "## 结构化证据包",
            "",
            f"- 结构化证据 Markdown: `{evidence_path}`",
            f"- 结构化证据 JSON: `{json_path}`",
        ]
        metadata_path.write_text(original.rstrip() + "\n" + "\n".join(lines).rstrip() + "\n", encoding="utf-8")

    def _build_or_refresh_bug_prompt_snapshot(
        self,
        *,
        details: dict[str, object],
        followup_text: str,
        request_text: str = "",
        output_dir: Path | None = None,
        metadata_path: Path | None = None,
        plans_override: list["BugAnalysisPlan"] | None = None,
    ) -> "prompt_snapshots.BugPromptSnapshot":
        snapshot_path = self._bug_prompt_snapshot_path(output_dir) if output_dir is not None else None
        existing = self._read_bug_prompt_snapshot(snapshot_path) if snapshot_path is not None and snapshot_path.exists() else None
        current_kind = self._bug_prompt_snapshot_analysis_kind(details=details, plans_override=plans_override)
        stable_fact_map: dict[str, str] = {}
        if existing is not None and existing.analysis_kind == current_kind:
            stable_fact_map = {item.label: item.value for item in existing.stable_facts if item.label and item.value}
        bug_url = str(details.get("bug_url") or "").strip() or self._extract_bug_url_from_text(request_text)
        target_time = str(details.get("target_time") or details.get("fault_time") or "").strip()
        report_version = str(details.get("report_version") or "").strip()
        prepared_input = str(details.get("prepared_log_input") or "").strip()
        selected_input = str(details.get("selected_log_input") or "").strip()
        signal_code = ""
        if plans_override:
            for plan in plans_override:
                if str(getattr(plan, "kind", "") or "").strip() != "signal":
                    continue
                signal_code = str(getattr(plan, "signal_code", "") or "").strip()
                if signal_code:
                    break
        if not signal_code and current_kind == "signal":
            signal_code = self._extract_signal_code_for_reanalysis(followup_text)
        for label, value in (
            ("bug_url", bug_url),
            ("analysis_kind", current_kind),
            ("target_time", target_time),
            ("report_version", report_version),
            ("prepared_log_input", prepared_input),
            ("selected_log_input", selected_input),
            ("signal_code", signal_code),
        ):
            if value:
                stable_fact_map[label] = value
        stable_facts = [
            prompt_snapshots.SnapshotFact(label=label, value=stable_fact_map[label])
            for label in (
                "bug_url",
                "analysis_kind",
                "target_time",
                "report_version",
                "prepared_log_input",
                "selected_log_input",
                "signal_code",
            )
            if stable_fact_map.get(label)
        ]
        evidence_refs: list[prompt_snapshots.SnapshotEvidence] = []
        if metadata_path is not None:
            evidence_refs.append(
                prompt_snapshots.SnapshotEvidence(title="Bug Follow-up Metadata", path=str(metadata_path), locator="")
            )
            for item in self._bug_summary_referenced_context_files(metadata_path)[:4]:
                title = str(item.get("title") or "").strip()
                ref_path = str(item.get("path") or "").strip()
                if title and ref_path:
                    evidence_refs.append(prompt_snapshots.SnapshotEvidence(title=title, path=ref_path, locator=""))
        elif existing is not None and existing.analysis_kind == current_kind:
            evidence_refs = list(existing.evidence_refs)
        open_questions = list(existing.open_questions) if existing is not None and existing.analysis_kind == current_kind else []
        snapshot = prompt_snapshots.BugPromptSnapshot(
            scope_key=self._bug_prompt_snapshot_scope_key(
                request_text=request_text,
                details=details,
                output_dir=output_dir,
                existing=existing,
            ),
            analysis_kind=current_kind,
            stable_facts=stable_facts,
            evidence_refs=evidence_refs,
            open_questions=open_questions,
        )
        if snapshot_path is not None:
            self._write_bug_prompt_snapshot(snapshot_path, snapshot)
        return snapshot

    def _build_bug_prompt_snapshot_prefix(
        self,
        *,
        request_text: str,
        metadata_path: Path,
        followup_text: str,
        snapshot_details: dict[str, object] | None = None,
        plans_override: list["BugAnalysisPlan"] | None = None,
    ) -> str:
        if not followup_text.strip():
            return ""
        details = self._bug_prompt_snapshot_details_from_metadata(
            request_text=request_text,
            metadata_path=metadata_path,
        )
        if snapshot_details:
            for key, value in snapshot_details.items():
                if value is None:
                    continue
                if isinstance(value, str) and not value.strip():
                    continue
                if isinstance(value, list) and not value:
                    continue
                details[key] = value
        snapshot = self._build_or_refresh_bug_prompt_snapshot(
            details=details,
            followup_text=followup_text,
            request_text=request_text,
            output_dir=metadata_path.parent,
            metadata_path=metadata_path,
            plans_override=plans_override,
        )
        return self._render_bug_prompt_snapshot_prefix(snapshot, followup_text=followup_text)

    def _render_bug_prompt_snapshot_prefix(
        self,
        snapshot: "prompt_snapshots.BugPromptSnapshot",
        *,
        followup_text: str,
    ) -> str:
        return prompt_snapshots.render_bug_snapshot_prefix(snapshot, followup_text=followup_text).rstrip() + "\n\n"

    def _build_omlx_bug_summary_prompt(
        self,
        *,
        request_text: str,
        request_artifact: Path,
        metadata_path: Path,
        followup_text: str = "",
        previous_summary_path: Path | None = None,
        include_context_file_excerpts: bool = False,
        snapshot_details: dict[str, object] | None = None,
        snapshot_plans: list["BugAnalysisPlan"] | None = None,
    ) -> str:
        budget = max(500, int(getattr(self.config.omlx_chat, "max_prompt_chars", 2000) or 2000))
        snapshot_prefix = ""
        if followup_text.strip():
            snapshot_prefix = self._build_bug_prompt_snapshot_prefix(
                request_text=request_text,
                metadata_path=metadata_path,
                followup_text=followup_text,
                snapshot_details=snapshot_details,
                plans_override=snapshot_plans,
            ).rstrip()
        sections = [
            "请基于以下已生成材料给出轻量 bug 总结。不要编造未提供的日志/源码证据。",
            "输出结构：## 结论摘要、## 关键证据、## 最可能原因、## 待确认项、## 建议动作。",
            snapshot_prefix,
            self._omlx_prompt_section("用户请求", request_text, 420),
            self._omlx_prompt_section("本次追问", followup_text, 240),
            self._omlx_prompt_section("请求文件摘录", self._read_text_excerpt(request_artifact, 500), 500),
            self._omlx_prompt_section("元数据摘录", self._read_text_excerpt(metadata_path, 900), 900),
        ]
        if previous_summary_path is not None and (
            not followup_text.strip()
            or self._should_include_previous_summary_for_followup(
                followup_text=followup_text,
                request_artifact=request_artifact,
                metadata_path=metadata_path,
            )
        ):
            sections.append(
                self._omlx_prompt_section("上一轮摘要摘录", self._read_text_excerpt(previous_summary_path, 700), 700)
            )
        for item in self._bug_summary_referenced_context_files(metadata_path)[:4]:
            path = Path(str(item["path"]))
            if include_context_file_excerpts:
                sections.append(
                    self._omlx_prompt_section(
                        str(item["title"]),
                        self._read_bug_summary_context_excerpt(path, 900),
                        900,
                    )
                )
                continue
            sections.append(
                self._omlx_prompt_section(
                    str(item["title"]),
                    f"本地路径: {item['path']}\n轻量模型不能读取本地文件；需要读取该文件时必须切换 Codex/Claude Agent。",
                    500,
                )
            )
        prompt = "\n\n".join(section for section in sections if section.strip()).strip()
        if len(prompt) > budget:
            prompt = prompt[: budget - 1].rstrip() + "…"
        return prompt

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
        target_time = str(payload.get("target_time") or "").strip()
        if target_time:
            lines.append(f"- target_time: {target_time}")
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

    def _run_bug_summary_pydantic_ai(
        self,
        *,
        request_text: str,
        request_artifact: Path,
        metadata_path: Path,
        output_path: Path,
        followup_text: str = "",
        previous_summary_path: Path | None = None,
        progress_callback: Callable[[dict[str, object]], None] | None,
        snapshot_details: dict[str, object] | None = None,
        snapshot_plans: list["BugAnalysisPlan"] | None = None,
    ) -> dict[str, object]:
        """Run bug summary via pydantic-ai agent runtime (structured output + tools).

        Unlike direct_api which inlines all files, this uses tools so the agent
        can selectively read analysis artifacts. Falls back to direct_api on failure.
        """
        from ..agent_runtime import AgentRuntime, _check_pydantic_ai

        if not _check_pydantic_ai():
            return {
                "message": "",
                "command": None,
                "error": "pydantic_ai_not_available",
                "provider": "pydantic_ai",
                "session_id": "",
                "resumed": False,
                "duration_seconds": 0.0,
                "usage": {},
                "usage_scope": "",
            }

        ai_opts = self.config.ai_provider
        started = time.monotonic()

        # Build a tool-oriented prompt: list file paths instead of inlining content.
        prompt = self._build_bug_summary_prompt_for_pydantic_ai(
            request_text=request_text,
            request_artifact=request_artifact,
            metadata_path=metadata_path,
            followup_text=followup_text,
            previous_summary_path=previous_summary_path,
            snapshot_details=snapshot_details,
            snapshot_plans=snapshot_plans,
        )
        if not prompt.strip():
            return {
                "message": "",
                "command": None,
                "error": "pydantic_ai_prompt_empty",
                "provider": "pydantic_ai",
                "session_id": "",
                "resumed": False,
                "duration_seconds": time.monotonic() - started,
                "usage": {},
                "usage_scope": "",
            }

        self._emit_progress(
            progress_callback,
            stage="bug_agent_summary_pydantic_ai",
            message="pydantic-ai Agent 整理最终结论（结构化输出 + 工具增强）",
            provider="pydantic_ai",
            model=ai_opts.primary_model,
        )

        system_prompt = (
            "你是一个通过飞书触发的 bug 分析总结 agent。\n"
            "你可以使用 read_file / grep / search_large_log / bash 工具读取分析产物文件。\n"
            "只读分析，不修改文件，不执行写入命令。\n"
            "遇到 *.alog.log、*.xlog.log 或大日志时，必须先用 search_large_log 或 bash rg 搜索关键词定位行号，再用 read_file 精读上下文。\n"
            "不要用连续 read_file 分页扫描大日志。\n"
            "必须完整响应用户原始请求中的所有诉求，输出中文 Markdown，结论先行。\n"
            "所有分析数据的路径已列在用户消息中，请根据文件类型选择合适工具读取。"
        )

        workspace = metadata_path.parent if metadata_path.exists() else Path.cwd()
        runtime = AgentRuntime(ai_opts, workspace=workspace, max_retries=1)
        result = runtime.run(
            system_prompt=system_prompt,
            user_prompt=prompt,
            tools_enabled=True,
            strict_tools=True,
        )

        duration = time.monotonic() - started
        if result.ok and (result.runtime_path != "pydantic_ai_agent" or result.tool_calls == 0):
            logger.warning(
                "pydantic-ai summary returned without tool-backed runtime "
                "(runtime_path=%s, tool_calls=%d); rejecting so caller can fallback",
                result.runtime_path,
                result.tool_calls,
            )
            return {
                "message": "",
                "command": None,
                "error": "pydantic_ai_summary_requires_tools",
                "provider": "pydantic_ai",
                "session_id": "",
                "resumed": False,
                "duration_seconds": duration,
                "usage": result.usage,
                "usage_scope": "",
                "runtime_path": result.runtime_path,
                "tool_calls": result.tool_calls,
                "tool_trace": result.tool_trace,
            }
        if result.ok and result.markdown.strip():
            message = result.markdown.strip()
            # Write to output_path for downstream consumers
            try:
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(message, encoding="utf-8")
            except OSError:
                pass
            self._emit_progress(
                progress_callback,
                stage="bug_agent_summary_completed",
                message=f"pydantic-ai Agent 已整理最终结论（{duration:.1f}s）",
                provider="pydantic_ai",
                model=result.model,
                duration_seconds=round(duration, 1),
                tool_calls=result.tool_calls,
                tool_trace=result.tool_trace[:20],
            )
            return {
                "message": message,
                "command": None,
                "error": "",
                "provider": "pydantic_ai",
                "session_id": "",
                "resumed": False,
                "duration_seconds": duration,
                "usage": result.usage,
                "usage_scope": "summary",
                "runtime_path": result.runtime_path,
                "tool_calls": result.tool_calls,
                "tool_trace": result.tool_trace,
            }

        logger.warning(
            "pydantic-ai summary failed (%.1fs, error=%s), will fallback",
            duration, result.error_code or result.error,
        )
        return {
            "message": "",
            "command": None,
            "error": result.error or "pydantic_ai_summary_failed",
            "provider": "pydantic_ai",
            "session_id": "",
            "resumed": False,
            "duration_seconds": duration,
            "usage": result.usage,
            "usage_scope": "",
        }

    def _build_bug_summary_prompt_for_pydantic_ai(
        self,
        *,
        request_text: str,
        request_artifact: Path,
        metadata_path: Path,
        followup_text: str = "",
        previous_summary_path: Path | None = None,
        snapshot_details: dict[str, object] | None = None,
        snapshot_plans: list["BugAnalysisPlan"] | None = None,
    ) -> str:
        """Build a tool-oriented summary prompt.

        Unlike _build_bug_agent_summary_prompt_for_api which inlines file content,
        this lists file paths so the pydantic-ai agent can use read_file/search tools
        tools to read them selectively.
        """
        prompt = "请基于以下分析材料完成 bug 会话的最终回答。\n"
        prompt += "材料文件路径已列出，请按文件类型选择工具：报告/Markdown 用 read_file，大日志先用 search_large_log 或 bash rg 定位行号。\n\n"

        snapshot_prefix = ""
        if followup_text.strip():
            prompt += (
                "这是续聊/追问。要求：\n"
                "1. 直接回答新问题，延续上一轮分析。\n"
                "2. 使用工具读取已有材料。\n"
                "3. 只读分析，不修改文件。\n"
                "4. 输出中文 Markdown，结论先行。\n\n"
            )
            snapshot_prefix = self._build_bug_prompt_snapshot_prefix(
                request_text=request_text,
                metadata_path=metadata_path,
                followup_text=followup_text,
                snapshot_details=snapshot_details,
                plans_override=snapshot_plans,
            )
        else:
            prompt += (
                "这是全新 bug 分析请求。要求：\n"
                "1. 完整覆盖用户请求里的所有诉求。\n"
                "2. 只读分析，不修改文件。\n"
                "3. 输出中文 Markdown，结论先行。\n\n"
            )

        prompt += (
            "输出结构：\n"
            "## 结论摘要\n- 3-5 条最重要结论，标明置信边界。\n"
            "## 关键证据\n- 每条带文件、行号、时间。\n"
            "## 最可能原因\n- 按可能性排序。\n"
            "## 待确认项\n- 真实证据缺口。\n"
            "## 建议动作\n- 下一轮可执行动作。\n\n"
        )

        if snapshot_prefix:
            prompt += snapshot_prefix

        prompt += f"### 用户原始请求\n{request_text}\n\n"
        if followup_text.strip():
            prompt += f"### 本次追问\n{followup_text.strip()}\n\n"

        # List file paths for the agent to read
        prompt += "### 可用材料文件\n"
        file_list: list[str] = []
        if request_artifact.exists():
            file_list.append(f"- Bug 请求文件: `{request_artifact}`")
        if metadata_path.exists():
            file_list.append(f"- Bug 元数据: `{metadata_path}`")
        if previous_summary_path and previous_summary_path.exists():
            file_list.append(f"- 上一轮总结: `{previous_summary_path}`")
        context_items = self._bug_summary_context_items(metadata_path, include_html_reports=False)
        for item in context_items:
            path = Path(str(item["path"]))
            title = str(item["title"])
            if path.exists():
                file_list.append(f"- {title}: `{path}`")

        if file_list:
            prompt += "\n".join(file_list) + "\n\n"
            prompt += (
                "请读取上述材料后整理回答。普通报告用 read_file；日志文件不要逐页扫描，"
                "先用 search_large_log(pattern, path=...) 或 bash(\"rg -n ...\") 搜索定位。\n\n"
            )
        else:
            prompt += "（无可用材料文件）\n"

        decoded_logs = self._bug_summary_decoded_log_paths(context_items)
        if decoded_logs:
            prompt += "### 可搜索解码日志\n"
            prompt += "\n".join(f"- `{path}`" for path in decoded_logs) + "\n\n"

        prompt += (
            "### 大日志搜索规则\n"
            "- 对 `*.alog.log`、`*.xlog.log` 或大于 1MB 的日志，先调用 `search_large_log(pattern, path=...)` 或 `bash(\"rg -n ...\")`。\n"
            "- 禁止用连续 `read_file` 分页扫描大日志；只有定位到行号后才用 `read_file(path, start_line, end_line)` 精读。\n"
            "- 普通 JSON/Markdown 报告可直接用 `read_file`。\n\n"
            "### 推荐启动排查关键词\n"
            "- `kill self for unity start dead`\n"
            "- `startCheck timeout`\n"
            "- `onUnityStartDeadTraceDump`\n"
            "- `onUnityHeartbeatSignal`\n"
            "- `X3DCB-DROP`\n"
            "- `没有注册回调函数`\n"
        )

        return prompt

    def _run_bug_agent_summary_via_api(
        self,
        *,
        request_text: str,
        request_artifact: Path,
        metadata_path: Path,
        output_path: Path,
        followup_text: str = "",
        previous_summary_path: Path | None = None,
        progress_callback: Callable[[dict[str, object]], None] | None,
        snapshot_details: dict[str, object] | None = None,
        snapshot_plans: list["BugAnalysisPlan"] | None = None,
    ) -> dict[str, object]:
        """Run bug summary via direct LLM API (fast path, no subprocess)."""
        from ..llm_client import LLMClient, LLMClientError

        ai_opts = self.config.ai_provider
        provider_tag = "direct_api"
        started = time.monotonic()
        client = LLMClient(ai_opts)
        if not client.is_available():
            return {
                "message": "",
                "command": None,
                "error": "direct_api_not_configured",
                "provider": provider_tag,
                "session_id": "",
                "resumed": False,
                "duration_seconds": time.monotonic() - started,
                "usage": {},
                "usage_scope": "",
            }
        prompt = self._build_bug_agent_summary_prompt_for_api(
            request_text=request_text,
            request_artifact=request_artifact,
            metadata_path=metadata_path,
            followup_text=followup_text,
            previous_summary_path=previous_summary_path,
            snapshot_details=snapshot_details,
            snapshot_plans=snapshot_plans,
        )
        if not prompt.strip():
            return {
                "message": "",
                "command": None,
                "error": "direct_api_prompt_empty",
                "provider": provider_tag,
                "session_id": "",
                "resumed": False,
                "duration_seconds": time.monotonic() - started,
                "usage": {},
                "usage_scope": "",
            }
        embedded_files = self._direct_api_bug_summary_embedded_files(
            request_artifact=request_artifact,
            metadata_path=metadata_path,
            followup_text=followup_text,
            previous_summary_path=previous_summary_path,
        )
        prompt_file, context_file = self._write_bug_agent_summary_audit(
            {
                "provider": provider_tag,
                "session_id": "",
                "resumed": False,
                "prompt": prompt,
                "embedded_files": embedded_files,
            },
            output_path,
        )
        self._emit_progress(
            progress_callback,
            stage="bug_agent_summary_direct_api",
            message="直接调用 API 整理最终结论（快速通道）",
            provider=provider_tag,
            model=ai_opts.primary_model,
        )
        system_prompt = (
            "你是一个通过飞书触发的 bug 分析总结 agent。"
            "只读分析，不修改文件，不执行写入命令。"
            "必须完整响应用户原始请求中的所有诉求，输出中文 Markdown，结论先行。"
            "所有分析数据已内嵌在用户消息中，直接基于这些数据分析即可。"
        )
        try:
            response = client.generate_summary(
                system_prompt=system_prompt,
                user_prompt=prompt,
            )
        except LLMClientError as exc:
            logger.warning("Direct API bug summary failed: %s", exc)
            return {
                "message": "",
                "command": None,
                "error": f"direct_api_error: {exc}",
                "provider": provider_tag,
                "session_id": "",
                "resumed": False,
                "duration_seconds": time.monotonic() - started,
                "usage": {},
                "usage_scope": "",
                "prompt_file": str(prompt_file) if prompt_file is not None else "",
                "context_file": str(context_file) if context_file is not None else "",
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("Direct API bug summary unexpected error: %s", exc)
            return {
                "message": "",
                "command": None,
                "error": f"direct_api_unexpected: {exc}",
                "provider": provider_tag,
                "session_id": "",
                "resumed": False,
                "duration_seconds": time.monotonic() - started,
                "usage": {},
                "usage_scope": "",
                "prompt_file": str(prompt_file) if prompt_file is not None else "",
                "context_file": str(context_file) if context_file is not None else "",
            }
        message = (response.content or "").strip()
        if not message:
            return {
                "message": "",
                "command": None,
                "error": "direct_api_empty_response",
                "provider": provider_tag,
                "session_id": "",
                "resumed": False,
                "duration_seconds": time.monotonic() - started,
                "usage": {},
                "usage_scope": "",
                "prompt_file": str(prompt_file) if prompt_file is not None else "",
                "context_file": str(context_file) if context_file is not None else "",
            }
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(message, encoding="utf-8")
        except OSError:
            pass
        duration = time.monotonic() - started
        self._emit_progress(
            progress_callback,
            stage="bug_agent_summary_completed",
            message=f"直接 API 已整理最终结论（{duration:.1f}s）",
            provider=provider_tag,
            model=response.model or ai_opts.primary_model,
            output_path=str(output_path),
        )
        return {
            "message": message,
            "command": None,
            "error": "",
            "provider": provider_tag,
            "model": response.model or ai_opts.primary_model,
            "session_id": "",
            "resumed": False,
            "duration_seconds": duration,
            "usage": response.usage,
            "usage_scope": "direct_api",
            "prompt_file": str(prompt_file) if prompt_file is not None else "",
            "context_file": str(context_file) if context_file is not None else "",
        }

    def _build_bug_agent_summary_prompt_for_api(
        self,
        *,
        request_text: str,
        request_artifact: Path,
        metadata_path: Path,
        followup_text: str = "",
        previous_summary_path: Path | None = None,
        snapshot_details: dict[str, object] | None = None,
        snapshot_plans: list["BugAnalysisPlan"] | None = None,
    ) -> str:
        """Build summary prompt with inlined file contents for direct API calls.

        Unlike the subprocess path where codex/claude can read files via tools,
        the direct API path must embed all relevant context inline.
        """
        max_file_chars = 12000
        compaction_profile = self._direct_api_compaction_profile(
            metadata_path=metadata_path,
            snapshot_details=snapshot_details,
        )
        prompt = "请基于以下已内嵌的分析材料完成同一个 bug 会话的最终回答。\n"
        prompt += "注意：所有相关文件内容已内嵌在本消息中，无需读取本地文件。\n\n要求：\n"
        snapshot_prefix = ""
        if followup_text.strip():
            prompt += (
                "1. 这是一条续聊/追问，必须直接回答这次新问题，并延续上一轮分析。\n"
                "2. 优先复用已内嵌的元数据和报告材料，不要要求用户重新上传日志。\n"
                "3. 只读分析，不修改任何文件。\n"
                "4. 输出中文 Markdown，结论先行，再给出证据。\n"
                "5. 如果现有材料仍不足以覆盖某个诉求，要明确指出缺口，但先回答已经能确认的部分。\n"
                "6. 对启动/生命周期类报告，若结构化 JSON 显示 `status != complete` 或仍有 `missing_critical`，即使 HTML/verdict 文案更乐观，也按“链路未闭环”处理，并明确指出报告内部冲突。\n"
                "7. “未命中关键节点”不等于“日志在这里截止”；除非材料明确显示文件结束或时间窗截断，否则不要把缺节点改写成日志截止。\n\n"
            )
            snapshot_prefix = self._build_bug_prompt_snapshot_prefix(
                request_text=request_text,
                metadata_path=metadata_path,
                followup_text=followup_text,
                snapshot_details=snapshot_details,
                plans_override=snapshot_plans,
            )
        else:
            prompt += (
                "1. 本次是全新 bug 分析请求，不是续聊/修正；不要虚构\u201c上一轮分析\u201d\u201c本次修正\u201d\u201c延续上一轮\u201d这类诉求或标题。\n"
                "2. 必须完整覆盖用户原始请求里的所有诉求，不要只回答其中一部分。\n"
                "3. 只读分析，不修改任何文件。\n"
                "4. 输出中文 Markdown，结论先行；若有多个诉求，按诉求分组说明结论和证据；若只有一个诉求，只在开头说明一次，"
                "不要在每条结论或证据前重复写相同的诉求。诉求标题只能来自用户原始请求，不要自行添加不存在的诉求。\n"
                "5. 如果材料无法覆盖用户某个诉求，要明确指出缺口。\n"
                "6. 已内嵌元数据中的\u201c本轮脚本初步摘要\u201d只是当前自动脚本输出，不要把它写成\u201c上一轮结论\u201d；"
                "只有显式提供 followup/previous summary 时，才能讨论修正上一轮结论。\n"
                "7. 如果元数据或报告里已经明确给出故障时间对应的主会话 / 主 PID / focus session，"
                "请优先围绕该主会话分析，不要展开无关会话；只有在需要证明时间不匹配时才提及其他会话。\n\n"
            )
        prompt += (
            "统一输出结构：请按以下中文二级标题组织最终回答，并只填入本次 bug 自身的证据，不套用示例业务词。\n"
            "## 结论摘要\n"
            "- 先给 3 到 5 条最重要结论，必须标明置信边界。\n"
            "## 关键证据\n"
            "- 每条证据尽量带文件、行号、时间、进程/package 或源码位置。\n"
            "## 最可能原因\n"
            "- 按可能性排序，说明支持证据和缺口；证据不足时明确不要强行定根因。\n"
            "## 待确认项\n"
            "- 只列真实证据缺口，例如精确时间、日志片段、运行状态、源码链路缺口。\n"
            "## 建议动作\n"
            "- 给出下一轮可执行动作，例如补日志、重跑某个 skill、沿某个源码或日志点继续查。\n\n"
        )
        if snapshot_prefix:
            prompt += snapshot_prefix
        prompt += f"### 用户原始请求\n{request_text}\n\n"
        if followup_text.strip():
            prompt += f"### 本次追问/修正\n{followup_text.strip()}\n\n"
        if previous_summary_path is not None and (
            not followup_text.strip()
            or self._should_include_previous_summary_for_followup(
                followup_text=followup_text,
                request_artifact=request_artifact,
                metadata_path=metadata_path,
            )
        ):
            prev_text = self._read_text_excerpt(previous_summary_path, max_file_chars)
            if prev_text:
                prompt += f"### 上一轮 Agent 总结\n{prev_text}\n\n"
        request_text_content = self._read_text_excerpt(request_artifact, max_file_chars)
        if request_text_content:
            prompt += f"### Bug Agent Request 文件内容\n{request_text_content}\n\n"
        metadata_text = self._read_text_excerpt(metadata_path, max_file_chars)
        if metadata_text:
            prompt += f"### Bug Metadata 文件内容\n{metadata_text}\n\n"
        for item in self._bug_summary_context_items(metadata_path, include_html_reports=False):
            path = Path(str(item["path"]))
            title = str(item["title"])
            guardrails = self._render_structured_report_guardrails(path)
            if guardrails:
                prompt += f"{guardrails}\n\n"
            content = self._direct_api_bug_summary_context_excerpt(
                title=title,
                path=path,
                max_chars=max_file_chars,
                compaction_profile=compaction_profile,
            )
            if content:
                prompt += f"### {title}\n来源: {path}\n{content}\n\n"
        return prompt
