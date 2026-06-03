# 源码分析：图优先 + 意图侧重的统一调查报告设计

## Goal

把当前"源码分析类请求"的产出，从**双报告 + 固定五段模板**，改造成**单份、图优先、侧重随意图自适应**的调查报告，并落一套可复用的 `ReportGraph` 数据契约，使任何分析只要产出该契约就能图形化渲染。

驱动这次改造的真实案例：Bug `6998811703`，调查信号 `SIGNAL_VCU_ELECTRICIT_PERCENT`（=40018），命中 `signal-chain-analyzer`，同时产出了 `bug_signal_chain_report.html`（信号链路）和 `source_stage_report.html`（源码阶段）两份报告。

## Scope

本次落地 **L1–L4 全部**：

- **L1 报告层**：`ReportGraph` 契约 + 合并为单份报告 + 图优先渲染（SVG 泳道，节点按状态染色，节点可展开 `file:line` + 原始日志证据）。
- **L2 意图维度**：分类器产出 `intent = consult | diagnose` 标签（启发式，不新增 LLM 调用）；报告章节权重、verdict 措辞、source_stage prompt 按 `(intent, has_logs)` 自适应。
- **L3 意图劫持修复**：带日志/附件的"咨询"请求不再被强制当诊断处理。
- **L4 咨询路径静态结构图**：基于 `codegraph_client` 的模块级调用/依赖图生成器，根治咨询路径"空壳占位/只取前 4 条证据"的低质问题。

不覆盖：重做整个 intent runner；重构所有结果卡样式；改 bug 报告（startup/stuck/crash/general）的现有外壳。

## 现状问题（体检结论，作为改造依据）

入口分流本身是健康的（`_route_bug_intent` #6 = 诊断 vs `_route_source_analysis` 仲裁 #10 = 咨询），但：

1. **🔴 意图被附带材料劫持（broken）**：`handle_event.py:642-645`（镜像 `532-542`）+ `parser.py:753-754` —— 只要带 bug 链接 / 可下载附件，咨询路径被无条件踢出，强制走 `_route_bug_intent`（#6 先于仲裁 #10 抢先返回）。"想了解链路"的咨询只要附了 bug 链接就拿不到咨询报告。
2. **🔴 没有意图维度**：分类器 `resolve_source.py:712-784 _classify` 只产 `analysis_kind/skill/signal_hint`，无 `consult|diagnose`，下游拿不到意图切侧重。
3. **🔴 报告侧重全写死**：五段模板与章节顺序硬编码于 `custom_skill.py:418`、`ld_executor.py:943-963`、`source_report_html.py:194-212`、`bug_prompt.py:475-516`；咨询型与诊断型得到字面相同输出。
4. **🟡 日志开关没接通侧重**：`has_logs = selected_input is not None` 仅在 `general_summary.py:204` 用于调 verdict 颜色，没接到 source_stage 模板和咨询渲染器。
5. **🟡 咨询路径产出弱**：全仓无静态模块/架构图生成器（只有 `codegraph_client.py:184-204` 符号级 `get_callers/get_callees`）；无证据时退化成固定占位骨架（`source_report_html.py:448-456`）；app-server 分路写死 `source_evidence=[]` 仍 `success=True`（`source_analysis.py:183`），空壳也标绿/黄。

场景矩阵（当前表现）：

| 场景 | 当前路径/产出 | 判定 |
|---|---|---|
| 咨询·无日志·问信号接入 | 走咨询路径，侧重对，但端到端结构图缺失，靠 LLM 自觉 | partial |
| 咨询·无日志·问模块链路 | 触发门槛严易漏（`parser.py:786-807`）；无静态模块图 | partial |
| 诊断·带日志·为什么收不到信号 | 走诊断对，但不强调断开点，日志没用来锚中断点 | partial |
| 诊断·带日志·通用 bug | 唯一真读 has_logs 的路径（仅调颜色，`general_summary.py:204-238`） | ok |
| **带日志的咨询** | **被劫持成诊断，拿不到咨询视图** | **broken** |

## 设计核心

### 一、ReportGraph 契约（举一反三的地基）

定义统一中间结构，既是渲染器唯一输入，也是 AI 的产物契约：

```
ReportGraph {
  intent:    consult | diagnose          # L2
  has_logs:  bool                        # L2 侧重开关
  verdict:   {status: ok|broken|inconclusive, headline, next_step}
  lanes:     [{id, title}]               # 泳道: CarService/Helper/DataCenter/业务/Unity
  nodes:     [{id, lane, label,
              status: ok|suspect|broken|unknown,
              anchors: [{file, line}],          # 源码锚点
              logs:    [{ts, file, line, text}],# 原始日志佐证
              note}]
  edges:     [{from, to, kind, status, note}]
  timeline:  [{t_offset, event, status, node_ref}]        # 生命周期: 注入/连接/注册/订阅/注销
  values:    [{ts, value, source: real|cache|hmi, abnormal, log_ref}]  # 值变化轨道
  findings:  [{severity, title, evidence_refs, kind: ok|risk|todo}]    # 根因判读/风险/待确认
}
```

