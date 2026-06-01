"""HTML renderer for generic requirement source analysis reports."""

from __future__ import annotations

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
    body = (
        '<div class="container">'
        f"<h1>{H(snapshot.title or '需求源码分析报告')}</h1>"
        f'<div class="sub">请求：{H(request_text)}<br>需求链接：{H(snapshot.ref.url)}</div>'
        f'<div class="verdict v-{severity}">一句话结论：{H(comparison.summary or "源码证据不足，详见未确认项。")}</div>'
        f'<div class="cards">{render_cards(_cards(snapshot, comparison, backend, warnings))}</div>'
        f'{render_section("需求解析结果", render_table(_snapshot_rows(snapshot), ("项目", "内容")))}'
        f'{render_section("需求事实矩阵", render_table(_fact_rows(comparison), ("状态", "Fact", "内容", "来源")))}'
        f'{render_section("源码证据", render_table(_evidence_rows(comparison.source_evidence), ("文件", "行号", "符号", "证据")))}'
        f'{render_section("问题与未确认项", render_issue_list(_issues(comparison, warnings)))}'
        f'{render_section("泳道图", _swimlane(snapshot, comparison))}'
        f'{render_chain(_chain_nodes(snapshot, comparison), title="卡点链路", description="按需求输入、源码证据、架构影响和未确认项组织。")}'
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
    return [
        ("需求状态", snapshot.status or "未知", "green" if snapshot.status else "yellow", ""),
        ("判定", comparison.verdict or "unknown", _severity(comparison.verdict), ""),
        ("源码证据", str(len(comparison.source_evidence)), "green" if comparison.source_evidence else "yellow", ""),
        ("警告", str(len(warnings)), "yellow" if warnings else "green", ""),
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
) -> list[dict[str, object]]:
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
            "downstream": "形成 implemented / partial / insufficient 等判定。",
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
            "downstream": "交由用户或模块 owner 判断。",
        },
    ]
