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


def test_build_combined_threads_intent(tmp_path):
    import shutil
    from pathlib import Path as _P
    from lark_agent_bridge.reporting.source_signal_report_html import build_combined_from_signal_json
    src = _P(__file__).parent / "fixtures" / "signal_chain_40018.json"
    dst = tmp_path / "bug_signal_chain_report.json"
    shutil.copy(src, dst)
    _, gd_consult = build_combined_from_signal_json(dst, request_text="x", has_logs=True, intent="consult")
    assert gd_consult["intent"] == "consult"
    _, gd_default = build_combined_from_signal_json(dst, request_text="x", has_logs=True)
    assert gd_default["intent"] == "diagnose"  # adapter default; empty intent must NOT override


def _render_with(intent, has_logs):
    import json
    from pathlib import Path as _P
    from lark_agent_bridge.reporting.graph_adapters import signal_json_to_graph
    from lark_agent_bridge.reporting.source_signal_report_html import render_signal_source_report
    g = signal_json_to_graph(json.load(open(_P(__file__).parent / "fixtures" / "signal_chain_40018.json")))
    g.intent = intent
    g.has_logs = has_logs
    return render_signal_source_report(g, request_text="x", backend="y")


def test_diagnose_puts_rootcause_before_lifecycle():
    html = _render_with("diagnose", True)
    assert html.index("根因判读") < html.index("生命周期")


def test_consult_puts_lifecycle_before_rootcause():
    html = _render_with("consult", True)
    assert html.index("生命周期") < html.index("根因判读")


def test_verdict_labels_intent():
    assert "故障诊断" in _render_with("diagnose", True)
    assert "链路咨询" in _render_with("consult", True)


def test_no_logs_annotates_verdict():
    assert "未结合运行态" in _render_with("consult", False)


def test_consult_with_logs_gets_consult_emphasis():
    # decision-3b: 带日志的咨询仍是咨询侧重（劫持修复的可观察结果），无路由改动。
    html = _render_with("consult", True)
    assert "链路咨询" in html
    assert html.index("生命周期") < html.index("根因判读")
