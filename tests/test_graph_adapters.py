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


def test_timeline_dedupes_and_keeps_key_diagnostic_event():
    # 重复 label(底层注册回调×20)必须去重为单条带计数，
    # 且关键诊断事件 "HMI 侧电量仍为 -1" 不能被挤掉。
    g = _graph()
    events = [e.event for e in g.timeline]
    assert any("HMI" in e and "-1" in e for e in events), f"key event dropped: {events}"
    # 去重：底层注册回调只出现一条（可能带 ×N 计数）
    reg = [e for e in events if "注册底层回调" in e]
    assert len(reg) == 1, f"repeated label not deduped: {reg}"
    assert "×" in reg[0]


def test_helper_node_has_no_bogus_zero_line_anchor():
    g = _graph()
    helper = next((n for n in g.nodes if n.lane == "helper"), None)
    assert helper is not None
    assert all(a.line != 0 for a in helper.anchors)
