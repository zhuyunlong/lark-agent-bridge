# L4：咨询路径静态调用图（函数级·标注模块） Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`).

**Goal:** 让"咨询型/无日志"源码分析请求得到真实的**函数级静态调用图**（按模块分泳道），根治"空壳骨架/空证据标绿"，并复用 L1 的 `ReportGraph` + `render_signal_source_report(intent=consult, has_logs=False)` 渲染，同时**保留 agent 的文字分析**作为一个章节。

**Architecture:** 三任务。L4-1=空壳标绿修复（success/severity 由证据驱动，独立小改）。L4-2=新增 `build_consult_graph_from_codegraph(seed, cg_client, repo, ...)`——依赖注入 `cg_client`（可用 fake 单测）、手写有界 BFS（codegraph 无多跳 API）、模块→lane/函数→node/调用→edge、status 一律 `unknown`、verdict `inconclusive`；codegraph 不可用/未建索引时**防御式回退**到 inconclusive 最小图（绝不伪 green）。L4-3=咨询路径接入：对 consult 意图**禁用 app-server 旁路**走主路径，用 codegraph 建图，经 `render_signal_source_report` 渲染并把 agent 文字分析作为折叠章节保留。

**Tech Stack:** Python 3.13、pytest。venv `python`（非 `python3`）；不用 `timeout`。codegraph 经 `CodeGraphClient`（`codegraph_client.py:63`，CLI subprocess 薄包装，优雅降级）。

依据 spec C7 + 决策点 4（函数级+标注模块）；用户本轮定：① 切图优先渲染+保留 agent 分析 ② consult 禁用 app-server 旁路。承接 L1/L2（已交付）。

## 用户已定 + 默认决策
- **渲染**：consult 切到 `render_signal_source_report`，agent prose 作为「源码分析说明」折叠章节保留（不弃用内容）。
- **旁路**：consult 意图跳过 app-server 旁路（`source_analysis.py:124-133`），走主路径 codegraph。
- **兜底（默认）**：codegraph 可用且已建索引→真实图；否则→inconclusive 最小图 + finding 说明（不伪 green）。code_index(ctags+rg) 退化图为可选增强，本计划不含。
- **种子（默认）**：`SourceAnalysisRequest.target` 先 `cg.search_symbol` 映射成真实符号；回退 `_extract_query_symbols`。
- **方向/规模（默认）**：双向 BFS（callees 主=如何接入/分发/消费，callers 辅=谁触发），max_depth=2，max_nodes=30，max_per_node=8；超限在 findings 记 todo「图已截断」。

## File Structure
- Modify `lark_agent_bridge/source_analysis.py`（L4-1 success；L4-3 禁旁路+建图+渲染）
- Modify `lark_agent_bridge/reporting/source_report_html.py:176`（L4-1 severity）
- Modify `lark_agent_bridge/reporting/graph_adapters.py`（L4-2 新增 `build_consult_graph_from_codegraph`）
- Modify `lark_agent_bridge/reporting/source_signal_report_html.py`（L4-3 `render_signal_source_report` 加可选 `analysis_markdown` 章节）
- Tests: `tests/test_source_report_html.py`/`tests/test_app_source_analysis.py`（扩展）、`tests/test_graph_adapters.py`（扩展）

---

## Task L4-1: 空壳标绿修复（success/severity 由证据驱动）

**Files:** `source_analysis.py`、`source_report_html.py`；Test: 扩展 `tests/test_source_report_html.py` + `tests/test_app_source_analysis.py`（若存在）

- [ ] **Step 1: 写失败测试**
```python
# tests/test_source_report_html.py 追加
from lark_agent_bridge.reporting.source_report_html import render_source_analysis_report


def test_empty_evidence_not_green():
    html = render_source_analysis_report(
        title="t", request_text="r", answer="（无结构化结论）", target="X",
        source_evidence=[], coverage_boundary="", diagram_kinds=[], backend="x", success=True,
    )
    assert "v-green" not in html  # 无证据不得标绿


def test_with_evidence_can_be_green():
    html = render_source_analysis_report(
        title="t", request_text="r", answer="## 结论摘要\n- ok", target="X",
        source_evidence=[{"file": "A.kt", "line": "10", "text": "hit"}],
        coverage_boundary="", diagram_kinds=[], backend="x", success=True,
    )
    assert "v-green" in html
```

- [ ] **Step 2: 运行确认失败**（当前 `:176` 无证据仍可 green）