渲染器只认 `ReportGraph`，与数据来源解耦。

### 二、intent × has_logs 二维侧重（不固定范式）

底层永远同一张 `ReportGraph`，渲染器+prompt 按两轴调**章节权重**与 **verdict 措辞**：

| | 无日志 | 有日志 |
|---|---|---|
| **咨询(consult)** | 置顶模块/架构泳道图 + 理论接入链路，明确标"未结合运行态" | 架构图 + 用日志佐证"确实这样接入/流转" |
| **诊断(diagnose)** | 静态推断可能断点（标注证据不足，弱） | **核心**：围绕图标 🟢OK/🔴断开，用日志锚定中断点；verdict 症状导向 |

本案例落右下格：verdict = "电量信号 40018 链路正常、95→94 全程送达，非'进场景没显示 3D'的断点 → 根因疑在主题切换/X3D ready/Unity 首帧"。

## 架构（按组件）

### C1. `reporting/report_graph.py`（新增）
- **职责**：定义 `ReportGraph` dataclass + JSON schema + 校验函数。
- **接口**：`build()` 构造、`validate(graph)`、`to_json()/from_json()`。
- **依赖**：无（纯数据）。

### C2. `reporting/graph_adapters.py`（新增）
- **职责**：各数据源 → `ReportGraph`。
  - `signal_json_to_graph(signal_json)`：确定性映射 signal 脚本 JSON（`chain_edges/lifecycle_report/value_samples/registration_checks/detailed_stats`）→ nodes/edges/timeline/values。包含**消噪**：按目标 SignalCode 过滤 `source_references`（剔除 gear/speed/door 等凑数项）。
  - `apply_source_stage(graph, source_stage_output)`：把 source_stage agent 的结构化输出（节点 `status` 标注 + `findings`）合并进同一张图，**共用证据池去重**。
  - `codegraph_to_graph(symbols)`（L4）：静态模块图 → lanes/nodes/edges，供咨询路径。
- **依赖**：C1、`codegraph_client`、signal 脚本 JSON。

### C3. 意图维度（L2）
- **落点**：`run_primary.py:205-235 _unified_classify_and_decide` 产 decision 时增加 `intent`；`resolve_source.py:712-784 _classify` 输出补 `intent`。
- **判据（启发式，不新增 LLM）**：有症状描述（"收不到/看不到/为什么没/黑屏/卡住"等）或 `selected_input` 存在且诉求是"排查" → `diagnose`；纯"了解/怎么接入/涉及哪些模块/链路怎么走" → `consult`。
- **透传**：`run_primary.py:538-585` source_stage 分支增传 `intent` 与 `has_logs = selected_input is not None` 给执行器与渲染器。

### C4. 意图劫持修复（L3）
- **落点**：`handle_event.py:642-645`（镜像 `532-542`）、`parser.py:753-754`。
- **改法**：当 `bug_request/referenced_resources` 存在但用户文本命中 `looks_like_source_analysis_prompt` 且**无症状词**（即 `intent=consult`）时，不再无条件踢出 source_analysis —— 允许产咨询报告并**把附带日志当佐证**（落右上格"咨询·有日志"）。
- **风险**：动路由判定，必须有针对性测试覆盖（见测试策略）。

### C5. `reporting/source_signal_report_html.py`（新增，侧重感知渲染）
- **职责**：消费 `ReportGraph` 出单份 HTML，章节权重/verdict 按 `(intent, has_logs)` 切换。
- **SVG 泳道**：**扩展** `combined_bug_html.py:265 render_swimlane_rows`（现为单行 3 列横向卡片）为：每条泳道一行、节点按 `status` 描边染色（🟢ok/🟡suspect/🔴broken/灰unknown）、跨泳道带 marker 折线连、节点编号锚点跳转到下方"证据卡片区"（`<details>` 含 `file:line` + 原始日志原文）。
  - **权衡**：复用其已测试的中英文换行逻辑；不做带时间轴的完整二维 swimlane（时间轴由 timeline 章节承担）。
- **静态 HTML**：节点→证据用锚点跳转 + `<details>`，不依赖 JS。
- **依赖**：C1、扩展后的 `render_swimlane_rows`。

### C6. 合并接线（L1）
- **落点**：扩展 `general_summary.py:548-607 _build_combined_report_artifacts`，新增分支处理 `[<domain>, source_stage]`（覆盖 `[signal, source_stage]` 及未来 `[startup/stuck/scene, source_stage]`）→ 返回单份 combined artifact；`run_primary.py:909-917` 走 combined 分支只发一份。两份原始 HTML 仍写 job 目录留档，不进 `files_to_send`。

