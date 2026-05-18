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
from lark_agent_bridge.reporting import ReportComposition


class AgentTests(unittest.TestCase):
    def setUp(self):
        self._bug_decision_patcher = mock.patch.object(
            BugAnalysisRunner,
            "_run_bug_decision_agent",
            return_value=(None, ""),
        )
        self._bug_decision_patcher.start()

    def tearDown(self):
        self._bug_decision_patcher.stop()

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

    def test_extract_bug_agent_usage_prefers_delta_over_cumulative(self):
        runner = BugAnalysisRunner(BridgeConfig(dry_run=True))
        output = "\n".join(
            [
                json.dumps({"usage": {"input_tokens": 1000000, "output_tokens": 10000, "total_tokens": 1010000}}, ensure_ascii=False),
                json.dumps({"delta_usage": {"input_tokens": 1200, "output_tokens": 80, "total_tokens": 1280}}, ensure_ascii=False),
            ]
        )

        usage, scope = runner._extract_bug_agent_usage("codex", output, "")

        self.assertEqual(usage, {"input_tokens": 1200, "output_tokens": 80, "total_tokens": 1280})
        self.assertEqual(scope, "delta")

    def test_build_agent_runtime_html_formats_token_units(self):
        runner = BugAnalysisRunner(BridgeConfig(dry_run=True))

        html = runner._build_agent_runtime_html(
            {
                "provider": "codex",
                "usage": {"input_tokens": 1197553, "output_tokens": 13026, "total_tokens": 1210579},
                "duration_seconds": 307.0,
                "session_id": "sess-1",
                "resumed": False,
            },
            total_duration_seconds=370.5,
        )

        self.assertIn("累计 Agent Token", html)
        self.assertIn("1.20M / 13.03K / 1.21M", html)

    def test_build_agent_runtime_html_marks_cumulative_usage(self):
        runner = BugAnalysisRunner(BridgeConfig(dry_run=True))

        html = runner._build_agent_runtime_html(
            {
                "provider": "codex",
                "usage": {"input_tokens": 1197553, "output_tokens": 13026, "total_tokens": 1210579},
                "duration_seconds": 307.0,
                "session_id": "sess-1",
                "resumed": False,
                "usage_scope": "cumulative",
            },
            total_duration_seconds=370.5,
        )

        self.assertIn("累计 Agent Token", html)

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

    def test_bug_agent_summary_followup_prompt_embeds_compact_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=True, data_dir=Path(tmp), workspace_root=Path(tmp))
            runner = BugAnalysisRunner(config)
            request_artifact = Path(tmp) / "bug_agent_followup_request.md"
            metadata_path = Path(tmp) / "bug_agent_followup_metadata.md"
            previous_summary = Path(tmp) / "bug_agent_summary.md"
            request_artifact.write_text("request\n" + "R" * 10000, encoding="utf-8")
            metadata_path.write_text("metadata\n" + "M" * 10000, encoding="utf-8")
            previous_summary.write_text("summary\n" + "S" * 10000, encoding="utf-8")

            prompt = runner._build_bug_agent_summary_prompt(
                request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 分析主题",
                request_artifact=request_artifact,
                metadata_path=metadata_path,
                followup_text="问题时刻系统主题是什么",
                previous_summary_path=previous_summary,
            )

        self.assertIn("续聊性能约束", prompt)
        self.assertIn("Bug Agent Follow-up Request", prompt)
        self.assertIn("Bug Follow-up Metadata", prompt)
        self.assertLess(len(prompt), 18000)
        self.assertNotIn("S" * 4000, prompt)

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

    def test_bug_agent_summary_prompt_for_new_bug_forbids_followup_language(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=True, data_dir=Path(tmp), workspace_root=Path(tmp))
            runner = BugAnalysisRunner(config)
            request_artifact = Path(tmp) / "bug_agent_request.md"
            metadata_path = Path(tmp) / "bug_metadata.md"
            request_artifact.write_text("# req\n", encoding="utf-8")
            metadata_path.write_text("# meta\n", encoding="utf-8")

            prompt = runner._build_bug_agent_summary_prompt(
                request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6986570719 分析5月19日的信号链路",
                request_artifact=request_artifact,
                metadata_path=metadata_path,
            )

        self.assertIn("本次是全新 bug 分析请求，不是续聊/修正", prompt)
        self.assertNotIn("如果这是续聊/修正", prompt)

    def test_bug_agent_summary_prompt_requires_structured_summary_sections(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=True, data_dir=Path(tmp), workspace_root=Path(tmp))
            runner = BugAnalysisRunner(config)
            request_artifact = Path(tmp) / "bug_agent_request.md"
            metadata_path = Path(tmp) / "bug_metadata.md"
            request_artifact.write_text("# req\n", encoding="utf-8")
            metadata_path.write_text("# meta\n", encoding="utf-8")

            prompt = runner._build_bug_agent_summary_prompt(
                request_text="分析 bug",
                request_artifact=request_artifact,
                metadata_path=metadata_path,
            )

        for heading in ["结论摘要", "关键证据", "最可能原因", "待确认项", "建议动作"]:
            self.assertIn(heading, prompt)
        self.assertNotIn("rawTmcData", prompt)

    def test_bug_agent_summary_command_for_claude_enables_repo_read_tools_and_embeds_files(self):
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

        self.assertIn("--allowedTools", invocation["command"])
        self.assertIn("Read,Grep,Glob,LS", invocation["command"])
        self.assertIn("--add-dir", invocation["command"])
        self.assertTrue(any("request-body" in part for part in invocation["command"]))
        self.assertTrue(any("metadata-body" in part for part in invocation["command"]))
        self.assertTrue(any(str(config.workspace_root.resolve()) == part for part in invocation["command"]))

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

    def test_bug_agent_summary_extracts_token_usage_from_codex_json_output(self):
        runner = BugAnalysisRunner(BridgeConfig(dry_run=True))
        output = "\n".join(
            [
                json.dumps({"type": "response.started"}),
                json.dumps(
                    {
                        "type": "response.completed",
                        "response": {
                            "usage": {
                                "input_tokens": 123,
                                "output_tokens": 45,
                                "total_tokens": 168,
                            }
                        },
                    }
                ),
            ]
        )

        usage, scope = runner._extract_bug_agent_usage("codex", output, "")

        self.assertEqual(
            usage,
            {
                "input_tokens": 123,
                "output_tokens": 45,
                "total_tokens": 168,
            },
        )
        self.assertEqual(scope, "cumulative")

    def test_bug_analysis_metadata_appends_agent_runtime_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp), workspace_root=Path(tmp))
            runner = BugAnalysisRunner(config)
            log_root = Path(tmp) / "logs"
            log_root.mkdir(parents=True, exist_ok=True)
            report_html = Path(tmp) / "bug_signal_chain_report.html"
            report_json = Path(tmp) / "bug_signal_chain_report.json"
            report_html.write_text("<html>signal</html>", encoding="utf-8")
            report_json.write_text("{}", encoding="utf-8")

            def fake_run_json_command(command, timeout):
                if "check-env" in command:
                    return {"meegle_installed": True, "auth_ok": True}
                if "resolve-url" in command:
                    return {"project_key": "xpfailuremgmt", "work_item_id": "6986570719"}
                if "fetch-data" in command:
                    return {
                        "title": "信号问题",
                        "status": "处理中",
                        "create_time": "2026-05-16 10:00",
                        "create_by": "tester",
                        "fields": {},
                        "attachments": [],
                    }
                if command[:3] == ["meegle", "workitem", "get"]:
                    return {"data": {}}
                raise AssertionError(f"unexpected command: {command}")

            with (
                mock.patch.object(runner, "_run_json_command", side_effect=fake_run_json_command),
                mock.patch.object(runner, "_load_option_map", return_value={}),
                mock.patch.object(runner, "_download_bug_attachments", return_value={"downloaded": [], "unzipped": [], "errors": [], "skipped": [], "ok": True}),
                mock.patch.object(runner, "_select_log_input", return_value=log_root),
                mock.patch.object(runner, "_prepare_log_input", return_value=log_root),
                mock.patch.object(runner, "_extract_fault_time", return_value=("2026-05-16 10:01", "从缺陷描述提取")),
                mock.patch.object(
                    runner,
                    "_run_analysis",
                    side_effect=lambda **kwargs: (
                        kwargs["html_path"].write_text("<html>signal</html>", encoding="utf-8"),
                        kwargs["json_path"].write_text("{}", encoding="utf-8"),
                        subprocess.CompletedProcess(args=["python3"], returncode=0, stdout="", stderr=""),
                    )[-1],
                ),
                mock.patch.object(
                    runner,
                    "_build_bug_outputs",
                    return_value=("# Bug Metadata\n\n- 原始字段: `ok`\n", "summary"),
                ),
                mock.patch.object(runner, "_build_combined_report_artifacts", return_value=None),
                mock.patch.object(
                    runner,
                    "_run_bug_agent_summary",
                    return_value={
                        "message": "agent summary",
                        "command": ["codex", "exec"],
                        "error": "",
                        "provider": "codex",
                        "session_id": "sess_123",
                        "resumed": False,
                        "duration_seconds": 12.5,
                        "usage": {
                            "input_tokens": 321,
                            "output_tokens": 54,
                            "total_tokens": 375,
                        },
                    },
                ),
            ):
                result = runner.run_bug_analysis(
                    BugRequest(
                        bug_url="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6986570719",
                        prompt="分析信号链路 主要是VCU_ELECTRICIT_PERCENT",
                        raw_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6986570719 分析信号链路 主要是VCU_ELECTRICIT_PERCENT",
                        triggered=True,
                    )
                )
                metadata_text = (
                    Path(result.details["agent_request_file"]).with_name("bug_metadata.md").read_text(encoding="utf-8")
                )

        self.assertTrue(result.success)
        self.assertIn("Agent 类型: `codex`", metadata_text)
        self.assertIn("Agent Token: `321 / 54 / 375`", metadata_text)
        self.assertIn("Agent 耗时: `12.5 秒`", metadata_text)
        self.assertIn("总耗时:", metadata_text)

    def test_bug_analysis_collects_source_evidence_on_first_run_when_prompt_requests_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            guideengine_repo = Path(tmp) / "guideengine"
            signal_proto = guideengine_repo / "module_floorcenter/module_proto/src/main/proto/signal.proto"
            signal_proto.parent.mkdir(parents=True, exist_ok=True)
            signal_proto.write_text("SIGNAL_VCU_ELECTRICIT_PERCENT = 40019; // 电池电量值\n", encoding="utf-8")

            config = BridgeConfig(dry_run=False, data_dir=Path(tmp), workspace_root=Path(tmp), guideengine_repo=guideengine_repo)
            runner = BugAnalysisRunner(config)
            log_root = Path(tmp) / "logs"
            log_root.mkdir(parents=True, exist_ok=True)

            def fake_run_json_command(command, timeout):
                if "check-env" in command:
                    return {"meegle_installed": True, "auth_ok": True}
                if "resolve-url" in command:
                    return {"project_key": "xpfailuremgmt", "work_item_id": "6986570719"}
                if "fetch-data" in command:
                    return {
                        "title": "车辆无法开启小憩模式",
                        "status": "处理中",
                        "create_time": "2026-05-16 10:00",
                        "create_by": "tester",
                        "fields": {},
                        "attachments": [],
                        "description": "VCU_ELECTRICIT_PERCENT 相关",
                    }
                if command[:3] == ["meegle", "workitem", "get"]:
                    return {"data": {}}
                raise AssertionError(f"unexpected command: {command}")

            with (
                mock.patch.object(runner, "_run_json_command", side_effect=fake_run_json_command),
                mock.patch.object(runner, "_load_option_map", return_value={}),
                mock.patch.object(runner, "_download_bug_attachments", return_value={"downloaded": [], "unzipped": [], "errors": [], "skipped": [], "ok": True}),
                mock.patch.object(runner, "_select_log_input", return_value=log_root),
                mock.patch.object(runner, "_prepare_log_input", return_value=log_root),
                mock.patch.object(runner, "_extract_fault_time", return_value=("2026-05-16 10:01", "从缺陷描述提取")),
                mock.patch.object(
                    runner,
                    "_run_analysis",
                    side_effect=lambda **kwargs: (
                        kwargs["html_path"].write_text("<html>signal</html>", encoding="utf-8"),
                        kwargs["json_path"].write_text("{}", encoding="utf-8"),
                        subprocess.CompletedProcess(args=["python3"], returncode=0, stdout="", stderr=""),
                    )[-1],
                ),
                mock.patch.object(
                    runner,
                    "_run_bug_agent_summary",
                    return_value={"message": "", "command": None, "error": "", "provider": "", "session_id": "", "resumed": False, "duration_seconds": 0.0, "usage": {}},
                ),
            ):
                result = runner.run_bug_analysis(
                    BugRequest(
                        bug_url="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6986570719",
                        prompt="分析信号链路，主要是VCU_ELECTRICIT_PERCENT，请参考源码分析",
                        raw_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6986570719 分析信号链路，主要是VCU_ELECTRICIT_PERCENT，请参考源码分析",
                        triggered=True,
                    )
                )
                metadata_text = Path(result.job_dir) / "output" / "bug_metadata.md"
                metadata_body = metadata_text.read_text(encoding="utf-8")

        self.assertTrue(result.success)
        self.assertIn("源码证据", metadata_body)
        self.assertIn("SIGNAL_VCU_ELECTRICIT_PERCENT", metadata_body)
        self.assertTrue(result.details["source_evidence_file"].endswith("bug_source_evidence.md"))

    def test_bug_followup_agent_decision_can_switch_to_xtheme_reanalysis(self):
        runner = BugAnalysisRunner(BridgeConfig(dry_run=False))
        previous_session = {
            "details": {
                "analysis_kinds": ["general"],
                "prepared_log_input": "",
                "selected_log_input": "",
                "user_request_text": "分析主题相关",
            }
        }
        previous_context = type(
            "Context",
            (),
            {
                "request_text": "分析主题相关",
                "summary_text": "上一轮静态结论",
                "report_excerpt": "xtheme 相关证据不足",
            },
        )()

        with mock.patch.object(
            runner,
            "_run_bug_decision_agent",
            return_value=(
                {
                    "action": "reanalyze",
                    "analysis_kind": "xtheme",
                    "skill": "xtheme-analyzer",
                    "signal_hint": "",
                    "retry_download_if_missing": True,
                    "reason": "追问明确点名 xtheme 时光变化，需要切换到 xtheme 专用分析。",
                },
                "claude",
            ),
        ):
            selection = runner.decide_bug_followup(
                followup_text="重新分析 xtheme 主题变化",
                previous_context=previous_context,
                previous_session=previous_session,
            )

        self.assertIsNotNone(selection)
        assert selection is not None
        self.assertTrue(selection.should_reanalyze)
        self.assertEqual(selection.skill_name, "xtheme-analyzer")
        self.assertEqual(selection.plans[0].kind, "xtheme")
        self.assertEqual(selection.provider, "claude")

    def test_bug_analysis_general_request_without_logs_falls_back_to_static_source_analysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            guideengine_repo = Path(tmp) / "guideengine"
            source_file = guideengine_repo / "module_display/theme/ThemeSceneMode.kt"
            source_file.parent.mkdir(parents=True, exist_ok=True)
            source_file.write_text(
                "class ThemeSceneMode {\n"
                "    // 场景模式需要跟随黑白夜切换\n"
                "}\n",
                encoding="utf-8",
            )

            config = BridgeConfig(
                dry_run=False,
                data_dir=Path(tmp),
                workspace_root=Path(tmp),
                guideengine_repo=guideengine_repo,
            )
            runner = BugAnalysisRunner(config)

            def fake_run_json_command(command, timeout):
                if "check-env" in command:
                    return {"meegle_installed": True, "auth_ok": True}
                if "resolve-url" in command:
                    return {"project_key": "xpfailuremgmt", "work_item_id": "6991604970"}
                if "fetch-data" in command:
                    return {
                        "title": "临停P档，切换黑白夜，场景模式没有更随黑白夜",
                        "status": "处理中",
                        "create_time": "2026-05-17 18:54",
                        "create_by": "tester",
                        "fields": {},
                        "attachments": [],
                        "description": "问题时间: 2026-05-14 17:56\n切换黑白夜后场景模式没有跟随变化",
                    }
                if command[:3] == ["meegle", "workitem", "get"]:
                    return {"work_item_current_node": []}
                raise AssertionError(f"unexpected command: {command}")

            with (
                mock.patch.object(runner, "_run_json_command", side_effect=fake_run_json_command),
                mock.patch.object(runner, "_load_option_map", return_value={}),
                mock.patch.object(
                    runner,
                    "_download_bug_attachments",
                    return_value={"downloaded": [], "unzipped": [], "errors": [], "skipped": [], "ok": True},
                ),
                mock.patch.object(runner, "_run_analysis", side_effect=AssertionError("general request should not run log scripts")),
                mock.patch.object(runner, "_build_combined_report_artifacts", return_value=None),
                mock.patch.object(
                    runner,
                    "_run_bug_agent_summary",
                    return_value={
                        "message": "agent summary",
                        "command": ["codex", "exec"],
                        "error": "",
                        "provider": "codex",
                        "session_id": "sess_general",
                        "resumed": False,
                        "duration_seconds": 5.2,
                        "usage": {
                            "input_tokens": 1200,
                            "output_tokens": 300,
                            "total_tokens": 1500,
                        },
                        "usage_scope": "delta",
                    },
                ),
            ):
                result = runner.run_bug_analysis(
                    BugRequest(
                        bug_url="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6991604970",
                        prompt="分析主题相关",
                        raw_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6991604970 分析主题相关",
                        triggered=True,
                    )
                )

            metadata_body = (Path(result.job_dir) / "output" / "bug_metadata.md").read_text(encoding="utf-8")
            evidence_body = Path(result.details["source_evidence_file"]).read_text(encoding="utf-8")

        self.assertTrue(result.success)
        self.assertEqual(result.details["analysis_kind"], "general")
        self.assertEqual(result.details["analysis_skill"], "general")
        self.assertEqual(result.details["classification_source"], "manual_fallback")
        self.assertEqual(result.details["selected_log_input"], "")
        self.assertIn("通用问题分析", metadata_body)
        self.assertIn("命中 Skill: `general`", metadata_body)
        self.assertIn("分类来源: `manual_fallback`", metadata_body)
        self.assertIn("无，可静态分析", metadata_body)
        self.assertIn("源码证据", metadata_body)
        self.assertIn("场景模式", evidence_body)

    def test_general_bug_report_uses_structured_summary_sections(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=True, data_dir=Path(tmp), workspace_root=Path(tmp))
            runner = BugAnalysisRunner(config)
            source_evidence = Path(tmp) / "source.md"
            source_evidence.write_text(
                "## Source Evidence\n\n"
                "- `/repo/ThemeSceneMode.kt:12` 场景模式需要跟随黑白夜切换\n",
                encoding="utf-8",
            )
            html_path = Path(tmp) / "general.html"
            json_path = Path(tmp) / "general.json"

            runner._write_general_bug_report(
                html_path=html_path,
                json_path=json_path,
                title="场景模式没有跟随黑白夜",
                description="切换黑白夜后场景模式没有跟随变化",
                prompt_text="分析主题相关",
                request_text="分析主题相关",
                fault_time="2026-05-14 17:56",
                selected_input=None,
                source_evidence_path=source_evidence,
                classification_skill="general",
                classification_source="agent",
                classification_reason="未命中专用脚本，按通用分析处理",
            )

            html = html_path.read_text(encoding="utf-8")

        for heading in ["结论摘要", "关键证据", "最可能原因", "待确认项", "建议动作"]:
            self.assertIn(heading, html)
        self.assertNotIn("rawTmcData", html)
        self.assertNotIn("TMC", html)

    def test_bug_analysis_missing_log_failure_includes_attachment_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp), workspace_root=Path(tmp))
            runner = BugAnalysisRunner(config)

            def fake_run_json_command(command, timeout):
                if "check-env" in command:
                    return {"meegle_installed": True, "auth_ok": True}
                if "resolve-url" in command:
                    return {"project_key": "xpfailuremgmt", "work_item_id": "6991604970"}
                if "fetch-data" in command:
                    return {
                        "title": "3D 启动黑屏",
                        "status": "处理中",
                        "create_time": "2026-05-17 18:54",
                        "create_by": "tester",
                        "fields": {},
                        "attachments": [{"name": "data_Log_log0.zip", "size": "47.56MB"}],
                        "description": "启动黑屏只有 logo",
                    }
                if command[:3] == ["meegle", "workitem", "get"]:
                    return {"work_item_current_node": []}
                raise AssertionError(f"unexpected command: {command}")

            with (
                mock.patch.object(runner, "_run_json_command", side_effect=fake_run_json_command),
                mock.patch.object(runner, "_load_option_map", return_value={}),
                mock.patch.object(
                    runner,
                    "_download_bug_attachments",
                    return_value={
                        "downloaded": [],
                        "unzipped": [],
                        "errors": ["data_Log_log0.zip"],
                        "error_details": [{"name": "data_Log_log0.zip", "reason": "AUTH_REQUIRED: authentication required"}],
                        "skipped": [],
                        "ok": True,
                    },
                ),
            ):
                result = runner.run_bug_analysis(
                    BugRequest(
                        bug_url="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6991604970",
                        prompt="调查3D启动时序",
                        raw_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6991604970 调查3D启动时序",
                        triggered=True,
                    )
                )

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "bug_analysis_missing_log_attachment")
        self.assertIn("AUTH_REQUIRED", result.message)
        self.assertIn("data_Log_log0.zip", result.message)

    def test_bug_reanalysis_retries_download_when_logs_missing_for_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = BugAnalysisRunner(BridgeConfig(dry_run=False, data_dir=Path(tmp), workspace_root=Path(tmp)))
            prepared_input = Path(tmp) / "logs"
            prepared_input.mkdir(parents=True, exist_ok=True)
            previous_context = type(
                "Context",
                (),
                {
                    "request_text": "原始 bug 分析",
                    "summary_text": "上一轮总结",
                    "report_excerpt": "报告摘录",
                    "history": [],
                },
            )()
            output_dir = Path(tmp) / "jobs" / "job_1" / "output"
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / "bug_agent_summary.md").write_text("old summary", encoding="utf-8")
            previous_session = {
                "job_id": "job_1",
                "job_dir": str(Path(tmp) / "jobs" / "job_1"),
                "details": {
                    "bug_url": "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6991604970",
                    "analysis_kinds": ["general"],
                    "prepared_log_input": "",
                    "selected_log_input": "",
                    "user_request_text": "原始 bug 分析",
                    "agent_summary_file": str(output_dir / "bug_agent_summary.md"),
                },
            }

            def fake_run_analysis(*, plan, input_path, html_path, json_path, analysis_dir, timeout, target_time, request_text=None):
                html_path.write_text("<html>xtheme</html>", encoding="utf-8")
                json_path.write_text("{}", encoding="utf-8")
                return subprocess.CompletedProcess(args=["python3"], returncode=0, stdout="[OK] HTML: x\n[OK] JSON: y\n", stderr="")

            with (
                mock.patch.object(
                    runner,
                    "_retry_bug_log_download",
                    return_value={
                        "ok": True,
                        "message": "已重新下载并准备日志输入。",
                        "selected_input": prepared_input,
                        "prepared_input": prepared_input,
                    },
                ) as retry_mock,
                mock.patch.object(runner, "_run_analysis", side_effect=fake_run_analysis),
                mock.patch.object(runner, "_write_reanalysis_source_evidence", return_value=None),
                mock.patch.object(runner, "_build_combined_report_artifacts", return_value=None),
                mock.patch.object(
                    runner,
                    "_run_bug_agent_summary",
                    return_value={"message": "agent summary", "command": None, "error": "", "provider": "codex", "session_id": "", "resumed": False, "duration_seconds": 1.0, "usage": {}},
                ),
            ):
                result = runner.run_bug_reanalysis(
                    followup_text="重新分析 xtheme",
                    previous_context=previous_context,
                    previous_session=previous_session,
                    plans_override=[__import__("lark_agent_bridge.agents", fromlist=["BugAnalysisPlan"]).BugAnalysisPlan(kind="xtheme")],
                    classification_skill="xtheme-analyzer",
                    classification_source="agent",
                    classification_reason="需要 xtheme 专用分析",
                    classification_provider="codex",
                )

        self.assertTrue(result.success)
        self.assertTrue(retry_mock.called)
        self.assertEqual(result.details["analysis_skill"], "xtheme-analyzer")
        self.assertEqual(result.details["download_retry"]["ok"], True)

    def test_bug_reanalysis_reports_retry_download_failure_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = BugAnalysisRunner(BridgeConfig(dry_run=False, data_dir=Path(tmp), workspace_root=Path(tmp)))
            previous_context = type(
                "Context",
                (),
                {
                    "request_text": "原始 bug 分析",
                    "summary_text": "上一轮总结",
                    "report_excerpt": "报告摘录",
                    "history": [],
                },
            )()
            previous_session = {
                "job_id": "job_1",
                "job_dir": str(Path(tmp) / "jobs" / "job_1"),
                "details": {
                    "bug_url": "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6991604970",
                    "analysis_kinds": ["general"],
                    "prepared_log_input": "",
                    "selected_log_input": "",
                    "user_request_text": "原始 bug 分析",
                },
            }

            with mock.patch.object(
                runner,
                "_retry_bug_log_download",
                return_value={
                    "ok": False,
                    "message": "重新下载日志失败：meegle 未登录或授权已失效（host=project.feishu.cn）。",
                    "reason": "AUTH_REQUIRED",
                },
            ):
                result = runner.run_bug_reanalysis(
                    followup_text="重新分析 xtheme",
                    previous_context=previous_context,
                    previous_session=previous_session,
                    plans_override=[__import__("lark_agent_bridge.agents", fromlist=["BugAnalysisPlan"]).BugAnalysisPlan(kind="xtheme")],
                    classification_skill="xtheme-analyzer",
                )

        self.assertFalse(result.success)
        self.assertIn("meegle 未登录或授权已失效", result.message)
        self.assertEqual(result.details["download_retry"]["reason"], "AUTH_REQUIRED")

    def test_bug_outputs_marks_script_summary_as_current_run_not_previous_round(self):
        runner = BugAnalysisRunner(BridgeConfig(dry_run=True))

        metadata, _ = runner._build_bug_outputs(
            plans=[__import__("lark_agent_bridge.agents", fromlist=["BugAnalysisPlan"]).BugAnalysisPlan(kind="signal", signal_code="SIGNAL_VCU_ELECTRICIT_PERCENT")],
            work_item_id="6986570719",
            fetched={
                "title": "车辆无法开启小憩模式",
                "status": "处理中",
                "create_time": "2026-05-16 10:00",
                "create_by": "tester",
                "fields": {},
                "attachments": [],
            },
            full_item={"work_item_current_node": []},
            option_map={},
            request_text="分析5月19日 的信号链路 主要是VCU_ELECTRICIT_PERCENT，请参考源码分析",
            prompt_text="分析5月19日 的信号链路 主要是VCU_ELECTRICIT_PERCENT，请参考源码分析",
            selected_input=None,
            report_jsons={"signal": None},
            download={"downloaded": [], "skipped": [], "errors": []},
            html_paths=[Path("/tmp/bug_signal_chain_report.html")],
        )

        self.assertIn("本轮脚本初步摘要", metadata)
        self.assertIn("不代表上一轮分析结论", metadata)

    def test_bug_analysis_reuses_cached_logs_for_same_bug_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp), workspace_root=Path(tmp))
            runner = BugAnalysisRunner(config)
            cache_dir = config.data_dir / "bug_cache" / "xpfailuremgmt_6986570719"
            cached_logs = cache_dir / "logs" / "Log" / "log0" / "app" / "com.xiaopeng.montecarlo"
            cached_logs.mkdir(parents=True, exist_ok=True)
            (cached_logs / "main_2026-05-11_23-00.alog").write_text("", encoding="utf-8")
            analysis_inputs = []

            def fake_run_json_command(command, timeout):
                if "check-env" in command:
                    return {"meegle_installed": True, "auth_ok": True}
                if "resolve-url" in command:
                    return {"project_key": "xpfailuremgmt", "work_item_id": "6986570719"}
                if "fetch-data" in command:
                    return {
                        "title": "启动问题",
                        "description": "问题时间: 2026-05-11 23:10",
                        "status": "处理中",
                        "create_time": "2026-05-16 10:00",
                        "create_by": "tester",
                        "fields": {},
                        "attachments": [{"name": "cached-log.zip"}],
                    }
                if command[:3] == ["meegle", "workitem", "get"]:
                    return {"data": {}}
                raise AssertionError(f"unexpected command: {command}")

            def fake_run_analysis(*, plan, input_path, html_path, json_path, analysis_dir, timeout, target_time, request_text=None):
                analysis_inputs.append(input_path)
                html_path.write_text("<html>startup</html>", encoding="utf-8")
                json_path.write_text("{}", encoding="utf-8")
                return subprocess.CompletedProcess(args=["python3"], returncode=0, stdout="", stderr="")

            with (
                mock.patch.object(runner, "_run_json_command", side_effect=fake_run_json_command),
                mock.patch.object(runner, "_load_option_map", return_value={}),
                mock.patch.object(runner, "_download_bug_attachments", side_effect=AssertionError("download should not be called")),
                mock.patch.object(runner, "_extract_fault_time", return_value=("2026-05-11 23:10", "")),
                mock.patch.object(runner, "_run_analysis", side_effect=fake_run_analysis),
                mock.patch.object(runner, "_build_bug_outputs", return_value=("# meta\n", "summary")),
                mock.patch.object(
                    runner,
                    "_build_combined_report_artifacts",
                    return_value={"html_path": Path(tmp) / "combined.html", "json_path": Path(tmp) / "combined.json", "summary": "summary"},
                ),
                mock.patch.object(
                    runner,
                    "_run_bug_agent_summary",
                    return_value={"message": "", "command": None, "error": "", "provider": "", "session_id": "", "resumed": False, "duration_seconds": 0.0, "usage": {}},
                ),
            ):
                result = runner.run_bug_analysis(
                    BugRequest(
                        bug_url="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6986570719",
                        prompt="调查3D启动时序",
                        raw_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6986570719 调查3D启动时序",
                        triggered=True,
                    )
                )

        self.assertTrue(result.success)
        self.assertEqual(analysis_inputs, [(cache_dir / "logs").resolve()])
        self.assertTrue(result.details["bug_cache_reused"])
        self.assertEqual(result.details["bug_cache_dir"], str(cache_dir.resolve()))

    def test_agent_runtime_annotation_injects_section_into_html_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = BugAnalysisRunner(BridgeConfig(dry_run=True, data_dir=Path(tmp), workspace_root=Path(tmp)))
            report_html = Path(tmp) / "bug_signal_chain_report.html"
            report_html.write_text("<html><body><h1>原始报告</h1></body></html>", encoding="utf-8")

            runner._annotate_html_reports(
                [report_html],
                agent_summary_result={
                    "provider": "codex",
                    "duration_seconds": 12.5,
                    "usage": {
                        "input_tokens": 321,
                        "output_tokens": 54,
                        "total_tokens": 375,
                    },
                },
                total_duration_seconds=45.6,
            )

            html = report_html.read_text(encoding="utf-8")

        self.assertIn("Agent 运行信息", html)
        self.assertIn("Agent 类型", html)
        self.assertIn("codex", html)
        self.assertIn("321 / 54 / 375", html)
        self.assertIn("45.6 秒", html)
        self.assertIn("原始报告", html)

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

    def test_bug_analysis_xtheme_command_passes_focus_context(self):
        runner = BugAnalysisRunner(BridgeConfig(dry_run=True))
        command = runner.build_command(
            plan=__import__("lark_agent_bridge.agents", fromlist=["BugAnalysisPlan"]).BugAnalysisPlan(kind="xtheme"),
            input_path=Path("/tmp/input"),
            html_path=Path("/tmp/out.html"),
            json_path=Path("/tmp/out.json"),
            analysis_dir=Path("/tmp/xtheme"),
            target_time="2026-05-18 11:19",
            request_text="调查主题变化",
        )

        self.assertIn("--target-time", command)
        self.assertIn("2026-05-18 11:19", command)
        self.assertIn("--request-text", command)
        self.assertIn("调查主题变化", command)

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

    def test_bug_analysis_prefers_bare_signal_name_over_vin_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "guideengine"
            signal_proto = repo / "module_floorcenter/module_proto/src/main/proto/signal.proto"
            signal_proto.parent.mkdir(parents=True, exist_ok=True)
            signal_proto.write_text("SIGNAL_VCU_ELECTRICIT_PERCENT = 40019;\n", encoding="utf-8")
            runner = BugAnalysisRunner(BridgeConfig(dry_run=True, guideengine_repo=repo))

            plans = runner.classify_requests(
                prompt_text="分析信号链路，主要是VCU_ELECTRICIT_PERCENT，请参考源码分析",
                title="车辆无法开启小憩模式",
                description="VIN码为: [L1NNSXTL4TB235082]",
            )

        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].kind, "signal")
        self.assertEqual(plans[0].signal_code, "SIGNAL_VCU_ELECTRICIT_PERCENT")

    def test_bug_analysis_classifies_crash_request(self):
        runner = BugAnalysisRunner(BridgeConfig(dry_run=True))

        plan = runner.classify_request(
            prompt_text="调查闪退和tombstone",
            title="",
            description="",
        )

        self.assertEqual(plan.kind, "crash")
        self.assertIsNone(plan.signal_code)

    def test_bug_analysis_agent_classifier_selects_xtheme_skill(self):
        runner = BugAnalysisRunner(BridgeConfig(dry_run=False))

        with mock.patch.object(
            runner,
            "_run_bug_decision_agent",
            return_value=(
                {
                    "analysis_kind": "xtheme",
                    "skill": "xtheme-analyzer",
                    "signal_hint": "",
                    "reason": "问题描述集中在 xtheme / 主题切换 / 晨曦时段。",
                },
                "codex",
            ),
        ):
            selection = runner._classify_bug_request_with_agent(
                prompt_text="分析 xtheme 主题切换",
                title="切换黑白夜后场景模式没有跟随",
                description="请看 xtheme/晨曦/傍晚变化",
                attachments=[],
            )

        self.assertIsNotNone(selection)
        assert selection is not None
        self.assertEqual(selection.skill_name, "xtheme-analyzer")
        self.assertEqual(selection.plans[0].kind, "xtheme")
        self.assertEqual(selection.source, "agent")
        self.assertEqual(selection.provider, "codex")

    def test_bug_analysis_classifies_startup_and_stuck_requests_together(self):
        runner = BugAnalysisRunner(BridgeConfig(dry_run=True))

        plans = runner.classify_requests(
            prompt_text="分析启动和卡顿",
            title="",
            description="",
        )

        self.assertEqual([plan.kind for plan in plans], ["startup", "stuck"])

    def test_bug_analysis_classifies_unmatched_request_as_general(self):
        runner = BugAnalysisRunner(BridgeConfig(dry_run=True))

        plan = runner.classify_request(
            prompt_text="分析主题相关",
            title="临停P档，切换黑白夜，场景模式没有更随黑白夜",
            description="",
        )

        self.assertEqual(plan.kind, "general")
        self.assertIsNone(plan.signal_code)

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

    def test_bug_analysis_select_log_input_skips_png_xp_attachment(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = BugAnalysisRunner(BridgeConfig(dry_run=True))
            bug_dir = Path(tmp)
            attachments_dir = bug_dir / "attachments"
            attachments_dir.mkdir(parents=True, exist_ok=True)
            (attachments_dir / "XpengFile_L1NNSXTL4TB235082_20260509190110.xp").write_bytes(
                b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
            )
            (attachments_dir / "XpengFile_L1NNSXTL4TB235082_20260509190110.zip").write_bytes(b"not-a-real-zip")

            selected = runner._select_log_input(
                bug_dir,
                fetched={
                    "attachments": [
                        {"name": "XpengFile_L1NNSXTL4TB235082_20260509190110.xp"},
                        {"name": "XpengFile_L1NNSXTL4TB235082_20260509190110.zip"},
                    ]
                },
            )

        self.assertIsNone(selected)

    def test_bug_analysis_download_whitelist_accepts_archives_text_and_logs(self):
        runner = BugAnalysisRunner(BridgeConfig(dry_run=True))

        accepted = [
            "logs.zip",
            "logs.tar.gz",
            "trace.7z",
            "trace.rar",
            "note.txt",
            "main.alog",
            "main.xlog",
            "main.log",
            "bundle.xp",
            "bundle.xp.zip.001",
        ]
        rejected = [
            "video.mp4",
            "image.png",
            "image.jpg",
            "sheet.xlsx",
            "slide.pptx",
            "doc.docx",
        ]

        for name in accepted:
            self.assertTrue(runner._should_download_bug_attachment(name), name)
        for name in rejected:
            self.assertFalse(runner._should_download_bug_attachment(name), name)

    def test_bug_analysis_download_bug_attachments_skips_non_whitelisted_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = BugAnalysisRunner(BridgeConfig(dry_run=False, data_dir=Path(tmp), workspace_root=Path(tmp)))
            bug_dir = Path(tmp) / "bug_1"
            commands = []

            def fake_process(command, **kwargs):
                commands.append(command)
                output_path = Path(command[command.index("--output") + 1])
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(b"log")
                return subprocess.CompletedProcess(command, 0, "{}", "")

            attachments = [
                {"name": "keep.zip", "url": "https://example.test/keep.zip"},
                {"name": "keep.log", "url": "https://example.test/keep.log"},
                {"name": "skip.mp4", "url": "https://example.test/skip.mp4"},
                {"name": "skip.png", "url": "https://example.test/skip.png"},
            ]

            with mock.patch("lark_agent_bridge.agents.run_tracked_process", side_effect=fake_process):
                result = runner._download_bug_attachments(
                    "xpfailuremgmt",
                    "6986570719",
                    bug_dir,
                    attachments,
                    timeout=120,
                )

        downloaded = set(result["downloaded"])
        self.assertEqual(downloaded, {"keep.zip", "keep.log"})
        self.assertEqual(set(result["skipped"]), {"skip.mp4", "skip.png"})
        command_text = "\n".join(" ".join(command) for command in commands)
        self.assertIn("keep.zip", command_text)
        self.assertIn("keep.log", command_text)
        self.assertIn("--overwrite", command_text)
        self.assertNotIn("skip.mp4", command_text)
        self.assertNotIn("skip.png", command_text)

    def test_bug_cache_download_output_path_is_absolute_when_working_dir_differs(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace_root = Path(tmp) / "workspace"
            workspace_root.mkdir()
            config = BridgeConfig(dry_run=False, data_dir=Path("data"), workspace_root=workspace_root)
            config.bug_analysis.working_dir = workspace_root
            runner = BugAnalysisRunner(config)
            bug_dir = runner._bug_cache_dir("xpfailuremgmt", "6991604970")
            commands = []

            def fake_process(command, **kwargs):
                commands.append(command)
                output_path = Path(command[command.index("--output") + 1])
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(b"log")
                return subprocess.CompletedProcess(command, 0, "{}", "")

            with mock.patch("lark_agent_bridge.agents.run_tracked_process", side_effect=fake_process):
                result = runner._download_bug_attachments(
                    "xpfailuremgmt",
                    "6991604970",
                    bug_dir,
                    [{"name": "data_Log_log0.zip", "url": "https://example.test/log.zip"}],
                    timeout=120,
                )

        output_path = Path(commands[0][commands[0].index("--output") + 1])
        self.assertTrue(result["downloaded"])
        self.assertTrue(output_path.is_absolute())
        self.assertEqual(output_path.parent, bug_dir / "attachments")

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

            def fake_run_analysis(*, plan, input_path, html_path, json_path, analysis_dir, timeout, target_time, request_text=None):
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

    def test_bug_agent_summary_once_missing_output_dir_returns_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp), workspace_root=Path(tmp))
            runner = BugAnalysisRunner(config)
            output_path = Path(tmp) / "missing-dir" / "bug_agent_summary.md"
            completed = subprocess.CompletedProcess(
                args=["claude"],
                returncode=0,
                stdout="Agent summary text",
                stderr="",
            )

            with mock.patch("lark_agent_bridge.agents.run_tracked_process", return_value=completed):
                result = runner._run_bug_agent_summary_once(
                    invocation={"command": ["claude"], "provider": "claude", "session_id": "", "resumed": False},
                    output_path=output_path,
                    progress_callback=None,
                    timeout=60,
                )

            self.assertTrue(output_path.exists())

        self.assertEqual(result["message"], "Agent summary text")
        self.assertEqual(result["error"], "")

    def test_bug_agent_summary_once_read_write_failure_falls_back_to_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp), workspace_root=Path(tmp))
            runner = BugAnalysisRunner(config)
            output_path = Path(tmp) / "bug_agent_summary.md"
            completed = subprocess.CompletedProcess(
                args=["claude"],
                returncode=0,
                stdout="Agent summary text",
                stderr="",
            )

            with (
                mock.patch("lark_agent_bridge.agents.run_tracked_process", return_value=completed),
                mock.patch.object(Path, "write_text", side_effect=OSError("disk full")),
            ):
                result = runner._run_bug_agent_summary_once(
                    invocation={"command": ["claude"], "provider": "claude", "session_id": "", "resumed": False},
                    output_path=output_path,
                    progress_callback=None,
                    timeout=60,
                )

        self.assertEqual(result["message"], "")
        self.assertEqual(result["provider"], "claude")
        self.assertEqual(result["error"], "agent_summary_output_io_error")

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

    def test_combined_report_does_not_require_sr_skill_common(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace_root = Path(tmp) / "workspace-without-common"
            workspace_root.mkdir()
            output_dir = Path(tmp) / "output"
            output_dir.mkdir()
            runner = BugAnalysisRunner(BridgeConfig(dry_run=True, workspace_root=workspace_root))
            startup_json = output_dir / "bug_3d_startup_report.json"
            stuck_json = output_dir / "bug_3d_stuck_report.json"
            startup_json.write_text(
                json.dumps({"verdict": {"severity": "green", "message": "启动链路正常"}}, ensure_ascii=False),
                encoding="utf-8",
            )
            stuck_json.write_text(
                json.dumps({"target_verdict": {"sev": "yellow", "message": "目标时间窗存在卡顿"}}, ensure_ascii=False),
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
                html_paths=[],
                report_jsons={"startup": startup_json, "stuck": stuck_json},
                selected_input=None,
            )

            self.assertIsNotNone(artifacts)
            html = Path(artifacts["html_path"]).read_text(encoding="utf-8")
            self.assertIn("3D启动卡顿综合报告", html)
            self.assertIn("启动链路正常", html)

    def test_bug_analysis_falls_back_when_agent_summary_output_io_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp), workspace_root=Path(tmp))
            runner = BugAnalysisRunner(config)
            log_root = Path(tmp) / "logs"
            log_root.mkdir(parents=True, exist_ok=True)
            combined_html = Path(tmp) / "bug_signal_overview_report.html"
            combined_json = Path(tmp) / "bug_signal_overview_report.json"
            combined_html.write_text("<html>merged</html>", encoding="utf-8")
            combined_json.write_text("{}", encoding="utf-8")

            def fake_run_json_command(command, timeout):
                if "check-env" in command:
                    return {"meegle_installed": True, "auth_ok": True}
                if "resolve-url" in command:
                    return {"project_key": "xpfailuremgmt", "work_item_id": "6986570719"}
                if "fetch-data" in command:
                    return {"title": "信号问题", "description": "2026-05-09 19:00 信号异常"}
                if command[:3] == ["meegle", "workitem", "get"]:
                    return {"data": {}}
                if "download" in command:
                    return {"downloaded": []}
                raise AssertionError(f"unexpected command: {command}")

            def fake_run_analysis(*, plan, input_path, html_path, json_path, analysis_dir, timeout, target_time, request_text=None):
                html_path.write_text("<html>signal</html>", encoding="utf-8")
                json_path.write_text('{"signal":{"code":"40018","name":"SIGNAL_VCU_ELECTRICIT_PERCENT"}}', encoding="utf-8")
                return subprocess.CompletedProcess(args=["python3"], returncode=0, stdout="", stderr="")

            with (
                mock.patch.object(runner, "_run_json_command", side_effect=fake_run_json_command),
                mock.patch.object(runner, "_load_option_map", return_value={}),
                mock.patch.object(runner, "_select_log_input", return_value=log_root),
                mock.patch.object(runner, "_prepare_log_input", return_value=log_root),
                mock.patch.object(runner, "_run_analysis", side_effect=fake_run_analysis),
                mock.patch.object(runner, "_build_bug_outputs", return_value=("metadata", "script summary")),
                mock.patch.object(
                    runner,
                    "_build_combined_report_artifacts",
                    return_value={"html_path": combined_html, "json_path": combined_json, "summary": "combined summary"},
                ),
                mock.patch.object(
                    runner,
                    "_run_bug_agent_summary",
                    return_value={
                        "message": "",
                        "command": ["claude"],
                        "error": "agent_summary_output_io_error",
                        "provider": "claude",
                        "session_id": "",
                        "resumed": False,
                    },
                ),
            ):
                result = runner.run_bug_analysis(
                    BugRequest(
                        bug_url="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6986570719",
                        prompt="分析信号链路",
                        raw_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6986570719 分析信号链路",
                        triggered=True,
                    )
                )

        self.assertTrue(result.success)
        self.assertEqual(result.message, "script summary")
        self.assertEqual(result.details["agent_summary_error"], "agent_summary_output_io_error")

    def test_build_combined_report_artifacts_for_signal_creates_generic_overview(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = BugAnalysisRunner(BridgeConfig(dry_run=True, workspace_root=Path("/Users/zhuyl/Documents/workspace")))
            output_dir = Path(tmp)
            signal_json = output_dir / "bug_signal_chain_report.json"
            signal_html = output_dir / "bug_signal_chain_report.html"
            signal_html.write_text("<html>signal</html>", encoding="utf-8")
            source_evidence = output_dir / "bug_source_evidence.md"
            source_evidence.write_text(
                "\n".join(
                    [
                        "# Bug Source Evidence",
                        "## module_sample/business/src/main/java/com/x/example/report/GenericFlowJudge.kt",
                        "- L48: `if (signalState == INVALID) {`",
                    ]
                ),
                encoding="utf-8",
            )
            signal_payload = {
                "signal": {
                    "code": "74001",
                    "name": "SIGNAL_SAMPLE_GENERIC_STATE",
                    "comment": "样例状态值",
                },
                "summary": "当前日志已命中 datacenter 与业务消费阶段。",
                "sources": ["CARSERVICE:ID_SAMPLE_GENERIC_STATE"],
                "derived": ["ANDROID:DataCenter Flow/Observer"],
                "chain_edges": [
                    {
                        "source": "CARSERVICE:ID_SAMPLE_GENERIC_STATE",
                        "target": "74001",
                        "kind": "CarService 属性映射",
                        "file": "module_sample/carservice/GenericSignalHelper.kt",
                        "line": 31,
                        "note": "GenericSignalHelper 将 ID_SAMPLE_GENERIC_STATE 映射为 SIGNAL_SAMPLE_GENERIC_STATE",
                        "confidence": "high",
                    },
                    {
                        "source": "74001",
                        "target": "ANDROID:DataCenter Flow/Observer",
                        "kind": "Kotlin 信号分发",
                        "file": "module_floorcenter/module_datacenter/src/main/java/com/xiaopeng/guideengine/center/DataCenter.kt",
                        "line": 311,
                        "note": "DataCenter.dispatchSignal 会写缓存、Flow 和 observer。",
                        "confidence": "medium",
                    },
                ],
                "source_references": [
                    {
                        "file": "module_sample/business/src/main/java/com/x/example/report/GenericStateCollector.kt",
                        "line": 97,
                        "text": "FloorCenterManager.instance.dataCenter.getSignalFlow(SIGNAL_SAMPLE_GENERIC_STATE).collect {",
                    },
                    {
                        "file": "module_sample/hmi/src/main/java/com/x/example/report/GenericStateReceiver.kt",
                        "line": 81,
                        "text": "SignalCode.SIGNAL_SAMPLE_GENERIC_STATE -> {",
                    },
                ],
                "log_report": {
                    "scanned_files": 82,
                    "scanned_lines": 500001,
                    "truncated": True,
                    "stages": {
                        "datacenter": {
                            "title": "Android DataCenter 分发",
                            "hits": 2,
                            "examples": [
                                {
                                    "code": "74001",
                                    "file": "/tmp/Log/log0/app/com.x.example.generic/main_2026-05-11_11-00.alog.log",
                                    "line": 2651,
                                    "text": "05-11 11:31:23.377 4321 6988 107899 I NAV_DataCenter: getSignalFlow: signalCode=SIGNAL_SAMPLE_GENERIC_STATE, isNeedCache=true, hasProvider=true",
                                }
                            ],
                        },
                        "android_business": {
                            "title": "Android 业务消费日志",
                            "hits": 2,
                            "examples": [
                                {
                                    "code": "74001",
                                    "file": "/tmp/Log/log0/app/com.x.example.generic/main_2026-05-11_11-00.alog.log",
                                    "line": 2694,
                                    "text": "05-11 11:31:23.477 4321 7012 107999 I NAV_GenericStateCollector: stateValue=61",
                                },
                                {
                                    "code": "74001",
                                    "file": "/tmp/Log/log0/app/com.x.example.generic/main_2026-05-11_11-00.alog.log",
                                    "line": 6005,
                                    "text": "05-11 11:31:37.447 4321 4098 121969 I NAV_GenericStateReceiver: state applied : 61",
                                },
                            ],
                        },
                        "other": {
                            "title": "其他命中",
                            "hits": 1,
                            "examples": [
                                {
                                    "code": "74001",
                                    "file": "/tmp/Log/log0/app/com.x.example.generic/main_2026-05-11_11-00.alog.log",
                                    "line": 328,
                                    "text": "05-11 11:31:02.299 4321 4078 86821 I XPD_SignalDispatcher: [registerSignalObserver][57]: registerSignal 74001 size 1",
                                }
                            ],
                        },
                    },
                },
                "lifecycle_report": {
                    "context": {
                        "provider_type": "CarService",
                        "helper_class": "CarVcuHelper",
                        "controller_class": "CarServiceController",
                    },
                    "runtime": {
                        "hits": 4,
                        "scanned_lines": 500001,
                        "truncated": True,
                        "events": [
                            {
                                "label": "进程启动",
                                "time": "05-11 11:30:53.000",
                                "delta": "+15.000s",
                                "file": "/tmp/Log/log0/app/com.x.example.generic/main_2026-05-11_11-00.alog.log",
                                "line": 165,
                                "text": "process begin^^^^^^^^^^Mar 11 2026^^^20:04:15^^^^^^^^^^[4321,3539][2026-05-11 +0800 11:30:53]",
                            },
                            {
                                "label": "DataCenter 注入 GenericSignalHelper",
                                "time": "05-11 11:30:56.679",
                                "delta": "+18.679s",
                                "file": "/tmp/Log/log0/app/com.x.example.generic/main_2026-05-11_11-00.alog.log",
                                "line": 652,
                                "text": "05-11 11:30:56.679 4321 3869 81201 I NAV_DataCenter: injectSignalProvider[com.x.example.GenericSignalHelper@f596912], number[36]",
                            },
                            {
                                "label": "XData 注册 74001",
                                "time": "05-11 11:31:02.299",
                                "delta": "+24.299s",
                                "file": "/tmp/Log/log0/app/com.x.example.generic/main_2026-05-11_11-00.alog.log",
                                "line": 328,
                                "text": "05-11 11:31:02.299 4321 4078 86821 I XPD_SignalDispatcher: [registerSignalObserver][57]: registerSignal 74001 size 1",
                            },
                        ],
                    },
                },
                "detailed_stats": {
                    "74001": {
                        "vhal": 0,
                        "dispatcher": 0,
                        "x3dcb": 0,
                        "unity_recv_windows": 0,
                        "unity_recv_total": 0,
                        "unity_drop_total": 0,
                        "unity_drop_nonzero_windows": 0,
                        "unity_drop_max": 0,
                    }
                },
            }
            signal_json.write_text(json.dumps(signal_payload, ensure_ascii=False), encoding="utf-8")

            artifacts = runner._build_combined_report_artifacts(
                plans=[__import__("lark_agent_bridge.agents", fromlist=["BugAnalysisPlan"]).BugAnalysisPlan(kind="signal", signal_code="74001")],
                prompt_text="分析这个状态信号的链路，请参考源码分析",
                fault_time="2026-05-09 19:00",
                output_dir=output_dir,
                html_paths=[signal_html],
                report_jsons={"signal": signal_json},
                selected_input=Path("/tmp/logs"),
                source_evidence_path=source_evidence,
            )

            self.assertIsNotNone(artifacts)
            html = Path(artifacts["html_path"]).read_text(encoding="utf-8")
            self.assertIn("生命周期流图", html)
            self.assertIn("数据流图", html)
            self.assertIn("com.x.example.generic", html)
            self.assertIn("PID 4321", html)
            self.assertIn("GenericFlowJudge", html)
            self.assertIn("证据边界", html)
            self.assertNotIn("montecarlo", html.casefold())
            self.assertNotIn("40018", html)
            self.assertNotIn("battery", html.casefold())

    def test_signal_business_entry_prefers_dispatcher_over_constants(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = BugAnalysisRunner(BridgeConfig(dry_run=True, workspace_root=Path("/Users/zhuyl/Documents/workspace")))
            evidence = Path(tmp) / "bug_source_evidence.md"
            evidence.write_text(
                "\n".join(
                    [
                        "# Bug Source Evidence",
                        "## module_core/manager_carscene_api/src/main/java/com/xiaopeng/manager_carscene_api/base/CarSceneHmiConstants.kt",
                        "- L23: `* 小憩模式`",
                        "## module_display/launcher_base/src/main/java/root/dispatcher/carscene/CarTakeNapDispatcher.kt",
                        "- L89: `if ((carStateSignal?.batteryLevel ?: 0) < Battery.BATTERY_LIMIT_LOW) {`",
                    ]
                ),
                encoding="utf-8",
            )

            selected = runner._signal_select_business_entry(evidence, prompt_text="看下小憩模式的判定链路")

        assert selected is not None
        self.assertIn("CarTakeNapDispatcher.kt", selected["file"])

    def test_signal_report_planner_returns_report_composition(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = BugAnalysisRunner(BridgeConfig(dry_run=True, workspace_root=Path("/Users/zhuyl/Documents/workspace")))
            evidence = Path(tmp) / "bug_source_evidence.md"
            evidence.write_text(
                "\n".join(
                    [
                        "# Bug Source Evidence",
                        "## module_sample/business/src/main/java/com/x/example/report/GenericFlowJudge.kt",
                        "- L48: `if (signalState == INVALID) {`",
                    ]
                ),
                encoding="utf-8",
            )
            signal_payload = {
                "signal": {"code": "74001", "name": "SIGNAL_SAMPLE_GENERIC_STATE", "comment": "样例状态值"},
                "summary": "当前日志已命中 datacenter 与业务消费阶段。",
                "chain_edges": [
                    {
                        "source": "CARSERVICE:ID_SAMPLE_GENERIC_STATE",
                        "target": "74001",
                        "kind": "CarService 属性映射",
                        "file": "module_sample/carservice/GenericSignalHelper.kt",
                        "line": 31,
                        "note": "GenericSignalHelper 将 ID_SAMPLE_GENERIC_STATE 映射为 SIGNAL_SAMPLE_GENERIC_STATE",
                    }
                ],
                "source_references": [
                    {
                        "file": "module_sample/business/src/main/java/com/x/example/report/GenericStateCollector.kt",
                        "line": 97,
                        "text": "FloorCenterManager.instance.dataCenter.getSignalFlow(SIGNAL_SAMPLE_GENERIC_STATE).collect {",
                    }
                ],
                "log_report": {
                    "stages": {
                        "datacenter": {
                            "hits": 1,
                            "examples": [
                                {
                                    "file": "/tmp/Log/log0/app/com.x.example.generic/main_2026-05-11_11-00.alog.log",
                                    "text": "05-11 11:31:23.377 4321 6988 107899 I NAV_DataCenter: getSignalFlow: signalCode=SIGNAL_SAMPLE_GENERIC_STATE, isNeedCache=true, hasProvider=true",
                                }
                            ],
                        }
                    }
                },
                "lifecycle_report": {
                    "context": {"helper_class": "GenericSignalHelper"},
                    "runtime": {
                        "events": [
                            {
                                "label": "进程启动",
                                "time": "05-11 11:30:53.000",
                                "file": "/tmp/Log/log0/app/com.x.example.generic/main_2026-05-11_11-00.alog.log",
                                "text": "process begin^^^^^^^^^^Mar 11 2026^^^20:04:15^^^^^^^^^^[4321,3539][2026-05-11 +0800 11:30:53]",
                            }
                        ]
                    },
                },
                "detailed_stats": {"74001": {"unity_drop_total": 0, "unity_recv_total": 0}},
            }
            focus_scope = runner._signal_focus_scope(signal_payload)

            composition = runner._plan_signal_report(
                signal_payload=signal_payload,
                prompt_text="分析这个状态信号的链路，请参考源码分析",
                fault_time="2026-05-09 19:00",
                selected_input=None,
                source_evidence_path=evidence,
                focus_scope=focus_scope,
            )

        self.assertIsInstance(composition, ReportComposition)
        self.assertEqual(composition.heading, "信号链路总览报告")
        self.assertTrue(any(section.kind == "flow" for section in composition.sections))
        self.assertEqual(
            [section.title for section in composition.sections[:5]],
            ["结论摘要", "关键证据", "最可能原因", "待确认项", "建议动作"],
        )

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

            def fake_run_analysis(*, plan, input_path, html_path, json_path, analysis_dir, timeout, target_time, request_text=None):
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

            def fake_run_analysis(*, plan, input_path, html_path, json_path, analysis_dir, timeout, target_time, request_text=None):
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
                        "command": ["codex", "exec"],
                        "error": "",
                        "provider": "codex",
                        "session_id": "fresh_sess",
                        "resumed": False,
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
        self.assertEqual(result.details["agent_summary_session_id"], "fresh_sess")
        self.assertNotIn("agent_summary_resumed", result.details)
        self.assertEqual(result.details["rerun_analysis_kinds"], ["startup"])
        self.assertEqual(result.details["reused_analysis_kinds"], ["stuck"])
        self.assertEqual(analysis_inputs[0][0], "startup")
        self.assertEqual(analysis_inputs[0][1], prepared_input)
        self.assertEqual(analysis_inputs[0][2], "23:12")
        self.assertEqual(summary_mock.call_args.kwargs["provider_session_id"], "")
        self.assertEqual(summary_mock.call_args.kwargs["previous_summary_path"], previous_summary)

    def test_bug_reanalysis_signal_followup_overrides_target_and_collects_source_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp), workspace_root=Path(tmp), guideengine_repo=Path(tmp) / "guideengine")
            runner = BugAnalysisRunner(config)
            job_dir = Path(tmp) / "jobs" / "job_1"
            output_dir = job_dir / "output"
            output_dir.mkdir(parents=True, exist_ok=True)
            prepared_input = Path(tmp) / "logs"
            prepared_input.mkdir(parents=True, exist_ok=True)
            repo_file = config.guideengine_repo / "module_floorcenter/module_proto/src/main/proto/signal.proto"
            repo_file.parent.mkdir(parents=True, exist_ok=True)
            repo_file.write_text(
                "SIGNAL_VCU_ELECTRICIT_PERCENT = 40018;// 电池电量值\n",
                encoding="utf-8",
            )
            signal_html = output_dir / "bug_signal_chain_report.html"
            signal_json = output_dir / "bug_signal_chain_report.json"
            signal_html.write_text("<html>old</html>", encoding="utf-8")
            signal_json.write_text('{"signal":{"code":"235082","name":"UNKNOWN_235082"}}', encoding="utf-8")
            previous_summary = output_dir / "bug_agent_summary.md"
            previous_summary.write_text("old summary", encoding="utf-8")
            analysis_inputs = []

            def fake_run_analysis(*, plan, input_path, html_path, json_path, analysis_dir, timeout, target_time, request_text=None):
                analysis_inputs.append((plan.kind, plan.signal_code, input_path))
                html_path.write_text("<html>new signal</html>", encoding="utf-8")
                json_path.write_text(
                    '{"signal":{"code":"40018","name":"SIGNAL_VCU_ELECTRICIT_PERCENT"}}',
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(args=["python3"], returncode=0, stdout="", stderr="")

            previous_session = {
                "job_id": "job_1",
                "job_dir": str(job_dir),
                "details": {
                    "analysis_kinds": ["signal"],
                    "signal_code": "235082",
                    "prepared_log_input": str(prepared_input),
                    "selected_log_input": str(prepared_input),
                    "user_request_text": (
                        "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6986570719 "
                        "分析信号链路 主要是VCU_ELECTRICIT_PERCENT，请参考源码分析"
                    ),
                    "agent_summary_file": str(previous_summary),
                    "agent_summary_session_id": "sess_123",
                },
            }
            previous_context = mock.Mock(
                request_text=previous_session["details"]["user_request_text"],
                summary_text="上一轮误判为 235082",
                report_excerpt="UNKNOWN_235082",
                history=[],
            )

            with (
                mock.patch.object(runner, "_run_analysis", side_effect=fake_run_analysis),
                mock.patch.object(
                    runner,
                    "_run_bug_decision_agent",
                    return_value=(
                        {
                            "action": "reanalyze",
                            "analysis_kind": "signal",
                            "skill": "signal-chain-analyzer",
                            "signal_hint": "VCU_ELECTRICIT_PERCENT",
                            "retry_download_if_missing": False,
                            "reason": "需要切换到新的信号定义重新分析。",
                        },
                        "codex",
                    ),
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
                ),
            ):
                followup_selection = runner.decide_bug_followup(
                    followup_text="分析结果不合理，在信号定义找到VCU_ELECTRICIT_PERCENT相关的信号定义，然后根据源码分析",
                    previous_context=previous_context,
                    previous_session=previous_session,
                )
                result = runner.run_bug_reanalysis(
                    followup_text="分析结果不合理，在信号定义找到VCU_ELECTRICIT_PERCENT相关的信号定义，然后根据源码分析",
                    previous_context=previous_context,
                    previous_session=previous_session,
                    plans_override=followup_selection.plans if followup_selection else None,
                    classification_skill=followup_selection.skill_name if followup_selection else "",
                    classification_source=followup_selection.source if followup_selection else "",
                    classification_reason=followup_selection.reason if followup_selection else "",
                    classification_provider=followup_selection.provider if followup_selection else "",
                )
                evidence_path = Path(result.details["source_evidence_file"])
                evidence_exists = evidence_path.exists()
                evidence = evidence_path.read_text(encoding="utf-8")
                metadata = Path(result.details["reanalysis_metadata_file"]).read_text(encoding="utf-8")

        self.assertTrue(result.success)
        self.assertEqual(result.details["rerun_analysis_kinds"], ["signal"])
        self.assertEqual(result.details["reused_analysis_kinds"], [])
        self.assertEqual(analysis_inputs, [("signal", "SIGNAL_VCU_ELECTRICIT_PERCENT", prepared_input)])
        self.assertEqual(result.details["signal_code"], "SIGNAL_VCU_ELECTRICIT_PERCENT")
        self.assertTrue(evidence_exists)
        self.assertIn("SIGNAL_VCU_ELECTRICIT_PERCENT", evidence)
        self.assertIn("本轮源码证据", metadata)

    def test_bug_reanalysis_source_evidence_uses_general_business_terms(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp), workspace_root=Path(tmp), guideengine_repo=Path(tmp) / "guideengine")
            runner = BugAnalysisRunner(config)
            job_dir = Path(tmp) / "jobs" / "job_1"
            output_dir = job_dir / "output"
            output_dir.mkdir(parents=True, exist_ok=True)
            prepared_input = Path(tmp) / "logs"
            prepared_input.mkdir(parents=True, exist_ok=True)
            repo_file = config.guideengine_repo / "module_core/manager_carscene/src/main/java/demo/CampingModeAction.kt"
            repo_file.parent.mkdir(parents=True, exist_ok=True)
            repo_file.write_text(
                "const val SIGNAL_CAMPING_MODE_STATUS = 50123\n"
                "fun handleCampingMode() = println(\"露营模式状态异常\")\n",
                encoding="utf-8",
            )
            (output_dir / "bug_signal_chain_report.html").write_text("<html>old</html>", encoding="utf-8")
            (output_dir / "bug_signal_chain_report.json").write_text("{}", encoding="utf-8")

            previous_session = {
                "job_id": "job_1",
                "job_dir": str(job_dir),
                "details": {
                    "analysis_kinds": ["signal"],
                    "signal_code": "123456",
                    "prepared_log_input": str(prepared_input),
                    "selected_log_input": str(prepared_input),
                    "user_request_text": "分析信号链路 主要是CAMPING_MODE_STATUS",
                },
            }
            previous_context = mock.Mock(
                request_text=previous_session["details"]["user_request_text"],
                summary_text="",
                report_excerpt="",
                history=[],
            )

            with (
                mock.patch.object(
                    runner,
                    "_run_analysis",
                    side_effect=lambda **kwargs: subprocess.CompletedProcess(
                        args=["python3"], returncode=0, stdout="", stderr=""
                    ),
                ),
                mock.patch.object(
                    runner,
                    "_run_bug_decision_agent",
                    return_value=(
                        {
                            "action": "reanalyze",
                            "analysis_kind": "signal",
                            "skill": "signal-chain-analyzer",
                            "signal_hint": "CAMPING_MODE_STATUS",
                            "retry_download_if_missing": False,
                            "reason": "结合信号定义和露营模式源码，需要按 signal-chain-analyzer 重分析。",
                        },
                        "claude",
                    ),
                ),
                mock.patch.object(
                    runner,
                    "_run_bug_agent_summary",
                    return_value={
                        "message": "agent continued",
                        "command": [],
                        "error": "",
                        "provider": "codex",
                        "session_id": "",
                        "resumed": False,
                    },
                ),
            ):
                followup_selection = runner.decide_bug_followup(
                    followup_text="结果不合理，在信号定义找到CAMPING_MODE_STATUS，然后结合露营模式源码重新分析",
                    previous_context=previous_context,
                    previous_session=previous_session,
                )
                result = runner.run_bug_reanalysis(
                    followup_text="结果不合理，在信号定义找到CAMPING_MODE_STATUS，然后结合露营模式源码重新分析",
                    previous_context=previous_context,
                    previous_session=previous_session,
                    plans_override=followup_selection.plans if followup_selection else None,
                    classification_skill=followup_selection.skill_name if followup_selection else "",
                    classification_source=followup_selection.source if followup_selection else "",
                    classification_reason=followup_selection.reason if followup_selection else "",
                    classification_provider=followup_selection.provider if followup_selection else "",
                )
                evidence = Path(result.details["source_evidence_file"]).read_text(encoding="utf-8")

        self.assertTrue(result.success)
        self.assertEqual(result.details["signal_code"], "SIGNAL_CAMPING_MODE_STATUS")
        self.assertIn("SIGNAL_CAMPING_MODE_STATUS", evidence)
        self.assertIn("露营模式", evidence)

    def test_bug_agent_followup_uses_fresh_agent_session_by_default(self):
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
                    "command": ["codex", "exec"],
                    "error": "",
                    "provider": "codex",
                    "session_id": "fresh_sess",
                    "resumed": False,
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
        self.assertEqual(result.details["agent_summary_session_id"], "fresh_sess")
        self.assertNotIn("agent_summary_resumed", result.details)
        self.assertIn("卡顿skill", request_text)
        self.assertIn(str(prepared_input), metadata_text)
        self.assertIn(str(combined_html), metadata_text)
        self.assertEqual(summary_mock.call_args.kwargs["provider_session_id"], "")
        self.assertEqual(summary_mock.call_args.kwargs["previous_summary_path"], previous_summary)

    def test_bug_agent_followup_can_opt_into_saved_agent_session_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp), workspace_root=Path(tmp))
            config.bug_analysis.resume_followup_sessions = True
            runner = BugAnalysisRunner(config)
            job_dir = Path(tmp) / "jobs" / "job_1"
            output_dir = job_dir / "output"
            output_dir.mkdir(parents=True, exist_ok=True)
            previous_summary = output_dir / "bug_agent_summary.md"
            previous_summary.write_text("old summary", encoding="utf-8")
            previous_session = {
                "job_id": "job_1",
                "job_dir": str(job_dir),
                "details": {
                    "user_request_text": "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 分析",
                    "agent_summary_file": str(previous_summary),
                    "agent_summary_session_id": "sess_123",
                },
            }
            previous_context = mock.Mock(
                request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 分析",
                summary_text="上一轮摘要",
                report_excerpt="上一轮摘录",
                report_url="",
                history=[],
            )
            with mock.patch.object(
                runner,
                "_run_bug_agent_summary",
                return_value={
                    "message": "agent resumed reply",
                    "command": ["codex", "exec", "resume"],
                    "error": "",
                    "provider": "codex",
                    "session_id": "sess_123",
                    "resumed": True,
                },
            ) as summary_mock:
                result = runner.run_bug_agent_followup(
                    followup_text="继续原 Agent 会话看一下刚才的推理链",
                    previous_context=previous_context,
                    previous_session=previous_session,
                )

        self.assertTrue(result.success)
        self.assertEqual(summary_mock.call_args.kwargs["provider_session_id"], "sess_123")
        self.assertTrue(result.details["agent_summary_resumed"])

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
