# L2B：不生成两份 + source_stage 语义升级 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`).

**Goal:** 完成 decision-1 的「真正不生成两份中间 HTML」（signal `--json-only` + run_primary 合并时跳过 source_stage 个体渲染），并把 source_stage agent 从五段 markdown 升级为**额外结构化输出**（node status 标注 + findings），经 `apply_source_stage` 合并入 ReportGraph，让"根因判读/哪里 OK/哪里断"从确定性升级为 **AI 语义**。

**Architecture:** 三组。G1=B1(signal 脚本 `--json-only`)+6b(run_primary 跳过 source_stage 个体渲染)——**强耦合必须同组**（因 `run_primary.py:719` 对每个 plan 硬校验 HTML 存在，signal 不再产 HTML 会触发 `bug_analysis_missing_html`，6b 同时放宽该校验）。G2=source_stage 两条执行路径（pydantic-ai 结构化 / file_agent JSON 围栏）额外产出 `node_status`+`findings` 并回流到 `source_stage_report.json`。G3=`apply_source_stage(graph, data)` 按**文件名/label 匹配**（非 node.id）覆盖节点 status、注入 findings，在**渲染前**改图；解析失败**防御式回退**到确定性 status。

**关于跳过 RED（用户显式 `直接改`）：** 本计划不跑 RED 基线，改用**强 TDD + 防御式回退**兜底。回退是关键安全网：apply_source_stage 任何解析失败/字段缺失/非法值 → 保留 `signal_json_to_graph` 的确定性 status，AI 语义纯增益、绝不降级。

**Tech Stack:** Python 3.13、pytest。测试用 venv `python`（非 `python3`），不用 `timeout`。

依据 spec：`...2026-06-03-source-analysis-intent-aware-diagram-report-design.md`（C8、决策点 1/2）。承接 L2A（已交付）。

## Non-Goals
- 不改路由（沿 L2A）。
- 不改 `_prompt_requires_json_fence`（custom_skill.py:1077）的全局判定——B2 用独立 `emit_node_status` 标志，仅 source_stage 注入 JSON 围栏，绝不误开其它 kind 的 JSON 输出。
- 不手改 signal 脚本的 4 份同步副本（`.claude/.codex/.github/.qoder`）——只改 `.ai` source-of-truth（bridge 经 `bug_cache.py:818` 调它）；副本漂移用 `.ai/tools` 同步工具事后再生（本计划记为收尾事项，不阻塞）。

## File Structure
- Modify `lark_agent_bridge/agents/bug/bug_cache.py:787-799`（argv 加 `--json-only`）
- Modify `.ai/skills/signal-chain-analyzer/scripts/analyze_signal_chain.py`（argparse + write_report 跳过 HTML）
- Modify `lark_agent_bridge/agents/bug/run_primary.py`（will_merge 预判 + 放宽 719 校验 + 条件 append 738 + 透传 render_html）
- Modify `lark_agent_bridge/agents/bug/bug_prompt.py`（`_write_custom_skill_agent_report` 加 `render_html` + file_agent 路径解析 JSON 尾块）
- Modify `lark_agent_bridge/agents/bug/ld_executor.py`（透传 render_html + pydantic-ai system_prompt 加结构化指令 + extra_payload 回填）
- Modify `lark_agent_bridge/agents/bug/agent_output_models.py:39-60`（SourceAnalysisOutput 加 node_status/findings）
- Modify `lark_agent_bridge/agents/bug/custom_skill.py:408-429,597-619`（file_agent prompt 注入 JSON 围栏指令，emit_node_status 门控）
- Modify `lark_agent_bridge/reporting/graph_adapters.py`（新增 `apply_source_stage`）
- Modify `lark_agent_bridge/reporting/source_signal_report_html.py:130-150`（build_combined 加 `source_stage_data` 形参，渲染前 apply）
- Modify `lark_agent_bridge/agents/bug/general_summary.py:603-617`（读 source_stage JSON 传入）
- Tests: `tests/test_signal_json_only.py`、`tests/test_combined_signal_source.py`（扩展）、`tests/test_apply_source_stage.py`

---

## Task G1: B1（signal --json-only）+ 6b（合并时跳过个体渲染）—— 同组

