# Log-Focus 真裁剪 + 探索约束强化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 app_server 自主分析的 `log_focus/` 从「整文件复制」改为「按时间窗 + 单文件行数上限 + 主日志 PID 连续性裁行」，并纳入 `logd/main.txt`，同时强化 prompt 约束，杜绝 codex 单 turn 上下文滚到 260 万 token 触发 600s 超时。

**Architecture:** 改动集中在 `lark_agent_bridge/agents/bug/custom_skill.py`（`_CustomSkillMixin`）。新增纯函数式裁行 helper（不依赖 self 状态，便于单测），复用已存在的 `_parse_log_line_datetime`（archive_extract.py:1232，其第三分支已支持无年份 `MM-DD HH:MM:SS` logcat 格式）和 `_is_montecarlo_or_logd_path`（ld_executor.py:1378）。`_build_file_agent_focus_dir` 的 `shutil.copy2` 替换为裁行写入。`_file_agent_focus_candidates` 放宽以纳入 logd 主日志。prompt 规则方法追加约束。

**Tech Stack:** Python 3.13, pytest, 标准库（datetime/re/collections）。日志为 Android logcat 格式：`MM-DD HH:MM:SS.mmm PID TID ... tag: msg`。

**关键语义（必须精确实现，勿自由发挥）：**
- 「接近问题时间点优先」= 单文件超上限时，保留 **`[…, 故障时刻]` 的末段最后 15000 行**（丢更早的，保留贴近故障的），**不是**文件开头 15000 行。
- 故障时间是门槛：裁剪窗口 = `[故障 − 回看窗, 故障 + 后向buffer]`。故障之后只留少量 buffer 供了解后续表现，**不作为根因/源码归因证据**（这点同时落到 prompt）。
- 「1 小时不足就往前找」= 窗内行数 < 下限时自动扩窗，但有止损：遇 PID 跳变即停、达最大回看即停。
- 「进程号一致」仅作用于 montecarlo/logd 主日志；其余文件只按时间窗裁。

**固定参数（写死为模块级常量）：**
| 常量 | 值 | 含义 |
|---|---|---|
| `_FOCUS_LOOKBACK_SECONDS` | `3600` | 默认回看窗：故障前 1h |
| `_FOCUS_FORWARD_BUFFER_SECONDS` | `300` | 后向 buffer：故障后 +5min |
| `_FOCUS_MAX_LINES_PER_FILE` | `15000` | 单文件行数上限 |
| `_FOCUS_MIN_LINES_PER_FILE` | `2000` | 行数下限，低于触发扩窗 |
| `_FOCUS_MAX_LOOKBACK_SECONDS` | `21600` | 最大回看 6h（扩窗止损） |
| `_FOCUS_EXPAND_STEP_SECONDS` | `3600` | 扩窗步长：每次前推 1h |

---

## File Structure

- **Modify** `lark_agent_bridge/agents/bug/custom_skill.py`:
  - 新增模块级常量（6 个，见上表）
  - 新增 `_detect_dominant_pid(lines, fault_dt, reference_year)` — 故障时刻附近最高频 PID
  - 新增 `_clip_log_lines(...)` — 单文件裁行核心（时间窗 + 上限末段 + 扩窗 + PID 连续性）
  - 改 `_file_agent_focus_candidates`（custom_skill.py:221-261）— 纳入 logd 主日志
  - 改 `_build_file_agent_focus_dir`（custom_skill.py:263-311）— copy2 → 裁行写入
  - 改 `_file_agent_log_rules` / `_file_agent_search_budget_rules` — 追加探索约束
- **Create** `tests/test_log_focus_clip.py` — 裁行/扩窗/PID/纳入 logd 单测
- **Modify** `tests/test_app_server_investigation_runner.py` 或新增 — prompt 含新约束断言

