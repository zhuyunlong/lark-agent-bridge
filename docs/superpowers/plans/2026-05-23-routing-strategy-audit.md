# 路由/策略分发规则审计与优化方案

适用仓库：`/Users/zhuyl/Documents/workspace/tools/lark-agent-bridge`

目标：系统盘点当前桥里的**模式匹配、策略分发、守卫回退、资源继承、API 直连**入口，找出高风险规则分裂点，并给出分阶段优化方案。

## 本轮已修

- `direct_analysis` 不再直接调用旧的 `_extract_fault_time("", request.prompt)`。
- 现在改为复用 `_resolve_bug_time_context()`，并从事件时间补参考日期，避免 `时间点5月22日 7:46` 被截成只有 `07:46`。
- 代码位置：
  - `lark_agent_bridge/agents/bug_runner.py`
  - `tests/test_agents.py::test_direct_analysis_cn_month_day_time_uses_full_datetime_context`

## 一、规则入口总表

### 1. parser 层：第一道模式匹配

| 入口 | 作用 | 当前风险 | 建议 |
| --- | --- | --- | --- |
| `parse_signal_request()` | 识别信号链分析 | 依赖关键词 + Signal 解析，和 scene-signal 有边界重叠 | 保留 deterministic 解析，但要把 scene-signal 交给统一裁决层 |
| `parse_rom_version_lookup_request()` | 识别 ROM/导航版本查询 | 风险较低 | 维持本地规则 |
| `parse_addr2line_request()` | 识别符号表/堆栈反解 | 风险中等，和 direct_analysis 都会吃文件/日志类请求 | 保持本地解析，但加强与 direct_analysis 的冲突测试 |
| `parse_followup_action()` | 识别 retry/continue/ask | 风险中等，续聊动作词比较松 | 与 follow-up 决策合并评估 |
| `parse_claude_skill_request()` | 识别本地 Claude skill 触发 | 风险低，但入口和 intent route 集合不一致 | 保留 parser，修复 intent route 不一致 |
| `parse_perception_summary_request()` | 识别感知统计类请求 | 风险中等，和 direct_analysis、signal、bug follow-up 可能重叠 | 进入统一候选评分 |
| `parse_direct_analysis_request()` | 识别附件/日志直传分析 | **高风险**，关键词太宽 | 收紧触发条件，不再靠大词兜底 |
| `parse_bug_request()` | 识别 Bug 链接 | 风险低 | 保持 deterministic |
| `should_use_omlx_chat()` | 兜底普通聊天 | 风险中等，取决于 `TASK_TERMS` 和 `OMLX_CHAT_TERMS` | 保留本地规则，但只在上层路由收窄后使用 |

### 2. app 层：第二道分发与资源继承

`_dispatch_route()` 当前顺序：

1. `bug_followup`
2. `bug_intent`
3. `addr2line`
4. `rom_version`
5. `scene_signal`
6. `knowledge_qa`
7. `signal`
8. `claude_skill`
9. `bug_request`
10. `perception`
11. `direct_analysis`
12. `followup_intent`
13. `general_followup`
14. `knowledge_probe`
15. `stale_light_interaction`
16. `basic_chat`
17. `omlx_chat`
18. `intent_router`

当前问题：

- 顶层是“**first match wins**”，但 `parser`、`app`、`intent_runner` 都在做第二次甚至第三次匹配。
- `direct_analysis` 不只由 `parser` 决定，还会被 `_build_direct_analysis_request()` 和 `_looks_like_direct_analysis_prompt()` 二次放大。
- `signal`、`scene_signal`、`perception`、`bug_followup` 都会受资源继承影响，导致“文本看起来不是这个路由，但因为引用了文件又被吸进去”。

### 3. bug_runner 层：第三道 bug 内部策略分发

