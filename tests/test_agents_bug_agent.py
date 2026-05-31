from _agents_base import *  # noqa: F401,F403
from _agents_base import _AgentTestBase


class AgentsBugAgentTests(_AgentTestBase):
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
                            "prompt_tokens": 321,
                            "cached_input_tokens": 280,
                            "completion_tokens": 54,
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
        self.assertIn("Agent Token: `321 / 280 / 54 / 375`", metadata_text)
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
