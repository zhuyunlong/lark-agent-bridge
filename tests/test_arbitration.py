"""Tests for the arbitration module."""

from __future__ import annotations

from lark_agent_bridge.arbitration import (
    AgentConclusion,
    ArbitrationResult,
    arbitrate,
    extract_conclusion,
)


class TestExtractConclusion:
    def test_extracts_root_cause(self):
        msg = "分析结果如下：\n根因：内存泄漏导致 OOM\n详细信息..."
        c = extract_conclusion(msg, provider="claude")
        assert c.root_cause == "内存泄漏导致 OOM"
        assert c.provider == "claude"

    def test_extracts_english_root_cause(self):
        msg = "Root cause: deadlock in main thread\nDetails..."
        c = extract_conclusion(msg)
        assert "deadlock" in c.root_cause

    def test_extracts_conclusion_fallback(self):
        msg = "结论：启动超时是由IO阻塞引起"
        c = extract_conclusion(msg)
        assert "启动超时" in c.root_cause

    def test_confidence_high(self):
        msg = "根因已确定：内存泄漏"
        c = extract_conclusion(msg)
        assert c.confidence == "high"

    def test_confidence_medium(self):
        msg = "可能是网络超时导致"
        c = extract_conclusion(msg)
        assert c.confidence == "medium"

    def test_confidence_low(self):
        msg = "信息不足，无法判断根因"
        c = extract_conclusion(msg)
        assert c.confidence == "low"

    def test_confidence_unknown(self):
        msg = "分析完成"
        c = extract_conclusion(msg)
        assert c.confidence == "unknown"

    def test_extracts_tags(self):
        msg = "发现内存泄漏和deadlock问题，导致超时"
        c = extract_conclusion(msg)
        assert "memory_leak" in c.tags
        assert "deadlock" in c.tags
        assert "timeout" in c.tags

    def test_extracts_key_findings(self):
        msg = "分析结果：\n- 主线程被阻塞 15 秒\n- GC 耗时过长\n- 内存占用 1.5GB"
        c = extract_conclusion(msg)
        assert len(c.key_findings) == 3

    def test_to_dict(self):
        c = AgentConclusion(provider="test", root_cause="rc", tags=["t1"])
        d = c.to_dict()
        assert d["provider"] == "test"
        assert d["tags"] == ["t1"]


class TestArbitrate:
    def test_consensus_same_conclusion(self):
        primary = AgentConclusion(
            provider="claude",
            root_cause="内存泄漏导致OOM",
            confidence="high",
            tags=["memory_leak", "oom"],
        )
        secondary = AgentConclusion(
            provider="codex",
            root_cause="内存泄漏导致OOM崩溃",
            confidence="high",
            tags=["memory_leak", "oom"],
        )
        result = arbitrate(primary, secondary)
        assert result.consensus is True
        assert result.adopted == "primary"
        assert result.combined_confidence == "high"

    def test_disagreement_different_conclusions(self):
        primary = AgentConclusion(
            provider="claude",
            root_cause="网络超时",
            confidence="medium",
            tags=["timeout", "network"],
        )
        secondary = AgentConclusion(
            provider="codex",
            root_cause="内存泄漏",
            confidence="high",
            tags=["memory_leak"],
        )
        result = arbitrate(primary, secondary)
        assert result.consensus is False
        assert result.adopted == "secondary"  # higher confidence
        assert len(result.diff_points) > 0

    def test_primary_low_confidence_overridden(self):
        primary = AgentConclusion(
            provider="claude",
            root_cause="不确定",
            confidence="low",
            tags=[],
        )
        secondary = AgentConclusion(
            provider="codex",
            root_cause="死锁导致ANR",
            confidence="high",
            tags=["deadlock", "anr"],
        )
        result = arbitrate(primary, secondary)
        assert result.adopted == "secondary"
        assert "低" in result.adoption_reason or "可信度" in result.adoption_reason

    def test_similarity_score(self):
        primary = AgentConclusion(provider="a", root_cause="crash due to null pointer")
        secondary = AgentConclusion(provider="b", root_cause="crash due to null pointer")
        result = arbitrate(primary, secondary)
        assert result.similarity_score > 0.9

    def test_diff_points_tags(self):
        primary = AgentConclusion(tags=["memory_leak", "timeout"])
        secondary = AgentConclusion(tags=["timeout", "deadlock"])
        result = arbitrate(primary, secondary)
        tag_diffs = [d for d in result.diff_points if "标签" in d]
        assert len(tag_diffs) > 0

    def test_to_dict(self):
        primary = AgentConclusion(provider="a", root_cause="test")
        secondary = AgentConclusion(provider="b", root_cause="test")
        result = arbitrate(primary, secondary)
        d = result.to_dict()
        assert "primary" in d
        assert "secondary" in d
        assert "similarity_score" in d

    def test_summary_text(self):
        primary = AgentConclusion(provider="claude", root_cause="OOM")
        secondary = AgentConclusion(provider="codex", root_cause="OOM")
        result = arbitrate(primary, secondary)
        text = result.summary_text()
        assert "claude" in text
        assert "codex" in text
        assert "OOM" in text

    def test_empty_conclusions(self):
        primary = AgentConclusion(provider="a")
        secondary = AgentConclusion(provider="b")
        result = arbitrate(primary, secondary)
        assert result.similarity_score == 0.0
        assert result.adopted == "primary"
