# Threaded Reply Chat Design

## Goal

让 `lark-agent-bridge` 在群聊里把知识问答和普通问答稳定地挂到用户原消息下回复，并支持多轮续聊时免 `@` 触发；同时保证从回复链中间分叉时，后续新分支不会带入更晚轮次的上下文，避免污染 agent 输入。

## Scope

本设计只覆盖以下两类用户可见回复：

- `knowledge_qa`
- 普通聊天/问答，包括 `basic_chat` 与 `omlx_chat`

本设计明确不改以下链路：

- Bug 分析、重分析、继续 Agent
- Bug 结果卡片、进度卡片、报告附件发送
- 现有审批、卡片按钮、报告上传与归档

## Current Problems

### 1. 回复挂载不统一

当前 follow-up 会明确走 `reply`，但普通问答并没有统一沉淀为“基于用户消息回复”的稳定会话链。知识问答虽然大多已经标记为 `reply`，普通聊天仍可能只是一次性回复，不足以形成可持续的多轮会话上下文。

### 2. 群聊续聊仍依赖显式 `@`

当前群聊入口在未命中前置 `@` 时，只有解析出 `followup_context` 才继续；但即便已经位于会话链中，也仍要求从消息文本里再次剥离 `@`，这与“进入会话链后不必再 `@`”的目标不一致。

### 3. 中间分叉会污染上下文

`ConversationContextStore.remember_alias()` 已经会把上下文快照复制到别名消息 ID，但 `lookup()` 命中别名后仍会优先回到 `root_message_id`。结果是：

- 用户从中间某条机器人回复继续追问时
- 系统最终仍拿到 root 上的全量历史
- 更晚轮次的用户/机器人内容会被带入新分支

这会直接污染后续的 `omlx_chat` 或 follow-up 判断上下文。

## Desired Behavior

### A. 知识问答与普通问答统一走“回复用户消息”

对于 `knowledge_qa`、`knowledge_probe`、`basic_chat`、`omlx_chat`：

- 文本回复应优先使用 `reply(event.message_id, ...)`
- 卡片回复应优先使用 `reply_card(event.message_id, ...)`
- 结果详情里必须写入：
  - `delivery = "reply"`
  - `conversation_root_message_id = event.root_id or event.message_id`

这保证新开的会话从第一条机器人回复开始就天然挂在用户原消息下。

### B. 群聊进入会话链后可免 `@`

群聊规则改为两段式：

- 新开请求：仍然必须 `@机器人`
- 已进入会话链的续聊请求：只要能解析出合法的 follow-up 上下文，就算本条消息不 `@` 也继续处理

这个放宽只适用于：

- `knowledge_qa`
- `knowledge_probe`
- `basic_chat`
- `omlx_chat`

不扩大到 Bug 分析、信号分析、直传文件分析等重型链路。

### C. 从中间回复时按分支快照继续，不回灌后续历史

当用户回复的是机器人在会话中的某一条历史回复，而不是最新一条时：

- 后续 agent 上下文应锚定到“被回复的那条机器人消息”对应的快照
- 只继承该消息之前的历史
- 不允许带入这条消息之后、同 root 下其他分支已经产生的更晚内容

这意味着会话上下文必须具备“分支快照”语义，而不是简单按 root 聚合全量历史。

## Design

### 1. Conversation Context Model

不引入新的复杂 DAG 存储，沿用当前 `ConversationContextStore` 的 root + alias 模型，但改变 alias 的语义优先级：

- root 仍表示整条会话的起点
- 每条机器人成功回复后，都为该回复消息 ID 记录一份 alias 上下文
- alias 上下文保留当时的 `history` 快照
- 后续 `lookup(alias_message_id)` 命中 alias 时，直接返回 alias 自身，而不是自动跳回 root

这样就能天然支持“从中间机器人消息继续聊”的分支隔离。

### 2. Alias Snapshot Rules

对所有本次纳入范围的成功回复，在发送成功后都要记录 alias：

- 文本 `reply(...)` 成功后，使用返回的消息 ID 做 alias
- 卡片 `reply_card(...)` 成功后，使用卡片消息 ID 做 alias

Alias 记录内容：

- `root_message_id` 保持原 root 不变
- `history` 复制当前上下文快照
- `updated_at` 更新为发送时间

后续在该 alias 上继续追加历史时，只改 alias 对应上下文，不回写到旧 alias；root 是否同步更新要按场景区分：