复用（勿重复实现）：
- `self._parse_log_line_datetime(line, reference_year=...)`（archive_extract.py:1232）解析行内时间戳，第三分支处理无年份 `MM-DD HH:MM:SS`。
- `self._parse_bug_datetime(fault_time)`（archive_extract.py:1270）→ `datetime`。
- `self._is_montecarlo_or_logd_path(path)`（ld_executor.py:1378）判主日志。
- `self._iter_log_coverage_files`、`self._parse_log_file_datetime`、`self._log_file_priority`（archive_extract.py）。

---

### Task 1: PID 检测 helper `_detect_dominant_pid`

**Files:**
- Modify: `lark_agent_bridge/agents/bug/custom_skill.py`（在 `_file_agent_focus_candidates` 之前插入常量与方法）
- Test: `tests/test_log_focus_clip.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_log_focus_clip.py
from datetime import datetime
from pathlib import Path

from lark_agent_bridge.agents.bug.custom_skill import _CustomSkillMixin


class _Mixin(_CustomSkillMixin):
    """裸 mixin 实例，仅测纯裁行逻辑，不触发 __init__。"""


def _mk():
    return _Mixin.__new__(_Mixin)


def test_detect_dominant_pid_picks_highest_freq_near_fault():
    fault = datetime(2026, 5, 25, 16, 50, 41)
    lines = [
        "05-25 16:50:40.100 2488 3081 I A: x",
        "05-25 16:50:41.055 2488 7459 I B: y",
        "05-25 16:50:41.900 2488 3081 I C: z",
        "05-25 16:36:58.479 11619 12337 I D: old",  # 远离故障的旧进程，应被忽略
    ]
    pid = _mk()._detect_dominant_pid(lines, fault, reference_year=2026)
    assert pid == "2488"


def test_detect_dominant_pid_returns_none_without_fault():
    pid = _mk()._detect_dominant_pid(["05-25 16:50:41.055 2488 7459 I B: y"], None, reference_year=2026)
    assert pid is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_log_focus_clip.py -k detect_dominant_pid -v`
Expected: FAIL，`AttributeError: '_Mixin' object has no attribute '_detect_dominant_pid'`

- [ ] **Step 3: 实现常量 + `_detect_dominant_pid`**

在 `custom_skill.py` 中 `_file_agent_focus_candidates` 方法定义之前插入：

```python
    # --- Log focus 裁行参数 ---
_FOCUS_LOOKBACK_SECONDS = 3600
_FOCUS_FORWARD_BUFFER_SECONDS = 300
_FOCUS_MAX_LINES_PER_FILE = 15000
_FOCUS_MIN_LINES_PER_FILE = 2000
_FOCUS_MAX_LOOKBACK_SECONDS = 21600
_FOCUS_EXPAND_STEP_SECONDS = 3600
```

注意：模块级常量放在 `class _CustomSkillMixin:` 之外（文件顶部 `from ._shared import *` 之后）。在类内新增方法：

```python
    def _detect_dominant_pid(
        self,
        lines: list[str],
        fault_dt: "datetime | None",
        *,
        reference_year: int,
        window_seconds: int = 120,
    ) -> "str | None":
        """故障时刻 ±window_seconds 内出现最频繁的 PID（时间戳后第一个数字）。"""
        if fault_dt is None:
            return None
        counts: dict[str, int] = {}
        for line in lines:
            line_dt = self._parse_log_line_datetime(line, reference_year=reference_year)
            if line_dt is None:
                continue
            if abs((line_dt - fault_dt).total_seconds()) > window_seconds:
                continue
            match = re.search(r"\d{2}:\d{2}:\d{2}(?:\.\d+)?\s+(\d+)\s+\d+", line)
            if not match:
                continue
            pid = match.group(1)
            counts[pid] = counts.get(pid, 0) + 1
        if not counts:
            return None
        return max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_log_focus_clip.py -k detect_dominant_pid -v`
Expected: PASS（2 passed）

- [ ] **Step 5: 提交**

```bash
git add tests/test_log_focus_clip.py lark_agent_bridge/agents/bug/custom_skill.py
git commit -m "feat(log-focus): add _detect_dominant_pid for main-log PID continuity"
```

---

### Task 2: 核心裁行 `_clip_log_lines`

