# S1：addr2line 子包化

## 动机
`agents/addr2line_runner.py` 1851 行，单类 80 个方法，主题混杂（外部脚本执行 / 集成符号解析 / 请求准备 / subrealitytrace 线程报告）。

## 改动
- 新建 `agents/addr2line/` 子包：
  - `common.py`（120 行）：11 个模块级常量/正则 + 7 个 dataclass
  - `request_prepare.py`（482 行）：`_RequestPrepareMixin` —— 资源下载、栈/prop 选择、时间解析
  - `symbols.py`（527 行）：`_SymbolResolveMixin` —— 集成符号解析、符号源选择/下载
  - `trace_report.py`（554 行）：`_TraceReportMixin` —— subrealitytrace 解析与渲染
- `agents/addr2line_runner.py` 缩为 311 行 facade：`Addr2LineRunner(三 Mixin)` + 外部脚本执行主干 + 公共出入口方法；常量从 common re-export 保持兼容。

## 关键约束处理
- `run_tracked_process` 被测试以 `mock.patch.object(addr2line_runner_module, ...)` 模块属性 patch，其唯一调用方 `_run_external_resolve` 保留在 facade 模块 → patch 语义不变。
- `mock.patch.object(runner, "_method")` 实例 patch 对 Mixin 布局不敏感，天然兼容。

## 踩坑记录
- 提取常量名时正则 `_[A-Z_]*` 不匹配带数字的 `_NAPA5_SYMBOL_SO_NAMES`，导致 import 名单缺一项（NameError）。教训：**机械拆分后必跑定向测试**；列名应取自 AST/逐行扫描而非手写正则。

## 验证
- 定向：test_app_addr2line_runner + test_internal_network_env + test_app_followup_reply = 67 passed
- 全量：1323 passed, 3 skipped（与基线一致）
