"""Generic Feishu Project requirement source-analysis orchestration."""

from __future__ import annotations

import json
import re
import subprocess
import time
from html import unescape
from pathlib import Path
from typing import Callable

from .health import run_tracked_process
from .models import (
    BridgeConfig,
    LarkEvent,
    RequirementAnalysisRequest,
    RequirementFact,
    RequirementSourceComparison,
    RequirementWorkItemRef,
    RequirementWorkItemSnapshot,
    SourceAnalysisRequest,
    TaskResult,
    create_job_context,
)
from .reporting.requirement_report_html import render_requirement_analysis_report
from .source_analysis import RepositorySourceAnalysisRunner

CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
JSON_BLOCK_RE = re.compile(r"```json\s*(?P<body>\{.*?\})\s*```", re.S | re.I)
MARKDOWN_FALLBACK_WARNING = "source_comparison_json_missing_markdown_fallback"
VALID_VERDICTS = {
    "implemented",
    "partially_implemented",
    "not_found_in_current_repo",
    "insufficient_evidence",
    "blocked",
}
VALID_IMPACTS = {"none", "low", "medium", "high", "unknown"}


class MeegleWorkItemClient:
    def __init__(
        self,
        *,
        command_runner: CommandRunner | None = None,
        timeout_seconds: int = 120,
        fetch_comments: bool = True,
    ) -> None:
        self.command_runner = command_runner or self._default_runner
        self.timeout_seconds = timeout_seconds
        self.fetch_comments = fetch_comments

    def fetch(
        self,
        ref: RequirementWorkItemRef,
        *,
        job_dir: Path,
        max_fact_count: int = 30,
    ) -> RequirementWorkItemSnapshot:
        raw = self._run_json(
            [
                "meegle",
                "workitem",
                "get",
                "--project-key",
                ref.project_key,
                "--work-item-id",
                ref.work_item_id,
                "--format",
                "json",
            ],
            job_dir=job_dir,
            debug_name="requirement_workitem_get",
        )
        warnings: list[str] = []
        comments_count = self._fetch_comments_count(ref, job_dir=job_dir, warnings=warnings) if self.fetch_comments else None
        attribute = raw.get("work_item_attribute") if isinstance(raw.get("work_item_attribute"), dict) else {}
        description = _plain_text(_field_value(raw, "description") or raw.get("description"))
        facts = extract_requirement_facts(description, max_fact_count=max_fact_count)
        return RequirementWorkItemSnapshot(
            ref=ref,
            title=str(
                raw.get("work_item_name")
                or raw.get("name")
                or attribute.get("work_item_name")
                or ""
            ),
            status=_nested_str(raw, "work_item_status", "name") or _nested_str(attribute, "work_item_status", "name"),
            item_type_name=_nested_str(raw, "work_item_type", "name") or _nested_str(attribute, "work_item_type", "name"),
            priority=(
                _nested_str(raw, "priority", "label")
                or _nested_str(raw, "priority", "name")
                or _field_nested_label(raw, "priority")
            ),
            wiki_url=str(_field_value(raw, "wiki") or raw.get("wiki") or ""),
            description=description,
            create_time=str(raw.get("create_time") or attribute.get("create_time") or ""),
            update_time=str(raw.get("update_time") or attribute.get("update_time") or ""),
            comments_count=comments_count,
            facts=facts,
            raw=raw,
            fetch_warnings=warnings,
        )

    def _fetch_comments_count(
        self,
        ref: RequirementWorkItemRef,
        *,
        job_dir: Path,
        warnings: list[str],
    ) -> int | None:
        try:
            payload = self._run_json(
                [
                    "meegle",
                    "comment",
                    "list",
                    "--project-key",
                    ref.project_key,
                    "--work-item-id",
                    ref.work_item_id,
                    "--format",
                    "json",
                ],
                job_dir=job_dir,
                debug_name="requirement_comment_list",
            )
        except Exception as exc:  # pragma: no cover - error handling path
            warnings.append(f"评论拉取失败：{type(exc).__name__}: {exc}")
            return None
        total = payload.get("total")
        if isinstance(total, int):
            return total
        items = payload.get("items")
        return len(items) if isinstance(items, list) else None

    def _run_json(self, command: list[str], *, job_dir: Path, debug_name: str) -> dict[str, object]:
        completed = self.command_runner(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self.timeout_seconds,
            check=False,
            debug_log_path=job_dir / f"{debug_name}.debug.log",
        )
        if completed.returncode != 0:
            raise RuntimeError((completed.stderr or completed.stdout or "meegle command failed").strip())
        parsed = json.loads(completed.stdout or "{}")
        return parsed if isinstance(parsed, dict) else {"items": parsed}

    @staticmethod
    def _default_runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return run_tracked_process(command, watchdog=None, name="requirement_meegle", **kwargs)


