"""HTML reports for source-only analysis and diagram followups."""

from __future__ import annotations

import html
import re
from typing import Iterable, Mapping, Sequence

from .combined_bug_html import render_swimlane_rows


BASE_REPORT_CSS = """
* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body {
  font-family: -apple-system, "Helvetica Neue", "PingFang SC", "Microsoft YaHei", sans-serif;
  margin: 0;
  min-height: 100vh;
  overflow-wrap: anywhere;
  color: #1f2937;
  background:
    radial-gradient(circle at 0 0, rgba(239, 68, 68, 0.08), transparent 38%),
    radial-gradient(circle at 100% 0, rgba(217, 119, 6, 0.06), transparent 42%),
    #f4f6f8;
}
.container { max-width: 1380px; margin: 0 auto; padding: 34px 28px 52px; }
h1 { margin: 0 0 10px; font-size: 36px; line-height: 1.2; color: #111827; }
.sub { max-width: 1040px; color: #6b7280; margin-bottom: 26px; font-size: 14px; line-height: 1.75; }
.verdict {
  padding: 22px 26px;
  border-radius: 18px;
  color: #fff;
  font-size: 20px;
  font-weight: 700;
  box-shadow: 0 18px 42px rgba(15, 23, 42, 0.16);
  border: 1px solid rgba(255, 255, 255, 0.24);
  margin-bottom: 26px;
  line-height: 1.6;
}
.v-red { background: linear-gradient(140deg, #ef4444, #b91c1c); }
.v-yellow { background: linear-gradient(140deg, #f59e0b, #b45309); }
.v-green { background: linear-gradient(140deg, #10b981, #047857); }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 14px; margin-bottom: 24px; }
.card {
  background: #ffffff;
  border-radius: 12px;
  border: 1px solid #e5e7eb;
  border-top: 3px solid #94a3b8;
  padding: 16px 18px;
  box-shadow: 0 10px 26px rgba(15, 23, 42, 0.07);
}
.card .lbl { font-size: 12px; font-weight: 700; letter-spacing: 0.02em; color: #64748b; }
.card .val { font-size: 24px; font-weight: 700; margin-top: 4px; word-break: break-word; color: #0f172a; }
.card .desc { color: #64748b; font-size: 13px; line-height: 1.65; margin-top: 6px; }
.card.red { border-top-color: #dc2626; }
.card.yellow { border-top-color: #d97706; }
.card.green { border-top-color: #059669; }
.card.red .val { color: #dc2626; }
.card.yellow .val { color: #b45309; }
.card.green .val { color: #047857; }
.section {
  background: #fff;
  border-radius: 14px;
  border: 1px solid #e5e7eb;
  padding: 22px 26px;
  margin-bottom: 20px;
  box-shadow: 0 10px 26px rgba(15, 23, 42, 0.06);
  overflow-x: auto;
}
.section h2 { margin: 0 0 16px; font-size: 21px; border-left: 4px solid #d97706; padding-left: 10px; }
.issue {
  padding: 12px 16px;
  border-radius: 10px;
  margin-bottom: 10px;
  border: 1px solid #d1d5db;
  border-left: 4px solid #94a3b8;
  background: #f8fafc;
}
.issue.red { border-color: #fecaca; border-left-color: #dc2626; background: #fef2f2; }
.issue.yellow { border-color: #fde68a; border-left-color: #d97706; background: #fffbeb; }
.issue.green { border-color: #a7f3d0; border-left-color: #059669; background: #ecfdf5; }
.issue .t { font-weight: 700; font-size: 15px; }
.issue .d { color: #6b7280; font-size: 13px; margin-top: 5px; line-height: 1.65; }
table { width: 100%; min-width: 840px; border-collapse: collapse; font-size: 14px; }
th, td { padding: 10px 12px; border-bottom: 1px solid #edf2f7; text-align: left; vertical-align: top; }
th { background: #f8fafc; font-weight: 600; color: #334155; }
details { margin-top: 10px; border: 1px solid rgba(148, 163, 184, 0.28); border-radius: 10px; padding: 10px 12px; background: rgba(248, 250, 252, 0.72); }
summary { cursor: pointer; color: #b45309; font-size: 14px; font-weight: 700; }
pre {
  max-height: 560px;
  background: #f8fafc;
  padding: 12px;
  border-radius: 8px;
  font-size: 13px;
  line-height: 1.65;
  white-space: pre-wrap;
  word-break: break-word;
  overflow-x: auto;
  border: 1px solid #e2e8f0;
}
.chain { display: flex; flex-direction: column; align-items: stretch; gap: 0; }
.chain-node {
  display: flex;
  align-items: stretch;
  background: #fff;
  border-radius: 12px;
  border: 1px solid #e5e7eb;
  border-left: 6px solid #94a3b8;
  box-shadow: 0 1px 3px rgba(15, 23, 42, 0.08);
  overflow: hidden;
}
.chain-node.chain-red { border-left-color: #dc2626; }
.chain-node.chain-yellow { border-left-color: #d97706; }
.chain-node.chain-green { border-left-color: #059669; }
.chain-step {
  width: 40px;
  min-width: 40px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 16px;
  font-weight: 700;
  color: #fff;
  background: #64748b;
}
.chain-red .chain-step { background: #dc2626; }
.chain-yellow .chain-step { background: #d97706; }
.chain-green .chain-step { background: #059669; }
.chain-body { padding: 12px 16px; flex: 1; }
.chain-title { font-weight: 700; font-size: 15px; margin-bottom: 4px; }
.chain-evi, .chain-down { font-size: 13px; color: #475569; line-height: 1.55; margin-top: 2px; }
.chain-arrow { text-align: center; font-size: 22px; color: #94a3b8; line-height: 1.1; padding: 4px 0; font-weight: 700; }
.swimlane-svg-wrap { overflow-x: auto; padding-bottom: 4px; }
.swimlane-svg { display: block; width: 100%; min-width: 1280px; height: auto; }
.flow-line { display: flex; align-items: stretch; gap: 10px; overflow-x: auto; padding-bottom: 2px; }
.flow-step { min-width: 230px; background: #f8fafc; border: 1px solid #e5e7eb; border-radius: 12px; padding: 12px 14px; }
.flow-step .tag { font-size: 11px; color: #b45309; margin-bottom: 6px; font-weight: 700; }
.flow-step .title { font-size: 15px; font-weight: 800; color: #10213d; }
.flow-step .note { font-size: 12px; color: #64748b; margin-top: 8px; line-height: 1.6; }
.flow-arrow { display: flex; align-items: center; justify-content: center; min-width: 28px; color: #94a3b8; font-size: 24px; font-weight: 700; }
.muted { color: #94a3b8; }
@media (max-width: 880px) {
  h1 { font-size: 24px; }
  .cards { grid-template-columns: 1fr; }
  .container { padding: 14px; }
  table { min-width: 640px; }
}
"""


