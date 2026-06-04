# 并发能力设计（Concurrency Design Plan）

本文档定义 `lark-agent-bridge` 从**单事件串行处理**升级为**多请求并发处理**的目标架构、改造方案、线程安全审计、分阶段路线图与风险评估。现状描述见 [project-overview-architecture.md](./project-overview-architecture.md)，结构重构见 [architecture-refactor-blueprint.md](./architecture-refactor-blueprint.md)。

> 适用范围：`lark_agent_bridge/` 包。
> 红线：**不改变对外行为/触发矩阵/权限语义**，不改变公共类名与方法签名。

---

## 0. 实现状态（2026-06）

| 阶段 | 范围 | 状态 |
|---|---|---|
| **Phase 1** | 线程安全加锁：EventStateStore / ConversationContextStore / CaseStore / HealthMonitor / `_progress_cards` | ✅ **已实现并通过测试** |
| **Phase 2** | EventDispatcher + ChatSessionLock + worker 池 + cli listen 改造 + 配置项 | ✅ **已实现并通过测试** |
| **Phase 3** | `/api/health` 并发指标 + admin worker 面板 | ✅ **已实现** |
| **Phase 4** | `lifecycle.py` QUEUED 状态启用（dispatcher 非侵入串联） | ✅ **已实现** |
| **Phase 5** | Codex App Server warm pool 池化 | ⛔ **Deferred**（评估为高风险 / 边际收益，见 §16） |

实测：`python3.11 -m pytest tests/` → **1246 passed, 41 subtests passed**，无新增失败（含 `test_dispatcher.py` 20 项含 lifecycle 串联、`test_state_thread_safety.py` 5 项、`test_report_server.py` 的 `/api/health` 集成项）。

> 本文档已根据真实代码与实测结果修订。下文凡标注 ✅ 的小节描述的是**已落地的实现**（含与初版计划不同的修正），标注 ⬜ 的是后续计划。

---

## 1. 现状分析

### 1.1 事件处理：原为严格串行（Phase 2 前）

`listen` 命令的主循环曾是 Python `for` 循环，逐条同步处理事件：

```python
# cli.py（改造前）
for payload in app.lark_client.consume_payloads(status_callback=app.record_daemon_status):
    _print_json(app.handle_payload(payload).to_dict())
```

- 无事件队列、无 worker 池、无异步调度
- 一条 `handle_payload` 从头跑到尾，才会取下一条
- 多人同时 @ 机器人，后一条完全等前一条结束

### 1.2 任务内部：已有局部并行

单次分析内部存在 `ThreadPoolExecutor` 并行（行号以当前代码为准）：

| 位置 | 模式 | 用途 |
|---|---|---|
| `agents/bug/run_primary.py:213` | `ThreadPoolExecutor(max_workers=3)` | 并行拉 bug 数据 + work item + 信号 catalog 预热 |
| `agents/bug/run_primary.py:411` | `ThreadPoolExecutor(max_workers=1)` | 后台收集 source evidence，不阻塞主流程 |
| `app_server_investigation.py:168` | `ThreadPoolExecutor(max_workers=3)` | 同上，bug 数据并行拉取 |

> 这些子进程/线程池意味着**重活实际跑在子进程（codex/claude CLI）与 I/O 等待中**，GIL 在等待时释放——因此「多线程 worker + subprocess」是正确的并发模型（见 §11 非目标，拒绝 asyncio/多进程）。

### 1.3 线程安全审计与改造结果

