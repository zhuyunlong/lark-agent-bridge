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
       margin: 0; min-height: 100vh; overflow-wrap: break-word; background:
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
.card.card-compact .val { font-size: 15px; line-height: 1.45; font-weight: 750; color: var(--text); word-break: normal; overflow-wrap: anywhere; }
.card.card-compact .desc { font-size: 11px; line-height: 1.55; word-break: normal; overflow-wrap: anywhere; }
.card.red .val { color: var(--red); }
.card.yellow .val { color: var(--yellow); }
.card.green .val { color: var(--green); }
.card.card-compact.red .val { color: var(--red); }
.card.card-compact.yellow .val { color: var(--yellow); }
.card.card-compact.green .val { color: var(--green); }
.section { background: linear-gradient(180deg, rgba(255,255,255,.96), rgba(250,252,255,.96)); border-radius: 16px; padding: 22px 24px; margin-bottom: 18px;
           box-shadow: 0 12px 30px rgba(15,23,42,.07); border: 1px solid var(--border); overflow-x: auto; }
.section h2 { margin: 0 0 14px; font-size: 18px; border-left: 4px solid var(--blue); padding-left: 10px; }
.issue { padding: 12px 14px; border-radius: 10px; margin-bottom: 10px; border-left: 4px solid #999; background: #f9fafb; }
.issue.red { border-color: var(--red); background: linear-gradient(90deg, rgba(220,38,38,.10), rgba(254,242,242,.86)); }
.issue.yellow { border-color: var(--yellow); background: linear-gradient(90deg, rgba(217,119,6,.10), rgba(255,251,235,.86)); }
.issue.green { border-color: var(--green); background: linear-gradient(90deg, rgba(22,163,74,.10), rgba(236,253,245,.86)); }
.issue .t { font-weight: 600; }
.issue .d { color: var(--muted); font-size: 12px; margin-top: 4px; line-height: 1.6; }
table { width: 100%; min-width: 980px; border-collapse: collapse; font-size: 13px; border-radius: 12px; overflow: hidden; table-layout: auto; }
th, td { padding: 9px 11px; border-bottom: 1px solid #eef0f3; text-align: left; vertical-align: top; word-break: normal; overflow-wrap: break-word; }
th:first-child, td:first-child { white-space: nowrap; }
th:last-child, td:last-child { min-width: 360px; }
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
.swimlane-svg-wrap { overflow-x: auto; padding-bottom: 4px; }
.swimlane-svg { display: block; width: 100%; min-width: 1024px; height: auto; }
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
        value_text, desc_text = _display_card_value(value, desc)
        classes = f"card {H(severity)}"
        if _is_compact_card_value(value_text, desc_text):
            classes += " card-compact"
        desc_html = f'<div class="desc">{H(desc_text)}</div>' if desc_text else ""
        chunks.append(
            f'<div class="{classes}"><div class="lbl">{H(label)}</div>'
            f'<div class="val">{H(value_text)}</div>'
            f"{desc_html}</div>"
        )
    return "".join(chunks)


def _display_card_value(value: object, desc: object) -> tuple[str, str]:
    value_text = str(value) if value is not None else ""
    desc_text = str(desc) if desc is not None else ""
    if desc_text.strip():
        return value_text, desc_text
    if _looks_like_path(value_text) and len(value_text) > 42:
        leaf = value_text.rstrip("/\\").replace("\\", "/").rsplit("/", 1)[-1]
        if leaf:
            return leaf, value_text
    return value_text, desc_text


def _looks_like_path(text: str) -> bool:
    stripped = text.strip()
    return (
        stripped.startswith(("/", "./", "../", "~/", "http://", "https://"))
        or "\\" in stripped
        or ("/" in stripped and " " not in stripped)
    )


def _is_compact_card_value(value: str, desc: str) -> bool:
    text = value.strip()
    if len(text) > 18:
        return True
    if _looks_like_path(desc):
        return True
    if text.count(".") >= 2:
        return True
    return any(len(token) > 18 for token in text.replace("/", " ").split())


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


def render_swimlane_rows(
    rows: Iterable[Sequence[object]],
    cols: Sequence[object] | None = None,
    empty_text: str = "未提取到结构化泳道节点。",
) -> str:
    row_list = [tuple(row) for row in rows if any(str(value).strip() for value in row)]
    if not row_list:
        return f'<p class="muted">{H(empty_text)}</p>'
    lane_label = str(cols[0]) if cols and len(cols) > 0 else "泳道"
    action_label = str(cols[1]) if cols and len(cols) > 1 else "时序动作"
    anchor_label = str(cols[2]) if cols and len(cols) > 2 else "源码锚点"
    outer_x = 28
    outer_y = 18
    lane_w = 250
    lane_gap = 20
    header_h = 54
    card_y = 108
    card_padding = 16
    step_r = 16
    line_h = 18
    total_w = outer_x * 2 + len(row_list) * lane_w + max(0, len(row_list) - 1) * lane_gap

    layout_rows: list[dict[str, object]] = []
    max_card_h = 0
    for index, row in enumerate(row_list, 1):
        lane_name = str(row[0]) if len(row) > 0 else ""
        action_text = str(row[1]) if len(row) > 1 else ""
        anchor_text = str(row[2]) if len(row) > 2 else ""
        lane_lines = _wrap_svg_text(lane_name, 12)
        action_lines = _wrap_svg_text(action_text, 20)
        anchor_lines = _wrap_svg_text(anchor_text, 22)
        card_h = max(138, 66 + len(action_lines) * 18 + len(anchor_lines) * 16)
        max_card_h = max(max_card_h, card_h)
        layout_rows.append(
            {
                "index": index,
                "lane_lines": lane_lines,
                "action_lines": action_lines,
                "anchor_lines": anchor_lines,
            }
        )
    total_h = card_y + max_card_h + 44
    chunks: list[str] = [
        '<div class="swimlane-svg-wrap">',
        (
            f'<svg class="swimlane-svg" viewBox="0 0 {total_w} {total_h}" '
            'xmlns="http://www.w3.org/2000/svg" role="img" aria-label="泳道图">'
        ),
        "<defs>",
        '<linearGradient id="laneHeader" x1="0" y1="0" x2="1" y2="0">',
        '<stop offset="0%" stop-color="#2563eb" stop-opacity="0.18"/>',
        '<stop offset="100%" stop-color="#0891b2" stop-opacity="0.08"/>',
        "</linearGradient>",
        '<marker id="laneArrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">',
        '<path d="M 0 0 L 10 5 L 0 10 z" fill="#7c8db0"/>',
        "</marker>",
        "</defs>",
        f'<rect x="{outer_x}" y="{outer_y}" width="{total_w - outer_x * 2}" height="{header_h}" rx="18" fill="#e9eef5" stroke="#d8e1f4"/>',
    ]
    for idx, row in enumerate(layout_rows):
        lane_x = outer_x + idx * (lane_w + lane_gap)
        if idx:
            separator_x = lane_x - lane_gap / 2
            chunks.append(
                f'<line x1="{separator_x}" y1="{outer_y + 10}" x2="{separator_x}" y2="{outer_y + header_h - 10}" stroke="#d1d9e6"/>'
            )
        lane_lines = row["lane_lines"]
        lane_text_y = outer_y + 28 - (len(lane_lines) - 1) * 8
        chunks.extend(
            _svg_text_block(
                lane_x + lane_w / 2,
                lane_text_y,
                lane_lines,
                fill="#111827",
                font_size=13,
                font_weight=700,
                line_height=16,
                anchor="middle",
            )
        )

    arrow_y = card_y + max_card_h / 2
    for idx, row in enumerate(layout_rows):
        lane_x = outer_x + idx * (lane_w + lane_gap)
        lane_lines = row["lane_lines"]
        action_lines = row["action_lines"]
        anchor_lines = row["anchor_lines"]
        chunks.append(
            f'<rect x="{lane_x}" y="{card_y}" width="{lane_w}" height="{max_card_h}" rx="18" fill="#ffffff" stroke="#94a3b8" stroke-width="1.5"/>'
        )
        chunks.append(
            f'<circle cx="{lane_x + 28}" cy="{card_y + 28}" r="{step_r}" fill="#2563eb"/>'
        )
        chunks.append(
            f'<text x="{lane_x + 28}" y="{card_y + 32}" text-anchor="middle" fill="#ffffff" font-size="12" font-weight="700">{row["index"]}</text>'
        )
        chunks.append(
            f'<text x="{lane_x + 54}" y="{card_y + 31}" fill="#7c3aed" font-size="11" font-weight="700">{H(action_label)}</text>'
        )
        chunks.extend(
            _svg_text_block(
                lane_x + card_padding,
                card_y + 62,
                action_lines,
                fill="#334155",
                font_size=13,
                font_weight=600,
            )
        )
        anchor_label_y = card_y + max_card_h - 38 - max(0, len(anchor_lines) - 1) * 16
        chunks.append(
            f'<line x1="{lane_x + 16}" y1="{anchor_label_y - 10}" x2="{lane_x + lane_w - 16}" y2="{anchor_label_y - 10}" stroke="#dbe4f0"/>'
        )
        chunks.append(
            f'<text x="{lane_x + 16}" y="{anchor_label_y}" fill="#1d4ed8" font-size="11" font-weight="700">{H(anchor_label)}</text>'
        )
        chunks.extend(
            _svg_text_block(
                lane_x + 16,
                anchor_label_y + 20,
                anchor_lines,
                fill="#2563eb",
                font_size=12,
                font_weight=500,
                line_height=16,
            )
        )
        if idx < len(layout_rows) - 1:
            start_x = lane_x + lane_w
            end_x = lane_x + lane_w + lane_gap
            chunks.append(
                f'<line x1="{start_x}" y1="{arrow_y}" x2="{end_x - 6}" y2="{arrow_y}" stroke="#94a3b8" stroke-width="2" marker-end="url(#laneArrow)"/>'
            )
    chunks.extend(["</svg>", "</div>"])
    return "".join(chunks)


_STATUS_FILL = {
    "ok": "#16a34a",
    "suspect": "#f59e0b",
    "broken": "#dc2626",
    "unknown": "#94a3b8",
}


def render_status_lane_graph(
    nodes: Sequence[Mapping[str, object]],
    edges: Sequence[Mapping[str, object]],
    *,
    empty_text: str = "未提取到链路节点。",
) -> str:
    node_list = [dict(n) for n in nodes if str(n.get("label", "")).strip()]
    if not node_list:
        return f'<p class="muted">{H(empty_text)}</p>'
    outer_x = 28
    lane_w, lane_gap = 220, 28
    card_y, card_h = 70, 132
    total_w = outer_x * 2 + len(node_list) * lane_w + max(0, len(node_list) - 1) * lane_gap
    total_h = card_y + card_h + 36
    pos = {n["id"]: outer_x + i * (lane_w + lane_gap) for i, n in enumerate(node_list)}

    chunks: list[str] = [
        '<div class="swimlane-svg-wrap">',
        (f'<svg class="swimlane-svg" viewBox="0 0 {total_w} {total_h}" '
         'xmlns="http://www.w3.org/2000/svg" role="img" aria-label="链路泳道图">'),
        '<defs><marker id="laneArrow" viewBox="0 0 10 10" refX="8" refY="5" '
        'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        '<path d="M 0 0 L 10 5 L 0 10 z" fill="#7c8db0"/></marker></defs>',
    ]
    arrow_y = card_y + card_h / 2
    for edge in edges:
        a, b = pos.get(str(edge.get("from"))), pos.get(str(edge.get("to")))
        if a is None or b is None or b <= a:
            continue
        start_x, end_x = a + lane_w, b
        chunks.append(
            f'<line x1="{start_x}" y1="{arrow_y}" x2="{end_x - 6}" y2="{arrow_y}" '
            'stroke="#94a3b8" stroke-width="2" marker-end="url(#laneArrow)"/>'
        )
    for n in node_list:
        x = pos[n["id"]]
        fill = _STATUS_FILL.get(str(n.get("status", "unknown")), "#94a3b8")
        lane_lines = _wrap_svg_text(str(n.get("lane_title", "")), 14)
        label_lines = _wrap_svg_text(str(n.get("label", "")), 18)
        chunks.append(
            f'<rect x="{x}" y="{card_y}" width="{lane_w}" height="{card_h}" rx="16" '
            f'fill="#ffffff" stroke="{fill}" stroke-width="2.5"/>'
        )
        chunks.append(f'<circle cx="{x + 26}" cy="{card_y + 26}" r="14" fill="{fill}"/>')
        chunks.append(
            f'<text x="{x + 26}" y="{card_y + 30}" text-anchor="middle" fill="#fff" '
            f'font-size="12" font-weight="700">{H(str(n.get("num", "")))}</text>'
        )
        chunks.extend(_svg_text_block(x + 50, card_y + 24, lane_lines, fill="#64748b",
                                      font_size=11, font_weight=700, line_height=14))
        chunks.extend(_svg_text_block(x + 16, card_y + 70, label_lines, fill="#1f2937",
                                      font_size=13, font_weight=600, line_height=18))
    chunks.extend(["</svg>", "</div>"])
    return "".join(chunks)


def _svg_text_block(
    x: int,
    y: int,
    lines: Sequence[str],
    *,
    fill: str,
    font_size: int,
    font_weight: int,
    line_height: int = 18,
    anchor: str = "start",
) -> list[str]:
    chunks = [
        (
            f'<text x="{x}" y="{y}" fill="{fill}" font-size="{font_size}" '
            f'font-weight="{font_weight}" text-anchor="{anchor}" '
            f'font-family="-apple-system, Helvetica Neue, PingFang SC, Microsoft YaHei, sans-serif">'
        )
    ]
    for index, line in enumerate(lines):
        dy = y + index * line_height
        chunks.append(f'<tspan x="{x}" y="{dy}">{H(line)}</tspan>')
    chunks.append("</text>")
    return chunks


def _wrap_svg_text(text: str, max_units: int) -> list[str]:
    normalized = " ".join(str(text or "").split())
    if not normalized:
        return [""]
    lines: list[str] = []
    current = ""
    for token in normalized.split(" "):
        pieces = _split_svg_token(token, max_units)
        for piece in pieces:
            if not current:
                current = piece
                continue
            candidate = f"{current} {piece}"
            if _svg_text_units(candidate) <= max_units:
                current = candidate
                continue
            lines.append(current)
            current = piece
    if current:
        lines.append(current)
    return lines or [normalized]


def _split_svg_token(token: str, max_units: int) -> list[str]:
    if _svg_text_units(token) <= max_units:
        return [token]
    pieces: list[str] = []
    current: list[str] = []
    current_units = 0
    for char in token:
        units = 1 if ord(char) < 128 else 2
        if current and current_units + units > max_units:
            pieces.append("".join(current))
            current = []
            current_units = 0
        current.append(char)
        current_units += units
    if current:
        pieces.append("".join(current))
    return pieces or [token]


def _svg_text_units(text: str) -> int:
    return sum(1 if ord(char) < 128 else 2 for char in text)


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
        elif kind == "swimlane":
            body_html = render_swimlane_rows(
                section.get("rows", []),
                cols=section.get("cols", []),
                empty_text=empty_text or "未提取到结构化泳道节点。",
            )
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
