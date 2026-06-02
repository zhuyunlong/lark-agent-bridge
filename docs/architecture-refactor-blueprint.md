# 架构重构蓝图（Architecture Refactor Blueprint）

本文档定义 `lark_agent_bridge` 的**目标架构**、解耦原则、业务域分层、组合替代 Mixin 的设计、文件行数预算与分阶段迁移路线图。现状描述见
[project-overview-architecture.md](./project-overview-architecture.md)。

> 适用范围：`lark_agent_bridge/` 包（约 5.4 万行源码 + 3.4 万行测试，929 个用例）。
> 重构红线：**不新增测试失败**。基线为 929 测试 / 30 项预存环境相关失败。

---

## 1. 现状问题清单

| # | 问题 | 位置 | 风险 |
|---|---|---|---|
| P1 | 上帝类：13 个 Mixin 拼一个 `BugAnalysisRunner` | `agents/bug/` | 职责纠缠、难测、难改 |
| P2 | ~~按行数硬切命名 `run_bug / run_bug_2 / run_bug_3`~~ ✅已重命名为 `run_primary / run_reanalysis / agent_summary` | `agents/bug/` | 命名无业务语义（已解决） |
| P3 | `__all__` 命名空间污染（标准库符号被 re-export） | `app/_shared.py`、`agents/bug/_shared.py` | `import *` 把 `os/json/re/Path` 灌进子模块命名空间 |
| P4 | 多文件逼近 2000 行硬上限 | `handle_event.py` 1750、`source_investigation.py` 1738、`bug_prompt.py` 1525 | 继续加功能就会撞墙 |
| P5 | 报告服务端口测试脆弱 | `tests/test_report_server.py` | 仅 catch `PermissionError`，端口占用变 error |
| P6 | `BridgeApp` 5-Mixin 横切 | `app/` | 同 P1，程度较轻 |

---

## 2. 目标架构原则

1. **组合优于继承（Composition over Mixin）**
   用「协作对象」替代「Mixin 多继承」。一个门面类（facade）持有若干**单一职责的协作者**，通过委托而非 MRO 共享行为。
2. **按业务域分层（Domain-oriented），不按行数切**
   文件名/模块名表达业务语义（`classify` / `execute` / `render` / `source_evidence`），禁止 `_2 / _3` 这类机械命名。
3. **显式依赖，禁止 `import *` 污染命名空间**
   `_shared.py` 只导出**本模块自己定义**的符号；标准库与跨包依赖由各文件按需显式 import。
4. **稳定的公共契约**
   对外入口（`BridgeApp`、`BugAnalysisRunner` 类名与公共方法签名、`agents/__init__.py` 的 `__all__`）保持不变，重构是内部结构调整。
5. **测试驱动的安全网**
   每一步以"运行 unittest 套件、对比基线无新增失败"为通过门槛。
6. **行数预算**
   单文件软目标 ≤ 800 行，硬上限 2000 行（由 `test_file_size_boundaries.py` 守护）。

---

## 3. 业务域划分

桥接器的真实业务域：

```
                ┌────────────────────────────────────────────┐
                │                 入口域 (Ingress)            │
                │  cli → BridgeApp(handle_event)              │
                │  权限(policy) · 解析(parser) · 路由          │
                └───────────────┬────────────────────────────┘
                                │ 分发
   ┌────────────┬──────────────┼───────────────┬──────────────┐
   ▼            ▼              ▼               ▼              ▼
┌────────┐ ┌────────┐   ┌────────────┐   ┌──────────┐   ┌──────────┐
│ Signal │ │  Bug   │   │   Direct   │   │Perception│   │Knowledge │
│ 信号域 │ │ 缺陷域 │   │ 直传日志域 │   │ 感知域   │   │ 知识域   │
└────────┘ └───┬────┘   └────────────┘   └──────────┘   └──────────┘
               │ bug 内部子路由
        ┌──────┼───────┬────────┬────────┐
        ▼      ▼       ▼        ▼        ▼
     startup  stuck  crash   signal  fallback
                                │
                ┌───────────────┴───────────────┐
                ▼                                ▼
         ┌────────────┐                   ┌────────────┐
         │  报告域    │                   │  状态域    │
         │ reporting  │                   │  state     │
         │report_server│                  │case/lifecycle│
         └────────────┘                   └────────────┘
```

