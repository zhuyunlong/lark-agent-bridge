from _agents_base import *  # noqa: F401,F403
from _agents_base import _AgentTestBase


class AgentsBugReanalysis2Tests(_AgentTestBase):
    def test_bug_reanalysis_does_not_reuse_split_part_as_prepared_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp), workspace_root=Path(tmp))
            runner = BugAnalysisRunner(config)
            job_dir = Path(tmp) / "jobs" / "job_split"
            output_dir = job_dir / "output"
            output_dir.mkdir(parents=True, exist_ok=True)
            split_part = Path(tmp) / "bug_cache" / "attachments" / "bundle.xp.zip.001"
            split_part.parent.mkdir(parents=True, exist_ok=True)
            split_part.write_bytes(b"partial")
            prepared_input = Path(tmp) / "prepared" / "Log"
            prepared_input.mkdir(parents=True, exist_ok=True)
            combined_html = output_dir / "bug_startup_stuck_report.html"
            combined_json = output_dir / "bug_startup_stuck_report.json"
            analysis_inputs = []

            def fake_run_analysis(*, plan, input_path, html_path, json_path, analysis_dir, timeout, target_time, request_text=None):
                analysis_inputs.append(input_path)
                html_path.write_text("<html>startup</html>", encoding="utf-8")
                json_path.write_text("{}", encoding="utf-8")
                return subprocess.CompletedProcess(args=["python3"], returncode=0, stdout="", stderr="")

            previous_session = {
                "job_id": "job_split",
                "job_dir": str(job_dir),
                "details": {
                    "bug_url": "https://project.feishu.cn/xpfailuremgmt/buglo/detail/7000000000",
                    "analysis_kinds": ["startup"],
                    "prepared_log_input": str(split_part),
                    "selected_log_input": str(split_part),
                    "user_request_text": (
                        "https://project.feishu.cn/xpfailuremgmt/buglo/detail/7000000000 "
                        "问题时间 2026-06-07 09:35"
                    ),
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
                    "_retry_bug_log_download",
                    return_value={"ok": True, "selected_input": split_part, "prepared_input": prepared_input},
                ) as retry_mock,
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
                    followup_text="重新分析",
                    previous_context=previous_context,
                    previous_session=previous_session,
                    force_rerun=True,
                )

        self.assertTrue(result.success)
        retry_mock.assert_called_once()
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
                request_text=(
                    "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 "
                    "问题时间 2026-05-11 23:10 分析启动和卡顿\n\n追问/修正：先按旧时间重跑"
                ),
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
        self.assertEqual(
            summary_mock.call_args.kwargs["request_text"],
            previous_session["details"]["user_request_text"],
        )
        self.assertIsNone(summary_mock.call_args.kwargs["previous_summary_path"])
        self.assertEqual(summary_mock.call_args.kwargs["timeout"], 500)
        self.assertNotIn(long_assistant_history, request_text)
        self.assertNotIn("上一轮长回答已省略", request_text)
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