| 组件 | 改造前 | 改造后（✅） | 说明 |
|---|---|---|---|
| `AgentActivityStore` | `threading.RLock` | 不变 | 原本已安全 |
| `ProcessWatchdog` | `threading.Lock` | 不变 | 原本已安全 |
| `CodexAppServerClient` | `threading.Lock` | 不变 | JSON-RPC 按 request_id 分发 |
| `EventStateStore` | 无锁 | `threading.Lock` | `mark_seen` 是散落在各处理路径上的 **test-and-set**（见 §1.3.1），加锁保证去重正确 |
| `ConversationContextStore` | 无锁 | `threading.RLock` | `find/delete/remember_alias` 内部调 `lookup`，必须可重入；`lookup` 返回 live reference（**不 deepcopy**） |
| `CaseStore` | 无锁 | `threading.RLock` | `save_from_result` 内部调 `save`，必须可重入 |
| `HealthMonitor` | 无锁 | `threading.Lock` | 保护 `_last_event_time` / `_event_consumer_pid`（非计数器，见 §4.5） |
| `BridgeApp._progress_cards` | 无锁 | `threading.Lock`（细粒度） | dict 结构操作持锁，网络发卡片在锁外 |
| `LarkClient`（发消息） | 无锁 | 不变（暂不加锁） | 各调用独立 `subprocess.run`；先观察，按需加发送信号量（见 §12） |
| `report_server` publish | 无锁 | 不变 | 各 job 用独立 `job_id` 目录，物理隔离 |

#### 1.3.1 关键认知：去重的真实机制

**去重不是入口处一个统一的「去重闸门」**，而是 `state_store.mark_seen(event)` 这一 **test-and-set** 散落在 7 个文件、50+ 处——每条处理路径在「即将产生副作用前」调用一次：

```python
if not self.state_store.mark_seen(event):
    return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)
```

含义与对并发的影响：
- 同一 `event_id` 即便被并发分到两个 worker，最终也只有一个能通过各自路径上的 `mark_seen`（加锁后该 test-and-set 原子）→ **正确性成立**；代价是重复 event 的「前段工作」可能做两遍（罕见，可接受）。
- dispatcher **不**承担去重职责，也不在入队前用 `has_seen` 做强去重（`has_seen` 只读、在并发下挡不住真正的并发重复，仅作 best-effort）。**实现时绝不能把 `mark_seen` 上移到 dispatcher 而删掉各路径上的调用**——那才会出错。

### 1.4 服务启动

```bash
# run.sh
exec "$PYTHON_BIN" -m lark_agent_bridge listen --config "$CONFIG"
```

仍为单进程。并发由进程内的 worker 线程池提供（见 §3），通过配置项控制（见 §5）。

---

## 2. 设计目标

| 目标 | 说明 |
|---|---|
| G1 | **不同聊天的不同请求可并发处理**：群 A 的 bug 分析和群 B 的信号分析同时进行 |
| G2 | **同一聊天的 followup 串行**：reply 续聊依赖上下文一致性，不能和前一个请求并发 |
| G3 | **轻量请求不被阻塞**：`你是谁` / `帮助` 不需要等 bug 分析结束 |
| G4 | **资源可控**：并发数有上限，内存/CPU/API 调用可预期 |
| G5 | **可观测**：可看到排队长度、活跃 worker 数、各 job 状态 |
| G6 | **渐进改造**：分阶段落地，每阶段可独立验证和回滚 |

---

## 3. 目标架构（✅ Phase 2 已实现）

### 3.1 整体流程与核心设计决策

```
飞书消息 → lark-cli event consume（子进程，不变）
    → LarkClient.consume_payloads()（生产者，主线程）
    → EventDispatcher.dispatch(payload)
         ├─ light → 主线程内联 app.handle_payload(payload)
         └─ heavy → 入队 → worker 线程 + chat 锁 → app.handle_payload(payload)
```

**核心决策：dispatcher 是纯调度层，只决定 `handle_payload` 在哪个线程跑，绝不改 `handle_payload` 本身。**

这一决策让红线天然满足：
- `handle_payload` 内部仍先判断 card action（`_looks_like_card_action_payload` → `CardActionEvent`）再走消息事件——**card 交互不被破坏**（初版计划的 dispatch 无脑 `LarkEvent.from_dict` 会破坏审批/重分析按钮，已修正）。
- 触发矩阵、权限语义、路由逻辑（`_dispatch_route`）完全不变。
- 重量误判只改变执行线程，不影响正确性。

