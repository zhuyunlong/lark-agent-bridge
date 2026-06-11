# S16/S20/S21：reporting 去重裁定 + 配置化

## S16 裁定：reporting CSS 不合并（调研结论）
量化结果：三大报告（combined_bug/source_report/app_server）的 CSS 哈希互不相同，
逐行交集仅 8/130 行（最大两两交集 38/130）。结合既有决策"source_analysis 交付报告
不得复用 bug/source_stage 报告外壳"与"输出逐字节不变"约束，合并收益 < 风险，
按蓝图"不为重构而重构"原则裁定不做。reporting 各文件均 <920 行（红线 2000）。

## S20：硬编码收编为配置（默认值 = 历史硬编码值，零配置零行为变化）

| 新配置 | 收编项 | 接线点 |
|---|---|---|
| `[health]` | watchdog 3600 / event_lag 300 / disk 90% | app/handle_event.py 构造 ProcessWatchdog/HealthMonitor |
| `[auth]` | PBKDF2 100000 / TTL 86400 / 上限 256 | AdminAuth 增构造参数；report_server 接线 |
| `[state]` | 去重 50000 / 进度 200 / 生命周期 500 / 进度卡 TTL 7200 | EventStateStore 增参；AgentActivityStore/LifecycleStore/进度卡 TTL 接线 |
| `[ai_provider]` 增键 | request_limit 50 / tool_calls_limit 80 / max_tokens_floor 8192 | agent_runtime 两处 UsageLimits/max_tokens |
| `[bug_analysis]` 增键 | 解压 900s / 解码 600s | archive_extract 四处 timeout |

**顺手修复的正确性陷阱**：原密码哈希格式（`salt:hash`）不内嵌迭代数、verify 永远用常量
——一旦配置不同迭代数，新建用户将永远无法登录。改为 `iterations:salt:hash` 新格式 +
旧两段格式向后兼容校验。

## S21：触发词 TOML 覆盖 + prompt 配置化现状确认
- `parsing/terms.py` 末尾新增 `_apply_term_overrides(globals())`：若存在
  `config/parser_terms.toml`，其中同名 key（list[str]）覆盖内置触发词常量（tuple/set
  自动保型）。文件不存在 → 行为与内置完全一致。已用临时文件实测覆盖生效。
- 系统提示词：核查确认 `claude_agent.system_prompt` / `intent_analysis.system_prompt` /
  `omlx_chat.system_prompt`/`followup_system_prompt` **均已支持 config.toml 覆盖**
  （dataclass 默认值仅作 fallback），无需新机制，记录在案。
- config.example.toml 已补全上述全部新 section/key 的注释文档。

## 验证
全量回归：1323 passed, 3 skipped（与基线一致）
