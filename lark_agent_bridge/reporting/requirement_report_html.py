"""HTML renderer for generic requirement source analysis reports."""

from __future__ import annotations

import re

from .source_report_html import (
    H,
    render_cards,
    render_chain,
    render_details,
    render_document,
    render_issue_list,
    render_section,
    render_table,
)
from ..models import RequirementSourceComparison, RequirementWorkItemSnapshot


def render_requirement_analysis_report(
    *,
    snapshot: RequirementWorkItemSnapshot,
    request_text: str,
    raw_source_answer: str,
    comparison: RequirementSourceComparison,
    backend: str,
    warnings: list[str],
) -> str:
    severity = _severity(comparison.verdict)
    sections = _markdown_sections(raw_source_answer)
    summary_text = _section_text(sections, "结论摘要") or comparison.summary or "源码证据不足，详见未确认项。"
    body = (
        '<div class="container">'
        f"<h1>{H(snapshot.title or '需求源码分析报告')}</h1>"
        f'<div class="sub">请求：{H(request_text)}<br>需求链接：{H(snapshot.ref.url)}</div>'
        f'<div class="verdict v-{severity}">一句话结论：{H(summary_text)}</div>'
        f'<div class="cards">{render_cards(_cards(snapshot, comparison, backend, warnings))}</div>'
        f'{render_section("结论摘要", _text_block(summary_text))}'
        f'{render_section("最可能原因", render_issue_list(_section_issue_items(sections.get("最可能原因", ""), "yellow"), empty_text="未明确给出最可能原因。"))}'
        f'{render_section("关键证据", render_issue_list(_evidence_issues(comparison.source_evidence, sections.get("关键证据", "")), empty_text="未返回源码证据。"))}'
        f'{render_section("问题与未确认项", render_issue_list(_pending_items(comparison, sections), empty_text="当前没有额外待确认项。"))}'
        f'{render_section("建议动作", render_issue_list(_section_issue_items(sections.get("建议动作", ""), "green"), empty_text="当前没有额外建议动作。"))}'
        f'{render_section("泳道图", _swimlane(snapshot, comparison))}'
        f'{render_chain(_chain_nodes(snapshot, comparison, sections), title="卡点链路", description="按需求输入、源码证据、架构影响和未确认项组织。")}'
        f'{render_section("源码证据表", render_table(_evidence_rows(comparison.source_evidence), ("文件", "行号", "符号", "证据")))}'
        f'{_requirement_details(snapshot, comparison)}'
        f'{render_details("处理说明", "展开查看需求拉取与解析边界", _processing_notes(warnings))}'
        f'{render_details("原始源码分析输出", "展开查看 agent 源码分析原文", raw_source_answer)}'
        "</div>"
    )
    return render_document(snapshot.title or "需求源码分析报告", body)


def _severity(verdict: str) -> str:
    if verdict == "implemented":
        return "green"
    if verdict in {"partially_implemented", "insufficient_evidence", "blocked"}:
        return "yellow"
    return "red"


def _cards(
    snapshot: RequirementWorkItemSnapshot,
    comparison: RequirementSourceComparison,
    backend: str,
    warnings: list[str],
) -> list[tuple[object, ...]]:
    pending_count = len(comparison.gap_items) + len(comparison.unknown_items)
    return [
        ("需求状态", snapshot.status or "未知", "green" if snapshot.status else "yellow", ""),
        ("可行性判定", _verdict_label(comparison.verdict), _severity(comparison.verdict), comparison.verdict or "unknown"),
        ("源码证据", str(len(comparison.source_evidence)), "green" if comparison.source_evidence else "yellow", "来自 agent 原始输出或结构化 JSON。"),
        ("待确认/缺口", str(pending_count), "yellow" if pending_count else "green", ""),
        ("处理边界", str(len(warnings)), "yellow" if warnings else "green", "需求系统/wiki/结构化解析边界。"),
        ("后端", backend or "unknown", "green" if backend else "yellow", ""),
    ]


def _snapshot_rows(snapshot: RequirementWorkItemSnapshot) -> list[tuple[object, object]]:
    return [
        ("WorkItem", f"{snapshot.ref.project_key}/{snapshot.ref.work_item_type}/{snapshot.ref.work_item_id}"),
        ("标题", snapshot.title or "(空)"),
        ("状态", snapshot.status or "(空)"),
        ("类型", snapshot.item_type_name or "(空)"),
        ("优先级", snapshot.priority or "(空)"),
        ("Wiki", snapshot.wiki_url or "(空)"),
        ("更新时间", snapshot.update_time or "(空)"),
        ("评论数", snapshot.comments_count if snapshot.comments_count is not None else "(未知)"),
        ("描述摘录", (snapshot.description or "(空)")[:1600]),
    ]


