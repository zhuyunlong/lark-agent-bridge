# 自主分析报告 — 形式重构对比

同一份真实自主分析产物（bug SB174577 / P挡SR无法显示 / 2026-04-26 16:07），分别用旧、新渲染器输出，便于对比。

- `old_report.html`：最近一次真实自主分析的旧报告。问题：verdict 横幅恒为绿色；结论摘要用 `<pre>` 套转义文本导致原始 markdown（`##`/`**`/反引号）原样泄漏成等宽乱码；卡片全绿无严重度；正文单薄、真实结构全塞进折叠的"原始 Markdown"。
- `new_report.html`：用新渲染器 `lark_agent_bridge/reporting/app_server_report_html.py`（镜像 `3d-stuck-investigate` 设计系统）重渲染。

## 修复要点
1. **诚实严重度**：本例结论是"不能证明卡死、可信度中低、缺核心日志"→ 横幅判**黄**（旧版恒绿；朴素 fault-first 会因"卡死"二字误判红，已改为 confidence-first）。
2. **结论摘要转可读正文**：无 `<pre>`，无 `##`/`**` 泄漏，列表/加粗/行内代码正确渲染。
3. **卡片按值分色**：命中Skill / 故障时间 / 现场日志 / 关键证据 / 触发词 / Token。
4. **结构化分节 + 归因链路**：源码侧判断、证据缺口渲染为 chain；原始 markdown / 上下文 JSON / Skill 清单收进"📂 展开原始与上下文证据"折叠区。

## 源码改动
- 新增 `lark_agent_bridge/reporting/app_server_report_html.py`
- `lark_agent_bridge/app_server_investigation.py`：`_render_html_report` 改为委托新渲染器（移除孤立的 `import html`、旧渲染导入、`_truncate`；新增 `_detect_selected_skill`）
- 新增 `tests/test_app_server_report_html.py`（9 个测试，全绿；全量套件 1269 通过）