裁行算法（一个文件的 `list[str]` → 裁剪后的 `list[str]`）：

1. 解析每行时间戳（`_parse_log_line_datetime`，`reference_year=fault_dt.year`）。无法解析时间的行（如 `~~~~~ begin of mmap ~~~~~`）跳过参与时间判断，但若落在保留区间的相邻位置则随邻近行保留——**简化实现**：只对能解析时间的行做窗口判断，不可解析行不纳入输出（日志正文行基本都带时间戳，足够）。
2. 上边界 = `fault_dt + _FOCUS_FORWARD_BUFFER_SECONDS`；下边界 = `fault_dt - lookback`（初始 `lookback=_FOCUS_LOOKBACK_SECONDS`）。
3. 选出 `[下边界, 上边界]` 窗内行。
4. **扩窗**：窗内行数 < `_FOCUS_MIN_LINES_PER_FILE` 且 `lookback < _FOCUS_MAX_LOOKBACK_SECONDS` 时，`lookback += _FOCUS_EXPAND_STEP_SECONDS` 重选；若是主日志且新纳入行触及与目标 PID 不同的 PID（跳变），停止扩窗（不跨进程重启取证）。
5. **PID 连续性**（仅主日志，`is_main_log=True`）：目标 PID 由 `_detect_dominant_pid` 给出；只保留目标 PID 的行（无法解析 PID 的行保留，避免误删表头/续行）。
6. **上限末段**：行数 > `_FOCUS_MAX_LINES_PER_FILE` 时，保留最后 `_FOCUS_MAX_LINES_PER_FILE` 行（贴近故障的末段）。

**Files:**
- Modify: `lark_agent_bridge/agents/bug/custom_skill.py`
- Test: `tests/test_log_focus_clip.py`

- [ ] **Step 1: 写失败测试**

```python
def test_clip_keeps_window_and_drops_after_fault_beyond_buffer():
    m = _mk()
    fault = datetime(2026, 5, 25, 16, 50, 41)
    lines = [
        "05-25 16:10:00.000 2488 1 I A: too-old",          # 故障前 40min，默认 1h 窗内
        "05-25 16:50:40.000 2488 1 I B: just-before",
        "05-25 16:50:41.000 2488 1 I C: at-fault",
        "05-25 16:52:00.000 2488 1 I D: within-buffer",    # +79s，5min buffer 内，保留
        "05-25 17:10:00.000 2488 1 I E: far-after",        # +20min，超 buffer，丢弃
    ]
    out = m._clip_log_lines(lines, fault, is_main_log=False)
    joined = "\n".join(out)
    assert "just-before" in joined and "at-fault" in joined and "within-buffer" in joined
    assert "far-after" not in joined


def test_clip_caps_to_max_lines_keeping_tail_near_fault():
    m = _mk()
    fault = datetime(2026, 5, 25, 16, 50, 41)
    # 18000 行，全部在故障前 30 分钟内（窗内），递增秒数贴近故障
    lines = []
    base = datetime(2026, 5, 25, 16, 20, 0)
    for i in range(18000):
        ts = base.timestamp() + i * 0.1
        from datetime import datetime as _dt
        d = _dt.fromtimestamp(ts)
        lines.append(f"05-25 {d:%H:%M:%S}.000 2488 1 I L: line{i}")
    out = m._clip_log_lines(lines, fault, is_main_log=False)
    assert len(out) == 15000
    # 保留的是末段（贴近故障），line17999 在，line0 不在
    assert "line17999" in out[-1]
    assert not any("line0 " in x or x.endswith("line0") for x in out[:5])


def test_clip_main_log_keeps_only_dominant_pid():
    m = _mk()
    fault = datetime(2026, 5, 25, 16, 50, 41)
    lines = [
        "05-25 16:50:40.000 2488 1 I A: keep1",
        "05-25 16:50:41.000 2488 1 I B: keep2",
        "05-25 16:50:41.500 9999 1 I C: drop-other-pid",
    ]
    out = m._clip_log_lines(lines, fault, is_main_log=True)
    joined = "\n".join(out)
    assert "keep1" in joined and "keep2" in joined
    assert "drop-other-pid" not in joined


def test_clip_expands_window_when_too_few_lines():
    m = _mk()
    fault = datetime(2026, 5, 25, 16, 50, 41)
    # 默认 1h 窗内只有 1 行（16:30），更早 2h 处有大量行 -> 触发扩窗纳入
    lines = ["05-25 16:30:00.000 2488 1 I A: in-1h-window"]
    for i in range(2500):
        lines.insert(0, f"05-25 14:55:{i%60:02d}.000 2488 1 I B: older{i}")  # 故障前~1h55m
    out = m._clip_log_lines(lines, fault, is_main_log=False)
    assert len(out) >= 2000  # 扩窗后达到下限
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_log_focus_clip.py -k clip -v`
Expected: FAIL，`AttributeError: ... has no attribute '_clip_log_lines'`