def extract_requirement_facts(text: str, *, max_fact_count: int) -> list[RequirementFact]:
    normalized = re.sub(r"\r\n?", "\n", text or "").strip()
    if not normalized:
        return []
    candidates: list[str] = []
    for line in normalized.splitlines():
        stripped = re.sub(r"^\s*(?:[-*•]|\d+[.、)]|[一二三四五六七八九十]+[、.])\s*", "", line).strip()
        if stripped:
            candidates.append(stripped)
    if len(candidates) <= 1:
        candidates = [part.strip() for part in re.split(r"[。；;]\s*", normalized) if part.strip()]
    facts: list[RequirementFact] = []
    for index, item in enumerate(candidates[:max_fact_count], start=1):
        facts.append(RequirementFact(fact_id=f"REQ-{index}", text=item, source_field="description"))
    return facts


def build_requirement_source_prompt(user_prompt: str, snapshot: RequirementWorkItemSnapshot) -> str:
    facts_text = "\n".join(f"- {fact.fact_id} [{fact.source_field}]: {fact.text}" for fact in snapshot.facts)
    return "\n".join(
        part
        for part in (
            user_prompt.strip(),
            "",
            "这是一个 Feishu Project 需求链接结合源码分析请求。请只读源码，不修改文件。",
            "你必须基于当前仓库源码证据回答；不要猜测，不要把没有证据的点写成确定结论。",
            "",
            f"需求链接：{snapshot.ref.url}",
            f"需求标题：{snapshot.title}",
            f"需求状态：{snapshot.status}",
            f"需求类型：{snapshot.item_type_name}",
            f"优先级：{snapshot.priority}",
            f"Wiki：{snapshot.wiki_url}",
            "",
            "需求事实列表：",
            facts_text or "(未能从描述中提取明确事实)",
            "",
            "请输出中文分析，并在末尾输出一个 ```json fenced block，字段必须包括：",
            '- verdict: one of implemented, partially_implemented, not_found_in_current_repo, insufficient_evidence, blocked',
            "- summary: one-sentence conclusion",
            "- matched_items: array of {fact_id, text, source_field}",
            "- gap_items: array of {fact_id, text, source_field}",
            "- unknown_items: array of {fact_id, text, source_field}",
            "- architecture_impact: one of none, low, medium, high, unknown",
            "- architecture_impact_reason: string",
            "- source_evidence: array of {file, line, symbol, text}",
            "- diagram_notes: array of strings for swimlane/flow diagram hops",
            "",
            "如果源码只证明部分链路，verdict 使用 partially_implemented。",
            "如果当前配置仓库没有发现实现，只能写 not_found_in_current_repo。",
            "如果需求或源码证据不足，写 insufficient_evidence，并把原因放入 unknown_items。",
        )
        if part
    )


def parse_requirement_source_comparison(answer: str) -> RequirementSourceComparison:
    match = JSON_BLOCK_RE.search(answer or "")
    if match is None:
        fallback = _comparison_from_markdown(answer)
        if fallback is not None:
            return fallback
        return RequirementSourceComparison(
            verdict="insufficient_evidence",
            summary=_first_line(answer) or "源码分析未返回结构化结果。",
            architecture_impact="unknown",
            architecture_impact_reason="缺少结构化 JSON 输出。",
            parse_warnings=["source_comparison_json_missing"],
        )
    try:
        data = json.loads(match.group("body"))
    except json.JSONDecodeError as exc:
        return RequirementSourceComparison(
            verdict="insufficient_evidence",
            summary=_first_line(answer) or "源码分析结构化结果无法解析。",
            architecture_impact="unknown",
            architecture_impact_reason=f"JSON 解析失败：{exc}",
            parse_warnings=["source_comparison_json_invalid"],
        )
    verdict = str(data.get("verdict") or "insufficient_evidence")
    if verdict not in VALID_VERDICTS:
        verdict = "insufficient_evidence"
    impact = str(data.get("architecture_impact") or "unknown")
    if impact not in VALID_IMPACTS:
        impact = "unknown"
    return RequirementSourceComparison(
        verdict=verdict,
        summary=str(data.get("summary") or _first_line(answer) or ""),
        matched_items=_facts_from_json(data.get("matched_items")),
        gap_items=_facts_from_json(data.get("gap_items")),
        unknown_items=_facts_from_json(data.get("unknown_items")),
        architecture_impact=impact,
        architecture_impact_reason=str(data.get("architecture_impact_reason") or ""),
        source_evidence=data.get("source_evidence") if isinstance(data.get("source_evidence"), list) else [],
        diagram_notes=[str(item) for item in data.get("diagram_notes", [])] if isinstance(data.get("diagram_notes"), list) else [],
        parse_warnings=[],
    )