### 3.2 核心组件

#### 3.2.1 `EventDispatcher`（`lark_agent_bridge/dispatcher.py`）

职责：重量分类 → light 主线程内联 / heavy 入队 → worker 池消费（按 `chat_id` 串行）。

```python
class EventDispatcher:
    def __init__(self, app, *, max_workers=3, max_queue_size=32,
                 heavy_timeout_seconds=1800, light_inline=True, on_result=None): ...
    def start(self) -> None: ...                 # 启动 worker 线程
    def dispatch(self, payload) -> None: ...      # 生产者（主线程）调用
    def shutdown(self, *, timeout=30) -> None: ...# sentinel + join，优雅退出
    def metrics(self) -> dict: ...                # active_workers / queue_depth / 计数

    def dispatch(self, payload):
        weight, chat_id = self._classify(payload)
        # ... 计数 ...
        if weight == "light":
            self._handle(payload, chat_id=None)   # 内联，且不持 chat 锁
        else:
            self._heavy_queue.put((payload, chat_id))  # 队列满则阻塞（背压，不丢事件）
```

`on_result` 回调（cli 传入 `lambda r: _print_json(r.to_dict())`）保留了「每事件结果输出到 stdout」这一可观测路径；调用被 `_output_lock` 串行化以避免多 worker 的 JSON 交错。

#### 3.2.2 重量分类（`_classify` / `_is_light_event`）

**「light」的定义极保守**：只有确定性轻、无任何 I/O 的请求才内联，避免「heavy 被误判 light → 阻塞 consume 主循环」。

```python
_HEAVY_CARD_ACTIONS = frozenset({"reanalyze", "continue_agent", "confirm_bug_agent_reanalysis"})

def _classify(self, payload):
    if self._app._looks_like_card_action_payload(payload):
        action = CardActionEvent.from_dict(payload).action.strip()
        return ("heavy" if action in _HEAVY_CARD_ACTIONS else "light"), chat_id
    event = LarkEvent.from_dict(payload)
    return ("light" if self._is_light_event(event) else "heavy"), event.chat_id

def _is_light_event(self, event):
    # 仅 p2p + 纯文本 + 非 reply + 命中 basic_chat 模板才内联
    return (self._light_inline
            and event.chat_type == "p2p"
            and event.message_type == "text"
            and not event.reply_to
            and build_basic_chat_reply(event.content, command_prefixes=...) is not None)
```

分类依据（来自代码侦察）：

| 路由 / card action | 归类 | 理由 |
|---|---|---|
| `basic_chat`（你是谁/帮助/问候） | **light** | `build_basic_chat_reply` 纯模板，无 I/O |
| `omlx_chat` | heavy | HTTP POST 到本地 LLM（1–30s+） |
| `knowledge_qa` / `knowledge_probe` | heavy | 向量检索 + 可能 source_investigation 子进程 |
| `addr2line_resolve` / `rom_version_lookup` | heavy | **看似轻实则起子进程**（符号表/ROM 元数据） |
| card: `reanalyze` / `continue_agent` / `confirm_bug_agent_reanalysis` | heavy | 触发 `bug_runner` 子进程（30–300s） |
| card: `approve`/`reject`/`feedback`/`escalate`/`select_bug_agent`/`answer_from_report`/`cancel_*` | light | 即时内存状态操作 + 单条回复 |

> 群消息一律 heavy：群消息需要先 strip `@bot` 才能判 basic_chat，dispatch 阶段不重复这套逻辑，交给 worker。这是保守取舍（最坏只是延迟），不影响正确性。

#### 3.2.3 `ChatSessionLock` 与 worker