| 入口 | 作用 | 当前风险 | 建议 |
| --- | --- | --- | --- |
| `classify_requests()` | bug 内部分析 plan 选择 | **高风险**，纯顺序关键词路由 | 改成候选打分 + 冲突裁决 |
| `_manual_bug_selection()` | Agent 不可用时的本地回退 | 风险中等，直接复用 `classify_requests()` 的偏差 | 保留，但依赖新评分引擎 |
| `_classify_bug_request_with_agent()` | bug skill 分类 | 和 `intent_runner` 有功能重复 | 保留语义，但迁移到 direct API 结构化决策链 |
| `decide_bug_followup()` | bug 续聊重分析 vs 直接回答 | 和 `app._bug_reanalysis_decision()` 有重叠 | 统一到一套 follow-up 决策模型 |
| `run_bug_analysis()` | bug 主入口守卫 | 时间、日志覆盖、分类都在这里 | 保留，但只做 deterministic guard |
| `run_bug_reanalysis()` | bug 重分析 | 仍混用旧时间提取和旧 plan 继承 | 统一到新时间上下文 + 新 plan 决策 |
| `run_direct_analysis()` | 直传文件分析 | 已修时间提取，但内部 plan 路由仍沿用旧 `classify_requests()` | 下一步继续改 plan 分发 |

### 4. intent_runner 层：第四道 API/CLI 意图裁决

当前链路：

- `Pydantic AI Agent`
- `Direct API`
- `Subprocess CLI`

当前问题：

- 它只负责顶层 route 分类，不负责 bug skill 细分；而 `bug_runner` 又自己维护了一套 agent 决策 prompt。
- `ROUTES` 集合里没有 `claude_skill`，但 `app._dispatch_intent_decision()` 里保留了 `route == "claude_skill"` 分支。这个分支当前是**不可达的死分支**。

## 二、高风险问题清单

### P0. 旧规则与新规则并存

- `run_bug_analysis()` 已经走 `_resolve_bug_time_context()`。
- `run_direct_analysis()` 原先走 `_extract_fault_time()`，本轮已修。
- `run_bug_reanalysis()` 仍保留 `_extract_followup_fault_time()` + `_extract_fault_time()` 的旧回退链。

风险：同一类输入在不同入口得到不同结果。

### P0. `direct_analysis` 触发条件太宽

`DIRECT_ANALYSIS_TERMS` 当前包含：

- `分析`
- `总结`
- `信号`
- `启动`
- `卡顿`
- `黑屏`

风险：

- 只要带文件资源，再加一个泛词，就可能被吃进 `direct_analysis`。
- 后面 `app._looks_like_direct_analysis_prompt()` 还会用 `file_probe` 再试一次，进一步放大误触发。

### P0. `classify_requests()` 是顺序吞噬模型

当前逻辑：

- 先看 `perception`
- 再看 `xtheme`
- 再看 `signal`
- 再看 `scene_signal`
- 再看 `crash`
- 最后看 `startup/stuck`

风险：

- 规则的“先后顺序”比“证据强弱”更重要。
- 用户一句话同时包含多个域词时，前面的宽词会直接抢走路由。

### P1. follow-up 决策重复实现

当前有两套：

- `bug_runner.decide_bug_followup()`
- `app._bug_reanalysis_decision()`

风险：

- 一套看 Agent skill，一套看 app 本地强规则。
- 用户续聊行为可能在两个地方被不同解释。

### P1. 资源继承规则过于激进

关键入口：

- `_fetch_referenced_message_resources()`
- `_should_lookup_current_message_for_resources()`
- `_should_inherit_signal_resources()`

风险：

- route 本意可能是 follow-up 或问答，但因为引用链里有旧文件就被重新路由成 direct_analysis / signal / perception。

### P1. intent route 集合和 app 分支不一致

明显例子：

- `intent_runner.ROUTES` 不包含 `claude_skill`
- `app._dispatch_intent_decision()` 仍保留 `route == "claude_skill"`

风险：

- 规则维护者会误以为某条路径还在生效。
- 实际行为和代码表象不一致。

## 三、优化方案

### 方案 A：收口 deterministic 守卫

适用范围：必须本地确定的规则

- 时间提取与日期补全
- Bug URL / Signal / 堆栈地址 / 文件资源解析
- 日志覆盖与时间窗扫描
- 本地文件授权

要求：

- 所有需要问题时间的入口统一走 `_resolve_bug_time_context()`。
- 删除旧 `_extract_fault_time()` / `_extract_followup_fault_time()` 在生产路径上的直接业务用途，保留仅作底层工具或迁移期兼容。