def H(value: object) -> str:
    return html.escape(str(value) if value is not None else "")


def render_source_analysis_report(
    *,
    title: str,
    request_text: str,
    answer: str,
    target: str,
    source_evidence: Iterable[Mapping[str, object]],
    coverage_boundary: str,
    diagram_kinds: Sequence[str],
    backend: str,
    success: bool,
) -> str:
    evidence = list(source_evidence or [])
    sections = _markdown_sections(answer)
    swimlane_rows = _markdown_swimlane_rows(sections.get("结论摘要", "")) if sections else []
    evidence_rows = _markdown_evidence_rows(sections.get("关键证据", "")) if sections else []
    visual_evidence = evidence or [
        {"file": row[2], "line": "", "text": f"{row[0]}：{row[1]}"}
        for row in evidence_rows
    ]
    effective_evidence_count = len(evidence) or len(evidence_rows)
    severity = "green" if success and effective_evidence_count else "yellow" if effective_evidence_count else "red"
    summary_items = _section_issue_items(_strip_markdown_tables(sections.get("结论摘要", ""))) if sections else []
    verdict = (
        str(summary_items[0].get("title") or "")
        if summary_items
        else _source_verdict(answer=answer, evidence=visual_evidence, success=success)
    )
    summary_body = render_issue_list(summary_items, empty_text=_short_text(answer or verdict, 240)) if summary_items else f"<pre>{H(answer or verdict)}</pre>"
    boundary_items = _source_boundary_items(coverage_boundary, sections.get("待确认项", "") if sections else "")
    reason_items = _section_issue_items(sections.get("最可能原因", "")) if sections else []
    action_items = _section_issue_items(sections.get("建议动作", "")) if sections else []
    chain_nodes = _chain_nodes_from_evidence(answer=answer, evidence=visual_evidence, target=target, success=success)
    cards = [
        ("目标", target or "未识别", "green" if target else "yellow", ""),
        ("输出", "咨询/源码报告", "green", "面向源码与业务咨询请求的独立报告壳。"),
        ("证据条数", str(effective_evidence_count), "green" if effective_evidence_count else "yellow", "来自源码调查结果。"),
        ("后端", backend or "unknown", "green" if backend else "yellow", ""),
    ]
    body = (
        '<div class="container">'
        f"<h1>{H(title or '源码分析报告')}</h1>"
        f'<div class="sub">请求：{H(request_text)}</div>'
        f'<div class="verdict v-{severity}">{H(verdict)}</div>'
        f'<div class="cards">{render_cards(cards)}</div>'
        f'{render_section("结论摘要", summary_body)}'
        f'{render_swimlane(target=target, answer=answer, evidence=evidence, diagram_kinds=diagram_kinds, swimlane_rows=swimlane_rows)}'
        f'{render_section("最可能原因", render_issue_list(reason_items, empty_text="未明确给出最可能原因。"))}'
        f'{render_section("边界与说明", render_issue_list(boundary_items, empty_text="未返回额外边界说明。"))}'
        f'{render_section("建议动作", render_issue_list(action_items, empty_text="当前没有额外建议动作。"))}'
        f'{render_flow(diagram_kinds=diagram_kinds, target=target, evidence=evidence)}'
        f'{render_chain(chain_nodes, title="分析链路", description="按入口、分发、消费和待确认组织源码证据。")}'
        f'{render_section("源码证据", render_table(_final_evidence_rows(evidence, evidence_rows), ("位置", "关键点", "源码锚点")))}'
        f'{render_details("边界说明", "展开查看覆盖范围与限制", coverage_boundary or "未返回覆盖边界。")}'
        f'{render_details("原始源码分析输出", "展开查看源码调查输出", answer)}'
        "</div>"
    )
    return render_document(title or "源码分析报告", body)