```python
class ChatSessionLock:
    def get_lock(self, chat_id) -> threading.Lock:   # 同 chat_id 返回同一把锁

def _handle(self, payload, *, chat_id):
    lock = self._chat_locks.get_lock(chat_id) if chat_id else None
    if lock: lock.acquire()
    try:
        result = self._app.handle_payload(payload)
        if self._on_result and result is not None:
            with self._output_lock:
                self._on_result(result)
    finally:
        if lock: lock.release()
```

约束与要点：
- **同 `chat_id` 的 heavy 任务串行**（G2）；不同 chat 用不同锁，并发（G1）。
- **light 不持 chat 锁**（`chat_id=None`）：basic_chat 无状态副作用，若让它去抢同 chat 的 heavy 锁反而会被阻塞，违背 G3。
- `chat_id` 是 `root_message_id` 锁的超集（context 按 root 存），同群不同对话链会被偏保守地串行化——正确但牺牲一点并发，可接受。
- 锁顺序固定为 `chat_lock → state/context 锁`，不存在反向获取，无死锁。

---

## 4. 线程安全改造清单（✅ Phase 1 已实现）

### 4.1 `EventStateStore` — `threading.Lock`

`mark_seen` 整体持锁（test-and-set + 文件 append + `_compact`）；`has_seen` 不持锁（set 成员查找原子，仅作 best-effort 预检）。保留 §1.3.1 的 per-path 调用语义不变。

### 4.2 `ConversationContextStore` — `threading.RLock`（关键修正）

- **必须用 `RLock` 而非 `Lock`**：`find`/`delete`/`remember_alias` 内部调用 `lookup`，普通 Lock 会自死锁。
- 加锁方法：读 `find`/`lookup`/`latest_for_chat`，写 `remember`/`remember_alias`/`append_exchange`/`rewrite_branch`/`clear`/`delete`/`prune_expired`（这些会遍历 `_contexts` 或调 `_save`，`_save` 自身遍历 `_contexts.items()`，并发改会触发 `dict changed size during iteration`）。
- **`lookup` 返回 live reference，不 deepcopy**：`append_exchange`/`rewrite_branch` 依赖拿到 stored object 引用做就地 mutate；9 个外部调用方全部只读；`context_key` 写回也不持久化。初版计划提议的 `deepcopy` 会破坏就地 mutate 模式且违反红线，已否决（基于对全部调用方的审计）。

### 4.3 `LarkClient` — 暂不加锁

各发消息方法走独立 `subprocess.run`，无共享状态。Phase 1 不加锁，观察运行表现；若出现偶发 429 / token 竞争，按 §12 加发送信号量。

### 4.4 `BridgeApp._progress_cards` — `threading.Lock`（细粒度）

- 锁定义在 `_HandleEventMixin.__init__`（`handle_event.py`），跨 `_HandleEventMixin`/`_ResultBugMixin`/`_LogResourcesMixin` 共 13 处访问点。
- **细粒度策略**：只在 dict 结构操作（get/setitem/pop/iterate/`in`）处持锁；`reply_card`/`update_card`/`send_card_response` 等**网络发送绝不持锁**（否则会序列化所有卡片发送并长时间持锁）。
- `_prune_stale_progress_cards` 的「遍历 + pop」整体持锁，消除与 worker / daemon 清理线程并发修改导致的 `RuntimeError`。

### 4.5 `HealthMonitor` — `threading.Lock`（修正字段）

实际可变态是 `_last_event_time`（`record_event_processed` 赋值时间戳）与 `_event_consumer_pid`，**并非**初版假设的 `_events_processed += 1` 计数器。`check_health` 不持锁，叶子读方法（`_check_event_consumer`/`_check_event_lag`）持锁读出快照后在锁外做系统调用（`_process_alive`），避免嵌套。

### 4.6 `CaseStore` — `threading.RLock`

`save_from_result` 内部调 `save`，需可重入。所有 public 方法持锁；`_persist`/`_enforce_limit` 仅被持锁的 public 方法调用，不单独加锁。

---

## 5. 配置扩展（✅ 已实现）

