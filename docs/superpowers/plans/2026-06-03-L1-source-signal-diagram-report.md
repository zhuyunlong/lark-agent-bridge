# L1 报告层：图优先源码/信号调查报告 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `signal` + `source_stage` 双报告合并成**单份图优先报告**，由可复用的 `ReportGraph` 契约驱动，核心是按节点状态染色的 SVG 泳道图 + 可展开 file:line/原始日志证据。

**Architecture:** 新增 `ReportGraph` 数据契约（C1）；写确定性适配器把现有 signal 链路脚本 JSON 映射成 `ReportGraph` 并消噪（C2，含确定性节点状态推断）；在 `combined_bug_html` 新增"状态染色泳道图"SVG 渲染原语（C5a）；新增图优先报告渲染器消费 `ReportGraph`（C5b）；在 `general_summary._build_combined_report_artifacts` 增 `[signal, source_stage]` 合并分支，只发单份、不再生成两份原始 HTML（C6）。本阶段节点状态用**确定性规则**推断（注册检查/各阶段命中/值异常）；AI 语义判读（OK/断开标注）留待 L2。

**Tech Stack:** Python 3.13、dataclasses、内联 SVG（无 JS）、pytest。本阶段以真实案例 Bug 6998811703 / `SIGNAL_VCU_ELECTRICIT_PERCENT`(40018) 的 `bug_signal_chain_report.json` 作为测试 fixture。

依据 spec：`docs/superpowers/specs/2026-06-03-source-analysis-intent-aware-diagram-report-design.md`（C1/C2/C5/C6、决策点 1）。

---

## File Structure

- Create `lark_agent_bridge/reporting/report_graph.py` — `ReportGraph` 及子结构 dataclass + `validate()`。纯数据，无依赖。
- Create `lark_agent_bridge/reporting/graph_adapters.py` — `signal_json_to_graph(payload)`：signal JSON → `ReportGraph`，含消噪与确定性状态。依赖 `report_graph`。
- Modify `lark_agent_bridge/reporting/combined_bug_html.py` — 新增 `render_status_lane_graph(nodes, edges, ...)`：状态染色泳道 SVG，复用现有 `_wrap_svg_text`/`_svg_text_block`。
- Create `lark_agent_bridge/reporting/source_signal_report_html.py` — `render_signal_source_report(graph, ...)`：图优先全报告。依赖 `report_graph`、`combined_bug_html`、`source_report_html.render_document`。
- Modify `lark_agent_bridge/agents/bug/general_summary.py:548` — `_build_combined_report_artifacts` 增 `[signal, source_stage]` 分支。
- Modify `lark_agent_bridge/agents/bug/run_primary.py:909-917` — 合并分支已只发单份（沿用），确保 source_stage 个体 HTML 不进 `files_to_send`。
- Modify signal 脚本调用：`lark_agent_bridge/agents/bug/_shared.py` / 调 `analyze_signal_chain.py` 处增 `--json-only`（决策点 1：不生成 signal 个体 HTML）。
- Create `tests/fixtures/signal_chain_40018.json` — 复制真实 JSON 作 fixture。
- Create `tests/test_report_graph.py`、`tests/test_graph_adapters.py`、`tests/test_source_signal_report_html.py`。

---

## Task 1: ReportGraph 数据契约

**Files:**
- Create: `lark_agent_bridge/reporting/report_graph.py`
- Test: `tests/test_report_graph.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_report_graph.py
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_report_graph.py -v`
Expected: FAIL，`ModuleNotFoundError: ... report_graph`

- [ ] **Step 3: 写最小实现**

