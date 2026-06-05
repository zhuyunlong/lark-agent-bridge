# 同群并发：把 dispatcher 锁从 chat 收窄到对话链根

日期：2026-06-04
状态：已实现并通过测试（工作区未提交）

## 背景与问题

`EventDispatcher`（`dispatcher.py`）原按 **`chat_id` 整群串行**所有 heavy 任务（`ChatSessionLock`）。
后果：同一个群里同时来两个请求必然排队（第二个阻塞在 chat 锁上），即便它们是**互不相关**的两条独立请求。

调查（4-agent workflow）结论：

- Phase 1 已把每个共享 store 逐个加锁，且几乎所有共享态都按 `root_message_id`/`message_id`/`event_id`/`job_id` 存（**非 `chat_id`**），所以同群两个**独立**请求触及的是不相交的 key——本就可并发。
- 真正需要串行的只有**续聊因果链**（followup B 要读父请求 A 在交付时才写的 root 上下文 + alias）。
- per-chat 锁因此是**过保守**：它顺带串行了独立请求。

## 两个必须处理的点

1. **硬伤前置**：`ReportVersionStore`（`report_version.py`）**完全没有锁**，且 `_persist` 用非原子 `write_text`。放开同群并发后，两个 heavy 任务并发 `add_version` 会触发
   `RuntimeError: dictionary changed size during iteration`（`_persist` 迭代 `_groups` 时另一线程插入新 group_key），以及撕裂 `report_versions.json`。
2. **parent-in-flight**：续聊 B 回复**还在飞**的 A 时，store 里还没 A 的 root → 简单按 root 定址会让 B 算出不同 key、不与 A 串行 → 误判独立链。

## 实现（TDD，全程 RED→GREEN）

### 1. `report_version.py` — 线程安全
- 加 `threading.RLock`（RLock：`compare_versions`→`get_version` 需可重入）。
- 所有读/写 `_groups` 的 public 方法持锁；`_persist` 改原子写（`tmp` + `replace`）。
- `add_version` 版本号去重：caller 传入的 `version`（来自 `peek_next_version`）若已被占用，回退到权威 `latest_version+1`，避免同 group 并发产生重复版本号。**不改发布流程**。
- 测试：`tests/test_report_version.py::TestReportVersionStoreConcurrency`（用 `sys.setswitchinterval(1e-6)` 强制频繁切换，稳定复现无锁崩溃）。

### 2. `dispatcher.py` — 锁粒度从 chat 收窄到对话链根
- 新增 `_resolve_lock_key(payload, chat_id) -> (lock_key, inflight_key)`，**纯内存**解析（生产者线程绝不做 `fetch_message` 网络调用）：
  - 全新链（无 reply/root/parent 引用）→ `lock_key = message_id`（各自独立 → 并发）。
  - 续聊：先查 **in-flight chain registry**（父在飞、store 还没写），再查 `conversation_store.lookup`（父已交付）→ 命中得 root。
  - 都未命中（冷/跨重启）→ 回退 `chat_id`（保守串行整群，等同旧行为）。
  - card action → `chat_id`（作用于既有会话，保持 chat 维度）。
- **eager in-flight registry**（加固，消除 parent-in-flight 窗口）：`dispatch()` 在串行生产者线程上把 `message_id -> lock_key` 登记进 `self._inflight_root`（`self._inflight_lock` 保护），`_handle` 完成时在 finally 清理。因 dispatch 串行，A 先登记、后到的 B 必能解析到 A 的链根并串行其后。
- `_handle`/`_worker_loop`：`chat_id` 形参改名 `lock_key`，队列项 3-tuple → 4-tuple（带 `inflight_key`）。`_classify` **未改**（仍返回 `(weight, chat_id)`，`TestClassification` 不受影响）。`_begin_lifecycle` 仍收真实 `chat_id`，lifecycle.chat_id 正确。
- 测试：`tests/test_dispatcher.py`：
  - 删 `test_same_chat_serialized`（编码的正是被移除的属性），换 `test_same_chat_independent_requests_run_in_parallel`（Barrier(2) 验同群独立并发）。
  - 加 `test_followup_to_inflight_parent_serializes`（registry 路径）、`test_followup_to_delivered_parent_serializes_via_store`（store 路径）。

