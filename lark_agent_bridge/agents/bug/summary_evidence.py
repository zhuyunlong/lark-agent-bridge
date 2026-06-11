from __future__ import annotations

from ._shared import *  # noqa: F401,F403


class _SummaryEvidenceMixin:
    """Bug 总结证据：结构化报告冲突检测、同 PID 日志摘录、证据落盘与元数据登记（与 DirectApiMixin 共享 self 状态）。"""

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