- [ ] **Step 3: 实现**
- `source_report_html.py:176` severity 改为无证据→red：
```python
    severity = "green" if success and effective_evidence_count else "yellow" if effective_evidence_count else "red"
```
- `source_analysis.py:89` 主路径 `success=True` → `success=bool(result.source_evidence)`（确认 `result.source_evidence` 字段名，按实际 result 对象调整）。
- `source_analysis.py:183/187` app-server 旁路：无真实结构化证据，改为 `success=False`（或 verdict inconclusive），不再 `success=True + source_evidence=[]`。
> 注：`requirement_analysis.py:320` 复用同一 runner，此 success 修复对其也生效（正确——空证据不该标成功）。

- [ ] **Step 4: 通过 + 回归**（`python -m pytest tests/ -q` 仅 2 已知失败；若 requirement_analysis 相关测试因 success 变化而失败，核对其是否原本依赖伪 success=True，按"证据驱动"语义更新断言）

- [ ] **Step 5: 提交**
```
git add lark_agent_bridge/source_analysis.py lark_agent_bridge/reporting/source_report_html.py tests/test_source_report_html.py
git commit -m "fix(source-analysis): evidence-driven success/severity (no green on empty evidence)"
```

---

## Task L4-2: build_consult_graph_from_codegraph 适配器（核心）

**Files:** `lark_agent_bridge/reporting/graph_adapters.py`；Test: 扩展 `tests/test_graph_adapters.py`

codegraph_client API（`codegraph_client.py` —— 实现者先读确认字段名）：`is_available()->bool`、`is_indexed(repo)->bool`、`search_symbol(query, repo, *, limit=10)->list[CgSymbolHit{qualified_name,path,line,signature}]`、`get_callees(symbol, repo, *, limit=20)->list[CgCallerHit{name,kind,path,line}]`、`get_callers(...)` 同。无多跳 API → 手写 BFS。

- [ ] **Step 1: 写失败测试（fake cg_client，纯函数无 IO）**
```python
# tests/test_graph_adapters.py 追加
from lark_agent_bridge.reporting.graph_adapters import build_consult_graph_from_codegraph
from lark_agent_bridge.reporting.report_graph import validate
from pathlib import Path
from types import SimpleNamespace


class _FakeCG:
    def __init__(self, available=True, indexed=True):
        self._a, self._i = available, indexed
    def is_available(self): return self._a
    def is_indexed(self, repo): return self._i
    def search_symbol(self, q, repo, *, limit=10):
        return [SimpleNamespace(qualified_name="CarVcuHelper.onChangeEvent",
                                path="module_datacenter/CarVcuHelper.kt", line=72, signature="fun onChangeEvent()")]
    def get_callees(self, sym, repo, *, limit=20):
        return [SimpleNamespace(name="DataCenter.dispatchSignal", kind="fun",
                                path="module_datacenter/DataCenter.kt", line=311)]
    def get_callers(self, sym, repo, *, limit=20):
        return [SimpleNamespace(name="CarService.onProp", kind="fun",
                                path="module_carservice/CarService.kt", line=40)]


def test_consult_graph_valid_and_consult_no_logs():
    g = build_consult_graph_from_codegraph(["电量信号"], _FakeCG(), Path("/repo"), request_text="了解链路")
    assert validate(g) == []
    assert g.intent == "consult" and g.has_logs is False
    assert g.verdict.status == "inconclusive"
    assert len(g.nodes) >= 1
    # 模块分泳道
    assert len({l["id"] for l in g.lanes}) >= 1
    # 边两端都在节点集合
    ids = {n.id for n in g.nodes}
    assert all(e.from_ in ids and e.to in ids for e in g.edges)
    # 节点状态静态→unknown
    assert all(n.status == "unknown" for n in g.nodes)


def test_consult_graph_fallback_when_cg_unavailable():
    g = build_consult_graph_from_codegraph(["x"], _FakeCG(available=False), Path("/repo"), request_text="r")
    assert validate(g) == []
    assert g.verdict.status == "inconclusive"
    assert "v-green" not in g.verdict.status  # 绝不伪 green
    assert any("codegraph" in f.title or "静态调用图" in f.title for f in g.findings)
```

- [ ] **Step 2: 运行确认失败**（ImportError）

