from lark_agent_bridge.reporting.report_graph import (
    ReportGraph, GraphNode, GraphEdge, Verdict, Anchor, LogRef,
    TimelineEvent, ValueSample, Finding, validate,
)


def test_minimal_graph_validates_clean():
    g = ReportGraph(
        intent="diagnose",
        has_logs=True,
        verdict=Verdict(status="ok", headline="链路正常", next_step="转查 X3D ready"),
        lanes=[{"id": "datacenter", "title": "DataCenter"}],
        nodes=[GraphNode(id="dc", lane="datacenter", label="dispatchSignal",
                         status="ok", anchors=[Anchor(file="DataCenter.kt", line=311)],
                         logs=[LogRef(ts="16:37:35", file="main.alog.log", line=2982, text="hasProvider=true")],
                         note="")],
        edges=[GraphEdge(**{"from": "src", "to": "dc", "kind": "dispatch", "status": "ok", "note": ""})],
        timeline=[TimelineEvent(t_offset="+2.2s", event="注入", status="ok", node_ref="dc")],
        values=[ValueSample(ts="16:37:38", value="95", source="real", abnormal=False, log_ref="line 5686")],
        findings=[Finding(severity="info", title="链路连通", evidence_refs=["dc"], kind="ok")],
    )
    assert validate(g) == []
    assert g.nodes[0].status == "ok"
    assert g.to_dict()["verdict"]["status"] == "ok"


def test_validate_flags_node_lane_not_in_lanes():
    g = ReportGraph(
        intent="consult", has_logs=False,
        verdict=Verdict(status="inconclusive", headline="", next_step=""),
        lanes=[{"id": "a", "title": "A"}],
        nodes=[GraphNode(id="n1", lane="MISSING", label="x", status="unknown",
                         anchors=[], logs=[], note="")],
        edges=[], timeline=[], values=[], findings=[],
    )
    errors = validate(g)
    assert any("MISSING" in e for e in errors)


def test_validate_flags_bad_status():
    g = ReportGraph(
        intent="diagnose", has_logs=True,
        verdict=Verdict(status="ok", headline="", next_step=""),
        lanes=[{"id": "a", "title": "A"}],
        nodes=[GraphNode(id="n1", lane="a", label="x", status="NOPE",
                         anchors=[], logs=[], note="")],
        edges=[], timeline=[], values=[], findings=[],
    )
    assert any("status" in e for e in validate(g))