def _fact_rows(comparison: RequirementSourceComparison) -> list[tuple[object, object, object, object]]:
    rows: list[tuple[object, object, object, object]] = []
    for fact in comparison.matched_items:
        rows.append(("已匹配", fact.fact_id, fact.text, fact.source_field))
    for fact in comparison.gap_items:
        rows.append(("缺口", fact.fact_id, fact.text, fact.source_field))
    for fact in comparison.unknown_items:
        rows.append(("未确认", fact.fact_id, fact.text, fact.source_field))
    return rows


def _evidence_rows(source_evidence: list[dict[str, object]]) -> list[tuple[object, object, object, object]]:
    return [
        (
            item.get("file", ""),
            item.get("line", ""),
            item.get("symbol", ""),
            item.get("text", ""),
        )
        for item in source_evidence
    ]


def _issues(
    comparison: RequirementSourceComparison,
    warnings: list[str],
) -> list[dict[str, object]]:
    issues: list[dict[str, object]] = []
    for item in warnings:
        issues.append({"sev": "yellow", "title": item, "detail": "需要人工确认或补充证据。"})
    for fact in comparison.gap_items:
        issues.append({"sev": "red", "title": fact.fact_id, "detail": fact.text})
    for fact in comparison.unknown_items:
        issues.append({"sev": "yellow", "title": fact.fact_id, "detail": fact.text})
    if not issues:
        issues.append({"sev": "green", "title": "未发现阻断项", "detail": "仍以源码证据范围为准。"})
    return issues


def _verdict_label(verdict: str) -> str:
    labels = {
        "implemented": "已闭环",
        "partially_implemented": "部分具备",
        "not_found_in_current_repo": "未命中实现",
        "insufficient_evidence": "证据不足",
        "blocked": "受阻",
    }
    return labels.get(verdict or "", verdict or "unknown")


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


def _section_text(sections: dict[str, str], title: str) -> str:
    text = sections.get(title, "")
    paragraphs = []
    current: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            if current:
                paragraphs.append(" ".join(current))
                current = []
            continue
        if re.match(r"^\s*(?:[-*•]|\d+[.、)])\s+", line):
            continue
        current.append(_clean_markdown_inline(line))
    if current:
        paragraphs.append(" ".join(current))
    return paragraphs[0] if paragraphs else ""


def _text_block(text: str) -> str:
    return f'<div class="issue {_severity_from_text(text)}"><div class="d">{H(text)}</div></div>'


def _severity_from_text(text: str) -> str:
    if re.search(r"未闭环|没有看到|缺少|冲突|不支持|未见", text or ""):
        return "yellow"
    return "green"


def _section_issue_items(section: str, severity: str) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for title, detail_lines in _numbered_markdown_blocks(section):
        detail = _clean_markdown_inline(" ".join(detail_lines))
        items.append({"sev": severity, "title": _clean_markdown_inline(title), "detail": detail})
    if items:
        return items
    for index, item in enumerate(_markdown_list_items(section), start=1):
        items.append({"sev": severity, "title": f"{index}. {item}", "detail": ""})
    return items


def _evidence_issues(source_evidence: list[dict[str, object]], section: str) -> list[dict[str, object]]:
    issues: list[dict[str, object]] = []
    markdown_blocks = _numbered_markdown_blocks(section)
    if markdown_blocks:
        for title, detail_lines in markdown_blocks[:12]:
            detail = _clean_markdown_inline(" ".join(detail_lines))
            sev = "yellow" if re.search(r"不含|没有|缺少|未见|不等于|冲突", title + detail) else "green"
            issues.append({"sev": sev, "title": _clean_markdown_inline(title), "detail": detail})
        return issues
    for item in source_evidence[:12]:
        location = _format_location(item)
        issues.append({"sev": "green", "title": str(item.get("text") or location or "源码证据"), "detail": location})
    return issues


def _pending_items(
    comparison: RequirementSourceComparison,
    sections: dict[str, str],
) -> list[dict[str, object]]:
    pending = [{"sev": "yellow", "title": fact.fact_id, "detail": fact.text} for fact in comparison.gap_items]
    pending.extend({"sev": "yellow", "title": fact.fact_id, "detail": fact.text} for fact in comparison.unknown_items)
    markdown_pending = _markdown_list_items(sections.get("待确认项", ""))
    existing = {str(item["detail"]) for item in pending}
    for index, item in enumerate(markdown_pending, start=1):
        if item not in existing:
            pending.append({"sev": "yellow", "title": f"待确认-{index}", "detail": item})
    return pending


