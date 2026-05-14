from pathlib import Path
import json
import subprocess
import tempfile
import unittest
from unittest import mock

from lark_agent_bridge.agents import (
    BugAnalysisRunner,
    ClaudeSkillRunner,
    IntentAnalysisRunner,
    OmlxChatClient,
    PerceptionSummaryRunner,
)
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

    def test_intent_analysis_runner_parses_claude_json_response(self):
        config = BridgeConfig(dry_run=False)
        config.intent_analysis.enabled = True
        config.intent_analysis.provider = "claude"
        config.intent_analysis.command = "claude"
        runner = IntentAnalysisRunner(config)
        completed = subprocess.CompletedProcess(
            args=["claude"],
            returncode=0,
            stdout=json.dumps(
                {
                    "route": "analysis_followup",
                    "followup_action": "continue_agent",
                    "context_source": "explicit",
                    "confidence": "high",
                    "reason": "同一个 bug 会话追问",
                },
                ensure_ascii=False,
            ),
            stderr="",
        )
        with mock.patch("subprocess.run", return_value=completed):
            decision = runner.classify(
                event=LarkEvent(
                    event_id="evt_1",
                    message_id="om_1",
                    chat_id="oc_1",
                    chat_type="group",
                    sender_id="ou_1",
                    message_type="text",
                    content="@bot 继续分析",
                ),
                route_content="继续分析",
            )

        self.assertEqual(decision.route, "analysis_followup")
        self.assertEqual(decision.followup_action, "continue_agent")
        self.assertEqual(decision.context_source, "explicit")
        self.assertEqual(decision.confidence, "high")

    def test_intent_analysis_runner_uses_bug_provider_defaults_for_codex(self):
        config = BridgeConfig(dry_run=False)
        config.intent_analysis.enabled = True
        config.bug_analysis.provider = "codex"
        config.bug_analysis.command = "codex"
        runner = IntentAnalysisRunner(config)
        command, output_path = runner._build_command("route this message")

        self.assertEqual(command[0:2], ["codex", "exec"])

    def test_intent_analysis_falls_back_to_claude_when_codex_unavailable(self):
        config = BridgeConfig(dry_run=False)
        config.intent_analysis.enabled = True
        config.intent_analysis.provider = "codex"
        config.intent_analysis.command = "codex"
        runner = IntentAnalysisRunner(config)
        response_path = Path("/tmp/intent-response.json")
        fallback_decision = '{"route":"chat","followup_action":"none","context_source":"none","confidence":"high","reason":"fallback"}'

        with (
            mock.patch.object(runner, "_build_command", return_value=(["codex", "exec"], response_path)),
            mock.patch.object(runner, "_fallback_intent_invocation", return_value=(["claude", "--print"], None)) as fallback_mock,
            mock.patch("subprocess.run", side_effect=[OSError("codex missing"), subprocess.CompletedProcess(args=["claude"], returncode=0, stdout=fallback_decision, stderr="")]),
        ):
            decision = runner.classify(
                event=LarkEvent(
                    event_id="evt_1",
                    message_id="om_1",
                    chat_id="oc_1",
                    chat_type="group",
                    sender_id="ou_1",
                    message_type="text",
                    content="@bot 帮我解释一下什么是 token？",
                ),
                route_content="帮我解释一下什么是 token？",
            )

        self.assertEqual(decision.route, "chat")
        self.assertEqual(decision.reason, "fallback")
        self.assertTrue(fallback_mock.called)

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
            self.assertIn("调查3D启动时序", result.details["user_request_text"])
            self.assertTrue(Path(result.details["agent_request_file"]).exists())

    def test_bug_analysis_dry_run_emits_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=True, data_dir=Path(tmp), workspace_root=Path(tmp))
            progress_events = []
            result = BugAnalysisRunner(config).run_bug_analysis(
                BugRequest(
                    bug_url="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722",
                    prompt="调查3D启动时序",
                    raw_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查3D启动时序",
                    triggered=True,
                ),
                progress_callback=progress_events.append,
            )

        self.assertTrue(result.success)
        self.assertEqual(progress_events[0]["stage"], "bug_job_created")
        self.assertEqual(progress_events[-1]["stage"], "bug_dry_run_planned")

    def test_bug_agent_summary_command_for_codex_includes_full_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=True, data_dir=Path(tmp), workspace_root=Path(tmp))
            config.bug_analysis.provider = "codex"
            config.bug_analysis.command = "codex"
            runner = BugAnalysisRunner(config)
            request_artifact = Path(tmp) / "bug_agent_request.md"
            metadata_path = Path(tmp) / "bug_metadata.md"
            output_path = Path(tmp) / "bug_agent_summary.md"
            invocation = runner._build_bug_agent_summary_command(
                request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 分析启动和卡顿",
                request_artifact=request_artifact,
                metadata_path=metadata_path,
                output_path=output_path,
            )
            command = invocation["command"]

        self.assertEqual(command[0:2], ["codex", "exec"])
        self.assertIn("--json", command)
        self.assertIn("--output-last-message", command)
        self.assertIn("分析启动和卡顿", command[-1])

    def test_bug_agent_summary_command_for_codex_resume_uses_session_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=True, data_dir=Path(tmp), workspace_root=Path(tmp))
            config.bug_analysis.provider = "codex"
            config.bug_analysis.command = "codex"
            runner = BugAnalysisRunner(config)
            invocation = runner._build_bug_agent_summary_command(
                request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 分析启动和卡顿",
                request_artifact=Path(tmp) / "bug_agent_request.md",
                metadata_path=Path(tmp) / "bug_metadata.md",
                output_path=Path(tmp) / "bug_agent_summary.md",
                provider_session_id="sess_123",
                followup_text="修正问题时间为23:12，请继续分析",
                previous_summary_path=Path(tmp) / "old_summary.md",
            )

        self.assertEqual(invocation["command"][0:3], ["codex", "exec", "resume"])

    def test_bug_agent_summary_prompt_biases_to_fault_time_focus_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=True, data_dir=Path(tmp), workspace_root=Path(tmp))
            runner = BugAnalysisRunner(config)
            request_artifact = Path(tmp) / "bug_agent_request.md"
            metadata_path = Path(tmp) / "bug_metadata.md"
            request_artifact.write_text("# req\n- 用户原始请求\n", encoding="utf-8")
            metadata_path.write_text("# meta\n- 故障时间: 2026-05-11 23:10\n- 主会话 PID: 2577\n", encoding="utf-8")
            prompt = runner._build_bug_agent_summary_prompt(
                request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 分析启动卡顿，问题时间 2026-05-11 23:10",
                request_artifact=request_artifact,
                metadata_path=metadata_path,
            )

        self.assertIn("主会话", prompt)
        self.assertIn("不要展开无关会话", prompt)
        self.assertIn("故障时间: 2026-05-11 23:10", prompt)
        self.assertIn("主会话 PID: 2577", prompt)

    def test_bug_agent_summary_command_for_claude_disables_tools_and_embeds_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=True, data_dir=Path(tmp), workspace_root=Path(tmp))
            config.bug_analysis.provider = "claude"
            config.bug_analysis.command = "claude"
            runner = BugAnalysisRunner(config)
            request_artifact = Path(tmp) / "request.md"
            metadata_path = Path(tmp) / "metadata.md"
            output_path = Path(tmp) / "summary.md"
            request_artifact.write_text("request-body", encoding="utf-8")
            metadata_path.write_text("metadata-body", encoding="utf-8")

            invocation = runner._build_bug_agent_summary_command(
                request_text="分析启动卡顿",
                request_artifact=request_artifact,
                metadata_path=metadata_path,
                output_path=output_path,
            )

        self.assertIn("--tools", invocation["command"])
        self.assertIn("", invocation["command"])
        self.assertTrue(any("request-body" in part for part in invocation["command"]))
        self.assertTrue(any("metadata-body" in part for part in invocation["command"]))

    def test_bug_agent_summary_falls_back_to_claude_when_codex_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp), workspace_root=Path(tmp))
            config.bug_analysis.provider = "codex"
            config.bug_analysis.command = "codex"
            runner = BugAnalysisRunner(config)
            output_path = Path(tmp) / "bug_agent_summary.md"
            request_artifact = Path(tmp) / "request.md"
            metadata_path = Path(tmp) / "metadata.md"
            request_artifact.write_text("request", encoding="utf-8")
            metadata_path.write_text("metadata", encoding="utf-8")

            with (
                mock.patch("subprocess.run", side_effect=[OSError("codex missing"), subprocess.CompletedProcess(args=["claude"], returncode=0, stdout="fallback summary", stderr="")]),
            ):
                result = runner._run_bug_agent_summary(
                    request_text="分析启动卡顿",
                    request_artifact=request_artifact,
                    metadata_path=metadata_path,
                    output_path=output_path,
                    progress_callback=None,
                    timeout=30,
                )

        self.assertEqual(result["message"], "fallback summary")
        self.assertEqual(result["provider"], "claude")

    def test_bug_analysis_classifies_startup_request(self):
        runner = BugAnalysisRunner(BridgeConfig(dry_run=True))

        plan = runner.classify_request(
            prompt_text="调查3D启动时序",
            title="",
            description="",
        )

        self.assertEqual(plan.kind, "startup")
        self.assertIsNone(plan.signal_code)

    def test_bug_analysis_startup_command_passes_target_time(self):
        runner = BugAnalysisRunner(BridgeConfig(dry_run=True))
        command = runner.build_command(
            plan=__import__("lark_agent_bridge.agents", fromlist=["BugAnalysisPlan"]).BugAnalysisPlan(kind="startup"),
            input_path=Path("/tmp/input"),
            html_path=Path("/tmp/out.html"),
            json_path=Path("/tmp/out.json"),
            analysis_dir=Path("/tmp/startup"),
            target_time="2026-05-11 23:10",
        )

        self.assertIn("--target-time", command)
        self.assertIn("2026-05-11 23:10", command)

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

    def test_bug_analysis_infers_startup_from_bug_context_when_prompt_only_mentions_stuck(self):
        runner = BugAnalysisRunner(BridgeConfig(dry_run=True))

        plans = runner.classify_requests(
            prompt_text="分析3D卡顿分析",
            title="【公测反馈】【6.2.0】【F01】升级后打不开sr界面-SB142730",
            description=(
                "23.10升级完后上车，大屏先是黑屏只有logo，可挂档，"
                "打不开360地图sr等功能。后面都加载出来后，发现账号都被退登了。"
            ),
        )

        self.assertEqual([plan.kind for plan in plans], ["startup", "stuck"])

    def test_bug_reanalysis_time_correction_reuses_previous_date(self):
        runner = BugAnalysisRunner(BridgeConfig(dry_run=True))

        target_time = runner._extract_followup_fault_time(
            "修复问题时间 23:12分 重新分析下",
            reference_text="故障时间: 2026-05-11 23:10",
        )

        self.assertEqual(target_time, "2026-05-11 23:12")

    def test_bug_analysis_extract_fault_time_normalizes_fullwidth_colon(self):
        runner = BugAnalysisRunner(BridgeConfig(dry_run=True))

        target_time, note = runner._extract_fault_time("", "问题时间: 2026-5-11 23：10")

        self.assertEqual(target_time, "2026-05-11 23:10")
        self.assertEqual(note, "从缺陷描述提取")

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
        self.assertIn("bug_startup_stuck_report.html", result.message)

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

    def test_bug_analysis_select_log_input_prefers_extracted_logs_over_invalid_zip_attachment(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = BugAnalysisRunner(BridgeConfig(dry_run=True))
            bug_dir = Path(tmp)
            attachments_dir = bug_dir / "attachments"
            logs_dir = bug_dir / "logs" / "data" / "Log" / "log0" / "app" / "com.xiaopeng.montecarlo"
            attachments_dir.mkdir(parents=True, exist_ok=True)
            logs_dir.mkdir(parents=True, exist_ok=True)
            (attachments_dir / "L1NNSGMA3SB142730log1.zip").write_bytes(b"not-a-real-zip")
            (bug_dir / "logs" / "prop.txt").write_text("prop", encoding="utf-8")
            (logs_dir / "main_2026-05-11_23-00.alog").write_text("", encoding="utf-8")

            selected = runner._select_log_input(
                bug_dir,
                fetched={"attachments": [{"name": "L1NNSGMA3SB142730log1.zip"}]},
            )

        self.assertEqual(selected, bug_dir / "logs")

    def test_bug_analysis_select_log_input_skips_invalid_zip_attachment(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = BugAnalysisRunner(BridgeConfig(dry_run=True))
            bug_dir = Path(tmp)
            attachments_dir = bug_dir / "attachments"
            logs_dir = bug_dir / "logs"
            attachments_dir.mkdir(parents=True, exist_ok=True)
            logs_dir.mkdir(parents=True, exist_ok=True)
            (attachments_dir / "L1NNSGMA3SB142730log1.zip").write_bytes(b"not-a-real-zip")
            (logs_dir / "prop.txt").write_text("prop", encoding="utf-8")
            (logs_dir / "dfx.txt").write_text("{}", encoding="utf-8")

            selected = runner._select_log_input(
                bug_dir,
                fetched={"attachments": [{"name": "L1NNSGMA3SB142730log1.zip"}]},
            )

        self.assertIsNone(selected)

    def test_bug_analysis_prepare_log_input_rejects_invalid_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = BugAnalysisRunner(BridgeConfig(dry_run=True))
            invalid_zip = Path(tmp) / "bad.zip"
            invalid_zip.write_bytes(b"not-a-real-zip")

            with self.assertRaisesRegex(RuntimeError, "不是有效 zip"):
                runner._prepare_log_input(invalid_zip)

    def test_bug_analysis_startup_uses_prepared_directory_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp), workspace_root=Path(tmp))
            runner = BugAnalysisRunner(config)
            log_root = Path(tmp) / "logs"
            log_root.mkdir(parents=True, exist_ok=True)
            startup_html = Path(tmp) / "bug_3d_startup_report.html"
            startup_json = Path(tmp) / "bug_3d_startup_report.json"
            analysis_inputs = []

            def fake_run_json_command(command, timeout):
                if "check-env" in command:
                    return {"meegle_installed": True, "auth_ok": True}
                if "resolve-url" in command:
                    return {"project_key": "xpfailuremgmt", "work_item_id": "6987292722"}
                if "fetch-data" in command:
                    return {"title": "启动问题", "description": "问题时间: 2026-05-11 23:10"}
                if command[:3] == ["meegle", "workitem", "get"]:
                    return {"data": {}}
                if "download" in command:
                    return {"downloaded": []}
                raise AssertionError(f"unexpected command: {command}")

            def fake_run_analysis(*, plan, input_path, html_path, json_path, analysis_dir, timeout, target_time):
                analysis_inputs.append((plan.kind, input_path, target_time))
                html_path.write_text("<html>startup</html>", encoding="utf-8")
                json_path.write_text("{}", encoding="utf-8")
                return subprocess.CompletedProcess(args=["python3"], returncode=0, stdout="", stderr="")

            with (
                mock.patch.object(runner, "_run_json_command", side_effect=fake_run_json_command),
                mock.patch.object(runner, "_load_option_map", return_value={}),
                mock.patch.object(runner, "_select_log_input", return_value=log_root),
                mock.patch.object(runner, "_prepare_log_input", return_value=log_root),
                mock.patch.object(runner, "_extract_fault_time", return_value=("2026-05-11 23:10", "")),
                mock.patch.object(runner, "_run_analysis", side_effect=fake_run_analysis),
                mock.patch.object(runner, "_build_bug_outputs", return_value=("metadata", "summary")),
                mock.patch.object(
                    runner,
                    "_build_combined_report_artifacts",
                    return_value={"html_path": startup_html, "json_path": startup_json, "summary": "summary"},
                ),
                mock.patch.object(
                    runner,
                    "_run_bug_agent_summary",
                    return_value={"message": "", "command": None, "error": "", "provider": "", "session_id": "", "resumed": False},
                ),
            ):
                runner.run_bug_analysis(
                    BugRequest(
                        bug_url="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722",
                        prompt="调查3D启动时序",
                        raw_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查3D启动时序",
                        triggered=True,
                    )
                )

        self.assertEqual(analysis_inputs[0][0], "startup")
        self.assertEqual(analysis_inputs[0][1], log_root)
        self.assertEqual(analysis_inputs[0][2], "2026-05-11 23:10")

    def test_build_combined_report_artifacts_for_startup_and_stuck(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = BugAnalysisRunner(BridgeConfig(dry_run=True, workspace_root=Path("/Users/zhuyl/Documents/workspace")))
            output_dir = Path(tmp)
            startup_json = output_dir / "bug_3d_startup_report.json"
            stuck_json = output_dir / "bug_3d_stuck_report.json"
            startup_html = output_dir / "bug_3d_startup_report.html"
            stuck_html = output_dir / "bug_3d_stuck_report.html"
            startup_html.write_text("<html>startup</html>", encoding="utf-8")
            stuck_html.write_text("<html>stuck</html>", encoding="utf-8")
            startup_json.write_text(
                json.dumps(
                    {
                        "verdict": {
                            "severity": "red",
                            "message": "启动链路异常",
                            "issues": [{"sev": "red", "title": "启动异常", "detail": "首帧未回"}],
                        },
                        "focus_session_pid": 2577,
                        "focus_session_index": 1,
                        "focus_reason": "按目标时间锁定",
                        "boot_relation": {"is_near_boot": True, "note": "刚启动阶段"},
                        "ig_context": {"after": {"timestamp": "2026-05-11T23:06:28.893", "value": 0}},
                        "system_load": {
                            "timestamp": "2026-05-11T23:06:35.164",
                            "total_cpu": 75,
                            "user_cpu": 24,
                            "system_cpu": 37,
                            "iow_cpu": 10,
                            "process_pid": 2577,
                            "process_cpu": 0.0,
                            "process_mem_rss_kb": 221596,
                            "process_io_read_kb": 2540,
                            "process_io_write_kb": 2540,
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            stuck_json.write_text(
                json.dumps(
                    {
                        "verdict": {"verdict_sev": "yellow", "verdict_msg": "卡顿分析已生成"},
                        "target_verdict": {"sev": "yellow", "message": "目标时间窗卡顿明显"},
                        "target_context": {"target": "2026-05-11 23:10"},
                        "power_context": {"render_anomaly_context": {"category": "unknown"}},
                        "app_pid_filter": {"selected_pid": 2577},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            artifacts = runner._build_combined_report_artifacts(
                plans=[
                    __import__("lark_agent_bridge.agents", fromlist=["BugAnalysisPlan"]).BugAnalysisPlan(kind="startup"),
                    __import__("lark_agent_bridge.agents", fromlist=["BugAnalysisPlan"]).BugAnalysisPlan(kind="stuck"),
                ],
                prompt_text="分析3D启动卡顿",
                fault_time="2026-05-11 23:10",
                output_dir=output_dir,
                html_paths=[startup_html, stuck_html],
                report_jsons={"startup": startup_json, "stuck": stuck_json},
                selected_input=Path("/tmp/logs"),
            )

            self.assertIsNotNone(artifacts)
            self.assertTrue(Path(artifacts["html_path"]).exists())
            self.assertTrue(Path(artifacts["json_path"]).exists())
            self.assertIn("3D启动卡顿综合报告", Path(artifacts["html_path"]).read_text(encoding="utf-8"))

    def test_bug_analysis_combined_route_uploads_only_merged_html(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp), workspace_root=Path(tmp))
            runner = BugAnalysisRunner(config)
            log_root = Path(tmp) / "logs"
            log_root.mkdir(parents=True, exist_ok=True)
            combined_html = Path(tmp) / "bug_startup_stuck_report.html"
            combined_json = Path(tmp) / "bug_startup_stuck_report.json"
            combined_html.write_text("<html>merged</html>", encoding="utf-8")
            combined_json.write_text("{}", encoding="utf-8")

            def fake_run_json_command(command, timeout):
                if "check-env" in command:
                    return {"meegle_installed": True, "auth_ok": True}
                if "resolve-url" in command:
                    return {"project_key": "xpfailuremgmt", "work_item_id": "6987292722"}
                if "fetch-data" in command:
                    return {"title": "启动卡顿问题", "description": "2026-05-11 23:10 启动卡顿"}
                if command[:3] == ["meegle", "workitem", "get"]:
                    return {"data": {}}
                if "download" in command:
                    return {"downloaded": []}
                raise AssertionError(f"unexpected command: {command}")

            def fake_run_analysis(*, plan, input_path, html_path, json_path, analysis_dir, timeout, target_time):
                html_path.write_text(f"<html>{plan.kind}</html>", encoding="utf-8")
                json_path.write_text("{}", encoding="utf-8")
                return subprocess.CompletedProcess(args=["python3"], returncode=0, stdout="", stderr="")

            with (
                mock.patch.object(runner, "_run_json_command", side_effect=fake_run_json_command),
                mock.patch.object(runner, "_load_option_map", return_value={}),
                mock.patch.object(runner, "_select_log_input", return_value=log_root),
                mock.patch.object(runner, "_prepare_log_input", return_value=log_root),
                mock.patch.object(runner, "_extract_fault_time", return_value=("2026-05-11 23:10", "")),
                mock.patch.object(runner, "_run_analysis", side_effect=fake_run_analysis),
                mock.patch.object(runner, "_build_bug_outputs", return_value=("metadata", "summary")),
                mock.patch.object(
                    runner,
                    "_build_combined_report_artifacts",
                    return_value={"html_path": combined_html, "json_path": combined_json},
                ),
                mock.patch.object(
                    runner,
                    "_run_bug_agent_summary",
                    return_value={"message": "", "command": None, "error": "", "provider": "", "session_id": "", "resumed": False},
                ),
            ):
                result = runner.run_bug_analysis(
                    BugRequest(
                        bug_url="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722",
                        prompt="分析启动和卡顿",
                        raw_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 分析启动和卡顿",
                        triggered=True,
                    )
                )

        self.assertTrue(result.success)
        self.assertEqual(result.details["analysis_kinds"], ["startup", "stuck"])
        self.assertEqual(result.details["files_to_send"], [combined_html])

    def test_bug_reanalysis_uses_agent_summary_and_persisted_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp), workspace_root=Path(tmp))
            runner = BugAnalysisRunner(config)
            job_dir = Path(tmp) / "jobs" / "job_1"
            output_dir = job_dir / "output"
            output_dir.mkdir(parents=True, exist_ok=True)
            prepared_input = Path(tmp) / "logs"
            prepared_input.mkdir(parents=True, exist_ok=True)
            previous_summary = output_dir / "bug_agent_summary.md"
            previous_summary.write_text("old summary", encoding="utf-8")
            combined_html = output_dir / "bug_startup_stuck_report.html"
            combined_json = output_dir / "bug_startup_stuck_report.json"
            startup_json = output_dir / "bug_3d_startup_report.json"
            startup_html = output_dir / "bug_3d_startup_report.html"
            stuck_json = output_dir / "bug_3d_stuck_report.json"
            stuck_html = output_dir / "bug_3d_stuck_report.html"
            stuck_html.write_text("<html>stuck</html>", encoding="utf-8")
            stuck_json.write_text("{}", encoding="utf-8")
            analysis_inputs = []

            def fake_run_analysis(*, plan, input_path, html_path, json_path, analysis_dir, timeout, target_time):
                analysis_inputs.append((plan.kind, input_path, target_time))
                html_path.write_text(f"<html>{plan.kind}</html>", encoding="utf-8")
                json_path.write_text("{}", encoding="utf-8")
                return subprocess.CompletedProcess(args=["python3"], returncode=0, stdout="", stderr="")

            previous_session = {
                "job_id": "job_1",
                "job_dir": str(job_dir),
                "details": {
                    "analysis_kinds": ["startup", "stuck"],
                    "prepared_log_input": str(prepared_input),
                    "selected_log_input": str(prepared_input / "selected.alog"),
                    "user_request_text": "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 分析启动和卡顿",
                    "agent_summary_file": str(previous_summary),
                    "agent_summary_session_id": "sess_123",
                },
            }
            previous_context = mock.Mock(
                request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 分析启动和卡顿",
                summary_text="上一轮摘要",
                report_excerpt="上一轮摘录",
                history=[{"role": "user", "content": "第一次分析"}, {"role": "assistant", "content": "第一次结论"}],
            )

            with (
                mock.patch.object(runner, "_run_analysis", side_effect=fake_run_analysis),
                mock.patch.object(
                    runner,
                    "_build_combined_report_artifacts",
                    return_value={"html_path": combined_html, "json_path": combined_json, "summary": "combined summary"},
                ),
                mock.patch.object(
                    runner,
                    "_run_bug_agent_summary",
                    return_value={
                        "message": "agent continued",
                        "command": ["codex", "exec", "resume"],
                        "error": "",
                        "provider": "codex",
                        "session_id": "sess_123",
                        "resumed": True,
                    },
                ) as summary_mock,
            ):
                result = runner.run_bug_reanalysis(
                    followup_text="修正问题时间为 23:12分 重新分析",
                    previous_context=previous_context,
                    previous_session=previous_session,
                )

        self.assertTrue(result.success)
        self.assertEqual(result.message, "agent continued")
        self.assertEqual(result.details["agent_summary_session_id"], "sess_123")
        self.assertTrue(result.details["agent_summary_resumed"])
        self.assertEqual(result.details["rerun_analysis_kinds"], ["startup"])
        self.assertEqual(result.details["reused_analysis_kinds"], ["stuck"])
        self.assertEqual(analysis_inputs[0][0], "startup")
        self.assertEqual(analysis_inputs[0][1], prepared_input)
        self.assertEqual(analysis_inputs[0][2], "23:12")
        self.assertEqual(summary_mock.call_args.kwargs["provider_session_id"], "sess_123")
        self.assertEqual(summary_mock.call_args.kwargs["previous_summary_path"], previous_summary)

    def test_bug_agent_followup_resumes_saved_agent_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp), workspace_root=Path(tmp))
            runner = BugAnalysisRunner(config)
            job_dir = Path(tmp) / "jobs" / "job_1"
            output_dir = job_dir / "output"
            output_dir.mkdir(parents=True, exist_ok=True)
            prepared_input = Path(tmp) / "logs"
            prepared_input.mkdir(parents=True, exist_ok=True)
            selected_input = prepared_input / "selected.alog"
            selected_input.write_text("log", encoding="utf-8")
            previous_summary = output_dir / "bug_agent_summary.md"
            previous_summary.write_text("old summary", encoding="utf-8")
            combined_html = output_dir / "bug_startup_stuck_report.html"
            combined_html.write_text("<html>combined</html>", encoding="utf-8")
            combined_json = output_dir / "bug_startup_stuck_report.json"
            combined_json.write_text("{}", encoding="utf-8")
            previous_session = {
                "job_id": "job_1",
                "job_dir": str(job_dir),
                "details": {
                    "analysis_kinds": ["startup", "stuck"],
                    "prepared_log_input": str(prepared_input),
                    "selected_log_input": str(selected_input),
                    "user_request_text": "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 分析启动和卡顿",
                    "agent_summary_file": str(previous_summary),
                    "agent_summary_session_id": "sess_123",
                    "combined_report_html": str(combined_html),
                    "combined_report_json": str(combined_json),
                },
            }
            previous_context = mock.Mock(
                request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 分析启动和卡顿",
                summary_text="上一轮摘要",
                report_excerpt="上一轮摘录",
                report_url="http://127.0.0.1:8765/reports/1/index.html",
                history=[{"role": "user", "content": "第一次分析"}, {"role": "assistant", "content": "第一次结论"}],
            )
            with mock.patch.object(
                runner,
                "_run_bug_agent_summary",
                return_value={
                    "message": "agent followup reply",
                    "command": ["codex", "exec", "resume"],
                    "error": "",
                    "provider": "codex",
                    "session_id": "sess_123",
                    "resumed": True,
                },
            ) as summary_mock:
                result = runner.run_bug_agent_followup(
                    followup_text="用你之前下载下来日志搜索 关键字看 卡顿skill 将23:10到23:15之间的系统卡顿报告发出来",
                    previous_context=previous_context,
                    previous_session=previous_session,
                )
                request_text = Path(result.details["agent_request_file"]).read_text(encoding="utf-8")
                metadata_text = Path(result.details["followup_metadata_file"]).read_text(encoding="utf-8")

        self.assertTrue(result.success)
        self.assertEqual(result.message, "agent followup reply")
        self.assertEqual(result.details["mode"], "bug_agent_followup")
        self.assertEqual(result.details["agent_summary_session_id"], "sess_123")
        self.assertTrue(result.details["agent_summary_resumed"])
        self.assertIn("卡顿skill", request_text)
        self.assertIn(str(prepared_input), metadata_text)
        self.assertIn(str(combined_html), metadata_text)
        self.assertEqual(summary_mock.call_args.kwargs["provider_session_id"], "sess_123")
        self.assertEqual(summary_mock.call_args.kwargs["previous_summary_path"], previous_summary)

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