横切关注点（cross-cutting）：下载（`downloader`）、健康（`health`）、审批/升级/仲裁/归档、token 统计、日志。

---

## 4. `BugAnalysisRunner` 组合化设计（P1/P2/P4 核心）

### 4.1 现状（13 Mixin 多继承）

```python
class BugAnalysisRunner(
    _ResolveSourceMixin, _RunBugMixin, _RunBug2Mixin, _BugCacheMixin,
    _ArchiveExtractMixin, _GeneralSummaryMixin, _SignalAndroidMixin,
    _CustomSkillMixin, _LdExecutorMixin, _BugPromptMixin,
    _DirectApiMixin, _RunBug3Mixin, _RenderBugMixin,
):
    ...
```

问题：所有 Mixin 共享同一个 `self` 命名空间，方法/属性互相隐式依赖，无法单独测试，`run_bug_2/3` 命名无意义。

### 4.2 目标（按业务职责归并的协作者）

把 13 个 Mixin 归并为 **5 个单一职责协作者**，`BugAnalysisRunner` 作为门面委托：

| 协作者 | 吸收的现有 Mixin | 职责 |
|---|---|---|
| `BugSourceResolver` | `_ResolveSourceMixin` + `_ArchiveExtractMixin` + `_BugCacheMixin` | 定位 bug 源、拉取、解压、缓存 |
| `BugSkillSelector` | `_SignalAndroidMixin` + `_CustomSkillMixin`（分类部分） | 路由到 startup/stuck/crash/signal/custom |
| `BugExecutor` | `_RunBugMixin` + `_RunBug2Mixin` + `_RunBug3Mixin` + `_LdExecutorMixin` + `_DirectApiMixin` | 执行分析脚本/直连 API（按业务重命名为 `run_startup_stuck` / `run_signal` / `run_direct` 等） |
| `BugPromptBuilder` | `_BugPromptMixin` + `_GeneralSummaryMixin` | 构建 Agent prompt / 结构化总结 |
| `BugReportRenderer` | `_RenderBugMixin` | 渲染最终报告 |

```python
class BugAnalysisRunner:
    def __init__(self, config, ...):
        self._source = BugSourceResolver(config, ...)
        self._selector = BugSkillSelector(config, ...)
        self._executor = BugExecutor(config, ...)
        self._prompt = BugPromptBuilder(config, ...)
        self._renderer = BugReportRenderer(config, ...)

    # 保持现有公共方法签名，内部委托
    def run_bug_analysis(self, request): ...
```

> 注意：现状下 Mixin 之间通过 `self.<other_mixin_method>` 强耦合。组合化是**阶段三的高风险改造**，必须：
> (a) 先抽出协作者但暂时仍由门面 `self` 透传共享状态；(b) 逐方法迁移；(c) 每步跑测试。
> 若某 Mixin 的内部耦合度过高、拆分收益 < 破坏测试风险，则保留为内部协作类但**至少重命名**去掉 `_2/_3`。

### 4.3 命名迁移（P2）

| 现状 | 落地结果（阶段三任务 8 已执行） |
|---|---|
| `run_bug.py` / `_RunBugMixin` | ✅ `run_primary.py` / `_RunPrimaryMixin`（主分析路径 `run_bug_analysis`） |
| `run_bug_2.py` / `_RunBug2Mixin` | ✅ `run_reanalysis.py` / `_RunReanalysisMixin`（`run_bug_reanalysis` + `run_bug_agent_followup`） |
| `run_bug_3.py` / `_RunBug3Mixin` | ✅ `agent_summary.py` / `_AgentSummaryMixin`（Agent 总结执行/用量/运行时 HTML） |

> 公共方法名（`run_bug_analysis` 等）作为对外契约保持不变，仅模块/Mixin 命名去机械化。
> 各 Mixin 之间无 import 依赖，仅在 `bug_runner.py` 组合时通过 `self` 共享状态，故重命名零回归。

---

## 5. `_shared.py` 命名空间收口（P3）

### 5.1 现状

