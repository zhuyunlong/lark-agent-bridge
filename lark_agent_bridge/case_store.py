"""Structured case library for analysis result archival and retrieval.

Every completed analysis (bug, signal, direct, perception) is automatically
saved as a **CaseRecord** with structured metadata that enables:

- Searching for similar past cases by problem type, root cause, or keywords.
- Building knowledge over time ("越用越聪明").
- Generating statistics and quality dashboards.

Fields per case
---------------
- ``case_id`` – unique identifier (based on job_id).
- ``problem_type`` – e.g. "启动卡顿", "信号异常", "crash".
- ``trigger_time`` – when the fault occurred.
- ``analysis_mode`` – bug_analysis / signal_lifecycle / direct_analysis / etc.
- ``main_pid`` – primary process ID if available.
- ``key_chain`` – critical call chain or signal path.
- ``root_cause_tags`` – structured root cause labels.
- ``gap_tags`` – identified analysis gaps.
- ``conclusion`` – human-readable conclusion text.
- ``conclusion_confidence`` – high / medium / low / unknown.
- ``report_url`` – link to the published report.
- ``reproduced`` – whether the issue was reproduced.
- ``human_confirmed`` – whether a human verified the conclusion.
- ``agent_provider`` – which agent produced the result.
- ``duration_seconds`` – analysis duration.
- ``created_at`` / ``updated_at`` – timestamps.

Usage example::

    from lark_agent_bridge.case_store import CaseStore

    store = CaseStore(data_dir / "state" / "cases.json")
    store.save_from_result(result, event=event)
    cases = store.search(problem_type="启动卡顿")
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class CaseRecord:
    """A single archived analysis case."""
    case_id: str
    problem_type: str = ""
    trigger_time: str = ""
    analysis_mode: str = ""
    main_pid: str = ""
    key_chain: str = ""
    root_cause_tags: list[str] = field(default_factory=list)
    gap_tags: list[str] = field(default_factory=list)
    conclusion: str = ""
    conclusion_confidence: str = "unknown"
    report_url: str = ""
    bug_url: str = ""
    reproduced: bool = False
    human_confirmed: bool = False
    agent_provider: str = ""
    duration_seconds: float | None = None
    request_text: str = ""
    chat_id: str = ""
    sender_id: str = ""
    job_id: str = ""
    created_at: str = ""
    updated_at: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "problem_type": self.problem_type,
            "trigger_time": self.trigger_time,
            "analysis_mode": self.analysis_mode,
            "main_pid": self.main_pid,
            "key_chain": self.key_chain,
            "root_cause_tags": self.root_cause_tags,
            "gap_tags": self.gap_tags,
            "conclusion": self.conclusion,
            "conclusion_confidence": self.conclusion_confidence,
            "report_url": self.report_url,
            "bug_url": self.bug_url,
            "reproduced": self.reproduced,
            "human_confirmed": self.human_confirmed,
            "agent_provider": self.agent_provider,
            "duration_seconds": self.duration_seconds,
            "request_text": self.request_text,
            "chat_id": self.chat_id,
            "sender_id": self.sender_id,
            "job_id": self.job_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "extra": self.extra,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CaseRecord:
        return cls(
            case_id=str(data.get("case_id", "")),
            problem_type=str(data.get("problem_type", "")),
            trigger_time=str(data.get("trigger_time", "")),
            analysis_mode=str(data.get("analysis_mode", "")),
            main_pid=str(data.get("main_pid", "")),
            key_chain=str(data.get("key_chain", "")),
            root_cause_tags=_coerce_str_list(data.get("root_cause_tags")),
            gap_tags=_coerce_str_list(data.get("gap_tags")),
            conclusion=str(data.get("conclusion", "")),
            conclusion_confidence=str(data.get("conclusion_confidence", "unknown")),
            report_url=str(data.get("report_url", "")),
            bug_url=str(data.get("bug_url", "")),
            reproduced=bool(data.get("reproduced", False)),
            human_confirmed=bool(data.get("human_confirmed", False)),
            agent_provider=str(data.get("agent_provider", "")),
            duration_seconds=data.get("duration_seconds"),
            request_text=str(data.get("request_text", "")),
            chat_id=str(data.get("chat_id", "")),
            sender_id=str(data.get("sender_id", "")),
            job_id=str(data.get("job_id", "")),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
            extra=data.get("extra") if isinstance(data.get("extra"), dict) else {},
        )


class CaseStore:
    """Persistent case library backed by a JSON file."""

    def __init__(self, state_file: str | Path, *, max_cases: int = 5000) -> None:
        self.state_file = Path(state_file)
        self.max_cases = max(10, int(max_cases))
        self._cases: dict[str, CaseRecord] = self._load()

    @property
    def count(self) -> int:
        return len(self._cases)

    def get(self, case_id: str) -> CaseRecord | None:
        return self._cases.get(case_id.strip())

    def save(self, case: CaseRecord) -> CaseRecord:
        """Save or update a case record."""
        now = datetime.now(timezone.utc).isoformat()
        if not case.created_at:
            case.created_at = now
        case.updated_at = now
        self._cases[case.case_id] = case
        self._enforce_limit()
        self._persist()
        return case

    def save_from_result(
        self,
        result: Any,
        *,
        event: Any | None = None,
        request_text: str = "",
        bug_url: str = "",
    ) -> CaseRecord | None:
        """Create and save a case from a TaskResult.

        Returns None if the result is not suitable for archival (skipped, no
        job_id, etc.).
        """
        if not hasattr(result, "success") or not hasattr(result, "details"):
            return None
        if getattr(result, "skipped", False):
            return None
        job_id = getattr(result, "job_id", "") or ""
        if not job_id:
            return None

        details = getattr(result, "details", {}) or {}
        mode = str(details.get("mode", ""))
        if not mode:
            return None

        message = getattr(result, "message", "") or ""
        report_url = str(details.get("published_report_url", ""))
        problem_type = _infer_problem_type(message, mode)
        root_cause_tags = _extract_root_cause_tags(message)
        conclusion_confidence = _infer_confidence(message)

        case = CaseRecord(
            case_id=job_id,
            problem_type=problem_type,
            analysis_mode=mode,
            conclusion=_truncate(message, 4000),
            conclusion_confidence=conclusion_confidence,
            report_url=report_url,
            bug_url=bug_url,
            root_cause_tags=root_cause_tags,
            agent_provider=str(details.get("provider", "")),
            duration_seconds=getattr(result, "duration_seconds", None),
            request_text=_truncate(request_text, 2000),
            job_id=job_id,
        )

        if event is not None:
            case.chat_id = getattr(event, "chat_id", "")
            case.sender_id = getattr(event, "sender_id", "")

        return self.save(case)

    def search(
        self,
        *,
        problem_type: str = "",
        analysis_mode: str = "",
        keyword: str = "",
        root_cause_tag: str = "",
        limit: int = 50,
    ) -> list[CaseRecord]:
        """Search cases by various criteria."""
        results: list[CaseRecord] = []
        for case in self._cases.values():
            if problem_type and problem_type.lower() not in case.problem_type.lower():
                continue
            if analysis_mode and case.analysis_mode != analysis_mode:
                continue
            if root_cause_tag and root_cause_tag not in case.root_cause_tags:
                continue
            if keyword and keyword.lower() not in _case_text(case).lower():
                continue
            results.append(case)

        results.sort(key=lambda c: c.updated_at or c.created_at, reverse=True)
        return results[:limit]

    def update_human_confirmation(self, case_id: str, *, confirmed: bool, notes: str = "") -> CaseRecord | None:
        """Mark a case as human-confirmed or not."""
        case = self._cases.get(case_id)
        if case is None:
            return None
        case.human_confirmed = confirmed
        if notes:
            case.extra["human_notes"] = notes
        case.updated_at = datetime.now(timezone.utc).isoformat()
        self._persist()
        return case

    def list_recent(self, *, limit: int = 20) -> list[CaseRecord]:
        """List most recent cases."""
        cases = list(self._cases.values())
        cases.sort(key=lambda c: c.updated_at or c.created_at, reverse=True)
        return cases[:limit]

    def clear(self) -> int:
        count = len(self._cases)
        self._cases = {}
        try:
            self.state_file.unlink()
        except FileNotFoundError:
            pass
        return count

    def prune_expired(self, *, max_age_hours: int, now: datetime | None = None) -> int:
        if max_age_hours <= 0:
            return 0
        reference = now or datetime.now(timezone.utc)
        cutoff = max_age_hours * 3600
        removed = 0
        for case_id in list(self._cases):
            case = self._cases[case_id]
            ts = _parse_timestamp(case.updated_at or case.created_at)
            if ts is None:
                continue
            if reference.timestamp() - ts.timestamp() > cutoff:
                del self._cases[case_id]
                removed += 1
        if removed:
            self._persist()
        return removed

    def _enforce_limit(self) -> None:
        if len(self._cases) <= self.max_cases:
            return
        sorted_cases = sorted(self._cases.values(), key=lambda c: c.updated_at or c.created_at)
        excess = len(self._cases) - self.max_cases
        for case in sorted_cases[:excess]:
            del self._cases[case.case_id]

    def _load(self) -> dict[str, CaseRecord]:
        if not self.state_file.exists():
            return {}
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        cases: dict[str, CaseRecord] = {}
        for key, value in data.items():
            if isinstance(value, dict):
                cases[key] = CaseRecord.from_dict(value)
        return cases

    def _persist(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {case_id: case.to_dict() for case_id, case in self._cases.items()}
        self.state_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


# ---------------------------------------------------------------------------
# Inference helpers
# ---------------------------------------------------------------------------

_PROBLEM_PATTERNS: list[tuple[str, str]] = [
    ("启动", "启动异常"),
    ("卡顿", "卡顿"),
    ("黑屏", "黑屏"),
    ("闪退", "闪退/Crash"),
    ("crash", "闪退/Crash"),
    ("tombstone", "闪退/Crash"),
    ("内存", "内存问题"),
    ("oom", "内存问题"),
    ("anr", "ANR"),
    ("信号", "信号异常"),
    ("signal", "信号异常"),
    ("感知", "感知数据异常"),
    ("超时", "超时"),
    ("timeout", "超时"),
]

_CONFIDENCE_KEYWORDS: dict[str, list[str]] = {
    "high": ["根因", "确定", "明确", "证实", "root cause", "confirmed"],
    "medium": ["可能", "推测", "疑似", "初步", "likely", "probable"],
    "low": ["不确定", "缺少", "无法判断", "信息不足", "unclear", "insufficient"],
}

_ROOT_CAUSE_PATTERNS: list[tuple[str, str]] = [
    ("内存泄漏", "memory_leak"),
    ("memory leak", "memory_leak"),
    ("死锁", "deadlock"),
    ("deadlock", "deadlock"),
    ("超时", "timeout"),
    ("timeout", "timeout"),
    ("空指针", "null_pointer"),
    ("null pointer", "null_pointer"),
    ("资源竞争", "race_condition"),
    ("race condition", "race_condition"),
    ("配置错误", "config_error"),
    ("权限", "permission"),
    ("网络", "network"),
    ("io 错误", "io_error"),
    ("首帧", "first_frame"),
    ("渲染", "rendering"),
]


def _infer_problem_type(text: str, mode: str) -> str:
    lowered = text.lower()
    for pattern, label in _PROBLEM_PATTERNS:
        if pattern.lower() in lowered:
            return label
    mode_defaults = {
        "signal_lifecycle": "信号异常",
        "perception_summary": "感知数据异常",
        "bug_analysis": "Bug分析",
        "direct_analysis": "日志分析",
    }
    return mode_defaults.get(mode, "未分类")


def _infer_confidence(text: str) -> str:
    lowered = text.lower()
    for level, keywords in _CONFIDENCE_KEYWORDS.items():
        if any(kw.lower() in lowered for kw in keywords):
            return level
    return "unknown"


def _extract_root_cause_tags(text: str) -> list[str]:
    lowered = text.lower()
    tags: list[str] = []
    seen: set[str] = set()
    for pattern, tag in _ROOT_CAUSE_PATTERNS:
        if pattern.lower() in lowered and tag not in seen:
            tags.append(tag)
            seen.add(tag)
    return tags


def _case_text(case: CaseRecord) -> str:
    return " ".join([
        case.problem_type,
        case.analysis_mode,
        case.conclusion,
        case.request_text,
        case.key_chain,
        " ".join(case.root_cause_tags),
    ])


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars]


def _coerce_str_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def _parse_timestamp(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
