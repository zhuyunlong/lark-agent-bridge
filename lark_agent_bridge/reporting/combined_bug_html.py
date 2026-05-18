"""HTML primitives for bridge-owned combined bug reports.

This module intentionally lives in the bridge package, not under a skill
directory. Individual skills should own their own HTML style; the bridge only
uses this renderer for reports it composes itself.
"""

from __future__ import annotations

import html
from typing import Iterable, Mapping, Sequence


BASE_REPORT_CSS = """
* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
:root{
  --bg:#eef3ff;
  --panel:#ffffff;
  --panel-2:#f8fbff;
  --text:#14213d;
  --muted:#5f6b85;
  --border:#d8e1f4;
  --blue:#2563eb;
  --cyan:#0891b2;
  --purple:#7c3aed;
  --green:#16a34a;
  --yellow:#d97706;
  --red:#dc2626;
}
body { font-family: -apple-system, "Helvetica Neue", "PingFang SC", "Microsoft YaHei", sans-serif;
       margin: 0; min-height: 100vh; overflow-wrap: anywhere; background:
       radial-gradient(circle at top left, rgba(37,99,235,.10), transparent 28%),
       radial-gradient(circle at top right, rgba(124,58,237,.10), transparent 26%),
       linear-gradient(180deg, #f4f7ff 0%, var(--bg) 100%);
       color: var(--text); }
body::before { content: ""; display: block; height: 5px; background: linear-gradient(90deg, var(--blue), var(--cyan), var(--green), var(--yellow)); }
.container { max-width: 1320px; margin: 0 auto; padding: 30px 24px 44px; }
h1 { margin: 0 0 8px; font-size: 30px; line-height: 1.25; letter-spacing: 0; }
.sub { max-width: 980px; color: var(--muted); margin-bottom: 24px; font-size: 13px; line-height: 1.7; }
.verdict { padding: 22px 24px; border-radius: 18px; color: #fff; font-size: 18px; font-weight: 700;
           box-shadow: 0 18px 48px rgba(37,99,235,.18); margin-bottom: 24px; line-height: 1.6;
           border: 1px solid rgba(255,255,255,.25); }
.v-red { background: linear-gradient(135deg, #ef4444, #b91c1c 60%, #7f1d1d); }
.v-yellow { background: linear-gradient(135deg, #f59e0b, #d97706 60%, #9a3412); }
.v-green { background: linear-gradient(135deg, #10b981, #059669 60%, #047857); }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 14px; margin-bottom: 24px; }
.card { background: linear-gradient(180deg, rgba(255,255,255,.96), rgba(248,251,255,.96)); border-radius: 16px; padding: 16px 18px;
        box-shadow: 0 10px 28px rgba(15,23,42,.08); border: 1px solid var(--border); }
.card .lbl { font-size: 12px; font-weight: 700; letter-spacing: .02em; color: var(--muted); }
.card .val { font-size: 24px; font-weight: 800; margin-top: 6px; overflow-wrap: anywhere; }
.card .desc { color: var(--muted); font-size: 12px; line-height: 1.6; margin-top: 8px; }
.card.red .val { color: var(--red); }
.card.yellow .val { color: var(--yellow); }
.card.green .val { color: var(--green); }
.section { background: linear-gradient(180deg, rgba(255,255,255,.96), rgba(250,252,255,.96)); border-radius: 16px; padding: 22px 24px; margin-bottom: 18px;
           box-shadow: 0 12px 30px rgba(15,23,42,.07); border: 1px solid var(--border); overflow-x: auto; }
.section h2 { margin: 0 0 14px; font-size: 18px; border-left: 4px solid var(--blue); padding-left: 10px; }
.issue { padding: 12px 14px; border-radius: 10px; margin-bottom: 10px; border-left: 4px solid #999; background: #f9fafb; }
.issue.red { border-color: var(--red); background: linear-gradient(90deg, rgba(220,38,38,.10), rgba(254,242,242,.86)); }
.issue.yellow { border-color: var(--yellow); background: linear-gradient(90deg, rgba(217,119,6,.10), rgba(255,251,235,.86)); }
.issue.green { border-color: var(--green); background: linear-gradient(90deg, rgba(22,163,74,.10), rgba(236,253,245,.86)); }
.issue .t { font-weight: 600; }
.issue .d { color: var(--muted); font-size: 12px; margin-top: 4px; line-height: 1.6; }
table { width: 100%; min-width: 720px; border-collapse: collapse; font-size: 13px; border-radius: 12px; overflow: hidden; }
th, td { padding: 9px 11px; border-bottom: 1px solid #eef0f3; text-align: left; vertical-align: top; }
th { background: linear-gradient(180deg, rgba(37,99,235,.12), rgba(8,145,178,.06)); font-weight: 700; color: var(--blue); }
tbody tr:nth-child(even) td { background: rgba(248,251,255,.72); }
code { background: rgba(37,99,235,.10); padding: 1px 6px; border-radius: 999px; font-size: 12px; color: var(--blue); }
.muted { color: #8591ab; }
.chain { display: flex; flex-direction: column; align-items: stretch; gap: 0; }
.chain-node { display: flex; align-items: stretch; background: var(--panel); border-radius: 14px;
              border-left: 6px solid #999; box-shadow: 0 10px 24px rgba(15,23,42,.08); overflow: hidden; border: 1px solid var(--border); }
.chain-node.chain-red { border-color: var(--red); }
.chain-node.chain-yellow { border-color: var(--yellow); }
.chain-node.chain-green { border-color: var(--green); }
.chain-step { width: 40px; min-width: 40px; display: flex; align-items: center; justify-content: center;
              font-size: 16px; font-weight: 800; color: #fff; background: #6b7280; }
.chain-red .chain-step { background: linear-gradient(180deg, #ef4444, #b91c1c); }
.chain-yellow .chain-step { background: linear-gradient(180deg, #f59e0b, #b45309); }
.chain-green .chain-step { background: linear-gradient(180deg, #10b981, #047857); }
.chain-body { padding: 12px 16px; flex: 1; }
.chain-title { font-weight: 700; font-size: 15px; margin-bottom: 4px; }
.chain-evi, .chain-down { font-size: 13px; color: #46526d; line-height: 1.6; margin-top: 2px; }
.chain-arrow { text-align: center; font-size: 22px; color: #93a0bc; line-height: 1.1; padding: 6px 0; font-weight: 700; }
.pill-row { display: flex; flex-wrap: wrap; gap: 8px; }
.pill { display: inline-flex; align-items: center; gap: 6px; padding: 6px 10px; border-radius: 999px;
        background: linear-gradient(180deg, rgba(37,99,235,.10), rgba(124,58,237,.08)); color: var(--blue); font-size: 12px; font-weight: 700; border: 1px solid rgba(37,99,235,.14); }
.flow-line { display: flex; align-items: stretch; gap: 10px; overflow-x: auto; padding-bottom: 2px; }
.flow-step { min-width: 250px; background: linear-gradient(180deg, rgba(248,250,255,.98), rgba(237,244,255,.96)); border: 1px solid var(--border); border-radius: 14px; padding: 14px; box-shadow: inset 0 1px 0 rgba(255,255,255,.6); }
.flow-step .flow-tag { font-size: 11px; color: var(--purple); margin-bottom: 6px; font-weight: 700; letter-spacing: .04em; }
.flow-step .flow-title { font-size: 15px; font-weight: 800; color: #10213d; }
.flow-step .flow-meta { font-size: 12px; color: #42506c; margin-top: 6px; line-height: 1.6; }
.flow-step .flow-note { font-size: 12px; color: var(--muted); margin-top: 8px; line-height: 1.6; }
.flow-arrow-inline { display: flex; align-items: center; justify-content: center; min-width: 28px;
                     color: #7c8db0; font-size: 24px; font-weight: 700; }
.insight { padding: 14px 16px; border-radius: 12px; border-left: 4px solid var(--blue); background: linear-gradient(90deg, rgba(37,99,235,.12), rgba(124,58,237,.06));
           color: #1d3d87; line-height: 1.7; box-shadow: inset 0 1px 0 rgba(255,255,255,.5); }
.split-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 14px; }
details.fold { margin-top: 12px; border: 1px solid rgba(148,163,184,.28); border-radius: 12px; padding: 10px 12px; background: rgba(248,250,252,.74); }
details.fold summary { cursor: pointer; color: var(--blue); font-weight: 700; }
@media (max-width: 880px) {
  h1 { font-size: 24px; }
  .cards { grid-template-columns: 1fr; }
  .container { padding: 14px; }
  table { min-width: 640px; }
}
"""


