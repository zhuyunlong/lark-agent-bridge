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