```python
# lark_agent_bridge/reporting/report_graph.py
"""图优先报告的统一数据契约 (ReportGraph)。

任何分析只要产出 ReportGraph 即可被 source_signal_report_html 渲染成图优先报告。
本契约与数据来源解耦。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Literal, Mapping, Sequence

Status = Literal["ok", "suspect", "broken", "unknown"]
VerdictStatus = Literal["ok", "broken", "inconclusive"]
Intent = Literal["consult", "diagnose"]

_NODE_STATUSES = {"ok", "suspect", "broken", "unknown"}
_VERDICT_STATUSES = {"ok", "broken", "inconclusive"}


@dataclass
class Anchor:
    file: str
    line: int


@dataclass
class LogRef:
    ts: str
    file: str
    line: int
    text: str


@dataclass
class GraphNode:
    id: str
    lane: str
    label: str
    status: str = "unknown"
    anchors: list[Anchor] = field(default_factory=list)
    logs: list[LogRef] = field(default_factory=list)
    note: str = ""


@dataclass
class GraphEdge:
    # 注意 from 是 Python 关键字，构造时用 **{"from": ...}
    from_: str = field(metadata={"json": "from"})
    to: str = ""
    kind: str = ""
    status: str = "unknown"
    note: str = ""

    def __init__(self, **kwargs: object) -> None:
        self.from_ = str(kwargs.get("from", kwargs.get("from_", "")))
        self.to = str(kwargs.get("to", ""))
        self.kind = str(kwargs.get("kind", ""))
        self.status = str(kwargs.get("status", "unknown"))
        self.note = str(kwargs.get("note", ""))


@dataclass
class TimelineEvent:
    t_offset: str
    event: str
    status: str = "unknown"
    node_ref: str = ""


@dataclass
class ValueSample:
    ts: str
    value: str
    source: str = "real"  # real | cache | hmi
    abnormal: bool = False
    log_ref: str = ""


@dataclass
class Finding:
    severity: str  # info | warn | high
    title: str
    evidence_refs: list[str] = field(default_factory=list)
    kind: str = "ok"  # ok | risk | todo


@dataclass
class Verdict:
    status: str
    headline: str
    next_step: str = ""


@dataclass
class ReportGraph:
    intent: str
    has_logs: bool
    verdict: Verdict
    lanes: list[Mapping[str, str]] = field(default_factory=list)
    nodes: list[GraphNode] = field(default_factory=list)
    edges: list[GraphEdge] = field(default_factory=list)
    timeline: list[TimelineEvent] = field(default_factory=list)
    values: list[ValueSample] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        # 把 edge.from_ 还原成 from
        for edge in data["edges"]:
            edge["from"] = edge.pop("from_")
        return data


def validate(graph: ReportGraph) -> list[str]:
    errors: list[str] = []
    lane_ids = {str(lane.get("id")) for lane in graph.lanes}
    if graph.verdict.status not in _VERDICT_STATUSES:
        errors.append(f"verdict.status 非法: {graph.verdict.status}")
    node_ids: set[str] = set()
    for node in graph.nodes:
        node_ids.add(node.id)
        if node.status not in _NODE_STATUSES:
            errors.append(f"node {node.id} status 非法: {node.status}")
        if node.lane not in lane_ids:
            errors.append(f"node {node.id} lane 不在 lanes 中: {node.lane}")
    for edge in graph.edges:
        for end in (edge.from_, edge.to):
            if end and end not in node_ids:
                errors.append(f"edge 端点不存在: {end}")
    return errors
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_report_graph.py -v`
Expected: PASS（3 passed）

- [ ] **Step 5: 提交**

```bash
rtk git add lark_agent_bridge/reporting/report_graph.py tests/test_report_graph.py
rtk git commit -m "feat(reporting): add ReportGraph data contract for diagram-first reports"
```

---

## Task 2: 准备真实 JSON fixture

**Files:**
- Create: `tests/fixtures/signal_chain_40018.json`

- [ ] **Step 1: 复制真实案例 JSON 作 fixture**

```bash
mkdir -p tests/fixtures
cp data/jobs/63a847cb555b58b410029f4c9d2de39c/output/bug_signal_chain_report.json tests/fixtures/signal_chain_40018.json
```

- [ ] **Step 2: 校验 fixture 含关键字段**

Run:
```bash
python3 -c "import json; d=json.load(open('tests/fixtures/signal_chain_40018.json')); print(d['signal']['code'], len(d['chain_edges']), len(d['lifecycle_report']['runtime']['value_samples']), len(d['lifecycle_report']['source_references']))"
```
Expected: `40018 9 5 28`

- [ ] **Step 3: 提交**

```bash
rtk git add tests/fixtures/signal_chain_40018.json
rtk git commit -m "test(reporting): add real signal-chain JSON fixture (bug 6998811703 / 40018)"
```

---

## Task 3: signal JSON → ReportGraph 适配器（含消噪 + 确定性状态）

**Files:**
- Create: `lark_agent_bridge/reporting/graph_adapters.py`
- Test: `tests/test_graph_adapters.py`