### 方案 B：把“粗匹配”改成候选评分，不再靠顺序吞噬

对 `classify_requests()` 改造为：

- 先生成候选：`perception / xtheme / signal / scene_signal / startup / stuck / crash / general`
- 每个候选输出：
  - `score`
  - `matched_terms`
  - `hard_constraints`
  - `requires_logs`
  - `why_not`
- 再做冲突裁决，而不是 `if ... return`

建议优先级：

- `xtheme`、`scene_signal`、`perception` 应高于 `signal`
- `signal` 必须要求“明确具体 SignalCode / SIGNAL_... / 通用信号链路问题”
- `general` 只能在没有专用 skill 明确命中时出现

### 方案 C：缩窄 `direct_analysis` 触发面

建议：

- `parse_direct_analysis_request()` 只负责“有资源 + 有明确分析动作/现象域词”的强触发。
- 去掉或收紧 `分析`、`总结`、`信号` 这类宽词。
- `_looks_like_direct_analysis_prompt()` 不应再用 `file_probe` 人工喂提示词；应改为显式判断：
  - 是否有资源
  - 是否没有 bug url
  - 是否没有明确 signal enum
  - 是否没有被 perception / addr2line / rom-version 更强命中

### 方案 D：把 bug skill 分类和 follow-up 决策迁到 direct API 结构化链

适合 API 直连：

- bug skill 分类：`_classify_bug_request_with_agent()`
- bug follow-up 决策：`decide_bug_followup()`
- 顶层 unresolved route 分类：`intent_runner.classify()`

原因：

- 都是短上下文
- 都需要结构化输出
- 都适合 schema 校验与 retry

不适合 API 直连：

- 时间抽取
- 资源抽取
- Bug URL / Signal / stack 解析
- 日志时间窗扫描

### 方案 E：统一 route schema

统一 route / action 枚举，避免分裂：

- `intent_runner.ROUTES`
- `app._dispatch_intent_decision()`
- follow-up action / reanalysis action
- bug skill selection output schema

首先要做的修正：

- 删除不可达的 `claude_skill` intent 分支，或者把它补回 `ROUTES` 和 prompt schema。

## 四、推荐改造顺序

### 第一阶段：收紧 deterministic 基础规则

1. 统一时间上下文入口
2. 收紧 `direct_analysis` parser 和 app hint
3. 修复 route schema 不一致（如 `claude_skill`）

### 第二阶段：重做 bug 内部 plan 分发

1. 把 `classify_requests()` 改成候选评分
2. 让 `_manual_bug_selection()` 复用新评分器
3. 补 perception / xtheme / scene_signal / signal 冲突矩阵测试

### 第三阶段：统一 follow-up / reanalysis 决策

1. 收敛 `app._bug_reanalysis_decision()` 与 `bug_runner.decide_bug_followup()`
2. 用 direct API 输出统一 schema
3. 本地只保留 deterministic hard rules

### 第四阶段：资源继承与 intent router 清理

1. 给资源继承打显式置信等级
2. 限制“有资源即 direct_analysis”的兜底
3. 让 intent router 真正成为 unresolved path 的最后裁决，而不是和本地规则平行竞争

## 五、测试补齐建议

必须新增的矩阵：

- `附件 + 泛词 + 无 bug url`
- `附件 + signal 词 + scene-signal 词`
- `附件 + 感知数据`
- `follow-up + 时间修正 + 新方向`
- `reply chain + inherited resources + 普通追问`
- `中文月日时分 + 直传文件分析`
- `intent direct API route` 与 `app dispatch` 一致性

## 六、当前建议

下一步不要继续零散补规则。

建议按以下顺序直接动代码：

1. `parser.py` 的 `parse_direct_analysis_request()` 和 `should_use_omlx_chat()`
2. `app.py` 的 `_build_direct_analysis_request()`、`_looks_like_direct_analysis_prompt()`、`_bug_reanalysis_decision()`
3. `bug_runner.py` 的 `classify_requests()`、`_plans_for_reanalysis()`、旧时间提取回退链
4. `intent_runner.py` 的 route schema 和 direct API 统一

