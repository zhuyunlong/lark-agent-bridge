# 群聊续聊链修复 · P1–P4

日期：2026-06-04
状态：P1/P2/P3/P4 已实现并验证（工作区未提交）

## 背景

群 `oc_efb38830583431c927586574bb67dd00` 三条消息构成一个对话流，最近两次 @机器人 请求都失败：

| 时间 | message_id | 内容 | 路由 | 结果 |
|---|---|---|---|---|
| 15:12 | om_…be884b | 传 `log0.7z`（未 @bot） | not_addressed | skipped（正确） |
| 15:14 | om_…ef338 | `@bot 源码分析 找出最后一次crash的原因`（回复①） | addr2line_resolve | ❌ missing_address |
| 15:15 | om_…dbaf | `@bot 那你看下3d生命周期 时间点 15:00`（回复 bot 对②的报错） | signal_lifecycle | ❌ missing_signal |

实测原始 Lark payload 确认：
- ② `reply_to = om_…be884b`（即 log 文件）——② 确实是"回复文件 + @bot"，所以拿到日志（resource_count=1）是正确的。
- bot 对② 的报错（om_…caad，sender_type=app）**没有 `reply_to`**——以独立消息发出，链断在 bot 这一跳。
- ③ `reply_to = om_…caad`（bot 的报错）——③ 是想续聊，但 BFS 走到 om_…caad 即断，回不到② → 文件。

## 根因模型

会话链的连续性同时被两类问题破坏：

- **链路断裂**：bot 对新建请求的回复用 `send_response` 发出（无 reply_to），引用图在 bot 那一跳断开 → 续聊拿不到链上的输入。
- **窄 handler 误抢**：含日志的"源码分析/找crash原因"被 `addr2line_resolve` 抢走（要 `#xx pc lib*.so` backtrace）；含日志的"3d生命周期"被 `signal_lifecycle` 抢走（要信号码）——都因前置条件不满足而死路。

用户确认的模型：链首必须 @机器人；之后回复、回复的回复都算同一条链；**被回复的消息本身（文件/Bug链接/他人对话）作为本次输入带入**；只有链内信息才有价值（因此 ① 未 @ 被忽略是正确的）。

---

## P1 — bot 回复挂线程到触发消息　【已完成】

- 根因：`handle_event.py` `_send_result` 文字 fallback 仅 `delivery=="reply"` 时挂线程；新建请求 delivery 默认 `"send"` → `send_response`（无 reply_to）。卡片路径本就挂线程，唯文字 fallback 漏。
- 改动（`handle_event.py:1132` 附近）：只要有 `event.message_id` 即用 `reply()` 挂线程，仅无 message_id 时退回 `send_response`。
- 效果：bot 所有直接答复（成功/失败/不支持）都挂线程并 @提问者 → 链路完整 → 现有 BFS 资源恢复机制自动生效。
- 测试：`tests/test_app_followup_reply.py::test_failed_fresh_result_is_threaded_reply_to_triggering_message`；并更新 5 个旧测试为新契约（答复=线程 `replies` 而非 `sent`）：
  - `test_app_bug_request.py`：`test_non_analysis_request_outside_allowed_chat_is_rejected`、`test_unsupported_request_still_sends_message`、`test_intent_unsupported_reanalysis_without_context_prompts_for_reply`、`test_claude_skill_request_sends_text_and_result_file_when_configured`、`test_claude_skill_request_accepts_bot_name_with_spaces`。
- 副作用（预期）：群里失败/不支持/skill 答复从"独立 send"变成"线程回复 + @提问者"。

## P2 — addr2line 不再误抢"源码分析"　【已完成】

- 根因（`parser.py` `_looks_like_addr2line_intent`）：`has_action("分析") and has_object("crash")` 即触发；叠加有资源使 `allow_missing_address=True`，误判为 addr2line。addr2line 优先级(7)早于 source/direct 分析(10)；且 `_would_route_source_analysis` 见到本地资源即放弃(`handle_event.py:542-543`)。
- 改动（`parser.py:1155` 附近）：在显式 `ADDR2LINE_TERMS` 检查之后新增守卫——**无真实地址/堆栈 + 命中 `SOURCE_ANALYSIS_TERMS`（源码/源码分析/source code…）→ 返回 False**，让位给 source/direct 分析。真实 backtrace 或显式"反解"仍触发。
- 效果：② 让位后 arbitrated 路由的 `direct_analysis`（带继承日志）接住 → 真正分析日志。
- 测试：`tests/test_parser.py`：`test_source_analysis_intent_with_crash_keyword_does_not_trigger_addr2line`、`test_explicit_addr2line_term_still_triggers_even_with_source_keyword`；端到端 `tests/test_app_followup_reply.py::test_source_analysis_find_crash_reply_to_file_routes_to_direct_analysis`。