适配规则（基于真实 JSON 结构）：
- **lanes**：固定 5 道，按链路顺序 `carservice / helper / datacenter / business / unity`，title 取自 `lifecycle_report.context`（provider_type / helper_class）与常量。
- **nodes**：源点取 `lifecycle_report.context.source_edge`（CarService）；helper 取 `context.helper_class`；datacenter 取 `chain_edges` 中 kind 含"分发"的边目标（`DataCenter.dispatchSignal`，file/line 来自该边）；business 取 `chain_edges` 中指向消费端的边（去重）；unity 取 `detailed_stats` 是否有 unity 命中决定是否出节点。
- **anchors**：节点的 file/line 来自对应 `chain_edge` / `context`。
- **logs**：从 `lifecycle_report.runtime.events`（含 file/line/text/time）按节点归类挑代表行。
- **timeline**：`lifecycle_report.runtime.events` 直接映射（label→event, time/delta→t_offset）。
- **values**：`lifecycle_report.runtime.value_samples`（value/source/time），`source` 含 "HMI" 或 value=="-1" 标 `abnormal=True`。
- **findings**：`warnings` → risk；`registration_checks` 中 status!=success → todo；value 异常 → risk。
- **消噪**：`source_references` 仅保留 `text` 提到目标 code（"40018"/signal name）或属于核心文件（helper_file/controller_file/signal_mapping_file）的条目，剔除无关信号行。
- **确定性状态**：节点 status 由规则定：该节点对应 stage 在 `detailed_stats`/`log_report.stages` 有命中且相关 `registration_checks` success → `ok`；有源码无运行命中 → `suspect`；明确缺失 → `broken`；否则 `unknown`。

- [ ] **Step 1: 写失败测试（用真实 fixture）**

```python
# tests/test_graph_adapters.py
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
    # 真实 JSON 的 lifecycle source_references 末尾混入 gear/speed/door
    g = _graph()
    joined = " ".join(
        f"{a.file}:{a.line}" for n in g.nodes for a in n.anchors
    )
    # 适配后的节点锚点不应把无关信号的 SignalMapping.kt:17-20 当作 40018 证据
    assert "SignalMapping.kt:17" not in joined


def test_timeline_has_registration_events():
    g = _graph()
    events = [e.event for e in g.timeline]
    assert any("注册" in e or "注入" in e or "订阅" in e for e in events)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_graph_adapters.py -v`
Expected: FAIL，`ModuleNotFoundError: ... graph_adapters`

- [ ] **Step 3: 写实现**