- [ ] **Step 3: 实现 `_clip_log_lines`**

在 `_detect_dominant_pid` 之后插入：

```python
    def _clip_log_lines(
        self,
        lines: list[str],
        fault_dt: "datetime | None",
        *,
        is_main_log: bool,
    ) -> list[str]:
        """按时间窗 + 单文件上限末段 + （主日志）PID 连续性裁行。

        fault_dt 为 None 时退化为「保留末段最多 _FOCUS_MAX_LINES_PER_FILE 行」。
        """
        if fault_dt is None:
            return lines[-_FOCUS_MAX_LINES_PER_FILE:]
        ref_year = fault_dt.year
        upper = fault_dt.timestamp() + _FOCUS_FORWARD_BUFFER_SECONDS

        # 预解析每行时间与 PID，避免重复解析
        parsed: list[tuple[str, "float | None", "str | None"]] = []
        for line in lines:
            ldt = self._parse_log_line_datetime(line, reference_year=ref_year)
            ts = ldt.timestamp() if ldt is not None else None
            pid = None
            pm = re.search(r"\d{2}:\d{2}:\d{2}(?:\.\d+)?\s+(\d+)\s+\d+", line)
            if pm:
                pid = pm.group(1)
            parsed.append((line, ts, pid))

        target_pid = (
            self._detect_dominant_pid(lines, fault_dt, reference_year=ref_year)
            if is_main_log
            else None
        )

        def _window(lookback: int) -> list[tuple[str, "float | None", "str | None"]]:
            lower = fault_dt.timestamp() - lookback
            sel = []
            for item in parsed:
                _, ts, pid = item
                if ts is None:
                    continue
                if ts < lower or ts > upper:
                    continue
                if target_pid is not None and pid is not None and pid != target_pid:
                    continue
                sel.append(item)
            return sel

        lookback = _FOCUS_LOOKBACK_SECONDS
        selected = _window(lookback)
        while (
            len(selected) < _FOCUS_MIN_LINES_PER_FILE
            and lookback < _FOCUS_MAX_LOOKBACK_SECONDS
        ):
            lookback = min(lookback + _FOCUS_EXPAND_STEP_SECONDS, _FOCUS_MAX_LOOKBACK_SECONDS)
            selected = _window(lookback)

        out = [item[0] for item in selected]
        if len(out) > _FOCUS_MAX_LINES_PER_FILE:
            out = out[-_FOCUS_MAX_LINES_PER_FILE:]
        return out
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_log_focus_clip.py -k clip -v`
Expected: PASS（4 passed）

- [ ] **Step 5: 提交**

```bash
git add tests/test_log_focus_clip.py lark_agent_bridge/agents/bug/custom_skill.py
git commit -m "feat(log-focus): add _clip_log_lines (time-window + max-tail + PID continuity)"
```

---

### Task 3: `_file_agent_focus_candidates` 纳入 logd 主日志

**现状（custom_skill.py:221-261）**：按文件名时间戳 ±3600s 挑文件；`logd/main.txt` 文件名无时间戳 → `_parse_log_file_datetime` 返回 None → 第 243-244 行 `continue` 跳过，永远进不来。这是 codex 最后回读原始 24MB logd 全量的根因。

