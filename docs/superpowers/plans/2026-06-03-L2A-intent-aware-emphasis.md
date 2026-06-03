# L2A 意图维度 + 按 (intent, has_logs) 侧重 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 让源码/信号调查报告的**侧重随意图自适应**：分类器产出 `intent = consult | diagnose`（纯启发式，不新增 LLM），透传到图优先合并报告渲染器，按 `(intent, has_logs)` 调整章节顺序与 verdict 措辞；并由此**顺带修复"带日志的咨询被劫持成诊断"**（decision-3b：不改路由）。

**Architecture:** 纯仓内、无需 RED。`intent` 字段加在 `SourceAnalysisDecision`（`_shared.py:552`），在 `resolve_source._decide_source_analysis_request`（`resolve_source.py:312`）由文本启发式产出初判；`run_primary` 在合并调用处用 `selected_input`（has_logs）做最终裁决并透传到 `_build_combined_report_artifacts` → `build_combined_from_signal_json` → `graph.intent`。渲染器 `render_signal_source_report` 直接读 `graph.intent`/`graph.has_logs`（单一真相源）重排章节 + 调措辞。

**Tech Stack:** Python 3.13、dataclasses、pytest。测试用 venv `python`（不是 `python3`），不使用 `timeout` 命令。

依据 spec：`docs/superpowers/specs/2026-06-03-source-analysis-intent-aware-diagram-report-design.md`（C3/C4/C5、决策点 3）。承接 L1（`docs/superpowers/plans/2026-06-03-L1-source-signal-diagram-report.md`，已交付）。

## Scope（含 L2 拆分说明）

L2 拆成两个计划：

- **本计划 L2A（纯仓内、无需 RED）**：intent 维度 + 透传 + 侧重渲染 + 劫持修复（纯逻辑）。
- **L2B（后续，需 RED 基线）**：① source_stage agent 结构化输出（status 标注 + findings，`apply_source_stage` 合并入图）② signal 脚本 `--json-only` ③ 6b 完整"不生成"（run_primary 跳过个体渲染 + signal 不双写）。这些改 workspace skill / skill prompt，按全局 CLAUDE.md 第 6 条必须先跑 RED 基线，故单独成计划。

**Non-Goals（明确排除，防误改）：**
- **不改路由拦截** `parser.py:753-754`、`handle_event.py:642-645`。这是被否决的方案(a)，会把 consult-with-logs 的用户日志丢弃。decision-3b 的劫持修复是 intent 透传 + 渲染分流的**副产物**，无独立路由改动。
- 不在 L2A 改任何 skill 脚本 / SKILL.md / agent prompt 行为（属 L2B）。
- 不做 source_stage 语义内容消费（属 L2B B2）。

## File Structure
- Modify `lark_agent_bridge/agents/bug/_shared.py:552` — `SourceAnalysisDecision` 加 `intent: str = ""`；新增模块级纯函数 `infer_intent_from_text` / `resolve_effective_intent` + 词表常量。
- Modify `lark_agent_bridge/agents/bug/resolve_source.py:312` — 构造 `SourceAnalysisDecision` 时传 `intent=...`。
- Modify `lark_agent_bridge/agents/bug/run_primary.py:789` — 合并调用处计算 effective intent 并透传。
- Modify `lark_agent_bridge/agents/bug/general_summary.py:548` — `_build_combined_report_artifacts` 加 `intent` 参，分支内透传。
- Modify `lark_agent_bridge/reporting/source_signal_report_html.py` — `build_combined_from_signal_json` 加 `intent` 参写入 `graph.intent`；`render_signal_source_report` 按 `graph.intent`/`has_logs` 侧重。
- Create `tests/test_intent_inference.py`、追加 `tests/test_source_signal_report_html.py`。

---

## Task A1: intent 启发式推断 + 字段

**Files:**
- Modify: `lark_agent_bridge/agents/bug/_shared.py`
- Modify: `lark_agent_bridge/agents/bug/resolve_source.py`
- Test: `tests/test_intent_inference.py`