def render_context_diagram_report(
    *,
    title: str,
    request_text: str,
    followup_text: str,
    summary_text: str,
    report_excerpt: str,
    history: Sequence[Mapping[str, str]],
    diagram_kinds: Sequence[str],
    source_mode: str,
    context_profile: str,
) -> str:
    kinds = list(diagram_kinds or ["swimlane"])
    context_text = "\n\n".join(part for part in (summary_text, report_excerpt) if part.strip())
    severity = "green" if context_text.strip() else "yellow"
    cards = [
        ("报告类型", "上下文图表", "green", "基于上一轮分析上下文生成，不重新读源码。"),
        ("图表", "、".join(_diagram_labels(kinds)) or "泳道图", "green", ""),
        ("源码模式", source_mode or "未记录", "green" if source_mode else "yellow", ""),
        ("上下文", context_profile or "通用分析", "green" if context_profile else "yellow", ""),
    ]
    issues = [
        {
            "sev": severity,
            "title": "已基于现有回复生成图表" if context_text.strip() else "上下文证据不足",
            "detail": "该报告不重新访问 app-server 或源码仓；需要重新查源码时应发起新的源码分析请求。",
        }
    ]
    evidence = [{"file": "conversation_context", "line": "", "text": context_text[:500] or "无上下文摘录"}]
    body = (
        '<div class="container">'
        f"<h1>{H(title or '图表报告')}</h1>"
        f'<div class="sub">原始请求：{H(request_text)}<br>追问：{H(followup_text)}</div>'
        f'<div class="verdict v-{severity}">{H("已生成上下文泳道图/链路图报告。" if context_text.strip() else "上下文不足，仅输出边界说明报告。")}</div>'
        f'<div class="cards">{render_cards(cards)}</div>'
        f'{render_section("异常摘要", render_issue_list(issues))}'
        f'{render_swimlane(target="上一轮分析", answer=summary_text or report_excerpt, evidence=evidence, diagram_kinds=kinds)}'
        f'{render_flow(diagram_kinds=kinds, target="上一轮分析", evidence=evidence)}'
        f'{render_section("上下文证据", render_table([("摘要", summary_text or "(空)"), ("报告摘录", report_excerpt or "(空)")], ("字段", "内容")))}'
        f'{render_details("历史对话", "展开查看历史追问", _history_text(history))}'
        "</div>"
    )
    return render_document(title or "图表报告", body)