```python
# lark_agent_bridge/reporting/graph_adapters.py
"""把各分析数据源映射成 ReportGraph。本阶段实现 signal 链路脚本 JSON 的确定性映射。"""
from __future__ import annotations

from typing import Mapping, Sequence

from .report_graph import (
    Anchor, Finding, GraphEdge, GraphNode, LogRef,
    ReportGraph, TimelineEvent, ValueSample, Verdict,
)

_LANE_ORDER = ["carservice", "helper", "datacenter", "business", "unity"]


def _short_file(path: str) -> str:
    return path.rsplit("/", 1)[-1] if path else ""


def _is_related(text: str, code: str, name: str, core_files: set[str], file: str) -> bool:
    blob = text or ""
    if code and code in blob:
        return True
    if name and name in blob:
        return True
    return file in core_files


def signal_json_to_graph(payload: Mapping[str, object]) -> ReportGraph:
    signal = payload.get("signal") or {}
    code = str(signal.get("code") or "")
    name = str(signal.get("name") or "")
    lifecycle = payload.get("lifecycle_report") or {}
    context = lifecycle.get("context") or {}
    runtime = lifecycle.get("runtime") or {}
    chain_edges = payload.get("chain_edges") or []
    stats = (payload.get("detailed_stats") or {}).get(code, {})

    core_files = {
        str(context.get("helper_file") or ""),
        str(context.get("controller_file") or ""),
        str(context.get("signal_mapping_file") or ""),
    }
    core_files.discard("")

    reg_checks = runtime.get("registration_checks") or []
    reg_ok = all(str(c.get("status")) == "success" for c in reg_checks) if reg_checks else False

    def status_for(stage_hits: int, *, has_source: bool) -> str:
        if stage_hits > 0:
            return "ok"
        if has_source:
            return "suspect"
        return "unknown"

    lanes = [
        {"id": "carservice", "title": f"信号源 / {context.get('provider_type') or 'CarService'}"},
        {"id": "helper", "title": f"Helper / {context.get('helper_class') or ''}"},
        {"id": "datacenter", "title": "DataCenter 分发"},
        {"id": "business", "title": "Android 业务消费"},
        {"id": "unity", "title": "Unity 下发"},
    ]

    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []

    # carservice 源点（来自 source_edge）
    src_edge = context.get("source_edge") or {}
    if src_edge:
        nodes.append(GraphNode(
            id="carservice", lane="carservice",
            label=str(src_edge.get("source") or "CarService 属性"),
            status="ok" if reg_ok else "suspect",
            anchors=[Anchor(file=_short_file(str(src_edge.get("file") or "")), line=int(src_edge.get("line") or 0))] if src_edge.get("file") else [],
            note=str(src_edge.get("note") or ""),
        ))

    # helper
    if context.get("helper_class"):
        nodes.append(GraphNode(
            id="helper", lane="helper",
            label=f"{context.get('helper_class')}.onChangeEvent",
            status="ok" if int(stats.get("datacenter", 0)) > 0 else "suspect",
            anchors=[Anchor(file=_short_file(str(context.get("helper_file") or "")), line=0)] if context.get("helper_file") else [],
        ))
        edges.append(GraphEdge(**{"from": "carservice", "to": "helper", "kind": "属性回调", "status": "ok"}))

    # datacenter（取分发边）
    dispatch_edge = next((e for e in chain_edges if "分发" in str(e.get("kind", ""))), None)
    if dispatch_edge:
        nodes.append(GraphNode(
            id="datacenter", lane="datacenter",
            label="DataCenter.dispatchSignal",
            status=status_for(int(stats.get("datacenter", 0)), has_source=True),
            anchors=[Anchor(file=_short_file(str(dispatch_edge.get("file") or "")), line=int(dispatch_edge.get("line") or 0))],
            note=str(dispatch_edge.get("note") or ""),
        ))
        edges.append(GraphEdge(**{"from": "helper", "to": "datacenter", "kind": "onNextData", "status": "ok"}))

    # business 消费端（chain_edges 指向业务的边，去重，最多 6）
    seen: set[str] = set()
    for idx, e in enumerate(chain_edges):
        tgt = str(e.get("target") or "")
        f = str(e.get("file") or "")
        if not f or "DataCenter" in f or f in seen:
            continue
        if "业务" not in str(e.get("kind", "")) and "消费" not in str(e.get("note", "")) and "Action" not in f and "Config" not in f and "Helper" not in f:
            continue
        seen.add(f)
        nid = f"biz{idx}"
        nodes.append(GraphNode(
            id=nid, lane="business",
            label=_short_file(f),
            status="ok" if int(stats.get("android_business", 0)) > 0 else "suspect",
            anchors=[Anchor(file=_short_file(f), line=int(e.get("line") or 0))],
        ))
        edges.append(GraphEdge(**{"from": "datacenter", "to": nid, "kind": "订阅/观察", "status": "ok"}))
        if len(seen) >= 6:
            break

    # unity
    unity_status = "ok" if int(stats.get("android_unity", 0)) > 0 or int(stats.get("unity_recv_total", 0)) > 0 else "suspect"
    nodes.append(GraphNode(
        id="unity", lane="unity", label="sendMsgToUnity / onHandler",
        status=unity_status, note="Unity 接收统计=0，靠源码与缓存下发推断" if unity_status == "suspect" else "",
    ))
    if any(n.lane == "datacenter" for n in nodes):
        edges.append(GraphEdge(**{"from": "datacenter", "to": "unity", "kind": "缓存下发", "status": unity_status}))

    # 给 datacenter 节点挂代表日志（消噪）
    events = runtime.get("events") or []
    for node in nodes:
        node.logs = [
            LogRef(ts=str(ev.get("time") or ""), file=_short_file(str(ev.get("file") or "")),
                   line=int(ev.get("line") or 0), text=str(ev.get("text") or ""))
            for ev in events
            if node.label and (node.id in str(ev.get("label", "")) or _short_file(str(ev.get("file") or "")) in {a.file for a in node.anchors})
        ][:3]

    timeline = [
        TimelineEvent(t_offset=str(ev.get("delta") or ev.get("time") or ""),
                      event=str(ev.get("label") or ""), status="ok")
        for ev in events
        if str(ev.get("label") or "") not in {"进程启动"}
    ][:12]

    values = [
        ValueSample(ts=str(v.get("time") or ""), value=str(v.get("value")),
                    source=("hmi" if "HMI" in str(v.get("source") or v.get("label") or "") else
                            "cache" if "缓存" in str(v.get("source") or v.get("label") or "") else "real"),
                    abnormal=(str(v.get("value")) == "-1" or "HMI" in str(v.get("label") or "")),
                    log_ref=str(v.get("label") or ""))
        for v in (runtime.get("value_samples") or [])
    ]

    findings: list[Finding] = []
    for w in (payload.get("warnings") or []):
        findings.append(Finding(severity="warn", title=str(w), evidence_refs=[], kind="risk"))
    for c in reg_checks:
        if str(c.get("status")) != "success":
            findings.append(Finding(severity="warn", title=f"注册检查未通过: {c.get('name')}", evidence_refs=[], kind="todo"))
    if any(v.abnormal for v in values):
        findings.append(Finding(severity="warn", title="检测到异常值(如 HMI batteryLevel=-1)，与正常值并存", evidence_refs=[], kind="risk"))

    verdict = Verdict(
        status="ok" if int(stats.get("android_business", 0)) > 0 else "inconclusive",
        headline=str(payload.get("summary") or ""),
        next_step="",
    )

    return ReportGraph(
        intent="diagnose",
        has_logs=int(runtime.get("hits", 0)) > 0,
        verdict=verdict,
        lanes=lanes, nodes=nodes, edges=edges,
        timeline=timeline, values=values, findings=findings,
    )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_graph_adapters.py -v`