- [ ] **Step 3: 实现（graph_adapters.py 新增）**
```python
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

    def ensure_node(name: str, path: str, line: int, kind: str = "") -> str | None:
        nid = _cg_node_id(path, line, name)
        if nid in nodes:
            return nid
        if len(nodes) >= max_nodes:
            return None
        lane = ensure_lane(path)
        nodes[nid] = GraphNode(id=nid, lane=lane, label=name or _short_file(path),
                               status="unknown", anchors=[Anchor(file=_short_file(path), line=int(line or 0))],
                               note=kind)
        return nid

    # 解析种子 → 根节点
    roots: list[str] = []
    for seed in seed_symbols or []:
        try:
            hits = cg_client.search_symbol(seed, repo, limit=3)
        except Exception:
            hits = []
        for h in hits[:1]:
            rid = ensure_node(getattr(h, "qualified_name", str(seed)), getattr(h, "path", ""), getattr(h, "line", 0))
            if rid:
                roots.append(rid)

    # 有界 BFS：双向展开
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
            callers = cg_client.get_callers(sym, repo, limit=max_per_node) or []
        except Exception:
            callees, callers = [], []
        for h in list(callees)[:max_per_node]:
            cid = ensure_node(getattr(h, "name", ""), getattr(h, "path", ""), getattr(h, "line", 0), getattr(h, "kind", ""))
            if cid is None:
                truncated = True; continue
            edges.append(GraphEdge(**{"from": nid, "to": cid, "kind": "calls", "status": "unknown"}))
            if cid not in visited:
                visited.add(cid); frontier.append((cid, depth + 1))
        for h in list(callers)[:max_per_node]:
            cid = ensure_node(getattr(h, "name", ""), getattr(h, "path", ""), getattr(h, "line", 0), getattr(h, "kind", ""))
            if cid is None:
                truncated = True; continue
            edges.append(GraphEdge(**{"from": cid, "to": nid, "kind": "calls", "status": "unknown"}))
            if cid not in visited:
                visited.add(cid); frontier.append((cid, depth + 1))

    node_list = list(nodes.values())
    # 去重边(同 from/to)
    seen_e = set(); uniq_edges = []
    for e in edges:
        k = (e.from_, e.to)
        if k in seen_e or e.from_ == e.to:
            continue
        seen_e.add(k); uniq_edges.append(e)
    if truncated:
        findings.append(Finding(severity="info", title=f"调用图已截断（max_nodes={max_nodes}）", kind="todo"))
    if not node_list:
        findings.append(Finding(severity="warn", title="未从 codegraph 解析到种子符号", kind="todo"))
    headline = f"静态调用图：{len(lanes)} 模块 / {len(node_list)} 函数 / {len(uniq_edges)} 调用关系"
    return ReportGraph(
        intent="consult", has_logs=False,
        verdict=Verdict(status="inconclusive", headline=headline, next_step="如需运行态确认，请提供日志/bug"),
        lanes=lanes, nodes=node_list, edges=uniq_edges,
        timeline=[], values=[], findings=findings,
    )
```
（确认 `Anchor`/`Finding`/`GraphNode`/`GraphEdge`/`Verdict`/`ReportGraph`/`_short_file` 在 graph_adapters.py 已可用——多数已 import；缺则补。）

- [ ] **Step 4: 通过 + 回归**（`python -m pytest tests/test_graph_adapters.py -v` 全过；全量仅 2 已知失败）

- [ ] **Step 5: 提交**
```
git add lark_agent_bridge/reporting/graph_adapters.py tests/test_graph_adapters.py
git commit -m "feat(reporting): build_consult_graph_from_codegraph (function-level static call graph, module lanes, defensive fallback)"
```

---

## Task L4-3: 咨询路径接入（禁旁路 + 建图 + 图优先渲染 + 保留 agent prose）

**Files:** `source_signal_report_html.py`（加 analysis_markdown 章节）、`source_analysis.py`（禁旁路+建图+渲染）；Test: 扩展 `tests/test_source_signal_report_html.py` + `tests/test_app_source_analysis.py`

- [ ] **Step 1: 写失败测试**
渲染器保留 prose：
```python
# tests/test_source_signal_report_html.py 追加
def test_render_keeps_agent_prose():
    import json
    from pathlib import Path as _P
    from lark_agent_bridge.reporting.graph_adapters import signal_json_to_graph
    from lark_agent_bridge.reporting.source_signal_report_html import render_signal_source_report
    g = signal_json_to_graph(json.load(open(_P(__file__).parent / "fixtures" / "signal_chain_40018.json")))
    g.intent = "consult"; g.has_logs = False
    html = render_signal_source_report(g, request_text="x", backend="codegraph",
                                       analysis_markdown="## 结论摘要\n- 这是 agent 的源码分析说明")
    assert "源码分析说明" in html
    assert "agent 的源码分析说明" in html
```