**改法**：主日志（`_is_montecarlo_or_logd_path` 为真）即便文件名无时间戳也纳入候选。

**Files:**
- Modify: `lark_agent_bridge/agents/bug/custom_skill.py:238-248`
- Test: `tests/test_log_focus_clip.py`

- [ ] **Step 1: 写失败测试**

```python
def test_focus_candidates_includes_logd_main_txt(tmp_path):
    m = _mk()
    logs = tmp_path / "logs"
    (logs / "logd").mkdir(parents=True)
    main_txt = logs / "logd" / "main.txt"
    main_txt.write_text("05-25 16:50:41.000 2488 1 I A: x\n", encoding="utf-8")
    # 一个带时间戳的普通 app 日志，确保正常路径仍工作
    app = logs / "app" / "com.x"
    app.mkdir(parents=True)
    (app / "user0_main_2026-05-25_16-00.alog.log").write_text(
        "05-25 16:50:41.000 2488 1 I B: y\n", encoding="utf-8"
    )
    cands = m._file_agent_focus_candidates(
        input_path=logs, fault_time="2026-05-25 16:50:41", analysis_kind="app_server_investigation"
    )
    names = {p.name for p in cands}
    assert "main.txt" in names
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_log_focus_clip.py -k focus_candidates_includes_logd -v`
Expected: FAIL，`assert 'main.txt' in names`（main.txt 被跳过）

- [ ] **Step 3: 修改候选挑选**

把 custom_skill.py 第 238-248 行的循环（当前为）：

```python
        for path in all_files:
            if path.name in {"prop.txt", "dfx.txt"}:
                decoded_aux.append(path)
                continue
            file_dt = self._parse_log_file_datetime(path.name)
            if fault_dt is None or file_dt is None:
                continue
            candidate_dt = datetime.fromtimestamp(time.mktime(file_dt))
            if abs((candidate_dt - fault_dt).total_seconds()) > 3600:
                continue
            selected.append(path)
```

改为（新增主日志无时间戳直纳分支）：

```python
        for path in all_files:
            if path.name in {"prop.txt", "dfx.txt"}:
                decoded_aux.append(path)
                continue
            file_dt = self._parse_log_file_datetime(path.name)
            if file_dt is None:
                # 主日志（logd/montecarlo）文件名常无时间戳；裁行阶段会按故障时间窗收窄，故此处直接纳入
                if self._is_montecarlo_or_logd_path(path):
                    selected.append(path)
                continue
            if fault_dt is None:
                continue
            candidate_dt = datetime.fromtimestamp(time.mktime(file_dt))
            if abs((candidate_dt - fault_dt).total_seconds()) > 3600:
                continue
            selected.append(path)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_log_focus_clip.py -k focus_candidates_includes_logd -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add tests/test_log_focus_clip.py lark_agent_bridge/agents/bug/custom_skill.py
git commit -m "feat(log-focus): include logd/montecarlo main logs without filename timestamp"
```

---

### Task 4: `_build_file_agent_focus_dir` 改为裁行写入

**现状（custom_skill.py:282-296）**：`shutil.copy2(source, dest)` 整文件复制 → montecarlo 10 万行全保留、聚焦目录 38MB。

**改法**：读源文件行 → `_clip_log_lines`（按是否主日志决定 PID 连续性）→ 写裁剪后内容到 dest。

**Files:**
- Modify: `lark_agent_bridge/agents/bug/custom_skill.py:282-296`
- Test: `tests/test_log_focus_clip.py`

- [ ] **Step 1: 写失败测试（端到端裁行）**