### 5.1 新增配置项（`[event_consumer]`）

```toml
[event_consumer]
# 现有
event_key = "im.message.receive_v1"
# ... 其余现有字段 ...
# 新增（Phase 2）
max_concurrent_jobs = 3          # worker 线程数
max_queue_size = 32              # 重量任务队列深度
heavy_job_timeout_seconds = 1800 # 保留字段（见下）
light_inline = true              # 是否允许 basic_chat 在主线程内联
```

`EventConsumerOptions`（`models.py`）已加上述字段并在 `config.py` 解析。

> `heavy_job_timeout_seconds` 暂为保留字段：Python 线程无法被强杀，单任务超时由 `handle_payload` 内部（各 runner 超时）与 `ProcessWatchdog` 负责，dispatcher 层不重复实现（避免过度设计）。

---

## 6. `cli.py` listen 改造（✅ 已实现）

```python
if args.command == "listen":
    # ... dry_run / purge / cleanup / start_report_server / _start_cleanup_loop 不变 ...
    ec = config.event_consumer
    dispatcher = EventDispatcher(
        app,
        max_workers=ec.max_concurrent_jobs,
        max_queue_size=ec.max_queue_size,
        heavy_timeout_seconds=ec.heavy_job_timeout_seconds,
        light_inline=ec.light_inline,
        on_result=lambda result: _print_json(result.to_dict()),  # 保留 per-event 可观测输出
    )
    dispatcher.start()
    try:
        for payload in app.lark_client.consume_payloads(status_callback=app.record_daemon_status):
            dispatcher.dispatch(payload)
    finally:
        dispatcher.shutdown()
        app.stop_report_server()
        if stop_cleanup is not None:
            stop_cleanup.set()
    return 0
```

---

## 7. `lifecycle.py` 状态机对齐（✅ Phase 4 已实现）

`AnalysisState.QUEUED` 现已正式启用。dispatcher **非侵入**地串联状态（不改 `handle_payload`/runner）：

```
入 heavy 队列 → QUEUED → worker 取出 → ANALYZING → 结果 → COMPLETED/FAILED
```

- `dispatcher._begin_lifecycle`（入队）→ `LifecycleStore.create` + `transition_to(QUEUED)`。
- `dispatcher._handle`（worker 取出）→ `ANALYZING`；结束据 `result.success` → `COMPLETED`/`FAILED`。
- light 请求不创建 lifecycle（不排队、瞬时）。
- **粗粒度且诚实**：dispatcher 不知道具体分析类型（由 `_dispatch_route` 决定），故 `analysis_type=UNKNOWN`，且不细分 DOWNLOADING（那需侵入 runner，违反红线）。
- 线程安全：`LifecycleStore` 补 `RLock`（保护 `_lifecycles` dict）；`AnalysisLifecycle.transition_to` 补 per-object `RLock`（原子更新 state + updated_at + `transitions.append`，经对抗审查加固，防御 health 端点并发读）。全程 best-effort：lifecycle 任何异常都不影响事件处理。

---

## 8. 可观测性

### 8.1 dispatcher 指标（✅ 已实现 `metrics()`）

`EventDispatcher.metrics()` 返回真实数据（`active_workers` 由 worker 进出时在锁内增减，已修正初版「恒为 0」的缺陷）：

```python
{"max_workers", "active_workers", "queue_depth", "chat_locks_held",
 "total_dispatched", "total_light", "total_heavy"}
```

### 8.2 接入端点 / admin 面板（✅ Phase 3 已实现）

- `/api/health` 的 `components` 现含 `dispatcher`（worker/queue/计数）与 `lifecycle`（`active_count` + `active_jobs` 摘要）。
- 接线 seam：`ReportHttpServer.set_dispatcher_metrics_provider(dispatcher.metrics)`，cli 在 `dispatcher.start()` 后设置（report server 先于 dispatcher 创建，故用 setter 而非构造参数，请求时读 live 值）；`lifecycle_store` 经 `BridgeApp` 传入 report server。
- lifecycle 摘要只取单字段（state/chat_id/request_text/elapsed），**不遍历 transitions list**，避免与 worker 的 append 竞争。
- `/admin` 的 daemon-view 追加 dispatcher/lifecycle 卡片（额外 fetch `/api/health`）。

