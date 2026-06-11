from __future__ import annotations

from ._shared import *  # noqa: F401,F403


class _SummaryContextMixin:
    """总结上下文与守护：追问总结判定、上下文条目与结构化报告 guardrails（与 BugPromptMixin 共享 self 状态）。"""

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