```python
def test_build_focus_dir_clips_large_main_log(tmp_path):
    m = _mk()
    logs = tmp_path / "logs"
    mc = logs / "app" / "com.xiaopeng.montecarlo"
    mc.mkdir(parents=True)
    big = mc / "user0_main_2026-05-25_16-00.alog.log"
    rows = []
    base = datetime(2026, 5, 25, 16, 0, 0)
    for i in range(40000):
        d = datetime.fromtimestamp(base.timestamp() + i * 0.05)
        rows.append(f"05-25 {d:%H:%M:%S}.000 2488 1 I L: line{i}")
    big.write_text("\n".join(rows) + "\n", encoding="utf-8")

    analysis_dir = tmp_path / "out"
    analysis_dir.mkdir()
    focus_dir, manifest, copied = m._build_file_agent_focus_dir(
        input_path=logs,
        fault_time="2026-05-25 16:50:41",
        analysis_kind="app_server_investigation",
        analysis_dir=analysis_dir,
    )
    assert copied, "应有裁剪文件产出"
    dest = copied[0]
    n = sum(1 for _ in dest.open(encoding="utf-8"))
    assert n <= 15000, f"裁行后应 <= 15000，实际 {n}"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_log_focus_clip.py -k build_focus_dir_clips -v`
Expected: FAIL（当前是整文件 copy2，n == 40000）

- [ ] **Step 3: 替换 copy2 为裁行写入**

把第 282-296 行循环（当前为）：

```python
        for source in candidates:
            if input_path is not None and input_path.exists() and input_path.is_dir():
                try:
                    relative = source.relative_to(input_path)
                except ValueError:
                    relative = Path(source.name)
            else:
                relative = Path(source.name)
            dest = focus_dir / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(source, dest)
            except OSError:
                continue
            copied.append(dest)
```

改为：

```python
        fault_dt = self._parse_bug_datetime(fault_time)
        for source in candidates:
            if input_path is not None and input_path.exists() and input_path.is_dir():
                try:
                    relative = source.relative_to(input_path)
                except ValueError:
                    relative = Path(source.name)
            else:
                relative = Path(source.name)
            dest = focus_dir / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                with source.open(encoding="utf-8", errors="replace") as handle:
                    raw_lines = [line.rstrip("\n") for line in handle]
            except OSError:
                continue
            clipped = self._clip_log_lines(
                raw_lines,
                fault_dt,
                is_main_log=self._is_montecarlo_or_logd_path(source),
            )
            try:
                dest.write_text("\n".join(clipped) + ("\n" if clipped else ""), encoding="utf-8")
            except OSError:
                continue
            copied.append(dest)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_log_focus_clip.py -v`
Expected: PASS（全部）

- [ ] **Step 5: 确认 manifest 仍生成 + 提交**

Run: `python -m pytest tests/test_log_focus_clip.py -v`

```bash
git add tests/test_log_focus_clip.py lark_agent_bridge/agents/bug/custom_skill.py
git commit -m "feat(log-focus): clip log lines on focus-dir build instead of copying whole files"
```

---

### Task 5: 探索约束 prompt 强化

**现状**：`_file_agent_log_rules`（custom_skill.py:203-210）已有「优先 log_focus」「围绕故障前后 1h」；`_file_agent_search_budget_rules`（212-219）已有「rg 加预算」「不无界递归」。**缺失**两条硬约束：①故障时间后不作根因证据 ②不回读原始全量 logs。

**改法**：仅追加缺失规则，不动现有条目。

**Files:**
- Modify: `lark_agent_bridge/agents/bug/custom_skill.py:203-219`
- Test: `tests/test_log_focus_clip.py`

- [ ] **Step 1: 写失败测试**

```python
def test_log_rules_contain_fault_time_gate_and_no_raw_reread():
    m = _mk()
    rules = " ".join(m._file_agent_log_rules("app_server_investigation"))
    budget = " ".join(m._file_agent_search_budget_rules())
    # 故障时间门槛：之后的日志不作根因/源码归因证据
    assert "故障时间之后" in rules or "故障时刻之后" in rules
    assert "证据" in rules
    # 不回读原始全量 logs
    assert "log_focus" in budget
    assert "bug_cache" in budget or "原始日志" in budget
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_log_focus_clip.py -k log_rules_contain -v`
Expected: FAIL

