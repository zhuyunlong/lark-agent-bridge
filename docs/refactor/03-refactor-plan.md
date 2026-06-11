# 分步重构计划（2026-06-11）

> 每步 = 独立 commit = 可单独回滚。每步完成后：
> 1) 跑该步定向测试（见各步"验证"列）；2) 在 docs/refactor/steps/ 写步骤记录；3) commit。
> 里程碑（M 标记）处跑全量 `python3.11 -m pytest tests/ -q` 与基线对比。
> 基线：tmp/baseline-pytest.log（重构前全量结果）。

| # | 步骤 | 范围 | 风险 | 验证 |
|---|---|---|---|---|
| S1 | addr2line 子包化 | agents/addr2line_runner.py(1851) → agents/addr2line/ | 低 | test_app_addr2line_runner |
| S2 | parser 子包化 | parser.py(1268) → parsing/（正则/触发词/各域 parse） | 低 | test_parser + test_app_bug_request |
| S3 | config 子包化 | config.py(1039) → configuration/（loader/sections/presets/paths） | 中 | test_config + test_cli |
| S4 M | 里程碑回归 1 | — | — | 全量 pytest 对比基线 |
| S5 | app 主控拆分 | handle_event.py(1813) → mention/progress_cards/delivery/routes_* 小 Mixin | 中高 | test_app_bug_request + test_app_group_bug + test_app_followup_reply |
| S6 | app 结果域拆分 | result_bug.py(1669) → skill_clarify/card_actions/request_exec | 中 | test_app_bug_followup + test_app_direct_analysis |
| S7 | app 资源域拆分 | log_resources.py(1513) → resources/request_build/version_lookup | 中 | test_app_direct_analysis + test_app_bug_request |
| S8 | app 上下文域拆分 | context_from.py(1351) → followup_clarify/existing_answer/context_lookup | 中 | test_app_followup_reply + test_app_bug_followup |
| S9 M | 里程碑回归 2 | — | — | 全量 pytest 对比基线 |
| S10 | agents/bug 大文件拆分 ①  | bug_prompt(1560)/general_summary(1442)/agent_summary(1376) 内部拆主题子模块 | 中 | test_agents_bug_analysis(+_2) |
| S11 | agents/bug 大文件拆分 ② | ld_executor(1459)/custom_skill(1418)/signal_android(1402)/direct_api(1350) | 中 | test_agents_custom_skill + test_agents_bug_agent(+_2) |
| S12 | agents/bug 大文件拆分 ③ | resolve_source(1414)/archive_extract(1403)/bug_cache(1400)/run_reanalysis(1080)/run_primary(1034) | 中 | test_agents_bug_analysis + test_agents_bug_reanalysis(+_2) |
| S13 | agent_tools 子包化 | agents/agent_tools.py(1319) → agents/tools/ | 低 | test_agent_runtime |
| S14 M | 里程碑回归 3 | — | — | 全量 pytest 对比基线 |
| S15 | knowledge 拆分 | source_investigation.py(1738) → investigation/；service.py(1136) 拆 answer_flow | 中 | test_knowledge_source_investigation + test_knowledge_warmup_codegraph |
| S16 | reporting 去重 | 公共 CSS/HTML → _assets.py；3 副本合一 | 低 | test_*_report_html（5 个） |
| S17 | report_server 子包化 | report_server.py(1537) → report_http/ | 中 | test_report_server |
| S18 | cards 子包化 | cards.py(970) → cards_ui/（elements/status/result/texts） | 低 | test_cards |
| S19 M | 里程碑回归 4 | — | — | 全量 pytest 对比基线 |
| S20 | 配置化 ①：limits/health/auth/state/cards section | 新增 Options + 接线（默认值=现行硬编码） | 中 | test_config + 各触点模块定向测试 |
| S21 | 配置化 ②：触发词表 TOML 覆盖 + prompt 外置覆盖 | parsing/terms + models prompt 加载 | 中 | test_parser + test_config + test_agents_intent_analysis |
| S22 | 行数红线收紧 | test_file_size_boundaries：2000 → 1000（若全部文件达标） | 低 | 该测试本身 |
| S23 M | 终验 | 全量回归 + FINAL-SUMMARY.md + 更新 architecture 文档 | — | 全量 pytest 对比基线 |

## 拆分操作规范（每步共用）

1. **纯函数/常量优先**：先把无 `self` 依赖的函数/常量搬到新子模块，原文件 `from .new import *`（或显式名单）re-export。
2. **Mixin 细分**：大 Mixin 按主题切成多个小 Mixin（同子包新文件），原文件保留壳：
   `class _XxxMixin(_ThemeAMixin, _ThemeBMixin): pass` —— `self` 命名空间与 MRO 行为不变。
3. **patch 路径守护**：拆分前 grep tests/ 中对该模块的 `mock.patch` / `monkeypatch` 字符串；被 patch 符号必须保留在原模块命名空间（re-export 满足 `patch("module.attr")` 语义——patch 作用于模块属性查找）。
   ⚠️ 注意：若被测代码在**函数内部**重新 `from x import y`，re-export 后 patch 仍有效；但若拆分后被测代码改为从新模块直接 import 符号，原模块 patch 会失效 → 拆分时**被测调用方的 import 来源保持原模块**或同步改测试（优先前者）。
4. 每步跑定向测试；任何新增失败立即修复或回滚，不带伤推进。
5. 步骤记录写入 docs/refactor/steps/SNN-名称.md：动机/改动清单/测试结果/遗留项。

## 回滚策略

- 单步回滚：`git revert <commit>` 或 `git reset --hard HEAD~1`（未推送）。
- 里程碑回归发现跨步问题：二分定位步骤 commit。
