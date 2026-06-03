"""把各分析数据源映射成 ReportGraph。本阶段实现 signal 链路脚本 JSON 的确定性映射。"""
from __future__ import annotations

from typing import Mapping

from .report_graph import (
    Anchor, Finding, GraphEdge, GraphNode, LogRef,
    ReportGraph, TimelineEvent, ValueSample, Verdict,
)


def _short_file(path: str) -> str:
    return path.rsplit("/", 1)[-1] if path else ""


_VALID_STATUS = {"ok", "suspect", "broken", "unknown"}


def apply_source_stage(graph: ReportGraph, source_stage_data: "dict | None") -> None:
    """把 source_stage agent 的 node_status/findings 合并入 graph（按文件名/label 匹配）。
    防御式：任何缺失/损坏/非法 → 保留确定性 status，绝不抛、绝不写非法值。"""
    if not source_stage_data:
        return
    try:
        node_status = source_stage_data.get("node_status") or {}
        findings = source_stage_data.get("findings") or []
        if isinstance(node_status, dict):
            for node in graph.nodes:
                keys = {node.label} | {a.file for a in node.anchors}
                for fname, status in node_status.items():
                    if str(status) not in _VALID_STATUS:
                        continue
                    if fname and (fname in keys or any(fname in k for k in keys)):
                        node.status = str(status)
        if isinstance(findings, list):
            for f in findings:
                if not isinstance(f, dict):
                    continue
                title = str(f.get("title") or "").strip()
                if not title:
                    continue
                graph.findings.append(Finding(
                    severity=str(f.get("severity") or "warn"),
                    title=f"[源码] {title}",
                    evidence_refs=[str(f.get("file"))] if f.get("file") else [],
                    kind=str(f.get("kind") or "risk"),
                ))
        verdict = source_stage_data.get("verdict") or {}
        if isinstance(verdict, dict):
            headline = str(verdict.get("headline") or "").strip()
            next_step = str(verdict.get("next_step") or "").strip()
            status = str(verdict.get("status") or "").strip()
            if headline:
                graph.verdict.headline = headline
            if next_step:
                graph.verdict.next_step = next_step
            if status in {"ok", "broken", "inconclusive"}:
                graph.verdict.status = status
    except Exception:
        return


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
            # context 无 helper_line，不发出误导性的 line=0 锚点；helper_file 放进 note。
            anchors=[],
            note=f"helper: {_short_file(str(context.get('helper_file') or ''))}" if context.get("helper_file") else "",
        ))
        # 仅当上游节点确实存在时才连边，保证 validate(graph) 恒为空（契约始终有效）。
        if any(n.id == "carservice" for n in nodes):
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
        if any(n.id == "helper" for n in nodes):
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

    # --- timeline: skip 进程启动, dedupe repeated labels (keep ×count), cap 16 ---
    # 去重很关键：底层注册回调等会重复 20 次，若不去重会挤掉 "HMI 侧电量仍为 -1"
    # 这类单次出现的关键诊断事件。
    timeline: list[TimelineEvent] = []
    _seen_labels: dict[str, TimelineEvent] = {}
    _counts: dict[str, int] = {}
    for ev in events:
        label = str(ev.get("label") or "")
        if not label or label == "进程启动":
            continue
        if label in _seen_labels:
            _counts[label] += 1
            continue
        te = TimelineEvent(
            t_offset=str(ev.get("delta") or ev.get("time") or ""),
            event=label, status="ok",
        )
        _seen_labels[label] = te
        _counts[label] = 1
        timeline.append(te)
    for label, te in _seen_labels.items():
        if _counts[label] > 1:
            te.event = f"{label} ×{_counts[label]}"
    timeline = timeline[:16]

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


def _module_of(path: str) -> tuple[str, str]:
    if not path:
        return ("unknown", "unknown")
    parts = path.split("/")
    return (parts[0], "/".join(parts[:2]) if len(parts) > 1 else parts[0])


def _cg_node_id(path: str, line: int, name: str) -> str:
    return f"{path}:{line}:{name}"