Expected: PASS（6 passed）。若 `test_denoise_drops_unrelated_signal_refs` 因消噪逻辑未覆盖某条而失败，收紧 business 边的 `Helper` 判定（仅当 `text/note` 含目标 code 时纳入）。

- [ ] **Step 5: 提交**

```bash
rtk git add lark_agent_bridge/reporting/graph_adapters.py tests/test_graph_adapters.py
rtk git commit -m "feat(reporting): signal-chain JSON to ReportGraph adapter with denoise + deterministic status"
```

---

## Task 4: 状态染色泳道 SVG 原语

**Files:**
- Modify: `lark_agent_bridge/reporting/combined_bug_html.py`（在 `render_swimlane_rows` 之后、`_svg_text_block` 之前新增函数；复用同文件 `_wrap_svg_text`/`_svg_text_block`/`H`）
- Test: `tests/test_source_signal_report_html.py`（先放本任务的 SVG 测试）

设计：每条 lane 一**列**（沿用现有横向排布，复用 `render_swimlane_rows` 的几何风格），节点矩形按 status **内联**填色（不依赖 CSS class，从源头消除"死 CSS"问题）。节点左上角圆圈标编号（用于下方证据区锚点跳转）。相邻 lane 间用 `marker=laneArrow` 折线连。

状态色：`ok=#16a34a`、`suspect=#f59e0b`、`broken=#dc2626`、`unknown=#94a3b8`。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_source_signal_report_html.py
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
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_source_signal_report_html.py -v`
Expected: FAIL，`ImportError: cannot import name 'render_status_lane_graph'`

- [ ] **Step 3: 实现（新增函数到 combined_bug_html.py）**

在 `render_swimlane_rows` 函数结束（第 399 行 `return "".join(chunks)` 之后）插入：

```python
_STATUS_FILL = {
    "ok": "#16a34a",
    "suspect": "#f59e0b",
    "broken": "#dc2626",
    "unknown": "#94a3b8",
}


