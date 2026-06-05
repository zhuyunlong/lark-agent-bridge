from datetime import datetime
from pathlib import Path

from lark_agent_bridge.agents.bug.archive_extract import _ArchiveExtractMixin
from lark_agent_bridge.agents.bug.custom_skill import _CustomSkillMixin


class _Mixin(_ArchiveExtractMixin, _CustomSkillMixin):
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
