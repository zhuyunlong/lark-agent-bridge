# 重构基线：现状架构盘点（2026-06-11）

> 本文档是本轮"完全重构"的第一份传承文档。后续文档：
> [02-target-architecture.md](./02-target-architecture.md)（目标架构）、
> [03-refactor-plan.md](./03-refactor-plan.md)（分步计划）、
> [steps/](./steps/)（每步执行记录）、[PROGRESS.md](./PROGRESS.md)（进度快照）。
> 历史蓝图见 [../architecture-refactor-blueprint.md](../architecture-refactor-blueprint.md)，本轮重构传承其原则并扩展"可配置/无硬编码"维度。

## 0. 规模与基线

| 维度 | 数值 |
|---|---|
| 源码 | `lark_agent_bridge/` 约 6.1 万行（总计含测试 9.9 万行） |
| 测试 | tests/ 87 个文件约 3.8 万行，基于 unittest 风格 + pytest 运行 |
| 行数红线 | `tests/test_file_size_boundaries.py`：单文件硬上限 2000 行 |
| 蓝图软目标 | 单文件 ≤ 800 行 |
| 测试基线 | 见 `tmp/baseline-pytest.log`（重构前全量跑一次，失败集合即基线） |
| 工作分支 | worktree `.claude/worktrees/refactor`，分支 `refactor/architecture-decoupling`，基于 b9b07d6 |

蓝图阶段进度核对（2026-06-11）：
- ✅ 阶段一（架构文档）已完成
- ✅ 阶段二 P3（`__all__` 收口，app/_shared.py 与 agents/bug/_shared.py 已无标准库符号）、P5（report_server 端口测试已 catch `(PermissionError, OSError)`）已完成
- ✅ P2 机械命名已消除（run_primary / run_reanalysis / agent_summary）
- ❌ 阶段三（组合化 + 超大文件按业务域拆分）未做 —— **本轮重构主战场**
- ❌ 配置化/去硬编码未系统做 —— **本轮新增维度**

## 1. 分层全景

```
入口层     run.sh → __main__ → cli.py(main/listen) → EventDispatcher(dispatcher.py)
应用层     app/BridgeApp = 5 个 Mixin（7173 行）：handle_event(主控+40路由) /
           result_bug / log_resources / context_from / replay_flow + _shared(汇聚导入)
解析层     parser.py(1268行: 15+正则、100+触发词) · policy.py · models.py(23个Options dataclass)
执行层     agents/：BugAnalysisRunner=18 Mixin 多继承 · IntentAnalysisRunner ·
           Addr2LineRunner(1851行单文件) · ClaudeSkillRunner · AgentRuntime(三层降级)
知识层     knowledge/：KnowledgeService(1136行) · SourceInvestigationRunner(1738行)
报告层     reporting/(纯字符串拼 HTML、3 套重复 CSS) · report_server.py(1537行 HTTP)
状态层     state.py(3个Store, RLock+原子写) · case_store · lifecycle · approval
横切层     config.py(1039行) · lark_client · cards.py(970行) · health · log ·
           downloader · escalation · arbitration · workflow_archive · token_usage
```

事件主链路：`lark_client.consume_payloads()` → `dispatcher.dispatch()`（light 内联 / heavy 入队按会话链加锁）→ `app.handle_payload()` → `_handle_event()`（@过滤→策略门控→资源获取→`_dispatch_route()` 40+ 分支）→ 各域 Runner → `_deliver_result()`（卡片+HTML报告+会话记录）。

## 2. 单体文件清单（重构目标）

| 文件 | 行数 | 内聚分组（自然拆分边界） |
|---|---|---|
| `agents/addr2line_runner.py` | 1851 | 主流程 / ROM版本 / 符号下载 / 辅助工具（4 界） |
| `app/handle_event.py` | 1813 | 初始化 / 事件入口 / 路由矩阵(40+) / 结果交付 / @提及解析 / 进度卡片 |
| `knowledge/source_investigation.py` | 1738 | 调查主流程 / 5级回退探针 / 证据组装 |
| `app/result_bug.py` | 1669 | 进度卡片 / Skill澄清 / 请求执行(6 runner) / 卡片操作(8种) |
| `agents/bug/bug_prompt.py` | 1560 | LD分析 / 通用分析 / 提示词构建 / 结果处理 |
| `report_server.py` | 1537 | HTTP路由(12+端点) / 发布器 / admin UI 接线 |
| `app/log_resources.py` | 1513 | 资源管理 / 请求构建 / ROM版本查询 / 意图路由 |
| `agents/bug/ld_executor.py` | 1459 | LD 车道级执行 |
| `agents/bug/general_summary.py` | 1442 | 通用总结 |
| `agents/bug/custom_skill.py` | 1418 | 自定义 skill 分类+执行 |
| `agents/bug/resolve_source.py` | 1414 | bug 源定位/拉取 |
| `agents/bug/archive_extract.py` | 1403 | 解压/日志组织 |
| `agents/bug/signal_android.py` | 1402 | 信号/安卓分类 |
| `agents/bug/bug_cache.py` | 1400 | 缓存 |
| `agents/bug/agent_summary.py` | 1376 | Agent 总结执行/用量/HTML |
| `app/context_from.py` | 1351 | 意图初始化 / 跟进澄清(20+方法) / 既有答案QA / 上下文查找 |
| `agents/bug/direct_api.py` | 1350 | 直连 API 执行 |
| `agents/agent_tools.py` | 1319 | 工具注册：缓存/读文件/搜索/codegraph/bash（5 界） |
| `parser.py` | 1268 | 正则区 / 触发词区 / 各 parse_* 函数 |
| `knowledge/service.py` | 1136 | 同步/搜索/答题 10+ 决策分支 |
| `agents/bug/run_reanalysis.py` | 1080 | 追问重分析 |
| `config.py` | 1039 | load_config 单函数 690 行 + 10+ 解析辅助 |
| `agents/bug/run_primary.py` | 1034 | 主分析编排 |