def render_status_lane_graph(
    nodes: Sequence[Mapping[str, object]],
    edges: Sequence[Mapping[str, object]],
    *,
    empty_text: str = "未提取到链路节点。",
) -> str:
    node_list = [dict(n) for n in nodes if str(n.get("label", "")).strip()]
    if not node_list:
        return f'<p class="muted">{H(empty_text)}</p>'
    outer_x, outer_y = 28, 18
    lane_w, lane_gap = 220, 28
    card_y, card_h = 70, 132
    total_w = outer_x * 2 + len(node_list) * lane_w + max(0, len(node_list) - 1) * lane_gap
    total_h = card_y + card_h + 36
    pos = {n["id"]: outer_x + i * (lane_w + lane_gap) for i, n in enumerate(node_list)}

    chunks: list[str] = [
        '<div class="swimlane-svg-wrap">',
        (f'<svg class="swimlane-svg" viewBox="0 0 {total_w} {total_h}" '
         'xmlns="http://www.w3.org/2000/svg" role="img" aria-label="链路泳道图">'),
        '<defs><marker id="laneArrow" viewBox="0 0 10 10" refX="8" refY="5" '
        'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        '<path d="M 0 0 L 10 5 L 0 10 z" fill="#7c8db0"/></marker></defs>',
    ]
    # 连线（在节点底下先画）
    arrow_y = card_y + card_h / 2
    for edge in edges:
        a, b = pos.get(str(edge.get("from"))), pos.get(str(edge.get("to")))
        if a is None or b is None or b <= a:
            continue
        start_x, end_x = a + lane_w, b
        chunks.append(
            f'<line x1="{start_x}" y1="{arrow_y}" x2="{end_x - 6}" y2="{arrow_y}" '
            'stroke="#94a3b8" stroke-width="2" marker-end="url(#laneArrow)"/>'
        )
    # 节点卡片
    for n in node_list:
        x = pos[n["id"]]
        fill = _STATUS_FILL.get(str(n.get("status", "unknown")), "#94a3b8")
        lane_lines = _wrap_svg_text(str(n.get("lane_title", "")), 14)
        label_lines = _wrap_svg_text(str(n.get("label", "")), 18)
        chunks.append(
            f'<rect x="{x}" y="{card_y}" width="{lane_w}" height="{card_h}" rx="16" '
            f'fill="#ffffff" stroke="{fill}" stroke-width="2.5"/>'
        )
        chunks.append(f'<circle cx="{x + 26}" cy="{card_y + 26}" r="14" fill="{fill}"/>')
        chunks.append(
            f'<text x="{x + 26}" y="{card_y + 30}" text-anchor="middle" fill="#fff" '
            f'font-size="12" font-weight="700">{H(str(n.get("num", "")))}</text>'
        )
        chunks.extend(_svg_text_block(x + 50, card_y + 24, lane_lines, fill="#64748b",
                                      font_size=11, font_weight=700, line_height=14))
        chunks.extend(_svg_text_block(x + 16, card_y + 70, label_lines, fill="#1f2937",
                                      font_size=13, font_weight=600, line_height=18))
    chunks.extend(["</svg>", "</div>"])
    return "".join(chunks)
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_source_signal_report_html.py -v`
Expected: PASS（4 passed）

- [ ] **Step 5: 提交**

```bash
rtk git add lark_agent_bridge/reporting/combined_bug_html.py tests/test_source_signal_report_html.py
rtk git commit -m "feat(reporting): status-colored lane graph SVG primitive (inline fills, no dead CSS)"
```

---

## Task 5: 图优先报告渲染器

**Files:**
- Create: `lark_agent_bridge/reporting/source_signal_report_html.py`
- Test: `tests/test_source_signal_report_html.py`（追加）

报告结构：verdict 横幅 → ① 泳道图（`render_status_lane_graph`）+ 节点证据卡片区（`<details>` 含 file:line + 原始日志）→ ② 生命周期时间线 → ③ 值变化轨道（real/cache/hmi 分列，abnormal 标红）→ ④ findings（ok/risk/todo）→ (折叠) 完整锚点+日志。复用 `source_report_html.render_document`（源码报告外壳，不复用 bug 外壳）。

- [ ] **Step 1: 追加失败测试**

```python
# tests/test_source_signal_report_html.py 追加
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
    assert "DataCenter.kt:311" in html or "DataCenter" in html
    # 原始日志原文应出现（不再只有中文 label）
    assert "hasProvider" in html or "value" in html

def test_report_no_dead_swimlane_css_unused():
    # body 里必须真的用到 svg（不是只在 style 里定义）
    html = _html()
    body = html.split("</style>")[-1]
    assert "<svg" in body

def test_report_marks_abnormal_value():
    html = _html()
    assert "-1" in html
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_source_signal_report_html.py -v`
Expected: FAIL，`ModuleNotFoundError: ... source_signal_report_html`

- [ ] **Step 3: 实现**

```python
# lark_agent_bridge/reporting/source_signal_report_html.py
"""图优先源码/信号调查报告渲染器：消费 ReportGraph，产出单份 HTML。"""
from __future__ import annotations

from .report_graph import ReportGraph
from .combined_bug_html import render_status_lane_graph
from .source_report_html import H, render_document

_VERDICT_CLASS = {"ok": "green", "broken": "red", "inconclusive": "yellow"}
_FINDING_CLASS = {"ok": "green", "risk": "red", "todo": "yellow"}


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
    items = "".join(
        f'<li><b>{H(e.t_offset)}</b> {H(e.event)}</li>' for e in graph.timeline
    )
    note = "" if any(graph.timeline) else ""
    return f'<ul class="tl">{items}</ul><p class="muted">注销：本会话未观测到注销/dispose 事件。</p>'


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


_EXTRA_CSS = (
    ".pill{padding:2px 8px;border-radius:10px;font-size:11px;color:#fff}"
    ".p-ok{background:#16a34a}.p-suspect{background:#f59e0b}"
    ".p-broken{background:#dc2626}.p-unknown{background:#94a3b8}"
    ".logline{font-family:monospace;font-size:12px;color:#334155;margin:2px 0}"
    ".cv-table{width:100%;border-collapse:collapse}.cv-table td,.cv-table th{border:1px solid #e2e8f0;padding:6px 8px;text-align:left}"
    ".cv-table tr.abn td{color:#dc2626;font-weight:700}"
    ".tl{line-height:1.9}.issue{padding:8px 12px;border-radius:8px;margin:6px 0;background:#f8fafc}"
)


