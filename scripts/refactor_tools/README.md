# 重构拆分工具（2026-06 架构重构沉淀）

来源与用法详见 `docs/refactor/`（计划 03、各步骤 steps/）。

- `split_mixin.py`：把一个大 Mixin 类按主题拆成同目录多个小 Mixin 文件，
  原文件保留核心方法并自动改为继承新 Mixin。`self` 命名空间与 MRO 行为不变。
  用法：`python3.11 split_mixin.py <spec.py>`，spec 格式见 `example_spec_mixin.py`。
- `split_module.py`：把模块顶层 函数/类/常量 按组移到子包，未分组的留在 facade，
  facade 显式 re-export。内置循环依赖检测（写文件前断言）。
  用法：`python3.11 split_module.py <spec.py>`，spec 格式见 `example_spec_module.py`。

## 踩坑清单（工具已内置防护，新场景注意）
1. 顶层名收集必须同时处理 `ast.Assign` 与 `ast.AnnAssign`（带类型注解的赋值）。
2. 测试以 `mock.patch("模块.符号")` / `patch.object(module, "符号")` 字符串 patch 的符号：
   其**调用方**必须留在原模块（facade），否则 patch 失效。拆前先 grep tests/。
3. 函数体内的相对 import（`from .x import y`）随文件下移层级后会指向错误，
   且常被 `except ImportError` 静默吞掉——拆后必须 `grep '^\s\+from \.'` 检查。
4. 并行拆分同目录文件时，新文件名必须事先全局分配，避免主题命名撞车。
5. 循环依赖解法三板斧：下沉公共底座 / 上移编排函数 / 移动常量归属。