class RequirementAnalysisRunner:
    def __init__(
        self,
        config: BridgeConfig,
        *,
        workitem_client: MeegleWorkItemClient | None = None,
        source_analysis_runner: RepositorySourceAnalysisRunner | None = None,
    ) -> None:
        self.config = config
        self.workitem_client = workitem_client or MeegleWorkItemClient(
            timeout_seconds=config.requirement_analysis.timeout_seconds,
            fetch_comments=config.requirement_analysis.fetch_comments,
        )
        self.source_analysis_runner = source_analysis_runner or RepositorySourceAnalysisRunner(config)

    def run(
        self,
        request: RequirementAnalysisRequest,
        event: LarkEvent | None = None,
        *,
        progress_callback: Callable[[dict[str, object]], None] | None = None,
    ) -> TaskResult:
        started = time.monotonic()
        context = create_job_context(self.config.data_dir, event)
        self._emit(progress_callback, "requirement_fetch_started", "拉取需求详情", work_item_id=request.workitem.work_item_id)
        try:
            snapshot = self.workitem_client.fetch(
                request.workitem,
                job_dir=context.logs_dir,
                max_fact_count=self.config.requirement_analysis.max_fact_count,
            )
        except Exception as exc:
            return TaskResult(
                success=False,
                message=f"需求拉取失败：{type(exc).__name__}: {exc}",
                job_id=context.job_id,
                job_dir=context.job_dir,
                error_code="requirement_fetch_failed",
                duration_seconds=time.monotonic() - started,
                details={
                    "mode": "requirement_analysis",
                    "source_mode": request.source_mode,
                    "context_profile": "requirement_analysis",
                    "classification_source": "deterministic_requirement_workitem",
                    "requirement_url": request.workitem.url,
                    "work_item_id": request.workitem.work_item_id,
                },
            )
        if snapshot.wiki_url and not self.config.requirement_analysis.fetch_wiki_body:
            snapshot.fetch_warnings.append("需求存在 wiki 链接；当前未读取 wiki 正文。")
        source_prompt = build_requirement_source_prompt(request.prompt, snapshot)
        source_request = SourceAnalysisRequest(
            prompt=source_prompt[: self.config.requirement_analysis.max_requirement_chars],
            target=snapshot.title or request.workitem.work_item_id,
            raw_text=request.raw_text or request.prompt,
            triggered=True,
            source_mode="requirement_source",
            diagram_kinds=list(request.diagram_kinds or ["swimlane"]),
            output_html=True,
            reason="feishu_project_workitem_source_analysis",
        )
        self._emit(progress_callback, "requirement_source_started", "结合源码分析需求", work_item_id=request.workitem.work_item_id)
        source_result = self.source_analysis_runner.run(source_request, event, progress_callback=progress_callback)
        raw_answer = str(source_result.details.get("source_answer") or source_result.message or "")
        comparison = parse_requirement_source_comparison(raw_answer)
        _fill_unclassified_facts(snapshot, comparison)
        warnings = list(snapshot.fetch_warnings) + list(comparison.parse_warnings)
        html_path = context.output_dir / "requirement_analysis_report.html"
        html_path.write_text(
            render_requirement_analysis_report(
                snapshot=snapshot,
                request_text=request.raw_text or request.prompt,
                raw_source_answer=raw_answer,
                comparison=comparison,
                backend=str(source_result.details.get("source_execution_backend") or "unknown"),
                warnings=warnings,
            ),
            encoding="utf-8",
        )
        self._emit(progress_callback, "requirement_analysis_completed", "需求源码分析完成", work_item_id=request.workitem.work_item_id)
        return TaskResult(
            success=source_result.success,
            message=comparison.summary or _first_line(raw_answer) or "需求源码分析完成，详见 HTML 报告。",
            job_id=context.job_id,
            job_dir=context.job_dir,
            html_report=html_path,
            duration_seconds=time.monotonic() - started,
            error_code=None if source_result.success else "requirement_source_analysis_failed",
            details={
                "mode": "requirement_analysis",
                "source_mode": "requirement_source",
                "context_profile": "requirement_analysis",
                "classification_source": "deterministic_requirement_workitem",
                "classification_reason": request.reason,
                "requirement_url": request.workitem.url,
                "project_key": request.workitem.project_key,
                "work_item_type": request.workitem.work_item_type,
                "work_item_id": request.workitem.work_item_id,
                "requirement_title": snapshot.title,
                "requirement_status": snapshot.status,
                "requirement_verdict": comparison.verdict,
                "architecture_impact": comparison.architecture_impact,
                "architecture_impact_reason": comparison.architecture_impact_reason,
                "requirement_parse_warnings": warnings,
                "source_evidence": comparison.source_evidence,
                "matched_items": [fact.fact_id for fact in comparison.matched_items],
                "gap_items": [fact.fact_id for fact in comparison.gap_items],
                "unknown_items": [fact.fact_id for fact in comparison.unknown_items],
                "files_to_send": [html_path],
                "user_request_text": request.raw_text or request.prompt,
            },
        )

    def _emit(
        self,
        progress_callback: Callable[[dict[str, object]], None] | None,
        stage: str,
        message: str,
        **details: object,
    ) -> None:
        if progress_callback is None:
            return
        progress_callback({"stage": stage, "message": message, "details": details})


