# 智能日志分析框架

## 概述

智能日志分析框架是 Lark Agent Bridge 的核心组件之一，用于实现基于进程号的日志定位和反推分析，取代了传统的固定时间窗口分析方式。

## 核心优势

| 维度 | 传统方案（固定窗口） | 新方案（智能反推） |
|------|---------------------|-------------------|
| **灵活性** | ❌ 固定 5 分钟窗口 | ✅ 根据问题动态调整 |
| **准确性** | ⚠️ 可能遗漏关键日志 | ✅ 基于进程号精确匹配 |
| **通用性** | ❌ 每个 Skill 单独实现 | ✅ 统一框架，所有 Skill 复用 |
| **可扩展性** | ❌ 新增 Skill 需要重复实现 | ✅ 新增 Skill 只需配置 |
| **维护成本** | ❌ 多处代码重复 | ✅ 集中维护 |

## 架构设计

### 核心组件

```
SmartLogAnalyzer
├── find_target_pid()          # 根据问题时间定位进程号
├── find_all_log_files()       # 基于 PID 找出所有相关日志
├── reverse_trace()            # 从问题时间点反推日志线索
└── identify_problem_type()    # 根据日志内容识别问题类型
```

### 调用链路

```
BugAnalysisRunner.run_bug_analysis()
    ├─ Step 1-6: 原有逻辑（环境检查、数据拉取、分类决策等）
    ├─ Step 7: 【智能日志分析】（新增）
    │      ├─ SmartLogAnalyzer.find_target_pid()
    │      ├─ SmartLogAnalyzer.find_all_log_files()
    │      ├─ SmartLogAnalyzer.reverse_trace()
    │      └─ 返回 {target_pid, log_files, timeline, suggested_skill}
    ├─ Step 8: 构建命令（传入 PID 和日志文件列表）
    ├─ Step 9: 执行分析脚本（接收 PID 和日志文件列表）
    └─ Step 10-11: 生成报告
```

## 配置说明

在 `config.toml` 中添加以下配置：

```toml
[smart_log_analysis]
enabled = true                           # 是否启用智能日志分析
process_name = "com.xiaopeng.montecarlo" # 目标进程名称
time_window_minutes = 1                  # 定位 PID 的时间窗口（分钟）
max_reverse_lines = 1000                 # 反推的最大行数
```

### 配置参数说明

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `enabled` | `true` | 是否启用智能日志分析 |
| `process_name` | `com.xiaopeng.montecarlo` | 目标进程名称 |
| `time_window_minutes` | `1` | 定位 PID 的时间窗口（分钟） |
| `max_reverse_lines` | `1000` | 反推的最大行数 |

## 使用示例

### 1. 自动启用（默认）

智能日志分析默认启用，无需手动调用。当 BugAnalysisRunner 执行分析时，会自动：

1. 根据问题时间定位进程号
2. 基于进程号找出所有相关日志
3. 反推日志线索
4. 将结果传给分析脚本

### 2. 分析脚本接收参数

分析脚本可以通过以下参数接收智能分析结果：

```bash
python extract_scene_signal_events.py \
  --log-path /path/to/logs \
  --target-time "2026-05-25 16:50" \
  --pid 1234 \
  --log-files "/path/to/file1.alog,/path/to/file2.txt"
```

### 3. 向后兼容

如果未传入 `--pid` 参数，分析脚本会回退到传统的固定时间窗口模式：

```bash
python extract_scene_signal_events.py \
  --log-path /path/to/logs \
  --target-time "2026-05-25 16:50" \
  --window-minutes 5
```

## 核心算法

### 1. 进程号定位算法

```python
def find_target_pid(log_dir, target_time):
    """
    1. 找到包含目标时间的日志文件
    2. 在目标时间 ±1 分钟内搜索
    3. 统计 PID 出现频率
    4. 返回出现频率最高的 PID（排除系统进程）
    """
```

### 2. 日志文件查找算法

