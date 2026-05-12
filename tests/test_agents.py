from pathlib import Path
import json
import subprocess
import tempfile
import unittest
from unittest import mock

from lark_agent_bridge.agents import BugAnalysisRunner, ClaudeSkillRunner, OmlxChatClient, PerceptionSummaryRunner
from lark_agent_bridge.models import BridgeConfig, BugRequest, DownloadResource, LarkEvent, PerceptionSummaryRequest


class AgentTests(unittest.TestCase):
    def test_omlx_chat_posts_to_chat_completions(self):
        response_payload = {"choices": [{"message": {"content": "本地模型回复"}}]}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return None

            def read(self):
                return json.dumps(response_payload).encode("utf-8")

        captured = {}

        def fake_urlopen(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeResponse()

        config = BridgeConfig(dry_run=False)
        config.omlx_chat.api_key = "test-api-key"
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = OmlxChatClient(config).reply("你好")

        self.assertTrue(result.success)
        self.assertEqual(result.message, "本地模型回复")
        self.assertEqual(captured["timeout"], config.omlx_chat.timeout_seconds)
        self.assertEqual(captured["request"].get_header("Authorization"), "Bearer test-api-key")
        body = json.loads(captured["request"].data.decode("utf-8"))
        self.assertEqual(body["model"], "gemma-4-26b-a4b-it-4bit")
        self.assertEqual(body["messages"][-1]["content"], "你好")

    def test_omlx_chat_dry_run_does_not_call_endpoint(self):
        config = BridgeConfig(dry_run=True)

        result = OmlxChatClient(config).reply("你好")

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "omlx_chat")
        self.assertIn("/chat/completions", result.details["url"])

    def test_claude_skill_dry_run_returns_command_and_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=True, data_dir=Path(tmp), workspace_root=Path(tmp))
            result = ClaudeSkillRunner(config).run_skill_analysis(
                __import__("lark_agent_bridge.models", fromlist=["ClaudeSkillRequest"]).ClaudeSkillRequest(
                    prompt="分析这个场景",
                    raw_text="/skill 分析这个场景",
                    triggered=True,
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "claude_skill")
        self.assertIn("--print", result.command)
        self.assertIn("--allowedTools", result.command)
        self.assertIn("claude_skill_result.md", result.message)

    def test_claude_skill_success_writes_markdown_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp), workspace_root=Path(tmp))
            completed = subprocess.CompletedProcess(
                args=["claude"],
                returncode=0,
                stdout="分析完成\n证据",
                stderr="",
            )
            with mock.patch("subprocess.run", return_value=completed):
                result = ClaudeSkillRunner(config).run_skill_analysis(
                    __import__("lark_agent_bridge.models", fromlist=["ClaudeSkillRequest"]).ClaudeSkillRequest(
                        prompt="分析这个场景",
                        raw_text="/skill 分析这个场景",
                        triggered=True,
                    )
                )

            self.assertTrue(result.success)
            artifact = Path(result.details["files_to_send"][0])
            self.assertTrue(artifact.exists())
            self.assertEqual(artifact.read_text(encoding="utf-8"), "分析完成\n证据")

    def test_claude_skill_failure_does_not_include_files_to_send(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp), workspace_root=Path(tmp))
            completed = subprocess.CompletedProcess(
                args=["claude"],
                returncode=1,
                stdout="",
                stderr="boom",
            )
            with mock.patch("subprocess.run", return_value=completed):
                result = ClaudeSkillRunner(config).run_skill_analysis(
                    __import__("lark_agent_bridge.models", fromlist=["ClaudeSkillRequest"]).ClaudeSkillRequest(
                        prompt="分析这个场景",
                        raw_text="/skill 分析这个场景",
                        triggered=True,
                    )
                )

            self.assertFalse(result.success)
            self.assertNotIn("files_to_send", result.details)

    def test_bug_analysis_dry_run_returns_command_and_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=True, data_dir=Path(tmp), workspace_root=Path(tmp))
            result = BugAnalysisRunner(config).run_bug_analysis(
                BugRequest(
                    bug_url="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722",
                    prompt="调查3D启动时序",
                    raw_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查3D启动时序",
                    triggered=True,
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "bug_analysis")
        self.assertIn("metadata:", result.message)
        self.assertIn("bug_metadata.md", result.message)
        self.assertIn("bug_3d_startup_report.html", result.message)

    def test_bug_analysis_classifies_startup_request(self):
        runner = BugAnalysisRunner(BridgeConfig(dry_run=True))

        plan = runner.classify_request(
            prompt_text="调查3D启动时序",
            title="",
            description="",
        )

        self.assertEqual(plan.kind, "startup")
        self.assertIsNone(plan.signal_code)

    def test_bug_analysis_classifies_stuck_request(self):
        runner = BugAnalysisRunner(BridgeConfig(dry_run=True))

        plan = runner.classify_request(
            prompt_text="调查3D卡顿黑屏和ANR",
            title="",
            description="",
        )

        self.assertEqual(plan.kind, "stuck")
        self.assertIsNone(plan.signal_code)

    def test_bug_analysis_classifies_signal_request_and_extracts_signal(self):
        runner = BugAnalysisRunner(BridgeConfig(dry_run=True))

        plan = runner.classify_request(
            prompt_text="分析 132002 为什么没到Unity",
            title="",
            description="",
        )

        self.assertEqual(plan.kind, "signal")
        self.assertEqual(plan.signal_code, "132002")

    def test_bug_analysis_does_not_treat_vin_suffix_as_signal(self):
        runner = BugAnalysisRunner(BridgeConfig(dry_run=True))

        plans = runner.classify_requests(
            prompt_text="分析感知数据",
            title="【RT项目】RT模式前后排屏SR无感知",
            description="VIN码为: [L1NNSTUK9TB000661]\n前后排屏SR无感知",
        )

        self.assertEqual([plan.kind for plan in plans], ["perception"])

    def test_bug_analysis_classifies_crash_request(self):
        runner = BugAnalysisRunner(BridgeConfig(dry_run=True))

        plan = runner.classify_request(
            prompt_text="调查闪退和tombstone",
            title="",
            description="",
        )

        self.assertEqual(plan.kind, "crash")
        self.assertIsNone(plan.signal_code)

    def test_bug_analysis_classifies_startup_and_stuck_requests_together(self):
        runner = BugAnalysisRunner(BridgeConfig(dry_run=True))

        plans = runner.classify_requests(
            prompt_text="分析启动和卡顿",
            title="",
            description="",
        )

        self.assertEqual([plan.kind for plan in plans], ["startup", "stuck"])

    def test_bug_analysis_classifies_perception_request(self):
        runner = BugAnalysisRunner(BridgeConfig(dry_run=True))

        plans = runner.classify_requests(
            prompt_text="总结当前感知数据",
            title="",
            description="",
        )

        self.assertEqual([plan.kind for plan in plans], ["perception"])

    def test_bug_analysis_dry_run_routes_to_stuck_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=True, data_dir=Path(tmp), workspace_root=Path(tmp))
            result = BugAnalysisRunner(config).run_bug_analysis(
                BugRequest(
                    bug_url="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722",
                    prompt="调查3D卡顿黑屏",
                    raw_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查3D卡顿黑屏",
                    triggered=True,
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["analysis_kind"], "stuck")
        self.assertTrue(any("analyze_3d_stuck.py" in part for part in result.command))
        self.assertIn("bug_3d_stuck_report.html", result.message)

    def test_bug_analysis_dry_run_routes_to_signal_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=True, data_dir=Path(tmp), workspace_root=Path(tmp))
            result = BugAnalysisRunner(config).run_bug_analysis(
                BugRequest(
                    bug_url="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722",
                    prompt="分析132002为什么没到Unity",
                    raw_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 分析132002为什么没到Unity",
                    triggered=True,
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["analysis_kind"], "signal")
        self.assertEqual(result.details["signal_code"], "132002")
        self.assertTrue(any("analyze_signal_chain.py" in part for part in result.command))
        self.assertIn("bug_signal_chain_report.html", result.message)

    def test_bug_analysis_dry_run_routes_to_crash_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=True, data_dir=Path(tmp), workspace_root=Path(tmp))
            result = BugAnalysisRunner(config).run_bug_analysis(
                BugRequest(
                    bug_url="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722",
                    prompt="调查闪退和tombstone",
                    raw_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查闪退和tombstone",
                    triggered=True,
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["analysis_kind"], "crash")
        self.assertTrue(any("analyze_3d_stuck.py" in part for part in result.command))
        self.assertIn("bug_crash_report.html", result.message)

    def test_bug_analysis_dry_run_routes_to_startup_and_stuck_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=True, data_dir=Path(tmp), workspace_root=Path(tmp))
            result = BugAnalysisRunner(config).run_bug_analysis(
                BugRequest(
                    bug_url="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722",
                    prompt="分析启动和卡顿",
                    raw_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 分析启动和卡顿",
                    triggered=True,
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["analysis_kinds"], ["startup", "stuck"])
        self.assertIn("bug_3d_startup_report.html", result.message)
        self.assertIn("bug_3d_stuck_report.html", result.message)

    def test_bug_analysis_dry_run_routes_to_perception_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=True, data_dir=Path(tmp), workspace_root=Path(tmp))
            result = BugAnalysisRunner(config).run_bug_analysis(
                BugRequest(
                    bug_url="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722",
                    prompt="总结当前感知数据",
                    raw_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 总结当前感知数据",
                    triggered=True,
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["analysis_kinds"], ["perception"])
        self.assertIn("bug_perception_data_summary.html", result.message)

    def test_bug_analysis_selects_startup_log_nearest_fault_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = BugAnalysisRunner(BridgeConfig(dry_run=True))
            root = Path(tmp) / "Log"
            target_dir = root / "log0" / "app" / "com.xiaopeng.montecarlo"
            target_dir.mkdir(parents=True, exist_ok=True)
            older = target_dir / "main_2026-05-11_10-00.alog"
            newer = target_dir / "main_2026-05-11_11-00.alog"
            older.write_text("", encoding="utf-8")
            newer.write_text("", encoding="utf-8")

            selected = runner._select_startup_input(root, "2026-05-11 11:30")

        self.assertEqual(selected, newer)

    def test_perception_summary_dry_run_returns_planned_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=True, data_dir=Path(tmp), workspace_root=Path(tmp))
            event = LarkEvent(
                event_id="evt_1",
                message_id="om_1",
                chat_id="oc_1",
                chat_type="group",
                sender_id="ou_1",
                message_type="text",
                content="",
            )
            result = PerceptionSummaryRunner(config).run_summary(
                PerceptionSummaryRequest(
                    prompt="总结当前感知数据",
                    raw_text="/perception-summary 总结当前感知数据 file_abc",
                    triggered=True,
                    resources=[DownloadResource(kind="file", value="file_abc")],
                ),
                event=event,
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "perception_summary")
        self.assertIn("perception_data_summary.html", result.message)


if __name__ == "__main__":
    unittest.main()
