# S15/S17/S18：knowledge / report_server / cards 拆分

> 注：因 API 限流（429/529）两次中断，S15、S18 由主会话直接完成，S17 由子 agent 完成
> （agent 在报告阶段断线，但工作已完成并经主会话验收）。S16 见后续步骤文档。

## S18 cards.py：970 → 105 facade（commit e18bab9）
`cards_ui/`：texts(46) + elements(83) + textutils(209) + choice_blocks(64) + status_card(246) + result_card(353) + serialize(32)。
choice_blocks 被 status/followup 两类卡片共用故独立成模块。texts.py 集中状态标签/文案常量（S20 配置化落点）。

## S17 report_server.py：1537 → 442 facade（commit 1207768）
`report_http/`：request_handler(371) + admin_api(218) + index_page(234) + http_io(160) +
skills_knowledge_api(129) + history(99) + knowledge_export(39) + paths(33) + query(18)。
AST 对照：HEAD 36 个顶层符号全部可达。

## S15 knowledge（本次提交）
- source_investigation.py 1738 → 784 facade（类整体保留），`investigation/` 子包：
  investigation_types(30) + warmup_state(85) + prompt_lines(213) + path_utils(113) + local_probe(332) + result_parse(315)
- service.py 1136 → 399 facade（KnowledgeService 保留），`answers/` 子包：
  answer_simulation(347) + answer_signal(120) + answer_command(67) + answer_lowconf(210) + answer_source(164)

### patch 契约处理
- `patch("...source_investigation._try_repo_warmup_lock")`：调用方 `_warmup_one_repo` 是类方法留在 facade，
  facade re-export 绑定被 patch 替换后类方法经 facade globals 解析 → 语义不变（已测）。
- `subprocess.run`/`threading.Thread` patch 的是全局单例模块属性，位置无关。
- `_CODEGRAPH_WARMUP_LOCK/_INFLIGHT` 可变状态仅类方法使用，保留 facade。

### 本步踩坑（沉淀到 tmp/split_module.py）
1. **环检测**：脚本写文件前断言"移出函数不得引用 facade 专属名"，本步拦下 3 处真环
   （prompt_lines⇄local_probe、command⇄lowconf、simulation⇄source），分别用"下沉公共底座
   （path_utils）/ 上移编排函数（_build_command_hit_result）/ 移动常量（_DERIVED_SOURCE_ID）"解开。
2. **函数体内相对 import**：`from .code_index import ...` 在文件下移一层后指向错误且被
   `except ImportError` 静默吞掉（测试才暴露）。拆分后必须 grep `^\s+from \.` 检查缩进相对导入。

## 验证
全量回归：1323 passed, 3 skipped（与基线一致）→ 兼做 S19 里程碑回归 4
