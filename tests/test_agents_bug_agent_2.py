from _agents_base import *  # noqa: F401,F403
from _agents_base import _AgentTestBase


class AgentsBugAgent2Tests(_AgentTestBase):
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
                mock.patch.object(
                    runner,
                    "_run_bug_agent_summary",
                    return_value={
                        "message": "agent summary",
                        "command": [],
                        "error": "",
                        "provider": "test",
                        "session_id": "",
                        "resumed": False,
                        "duration_seconds": 0.0,
                        "usage": {},
                    },
                ),
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
    def test_direct_analysis_passes_parsed_fault_time_to_log_intelligence(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp), workspace_root=Path(tmp))
            runner = BugAnalysisRunner(config)
            log_root = Path(tmp) / "direct_logs"
            self._write_matching_log(log_root, "2026-05-29 17:16:00")
            resource = DownloadResource(kind="file", value="log.zip")
            seen_problem_times: list[datetime | None] = []

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
                json_path.write_text("{}", encoding="utf-8")
                return subprocess.CompletedProcess(args=["python3"], returncode=0, stdout="", stderr="")

            def fake_analyze_logs_intelligently(*, log_dir, problem_time, plan):
                seen_problem_times.append(problem_time)
                return None

            with (
                mock.patch.object(runner, "_prepare_log_input", return_value=log_root),
                mock.patch.object(runner, "classify_requests", return_value=[BugAnalysisPlan(kind="startup")]),
                mock.patch.object(runner, "_write_reanalysis_source_evidence", return_value=None),
                mock.patch.object(runner, "_analyze_logs_intelligently", side_effect=fake_analyze_logs_intelligently),
                mock.patch.object(runner, "_run_analysis", side_effect=fake_run_analysis),
                mock.patch.object(runner, "_build_combined_report_artifacts", return_value=None),
            ):
                result = runner.run_direct_analysis(
                    DirectAnalysisRequest(
                        prompt="问题时间 2026-05-29 17:16，分析启动",
                        resources=[resource],
                        raw_text="问题时间 2026-05-29 17:16，分析启动",
                        triggered=True,
                    )
                )

        self.assertTrue(result.success)
        self.assertEqual(len(seen_problem_times), 1)
        self.assertIsInstance(seen_problem_times[0], datetime)
        self.assertEqual(seen_problem_times[0].strftime("%Y-%m-%d %H:%M"), "2026-05-29 17:16")
