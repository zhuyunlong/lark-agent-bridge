"""源码调查结果与待定快照数据结构。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING
from ... import prompt_snapshots


@dataclass(slots=True)
class SourceInvestigationResult:
    success: bool
    answer: str = ""
    canonical_key: str = ""
    confidence: float = 0.0
    commands: list[str] = field(default_factory=list)
    source_evidence: list[dict[str, Any]] = field(default_factory=list)
    coverage_boundary: str = ""
    writeback_allowed: bool = False
    error: str = ""
    command: list[str] = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""


@dataclass(slots=True)
class PendingSourceSnapshot:
    snapshot: prompt_snapshots.BugPromptSnapshot
    first_seen_monotonic: float
    last_seen_monotonic: float