## 验证
- `tests/test_dispatcher.py` 22 passed；`tests/test_report_version.py` 21 passed；并发 suite 5× 稳定。
- 全量 `python -m pytest tests/ -q` → **1271 passed, 2 skipped**（含 card-action 修复、per-chat cap、importorskip 守卫后）；2 个 skip 为 `pydantic_ai` 可选 extra 未装（已从 fail 改为干净 skip）。零回归。

## 对抗式 review 结论（3-lens × 验证，15 agents）

- **[major，已修]** card-action heavy 任务（reanalyze/continue/confirm）原被键到 `chat_id`，而同链的消息续聊键到 `root` → 同一条链反而并发 → `conversation_store.remember`/`append_exchange` lost-update、进度卡竞争、重复发布。本是我这次 diff 引入的回归（旧 chat 锁两者共锁）。**修复**：`_resolve_lock_key` 对 card action 解析 `CardActionEvent.root_message_id`，键到链根，无 root 才回退 chat_id（`dispatcher.py`）。测试 `test_heavy_card_action_locks_on_chain_root_not_chat` / `_without_root_falls_back_to_chat`。
- **[minor，保留]** in-flight registry 对"回复 bot 卡片"的现实续聊很少触发（bot 卡片 id 在父交付前不存在），但 chat_id 兜底仍**正确**串行。registry 只对"用户回复自己仍在飞的触发消息"生效——是真实但较窄的场景。保留。
- **[nit，非回归]** 链内严格 FIFO 顺序：B 可能先于 A 拿到链根锁。新旧设计同性质（都用非公平 `threading.Lock`），非本次引入。见下"已知边界"。

## 后续补强（已落地）
- **每-chat 在飞上限（G4 公平性）已做**：`EventDispatcher(max_concurrent_per_chat=N)` + 配置项 `[event_consumer].max_concurrent_per_chat`（默认 `0`=不限，等于 `max_workers`）。实现为每-chat `BoundedSemaphore`，在 `_handle` 里**先于链根锁**获取、finally 反序释放（锁序恒为 sem→root，无死锁）。`test_per_chat_cap_serializes_independent_roots` / `test_per_chat_cap_allows_other_chats`。
  - **残留 caveat**：被 cap 挡住的 overflow 任务是在 worker 线程上 `sem.acquire()` 阻塞，仍占着 worker。所以 cap 限的是**同群并发运行数**，并不能在某群猛灌时严格保证给别群留空闲 worker（要严格保证需 admission-control 的 chat-aware 调度，当前负载下属过度设计，未做）。默认 `0` 时此路径完全不触发，零行为变化。
- **pydantic_ai 两个测试加 `importorskip` 守卫**：`test_agent_runtime.py:354/370` 加 `pytest.importorskip("pydantic_ai")`，可选 extra 未装时干净 skip 而非 fail（根因：`_build_model_settings` 的 anthropic except 兜底与 openai 路径都直接 `from pydantic_ai...`，未防"整包缺失"）。

## 已知边界与未做项（建议后续）
- **链内严格 FIFO 顺序不保证**：锁只保证互斥；两个同链任务若被两个 worker 几乎同时取出，谁先拿到链根锁不确定（与旧 per-chat 锁同样的性质，非回归）。现实里续聊总在父已在飞/已交付后到达，影响极小。
- **`latest_for_chat` 交错语义**：两个同群链并发 `remember` 时，作为兜底的"取最近上下文"可能取到兄弟在飞链（store 不崩，已有 `test_state_thread_safety` 保证；选择语义未钉死）。
- 指标 key 仍叫 `chat_locks_held`（现计的是链根锁数），保留原名以免破坏 `test_report_server` 与 admin 面板。