def H(value: object) -> str:
    return html.escape(str(value) if value is not None else "")


def render_document(title: str, body_html: str, css: str = BASE_REPORT_CSS) -> str:
    return (
        '<!DOCTYPE html>\n'
        '<html lang="zh-CN">\n'
        '<head>\n'
        '  <meta charset="utf-8">\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"  <title>{H(title)}</title>\n"
        f"  <style>{css}</style>\n"
        '</head>\n'
        '<body>\n'
        f"{body_html}\n"
        '</body>\n'
        '</html>\n'
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
    title: str = "根因链分析",
    description: str = "按系统层、应用层和用户表现组织证据链。",
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
            f'</div></div>{arrow}'
        )
    return (
        '<div class="section">'
        f'<h2>{H(title)}</h2>'
        f'<p class="muted">{description}</p>'
        f'<div class="chain">{"".join(chunks)}</div>'
        '</div>'
    )


def render_table(rows: Iterable[Sequence[object]], cols: Sequence[object], empty_text: str = "(无)") -> str:
    row_list = list(rows)
    head = "".join(f"<th>{H(col)}</th>" for col in cols)
    if row_list:
        body = "".join("<tr>" + "".join(f"<td>{H(value)}</td>" for value in row) + "</tr>" for row in row_list)
    else:
        body = f'<tr><td colspan="99" class="muted">{H(empty_text)}</td></tr>'
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def render_flow_line(nodes: Iterable[Mapping[str, object]], empty_text: str = "未提取到可视化链路节点。") -> str:
    node_list = list(nodes)
    if not node_list:
        return f'<p class="muted">{H(empty_text)}</p>'
    chunks: list[str] = ['<div class="flow-line">']
    for index, node in enumerate(node_list):
        if index:
            chunks.append('<div class="flow-arrow-inline">→</div>')
        chunks.append(
            '<div class="flow-step">'
            f'<div class="flow-tag">{H(node.get("tag", ""))}</div>'
            f'<div class="flow-title">{H(node.get("title", ""))}</div>'
            f'<div class="flow-meta">{H(node.get("meta", ""))}</div>'
            f'<div class="flow-note">{H(node.get("note", ""))}</div>'
            '</div>'
        )
    chunks.append("</div>")
    return "".join(chunks)


