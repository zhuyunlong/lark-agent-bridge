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
    from_: str = ""
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
    return errors