def render_document(title: str, body_html: str, css: str = BASE_REPORT_CSS) -> str:
    return (
        "<!DOCTYPE html>\n"
        '<html lang="zh-CN">\n'
        "<head>\n"
        '  <meta charset="utf-8">\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"  <title>{H(title)}</title>\n"
        f"  <style>{css}</style>\n"
        "</head>\n"
        "<body>\n"
        f"{body_html}\n"
        "</body>\n"
        "</html>\n"
    )


def render_cards(cards: Iterable[Sequence[object]]) -> str:
    chunks: list[str] = []
    for card in cards:
        label = card[0] if len(card) > 0 else ""
        value = card[1] if len(card) > 1 else ""
        severity = card[2] if len(card) > 2 else "green"
        desc = card[3] if len(card) > 3 else ""
        desc_html = f'<div class="desc">{H(desc)}</div>' if desc else ""
        chunks.append(
            f'<div class="card {H(severity)}"><div class="lbl">{H(label)}</div>'
            f'<div class="val">{H(value)}</div>{desc_html}</div>'
        )
    return "".join(chunks)


def render_issue_list(issues: Iterable[Mapping[str, object]], empty_text: str = "未发现明显异常") -> str:
    chunks = []
    for issue in issues:
        severity = issue.get("sev", "green")
        title = issue.get("title", "")
        detail = issue.get("detail", "")
        detail_html = f'<div class="d">{H(detail)}</div>' if detail else ""
        chunks.append(f'<div class="issue {H(severity)}"><div class="t">{H(title)}</div>{detail_html}</div>')
    if chunks:
        return "".join(chunks)
    return f'<div class="issue green"><div class="t">{H(empty_text)}</div></div>'


def render_chain(
    nodes: Iterable[Mapping[str, object]],
    *,
    title: str,
    description: str,
) -> str:
    node_list = list(nodes)
    if not node_list:
        return ""
    chunks = []
    for idx, node in enumerate(node_list, 1):
        arrow = '<div class="chain-arrow">↓</div>' if idx < len(node_list) else ""
        chunks.append(
            f'<div class="chain-node chain-{H(node.get("sev", "green"))}">'
            f'<div class="chain-step">{idx}</div>'
            '<div class="chain-body">'
            f'<div class="chain-title">{H(node.get("title", ""))}</div>'
            f'<div class="chain-evi"><b>证据：</b>{H(node.get("evidence", ""))}</div>'
            f'<div class="chain-down"><b>下游影响：</b>{H(node.get("downstream", ""))}</div>'
            f"</div></div>{arrow}"
        )
    return (
        '<div class="section">'
        f"<h2>{H(title)}</h2>"
        f'<p class="muted">{H(description)}</p>'
        f'<div class="chain">{"".join(chunks)}</div>'
        "</div>"
    )