`app/_shared.py` 末尾的 `__all__` 同时列入：
- **标准库符号**：`dataclass, field, datetime, timezone, html, json, os, Path, re, shutil, stat, Callable, quote, urlsplit, urlunsplit`
- 跨包 re-export：`BugAnalysisRunner, LarkClient, ...`（约 100 个）
- 本模块定义：`_RouteContext, RouteCandidate, _BugReanalysisDecision, CHAT_COMMAND_PREFIXES, ...`

各子模块用 `from ._shared import *` 一次性拿到全部，导致 `os/json/re/Path` 等被注入子模块命名空间。

### 5.2 目标

`__all__` **只保留本模块自己定义的符号**（路由上下文、常量、决策类）。标准库与跨包依赖：
- 方案 A（低风险，阶段二采用）：`_shared.py` 仍 import 这些符号供运行期使用，但**从 `__all__` 移除**标准库符号；子模块改为显式 import 标准库。
- 方案 B（彻底）：删除 `import *`，各子模块显式 import。属阶段三范畴。

阶段二只做**方案 A 的 `__all__` 收口**：移除标准库符号，使 `import *` 不再污染。需先核对各子模块是否依赖经由 `import *` 透传的标准库符号，必要时在子模块补显式 import。

---

## 6. `report_server` 测试稳定化（P5）

现状：真实绑定 socket 的 4 处用例只 `except PermissionError: skipTest(...)`。端口被占用时抛 `OSError(EADDRINUSE)` → 变成 test error。

目标：将捕获范围扩展为 `(PermissionError, OSError)` 统一 skip（沙箱/CI 环境绑定受限是已知约束，非逻辑错误）。端口已用 `port=0`（系统分配临时端口），无需额外 find-free-port 逻辑。

---

## 7. 分阶段迁移路线图

| 阶段 | 范围 | 风险 | 验证门槛 |
|---|---|---|---|
| **一**（本次已做） | 架构文档：重写总览 + 本蓝图 | 无（纯文档） | N/A |
| **二** | P3 `__all__` 收口 + P5 端口测试稳定化 | 中（小步可验证） | 每步跑 unittest，对比基线无新增失败 |
| **三** | P1/P2/P4 `BugAnalysisRunner` 组合化 + 超大文件按业务域拆分 | 高 | 逐协作者迁移，每步保证测试绿 |

### 阶段三细化步骤（建议顺序）

1. 抽 `BugReportRenderer`（`_RenderBugMixin`，渲染相对独立，风险最低）→ 跑测试
2. 抽 `BugPromptBuilder`（prompt + summary）→ 跑测试
3. 抽 `BugSourceResolver`（source + archive + cache）→ 跑测试
4. 抽 `BugSkillSelector`（分类路由）→ 跑测试
5. ✅ 去机械命名：`run_bug_2/3` → `run_reanalysis`/`agent_summary`（已完成）。进一步抽 `BugExecutor` 委托式组合因测试 mock 路径深度耦合（`agents.bug.custom_skill.*` / `archive_extract.*` 等被字符串 patch）暂缓，按 §4.2 原则保留协作模块
6. 对 `app/handle_event.py`、`knowledge/source_investigation.py`、`agents/bug/bug_prompt.py` 等逼近行数上限的文件，在拆分协作者后顺势按业务域切分，保持 ≤ 800 行软目标

> 每一步都是独立可回滚的提交粒度。任一步出现新增测试失败，立即停止并定位根因，不做增量打补丁式的盲改。

---

## 8. 不做什么（Non-goals）

- 不改对外行为/触发矩阵/权限语义
- 不改公共类名与公共方法签名（`BridgeApp`、`BugAnalysisRunner`、`agents/__init__.py` 的 `__all__`）
- 不引入新框架/新依赖
- 不动配置格式与 CLI 命令
- 不为「重构而重构」拆分耦合度极高、收益低于风险的内部逻辑——此类只做去机械命名

---

## 9. 验证命令

```bash
# 全量基线（pytest 不可用，使用 unittest）
python3.11 -m unittest discover -s tests

# 单模块快速回归
python3.11 -m unittest tests.test_report_server -v
python3.11 -m unittest tests.test_agents_bug_agent tests.test_agents_bug_analysis -v

# 文件行数守护
python3.11 -m pytest tests/test_file_size_boundaries.py  # 或在 unittest 中由 discover 覆盖
```

通过标准：失败集合 ⊆ 基线 30 项预存环境相关失败，无新增。