- [ ] **Step 2: 运行确认失败**（`analysis_markdown` 未知参数）

- [ ] **Step 3: 实现渲染器章节**
`source_signal_report_html.py` 的 `render_signal_source_report` 加可选参数 `analysis_markdown: str = ""`；当非空时，在 sections 字典加一项 `"prose": ("源码分析说明", f'<details class="fold"><summary>展开 agent 源码分析</summary><pre>{H(analysis_markdown)}</pre></details>')`，并把 `"prose"` 放入 consult 的 order 末尾（diagnose order 也可附在末尾）。签名：`def render_signal_source_report(graph, *, request_text, backend, analysis_markdown: str = "") -> str:`。

- [ ] **Step 4: 实现咨询路径接入（source_analysis.py）**
- **禁旁路**：在 app-server 旁路判断处（约 `:124-133`，`codex_app_server.enabled and use_for_file_agent` 时 return 旁路结果）增加条件：consult 路径跳过旁路。`_route_source_analysis` 进来的请求即 consult（无日志），最简：在该旁路判断加 `and not request_is_consult`（或直接在咨询入口不走旁路）。实现者读 `source_analysis.py:124-190` 确认旁路触发点，最小化跳过。
- **建图 + 渲染**（主路径 `:55-92`）：拿到 runner 结果后，构造 `cg_client = CodeGraphClient(...)`（按 `models.py` 配置 codegraph_command/timeout）、`repo = <guideengine_repo 或 repo_roots[0]>`；种子 = `[request.target]`（回退 `_extract_query_symbols`）；
```python
    from .reporting.graph_adapters import build_consult_graph_from_codegraph
    from .reporting.source_signal_report_html import render_signal_source_report
    graph = build_consult_graph_from_codegraph([request.target], cg_client, repo, request_text=request.prompt)
    html = render_signal_source_report(graph, request_text=request.prompt, backend="codegraph",
                                       analysis_markdown=result.answer or "")
    html_path.write_text(html, encoding="utf-8")
```
替换原 `render_source_analysis_report(...)`（`:80`）。`result.answer`/prose 字段名按实际 result 对象调整。

- [ ] **Step 5: 集成测试 + 回归**
扩展 `tests/test_app_source_analysis.py`：consult 请求产出的 HTML 含泳道 SVG + "未结合运行态" 标注 + 不出现 `v-green`（codegraph 不可用时走 inconclusive 兜底，断言含 finding 说明）。用 fake/缺失 codegraph 环境跑（CI 一般无 codegraph CLI → 命中兜底分支，正好测兜底）。
`python -m pytest tests/ -q` 仅 2 已知失败。

- [ ] **Step 6: 提交**
```
git add lark_agent_bridge/reporting/source_signal_report_html.py lark_agent_bridge/source_analysis.py tests/test_source_signal_report_html.py tests/test_app_source_analysis.py
git commit -m "feat(source-analysis): consult path renders codegraph static graph (diagram-first) + keeps agent prose; bypass disabled for consult"
```

---

## Self-Review
- **Spec 覆盖**：C7 函数级+标注模块=L4-2；空壳标绿修复=L4-1；咨询接入图优先+保留 prose=L4-3；用户决策①②已编码（切渲染+保留 prose / 禁旁路）。
- **占位符**：无 TBD；适配器全代码 + fake-client 测试钉住行为；接入处给精确落点 + 字段名按实际核对。
- **类型一致**：`build_consult_graph_from_codegraph` 产 `ReportGraph`（L4-2 定义，L4-3 消费）；`render_signal_source_report(..., analysis_markdown="")`（L4-3 定义/调用一致）；node.status 全 `unknown`（合法值），verdict `inconclusive`（合法 VerdictStatus）。
- **风险**：① codegraph CI 多半无索引→命中兜底（正好可测，且不伪 green）；② cg_client 字段名/构造按真实 `codegraph_client.py` 核对（实现者先读）；③ 禁旁路要精确定位旁路触发点，勿误伤非 consult 路径；④ `result.answer`/`request.target` 字段名按真实对象核对。

## Execution Handoff
subagent-driven，L4-1 与 L4-2 可并行（无共享状态），L4-3 依赖 L4-2。每任务 fresh subagent + 两段评审；L4-3 接入处评审重点核"禁旁路不误伤"与字段名正确性。
