"""图优先源码/信号调查报告渲染器：消费 ReportGraph，产出单份 HTML。"""
from __future__ import annotations

import json as _json
from pathlib import Path

from .report_graph import ReportGraph
from .combined_bug_html import render_status_lane_graph
from .source_report_html import H, render_document, BASE_REPORT_CSS

_VERDICT_CLASS = {"ok": "green", "broken": "red", "inconclusive": "yellow"}
_FINDING_CLASS = {"ok": "green", "risk": "red", "todo": "yellow"}

_EXTRA_CSS = (
    ".pill{padding:2px 8px;border-radius:10px;font-size:11px;color:#fff}"
    ".p-ok{background:#16a34a}.p-suspect{background:#f59e0b}"
    ".p-broken{background:#dc2626}.p-unknown{background:#94a3b8}"
    ".logline{font-family:monospace;font-size:12px;color:#334155;margin:2px 0}"
    ".cv-table{width:100%;border-collapse:collapse}"
    ".cv-table td,.cv-table th{border:1px solid #e2e8f0;padding:6px 8px;text-align:left}"
    ".cv-table tr.abn td{color:#dc2626;font-weight:700}"
    ".tl{line-height:1.9}.issue{padding:8px 12px;border-radius:8px;margin:6px 0;background:#f8fafc}"
)


def _evidence_cards(graph: ReportGraph) -> str:
    rows = []
    for idx, n in enumerate(graph.nodes, 1):
        anchors = "、".join(f"{a.file}:{a.line}" for a in n.anchors) or "—"
        logs = "".join(
            f'<div class="logline">[{H(l.ts)}] {H(l.file)}:{l.line} — {H(l.text)}</div>'
            for l in n.logs
        ) or '<div class="muted">无代表性日志行</div>'
        rows.append(
            f'<details class="fold"><summary>#{idx} {H(n.label)} '
            f'<span class="pill p-{H(n.status)}">{H(n.status)}</span></summary>'
            f'<div class="muted">源码锚点：{H(anchors)}</div>{logs}</details>'
        )
    return "".join(rows)


def _timeline(graph: ReportGraph) -> str:
    if not graph.timeline:
        return '<p class="muted">无运行态时间线（仅静态源码生命周期）。</p>'
    items = "".join(f'<li><b>{H(e.t_offset)}</b> {H(e.event)}</li>' for e in graph.timeline)
    return (f'<ul class="tl">{items}</ul>'
            '<p class="muted">注销：本会话未观测到注销/dispose 事件。</p>')


def _value_track(graph: ReportGraph) -> str:
    if not graph.values:
        return '<p class="muted">未采集到值样本。</p>'
    rows = "".join(
        f'<tr class="{"abn" if v.abnormal else ""}"><td>{H(v.ts)}</td>'
        f'<td>{H(v.value)}</td><td>{H(v.source)}</td><td>{H(v.log_ref)}</td></tr>'
        for v in graph.values
    )
    return ('<table class="cv-table"><thead><tr><th>时间</th><th>值</th>'
            '<th>来源</th><th>日志</th></tr></thead><tbody>' + rows + '</tbody></table>')


def _findings(graph: ReportGraph) -> str:
    if not graph.findings:
        return '<p class="muted">无额外风险/待确认项。</p>'
    return "".join(
        f'<div class="issue v-{_FINDING_CLASS.get(f.kind, "yellow")}">'
        f'<b>[{H(f.kind)}]</b> {H(f.title)}</div>'
        for f in graph.findings
    )


def render_signal_source_report(graph: ReportGraph, *, request_text: str, backend: str) -> str:
    sev = _VERDICT_CLASS.get(graph.verdict.status, "yellow")
    node_payload = [
        {
            "id": n.id,
            "lane_title": next((str(l.get("title")) for l in graph.lanes if l.get("id") == n.lane), n.lane),
            "label": n.label,
            "status": n.status,
            "num": i,
        }
        for i, n in enumerate(graph.nodes, 1)
    ]
    edge_payload = [{"from": e.from_, "to": e.to} for e in graph.edges]
    next_step = (
        f'<div class="muted">下一步：{H(graph.verdict.next_step)}</div>'
        if graph.verdict.next_step else ""
    )
    body = (
        '<div class="container">'
        "<h1>源码/信号调查报告</h1>"
        f'<div class="sub">请求：{H(request_text)} · 后端：{H(backend)}</div>'
        f'<div class="verdict v-{sev}">{H(graph.verdict.headline)}</div>{next_step}'
        f'<div class="section"><h2>① 数据流泳道图</h2>{render_status_lane_graph(node_payload, edge_payload)}'
        '<div class="muted">节点描边色：绿=ok 橙=待确认 红=断开。展开看源码锚点与原始日志：</div>'
        f'{_evidence_cards(graph)}</div>'
        f'<div class="section"><h2>② 生命周期时间线</h2>{_timeline(graph)}</div>'
        f'<div class="section"><h2>③ 值变化轨道</h2>{_value_track(graph)}</div>'
        f'<div class="section"><h2>④ 根因判读 / 风险 / 待确认</h2>{_findings(graph)}</div>'
        "</div>"
    )
    return render_document("源码/信号调查报告", body, css=BASE_REPORT_CSS + _EXTRA_CSS)


def build_combined_from_signal_json(
    signal_json_path: Path,
    *,
    request_text: str,
    has_logs: bool,
    backend: str = "signal-chain-analyzer",
) -> tuple[str, dict]:
    """读取 signal 链路脚本 JSON，构造图优先合并报告。

    返回 (html, graph_dict)。graph_dict 适合写成合并报告的 JSON 旁路。
    """
    from .graph_adapters import signal_json_to_graph

    payload = _json.loads(Path(signal_json_path).read_text(encoding="utf-8"))
    graph = signal_json_to_graph(payload)
    graph.has_logs = bool(has_logs)
    html = render_signal_source_report(graph, request_text=request_text, backend=backend)
    return html, graph.to_dict()