## 3. 两大上帝类

### 3.1 `BridgeApp`（app/，5 Mixin，7173 行）
- `_HandleEventMixin` 初始化 **48 个子组件**，路由矩阵 40+ `_route_*` 方法集中一处。
- Mixin 间无直接 import（通过 `self` duck-typing 协作），`_shared.py` 汇聚全部外部依赖防循环。
- 无循环依赖（已核实）；耦合形态 = 所有 Mixin 共享一个 `self` 命名空间。

### 3.2 `BugAnalysisRunner`（agents/bug_runner.py，18 Mixin）
- 流水线：`run_bug_analysis()` → 计划分类 → 时间推理 → 脚本/agent 执行 → 报告渲染；追问走 `run_bug_reanalysis()`。
- 所有 mixin 通过 `self.config` / `self.skill_manager` 共享状态。
- ⚠️ 测试以字符串路径深度 patch（`agents.bug.custom_skill.*` 等），蓝图据此判定 `BugExecutor` 委托式组合"暂缓"——拆分时必须保持 **模块路径** 不变或在原路径留兼容别名。

## 4. 配置与硬编码现状（Phase 4 输入）

配置事实上散布 **5 处**：
1. `config.py`：TOML + `LARK_AGENT_BRIDGE_*` 环境变量 + 默认值（部分与 models.py 重复定义）
2. `models.py`：23 个 Options dataclass 内嵌默认值 + **3 套内嵌中文系统提示词**（Claude/Intent/OmlxChat）
3. `parser.py`：15+ 正则、100+ 中文触发词（完全不可配置；`config/routing_terms.toml` 已存在先例可借鉴）
4. `cards.py`：UI 文案/状态标签/截断常量内嵌
5. 各模块私有常量：auth.py(PBKDF2/会话TTL)、state.py(50000去重上限)、health.py(3600/300/90阈值)、agent_runtime.py(request_limit=50/tool_calls_limit=80/8192)、agent_tools.py(30s bash超时/256KB/50KB/500行)、archive_extract.py(900/600s)、bug/_shared.py(10分钟/400文件/20000行)、handle_event.py(_progress_cards_max_age_seconds=7200)、lifecycle.py(max_active=500)、codex_app_server_runtime.py(min version (0,125,0)) 等 30+ 项

prompt 管理：agents 层 100% 内联字符串，无模板文件。reporting 层 HTML/CSS 内联拼接，3 套 CSS 副本（约 819+296+138 行重复）。

## 5. 重构必须保持的对外契约

1. **公共 import 路径**（87 个测试文件依赖）：
   `lark_agent_bridge.app.BridgeApp`、`lark_agent_bridge.agents.{BugAnalysisRunner, IntentAnalysisRunner, ClaudeSkillRunner, Addr2LineRunner, OmlxChatClient, PerceptionSummaryRunner}`、`lark_agent_bridge.config.load_config`、`lark_agent_bridge.models.*`、`lark_agent_bridge.parser.parse_*`、`lark_agent_bridge.state.*`、`lark_agent_bridge.lark_client.*`、`lark_agent_bridge.knowledge.*`、`lark_agent_bridge.cards.*`、`lark_agent_bridge.report_server.*`
2. **mock patch 字符串路径**：测试用 `mock.patch("lark_agent_bridge.agents.bug.custom_skill.xxx")` 等字符串 patch —— 被 patch 的符号必须留在原模块路径（facade 重导出可满足）。
3. **公共方法签名**：`run_bug_analysis` / `run_bug_reanalysis` / `handle_event` / `handle_payload` 等不变。
4. **配置格式**：config.toml 既有 section/key 全部向后兼容（只增不改不删）。
5. **CLI 命令**与 run.sh/stop.sh 行为不变。
6. 行为红线：触发矩阵、权限语义、卡片交互不变；测试失败集合 ⊆ 基线。

## 6. 已知测试事实

- 测试运行：`python3.11 -m pytest tests/ -q`（全量约 33 分钟）；`scripts/test_group_chat.py` 的 3 个 collection error 为已知噪音（仅在收集 scripts/ 时出现）。
- 共享基建：`tests/_app_base.py`（FakeLarkClient + 7 个 Fake runner 工厂）、`tests/_agents_base.py`、`tests/_knowledge_base.py`；无 conftest.py。
- 蓝图记录的历史基线：929 用例 / 30 项环境相关预存失败（unittest 口径）。本轮以 `tmp/baseline-pytest.log` 为准。
