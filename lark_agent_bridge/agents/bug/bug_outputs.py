from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from . import _shared


class _BugOutputsMixin:
    """Bug 元数据与产出渲染：描述/评论/参考时间提取、输出文件与摘要构建（与 ArchiveExtractMixin 共享 self 状态）。"""

    def _bug_description(self, fetched: dict[str, object]) -> str:
        fields = fetched.get("fields", {})
        fallback = fetched.get("description", "")
        if not isinstance(fields, dict):
            return fallback if isinstance(fallback, str) else ""
        value = fields.get("field_204366", "")
        if isinstance(value, str) and value:
            return value
        return fallback if isinstance(fallback, str) else ""
    def _bug_comment_text(self, fetched: dict[str, object]) -> str:
        comments: list[str] = []

        def collect(value: object) -> None:
            if isinstance(value, str):
                text = value.strip()
                if text:
                    comments.append(text)
                return
            if isinstance(value, list):
                for item in value:
                    collect(item)
                return
            if isinstance(value, dict):
                for key in ("text", "content", "body", "description", "comment", "message"):
                    if key in value:
                        collect(value.get(key))

        for key in ("comments", "comment", "comment_list", "discussions", "notes"):
            collect(fetched.get(key))
        return "\n".join(comments)
    def _bug_stack_payload_text(self, fetched: dict[str, object], description: str) -> str:
        return "\n".join(
            part for part in (description.strip(), self._bug_comment_text(fetched).strip()) if part
        )
    def _bug_reference_time(self, fetched: dict[str, object], full_item: dict[str, object]) -> str:
        for value in (
            fetched.get("create_time"),
            fetched.get("created_at"),
            fetched.get("createTime"),
            self._nested_string(full_item, "work_item_attribute", "create_time"),
            self._nested_string(full_item, "work_item_attribute", "created_at"),
            self._nested_string(full_item, "data", "work_item_attribute", "create_time"),
            self._nested_string(full_item, "data", "work_item_attribute", "created_at"),
            full_item.get("create_time"),
            full_item.get("created_at"),
        ):
            text = str(value or "").strip()
            if text:
                return text
        return ""
    def _nested_string(self, payload: object, *keys: str) -> str:
        current = payload
        for key in keys:
            if not isinstance(current, dict):
                return ""
            current = current.get(key)
        return str(current or "").strip()
    def _build_bug_outputs(
        self,
        *,
        plans: list["BugAnalysisPlan"],
        work_item_id: str,
        fetched: dict[str, object],
        full_item: dict[str, object],
        option_map: dict[str, str],
        request_text: str,
        prompt_text: str,
        selected_input: Path | None,
        report_jsons: dict[str, Path | None],
        download: dict[str, object],
        html_paths: list[Path],
        classification_skill: str = "",
        classification_source: str = "",
        classification_reason: str = "",
        classification_provider: str = "",
        fault_time: str | None = None,
        fault_time_note: str | None = None,
        log_coverage: LogCoverage | None = None,
    ) -> tuple[str, str]:
        title = str(fetched.get("title", ""))
        description = self._bug_description(fetched)
        status = str(fetched.get("status", ""))
        create_time = self._bug_reference_time(fetched, full_item)
        create_by = str(fetched.get("create_by", ""))
        owner = self._extract_owner(full_item)
        bug_source = self._map_option(option_map, fetched.get("fields", {}), "field_24095d")
        found_version = self._string_field(fetched.get("fields", {}), "field_010122")
        probability = self._map_option(option_map, fetched.get("fields", {}), "field_45dc84")
        if fault_time is None or fault_time_note is None:
            extracted_fault_time, extracted_fault_time_note = self._extract_fault_time(title, description)
            fault_time = extracted_fault_time if fault_time is None else fault_time
            fault_time_note = extracted_fault_time_note if fault_time_note is None else fault_time_note
        summary_blocks: list[str] = []
        for plan in plans:
            html_path = next((path for path in html_paths if path.name == self._report_name(plan.kind, "html")), None)
            if html_path is None:
                continue
            summary_blocks.append(
                self._build_summary_from_report(
                    plan=plan,
                    report_json=report_jsons.get(plan.kind),
                    prompt_text=prompt_text,
                    fault_time=fault_time,
                    html_path=html_path,
                    selected_input=selected_input,
                )
            )
        summary = "\n\n".join(summary_blocks)
        attachment_lines = self._render_attachment_lines(fetched.get("attachments", []), download)
        analysis_lines = "\n".join(
            f"  - `{self._analysis_label(plan.kind)}` -> `{self._report_name(plan.kind, 'html')}`"
            for plan in plans
        )
        report_artifact_lines: list[str] = []
        for plan in plans:
            html_path = next((path for path in html_paths if path.name == self._report_name(plan.kind, "html")), None)
            json_path = report_jsons.get(plan.kind)
            if html_path is not None:
                report_artifact_lines.append(f"  - HTML `{self._analysis_label(plan.kind)}`: `{html_path}`")
            if json_path is not None:
                report_artifact_lines.append(f"  - JSON `{self._analysis_label(plan.kind)}`: `{json_path}`")
            analysis_md_name = self._skill_agent_analysis_markdown_name(plan.kind)
            analysis_md_path = (html_path.parent / f"{plan.kind}_analysis" / analysis_md_name) if html_path else None
            if analysis_md_path and analysis_md_path.exists():
                report_artifact_lines.append(f"  - Analysis `{self._analysis_label(plan.kind)}`: `{analysis_md_path}`")
        report_artifacts = "\n".join(report_artifact_lines) if report_artifact_lines else "  - 无"
        metadata = (
            "# Bug Metadata\n\n"
            f"- Bug ID: `{work_item_id}`\n"
            f"- 标题: `{title}`\n"
            f"- 当前状态: `{status or '未返回 / 未设置'}`\n"
            f"- 创建时间: `{create_time or '未返回 / 未设置'}`\n"
            f"- 创建人: `{create_by or '未返回 / 未设置'}`\n"
            f"- 当前负责人: `{owner}`\n"
            f"- 缺陷来源: `{bug_source}`\n"
            f"- 发现版本: `{found_version}`\n"
            f"- 发生概率: `{probability}`\n"
            f"- 分析类型:\n{analysis_lines}\n"
            f"- 命中 Skill: `{classification_skill or self._skill_name_for_kind(plans[0].kind if plans else 'general')}`\n"
            f"- 分类来源: `{classification_source or 'manual_fallback'}`\n"
            f"- 分类 Agent: `{classification_provider or '无'}`\n"
            f"- 分类理由: `{classification_reason or '未记录'}`\n"
            f"- Skill 规范:\n{self._render_skill_context_lines(classification_skill)}"
            f"- 信号代码: `{', '.join(plan.signal_code for plan in plans if plan.signal_code) or '无'}`\n"
            f"- 故障时间: `{fault_time or '未识别'}`\n"
            f"  说明: {fault_time_note}\n"
            f"- 日志覆盖范围: `{self._format_log_coverage_for_metadata(log_coverage)}`\n"
            "- 用户原始请求:\n\n```text\n"
            f"{request_text}\n"
            "```\n"
            f"- 分析请求: `{prompt_text}`\n"
            f"- 选中日志输入: `{selected_input or '无，可静态分析'}`\n"
            f"- 报告产物:\n{report_artifacts}\n"
            f"- 附件:\n{attachment_lines}\n"
            "- 缺陷描述:\n\n```text\n"
            f"{description.strip() or '(无描述)'}\n"
            "```\n\n"
            "- 本轮脚本初步摘要（仅代表本轮自动脚本输出，不代表上一轮分析结论；若与源码证据冲突，以源码与原始输入为准）:\n\n"
            f"{summary}\n"
        )
        return metadata, summary
    def _skill_context_paths(self, skill_name: str) -> list[Path]:
        normalized = skill_name.strip()
        if not normalized or normalized == "general":
            return []
        try:
            record = self.skill_manager.get_skill(normalized, include_content=False)
        except Exception:
            return []
        paths: list[Path] = []
        skill_md = Path(record.skill_md_path).expanduser() if record.skill_md_path else Path()
        if skill_md and skill_md.exists() and skill_md.is_file():
            paths.append(skill_md)
            references_dir = skill_md.parent / "references"
            if references_dir.exists():
                for path in sorted(references_dir.glob("*.md"))[:4]:
                    if path.is_file():
                        paths.append(path)
        return paths
    def _render_skill_context_lines(self, skill_name: str) -> str:
        paths = self._skill_context_paths(skill_name)
        if not paths:
            return "  - 无\n"
        return "".join(f"  - `{path}`\n" for path in paths)
    def _extract_owner(self, full_item: dict[str, object]) -> str:
        current_nodes = full_item.get("work_item_current_node", [])
        if isinstance(current_nodes, list) and current_nodes:
            owners = current_nodes[0].get("owners", []) if isinstance(current_nodes[0], dict) else []
            if isinstance(owners, list) and owners:
                owner = owners[0]
                if isinstance(owner, dict):
                    return str(owner.get("name", "未返回 / 未设置"))
        return "未返回 / 未设置"
    def _map_option(self, option_map: dict[str, str], fields: object, key: str) -> str:
        if not isinstance(fields, dict):
            return "未返回 / 未设置"
        raw = fields.get(key, "")
        if isinstance(raw, str) and raw:
            return option_map.get(raw, raw)
        return "未返回 / 未设置"
    def _string_field(self, fields: object, key: str) -> str:
        if not isinstance(fields, dict):
            return "未返回 / 未设置"
        value = fields.get(key, "")
        if isinstance(value, str) and value:
            return value
        return "未返回 / 未设置"
    def _build_direct_analysis_summary(
        self,
        plans: list["BugAnalysisPlan"],
        prompt_text: str,
        html_paths: list[Path],
    ) -> str:
        lines = ["直传文件分析完成", f"描述: {prompt_text}", "报告:"]
        for plan, html_path in zip(plans, html_paths):
            lines.append(f"- {self._analysis_label(plan.kind)}: {html_path}")
        return "\n".join(lines)
    def _render_attachment_lines(self, attachments: object, download: dict[str, object]) -> str:
        downloaded = set(str(name) for name in download.get("downloaded", []) if isinstance(name, str))
        skipped = set(str(name) for name in download.get("skipped", []) if isinstance(name, str))
        errors = set(str(name) for name in download.get("errors", []) if isinstance(name, str))
        error_detail_map = {
            str(item.get("name") or ""): str(item.get("reason") or "")
            for item in download.get("error_details", [])
            if isinstance(item, dict)
        }
        lines: list[str] = []
        if isinstance(attachments, list):
            for item in attachments:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name", ""))
                size = str(item.get("size", ""))
                status = (
                    "已下载"
                    if name in downloaded
                    else "已复用缓存日志"
                    if download.get("reused")
                    else "已跳过"
                    if name in skipped
                    else "下载失败"
                    if name in errors
                    else "未下载"
                )
                reason = error_detail_map.get(name, "")
                suffix = f" ({reason})" if reason and status == "下载失败" else ""
                lines.append(f"  - `{name}` (`{size}`) - {status}{suffix}")
        return "\n".join(lines) if lines else "  - (无附件)"