**Files:** `bug_cache.py`、`.ai/.../analyze_signal_chain.py`、`run_primary.py`、`bug_prompt.py`、`ld_executor.py`
**Test:** `tests/test_signal_json_only.py`、扩展 `tests/test_combined_signal_source.py`

- [ ] **Step 1: 写失败测试**
脚本级（`tests/test_signal_json_only.py`）：
```python
import json, subprocess, sys
from pathlib import Path

SCRIPT = Path("/Users/zhuyl/Documents/workspace/.ai/skills/signal-chain-analyzer/scripts/analyze_signal_chain.py")


def test_json_only_skips_html(tmp_path):
    # 用一个最小日志目录跑脚本；--json-only 应产 JSON、不产 HTML、returncode 0
    out_html = tmp_path / "r.html"
    out_json = tmp_path / "r.json"
    logdir = tmp_path / "logs"; logdir.mkdir()
    (logdir / "empty.log").write_text("", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--signal-code", "SIGNAL_VCU_ELECTRICIT_PERCENT",
         "--log-path", str(logdir), "--output", str(out_html), "--json-output", str(out_json), "--json-only"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert out_json.exists()
    assert not out_html.exists()
```
> 若脚本对空日志目录非 0 退出，改用一个最小但合法的样例日志（参考 `.ai/skills/signal-chain-analyzer/evals` 下样例），关键断言不变：JSON 存在、HTML 不存在、returncode 0。

集成级（扩展 `tests/test_combined_signal_source.py`，复用既有夹具）：断言 signal+source_stage 合并时：combined HTML 生成、**个体 source_stage HTML 不在磁盘**、`report_jsons['source_stage']` 仍有 JSON、不触发 `bug_analysis_missing_html`。

- [ ] **Step 2: 运行确认失败**（`--json-only` 未知参数 → 脚本报错；集成测试因 missing_html 失败）

- [ ] **Step 3: 实现 B1**
`.ai/skills/signal-chain-analyzer/scripts/analyze_signal_chain.py`：
- argparse（约 2296，`--target-time` 之后、`parse_args()` 之前）：`parser.add_argument("--json-only", action="store_true", help="Only write JSON, skip HTML render")`
- `write_report`（约 1887 签名）加 `json_only: bool = False`；在 JSON 写出（约 1932 `output_json.write_text(...)`）之后立即 `if json_only: return`（跳过 1934-2197 的 HTML 构建与写出）
- main() 调 write_report（约 2351-2362）传 `json_only=args.json_only`；约 2364 `print(f"[OK] HTML: ...")` 用 `if not args.json_only:` 包裹
`lark_agent_bridge/agents/bug/bug_cache.py`：command 列表（约 787-799）末尾、`return` 前 `command.append("--json-only")`。

- [ ] **Step 4: 实现 6b**（run_primary.py）
- loop init（368 后、370 前）：
```python
            kinds = [p.kind for p in plans]
            will_merge = "signal" in kinds and "source_stage" in kinds  # 必须与 general_summary.py:603 逐字一致
```
- 放宽 missing_html 硬校验（719）：
```python
                if not current_html.exists() and not (
                    will_merge and current_plan.kind in ("signal", "source_stage")
                ):
```
- 条件 append（738）：
```python
                if not (will_merge and current_plan.kind in ("signal", "source_stage")):
                    html_paths.append(current_html)
```
（739 `report_jsons[...] = ...` 不变——合并必读 JSON。）
- 透传 `render_html=not will_merge` 到 source_stage 执行：`bug_prompt.py:_write_custom_skill_agent_report`（约 518）签名加 `render_html: bool = True`，把 HTML 写出（约 628-631）包到 `if render_html:`（JSON 632 始终写）；沿调用链 `ld_executor.py:458` 与 `1055` 两处调用加 `render_html=...`，对应执行方法新增 kwarg；`run_primary.py:542/568/598/622` 的 source_stage 调用处传 `render_html=not will_merge`。

- [ ] **Step 5: 运行确认通过 + 全量回归**
`python -m pytest tests/test_signal_json_only.py tests/test_combined_signal_source.py -v` 通过；`python -m pytest tests/ -q` 仅 2 个已知 pydantic_ai 失败。

