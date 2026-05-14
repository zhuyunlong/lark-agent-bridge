"""Tests for the cards module."""

from __future__ import annotations

import json

from lark_agent_bridge.cards import (
    build_confirmation_card,
    build_result_card,
    build_status_card,
    build_text_fallback,
    card_to_json,
)


class TestBuildStatusCard:
    def test_basic_status_card(self):
        card = build_status_card(title="Bug 分析", status="analyzing")
        assert card["header"]["title"]["content"] == "📋 Bug 分析"
        assert card["header"]["template"] == "blue"
        assert card["config"]["wide_screen_mode"] is True
        elements = card["elements"]
        assert any("分析中" in str(e) for e in elements)

    def test_status_with_details(self):
        card = build_status_card(
            title="信号分析",
            status="downloading",
            details={"信号": "SIGNAL_X3D", "日志": "file_abc"},
        )
        elements = card["elements"]
        assert any("信号" in str(e) and "SIGNAL_X3D" in str(e) for e in elements)

    def test_status_with_note(self):
        card = build_status_card(title="分析", status="queued", note="排队等待中")
        elements = card["elements"]
        assert any("排队等待中" in str(e) for e in elements)

    def test_status_completed(self):
        card = build_status_card(title="完成", status="completed")
        assert card["header"]["template"] == "green"
        assert any("已完成" in str(e) for e in card["elements"])

    def test_status_failed(self):
        card = build_status_card(title="失败", status="failed")
        assert card["header"]["template"] == "red"
        assert any("失败" in str(e) for e in card["elements"])


class TestBuildResultCard:
    def test_success_result(self):
        card = build_result_card(
            title="Bug 分析",
            success=True,
            summary="分析完成，根因是内存泄漏。",
            report_url="http://example.com/report",
            job_id="job123",
        )
        assert "✅" in card["header"]["title"]["content"]
        assert card["header"]["template"] == "green"
        elements = card["elements"]
        assert any("打开报告" in str(e) for e in elements)
        assert any("重新分析" in str(e) for e in elements)

    def test_failed_result(self):
        card = build_result_card(
            title="分析",
            success=False,
            summary="分析超时。",
        )
        assert "❌" in card["header"]["title"]["content"]
        assert card["header"]["template"] == "red"

    def test_result_with_metadata(self):
        card = build_result_card(
            title="信号分析",
            success=True,
            summary="OK",
            metadata={"分析类型": "信号生命周期", "故障时间": "2024-01-01 10:00"},
        )
        elements = card["elements"]
        assert any("信号生命周期" in str(e) for e in elements)
        assert any("故障时间" in str(e) for e in elements)

    def test_result_with_duration(self):
        card = build_result_card(
            title="分析",
            success=True,
            summary="OK",
            duration_seconds=42.5,
        )
        assert any("42.5" in str(e) for e in card["elements"])

    def test_result_no_actions(self):
        card = build_result_card(
            title="分析",
            success=True,
            summary="OK",
            show_actions=False,
        )
        elements = card["elements"]
        assert not any(e.get("tag") == "action" for e in elements)

    def test_summary_truncation(self):
        long_summary = "A" * 2000
        card = build_result_card(title="分析", success=True, summary=long_summary)
        for e in card["elements"]:
            text = e.get("text", {})
            if isinstance(text, dict):
                content = text.get("content", "")
                if "完整内容" in content:
                    assert len(content) < 2000
                    break


class TestBuildConfirmationCard:
    def test_basic_confirmation(self):
        card = build_confirmation_card(
            title="大文件下载确认",
            description="即将下载 5GB 日志文件，预计耗时 10 分钟。",
            risk_level="medium",
            action_id="dl_001",
        )
        assert card["header"]["template"] == "orange"
        assert "⚠️" in card["header"]["title"]["content"]
        elements = card["elements"]
        assert any("确认执行" in str(e) for e in elements)
        assert any("取消" in str(e) for e in elements)
        assert any("🟡 中" in str(e) for e in elements)

    def test_high_risk(self):
        card = build_confirmation_card(
            title="批量操作",
            description="将归档 50 条记录。",
            risk_level="high",
        )
        assert any("🔴 高" in str(e) for e in card["elements"])


class TestCardToJson:
    def test_serialization(self):
        card = build_status_card(title="Test", status="queued")
        json_str = card_to_json(card)
        parsed = json.loads(json_str)
        assert parsed["header"]["title"]["content"] == "📋 Test"
        assert " " not in json_str or "排队" in json_str  # compact separators

    def test_roundtrip(self):
        card = build_result_card(
            title="分析", success=True, summary="OK", report_url="http://x.com"
        )
        json_str = card_to_json(card)
        assert json.loads(json_str) == card


class TestBuildTextFallback:
    def test_extracts_title_and_content(self):
        card = build_status_card(title="Bug", status="analyzing", details={"信号": "X"})
        fallback = build_text_fallback(card)
        assert "Bug" in fallback
        assert "分析中" in fallback

    def test_empty_card(self):
        fallback = build_text_fallback({})
        assert fallback == ""