### C7. 静态模块/架构图生成器（L4）
- **落点**：新增 `source_analysis/module_graph.py`，用 `codegraph_client.py:184-204 get_callers/get_callees` 构造模块级调用/依赖图 → 喂 `ReportGraph`（咨询路径）。
- **附带修复**：空证据咨询报告不再标绿/黄成功（`source_analysis.py:183`、`source_report_html.py:176`），如实反映"无运行态证据"。

### C8. skill 规范更新（F）
- 改 `signal-chain-analyzer/SKILL.md:252-256`：允许 HTML 在**节点可展开证据区**展示 `file:line` + 日志原文。
- 两个 skill 补"输出 ReportGraph 契约字段 + intent 感知"说明。
- **⚠️ 纪律**：按全局 CLAUDE.md 第 6 条，改 skill 前先跑 RED 基线（用不带新规范的 agent 复现它在真实案例上的错法），再据此最小化改动。

## 实施分期（可独立交付，每期带验收）

1. **L1**：C1 契约 + C2(signal/source_stage 适配) + C5 渲染 + C6 合并 → 本案例产出单份图优先报告。验收：见 Acceptance 1–4。
2. **L2**：C3 意图维度 + 把 intent/has_logs 接到 C5 渲染与 source_stage prompt → 侧重自适应。验收：见 Acceptance 5–6。
3. **L3**：C4 劫持修复 → "带日志的咨询"场景转 ok。验收：见 Acceptance 7。
4. **L4**：C7 静态模块图 + 空壳成功修复 → 咨询路径产出转扎实。验收：见 Acceptance 8。

## 测试策略

- **C2 适配器单测**：以本案例真实 `bug_signal_chain_report.json` 为 fixture，断言 graph 的 nodes/edges/timeline/values 正确，且消噪后不含 gear/speed/door。
- **C5 渲染器测**：断言 SVG 节点按 status 染色已输出；**断言无死 CSS**（每个定义的 chain/flow/swimlane class 都被使用）；断言证据卡含 `file:line` + 原始日志原文；断言 `(intent, has_logs)` 四格下章节顺序不同。
- **C3/C4 意图测**：矩阵 5 场景 → 正确 intent + 路径；重点回归"咨询·带日志"→ 产咨询报告而非诊断。
- **回归**：迁移期保留旧双报告路径可用，L1 完成后切换。

## 决策点（请在 spec review 时确认）

1. **旧双报告**：合并后两份原始 HTML 仅 job 目录留档（默认），还是彻底不生成？
2. **source_stage agent 输出格式**：改为输出"对图节点的 status 标注 + findings"是行为变更，需先 RED 基线 —— 是否接受这一步纳入 L2？
3. **L3 劫持修复粒度**：在路由层直接放行 consult（改 `handle_event`），还是在 bug 路径内按 `intent=consult` 产咨询报告？前者更彻底、风险更集中在路由。
4. **L4 模块图深度**：模块级（文件/类粒度）够用，还是要到函数级调用图？后者更重。

## Non-Goals

- 不切换 `card.action.trigger`；不重做 intent runner。
- 不改 bug 报告（startup/stuck/crash/general）现有外壳与 CSS。
- 不做带时间轴的完整二维 swimlane（timeline 章节承担时间维度）。
- 不一次性统一所有分类来源字段。

## Acceptance Criteria

1. 源码分析类请求（含本案例）产出**单份**报告，不再同时上传 signal + source_stage 两份。
2. 报告以 **SVG 泳道图为核心**，节点按 status 染色，body 中不再有死 CSS（定义了却不用的 chain/flow/swimlane 样式）。
3. 泳道节点可展开看 `file:line` + **原始日志原文**（覆盖 +41s/+582s 等后段事件，不再截断在 +22s）。
4. 报告含生命周期时间线（注入→连接→注册→订阅，注销缺失如实标注）+ 值变化分轨（真实/缓存/HMI）+ findings（OK/risk/todo），且把 `-1` 矛盾、property id `557847217`=40019 陷阱、Unity recv=0、16:50:41 窗口未分析等提炼为 risk/todo。
5. 分类决策产出 `intent = consult | diagnose`，并透传到渲染器与 source_stage prompt。
6. 同一诉求在 `(intent, has_logs)` 四格下，报告章节顺序与 verdict 措辞不同（咨询置顶架构视图、诊断置顶断开点）。
7. "带 bug 链接但文本是咨询链路"的请求，产出咨询型报告（用日志佐证），不再被劫持成诊断。
8. 咨询路径在无日志时产出基于静态 codegraph 的模块/架构图，不再是占位骨架；空证据不再标绿/黄成功。