- 对“当前最新分支”可同步 root，保持 latest-for-chat 可工作
- 但旧 alias 的快照必须保持不可变语义，不被 root 覆盖

### 3. Group Message Admission

当前群聊 Phase 1 的逻辑是：

- 先尝试剥离开头 `@`
- 如果没有，再解析 `followup_context`
- 解析到了也仍尝试从文本里剥离机器人 mention

新逻辑改为：

- 先尝试显式 `@`
- 如果没有显式 `@`，先解析 `followup_context`
- 如果存在 `followup_context`，并且上下文模式属于允许免 `@` 的集合，则直接接受原始文本作为 `route_content`
- 只有既无显式 `@`、又无允许免 `@` 的 follow-up 上下文时，才判定为 `not_addressed`

这样可以支持：

- 第一轮必须 `@`
- 第二轮起在同一回复链内不再 `@`
- 用户从中间回复时仍能命中正确上下文

### 4. Knowledge Card Readability

知识问答卡片保持卡片形态，但答案主体不应依赖“折叠后查看”或“完整内容请查看报告”这类结论隐藏方式。

要求：

- 卡片头部第一段直接展示完整、清晰、可读的结论主体
- 可接受平台上限内的自然截断，但不能追加“完整内容请查看报告”式提示来替代结论
- 来源列表保持单独分段，最多列前几条，避免喧宾夺主

换句话说，卡片本身必须可单独阅读结论，不把关键结论藏到外链或折叠后。

### 5. Basic Chat / OMLX Chat Persistence

当前 `basic_chat` / `omlx_chat` 的成功结果默认不一定进入 `conversation_store.remember(...)`，这会导致：

- 形式上回复到了用户消息
- 但后续上下文查找不到这条会话

因此需要补齐：

- `basic_chat` 成功后记录 root context
- `omlx_chat` 成功后记录 root context
- 每次成功回复后追加 `history`
- 每次回复成功后记录 alias 快照

## Non-Goals

- 不尝试把所有业务模式都统一改成 reply-only
- 不把 Bug 链路也放宽成“进入回复链后可免 `@`”
- 不重做会话存储为显式图数据库或消息树
- 不在本次改动中调整报告附件上传行为

## Risks

### 1. 放宽免 `@` 可能误吸收普通群聊消息

缓解方式：

- 仅当 `followup_context` 已存在且模式属于允许集合时才放宽
- 不使用“同群最近一次聊天上下文”作为免 `@` 依据
- 仍要求消息处于回复链内，不能只靠聊天窗口时间邻近

### 2. Alias 命中优先级变化可能影响旧 follow-up 行为

缓解方式：

- 仅在 `lookup(alias)` 命中 alias 时返回 alias 自身
- root 查询行为保持兼容
- 补回归测试，覆盖 Bug 现有回复链、进度卡片 alias、上传 HTML alias

### 3. 卡片过长影响 Feishu 渲染

缓解方式：

- 不做“隐藏式折叠”
- 但仍保留安全长度上限
- 优先保留结论主体，来源和附加说明可以压缩

## Validation Plan

### Unit / Integration Tests

- `knowledge_qa` 结果走 `reply_card`，且卡片主体不带“完整内容请查看报告”
- `basic_chat` 结果走 `reply`
- `omlx_chat` 结果走 `reply`
- 群聊第一轮不 `@` 仍被忽略
- 群聊在已建立知识/聊天回复链内不 `@` 也能触发
- 从中间机器人消息回复时，只使用该消息快照前的历史
- 从最新机器人消息继续回复时，正常继承最新历史
- Bug follow-up、进度卡片 alias、上传 HTML alias 的现有测试继续通过

### Manual Checks

- 群里 `@机器人` 发知识问答，机器人以卡片回复该条消息
- 对该卡片继续回复，不再 `@`，仍能触发知识或普通问答
- 在会话中间某条机器人回复下继续追问，验证新答案不引用该条之后才出现的信息

## Implementation Summary

实现时应优先修改以下区域：

- `lark_agent_bridge/state.py`
  - alias lookup 语义
  - history / snapshot 追加规则
- `lark_agent_bridge/app.py`
  - 群聊 Phase 1 的 `@` 过滤
  - `basic_chat` / `omlx_chat` 的 delivery 与 context 持久化
  - reply 成功后的 alias 记录
- `lark_agent_bridge/cards.py`
  - `build_knowledge_answer_card()` 的主体可读性
- `tests/test_app.py`
  - reply / follow-up / branch isolation 回归
- `tests/test_cards.py`
  - knowledge card 主体渲染断言