def _requirement_details(snapshot: RequirementWorkItemSnapshot, comparison: RequirementSourceComparison) -> str:
    return (
        '<div class="section"><h2>需求字段与事实矩阵</h2>'
        '<details><summary>展开查看需求字段与事实矩阵</summary>'
        '<h3>需求解析结果</h3>'
        f'{render_table(_snapshot_rows(snapshot), ("项目", "内容"))}'
        '<h3>需求事实矩阵</h3>'
        f'{render_table(_fact_rows(comparison), ("状态", "Fact", "内容", "来源"))}'
        "</details></div>"
    )


def _processing_notes(warnings: list[str]) -> str:
    visible = [_warning_label(item) for item in warnings if item]
    if not visible:
        return "无额外处理边界。"
    return "\n".join(f"- {item}" for item in visible)


def _warning_label(item: str) -> str:
    labels = {
        "source_comparison_json_missing_markdown_fallback": "agent 未输出结构化 JSON，报告已从 Markdown 章节恢复结论、证据和待确认项。",
        "source_comparison_json_missing": "agent 未输出结构化 JSON，无法恢复结构化需求对比。",
        "source_comparison_json_invalid": "agent 输出的结构化 JSON 无法解析。",
    }
    return labels.get(item, item)


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
            current_lines.append(raw_line.strip())
    if current_title:
        items.append((current_title, current_lines))
    return items


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


def _clean_markdown_inline(text: str) -> str:
    cleaned = re.sub(r"`([^`]*)`", r"\1", text or "")
    cleaned = re.sub(r"\*\*([^*]+)\*\*", r"\1", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def _format_location(item: dict[str, object]) -> str:
    file_path = str(item.get("file") or "")
    line = str(item.get("line") or "")
    symbol = str(item.get("symbol") or "")
    location = f"{file_path}:{line}" if file_path and line else file_path
    if symbol:
        location = f"{location} {symbol}".strip()
    return location


def _swimlane(snapshot: RequirementWorkItemSnapshot, comparison: RequirementSourceComparison) -> str:
    return (
        '<div class="swimlane">'
        '<div class="lane"><div class="lane-title">用户输入</div>'
        f'<div class="lane-step"><div class="name">需求链接</div><div class="desc">{H(snapshot.ref.url)}</div></div></div>'
        '<div class="lane"><div class="lane-title">需求系统</div>'
        f'<div class="lane-step"><div class="name">事实</div><div class="desc">{H(str(len(snapshot.facts)) + " 条需求事实")}</div></div></div>'
        '<div class="lane"><div class="lane-title">源码仓</div>'
        f'<div class="lane-step"><div class="name">证据</div><div class="desc">{H(str(len(comparison.source_evidence)) + " 条源码证据")}</div></div></div>'
        '<div class="lane"><div class="lane-title">输出判断</div>'
        f'<div class="lane-step"><div class="name">结论</div><div class="desc">{H(comparison.verdict or "unknown")}</div></div></div>'
        "</div>"
    )


def _chain_nodes(
    snapshot: RequirementWorkItemSnapshot,
    comparison: RequirementSourceComparison,
    sections: dict[str, str],
) -> list[dict[str, object]]:
    first_cause = _markdown_list_items(sections.get("最可能原因", ""))
    first_action = _markdown_list_items(sections.get("建议动作", ""))
    return [
        {
            "sev": "green",
            "title": "需求链接解析",
            "evidence": snapshot.ref.url,
            "downstream": "获取标题、状态、描述和需求事实。",
        },
        {
            "sev": "green" if comparison.source_evidence else "yellow",
            "title": "源码对比",
            "evidence": f"{len(comparison.source_evidence)} 条源码证据",
            "downstream": first_cause[0] if first_cause else "形成 implemented / partial / insufficient 等判定。",
        },
        {
            "sev": _severity(comparison.verdict),
            "title": "架构影响",
            "evidence": comparison.architecture_impact or "unknown",
            "downstream": comparison.architecture_impact_reason or "需要结合源码证据判断。",
        },
        {
            "sev": "yellow" if comparison.unknown_items else "green",
            "title": "未确认项",
            "evidence": f"{len(comparison.unknown_items)} 项",
            "downstream": first_action[0] if first_action else "交由用户或模块 owner 判断。",
        },
    ]