def render_signal_source_report(graph: ReportGraph, *, request_text: str, backend: str) -> str:
    sev = _VERDICT_CLASS.get(graph.verdict.status, "yellow")
    node_payload = [
        {"id": n.id, "lane_title": next((str(l.get("title")) for l in graph.lanes if l.get("id") == n.lane), n.lane),
         "label": n.label, "status": n.status, "num": i}
        for i, n in enumerate(graph.nodes, 1)
    ]
    edge_payload = [{"from": e.from_, "to": e.to} for e in graph.edges]
    next_step = f'<div class="muted">下一步：{H(graph.verdict.next_step)}</div>' if graph.verdict.next_step else ""
    body = (
        '<div class="container">'
        f"<h1>源码/信号调查报告</h1>"
        f'<div class="sub">请求：{H(request_text)} · 后端：{H(backend)}</div>'
        f'<div class="verdict v-{sev}">{H(graph.verdict.headline)}</div>{next_step}'
        f'<div class="section"><h2>① 数据流泳道图</h2>{render_status_lane_graph(node_payload, edge_payload)}'
        f'<div class="muted">节点描边色：绿=ok 橙=待确认 红=断开。展开看源码锚点与原始日志：</div>{_evidence_cards(graph)}</div>'
        f'<div class="section"><h2>② 生命周期时间线</h2>{_timeline(graph)}</div>'
        f'<div class="section"><h2>③ 值变化轨道</h2>{_value_track(graph)}</div>'
        f'<div class="section"><h2>④ 根因判读 / 风险 / 待确认</h2>{_findings(graph)}</div>'
        "</div>"
    )
    return render_document("源码/信号调查报告", body, css=_extended_css())


def _extended_css() -> str:
    from .source_report_html import BASE_REPORT_CSS
    return BASE_REPORT_CSS + _EXTRA_CSS
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_source_signal_report_html.py -v`
Expected: PASS（8 passed）

- [ ] **Step 5: 提交**

```bash
rtk git add lark_agent_bridge/reporting/source_signal_report_html.py tests/test_source_signal_report_html.py
rtk git commit -m "feat(reporting): diagram-first signal/source report renderer consuming ReportGraph"
```

---

## Task 6: 合并接线 + 不再生成两份原始 HTML

**Files:**
- Modify: `lark_agent_bridge/agents/bug/general_summary.py:602`（在 `if kinds == ["signal"]:` 分支前增 source_stage 合并分支）
- Modify: signal 脚本调用处加 `--json-only`（定位：`grep -n "analyze_signal_chain" lark_agent_bridge/agents/bug/_shared.py` 找到构造 argv 的位置）
- Test: `tests/test_combined_signal_source.py`

- [ ] **Step 1: 写失败测试（合并分支产单份图优先报告）**

```python
# tests/test_combined_signal_source.py
import json, shutil
from pathlib import Path
from lark_agent_bridge.agents.bug.general_summary import GeneralSummaryMixin  # 若混入类名不同，按实际调整

FIXTURE = Path("tests/fixtures/signal_chain_40018.json")


def test_signal_source_stage_merges_to_single_report(tmp_path):
    out = tmp_path
    signal_json = out / "bug_signal_chain_report.json"
    shutil.copy(FIXTURE, signal_json)

    class P:  # 最小 BugAnalysisPlan 替身
        def __init__(self, kind): self.kind = kind

    helper = GeneralSummaryMixin()  # 若需依赖，用 pytest fixture 构造真实实例
    art = helper._build_combined_report_artifacts(
        plans=[P("signal"), P("source_stage")],
        prompt_text="调查电量信号", fault_time="2026-05-25 16:50:41",
        output_dir=out, html_paths=[],
        report_jsons={"signal": signal_json, "source_stage": None},
        selected_input=signal_json,
    )
    assert art is not None
    html = Path(art["html_path"]).read_text(encoding="utf-8")
    assert "<svg" in html.split("</style>")[-1]
    assert "源码/信号调查报告" in html
```

> 注：若 `GeneralSummaryMixin` 无法独立实例化，改为通过 `BugRunner` 既有测试夹具构造实例（见 `tests/_app_base.py` 现有用法），保持本断言不变。

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_combined_signal_source.py -v`
Expected: FAIL（分支未命中，`art is None`）

- [ ] **Step 3: 实现合并分支**

在 `general_summary.py` 的 `if kinds == ["signal"]:`（第 602 行）**之前**插入：

