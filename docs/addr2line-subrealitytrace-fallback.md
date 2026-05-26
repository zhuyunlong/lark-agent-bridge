# subrealitytrace 单文件解析回退

## 背景

当用户只提供单个 `subrealitytrace_*` 文件，且没有 ROM / Napa / APK 版本号时，传统 `addr2line` 流程无法下载外部符号表。

此前这类请求会直接返回“缺少符号表版本”，或者只能输出基于原始文本的粗粒度摘要，无法充分利用 trace 内自带符号和本机已有的 Unity 符号表。

## 当前行为

`lark_agent_bridge/agents/addr2line_runner.py` 在缺少外部版本号时，会先尝试识别 `subrealitytrace` 单文件回退路径。

命中后执行以下步骤：

1. 解析 trace 中 `"线程名" sysTid=...` 的线程块。
2. 识别重点线程：
   - `UnityMain`
   - `JNISurfaceTextu*`
   - 主线程
   - 渲染相关线程
   - `XPD_*`
3. 优先使用 trace 行中已有的函数名。
4. 对 `libunity.so` / `libmain.so` 且 trace 本身没有函数名的地址，使用本机 `llvm-addr2line` + 内置 Unity 符号表补充反解。
5. 如果同目录存在更早的 `subrealitytrace_*` 文件，提取同名重点线程并输出“与上一帧不同”的差异说明。

## 输出规则

群聊回复不再输出整段原始 `#xx pc ...` 文本。

当前输出分为三部分：

1. 场景 / 渲染相关 so 摘要
2. 线程分类统计
3. 重点线程完整帧列表

重点线程输出格式：

- 线程标题：`UnityMain  sysTid=25293  (20:04:15)`
- 帧列表：`#03  WaitVSync  libunity.so`
- 差异说明：`▎ 与上一帧 20:04:00 不同：...`

## 典型收益

在没有外部版本号的前提下，以下内容现在可以直接输出：

- `UnityMain` 的 Unity 主循环相关符号
- `JNISurfaceTextu` 的 Unity 渲染工作线程符号
- trace 自带的 `jit-cache` / `boot-framework.oat` / `libart.so` 符号
- 渲染等待 / VSync 等状态变化的相邻帧比较

## 当前边界

仍然存在以下限制：

1. 只有 `libunity.so` / `libmain.so` 会走本地内置符号表补充反解。
2. 其他 so 仍以 trace 自带符号为主；如果 trace 本身没有符号，则只能显示 `+0x地址`。
3. “与上一帧不同”的比较依赖同目录存在更早的 `subrealitytrace_*` 文件。
4. 这条回退链路是线程态分析，不等价于完整 crash 根因分析。

## 验证

已覆盖以下验证：

- `tests/test_app.py -k "addr2line"`
- `tests/test_validate_refactor_scenarios.py`
- `scripts/validate_refactor_scenarios.py`

真实样例验证文件：

- `/Users/zhuyl/Downloads/subrealitytrace_2026-05-24-20-04-00`
- `/Users/zhuyl/Downloads/subrealitytrace_2026-05-24-20-04-15`