---

## 9. 测试策略

### 9.1 已新增测试（✅）

| 文件 | 覆盖 |
|---|---|
| `tests/test_dispatcher.py` | 分类（light/heavy/card 分流）、light 主线程内联、heavy worker、同 chat 串行、不同 chat 并行、优雅 shutdown、metrics、ChatSessionLock |
| `tests/test_state_thread_safety.py` | `EventStateStore` 并发 `mark_seen` 单一 winner、`ConversationContextStore` 并发读写不崩 |

### 9.2 压力测试方式（修正）

> **不要用多进程 `handle-event` 压测**：那是进程隔离，验证不了进程内的线程安全，反而制造多进程争抢同一 state 文件的假象。线程安全用**单进程多线程**（即 `test_dispatcher.py` / `test_state_thread_safety.py` 的方式）验证。

---

## 10. 分阶段路线图

| 阶段 | 范围 | 风险 | 状态 |
|---|---|---|---|
| **Phase 1** | 各 store + `_progress_cards` 加锁 | 低 | ✅ 完成，全量测试无新增失败 |
| **Phase 2** | EventDispatcher + chat 锁 + worker 池 + cli + 配置 | 中 | ✅ 完成，新增单元/并发测试通过 |
| **Phase 3** | `/api/health` 指标 + admin 面板 | 低 | ✅ 完成 |
| **Phase 4** | QUEUED 状态启用 + lifecycle 并发追踪 | 低 | ✅ 完成（非侵入粗粒度 + LifecycleStore 加锁） |
| **Phase 5** | Codex App Server warm pool | 高 | ⛔ Deferred（见 §16） |

---

## 11. 不做什么（Non-goals）

| 项 | 理由 |
|---|---|
| 改为 asyncio 架构 | 现有代码明确选择同步 API，改造成本极高；重活在子进程，多线程已够 |
| 多进程 worker（fork） | 共享状态（`data/` 目录、JSON 文件）需分布式锁，复杂度过高 |
| 外部队列（Redis/RabbitMQ） | 引入新依赖，当前规模不需要 |
| 通用任务调度框架 | 过度设计 |
| 按能力分独立 worker 池 | 当前并发量不足以支撑此复杂度 |

---

## 12. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| 重量预分类把 heavy 误判为 light | 重活在主线程跑，阻塞 consume 循环 | **已通过极保守的 light 定义规避**（仅 p2p 纯文本非 reply 且命中 basic_chat 模板）；其余一律 heavy |
| `lark-cli` 并发调用超飞书 API 限流 | 偶发 429 | 先观察；必要时加发送信号量 `threading.Semaphore(N)` |
| 多个 heavy 同时下载大日志 | 磁盘 I/O 争用 | job 目录按 `event_id`/`job_id` 隔离；HDD 环境降 `max_concurrent_jobs=2` |
| Codex API rate limit | 多分析同时调 LLM 超限 | `max_concurrent_jobs` 即并发上限 |
| chat 锁死锁 | worker 永久阻塞 | 用 `Lock`（非嵌套获取不同 chat 锁）；锁顺序固定 `chat→state/context`；watchdog 检测 stuck |
| 优雅 shutdown 超时 | 退出时 worker 未完成 | `shutdown(timeout=30)` + 日志记录未处理队列长度 |

---

## 13. 容量估算

### 13.1 负载假设

- 日均消息 ~100–500 条（需 bot 处理 ~20–50 条）；重分析 ~5–15 条/天；峰值群内 3–5 条/分钟。

### 13.2 资源与推荐配置