判据（纯启发式，不复用路由触发词——语义错配）：症状词 → diagnose；咨询词 → consult；都不命中 → ""（空，由 run_primary 用 has_logs 兜底裁决）。

- [ ] **Step 1: 写失败测试**
```python
# tests/test_intent_inference.py
from lark_agent_bridge.agents.bug._shared import infer_intent_from_text, resolve_effective_intent


def test_symptom_text_is_diagnose():
    assert infer_intent_from_text("为什么收不到 SIGNAL_VCU_ELECTRICIT_PERCENT，场景里看不到") == "diagnose"


def test_consult_text_is_consult():
    assert infer_intent_from_text("了解下这个信号怎么接入、涉及哪些模块、链路怎么走") == "consult"


def test_neutral_text_is_empty():
    assert infer_intent_from_text("SIGNAL_VCU_ELECTRICIT_PERCENT") == ""


def test_effective_intent_keeps_explicit():
    # 显式 consult 即使带日志也保持 consult —— 这是"带日志的咨询不被劫持"的核心
    assert resolve_effective_intent("consult", has_logs=True) == "consult"
    assert resolve_effective_intent("diagnose", has_logs=False) == "diagnose"


def test_effective_intent_falls_back_on_logs():
    assert resolve_effective_intent("", has_logs=True) == "diagnose"
    assert resolve_effective_intent("", has_logs=False) == "consult"
```

- [ ] **Step 2: 运行确认失败**
Run: `python -m pytest tests/test_intent_inference.py -v`
Expected: FAIL，ImportError（函数不存在）。

- [ ] **Step 3: 实现**
在 `lark_agent_bridge/agents/bug/_shared.py` 顶部（dataclass 定义附近、模块级）新增：
```python
_DIAGNOSE_TERMS: tuple[str, ...] = (
    "收不到", "没收到", "看不到", "不显示", "没显示", "不展示", "没展示",
    "为什么没", "为啥没", "为何没", "黑屏", "卡住", "卡死", "闪退", "崩溃",
    "掉帧", "不刷新", "报错", "异常", "ANR", "无法", "失败", "没反应", "丢失", "不生效",
)
_CONSULT_TERMS: tuple[str, ...] = (
    "了解", "怎么接入", "如何接入", "涉及哪些模块", "涉及哪些", "链路怎么走",
    "怎么走", "数据怎么来", "从哪来", "从哪里来", "怎么分发", "如何分发",
    "注册在哪", "在哪注册", "梳理", "讲解", "说明", "介绍", "怎么实现", "如何实现",
)


def infer_intent_from_text(text: str) -> str:
    """纯文本启发式意图初判：diagnose / consult / ""（不确定）。"""
    blob = text or ""
    if any(term in blob for term in _DIAGNOSE_TERMS):
        return "diagnose"
    if any(term in blob for term in _CONSULT_TERMS):
        return "consult"
    return ""


def resolve_effective_intent(text_intent: str, *, has_logs: bool) -> str:
    """最终裁决：显式初判优先；不确定时按是否有日志兜底（有日志→诊断，无→咨询）。"""
    if text_intent in {"consult", "diagnose"}:
        return text_intent
    return "diagnose" if has_logs else "consult"
```
在 `SourceAnalysisDecision`（`_shared.py:552-562`）的字段末尾加：
```python
    intent: str = ""
```
在 `lark_agent_bridge/agents/bug/resolve_source.py` 的 `_decide_source_analysis_request` 构造处（`resolve_source.py:312-322` 的 `return SourceAnalysisDecision(...)`）增加一行实参：
```python
            stage_kinds=[stage.kind for stage in analysis_plan.stages],
            intent=infer_intent_from_text(f"{prompt_text} {request_text}"),
```
确认 `infer_intent_from_text` 已从 `_shared` 导入（该文件已 `from ._shared import ... SourceAnalysisDecision ...`，把 `infer_intent_from_text` 加入同一 import）。