def build_consult_graph_from_codegraph(
    seed_symbols, cg_client, repo, *,
    request_text: str, max_depth: int = 2, max_nodes: int = 30, max_per_node: int = 8,
) -> ReportGraph:
    """从种子符号用 codegraph 构造函数级静态调用图(按模块分泳道)。
    codegraph 不可用/未建索引 → inconclusive 最小图(不伪 green)。"""
    try:
        available = bool(cg_client.is_available() and cg_client.is_indexed(repo))
    except Exception:
        available = False
    if not available:
        return ReportGraph(
            intent="consult", has_logs=False,
            verdict=Verdict(status="inconclusive",
                            headline="未建立静态调用图（codegraph 不可用或未建索引）",
                            next_step="建立 codegraph 索引后重试以获得函数级链路图"),
            lanes=[{"id": "source", "title": "源码"}], nodes=[], edges=[],
            timeline=[], values=[],
            findings=[Finding(severity="warn", title="codegraph 不可用：无法构建静态调用图", kind="todo")],
        )

    lanes: list[dict] = []
    lane_ids: set[str] = set()
    nodes: dict[str, GraphNode] = {}
    edges: list[GraphEdge] = []
    findings: list[Finding] = []

    def ensure_lane(path: str) -> str:
        mid, title = _module_of(path)
        if mid not in lane_ids:
            lane_ids.add(mid)
            lanes.append({"id": mid, "title": f"模块 / {title}"})
        return mid

    def ensure_node(name: str, path: str, line, kind: str = ""):
        nid = _cg_node_id(path, int(line or 0), name)
        if nid in nodes:
            return nid
        if len(nodes) >= max_nodes:
            return None
        lane = ensure_lane(path)
        nodes[nid] = GraphNode(id=nid, lane=lane, label=name or _short_file(path),
                               status="unknown", anchors=[Anchor(file=_short_file(path), line=int(line or 0))],
                               note=kind)
        return nid

    roots = []
    for seed in seed_symbols or []:
        try:
            hits = cg_client.search_symbol(seed, repo, limit=3)
        except Exception:
            hits = []
        for h in list(hits)[:1]:
            rid = ensure_node(getattr(h, "qualified_name", str(seed)), getattr(h, "path", ""), getattr(h, "line", 0))
            if rid:
                roots.append(rid)

    frontier = [(rid, 0) for rid in roots]
    visited = set(roots)
    truncated = False
    while frontier:
        nid, depth = frontier.pop(0)
        if depth >= max_depth:
            continue
        node = nodes.get(nid)
        if node is None:
            continue
        sym = node.label
        try:
            callees = cg_client.get_callees(sym, repo, limit=max_per_node) or []
        except Exception:
            callees = []
        try:
            callers = cg_client.get_callers(sym, repo, limit=max_per_node) or []
        except Exception:
            callers = []
        for h in list(callees)[:max_per_node]:
            cid = ensure_node(getattr(h, "name", ""), getattr(h, "path", ""), getattr(h, "line", 0), getattr(h, "kind", ""))
            if cid is None:
                truncated = True
                continue
            edges.append(GraphEdge(**{"from": nid, "to": cid, "kind": "calls", "status": "unknown"}))
            if cid not in visited:
                visited.add(cid)
                frontier.append((cid, depth + 1))
        for h in list(callers)[:max_per_node]:
            cid = ensure_node(getattr(h, "name", ""), getattr(h, "path", ""), getattr(h, "line", 0), getattr(h, "kind", ""))
            if cid is None:
                truncated = True
                continue
            edges.append(GraphEdge(**{"from": cid, "to": nid, "kind": "calls", "status": "unknown"}))
            if cid not in visited:
                visited.add(cid)
                frontier.append((cid, depth + 1))

    node_list = list(nodes.values())
    seen_e: set[tuple[str, str]] = set()
    uniq_edges: list[GraphEdge] = []
    for e in edges:
        k = (e.from_, e.to)
        if k in seen_e or e.from_ == e.to:
            continue
        seen_e.add(k)
        uniq_edges.append(e)
    if truncated:
        findings.append(Finding(severity="info", title=f"调用图已截断（max_nodes={max_nodes}）", kind="todo"))
    if not node_list:
        findings.append(Finding(severity="warn", title="未从 codegraph 解析到种子符号；静态调用图为空", kind="todo"))
    headline = f"静态调用图：{len(lanes)} 模块 / {len(node_list)} 函数 / {len(uniq_edges)} 调用关系"
    return ReportGraph(
        intent="consult", has_logs=False,
        verdict=Verdict(status="inconclusive", headline=headline, next_step="如需运行态确认，请提供日志/bug"),
        lanes=lanes, nodes=node_list, edges=uniq_edges,
        timeline=[], values=[], findings=findings,
    )