def render_section(title: str, body_html: str, description: str = "") -> str:
    desc_html = f'<p class="muted">{H(description)}</p>' if description else ""
    return f'<div class="section"><h2>{H(title)}</h2>{desc_html}{body_html}</div>'


def render_details(summary: str, body_html: str) -> str:
    return (
        '<details class="fold">'
        f'<summary>{H(summary)}</summary>'
        f"{body_html}"
        "</details>"
    )


def render_sections(sections: Iterable[Mapping[str, object]]) -> str:
    rendered: list[str] = []
    for section in sections:
        kind = str(section.get("kind") or "custom")
        title = str(section.get("title") or "")
        description = str(section.get("description") or "")
        empty_text = str(section.get("empty_text") or "")
        body_html = ""
        if kind == "issues":
            body_html = render_issue_list(section.get("items", []), empty_text or "未发现明显异常")
        elif kind == "chain":
            nodes = section.get("nodes", [])
            if nodes:
                rendered.append(
                    render_chain(
                        nodes,
                        title=title or "根因链分析",
                        description=description or "按系统层、应用层和用户表现组织证据链。",
                    )
                )
                continue
            body_html = f'<p class="muted">{H(empty_text or "未提取到链路节点")}</p>'
        elif kind == "table":
            body_html = render_table(
                section.get("rows", []),
                section.get("cols", []),
                empty_text=empty_text or "(无)",
            )
        elif kind == "flow":
            body_html = render_flow_line(section.get("nodes", []), empty_text=empty_text or "未提取到可视化链路节点。")
        elif kind == "text":
            text = str(section.get("text") or "")
            css_class = str(section.get("class_name") or "insight")
            body_html = f'<div class="{H(css_class)}">{H(text)}</div>'
        elif kind == "details":
            summary = str(section.get("summary") or "展开查看")
            inner_html = str(section.get("body_html") or "")
            body_html = render_details(summary, inner_html)
        else:
            body_html = str(section.get("body_html") or "")
        rendered.append(render_section(title, body_html, description=description))
    return "".join(rendered)


def render_report_shell(
    *,
    title: str,
    heading: str,
    subtitle: str = "",
    verdict: Mapping[str, object] | None = None,
    cards: Iterable[Sequence[object]] = (),
    sections: Iterable[Mapping[str, object]] = (),
) -> str:
    subtitle_html = f'<div class="sub">{subtitle}</div>' if subtitle else ""
    verdict = verdict or {}
    verdict_html = ""
    verdict_text = str(verdict.get("text") or "")
    if verdict_text:
        severity = str(verdict.get("sev") or "green")
        verdict_html = f'<div class="verdict v-{H(severity)}">{H(verdict_text)}</div>'
    cards_list = list(cards)
    cards_html = f'<div class="cards">{render_cards(cards_list)}</div>' if cards_list else ""
    body_html = (
        '<div class="container">'
        f'<h1>{H(heading)}</h1>'
        f"{subtitle_html}"
        f"{verdict_html}"
        f"{cards_html}"
        f"{render_sections(sections)}"
        '</div>'
    )
    return render_document(title, body_html)
