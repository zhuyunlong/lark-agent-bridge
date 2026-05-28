import os
from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import zipfile
from unittest import mock

import lark_agent_bridge.prompt_snapshots as prompt_snapshots_module
from lark_agent_bridge.agents import (
    BugAnalysisPlan,
    BugAnalysisRunner,
    ClaudeSkillRunner,
    IntentAnalysisFailure,
    IntentAnalysisRunner,
    OmlxChatClient,
    PerceptionSummaryRunner,
)
from lark_agent_bridge.agents.bug_summary_policy import (
    SummaryBackendInput,
    choose_summary_backend,
)
from lark_agent_bridge.agents.llm_client import LLMClientError
from lark_agent_bridge.models import (
    BridgeConfig,
    BugRequest,
    DirectAnalysisRequest,
    DownloadedResource,
    DownloadResource,
    InternalNetworkEnvOptions,
    IntentDecision,
    LarkEvent,
    PerceptionSummaryRequest,
    SourceInvestigationOptions,
    TaskResult,
)
from lark_agent_bridge.prompt_snapshots import (
    BugPromptSnapshot,
    SnapshotEvidence,
    SnapshotFact,
)
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

    def _write_matching_log(self, root: Path, timestamp: str = "2026-05-16 10:01:00") -> Path:
        path = root / "time_anchor.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{timestamp} I TestTag: time anchor\n", encoding="utf-8")
        return path

    def test_bug_analysis_subprocess_gets_default_debug_log_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = BugAnalysisRunner(
                BridgeConfig(dry_run=False, data_dir=Path(tmp), workspace_root=Path(tmp))
            )
            analysis_dir = Path(tmp) / "job" / "output" / "scene_signal_analysis"
            html_path = Path(tmp) / "job" / "output" / "bug_scene_signal_report.html"
            json_path = Path(tmp) / "job" / "output" / "bug_scene_signal_report.json"

            with (
                mock.patch.object(runner, "build_command", return_value=["python3", "script.py"]),
                mock.patch(
                    "lark_agent_bridge.agents.bug_runner._run_tracked_process",
                    return_value=subprocess.CompletedProcess(["python3"], 0, "ok", ""),
                ) as run_mock,
            ):
                runner._run_analysis(
                    plan=BugAnalysisPlan(kind="scene_signal"),
                    input_path=Path(tmp),
                    html_path=html_path,
                    json_path=json_path,
                    analysis_dir=analysis_dir,
                    timeout=5,
                    bridge_session_id="session_1",
                )

        debug_path = run_mock.call_args.kwargs["debug_log_path"]
        self.assertEqual(debug_path.name, "session_1.scene_signal-analysis.debug.log")
        self.assertEqual(debug_path.parent, analysis_dir)

    def test_scene_signal_analysis_decodes_raw_alog_before_extracting_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace_root = Path(tmp) / "workspace"
            decoder_script = workspace_root / ".ai" / "skills" / "log-decoder" / "tools" / "alog_decoder.py"
            scene_script = workspace_root / ".ai" / "skills" / "scene-signal-diagnosis" / "scripts" / "extract_scene_signal_events.py"
            decoder_script.parent.mkdir(parents=True)
            scene_script.parent.mkdir(parents=True)
            decoder_script.write_text("# decoder\n", encoding="utf-8")
            scene_script.write_text("# scene\n", encoding="utf-8")
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp) / "data", workspace_root=workspace_root)
            runner = BugAnalysisRunner(config)
            log_root = Path(tmp) / "logs"
            target_dir = log_root / "app" / "com.xiaopeng.montecarlo"
            target_dir.mkdir(parents=True)
            raw_log = target_dir / "user0_main_2026-05-25_16-00.alog"
            raw_log.write_bytes(b"\x06raw-alog")
            analysis_dir = Path(tmp) / "analysis"
            html_path = Path(tmp) / "bug_scene_signal_report.html"
            json_path = Path(tmp) / "bug_scene_signal_report.json"
            commands: list[list[str]] = []

            def fake_process(command, **kwargs):
                commands.append(command)
                script = Path(command[1]).name if len(command) > 1 else ""
                if script == "alog_decoder.py":
                    Path(str(raw_log) + ".log").write_text("decoded scene signal log\n", encoding="utf-8")
                if script == "extract_scene_signal_events.py":
                    analysis_dir.mkdir(parents=True, exist_ok=True)
                    (analysis_dir / "scene_signal_events.html").write_text("<html>scene</html>", encoding="utf-8")
                    (analysis_dir / "scene_signal_events.json").write_text('{"event_count": 1}', encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "ok", "")

            with mock.patch(
                "lark_agent_bridge.agents.bug_runner._run_tracked_process",
                side_effect=fake_process,
            ):
                runner._run_analysis(
                    plan=BugAnalysisPlan(kind="scene_signal"),
                    input_path=log_root,
                    html_path=html_path,
                    json_path=json_path,
                    analysis_dir=analysis_dir,
                    timeout=5,
                    target_time="2026-05-25 16:50:41",
                )

            self.assertGreaterEqual(len(commands), 2)
            self.assertEqual(Path(commands[0][0]).name, "python3")
            self.assertEqual(Path(commands[0][1]).name, "alog_decoder.py")
            self.assertEqual(commands[0][-1], "2026-05-25 16")
            self.assertEqual(Path(commands[1][1]).name, "extract_scene_signal_events.py")
            self.assertTrue(html_path.exists())
            self.assertTrue(json_path.exists())

    def test_scene_signal_analysis_decodes_single_raw_alog_without_time_arg(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace_root = Path(tmp) / "workspace"
            decoder_script = workspace_root / ".ai" / "skills" / "log-decoder" / "tools" / "alog_decoder.py"
            decoder_script.parent.mkdir(parents=True)
            decoder_script.write_text("# decoder\n", encoding="utf-8")
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp) / "data", workspace_root=workspace_root)
            runner = BugAnalysisRunner(config)
            raw_log = Path(tmp) / "main_2026-05-25_16-00.alog"
            raw_log.write_bytes(b"\x06raw-alog")
            analysis_dir = Path(tmp) / "analysis"

            with mock.patch(
                "lark_agent_bridge.agents.bug_runner._run_tracked_process",
                return_value=subprocess.CompletedProcess(["python3"], 0, "ok", ""),
            ) as run_mock:
                runner._decode_raw_logs_before_analysis(
                    plan=BugAnalysisPlan(kind="scene_signal"),
                    input_path=raw_log,
                    analysis_dir=analysis_dir,
                    target_time="2026-05-25 16:50:41",
                )

            command = run_mock.call_args.args[0]
            self.assertEqual(Path(command[1]).name, "alog_decoder.py")
            self.assertEqual(command[2], str(raw_log))
            self.assertEqual(len(command), 3)

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

    def test_omlx_chat_401_without_key_reports_missing_api_key(self):
        config = BridgeConfig(dry_run=False)
        config.omlx_chat.api_key = ""
        error = urllib.error.HTTPError(
            url="http://127.0.0.1:8000/v1/chat/completions",
            code=401,
            msg="Unauthorized",
            hdrs=None,
            fp=None,
        )

        with mock.patch("urllib.request.urlopen", side_effect=error):
            result = OmlxChatClient(config).reply("你好")

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "omlx_auth_missing")
        self.assertIn("LARK_AGENT_BRIDGE_OMLX_API_KEY", result.message)

    def test_omlx_chat_401_with_key_reports_invalid_api_key(self):
        config = BridgeConfig(dry_run=False)
        config.omlx_chat.api_key = "wrong-key"
        error = urllib.error.HTTPError(
            url="http://127.0.0.1:8000/v1/chat/completions",
            code=401,
            msg="Unauthorized",
            hdrs=None,
            fp=None,
        )

        with mock.patch("urllib.request.urlopen", side_effect=error):
            result = OmlxChatClient(config).reply("你好")

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "omlx_auth_invalid")
        self.assertIn("已配置的 OMLX API key 被拒绝", result.message)

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
        self.assertEqual(command[command.index("-m") + 1], "gpt-5.4")

    def test_intent_analysis_does_not_fall_back_to_other_provider(self):
        config = BridgeConfig(dry_run=False)
        config.intent_analysis.enabled = True
        config.intent_analysis.provider = "codex"
        config.intent_analysis.command = "codex"
        runner = IntentAnalysisRunner(config)
        response_path = Path("/tmp/intent-response.json")

        with (
            mock.patch.object(runner, "_build_command", return_value=(["codex", "exec"], response_path)),
            mock.patch.object(runner, "_fallback_intent_invocation", return_value=([], None)) as fallback_mock,
            mock.patch("subprocess.run", side_effect=OSError("codex missing")),
        ):
            with self.assertRaises(IntentAnalysisFailure):
                runner.classify(
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

        self.assertTrue(fallback_mock.called)

    def test_intent_analysis_api_failure_does_not_use_subprocess_by_default(self):
        config = BridgeConfig(dry_run=False)
        config.intent_analysis.enabled = True
        runner = IntentAnalysisRunner(config)
        runner._llm_client = mock.Mock()
        runner._llm_client.is_available.return_value = True
        api_error = LLMClientError("endpoint unavailable", error_code="llm_api_error")

        with (
            mock.patch.object(runner, "_classify_via_api", side_effect=api_error),
            mock.patch.object(runner, "_classify_via_subprocess") as subprocess_mock,
        ):
            with self.assertRaises(IntentAnalysisFailure) as raised:
                runner.classify(
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

        self.assertEqual(raised.exception.error_code, "intent_analysis_api_failed")
        subprocess_mock.assert_not_called()

    def test_intent_analysis_api_failure_can_use_subprocess_when_enabled(self):
        config = BridgeConfig(dry_run=False)
        config.intent_analysis.enabled = True
        config.intent_analysis.allow_subprocess_fallback = True
        runner = IntentAnalysisRunner(config)
        runner._llm_client = mock.Mock()
        runner._llm_client.is_available.return_value = True
        expected = IntentDecision(
            route="chat",
            followup_action="none",
            context_source="none",
            confidence="medium",
            reason="fallback",
        )

        with (
            mock.patch.object(
                runner,
                "_classify_via_api",
                side_effect=LLMClientError("endpoint unavailable", error_code="llm_api_error"),
            ),
            mock.patch.object(runner, "_classify_via_subprocess", return_value=expected) as subprocess_mock,
        ):
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

        self.assertIs(decision, expected)
        subprocess_mock.assert_called_once()

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

    def test_claude_skill_prompt_is_passed_via_stdin_not_after_add_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp), workspace_root=Path(tmp))
            config.claude_agent.add_dirs = [Path(tmp)]
            completed = subprocess.CompletedProcess(
                args=["claude"],
                returncode=0,
                stdout="分析完成",
                stderr="",
            )
            with mock.patch("subprocess.run", return_value=completed) as run_mock:
                result = ClaudeSkillRunner(config).run_skill_analysis(
                    __import__("lark_agent_bridge.models", fromlist=["ClaudeSkillRequest"]).ClaudeSkillRequest(
                        prompt="根据导航源码分析",
                        raw_text="/skill 根据导航源码分析",
                        triggered=True,
                    )
                )

            self.assertTrue(result.success)
            command = run_mock.call_args.args[0]
            kwargs = run_mock.call_args.kwargs
            self.assertIn("--add-dir", command)
            self.assertEqual(command[-1], str(Path(tmp)))
            self.assertIn("根据导航源码分析", kwargs["input"])

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
            self.assertEqual((Path(result.job_dir) / "logs" / "claude_skill.stderr.log").read_text(encoding="utf-8"), "boom")

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
        self.assertEqual(command[command.index("-m") + 1], "gpt-5.4")
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
                "model": "gpt-5.4",
                "usage": {"input_tokens": 1197553, "output_tokens": 13026, "total_tokens": 1210579},
                "duration_seconds": 307.0,
                "session_id": "sess-1",
                "resumed": False,
            },
            total_duration_seconds=370.5,
        )

        self.assertIn("Agent 模型", html)
        self.assertIn("gpt-5.4", html)
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
        self.assertEqual(invocation["command"][invocation["command"].index("-m") + 1], "gpt-5.4")

    def test_bug_agent_summary_followup_prompt_embeds_compact_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=True, data_dir=Path(tmp), workspace_root=Path(tmp))
            runner = BugAnalysisRunner(config)
            request_artifact = Path(tmp) / "bug_agent_followup_request.md"
            metadata_path = Path(tmp) / "bug_agent_followup_metadata.md"
            previous_summary = Path(tmp) / "bug_agent_summary.md"
            snapshot_path = Path(tmp) / "conversation_facts.json"
            long_assistant_history = "上一轮长结论" + "A" * 6000
            request_artifact.write_text(
                runner._render_bug_agent_followup_request(
                    request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 分析主题",
                    followup_text="问题时刻系统主题是什么",
                    history=[
                        {"role": "user", "content": "继续看主题变化"},
                        {"role": "assistant", "content": long_assistant_history},
                    ],
                ),
                encoding="utf-8",
            )
            metadata_path.write_text(
                "- 用户原始请求: `https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 分析主题`\n"
                "- 分析类型: `startup`\n"
                "- 复用 prepared log 输入: `/tmp/from-metadata.log`\n"
                "- report_version: `3`\n",
                encoding="utf-8",
            )
            previous_summary.write_text("summary\n" + "S" * 10000, encoding="utf-8")

            with (
                mock.patch(
                    "lark_agent_bridge.prompt_snapshots.write_prompt_snapshot",
                    wraps=prompt_snapshots_module.write_prompt_snapshot,
                ) as write_snapshot_mock,
                mock.patch(
                    "lark_agent_bridge.prompt_snapshots.render_bug_snapshot_prefix",
                    return_value="### 会话事实快照\n- stable_fact: xtheme\n",
                ) as render_snapshot_mock,
            ):
                prompt = runner._build_bug_agent_summary_prompt(
                    request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 分析主题",
                    request_artifact=request_artifact,
                    metadata_path=metadata_path,
                    followup_text="问题时刻系统主题是什么",
                    previous_summary_path=previous_summary,
                    snapshot_details={
                        "analysis_kind": "xtheme",
                        "analysis_kinds": ["xtheme"],
                        "prepared_log_input": "/tmp/prepared.log",
                        "report_version": 7,
                        "bug_url": "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722",
                    },
                    snapshot_plans=[BugAnalysisPlan(kind="xtheme")],
                )

            request_body = request_artifact.read_text(encoding="utf-8")
            snapshot_text = snapshot_path.read_text(encoding="utf-8")

        self.assertNotIn("上一轮摘要", request_body)
        self.assertNotIn(long_assistant_history, request_body)
        self.assertIn("续聊性能约束", prompt)
        self.assertIn("### 会话事实快照\n- stable_fact: xtheme", prompt)
        self.assertIn("Bug Agent Follow-up Request", prompt)
        self.assertIn("Bug Follow-up Metadata", prompt)
        self.assertLess(len(prompt), 18000)
        self.assertNotIn(long_assistant_history, prompt)
        self.assertNotIn("上一轮 Agent 总结", prompt)
        write_snapshot_mock.assert_called_once()
        render_snapshot_mock.assert_called_once()
        persisted_snapshot = write_snapshot_mock.call_args.args[1]
        self.assertEqual(persisted_snapshot.analysis_kind, "xtheme")
        self.assertNotIn(long_assistant_history, snapshot_text)
        self.assertNotIn("summary_text", snapshot_text)
        self.assertNotIn("report_excerpt", snapshot_text)
        self.assertNotIn("history", snapshot_text)

    def test_bug_agent_summary_followup_prompt_embeds_referenced_report_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=True, data_dir=Path(tmp), workspace_root=Path(tmp))
            runner = BugAnalysisRunner(config)
            output_dir = Path(tmp) / "jobs" / "job_1" / "output"
            output_dir.mkdir(parents=True, exist_ok=True)
            request_artifact = output_dir / "bug_agent_reanalysis_request.md"
            metadata_path = output_dir / "bug_reanalysis_metadata.md"
            source_evidence = output_dir / "bug_source_evidence.md"
            report_json = output_dir / "bug_3d_stuck_report.json"
            request_artifact.write_text("本次追问: 基于源码分析 unity场景", encoding="utf-8")
            source_evidence.write_text(
                "module_scene/UnitySceneRouter.kt:42 fun chooseUnityScene() = \"SR\"",
                encoding="utf-8",
            )
            report_json.write_text('{"summary":"P挡时 Unity 场景没有进入 SR"}', encoding="utf-8")
            metadata_path.write_text(
                f"- 本轮源码证据: `{source_evidence}`\n"
                "- 最新 JSON 报告:\n"
                f"  - `stuck` -> `{report_json}`\n",
                encoding="utf-8",
            )

            prompt = runner._build_bug_agent_summary_prompt(
                request_text="分析3D生命周期",
                request_artifact=request_artifact,
                metadata_path=metadata_path,
                followup_text="基于源码分析 unity场景",
            )

        self.assertIn("Bug Source Evidence", prompt)
        self.assertIn(str(source_evidence), prompt)
        self.assertIn(str(report_json), prompt)
        self.assertIn("源码证据文件；需要源码链路时读取", prompt)
        self.assertIn("结构化分析结果，必须优先读取", prompt)
        self.assertNotIn("UnitySceneRouter.kt", prompt)
        self.assertNotIn("P挡时 Unity 场景没有进入 SR", prompt)

    def test_omlx_bug_summary_prompt_embeds_referenced_report_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=True, data_dir=Path(tmp), workspace_root=Path(tmp))
            config.omlx_chat.max_prompt_chars = 9000
            runner = BugAnalysisRunner(config)
            output_dir = Path(tmp) / "jobs" / "job_1" / "output"
            output_dir.mkdir(parents=True, exist_ok=True)
            request_artifact = output_dir / "bug_agent_reanalysis_request.md"
            metadata_path = output_dir / "bug_reanalysis_metadata.md"
            source_evidence = output_dir / "bug_source_evidence.md"
            report_json = output_dir / "bug_3d_startup_report.json"
            request_artifact.write_text("本次追问: 基于源码分析 unity场景", encoding="utf-8")
            source_evidence.write_text("UnitySceneRouter.kt:42 P挡选择 ParkingScene", encoding="utf-8")
            report_json.write_text('{"summary":"主 PID 2466，Unity 场景切换缺少完成日志"}', encoding="utf-8")
            metadata_path.write_text(
                f"- 本轮源码证据: `{source_evidence}`\n"
                "- 最新 JSON 报告:\n"
                f"  - `startup` -> `{report_json}`\n",
                encoding="utf-8",
            )

            prompt = runner._build_omlx_bug_summary_prompt(
                request_text="分析3D生命周期",
                request_artifact=request_artifact,
                metadata_path=metadata_path,
                followup_text="基于源码分析 unity场景",
            )

        self.assertIn(str(source_evidence), prompt)
        self.assertIn(str(report_json), prompt)
        self.assertIn("轻量模型不能读取本地文件", prompt)
        self.assertNotIn("UnitySceneRouter.kt", prompt)
        self.assertNotIn("主 PID 2466", prompt)

    def test_bug_reanalysis_prompt_for_api_uses_snapshot_prefix_before_incremental_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=True, data_dir=Path(tmp), workspace_root=Path(tmp))
            runner = BugAnalysisRunner(config)
            output_dir = Path(tmp) / "jobs" / "job_1" / "output"
            output_dir.mkdir(parents=True, exist_ok=True)
            request_artifact = output_dir / "bug_agent_reanalysis_request.md"
            metadata_path = output_dir / "bug_reanalysis_metadata.md"
            previous_summary = output_dir / "bug_agent_summary.md"
            snapshot_path = output_dir / "conversation_facts.json"
            request_artifact.write_text("本次追问: 重新源码分析 重点看 displaychange", encoding="utf-8")
            metadata_path.write_text(
                "- 用户原始请求: `https://project.feishu.cn/xpfailuremgmt/buglo/detail/6995823164 调查3D启动生命周期`\n"
                "- 修正后的故障时间: `2026-05-21 03:38:17`\n"
                "- 分析类型: `startup`\n"
                "- 复用 prepared log 输入: `/tmp/prepared.log`\n"
                "- report_version: `7`\n",
                encoding="utf-8",
            )
            previous_summary.write_text("summary\n" + "S" * 10000, encoding="utf-8")

            with (
                mock.patch(
                    "lark_agent_bridge.prompt_snapshots.write_prompt_snapshot",
                    wraps=prompt_snapshots_module.write_prompt_snapshot,
                ) as write_snapshot_mock,
                mock.patch(
                    "lark_agent_bridge.prompt_snapshots.render_bug_snapshot_prefix",
                    return_value="### 会话事实快照\n- stable_fact: startup\n",
                ) as render_snapshot_mock,
            ):
                prompt = runner._build_bug_agent_summary_prompt_for_api(
                    request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6995823164 调查3D启动生命周期",
                    request_artifact=request_artifact,
                    metadata_path=metadata_path,
                    followup_text="重新源码分析 重点看 displaychange",
                    previous_summary_path=previous_summary,
                    snapshot_details={
                        "analysis_kind": "signal",
                        "analysis_kinds": ["signal"],
                        "report_version": 9,
                        "prepared_log_input": "/tmp/structured.log",
                        "bug_url": "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6995823164",
                    },
                    snapshot_plans=[BugAnalysisPlan(kind="signal", signal_code="SIGNAL_VCU_ELECTRICIT_PERCENT")],
                )

            self.assertTrue(snapshot_path.exists())
            self.assertIn("### 会话事实快照\n- stable_fact: startup", prompt)
            self.assertIn("### 本次追问/修正\n重新源码分析 重点看 displaychange", prompt)
            self.assertLess(prompt.index("### 会话事实快照"), prompt.index("### 本次追问/修正"))
            self.assertIn("### 上一轮 Agent 总结", prompt)
            write_snapshot_mock.assert_called_once()
            render_snapshot_mock.assert_called_once()
            persisted_snapshot = write_snapshot_mock.call_args.args[1]
            self.assertEqual(persisted_snapshot.analysis_kind, "signal")
            self.assertEqual(
                [item.value for item in persisted_snapshot.stable_facts if item.label == "prepared_log_input"],
                ["/tmp/structured.log"],
            )

    def test_bug_reanalysis_direct_api_prompt_skips_html_excerpt_and_adds_structured_guardrails(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=True, data_dir=Path(tmp), workspace_root=Path(tmp))
            runner = BugAnalysisRunner(config)
            output_dir = Path(tmp) / "jobs" / "job_1" / "output"
            output_dir.mkdir(parents=True, exist_ok=True)
            request_artifact = output_dir / "bug_agent_reanalysis_request.md"
            metadata_path = output_dir / "bug_reanalysis_metadata.md"
            previous_summary = output_dir / "bug_agent_summary.md"
            report_json = output_dir / "bug_3d_startup_report.json"
            report_html = output_dir / "bug_3d_startup_report.html"
            request_artifact.write_text("本次追问: 重新分析", encoding="utf-8")
            previous_summary.write_text("旧结论：上一轮说链路完整\n", encoding="utf-8")
            report_json.write_text(
                json.dumps(
                    {
                        "verdict": {"message": "启动链路完整，已经到达 3D 最终首帧展示。"},
                        "focus_session_index": 4,
                        "focus_session_pid": 8066,
                        "sessions": [
                            {
                                "index": 4,
                                "status": "partial",
                                "diagnosis": "启动链路完整，已经到达 3D 最终首帧展示。",
                                "missing_critical": [
                                    "createUnityPlayerOnMainThread",
                                    "SET_READY_PREPARE / UnityReady",
                                ],
                                "events": [
                                    {
                                        "title": "Unity so preload 成功",
                                        "timestamp_text": "2026-05-22 17:30:03.946",
                                        "file_path": "/tmp/main.alog.log",
                                        "line_no": 22172,
                                        "pid": 8066,
                                    }
                                ],
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            report_html.write_text(
                "<style>body{color:red}</style><h1>HTML_ONLY_MISLEADING_TEXT</h1>",
                encoding="utf-8",
            )
            metadata_path.write_text(
                "- 最新 JSON 报告:\n"
                f"  - `startup` -> `{report_json}`\n"
                "- 最新 HTML 报告:\n"
                f"  - `{report_html}`\n",
                encoding="utf-8",
            )

            prompt = runner._build_bug_agent_summary_prompt_for_api(
                request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6997535619 分析3D生命周期",
                request_artifact=request_artifact,
                metadata_path=metadata_path,
                followup_text="重新分析",
                previous_summary_path=previous_summary,
                snapshot_details={
                    "analysis_kind": "startup",
                    "analysis_kinds": ["startup"],
                    "bug_url": "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6997535619",
                },
                snapshot_plans=[BugAnalysisPlan(kind="startup")],
            )

        self.assertIn("### 结构化证据护栏", prompt)
        self.assertIn("status=partial", prompt)
        self.assertIn("createUnityPlayerOnMainThread", prompt)
        self.assertIn("### 上一轮 Agent 总结", prompt)
        self.assertNotIn("HTML_ONLY_MISLEADING_TEXT", prompt)

    def test_write_bug_summary_evidence_for_startup_includes_conflicts_and_trailing_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=True, data_dir=Path(tmp), workspace_root=Path(tmp))
            runner = BugAnalysisRunner(config)
            output_dir = Path(tmp) / "jobs" / "job_1" / "output"
            output_dir.mkdir(parents=True, exist_ok=True)
            log_path = output_dir / "main_2026-05-22_17-00.alog.log"
            log_path.write_text(
                "\n".join(
                    [
                        "05-22 17:30:03.946 8066 8522 90081 I NAV_SrSM_SrUnityPlayer: preloadNativeLibrary: success, cost 20ms",
                        "05-22 17:30:03.946 8066 8522 90081 I NAV_UnityServiceManager: init UnityServiceManager start dpi: 152",
                        "05-22 17:30:03.946 8066 8522 90081 I NAV_AndroidUnityConnector: init",
                        "05-22 17:30:03.949 8066 8547 90084 E NAV_guideService: java.lang.IllegalStateException: sdk 引擎未初始化完成",
                        "05-22 17:30:04.055 8066 8567 90190 I NAV_UnityAdapter: Engine started, syncing state to core",
                    ]
                ),
                encoding="utf-8",
            )
            report_json = output_dir / "bug_3d_startup_report.json"
            report_json.write_text(
                json.dumps(
                    {
                        "verdict": {"message": "启动链路完整，已经到达 3D 最终首帧展示。"},
                        "target_time": "2026-05-22T17:29:48",
                        "focus_session_index": 4,
                        "focus_session_pid": 8066,
                        "sessions": [
                            {
                                "index": 4,
                                "status": "partial",
                                "diagnosis": "启动链路完整，已经到达 3D 最终首帧展示。",
                                "missing_critical": [
                                    "createUnityPlayerOnMainThread",
                                    "SET_READY_PREPARE / UnityReady",
                                    "UnityMainFirstFrameReadyRenderMsg",
                                ],
                                "events": [
                                    {
                                        "title": "Unity so preload 成功",
                                        "timestamp_text": "2026-05-22 17:30:03.946",
                                        "file_path": str(log_path),
                                        "line_no": 1,
                                        "pid": 8066,
                                        "excerpt": "05-22 17:30:03.946 8066 8522 90081 I NAV_SrSM_SrUnityPlayer: preloadNativeLibrary: success, cost 20ms",
                                    }
                                ],
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            evidence_path = runner._write_bug_summary_evidence(
                output_dir=output_dir,
                analysis_kind="startup",
                report_jsons={"startup": report_json},
            )
            self.assertIsNotNone(evidence_path)
            assert evidence_path is not None
            content = evidence_path.read_text(encoding="utf-8")

        self.assertIn("status: `partial`", content)
        self.assertIn("status=partial 但 report verdict/diagnosis 仍声称链路完整", content)
        self.assertIn("UnityServiceManager: init UnityServiceManager start dpi: 152", content)
        self.assertIn("sdk 引擎未初始化完成", content)
        self.assertIn("Forbidden assertions", content)
        self.assertIn("日志在 preload 后截止", content)

    def test_bug_summary_source_or_postmortem_followup_skips_direct_api_fast_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp), workspace_root=Path(tmp))
            config.bug_analysis.provider = "codex"
            config.bug_analysis.command = "codex"
            config.ai_provider.enabled = True
            config.ai_provider.base_url = "http://127.0.0.1:8000/v1"
            config.ai_provider.primary_model = "demo"
            runner = BugAnalysisRunner(config)
            output_path = Path(tmp) / "bug_agent_summary.md"
            request_artifact = Path(tmp) / "bug_agent_reanalysis_request.md"
            metadata_path = Path(tmp) / "bug_reanalysis_metadata.md"
            previous_summary = Path(tmp) / "bug_agent_summary_prev.md"
            request_artifact.write_text("followup", encoding="utf-8")
            metadata_path.write_text("metadata", encoding="utf-8")
            previous_summary.write_text("旧结论", encoding="utf-8")

            def fake_file_agent(command, **kwargs):
                output_path.write_text("file-capable agent summary", encoding="utf-8")
                return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

            with (
                mock.patch.object(runner, "_run_bug_agent_summary_via_api") as api_mock,
                mock.patch("lark_agent_bridge.agents.run_tracked_process", side_effect=fake_file_agent) as run_mock,
            ):
                result = runner._run_bug_agent_summary(
                    request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6997535619 分析3D生命周期",
                    request_artifact=request_artifact,
                    metadata_path=metadata_path,
                    output_path=output_path,
                    progress_callback=None,
                    timeout=30,
                    followup_text="基于源码重新分析，并针对前后结论冲突做复盘",
                    previous_summary_path=previous_summary,
                )

        self.assertEqual(api_mock.call_count, 0)
        self.assertEqual(run_mock.call_count, 1)
        self.assertEqual(result["provider"], "codex")
        self.assertEqual(result["message"], "file-capable agent summary")

    def test_bug_summary_report_conflict_skips_direct_api_fast_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp), workspace_root=Path(tmp))
            config.bug_analysis.provider = "codex"
            config.bug_analysis.command = "codex"
            config.ai_provider.enabled = True
            config.ai_provider.base_url = "http://127.0.0.1:8000/v1"
            config.ai_provider.primary_model = "demo"
            runner = BugAnalysisRunner(config)
            output_path = Path(tmp) / "bug_agent_summary.md"
            request_artifact = Path(tmp) / "bug_agent_reanalysis_request.md"
            metadata_path = Path(tmp) / "bug_reanalysis_metadata.md"
            report_json = Path(tmp) / "bug_3d_startup_report.json"
            request_artifact.write_text("followup", encoding="utf-8")
            report_json.write_text(
                json.dumps(
                    {
                        "verdict": {"message": "启动链路完整，已经到达 3D 最终首帧展示。"},
                        "focus_session_index": 4,
                        "focus_session_pid": 8066,
                        "sessions": [
                            {
                                "index": 4,
                                "status": "partial",
                                "diagnosis": "启动链路完整，已经到达 3D 最终首帧展示。",
                                "missing_critical": ["SET_READY_PREPARE / UnityReady"],
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            metadata_path.write_text(f"- 最新 JSON 报告:\n  - `startup` -> `{report_json}`\n", encoding="utf-8")

            def fake_file_agent(command, **kwargs):
                output_path.write_text("file-capable agent summary", encoding="utf-8")
                return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

            with (
                mock.patch.object(runner, "_run_bug_agent_summary_via_api") as api_mock,
                mock.patch("lark_agent_bridge.agents.run_tracked_process", side_effect=fake_file_agent) as run_mock,
            ):
                result = runner._run_bug_agent_summary(
                    request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6997535619 分析3D生命周期",
                    request_artifact=request_artifact,
                    metadata_path=metadata_path,
                    output_path=output_path,
                    progress_callback=None,
                    timeout=30,
                    followup_text="重新分析",
                )

        self.assertEqual(api_mock.call_count, 0)
        self.assertEqual(run_mock.call_count, 1)
        self.assertEqual(result["provider"], "codex")

    def test_summary_backend_policy_routes_source_or_conflict_to_file_agent(self):
        decision = choose_summary_backend(
            SummaryBackendInput(
                explicit_provider="",
                provider_session_id="",
                prefer_lightweight=False,
                ai_provider_enabled=True,
                ai_provider_base_url="http://127.0.0.1:8000/v1",
                ai_provider_primary_model="demo",
                skip_direct_api=True,
                direct_api_failure="",
                auto_fallback_to_file_agent=False,
            )
        )

        self.assertEqual(decision.backend, "file_agent")
        self.assertEqual(decision.reason, "direct_api_policy_skip")
        self.assertFalse(decision.fallback_from)

    def test_bug_summary_direct_api_failure_does_not_fallback_to_file_agent_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp), workspace_root=Path(tmp))
            config.bug_analysis.provider = "codex"
            config.bug_analysis.command = "codex"
            config.ai_provider.enabled = True
            config.ai_provider.base_url = "http://127.0.0.1:8000/v1"
            config.ai_provider.primary_model = "demo"
            runner = BugAnalysisRunner(config)
            output_path = Path(tmp) / "bug_agent_summary.md"
            request_artifact = Path(tmp) / "bug_agent_request.md"
            metadata_path = Path(tmp) / "bug_metadata.md"
            request_artifact.write_text("request", encoding="utf-8")
            metadata_path.write_text("metadata", encoding="utf-8")
            api_failure = {
                "message": "",
                "command": None,
                "error": "direct_api_error: timeout",
                "provider": "direct_api",
                "session_id": "",
                "resumed": False,
                "duration_seconds": 1.2,
                "usage": {},
                "usage_scope": "",
            }

            def fake_file_agent(command, **kwargs):
                output_path.write_text("file-capable agent summary", encoding="utf-8")
                return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

            with (
                mock.patch.object(runner, "_run_bug_agent_summary_via_api", return_value=api_failure) as api_mock,
                mock.patch("lark_agent_bridge.agents.run_tracked_process", side_effect=fake_file_agent) as run_mock,
            ):
                result = runner._run_bug_agent_summary(
                    request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 分析启动和卡顿",
                    request_artifact=request_artifact,
                    metadata_path=metadata_path,
                    output_path=output_path,
                    progress_callback=None,
                    timeout=30,
                )

        self.assertEqual(api_mock.call_count, 1)
        self.assertEqual(run_mock.call_count, 0)
        self.assertEqual(result["provider"], "direct_api")
        self.assertEqual(result["execution_backend"], "direct_api")
        self.assertEqual(result["error"], "direct_api_error: timeout")
        self.assertFalse(result.get("fallback_from"))

    def test_bug_reanalysis_snapshot_rebuilds_when_analysis_kind_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=True, data_dir=Path(tmp), workspace_root=Path(tmp))
            runner = BugAnalysisRunner(config)
            output_dir = Path(tmp) / "jobs" / "job_1" / "output"
            output_dir.mkdir(parents=True, exist_ok=True)
            snapshot_path = output_dir / "conversation_facts.json"
            snapshot_path.write_text("{}", encoding="utf-8")
            existing_snapshot = BugPromptSnapshot(
                scope_key="bug:6995823164:job_1",
                analysis_kind="startup",
                stable_facts=[SnapshotFact(label="target_time", value="2026-05-21 03:38:17")],
                evidence_refs=[
                    SnapshotEvidence(
                        title="startup",
                        path="/tmp/bug_3d_startup_report.json",
                        locator="",
                    )
                ],
                open_questions=["startup 旧问题"],
            )

            with (
                mock.patch(
                    "lark_agent_bridge.prompt_snapshots.read_prompt_snapshot",
                    return_value=existing_snapshot,
                ) as read_snapshot_mock,
                mock.patch(
                    "lark_agent_bridge.prompt_snapshots.write_prompt_snapshot",
                ) as write_snapshot_mock,
            ):
                snapshot = runner._build_or_refresh_bug_prompt_snapshot(
                    details={
                        "analysis_kind": "startup",
                        "report_version": 7,
                        "prepared_log_input": "/tmp/prepared.log",
                        "target_time": "2026-05-21 03:38:17",
                    },
                    followup_text="改查信号链路 SIGNAL_VCU_ELECTRICIT_PERCENT",
                    plans_override=[BugAnalysisPlan(kind="signal", signal_code="SIGNAL_VCU_ELECTRICIT_PERCENT")],
                    request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6995823164 调查3D启动生命周期",
                    output_dir=output_dir,
                )

            self.assertEqual(snapshot.analysis_kind, "signal")
            read_snapshot_mock.assert_called_once_with(snapshot_path)
            write_snapshot_mock.assert_called_once()
            persisted_snapshot = write_snapshot_mock.call_args.args[1]
            self.assertEqual(persisted_snapshot.analysis_kind, "signal")

    def test_bug_snapshot_prefix_keeps_open_questions_but_not_prior_conclusion_paragraphs(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=True, data_dir=Path(tmp), workspace_root=Path(tmp))
            runner = BugAnalysisRunner(config)
            request_artifact = Path(tmp) / "bug_agent_followup_request.md"
            metadata_path = Path(tmp) / "bug_agent_followup_metadata.md"
            previous_summary = Path(tmp) / "bug_agent_summary.md"
            request_artifact.write_text("followup request", encoding="utf-8")
            metadata_path.write_text(
                "- 用户原始请求: `https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 分析主题`\n"
                "- 分析类型: `xtheme`\n",
                encoding="utf-8",
            )
            previous_summary.write_text("旧结论第一段\n旧结论第二段\n", encoding="utf-8")

            prompt = runner._build_bug_agent_summary_prompt(
                request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 分析主题",
                request_artifact=request_artifact,
                metadata_path=metadata_path,
                followup_text="重新源码分析 displaychange",
                previous_summary_path=previous_summary,
                snapshot_details={
                    "analysis_kind": "xtheme",
                    "analysis_kinds": ["xtheme"],
                    "bug_url": "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722",
                },
                snapshot_plans=[BugAnalysisPlan(kind="xtheme")],
            )

        self.assertNotIn("### 上一轮 Agent 总结", prompt)
        self.assertNotIn("旧结论第一段", prompt)

    def test_bug_file_capable_prompt_reads_previous_summary_only_for_explicit_compare_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=True, data_dir=Path(tmp), workspace_root=Path(tmp))
            runner = BugAnalysisRunner(config)
            request_artifact = Path(tmp) / "bug_agent_followup_request.md"
            metadata_path = Path(tmp) / "bug_agent_followup_metadata.md"
            previous_summary = Path(tmp) / "bug_agent_summary.md"
            request_artifact.write_text("followup request", encoding="utf-8")
            metadata_path.write_text(
                "- 用户原始请求: `https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 分析主题`\n"
                "- 分析类型: `xtheme`\n",
                encoding="utf-8",
            )
            previous_summary.write_text("旧结论：displayChanged 是关键线索\n", encoding="utf-8")

            prompt = runner._build_bug_agent_summary_prompt(
                request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 分析主题",
                request_artifact=request_artifact,
                metadata_path=metadata_path,
                followup_text="对比上一轮结论，解释为什么你上次判断 displayChanged 是关键线索",
                previous_summary_path=previous_summary,
                snapshot_details={
                    "analysis_kind": "xtheme",
                    "analysis_kinds": ["xtheme"],
                    "bug_url": "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722",
                },
                snapshot_plans=[BugAnalysisPlan(kind="xtheme")],
            )

        self.assertIn("上一轮 Agent 总结", prompt)

    def test_bug_file_capable_prompt_reads_previous_summary_for_explain_old_conclusion_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=True, data_dir=Path(tmp), workspace_root=Path(tmp))
            runner = BugAnalysisRunner(config)
            request_artifact = Path(tmp) / "bug_agent_followup_request.md"
            metadata_path = Path(tmp) / "bug_agent_followup_metadata.md"
            previous_summary = Path(tmp) / "bug_agent_summary.md"
            request_artifact.write_text("followup request", encoding="utf-8")
            metadata_path.write_text(
                "- 用户原始请求: `https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 分析主题`\n"
                "- 分析类型: `xtheme`\n",
                encoding="utf-8",
            )
            previous_summary.write_text("旧结论：displayChanged 是关键线索\n", encoding="utf-8")

            prompt = runner._build_bug_agent_summary_prompt(
                request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 分析主题",
                request_artifact=request_artifact,
                metadata_path=metadata_path,
                followup_text="上次为什么判断 displayChanged 是关键线索",
                previous_summary_path=previous_summary,
                snapshot_details={
                    "analysis_kind": "xtheme",
                    "analysis_kinds": ["xtheme"],
                    "bug_url": "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722",
                },
                snapshot_plans=[BugAnalysisPlan(kind="xtheme")],
            )

        self.assertIn("上一轮 Agent 总结", prompt)
        self.assertIn("displayChanged 是关键线索", prompt)

    def test_bug_direct_api_prompt_reads_previous_summary_only_for_explicit_compare_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=True, data_dir=Path(tmp), workspace_root=Path(tmp))
            runner = BugAnalysisRunner(config)
            request_artifact = Path(tmp) / "bug_agent_followup_request.md"
            metadata_path = Path(tmp) / "bug_agent_followup_metadata.md"
            previous_summary = Path(tmp) / "bug_agent_summary.md"
            request_artifact.write_text("followup request", encoding="utf-8")
            metadata_path.write_text(
                "- 用户原始请求: `https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 分析主题`\n"
                "- 分析类型: `xtheme`\n",
                encoding="utf-8",
            )
            previous_summary.write_text("旧结论：displayChanged 是关键线索\n", encoding="utf-8")

            prompt = runner._build_bug_agent_summary_prompt_for_api(
                request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 分析主题",
                request_artifact=request_artifact,
                metadata_path=metadata_path,
                followup_text="你上次为什么判断 displayChanged 是关键线索？请对比上一轮结论",
                previous_summary_path=previous_summary,
                snapshot_details={
                    "analysis_kind": "xtheme",
                    "analysis_kinds": ["xtheme"],
                    "bug_url": "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722",
                },
                snapshot_plans=[BugAnalysisPlan(kind="xtheme")],
            )

        self.assertIn("### 上一轮 Agent 总结", prompt)
        self.assertIn("displayChanged 是关键线索", prompt)

    def test_bug_direct_api_prompt_reads_previous_summary_for_explain_old_conclusion_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=True, data_dir=Path(tmp), workspace_root=Path(tmp))
            runner = BugAnalysisRunner(config)
            request_artifact = Path(tmp) / "bug_agent_followup_request.md"
            metadata_path = Path(tmp) / "bug_agent_followup_metadata.md"
            previous_summary = Path(tmp) / "bug_agent_summary.md"
            request_artifact.write_text("followup request", encoding="utf-8")
            metadata_path.write_text(
                "- 用户原始请求: `https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 分析主题`\n"
                "- 分析类型: `xtheme`\n",
                encoding="utf-8",
            )
            previous_summary.write_text("旧结论：displayChanged 是关键线索\n", encoding="utf-8")

            prompt = runner._build_bug_agent_summary_prompt_for_api(
                request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 分析主题",
                request_artifact=request_artifact,
                metadata_path=metadata_path,
                followup_text="上一轮为什么得出这个结论",
                previous_summary_path=previous_summary,
                snapshot_details={
                    "analysis_kind": "xtheme",
                    "analysis_kinds": ["xtheme"],
                    "bug_url": "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722",
                },
                snapshot_plans=[BugAnalysisPlan(kind="xtheme")],
            )

        self.assertIn("### 上一轮 Agent 总结", prompt)
        self.assertIn("displayChanged 是关键线索", prompt)

    def test_bug_direct_api_prompt_explicit_compare_request_does_not_trigger_for_generic_previous_round_question(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=True, data_dir=Path(tmp), workspace_root=Path(tmp))
            runner = BugAnalysisRunner(config)
            request_artifact = Path(tmp) / "bug_agent_followup_request.md"
            metadata_path = Path(tmp) / "bug_agent_followup_metadata.md"
            previous_summary = Path(tmp) / "bug_agent_summary.md"
            request_artifact.write_text("followup request", encoding="utf-8")
            metadata_path.write_text(
                "- 用户原始请求: `https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 分析主题`\n"
                "- 分析类型: `xtheme`\n",
                encoding="utf-8",
            )
            previous_summary.write_text("旧结论：displayChanged 是关键线索\n", encoding="utf-8")

            prompt = runner._build_bug_agent_summary_prompt_for_api(
                request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 分析主题",
                request_artifact=request_artifact,
                metadata_path=metadata_path,
                followup_text="上次为什么没生成报告",
                previous_summary_path=previous_summary,
                snapshot_details={
                    "analysis_kind": "xtheme",
                    "analysis_kinds": ["xtheme"],
                    "bug_url": "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722",
                },
                snapshot_plans=[BugAnalysisPlan(kind="xtheme")],
            )

        self.assertNotIn("### 上一轮 Agent 总结", prompt)
        self.assertNotIn("displayChanged 是关键线索", prompt)

    def test_bug_omlx_prompt_explicit_compare_request_does_not_trigger_for_generic_previous_round_question(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=True, data_dir=Path(tmp), workspace_root=Path(tmp))
            config.omlx_chat.max_prompt_chars = 9000
            runner = BugAnalysisRunner(config)
            request_artifact = Path(tmp) / "bug_agent_followup_request.md"
            metadata_path = Path(tmp) / "bug_agent_followup_metadata.md"
            previous_summary = Path(tmp) / "bug_agent_summary.md"
            request_artifact.write_text("followup request", encoding="utf-8")
            metadata_path.write_text(
                "- 用户原始请求: `https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 分析主题`\n"
                "- 分析类型: `xtheme`\n",
                encoding="utf-8",
            )
            previous_summary.write_text("旧结论：displayChanged 是关键线索\n", encoding="utf-8")

            prompt = runner._build_omlx_bug_summary_prompt(
                request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 分析主题",
                request_artifact=request_artifact,
                metadata_path=metadata_path,
                followup_text="上一轮为什么没上传文件",
                previous_summary_path=previous_summary,
                snapshot_details={
                    "analysis_kind": "xtheme",
                    "analysis_kinds": ["xtheme"],
                    "bug_url": "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722",
                },
                snapshot_plans=[BugAnalysisPlan(kind="xtheme")],
            )

        self.assertNotIn("上一轮摘要摘录", prompt)
        self.assertNotIn("displayChanged 是关键线索", prompt)

    def test_bug_omlx_prompt_reads_previous_summary_only_for_explicit_compare_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=True, data_dir=Path(tmp), workspace_root=Path(tmp))
            config.omlx_chat.max_prompt_chars = 9000
            runner = BugAnalysisRunner(config)
            request_artifact = Path(tmp) / "bug_agent_followup_request.md"
            metadata_path = Path(tmp) / "bug_agent_followup_metadata.md"
            previous_summary = Path(tmp) / "bug_agent_summary.md"
            request_artifact.write_text("followup request", encoding="utf-8")
            metadata_path.write_text(
                "- 用户原始请求: `https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 分析主题`\n"
                "- 分析类型: `xtheme`\n",
                encoding="utf-8",
            )
            previous_summary.write_text("旧结论：displayChanged 是关键线索\n", encoding="utf-8")

            prompt = runner._build_omlx_bug_summary_prompt(
                request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 分析主题",
                request_artifact=request_artifact,
                metadata_path=metadata_path,
                followup_text="对比上一轮结论，解释为什么你上次判断 displayChanged 是关键线索",
                previous_summary_path=previous_summary,
                snapshot_details={
                    "analysis_kind": "xtheme",
                    "analysis_kinds": ["xtheme"],
                    "bug_url": "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722",
                },
                snapshot_plans=[BugAnalysisPlan(kind="xtheme")],
            )

        self.assertIn("上一轮摘要摘录", prompt)
        self.assertIn("displayChanged 是关键线索", prompt)

    def test_bug_omlx_prompt_reads_previous_summary_for_explain_old_conclusion_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=True, data_dir=Path(tmp), workspace_root=Path(tmp))
            config.omlx_chat.max_prompt_chars = 9000
            runner = BugAnalysisRunner(config)
            request_artifact = Path(tmp) / "bug_agent_followup_request.md"
            metadata_path = Path(tmp) / "bug_agent_followup_metadata.md"
            previous_summary = Path(tmp) / "bug_agent_summary.md"
            request_artifact.write_text("followup request", encoding="utf-8")
            metadata_path.write_text(
                "- 用户原始请求: `https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 分析主题`\n"
                "- 分析类型: `xtheme`\n",
                encoding="utf-8",
            )
            previous_summary.write_text("旧结论：displayChanged 是关键线索\n", encoding="utf-8")

            prompt = runner._build_omlx_bug_summary_prompt(
                request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 分析主题",
                request_artifact=request_artifact,
                metadata_path=metadata_path,
                followup_text="上次为什么判断 displayChanged 是关键线索",
                previous_summary_path=previous_summary,
                snapshot_details={
                    "analysis_kind": "xtheme",
                    "analysis_kinds": ["xtheme"],
                    "bug_url": "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722",
                },
                snapshot_plans=[BugAnalysisPlan(kind="xtheme")],
            )

        self.assertIn("上一轮摘要摘录", prompt)
        self.assertIn("displayChanged 是关键线索", prompt)

    def test_bug_prompt_explicit_compare_request_does_not_trigger_for_generic_previous_round_question(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=True, data_dir=Path(tmp), workspace_root=Path(tmp))
            runner = BugAnalysisRunner(config)
            request_artifact = Path(tmp) / "bug_agent_followup_request.md"
            metadata_path = Path(tmp) / "bug_agent_followup_metadata.md"
            previous_summary = Path(tmp) / "bug_agent_summary.md"
            request_artifact.write_text("followup request", encoding="utf-8")
            metadata_path.write_text(
                "- 用户原始请求: `https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 分析主题`\n"
                "- 分析类型: `xtheme`\n",
                encoding="utf-8",
            )
            previous_summary.write_text("旧结论：displayChanged 是关键线索\n", encoding="utf-8")

            prompt = runner._build_bug_agent_summary_prompt(
                request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 分析主题",
                request_artifact=request_artifact,
                metadata_path=metadata_path,
                followup_text="上次为什么没生成报告",
                previous_summary_path=previous_summary,
                snapshot_details={
                    "analysis_kind": "xtheme",
                    "analysis_kinds": ["xtheme"],
                    "bug_url": "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722",
                },
                snapshot_plans=[BugAnalysisPlan(kind="xtheme")],
            )

        self.assertNotIn("上一轮 Agent 总结", prompt)
        self.assertNotIn("displayChanged 是关键线索", prompt)

    def test_bug_followup_metadata_reads_previous_summary_only_for_explicit_compare_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = BugAnalysisRunner(BridgeConfig(dry_run=True, data_dir=Path(tmp), workspace_root=Path(tmp)))
            previous_summary = Path(tmp) / "bug_agent_summary.md"
            previous_summary.write_text("旧结论：displayChanged 是关键线索\n", encoding="utf-8")
            generic_metadata = runner._render_bug_agent_followup_metadata(
                request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 分析主题",
                followup_text="问题时刻系统主题是什么",
                job_id="job_1",
                job_dir=Path(tmp) / "jobs" / "job_1",
                output_dir=Path(tmp) / "jobs" / "job_1" / "output",
                prepared_input=None,
                selected_input=None,
                previous_summary_path=previous_summary,
                report_files=[],
                report_url="",
                analysis_skill="xtheme-analyzer",
            )
            compare_metadata = runner._render_bug_agent_followup_metadata(
                request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 分析主题",
                followup_text="对比上一轮结论，解释为什么你上次判断 displayChanged 是关键线索",
                job_id="job_1",
                job_dir=Path(tmp) / "jobs" / "job_1",
                output_dir=Path(tmp) / "jobs" / "job_1" / "output",
                prepared_input=None,
                selected_input=None,
                previous_summary_path=previous_summary,
                report_files=[],
                report_url="",
                analysis_skill="xtheme-analyzer",
            )

        self.assertNotIn("上一轮 Agent 总结", generic_metadata)
        self.assertIn("上一轮 Agent 总结", compare_metadata)

    def test_bug_followup_metadata_reads_previous_summary_for_explain_old_conclusion_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = BugAnalysisRunner(BridgeConfig(dry_run=True, data_dir=Path(tmp), workspace_root=Path(tmp)))
            previous_summary = Path(tmp) / "bug_agent_summary.md"
            previous_summary.write_text("旧结论：displayChanged 是关键线索\n", encoding="utf-8")
            metadata = runner._render_bug_agent_followup_metadata(
                request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 分析主题",
                followup_text="上次为什么判断 displayChanged 是关键线索",
                job_id="job_1",
                job_dir=Path(tmp) / "jobs" / "job_1",
                output_dir=Path(tmp) / "jobs" / "job_1" / "output",
                prepared_input=None,
                selected_input=None,
                previous_summary_path=previous_summary,
                report_files=[],
                report_url="",
                analysis_skill="xtheme-analyzer",
            )

        self.assertIn("上一轮 Agent 总结", metadata)

    def test_bug_followup_metadata_explicit_compare_request_does_not_trigger_for_generic_previous_round_question(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = BugAnalysisRunner(BridgeConfig(dry_run=True, data_dir=Path(tmp), workspace_root=Path(tmp)))
            previous_summary = Path(tmp) / "bug_agent_summary.md"
            previous_summary.write_text("旧结论：displayChanged 是关键线索\n", encoding="utf-8")
            metadata = runner._render_bug_agent_followup_metadata(
                request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 分析主题",
                followup_text="上一轮为什么没上传文件",
                job_id="job_1",
                job_dir=Path(tmp) / "jobs" / "job_1",
                output_dir=Path(tmp) / "jobs" / "job_1" / "output",
                prepared_input=None,
                selected_input=None,
                previous_summary_path=previous_summary,
                report_files=[],
                report_url="",
                analysis_skill="xtheme-analyzer",
            )

        self.assertNotIn("上一轮 Agent 总结", metadata)

    def test_bug_reanalysis_metadata_reads_previous_summary_only_for_explicit_compare_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = BugAnalysisRunner(BridgeConfig(dry_run=True, data_dir=Path(tmp), workspace_root=Path(tmp)))
            previous_summary = Path(tmp) / "bug_agent_summary.md"
            previous_summary.write_text("旧结论：displayChanged 是关键线索\n", encoding="utf-8")
            generic_metadata = runner._render_bug_reanalysis_metadata(
                request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 分析主题",
                followup_text="修正问题时间为23:12，重新分析",
                job_id="job_1",
                target_time="2026-05-11 23:12",
                prepared_input=None,
                selected_input=None,
                plans=[BugAnalysisPlan(kind="startup")],
                rerun_kinds=["startup"],
                reused_kinds=[],
                html_paths=[],
                report_jsons={},
                combined_artifacts=None,
                previous_summary_path=previous_summary,
                classification_skill="startup-analyzer",
            )
            compare_metadata = runner._render_bug_reanalysis_metadata(
                request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 分析主题",
                followup_text="对比上一轮结论，解释为什么你上次判断 displayChanged 是关键线索",
                job_id="job_1",
                target_time="2026-05-11 23:12",
                prepared_input=None,
                selected_input=None,
                plans=[BugAnalysisPlan(kind="startup")],
                rerun_kinds=["startup"],
                reused_kinds=[],
                html_paths=[],
                report_jsons={},
                combined_artifacts=None,
                previous_summary_path=previous_summary,
                classification_skill="startup-analyzer",
            )

        self.assertNotIn("上一轮 Agent 总结", generic_metadata)
        self.assertIn("上一轮 Agent 总结", compare_metadata)

    def test_bug_reanalysis_metadata_reads_previous_summary_for_explain_old_conclusion_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = BugAnalysisRunner(BridgeConfig(dry_run=True, data_dir=Path(tmp), workspace_root=Path(tmp)))
            previous_summary = Path(tmp) / "bug_agent_summary.md"
            previous_summary.write_text("旧结论：displayChanged 是关键线索\n", encoding="utf-8")
            metadata = runner._render_bug_reanalysis_metadata(
                request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 分析主题",
                followup_text="上一轮为什么得出这个结论",
                job_id="job_1",
                target_time="2026-05-11 23:12",
                prepared_input=None,
                selected_input=None,
                plans=[BugAnalysisPlan(kind="startup")],
                rerun_kinds=["startup"],
                reused_kinds=[],
                html_paths=[],
                report_jsons={},
                combined_artifacts=None,
                previous_summary_path=previous_summary,
                classification_skill="startup-analyzer",
            )

        self.assertIn("上一轮 Agent 总结", metadata)

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
        self.assertIn(str(metadata_path), prompt)
        self.assertIn("元数据索引文件", prompt)
        self.assertNotIn("故障时间: 2026-05-11 23:10", prompt)
        self.assertNotIn("主会话 PID: 2577", prompt)

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
        self.assertIn("不要在每条结论或证据前重复写相同的诉求", prompt)
        self.assertNotIn("rawTmcData", prompt)

    def test_bug_agent_summary_command_for_claude_enables_repo_read_tools_and_lists_files(self):
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
        self.assertTrue(any(str(request_artifact) in part for part in invocation["command"]))
        self.assertTrue(any(str(metadata_path) in part for part in invocation["command"]))
        self.assertFalse(any("request-body" in part for part in invocation["command"]))
        self.assertFalse(any("metadata-body" in part for part in invocation["command"]))
        self.assertTrue(any(str(config.workspace_root.resolve()) == part for part in invocation["command"]))

    def test_custom_skill_agent_command_for_claude_redirects_stdout_and_allows_edit_in_analysis_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            skill_dir = root / ".ai" / "skills" / "source-analysis-skill"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: Source Analysis Skill\ndescription: 基于 skill 的源码分析。\n---\n\n# Source Analysis\n",
                encoding="utf-8",
            )
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp) / "data", workspace_root=root)
            config.bug_analysis.provider = "claude"
            config.bug_analysis.command = "claude"
            config.guideengine_repo = root
            runner = BugAnalysisRunner(config)
            analysis_path = Path(tmp) / "out" / "source_stage_analysis.md"
            invocation = runner._build_custom_skill_agent_command(
                analysis_kind="source_stage",
                skill_name="source-analysis-skill",
                request_text="基于源码分析",
                prompt_text="基于源码分析",
                title="源码问题",
                description="",
                fault_time="2026-05-22 19:46",
                selected_input=Path(tmp) / "logs",
                prepared_input=Path(tmp) / "logs",
                source_evidence_path=None,
                analysis_markdown_path=analysis_path,
                provider_override="claude",
            )

        self.assertEqual(invocation["output_mode"], "stdout_json")
        self.assertEqual(Path(invocation["cwd"]), analysis_path.parent)
        self.assertIn("--disable-slash-commands", invocation["command"])
        self.assertIn("--tools", invocation["command"])
        self.assertIn("--permission-mode", invocation["command"])
        self.assertIn("bypassPermissions", invocation["command"])
        self.assertIn("--allowedTools", invocation["command"])
        self.assertIn("Read,Grep,Glob,LS,Bash", invocation["command"])
        self.assertIn(str(analysis_path.parent), invocation["command"])
        self.assertIn(str(root.resolve()), invocation["command"])

    def test_custom_skill_agent_command_for_claude_adds_debug_file_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            skill_dir = root / ".ai" / "skills" / "source-analysis-skill"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: Source Analysis Skill\ndescription: 基于 skill 的源码分析。\n---\n\n# Source Analysis\n",
                encoding="utf-8",
            )
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp) / "data", workspace_root=root)
            config.bug_analysis.provider = "claude"
            config.bug_analysis.command = "claude"
            config.bug_analysis.file_agent_debug_logs = True
            config.guideengine_repo = root
            runner = BugAnalysisRunner(config)
            analysis_path = Path(tmp) / "out" / "source_stage_analysis.md"
            debug_path = Path(tmp) / "out" / "source_stage.debug.log"
            invocation = runner._build_custom_skill_agent_command(
                analysis_kind="source_stage",
                skill_name="source-analysis-skill",
                request_text="基于源码分析",
                prompt_text="基于源码分析",
                title="源码问题",
                description="",
                fault_time="2026-05-22 19:46",
                selected_input=Path(tmp) / "logs",
                prepared_input=Path(tmp) / "logs",
                source_evidence_path=None,
                analysis_markdown_path=analysis_path,
                provider_override="claude",
                debug_log_path=debug_path,
            )

        self.assertIn("--debug-file", invocation["command"])
        self.assertIn(str(debug_path), invocation["command"])

    def test_bug_agent_summary_does_not_fall_back_to_other_provider(self):
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
                mock.patch("subprocess.run", side_effect=OSError("codex missing")),
            ):
                result = runner._run_bug_agent_summary(
                    request_text="分析启动卡顿",
                    request_artifact=request_artifact,
                    metadata_path=metadata_path,
                    output_path=output_path,
                    progress_callback=None,
                    timeout=30,
                )

        self.assertEqual(result["message"], "")
        self.assertEqual(result["provider"], "codex")
        self.assertEqual(result["error"], "codex missing")

    def test_bug_agent_summary_provider_override_uses_matching_default_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=True, data_dir=Path(tmp), workspace_root=Path(tmp))
            config.bug_analysis.provider = "codex"
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
                provider_override="codex",
            )

        self.assertEqual(invocation["provider"], "codex")
        self.assertEqual(invocation["command"][0], "codex")

    def test_explicit_bug_agent_summary_provider_does_not_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp), workspace_root=Path(tmp))
            config.bug_analysis.provider = "claude"
            config.bug_analysis.command = "claude"
            runner = BugAnalysisRunner(config)
            output_path = Path(tmp) / "bug_agent_summary.md"
            request_artifact = Path(tmp) / "request.md"
            metadata_path = Path(tmp) / "metadata.md"
            request_artifact.write_text("request", encoding="utf-8")
            metadata_path.write_text("metadata", encoding="utf-8")

            with mock.patch("subprocess.run", side_effect=OSError("codex missing")):
                result = runner._run_bug_agent_summary(
                    request_text="分析启动卡顿",
                    request_artifact=request_artifact,
                    metadata_path=metadata_path,
                    output_path=output_path,
                    progress_callback=None,
                    timeout=30,
                    provider_override="codex",
                )

        self.assertEqual(result["message"], "")
        self.assertEqual(result["provider"], "codex")
        self.assertEqual(result["error"], "codex missing")

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

    def test_codex_stream_preview_formats_jsonl_events(self):
        runner = BugAnalysisRunner(BridgeConfig(dry_run=True))

        self.assertEqual(
            runner._codex_stream_preview(json.dumps({"type": "thread.started", "thread_id": "sess_1"})),
            "Codex 会话已创建 sess_1",
        )
        self.assertEqual(
            runner._codex_stream_preview(json.dumps({"type": "turn.started"})),
            "Codex 已开始深度分析",
        )
        self.assertEqual(
            runner._codex_stream_preview(
                json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 3}})
            ),
            "Codex 深度分析完成，token≈13",
        )
        self.assertEqual(
            runner._codex_stream_preview(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "结论 A\n证据 B"}})),
            "agent_message: 结论 A 证据 B",
        )
        self.assertEqual(
            runner._codex_stream_preview(
                json.dumps(
                    {
                        "type": "item.started",
                        "item": {
                            "type": "command_execution",
                            "command": '/bin/zsh -lc "nl -ba /repo/log.txt | sed -n \'1,20p\'"',
                        },
                    }
                )
            ),
            "工具调用：执行命令 nl -ba /repo/log.txt | sed -n '1,20p'",
        )
        self.assertEqual(
            runner._codex_stream_preview(
                json.dumps({"type": "item.completed", "item": {"type": "command_execution"}})
            ),
            "",
        )
        self.assertEqual(
            runner._codex_stream_preview(
                json.dumps({"type": "item.completed", "item": {"type": "error", "message": "tool failed"}})
            ),
            "Agent 错误：tool failed",
        )
        self.assertEqual(
            runner._codex_stream_preview(json.dumps({"type": "item.completed", "item": {"type": "error"}})),
            "",
        )

    def test_streaming_agent_summary_does_not_timeout_while_tool_calls_continue(self):
        runner = BugAnalysisRunner(BridgeConfig(dry_run=False))
        events = []
        script = (
            "import json, time\n"
            "for i in range(8):\n"
            "    print(json.dumps({'type':'item.started','item':{'type':'command_execution','command':f'echo {i}'}}), flush=True)\n"
            "    time.sleep(0.25)\n"
            "print('done', flush=True)\n"
        )

        completed = runner._run_bug_agent_summary_streaming_process(
            command=[sys.executable, "-c", script],
            provider="codex",
            progress_callback=events.append,
            timeout=1,
        )

        self.assertEqual(completed.returncode, 0)
        self.assertIn("done", completed.stdout)
        self.assertTrue(any("工具调用：执行命令 echo" in item["details"]["stream_preview"] for item in events))

    def test_streaming_agent_summary_times_out_when_agent_is_idle(self):
        runner = BugAnalysisRunner(BridgeConfig(dry_run=False))

        with self.assertRaises(subprocess.TimeoutExpired):
            runner._run_bug_agent_summary_streaming_process(
                command=[sys.executable, "-c", "import time; time.sleep(2)"],
                provider="codex",
                progress_callback=lambda _event: None,
                timeout=1,
            )

    def test_streaming_agent_summary_times_out_on_partial_line_idle(self):
        runner = BugAnalysisRunner(BridgeConfig(dry_run=False))
        script = (
            "import sys, time\n"
            "sys.stdout.write('{\"type\":\"turn.started\"')\n"
            "sys.stdout.flush()\n"
            "time.sleep(2)\n"
            "sys.stdout.write('}\\n')\n"
            "sys.stdout.flush()\n"
        )

        with self.assertRaises(subprocess.TimeoutExpired):
            runner._run_bug_agent_summary_streaming_process(
                command=[sys.executable, "-c", script],
                provider="codex",
                progress_callback=lambda _event: None,
                timeout=1,
            )

    def test_emit_agent_summary_stream_progress_records_preview(self):
        runner = BugAnalysisRunner(BridgeConfig(dry_run=True))
        events = []

        runner._emit_agent_summary_stream_progress(
            events.append,
            provider="codex",
            line=json.dumps({"type": "turn.started"}),
            elapsed_seconds=1.26,
        )

        self.assertEqual(events[0]["stage"], "bug_agent_summary_stream")
        self.assertEqual(events[0]["message"], "深度分析输出更新：Codex 已开始深度分析")
        self.assertEqual(events[0]["details"]["provider"], "codex")
        self.assertEqual(events[0]["details"]["stream_preview"], "Codex 已开始深度分析")
        self.assertEqual(events[0]["details"]["elapsed_seconds"], 1.3)

    def test_bug_analysis_metadata_appends_agent_runtime_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp), workspace_root=Path(tmp))
            runner = BugAnalysisRunner(config)
            log_root = Path(tmp) / "logs"
            log_root.mkdir(parents=True, exist_ok=True)
            self._write_matching_log(log_root, "2026-05-16 10:01:00")
            self._write_matching_log(log_root, "2026-05-16 10:01:00")
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
                        "description": "问题时间: 2026-05-16 10:01\n信号异常",
                    }
                if command[:3] == ["meegle", "workitem", "get"]:
                    return {"data": {}}
                raise AssertionError(f"unexpected command: {command}")

            def fake_custom_skill_agent_analysis(**kwargs):
                kwargs["html_path"].write_text("<html>source</html>", encoding="utf-8")
                kwargs["json_path"].write_text("{}", encoding="utf-8")
                return {
                    "ok": True,
                    "message": "source ok",
                    "command": ["claude"],
                    "stdout": "source ok",
                    "stderr": "",
                    "analysis_markdown_path": kwargs["analysis_dir"] / "source_stage_analysis.md",
                }

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
                mock.patch.object(runner, "_run_custom_skill_agent_analysis", side_effect=fake_custom_skill_agent_analysis),
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
                        prompt="分析信号链路 SIGNAL_VCU_ELECTRICIT_PERCENT",
                        raw_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6986570719 分析信号链路 SIGNAL_VCU_ELECTRICIT_PERCENT",
                        triggered=True,
                    )
                )
                metadata_text = (
                    Path(result.details["agent_request_file"]).with_name("bug_metadata.md").read_text(encoding="utf-8")
                )

        self.assertTrue(result.success)
        self.assertIn("Agent 类型: `codex`", metadata_text)
        self.assertIn("Agent 模型: `gpt-5.4`", metadata_text)
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
            self._write_matching_log(log_root, "2026-05-16 10:01:00")

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
                        "description": "问题时间: 2026-05-16 10:01\nVCU_ELECTRICIT_PERCENT 相关",
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
                followup_text="xtheme 主题变化为什么没跟上",
                previous_context=previous_context,
                previous_session=previous_session,
            )

        self.assertIsNotNone(selection)
        assert selection is not None
        self.assertTrue(selection.should_reanalyze)
        self.assertEqual(selection.skill_name, "xtheme-analyzer")
        self.assertEqual(selection.plans[0].kind, "xtheme")
        self.assertEqual(selection.provider, "claude")

    def test_bug_followup_retry_without_explicit_route_keeps_previous_kind(self):
        runner = BugAnalysisRunner(BridgeConfig(dry_run=False))
        previous_session = {
            "details": {
                "analysis_kinds": ["startup"],
                "prepared_log_input": "",
                "selected_log_input": "",
                "user_request_text": "调查3D生命周期",
            }
        }
        previous_context = type(
            "Context",
            (),
            {
                "request_text": "调查3D生命周期",
                "summary_text": "上一轮分析结论",
                "report_excerpt": "上一轮报告摘录",
            },
        )()

        selection = runner.decide_bug_followup(
            followup_text="重新分析一遍",
            previous_context=previous_context,
            previous_session=previous_session,
        )

        self.assertIsNotNone(selection)
        assert selection is not None
        self.assertTrue(selection.should_reanalyze)
        self.assertTrue(selection.force_rerun)
        self.assertEqual(selection.plans, [])
        self.assertEqual(selection.skill_name, "")

    def test_bug_followup_signal_request_uses_signal_plan_without_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "guideengine"
            signal_proto = repo / "module_floorcenter/module_proto/src/main/proto/signal.proto"
            signal_proto.parent.mkdir(parents=True, exist_ok=True)
            signal_proto.write_text("SIGNAL_VCU_ELECTRICIT_PERCENT = 40019;\n", encoding="utf-8")
            runner = BugAnalysisRunner(BridgeConfig(dry_run=False, guideengine_repo=repo))
            previous_session = {
                "details": {
                    "analysis_kinds": ["general"],
                    "prepared_log_input": "",
                    "selected_log_input": "",
                    "user_request_text": "调查车辆状态",
                }
            }
            previous_context = type(
                "Context",
                (),
                {
                    "request_text": "调查车辆状态",
                    "summary_text": "上一轮静态结论",
                    "report_excerpt": "信号定义未展开",
                },
            )()

            selection = runner.decide_bug_followup(
                followup_text="基于源码重新看 VCU_ELECTRICIT_PERCENT 信号链路",
                previous_context=previous_context,
                previous_session=previous_session,
            )

        self.assertIsNotNone(selection)
        assert selection is not None
        self.assertTrue(selection.should_reanalyze)
        self.assertEqual(selection.plans[0].kind, "signal")
        self.assertEqual(selection.plans[0].signal_code, "SIGNAL_VCU_ELECTRICIT_PERCENT")

    def test_bug_analysis_general_request_with_logs_collects_source_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            guideengine_repo = Path(tmp) / "guideengine"
            source_file = guideengine_repo / "module_display/theme/ThemeConfig.kt"
            source_file.parent.mkdir(parents=True, exist_ok=True)
            source_file.write_text(
                "class ThemeConfig {\n"
                "    // 主题配置需要跟随用户设置刷新\n"
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
            log_root = Path(tmp) / "logs"
            self._write_matching_log(log_root, "2026-05-14 17:56:00")

            def fake_run_json_command(command, timeout):
                if "check-env" in command:
                    return {"meegle_installed": True, "auth_ok": True}
                if "resolve-url" in command:
                    return {"project_key": "xpfailuremgmt", "work_item_id": "6991604970"}
                if "fetch-data" in command:
                    return {
                        "title": "主题配置切换后页面状态没有刷新",
                        "status": "处理中",
                        "create_time": "2026-05-17 18:54",
                        "create_by": "tester",
                        "fields": {},
                        "attachments": [],
                        "description": "问题时间: 2026-05-14 17:56\n用户切换主题配置后页面状态没有刷新",
                    }
                if command[:3] == ["meegle", "workitem", "get"]:
                    return {"work_item_current_node": []}
                raise AssertionError(f"unexpected command: {command}")

            with (
                mock.patch.object(runner, "_run_json_command", side_effect=fake_run_json_command),
                mock.patch.object(runner, "_load_option_map", return_value={}),
                mock.patch.object(runner, "_select_log_input", return_value=log_root),
                mock.patch.object(runner, "_prepare_log_input", return_value=log_root),
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
                        prompt="根据 ThemeConfig 源码分析主题刷新链路",
                        raw_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6991604970 根据 ThemeConfig 源码分析主题刷新链路",
                        triggered=True,
                    )
                )

            metadata_body = (Path(result.job_dir) / "output" / "bug_metadata.md").read_text(encoding="utf-8")
            evidence_body = Path(result.details["source_evidence_file"]).read_text(encoding="utf-8")

        self.assertTrue(result.success)
        self.assertEqual(result.details["analysis_kind"], "source_stage")
        self.assertEqual(result.details["analysis_skill"], "source_analysis")
        self.assertEqual(result.details["classification_source"], "manual_fallback")
        self.assertEqual(result.details["selected_log_input"], str(log_root))
        self.assertIn("源码分析阶段", metadata_body)
        self.assertIn("命中 Skill: `source_analysis`", metadata_body)
        self.assertIn("分类来源: `manual_fallback`", metadata_body)
        self.assertIn(str(log_root), metadata_body)
        self.assertIn("源码证据", metadata_body)
        self.assertIn("主题配置", evidence_body)

    def test_bug_analysis_general_request_without_clear_direction_asks_for_direction(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(
                dry_run=False,
                data_dir=Path(tmp),
                workspace_root=Path(tmp),
                guideengine_repo=Path(tmp) / "guideengine",
            )
            runner = BugAnalysisRunner(config)

            def fake_run_json_command(command, timeout):
                if "check-env" in command:
                    return {"meegle_installed": True, "auth_ok": True}
                if "resolve-url" in command:
                    return {"project_key": "xpfailuremgmt", "work_item_id": "6991604970"}
                if "fetch-data" in command:
                    return {
                        "title": "车辆偶现异常",
                        "status": "处理中",
                        "create_time": "2026-05-17 18:54",
                        "create_by": "tester",
                        "fields": {},
                        "attachments": [],
                        "description": "用户反馈车辆状态异常，但未提供具体页面、模块、信号或时间。",
                    }
                if command[:3] == ["meegle", "workitem", "get"]:
                    return {"work_item_current_node": []}
                raise AssertionError(f"unexpected command: {command}")

            with (
                mock.patch.object(runner, "_run_json_command", side_effect=fake_run_json_command),
                mock.patch.object(runner, "_load_option_map", return_value={}),
                mock.patch.object(runner, "_classify_bug_request_with_agent", return_value=None),
                mock.patch.object(
                    runner,
                    "_download_bug_attachments",
                    side_effect=AssertionError("generic unclear bug request should not download logs"),
                ),
                mock.patch.object(
                    runner,
                    "_run_bug_agent_summary",
                    side_effect=AssertionError("generic unclear bug request should not call agent summary"),
                ),
            ):
                result = runner.run_bug_analysis(
                    BugRequest(
                        bug_url="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6991604970",
                        prompt="分析下",
                        raw_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6991604970 分析下",
                        triggered=True,
                    )
                )

        self.assertTrue(result.success)
        self.assertTrue(result.skipped)
        self.assertEqual(result.details["mode"], "bug_clarification")
        self.assertEqual(result.details["analysis_skill"], "general")
        self.assertIn("没有命中专用分析预设", result.message)
        self.assertIn("请补充", result.message)
        self.assertIn("目前能力不足", result.message)

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
                        "description": "问题时间: 2026-05-17 18:54\n启动黑屏只有 logo",
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
            self._write_matching_log(prepared_input, "2026-05-19 00:00:00")
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
                    followup_text="2026-05-19 00:00 重新分析 xtheme",
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
                    followup_text="2026-05-19 00:00 重新分析 xtheme",
                    previous_context=previous_context,
                    previous_session=previous_session,
                    plans_override=[__import__("lark_agent_bridge.agents", fromlist=["BugAnalysisPlan"]).BugAnalysisPlan(kind="xtheme")],
                    classification_skill="xtheme-analyzer",
                )

        self.assertFalse(result.success)
        self.assertIn("meegle 未登录或授权已失效", result.message)
        self.assertEqual(result.details["download_retry"]["reason"], "AUTH_REQUIRED")

    def test_bug_reanalysis_missing_problem_time_asks_before_retry_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = BugAnalysisRunner(BridgeConfig(dry_run=False, data_dir=Path(tmp), workspace_root=Path(tmp)))
            previous_context = type(
                "Context",
                (),
                {"request_text": "原始 bug 分析", "summary_text": "", "report_excerpt": "", "history": []},
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
                side_effect=AssertionError("missing time should not retry log download"),
            ):
                result = runner.run_bug_reanalysis(
                    followup_text="重新分析 xtheme",
                    previous_context=previous_context,
                    previous_session=previous_session,
                    plans_override=[BugAnalysisPlan(kind="xtheme")],
                    classification_skill="xtheme-analyzer",
                )

        self.assertTrue(result.success)
        self.assertTrue(result.skipped)
        self.assertEqual(result.details["mode"], "bug_time_clarification")
        self.assertEqual(result.details["time_gate_status"], "missing_fault_time")

    def test_bug_reanalysis_uses_local_log_resource_when_previous_logs_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = BugAnalysisRunner(BridgeConfig(dry_run=False, data_dir=Path(tmp), workspace_root=Path(tmp)))
            local_log = Path(tmp) / "downloads" / "Log.log"
            local_log.parent.mkdir()
            local_log.write_text("05-19 00:00:00.000 I Demo: log line", encoding="utf-8")
            previous_context = type(
                "Context",
                (),
                {
                    "request_text": "原始 bug 分析",
                    "summary_text": "上一轮没有日志",
                    "report_excerpt": "缺少日志",
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

            with (
                mock.patch.object(runner, "_retry_bug_log_download") as retry_mock,
                mock.patch.object(runner, "_write_reanalysis_source_evidence", return_value=None),
                mock.patch.object(runner, "_build_combined_report_artifacts", return_value=None),
                mock.patch.object(
                    runner,
                    "_run_bug_agent_summary",
                    return_value={
                        "message": "agent summary",
                        "command": None,
                        "error": "",
                        "provider": "codex",
                        "session_id": "",
                        "resumed": False,
                        "duration_seconds": 1.0,
                        "usage": {},
                    },
                ),
            ):
                result = runner.run_bug_reanalysis(
                    followup_text="问题时间 2026-05-19 00:00，日志我下载到服务器的下载目录了 Log.log 基于这个日志分析",
                    previous_context=previous_context,
                    previous_session=previous_session,
                    plans_override=[BugAnalysisPlan(kind="general")],
                    local_log_resources=[DownloadResource(kind="local", value=str(local_log))],
                )

        self.assertTrue(result.success)
        retry_mock.assert_not_called()
        self.assertEqual(Path(result.details["selected_log_input"]), local_log.resolve())
        self.assertEqual(Path(result.details["prepared_log_input"]), local_log.resolve())
        self.assertEqual(result.details["local_log_resources"], [str(local_log)])

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

    def test_bug_outputs_marks_cached_attachments_and_report_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = BugAnalysisRunner(BridgeConfig(dry_run=True))
            html_path = Path(tmp) / "bug_xtheme_analysis_report.html"
            json_path = Path(tmp) / "bug_xtheme_analysis_report.json"
            html_path.write_text("<html>xtheme</html>", encoding="utf-8")
            json_path.write_text(
                json.dumps(
                    {
                        "verdict": {"msg": "uiMode/ThemeMode 不一致且未修正 @ 05-18 14:02:53.330"},
                        "counts": {"xtheme_msg": 8, "timer_checks": 5},
                        "issues": [
                            {
                                "title": "uiMode/ThemeMode 不一致",
                                "detail": "uiMode Night, themeMode Day [main_2026-05-18_14-00.alog.log:39722]",
                            }
                        ],
                        "focus_snapshot": [
                            {
                                "kind": "UI Mode observer",
                                "value": "mCurrentUiMode=1 themeMode=0",
                                "ts": "05-18 14:03:33.485",
                                "source": "main_2026-05-18_14-00.alog.log:47955",
                            }
                        ],
                        "target_time": "2026-05-18 14:03",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            metadata, summary = runner._build_bug_outputs(
                plans=[BugAnalysisPlan(kind="xtheme")],
                work_item_id="6994226322",
                fetched={
                    "title": "2026-05-18 14:03:01:457 黑夜模式 SR界面不显示黑夜模式",
                    "status": "处理中",
                    "create_time": "2026-05-18 17:02",
                    "create_by": "tester",
                    "fields": {},
                    "attachments": [{"name": "data_Log.zip", "size": "74MB"}],
                },
                full_item={"work_item_current_node": []},
                option_map={},
                request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6994226322 分析主题变化",
                prompt_text="分析主题变化",
                selected_input=Path(tmp) / "logs",
                report_jsons={"xtheme": json_path},
                download={"downloaded": [], "skipped": [], "errors": [], "reused": True},
                html_paths=[html_path],
            )

        self.assertIn("已复用缓存日志", metadata)
        self.assertIn(f"HTML `XTheme时光主题分析`: `{html_path}`", metadata)
        self.assertIn(f"JSON `XTheme时光主题分析`: `{json_path}`", metadata)
        self.assertIn("uiMode/ThemeMode 不一致且未修正", summary)
        self.assertIn("问题时间证据", summary)
        self.assertIn("UI Mode observer", summary)

    def test_bug_agent_summary_context_embeds_unquoted_report_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = BugAnalysisRunner(BridgeConfig(dry_run=True))
            metadata_path = Path(tmp) / "bug_metadata.md"
            request_path = Path(tmp) / "bug_agent_request.md"
            request_path.write_text("request", encoding="utf-8")
            html_path = Path(tmp) / "bug_xtheme_analysis_report.html"
            json_path = Path(tmp) / "bug_xtheme_analysis_report.json"
            html_path.write_text("<html>xtheme</html>", encoding="utf-8")
            json_path.write_text('{"verdict":{"msg":"ok"}}', encoding="utf-8")
            metadata_path.write_text(
                f"HTML: {html_path}\nJSON: {json_path}\n",
                encoding="utf-8",
            )

            files = runner._bug_agent_summary_context_files(
                request_artifact=request_path,
                metadata_path=metadata_path,
            )

        paths = {item["path"] for item in files}
        self.assertIn(str(html_path), paths)
        self.assertIn(str(json_path), paths)

    def test_bug_agent_summary_prompt_lists_report_paths_without_html_css_noise(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = BugAnalysisRunner(BridgeConfig(dry_run=True))
            metadata_path = Path(tmp) / "bug_metadata.md"
            request_path = Path(tmp) / "bug_agent_request.md"
            html_path = Path(tmp) / "bug_3d_startup_report.html"
            json_path = Path(tmp) / "bug_3d_startup_report.json"
            request_path.write_text("# request\nrequest-only-body\n", encoding="utf-8")
            html_path.write_text(
                "<!doctype html><html><head><style>body{color:red}</style></head>"
                "<body><h1>真实报告正文</h1></body></html>",
                encoding="utf-8",
            )
            json_path.write_text(
                '{"verdict":{"message":"启动链路完整"},"focus_session_pid":14941}',
                encoding="utf-8",
            )
            metadata_path.write_text(
                "# Bug Metadata\n"
                f"- HTML `3D启动时序分析`: `{html_path}`\n"
                f"- JSON `3D启动时序分析`: `{json_path}`\n",
                encoding="utf-8",
            )

            prompt = runner._build_bug_agent_summary_prompt(
                request_text="分析3D生命周期",
                request_artifact=request_path,
                metadata_path=metadata_path,
            )

        self.assertIn(str(html_path), prompt)
        self.assertIn(str(json_path), prompt)
        self.assertIn(str(request_path), prompt)
        self.assertIn(str(metadata_path), prompt)
        self.assertIn("请按需读取这些本地文件", prompt)
        self.assertNotIn("<style>", prompt)
        self.assertNotIn("body{color:red}", prompt)
        self.assertNotIn("真实报告正文", prompt)
        self.assertNotIn("request-only-body", prompt)
        self.assertNotIn("Bug Metadata\n- HTML", prompt)

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
        # With fixed _startup_analysis_input, the runner selects the best
        # matching log file (by fault time) instead of passing the whole dir.
        self.assertEqual(len(analysis_inputs), 1)
        selected = analysis_inputs[0]
        self.assertIn("main_2026-05-11_23-00.alog", str(selected))
        self.assertTrue(result.details["bug_cache_reused"])
        self.assertEqual(result.details["bug_cache_dir"], str(cache_dir.resolve()))

    def test_startup_input_selects_logs_with_variable_prefix_before_fixed_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = BugAnalysisRunner(BridgeConfig(dry_run=False, data_dir=Path(tmp), workspace_root=Path(tmp)))
            log_root = Path(tmp) / "logs"
            target_dir = log_root / "Log" / "log0" / "app" / "com.xiaopeng.montecarlo"
            target_dir.mkdir(parents=True)
            stale = target_dir / "user0_main_2026-05-11_10-00.alog"
            matching = target_dir / "user0_main_2026-05-11_23-00.alog"
            stale.write_text("", encoding="utf-8")
            matching.write_text("", encoding="utf-8")

            selected = runner._startup_analysis_input(log_root, "2026-05-11 23:10")

        self.assertEqual(selected, matching)

    def test_agent_runtime_annotation_injects_section_into_html_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = BugAnalysisRunner(BridgeConfig(dry_run=True, data_dir=Path(tmp), workspace_root=Path(tmp)))
            report_html = Path(tmp) / "bug_signal_chain_report.html"
            report_html.write_text("<html><body><h1>原始报告</h1></body></html>", encoding="utf-8")

            runner._annotate_html_reports(
                [report_html],
                agent_summary_result={
                    "provider": "codex",
                    "model": "gpt-5.4",
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
        self.assertIn("Agent 模型", html)
        self.assertIn("gpt-5.4", html)
        self.assertIn("codex", html)
        self.assertIn("321 / 54 / 375", html)
        self.assertIn("45.6 秒", html)
        self.assertIn("原始报告", html)

    def test_agent_runtime_annotation_does_not_embed_raw_agent_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = BugAnalysisRunner(BridgeConfig(dry_run=True, data_dir=Path(tmp), workspace_root=Path(tmp)))
            report_html = Path(tmp) / "bug_signal_chain_report.html"
            report_html.write_text("<html><body><h1>原始报告</h1></body></html>", encoding="utf-8")

            runner._annotate_html_reports(
                [report_html],
                agent_summary_result={
                    "provider": "codex",
                    "duration_seconds": 12.5,
                    "usage": {},
                    "message": "## 结论摘要\n- 诉求：`分析3D生命周期`\n结论：重复的 agent 原文",
                },
                total_duration_seconds=45.6,
            )

            html = report_html.read_text(encoding="utf-8")

        self.assertIn("Agent 运行信息", html)
        self.assertNotIn("Agent 最终结论", html)
        self.assertNotIn("重复的 agent 原文", html)

    def test_agent_runtime_details_include_summary_backend_decision(self):
        runner = BugAnalysisRunner(BridgeConfig(dry_run=True))
        details: dict[str, object] = {}

        runner._apply_agent_runtime_details(
            details,
            {
                "message": "summary",
                "command": None,
                "error": "",
                "provider": "direct_api",
                "model": "demo",
                "session_id": "",
                "resumed": False,
                "duration_seconds": 1.2,
                "usage": {},
                "usage_scope": "direct_api",
                "execution_backend": "direct_api",
                "backend_reason": "direct_api_failed_no_fallback",
                "fallback_from": "",
            },
        )

        self.assertEqual(details["agent_summary_execution_backend"], "direct_api")
        self.assertEqual(details["agent_summary_backend_reason"], "direct_api_failed_no_fallback")
        self.assertNotIn("agent_summary_fallback_from", details)

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

    def test_bug_analysis_stuck_command_passes_target_time(self):
        runner = BugAnalysisRunner(BridgeConfig(dry_run=True))
        plan_cls = __import__("lark_agent_bridge.agents", fromlist=["BugAnalysisPlan"]).BugAnalysisPlan
        command = runner.build_command(
            plan=plan_cls(kind="stuck"),
            input_path=Path("/tmp/input"),
            html_path=Path("/tmp/out.html"),
            json_path=Path("/tmp/out.json"),
            analysis_dir=Path("/tmp/stuck"),
            target_time="2026-04-29 20:49",
        )
        self.assertIn("--target-time", command)
        self.assertIn("2026-04-29 20:49", command)

    def test_bug_analysis_crash_command_passes_target_time(self):
        runner = BugAnalysisRunner(BridgeConfig(dry_run=True))
        plan_cls = __import__("lark_agent_bridge.agents", fromlist=["BugAnalysisPlan"]).BugAnalysisPlan
        command = runner.build_command(
            plan=plan_cls(kind="crash"),
            input_path=Path("/tmp/input"),
            html_path=Path("/tmp/out.html"),
            json_path=Path("/tmp/out.json"),
            analysis_dir=Path("/tmp/crash"),
            target_time="2026-05-28 11:18",
        )
        self.assertIn("--target-time", command)
        self.assertIn("2026-05-28 11:18", command)

    def test_bug_analysis_signal_command_does_not_accept_target_time(self):
        from lark_agent_bridge.agents.bug_runner import _kind_accepts_target_time
        self.assertTrue(_kind_accepts_target_time("startup"))
        self.assertTrue(_kind_accepts_target_time("stuck"))
        self.assertTrue(_kind_accepts_target_time("crash"))
        self.assertTrue(_kind_accepts_target_time("scene_signal"))
        self.assertTrue(_kind_accepts_target_time("perception"))
        self.assertTrue(_kind_accepts_target_time("xtheme"))
        self.assertFalse(_kind_accepts_target_time("signal"))
        self.assertFalse(_kind_accepts_target_time("general"))
        self.assertFalse(_kind_accepts_target_time("source_stage"))

    def test_bug_report_adapter_normalize_verdict_per_kind(self):
        from lark_agent_bridge.agents.bug_runner import BugReportAdapter, NormalizedVerdict

        # startup
        v = BugReportAdapter.normalize_verdict(
            kind="startup",
            payload={"verdict": {"severity": "red", "message": "boot stuck", "issues": [{"sev": "red", "title": "t", "detail": "d"}]}},
        )
        self.assertEqual(v.sev, "red")
        self.assertEqual(v.msg, "boot stuck")
        self.assertEqual(len(v.issues), 1)

        # stuck
        v = BugReportAdapter.normalize_verdict(
            kind="stuck",
            payload={"verdict": {"verdict_sev": "yellow", "verdict_msg": "jank"}},
        )
        self.assertEqual(v.sev, "yellow")
        self.assertEqual(v.msg, "jank")

        # crash (same shape as stuck)
        v = BugReportAdapter.normalize_verdict(
            kind="crash",
            payload={"verdict": {"verdict_sev": "red", "verdict_msg": "tombstone"}},
        )
        self.assertEqual(v.sev, "red")
        self.assertEqual(v.msg, "tombstone")

        # perception (nested under summary)
        v = BugReportAdapter.normalize_verdict(
            kind="perception",
            payload={"summary": {"verdict": {"sev": "green", "msg": "ok"}, "issues": []}},
        )
        self.assertEqual(v.sev, "green")
        self.assertEqual(v.msg, "ok")

        # xtheme
        v = BugReportAdapter.normalize_verdict(
            kind="xtheme",
            payload={"verdict": {"sev": "yellow", "msg": "theme drift"}, "issues": [{"sev": "yellow", "title": "t", "detail": "d"}]},
        )
        self.assertEqual(v.sev, "yellow")
        self.assertEqual(len(v.issues), 1)

        # scene_signal (flat severity + verdict string)
        v = BugReportAdapter.normalize_verdict(
            kind="scene_signal",
            payload={"severity": "warning", "verdict": "pk-incomplete"},
        )
        self.assertEqual(v.sev, "yellow")
        self.assertEqual(v.msg, "pk-incomplete")
        v = BugReportAdapter.normalize_verdict(
            kind="scene_signal",
            payload={"severity": "success", "verdict": "ok"},
        )
        self.assertEqual(v.sev, "green")

        # signal (summary + warnings list)
        v = BugReportAdapter.normalize_verdict(
            kind="signal",
            payload={"summary": "132002 dispatched", "warnings": ["no source", "no log"]},
        )
        self.assertEqual(v.sev, "info")
        self.assertEqual(v.msg, "132002 dispatched")
        self.assertEqual(len(v.issues), 2)

        # general (bug_runner-constructed verdict.text)
        v = BugReportAdapter.normalize_verdict(
            kind="general",
            payload={"verdict": {"sev": "green", "text": "通用分析"}},
        )
        self.assertEqual(v.sev, "green")
        self.assertEqual(v.msg, "通用分析")

        # malformed payload — defaults
        v = BugReportAdapter.normalize_verdict(kind="startup", payload="not a dict")
        self.assertEqual(v.sev, "unknown")
        self.assertEqual(v.msg, "")
        self.assertEqual(v.issues, [])

    def test_bug_report_adapter_locate_report_per_kind(self):
        from lark_agent_bridge.agents.bug_runner import BugReportAdapter

        analysis_dir = Path("/tmp/analysis_dir")
        html = Path("/tmp/out.html")
        jsn = Path("/tmp/out.json")

        # startup — fixed paths in analysis_dir
        h, j = BugReportAdapter.locate_report(
            kind="startup", completed_stdout="", analysis_dir=analysis_dir,
            html_path=html, json_path=jsn,
        )
        self.assertEqual(h, analysis_dir / "unity_startup_lifecycle_report.html")
        self.assertEqual(j, analysis_dir / "unity_startup_lifecycle_report.json")

        # scene_signal — fixed paths
        h, j = BugReportAdapter.locate_report(
            kind="scene_signal", completed_stdout="", analysis_dir=analysis_dir,
            html_path=html, json_path=jsn,
        )
        self.assertEqual(h, analysis_dir / "scene_signal_events.html")
        self.assertEqual(j, analysis_dir / "scene_signal_events.json")

        # stuck — stdout `[OK] 报告:` / `[OK] JSON:`
        stdout = "noise\n[OK] 报告: /a/stuck.html\n[OK] JSON: /a/stuck.json\n"
        h, j = BugReportAdapter.locate_report(
            kind="stuck", completed_stdout=stdout, analysis_dir=analysis_dir,
            html_path=html, json_path=jsn,
        )
        self.assertEqual(h, Path("/a/stuck.html"))
        self.assertEqual(j, Path("/a/stuck.json"))

        # perception — stdout `[OK] HTML:` / `[OK] JSON:`
        stdout = "[OK] HTML: /a/perc.html\n[OK] JSON: /a/perc.json\n"
        h, j = BugReportAdapter.locate_report(
            kind="perception", completed_stdout=stdout, analysis_dir=analysis_dir,
            html_path=html, json_path=jsn,
        )
        self.assertEqual(h, Path("/a/perc.html"))
        self.assertEqual(j, Path("/a/perc.json"))

        # xtheme — stdout `[OK] HTML:` / `[OK] JSON:`
        stdout = "[OK] HTML: /a/x.html\n[OK] JSON: /a/x.json\n"
        h, j = BugReportAdapter.locate_report(
            kind="xtheme", completed_stdout=stdout, analysis_dir=analysis_dir,
            html_path=html, json_path=jsn,
        )
        self.assertEqual(h, Path("/a/x.html"))
        self.assertEqual(j, Path("/a/x.json"))

        # signal — script writes directly to html/json paths; missing files yield None
        h, j = BugReportAdapter.locate_report(
            kind="signal", completed_stdout="", analysis_dir=analysis_dir,
            html_path=Path("/non/existent.html"), json_path=Path("/non/existent.json"),
        )
        self.assertIsNone(h)
        self.assertIsNone(j)

    def test_plan_kind_spec_slim_4_fields_with_derived(self):
        from lark_agent_bridge.agents.bug_runner import PlanKindSpec, _kind_spec

        # Declared fields are exactly 4 (no needs_decode/needs_target_time/stdout_report/has_verdict)
        from dataclasses import fields
        declared = {f.name for f in fields(PlanKindSpec)}
        self.assertEqual(
            declared,
            {"is_agent_handled", "is_custom_agent", "is_source_stage", "needs_source_evidence"},
        )

        # Derived properties hold expected values
        startup = _kind_spec("startup")
        self.assertFalse(startup.is_agent_handled)
        self.assertTrue(startup.log_dependent)  # script-driven => log dependent
        self.assertFalse(startup.needs_custom_executor_check)

        ld = _kind_spec("ld_lane_level")
        self.assertTrue(ld.is_agent_handled)
        self.assertTrue(ld.is_custom_agent)
        self.assertFalse(ld.is_source_stage)
        self.assertFalse(ld.needs_source_evidence)
        self.assertFalse(ld.log_dependent)
        self.assertFalse(ld.needs_custom_executor_check)  # is_custom_agent but not source-evidence

        source = _kind_spec("source_stage")
        self.assertTrue(source.is_agent_handled)
        self.assertTrue(source.is_custom_agent)
        self.assertTrue(source.is_source_stage)
        self.assertTrue(source.needs_source_evidence)
        self.assertTrue(source.needs_custom_executor_check)

        # Unknown kind returns default empty spec
        unknown = _kind_spec("not_a_real_kind")
        self.assertFalse(unknown.is_agent_handled)
        self.assertFalse(unknown.needs_source_evidence)

    def test_retry_kinds_only_contains_startup(self):
        from lark_agent_bridge.agents.bug_runner import _RETRY_KINDS
        self.assertEqual(_RETRY_KINDS, frozenset({"startup"}))

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

    def test_bug_analysis_classifies_scene_signal_before_generic_signal(self):
        runner = BugAnalysisRunner(BridgeConfig(dry_run=True))

        plan = runner.classify_request(
            prompt_text="分析3D场景信号",
            title="",
            description="",
        )

        self.assertEqual(plan.kind, "scene_signal")
        self.assertIsNone(plan.signal_code)

    def test_bug_analysis_core_scene_signal_uses_scene_signal_skill(self):
        runner = BugAnalysisRunner(BridgeConfig(dry_run=True))

        plan = runner.classify_request(
            prompt_text="分析 SIGNAL_SR_SCENE_TYPE 场景链路",
            title="",
            description="",
        )

        self.assertEqual(plan.kind, "scene_signal")
        self.assertIsNone(plan.signal_code)

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

    def test_stage_plan_plain_scene_signal_request_stays_domain_only(self):
        runner = BugAnalysisRunner(BridgeConfig(dry_run=False))

        decision = runner._build_analysis_decision(
            request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6998811703 分析 3D场景模式",
            prompt_text="分析 3D场景模式",
            title="进入场景模式没有展示3D场景",
            description="Version: 6.2.3\nSerial: ABC\nICCID: 8986\nVIN: TESTVIN",
            plans=[BugAnalysisPlan(kind="scene_signal")],
            skill_name="scene-signal-diagnosis",
        )
        plan = runner._build_analysis_plan(decision)

        self.assertEqual(decision.domain_kind, "scene_signal")
        self.assertEqual(decision.source_mode, "off")
        self.assertEqual(decision.context_profile, "scene-signal-diagnosis")
        self.assertEqual([stage.kind for stage in plan.stages], ["domain", "summary"])

    def test_stage_plan_signal_name_without_source_request_stays_domain_only(self):
        runner = BugAnalysisRunner(BridgeConfig(dry_run=False))

        decision = runner._build_analysis_decision(
            request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6998811703 分析 SIGNAL_SR_SCENE_TYPE",
            prompt_text="分析 SIGNAL_SR_SCENE_TYPE",
            title="进入场景模式没有展示3D场景",
            description="问题时间: 2026-05-25 16:50:41",
            plans=[BugAnalysisPlan(kind="scene_signal")],
            skill_name="scene-signal-diagnosis",
        )
        plan = runner._build_analysis_plan(decision)

        self.assertEqual(decision.source_mode, "off")
        self.assertIn("SIGNAL_SR_SCENE_TYPE", decision.source_targets)
        self.assertEqual([stage.kind for stage in plan.stages], ["domain", "summary"])

    def test_stage_plan_explicit_source_request_appends_source_stage_with_domain_context(self):
        runner = BugAnalysisRunner(BridgeConfig(dry_run=False))

        decision = runner._build_analysis_decision(
            request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6998811703 基于源码分析 3D场景模式",
            prompt_text="基于源码分析 3D场景模式",
            title="进入场景模式没有展示3D场景",
            description="问题时间: 2026-05-25 16:50:41",
            plans=[BugAnalysisPlan(kind="scene_signal")],
            skill_name="scene-signal-diagnosis",
        )
        plan = runner._build_analysis_plan(decision)
        legacy_plans = runner._augment_plans_for_source_analysis(
            [BugAnalysisPlan(kind="scene_signal")],
            source_decision=runner._decide_source_analysis_request(
                request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6998811703 基于源码分析 3D场景模式",
                prompt_text="基于源码分析 3D场景模式",
                title="进入场景模式没有展示3D场景",
                description="问题时间: 2026-05-25 16:50:41",
                plans=[BugAnalysisPlan(kind="scene_signal")],
                skill_name="scene-signal-diagnosis",
            ),
        )

        self.assertEqual(decision.domain_kind, "scene_signal")
        self.assertEqual(decision.source_mode, "append")
        self.assertEqual(decision.context_profile, "scene-signal-diagnosis")
        self.assertEqual([stage.kind for stage in plan.stages], ["domain", "source", "summary"])
        self.assertEqual([item.kind for item in legacy_plans], ["scene_signal", "source_stage"])

    def test_stage_plan_explicit_source_without_domain_runs_standalone_source_stage(self):
        runner = BugAnalysisRunner(BridgeConfig(dry_run=False))

        decision = runner._build_analysis_decision(
            request_text="基于源码排查 SRViolationHandler.kt",
            prompt_text="基于源码排查 SRViolationHandler.kt",
            title="",
            description="",
            plans=[BugAnalysisPlan(kind="general")],
            skill_name="general",
        )
        plan = runner._build_analysis_plan(decision)

        self.assertEqual(decision.source_mode, "standalone")
        self.assertEqual([stage.kind for stage in plan.stages], ["source", "summary"])
        self.assertIn("SRViolationHandler.kt", decision.source_targets)

    def test_source_evidence_terms_ignore_environment_labels(self):
        runner = BugAnalysisRunner(BridgeConfig(dry_run=False))

        terms = runner._source_evidence_terms(
            plans=[BugAnalysisPlan(kind="scene_signal")],
            request_text="分析 3D场景模式",
            followup_text="",
            extra_texts=(
                "Version: 6.2.3\nBuild: 12345\nSerial: ABC123\nICCID: 89860000\nVIN: LTESTVIN",
            ),
        )

        self.assertNotIn("Version", terms)
        self.assertNotIn("Build", terms)
        self.assertNotIn("Serial", terms)
        self.assertNotIn("ICCID", terms)
        self.assertNotIn("VIN", terms)

    def test_source_stage_context_uses_domain_context_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            skill_dir = root / ".ai" / "skills" / "scene-signal-diagnosis"
            references_dir = skill_dir / "references"
            references_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("# Scene Signal\n", encoding="utf-8")
            (references_dir / "workflow.md").write_text("# Workflow\n", encoding="utf-8")
            analysis_dir = Path(tmp) / "analysis"
            runner = BugAnalysisRunner(BridgeConfig(dry_run=False, workspace_root=root, data_dir=Path(tmp) / "data"))

            context_path = runner._write_file_agent_context(
                analysis_kind="source_stage",
                skill_name="scene-signal-diagnosis",
                request_text="基于源码分析 3D场景模式",
                prompt_text="基于源码分析 3D场景模式",
                title="进入场景模式没有展示3D场景",
                description="问题时间: 2026-05-25 16:50:41",
                fault_time="2026-05-25 16:50:41",
                original_selected_input=Path(tmp) / "logs",
                focused_log_input=Path(tmp) / "focus",
                log_focus_manifest=Path(tmp) / "focus.md",
                source_evidence_path=None,
                analysis_dir=analysis_dir,
                analysis_markdown_path=analysis_dir / "source_stage_analysis.md",
                prior_findings=[("3D场景信号分析", "scene_signal", "[green] 已命中场景模式切换异常")],
            )
            context_text = context_path.read_text(encoding="utf-8")

            self.assertIn("scene-signal-diagnosis/SKILL.md", context_text)
            self.assertIn("scene-signal-diagnosis/references/workflow.md", context_text)
            self.assertIn("前序分析结论", context_text)

    def test_file_agent_context_includes_search_output_budget_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            analysis_dir = Path(tmp) / "analysis"
            runner = BugAnalysisRunner(BridgeConfig(dry_run=False, workspace_root=root, data_dir=Path(tmp) / "data"))

            context_path = runner._write_file_agent_context(
                analysis_kind="source_stage",
                skill_name="source_analysis",
                request_text="基于源码重新分析",
                prompt_text="基于源码重新分析",
                title="进入场景模式没有展示3D场景",
                description="",
                fault_time="2026-05-25 16:50:41",
                original_selected_input=Path(tmp) / "logs",
                focused_log_input=Path(tmp) / "focus",
                log_focus_manifest=Path(tmp) / "focus.md",
                source_evidence_path=Path(tmp) / "bug_source_evidence.md",
                analysis_dir=analysis_dir,
                analysis_markdown_path=analysis_dir / "source_stage_analysis.md",
            )

            context_text = context_path.read_text(encoding="utf-8")

        self.assertIn("## 检索预算", context_text)
        self.assertIn("不要在整个源码根目录直接执行无边界 `rg`", context_text)
        self.assertIn("--max-count", context_text)
        self.assertIn("head -80", context_text)

    def test_file_agent_prompt_includes_search_output_budget_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            config = BridgeConfig(dry_run=False, workspace_root=root, data_dir=Path(tmp) / "data")
            config.bug_analysis.provider = "codex"
            config.bug_analysis.command = "codex"
            runner = BugAnalysisRunner(config)
            analysis_path = Path(tmp) / "analysis" / "source_stage_analysis.md"

            invocation = runner._build_custom_skill_agent_command(
                analysis_kind="source_stage",
                skill_name="source_analysis",
                request_text="基于源码重新分析",
                prompt_text="基于源码重新分析",
                title="进入场景模式没有展示3D场景",
                description="",
                fault_time="2026-05-25 16:50:41",
                selected_input=Path(tmp) / "focus",
                prepared_input=Path(tmp) / "focus",
                source_evidence_path=Path(tmp) / "bug_source_evidence.md",
                analysis_markdown_path=analysis_path,
                context_path=Path(tmp) / "analysis" / "source_stage.context.md",
            )
            prompt = str(invocation["prompt"])

        self.assertIn("检索预算", prompt)
        self.assertIn("不要在整个源码根目录直接执行无边界 `rg`", prompt)
        self.assertIn("--max-count", prompt)
        self.assertIn("head -80", prompt)

    def test_custom_skill_route_can_enter_bug_primary_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            skill_dir = root / ".ai" / "skills" / "source-analysis-skill"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: Source Analysis Skill\ndescription: 基于 skill 的源码分析。\n---\n\n# Source Analysis\n",
                encoding="utf-8",
            )
            config = BridgeConfig(dry_run=False, workspace_root=root, data_dir=Path(tmp) / "data")
            runner = BugAnalysisRunner(config)
            runner.skill_manager.set_skill_route("source-analysis-skill", role="primary")

            supported = runner.supported_primary_bug_skills()
            self.assertFalse(any(item["name"] == "source-analysis-skill" for item in supported))
            runner.skill_manager.set_skill_route("source-analysis-skill", role="primary", executor="file_agent")
            supported = runner.supported_primary_bug_skills()
            self.assertTrue(any(item["name"] == "source-analysis-skill" and item["executor"] == "file_agent" for item in supported))
            selection = runner.selection_for_skill_name(
                "source-analysis-skill",
                source="user_selected_card",
            )

        self.assertIsNotNone(selection)
        assert selection is not None
        self.assertEqual(selection.skill_name, "source-analysis-skill")
        self.assertEqual(selection.skill_label, "Source Analysis Skill")
        self.assertIn(selection.plans[0].kind, {"custom_skill", "source_code_skill"})

    def test_agent_selected_custom_skill_overrides_general_kind(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            skill_dir = root / ".ai" / "skills" / "source-analysis-skill"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: Source Analysis Skill\ndescription: 基于 skill 的源码分析。\n---\n\n# Source Analysis\n",
                encoding="utf-8",
            )
            runner = BugAnalysisRunner(BridgeConfig(dry_run=False, workspace_root=root, data_dir=Path(tmp) / "data"))
            runner.skill_manager.set_skill_route("source-analysis-skill", role="primary", kind="general")
            with mock.patch.object(
                runner,
                "_run_bug_decision_agent",
                return_value=(
                    {
                        "analysis_kind": "general",
                        "skill": "source-analysis-skill",
                        "signal_hint": "",
                        "reason": "用户明确要求按源码 skill 分析。",
                    },
                    "codex",
                ),
            ):
                selection = runner._classify_bug_request_with_agent(
                    prompt_text="基于 SRViolationHandler.kt 源码分析",
                    title="源码链路分析",
                    description="用户要求基于 skill 的源码分析",
                    attachments=[],
                )

        self.assertIsNotNone(selection)
        assert selection is not None
        self.assertEqual(selection.skill_name, "source-analysis-skill")
        self.assertIn(selection.plans[0].kind, {"custom_skill", "source_code_skill"})

    def test_bug_analysis_custom_skill_executor_not_ready_skips_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            data_dir = Path(tmp) / "data"
            skill_name = "source-analysis-skill"
            skill_dir = root / ".ai" / "skills" / skill_name
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\n"
                "name: Source Analysis Skill\n"
                "description: 基于 skill 的源码分析。\n"
                "---\n\n"
                "# Source Analysis\n",
                encoding="utf-8",
            )
            config = BridgeConfig(dry_run=False, data_dir=data_dir, workspace_root=root)
            runner = BugAnalysisRunner(config)
            runner.skill_manager.set_skill_route(skill_name, role="primary")
            log_root = Path(tmp) / "logs"
            self._write_matching_log(log_root, "2026-05-22 19:46:00")

            def fake_run_json_command(command, timeout):
                if "check-env" in command:
                    return {"meegle_installed": True, "auth_ok": True}
                if "resolve-url" in command:
                    return {"project_key": "xpfailuremgmt", "work_item_id": "6998107767"}
                if "fetch-data" in command:
                    return {
                        "title": "车道级导航退无图",
                        "status": "处理中",
                        "create_time": "2026-05-22 19:40",
                        "create_by": "tester",
                        "fields": {},
                        "attachments": [{"name": "Log.zip", "size": "12MB"}],
                        "description": "问题时间: 2026-05-22 19:46\n车道级不进，LD 退无图。",
                    }
                if command[:3] == ["meegle", "workitem", "get"]:
                    return {"data": {}}
                raise AssertionError(f"unexpected command: {command}")

            selection = runner.selection_for_skill_name(
                skill_name,
                source="agent",
                reason="命中源码分析专用 Skill",
                provider="codex",
            )
            assert selection is not None
            with (
                mock.patch.object(runner, "_run_json_command", side_effect=fake_run_json_command),
                mock.patch.object(runner, "_load_option_map", return_value={}),
                mock.patch.object(
                    runner,
                    "_download_bug_attachments",
                    return_value={"downloaded": ["Log.zip"], "unzipped": [], "errors": [], "skipped": [], "ok": True},
                ),
                mock.patch.object(runner, "_select_log_input", return_value=log_root),
                mock.patch.object(runner, "_prepare_log_input", return_value=log_root),
                mock.patch.object(runner, "_classify_bug_request_with_agent", return_value=selection),
                mock.patch.object(runner, "_build_bug_outputs", return_value=("# meta\n", "summary")),
                mock.patch.object(runner, "_build_combined_report_artifacts", return_value=None),
                mock.patch.object(
                    runner,
                    "_run_bug_agent_summary",
                    return_value={
                        "message": "fake root cause conclusion",
                        "command": ["codex", "exec"],
                        "error": "",
                        "provider": "codex",
                        "session_id": "sess_custom",
                        "resumed": False,
                    },
                ) as summary_mock,
            ):
                result = runner.run_bug_analysis(
                    BugRequest(
                        bug_url="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6998107767",
                        prompt="基于 SRViolationHandler.kt 源码重新分析",
                        raw_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6998107767 基于 SRViolationHandler.kt 源码重新分析",
                        triggered=True,
                    )
                )

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "custom_skill_executor_not_ready")
        self.assertIn("已命中专用 Skill", result.message)
        self.assertIn("当前没有可执行源码分析器", result.message)
        self.assertIn(skill_name, result.message)
        self.assertEqual(result.details["target_time"], "2026-05-22 19:46")
        self.assertEqual(result.details["prepared_log_input"], str(log_root))
        summary_mock.assert_not_called()

    def test_bug_analysis_source_stage_uses_source_analysis_for_builtin_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            data_dir = Path(tmp) / "data"
            skill_name = "scene-signal-diagnosis"
            skill_dir = root / ".ai" / "skills" / skill_name
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\n"
                "name: Scene Signal Diagnosis\n"
                "description: 3D 场景模式分析。\n"
                "---\n\n"
                "# Scene Signal\n",
                encoding="utf-8",
            )

            config = BridgeConfig(dry_run=False, data_dir=data_dir, workspace_root=root)
            config.bug_analysis.provider = "codex"
            config.bug_analysis.command = "codex"
            runner = BugAnalysisRunner(config)
            log_root = Path(tmp) / "logs"
            self._write_matching_log(log_root, "2026-05-25 16:50:41")
            executed_plans: list[str] = []
            seen_skill_names: list[str] = []

            selection = runner.selection_for_skill_name(
                skill_name,
                source="agent",
                reason="3D 场景模式命中专用 Skill",
                provider="codex",
            )
            assert selection is not None

            def fake_run_json_command(command, timeout, **kwargs):
                if "check-env" in command:
                    return {"meegle_installed": True, "auth_ok": True}
                if "resolve-url" in command:
                    return {"project_key": "xpfailuremgmt", "work_item_id": "6998811703"}
                if "fetch-data" in command:
                    return {
                        "title": "拾光主题切天玑后进入场景模式未展示 3D 场景",
                        "status": "处理中",
                        "create_time": "2026-05-25 16:50",
                        "attachments": [{"name": "Log.zip", "size": "12MB"}],
                        "fields": {},
                        "description": "问题时间: 2026-05-25 16:50:41\n分析 3D场景模式。",
                    }
                if command[:3] == ["meegle", "workitem", "get"]:
                    return {"data": {}}
                raise AssertionError(f"unexpected command: {command}")

            def fake_run_analysis(
                *,
                plan,
                input_path,
                html_path,
                json_path,
                analysis_dir,
                timeout,
                target_time,
                request_text=None,
                bridge_session_id=None,
            ):
                executed_plans.append(plan.kind)
                html_path.write_text("<html>scene</html>", encoding="utf-8")
                json_path.write_text('{"verdict":"scene ok"}', encoding="utf-8")
                return subprocess.CompletedProcess(args=["python3"], returncode=0, stdout="", stderr="")

            def fake_file_agent(**kwargs):
                seen_skill_names.append(kwargs["skill_name"])
                return {
                    "ok": False,
                    "error_code": "custom_skill_agent_failed",
                    "message": "stop after capturing skill",
                    "command": ["codex"],
                    "provider": "codex",
                    "stdout": "",
                    "stderr": "",
                    "stdout_path": Path(tmp) / "stdout.txt",
                    "stderr_path": Path(tmp) / "stderr.txt",
                    "command_path": Path(tmp) / "command.txt",
                    "analysis_markdown_path": Path(tmp) / "analysis.md",
                }

            with (
                mock.patch.object(runner, "_run_json_command", side_effect=fake_run_json_command),
                mock.patch.object(runner, "_load_option_map", return_value={}),
                mock.patch.object(
                    runner,
                    "_download_bug_attachments",
                    return_value={"downloaded": ["Log.zip"], "unzipped": [], "errors": [], "skipped": [], "ok": True},
                ),
                mock.patch.object(runner, "_select_log_input", return_value=log_root),
                mock.patch.object(runner, "_prepare_log_input", return_value=log_root),
                mock.patch.object(runner, "_write_reanalysis_source_evidence", return_value=None),
                mock.patch.object(runner, "_classify_bug_request_with_agent", return_value=selection),
                mock.patch.object(
                    runner,
                    "_decide_source_analysis_request",
                    return_value=mock.Mock(
                        requested=True,
                        reason="追加源码分析",
                        targets=["SceneType"],
                        source="agent",
                    ),
                ),
                mock.patch.object(runner, "_run_analysis", side_effect=fake_run_analysis),
                mock.patch.object(runner, "_run_custom_skill_agent_analysis", side_effect=fake_file_agent),
                mock.patch.object(runner, "_build_combined_report_artifacts", return_value=None),
            ):
                result = runner.run_bug_analysis(
                    BugRequest(
                        bug_url="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6998811703",
                        prompt="分析 3D场景模式",
                        raw_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6998811703 分析 3D场景模式",
                        triggered=True,
                    )
                )

        self.assertFalse(result.success)
        self.assertEqual(executed_plans, ["scene_signal"])
        self.assertEqual(seen_skill_names, ["source_analysis"])
        self.assertEqual(result.error_code, "source_stage_agent_failed")

    def test_bug_6998107767_ld_lane_level_builtin_runs_before_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            data_dir = Path(tmp) / "data"
            skill_name = "ld-lane-level-log-analysis-portable"
            skill_dir = root / ".ai" / "skills" / skill_name
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\n"
                "name: LD Lane Level Log Analysis\n"
                "description: 分析车道级、退无图和 LD 状态日志。\n"
                "---\n\n"
                "# LD Lane\n",
                encoding="utf-8",
            )
            config = BridgeConfig(dry_run=False, data_dir=data_dir, workspace_root=root)
            config.bug_analysis.provider = "codex"
            config.bug_analysis.command = "codex"
            runner = BugAnalysisRunner(config)
            log_root = Path(tmp) / "logs"
            self._write_matching_log(log_root, "2026-05-22 19:46:00")

            def fake_run_json_command(command, timeout):
                if "check-env" in command:
                    return {"meegle_installed": True, "auth_ok": True}
                if "resolve-url" in command:
                    return {"project_key": "xpfailuremgmt", "work_item_id": "6998107767"}
                if "fetch-data" in command:
                    return {
                        "title": "车道级导航退无图",
                        "status": "处理中",
                        "create_time": "2026-05-22 19:40",
                        "create_by": "tester",
                        "fields": {},
                        "attachments": [{"name": "Log.zip", "size": "12MB"}],
                        "description": "问题时间: 2026-05-22 19:46\n车道级不进，LD 退无图。",
                    }
                if command[:3] == ["meegle", "workitem", "get"]:
                    return {"data": {}}
                raise AssertionError(f"unexpected command: {command}")

            def fake_file_agent(command, **kwargs):
                output_path = Path(command[command.index("--output-last-message") + 1])
                output_path.write_text(
                    "## 结论摘要\nLD 车道级日志已重新分析。\n\n"
                    "## 关键证据\n"
                    "- `time_anchor.log`: 2026-05-22 19:46:00 覆盖用户故障时间。\n"
                    "- `ld.log`: LD lane level status changed to NO_MAP。\n\n"
                    "## 待确认项\n- 需要业务确认地图源状态。\n\n"
                    "## 建议动作\n- 继续追踪 LD 状态切换前一帧。\n",
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

            selection = runner.selection_for_skill_name(
                skill_name,
                source="agent",
                reason="LD 退无图命中车道级内建 Skill",
                provider="codex",
            )
            assert selection is not None

            def fake_build_bug_outputs(**kwargs):
                custom_json = kwargs["report_jsons"]["ld_lane_level"]
                self.assertIsNotNone(custom_json)
                assert custom_json is not None
                payload = json.loads(custom_json.read_text(encoding="utf-8"))
                self.assertEqual(payload["mode"], "ld_lane_level_agent_analysis")
                self.assertEqual(payload["ld_lane_level_analysis_status"], "completed")
                self.assertEqual(payload["analysis_kind"], "ld_lane_level")
                return "# meta\n", "script summary"

            with (
                mock.patch.object(runner, "_run_json_command", side_effect=fake_run_json_command),
                mock.patch.object(runner, "_load_option_map", return_value={}),
                mock.patch.object(
                    runner,
                    "_download_bug_attachments",
                    return_value={"downloaded": ["Log.zip"], "unzipped": [], "errors": [], "skipped": [], "ok": True},
                ),
                mock.patch.object(runner, "_select_log_input", return_value=log_root),
                mock.patch.object(runner, "_prepare_log_input", return_value=log_root),
                mock.patch.object(runner, "_classify_bug_request_with_agent", return_value=selection),
                mock.patch.object(runner, "_build_bug_outputs", side_effect=fake_build_bug_outputs),
                mock.patch.object(runner, "_build_combined_report_artifacts", return_value=None),
                mock.patch("lark_agent_bridge.agents.run_tracked_process", side_effect=fake_file_agent) as run_mock,
                mock.patch.object(
                    runner,
                    "_run_bug_agent_summary",
                    return_value={
                        "message": "agent final conclusion",
                        "command": ["codex", "exec"],
                        "error": "",
                        "provider": "codex",
                        "session_id": "sess_ld",
                        "resumed": False,
                    },
                ) as summary_mock,
            ):
                result = runner.run_bug_analysis(
                    BugRequest(
                        bug_url="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6998107767",
                        prompt="2026-05-22 19:46 退无图",
                        raw_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6998107767 2026-05-22 19:46 退无图",
                        triggered=True,
                    )
                )
                analysis_file_exists = Path(result.details.get("ld_lane_level_analysis_file", "")).exists()

        self.assertTrue(result.success)
        self.assertEqual(result.message, "agent final conclusion")
        self.assertEqual(result.details["analysis_kind"], "ld_lane_level")
        self.assertEqual(result.details["analysis_skill"], skill_name)
        self.assertEqual(result.details["ld_lane_level_analysis_status"], "completed")
        self.assertEqual(result.details["ld_lane_level_evidence_count"], 2)
        self.assertTrue(analysis_file_exists)
        run_mock.assert_called_once()
        summary_mock.assert_called_once()

    def test_custom_skill_analysis_validation_requires_key_evidence_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = BugAnalysisRunner(BridgeConfig(dry_run=False, data_dir=Path(tmp), workspace_root=Path(tmp)))
            valid = Path(tmp) / "custom_skill_analysis.md"
            valid.write_text(
                "## 结论摘要\n命中车道级异常。\n\n"
                "## 关键证据\n"
                "- `time_anchor.log`: 2026-05-22 19:46 LD 退无图。\n"
                "- `ld.log`: lane level status changed to NO_MAP。\n\n"
                "## 待确认项\n无\n",
                encoding="utf-8",
            )
            ok, reason, evidence_count = runner._validate_custom_skill_analysis(valid)

            self.assertTrue(ok)
            self.assertEqual(reason, "")
            self.assertEqual(evidence_count, 2)

            missing = Path(tmp) / "missing_key_evidence.md"
            missing.write_text("## 结论摘要\nL 不是证据计数。\n", encoding="utf-8")
            ok, reason, evidence_count = runner._validate_custom_skill_analysis(missing)
            self.assertFalse(ok)
            self.assertEqual(reason, "missing_key_evidence_section")
            self.assertEqual(evidence_count, 0)

            empty = Path(tmp) / "empty_key_evidence.md"
            empty.write_text("## 结论摘要\n占位。\n\n## 关键证据\n\n## 待确认项\n无\n", encoding="utf-8")
            ok, reason, evidence_count = runner._validate_custom_skill_analysis(empty)
            self.assertFalse(ok)
            self.assertEqual(reason, "empty_key_evidence_section")
            self.assertEqual(evidence_count, 0)

    def test_custom_skill_file_agent_timeout_returns_explicit_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            skill_name = "timeout-custom-skill"
            skill_dir = root / ".ai" / "skills" / skill_name
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: Timeout Custom Skill\ndescription: timeout test。\n---\n\n# Timeout\n",
                encoding="utf-8",
            )
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp) / "data", workspace_root=root)
            config.bug_analysis.provider = "codex"
            config.bug_analysis.command = "codex"
            runner = BugAnalysisRunner(config)
            log_root = Path(tmp) / "logs"
            self._write_matching_log(log_root, "2026-05-22 19:46:00")
            timeout_error = subprocess.TimeoutExpired(
                cmd=["codex", "exec"],
                timeout=12,
                output=b"partial",
                stderr=b"hung",
            )

            with mock.patch("lark_agent_bridge.agents.run_tracked_process", side_effect=timeout_error):
                result = runner._run_custom_skill_agent_analysis(
                    skill_name=skill_name,
                    request_text="2026-05-22 19:46 退无图",
                    prompt_text="2026-05-22 19:46 退无图",
                    title="车道级退无图",
                    description="",
                    fault_time="2026-05-22 19:46",
                    selected_input=log_root,
                    prepared_input=log_root,
                    source_evidence_path=None,
                    html_path=Path(tmp) / "bug_custom_skill_report.html",
                    json_path=Path(tmp) / "bug_custom_skill_report.json",
                    analysis_dir=Path(tmp) / "custom_skill_analysis",
                    progress_callback=None,
                    timeout=12,
                )
                stdout_text = Path(result["stdout_path"]).read_text(encoding="utf-8")
                stderr_text = Path(result["stderr_path"]).read_text(encoding="utf-8")

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "custom_skill_agent_timeout")
        self.assertIn("超时", result["message"])
        self.assertEqual(result["timeout_seconds"], 12)
        self.assertEqual(stdout_text, "partial")
        self.assertEqual(stderr_text, "hung")

    def test_custom_skill_file_agent_missing_output_keeps_diagnostics_and_clears_stale_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            skill_name = "missing-output-custom-skill"
            skill_dir = root / ".ai" / "skills" / skill_name
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: Missing Output Custom Skill\ndescription: missing output test。\n---\n\n# Missing Output\n",
                encoding="utf-8",
            )
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp) / "data", workspace_root=root)
            config.bug_analysis.provider = "claude"
            config.bug_analysis.command = "claude"
            runner = BugAnalysisRunner(config)
            log_root = Path(tmp) / "logs"
            self._write_matching_log(log_root, "2026-05-22 19:46:00")
            html_path = Path(tmp) / "bug_custom_skill_report.html"
            json_path = Path(tmp) / "bug_custom_skill_report.json"
            html_path.write_text("stale html", encoding="utf-8")
            json_path.write_text('{"mode":"custom_skill_overview"}', encoding="utf-8")

            with mock.patch(
                "lark_agent_bridge.agents.run_tracked_process",
                return_value=subprocess.CompletedProcess(args=["claude", "--print"], returncode=0, stdout="", stderr=""),
            ):
                result = runner._run_custom_skill_agent_analysis(
                    skill_name=skill_name,
                    request_text="2026-05-22 19:46 退无图",
                    prompt_text="2026-05-22 19:46 退无图",
                    title="车道级退无图",
                    description="",
                    fault_time="2026-05-22 19:46",
                    selected_input=log_root,
                    prepared_input=log_root,
                    source_evidence_path=None,
                    html_path=html_path,
                    json_path=json_path,
                    analysis_dir=Path(tmp) / "custom_skill_analysis",
                    progress_callback=None,
                    timeout=30,
                )
                self.assertFalse(result["ok"])
                self.assertEqual(result["error_code"], "custom_skill_agent_missing_output")
                self.assertIn("stdout.txt", result["message"])
                self.assertTrue(Path(result["stdout_path"]).exists())
                self.assertTrue(Path(result["stderr_path"]).exists())
                self.assertTrue(Path(result["command_path"]).exists())
                self.assertFalse(html_path.exists())
                self.assertFalse(json_path.exists())

    def test_custom_skill_file_agent_uses_configured_internal_network_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            skill_name = "network-env-custom-skill"
            skill_dir = root / ".ai" / "skills" / skill_name
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: Network Env Custom Skill\ndescription: proxy cleanup test。\n---\n\n# Network Env\n",
                encoding="utf-8",
            )
            config = BridgeConfig(
                dry_run=False,
                data_dir=Path(tmp) / "data",
                workspace_root=root,
                internal_network_env=InternalNetworkEnvOptions(
                    inherit_env=["PATH", "HOME"],
                    unset_env=["HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"],
                ),
            )
            config.bug_analysis.provider = "claude"
            config.bug_analysis.command = "claude"
            config.bug_analysis.file_agent_debug_logs = True
            config.guideengine_repo = root
            runner = BugAnalysisRunner(config)
            log_root = Path(tmp) / "logs"
            focus_log = log_root / "data" / "Log" / "log1" / "app" / "com.xiaopeng.montecarlo" / "main_2026-05-22_19-00.alog.log"
            focus_log.parent.mkdir(parents=True, exist_ok=True)
            focus_log.write_text("05-22 19:46:00.000 I Unity: focused log\n", encoding="utf-8")
            old_log = log_root / "data" / "Log" / "log1" / "app" / "com.xiaopeng.montecarlo" / "main_2026-05-21_10-00.alog.log"
            old_log.parent.mkdir(parents=True, exist_ok=True)
            old_log.write_text("05-21 10:00:00.000 I Unity: old log\n", encoding="utf-8")
            html_path = Path(tmp) / "bug_custom_skill_report.html"
            json_path = Path(tmp) / "bug_custom_skill_report.json"
            captured: dict[str, object] = {}

            def fake_file_agent(command, **kwargs):
                captured["env"] = kwargs.get("env")
                captured["command"] = command
                output = "## 结论摘要\n- 已执行。\n\n## 关键证据\n- L1: evidence\n\n## 待确认项\n- 无\n\n## 建议动作\n- 无\n"
                stdout_handle = kwargs.get("stdout")
                if stdout_handle is not None:
                    stdout_handle.write(output)
                    stdout_handle.flush()
                    return subprocess.CompletedProcess(command, 0, "", "")
                return subprocess.CompletedProcess(command, 0, json.dumps({"result": output}), "")

            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "PATH": "/usr/bin:/bin",
                        "HOME": "/Users/tester",
                        "HTTP_PROXY": "http://127.0.0.1:7897",
                        "HTTPS_PROXY": "http://127.0.0.1:7897",
                        "NO_PROXY": "localhost",
                    },
                    clear=True,
                ),
                mock.patch("lark_agent_bridge.agents.run_tracked_process", side_effect=fake_file_agent),
            ):
                result = runner._run_custom_skill_agent_analysis(
                    skill_name=skill_name,
                    request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6998107767 2026-05-22 19:46 退无图",
                    prompt_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6998107767 2026-05-22 19:46 退无图",
                    title="车道级退无图",
                    description="",
                    fault_time="2026-05-22 19:46",
                    selected_input=log_root,
                    prepared_input=log_root,
                    source_evidence_path=None,
                    html_path=html_path,
                    json_path=json_path,
                    analysis_dir=Path(tmp) / "custom_skill_analysis",
                    progress_callback=None,
                    timeout=30,
                )
                context_body = Path(result["context_path"]).read_text(encoding="utf-8")
                context_exists = Path(result["context_path"]).exists()
                focused_log_input_exists = Path(result["focused_log_input"]).exists()
                log_focus_manifest_exists = Path(result["log_focus_manifest_path"]).exists()

        self.assertTrue(result["ok"])
        env = captured["env"]
        self.assertIsInstance(env, dict)
        self.assertEqual(env.get("PATH"), "/usr/bin:/bin")
        self.assertEqual(env.get("HOME"), "/Users/tester")
        self.assertNotIn("HTTP_PROXY", env)
        self.assertNotIn("HTTPS_PROXY", env)
        self.assertNotIn("NO_PROXY", env)
        self.assertIn("--tools", captured["command"])
        self.assertIn("--debug-file", captured["command"])
        self.assertTrue(str(result["debug_log_path"]).endswith("source_code_skill_agent.debug.log"))
        self.assertTrue(context_exists)
        self.assertTrue(focused_log_input_exists)
        self.assertTrue(log_focus_manifest_exists)
        self.assertIn("## 输出要求", context_body)
        self.assertIn("## 执行约束", context_body)
        self.assertNotIn("project.feishu.cn", context_body)

    def test_bug_analysis_custom_skill_file_agent_runs_before_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            data_dir = Path(tmp) / "data"
            skill_name = "agent-ready-lane-skill"
            skill_dir = root / ".ai" / "skills" / skill_name
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\n"
                "name: Agent Ready Lane Skill\n"
                "description: 分析车道级、退无图和 LD 状态日志。\n"
                "---\n\n"
                "# Agent Ready Lane\n",
                encoding="utf-8",
            )
            config = BridgeConfig(dry_run=False, data_dir=data_dir, workspace_root=root)
            config.bug_analysis.provider = "codex"
            config.bug_analysis.command = "codex"
            runner = BugAnalysisRunner(config)
            runner.skill_manager.set_skill_route(skill_name, role="primary", executor="file_agent")
            log_root = Path(tmp) / "logs"
            self._write_matching_log(log_root, "2026-05-22 19:46:00")

            def fake_run_json_command(command, timeout):
                if "check-env" in command:
                    return {"meegle_installed": True, "auth_ok": True}
                if "resolve-url" in command:
                    return {"project_key": "xpfailuremgmt", "work_item_id": "6998107767"}
                if "fetch-data" in command:
                    return {
                        "title": "车道级导航退无图",
                        "status": "处理中",
                        "create_time": "2026-05-22 19:40",
                        "create_by": "tester",
                        "fields": {},
                        "attachments": [{"name": "Log.zip", "size": "12MB"}],
                        "description": "问题时间: 2026-05-22 19:46\n车道级不进，LD 退无图。",
                    }
                if command[:3] == ["meegle", "workitem", "get"]:
                    return {"data": {}}
                raise AssertionError(f"unexpected command: {command}")

            def fake_file_agent(command, **kwargs):
                output_path = Path(command[command.index("--output-last-message") + 1])
                output_path.write_text(
                    "## 结论摘要\n车道级状态在故障时间进入退无图。\n\n"
                    "## 关键证据\n"
                    "- `time_anchor.log`: 2026-05-22 19:46:00 覆盖用户故障时间。\n"
                    "- `ld.log`: LD lane level status changed to NO_MAP。\n\n"
                    "## 待确认项\n- 需要业务确认地图源状态。\n\n"
                    "## 建议动作\n- 继续追踪 LD 状态切换前一帧。\n",
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

            selection = runner.selection_for_skill_name(
                skill_name,
                source="agent",
                reason="LD 退无图命中车道级专用 Skill",
                provider="codex",
            )
            assert selection is not None

            def fake_build_bug_outputs(**kwargs):
                custom_json = kwargs["report_jsons"]["custom_skill"]
                self.assertIsNotNone(custom_json)
                assert custom_json is not None
                payload = json.loads(custom_json.read_text(encoding="utf-8"))
                self.assertEqual(payload["mode"], "custom_skill_agent_analysis")
                self.assertEqual(payload["custom_skill_analysis_status"], "completed")
                self.assertEqual(payload["analysis_kind"], "custom_skill")
                self.assertEqual(payload["evidence_count"], 2)
                return "# meta\n", "script summary"

            with (
                mock.patch.object(runner, "_run_json_command", side_effect=fake_run_json_command),
                mock.patch.object(runner, "_load_option_map", return_value={}),
                mock.patch.object(
                    runner,
                    "_download_bug_attachments",
                    return_value={"downloaded": ["Log.zip"], "unzipped": [], "errors": [], "skipped": [], "ok": True},
                ),
                mock.patch.object(runner, "_select_log_input", return_value=log_root),
                mock.patch.object(runner, "_prepare_log_input", return_value=log_root),
                mock.patch.object(runner, "_classify_bug_request_with_agent", return_value=selection),
                mock.patch.object(runner, "_build_bug_outputs", side_effect=fake_build_bug_outputs),
                mock.patch.object(runner, "_build_combined_report_artifacts", return_value=None),
                mock.patch("lark_agent_bridge.agents.run_tracked_process", side_effect=fake_file_agent) as run_mock,
                mock.patch.object(
                    runner,
                    "_run_bug_agent_summary",
                    return_value={
                        "message": "agent final conclusion",
                        "command": ["codex", "exec"],
                        "error": "",
                        "provider": "codex",
                        "session_id": "sess_custom",
                        "resumed": False,
                    },
                ) as summary_mock,
            ):
                result = runner.run_bug_analysis(
                    BugRequest(
                        bug_url="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6998107767",
                        prompt="2026-05-22 19:46 退无图",
                        raw_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6998107767 2026-05-22 19:46 退无图",
                        triggered=True,
                    )
                )
                analysis_file_exists = Path(result.details.get("custom_skill_analysis_file", "")).exists()

        self.assertTrue(result.success)
        self.assertEqual(result.message, "agent final conclusion")
        self.assertEqual(result.details["analysis_kind"], "custom_skill")
        self.assertEqual(result.details["custom_skill_analysis_status"], "completed")
        self.assertEqual(result.details["custom_skill_evidence_count"], 2)
        self.assertTrue(analysis_file_exists)
        run_mock.assert_called_once()
        summary_mock.assert_called_once()

    def test_bug_analysis_custom_skill_invalid_evidence_skips_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            data_dir = Path(tmp) / "data"
            skill_name = "agent-ready-lane-skill"
            skill_dir = root / ".ai" / "skills" / skill_name
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: Agent Ready Lane Skill\ndescription: 分析车道级日志。\n---\n\n# Lane\n",
                encoding="utf-8",
            )
            config = BridgeConfig(dry_run=False, data_dir=data_dir, workspace_root=root)
            config.bug_analysis.provider = "codex"
            config.bug_analysis.command = "codex"
            runner = BugAnalysisRunner(config)
            runner.skill_manager.set_skill_route(skill_name, role="primary", executor="file_agent")
            log_root = Path(tmp) / "logs"
            self._write_matching_log(log_root, "2026-05-22 19:46:00")

            def fake_run_json_command(command, timeout):
                if "check-env" in command:
                    return {"meegle_installed": True, "auth_ok": True}
                if "resolve-url" in command:
                    return {"project_key": "xpfailuremgmt", "work_item_id": "6998107767"}
                if "fetch-data" in command:
                    return {
                        "title": "车道级导航退无图",
                        "status": "处理中",
                        "create_time": "2026-05-22 19:40",
                        "create_by": "tester",
                        "fields": {},
                        "attachments": [{"name": "Log.zip", "size": "12MB"}],
                        "description": "问题时间: 2026-05-22 19:46\n车道级不进，LD 退无图。",
                    }
                if command[:3] == ["meegle", "workitem", "get"]:
                    return {"data": {}}
                raise AssertionError(f"unexpected command: {command}")

            def fake_file_agent(command, **kwargs):
                output_path = Path(command[command.index("--output-last-message") + 1])
                output_path.write_text(
                    "## 结论摘要\n占位结论。\n\n## 关键证据\n\n## 待确认项\n无\n",
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

            selection = runner.selection_for_skill_name(skill_name, source="agent", reason="LD 退无图", provider="codex")
            assert selection is not None
            with (
                mock.patch.object(runner, "_run_json_command", side_effect=fake_run_json_command),
                mock.patch.object(runner, "_load_option_map", return_value={}),
                mock.patch.object(
                    runner,
                    "_download_bug_attachments",
                    return_value={"downloaded": ["Log.zip"], "unzipped": [], "errors": [], "skipped": [], "ok": True},
                ),
                mock.patch.object(runner, "_select_log_input", return_value=log_root),
                mock.patch.object(runner, "_prepare_log_input", return_value=log_root),
                mock.patch.object(runner, "_classify_bug_request_with_agent", return_value=selection),
                mock.patch("lark_agent_bridge.agents.run_tracked_process", side_effect=fake_file_agent),
                mock.patch.object(runner, "_build_bug_outputs", side_effect=AssertionError("summary metadata should not build")),
                mock.patch.object(runner, "_run_bug_agent_summary") as summary_mock,
            ):
                result = runner.run_bug_analysis(
                    BugRequest(
                        bug_url="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6998107767",
                        prompt="2026-05-22 19:46 退无图",
                        raw_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6998107767 2026-05-22 19:46 退无图",
                        triggered=True,
                    )
                )

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "custom_skill_agent_invalid_evidence")
        self.assertIn("关键证据", result.message)
        summary_mock.assert_not_called()

    def test_bug_analysis_scene_signal_with_added_source_stage_uses_source_analysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            data_dir = Path(tmp) / "data"
            skill_name = "scene-signal-diagnosis"
            skill_dir = root / ".ai" / "skills" / skill_name
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: Scene Signal Diagnosis\ndescription: 分析 3D 场景信号。\n---\n\n# Scene Signal\n",
                encoding="utf-8",
            )
            config = BridgeConfig(dry_run=False, data_dir=data_dir, workspace_root=root)
            config.bug_analysis.provider = "codex"
            config.bug_analysis.command = "codex"
            runner = BugAnalysisRunner(config)
            log_root = Path(tmp) / "logs"
            self._write_matching_log(log_root, "2026-05-25 16:50:41")

            def fake_run_json_command(command, timeout):
                if "check-env" in command:
                    return {"meegle_installed": True, "auth_ok": True}
                if "resolve-url" in command:
                    return {"project_key": "xpfailuremgmt", "work_item_id": "6998811703"}
                if "fetch-data" in command:
                    return {
                        "title": "场景模式异常",
                        "status": "处理中",
                        "create_time": "2026-05-25 16:50",
                        "create_by": "tester",
                        "fields": {},
                        "attachments": [{"name": "Log.zip", "size": "12MB"}],
                        "description": "问题时间: 2026-05-25 16:50:41\n拾光主题切换成天玑场景模式异常。",
                    }
                if command[:3] == ["meegle", "workitem", "get"]:
                    return {"data": {}}
                raise AssertionError(f"unexpected command: {command}")

            selection = runner._selection_from_plans(
                [BugAnalysisPlan(kind="scene_signal"), BugAnalysisPlan(kind="source_stage")],
                source="agent",
                reason="场景信号主分析 + 源码分析补充",
                provider="codex",
            )
            selection.skill_name = skill_name
            selection.skill_label = "3D场景信号分析"
            seen_skill_names: list[str] = []

            def fake_scene_signal(*, plan, input_path, html_path, json_path, analysis_dir, timeout, target_time, request_text=None):
                html_path.write_text("<html>scene signal</html>", encoding="utf-8")
                json_path.write_text("{}", encoding="utf-8")
                return subprocess.CompletedProcess(args=["python"], returncode=0, stdout="", stderr="")

            def fake_file_agent(**kwargs):
                seen_skill_names.append(kwargs["skill_name"])
                return {
                    "ok": False,
                    "error_code": "custom_skill_agent_failed",
                    "message": "stop after capturing skill",
                    "command": ["codex"],
                    "provider": "codex",
                    "stdout": "",
                    "stderr": "",
                    "stdout_path": Path(tmp) / "stdout.txt",
                    "stderr_path": Path(tmp) / "stderr.txt",
                    "command_path": Path(tmp) / "command.txt",
                    "analysis_markdown_path": Path(tmp) / "analysis.md",
                }

            with (
                mock.patch.object(runner, "_run_json_command", side_effect=fake_run_json_command),
                mock.patch.object(runner, "_load_option_map", return_value={}),
                mock.patch.object(
                    runner,
                    "_download_bug_attachments",
                    return_value={"downloaded": ["Log.zip"], "unzipped": [], "errors": [], "skipped": [], "ok": True},
                ),
                mock.patch.object(runner, "_select_log_input", return_value=log_root),
                mock.patch.object(runner, "_prepare_log_input", return_value=log_root),
                mock.patch.object(runner, "_classify_bug_request_with_agent", return_value=selection),
                mock.patch.object(runner, "_run_analysis", side_effect=fake_scene_signal),
                mock.patch.object(runner, "_run_custom_skill_agent_analysis", side_effect=fake_file_agent),
            ):
                result = runner.run_bug_analysis(
                    BugRequest(
                        bug_url="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6998811703",
                        prompt="分析 3D场景模式",
                        raw_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6998811703 分析 3D场景模式",
                        triggered=True,
                    )
                )

        self.assertFalse(result.success)
        self.assertEqual(seen_skill_names, ["source_analysis"])

    def test_bug_analysis_classifies_startup_and_stuck_requests_together(self):
        runner = BugAnalysisRunner(BridgeConfig(dry_run=True))

        plans = runner.classify_requests(
            prompt_text="分析启动和卡顿",
            title="",
            description="",
        )

        self.assertEqual([plan.kind for plan in plans], ["startup", "stuck"])

    def test_bug_analysis_classifies_black_white_theme_request_as_xtheme(self):
        runner = BugAnalysisRunner(BridgeConfig(dry_run=True))

        plan = runner.classify_request(
            prompt_text="分析主题相关",
            title="临停P档，切换黑白夜，场景模式没有更随黑白夜",
            description="",
        )

        self.assertEqual(plan.kind, "xtheme")
        self.assertIsNone(plan.signal_code)

    def test_bug_analysis_prefers_xtheme_over_generic_signal_terms(self):
        runner = BugAnalysisRunner(BridgeConfig(dry_run=True))

        plan = runner.classify_request(
            prompt_text="分析 105004 主题切换信号为什么异常",
            title="晨曦时光主题没有切换",
            description="xtheme 相关主题变化异常",
        )

        self.assertEqual(plan.kind, "xtheme")
        self.assertIsNone(plan.signal_code)

    def test_bug_analysis_prefers_perception_over_generic_signal_terms(self):
        runner = BugAnalysisRunner(BridgeConfig(dry_run=True))

        plan = runner.classify_request(
            prompt_text="分析当前感知数据和信号链路",
            title="SR无感知显示",
            description="请看 VHALHelper / X3DCB / XDataNativeProxy",
        )

        self.assertEqual(plan.kind, "perception")
        self.assertIsNone(plan.signal_code)

    def test_bug_analysis_does_not_pick_signal_from_report_css_for_source_unity_followup(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "guideengine"
            signal_proto = repo / "module_floorcenter/module_proto/src/main/proto/signal.proto"
            signal_proto.parent.mkdir(parents=True, exist_ok=True)
            signal_proto.write_text("SIGNAL_CTL_DRIVE_SETTINGS_EPB = 14002;\n", encoding="utf-8")
            runner = BugAnalysisRunner(BridgeConfig(dry_run=True, guideengine_repo=repo))

            plans = runner.classify_requests(
                prompt_text="基于源码分析 unity场景",
                title=(
                    "车机大屏在停车驻车状态下页面卡住,SR页面无法正常显示"
                    " https://project.feishu.cn/xpfailuremgmt/buglo/detail/6979499593 分析3D生命周期"
                ),
                description=(
                    "信号链路总览 CSS --green:#059669; "
                    "报告链接 http://10.99.149.127:8765/reports/e1c56e6bb411636da68eea2e0c91f5cc/"
                ),
            )

        self.assertNotIn("signal", [plan.kind for plan in plans])
        self.assertTrue(any(plan.kind in {"startup", "stuck", "general"} for plan in plans))

    def test_reanalysis_source_terms_keep_mixed_unity_and_sr_business_words(self):
        runner = BugAnalysisRunner(BridgeConfig(dry_run=True))

        terms = runner._source_evidence_terms(
            plans=[BugAnalysisPlan(kind="stuck")],
            request_text="车机大屏 P挡时 SR页面无法显示",
            followup_text="基于源码分析 unity场景",
        )

        self.assertIn("SR页面", terms)
        self.assertIn("unity场景", terms)
        self.assertIn("unity", [term.casefold() for term in terms])

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

    def test_bug_analysis_extract_fault_time_normalizes_fullwidth_colon(self):
        runner = BugAnalysisRunner(BridgeConfig(dry_run=True))

        target_time, note = runner._extract_fault_time("", "问题时间: 2026-5-11 23：10")

        self.assertEqual(target_time, "2026-05-11 23:10")
        self.assertEqual(note, "从缺陷描述提取")

    def test_bug_time_context_completes_user_short_time_from_description_date(self):
        runner = BugAnalysisRunner(BridgeConfig(dry_run=True))

        context = runner._resolve_bug_time_context(
            request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/1 修正问题时间 23:12 重新分析",
            title="车机页面卡住",
            description="原始问题时间: 2026-05-11 23:10\n页面卡住",
        )

        self.assertEqual(context.fault_time, "2026-05-11 23:12")
        self.assertEqual(context.source, "user")
        self.assertTrue(context.has_full_datetime)

    def test_bug_time_context_uses_bug_create_year_for_description_month_day_time(self):
        runner = BugAnalysisRunner(BridgeConfig(dry_run=True))

        context = runner._resolve_bug_time_context(
            request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6995380113 分析3D生命周期",
            title="sr底图黑屏，不显示内容",
            description="车型：F57AES\n问题时间：05-19 14:33\n问题描述：sr底图黑屏",
            reference_time="2026-05-20T15:24:24+08:00",
        )

        self.assertEqual(context.fault_time, "2026-05-19 14:33")
        self.assertEqual(context.source, "description")
        self.assertTrue(context.has_full_datetime)

    def test_bug_time_context_completes_description_short_time_from_reference_date(self):
        runner = BugAnalysisRunner(BridgeConfig(dry_run=True))

        context = runner._resolve_bug_time_context(
            request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6997398474 分析感知数据",
            title="SR无感知显示",
            description="用户反馈 16:17 SR 无感知显示",
            reference_time="2026-05-22T16:39:07+08:00",
        )

        self.assertEqual(context.fault_time, "2026-05-22 16:17")
        self.assertEqual(context.source, "description")
        self.assertTrue(context.has_full_datetime)

    def test_bug_reference_time_uses_full_workitem_create_time_when_fetch_data_missing(self):
        runner = BugAnalysisRunner(BridgeConfig(dry_run=True))

        reference = runner._bug_reference_time(
            {"title": "SR无感知显示"},
            {"work_item_attribute": {"create_time": "2026-05-22T16:39:07+08:00"}},
        )

        self.assertEqual(reference, "2026-05-22T16:39:07+08:00")

    def test_bug_time_context_parses_bracket_date_format(self):
        """[05/19] 14:33 format should be recognized as a valid MM/DD HH:mm date."""
        runner = BugAnalysisRunner(BridgeConfig(dry_run=True))

        context = runner._resolve_bug_time_context(
            request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/123 分析根因",
            title="[05/19] 14:33 sr底图黑屏",
            description="车型：F57AES\n问题描述：sr底图黑屏",
            reference_time="2026-05-20T15:24:24+08:00",
        )

        self.assertIn("05-19", context.fault_time)
        self.assertIn("14:33", context.fault_time)
        self.assertTrue(context.has_full_datetime)

    def test_extract_time_candidate_bracket_date(self):
        """Direct test that _extract_time_candidate handles [MM/DD] HH:mm."""
        runner = BugAnalysisRunner(BridgeConfig(dry_run=True))

        result = runner._extract_time_candidate("[05/19] 14:33 sr底图黑屏")

        self.assertIsNotNone(result)
        self.assertIn("05-19", result["date"])
        self.assertEqual(result["time"], "14:33")

    def test_extract_time_via_llm_fallback_success(self):
        """When regex fails, LLM extraction should return valid time."""
        runner = BugAnalysisRunner(BridgeConfig(dry_run=True))
        fake_response = mock.MagicMock()
        fake_response.content = json.dumps({
            "found": True,
            "datetime": "2026-05-19 18:35",
            "source": "标题",
            "reason": "从标题提取中文时间",
        })
        fake_client = mock.MagicMock()
        fake_client.is_available.return_value = True
        fake_client.chat.return_value = fake_response
        fake_client._effective_api_format.return_value = "openai"

        with mock.patch(
            "lark_agent_bridge.agents.llm_client.LLMClient",
            return_value=fake_client,
        ):
            result = runner._extract_time_via_llm(
                request_text="",
                title="G02ESVR_SR2.0_V5511六座左舵车_5月19日_18点35分_全域智驾场景未显示车位信息",
                description="",
            )

        self.assertIsNotNone(result)
        self.assertEqual(result["value"], "2026-05-19 18:35")
        self.assertEqual(result["date"], "2026-05-19")
        self.assertEqual(result["time"], "18:35")
        self.assertEqual(result["source"], "llm")

    def test_extract_time_via_llm_returns_none_when_unavailable(self):
        """When LLMClient is not configured, return None gracefully."""
        runner = BugAnalysisRunner(BridgeConfig(dry_run=True))
        fake_client = mock.MagicMock()
        fake_client.is_available.return_value = False

        with mock.patch(
            "lark_agent_bridge.agents.llm_client.LLMClient",
            return_value=fake_client,
        ):
            result = runner._extract_time_via_llm(
                request_text="",
                title="5月19日_18点35分_测试",
                description="",
            )

        self.assertIsNone(result)

    def test_extract_time_via_llm_handles_invalid_json(self):
        """When LLM returns invalid JSON, return None gracefully."""
        runner = BugAnalysisRunner(BridgeConfig(dry_run=True))
        fake_response = mock.MagicMock()
        fake_response.content = "not valid json"
        fake_client = mock.MagicMock()
        fake_client.is_available.return_value = True
        fake_client.chat.return_value = fake_response
        fake_client._effective_api_format.return_value = "openai"

        with mock.patch(
            "lark_agent_bridge.agents.llm_client.LLMClient",
            return_value=fake_client,
        ):
            result = runner._extract_time_via_llm(
                request_text="",
                title="5月19日_18点35分_测试",
                description="",
            )

        self.assertIsNone(result)

    def test_extract_time_via_llm_handles_found_false(self):
        """When LLM returns found=false, return None."""
        runner = BugAnalysisRunner(BridgeConfig(dry_run=True))
        fake_response = mock.MagicMock()
        fake_response.content = json.dumps({"found": False, "datetime": "", "source": "", "reason": "无时间"})
        fake_client = mock.MagicMock()
        fake_client.is_available.return_value = True
        fake_client.chat.return_value = fake_response
        fake_client._effective_api_format.return_value = "openai"

        with mock.patch(
            "lark_agent_bridge.agents.llm_client.LLMClient",
            return_value=fake_client,
        ):
            result = runner._extract_time_via_llm(
                request_text="分析问题",
                title="无时间信息的标题",
                description="",
            )

        self.assertIsNone(result)

    def test_resolve_bug_time_context_uses_llm_fallback(self):
        """_resolve_bug_time_context should call LLM when regex finds nothing."""
        runner = BugAnalysisRunner(BridgeConfig(dry_run=True))
        llm_return = {
            "value": "2026-05-19 18:35",
            "date": "2026-05-19",
            "time": "18:35",
            "raw": "标题",
            "source": "llm",
            "note": "从标题提取",
        }

        with mock.patch.object(runner, "_extract_time_via_llm", return_value=llm_return):
            context = runner._resolve_bug_time_context(
                request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/1",
                title="G02ESVR_SR2.0_V5511六座左舵车_5月19日_18点35分_全域智驾场景未显示车位信息",
                description="",
            )

        self.assertEqual(context.fault_time, "2026-05-19 18:35")
        self.assertTrue(context.has_full_datetime)
        self.assertEqual(context.source, "llm")

    def test_resolve_bug_time_context_skips_llm_when_regex_succeeds(self):
        """LLM should not be called when regex already found a time."""
        runner = BugAnalysisRunner(BridgeConfig(dry_run=True))

        with mock.patch.object(runner, "_extract_time_via_llm") as llm_mock:
            context = runner._resolve_bug_time_context(
                request_text="",
                title="问题时间 2026-05-11 23:12",
                description="",
            )

        llm_mock.assert_not_called()
        self.assertEqual(context.fault_time, "2026-05-11 23:12")
        self.assertTrue(context.has_full_datetime)
        self.assertEqual(context.source, "title")

    def test_bug_log_coverage_detects_android_log_time_inside_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = BugAnalysisRunner(BridgeConfig(dry_run=True))
            log_path = Path(tmp) / "Log" / "log0" / "app" / "com.xiaopeng.montecarlo" / "main.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(
                "05-11 23:08:00.000 I MonteCarlo: before\n"
                "05-11 23:12:30.000 I MonteCarlo: target\n",
                encoding="utf-8",
            )

            coverage = runner._scan_log_time_coverage(Path(tmp), fault_time="2026-05-11 23:12")

        self.assertTrue(coverage.has_time_evidence)
        self.assertTrue(coverage.covers_fault_time)
        self.assertEqual(coverage.start_time, "2026-05-11 23:08")
        self.assertEqual(coverage.end_time, "2026-05-11 23:12")

    def test_bug_analysis_missing_problem_time_asks_before_download(self):
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
                        "title": "3D 页面卡顿",
                        "status": "处理中",
                        "fields": {},
                        "attachments": [{"name": "Log.zip", "size": "12MB"}],
                        "description": "用户反馈页面卡顿，但未写明几月几日几点几分。",
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
                    side_effect=AssertionError("missing problem time should stop before download"),
                ),
            ):
                result = runner.run_bug_analysis(
                    BugRequest(
                        bug_url="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6991604970",
                        prompt="分析3D卡顿",
                        raw_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6991604970 分析3D卡顿",
                        triggered=True,
                    )
                )

        self.assertTrue(result.success)
        self.assertTrue(result.skipped)
        self.assertEqual(result.details["mode"], "bug_time_clarification")
        self.assertEqual(result.details["time_gate_status"], "missing_fault_time")
        self.assertIn("缺少明确问题时间", result.message)
        self.assertIn("几月几日 几点几分", result.message)

    def test_bug_analysis_log_time_mismatch_asks_for_matching_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp), workspace_root=Path(tmp))
            runner = BugAnalysisRunner(config)
            log_root = Path(tmp) / "logs"
            log_file = log_root / "Log" / "log0" / "app" / "com.xiaopeng.montecarlo" / "main.log"
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_file.write_text("05-10 10:00:00.000 I MonteCarlo: old log\n", encoding="utf-8")

            def fake_run_json_command(command, timeout):
                if "check-env" in command:
                    return {"meegle_installed": True, "auth_ok": True}
                if "resolve-url" in command:
                    return {"project_key": "xpfailuremgmt", "work_item_id": "6991604970"}
                if "fetch-data" in command:
                    return {
                        "title": "3D 页面卡顿",
                        "status": "处理中",
                        "fields": {},
                        "attachments": [{"name": "Log.zip", "size": "12MB"}],
                        "description": "问题时间: 2026-05-11 23:12\n用户反馈页面卡顿。",
                    }
                if command[:3] == ["meegle", "workitem", "get"]:
                    return {"work_item_current_node": []}
                raise AssertionError(f"unexpected command: {command}")

            with (
                mock.patch.object(runner, "_run_json_command", side_effect=fake_run_json_command),
                mock.patch.object(runner, "_load_option_map", return_value={}),
                mock.patch.object(runner, "_download_bug_attachments", return_value={"downloaded": ["Log.zip"], "unzipped": [], "errors": [], "skipped": [], "ok": True}),
                mock.patch.object(runner, "_select_log_input", return_value=log_root),
                mock.patch.object(runner, "_prepare_log_input", return_value=log_root),
                mock.patch.object(runner, "_run_analysis", side_effect=AssertionError("log time mismatch should stop before analysis")),
            ):
                result = runner.run_bug_analysis(
                    BugRequest(
                        bug_url="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6991604970",
                        prompt="分析3D卡顿",
                        raw_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6991604970 分析3D卡顿",
                        triggered=True,
                    )
                )

        self.assertTrue(result.success)
        self.assertTrue(result.skipped)
        self.assertEqual(result.details["mode"], "bug_time_clarification")
        self.assertEqual(result.details["time_gate_status"], "log_not_covering_fault_time")
        self.assertIn("日志时间范围未覆盖问题时间", result.message)
        self.assertIn("2026-05-11 23:12", result.message)

    def test_bug_analysis_classifies_perception_request(self):
        runner = BugAnalysisRunner(BridgeConfig(dry_run=True))

        plans = runner.classify_requests(
            prompt_text="总结当前感知数据",
            title="",
            description="",
        )

        self.assertEqual([plan.kind for plan in plans], ["perception"])

    def test_bug_analysis_classifies_pullover_chain_request_chinese(self):
        runner = BugAnalysisRunner(BridgeConfig(dry_run=True))
        plan = runner.classify_request(
            prompt_text="靠边停车不显示",
            title="",
            description="",
        )
        self.assertEqual(plan.kind, "pullover_chain")
        self.assertIsNone(plan.signal_code)

    def test_bug_analysis_classifies_pullover_chain_request_english(self):
        runner = BugAnalysisRunner(BridgeConfig(dry_run=True))
        plan = runner.classify_request(
            prompt_text="百度回调没有 SideParkInfo",
            title="",
            description="",
        )
        self.assertEqual(plan.kind, "pullover_chain")

    def test_pullover_chain_kind_is_agent_handled_custom_agent(self):
        from lark_agent_bridge.agents.bug_runner import _kind_spec
        spec = _kind_spec("pullover_chain")
        self.assertTrue(spec.is_agent_handled)
        self.assertTrue(spec.is_custom_agent)
        self.assertFalse(spec.is_source_stage)
        self.assertFalse(spec.needs_source_evidence)
        self.assertFalse(spec.log_dependent)
        self.assertFalse(spec.needs_custom_executor_check)

    def test_pullover_chain_report_name_and_label(self):
        runner = BugAnalysisRunner(BridgeConfig(dry_run=True))
        self.assertEqual(runner._report_name("pullover_chain", "html"), "bug_pullover_chain_report.html")
        self.assertEqual(runner._report_name("pullover_chain", "json"), "bug_pullover_chain_report.json")
        self.assertEqual(runner._analysis_label("pullover_chain"), "靠边停车链路分析")

    def test_pullover_chain_registered_in_primary_skill_map(self):
        from lark_agent_bridge.skill_registry import PRIMARY_BUG_SKILL_MAP
        entry = PRIMARY_BUG_SKILL_MAP.get("pullover-chain-analyzer")
        self.assertIsNotNone(entry)
        kind, label, requires_logs = entry
        self.assertEqual(kind, "pullover_chain")
        self.assertEqual(label, "靠边停车链路分析")
        self.assertFalse(requires_logs)

    def test_pullover_chain_in_all_plan_kinds(self):
        from lark_agent_bridge.agents.bug_runner import ALL_PLAN_KINDS
        self.assertIn("pullover_chain", ALL_PLAN_KINDS)

    def test_pullover_chain_does_not_accept_target_time(self):
        from lark_agent_bridge.agents.bug_runner import _kind_accepts_target_time
        # Agent-handled kinds pass fault_time via prompt, not as a CLI flag.
        self.assertFalse(_kind_accepts_target_time("pullover_chain"))

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

    def test_bug_analysis_dry_run_routes_to_scene_signal_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=True, data_dir=Path(tmp), workspace_root=Path(tmp))
            result = BugAnalysisRunner(config).run_bug_analysis(
                BugRequest(
                    bug_url="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722",
                    prompt="分析3D场景信号",
                    raw_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 分析3D场景信号",
                    triggered=True,
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["analysis_kind"], "scene_signal")
        self.assertTrue(any("extract_scene_signal_events.py" in part for part in result.command))
        self.assertIn("bug_scene_signal_report.html", result.message)

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
                    prompt="总结当前感知数据 时间点2026-05-22 16:17",
                    raw_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 总结当前感知数据 时间点2026-05-22 16:17",
                    triggered=True,
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["analysis_kinds"], ["perception"])
        self.assertIn("--target-time", result.command)
        self.assertIn("2026-05-22 16:17", result.command)
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

    def test_bug_analysis_selects_decoded_startup_log_over_raw_alog(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = BugAnalysisRunner(BridgeConfig(dry_run=True))
            root = Path(tmp) / "Log"
            target_dir = root / "log0" / "app" / "com.xiaopeng.montecarlo"
            target_dir.mkdir(parents=True, exist_ok=True)
            raw = target_dir / "main_2026-05-11_11-00.alog"
            decoded = target_dir / "main_2026-05-11_11-00.alog.log"
            raw.write_text("", encoding="utf-8")
            decoded.write_text("", encoding="utf-8")

            selected = runner._select_startup_input(root, "2026-05-11 11:30")

        self.assertEqual(selected, decoded)

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

    def test_bug_analysis_selects_and_extracts_7z_log_attachment(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = BugAnalysisRunner(BridgeConfig(dry_run=True))
            bug_dir = Path(tmp)
            attachments_dir = bug_dir / "attachments"
            attachments_dir.mkdir(parents=True, exist_ok=True)
            archive = attachments_dir / "20260519.7z"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("Log/log0/app/com.xiaopeng.montecarlo/main_2026-05-19_15-00.txt", "signal log")

            selected = runner._select_log_input(
                bug_dir,
                fetched={"attachments": [{"name": "20260519.7z"}]},
            )
            prepared = runner._prepare_log_input(selected)

            self.assertEqual(selected, archive)
            self.assertTrue(prepared.is_dir())
            self.assertTrue((prepared / "Log/log0/app/com.xiaopeng.montecarlo/main_2026-05-19_15-00.txt").exists())

    def test_bug_analysis_prepare_log_input_extracts_archive_in_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = BugAnalysisRunner(BridgeConfig(dry_run=True))
            input_dir = Path(tmp) / "input"
            input_dir.mkdir()
            (input_dir / "browser").write_text("<html>page</html>", encoding="utf-8")
            archive = input_dir / "Log.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("Log/log0/app/com.xiaopeng.test/main_2026-05-28_15-00.alog", "binary-log")

            prepared = runner._prepare_log_input(input_dir)

            self.assertTrue(prepared.is_dir())
            self.assertTrue((prepared / "Log/log0/app/com.xiaopeng.test/main_2026-05-28_15-00.alog").exists())

    def test_bug_analysis_startup_uses_prepared_directory_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp), workspace_root=Path(tmp))
            runner = BugAnalysisRunner(config)
            log_root = Path(tmp) / "logs"
            log_root.mkdir(parents=True, exist_ok=True)
            self._write_matching_log(log_root, "2026-05-11 23:10:00")
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

    def test_bug_agent_summary_once_writes_prompt_audit_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp), workspace_root=Path(tmp))
            runner = BugAnalysisRunner(config)
            output_path = Path(tmp) / "bug_agent_summary.md"
            completed = subprocess.CompletedProcess(
                args=["codex"],
                returncode=0,
                stdout="Agent summary text",
                stderr="",
            )
            invocation = {
                "command": ["codex", "exec", "PROMPT"],
                "provider": "codex",
                "session_id": "",
                "resumed": False,
                "prompt": "PROMPT BODY",
                "embedded_files": [
                    {
                        "title": "Bug Metadata",
                        "path": str(Path(tmp) / "bug_metadata.md"),
                        "max_chars": 6000,
                    }
                ],
            }

            with mock.patch("lark_agent_bridge.agents.run_tracked_process", return_value=completed):
                result = runner._run_bug_agent_summary_once(
                    invocation=invocation,
                    output_path=output_path,
                    progress_callback=None,
                    timeout=60,
                )

            prompt_file = Path(result["prompt_file"])
            context_file = Path(result["context_file"])
            prompt_text = prompt_file.read_text(encoding="utf-8")
            context = json.loads(context_file.read_text(encoding="utf-8"))

        self.assertEqual(prompt_file.name, "bug_agent_summary_prompt.md")
        self.assertIn("PROMPT BODY", prompt_text)
        self.assertEqual(context["provider"], "codex")
        self.assertEqual(context["embedded_files"][0]["title"], "Bug Metadata")

    def test_bug_agent_summary_once_timeout_returns_explicit_timeout_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp), workspace_root=Path(tmp))
            runner = BugAnalysisRunner(config)
            output_path = Path(tmp) / "bug_agent_summary.md"
            timeout_error = subprocess.TimeoutExpired(cmd=["codex"], timeout=3)

            with mock.patch("lark_agent_bridge.agents.run_tracked_process", side_effect=timeout_error):
                result = runner._run_bug_agent_summary_once(
                    invocation={"command": ["codex"], "provider": "codex", "session_id": "", "resumed": False},
                    output_path=output_path,
                    progress_callback=None,
                    timeout=3,
                )

        self.assertEqual(result["message"], "")
        self.assertEqual(result["error"], "agent_summary_timeout")
        self.assertEqual(result["timeout_seconds"], 3)

    def test_bug_agent_summary_timeout_uses_fresh_output_without_provider_fallback(self):
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
            calls = []

            def timeout_after_writing_output(command, **kwargs):
                calls.append(command)
                output_path.write_text("fresh codex summary", encoding="utf-8")
                raise subprocess.TimeoutExpired(cmd=command, timeout=3)

            with mock.patch("lark_agent_bridge.agents.run_tracked_process", side_effect=timeout_after_writing_output):
                result = runner._run_bug_agent_summary(
                    request_text="根据导航源码分析",
                    request_artifact=request_artifact,
                    metadata_path=metadata_path,
                    output_path=output_path,
                    progress_callback=None,
                    timeout=3,
                )

        self.assertEqual(len(calls), 1)
        self.assertEqual(result["message"], "fresh codex summary")
        self.assertEqual(result["provider"], "codex")
        self.assertEqual(result["error"], "")
        self.assertTrue(result["timed_out"])
        self.assertEqual(result["timeout_seconds"], 3)

    def test_bug_agent_summary_timeout_without_output_uses_omlx_without_provider_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp), workspace_root=Path(tmp))
            config.bug_analysis.provider = "codex"
            config.bug_analysis.command = "codex"
            config.omlx_chat.enabled = True
            config.omlx_chat.model = "local-small"
            runner = BugAnalysisRunner(config)
            output_path = Path(tmp) / "bug_agent_summary.md"
            request_artifact = Path(tmp) / "request.md"
            metadata_path = Path(tmp) / "metadata.md"
            request_artifact.write_text("request summary", encoding="utf-8")
            metadata_path.write_text("metadata summary", encoding="utf-8")

            def timeout_without_output(command, **kwargs):
                raise subprocess.TimeoutExpired(cmd=command, timeout=3)

            with (
                mock.patch("lark_agent_bridge.agents.run_tracked_process", side_effect=timeout_without_output) as run_mock,
                mock.patch.object(
                    OmlxChatClient,
                    "_chat",
                    return_value=TaskResult(
                        success=True,
                        message="omlx lightweight summary",
                        duration_seconds=1.2,
                        details={"mode": "bug_agent_summary_omlx"},
                    ),
                ) as chat_mock,
            ):
                result = runner._run_bug_agent_summary(
                    request_text="根据导航源码分析",
                    request_artifact=request_artifact,
                    metadata_path=metadata_path,
                    output_path=output_path,
                    progress_callback=None,
                    timeout=3,
                )

        self.assertEqual(run_mock.call_count, 1)
        self.assertEqual(chat_mock.call_count, 1)
        self.assertEqual(result["message"], "omlx lightweight summary")
        self.assertEqual(result["provider"], "omlx")
        self.assertEqual(result["command"], ["omlx", "local-small"])

    def test_bug_agent_summary_prefer_lightweight_uses_omlx_before_primary_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp), workspace_root=Path(tmp))
            config.bug_analysis.provider = "codex"
            config.bug_analysis.command = "codex"
            config.omlx_chat.enabled = True
            config.omlx_chat.model = "local-small"
            runner = BugAnalysisRunner(config)
            output_path = Path(tmp) / "bug_agent_summary.md"
            request_artifact = Path(tmp) / "request.md"
            metadata_path = Path(tmp) / "metadata.md"
            request_artifact.write_text("request summary", encoding="utf-8")
            metadata_path.write_text("metadata summary", encoding="utf-8")
            progress_events = []

            with (
                mock.patch("lark_agent_bridge.agents.run_tracked_process") as run_mock,
                mock.patch.object(
                    OmlxChatClient,
                    "_chat",
                    return_value=TaskResult(
                        success=True,
                        message="omlx first summary",
                        duration_seconds=1.1,
                        details={"mode": "bug_agent_summary_omlx"},
                    ),
                ) as chat_mock,
            ):
                result = runner._run_bug_agent_summary(
                    request_text="重新分析",
                    request_artifact=request_artifact,
                    metadata_path=metadata_path,
                    output_path=output_path,
                    progress_callback=progress_events.append,
                    timeout=90,
                    prefer_lightweight=True,
                )

        self.assertEqual(run_mock.call_count, 0)
        self.assertEqual(chat_mock.call_count, 1)
        self.assertEqual(result["message"], "omlx first summary")
        self.assertEqual(result["provider"], "omlx")
        self.assertEqual(progress_events[0]["stage"], "bug_agent_summary_omlx")
        self.assertEqual(progress_events[0]["details"]["reason"], "lightweight_first")

    def test_bug_agent_summary_with_report_paths_skips_omlx_lightweight_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp), workspace_root=Path(tmp))
            config.bug_analysis.provider = "codex"
            config.bug_analysis.command = "codex"
            config.omlx_chat.enabled = True
            runner = BugAnalysisRunner(config)
            output_path = Path(tmp) / "bug_agent_summary.md"
            request_artifact = Path(tmp) / "request.md"
            metadata_path = Path(tmp) / "metadata.md"
            report_json = Path(tmp) / "bug_3d_startup_report.json"
            request_artifact.write_text("request summary", encoding="utf-8")
            report_json.write_text('{"summary":"should be read by file-capable agent"}', encoding="utf-8")
            metadata_path.write_text(f"JSON: `{report_json}`\n", encoding="utf-8")

            def fake_file_agent(command, **kwargs):
                output_path.write_text("file-capable agent summary", encoding="utf-8")
                return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

            with (
                mock.patch("lark_agent_bridge.agents.run_tracked_process", side_effect=fake_file_agent) as run_mock,
                mock.patch.object(OmlxChatClient, "_chat") as chat_mock,
            ):
                result = runner._run_bug_agent_summary(
                    request_text="分析3D生命周期",
                    request_artifact=request_artifact,
                    metadata_path=metadata_path,
                    output_path=output_path,
                    progress_callback=None,
                    timeout=90,
                    prefer_lightweight=True,
                )

        self.assertEqual(chat_mock.call_count, 0)
        self.assertEqual(run_mock.call_count, 1)
        self.assertEqual(result["message"], "file-capable agent summary")
        self.assertEqual(result["provider"], "codex")

    def test_bug_summary_lightweight_policy_keeps_source_and_resume_on_primary_agent(self):
        config = BridgeConfig(dry_run=False)
        runner = BugAnalysisRunner(config)

        self.assertFalse(
            runner._should_prefer_lightweight_bug_summary(
                request_text="分析主题变化",
                followup_text="重新分析",
                provider_session_id="",
            )
        )
        self.assertFalse(
            runner._should_prefer_lightweight_bug_summary(
                request_text="分析主题变化",
                followup_text="基于源码重新分析",
                provider_session_id="",
            )
        )
        self.assertFalse(
            runner._should_prefer_lightweight_bug_summary(
                request_text="分析主题变化",
                followup_text="继续看一下",
                provider_session_id="sess_123",
            )
        )

    def test_agent_summary_timeout_uses_previous_analysis_duration_as_floor(self):
        config = BridgeConfig(dry_run=False)
        config.bug_analysis.agent_summary_timeout_seconds = 90
        runner = BugAnalysisRunner(config)

        timeout = runner._agent_summary_timeout(
            operation_timeout_seconds=5400,
            reference_seconds=300.0,
        )

        self.assertEqual(timeout, 375)

    def test_agent_summary_timeout_reference_reads_previous_session_duration(self):
        runner = BugAnalysisRunner(BridgeConfig(dry_run=True))

        reference = runner._agent_summary_timeout_reference(
            {
                "duration_seconds": 420.0,
                "details": {"agent_summary_duration_seconds": 90.0},
            }
        )

        self.assertEqual(reference, 420.0)

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
            self._write_matching_log(log_root, "2026-05-09 19:00:00")
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
                        prompt="分析信号链路 SIGNAL_VCU_ELECTRICIT_PERCENT",
                        raw_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6986570719 分析信号链路 SIGNAL_VCU_ELECTRICIT_PERCENT",
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

    def test_signal_overview_does_not_count_other_signal_business_hits_as_target_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = BugAnalysisRunner(BridgeConfig(dry_run=True, workspace_root=Path("/Users/zhuyl/Documents/workspace")))
            output_dir = Path(tmp)
            signal_json = output_dir / "bug_signal_chain_report.json"
            signal_html = output_dir / "bug_signal_chain_report.html"
            signal_html.write_text("<html>signal</html>", encoding="utf-8")
            target_log = (
                "/tmp/attachments/20260519/data_Log_log0_app_com.xiaopeng.montecarlo_/"
                "com.xiaopeng.montecarlo/main_2026-05-19_15-00.alog.log"
            )
            signal_payload = {
                "signal": {
                    "code": "16042",
                    "name": "SIGNAL_CTL_POWERCENTER_SYNTHESIS_REMAIN_DIS_CHANGE",
                    "comment": "综合续航",
                },
                "summary": "日志中已命中 Android datacenter 相关阶段或业务消费端日志，可结合源码确认。",
                "log_report": {
                    "stages": {
                        "datacenter": {
                            "title": "Android DataCenter 分发",
                            "hits": 3,
                            "codes": {"16042": 3},
                            "examples": [
                                {
                                    "code": "16042",
                                    "file": target_log,
                                    "line": 34033,
                                    "text": "05-19 15:32:59.831 11311 11659 330578 I NAV_DataCenter: getSignalFlow: signalCode=SIGNAL_CTL_POWERCENTER_SYNTHESIS_REMAIN_DIS_CHANGE, isNeedCache=true, hasProvider=false",
                                }
                            ],
                        },
                        "android_business": {
                            "title": "Android 业务消费日志",
                            "hits": 12,
                            "codes": {"16031": 12},
                            "examples": [
                                {
                                    "code": "16031",
                                    "file": target_log,
                                    "line": 5828,
                                    "text": "05-19 15:28:17.742 2531 3078 48489 I NAV_CarPowerChargeFlagAction: update: chargeFlag=0",
                                }
                            ],
                        },
                    }
                },
                "lifecycle_report": {
                    "runtime": {
                        "events": [
                            {
                                "label": "进程启动",
                                "time": "05-19 15:32:57.000",
                                "file": target_log,
                                "line": 1,
                                "text": "process begin^^^^^^^^^^[11311,3582][2026-05-19 +0800 15:32:57]",
                            }
                        ]
                    }
                },
            }
            signal_json.write_text(json.dumps(signal_payload, ensure_ascii=False), encoding="utf-8")

            artifacts = runner._build_combined_report_artifacts(
                plans=[BugAnalysisPlan(kind="signal", signal_code="16042")],
                prompt_text="调查 SIGNAL_CTL_POWERCENTER_SYNTHESIS_REMAIN_DIS_CHANGE 2026-05-19 15:35:35",
                fault_time="2026-05-19 15:35:35",
                output_dir=output_dir,
                html_paths=[signal_html],
                report_jsons={"signal": signal_json},
                selected_input=Path("/tmp/logs"),
            )

            html = Path(artifacts["html_path"]).read_text(encoding="utf-8")
            payload = json.loads(Path(artifacts["json_path"]).read_text(encoding="utf-8"))
        self.assertEqual(payload["focus_scope"]["pid"], "11311")
        self.assertEqual(payload["focus_scope"]["package"], "com.xiaopeng.montecarlo")
        self.assertIn("目标信号已进入 DataCenter，但业务消费证据不足", payload["summary"])
        self.assertIn("目标信号已进入 DataCenter，但业务消费证据不足", html)
        self.assertNotIn("DataCenter -&gt; 业务消费", html)
        self.assertNotIn("业务消费端日志", html)
        self.assertNotIn("业务消费端日志", json.dumps(payload, ensure_ascii=False))

    def test_signal_overview_reports_android_datacenter_checkpoints(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            repo = workspace / "guideengine"
            helper = repo / "module_floorcenter/module_datacenter/src/main/java/com/xiaopeng/guideengine/helper/carcontrol/CarCtlPowerCenterHelper.kt"
            helper.parent.mkdir(parents=True, exist_ok=True)
            helper.write_text(
                "\n".join(
                    [
                        "class CarCtlPowerCenterHelper {",
                        "  private val carControlMap = mapOf(",
                        "    // SignalCode.SIGNAL_CTL_POWERCENTER_SYNTHESIS_REMAIN_DIS_CHANGE",
                        "    3500003 to SignalCode.SIGNAL_CTL_POWERCENTER_CRUISINGRANGE_COMBINE",
                        "  )",
                        "  private val callbackMap = mapOf(3500003 to this::cruisingRangeCallback)",
                        "  private fun cruisingRangeCallback(eventValue: EventValue) {",
                        "    when (state[EVENT_KEY]) {",
                        "      EVENT_KEY_VALUE_VEHICLE_REMAINING_DISTANCE -> {",
                        "        val synthesisRemainingDis = state[VALUE_KEY_REMAINING_DISTANCE_MILEAGE] as Float?",
                        "        L.i(TAG, \"EVENT_KEY_VALUE_VEHICLE_REMAINING_DISTANCE $synthesisRemainingDis\")",
                        "        onNextData(SignalCode.SIGNAL_CTL_POWERCENTER_SYNTHESIS_REMAIN_DIS_CHANGE, SignalFormat.Float, synthesisRemainingDis)",
                        "      }",
                        "    }",
                        "  }",
                        "  companion object {",
                        "    const val EVENT_KEY_VALUE_VEHICLE_REMAINING_DISTANCE = \"vehicle_remaining_distance\"",
                        "    const val VALUE_KEY_REMAINING_DISTANCE_MILEAGE = \"value_key_remaining_distance_mileage\"",
                        "  }",
                        "}",
                    ]
                ),
                encoding="utf-8",
            )
            consumer = repo / "module_core/manager_carscene/src/main/java/demo/CarSceneExternalDischargeAction.kt"
            consumer.parent.mkdir(parents=True, exist_ok=True)
            consumer.write_text(
                "\n".join(
                    [
                        "class CarSceneExternalDischargeAction {",
                        "  fun getRegisterSignalCode() = setOf(SignalCode.SIGNAL_CTL_POWERCENTER_SYNTHESIS_REMAIN_DIS_CHANGE)",
                        "  fun update(xDataPropertyValue: XDataPropertyValue) {",
                        "    when (xDataPropertyValue.code) {",
                        "      SignalCode.SIGNAL_CTL_POWERCENTER_SYNTHESIS_REMAIN_DIS_CHANGE -> {",
                        "        L.i(TAG, \"update vehicle remainDis:${xDataPropertyValue.value}\")",
                        "      }",
                        "    }",
                        "  }",
                        "}",
                    ]
                ),
                encoding="utf-8",
            )
            runner = BugAnalysisRunner(BridgeConfig(dry_run=True, workspace_root=workspace, guideengine_repo=repo, data_dir=workspace / "data"))
            output_dir = workspace / "out"
            output_dir.mkdir()
            target_log = output_dir / "data_Log_log0_app_com.xiaopeng.montecarlo_" / "com.xiaopeng.montecarlo" / "main.log"
            target_log.parent.mkdir(parents=True)
            target_log.write_text(
                "\n".join(
                    [
                        "05-19 15:32:57.000 11311 11311 1 I process begin^^^^^^^^^^[11311,3582][2026-05-19 +0800 15:32:57]",
                        "05-19 15:32:57.200 11311 11311 2 I NAV_DataCenter: injectSignalProvider[com.xiaopeng.guideengine.helper.carcontrol.CarCtlPowerCenterHelper@1], number[43]",
                        "05-19 15:32:58.000 11311 11389 3 I NAV_CarCtlPowerCenterHelper: register 3500003 indeed isNeedCache:true",
                        "05-19 15:32:58.001 11311 11389 4 I EventManager_montecarlo: registerRemoteListener: event:3500003 service:com.xiaopeng.aicabin.IAiCabinService$Stub$a@1 listener:demo",
                        "05-19 15:32:58.002 11311 11389 5 W NAV_XBaseSignalHelper: disposeFirstRegisterSignalCallback  signalCodeGetMap not contains SIGNAL_CTL_POWERCENTER_CRUISINGRANGE_COMBINE",
                        "05-19 15:32:59.800 11311 11735 6 I NAV_CarSceneExternalDischargeAction: register",
                        "05-19 15:32:59.831 11311 11659 7 I NAV_DataCenter: getSignalFlow: signalCode=SIGNAL_CTL_POWERCENTER_SYNTHESIS_REMAIN_DIS_CHANGE, isNeedCache=true, hasProvider=false",
                    ]
                ),
                encoding="utf-8",
            )
            signal_json = output_dir / "bug_signal_chain_report.json"
            signal_html = output_dir / "bug_signal_chain_report.html"
            signal_html.write_text("<html>signal</html>", encoding="utf-8")
            signal_payload = {
                "signal": {
                    "code": "16042",
                    "name": "SIGNAL_CTL_POWERCENTER_SYNTHESIS_REMAIN_DIS_CHANGE",
                    "comment": "综合续航",
                },
                "summary": "日志中已命中 Android datacenter 相关阶段或业务消费端日志，可结合源码确认。",
                "source_references": [
                    {
                        "file": "module_floorcenter/module_proto/src/main/proto/signal.proto",
                        "line": 243,
                        "text": "SIGNAL_CTL_POWERCENTER_SYNTHESIS_REMAIN_DIS_CHANGE = 16042;// 综合续航",
                    },
                    {
                        "file": "module_core/manager_carscene/src/main/java/demo/CarSceneExternalDischargeAction.kt",
                        "line": 2,
                        "text": "SignalCode.SIGNAL_CTL_POWERCENTER_SYNTHESIS_REMAIN_DIS_CHANGE",
                    },
                ],
                "log_report": {
                    "scanned_files": 1,
                    "scanned_lines": 7,
                    "truncated": False,
                    "stages": {
                        "datacenter": {
                            "hits": 1,
                            "codes": {"16042": 1},
                            "examples": [
                                {
                                    "code": "16042",
                                    "file": str(target_log),
                                    "line": 7,
                                    "text": "05-19 15:32:59.831 11311 11659 7 I NAV_DataCenter: getSignalFlow: signalCode=SIGNAL_CTL_POWERCENTER_SYNTHESIS_REMAIN_DIS_CHANGE, isNeedCache=true, hasProvider=false",
                                }
                            ],
                        },
                        "android_business": {"hits": 0, "codes": {}, "examples": []},
                        "android_unity": {"hits": 0, "codes": {}, "examples": []},
                        "unity_received": {"hits": 0, "codes": {}, "examples": []},
                        "vhal": {"hits": 0, "codes": {}, "examples": []},
                    },
                },
                "lifecycle_report": {
                    "context": {
                        "provider_type": "CarControl",
                        "helper_class": "CarCtlPowerCenterHelper",
                        "helper_file": "module_floorcenter/module_datacenter/src/main/java/com/xiaopeng/guideengine/helper/carcontrol/CarCtlPowerCenterHelper.kt",
                        "controller_class": "SmartCtlController",
                    },
                    "runtime": {
                        "events": [
                            {
                                "label": "进程启动",
                                "time": "05-19 15:32:57.000",
                                "file": str(target_log),
                                "line": 1,
                                "text": "process begin^^^^^^^^^^[11311,3582][2026-05-19 +0800 15:32:57]",
                            },
                            {
                                "label": "DataCenter 注入 CarCtlPowerCenterHelper",
                                "time": "05-19 15:32:57.200",
                                "file": str(target_log),
                                "line": 2,
                                "text": "05-19 15:32:57.200 11311 11311 2 I NAV_DataCenter: injectSignalProvider[com.xiaopeng.guideengine.helper.carcontrol.CarCtlPowerCenterHelper@1], number[43]",
                            },
                        ]
                    },
                },
            }
            signal_json.write_text(json.dumps(signal_payload, ensure_ascii=False), encoding="utf-8")

            artifacts = runner._build_combined_report_artifacts(
                plans=[BugAnalysisPlan(kind="signal", signal_code="16042")],
                prompt_text="调查 SIGNAL_CTL_POWERCENTER_SYNTHESIS_REMAIN_DIS_CHANGE 2026-05-19 15:35:35",
                fault_time="2026-05-19 15:35:35",
                output_dir=output_dir,
                html_paths=[signal_html],
                report_jsons={"signal": signal_json},
                selected_input=output_dir,
            )

            html = Path(artifacts["html_path"]).read_text(encoding="utf-8")
            payload = json.loads(Path(artifacts["json_path"]).read_text(encoding="utf-8"))
            cache_payload = json.loads((workspace / "data" / "signal_keyword_cache.json").read_text(encoding="utf-8"))

        self.assertIn("Android 数据链路排查", html)
        self.assertLess(html.index("<h2>结论摘要</h2>"), html.index("<h2>Android 数据链路排查</h2>"))
        self.assertLess(html.index("<h2>Android 数据链路排查</h2>"), html.index("<h2>关键证据</h2>"))
        self.assertIn("当前最可能卡点", html)
        self.assertNotIn("<h2>最可能卡点</h2>", html)
        self.assertEqual([item["status"] for item in payload["android_data_link"]], ["通过", "通过", "未通过", "通过", "未通过"])
        self.assertIn("3500003", html)
        self.assertIn("未看到 3500003 回调数据进入 CarCtlPowerCenterHelper", html)
        self.assertIn("业务已注册目标信号", html)
        self.assertIn("业务未收到目标信号", html)
        cached_keywords = cache_payload["16042"]
        self.assertEqual(cached_keywords["event_ids"], ["3500003"])
        self.assertIn("vehicle_remaining_distance", cached_keywords["producer_terms"])
        self.assertNotIn("3500200", json.dumps(payload["android_data_link"], ensure_ascii=False))

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
            ["结论摘要", "关键证据", "待确认项", "建议动作", "生命周期流图"],
        )

    def test_bug_analysis_combined_route_uploads_only_merged_html(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp), workspace_root=Path(tmp))
            runner = BugAnalysisRunner(config)
            log_root = Path(tmp) / "logs"
            nav_log = log_root / "Log" / "log1" / "app" / "com.xiaopeng.montecarlo" / "nav.log"
            logd_log = log_root / "Log" / "log1" / "logd" / "main.txt"
            vehicle_log = log_root / "Log" / "log1" / "app" / "vehicle" / "vehicle.log"
            self._write_matching_log(log_root, "2026-05-11 23:10:00")
            for path in (nav_log, logd_log, vehicle_log):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(path.name, encoding="utf-8")
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
                json_path.write_text(json.dumps({"evidence": [{"file": str(nav_log), "line": 7}]}), encoding="utf-8")
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
            evidence_bundle = Path(result.details["evidence_log_bundle"])
            evidence_manifest = Path(result.details["evidence_log_manifest"])
            evidence_bundle_exists = evidence_bundle.exists()
            evidence_manifest_exists = evidence_manifest.exists()
            evidence_nav_exists = (evidence_bundle / "Log" / "log1" / "app" / "com.xiaopeng.montecarlo" / "nav.log").exists()
            evidence_logd_exists = (evidence_bundle / "Log" / "log1" / "logd" / "main.txt").exists()
            evidence_vehicle_exists = (evidence_bundle / "Log" / "log1" / "app" / "vehicle" / "vehicle.log").exists()
            metadata_body = (Path(result.job_dir) / "output" / "bug_metadata.md").read_text(encoding="utf-8")

        self.assertTrue(result.success)
        self.assertEqual(result.details["analysis_kinds"], ["startup", "stuck"])
        self.assertEqual(result.details["files_to_send"], [combined_html])
        self.assertTrue(evidence_bundle_exists, str(evidence_bundle))
        self.assertTrue(evidence_manifest_exists, str(evidence_manifest))
        self.assertTrue(evidence_nav_exists)
        self.assertTrue(evidence_logd_exists)
        self.assertTrue(evidence_vehicle_exists)
        self.assertIn("证据日志保留包", metadata_body)

    def test_direct_analysis_preserves_focused_evidence_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp), workspace_root=Path(tmp))
            runner = BugAnalysisRunner(config)
            log_root = Path(tmp) / "direct_logs"
            nav_log = log_root / "Log" / "log2" / "app" / "com.xiaopeng.montecarlo" / "nav.log"
            logd_log = log_root / "Log" / "log2" / "logd" / "main.txt"
            vehicle_log = log_root / "Log" / "log2" / "app" / "vehicle" / "vehicle.log"
            other_log = log_root / "Log" / "log1" / "app" / "com.xiaopeng.montecarlo" / "nav.log"
            self._write_matching_log(log_root, "2026-05-19 00:00:00")
            for path in (nav_log, logd_log, vehicle_log, other_log):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(path.name, encoding="utf-8")

            resource = DownloadResource(kind="file", value="log.zip")

            class FakeDownloader:
                def download_all(self, resources, *, context, message_id):
                    return [DownloadedResource(resource=resource, path=log_root)]

            runner._direct_downloader = FakeDownloader()

            def fake_run_analysis(*, plan, input_path, html_path, json_path, analysis_dir, timeout, target_time, request_text=None):
                html_path.write_text("<html>stuck</html>", encoding="utf-8")
                json_path.write_text(json.dumps({"evidence": [{"file": str(nav_log), "line": 17}]}), encoding="utf-8")
                return subprocess.CompletedProcess(args=["python3"], returncode=0, stdout="", stderr="")

            with (
                mock.patch.object(runner, "_prepare_log_input", return_value=log_root),
                mock.patch.object(runner, "_run_analysis", side_effect=fake_run_analysis),
                mock.patch.object(runner, "_build_combined_report_artifacts", return_value=None),
            ):
                result = runner.run_direct_analysis(
                    DirectAnalysisRequest(
                        prompt="问题时间: 2026-05-19 00:00 分析卡顿",
                        resources=[resource],
                        raw_text="问题时间: 2026-05-19 00:00 分析卡顿",
                        triggered=True,
                    )
            )
            evidence_bundle = Path(result.details["evidence_log_bundle"])
            evidence_manifest = Path(result.details["evidence_log_manifest"])
            evidence_bundle_exists = evidence_bundle.exists()
            evidence_manifest_exists = evidence_manifest.exists()
            copied_nav = (evidence_bundle / "Log" / "log2" / "app" / "com.xiaopeng.montecarlo" / "nav.log").exists()
            copied_logd = (evidence_bundle / "Log" / "log2" / "logd" / "main.txt").exists()
            copied_vehicle = (evidence_bundle / "Log" / "log2" / "app" / "vehicle" / "vehicle.log").exists()
            copied_other = (evidence_bundle / "Log" / "log1" / "app" / "com.xiaopeng.montecarlo" / "nav.log").exists()
            metadata_body = (Path(result.job_dir) / "output" / "direct_analysis_metadata.md").read_text(encoding="utf-8")

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "direct_analysis")
        self.assertTrue(evidence_bundle_exists, str(evidence_bundle))
        self.assertTrue(evidence_manifest_exists, str(evidence_manifest))
        self.assertTrue(copied_nav)
        self.assertTrue(copied_logd)
        self.assertTrue(copied_vehicle)
        self.assertFalse(copied_other)
        self.assertEqual(result.details["evidence_log_focus_logs"], ["log2"])
        self.assertIn("证据日志保留包", metadata_body)

    def test_direct_analysis_missing_problem_time_asks_before_run_analysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp), workspace_root=Path(tmp))
            runner = BugAnalysisRunner(config)
            log_root = Path(tmp) / "direct_logs"
            self._write_matching_log(log_root, "2026-05-19 00:00:00")
            resource = DownloadResource(kind="file", value="log.zip")

            class FakeDownloader:
                def download_all(self, resources, *, context, message_id):
                    return [DownloadedResource(resource=resource, path=log_root)]

            runner._direct_downloader = FakeDownloader()

            with (
                mock.patch.object(runner, "_prepare_log_input", return_value=log_root),
                mock.patch.object(
                    runner,
                    "_run_analysis",
                    side_effect=AssertionError("missing problem time should not run analysis"),
                ),
            ):
                result = runner.run_direct_analysis(
                    DirectAnalysisRequest(
                        prompt="分析卡顿",
                        resources=[resource],
                        raw_text="分析卡顿",
                        triggered=True,
                    )
                )

        self.assertTrue(result.success)
        self.assertTrue(result.skipped)
        self.assertEqual(result.details["mode"], "bug_time_clarification")
        self.assertEqual(result.details["time_gate_status"], "missing_fault_time")

    def test_direct_analysis_cn_month_day_time_uses_full_datetime_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp), workspace_root=Path(tmp))
            runner = BugAnalysisRunner(config)
            log_root = Path(tmp) / "direct_logs"
            self._write_matching_log(log_root, "2026-05-22 07:46:00")
            resource = DownloadResource(kind="file", value="log.zip")

            class FakeDownloader:
                def download_all(self, resources, *, context, message_id):
                    return [DownloadedResource(resource=resource, path=log_root)]

            runner._direct_downloader = FakeDownloader()

            def fake_run_analysis(
                *,
                plan,
                input_path,
                html_path,
                json_path,
                analysis_dir,
                timeout,
                target_time,
                request_text=None,
                bridge_session_id=None,
            ):
                html_path.write_text("<html>ok</html>", encoding="utf-8")
                json_path.write_text(json.dumps({"target_time": target_time}), encoding="utf-8")
                return subprocess.CompletedProcess(args=["python3"], returncode=0, stdout="", stderr="")

            def fake_custom_skill_agent_analysis(**kwargs):
                kwargs["html_path"].write_text("<html>source ok</html>", encoding="utf-8")
                kwargs["json_path"].write_text('{"source":"ok"}', encoding="utf-8")
                return {
                    "ok": True,
                    "message": "source ok",
                    "command": ["claude"],
                    "stdout": "source ok",
                    "stderr": "",
                    "analysis_markdown_path": kwargs["analysis_dir"] / "source_stage_analysis.md",
                }

            prompt = "基于SRViolationHandler.kt源码分析 时间点5月22日 7:46 分析超速状态"
            event = LarkEvent(
                event_id="evt_1",
                message_id="om_1",
                chat_id="oc_1",
                chat_type="group",
                sender_id="ou_1",
                message_type="text",
                content=prompt,
                create_time="1779536637134",
                timestamp="1779536637482",
            )

            with (
                mock.patch.object(runner, "_prepare_log_input", return_value=log_root),
                mock.patch.object(runner, "classify_requests", return_value=[BugAnalysisPlan(kind="perception")]),
                mock.patch.object(runner, "_write_reanalysis_source_evidence", return_value=None),
                mock.patch.object(runner, "_run_analysis", side_effect=fake_run_analysis),
                mock.patch.object(runner, "_run_custom_skill_agent_analysis", side_effect=fake_custom_skill_agent_analysis),
                mock.patch.object(runner, "_build_combined_report_artifacts", return_value=None),
            ):
                result = runner.run_direct_analysis(
                    DirectAnalysisRequest(
                        prompt=prompt,
                        resources=[resource],
                        raw_text=prompt,
                        triggered=True,
                    ),
                    event=event,
                )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "direct_analysis")
        self.assertEqual(result.details["fault_time"], "2026-05-22 07:46")

    def test_direct_analysis_respects_plans_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp), workspace_root=Path(tmp))
            runner = BugAnalysisRunner(config)
            log_root = Path(tmp) / "direct_logs"
            self._write_matching_log(log_root, "2026-05-22 07:46:00")
            resource = DownloadResource(kind="file", value="log.zip")
            executed_plans = []

            class FakeDownloader:
                def download_all(self, resources, *, context, message_id):
                    return [DownloadedResource(resource=resource, path=log_root)]

            runner._direct_downloader = FakeDownloader()

            def fake_run_analysis(
                *,
                plan,
                input_path,
                html_path,
                json_path,
                analysis_dir,
                timeout,
                target_time,
                request_text=None,
                bridge_session_id=None,
            ):
                executed_plans.append(plan.kind)
                html_path.write_text("<html>ok</html>", encoding="utf-8")
                json_path.write_text("{}", encoding="utf-8")
                return subprocess.CompletedProcess(args=["python3"], returncode=0, stdout="", stderr="")

            with (
                mock.patch.object(runner, "_prepare_log_input", return_value=log_root),
                mock.patch.object(runner, "classify_requests", side_effect=AssertionError("classify_requests should not run when plans_override is provided")),
                mock.patch.object(runner, "_write_reanalysis_source_evidence", return_value=None),
                mock.patch.object(runner, "_run_analysis", side_effect=fake_run_analysis),
                mock.patch.object(runner, "_build_combined_report_artifacts", return_value=None),
            ):
                result = runner.run_direct_analysis(
                    DirectAnalysisRequest(
                        prompt="时间点2026-05-22 07:46 调查3D生命周期",
                        resources=[resource],
                        raw_text="时间点2026-05-22 07:46 调查3D生命周期",
                        triggered=True,
                    ),
                    plans_override=[BugAnalysisPlan(kind="startup")],
                )

        self.assertTrue(result.success)
        self.assertEqual(executed_plans, ["startup"])

    def test_direct_analysis_custom_skill_executor_not_ready_skips_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp), workspace_root=Path(tmp))
            runner = BugAnalysisRunner(config)
            log_root = Path(tmp) / "direct_logs"
            self._write_matching_log(log_root, "2026-05-22 07:46:00")
            resource = DownloadResource(kind="file", value="log.zip")

            class FakeDownloader:
                def download_all(self, resources, *, context, message_id):
                    return [DownloadedResource(resource=resource, path=log_root)]

            runner._direct_downloader = FakeDownloader()

            with (
                mock.patch.object(runner, "_prepare_log_input", return_value=log_root),
                mock.patch.object(runner, "_write_reanalysis_source_evidence", return_value=None),
                mock.patch.object(runner, "_build_combined_report_artifacts", return_value=None),
                mock.patch.object(
                    runner,
                    "_run_bug_agent_summary",
                    return_value={
                        "message": "agent summary",
                        "provider": "codex",
                        "model": "gpt-5.4",
                        "session_id": "sess_1",
                        "resumed": False,
                        "duration_seconds": 1.2,
                        "usage": {},
                        "usage_scope": "",
                    },
                ) as summary_mock,
            ):
                result = runner.run_direct_analysis(
                    DirectAnalysisRequest(
                        prompt="基于SRViolationHandler.kt源码分析 时间点2026-05-22 07:46 分析超速状态",
                        resources=[resource],
                        raw_text="基于SRViolationHandler.kt源码分析 时间点2026-05-22 07:46 分析超速状态",
                        triggered=True,
                    ),
                    plans_override=[BugAnalysisPlan(kind="custom_skill")],
                    classification_skill="source-analysis-skill",
                    classification_source="preflight_rules",
                    classification_reason="explicit source clue",
                )

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "direct_custom_skill_executor_not_ready")
        self.assertIn("已命中专用 Skill", result.message)
        self.assertIn("当前没有可执行源码分析器", result.message)
        self.assertIn("source-analysis-skill", result.message)
        self.assertEqual(result.details["analysis_kind"], "custom_skill")
        self.assertEqual(result.details["analysis_skill"], "source-analysis-skill")
        self.assertEqual(result.details["custom_skill_analysis_status"], "executor_not_ready")
        summary_mock.assert_not_called()

    def test_direct_analysis_source_stage_ignores_builtin_skill_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp), workspace_root=Path(tmp))
            config.bug_analysis.provider = "codex"
            config.bug_analysis.command = "codex"
            runner = BugAnalysisRunner(config)
            log_root = Path(tmp) / "direct_logs"
            self._write_matching_log(log_root, "2026-05-25 16:50:41")
            resource = DownloadResource(kind="file", value="log.zip")

            class FakeDownloader:
                def download_all(self, resources, *, context, message_id):
                    return [DownloadedResource(resource=resource, path=log_root)]

            runner._direct_downloader = FakeDownloader()
            seen_skill_names: list[str] = []

            def fake_file_agent(**kwargs):
                seen_skill_names.append(kwargs["skill_name"])
                return {
                    "ok": False,
                    "error_code": "custom_skill_agent_failed",
                    "message": "stop after capturing skill",
                    "command": ["codex"],
                    "provider": "codex",
                    "stdout": "",
                    "stderr": "",
                    "stdout_path": Path(tmp) / "stdout.txt",
                    "stderr_path": Path(tmp) / "stderr.txt",
                    "command_path": Path(tmp) / "command.txt",
                    "analysis_markdown_path": Path(tmp) / "analysis.md",
                }

            with (
                mock.patch.object(runner, "_prepare_log_input", return_value=log_root),
                mock.patch.object(runner, "_write_reanalysis_source_evidence", return_value=None),
                mock.patch.object(runner, "_build_combined_report_artifacts", return_value=None),
                mock.patch.object(runner, "_run_custom_skill_agent_analysis", side_effect=fake_file_agent),
            ):
                result = runner.run_direct_analysis(
                    DirectAnalysisRequest(
                        prompt="2026-05-25 16:50:41 分析 3D场景模式",
                        resources=[resource],
                        raw_text="2026-05-25 16:50:41 分析 3D场景模式",
                        triggered=True,
                    ),
                    plans_override=[BugAnalysisPlan(kind="source_stage")],
                    classification_skill="scene-signal-diagnosis",
                    classification_source="agent",
                    classification_reason="污染的 built-in skill 名称",
                )

        self.assertFalse(result.success)
        self.assertEqual(seen_skill_names, ["source_analysis"])

    def test_direct_analysis_custom_skill_file_agent_runs_before_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            skill_name = "agent-ready-direct-skill"
            skill_dir = root / ".ai" / "skills" / skill_name
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: Agent Ready Direct Skill\ndescription: 直传日志专用分析。\n---\n\n# Direct\n",
                encoding="utf-8",
            )
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp) / "data", workspace_root=root)
            config.bug_analysis.provider = "codex"
            config.bug_analysis.command = "codex"
            runner = BugAnalysisRunner(config)
            runner.skill_manager.set_skill_route(skill_name, role="primary", executor="file_agent")
            log_root = Path(tmp) / "direct_logs"
            self._write_matching_log(log_root, "2026-05-22 07:46:00")
            resource = DownloadResource(kind="file", value="log.zip")

            class FakeDownloader:
                def download_all(self, resources, *, context, message_id):
                    return [DownloadedResource(resource=resource, path=log_root)]

            def fake_file_agent(command, **kwargs):
                output_path = Path(command[command.index("--output-last-message") + 1])
                output_path.write_text(
                    "## 结论摘要\n直传日志已完成专用分析。\n\n"
                    "## 关键证据\n"
                    "- `time_anchor.log`: 2026-05-22 07:46:00 覆盖故障时间。\n\n"
                    "## 待确认项\n- 无。\n\n"
                    "## 建议动作\n- 继续复核业务状态。\n",
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

            runner._direct_downloader = FakeDownloader()

            with (
                mock.patch.object(runner, "_prepare_log_input", return_value=log_root),
                mock.patch.object(runner, "_write_reanalysis_source_evidence", return_value=None),
                mock.patch.object(runner, "_build_combined_report_artifacts", return_value=None),
                mock.patch("lark_agent_bridge.agents.run_tracked_process", side_effect=fake_file_agent) as run_mock,
                mock.patch.object(
                    runner,
                    "_run_bug_agent_summary",
                    return_value={
                        "message": "direct agent final",
                        "provider": "codex",
                        "model": "gpt-5.4",
                        "session_id": "sess_direct",
                        "resumed": False,
                        "duration_seconds": 1.2,
                        "usage": {},
                        "usage_scope": "",
                    },
                ) as summary_mock,
            ):
                result = runner.run_direct_analysis(
                    DirectAnalysisRequest(
                        prompt="时间点2026-05-22 07:46 分析直传日志",
                        resources=[resource],
                        raw_text="时间点2026-05-22 07:46 分析直传日志",
                        triggered=True,
                    ),
                    plans_override=[BugAnalysisPlan(kind="custom_skill")],
                    classification_skill=skill_name,
                    classification_source="user_selected_card",
                    classification_reason="用户选择 ready custom skill",
                )
                analysis_file_exists = Path(result.details.get("custom_skill_analysis_file", "")).exists()

        self.assertTrue(result.success)
        self.assertEqual(result.message, "direct agent final")
        self.assertEqual(result.details["analysis_kind"], "custom_skill")
        self.assertEqual(result.details["custom_skill_analysis_status"], "completed")
        self.assertEqual(result.details["custom_skill_evidence_count"], 1)
        self.assertTrue(analysis_file_exists)
        run_mock.assert_called_once()
        summary_mock.assert_called_once()

    def test_bug_reanalysis_custom_skill_executor_not_ready_skips_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp), workspace_root=Path(tmp))
            runner = BugAnalysisRunner(config)
            job_dir = Path(tmp) / "jobs" / "job_custom"
            output_dir = job_dir / "output"
            output_dir.mkdir(parents=True, exist_ok=True)
            prepared_input = Path(tmp) / "logs"
            self._write_matching_log(prepared_input, "2026-05-22 19:46:00")
            previous_summary = output_dir / "bug_agent_summary.md"
            previous_summary.write_text("old summary", encoding="utf-8")
            previous_session = {
                "job_id": "job_custom",
                "job_dir": str(job_dir),
                "details": {
                    "analysis_kinds": ["custom_skill"],
                    "analysis_skill": "source_analysis",
                    "prepared_log_input": str(prepared_input),
                    "selected_log_input": str(prepared_input),
                    "target_time": "2026-05-22 19:46",
                    "user_request_text": (
                        "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6998107767 "
                        "基于 SRViolationHandler.kt 源码重新分析"
                    ),
                    "agent_summary_file": str(previous_summary),
                },
            }
            previous_context = mock.Mock(
                request_text=previous_session["details"]["user_request_text"],
                summary_text="上一轮摘要",
                report_excerpt="上一轮报告摘录",
                history=[],
            )

            with (
                mock.patch.object(runner, "_write_reanalysis_source_evidence", return_value=None),
                mock.patch.object(runner, "_build_combined_report_artifacts", return_value=None),
                mock.patch.object(
                    runner,
                    "_run_bug_agent_summary",
                    return_value={
                        "message": "fake reanalysis conclusion",
                        "command": ["codex", "exec"],
                        "error": "",
                        "provider": "codex",
                        "session_id": "sess_custom",
                        "resumed": False,
                    },
                ) as summary_mock,
            ):
                result = runner.run_bug_reanalysis(
                    followup_text="重新分析，确认 SRViolationHandler.kt 的源码链路",
                    previous_context=previous_context,
                    previous_session=previous_session,
                    plans_override=[BugAnalysisPlan(kind="custom_skill")],
                    classification_skill="source-analysis-skill",
                    classification_source="agent",
                    classification_reason="explicit source clue",
                )

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "custom_skill_reanalysis_executor_not_ready")
        self.assertIn("已命中专用 Skill", result.message)
        self.assertIn("当前没有可执行源码分析器", result.message)
        self.assertEqual(result.details["analysis_kind"], "custom_skill")
        self.assertEqual(result.details["analysis_skill"], "source-analysis-skill")
        self.assertEqual(result.details["custom_skill_analysis_status"], "executor_not_ready")
        summary_mock.assert_not_called()

    def test_bug_reanalysis_source_stage_ignores_builtin_skill_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp), workspace_root=Path(tmp))
            config.bug_analysis.provider = "codex"
            config.bug_analysis.command = "codex"
            runner = BugAnalysisRunner(config)
            job_dir = Path(tmp) / "jobs" / "job_scene_signal_source_guard"
            output_dir = job_dir / "output"
            output_dir.mkdir(parents=True, exist_ok=True)
            prepared_input = Path(tmp) / "logs"
            self._write_matching_log(prepared_input, "2026-05-25 16:50:41")
            previous_session = {
                "job_id": "job_scene_signal_source_guard",
                "job_dir": str(job_dir),
                "details": {
                    "analysis_kind": "scene_signal",
                    "analysis_kinds": ["scene_signal", "source_stage"],
                    "analysis_skill": "scene-signal-diagnosis",
                    "prepared_log_input": str(prepared_input),
                    "selected_log_input": str(prepared_input),
                    "target_time": "2026-05-25 16:50",
                    "user_request_text": "2026-05-25 16:50:41 场景模式异常",
                },
            }
            previous_context = mock.Mock(
                request_text=previous_session["details"]["user_request_text"],
                summary_text="上一轮摘要",
                report_excerpt="上一轮报告摘录",
                history=[],
            )
            seen_skill_names: list[str] = []

            def fake_file_agent(**kwargs):
                seen_skill_names.append(kwargs["skill_name"])
                return {
                    "ok": False,
                    "error_code": "custom_skill_agent_failed",
                    "message": "stop after capturing skill",
                    "command": ["codex"],
                    "provider": "codex",
                    "stdout": "",
                    "stderr": "",
                    "stdout_path": Path(tmp) / "stdout.txt",
                    "stderr_path": Path(tmp) / "stderr.txt",
                    "command_path": Path(tmp) / "command.txt",
                    "analysis_markdown_path": Path(tmp) / "analysis.md",
                }

            with (
                mock.patch.object(runner, "_write_reanalysis_source_evidence", return_value=None),
                mock.patch.object(runner, "_build_combined_report_artifacts", return_value=None),
                mock.patch.object(runner, "_run_custom_skill_agent_analysis", side_effect=fake_file_agent),
            ):
                result = runner.run_bug_reanalysis(
                    followup_text="基于源码重新分析 3D场景模式",
                    previous_context=previous_context,
                    previous_session=previous_session,
                    plans_override=[BugAnalysisPlan(kind="source_stage")],
                    classification_skill="scene-signal-diagnosis",
                    classification_source="agent",
                    classification_reason="污染的 built-in skill 名称",
                )

        self.assertFalse(result.success)
        self.assertEqual(seen_skill_names, ["source_analysis"])

    def test_bug_reanalysis_ld_lane_level_builtin_runs_before_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            skill_name = "ld-lane-level-log-analysis-portable"
            skill_dir = root / ".ai" / "skills" / skill_name
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: LD Lane Level Log Analysis\ndescription: 续聊专用 LD 分析。\n---\n\n# LD Lane\n",
                encoding="utf-8",
            )
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp) / "data", workspace_root=root)
            config.bug_analysis.provider = "codex"
            config.bug_analysis.command = "codex"
            runner = BugAnalysisRunner(config)
            job_dir = Path(tmp) / "jobs" / "job_ld_ready"
            output_dir = job_dir / "output"
            output_dir.mkdir(parents=True, exist_ok=True)
            prepared_input = Path(tmp) / "logs"
            self._write_matching_log(prepared_input, "2026-05-22 19:46:00")
            previous_summary = output_dir / "bug_agent_summary.md"
            previous_summary.write_text("old summary", encoding="utf-8")
            previous_session = {
                "job_id": "job_ld_ready",
                "job_dir": str(job_dir),
                "details": {
                    "analysis_kinds": ["ld_lane_level"],
                    "analysis_skill": skill_name,
                    "prepared_log_input": str(prepared_input),
                    "selected_log_input": str(prepared_input),
                    "target_time": "2026-05-22 19:46",
                    "user_request_text": "2026-05-22 19:46 退无图",
                    "agent_summary_file": str(previous_summary),
                },
            }
            previous_context = mock.Mock(
                request_text=previous_session["details"]["user_request_text"],
                summary_text="上一轮摘要",
                report_excerpt="上一轮报告摘录",
                history=[],
            )

            def fake_file_agent(command, **kwargs):
                output_path = Path(command[command.index("--output-last-message") + 1])
                output_path.write_text(
                    "## 结论摘要\n续聊已重新读取 LD 日志。\n\n"
                    "## 关键证据\n"
                    "- `time_anchor.log`: 2026-05-22 19:46:00 覆盖追问时间。\n\n"
                    "## 待确认项\n- 无。\n\n"
                    "## 建议动作\n- 输出给用户前再做最终总结。\n",
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

            with (
                mock.patch.object(runner, "_write_reanalysis_source_evidence", return_value=None),
                mock.patch.object(runner, "_build_combined_report_artifacts", return_value=None),
                mock.patch("lark_agent_bridge.agents.run_tracked_process", side_effect=fake_file_agent) as run_mock,
                mock.patch.object(
                    runner,
                    "_run_bug_agent_summary",
                    return_value={
                        "message": "reanalysis ld final",
                        "command": ["codex", "exec"],
                        "error": "",
                        "provider": "codex",
                        "session_id": "sess_ld",
                        "resumed": False,
                    },
                ) as summary_mock,
            ):
                result = runner.run_bug_reanalysis(
                    followup_text="重新分析，确认 LD 退无图根因",
                    previous_context=previous_context,
                    previous_session=previous_session,
                    plans_override=[BugAnalysisPlan(kind="ld_lane_level")],
                    classification_skill=skill_name,
                    classification_source="agent",
                    classification_reason="ready ld lane level skill",
                )
                analysis_file_exists = Path(result.details.get("ld_lane_level_analysis_file", "")).exists()

        self.assertTrue(result.success)
        self.assertEqual(result.message, "reanalysis ld final")
        self.assertEqual(result.details["analysis_kind"], "ld_lane_level")
        self.assertEqual(result.details["analysis_skill"], skill_name)
        self.assertEqual(result.details["ld_lane_level_analysis_status"], "completed")
        self.assertEqual(result.details["ld_lane_level_evidence_count"], 1)
        self.assertTrue(analysis_file_exists)
        run_mock.assert_called_once()
        summary_mock.assert_called_once()

    def test_bug_reanalysis_generic_retry_prefers_existing_non_source_report_over_sticky_source_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            root.mkdir(parents=True)
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp) / "data", workspace_root=root)
            config.bug_analysis.provider = "codex"
            config.bug_analysis.command = "codex"
            runner = BugAnalysisRunner(config)
            job_dir = Path(tmp) / "jobs" / "job_retry_guard"
            output_dir = job_dir / "output"
            output_dir.mkdir(parents=True, exist_ok=True)
            prepared_input = Path(tmp) / "logs"
            self._write_matching_log(prepared_input, "2026-05-22 19:46:00")
            (output_dir / "bug_ld_lane_level_report.html").write_text("<html>old ld</html>", encoding="utf-8")
            (output_dir / "bug_ld_lane_level_report.json").write_text("{}", encoding="utf-8")
            previous_session = {
                "job_id": "job_retry_guard",
                "job_dir": str(job_dir),
                "details": {
                    "analysis_kind": "source_stage",
                    "analysis_kinds": ["source_stage"],
                    "analysis_skill": "source_analysis",
                    "classification_source": "deterministic_fallback",
                    "prepared_log_input": str(prepared_input),
                    "selected_log_input": str(prepared_input),
                    "target_time": "2026-05-22 19:46",
                    "user_request_text": "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6998107767 调查车道级进不去",
                },
            }
            previous_context = mock.Mock(
                request_text=previous_session["details"]["user_request_text"],
                summary_text="上一轮失败摘要",
                report_excerpt="上一轮失败摘录",
                history=[],
            )
            chosen_plans: list[str] = []

            def fake_run_analysis(*, plan, input_path, html_path, json_path, analysis_dir, timeout, target_time, request_text=None):
                chosen_plans.append(plan.kind)
                html_path.write_text("<html>rerun</html>", encoding="utf-8")
                json_path.write_text("{}", encoding="utf-8")
                return subprocess.CompletedProcess(args=["codex"], returncode=0, stdout="", stderr="")

            with (
                mock.patch.object(runner, "_write_reanalysis_source_evidence", return_value=None),
                mock.patch.object(runner, "_run_analysis", side_effect=fake_run_analysis),
                mock.patch.object(runner, "_run_bug_agent_summary", return_value={"message": "", "command": None, "error": "", "provider": "", "model": "", "session_id": "", "resumed": False, "duration_seconds": 0.0, "usage": {}, "usage_scope": ""}),
            ):
                result = runner.run_bug_reanalysis(
                    followup_text="重新分析",
                    previous_context=previous_context,
                    previous_session=previous_session,
                )

        self.assertTrue(result.success)
        self.assertEqual(chosen_plans, [])
        self.assertEqual(result.details["analysis_kind"], "ld_lane_level")

    def test_bug_reanalysis_ld_lane_level_ignores_sticky_source_analysis_skill_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            skill_name = "ld-lane-level-log-analysis-portable"
            skill_dir = root / ".ai" / "skills" / skill_name
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: LD Lane Level Log Analysis\ndescription: 续聊专用 LD 分析。\n---\n\n# LD Lane\n",
                encoding="utf-8",
            )
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp) / "data", workspace_root=root)
            config.bug_analysis.provider = "codex"
            config.bug_analysis.command = "codex"
            runner = BugAnalysisRunner(config)
            job_dir = Path(tmp) / "jobs" / "job_ld_skill_guard"
            output_dir = job_dir / "output"
            output_dir.mkdir(parents=True, exist_ok=True)
            prepared_input = Path(tmp) / "logs"
            self._write_matching_log(prepared_input, "2026-05-22 19:46:00")
            previous_session = {
                "job_id": "job_ld_skill_guard",
                "job_dir": str(job_dir),
                "details": {
                    "analysis_kind": "ld_lane_level",
                    "analysis_kinds": ["ld_lane_level"],
                    "analysis_skill": skill_name,
                    "prepared_log_input": str(prepared_input),
                    "selected_log_input": str(prepared_input),
                    "target_time": "2026-05-22 19:46",
                    "user_request_text": "2026-05-22 19:46 退无图",
                },
            }
            previous_context = mock.Mock(
                request_text=previous_session["details"]["user_request_text"],
                summary_text="上一轮摘要",
                report_excerpt="上一轮报告摘录",
                history=[],
            )
            seen_skill_names: list[str] = []

            def fake_file_agent(**kwargs):
                seen_skill_names.append(kwargs["skill_name"])
                return {
                    "ok": False,
                    "error_code": "custom_skill_agent_failed",
                    "message": "stop after capturing skill",
                    "command": ["codex"],
                    "provider": "codex",
                    "stdout": "",
                    "stderr": "",
                    "stdout_path": Path(tmp) / "stdout.txt",
                    "stderr_path": Path(tmp) / "stderr.txt",
                    "command_path": Path(tmp) / "command.txt",
                    "analysis_markdown_path": Path(tmp) / "analysis.md",
                }

            with mock.patch.object(runner, "_run_custom_skill_agent_analysis", side_effect=fake_file_agent):
                result = runner.run_bug_reanalysis(
                    followup_text="重新分析车道级进不去问题",
                    previous_context=previous_context,
                    previous_session=previous_session,
                    plans_override=[BugAnalysisPlan(kind="ld_lane_level")],
                    classification_skill="source_analysis",
                    classification_source="deterministic_fallback",
                    classification_reason="污染的源码 skill 名称",
                )

        self.assertFalse(result.success)
        self.assertEqual(seen_skill_names, ["ld-lane-level-log-analysis-portable"])

    def test_bug_reanalysis_custom_skill_file_agent_runs_before_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            skill_name = "agent-ready-reanalysis-skill"
            skill_dir = root / ".ai" / "skills" / skill_name
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: Agent Ready Reanalysis Skill\ndescription: 续聊专用分析。\n---\n\n# Reanalysis\n",
                encoding="utf-8",
            )
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp) / "data", workspace_root=root)
            config.bug_analysis.provider = "codex"
            config.bug_analysis.command = "codex"
            runner = BugAnalysisRunner(config)
            runner.skill_manager.set_skill_route(skill_name, role="primary", executor="file_agent")
            job_dir = Path(tmp) / "jobs" / "job_custom_ready"
            output_dir = job_dir / "output"
            output_dir.mkdir(parents=True, exist_ok=True)
            prepared_input = Path(tmp) / "logs"
            self._write_matching_log(prepared_input, "2026-05-22 19:46:00")
            previous_summary = output_dir / "bug_agent_summary.md"
            previous_summary.write_text("old summary", encoding="utf-8")
            previous_session = {
                "job_id": "job_custom_ready",
                "job_dir": str(job_dir),
                "details": {
                    "analysis_kinds": ["custom_skill"],
                    "analysis_skill": skill_name,
                    "prepared_log_input": str(prepared_input),
                    "selected_log_input": str(prepared_input),
                    "target_time": "2026-05-22 19:46",
                    "user_request_text": "2026-05-22 19:46 退无图",
                    "agent_summary_file": str(previous_summary),
                },
            }
            previous_context = mock.Mock(
                request_text=previous_session["details"]["user_request_text"],
                summary_text="上一轮摘要",
                report_excerpt="上一轮报告摘录",
                history=[],
            )

            def fake_file_agent(command, **kwargs):
                output_path = Path(command[command.index("--output-last-message") + 1])
                output_path.write_text(
                    "## 结论摘要\n续聊已重新读取日志。\n\n"
                    "## 关键证据\n"
                    "- `time_anchor.log`: 2026-05-22 19:46:00 覆盖追问时间。\n\n"
                    "## 待确认项\n- 无。\n\n"
                    "## 建议动作\n- 输出给用户前再做最终总结。\n",
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

            with (
                mock.patch.object(runner, "_write_reanalysis_source_evidence", return_value=None),
                mock.patch.object(runner, "_build_combined_report_artifacts", return_value=None),
                mock.patch("lark_agent_bridge.agents.run_tracked_process", side_effect=fake_file_agent) as run_mock,
                mock.patch.object(
                    runner,
                    "_run_bug_agent_summary",
                    return_value={
                        "message": "reanalysis agent final",
                        "command": ["codex", "exec"],
                        "error": "",
                        "provider": "codex",
                        "session_id": "sess_custom",
                        "resumed": False,
                    },
                ) as summary_mock,
            ):
                result = runner.run_bug_reanalysis(
                    followup_text="重新分析，确认 LD 退无图根因",
                    previous_context=previous_context,
                    previous_session=previous_session,
                    plans_override=[BugAnalysisPlan(kind="custom_skill")],
                    classification_skill=skill_name,
                    classification_source="agent",
                    classification_reason="ready custom skill",
                )
                analysis_file_exists = Path(result.details.get("custom_skill_analysis_file", "")).exists()

        self.assertTrue(result.success)
        self.assertEqual(result.message, "reanalysis agent final")
        self.assertEqual(result.details["analysis_kind"], "custom_skill")
        self.assertEqual(result.details["custom_skill_analysis_status"], "completed")
        self.assertEqual(result.details["custom_skill_evidence_count"], 1)
        self.assertTrue(analysis_file_exists)
        run_mock.assert_called_once()
        summary_mock.assert_called_once()

    def test_source_evidence_terms_keep_explicit_source_file_and_class(self):
        runner = BugAnalysisRunner(BridgeConfig(dry_run=False))

        terms = runner._source_evidence_terms(
            plans=[BugAnalysisPlan(kind="custom_skill")],
            request_text="基于SRViolationHandler.kt源码分析 时间点2026-05-22 07:46 分析超速状态",
            followup_text="",
        )

        self.assertIn("SRViolationHandler.kt", terms)
        self.assertIn("SRViolationHandler", terms)

    def test_write_reanalysis_source_evidence_prioritizes_explicit_target_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            target = repo / "module_core" / "subreality_biz" / "src" / "main" / "java" / "com" / "xiaopeng" / "ainavi" / "subreality_biz" / "violation" / "processor" / "SRViolationHandler.kt"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                "package demo\n\nclass SRViolationHandler {\n    companion object {\n        const val MSG_NOTIFY_SPEED = 1\n    }\n}\n",
                encoding="utf-8",
            )
            distractor = repo / "foo" / "ProtocolFile.java"
            distractor.parent.mkdir(parents=True, exist_ok=True)
            distractor.write_text("public class ProtocolFile { void printStackTrace() {} }\n", encoding="utf-8")
            runner = BugAnalysisRunner(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp) / "data",
                    workspace_root=repo,
                    guideengine_repo=repo,
                )
            )
            output_dir = Path(tmp) / "out"
            output_dir.mkdir(parents=True, exist_ok=True)

            evidence_path = runner._write_reanalysis_source_evidence(
                plans=[BugAnalysisPlan(kind="custom_skill")],
                request_text="基于SRViolationHandler.kt源码分析 时间点2026-05-22 07:46 分析超速状态",
                followup_text="",
                output_dir=output_dir,
                enabled=True,
            )

            assert evidence_path is not None
            body = evidence_path.read_text(encoding="utf-8")

        self.assertIn("SRViolationHandler.kt", body)
        self.assertIn("class SRViolationHandler", body)

    def test_bug_reanalysis_uses_agent_summary_and_persisted_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp), workspace_root=Path(tmp))
            runner = BugAnalysisRunner(config)
            job_dir = Path(tmp) / "jobs" / "job_1"
            output_dir = job_dir / "output"
            output_dir.mkdir(parents=True, exist_ok=True)
            prepared_input = Path(tmp) / "logs"
            prepared_input.mkdir(parents=True, exist_ok=True)
            self._write_matching_log(prepared_input, "2026-05-11 23:12:00")
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
            long_assistant_history = "上一轮超长分析结论" + "A" * 6000

            def fake_run_analysis(*, plan, input_path, html_path, json_path, analysis_dir, timeout, target_time, request_text=None):
                analysis_inputs.append((plan.kind, input_path, target_time))
                html_path.write_text(f"<html>{plan.kind}</html>", encoding="utf-8")
                json_path.write_text("{}", encoding="utf-8")
                return subprocess.CompletedProcess(args=["python3"], returncode=0, stdout="", stderr="")

            previous_session = {
                "job_id": "job_1",
                "job_dir": str(job_dir),
                "duration_seconds": 400.0,
                "details": {
                    "analysis_kinds": ["startup", "stuck"],
                    "prepared_log_input": str(prepared_input),
                    "selected_log_input": str(prepared_input / "selected.alog"),
                    "user_request_text": "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 问题时间 2026-05-11 23:10 分析启动和卡顿",
                    "agent_summary_file": str(previous_summary),
                    "agent_summary_session_id": "sess_123",
                },
            }
            previous_context = mock.Mock(
                request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 问题时间 2026-05-11 23:10 分析启动和卡顿",
                summary_text="上一轮摘要",
                report_excerpt="上一轮摘录",
                history=[{"role": "user", "content": "第一次分析"}, {"role": "assistant", "content": long_assistant_history}],
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
                request_text = (output_dir / "bug_agent_reanalysis_request.md").read_text(encoding="utf-8")
                prompt_text = summary_mock.call_args.kwargs["request_artifact"].read_text(encoding="utf-8")

        self.assertTrue(result.success)
        self.assertEqual(result.message, "agent continued")
        self.assertEqual(result.details["agent_summary_session_id"], "fresh_sess")
        self.assertNotIn("agent_summary_resumed", result.details)
        self.assertEqual(result.details["rerun_analysis_kinds"], ["startup"])
        self.assertEqual(result.details["reused_analysis_kinds"], ["stuck"])
        self.assertEqual(analysis_inputs[0][0], "startup")
        self.assertEqual(analysis_inputs[0][1], prepared_input)
        self.assertEqual(analysis_inputs[0][2], "2026-05-11 23:12")
        self.assertEqual(summary_mock.call_args.kwargs["provider_session_id"], "")
        self.assertEqual(summary_mock.call_args.kwargs["previous_summary_path"], previous_summary)
        self.assertEqual(summary_mock.call_args.kwargs["timeout"], 500)
        self.assertNotIn(long_assistant_history, request_text)
        self.assertIn("上一轮长回答已省略", request_text)
        self.assertNotIn(long_assistant_history, prompt_text)

    def test_bug_reanalysis_recovers_original_request_time_and_cached_bug_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            config = BridgeConfig(dry_run=False, data_dir=data_dir, workspace_root=Path(tmp), guideengine_repo=Path(tmp) / "guideengine")
            runner = BugAnalysisRunner(config)
            job_dir = data_dir / "jobs" / "job_1"
            output_dir = job_dir / "output"
            output_dir.mkdir(parents=True, exist_ok=True)
            bug_url = "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6995163459?tab_key=comment"
            request_text = (
                f"{bug_url} 调查SIGNAL_CTL_POWERCENTER_SYNTHESIS_REMAIN_DIS_CHANGE "
                "信号链路 2026-05-19 15:35:35"
            )
            bug_cache = data_dir / "bug_cache" / "xpfailuremgmt_6995163459"
            log_file = (
                bug_cache
                / "attachments"
                / "20260519"
                / "2026-05-19-15-38-51"
                / "data_Log_log0_app_com.xiaopeng.montecarlo_"
                / "com.xiaopeng.montecarlo"
                / "main_2026-05-19_15-00.alog.log"
            )
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_file.write_text(
                "05-19 15:35:35.000 11311 12000 1 I NAV_DataCenter: "
                "getSignalFlow: signalCode=SIGNAL_CTL_POWERCENTER_SYNTHESIS_REMAIN_DIS_CHANGE, "
                "isNeedCache=true, hasProvider=false\n",
                encoding="utf-8",
            )
            (bug_cache / "cache.json").write_text(
                json.dumps(
                    {
                        "bug_url": bug_url,
                        "project_key": "xpfailuremgmt",
                        "work_item_id": "6995163459",
                        "selected_log_input": "",
                        "prepared_log_input": "",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            analysis_inputs = []

            def fake_run_analysis(*, plan, input_path, html_path, json_path, analysis_dir, timeout, target_time, request_text=None):
                analysis_inputs.append((plan.kind, input_path, target_time))
                html_path.write_text("<html>signal</html>", encoding="utf-8")
                json_path.write_text(
                    json.dumps(
                        {
                            "signal": {
                                "code": "16042",
                                "name": "SIGNAL_CTL_POWERCENTER_SYNTHESIS_REMAIN_DIS_CHANGE",
                                "comment": "综合续航",
                            },
                            "summary": "正确 bug 日志显示 16042 订阅时 hasProvider=false。",
                            "log_report": {
                                "stages": {
                                    "datacenter": {
                                        "title": "Android DataCenter 分发",
                                        "hits": 1,
                                        "examples": [
                                            {
                                                "code": "16042",
                                                "file": str(log_file),
                                                "line": 1,
                                                "text": log_file.read_text(encoding="utf-8").strip(),
                                            }
                                        ],
                                    }
                                }
                            },
                            "lifecycle_report": {
                                "runtime": {
                                    "events": [
                                        {
                                            "label": "进程启动",
                                            "time": "05-19 15:32:57.000",
                                            "file": str(log_file),
                                            "line": 1,
                                            "text": "process begin^^^^^^^^^^[11311,3582][2026-05-19 +0800 15:32:57]",
                                        }
                                    ]
                                }
                            },
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(args=["python3"], returncode=0, stdout="", stderr="")

            previous_session = {
                "job_id": "job_1",
                "job_dir": str(job_dir),
                "details": {
                    "bug_url": bug_url,
                    "analysis_kinds": ["signal"],
                    "signal_code": "SIGNAL_CTL_POWERCENTER_SYNTHESIS_REMAIN_DIS_CHANGE",
                    "prepared_log_input": "",
                    "selected_log_input": "",
                    "user_request_text": request_text,
                },
            }
            previous_context = mock.Mock(request_text=request_text, summary_text="", report_excerpt="", history=[])

            with (
                mock.patch.object(runner, "_retry_bug_log_download", side_effect=AssertionError("should reuse cached bug logs")),
                mock.patch.object(runner, "_run_analysis", side_effect=fake_run_analysis),
                mock.patch.object(runner, "_write_reanalysis_source_evidence", return_value=None),
                mock.patch.object(
                    runner,
                    "_run_bug_agent_summary",
                    return_value={
                        "message": "agent final summary",
                        "command": None,
                        "error": "",
                        "provider": "codex",
                        "session_id": "sess_current",
                        "resumed": False,
                        "duration_seconds": 1.0,
                        "usage": {},
                    },
                ),
            ):
                result = runner.run_bug_reanalysis(
                    followup_text="重新分析下",
                    previous_context=previous_context,
                    previous_session=previous_session,
                    force_rerun=True,
                )
                combined_html = Path(result.details["combined_report_html"]).read_text(encoding="utf-8")

            self.assertTrue(result.success)
            self.assertEqual(result.details["target_time"], "2026-05-19 15:35:35")
            self.assertEqual(Path(result.details["prepared_log_input"]).resolve(), (bug_cache / "attachments").resolve())
            self.assertEqual(analysis_inputs, [("signal", (bug_cache / "attachments").resolve(), None)])
            self.assertIn("2026-05-19 15:35:35", combined_html)
            self.assertNotIn("agent final summary", combined_html)
            self.assertEqual(result.message, "agent final summary")

    def test_bug_reanalysis_signal_followup_overrides_target_and_collects_source_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp), workspace_root=Path(tmp), guideengine_repo=Path(tmp) / "guideengine")
            runner = BugAnalysisRunner(config)
            job_dir = Path(tmp) / "jobs" / "job_1"
            output_dir = job_dir / "output"
            output_dir.mkdir(parents=True, exist_ok=True)
            prepared_input = Path(tmp) / "logs"
            prepared_input.mkdir(parents=True, exist_ok=True)
            self._write_matching_log(prepared_input, "2026-05-16 10:01:00")
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
                "duration_seconds": 420.0,
                "details": {
                    "analysis_kinds": ["signal"],
                    "signal_code": "235082",
                    "prepared_log_input": str(prepared_input),
                    "selected_log_input": str(prepared_input),
                    "user_request_text": (
                        "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6986570719 "
                        "问题时间 2026-05-16 10:01 分析信号链路 主要是VCU_ELECTRICIT_PERCENT，请参考源码分析"
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
        self.assertEqual(result.details["rerun_analysis_kinds"], ["signal", "source_stage"])
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
            self._write_matching_log(prepared_input, "2026-05-16 10:01:00")
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
                "duration_seconds": 420.0,
                "details": {
                    "analysis_kinds": ["signal"],
                    "signal_code": "123456",
                    "prepared_log_input": str(prepared_input),
                    "selected_log_input": str(prepared_input),
                    "user_request_text": "问题时间 2026-05-16 10:01 分析信号链路 主要是CAMPING_MODE_STATUS",
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

    def test_write_reanalysis_source_evidence_searches_all_repo_roots(self):
        """Source evidence searches all repos in source_investigation.repo_roots (incl. Napa5)."""
        with tempfile.TemporaryDirectory() as tmp:
            # Two repos: guideengine and napa5-like
            repo1 = Path(tmp) / "guideengine"
            repo2 = Path(tmp) / "Napa5"
            (repo1 / "module_core").mkdir(parents=True)
            (repo2 / "module_napa").mkdir(parents=True)

            kt1 = repo1 / "module_core" / "SceneManager.kt"
            kt1.write_text("class SceneManager { fun enter3DScene() {} }", encoding="utf-8")
            kt2 = repo2 / "module_napa" / "SceneManager.kt"
            kt2.write_text("class SceneManager { fun render3DScene() {} }", encoding="utf-8")

            config = BridgeConfig(
                dry_run=False,
                data_dir=Path(tmp),
                workspace_root=Path(tmp),
                guideengine_repo=repo1,
                source_investigation=SourceInvestigationOptions(
                    repo_roots=[repo1, repo2],
                    codegraph_enabled=False,
                ),
            )
            runner = BugAnalysisRunner(config)
            output_dir = Path(tmp) / "output"
            output_dir.mkdir()

            result_path = runner._write_reanalysis_source_evidence(
                plans=[BugAnalysisPlan(kind="general")],
                request_text="SceneManager 3D场景",
                followup_text="基于源码 重新分析",
                output_dir=output_dir,
                enabled=True,
            )

            self.assertIsNotNone(result_path)
            content = result_path.read_text(encoding="utf-8")
            # Should contain results from BOTH repos
            self.assertIn("guideengine", content)
            self.assertIn("Napa5", content)
            self.assertIn("SceneManager", content)

    def test_source_evidence_future_wait_is_capped_before_analysis_loop(self):
        class SlowEvidenceFuture:
            def __init__(self):
                self.timeout = None

            def result(self, timeout=None):
                self.timeout = timeout
                raise TimeoutError("slow source evidence")

        with tempfile.TemporaryDirectory() as tmp:
            runner = BugAnalysisRunner(BridgeConfig(dry_run=False, data_dir=Path(tmp), workspace_root=Path(tmp)))
            future = SlowEvidenceFuture()

            resolved = runner._resolve_source_evidence_future(future)

        self.assertIsNone(resolved)
        self.assertEqual(future.timeout, 30)

    def test_source_evidence_skips_python_full_scan_when_rg_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "guideengine"
            (repo / "module_core").mkdir(parents=True)
            (repo / "module_core" / "ThemeManager.kt").write_text(
                "class ThemeManager { fun applyTheme() {} }",
                encoding="utf-8",
            )
            runner = BugAnalysisRunner(BridgeConfig(dry_run=False, data_dir=Path(tmp), workspace_root=Path(tmp)))

            with (
                mock.patch("shutil.which", return_value=None),
                mock.patch.object(runner, "_collect_source_evidence_by_scan", side_effect=AssertionError("scan used")),
            ):
                matches = runner._collect_source_evidence(repo=repo, terms=["ThemeManager"])

        self.assertEqual(matches, [])

    def test_source_evidence_codegraph_uses_capped_cli_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "guideengine"
            repo.mkdir()
            config = BridgeConfig(
                dry_run=False,
                data_dir=Path(tmp),
                workspace_root=Path(tmp),
                guideengine_repo=repo,
                source_investigation=SourceInvestigationOptions(
                    repo_roots=[repo],
                    codegraph_enabled=True,
                    codegraph_timeout_seconds=10,
                ),
            )
            runner = BugAnalysisRunner(config)

            with mock.patch("lark_agent_bridge.knowledge.codegraph_client.CodeGraphClient") as client_cls:
                client = client_cls.return_value
                client.is_available.return_value = True
                client.is_indexed.return_value = True
                client.search_symbol.return_value = []
                client.get_callers.return_value = []

                result = runner._collect_source_evidence_with_codegraph(repo=repo, terms=["ThemeManager"])

        self.assertIsNone(result)
        self.assertEqual(client_cls.call_args.kwargs["timeout"], 5.0)
        self.assertLessEqual(client.search_symbol.call_args.kwargs["timeout"], 5.0)

    def test_write_reanalysis_source_evidence_uses_codegraph_when_indexed(self):
        """When codegraph is available and indexed, it's used instead of ripgrep."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "guideengine"
            repo.mkdir()
            (repo / ".codegraph").mkdir()  # mark as indexed

            config = BridgeConfig(
                dry_run=False,
                data_dir=Path(tmp),
                workspace_root=Path(tmp),
                guideengine_repo=repo,
                source_investigation=SourceInvestigationOptions(
                    repo_roots=[repo],
                    codegraph_enabled=True,
                    codegraph_min_confidence=0.5,
                ),
            )
            runner = BugAnalysisRunner(config)
            output_dir = Path(tmp) / "output"
            output_dir.mkdir()

            from lark_agent_bridge.knowledge.codegraph_client import CgCallerHit, CgSymbolHit

            cg_hits = [
                CgSymbolHit(
                    name="SceneManager", kind="class",
                    qualified_name="SceneManager", path="module_core/SceneManager.kt",
                    line=10, language="Kotlin", score=120.0,
                ),
            ]
            cg_callers = [
                CgCallerHit(
                    name="initScene", kind="method",
                    path="module_display/Launcher.kt", line=55,
                ),
            ]

            with (
                mock.patch("shutil.which", return_value="/usr/local/bin/codegraph"),
                mock.patch(
                    "lark_agent_bridge.knowledge.codegraph_client.CodeGraphClient.search_symbol",
                    return_value=cg_hits,
                ),
                mock.patch(
                    "lark_agent_bridge.knowledge.codegraph_client.CodeGraphClient.get_callers",
                    return_value=cg_callers,
                ),
            ):
                result_path = runner._write_reanalysis_source_evidence(
                    plans=[BugAnalysisPlan(kind="general")],
                    request_text="SceneManager 3D场景问题",
                    followup_text="基于源码 重新分析",
                    output_dir=output_dir,
                    enabled=True,
                )

            self.assertIsNotNone(result_path)
            content = result_path.read_text(encoding="utf-8")
            self.assertIn("SceneManager", content)
            # Codegraph result has [class] / [caller] markers
            self.assertIn("[class]", content)
            self.assertIn("[caller", content)

    def test_write_reanalysis_source_evidence_falls_back_to_rg_when_codegraph_not_indexed(self):
        """When codegraph is not yet indexed, falls back to ripgrep."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "guideengine"
            (repo / "module_core").mkdir(parents=True)
            kt = repo / "module_core" / "ThemeManager.kt"
            kt.write_text("class ThemeManager { fun applyTheme() {} }", encoding="utf-8")
            # No .codegraph/ dir → not indexed

            config = BridgeConfig(
                dry_run=False,
                data_dir=Path(tmp),
                workspace_root=Path(tmp),
                guideengine_repo=repo,
                source_investigation=SourceInvestigationOptions(
                    repo_roots=[repo],
                    codegraph_enabled=True,
                ),
            )
            runner = BugAnalysisRunner(config)
            output_dir = Path(tmp) / "output"
            output_dir.mkdir()

            with mock.patch("shutil.which", return_value="/usr/local/bin/codegraph"):
                result_path = runner._write_reanalysis_source_evidence(
                    plans=[BugAnalysisPlan(kind="general")],
                    request_text="ThemeManager 主题切换",
                    followup_text="基于源码 重新分析",
                    output_dir=output_dir,
                    enabled=True,
                )

            self.assertIsNotNone(result_path)
            content = result_path.read_text(encoding="utf-8")
            # Should have found ThemeManager via ripgrep fallback
            self.assertIn("ThemeManager", content)
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
                "duration_seconds": 420.0,
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
        self.assertEqual(summary_mock.call_args.kwargs["timeout"], 525)

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
