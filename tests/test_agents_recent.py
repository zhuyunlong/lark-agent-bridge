from _agents_base import *  # noqa: F401,F403
from _agents_base import _AgentTestBase
import time

from lark_agent_bridge.models import create_job_context


class AgentsRecentRefactorTests(_AgentTestBase):
    def test_confirmed_startup_stuck_plan_group_uses_combined_skill_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=True, data_dir=Path(tmp), workspace_root=Path(tmp))
            runner = BugAnalysisRunner(config)

            result = runner.run_bug_analysis(
                BugRequest(
                    bug_url="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6995380113",
                    prompt="3D生命周期",
                    raw_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6995380113 3D生命周期",
                    triggered=True,
                ),
                plans_override=[
                    BugAnalysisPlan(kind="startup"),
                    BugAnalysisPlan(kind="stuck"),
                ],
                classification_skill="startup+stuck",
                classification_source="user_selected_reply",
                classification_reason="用户选择两个方向都跑。",
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["analysis_skill"], "startup+stuck")
        self.assertEqual(result.details["analysis_skill_label"], "3D启动卡顿综合分析")

    def test_bug_analysis_agent_signal_selection_requires_explicit_signal_target(self):
        runner = BugAnalysisRunner(BridgeConfig(dry_run=True))

        with mock.patch.object(
            runner,
            "_run_bug_decision_agent",
            return_value=(
                {
                    "analysis_kind": "signal",
                    "skill": "signal-chain-analyzer",
                    "signal_hint": "SIGNAL_FEATURE_SFM",
                    "confidence": "high",
                    "reason": "误把档位信号链路当成通用 signal 分析。",
                },
                "claude",
            ),
        ):
            decision = runner.classify_and_decide(
                request_text=(
                    "https://project.feishu.cn/xpfailuremgmt/buglo/detail/7003441850 "
                    "5-29 16:06 调查档位信号链路"
                ),
                prompt_text="5-29 16:06 调查档位信号链路",
                title="【G02】【623】【Android16】标定后，倒车影像不出图",
                description="测试步骤：AVM标定，后挂R档\n实际结果：R档不出图",
            )

        self.assertEqual([plan.kind for plan in decision.selection.plans], ["scene_signal"])
        self.assertEqual(decision.selection.skill_name, "scene-signal-diagnosis")
        self.assertEqual(decision.selection.source, "manual_fallback")

    def test_bug_analysis_classifies_gear_signal_chain_as_scene_signal(self):
        runner = BugAnalysisRunner(BridgeConfig(dry_run=True))

        plan = runner.classify_request(
            prompt_text="调查档位信号链路",
            title="【G02】【623】【Android16】标定后，倒车影像不出图",
            description="测试步骤：AVM标定，后挂R档\n实际结果：R档不出图",
        )

        self.assertEqual(plan.kind, "scene_signal")
        self.assertIsNone(plan.signal_code)

    def test_bug_reanalysis_partial_source_stage_falls_back_to_summary(self):
        # A non-complete app-server result must NOT be reused as the final answer;
        # the summary path runs instead (no truncated direct reply).
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(
                dry_run=False, data_dir=Path(tmp), workspace_root=Path(tmp),
                codex_app_server=CodexAppServerOptions(enabled=True, use_for_file_agent=True),
            )
            config.bug_analysis.provider = "codex"
            config.bug_analysis.command = "codex"
            runner = BugAnalysisRunner(config)
            job_dir = Path(tmp) / "jobs" / "job_partial"
            (job_dir / "output").mkdir(parents=True, exist_ok=True)
            prepared_input = Path(tmp) / "logs"
            self._write_matching_log(prepared_input, "2026-05-25 16:50:41")
            previous_session = {
                "job_id": "job_partial",
                "job_dir": str(job_dir),
                "details": {
                    "analysis_kind": "xtheme",
                    "analysis_kinds": ["xtheme", "source_stage"],
                    "analysis_skill": "xtheme-analyzer",
                    "prepared_log_input": str(prepared_input),
                    "selected_log_input": str(prepared_input),
                    "target_time": "2026-05-25 16:50",
                    "user_request_text": "问题时间 2026-05-25 16:50:41 源码分析 3D场景信号分析",
                },
            }
            previous_context = mock.Mock(
                request_text=previous_session["details"]["user_request_text"],
                summary_text="上一轮摘要", report_excerpt="上一轮报告摘录", history=[],
            )

            def fake_run_analysis(*, plan, html_path, json_path, **kwargs):
                html_path.write_text("<html>scene</html>", encoding="utf-8")
                json_path.write_text("{}", encoding="utf-8")
                return subprocess.CompletedProcess(args=["scene_signal"], returncode=0, stdout="", stderr="")

            def fake_file_agent(**kwargs):
                analysis_dir = kwargs["analysis_dir"]
                analysis_dir.mkdir(parents=True, exist_ok=True)
                analysis_path = analysis_dir / "source_stage_analysis.md"
                analysis_path.write_text("## 结论摘要\n- 半截\n\n## 关键证据\n- L1\n", encoding="utf-8")
                kwargs["html_path"].write_text("<html>source</html>", encoding="utf-8")
                kwargs["json_path"].write_text("{}", encoding="utf-8")
                return {
                    "ok": True,
                    "analysis_kind": "source_stage",
                    "provider": "codex",
                    "executor": "codex_app_server",
                    "completion_state": "partial",  # the key difference
                    "analysis_markdown_path": analysis_path,
                    "html_path": kwargs["html_path"],
                    "json_path": kwargs["json_path"],
                    "evidence_count": 1,
                    "duration_seconds": 42.0,
                    "usage": {},
                    "custom_skill_analysis_status": "completed",
                    "command": ["codex", "app-server"],
                    "stdout": "", "stderr": "",
                }

            with (
                mock.patch.object(runner, "_write_reanalysis_source_evidence", return_value=None),
                mock.patch.object(runner, "_build_combined_report_artifacts", return_value=None),
                mock.patch.object(runner, "_run_analysis", side_effect=fake_run_analysis),
                mock.patch.object(runner, "_run_custom_skill_agent_analysis", side_effect=fake_file_agent),
                mock.patch.object(runner, "_run_bug_agent_summary") as summary_mock,
            ):
                summary_mock.return_value = {
                    "message": "## 结论摘要\n- 总结\n\n## 关键证据\n- L1\n",
                    "command": None, "error": "", "provider": "codex", "session_id": "",
                    "resumed": False, "duration_seconds": 1.0, "usage": {},
                    "execution_backend": "codex_exec",
                }
                result = runner.run_bug_reanalysis(
                    followup_text="基于源码重新分析，检查信号处理相关的代码逻辑",
                    previous_context=previous_context,
                    previous_session=previous_session,
                    force_rerun=True,
                    bridge_session_id="test-reanalysis-partial",
                )

        self.assertTrue(result.success)
        self.assertNotEqual(result.details.get("agent_summary_execution_backend"), "source_stage_direct")
        summary_mock.assert_called_once()

    def test_decide_replay_uses_direct_api_when_available(self):
        config = BridgeConfig(dry_run=False)
        config.intent_analysis.enabled = True
        runner = IntentAnalysisRunner(config)
        runner._llm_client = mock.Mock()
        runner._llm_client.is_available.return_value = True
        runner._llm_client.classify_intent.return_value = LLMResponse(
            content=json.dumps(
                {
                    "action": "answer_from_existing",
                    "mode": "direct_analysis",
                    "confidence": "medium",
                    "reason": "用户询问已有报告结论",
                },
                ensure_ascii=False,
            ),
            model="test-model",
            duration_seconds=0.1,
        )
        context = AnalysisReplayContext(
            root_message_id="om_root",
            chat_id="oc_1",
            mode="direct_analysis",
            previous_mode="direct_analysis",
            original_request_text="分析这份日志",
            current_text="这个结论依据是什么？",
            history=[],
        )

        decision = runner.decide_replay(context=context)

        self.assertEqual(decision.action, "answer_from_existing")
        self.assertEqual(decision.mode, "direct_analysis")
        runner._llm_client.classify_intent.assert_called_once()

    def test_ld_focus_log_candidates_prefer_montecarlo_logd_near_fault(self):
        from datetime import datetime as _dt
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp) / "data", workspace_root=Path(tmp))
            runner = BugAnalysisRunner(config)
            logs = Path(tmp) / "logs" / "data" / "Log" / "log1"
            # noise package far from fault, montecarlo + logd near fault
            (logs / "app" / "com.xiaopeng.aicabinservice").mkdir(parents=True)
            (logs / "app" / "com.xiaopeng.aicabinservice" / "main_2026-05-22_00-00.log").write_text("x", encoding="utf-8")
            (logs / "app" / "com.xiaopeng.montecarlo").mkdir(parents=True)
            (logs / "app" / "com.xiaopeng.montecarlo" / "main_2026-05-22_19-00.log").write_text("x", encoding="utf-8")
            (logs / "logd").mkdir(parents=True)
            (logs / "logd" / "main.txt").write_text("x", encoding="utf-8")
            cands = runner._ld_focus_log_candidates(Path(tmp) / "logs", _dt(2026, 5, 22, 19, 46), limit=8)
            joined = [str(c) for c in cands]
        # montecarlo + logd must outrank the alphabetically-first aicabinservice noise
        self.assertTrue(any("com.xiaopeng.montecarlo" in p for p in joined))
        self.assertTrue(any("/logd/" in p for p in joined))
        self.assertLess(
            min(i for i, p in enumerate(joined) if "montecarlo" in p or "/logd/" in p),
            next(i for i, p in enumerate(joined) if "aicabinservice" in p),
        )

    def test_ld_focus_log_candidates_exclude_baidu_sdk_diag_logs_by_default(self):
        from datetime import datetime as _dt
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp) / "data", workspace_root=Path(tmp))
            runner = BugAnalysisRunner(config)
            logs = Path(tmp) / "logs" / "data" / "Log" / "log1"
            mc_dir = logs / "app" / "com.xiaopeng.montecarlo"
            mc_dir.mkdir(parents=True)
            (mc_dir / "main_2026-06-14_22-00.log").write_text("LD:false\n", encoding="utf-8")
            ldlog_dir = mc_dir / "ldnavi_log" / "ldlog"
            ldlog_dir.mkdir(parents=True)
            diag_log = ldlog_dir / "DIAG_D-373-20260614-221301_1780998853-2520.log"
            diag_zst = ldlog_dir / "DIAG_D-374-20260614-221602_1780998853-2520.log.zst"
            diag_log.write_text("map_status:2\n", encoding="utf-8")
            diag_zst.write_text("compressed placeholder\n", encoding="utf-8")
            diag_zst.with_suffix("").write_text("decoded placeholder\n", encoding="utf-8")

            cands = runner._ld_focus_log_candidates(Path(tmp) / "logs", _dt(2026, 6, 14, 22, 13), limit=8)
            joined = [str(c) for c in cands]

        self.assertTrue(any("main_2026-06-14_22-00.log" in p for p in joined))
        self.assertFalse(any("ldnavi_log" in p or "DIAG_D-" in p for p in joined))

    def test_ld_executor_find_log_files_excludes_baidu_sdk_diag_logs_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp) / "data", workspace_root=Path(tmp))
            runner = BugAnalysisRunner(config)
            cache_dir = Path(tmp) / "cache"
            mc_dir = cache_dir / "logs" / "data" / "Log" / "log1" / "app" / "com.xiaopeng.montecarlo"
            mc_dir.mkdir(parents=True)
            (mc_dir / "main_2026-06-14_22-00.log").write_text("LD:false\n", encoding="utf-8")
            ldlog_dir = mc_dir / "ldnavi_log" / "ldlog"
            ldlog_dir.mkdir(parents=True)
            (ldlog_dir / "DIAG_D-373-20260614-221301_1780998853-2520.log").write_text(
                "map_status:2\n",
                encoding="utf-8",
            )

            log_files = runner._ld_executor_find_log_files(
                cache_dir=cache_dir,
                fault_time="2026-06-14 22:13",
            )
            joined = [str(path) for path in log_files]

        self.assertTrue(any("main_2026-06-14_22-00.log" in p for p in joined))
        self.assertFalse(any("ldnavi_log" in p or "DIAG_D-" in p for p in joined))

    def test_ld_executor_prefers_plain_text_sibling_over_raw_alog(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp) / "data", workspace_root=Path(tmp))
            runner = BugAnalysisRunner(config)
            cache_dir = Path(tmp) / "cache"
            mc_dir = cache_dir / "logs" / "data" / "Log" / "log1" / "app" / "com.xiaopeng.montecarlo"
            mc_dir.mkdir(parents=True)
            raw = mc_dir / "main_2026-06-17_20-00.alog"
            decoded = mc_dir / "main_2026-06-17_20-00.txt"
            raw.write_bytes(b"raw")
            decoded.write_text("decoded\n", encoding="utf-8")

            log_files = runner._ld_executor_find_log_files(
                cache_dir=cache_dir,
                fault_time="2026-06-17 20:05",
            )

        self.assertEqual(log_files, [decoded])

    def test_needs_skill_confirmation_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(data_dir=Path(tmp), workspace_root=Path(tmp))
            runner = BugAnalysisRunner(config)
            sel = runner._selection_from_plans([BugAnalysisPlan(kind="scene_signal")], source="agent", reason="r")
            sel.confidence = "low"
            self.assertTrue(runner._needs_skill_confirmation(sel))   # low + specific skill -> confirm
            for c in ("high", "medium", ""):
                sel.confidence = c
                self.assertFalse(runner._needs_skill_confirmation(sel))  # only low confirms
            gen = runner._selection_from_plans([BugAnalysisPlan(kind="general")], source="agent", reason="r")
            gen.confidence = "low"
            self.assertFalse(runner._needs_skill_confirmation(gen))  # general handled elsewhere
            sel.confidence = "low"
            config.bug_analysis.confirm_low_confidence_skill = False
            self.assertFalse(runner._needs_skill_confirmation(sel))  # flag off disables gate

    def test_lifecycle_stuck_conflict_requires_skill_confirmation_with_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(data_dir=Path(tmp), workspace_root=Path(tmp))
            runner = BugAnalysisRunner(config)
            selection = runner._selection_from_plans(
                [BugAnalysisPlan(kind="stuck")],
                source="agent",
                reason="SR底图黑屏/不显示内容属于3D渲染黑屏问题",
                provider="claude",
            )
            selection.confidence = "high"

            normalized = runner._downgrade_lifecycle_stuck_conflict(
                selection,
                prompt_text="3D生命周期",
                title="sr底图黑屏，不显示内容",
                description="问题时间：05-19 14:33",
            )
            options = runner._bug_skill_confirmation_options(normalized, request_text="3D生命周期")

        assert normalized.confidence == "low"
        assert "生命周期" in normalized.reason
        assert runner._needs_skill_confirmation(normalized)
        assert options[0]["skill_name"] == "unity-startup-lifecycle-check"
        assert options[1]["skill_name"] == "3d-stuck-investigate"
        assert options[2]["type"] == "plans"
        assert options[2]["plan_kinds"] == ["startup", "stuck"]

    def test_lifecycle_startup_with_stuck_description_requires_skill_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(data_dir=Path(tmp), workspace_root=Path(tmp))
            runner = BugAnalysisRunner(config)
            selection = runner._selection_from_plans(
                [BugAnalysisPlan(kind="startup")],
                source="agent",
                reason="用户明确要求 3D 生命周期。",
                provider="claude",
            )
            selection.confidence = "high"

            normalized = runner._downgrade_lifecycle_stuck_conflict(
                selection,
                prompt_text="3D生命周期",
                title="sr底图黑屏，不显示内容",
                description="问题时间：05-19 14:33",
            )

        assert normalized.confidence == "low"
        assert runner._needs_skill_confirmation(normalized)

    def test_skill_confirmation_result_contains_intent_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(data_dir=Path(tmp), workspace_root=Path(tmp))
            runner = BugAnalysisRunner(config)
            context = create_job_context(Path(tmp))
            selection = runner.selection_for_skill_name(
                "3d-stuck-investigate",
                source="agent",
                reason="黑屏描述命中卡顿 skill，但用户要求 3D 生命周期。",
            )
            assert selection is not None
            selection.confidence = "low"

            result = runner._skill_confirmation_needed_result(
                context=context,
                selection=selection,
                started=time.monotonic(),
                request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6995380113 3D生命周期",
                bug_url="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6995380113",
                title="sr底图黑屏，不显示内容",
                progress_callback=None,
            )

        assert result.details["mode"] == "bug_skill_confirmation"
        assert result.details["needs_user_direction"] is True
        assert result.details["intent_options"]
        assert "1." in result.message
        assert "3D启动" in result.message

    def test_non_lifecycle_low_confidence_confirmation_does_not_show_3d_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(data_dir=Path(tmp), workspace_root=Path(tmp))
            runner = BugAnalysisRunner(config)
            context = create_job_context(Path(tmp))
            selection = runner.selection_for_skill_name(
                "xtheme-analyzer",
                source="agent",
                reason="Agent 认为该请求可能是主题切换问题，但置信度偏低。",
            )
            assert selection is not None
            selection.confidence = "low"

            result = runner._skill_confirmation_needed_result(
                context=context,
                selection=selection,
                started=time.monotonic(),
                request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/7000000001 主题切换后界面异常",
                bug_url="https://project.feishu.cn/xpfailuremgmt/buglo/detail/7000000001",
                title="主题切换后界面异常",
                progress_callback=None,
            )

        assert result.details["mode"] == "bug_skill_confirmation"
        assert "3D启动/Surface生命周期分析" not in result.message
        assert "两个方向都跑" not in result.message
        assert result.details["intent_options"][0]["skill_name"] == "xtheme-analyzer"
        assert result.details["intent_options"][0]["label"] == "XTheme时光主题分析"

    def test_replay_decision_parser_returns_replay_decision(self):
        config = BridgeConfig(dry_run=False)
        runner = IntentAnalysisRunner(config)

        decision = runner._parse_replay_decision(
            json.dumps(
                {
                    "action": "reanalyze",
                    "mode": "bug_analysis",
                    "confidence": "high",
                    "reason": "用户修正了时间",
                    "analysis_kind": "scene_signal",
                    "signal_hint": "SCENE_CHANGED",
                    "retry_download_if_missing": True,
                    "normalized_request_text": "按 10:35 重新分析",
                },
                ensure_ascii=False,
            )
        )

        self.assertEqual(decision.action, "reanalyze")
        self.assertEqual(decision.mode, "bug")
        self.assertEqual(decision.confidence, "high")
        self.assertEqual(decision.analysis_kind, "scene_signal")
        self.assertEqual(decision.signal_hint, "SCENE_CHANGED")
        self.assertTrue(decision.retry_download_if_missing)
        self.assertEqual(decision.normalized_request_text, "按 10:35 重新分析")

    def test_replay_decision_prompt_includes_full_context_and_resources(self):
        config = BridgeConfig(dry_run=False)
        runner = IntentAnalysisRunner(config)
        context = AnalysisReplayContext(
            root_message_id="om_root",
            chat_id="oc_1",
            previous_mode="bug_analysis",
            mode="bug",
            original_request_text="原始请求：分析 bug",
            current_text="重新分析，时间改成 10:35",
            history=[{"role": "user", "content": "之前的问题"}],
            summary_text="旧结论",
            report_excerpt="报告摘录",
            report_url="http://report.local/r/1",
            bug_url="https://bug.example/123",
            bug_title="3D 场景卡住",
            bug_description="bug body",
            resources=ReplayResourceBundle(
                current=[
                    DownloadResource(
                        kind="file",
                        value="https://files.example/current.zip",
                        display_name="current.zip",
                    )
                ],
                reply_chain=[],
                session=[DownloadResource(kind="local", value="/tmp/prepared_logs", display_name="prepared")],
                local_existing=[DownloadResource(kind="local", value="/tmp/prepared_logs", display_name="prepared")],
                remote=[DownloadResource(kind="bug", value="https://bug.example/123")],
                missing_local_values=["/tmp/missing_logs"],
            ),
        )

        prompt = runner._build_replay_prompt(context=context)

        self.assertIn('"previous_mode": "bug_analysis"', prompt)
        self.assertIn('"current_user_text": "重新分析，时间改成 10:35"', prompt)
        self.assertIn('"url": "https://bug.example/123"', prompt)
        self.assertIn('"title": "3D 场景卡住"', prompt)
        self.assertIn("/tmp/prepared_logs", prompt)
        self.assertIn("/tmp/missing_logs", prompt)
        self.assertIn("最新用户文本优先", prompt)
