# 目标架构（2026-06-11）

> 前置：[01-architecture-baseline.md](./01-architecture-baseline.md)。
> 传承 [../architecture-refactor-blueprint.md](../architecture-refactor-blueprint.md) 的原则：
> 组合优于继承 / 按业务域分层 / 显式依赖 / 稳定公共契约 / 测试安全网 / 行数预算。
> 本文档在其上扩展两点：**全包范围的子包化拆分** 与 **配置收敛（无硬编码）**。

## 1. 设计原则（含本轮新增）

1. **业务域子包化**：每个 >1000 行的单体文件拆为业务域子包，文件软目标 ≤800 行。
2. **facade 兼容**：原模块路径一律保留，内容变为从子包重导出（或薄壳类）。
   测试的 import 路径与 `mock.patch("模块路径.符号")` 字符串全部不破坏。
3. **逻辑分类拆分法**：
   - 纯函数/常量 → 直接搬到子模块，原路径 re-export；
   - Mixin 方法 → 按主题把一个大 Mixin 切成多个小 Mixin，门面类继承列表合并（`self` 命名空间不变，零行为差异）；
   - 可独立协作者（无 `self` 状态依赖或依赖少）→ 抽成协作类/纯函数，原方法变薄委托。
4. **配置三层收敛**：代码内默认值（fallback，保证零配置可跑）→ config.toml 覆盖 → 环境变量覆盖。
   新增配置只增不改：现有 section/key 全部兼容。
5. **文案与 prompt 外置**：触发词表 → `config/` TOML（沿用 routing_terms.toml 先例）；
   系统提示词 → 可由 TOML/文件覆盖，默认值保留在代码中。
6. **报告渲染去重**：公共 CSS/HTML 骨架收敛到 `reporting/_assets.py`（或 templates/），3 套副本合一。
   （注意既有约束：交付给用户的 source_analysis HTML 不复用 bug/source_stage 报告外壳——去重只合并真正相同的基础层。）

## 2. 目标包结构

```
lark_agent_bridge/
├── cli.py / __main__.py / dispatcher.py / lifecycle.py / health.py   （已 <800 行，不动）
├── config.py                  → facade，重导出 configuration/
├── configuration/             【新】config.py 拆分
│   ├── loader.py              load_config 主干（精简后）
│   ├── sections.py            per-section builder（download/lark/bug/ai/...）
│   ├── presets.py             provider preset 加载/应用
│   └── paths.py               路径解析/告警辅助
├── parser.py                  → facade，重导出 parsing/
├── parsing/                   【新】parser.py 拆分
│   ├── patterns.py            全部正则
│   ├── terms.py               触发词表（支持 TOML 覆盖）
│   ├── signal.py / bug.py / addr2line.py / direct.py / misc.py   各域 parse_*
├── models.py                  → 保留 dataclass（955 行 <红线，仅在配置化时小改）
├── cards.py                   → facade，重导出 cards_ui/
├── cards_ui/                  【新】卡片构建拆分：elements.py / status_card.py / result_card.py / texts.py
├── app/
│   ├── __init__.py            BridgeApp（继承列表合并各小 Mixin，不变契约）
│   ├── _shared.py             保持（汇聚导入，已收口）
│   ├── handle_event.py        → 主控精简：初始化 + 事件入口 + 核心路由分发
│   ├── routes_*.py            【新】路由矩阵按域分组（bug/signal/report/misc）
│   ├── mention.py             【新】@提及解析（纯函数为主）
│   ├── progress_cards.py      【新】进度卡片管理
│   ├── delivery.py            【新】结果交付
│   ├── result_bug.py          → 拆出 skill_clarify.py / card_actions.py / request_exec.py
│   ├── log_resources.py       → 拆出 resources.py / request_build.py / version_lookup.py
│   ├── context_from.py        → 拆出 followup_clarify.py / existing_answer.py / context_lookup.py
│   └── replay_flow.py         （360 行，不动）
├── agents/
│   ├── addr2line_runner.py    → facade，重导出 addr2line/
│   ├── addr2line/             【新】runner.py / rom_version.py / symbols.py / helpers.py
│   ├── agent_tools.py         → facade，重导出 tools/（cache/files/search/codegraph/bash）
│   ├── bug/                   文件路径全部保留（mock.patch 依赖），大文件内部拆出 *_helpers.py
│   │   └── （每个 >1300 行文件 → 同目录拆出主题子模块，原文件保留主 Mixin + re-export）
│   └── 其余 runner            （<800 行，不动）
├── knowledge/
│   ├── source_investigation.py → facade + 拆 investigation/（probes.py / evidence.py / runner.py）
│   └── service.py             → 拆出 answer_flow.py（答题决策分支）
├── reporting/
│   ├── _assets.py             【新】公共 CSS/HTML 骨架（合并重复副本）
│   └── 各 report_html         引用 _assets，体积显著下降
├── report_server.py           → facade，重导出 report_http/（server.py / routes.py / publisher.py）
└── 其余横切模块               （<800 行，仅做配置化触碰）
```

## 3. 配置收敛目标（Phase 4）

新增 config.toml section（全部可选、默认值=现行硬编码值，行为零变化）：

| 新 section | 收编的硬编码 |
|---|---|
| `[limits]` | agent_runtime request_limit=50 / tool_calls_limit=80 / max_tokens_floor=8192；agent_tools bash_timeout=30、file 256KB/50KB/500行；archive 解压 900/600s；日志窗口 10min/400文件/20000行 |
| `[health]` | watchdog max_idle=3600 / event_lag=300 / disk=90% |
| `[auth]` | PBKDF2 iterations=100000 / session_ttl=86400 / max_sessions=256 |
| `[state]` | max_seen_events=50000 / max_progress_events=200 / lifecycle max_active=500 |
| `[cards]` | progress_preview=4 / note_max=520·700 / error_preview=500·300 / progress_card_ttl=7200 |
| `[parser]`（或 config/parser_terms.toml） | 触发词表外置可覆盖（默认仍内置） |
| `[prompts]` | system_prompt 可指向外部文件覆盖（默认内嵌不变） |

## 4. 不做什么（沿袭蓝图并加严）

- 不改对外行为/触发矩阵/权限语义/卡片交互
- 不改公共类名、公共方法签名、`agents/__init__.py` 的 `__all__`
- 不引入新框架/新依赖（不上 Jinja2——HTML 去重用 Python 常量/函数即可）
- 不删除既有 config 键；不改 CLI 与 run.sh/stop.sh
- 不动 `agents/bug/` 的模块文件名（测试字符串 patch 依赖），只在文件**内部**拆分 + 同目录新增子模块
- 拆分不追求一步到位的"纯组合"：Mixin 细分 + 协作者抽取并行，以测试零破坏为上限