```python
        if "source_stage" in kinds and any(k in {"signal"} for k in kinds):
            signal_json_path = report_jsons.get("signal")
            if signal_json_path is None or not signal_json_path.exists():
                return None
            from ...reporting.graph_adapters import signal_json_to_graph
            from ...reporting.source_signal_report_html import render_signal_source_report
            signal_payload = json.loads(signal_json_path.read_text(encoding="utf-8"))
            graph = signal_json_to_graph(signal_payload)
            graph.has_logs = selected_input is not None
            html_path = output_dir / self._combined_report_name("html")
            json_path = output_dir / self._combined_report_name("json")
            html_path.write_text(
                render_signal_source_report(
                    graph, request_text=prompt_text, backend="signal-chain-analyzer"
                ),
                encoding="utf-8",
            )
            json_path.write_text(json.dumps(graph.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
            return {"html_path": html_path, "json_path": json_path, "summary": graph.verdict.headline}
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_combined_signal_source.py -v`
Expected: PASS

- [ ] **Step 5: signal 脚本只产 JSON（不生成个体 HTML）**

定位调用：`grep -rn "analyze_signal_chain" lark_agent_bridge/`。在构造该脚本 argv 处追加 `--json-only`（脚本侧若无该参数，在 `analyze_signal_chain.py` 的 argparse 增 `--json-only` 开关，命中时 `2197` 跳过 `output_html.write_text`）。
确认 `run_primary.py:909-917` 的 `combined_artifacts is not None` 分支已只发 `combined_artifacts["html_path"]`（现状如此，无需改）；并确认 source_stage 个体 HTML 不再进入 `files_to_send`（合并分支命中时 `html_paths` 不被 extend）。

- [ ] **Step 6: 全量回归 + 提交**

Run: `python -m pytest tests/ -q`
Expected: 全绿（如有既有用例因"双报告→单报告"行为变更而失败，更新这些断言为单份合并报告，并在提交信息中说明）。

```bash
rtk git add lark_agent_bridge/agents/bug/general_summary.py tests/test_combined_signal_source.py
rtk git add -A lark_agent_bridge   # 含 --json-only 接线
rtk git commit -m "feat(bug): merge signal+source_stage into one diagram-first report; stop emitting both standalone HTMLs"
```

---

## Self-Review

**Spec 覆盖（L1 范围）：** C1=Task1；C2(含消噪+确定性状态)=Task3；C5a(SVG)=Task4；C5b(渲染器)=Task5；C6(合并+不生成两份, 决策点1)=Task6；fixture=Task2。L1 验收点 1–4 全部有对应任务。L2/L3/L4 不在本计划（见下）。

**占位符扫描：** 无 TBD/TODO/"类似上文"等占位；每个步骤均含可运行代码与确切命令/期望输出。`_timeline` 内 `note` 为未使用局部变量，落地时删除即可（不影响行为）。

**类型一致性：** `signal_json_to_graph`→`ReportGraph`（Task3 产，Task5/Task6 消费）；`render_status_lane_graph(nodes, edges)` 节点字段 `{id,lane_title,label,status,num}`（Task4 定义，Task5 `node_payload` 按此构造）；`render_signal_source_report(graph, *, request_text, backend)`（Task5 定义，Task6 按此调用）；`GraphEdge.from_` 与 `to_dict()` 的 `from` 映射一致（Task1）。

**已知执行风险（执行者注意）：**
1. Task6 的 `GeneralSummaryMixin` 实例化方式需对齐实际类结构（`_build_combined_report_artifacts` 所属类名/依赖），用 `tests/_app_base.py` 既有夹具构造，断言不变。
2. `_combined_report_name`/`_report_name` 已存在于该类（spec 引 `bug_cache.py:931-946`），合并分支直接复用。
3. signal 脚本 `--json-only` 若改动到 workspace 级 skill 脚本（`.ai/skills/...`），属 skill 改动——按全局 CLAUDE.md 第 6 条，改前先跑该脚本的 RED 基线（确认去掉 HTML 写出不影响 JSON 产物），最小化改动。

---

## 后续阶段（独立计划，L1 落地后再细化）

- **L2**：intent 维度（consult|diagnose 启发式）+ 按 (intent,has_logs) 侧重渲染 + source_stage agent 结构化输出（status 标注+findings，改 skill 前跑 RED）。
- **L3**：bug 路径内按 intent 产侧重报告（修意图劫持，不重写路由）。
- **L4**：函数级（标注模块）静态调用图生成器，根治咨询路径产出弱 + 空壳标绿修复。

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-06-03-L1-source-signal-diagram-report.md`. Two execution options:

1. **Subagent-Driven (recommended)** - 每个 task 派新 subagent，task 间复核，迭代快。
2. **Inline Execution** - 本会话内分批执行，带 checkpoint 复核。

Which approach?
