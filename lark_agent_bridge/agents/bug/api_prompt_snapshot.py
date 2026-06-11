from __future__ import annotations

from ._shared import *  # noqa: F401,F403


class _ApiPromptSnapshotMixin:
    """direct-api 侧 Bug prompt 快照：快照构建/刷新、前缀渲染与 OMLX 总结提示词（与 DirectApiMixin 共享 self 状态）。"""

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
