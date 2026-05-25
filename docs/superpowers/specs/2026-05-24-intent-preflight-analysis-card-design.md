# Intent Preflight Analysis Card Design

## Goal

在所有“分析型请求”正式执行前，先回复一张意图分析卡片。卡片需要明确说明系统对本次请求的理解、置信度、计划执行路径，以及在意图不明确时给用户的下一步选择。

## Scope

本次设计只覆盖 `lark-agent-bridge` 的分析型消息入口，不覆盖普通闲聊、知识库 QA、审批卡片和结果报告渲染样式的大改。

分析型请求包括：

- Bug 链接分析
- 直传文件 / 回复文件 / 回复失败卡片后的文件分析
- 感知数据总结
- 信号链分析
- 以上几类请求的 follow-up / retry / clarification

## Existing Constraints

- 当前 `config.toml` 只监听 `im.message.receive_v1`，没有开启 `card.action.trigger`，因此不能把“点按钮”当成唯一交互方式。
- 现有系统已经支持：
  - 回复链取文件资源
  - 直传文件分析
  - bug_clarification / bug_time_clarification
  - 结果卡中的 skill 纠偏展示
- 现有系统的问题是：
  - 第一张卡片只显示“分析进行中”，不显示意图分析
  - 文件型歧义请求会直接落到 `direct_analysis` 或后续 `analysis_followup`
  - 文本 follow-up（如“重新分析”）在非 bug 上下文下会退化到本地 OMLX

## Desired Behavior

### 1. 统一前置卡片

对所有分析型请求，在真正执行之前先发一张卡片，内容至少包括：

- 意图分析结果
- 置信度（高 / 中 / 低）
- 计划执行路径
- 分类来源（规则 / Agent / 用户补充）

### 2. 高置信度请求

当系统对意图分析足够明确时：

- 第一张卡片显示“已完成意图分析，准备执行”
- 随后同一张卡片进入进度更新
- 不要求用户再次确认

### 3. 低置信度或失败请求

当系统不能明确决定执行路径时：

- 不直接执行分析
- 返回一张“等待用户补充方向”的卡片
- 卡片正文给出编号选项和文本回复示例
- 用户可以：
  - 回复序号选择既有 skill
  - 直接回复“直接源码分析”
  - 补充更明确的方向文本

### 4. 文件型请求的语义提升

对于“给了文件 + 给了时间 + 给了请求”的场景，默认按“文件型 bug 分析”理解，而不是普通聊天。

细分规则：

- 若用户给了明确源码线索（类名 / 函数名 / `.kt/.java` / “根据源码/基于源码/重点看某关键字”）：
  - 高置信度
  - 直接按“源码导向文件分析”执行
  - 不要求先命中既有 skill
- 若用户只给了现象描述（如“调查3D生命周期”）：
  - 先尝试匹配既有 bug skill
  - 命中则按 skill 执行
  - 未命中则进入“等待用户补充方向”的卡片

### 5. 文件型 clarification / retry

对于源自文件分析或文件澄清链的 follow-up：

- 不能再默认落到 `analysis_followup -> OMLX`
- 若是 `重新分析 / 重跑 / 再分析` 等 retry 意图：
  - 直接恢复原始文件分析请求并重跑
- 若是 clarification 卡片后的“1 / 2 / 直接源码分析 / 补充方向文本”：
  - 应恢复原始文件请求，并按用户给出的方向继续执行

## Minimal Architecture

### A. Intent Preflight Decision

在 `BridgeApp` 增加一个轻量 preflight 决策对象，用于描述：

- 是否自动执行
- 卡片标题 / 执行路径说明
- 置信度
- 分类原因
- 若不执行，给用户的文本选项

### B. Reuse Existing Card Surface

不新建新的卡片协议，继续复用：

- `send_status_card(...)` 作为高置信度“准备执行 / 执行中”卡片
- `bug_clarification` 结果卡作为“等待用户补充方向”的卡片

### C. Text-Reply Choice Flow

由于当前未启用 `card.action.trigger`，编号选择必须通过文本 follow-up 实现：

- 把可选项写入 `TaskResult.details`
- 让 `AgentActivityStore` 持久化这些选项
- 在后续文本 follow-up 中读取这些选项并解释 `1 / 2 / 直接源码分析`

### D. Direct Analysis Retry / Override

为 `run_direct_analysis(...)` 增加最小 override 能力：

- 允许上层显式传入 `plans_override`
- 允许把“用户选中的 skill / 源码分析策略”传给 direct-analysis 执行器

## Non-Goals

- 本次不切换到 `card.action.trigger`
- 不重做整个 intent runner
- 不重构所有结果卡样式
- 不一次性统一 bug / direct / signal / perception 的所有分类来源字段

## Acceptance Criteria

- 分析型请求的第一张 bot 卡片必须包含意图分析
- 高置信度请求会自动进入执行
- 低置信度文件型请求不会直接落到 OMLX
- clarification 卡片后的文本回复 `1 / 2 / 直接源码分析 / 补充方向` 可以继续推进分析
- “重新分析”回复失败文件卡片时，不再落到 `analysis_followup -> OMLX`