- [ ] **Step 6: 提交**（精确 add 仓内文件；脚本在 .ai 仓外，单独 add 其绝对路径或在其 git 仓提交——确认 `.ai` 是否同一 git 仓：`git -C /Users/zhuyl/Documents/workspace/.ai status`，按实际仓提交脚本改动）
```
git add lark_agent_bridge/agents/bug/bug_cache.py lark_agent_bridge/agents/bug/run_primary.py lark_agent_bridge/agents/bug/bug_prompt.py lark_agent_bridge/agents/bug/ld_executor.py tests/test_signal_json_only.py tests/test_combined_signal_source.py
git commit -m "feat(bug): stop generating both intermediate HTMLs (signal --json-only + skip source_stage render on merge)"
```
> 注：`.ai/.../analyze_signal_chain.py` 若属另一 git 仓/工作区，按该仓单独提交，并记一条收尾事项：用 `.ai/tools` 同步 4 份副本。

---

## Task G2: source_stage 结构化输出（node_status + findings）

**Files:** `agent_output_models.py`、`ld_executor.py`、`custom_skill.py`、`bug_prompt.py`
**Test:** 扩展 `tests/test_combined_signal_source.py` 或新建 `tests/test_source_stage_structured.py`

目标：两条执行路径都让 `source_stage_report.json` 携带 `node_status`（文件名→ok/suspect/broken/unknown）与 `findings`（含 file/severity/title）。

- [ ] **Step 1: 写失败测试**
- pydantic-ai：`SourceAnalysisOutput(...).model_dump()` 含 `node_status`/`findings` 字段（默认空）。
- file_agent：给 `_write_custom_skill_agent_report` 一段带 JSON 尾块的 analysis markdown（`...\n\`\`\`json\n{"node_status":{"DataCenter.kt":"ok"},"findings":[{"file":"x","severity":"warn","title":"t"}]}\n\`\`\``），断言写出的 source_stage JSON payload 含 `node_status`/`findings`；无尾块时 payload 不含但不报错。

- [ ] **Step 2: 运行确认失败**

- [ ] **Step 3: 实现**
- `agent_output_models.py:39-60` `SourceAnalysisOutput` 加：
```python
    node_status: dict[str, str] = Field(default_factory=dict, description="源码文件名→ok/suspect/broken/unknown：标注链路节点是否打通")
    findings: list[dict] = Field(default_factory=list, description="每项含 file/severity/title/kind")
```
- pydantic-ai 路径：`ld_executor.py:905-941` system_prompt 工作要求末尾追加一条要求输出 node_status/findings 的指令；`ld_executor.py:1054-1074` 调 `_write_custom_skill_agent_report` 时把 `result.output.node_status`/`findings` 经 `extra_payload`（bug_prompt.py:539/626-627）写进 source_stage JSON。
- file_agent 路径：custom_skill.py 用**独立标志** `emit_node_status`（仅 source_stage 为真，**不复用** `_prompt_requires_json_fence`）在 prompt（约 408-429 context + 597-619 喂 prompt）注入"正文后追加一个 json 围栏，含 node_status 与 findings"指令；在 `bug_prompt.py:_write_custom_skill_agent_report`（约 541 读 analysis_text 后、604 组 payload 前）用简单 fence regex 抽取末尾 ```json 块，`json.loads` 取 node_status/findings 合并进 payload。
- **两路写进 payload 的字段名必须一致**（`node_status`/`findings`），合并分支才能稳定取键。

- [ ] **Step 4: 通过 + 回归**（同 G1 标准）

- [ ] **Step 5: 提交**
```
git add lark_agent_bridge/agents/bug/agent_output_models.py lark_agent_bridge/agents/bug/ld_executor.py lark_agent_bridge/agents/bug/custom_skill.py lark_agent_bridge/agents/bug/bug_prompt.py tests/test_source_stage_structured.py
git commit -m "feat(bug): source_stage emits structured node_status + findings (both exec paths)"
```

---

## Task G3: apply_source_stage（语义合并入图，防御式回退）

**Files:** `graph_adapters.py`、`source_signal_report_html.py`、`general_summary.py`
**Test:** `tests/test_apply_source_stage.py`

- [ ] **Step 1: 写失败测试**
```python
import json
from pathlib import Path
from lark_agent_bridge.reporting.graph_adapters import signal_json_to_graph, apply_source_stage
from lark_agent_bridge.reporting.report_graph import validate

FIX = Path(__file__).parent / "fixtures" / "signal_chain_40018.json"


def _graph():
    return signal_json_to_graph(json.load(open(FIX)))