- [ ] **Step 4: 运行确认通过**
Run: `python -m pytest tests/test_intent_inference.py -v`
Expected: 5 passed。

- [ ] **Step 5: 提交**
```
git add lark_agent_bridge/agents/bug/_shared.py lark_agent_bridge/agents/bug/resolve_source.py tests/test_intent_inference.py
git commit -m "feat(bug): heuristic consult|diagnose intent on SourceAnalysisDecision"
```

---

## Task A2: 透传 intent 到合并渲染器

**Files:**
- Modify: `lark_agent_bridge/reporting/source_signal_report_html.py`（`build_combined_from_signal_json`）
- Modify: `lark_agent_bridge/agents/bug/general_summary.py`（`_build_combined_report_artifacts`）
- Modify: `lark_agent_bridge/agents/bug/run_primary.py`（合并调用处）
- Test: 追加到 `tests/test_source_signal_report_html.py`

- [ ] **Step 1: 写失败测试（追加）**
```python
def test_build_combined_threads_intent(tmp_path):
    import shutil
    from pathlib import Path as _P
    from lark_agent_bridge.reporting.source_signal_report_html import build_combined_from_signal_json
    src = _P(__file__).parent / "fixtures" / "signal_chain_40018.json"
    dst = tmp_path / "bug_signal_chain_report.json"
    shutil.copy(src, dst)
    _, gd_consult = build_combined_from_signal_json(dst, request_text="x", has_logs=True, intent="consult")
    assert gd_consult["intent"] == "consult"
    _, gd_default = build_combined_from_signal_json(dst, request_text="x", has_logs=True)
    assert gd_default["intent"] == "diagnose"  # 适配器默认，空 intent 不覆盖
```

- [ ] **Step 2: 运行确认失败** (`TypeError: unexpected keyword 'intent'`)
Run: `python -m pytest tests/test_source_signal_report_html.py::test_build_combined_threads_intent -v`

- [ ] **Step 3: 实现**
`source_signal_report_html.py:107` 的 `build_combined_from_signal_json` 加参并写入 graph.intent：
```python
def build_combined_from_signal_json(
    signal_json_path: Path,
    *,
    request_text: str,
    has_logs: bool,
    backend: str = "signal-chain-analyzer",
    intent: str = "",
) -> tuple[str, dict]:
    from .graph_adapters import signal_json_to_graph

    payload = _json.loads(Path(signal_json_path).read_text(encoding="utf-8"))
    graph = signal_json_to_graph(payload)
    graph.has_logs = bool(has_logs)
    if intent:
        graph.intent = intent
    html = render_signal_source_report(graph, request_text=request_text, backend=backend)
    return html, graph.to_dict()
```
`general_summary.py:548` 的 `_build_combined_report_artifacts` 签名加 `intent: str = ""`（放在 `source_evidence_path` 之后即可），并在 `[signal, source_stage]` 分支（`general_summary.py:607-611`）的调用加 `intent=intent`：
```python
            html, graph_dict = build_combined_from_signal_json(
                signal_json_path,
                request_text=prompt_text,
                has_logs=selected_input is not None,
                intent=intent,
            )
```
`run_primary.py:789` 的调用处增传（`source_decision` 在该 scope 内已存在，见 run_primary.py:213-214）：
```python
            combined_artifacts = self._build_combined_report_artifacts(
                plans=plans,
                prompt_text=prompt_text,
                fault_time=fault_time,
                output_dir=context.output_dir,
                html_paths=html_paths,
                report_jsons=report_jsons,
                selected_input=selected_input,
                source_evidence_path=source_evidence_path,
                intent=resolve_effective_intent(
                    getattr(source_decision, "intent", ""),
                    has_logs=selected_input is not None,
                ),
            )
```
在 run_primary.py 顶部 import 加上 `resolve_effective_intent`（与现有 `from ._shared import ...` 合并；确认无循环导入——`_shared` 不 import run_primary）。若该 scope 内变量名不是 `source_decision`，按实际名（如 `decision.source_decision`）调整 `getattr` 的对象。