def render_table(rows: Iterable[Sequence[object]], cols: Sequence[object], empty_text: str = "(无)") -> str:
    row_list = list(rows)
    head = "".join(f"<th>{H(col)}</th>" for col in cols)
    if row_list:
        body = "".join("<tr>" + "".join(f"<td>{H(value)}</td>" for value in row) + "</tr>" for row in row_list)
    else:
        body = f'<tr><td colspan="99" class="muted">{H(empty_text)}</td></tr>'
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def render_section(title: str, body_html: str) -> str:
    return f'<div class="section"><h2>{H(title)}</h2>{body_html}</div>'


def render_details(title: str, summary: str, text: str) -> str:
    return (
        f'<div class="section"><h2>{H(title)}</h2>'
        f"<details><summary>{H(summary)}</summary><pre>{H(text)}</pre></details>"
        "</div>"
    )


def render_swimlane(
    *,
    target: str,
    answer: str,
    evidence: Sequence[Mapping[str, object]],
    diagram_kinds: Sequence[str],
    swimlane_rows: Sequence[Sequence[object]] | None = None,
) -> str:
    if diagram_kinds and "swimlane" not in diagram_kinds and "chain" not in diagram_kinds:
        return ""
    if swimlane_rows:
        return render_section(
            "泳道图",
            render_swimlane_rows(swimlane_rows, cols=("泳道", "时序动作", "源码锚点")),
        )
    lanes = [
        ("入口识别", target or "用户问题", "从用户请求中识别源码目标和链路问题。"),
        ("源码定位", _first_evidence_file(evidence) or "待检索", "通过源码调查、索引或 app-server 读取关键定义。"),
        ("分发链路", "调用/回调/Flow", "梳理注册、监听、分发和状态更新节点。"),
        ("消费侧", "下游模块", _short_text(answer, 120) or "等待源码证据确认。"),
    ]
    lane_html = "".join(
        (
            '<div class="lane">'
            f'<div class="lane-title">{H(title)}</div>'
            '<div class="lane-step">'
            f'<div class="name">{H(name)}</div>'
            f'<div class="desc">{H(desc)}</div>'
            '</div></div>'
        )
        for title, name, desc in lanes
    )
    return render_section("泳道图", f'<div class="swimlane">{lane_html}</div>')


def render_flow(
    *,
    diagram_kinds: Sequence[str],
    target: str,
    evidence: Sequence[Mapping[str, object]],
) -> str:
    if diagram_kinds and not any(kind in diagram_kinds for kind in ("sequence", "process", "data_flow", "chain")):
        return ""
    steps = [
        ("目标", target or "未识别", "用户提出的源码/链路对象。"),
        ("证据", _first_evidence_file(evidence) or "未命中", "源码调查返回的首个关键证据。"),
        ("结论", "回答生成", "结合证据和覆盖边界生成报告。"),
    ]
    chunks: list[str] = []
    for idx, (tag, title, note) in enumerate(steps):
        if idx:
            chunks.append('<div class="flow-arrow">→</div>')
        chunks.append(
            '<div class="flow-step">'
            f'<div class="tag">{H(tag)}</div>'
            f'<div class="title">{H(title)}</div>'
            f'<div class="note">{H(note)}</div>'
            '</div>'
        )
    return render_section("流程图", f'<div class="flow-line">{"".join(chunks)}</div>')


def _source_verdict(*, answer: str, evidence: Sequence[Mapping[str, object]], success: bool) -> str:
    if not success:
        return "源码分析失败，未生成可验证结论。"
    if evidence:
        first_line = (answer or "").strip().splitlines()[0] if answer else ""
        return first_line or "源码分析完成，报告已整理关键证据。"
    return "源码分析完成，但证据不足，需要按边界说明继续补证。"


def _source_issues(
    *,
    success: bool,
    evidence: Sequence[Mapping[str, object]],
    coverage_boundary: str,
) -> list[dict[str, object]]:
    if not success:
        return [{"sev": "red", "title": "源码调查失败", "detail": coverage_boundary or "执行后端未返回成功结果。"}]
    if not evidence:
        return [{"sev": "yellow", "title": "证据不足", "detail": coverage_boundary or "未返回源码文件/行号证据。"}]
    return [{"sev": "green", "title": "源码证据已返回", "detail": f"共 {len(evidence)} 条证据，详见源码证据表。"}]


