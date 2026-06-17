from _agents_base import *  # noqa: F401,F403
from _agents_base import _AgentTestBase


class AgentsBugAnalysis2Tests(_AgentTestBase):
    def test_custom_skill_file_agent_app_server_falls_back_to_exec(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            skill_name = "app-server-fallback-skill"
            skill_dir = root / ".ai" / "skills" / skill_name
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: App Server Fallback Skill\ndescription: fallback test.\n---\n\n# Fallback\n",
                encoding="utf-8",
            )
            config = BridgeConfig(
                dry_run=False,
                data_dir=Path(tmp) / "data",
                workspace_root=root,
                codex_app_server=CodexAppServerOptions(
                    enabled=True,
                    use_for_file_agent=True,
                    fallback_to_exec=True,
                ),
            )
            config.bug_analysis.provider = "codex"
            config.bug_analysis.command = "codex"
            runner = BugAnalysisRunner(config)
            log_root = Path(tmp) / "logs"
            self._write_matching_log(log_root, "2026-05-25 16:50:41")
            runtime = mock.Mock()
            runtime.run_turn.return_value = CodexAppServerResult(
                ok=False,
                error="startup failed",
                error_code="codex_app_server_startup_failed",
                command=["codex", "app-server"],
                stderr="startup failed",
                should_retire=True,
            )

            def fake_file_agent(command, **kwargs):
                output_path = Path(command[command.index("--output-last-message") + 1])
                output_path.write_text(
                    "## 结论摘要\n- exec fallback 完成。\n\n"
                    "## 关键证据\n- L1: fallback evidence\n\n"
                    "## 待确认项\n- 无\n\n"
                    "## 建议动作\n- 无\n",
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

            with (
                mock.patch(
                    "lark_agent_bridge.agents.bug.custom_skill.check_codex_app_server_available",
                    return_value=(True, "0.134.0"),
                ),
                mock.patch(
                    "lark_agent_bridge.agents.bug.custom_skill.CodexAppServerRuntime",
                    return_value=runtime,
                ),
                mock.patch("lark_agent_bridge.agents.run_tracked_process", side_effect=fake_file_agent) as run_mock,
            ):
                result = runner._run_custom_skill_agent_analysis(
                    skill_name=skill_name,
                    request_text="2026-05-25 16:50:41 3D场景模式",
                    prompt_text="2026-05-25 16:50:41 3D场景模式",
                    title="3D 场景模式",
                    description="",
                    fault_time="2026-05-25 16:50:41",
                    selected_input=log_root,
                    prepared_input=log_root,
                    source_evidence_path=None,
                    html_path=Path(tmp) / "bug_custom_skill_report.html",
                    json_path=Path(tmp) / "bug_custom_skill_report.json",
                    analysis_dir=Path(tmp) / "custom_skill_analysis",
                    progress_callback=None,
                    timeout=30,
                )

        self.assertTrue(result["ok"])
        self.assertEqual(result["executor"], "file_agent")
        self.assertEqual(result["app_server_error_code"], "codex_app_server_startup_failed")
        run_mock.assert_called_once()
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
                custom_json = kwargs["report_jsons"].get("source_code_skill") or kwargs["report_jsons"].get("custom_skill")
                self.assertIsNotNone(custom_json)
                assert custom_json is not None
                payload = json.loads(custom_json.read_text(encoding="utf-8"))
                self.assertEqual(payload["mode"], "source_code_skill_agent_analysis")
                self.assertEqual(payload["source_code_skill_analysis_status"], "completed")
                self.assertEqual(payload["analysis_kind"], "source_code_skill")
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
                analysis_file = (
                    result.details.get("source_code_skill_analysis_file")
                    or result.details.get("custom_skill_analysis_file")
                    or ""
                )
                analysis_file_exists = Path(analysis_file).exists()

        self.assertTrue(result.success)
        self.assertEqual(result.message, "agent final conclusion")
        self.assertEqual(result.details["analysis_kind"], "source_code_skill")
        self.assertEqual(result.details["source_code_skill_analysis_status"], "completed")
        self.assertEqual(result.details["source_code_skill_evidence_count"], 2)
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
    def test_bug_analysis_classifies_xtheme_source_log_tokens(self):
        runner = BugAnalysisRunner(BridgeConfig(dry_run=True))

        plan = runner.classify_request(
            prompt_text="NAV_XThemeStrategy ThemeMode TimePeriod SrThemeSkin 发送后黑白夜异常",
            title="",
            description="",
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
    def test_bug_analysis_classifies_perception_receive_msg_tokens(self):
        runner = BugAnalysisRunner(BridgeConfig(dry_run=True))

        plan = runner.classify_request(
            prompt_text="XDataNativeProxy ReceiveMsg bizCode 收到:放弃 当前统计是否正常",
            title="",
            description="",
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
    def test_bug_time_context_prefers_title_when_user_full_date_conflicts_with_bug_reference(self):
        runner = BugAnalysisRunner(BridgeConfig(dry_run=True))

        context = runner._resolve_bug_time_context(
            request_text=(
                "问题时间 2025-05-10 14:30:00 源码分析 3D场景信号分析 "
                "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6998811703"
            ),
            title="2026-05-25 16:50:41 【d03】6.2.3】拾光主题切换成天玑主题，进入场景模式没有展示3D场景",
            description="问题描述：进入场景模式后没有展示3D场景",
            reference_time="2026-05-25T16:52:33+08:00",
        )

        self.assertEqual(context.fault_time, "2026-05-25 16:50:41")
        self.assertEqual(context.source, "title")
        self.assertIn("冲突", context.note)
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
    def test_bug_analysis_classifies_pullover_chain_log_token(self):
        runner = BugAnalysisRunner(BridgeConfig(dry_run=True))
        plan = runner.classify_request(
            prompt_text="Side Parking 日志没有出现，side_parking_info 也没到 Unity",
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
    def test_bug_analysis_select_log_input_waits_for_all_split_xp_zip_parts(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = BugAnalysisRunner(BridgeConfig(dry_run=True))
            bug_dir = Path(tmp)
            attachments_dir = bug_dir / "attachments"
            attachments_dir.mkdir(parents=True, exist_ok=True)
            first_part = attachments_dir / "bundle.xp.zip.001"
            first_part.write_bytes(b"part1")

            selected = runner._select_log_input(
                bug_dir,
                fetched={
                    "attachments": [
                        {"name": "bundle.xp.zip.001"},
                        {"name": "bundle.xp.zip.002"},
                    ]
                },
            )
            self.assertIsNone(selected)

            (attachments_dir / "bundle.xp.zip.002").write_bytes(b"part2")
            selected = runner._select_log_input(
                bug_dir,
                fetched={
                    "attachments": [
                        {"name": "bundle.xp.zip.001"},
                        {"name": "bundle.xp.zip.002"},
                    ]
                },
            )

        self.assertEqual(selected, first_part)
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
            "bundle.xp.zip.002",
            "bundle.xp.zip.006",
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
                {"name": "parts.xp.zip.001", "url": "https://example.test/parts.xp.zip.001"},
                {"name": "parts.xp.zip.002", "url": "https://example.test/parts.xp.zip.002"},
                {"name": "parts.xp.zip.003", "url": "https://example.test/parts.xp.zip.003"},
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
        self.assertEqual(
            downloaded,
            {
                "keep.zip",
                "keep.log",
                "parts.xp.zip.001",
                "parts.xp.zip.002",
                "parts.xp.zip.003",
            },
        )
        self.assertEqual(set(result["skipped"]), {"skip.mp4", "skip.png"})
        command_text = "\n".join(" ".join(command) for command in commands)
        self.assertIn("keep.zip", command_text)
        self.assertIn("keep.log", command_text)
        self.assertIn("parts.xp.zip.001", command_text)
        self.assertIn("parts.xp.zip.002", command_text)
        self.assertIn("parts.xp.zip.003", command_text)
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
    def test_bug_analysis_prepare_log_input_combines_split_xp_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = BugAnalysisRunner(BridgeConfig(dry_run=True))
            root = Path(tmp)
            base_zip = root / "bundle.xp.zip"
            xp_name = "bundle.xp"
            with zipfile.ZipFile(base_zip, "w") as zf:
                zf.writestr(xp_name, b"encrypted")
            payload = base_zip.read_bytes()
            base_zip.unlink()
            part_size = max(1, len(payload) // 3)
            parts = [
                root / "bundle.xp.zip.001",
                root / "bundle.xp.zip.002",
                root / "bundle.xp.zip.003",
            ]
            for index, part in enumerate(parts):
                start = index * part_size
                end = None if index == len(parts) - 1 else (index + 1) * part_size
                part.write_bytes(payload[start:end])
            prepared_dir = root / "prepared" / "Log"
            prepared_dir.mkdir(parents=True)
            expanded_paths = []

            def fake_expand_xp_file(path):
                expanded_paths.append(path)
                return prepared_dir

            with mock.patch.object(runner, "_expand_xp_file", side_effect=fake_expand_xp_file):
                prepared = runner._prepare_log_input(parts[0])

        self.assertEqual(prepared, prepared_dir)
        self.assertEqual([path.name for path in expanded_paths], [xp_name])
        self.assertFalse((root / "bundle.xp.zip").exists())
    def test_bug_cache_metadata_does_not_reuse_split_part_as_prepared_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = BugAnalysisRunner(BridgeConfig(dry_run=True))
            bug_dir = Path(tmp) / "bug"
            attachments_dir = bug_dir / "attachments"
            attachments_dir.mkdir(parents=True)
            split_part = attachments_dir / "bundle.xp.zip.001"
            split_part.write_bytes(b"partial")
            prepared_dir = attachments_dir / "bundle" / "Log"
            prepared_dir.mkdir(parents=True)
            (bug_dir / "cache.json").write_text(
                json.dumps(
                    {
                        "selected_log_input": str(split_part),
                        "prepared_log_input": str(split_part),
                    }
                ),
                encoding="utf-8",
            )

            with mock.patch.object(runner, "_prepare_log_input", return_value=prepared_dir) as prepare:
                selected, prepared = runner._read_bug_cache_log_input(bug_dir)

        self.assertEqual(selected, split_part)
        self.assertEqual(prepared, prepared_dir)
        prepare.assert_called_once_with(split_part)
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
