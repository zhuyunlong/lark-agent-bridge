"""把各分析数据源映射成 ReportGraph。本阶段实现 signal 链路脚本 JSON 的确定性映射。"""
from __future__ import annotations

from typing import Mapping

from .report_graph import (
    Anchor, Finding, GraphEdge, GraphNode, LogRef,
    ReportGraph, TimelineEvent, ValueSample, Verdict,
)


def _short_file(path: str) -> str:
    return path.rsplit("/", 1)[-1] if path else ""


def signal_json_to_graph(payload: Mapping[str, object]) -> ReportGraph:
    signal = payload.get("signal") or {}
    code = str(signal.get("code") or "")
    lifecycle = payload.get("lifecycle_report") or {}
    context = lifecycle.get("context") or {}
    runtime = lifecycle.get("runtime") or {}
    chain_edges = payload.get("chain_edges") or []
    stats = (payload.get("detailed_stats") or {}).get(code, {})

    reg_checks = runtime.get("registration_checks") or []
    reg_ok = all(str(c.get("status")) == "success" for c in reg_checks) if reg_checks else False

    lanes = [
        {"id": "carservice", "title": f"信号源 / {context.get('provider_type') or 'CarService'}"},
        {"id": "helper", "title": f"Helper / {context.get('helper_class') or ''}"},
        {"id": "datacenter", "title": "DataCenter 分发"},
        {"id": "business", "title": "Android 业务消费"},
        {"id": "unity", "title": "Unity 下发"},
    ]

    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []

    # --- carservice node ---
    src_edge = context.get("source_edge") or {}
    if src_edge:
        nodes.append(GraphNode(
            id="carservice", lane="carservice",
            label=str(src_edge.get("source") or "CarService 属性"),
            status="ok" if reg_ok else "suspect",
            anchors=[Anchor(file=_short_file(str(src_edge.get("file") or "")), line=int(src_edge.get("line") or 0))] if src_edge.get("file") else [],
            note=str(src_edge.get("note") or ""),
        ))

    # --- helper node ---
    if context.get("helper_class"):
        nodes.append(GraphNode(
            id="helper", lane="helper",
            label=f"{context.get('helper_class')}.onChangeEvent",
            status="ok" if int(stats.get("datacenter", 0)) > 0 else "suspect",
            anchors=[Anchor(file=_short_file(str(context.get("helper_file") or "")), line=0)] if context.get("helper_file") else [],
        ))
        edges.append(GraphEdge(**{"from": "carservice", "to": "helper", "kind": "属性回调", "status": "ok"}))

    # --- datacenter node ---
    # Find chain edge whose kind contains "分发" (Kotlin 信号分发 matches)
    dispatch_edge = next((e for e in chain_edges if "分发" in str(e.get("kind", ""))), None)
    if dispatch_edge:
        nodes.append(GraphNode(
            id="datacenter", lane="datacenter",
            label="DataCenter.dispatchSignal",
            status="ok" if int(stats.get("datacenter", 0)) > 0 else "suspect",
            anchors=[Anchor(file=_short_file(str(dispatch_edge.get("file") or "")), line=int(dispatch_edge.get("line") or 0))],
            note=str(dispatch_edge.get("note") or ""),
        ))
        edges.append(GraphEdge(**{"from": "helper", "to": "datacenter", "kind": "onNextData", "status": "ok"}))

    # --- business nodes ---
    # Use edges with kind "Android 业务消费" (more precise than filename substring)
    seen: set[str] = set()
    for idx, e in enumerate(chain_edges):
        if str(e.get("kind", "")) != "Android 业务消费":
            continue
        target = str(e.get("target") or "")
        if target in seen:
            continue
        seen.add(target)
        f = str(e.get("file") or "")
        nid = f"biz{idx}"
        nodes.append(GraphNode(
            id=nid, lane="business",
            label=_short_file(f) if f else target,
            status="ok" if int(stats.get("android_business", 0)) > 0 else "suspect",
            anchors=[Anchor(file=_short_file(f), line=int(e.get("line") or 0))] if f else [],
        ))
        edges.append(GraphEdge(**{"from": "datacenter", "to": nid, "kind": "订阅/观察", "status": "ok"}))
        if len(seen) >= 6:
            break

    # --- unity node ---
    unity_status = "ok" if int(stats.get("android_unity", 0)) > 0 or int(stats.get("unity_recv_total", 0)) > 0 else "suspect"
    nodes.append(GraphNode(
        id="unity", lane="unity", label="sendMsgToUnity / onHandler",
        status=unity_status,
        note="Unity 接收统计=0，靠源码与缓存下发推断" if unity_status == "suspect" else "",
    ))
    if any(n.lane == "datacenter" for n in nodes):
        edges.append(GraphEdge(**{"from": "datacenter", "to": "unity", "kind": "缓存下发", "status": unity_status}))

    # --- attach logs to nodes ---
    events = runtime.get("events") or []
    for node in nodes:
        node_files = {a.file for a in node.anchors}
        node.logs = [
            LogRef(ts=str(ev.get("time") or ""), file=_short_file(str(ev.get("file") or "")),
                   line=int(ev.get("line") or 0), text=str(ev.get("text") or ""))
            for ev in events
            if _short_file(str(ev.get("file") or "")) in node_files
        ][:3]

    # --- timeline: skip 进程启动, cap at 12 ---
    timeline = [
        TimelineEvent(t_offset=str(ev.get("delta") or ev.get("time") or ""),
                      event=str(ev.get("label") or ""), status="ok")
        for ev in events
        if str(ev.get("label") or "") not in {"进程启动"}
    ][:12]

    # --- values ---
    values = [
        ValueSample(
            ts=str(v.get("time") or ""),
            value=str(v.get("value")),
            source=(
                "hmi" if "HMI" in str(v.get("source") or v.get("label") or "")
                else "cache" if "缓存" in str(v.get("source") or v.get("label") or "")
                else "real"
            ),
            abnormal=(str(v.get("value")) == "-1" or "HMI" in str(v.get("source") or v.get("label") or "")),
            log_ref=str(v.get("label") or ""),
        )
        for v in (runtime.get("value_samples") or [])
    ]

    # --- findings ---
    findings: list[Finding] = []
    for w in (payload.get("warnings") or []):
        findings.append(Finding(severity="warn", title=str(w), kind="risk"))
    for c in reg_checks:
        if str(c.get("status")) != "success":
            findings.append(Finding(severity="warn", title=f"注册检查未通过: {c.get('name')}", kind="todo"))
    if any(v.abnormal for v in values):
        findings.append(Finding(severity="warn", title="检测到异常值(如 HMI batteryLevel=-1)，与正常值并存", kind="risk"))

    verdict = Verdict(
        status="ok" if int(stats.get("android_business", 0)) > 0 else "inconclusive",
        headline=str(payload.get("summary") or ""),
        next_step="",
    )

    return ReportGraph(
        intent="diagnose",
        has_logs=int(runtime.get("hits", 0)) > 0,
        verdict=verdict, lanes=lanes, nodes=nodes, edges=edges,
        timeline=timeline, values=values, findings=findings,
    )