- [ ] **Step 4: 运行确认通过 + 局部回归**
Run: `python -m pytest tests/test_source_signal_report_html.py -v`
Expected: 全部通过（含新加的 intent 透传测试）。

- [ ] **Step 5: 提交**
```
git add lark_agent_bridge/reporting/source_signal_report_html.py lark_agent_bridge/agents/bug/general_summary.py lark_agent_bridge/agents/bug/run_primary.py tests/test_source_signal_report_html.py
git commit -m "feat(bug): thread effective intent (consult|diagnose) into combined report renderer"
```

---

## Task A3: 按 (intent, has_logs) 侧重渲染 + 劫持修复测试

**Files:**
- Modify: `lark_agent_bridge/reporting/source_signal_report_html.py`（`render_signal_source_report`）
- Test: 追加到 `tests/test_source_signal_report_html.py`

侧重规则：
- **章节顺序**：consult → [泳道图, 生命周期, 值变化, 根因判读]（架构/链路优先，findings 收尾）；diagnose → [泳道图, 根因判读, 生命周期, 值变化]（断开点/根因紧跟图）。
- **verdict 措辞**：intent 前缀「链路咨询」/「故障诊断」；`has_logs=False` 时附「（未结合运行态：基于源码/缓存推断）」。
- 去掉固定 ①②③④ 编号（重排后编号会乱），用纯标题。

- [ ] **Step 1: 写失败测试（追加）**
```python
def _render_with(intent, has_logs):
    import json
    from pathlib import Path as _P
    from lark_agent_bridge.reporting.graph_adapters import signal_json_to_graph
    from lark_agent_bridge.reporting.source_signal_report_html import render_signal_source_report
    g = signal_json_to_graph(json.load(open(_P(__file__).parent / "fixtures" / "signal_chain_40018.json")))
    g.intent = intent
    g.has_logs = has_logs
    return render_signal_source_report(g, request_text="x", backend="y")


def test_diagnose_puts_rootcause_before_lifecycle():
    html = _render_with("diagnose", True)
    assert html.index("根因判读") < html.index("生命周期")


def test_consult_puts_lifecycle_before_rootcause():
    html = _render_with("consult", True)
    assert html.index("生命周期") < html.index("根因判读")


def test_verdict_labels_intent():
    assert "故障诊断" in _render_with("diagnose", True)
    assert "链路咨询" in _render_with("consult", True)


def test_no_logs_annotates_verdict():
    assert "未结合运行态" in _render_with("consult", False)


def test_consult_with_logs_gets_consult_emphasis():
    # decision-3b：带日志的咨询仍是咨询侧重（劫持修复的可观察结果）；无路由改动。
    html = _render_with("consult", True)
    assert "链路咨询" in html
    assert html.index("生命周期") < html.index("根因判读")
```

- [ ] **Step 2: 运行确认失败**
Run: `python -m pytest tests/test_source_signal_report_html.py -v`
Expected: 新增 5 个 FAIL（旧 8 个仍过）。

