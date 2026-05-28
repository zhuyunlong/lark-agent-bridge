# Pydantic-AI Agentic Loop 修复计划

> 基于 `2026-05-27-source-code-skill-pydantic-ai-runtime.md` 原始设计文档的对齐修复

**问题：** pydantic-ai runtime 已接入 3 条执行路径，但从未真正完成过一次成功的源码分析。
每次都 fallback 到 file_agent（CLI 子进程）——正是我们要替换的路径。

**根因分析：**

1. **工具名不匹配**：system prompt 写 `read_file, grep, glob, list_dir`，
   实际注册名是 `tool_read_file, tool_grep, tool_glob, tool_list_dir`
   → 模型调用工具时找不到 → `tool_calls=0` → `evidence=[]` → `pydantic_ai_invalid_evidence`
2. **direct_api 静默降级**：`agent_runtime.run()` 内 pydantic-ai 失败后悄悄 fallback 到 direct_api，
   返回 `ok=True` 但 `runtime_path="direct_api"`；外层仍标记 `provider="pydantic_ai"`
3. **工具搜索范围错误**：workspace 指向整个 lark-agent-bridge 目录，不是 source repo roots
4. **设计文档要求的 6 个工具未实现**：`read_bug_context`（函数存在但未注册）、
   `read_skill_context`、`read_report_artifact`、`search_code_index`、`search_codegraph`、`read_prepared_log_metadata`
5. **工具调用无运行时日志**：只在 run_sync 完成后才记录 tool_trace，过程中不可观察
6. **file_agent fallback 无超时上限**：可跑 10+ 分钟，调试期间浪费时间

---

## Phase F1：修复工具注册 + 工具名对齐（核心修复）

### F1.1 工具名对齐
- `agent_tools.py`: 将注册函数名从 `tool_read_file` → `read_file` 等，与 system prompt 一致
- 或改 system prompt 使用实际注册名（选前者，更清晰）

### F1.2 注册缺失的域工具
- `read_bug_context` → 已有函数，注册为 tool
- `read_report_artifact` → 读取已完成的分析报告 JSON/HTML/Markdown
- `read_prepared_log_metadata` → 读取日志元数据摘要

### F1.3 接入 source_investigation 能力
- `search_codegraph` → 通过 `CodeGraphClient.search_symbol()` 实现符号级搜索
- 限制工具搜索根到 `source_investigation.repo_roots` + `add_dirs`

### F1.4 工具调用日志
- 每个工具函数内部加 `logger.info("tool_call: %s(%s)", name, args)` 
- 在 `_run_source_stage_pydantic_ai` 中遍历 `result.tool_trace` 逐条 emit progress

## Phase F2：禁止 direct_api 静默降级

- `agent_runtime.run()` 增加 `strict_tools: bool = False` 参数
- 当 `strict_tools=True` 时，pydantic-ai 失败不 fallback 到 direct_api，直接返回 `ok=False`
- `_run_source_stage_pydantic_ai()` 调用时传 `strict_tools=True`
  （源码分析必须有工具调用才有意义）
- 额外检查：`result.tool_calls == 0` 时返回 `error_code="pydantic_ai_no_tool_calls"`

## Phase F3：file_agent fallback 超时限制

- file_agent fallback 加显式 180s 超时上限
- 超时直接跳出，不影响后续 summary 流程

## Phase F4：群聊验证

- 重启 bot
- 触发源码分析续聊请求
- 监控日志，确认 tool_calls > 0 且无 fallback
- 验证 summary 阶段也走 pydantic-ai

---

## 验证标准

1. pydantic-ai `tool_calls > 0`（agent 真正探索了代码库）
2. 不 fallback 到 file_agent（除非 pydantic-ai 真的失败）
3. 每个工具调用有日志记录
4. 最终报告包含具体的文件路径 + 行号证据
5. 群聊卡片显示 `pydantic_ai` 而非 `本地 Agent(claude)`
