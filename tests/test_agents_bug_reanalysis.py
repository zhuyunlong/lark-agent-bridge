from _agents_base import *  # noqa: F401,F403
from _agents_base import _AgentTestBase


class AgentsBugReanalysisTests(_AgentTestBase):
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
    def test_bug_followup_generic_source_reanalysis_does_not_leak_old_title_into_xtheme(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp), workspace_root=Path(tmp))
            runner = BugAnalysisRunner(config)
            previous_session = {
                "details": {
                    "analysis_kind": "scene_signal",
                    "analysis_kinds": ["scene_signal", "source_stage"],
                    "analysis_skill": "scene-signal-diagnosis",
                    "user_request_text": "问题时间 2025-05-10 14:30:00 源码分析 3D场景信号分析 https://project.feishu.cn/xpfailuremgmt/buglo/detail/6998811703",
                }
            }
            previous_context = mock.Mock(
                request_text=previous_session["details"]["user_request_text"],
                summary_text="上一轮摘要",
                report_excerpt="上一轮报告摘录",
                history=[],
            )

            selection = runner.decide_bug_followup(
                followup_text="基于源码重新分析，检查信号处理相关的代码逻辑",
                previous_context=previous_context,
                previous_session=previous_session,
            )

        self.assertIsNotNone(selection)
        assert selection is not None
        self.assertTrue(selection.should_reanalyze)
        self.assertEqual([plan.kind for plan in selection.plans], ["scene_signal"])
        self.assertEqual(selection.skill_name, "scene-signal-diagnosis")
    def test_bug_reanalysis_explicit_source_followup_rebuilds_domain_context_from_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(
                dry_run=False,
                data_dir=Path(tmp),
                workspace_root=Path(tmp),
                codex_app_server=CodexAppServerOptions(enabled=True, use_for_file_agent=True),
            )
            config.bug_analysis.provider = "codex"
            config.bug_analysis.command = "codex"
            runner = BugAnalysisRunner(config)
            job_dir = Path(tmp) / "jobs" / "job_scene_signal_source_context_rebuild"
            output_dir = job_dir / "output"
            output_dir.mkdir(parents=True, exist_ok=True)
            prepared_input = Path(tmp) / "logs"
            self._write_matching_log(prepared_input, "2026-05-25 16:50:41")
            previous_session = {
                "job_id": "job_scene_signal_source_context_rebuild",
                "job_dir": str(job_dir),
                "details": {
                    "analysis_kind": "xtheme",
                    "analysis_kinds": ["xtheme", "source_stage"],
                    "analysis_skill": "xtheme-analyzer",
                    "prepared_log_input": str(prepared_input),
                    "selected_log_input": str(prepared_input),
                    "target_time": "2026-05-25 16:50",
                    "user_request_text": "问题时间 2026-05-25 16:50:41 源码分析 3D场景信号分析 https://project.feishu.cn/xpfailuremgmt/buglo/detail/6998811703",
                },
            }
            previous_context = mock.Mock(
                request_text=previous_session["details"]["user_request_text"],
                summary_text="上一轮摘要",
                report_excerpt="上一轮报告摘录",
                history=[],
            )
            executed_plans: list[str] = []
            seen_skill_names: list[str] = []
            seen_context_profiles: list[str] = []
            seen_request_texts: list[str] = []

            def fake_run_analysis(*, plan, input_path, html_path, json_path, analysis_dir, timeout, target_time=None, request_text=None, **kwargs):
                executed_plans.append(plan.kind)
                html_path.write_text("<html>scene signal</html>", encoding="utf-8")
                json_path.write_text("{}", encoding="utf-8")
                return subprocess.CompletedProcess(args=["scene_signal"], returncode=0, stdout="", stderr="")

            def fake_file_agent(**kwargs):
                seen_skill_names.append(kwargs["skill_name"])
                seen_context_profiles.append(kwargs.get("context_profile", ""))
                seen_request_texts.append(kwargs.get("request_text", ""))
                analysis_dir = kwargs["analysis_dir"]
                analysis_dir.mkdir(parents=True, exist_ok=True)
                analysis_path = analysis_dir / "source_stage_analysis.md"
                analysis_path.write_text(
                    "## 结论摘要\n- app-server 源码分析正文。\n\n## 关键证据\n- L1\n\n## 最可能原因\n- R1\n\n## 待确认项\n- 无\n\n## 建议动作\n- 无\n",
                    encoding="utf-8",
                )
                kwargs["html_path"].write_text("<html>source stage</html>", encoding="utf-8")
                kwargs["json_path"].write_text("{}", encoding="utf-8")
                context_path = analysis_dir / "source_stage.context.md"
                context_path.write_text("ctx", encoding="utf-8")
                log_focus_path = analysis_dir / "log_focus.md"
                log_focus_path.write_text("focus", encoding="utf-8")
                debug_log_path = analysis_dir / "source_stage.debug.log"
                debug_log_path.write_text("", encoding="utf-8")
                events_path = analysis_dir / "source_stage.app_server_events.jsonl"
                events_path.write_text("{\"method\":\"turn/completed\"}\n", encoding="utf-8")
                return {
                    "ok": True,
                    "analysis_kind": "source_stage",
                    "provider": "codex",
                    "executor": "codex_app_server",
                    "completion_state": "complete",
                    "analysis_markdown_path": analysis_path,
                    "html_path": kwargs["html_path"],
                    "json_path": kwargs["json_path"],
                    "context_path": context_path,
                    "log_focus_manifest_path": log_focus_path,
                    "focused_log_input": prepared_input,
                    "debug_log_path": debug_log_path,
                    "events_path": events_path,
                    "evidence_count": 1,
                    "duration_seconds": 42.0,
                    "usage": {"totalTokens": 123},
                    "custom_skill_analysis_status": "completed",
                    "command": ["codex", "app-server"],
                    "stdout": "",
                    "stderr": "",
                    "thread_id": "thread-app-server",
                    "turn_id": "turn-app-server",
                }

            with (
                mock.patch.object(runner, "_write_reanalysis_source_evidence", return_value=None),
                mock.patch.object(runner, "_build_combined_report_artifacts", return_value=None),
                mock.patch.object(runner, "_run_analysis", side_effect=fake_run_analysis),
                mock.patch.object(runner, "_run_custom_skill_agent_analysis", side_effect=fake_file_agent),
                mock.patch.object(runner, "_run_bug_agent_summary") as summary_mock,
            ):
                result = runner.run_bug_reanalysis(
                    followup_text="基于源码重新分析，检查信号处理相关的代码逻辑",
                    previous_context=previous_context,
                    previous_session=previous_session,
                    force_rerun=True,
                    bridge_session_id="test-reanalysis-source-context-rebuild",
                )

        self.assertTrue(result.success)
        self.assertEqual(executed_plans, ["scene_signal"])
        self.assertEqual(seen_skill_names, ["scene-signal-diagnosis"])
        self.assertEqual(seen_context_profiles, ["scene-signal-diagnosis"])
        self.assertEqual(seen_request_texts, ["基于源码重新分析，检查信号处理相关的代码逻辑"])
        self.assertEqual(result.details["analysis_kinds"], ["scene_signal", "source_stage"])
        self.assertEqual(result.details["analysis_skill"], "scene-signal-diagnosis")
        self.assertEqual(result.details["context_profile"], "scene-signal-diagnosis")
        self.assertEqual(result.details["agent_summary_execution_backend"], "source_stage_direct")
        summary_mock.assert_not_called()
    def test_bug_reanalysis_source_stage_prefers_app_server_file_agent_over_pydantic_ai(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(
                dry_run=False,
                data_dir=Path(tmp),
                workspace_root=Path(tmp),
                codex_app_server=CodexAppServerOptions(enabled=True, use_for_file_agent=True),
            )
            config.bug_analysis.provider = "codex"
            config.bug_analysis.command = "codex"
            runner = BugAnalysisRunner(config)
            job_dir = Path(tmp) / "jobs" / "job_scene_signal_source_appserver"
            output_dir = job_dir / "output"
            output_dir.mkdir(parents=True, exist_ok=True)
            prepared_input = Path(tmp) / "logs"
            self._write_matching_log(prepared_input, "2026-05-25 16:50:41")
            previous_session = {
                "job_id": "job_scene_signal_source_appserver",
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
            counts = {"pai": 0, "file": 0}

            def fake_file_agent(**kwargs):
                counts["file"] += 1
                return {
                    "ok": False,
                    "error_code": "custom_skill_agent_failed",
                    "message": "stop after preferred file-agent path",
                    "command": ["codex"],
                    "provider": "codex",
                    "stdout": "",
                    "stderr": "",
                    "stdout_path": Path(tmp) / "stdout.txt",
                    "stderr_path": Path(tmp) / "stderr.txt",
                    "command_path": Path(tmp) / "command.txt",
                    "analysis_markdown_path": Path(tmp) / "analysis.md",
                }

            def fake_pai(**kwargs):
                counts["pai"] += 1
                return {"ok": True}

            with (
                mock.patch.object(runner, "_write_reanalysis_source_evidence", return_value=None),
                mock.patch.object(runner, "_build_combined_report_artifacts", return_value=None),
                mock.patch.object(runner, "_run_custom_skill_agent_analysis", side_effect=fake_file_agent),
                mock.patch.object(runner, "_run_source_stage_pydantic_ai", side_effect=fake_pai),
            ):
                result = runner.run_bug_reanalysis(
                    followup_text="基于源码重新分析 3D场景模式",
                    previous_context=previous_context,
                    previous_session=previous_session,
                    plans_override=[BugAnalysisPlan(kind="source_stage")],
                    classification_skill="scene-signal-diagnosis",
                    classification_source="agent",
                    classification_reason="prefer app-server",
                )

        self.assertFalse(result.success)
        self.assertEqual(counts["file"], 1)
        self.assertEqual(counts["pai"], 0)
    def test_bug_reanalysis_source_stage_timeout_uses_partial_stream_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp), workspace_root=Path(tmp))
            config.bug_analysis.provider = "codex"
            config.bug_analysis.command = "codex"
            runner = BugAnalysisRunner(config)
            job_dir = Path(tmp) / "jobs" / "job_scene_signal_source_partial"
            output_dir = job_dir / "output"
            output_dir.mkdir(parents=True, exist_ok=True)
            prepared_input = Path(tmp) / "logs"
            self._write_matching_log(prepared_input, "2026-05-25 16:50:41")
            previous_session = {
                "job_id": "job_scene_signal_source_partial",
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

            def fake_pai(**kwargs):
                return {
                    "ok": False,
                    "error_code": "pydantic_ai_stream_error",
                    "message": "stream failed",
                    "analysis_kind": "source_stage",
                }

            def fake_file_agent(**kwargs):
                return {
                    "ok": False,
                    "error_code": "custom_skill_agent_timeout",
                    "message": "专用 Skill `source_analysis` 文件 Agent 执行超时，未生成可验证证据。",
                    "analysis_kind": "source_stage",
                    "provider": "codex",
                    "partial_markdown": (
                        "## 阶段性结论（超时前）\n"
                        "- `SIGNAL_SR_SCENE_TYPE=14` 已闭环，卡点不在 Android->Unity 输入链路。\n"
                        "- 更可能是 Unity 收到 scene=14 后的主题/显示消费逻辑未展示 3D。\n"
                        "## 当前缺口\n"
                        "- 文件 Agent 在整理最终关键证据前超时。\n"
                    ),
                    "command": ["codex", "exec"],
                }

            with (
                mock.patch.object(runner, "_write_reanalysis_source_evidence", return_value=None),
                mock.patch.object(runner, "_build_combined_report_artifacts", return_value=None),
                mock.patch.object(runner, "_run_source_stage_pydantic_ai", side_effect=fake_pai),
                mock.patch.object(runner, "_run_custom_skill_agent_analysis", side_effect=fake_file_agent),
            ):
                result = runner.run_bug_reanalysis(
                    followup_text="基于源码重新分析",
                    previous_context=previous_context,
                    previous_session=previous_session,
                    plans_override=[BugAnalysisPlan(kind="source_stage")],
                    classification_skill="scene-signal-diagnosis",
                    classification_source="agent",
                    classification_reason="用户明确要求源码重分析",
                )

        self.assertTrue(result.success)
        self.assertIn("阶段性结论", result.message)
        self.assertIn("scene=14", result.message)
        self.assertEqual(result.details["source_stage_analysis_status"], "partial_timeout")
        self.assertEqual(result.details["agent_summary_execution_backend"], "source_stage_partial")
    def test_run_source_stage_pydantic_ai_returns_executor_and_report_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp), workspace_root=Path(tmp))
            runner = BugAnalysisRunner(config)
            html_path = Path(tmp) / "source_stage_report.html"
            json_path = Path(tmp) / "source_stage_report.json"
            analysis_dir = Path(tmp) / "source_stage_analysis"

            fake_result = mock.Mock(
                ok=True,
                markdown=(
                    "## 结论摘要\n- pydantic-ai 输出。\n\n"
                    "## 关键证据\n- L1\n\n"
                    "## 最可能原因\n- R1\n\n"
                    "## 待确认项\n- 无\n\n"
                    "## 建议动作\n- 无\n"
                ),
                output="",
                duration_seconds=1.5,
                tool_calls=1,
                tool_trace=[{"tool": "read_file", "args": "{}"}],
                usage={"totalTokens": 12},
                runtime_path="pydantic_ai_agent",
                model="mimo-v2.5-pro",
            )

            with mock.patch("lark_agent_bridge.agents.agent_runtime.AgentRuntime") as runtime_cls:
                runtime = runtime_cls.return_value
                runtime.is_available.return_value = True
                runtime.run.return_value = fake_result
                skill_record = mock.Mock(content="", skill_md_path="")
                with mock.patch.object(runner.skill_manager, "get_skill", return_value=skill_record):
                    result = runner._run_source_stage_pydantic_ai(
                        analysis_kind="source_stage",
                        skill_name="source_analysis",
                        request_text="基于源码分析 3D场景模式",
                        prompt_text="基于源码分析 3D场景模式",
                        title="进入场景模式没有展示3D场景",
                        description="问题时间: 2026-05-25 16:50:41",
                        fault_time="2026-05-25 16:50:41",
                        selected_input=Path(tmp) / "logs",
                        prepared_input=Path(tmp) / "logs",
                        source_evidence_path=None,
                        html_path=html_path,
                        json_path=json_path,
                        analysis_dir=analysis_dir,
                        progress_callback=None,
                    )

                payload = json.loads(json_path.read_text(encoding="utf-8"))
                html = html_path.read_text(encoding="utf-8")

        self.assertTrue(result["ok"])
        self.assertEqual(result["executor"], "pydantic_ai")
        self.assertEqual(result["html_path"], html_path)
        self.assertEqual(result["json_path"], json_path)
        self.assertEqual(payload["executor"], "pydantic_ai")
        self.assertIn(">pydantic_ai<", html)
        self.assertNotIn(">file_agent<", html)
    def test_write_custom_skill_agent_report_surfaces_source_stage_highlights(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp), workspace_root=Path(tmp))
            runner = BugAnalysisRunner(config)
            html_path = Path(tmp) / "source_stage_report.html"
            json_path = Path(tmp) / "source_stage_report.json"
            analysis_path = Path(tmp) / "source_stage_analysis.md"
            analysis_path.write_text(
                "\n".join(
                    [
                        "## 结论摘要",
                        "",
                        "- 高置信：主题皮肤被回退成 Basic。",
                        "- 中置信：场景模式信号链路本身完整。",
                        "",
                        "## 最可能原因",
                        "",
                        "GuideEngine 在 SR ThemeElement 为空时回退 Basic，并继续把该主题下发给 Unity。",
                        "",
                        "## 关键证据",
                        "",
                        "- **foo.kt:12**: `if (srThemeElement == null || srThemeElement.isEmpty()) { ... }` 说明空列表会回退 Basic。",
                        "- **bar.cs:34**: `current_theme = data.SRThemeSkin.Name` 说明 Unity 侧会直接采用回退后的主题名。",
                        "",
                        "## 待确认项",
                        "",
                        "- 缺少故障时刻的 Unity 运行日志。",
                        "",
                        "## 建议动作",
                        "",
                        "- 补抓故障时刻附近的 Unity 日志。",
                    ]
                ),
                encoding="utf-8",
            )
            skill_md = Path(tmp) / "SKILL.md"
            skill_md.write_text("# Source Analysis\n", encoding="utf-8")

            with mock.patch.object(runner, "_skill_context_paths", return_value=[skill_md]):
                runner._write_custom_skill_agent_report(
                    analysis_kind="source_stage",
                    analysis_label="源码分析阶段",
                    html_path=html_path,
                    json_path=json_path,
                    analysis_markdown_path=analysis_path,
                    skill_name="source_analysis",
                    provider="pydantic_ai",
                    request_text="源码分析 3D场景信号分析",
                    prompt_text="源码分析 3D场景信号分析",
                    title="进入场景模式没有展示3D场景",
                    description="问题描述",
                    fault_time="2026-05-25 16:50:41",
                    selected_input=Path(tmp) / "logs",
                    prepared_input=Path(tmp) / "logs",
                    source_evidence_path=Path(tmp) / "bug_source_evidence.md",
                    evidence_count=2,
                    duration_seconds=12.5,
                    executor="pydantic_ai",
                )

            html = html_path.read_text(encoding="utf-8")

        self.assertIn("<h2>结论摘要</h2>", html)
        self.assertIn("高置信：主题皮肤被回退成 Basic。", html)
        self.assertIn("<h2>最可能原因</h2>", html)
        self.assertIn("GuideEngine 在 SR ThemeElement 为空时回退 Basic", html)
        self.assertIn("<h2>关键证据</h2>", html)
        self.assertIn("foo.kt:12", html)
        self.assertIn("bar.cs:34", html)
        self.assertIn("展开查看完整分析 Markdown", html)
    def test_bug_reanalysis_source_stage_app_server_can_skip_separate_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(
                dry_run=False,
                data_dir=Path(tmp),
                workspace_root=Path(tmp),
                codex_app_server=CodexAppServerOptions(enabled=True, use_for_file_agent=True),
            )
            config.bug_analysis.provider = "codex"
            config.bug_analysis.command = "codex"
            runner = BugAnalysisRunner(config)
            job_dir = Path(tmp) / "jobs" / "job_scene_signal_source_direct_reply"
            output_dir = job_dir / "output"
            output_dir.mkdir(parents=True, exist_ok=True)
            prepared_input = Path(tmp) / "logs"
            self._write_matching_log(prepared_input, "2026-05-25 16:50:41")
            previous_session = {
                "job_id": "job_scene_signal_source_direct_reply",
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
            analysis_dir = output_dir / "source_stage_analysis"
            analysis_dir.mkdir(parents=True, exist_ok=True)
            analysis_path = analysis_dir / "source_stage_analysis.md"
            analysis_path.write_text(
                "## 结论摘要\n- app-server 源码分析正文。\n\n## 关键证据\n- L1\n\n## 最可能原因\n- R1\n\n## 待确认项\n- 无\n\n## 建议动作\n- 无\n",
                encoding="utf-8",
            )

            def fake_file_agent(**kwargs):
                return {
                    "ok": True,
                    "analysis_kind": "source_stage",
                    "provider": "codex",
                    "executor": "codex_app_server",
                    "completion_state": "complete",
                    "analysis_markdown_path": analysis_path,
                    "html_path": output_dir / "source_stage_report.html",
                    "json_path": output_dir / "source_stage_report.json",
                    "context_path": analysis_dir / "source_stage.context.md",
                    "log_focus_manifest_path": analysis_dir / "log_focus.md",
                    "focused_log_input": prepared_input,
                    "debug_log_path": analysis_dir / "source_stage.debug.log",
                    "events_path": analysis_dir / "source_stage.app_server_events.jsonl",
                    "evidence_count": 1,
                    "duration_seconds": 42.0,
                    "usage": {"totalTokens": 123},
                    "custom_skill_analysis_status": "completed",
                    "command": ["codex", "app-server"],
                    "stdout": "",
                    "stderr": "",
                }

            with (
                mock.patch.object(runner, "_write_reanalysis_source_evidence", return_value=None),
                mock.patch.object(runner, "_build_combined_report_artifacts", return_value=None),
                mock.patch.object(runner, "_run_custom_skill_agent_analysis", side_effect=fake_file_agent),
                mock.patch.object(runner, "_run_bug_agent_summary") as summary_mock,
            ):
                result = runner.run_bug_reanalysis(
                    followup_text="基于源码重新分析 3D场景模式",
                    previous_context=previous_context,
                    previous_session=previous_session,
                    plans_override=[BugAnalysisPlan(kind="source_stage")],
                    classification_skill="scene-signal-diagnosis",
                    classification_source="agent",
                    classification_reason="direct source reply",
                )

        self.assertTrue(result.success)
        self.assertIn("app-server 源码分析正文", result.message)
        self.assertEqual(result.details["agent_summary_execution_backend"], "source_stage_direct")
        summary_mock.assert_not_called()
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
