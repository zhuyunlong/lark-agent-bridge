# S2：parser 子包化

## 动机
`parser.py` 1268 行：15+ 正则、100+ 触发词、12 个公开 parse 函数与 40+ 私有辅助混在一个模块。

## 改动
- 新建 `parsing/` 子包，AST 驱动拆分（tmp/split_parser.py），按调用图分层（已验证无环）：
  - `patterns.py`（145 行）：全部 re.compile 常量 + build_bug_url_re
  - `terms.py`（322 行）：全部触发词/文案常量 —— S21 配置化的落点
  - `textutils.py`（73 行）：通用文本匹配辅助（叶子层）
  - `resources.py`（76 行）：资源/时间范围提取（叶子层）
  - `bug.py`（65）/ `chat.py`（203）/ `addr2line.py`（231）：中间层
  - `signal.py`（118）/ `analysis.py`（318）：顶层
- `parser.py` 缩为 237 行纯 facade，re-export 全部 104 个符号（含私有辅助，测试有直接引用）。

## 关键约束处理
- 无 mock.patch 模块属性引用（已核查）→ 纯 re-export 安全。
- 依赖方向：textutils/resources ← bug/chat/addr2line ← signal/analysis，无循环。

## 踩坑记录
- AST 扫描最初只收 `ast.Assign`，漏掉带注解赋值 `_ASCII_TERM_RE_CACHE: dict = {}`（ast.AnnAssign）→ NameError。教训：**顶层名收集必须同时处理 Assign / AnnAssign**（S1 的正则漏数字、S2 的 AST 漏注解，同类错误，已沉淀到脚本）。
- 可变缓存 dict 必须与使用它的函数同模块（textutils），不能进 terms。

## 验证
- 定向：test_parser + test_app_source_analysis = 72 passed
- 全量：1323 passed, 3 skipped（与基线一致）