def _chain_nodes_from_evidence(
    *,
    answer: str,
    evidence: Sequence[Mapping[str, object]],
    target: str,
    success: bool,
) -> list[dict[str, object]]:
    if not evidence:
        return [
            {
                "sev": "yellow" if success else "red",
                "title": target or "源码目标",
                "evidence": "未返回可回指源码证据。",
                "downstream": _short_text(answer, 180) or "需要重新检索或补充更明确目标。",
            }
        ]
    nodes: list[dict[str, object]] = []
    for item in evidence[:4]:
        file_name = str(item.get("file") or "源码文件")
        line = str(item.get("line") or "")
        text = str(item.get("text") or "")
        locator = f"{file_name}:{line}" if line else file_name
        nodes.append(
            {
                "sev": "green",
                "title": locator,
                "evidence": text or locator,
                "downstream": "支撑源码链路中的一个关键节点。",
            }
        )
    return nodes


def _evidence_rows(evidence: Sequence[Mapping[str, object]]) -> list[tuple[object, object, object]]:
    return [
        (
            item.get("file", ""),
            item.get("line", ""),
            item.get("text", ""),
        )
        for item in evidence
    ]


def _first_evidence_file(evidence: Sequence[Mapping[str, object]]) -> str:
    if not evidence:
        return ""
    first = evidence[0]
    file_name = str(first.get("file") or "")
    line = str(first.get("line") or "")
    return f"{file_name}:{line}" if file_name and line else file_name


def _diagram_labels(kinds: Sequence[str]) -> list[str]:
    mapping = {
        "swimlane": "泳道图",
        "sequence": "时序图",
        "data_flow": "数据流图",
        "process": "流程图",
        "chain": "链路图",
    }
    return [mapping.get(kind, kind) for kind in kinds]


def _history_text(history: Sequence[Mapping[str, str]]) -> str:
    lines: list[str] = []
    for item in history:
        role = str(item.get("role") or "").strip()
        content = str(item.get("content") or "").strip()
        user = str(item.get("user") or "").strip()
        assistant = str(item.get("assistant") or "").strip()
        if role == "user":
            user = user or content
        elif role == "assistant":
            assistant = assistant or content
        if user:
            lines.append(f"用户：{user}")
        if assistant:
            lines.append(f"助手：{assistant}")
    return "\n".join(lines) or "无历史追问。"


