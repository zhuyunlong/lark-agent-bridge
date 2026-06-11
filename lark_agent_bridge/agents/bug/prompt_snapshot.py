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


class _PromptSnapshotMixin:
    """Bug Prompt 快照：读写、字段提取与 details 构建（与 BugPromptMixin 共享 self 状态）。"""

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