- [ ] **Step 3: 追加规则**

`_file_agent_log_rules` 的 `rules` 列表末尾（第 208 行那条之后）追加：

```python
            "故障时间是分析门槛：故障时刻之后的日志只能用于了解后续表现，不得作为根因判断或源码归因的证据；归因必须基于故障时刻及之前的状态。",
            "log_focus/ 内的日志已按故障时间窗裁剪过，行数有限是正常的；不要因为「行数少」就回到原始日志目录重新全量搜索。",
```

`_file_agent_search_budget_rules` 的返回列表末尾追加：

```python
            "只在 `log_focus/` 目录内检索日志；非必要不要回读原始 `bug_cache/.../logs` 全量日志，尤其禁止对原始 `logd/main.txt` / `main.txt.01` 做无时间窗的 `rg` / `sed`。",
            "源码定位禁止 `rg --files <整个源码根>` 全树枚举；先读领域优先文件，再按符号名精确 `rg --max-count`。",
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_log_focus_clip.py -k log_rules_contain -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add tests/test_log_focus_clip.py lark_agent_bridge/agents/bug/custom_skill.py
git commit -m "feat(log-focus): prompt rules — fault-time evidence gate + no raw-log re-read"
```

---

### Task 6: 全量回归 + 收尾

- [ ] **Step 1: 跑新测试文件全绿**

Run: `python -m pytest tests/test_log_focus_clip.py -v`
Expected: 全部 PASS（约 9 个）

- [ ] **Step 2: 跑受影响的既有测试**

Run: `python -m pytest tests/test_app_server_investigation_runner.py tests/test_app_server_investigation.py tests/test_agents_bug_agent.py -v`
Expected: 全部 PASS（若有断言因 focus 内容变化而失败，说明该测试依赖了「整文件复制」旧行为，需按新裁行语义更新断言——更新断言而非改回旧行为）

- [ ] **Step 3: 跑 import 冒烟**

Run: `python -c "import lark_agent_bridge.agents.bug.custom_skill"`
Expected: 无错误（确认 `re` / `datetime` / `shutil` 等已由 `from ._shared import *` 提供；若 `re` 或 `datetime` 未导出，在 `_shared.py` 补 import）

- [ ] **Step 4: 最终提交**

```bash
git add -A
git commit -m "test(log-focus): full regression green for clip + constraints"
```

---

## 注意事项（给执行者）

1. **依赖导入**：`custom_skill.py` 顶部是 `from ._shared import *`。实现用到的 `re`、`datetime`、`time`、`shutil`、`Path` 应已由 `_shared` 提供（现有代码已用 `datetime.fromtimestamp`、`time.mktime`、`shutil.copy2`、`Path`）。`re` 若未导出，Task 1 跑测试时会 `NameError`，此时在 `_shared.py` 顶部补 `import re` 并在 `__all__`（若有）补上。
2. **模块级常量位置**：放在 `custom_skill.py` 文件级（`class _CustomSkillMixin:` 之外），Task 2 的 `_clip_log_lines` 内直接按全局名引用。
3. **不可解析时间的行**：`~~~~~ begin of mmap ~~~~~`、堆栈续行等无时间戳行，按本计划在窗口判断中跳过（不纳入裁剪输出）。这是有意简化，日志正文行基本都带时间戳。
4. **PID 正则**：`\d{2}:\d{2}:\d{2}(?:\.\d+)?\s+(\d+)\s+\d+` 抓「时间戳后第一个数字」为 PID（logcat 的 PID 列），第二个数字是 TID。已用真实日志行验证：`05-25 16:50:41.055 2488 7459 ...` → PID=2488。
5. **`fault_time` 格式**：来自 `_parse_bug_datetime`，固定 `YYYY-MM-DD HH:MM:SS`。行内时间戳无年份，靠 `reference_year=fault_dt.year` 补。
6. **不要改** `ld_executor.py` 的 `_ld_focus_log_candidates`（那是另一条 bug 分析链路的 6h 窗逻辑，本次不动，避免越界）。