def _fill_unclassified_facts(snapshot: RequirementWorkItemSnapshot, comparison: RequirementSourceComparison) -> None:
    if MARKDOWN_FALLBACK_WARNING in comparison.parse_warnings:
        return
    known = {fact.fact_id for fact in comparison.matched_items}
    known.update(fact.fact_id for fact in comparison.gap_items)
    known.update(fact.fact_id for fact in comparison.unknown_items)
    for fact in snapshot.facts:
        if fact.fact_id not in known:
            comparison.unknown_items.append(fact)


def _comparison_from_markdown(answer: str) -> RequirementSourceComparison | None:
    sections = _markdown_sections(answer)
    if not sections:
        return None
    summary = _first_paragraph(sections.get("结论摘要", "")) or _first_line(answer)
    evidence = _markdown_evidence(sections.get("关键证据", ""))
    causes = _markdown_list_items(sections.get("最可能原因", ""))
    unknowns = [
        RequirementFact(fact_id=f"待确认-{index}", text=item, source_field="agent_markdown")
        for index, item in enumerate(_markdown_list_items(sections.get("待确认项", "")), start=1)
    ]
    actions = _markdown_list_items(sections.get("建议动作", ""))
    if not (summary or evidence or causes or unknowns or actions):
        return None
    return RequirementSourceComparison(
        verdict=_infer_markdown_verdict(answer),
        summary=summary or "源码分析已返回 Markdown 结果，详见报告正文。",
        matched_items=[],
        gap_items=[],
        unknown_items=unknowns,
        architecture_impact=_infer_markdown_impact(answer),
        architecture_impact_reason="；".join(causes[:3]),
        source_evidence=evidence,
        diagram_notes=actions[:4],
        parse_warnings=[MARKDOWN_FALLBACK_WARNING],
    )


