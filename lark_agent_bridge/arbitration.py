"""Dual agent validation / arbitration module.

Runs a secondary agent to cross-check the primary agent's conclusion.
Compares the two results and produces a combined verdict with:

- Primary conclusion
- Secondary conclusion
- Difference summary
- Adopted version (primary by default, unless secondary flags issues)
- Confidence adjustment

Design
------
The module is decoupled from specific agent implementations.  Callers
supply two ``TaskResult`` objects (or callables that produce them), and
the arbitrator compares conclusions using keyword overlap, structural
similarity, and explicit contradiction detection.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any


@dataclass
class AgentConclusion:
    """Extracted structured conclusion from an agent result."""

    provider: str = ""
    raw_message: str = ""
    root_cause: str = ""
    confidence: str = "unknown"  # high / medium / low / unknown
    key_findings: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "raw_message": self.raw_message,
            "root_cause": self.root_cause,
            "confidence": self.confidence,
            "key_findings": self.key_findings,
            "tags": self.tags,
        }


@dataclass
class ArbitrationResult:
    """Result of dual-agent comparison."""

    primary: AgentConclusion
    secondary: AgentConclusion
    similarity_score: float = 0.0  # 0.0 - 1.0
    diff_points: list[str] = field(default_factory=list)
    adopted: str = "primary"  # "primary" or "secondary"
    adoption_reason: str = ""
    consensus: bool = False
    combined_confidence: str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "primary": self.primary.to_dict(),
            "secondary": self.secondary.to_dict(),
            "similarity_score": round(self.similarity_score, 3),
            "diff_points": self.diff_points,
            "adopted": self.adopted,
            "adoption_reason": self.adoption_reason,
            "consensus": self.consensus,
            "combined_confidence": self.combined_confidence,
        }

    def summary_text(self) -> str:
        """Human-readable summary of the arbitration."""
        lines: list[str] = []
        if self.consensus:
            lines.append("✅ 双 Agent 结论一致")
        else:
            lines.append("⚠️ 双 Agent 结论存在差异")

        lines.append(f"\n**主 Agent** ({self.primary.provider})：")
        lines.append(self.primary.root_cause or "(无明确根因)")

        lines.append(f"\n**副 Agent** ({self.secondary.provider})：")
        lines.append(self.secondary.root_cause or "(无明确根因)")

        if self.diff_points:
            lines.append("\n**差异点：**")
            for dp in self.diff_points:
                lines.append(f"  - {dp}")

        lines.append(f"\n**采用版本：** {self.adopted}")
        if self.adoption_reason:
            lines.append(f"**采用理由：** {self.adoption_reason}")
        lines.append(f"**综合可信度：** {self.combined_confidence}")
        lines.append(f"**相似度：** {self.similarity_score:.1%}")

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Conclusion extraction
# ---------------------------------------------------------------------------

_ROOT_CAUSE_PATTERNS = [
    re.compile(r"根因[：:]\s*(.+?)(?:\n|$)"),
    re.compile(r"root\s*cause[：:]\s*(.+?)(?:\n|$)", re.IGNORECASE),
    re.compile(r"结论[：:]\s*(.+?)(?:\n|$)"),
    re.compile(r"原因[：:]\s*(.+?)(?:\n|$)"),
]

_CONFIDENCE_KEYWORDS: dict[str, list[str]] = {
    "high": ["根因已确定", "明确", "确定", "confirmed", "certain", "根因"],
    "medium": ["可能", "likely", "probable", "疑似", "推测"],
    "low": ["不确定", "uncertain", "insufficient", "无法判断", "信息不足"],
}

_TAG_KEYWORDS: dict[str, str] = {
    "内存泄漏": "memory_leak",
    "memory_leak": "memory_leak",
    "OOM": "oom",
    "死锁": "deadlock",
    "deadlock": "deadlock",
    "超时": "timeout",
    "timeout": "timeout",
    "crash": "crash",
    "崩溃": "crash",
    "闪退": "crash",
    "ANR": "anr",
    "卡顿": "jank",
    "启动": "startup",
    "网络": "network",
    "IO": "io",
    "线程": "threading",
}


def extract_conclusion(
    message: str,
    *,
    provider: str = "",
) -> AgentConclusion:
    """Extract structured conclusion from an agent result message."""
    root_cause = ""
    for pattern in _ROOT_CAUSE_PATTERNS:
        m = pattern.search(message)
        if m:
            root_cause = m.group(1).strip()
            break

    confidence = "unknown"
    for level in ("low", "medium", "high"):
        keywords = _CONFIDENCE_KEYWORDS[level]
        for kw in keywords:
            if kw in message:
                confidence = level
                break
        if confidence != "unknown":
            break

    tags: list[str] = []
    for keyword, tag in _TAG_KEYWORDS.items():
        if keyword.lower() in message.lower() and tag not in tags:
            tags.append(tag)

    # Extract key findings (lines starting with - or *)
    findings: list[str] = []
    for line in message.split("\n"):
        stripped = line.strip()
        if stripped.startswith(("- ", "* ", "• ")):
            finding = stripped.lstrip("-*• ").strip()
            if len(finding) > 5:
                findings.append(finding)

    return AgentConclusion(
        provider=provider,
        raw_message=message,
        root_cause=root_cause,
        confidence=confidence,
        key_findings=findings[:10],
        tags=tags,
    )


# ---------------------------------------------------------------------------
# Arbitration logic
# ---------------------------------------------------------------------------

def arbitrate(
    primary: AgentConclusion,
    secondary: AgentConclusion,
) -> ArbitrationResult:
    """Compare two agent conclusions and produce an arbitration result.

    The primary conclusion is adopted by default unless the secondary
    has higher confidence or the primary has very low confidence.
    """
    # Text similarity
    similarity = _text_similarity(
        primary.root_cause or primary.raw_message[:300],
        secondary.root_cause or secondary.raw_message[:300],
    )

    # Find differences
    diff_points = _find_diff_points(primary, secondary)

    # Determine consensus (>70% similarity and same tags)
    tag_overlap = set(primary.tags) & set(secondary.tags)
    tag_union = set(primary.tags) | set(secondary.tags)
    tag_similarity = len(tag_overlap) / max(len(tag_union), 1)
    consensus = similarity > 0.7 and tag_similarity > 0.5

    # Decide adoption
    adopted = "primary"
    adoption_reason = "默认采用主 Agent 结论"
    confidence_order = {"high": 3, "medium": 2, "low": 1, "unknown": 0}
    primary_conf = confidence_order.get(primary.confidence, 0)
    secondary_conf = confidence_order.get(secondary.confidence, 0)

    if primary.confidence == "low" and secondary_conf > primary_conf:
        adopted = "secondary"
        adoption_reason = "主 Agent 可信度低，采用副 Agent 高可信度结论"
    elif secondary_conf > primary_conf and not consensus:
        adopted = "secondary"
        adoption_reason = "副 Agent 可信度更高且结论不同"

    # Combined confidence
    if consensus:
        combined_confidence = max(
            primary.confidence,
            secondary.confidence,
            key=lambda c: confidence_order.get(c, 0),
        )
    else:
        combined_confidence = "medium" if primary_conf > 0 or secondary_conf > 0 else "low"

    return ArbitrationResult(
        primary=primary,
        secondary=secondary,
        similarity_score=similarity,
        diff_points=diff_points,
        adopted=adopted,
        adoption_reason=adoption_reason,
        consensus=consensus,
        combined_confidence=combined_confidence,
    )


def _text_similarity(a: str, b: str) -> float:
    """Compute text similarity using SequenceMatcher."""
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _find_diff_points(
    primary: AgentConclusion,
    secondary: AgentConclusion,
) -> list[str]:
    """Identify specific differences between conclusions."""
    diffs: list[str] = []

    if primary.root_cause and secondary.root_cause:
        if _text_similarity(primary.root_cause, secondary.root_cause) < 0.5:
            diffs.append(
                f"根因不同：主=\"{_truncate(primary.root_cause, 60)}\" "
                f"副=\"{_truncate(secondary.root_cause, 60)}\""
            )

    if primary.confidence != secondary.confidence:
        diffs.append(f"可信度不同：主={primary.confidence} 副={secondary.confidence}")

    primary_tags = set(primary.tags)
    secondary_tags = set(secondary.tags)
    only_primary = primary_tags - secondary_tags
    only_secondary = secondary_tags - primary_tags
    if only_primary:
        diffs.append(f"仅主 Agent 标签：{', '.join(sorted(only_primary))}")
    if only_secondary:
        diffs.append(f"仅副 Agent 标签：{', '.join(sorted(only_secondary))}")

    return diffs


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"
