"""Tests for the cards module."""

from __future__ import annotations

import json
import unittest

from lark_agent_bridge.cards import (
    build_agent_reanalysis_confirmation_card,
    build_confirmation_card,
    build_followup_result_card,
    build_knowledge_answer_card,
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

    def test_completed_status_note_is_readable_preview(self):
        note = (
            "## 结论摘要\n"
            "- `SIGNAL_CTL_POWERCENTER_SYNTHESIS_REMAIN_DIS_CHANGE` 是 `16042`，含义为综合续航。\n"
            "- 故障时间附近未看到回调。\n"
            "## 关键证据\n"
            "- 证据详情应留在报告中。"
        )

        card = build_status_card(title="Bug 分析", status="completed", note=note)

        rendered = str(card)
        assert "**结论摘要**" in rendered
        assert "## 结论摘要" not in rendered
        assert "SIGNAL_CTL_POWERCENTER_SYNTHESIS_REMAIN_DIS_CHANGE" not in rendered
        assert "SIGNAL_CTL_POWERCENTER_..._CHANGE" in rendered
        assert "`16042`" not in rendered
        assert "## 关键证据" not in rendered
        assert "证据详情应留在报告中" not in rendered

    def test_status_card_can_offer_bug_skill_choices(self):
        card = build_status_card(
            title="Bug 分析分诊",
            status="completed",
            job_id="job_1",
            root_message_id="om_root",
            bug_skill_choice_note="当前命中：当前感知数据总结。如果意图不正确，可以改选支持的 Skill 重新分析。",
            bug_skill_choices=[
                {
                    "name": "xtheme-analyzer",
                    "label": "XTheme时光主题分析",
                    "description": "分析主题切换、UI mode、日出日落等问题。",
                    "selected": False,
                },
                {
                    "name": "3d-stuck-investigate",
                    "label": "3D卡顿分析",
                    "description": "分析画面卡顿、黑屏、掉帧。",
                    "selected": True,
                },
            ],
        )

        rendered = str(card)
        assert "意图/Skill 校正" in rendered
        assert "当前命中：当前感知数据总结" in rendered
        assert "当前命中" in rendered
        assert "bug_skill_choice_form" in rendered
        assert "select_bug_skill" in rendered
        assert "xtheme-analyzer" in rendered
        assert "3D卡顿分析" in rendered
        assert "不代表当前报告已使用这些 Skill" in rendered
        assert "下方按钮只是重新分析入口" not in rendered
        assert "本区的 Skill 按钮会按所选 Skill" in rendered
        assert "分析主题切换" not in rendered
        assert "分析画面卡顿" not in rendered

    def test_status_card_can_offer_agent_switch_choices(self):
        card = build_status_card(
            title="Bug 分析",
            status="completed",
            job_id="job_1",
            root_message_id="om_root",
            show_followup_actions=True,
            bug_agent_choices=[
                {"provider": "claude", "label": "换 Claude 重分析"},
                {"provider": "omlx", "label": "用 OMLX 本地模型"},
            ],
        )

        rendered = str(card)
        assert "换 Agent 会先弹出确认卡" in rendered
        assert "select_bug_agent" in rendered
        assert "换 Claude 重分析" in rendered
        assert "用 OMLX 本地模型" in rendered
        assert "'agent_provider': 'claude'" in rendered
        assert "'agent_provider': 'omlx'" in rendered

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


class TestBuildFollowupResultCard:
    def test_followup_card_has_choice_and_feedback_buttons(self):
        card = build_followup_result_card(
            title="Bug 追问",
            summary="系统主题是黑夜。",
            report_url="http://example.com/report",
            root_message_id="om_root",
            job_id="job_1",
            followup_text="问题时刻系统主题是白天还是黑夜",
            answer_confidence=0.9,
        )

        rendered = str(card)
        assert "打开报告" in rendered
        assert "先输入追问" in rendered
        assert "上次追问" in rendered
        assert "followup_prompt" in rendered
        assert "按输入从报告回答" in rendered
        assert "按输入重跑日志" in rendered
        assert "按输入续 Agent" in rendered
        assert "有用(记录)" in rendered
        assert "不准(记录)" in rendered
        assert "'action': 'answer_from_report'" in rendered
        assert "'action': 'continue_agent'" in rendered
        assert "'tag': 'form'" in rendered
        assert "'followup_text': '问题时刻系统主题是白天还是黑夜'" in rendered


class TestBuildKnowledgeAnswerCard:
    def test_knowledge_answer_card_lists_multiple_hits(self):
        card = build_knowledge_answer_card(
            title="知识库回答",
            answer="SIGNAL_OTA_ST 四种组合指令\nadb shell am broadcast ...",
            hits=[
                {
                    "source_id": "guideengine-signals",
                    "title": "SIGNAL_OTA_ST 定义",
                    "source_ref": "/path/to/signal.proto",
                    "score": 8.5,
                },
                {
                    "source_id": "guideengine-adb",
                    "title": "ADB 命令集",
                    "source_ref": "/path/to/adb_data.json",
                    "score": 4.2,
                },
            ],
        )

        rendered = str(card)
        assert "知识库回答" in card["header"]["title"]["content"]
        assert "SIGNAL_OTA_ST 四种组合指令" in rendered
        assert "参考来源" in rendered
        assert "score=" not in rendered
        assert "guideengine-signals" not in rendered
        assert "ADB 命令集" in rendered


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

    def test_agent_reanalysis_confirmation(self):
        card = build_agent_reanalysis_confirmation_card(
            agent_label="Claude Agent",
            agent_provider="claude",
            job_id="job_1",
            root_message_id="om_root",
            followup_text="重点看源码证据",
        )

        rendered = str(card)
        assert "确认换 Agent 重分析" in card["header"]["title"]["content"]
        assert "Claude Agent" in rendered
        assert "重点看源码证据" in rendered
        assert "confirm_bug_agent_reanalysis" in rendered
        assert "cancel_bug_agent_reanalysis" in rendered
        assert "'agent_provider': 'claude'" in rendered


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


class DynamicStatusCardTests(unittest.TestCase):
    def test_status_card_with_progress_runtime_and_tokens(self):
        card = build_status_card(
            title="Bug 分析",
            status="analyzing",
            details={"任务ID": "job_1"},
            progress=[
                {"stage": "bug_fetch_data", "message": "拉取 bug 详情"},
                {"stage": "bug_agent_summary", "message": "调用本地 Agent", "details": {"provider": "codex"}},
            ],
            elapsed_seconds=12.4,
            token_usage={"input_tokens": 1000, "output_tokens": 200, "total_tokens": 1200},
            report_url="http://127.0.0.1:8765/reports/job_1/",
        )

        rendered = str(card)
        self.assertIn("后台进度", rendered)
        self.assertIn("bug_fetch_data", rendered)
        self.assertIn("12.4 秒", rendered)
        self.assertIn("1200", rendered)
        self.assertIn("打开报告", rendered)

    def test_status_card_progress_lines_include_time(self):
        card = build_status_card(
            title="Bug 分析",
            status="analyzing",
            progress=[
                {
                    "timestamp": "2026-05-19T12:34:56",
                    "stage": "bug_fetch_data",
                    "message": "拉取 bug 详情",
                },
            ],
        )

        rendered = str(card)
        self.assertIn("后台进度", rendered)
        self.assertIn("12:34:56", rendered)
        self.assertIn("bug_fetch_data", rendered)

    def test_status_card_progress_lines_include_executor(self):
        card = build_status_card(
            title="Bug 分析",
            status="analyzing",
            progress=[
                {
                    "timestamp": "2026-05-19T12:34:56",
                    "stage": "bug_agent_summary",
                    "message": "调用本地 Agent",
                    "details": {"executor": "本地 Agent(codex)", "provider": "codex"},
                },
            ],
        )

        rendered = str(card)
        self.assertIn("本地 Agent(codex)", rendered)
        self.assertIn("bug_agent_summary", rendered)

    def test_status_card_shows_stream_preview_and_live_url(self):
        card = build_status_card(
            title="Bug 分析",
            status="analyzing",
            progress=[
                {
                    "timestamp": "2026-05-19T12:34:56",
                    "stage": "bug_fetch_data",
                    "message": "拉取 bug 详情",
                },
                {
                    "timestamp": "2026-05-19T12:35:00",
                    "stage": "bug_agent_summary_stream",
                    "message": "深度分析输出更新：Codex 已开始深度分析",
                    "details": {"stream_preview": "Codex 已开始深度分析"},
                },
            ],
            live_url="http://127.0.0.1:8765/sessions?session=om_1",
        )

        rendered = str(card)
        self.assertIn("后台进度", rendered)
        self.assertIn("bug_fetch_data", rendered)
        self.assertIn("深度分析输出", rendered)
        self.assertIn("Codex 已开始深度分析", rendered)
        self.assertIn("实时进度", rendered)
        self.assertIn("http://127.0.0.1:8765/sessions?session=om_1", rendered)
        self.assertNotIn("`bug_agent_summary_stream`", rendered)

    def test_status_card_compacts_raw_item_started_as_tool_call(self):
        card = build_status_card(
            title="Bug 分析",
            status="analyzing",
            progress=[
                {
                    "timestamp": "2026-05-19T12:35:00",
                    "stage": "bug_agent_summary_stream",
                    "details": {
                        "stream_preview": (
                            'item.started: {"item":{"type":"command_execution",'
                            '"command":"/bin/zsh -lc \\"rg -n tool_call lark_agent_bridge\\""}}'
                        )
                    },
                },
            ],
        )

        rendered = str(card)
        self.assertIn("工具调用：执行命令 rg -n tool_call lark_agent_bridge", rendered)
        self.assertNotIn("item.started", rendered)

    def test_status_card_does_not_show_empty_error_completed_stream_event(self):
        card = build_status_card(
            title="Bug 分析",
            status="analyzing",
            progress=[
                {
                    "timestamp": "2026-05-19T12:35:00",
                    "stage": "bug_agent_summary_stream",
                    "details": {"stream_preview": "error 已完成"},
                },
                {
                    "timestamp": "2026-05-19T12:35:01",
                    "stage": "bug_agent_summary_stream",
                    "details": {"stream_preview": "Codex 已开始深度分析"},
                },
            ],
        )

        rendered = str(card)
        self.assertNotIn("error 已完成", rendered)
        self.assertIn("Codex 已开始深度分析", rendered)

    def test_status_card_keeps_progress_when_stream_events_are_latest(self):
        card = build_status_card(
            title="Bug 分析",
            status="analyzing",
            progress=[
                {"stage": "bug_fetch_data", "message": "拉取 bug 详情"},
                *[
                    {
                        "stage": "bug_agent_summary_stream",
                        "message": f"stream {index}",
                        "details": {"stream_preview": f"stream {index}"},
                    }
                    for index in range(8)
                ],
            ],
        )

        rendered = str(card)
        self.assertIn("bug_fetch_data", rendered)
        self.assertIn("stream 7", rendered)
        self.assertNotIn("stream 0", rendered)

    def test_status_card_compacts_progress_and_stream_preview(self):
        card = build_status_card(
            title="Bug 分析",
            status="completed",
            progress=[
                *[
                    {"stage": f"stage_{index}", "message": f"阶段 {index}"}
                    for index in range(6)
                ],
                *[
                    {
                        "timestamp": "2026-05-19T12:35:00",
                        "stage": "bug_agent_summary_stream",
                        "details": {
                            "stream_preview": (
                                'item.started: {"type":"item.started","item":{"type":"command_execution",'
                                '"command":"/bin/zsh -lc \\"nl -ba /tmp/example.log\\""}}'
                            )
                        },
                    },
                    {
                        "timestamp": "2026-05-19T12:35:01",
                        "stage": "bug_agent_summary_stream",
                        "details": {"stream_preview": "command_execution 已完成"},
                    },
                    {
                        "timestamp": "2026-05-19T12:35:02",
                        "stage": "bug_agent_summary_stream",
                        "details": {"stream_preview": "已经定位到候选证据"},
                    },
                    {
                        "timestamp": "2026-05-19T12:35:03",
                        "stage": "bug_agent_summary_stream",
                        "details": {"stream_preview": "开始整理结论"},
                    },
                ],
            ],
            note="## 结论摘要\n当前结论更像是业务策略问题。",
        )

        rendered = str(card)
        self.assertIn("结论摘要", rendered)
        self.assertIn("当前结论更像是业务策略问题", rendered)
        self.assertNotIn("stage_0", rendered)
        self.assertNotIn("stage_1", rendered)
        self.assertIn("stage_5", rendered)
        self.assertIn("深度分析输出（最近 3 条）", rendered)
        self.assertNotIn("命令执行完成", rendered)
        self.assertIn("开始整理结论", rendered)
        self.assertNotIn("item.started", rendered)

    def test_completed_status_card_can_keep_followup_actions(self):
        card = build_status_card(
            title="直传文件分析",
            status="completed",
            report_url="http://127.0.0.1:8765/reports/job_1/",
            live_url="http://127.0.0.1:8765/sessions?session=om_1",
            job_id="job_1",
            root_message_id="om_1",
            show_followup_actions=True,
        )

        rendered = str(card)
        self.assertIn("打开报告", rendered)
        self.assertIn("实时进度", rendered)
        self.assertIn("followup_prompt", rendered)
        self.assertIn("按输入从报告回答", rendered)
        self.assertIn("按输入重跑日志", rendered)
        self.assertIn("按输入续 Agent", rendered)
        self.assertIn("'action': 'answer_from_report'", rendered)
        self.assertIn("'action': 'reanalyze'", rendered)
        self.assertIn("'action': 'continue_agent'", rendered)
        self.assertIn("有用(记录)", rendered)
        self.assertIn("不准(记录)", rendered)