```python
def find_all_log_files(log_dir, target_pid):
    """
    1. 扫描应用日志: app/com.xiaopeng.montecarlo/main_*.alog
    2. 扫描系统日志: logd/*.txt
    3. 检查每个文件是否包含目标 PID
    4. 返回包含该 PID 的所有文件
    """
```

### 3. 反推日志线索算法

```python
def reverse_trace(log_files, target_time, target_pid):
    """
    1. 从目标时间点开始，向前反推
    2. 提取目标 PID 的所有日志
    3. 识别关键事件和模式
    4. 构建事件时间线
    """
```

### 4. 问题类型识别算法

```python
def identify_problem_type(timeline):
    """
    根据日志内容判断使用哪个 Skill：
    - Watchdog kick/fatal → 3d-stuck-investigate
    - SIGNAL_SR_SCENE_TYPE → scene-signal-diagnosis
    - displayChanged → unity-startup-lifecycle-check
    - VHALHelper → perception-data-summary
    """
```

## 日志格式规范

### 目录结构

```
{bug_cache_dir}/logs/
├── log0/                              # 最新日志
│   ├── app/
│   │   └── com.xiaopeng.montecarlo/  # 3D 应用日志
│   │       ├── main_2026-05-25_16-00.alog
│   │       ├── main_2026-05-25_17-00.alog
│   │       └── ...
│   ├── logd/                          # 系统日志
│   │   ├── main.txt
│   │   ├── events.txt
│   │   └── kernel.txt
│   └── prop.txt                       # 系统属性
├── log1/                              # 次新日志
└── log2/                              # 更早日志
```

### 文件命名规则

- **应用日志**：`main_{yyyy-MM-dd}_{HH}-00.{alog|xlog}`
  - 每小时一个文件
  - 分钟固定为 00
  - `.alog` 和 `.xlog` 都是 MARS 二进制格式

### 日志行格式

```
05-25 16:50:41.123  1234  5678 I TAG: message
                 ↑    ↑    ↑
              时间戳  PID  TID
```

- **时间戳**：`MM-DD HH:MM:SS.mmm`
- **PID**：进程号
- **TID**：线程号
- **日志级别**：D/I/W/E
- **TAG**：日志标签
- **消息**：日志内容

## 问题类型识别关键词

### 3D 卡顿

- `Watchdog kick`
- `Watchdog fatal`
- `UnityRequest count`
- `UnityMain thread block`
- `kill self for unity start dead`

### 场景信号

- `SIGNAL_SR_SCENE_TYPE`
- `OnSceneChanged`
- `收到场景变化`
- `SIGNAL_CUSTOM_GEAR_ST`
- `SIGNAL_CUSTOM_PK_HMI_MODE`

### 启动问题

- `displayChanged`
- `startRender`
- `stopRender`
- `surfaceCreated`
- `surfaceDestroyed`
- `UnityReady`

### 感知数据

- `VHALHelper`
- `MapDataHandler`
- `X3DCB`
- `XDataNativeProxy`

### XTheme

- `105004`
- `105009`
- `XuiConditionHelper`

## 测试

运行单元测试：

```bash
.venv/bin/python -m pytest tests/test_log_analyzer.py -v
```

## 文件清单

```
lark_agent_bridge/
├── log_analyzer.py              # SmartLogAnalyzer 核心类
├── agents/
│   └── bug_runner.py            # BugAnalysisRunner（集成 SmartLogAnalyzer）
└── ...

tests/
└── test_log_analyzer.py         # 单元测试

config.toml                      # 配置文件（新增 smart_log_analysis 段）
```

## 后续优化

1. **集成 log-decoder**：支持 .alog/.xlog 二进制格式解码
2. **优化 PID 定位算法**：使用更精确的进程识别策略
3. **支持多进程分析**：同时分析多个相关进程
4. **增加缓存机制**：缓存已分析的日志文件，避免重复扫描
5. **支持自定义关键词**：允许用户配置问题类型识别的关键词