| `max_concurrent_jobs` | 内存增量 | 适用场景 |
|---|---|---|
| 1 | 基线 | 开发/测试（等同串行，便于调试） |
| 2–3（默认 3） | ~200–300MB | 小团队（5–20 人） |
| 3–5 | ~500MB | 大团队（50+，需 8+ 核 + SSD） |

---

## 14. 验证命令（pytest）

```bash
# 全量回归（实测 1242 passed, 41 subtests, ~66s）
python3.11 -m pytest tests/ -q

# 新增并发测试
python3.11 -m pytest tests/test_dispatcher.py tests/test_state_thread_safety.py -v

# 变更相关快速子集（~1s）
python3.11 -m pytest tests/test_state.py tests/test_case_store.py tests/test_health.py \
    tests/test_cli.py tests/test_card_actions.py tests/test_lifecycle.py \
    tests/test_file_size_boundaries.py -q
```

通过标准：失败集合 ⊆ 基线预存环境相关失败，无新增。**当前实测：零失败。**

> 注：测试框架是 **pytest**（class-based + `assert`），非 unittest。文件行数上限 2000 行（`test_file_size_boundaries.py` 守护）。

---

## 15. 典型并发场景时序

### 15.1 不同群同时发 bug 链接（G1）

```
T=0s  群A @bot bug...  → heavy → worker-1 取出，acquire(chatA)
T=1s  群B @bot bug...  → heavy → worker-2 取出，acquire(chatB)   ← 并行
T=5s  私聊 你是谁       → light → 主线程 0.1s 回复（不占 worker、不阻塞）
T=60s worker-1 完成A → release(chatA)
T=75s worker-2 完成B → release(chatB)
```

### 15.2 同群 reply 续聊（G2）

```
T=0s   群A @bot bug...   → heavy → worker-1 acquire(chatA)
T=10s  群A @bot 继续分析 → heavy → worker-2 取出，acquire(chatA) → 阻塞等待
T=60s  worker-1 完成 → release(chatA)
T=60s  worker-2 获锁 → 开始处理
```

### 15.3 轻量请求不被阻塞（G3）

```
T=0s   群A @bot bug...  → heavy → worker-1（预计 60s）
T=5s   私聊 你是谁       → light → 主线程 0.1s 回复（不入队、不持任何锁）
T=10s  群B @bot 帮我查… → heavy → worker-2 立即处理
```

---

## 16. Phase 5 评估结论：Codex App Server 池化 —— Deferred（不实现）

经代码评估（`agents/codex_app_server_runtime.py` / `agents/bug/custom_skill.py`），warm pool **高风险、边际收益**，决定不实现，保持现状（每次分析 fresh instance）。

**现状**：每次 `run_turn` 由 `CodexAppServerClient` `subprocess.Popen` 启一个新 codex app-server 进程，结束即销毁；无池化基础设施。

**风险（均为会引入并发数据损坏的硬伤）**：
- **JSON-RPC request_id 冲突**：`_next_id` 是实例内计数器；进程被多 client 共享时不同 client 的 id 会碰撞，响应路由到错误的 pending 队列 → 挂起/错乱。
- **stdout/stderr 多路复用缺失**：每 client 独立读线程 + 实例内队列，无按 request_id 解复用；多 client 并发写同一进程 stdin → JSON 行交错/截断。
- **session 隔离缺失**：codex app-server 不支持单进程多并发 turn；`thread_id`/`turn_id` 是实例级，复用进程会串话/状态泄漏。

**收益**：冷启动 ~15s（含 minimal home 拷贝），而单次分析 300–600s+，仅省 2–5%，不足以抵消上述风险。

**若将来确需**：先在生产测量真实冷启动占比；若 >10% 再设计带显式 request_id 工厂、client-进程绑定、解复用层的会话池，并配全面并发测试。轻量替代：boot 时预热 template home、预取 `models_cache.json`，或做常驻 sidecar 守护进程（独立 Phase），而非每 job 实例。
