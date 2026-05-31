from _agents_base import *  # noqa: F401,F403
from _agents_base import _AgentTestBase


class AgentsIntentAnalysisTests(_AgentTestBase):
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
                    "lark_agent_bridge.agents.bug.archive_extract._run_tracked_process",
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
                "lark_agent_bridge.agents.bug.archive_extract._run_tracked_process",
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
            self.assertIn(Path(commands[0][0]).name, {"python3.11", "python3"})
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
                "lark_agent_bridge.agents.bug.archive_extract._run_tracked_process",
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

    def test_log_decoder_prefers_python311_over_generic_system_python(self):
        runner = BugAnalysisRunner(BridgeConfig(dry_run=False))

        def fake_which(name):
            return {
                "python3.11": "/opt/example/bin/python3.11",
                "python3": "/usr/bin/python3",
            }.get(name)

        with mock.patch("lark_agent_bridge.agents.bug.archive_extract.shutil.which", side_effect=fake_which):
            decoder_python = runner._decoder_python()

        self.assertEqual(decoder_python, "/opt/example/bin/python3.11")
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
            self.assertNotIn("### 上一轮 Agent 总结", prompt)
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
        self.assertNotIn("### 上一轮 Agent 总结", prompt)
        self.assertNotIn("HTML_ONLY_MISLEADING_TEXT", prompt)
    def test_bug_direct_api_prompt_compacts_context_only_for_startup_unity_lifecycle_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=True, data_dir=Path(tmp), workspace_root=Path(tmp))
            runner = BugAnalysisRunner(config)
            output_dir = Path(tmp) / "jobs" / "job_1" / "output"
            output_dir.mkdir(parents=True, exist_ok=True)
            skill_dir = Path(tmp) / ".ai" / "skills" / "unity-startup-lifecycle-check"
            skill_dir.mkdir(parents=True, exist_ok=True)
            skill_path = skill_dir / "SKILL.md"
            ref_dir = skill_dir / "references"
            ref_dir.mkdir(parents=True, exist_ok=True)
            ref_path = ref_dir / "NODES.md"
            request_artifact = output_dir / "bug_agent_request.md"
            metadata_path = output_dir / "bug_metadata.md"
            evidence_md = output_dir / "bug_summary_evidence.md"
            evidence_json = output_dir / "bug_summary_evidence.json"
            report_json = output_dir / "bug_3d_startup_report.json"
            request_artifact.write_text("request body", encoding="utf-8")
            skill_path.write_text(
                "---\n"
                "name: unity-startup-lifecycle-check\n"
                "description: demo skill summary description\n"
                "---\n\n"
                "# Demo Skill\n\n"
                "## 目标\n\n"
                "- 保留 focus session\n"
                "- 不要把未命中关键节点改写成日志截止\n\n"
                "## 工作边界\n\n"
                "- 只能围绕同一次启动会话分析\n"
                "- SKILL_NOISE should not survive in full\n\n"
                + ("SKILL_NOISE " * 300),
                encoding="utf-8",
            )
            ref_path.write_text(
                "# 节点定义\n\n"
                "## Activity\n\n"
                "| 节点 | 代码落点 |\n"
                "|---|---|\n"
                "| NodeA | CodeA |\n"
                "| NodeB | CodeB |\n"
                "| REF_NOISE | SHOULD_NOT_SURVIVE |\n"
                + ("REF_NOISE " * 200),
                encoding="utf-8",
            )
            evidence_md.write_text(
                "# Bug Summary Evidence\n\n"
                "## Missing critical nodes\n\n"
                "- createUnityPlayerOnMainThread\n"
                "- UnityReady\n\n"
                "## Last matched lifecycle event\n\n"
                "- 2026-05-19 13:48:48.207\n"
                "- AnalyseNode current version\n\n"
                "## Same PID trailing logs\n\n"
                "- TRAILING_NOISE_1\n"
                "- TRAILING_NOISE_2\n"
                + ("TRAILING_NOISE " * 200),
                encoding="utf-8",
            )
            evidence_json.write_text(
                json.dumps(
                    {
                        "analysis_kind": "startup",
                        "focus_status": "partial",
                        "focus_session_index": 2,
                        "focus_session_pid": 10058,
                        "missing_critical": ["createUnityPlayerOnMainThread", "UnityReady"],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            report_json.write_text(
                json.dumps(
                    {
                        "target_time": "2026-05-19T13:47:01",
                        "focus_reason": "问题时间命中 focus session",
                        "focus_session_index": 2,
                        "focus_session_pid": 10058,
                        "runtime_context": {
                            "resource_type": "outer",
                            "proto_type": "1",
                            "car_type": "D03S",
                        },
                        "internal_exception_chain": {
                            "summary": "AddressModel 依赖异常 -> BaseCamera prefab 初始化失败",
                            "hits": [
                                {
                                    "label": "AddressModel 依赖异常",
                                    "evidence": "RemoteProviderException: Invalid path in AssetBundleProvider",
                                }
                            ],
                        },
                        "verdict": {
                            "message": "启动链路异常，需要围绕 focus session 总结。",
                            "issues": [
                                {"title": "目标时间主会话", "detail": "锁定 Session 2 / PID 10058"},
                                {"title": "异常链", "detail": "BaseCamera prefab 加载失败"},
                            ],
                        },
                        "warnings": ["REPORT_NOISE should not survive in full" * 50],
                        "sessions": [
                            {
                                "index": 2,
                                "status": "partial",
                                "diagnosis": "启动链路未闭环",
                                "missing_critical": ["createUnityPlayerOnMainThread", "UnityReady"],
                                "events": [
                                    {
                                        "title": "AnalyseNode current version",
                                        "timestamp_text": "2026-05-19 13:48:48.207",
                                        "file_path": "/tmp/main.alog.log",
                                        "line_no": 59044,
                                    }
                                ],
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            metadata_path.write_text(
                "# Bug Metadata\n"
                "- 分析类型:\n"
                "  - `3D启动时序分析` -> `bug_3d_startup_report.html`\n"
                "- 命中 Skill: `unity-startup-lifecycle-check`\n"
                f"- Skill 规范:\n  - `{skill_path}`\n  - `{ref_path}`\n"
                f"- 结构化证据 Markdown: `{evidence_md}`\n"
                f"- 结构化证据 JSON: `{evidence_json}`\n"
                f"- JSON `3D启动时序分析`: `{report_json}`\n",
                encoding="utf-8",
            )

            prompt = runner._build_bug_agent_summary_prompt_for_api(
                request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6994901014 调查3D启动生命周期",
                request_artifact=request_artifact,
                metadata_path=metadata_path,
            )

        self.assertIn("demo skill summary description", prompt)
        self.assertIn("## 工作边界", prompt)
        self.assertNotIn("SKILLNOISE SKILLNOISE", prompt)
        self.assertIn("NodeA: CodeA", prompt)
        self.assertNotIn("REF_NOISE: SHOULDNOTSURVIVE", prompt)
        self.assertIn("## Missing critical nodes", prompt)
        self.assertNotIn("TRAILING_NOISE_1", prompt)
        self.assertIn("focus_status: partial", prompt)
        self.assertIn("resource_type=outer", prompt)
        self.assertIn("Invalid path in AssetBundleProvider", prompt)
        self.assertIn("BaseCamera prefab 加载失败", prompt)
        self.assertNotIn("REPORT_NOISE", prompt)
    def test_bug_direct_api_prompt_falls_back_to_raw_excerpt_for_other_skills(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=True, data_dir=Path(tmp), workspace_root=Path(tmp))
            runner = BugAnalysisRunner(config)
            output_dir = Path(tmp) / "jobs" / "job_1" / "output"
            output_dir.mkdir(parents=True, exist_ok=True)
            skill_dir = Path(tmp) / ".ai" / "skills" / "demo-startup-skill"
            skill_dir.mkdir(parents=True, exist_ok=True)
            skill_path = skill_dir / "SKILL.md"
            request_artifact = output_dir / "bug_agent_request.md"
            metadata_path = output_dir / "bug_metadata.md"
            report_json = output_dir / "bug_3d_startup_report.json"
            request_artifact.write_text("request body", encoding="utf-8")
            skill_path.write_text(
                "---\n"
                "name: demo-startup-skill\n"
                "description: demo skill summary description\n"
                "---\n\n"
                "# Demo Skill\n\n"
                "## 工作边界\n\n"
                "- SKILL_NOISE should survive when profile is off\n\n"
                + ("SKILL_NOISE " * 80),
                encoding="utf-8",
            )
            report_json.write_text(
                json.dumps(
                    {
                        "target_time": "2026-05-19T13:47:01",
                        "warnings": ["REPORT_NOISE should survive when profile is off" * 20],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            metadata_path.write_text(
                "# Bug Metadata\n"
                "- 分析类型: `startup`\n"
                "- 命中 Skill: `demo-startup-skill`\n"
                f"- Skill 规范:\n  - `{skill_path}`\n"
                f"- JSON `3D启动时序分析`: `{report_json}`\n",
                encoding="utf-8",
            )

            prompt = runner._build_bug_agent_summary_prompt_for_api(
                request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6994901014 调查3D启动生命周期",
                request_artifact=request_artifact,
                metadata_path=metadata_path,
            )

        self.assertIn("SKILL_NOISE", prompt)
        self.assertIn("REPORT_NOISE", prompt)
    def test_bug_direct_api_summary_writes_prompt_audit_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            ai_provider = AIProviderOptions(
                enabled=True,
                base_url="https://example.invalid/v1",
                primary_model="demo-model",
            )
            config = BridgeConfig(
                dry_run=False,
                data_dir=Path(tmp),
                workspace_root=Path(tmp),
                ai_provider=ai_provider,
            )
            runner = BugAnalysisRunner(config)
            output_dir = Path(tmp) / "jobs" / "job_1" / "output"
            output_dir.mkdir(parents=True, exist_ok=True)
            request_artifact = output_dir / "bug_agent_request.md"
            metadata_path = output_dir / "bug_metadata.md"
            output_path = output_dir / "bug_agent_summary_compact.md"
            request_artifact.write_text("request body", encoding="utf-8")
            metadata_path.write_text("- 分析类型: `startup`\n", encoding="utf-8")

            with mock.patch("lark_agent_bridge.agents.llm_client.LLMClient") as client_cls:
                client = client_cls.return_value
                client.is_available.return_value = True
                client.generate_summary.return_value = LLMResponse(
                    content="summary via direct api",
                    model="demo-model",
                    usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                )

                result = runner._run_bug_agent_summary_via_api(
                    request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6994901014 调查3D启动生命周期",
                    request_artifact=request_artifact,
                    metadata_path=metadata_path,
                    output_path=output_path,
                    progress_callback=None,
                )

                prompt_file = Path(result["prompt_file"])
                context_file = Path(result["context_file"])
                prompt_text = prompt_file.read_text(encoding="utf-8")
                context = json.loads(context_file.read_text(encoding="utf-8"))
                self.assertTrue(prompt_file.exists())
                self.assertTrue(context_file.exists())

            self.assertEqual(result["message"], "summary via direct api")
            self.assertIn("request body", prompt_text)
            self.assertEqual(context["provider"], "direct_api")
            self.assertEqual(context["embedded_files"][0]["title"], "Bug Agent Request")
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
    def test_bug_summary_direct_api_ready_does_not_try_pydantic_first(self):
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
            api_success = {
                "message": "direct api summary",
                "command": None,
                "error": "",
                "provider": "direct_api",
                "session_id": "",
                "resumed": False,
                "duration_seconds": 1.2,
                "usage": {},
                "usage_scope": "direct_api",
            }

            with (
                mock.patch.object(runner, "_run_bug_summary_pydantic_ai", return_value={"message": "pydantic"}) as pai_mock,
                mock.patch.object(runner, "_run_bug_agent_summary_via_api", return_value=api_success) as api_mock,
            ):
                result = runner._run_bug_agent_summary(
                    request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/7001730415 分析3D启动生命周期",
                    request_artifact=request_artifact,
                    metadata_path=metadata_path,
                    output_path=output_path,
                    progress_callback=None,
                    timeout=30,
                )

        self.assertEqual(pai_mock.call_count, 0)
        self.assertEqual(api_mock.call_count, 1)
        self.assertEqual(result["message"], "direct api summary")
        self.assertEqual(result["execution_backend"], "direct_api")
