import json
from pathlib import Path

from lark_agent_bridge.reporting.graph_adapters import signal_json_to_graph
from lark_agent_bridge.reporting.report_graph import validate

FIXTURE = Path(__file__).parent / "fixtures" / "signal_chain_40018.json"


def _graph():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return signal_json_to_graph(payload)


def test_graph_is_valid():
    assert validate(_graph()) == []


def test_lanes_in_chain_order():
    g = _graph()
    ids = [lane["id"] for lane in g.lanes]
    assert ids == ["carservice", "helper", "datacenter", "business", "unity"]


def test_datacenter_node_has_real_anchor():
    g = _graph()
    dc = next(n for n in g.nodes if n.lane == "datacenter")
    assert any(a.line == 311 and "DataCenter" in a.file for a in dc.anchors)


def test_values_mark_minus_one_abnormal():
    g = _graph()
    abnormal = [v for v in g.values if v.abnormal]
    assert any(v.value == "-1" for v in abnormal)


def test_denoise_drops_unrelated_signal_refs():
    g = _graph()
    joined = " ".join(f"{a.file}:{a.line}" for n in g.nodes for a in n.anchors)
    assert "SignalMapping.kt:17" not in joined


def test_timeline_has_registration_events():
    g = _graph()
    events = [e.event for e in g.timeline]
    assert any("注册" in e or "注入" in e or "订阅" in e for e in events)