def test_apply_overrides_status_by_filename():
    g = _graph()
    # datacenter 节点 anchor 文件名 DataCenter.kt
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
    assert [(n.id, n.status) for n in g.nodes] == statuses  # 不变、不抛


def test_apply_illegal_status_is_skipped():
    g = _graph()
    dc = next(n for n in g.nodes if n.lane == "datacenter")
    orig = dc.status
    apply_source_stage(g, {"node_status": {"DataCenter.kt": "正常"}, "findings": []})  # 非法值
    assert dc.status == orig  # 跳过非法值、保留确定性
    assert validate(g) == []
```

- [ ] **Step 2: 运行确认失败**

- [ ] **Step 3: 实现 apply_source_stage（graph_adapters.py 新增）**
```python
from .report_graph import Finding, ReportGraph  # 确认已导入

_VALID_STATUS = {"ok", "suspect", "broken", "unknown"}


def apply_source_stage(graph: ReportGraph, source_stage_data: dict | None) -> None:
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
                        continue  # 跳过非法值，保留确定性
                    if any(fname and fname in k for k in keys) or fname in keys:
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
    except Exception:
        return  # 任何异常都回退到确定性 status
```

- [ ] **Step 4: 渲染前 apply（source_signal_report_html.py:130-150）**
`build_combined_from_signal_json` 加形参 `source_stage_data: dict | None = None`；在 `graph = signal_json_to_graph(payload)` 之后、`graph.has_logs=...`/`graph.intent=...` 设置后、`render_signal_source_report(...)` 之前插入：
```python
    from .graph_adapters import apply_source_stage
    apply_source_stage(graph, source_stage_data)
```

- [ ] **Step 5: 合并分支读 source_stage JSON 传入（general_summary.py:603-617）**
在 `[signal, source_stage]` 分支，读取 `report_jsons.get("source_stage")` 的 JSON（防御式）取 `{node_status, findings}`，传 `source_stage_data=` 给 `build_combined_from_signal_json`：
```python
            source_stage_data = None
            ss_json = report_jsons.get("source_stage")
            if ss_json is not None and ss_json.exists():
                try:
                    ss_payload = json.loads(ss_json.read_text(encoding="utf-8"))
                    source_stage_data = {
                        "node_status": ss_payload.get("node_status") or {},
                        "findings": ss_payload.get("findings") or [],
                    }
                except Exception:
                    source_stage_data = None
            html, graph_dict = build_combined_from_signal_json(
                signal_json_path, request_text=prompt_text,
                has_logs=selected_input is not None, intent=intent,
                source_stage_data=source_stage_data,
            )
```

- [ ] **Step 6: 通过 + 回归 + 提交**
`python -m pytest tests/test_apply_source_stage.py tests/test_combined_signal_source.py -v` 通过；`python -m pytest tests/ -q` 仅 2 已知失败。
```
git add lark_agent_bridge/reporting/graph_adapters.py lark_agent_bridge/reporting/source_signal_report_html.py lark_agent_bridge/agents/bug/general_summary.py tests/test_apply_source_stage.py
git commit -m "feat(reporting): apply_source_stage merges AI node_status+findings into graph (filename match, defensive fallback)"
```

---

## Self-Review
- **Spec 覆盖**：决策点1 不生成=G1；决策点2/C8 source_stage 结构化=G2；C5 语义判读上图=G3。
- **占位符**：无 TBD；新函数全代码，threading 给精确 file:line + 变换说明（执行者按实际行号核对，沿 L1/L2A 惯例）。
- **类型一致**：`node_status`/`findings` 字段名贯穿 G2(产出)→G3(消费)；`apply_source_stage(graph, data)` 签名 G3 定义/调用一致；status 值域夹到 `_NODE_STATUSES`。
- **关键风险**：① will_merge 表达式逐字对齐 general_summary.py:603；② 跳过 RED → 全靠 G3 防御式回退兜底（AI 语义纯增益）；③ 不动 `_prompt_requires_json_fence` 全局；④ signal 脚本只改 `.ai` 副本（bridge 调它），其余 4 份用 `.ai/tools` 同步（收尾事项）。

## Execution Handoff
subagent-driven 执行，G1（同组）→ G2 → G3。每组 fresh subagent + 两段评审；G1 因耦合最易踩 missing_html 地雷，评审重点核 will_merge 一致性。
