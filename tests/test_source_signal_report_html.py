from lark_agent_bridge.reporting.combined_bug_html import render_status_lane_graph

NODES = [
    {"id": "carservice", "lane_title": "信号源", "label": "CarVcuManager", "status": "ok", "num": 1},
    {"id": "datacenter", "lane_title": "DataCenter", "label": "dispatchSignal", "status": "suspect", "num": 2},
    {"id": "unity", "lane_title": "Unity", "label": "onHandler", "status": "broken", "num": 3},
]
EDGES = [{"from": "carservice", "to": "datacenter"}, {"from": "datacenter", "to": "unity"}]


def test_lane_graph_emits_svg():
    svg = render_status_lane_graph(NODES, EDGES)
    assert "<svg" in svg and "viewBox" in svg


def test_lane_graph_colors_by_status():
    svg = render_status_lane_graph(NODES, EDGES)
    assert "#16a34a" in svg  # ok
    assert "#f59e0b" in svg  # suspect
    assert "#dc2626" in svg  # broken


def test_lane_graph_has_arrow_between_lanes():
    svg = render_status_lane_graph(NODES, EDGES)
    assert "marker-end=\"url(#laneArrow)\"" in svg


def test_lane_graph_empty():
    assert "muted" in render_status_lane_graph([], [])


import json
from pathlib import Path
from lark_agent_bridge.reporting.graph_adapters import signal_json_to_graph
from lark_agent_bridge.reporting.source_signal_report_html import render_signal_source_report

FIXTURE = Path(__file__).parent / "fixtures" / "signal_chain_40018.json"


def _html():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    graph = signal_json_to_graph(payload)
    return render_signal_source_report(graph, request_text="调查电量信号", backend="signal-chain-analyzer")


def test_report_has_swimlane_and_sections():
    html = _html()
    assert "<svg" in html
    assert "生命周期" in html and "值变化" in html


def test_report_exposes_file_line_and_raw_log():
    html = _html()
    assert "DataCenter.kt:311" in html
    # 原始日志原文/值证据应出现，不再只有中文 label
    assert ("hasProvider" in html) or ("value" in html) or ("95" in html)


def test_report_no_dead_swimlane_css_unused():
    # body 里必须真的用到 svg（不是只在 style 里定义）
    html = _html()
    body = html.split("</style>")[-1]
    assert "<svg" in body


def test_report_marks_abnormal_value():
    html = _html()
    assert "-1" in html


def test_build_combined_from_signal_json(tmp_path):
    import shutil
    from lark_agent_bridge.reporting.source_signal_report_html import build_combined_from_signal_json
    src = Path(__file__).parent / "fixtures" / "signal_chain_40018.json"
    dst = tmp_path / "bug_signal_chain_report.json"
    shutil.copy(src, dst)
    html, graph_dict = build_combined_from_signal_json(dst, request_text="调查电量信号", has_logs=True)
    assert "<svg" in html.split("</style>")[-1]
    assert graph_dict["has_logs"] is True
    assert graph_dict["verdict"]["status"] in {"ok", "broken", "inconclusive"}
    assert any(n["lane"] == "datacenter" for n in graph_dict["nodes"])