def _markdown_sections(markdown: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current = ""
    for raw_line in (markdown or "").splitlines():
        line = raw_line.rstrip()
        heading = re.match(r"^##\s+(.+?)\s*$", line)
        if heading:
            current = heading.group(1).strip()
            sections.setdefault(current, [])
            continue
        if current:
            sections[current].append(line)
    return {key: "\n".join(value).strip() for key, value in sections.items()}


def _first_paragraph(text: str) -> str:
    lines: list[str] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            if lines:
                break
            continue
        lines.append(_clean_markdown_inline(line))
    return " ".join(lines).strip()


def _markdown_evidence(section: str) -> list[dict[str, object]]:
    items = _numbered_markdown_blocks(section)
    evidence: list[dict[str, object]] = []
    for title, lines in items:
        block = "\n".join(lines)
        file_path, line_no = _first_source_location(block)
        key_content = _first_labeled_line(block, ("关键内容", "影响", "说明")) or _clean_markdown_inline(title)
        evidence.append(
            {
                "file": file_path,
                "line": line_no,
                "symbol": "",
                "text": _truncate_text(f"{_clean_markdown_inline(title)}：{key_content}", 420),
            }
        )
    if evidence:
        return evidence
    for file_path, line_no in _all_source_locations(section):
        evidence.append({"file": file_path, "line": line_no, "symbol": "", "text": ""})
    return evidence


def _numbered_markdown_blocks(section: str) -> list[tuple[str, list[str]]]:
    items: list[tuple[str, list[str]]] = []
    current_title = ""
    current_lines: list[str] = []
    for raw_line in (section or "").splitlines():
        match = re.match(r"^\s*\d+[.、]\s+(?:\*\*)?(.+?)(?:\*\*)?\s*$", raw_line)
        if match:
            if current_title:
                items.append((current_title, current_lines))
            current_title = match.group(1).strip()
            current_lines = []
            continue
        if current_title:
            current_lines.append(raw_line)
    if current_title:
        items.append((current_title, current_lines))
    return items


def _first_source_location(text: str) -> tuple[str, int | str]:
    locations = _all_source_locations(text)
    return locations[0] if locations else ("", "")


def _all_source_locations(text: str) -> list[tuple[str, int | str]]:
    locations: list[tuple[str, int | str]] = []
    for match in re.finditer(r"`([^`\n]+?):(\d+)(?:-\d+)?`", text or ""):
        path = match.group(1).strip()
        if not path or "://" in path:
            continue
        locations.append((path, int(match.group(2))))
    return locations


def _first_labeled_line(text: str, labels: tuple[str, ...]) -> str:
    label_pattern = "|".join(re.escape(label) for label in labels)
    for raw_line in (text or "").splitlines():
        match = re.search(rf"(?:{label_pattern})：(.+)", raw_line)
        if match:
            return _clean_markdown_inline(match.group(1))
    return ""


def _markdown_list_items(section: str) -> list[str]:
    items: list[str] = []
    for raw_line in (section or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        line = re.sub(r"^\s*(?:[-*•]|\d+[.、)])\s*", "", line).strip()
        line = _clean_markdown_inline(line)
        if not line or line.startswith(("支撑证据：", "说明：")):
            continue
        items.append(line)
    return items


def _infer_markdown_verdict(answer: str) -> str:
    text = answer or ""
    for verdict in ("partially_implemented", "implemented", "not_found_in_current_repo", "insufficient_evidence", "blocked"):
        if re.search(rf"\b{re.escape(verdict)}\b", text):
            return verdict
    if re.search(r"部分(?:具备|实现)|未闭环|尚未.*打通|并行链路", text):
        return "partially_implemented"
    if re.search(r"未发现|未找到|未见.*实现|当前仓库没有", text):
        return "not_found_in_current_repo"
    if re.search(r"证据不足|无法证明|不能证明", text):
        return "insufficient_evidence"
    if re.search(r"已实现|已闭环|完整闭环", text):
        return "implemented"
    return "insufficient_evidence"


def _infer_markdown_impact(answer: str) -> str:
    if re.search(r"新增|状态机|链路|架构|模式|framework|Unity|native", answer or "", re.I):
        return "medium"
    return "unknown"


def _clean_markdown_inline(text: str) -> str:
    cleaned = re.sub(r"`([^`]*)`", r"\1", text or "")
    cleaned = re.sub(r"\*\*([^*]+)\*\*", r"\1", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def _truncate_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


def _facts_from_json(value: object) -> list[RequirementFact]:
    if not isinstance(value, list):
        return []
    facts: list[RequirementFact] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        facts.append(
            RequirementFact(
                fact_id=str(item.get("fact_id") or ""),
                text=str(item.get("text") or ""),
                source_field=str(item.get("source_field") or ""),
            )
        )
    return facts


def _nested_str(data: dict[str, object], key: str, child: str) -> str:
    value = data.get(key)
    if isinstance(value, dict):
        return str(value.get(child) or "")
    return ""


def _field_value(data: dict[str, object], field_key: str) -> object:
    fields = data.get("work_item_fields")
    if not isinstance(fields, list):
        return None
    for item in fields:
        if not isinstance(item, dict):
            continue
        if item.get("key") == field_key:
            return item.get("value")
    return None


def _field_nested_label(data: dict[str, object], field_key: str) -> str:
    value = _field_value(data, field_key)
    if isinstance(value, dict):
        return str(value.get("label") or value.get("name") or "")
    return str(value or "")


def _plain_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = unescape(value)
        text = re.sub(r"(?i)<br\\s*/?>", "\n", text)
        text = re.sub(r"(?i)</?(?:p|div|span|table|tr|td|ul|ol|li|blockquote|strong|em|h\\d)[^>]*>", "\n", text)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        text = re.sub(r"!\[[^\]]*]\([^)]+\)", " ", text)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()
    if isinstance(value, dict):
        return "\n".join(text for item in value.values() if (text := _plain_text(item)))
    if isinstance(value, list):
        return "\n".join(text for item in value if (text := _plain_text(item)))
    return str(value)


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:1200]
    return ""
