import json
from pathlib import Path
from lark_agent_bridge.reporting.graph_adapters import signal_json_to_graph, apply_source_stage
from lark_agent_bridge.reporting.report_graph import validate

FIX = Path(__file__).parent / "fixtures" / "signal_chain_40018.json"


def _graph():
    return signal_json_to_graph(json.load(open(FIX)))


def test_apply_overrides_status_by_filename():
    g = _graph()
    apply_source_stage(g, {"node_status": {"DataCenter.kt": "broken"}, "findings": []})
    dc = next(n for n in g.nodes if n.lane == "datacenter")
    assert dc.status == "broken"
    assert validate(g) == []


def test_apply_injects_findings():
    g = _graph()
    before = len(g.findings)
    apply_source_stage(g, {"node_status": {}, "findings": [{"file": "x", "severity": "warn", "title": "断点可疑"}]})
    assert len(g.findings) == before + 1
    assert any("断点可疑" in f.title for f in g.findings)


def test_apply_none_keeps_deterministic_status():
    g = _graph()
    statuses = [(n.id, n.status) for n in g.nodes]
    apply_source_stage(g, None)
    assert [(n.id, n.status) for n in g.nodes] == statuses


def test_apply_illegal_status_is_skipped():
    g = _graph()
    dc = next(n for n in g.nodes if n.lane == "datacenter")
    orig = dc.status
    apply_source_stage(g, {"node_status": {"DataCenter.kt": "正常"}, "findings": []})
    assert dc.status == orig
    assert validate(g) == []


def test_apply_overrides_verdict():
    g = _graph()
    apply_source_stage(g, {"node_status": {}, "findings": [], "verdict": {
        "status": "ok", "headline": "电量链路正常、非3D断点", "next_step": "转查 X3D ready"}})
    assert g.verdict.headline == "电量链路正常、非3D断点"
    assert g.verdict.next_step == "转查 X3D ready"
    assert g.verdict.status == "ok"
    assert validate(g) == []


def test_apply_verdict_illegal_status_kept():
    g = _graph()
    orig = g.verdict.status
    apply_source_stage(g, {"verdict": {"status": "正常", "headline": "X"}})
    assert g.verdict.status == orig      # 非法 status 跳过
    assert g.verdict.headline == "X"     # headline 仍覆盖


def test_apply_no_verdict_keeps_original():
    g = _graph()
    orig = g.verdict.headline
    apply_source_stage(g, {"node_status": {}, "findings": []})
    assert g.verdict.headline == orig
