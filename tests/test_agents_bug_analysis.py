from _agents_base import *  # noqa: F401,F403
from _agents_base import _AgentTestBase


class AgentsBugAnalysisTests(_AgentTestBase):
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
                    "_run_source_stage_pydantic_ai",
                    side_effect=self._fake_source_stage_success,
                ),
                mock.patch.object(
                    runner,
                    "_run_custom_skill_agent_analysis",
                    side_effect=self._fake_source_stage_success,
                ),
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
                "tool_calls": 2,
                "tool_trace": [{"tool": "search_large_log", "args": "{'pattern': 'startCheck timeout'}"}],
            },
        )

        self.assertEqual(details["agent_summary_execution_backend"], "direct_api")
        self.assertEqual(details["agent_summary_backend_reason"], "direct_api_failed_no_fallback")
        self.assertNotIn("agent_summary_fallback_from", details)
        self.assertEqual(details["agent_summary_tool_calls"], 2)
        self.assertEqual(
            details["agent_summary_tool_trace"],
            [{"tool": "search_large_log", "args": "{'pattern': 'startCheck timeout'}"}],
        )
    def test_agent_runtime_details_normalize_prompt_completion_and_cached_usage(self):
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
                "usage": {
                    "prompt_tokens": 321,
                    "cached_input_tokens": 280,
                    "completion_tokens": 54,
                    "total_tokens": 375,
                },
                "usage_scope": "direct_api",
            },
        )

        self.assertEqual(details["agent_summary_input_tokens"], 321)
        self.assertEqual(details["agent_summary_cached_input_tokens"], 280)
        self.assertEqual(details["agent_summary_output_tokens"], 54)
        self.assertEqual(details["agent_summary_total_tokens"], 375)
    def test_pydantic_summary_prompt_lists_decoded_logs_and_search_guidance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report_json = root / "bug_3d_startup_report.json"
            decoded_log = root / "startup_analysis" / "workspace" / "collected" / "user0_main_2026-05-28_11-00.alog.log"
            decoded_log.parent.mkdir(parents=True)
            decoded_log.write_text("05-28 11:18:00 startCheck timeout\n", encoding="utf-8")
            report_json.write_text(
                json.dumps(
                    {
                        "decoded_logs": [str(decoded_log)],
                        "sessions": [],
                        "target_time": "2026-05-28T11:18:00",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            request_artifact = root / "bug_agent_request.md"
            metadata_path = root / "bug_metadata.md"
            request_artifact.write_text("分析3D启动生命周期", encoding="utf-8")
            metadata_path.write_text(f"- JSON: `{report_json}`\n", encoding="utf-8")

            runner = BugAnalysisRunner(BridgeConfig(dry_run=True, data_dir=root / "data", workspace_root=root))
            prompt = runner._build_bug_summary_prompt_for_pydantic_ai(
                request_text="分析3D启动生命周期",
                request_artifact=request_artifact,
                metadata_path=metadata_path,
            )

        self.assertIn("search_large_log", prompt)
        self.assertIn(str(decoded_log), prompt)
        self.assertIn("startCheck timeout", prompt)
        self.assertIn("X3DCB-DROP", prompt)
        self.assertNotIn("grep_text", prompt)
    def test_pydantic_summary_result_and_progress_include_tool_trace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            request_artifact = root / "bug_agent_request.md"
            metadata_path = root / "bug_metadata.md"
            output_path = root / "bug_agent_summary.md"
            request_artifact.write_text("request", encoding="utf-8")
            metadata_path.write_text("metadata", encoding="utf-8")
            progress_events: list[dict[str, object]] = []

            class FakeRuntimeResult:
                ok = True
                markdown = "## 结论摘要\n- done"
                model = "gpt-test"
                duration_seconds = 2.0
                usage = {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12}
                runtime_path = "pydantic_ai_agent"
                tool_calls = 1
                tool_trace = [{"tool": "search_large_log", "args": "{'pattern': 'startCheck timeout'}"}]

            class FakeRuntime:
                def __init__(self, *args, **kwargs):
                    pass

                def run(self, **kwargs):
                    self.system_prompt = kwargs["system_prompt"]
                    return FakeRuntimeResult()

            config = BridgeConfig(dry_run=False, data_dir=root / "data", workspace_root=root)
            runner = BugAnalysisRunner(config)

            with (
                mock.patch("lark_agent_bridge.agents.agent_runtime._check_pydantic_ai", return_value=True),
                mock.patch("lark_agent_bridge.agents.agent_runtime.AgentRuntime", FakeRuntime),
            ):
                result = runner._run_bug_summary_pydantic_ai(
                    request_text="分析3D启动生命周期",
                    request_artifact=request_artifact,
                    metadata_path=metadata_path,
                    output_path=output_path,
                    progress_callback=progress_events.append,
                )

        completed = [event for event in progress_events if event["stage"] == "bug_agent_summary_completed"]
        self.assertEqual(result["tool_trace"], FakeRuntimeResult.tool_trace)
        self.assertEqual(completed[-1]["details"]["tool_trace"], FakeRuntimeResult.tool_trace)
    def test_pydantic_summary_rejects_runtime_without_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            request_artifact = root / "bug_agent_request.md"
            metadata_path = root / "bug_metadata.md"
            output_path = root / "bug_agent_summary.md"
            request_artifact.write_text("request", encoding="utf-8")
            metadata_path.write_text("metadata", encoding="utf-8")

            class FakeRuntimeResult:
                ok = True
                markdown = "tools unavailable"
                model = "gpt-test"
                duration_seconds = 2.0
                usage = {"total_tokens": 12}
                runtime_path = "direct_api"
                tool_calls = 0
                tool_trace = []
                error = ""
                error_code = ""

            class FakeRuntime:
                last_kwargs = {}

                def __init__(self, *args, **kwargs):
                    pass

                def run(self, **kwargs):
                    type(self).last_kwargs = kwargs
                    return FakeRuntimeResult()

            runner = BugAnalysisRunner(BridgeConfig(dry_run=False, data_dir=root / "data", workspace_root=root))

            with (
                mock.patch("lark_agent_bridge.agents.agent_runtime._check_pydantic_ai", return_value=True),
                mock.patch("lark_agent_bridge.agents.agent_runtime.AgentRuntime", FakeRuntime),
            ):
                result = runner._run_bug_summary_pydantic_ai(
                    request_text="分析3D启动生命周期",
                    request_artifact=request_artifact,
                    metadata_path=metadata_path,
                    output_path=output_path,
                    progress_callback=None,
                )

        self.assertTrue(FakeRuntime.last_kwargs["strict_tools"])
        self.assertEqual(result["message"], "")
        self.assertEqual(result["error"], "pydantic_ai_summary_requires_tools")
        self.assertFalse(output_path.exists())
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
