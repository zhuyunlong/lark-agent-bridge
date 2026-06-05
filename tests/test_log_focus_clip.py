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
