# S10–S13：agents/bug 大文件拆分 + agent_tools 子包化（并行执行）

4 个子任务并行委派执行，方法论同 S5–S8（tmp/split_mixin.py + spec），patch 安全规则：
测试以模块属性引用的名字（`archive_extract._run_tracked_process`/`shutil`、
`custom_skill.CodexAppServerRuntime`/`check_codex_app_server_available`），其调用方法保留在原模块。

## S10（bug_prompt / general_summary / agent_summary）
- bug_prompt.py 1560→251：skill_report_render(493) + summary_backend(425) + prompt_snapshot(176) + summary_context(206)
- general_summary.py 1442→544：combined_report(387) + signal_report(506)
- agent_summary.py 1376→281：summary_stream(289) + summary_usage(158) + runtime_metadata(286) + source_evidence(369)

## S11（ld_executor / custom_skill / signal_android / direct_api）
- ld_executor.py 1459→539：ld_log_evidence(351) + ld_pydantic_runs(573)
- custom_skill.py 1418→566：log_focus(389) + agent_command(256) + agent_output(220)；codex app-server 调用留 facade（patch 约束）
- signal_android.py 1402→221：signal_data_link(264) + signal_keywords(321) + signal_kotlin_source(194) + signal_stage_view(373)
- direct_api.py 1350→536：summary_evidence(222) + api_prompt_snapshot(195) + context_excerpt(400)

## S12（resolve_source / archive_extract / bug_cache / run_reanalysis / run_primary）
- resolve_source.py 1414→538：followup_route(272) + decision_agent(241) + clarify_confirm(342)
- archive_extract.py 1403→301：fault_time(472) + log_input_select(340) + bug_outputs(257)；7 个引用被 patch 名字的方法留 facade（AST 验证）
- bug_cache.py 1400→646：plan_command(313) + reanalysis_plans(100) + cache_store(328)
- run_reanalysis.py 1080→905 ⚠️：仅拆出 agent_followup(184)；`run_bug_reanalysis` 是单个 897 行方法
- run_primary.py 1034 行未拆 ❌：单方法 `run_bug_analysis`（1027 行）

## S13（agent_tools → agents/tools/）
- agent_tools.py 1319→95 facade：cache(85) + file_access(224) + search(285) + shell(130) + register(633)
- 44 个原顶层符号全部可达（AST 对照验证）

## 事故记录（重要教训）
并行拆分时 S11 的 direct_api 与 S10 的 bug_prompt 各自生成了同名 `prompt_snapshot.py`，后者被覆盖，
14 个测试失败。S11 agent 依据 spec + git HEAD 重新生成了 S10 的文件并把自己的改名为
`api_prompt_snapshot.py`，集合校验方法不重不漏后恢复绿色。
**教训：并行拆分同目录文件时，新文件名必须事先全局分配，避免主题命名撞车。**

## 遗留技术债（转入后续步骤或记录）
- `run_primary.run_bug_analysis`（1027 行单方法）与 `run_reanalysis.run_bug_reanalysis`（897 行单方法）：
  方法体肢解属行为风险改造，本轮按蓝图"不为重构而重构"原则保留，文件仍 <2000 红线。
- register.py 中 codegraph 闭包工具（共享缓存/进度状态）保留闭包结构。

## 验证
全量回归：1323 passed, 3 skipped（与基线一致）→ 兼做 S14 里程碑回归 3