- [ ] **Step 3: 实现** —— 重写 `render_signal_source_report`（替换 L1 版本，保留各 `_xxx` 辅助函数不变）：
```python
def _verdict_block(graph: ReportGraph) -> str:
    sev = _VERDICT_CLASS.get(graph.verdict.status, "yellow")
    label = "故障诊断" if graph.intent == "diagnose" else "链路咨询" if graph.intent == "consult" else "源码分析"
    runtime_note = "" if graph.has_logs else ' <span class="muted">（未结合运行态：基于源码/缓存推断）</span>'
    next_step = (
        f'<div class="muted">下一步：{H(graph.verdict.next_step)}</div>'
        if graph.verdict.next_step else ""
    )
    return (
        f'<div class="verdict v-{sev}"><b>[{H(label)}]</b> {H(graph.verdict.headline)}{runtime_note}</div>'
        f'{next_step}'
    )


def render_signal_source_report(graph: ReportGraph, *, request_text: str, backend: str) -> str:
    node_payload = [
        {
            "id": n.id,
            "lane_title": next((str(l.get("title")) for l in graph.lanes if l.get("id") == n.lane), n.lane),
            "label": n.label,
            "status": n.status,
            "num": i,
        }
        for i, n in enumerate(graph.nodes, 1)
    ]
    edge_payload = [{"from": e.from_, "to": e.to} for e in graph.edges]
    swimlane_html = (
        f'{render_status_lane_graph(node_payload, edge_payload)}'
        '<div class="muted">节点描边色：绿=ok 橙=待确认 红=断开。展开看源码锚点与原始日志：</div>'
        f'{_evidence_cards(graph)}'
    )
    sections = {
        "swimlane": ("数据流泳道图", swimlane_html),
        "timeline": ("生命周期时间线", _timeline(graph)),
        "values": ("值变化轨道", _value_track(graph)),
        "findings": ("根因判读 / 风险 / 待确认", _findings(graph)),
    }
    if graph.intent == "consult":
        order = ["swimlane", "timeline", "values", "findings"]
    else:  # diagnose 或默认
        order = ["swimlane", "findings", "timeline", "values"]
    sections_html = "".join(
        f'<div class="section"><h2>{H(sections[key][0])}</h2>{sections[key][1]}</div>'
        for key in order
    )
    body = (
        '<div class="container">'
        "<h1>源码/信号调查报告</h1>"
        f'<div class="sub">请求：{H(request_text)} · 后端：{H(backend)}</div>'
        f'{_verdict_block(graph)}'
        f'{sections_html}'
        "</div>"
    )
    return render_document("源码/信号调查报告", body, css=BASE_REPORT_CSS + _EXTRA_CSS)
```

- [ ] **Step 4: 运行确认通过（含 L1 旧测试不回归）**
Run: `python -m pytest tests/test_source_signal_report_html.py -v`
Expected: 全过（L1 的 8 个 + A2 的 1 个 + A3 的 5 个）。注意 L1 的 `test_report_has_swimlane_and_sections` 断言 "生命周期"/"值变化" 子串仍在（重排后仍存在）。

- [ ] **Step 5: 全量回归 + 提交**
Run: `python -m pytest tests/ -q`
Expected: 仅 2 个预先存在的 `pydantic_ai` 失败，无新增。
```
git add lark_agent_bridge/reporting/source_signal_report_html.py tests/test_source_signal_report_html.py
git commit -m "feat(reporting): intent x has_logs emphasis in report (section order + verdict framing); covers consult-with-logs (decision-3b)"
```

---

## Self-Review
- **Spec 覆盖**：C3 intent 维度=A1；透传=A2；C5 侧重渲染=A3；C4/decision-3b 劫持修复=A3 的 `test_consult_with_logs_gets_consult_emphasis`（无路由改动，已列 Non-Goal）。L2B（skill 改动 + 6b + source_stage 语义）不在本计划。
- **占位符扫描**：无 TBD；每步含可运行代码与确切命令/期望。
- **类型一致性**：`infer_intent_from_text`/`resolve_effective_intent`（A1 定义，A2 run_primary 用）；`build_combined_from_signal_json(..., intent="")`（A2 定义，general_summary 调）；`render_signal_source_report` 读 `graph.intent`/`graph.has_logs`（A3，字段在 report_graph 已存在）。
- **执行风险**：run_primary 合并调用处的 `source_decision` 变量名需对齐实际（基线显示 run_primary.py:213-214 `source_decision = decision.source_decision`）；若名字不同按实际调整 `getattr` 目标。`_shared` 加纯函数注意不要引入对 run_primary 的反向 import（保持 `_shared` 无重依赖）。

---

## Execution Handoff
Plan complete. Execute via superpowers:subagent-driven-development（fresh subagent per task + 两段评审）。L2B（需 RED 基线的 skill 改动 + 6b 完整不生成 + source_stage 语义消费）随后单独成计划。