def _short_text(text: str, limit: int) -> str:
    normalized = " ".join((text or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "..."


def _markdown_sections(text: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current = ""
    for raw_line in (text or "").splitlines():
        match = re.match(r"^\s*##\s+(.+?)\s*$", raw_line.rstrip())
        if match:
            current = match.group(1).strip()
            sections.setdefault(current, [])
            continue
        if current:
            sections[current].append(raw_line.rstrip())
    return {key: "\n".join(value).strip() for key, value in sections.items()}


def _markdown_section_entries(section_text: str) -> list[str]:
    entries: list[str] = []
    current: list[str] = []
    list_prefix = r"^(?:[-*]|\d+[.)、])\s+"
    for raw_line in (section_text or "").splitlines():
        stripped = raw_line.strip()
        if not stripped:
            if current:
                entries.append(" ".join(current).strip())
                current = []
            continue
        if stripped.startswith("|") and stripped.endswith("|"):
            continue
        if re.match(list_prefix, stripped):
            if current:
                entries.append(" ".join(current).strip())
            current = [re.sub(list_prefix, "", stripped)]
        else:
            if current:
                current.append(stripped)
            else:
                current = [stripped]
    if current:
        entries.append(" ".join(current).strip())
    return [item for item in entries if item]


def _clean_markdown_inline(text: str) -> str:
    cleaned = text.strip()
    cleaned = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", cleaned)
    cleaned = re.sub(r"`([^`]+)`", r"\1", cleaned)
    cleaned = re.sub(r"\*\*([^*]+)\*\*", r"\1", cleaned)
    cleaned = re.sub(r"\*([^*]+)\*", r"\1", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _markdown_table_block_length(lines: Sequence[str], start: int) -> int:
    if start + 2 >= len(lines):
        return 0
    header = lines[start].strip()
    separator = lines[start + 1].strip()
    if not (header.startswith("|") and header.endswith("|")):
        return 0
    if not re.match(r"^\|?(?:\s*:?-{3,}:?\s*\|)+\s*:?-{3,}:?\s*\|?$", separator):
        return 0
    index = start + 2
    row_count = 0
    while index < len(lines):
        current = lines[index].strip()
        if not (current.startswith("|") and current.endswith("|")):
            break
        row_count += 1
        index += 1
    return index - start if row_count else 0


def _parse_markdown_table_row(raw_line: str) -> list[str]:
    return [_clean_markdown_inline(cell) for cell in raw_line.strip().strip("|").split("|")]


def _markdown_swimlane_rows(section_text: str) -> list[tuple[str, str, str]]:
    lines = (section_text or "").splitlines()
    for index in range(len(lines)):
        block_len = _markdown_table_block_length(lines, index)
        if not block_len:
            continue
        block = lines[index:index + block_len]
        header = _parse_markdown_table_row(block[0])
        if not header or header[0] != "泳道":
            continue
        rows: list[tuple[str, str, str]] = []
        for row_line in block[2:]:
            cells = _parse_markdown_table_row(row_line)
            while len(cells) < 3:
                cells.append("")
            rows.append((cells[0], cells[1], cells[2]))
        return rows
    return []


def _strip_markdown_tables(section_text: str) -> str:
    cleaned: list[str] = []
    lines = (section_text or "").splitlines()
    index = 0
    while index < len(lines):
        block_len = _markdown_table_block_length(lines, index)
        if block_len:
            index += block_len
            continue
        cleaned.append(lines[index].rstrip())
        index += 1
    return "\n".join(cleaned).strip()


def _entry_title_detail(entry: str) -> tuple[str, str]:
    raw = entry.strip()
    patterns = [
        r"^\d+[.)、]?\s*\*\*(.+?)\*\*[：:，,]\s*(.+)$",
        r"^\*\*(.+?)\*\*[：:，,]\s*(.+)$",
        r"^([^：:]{1,24})[：:]\s*(.+)$",
    ]
    for pattern in patterns:
        match = re.match(pattern, raw)
        if match:
            return _clean_markdown_inline(match.group(1)), _clean_markdown_inline(match.group(2))
    return "", _clean_markdown_inline(raw)


def _section_issue_items(section_text: str) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for entry in _markdown_section_entries(section_text):
        title, detail = _entry_title_detail(entry)
        if title:
            items.append({"sev": "green", "title": title, "detail": detail})
        elif detail:
            items.append({"sev": "green", "title": detail, "detail": ""})
    return items


def _markdown_evidence_rows(section_text: str) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    for entry in _markdown_section_entries(section_text):
        detail_text = entry
        anchor_text = ""
        source_match = re.search(r"(?:来源|源码锚点)[：:]\s*(.+)$", entry)
        if source_match:
            detail_text = entry[:source_match.start()].strip()
            anchor_text = _clean_markdown_inline(source_match.group(1))
        title, detail = _entry_title_detail(detail_text)
        rows.append((title or "证据", detail or _clean_markdown_inline(detail_text), anchor_text or "见原始输出"))
    return rows


def _source_boundary_items(coverage_boundary: str, pending_section: str) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    if coverage_boundary.strip():
        items.append({"sev": "yellow", "title": "覆盖边界", "detail": coverage_boundary.strip()})
    for item in _section_issue_items(pending_section):
        item["sev"] = "yellow"
        items.append(item)
    return items


def _final_evidence_rows(
    evidence: Sequence[Mapping[str, object]],
    markdown_rows: Sequence[tuple[str, str, str]],
) -> list[tuple[object, object, object]]:
    if markdown_rows:
        return list(markdown_rows)
    return [
        (
            f'{item.get("file", "")}:{item.get("line", "")}'.strip(":"),
            item.get("text", ""),
            "",
        )
        for item in evidence
    ]