## P3 — msg3 不再死在 missing_signal　【已完成 · 无需新增生产代码】

- 验证结论：P1+P2 落地后，③ 继承到日志 → `_route_signal_request` 的 defer 逻辑（`handle_event.py:805-811`）让位 → arbitrated 的 `direct_analysis` 接住。因"3d生命周期"方向歧义（startup vs stuck），落到 `bug_clarification` 方向澄清——与 `2026-06-03-bug-skill-conflict-confirmation` 一致，是期望行为。
- 测试：`tests/test_app_followup_reply.py::test_3d_lifecycle_reply_to_file_routes_to_direct_analysis_not_missing_signal`（断言：非 missing_signal、非 signal_lifecycle、进入 direct-analysis/澄清流程）。

## 跳过项

- **①（`event.reply_to` 未捕获到事件/session）**：`models.py:474` 从事件 payload 读 reply_to，但事件流未提供；代码已有 `_direct_reply_to`/`fetch_message` 兜底，功能上自愈，仅日志/性能层面。本轮不改。

## 已完成部分验证

- 全量 `python -m pytest tests/` → **1249 passed**；仅余 2 个改动前即存在的 `pydantic_ai` 缺失失败（`git stash` 已确认与本次无关）。
- 生产改动仅 `handle_event.py` 4 行 + `parser.py` 6 行。

---

## P4 — "失败答复后不@只回复"也能续聊　【已完成】

### 缺口
本次 ③ 是 @bot 的，P1 已够。但若用户**不再 @、只是回复**一条**失败**请求的 bot 答复来续聊，目前会被判 `not_addressed` 忽略。成功请求不受影响。符合用户"回复即续聊"模型，但属独立行为变更。

### 根因（已定位）
- 门控续聊判定（`handle_event.py:264-272`）依赖 `_lookup_bot_alias_context(被回复消息)` 非空。
- `_lookup_bot_alias_context`（`context_from.py:794-803`）要求被回复消息是已登记 alias；`remember_alias`（`state.py:157-165`）又要求 root 上下文已存在。
- root 上下文落地在 `_deliver_result`（`log_resources.py:292`）被 `if result.success` 卡住——**失败结果不落 root 上下文、不登记 alias**。

### 改动
1. **P4a 落最小 root 上下文（失败也落）**：`_prepare_delivery_result` 失败分支先写入 `conversation_root_message_id` / `delivery=reply`，再调用 `conversation_store.remember(...)` 与 `_remember_progress_card_aliases(...)`。随后 `_send_result(...)` 发送文字线程回复时可用返回的 bot message id 调 `_remember_delivery_alias_from_result(...)`，使失败答复成为 alias→root。
2. **P4b 无需新增生产分支**：RED 转绿后验证，alias 放行后现有 Phase 3 优先级已足够：`direct_analysis` / `scene_signal` 会先于 `_route_general_followup` 接住新消息意图并继承回复链资源，不会盲目按失败上下文的 `addr2line_resolve` / `signal_lifecycle` 重放。
3. **资源继承**：继续依赖 P1 修复后的 Lark reply 链 BFS；测试覆盖 bot 失败答复 → 原请求 → 文件消息的链路恢复。

### RED 基线
- `test_failed_fresh_delivery_registers_bot_reply_alias_for_group_followup` 初始失败：失败结果没有 `conversation_root_message_id`，bot 回复 id 不能登记 alias。
- `test_group_reply_without_mention_to_failed_bot_reply_routes_with_reply_chain_resources` 初始失败：无 @ 回复失败 bot 答复被 `not_addressed` 忽略。
- `test_source_analysis_without_mention_after_failed_bot_reply_does_not_return_missing_address` 覆盖源码分析续聊不会回到 `missing_address`。

### 测试
1. reply(no @) 到失败请求的 bot 答复 → 被识别为续聊（非 `not_addressed`）。
2. 该续聊按新意图 + 继承的被回复文件路由到 direct/source 分析或分诊澄清，不复发 `missing_address`/`missing_signal`。
3. 回归护栏：成功答复无 @ 续聊、回复非 bot 线程、回复 root 不触发、失败 bug session 恢复均保持原行为。

### 风险与代价
- 改变 Phase 1 门控：更多消息成为续聊候选，bot 可能开始响应以前忽略的回复 → 需回归护栏严防误触。
- 失败请求新增持久化状态。
- 独立行为变更，建议单独一轮、单独 RED 基线、不与 P1-P3 混提交。

### 验收
- 三条 P4 新测试绿。
- `tests/test_app_followup_reply.py` 全绿。
- 门控回归护栏全绿：无 @ 成功续聊、回复非 bot 线程、回复 root 不触发、失败 bug session 恢复。
- 全量 `PYTHONPATH=. python -m pytest tests/ -q`：`1252 passed`；仅余既有 `tests/test_agent_runtime.py` 两个 `pydantic_ai` 缺失失败。
