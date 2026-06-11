from _agents_base import *  # noqa: F401,F403
from _agents_base import _AgentTestBase
import tomllib


class AgentsCustomSkillTests(_AgentTestBase):
    def test_file_agent_focus_dir_keeps_navigation_and_key_logd_when_app_noise_exceeds_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = BugAnalysisRunner(BridgeConfig(dry_run=False, data_dir=Path(tmp) / "data"))
            input_path = Path(tmp) / "logs" / "Log" / "log1"
            fault_time = "2026-05-25 16:50:41"
            line = "05-25 16:50:35.000 100 100 I FocusTest: hit\n"

            for index in range(30):
                path = input_path / "app" / f"com.xiaopeng.a{index:02d}" / "user0_main_2026-05-25_16-00.alog.log"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(line, encoding="utf-8")
            nav_log = input_path / "app" / "com.xiaopeng.montecarlo" / "user0_main_2026-05-25_16-00.alog.log"
            nav_log.parent.mkdir(parents=True, exist_ok=True)
            nav_log.write_text(line, encoding="utf-8")
            for name in ("kernel.txt", "main.txt", "events.txt", "crash.txt"):
                path = input_path / "logd" / name
                path.parent.mkdir(parents=True, exist_ok=True)
                content = line
                if name == "main.txt":
                    content = (
                        line * 3
                        + "05-25 16:50:36.000 200 201 I SurfaceFlinger: GSL fence timeout\n"
                    )
                path.write_text(content, encoding="utf-8")

            focus_dir, _, copied = runner._build_file_agent_focus_dir(
                input_path=input_path,
                fault_time=fault_time,
                analysis_kind="app_server_autonomous",
                analysis_dir=Path(tmp) / "analysis",
            )
            assert focus_dir is not None
            copied_rel = {path.relative_to(focus_dir).as_posix() for path in copied}
            focused_main = (focus_dir / "logd" / "main.txt").read_text(encoding="utf-8")

        self.assertIn("app/com.xiaopeng.montecarlo/user0_main_2026-05-25_16-00.alog.log", copied_rel)
        self.assertIn("logd/kernel.txt", copied_rel)
        self.assertIn("logd/main.txt", copied_rel)
        self.assertIn("logd/events.txt", copied_rel)
        self.assertIn("logd/crash.txt", copied_rel)
        self.assertIn("SurfaceFlinger: GSL", focused_main)

    def test_key_logd_survives_when_montecarlo_exceeds_24_files(self):
        """Regression: key logd (kernel/main/events/crash) must not be squeezed
        out when MonteCarlo directory has more than 24 files."""
        with tempfile.TemporaryDirectory() as tmp:
            runner = BugAnalysisRunner(BridgeConfig(dry_run=False, data_dir=Path(tmp) / "data"))
            input_path = Path(tmp) / "logs" / "Log" / "log1"
            fault_time = "2026-05-25 16:50:41"
            line = "05-25 16:50:35.000 100 100 I FocusTest: hit\n"

            # 30 files inside montecarlo alone — exceeds the 24-cap
            mc_dir = input_path / "app" / "com.xiaopeng.montecarlo"
            mc_dir.mkdir(parents=True, exist_ok=True)
            for index in range(30):
                (mc_dir / f"user{index}_main_2026-05-25_16-00.alog.log").write_text(line, encoding="utf-8")
            for index in range(6):
                path = input_path / "logd" / f"kernel.txt.{index:02d}"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(line, encoding="utf-8")
            for name in ("main.txt", "events.txt", "crash.txt"):
                path = input_path / "logd" / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(line, encoding="utf-8")

            focus_dir, _, copied = runner._build_file_agent_focus_dir(
                input_path=input_path,
                fault_time=fault_time,
                analysis_kind="app_server_autonomous",
                analysis_dir=Path(tmp) / "analysis",
            )
            assert focus_dir is not None
            copied_rel = {path.relative_to(focus_dir).as_posix() for path in copied}

        # All 4 key logd files must survive even with 30 montecarlo files
        self.assertTrue(any(path.startswith("logd/kernel.txt") for path in copied_rel))
        self.assertIn("logd/main.txt", copied_rel)
        self.assertIn("logd/events.txt", copied_rel)
        self.assertIn("logd/crash.txt", copied_rel)

    def test_file_agent_context_allows_json_fence_when_prompt_requires_structured_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = BugAnalysisRunner(BridgeConfig(dry_run=False, data_dir=Path(tmp) / "data"))
            analysis_dir = Path(tmp) / "analysis"
            prompt_text = "请输出中文分析，并在末尾输出一个 ```json fenced block，字段必须包括 verdict。"

            context_path = runner._write_file_agent_context(
                analysis_kind="source_stage",
                skill_name="source_analysis",
                request_text=prompt_text,
                prompt_text=prompt_text,
                title="需求源码分析",
                description="",
                fault_time="",
                original_selected_input=None,
                focused_log_input=None,
                log_focus_manifest=None,
                source_evidence_path=None,
                analysis_dir=analysis_dir,
                analysis_markdown_path=analysis_dir / "source_stage_analysis.md",
            )
            context_body = context_path.read_text(encoding="utf-8")
            prompt_body = runner._build_custom_skill_agent_prompt(
                analysis_kind="source_stage",
                skill_name="source_analysis",
                request_text=prompt_text,
                prompt_text=prompt_text,
                title="需求源码分析",
                description="",
                fault_time="",
                selected_input=None,
                prepared_input=None,
                source_evidence_path=None,
                analysis_markdown_path=analysis_dir / "source_stage_analysis.md",
                context_path=context_path,
                writes_output_file=True,
            )

            self.assertIn("允许在末尾输出该 JSON 代码块", context_body)
            self.assertIn("允许在末尾输出该 JSON 代码块", prompt_body)
            self.assertNotIn("不要输出代码块围栏", context_body)
            self.assertNotIn("不要输出代码块围栏", prompt_body)

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
    def test_source_evidence_terms_prioritize_request_and_title_business_terms_over_description_noise(self):
        runner = BugAnalysisRunner(BridgeConfig(dry_run=False))

        terms = runner._source_evidence_terms(
            plans=[BugAnalysisPlan(kind="scene_signal"), BugAnalysisPlan(kind="source_stage")],
            request_text=(
                "问题时间 2025-05-10 14:30:00 源码分析 3D场景信号分析 "
                "[ [缺陷] 2026-05-25 16:50:41 "
                "【d03】6.2.3】拾光主题切换成天玑主题，进入场景模式没有展示3D场景]"
            ),
            followup_text="问题时间 2025-05-10 14:30:00 源码分析 3D场景信号分析",
            extra_texts=(
                "2026-05-25 16:50:41 【d03】6.2.3】拾光主题切换成天玑主题，进入场景模式没有展示3D场景",
                (
                    "环境信息：\n"
                    "UUID 000000UIQEOB3ZIZ0RJLNIETRT7Q625X\n"
                    "ROM Version XMARTM3ZHD03E1_V6.2.3.1821_20260525034947.0_REV01_USER_DEV\n"
                    "SDK_VERSION(android.9.810.27.0.346)\n"
                    "ENGINE_VERSION(AutoEngine_13.16.40.3795_8694_LVJ17902)\n"
                ),
            ),
        )

        self.assertIn("进入场景模式", terms)
        self.assertIn("场景模式", terms)
        self.assertIn("场景信号", terms)
        self.assertNotIn("Number", terms)
        self.assertNotIn("SDK_VERSION", terms)
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
        self.assertIn("## 5.1 CodeGraph 语义检索", context_text)
        self.assertIn("codegraph status <源码根>", context_text)
        self.assertIn("codegraph query", context_text)
        self.assertIn("codegraph callers", context_text)
        self.assertIn("codegraph context", context_text)
        self.assertIn("--max-nodes 30", context_text)
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
        self.assertIn("codegraph status <源码根>", prompt)
        self.assertIn("codegraph query", prompt)
        self.assertIn("codegraph callers", prompt)
        self.assertIn("codegraph context", prompt)
        self.assertIn("--max-nodes 30", prompt)
    def test_custom_skill_agent_command_for_codex_uses_source_root_as_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            source_root = Path(tmp) / "guideengine"
            source_root.mkdir(parents=True)
            config = BridgeConfig(dry_run=False, workspace_root=workspace, data_dir=Path(tmp) / "data")
            config.bug_analysis.provider = "codex"
            config.bug_analysis.command = "codex"
            config.source_investigation.repo_roots = [source_root]
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

        self.assertEqual(Path(invocation["cwd"]), source_root.resolve())
        command = invocation["command"]
        self.assertEqual(command[command.index("-C") + 1], str(source_root.resolve()))
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
        self.assertIn(result.error_code, {"custom_skill_executor_not_ready", "source_code_skill_executor_not_ready"})
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
    def test_ld_executor_find_log_files_supports_flat_backslash_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = BugAnalysisRunner(
                BridgeConfig(dry_run=False, data_dir=Path(tmp) / "data", workspace_root=Path(tmp))
            )
            cache_dir = Path(tmp) / "bug_cache"
            logs_dir = cache_dir / "logs"
            logs_dir.mkdir(parents=True)
            flat_raw = logs_dir / r"ALLlog\log0\app\com.xiaopeng.montecarlo\main_2026-05-29_11-00.alog"
            flat_decoded = logs_dir / r"ALLlog\log0\app\com.xiaopeng.montecarlo\main_2026-05-29_11-00.alog.log"
            outside_window = logs_dir / r"ALLlog\log0\app\com.xiaopeng.montecarlo\main_2026-05-29_14-00.alog"
            flat_raw.write_bytes(b"raw")
            flat_decoded.write_text("decoded", encoding="utf-8")
            outside_window.write_bytes(b"later")

            files = runner._ld_executor_find_log_files(
                cache_dir=cache_dir,
                fault_time="2026-05-29 11:12",
            )

        self.assertEqual(files, [flat_decoded])
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
            progress_events = []
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
                    progress_callback=progress_events.append,
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
                self.assertEqual(progress_events[-1]["stage"], "source_code_skill_agent_failed")
                self.assertEqual(
                    progress_events[-1]["details"]["error_code"],
                    "custom_skill_agent_missing_output",
                )
    def test_bug_ld_direct_api_fallback_progress_includes_error_details(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            data_dir = Path(tmp) / "data"
            skill_name = "ld-lane-level-log-analysis-portable"
            skill_dir = root / ".ai" / "skills" / skill_name
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: LD Lane Level Log Analysis\ndescription: 分析车道级、退无图和 LD 状态日志。\n---\n",
                encoding="utf-8",
            )
            config = BridgeConfig(dry_run=False, data_dir=data_dir, workspace_root=root)
            config.bug_analysis.provider = "codex"
            config.bug_analysis.command = "codex"
            runner = BugAnalysisRunner(config)
            log_root = Path(tmp) / "logs"
            self._write_matching_log(log_root, "2026-05-29 11:12:00")
            selection = runner.selection_for_skill_name(
                skill_name,
                source="agent",
                reason="LD 退无图命中车道级内建 Skill",
                provider="codex",
            )
            assert selection is not None
            progress_events = []

            def fake_run_json_command(command, timeout):
                if "check-env" in command:
                    return {"meegle_installed": True, "auth_ok": True}
                if "resolve-url" in command:
                    return {"project_key": "xpfailuremgmt", "work_item_id": "7003428840"}
                if "fetch-data" in command:
                    return {
                        "title": "车道级导航不进",
                        "status": "处理中",
                        "create_time": "2026-05-29 11:00",
                        "create_by": "tester",
                        "fields": {},
                        "attachments": [{"name": "Log.zip", "size": "12MB"}],
                        "description": "问题时间: 2026-05-29 11:12\n未进车道级。",
                    }
                if command[:3] == ["meegle", "workitem", "get"]:
                    return {"data": {}}
                raise AssertionError(f"unexpected command: {command}")

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
                mock.patch.object(
                    runner,
                    "_run_ld_pydantic_ai_analysis",
                    return_value={
                        "ok": False,
                        "error_code": "pydantic_ai_invalid_evidence",
                        "message": "pydantic-ai 输出缺少关键证据",
                    },
                ),
                mock.patch.object(
                    runner,
                    "_run_ld_direct_api_analysis",
                    return_value={
                        "ok": False,
                        "error_code": "ld_direct_api_error",
                        "message": "LD 车道级分析 API 调用失败：upstream forbidden",
                    },
                ),
                mock.patch.object(
                    runner,
                    "_run_custom_skill_agent_analysis",
                    return_value={
                        "ok": False,
                        "error_code": "custom_skill_agent_missing_output",
                        "message": "专用 Skill 文件 Agent 未生成结果",
                        "stdout": "",
                        "stderr": "",
                    },
                ),
            ):
                result = runner.run_bug_analysis(
                    BugRequest(
                        bug_url="https://project.feishu.cn/xpfailuremgmt/buglo/detail/7003428840",
                        prompt="未进车道级",
                        raw_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/7003428840 未进车道级",
                        triggered=True,
                    ),
                    progress_callback=progress_events.append,
                )

        self.assertFalse(result.success)
        fallback_event = next(item for item in progress_events if item["stage"] == "ld_direct_api_fallback")
        self.assertEqual(fallback_event["details"]["error_code"], "ld_direct_api_error")
        self.assertEqual(
            fallback_event["details"]["error_message"],
            "LD 车道级分析 API 调用失败：upstream forbidden",
        )
    def test_ld_pydantic_ai_analysis_is_bounded_to_prepared_log_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            skill_name = "ld-lane-level-log-analysis-portable"
            skill_dir = root / ".ai" / "skills" / skill_name
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: LD Lane Level Log Analysis\ndescription: 分析车道级、退无图和 LD 状态日志。\n---\n",
                encoding="utf-8",
            )
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp) / "data", workspace_root=root)
            config.source_investigation.repo_roots = [root]
            runner = BugAnalysisRunner(config)
            prepared_dir = Path(tmp) / "job_input"
            prepared_dir.mkdir()
            prepared_input = prepared_dir / "main_2026-05-29_17-00.alog"
            decoded_input = prepared_dir / "main_2026-05-29_17-00.alog.log"
            sibling_input = prepared_dir / "main_2026-05-29_18-00.alog.log"
            prepared_input.write_bytes(b"raw")
            decoded_input.write_text("decoded", encoding="utf-8")
            sibling_input.write_text("later", encoding="utf-8")
            analysis_dir = Path(tmp) / "analysis"
            html_path = Path(tmp) / "report.html"
            json_path = Path(tmp) / "report.json"

            class FakeRuntimeResult:
                ok = True
                markdown = (
                    "## 结论摘要\n\n已收敛。\n\n"
                    "## 最可能原因\n\n主日志命中 LDConf。\n\n"
                    "## 关键证据\n\n"
                    f"- **{decoded_input}:12**: LDConf: False\n\n"
                    "## 待确认项\n\n- 无\n\n"
                    "## 建议动作\n\n- 继续确认。\n"
                )
                model = "mimo-v2.5-pro"
                duration_seconds = 1.2
                usage = {"total_tokens": 12}
                runtime_path = "pydantic_ai_agent"
                tool_calls = 1
                tool_trace = [{"tool": "read_prepared_log_metadata", "args": "{}"}]
                error = ""
                error_code = ""

            class FakeRuntime:
                init_kwargs: dict[str, object] = {}
                last_kwargs: dict[str, object] = {}

                def __init__(self, options, *, workspace=None, max_retries=2):
                    type(self).init_kwargs = {"workspace": workspace, "max_retries": max_retries}

                def is_available(self):
                    return True

                def run(self, **kwargs):
                    type(self).last_kwargs = kwargs
                    return FakeRuntimeResult()

            with mock.patch("lark_agent_bridge.agents.agent_runtime.AgentRuntime", FakeRuntime):
                result = runner._run_ld_pydantic_ai_analysis(
                    skill_name=skill_name,
                    request_text="问题时间 2026-05-29 17:16，分析车道级",
                    prompt_text="问题时间 2026-05-29 17:16，分析车道级",
                    title="车道级不进",
                    description="",
                    fault_time="2026-05-29 17:16",
                    selected_input=prepared_input,
                    prepared_input=prepared_input,
                    html_path=html_path,
                    json_path=json_path,
                    analysis_dir=analysis_dir,
                    progress_callback=None,
                )
                metadata_path = FakeRuntime.last_kwargs["log_metadata_path"]
                metadata_text = metadata_path.read_text(encoding="utf-8")
                user_prompt = FakeRuntime.last_kwargs["user_prompt"]

        self.assertTrue(result["ok"])
        self.assertEqual(FakeRuntime.init_kwargs["workspace"], root.resolve())
        self.assertTrue(FakeRuntime.last_kwargs["strict_tools"])
        self.assertEqual(FakeRuntime.last_kwargs["report_dir"], analysis_dir.parent)
        self.assertIn(str(prepared_input), metadata_text)
        self.assertIn(str(decoded_input), metadata_text)
        self.assertIn("必须先调用 read_prepared_log_metadata()", user_prompt)
        self.assertIn(str(prepared_input), user_prompt)
        self.assertIn("禁止扫描 bug_cache 以外的历史 job 目录", user_prompt)
        self.assertIn(prepared_dir, FakeRuntime.last_kwargs["extra_roots"])
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
    def test_custom_skill_file_agent_uses_codex_app_server_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            skill_name = "app-server-custom-skill"
            skill_dir = root / ".ai" / "skills" / skill_name
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: App Server Custom Skill\ndescription: app-server test.\n---\n\n# App Server\n",
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
            config.source_investigation.repo_roots = [root]
            runner = BugAnalysisRunner(config)
            log_root = Path(tmp) / "logs"
            self._write_matching_log(log_root, "2026-05-25 16:50:41")
            progress_events: list[dict[str, object]] = []
            runtime = mock.Mock()
            runtime.run_turn.return_value = CodexAppServerResult(
                ok=True,
                final_text=(
                    "## 结论摘要\n- app-server 已完成。\n\n"
                    "## 关键证据\n- L1: scene evidence\n\n"
                    "## 待确认项\n- 无\n\n"
                    "## 建议动作\n- 继续验证。\n"
                ),
                command=["codex", "app-server", "-c", 'sandbox_mode=\"read-only\"'],
                stdout="Codex command rg --line-number 3D场景",
                stderr="",
                thread_id="thread-app-server",
                turn_id="turn-app-server",
                duration_seconds=4.2,
                events=[
                    {"method": "turn/started", "params": {"turn": {"id": "turn-app-server"}}},
                    {"method": "turn/completed", "params": {"turn": {"status": "completed"}}},
                ],
                usage={"totalTokens": 2468, "inputTokens": 2000, "outputTokens": 468},
                completion_state=CompletionState.COMPLETE,
            )

            with (
                mock.patch(
                    "lark_agent_bridge.agents.bug.custom_skill.check_codex_app_server_available",
                    return_value=(True, "0.134.0"),
                ),
                mock.patch(
                    "lark_agent_bridge.agents.bug.custom_skill.CodexAppServerRuntime",
                    return_value=runtime,
                ) as runtime_cls,
                mock.patch("lark_agent_bridge.agents.run_tracked_process") as run_mock,
                mock.patch.object(
                    runner,
                    "_prepare_codex_app_server_minimal_home",
                    return_value=Path(tmp) / "codex_home",
                ),
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
                    progress_callback=progress_events.append,
                    timeout=30,
                )
                analysis_text = Path(result["analysis_markdown_path"]).read_text(encoding="utf-8")
                event_audit_text = Path(result["events_path"]).read_text(encoding="utf-8")

        self.assertTrue(result["ok"])
        self.assertEqual(result["executor"], "codex_app_server")
        self.assertEqual(result["completion_state"], "complete")
        self.assertEqual(result["usage"]["total_tokens"], 2468)
        self.assertEqual(result["usage"]["input_tokens"], 2000)
        self.assertEqual(result["usage"]["output_tokens"], 468)
        self.assertEqual(result["thread_id"], "thread-app-server")
        self.assertIn("app-server 已完成", analysis_text)
        self.assertIn("\"method\": \"turn/completed\"", event_audit_text)
        self.assertTrue(any("Codex" in str(event.get("message") or "") for event in progress_events))
        self.assertEqual(Path(runtime_cls.call_args.kwargs["cwd"]), root.resolve())
        self.assertTrue(runtime_cls.call_args.kwargs["disable_node_repl"])
        self.assertFalse(runtime_cls.call_args.kwargs["emit_node_repl_flag"])
        self.assertTrue(runtime_cls.call_args.kwargs["disable_analytics"])
        self.assertTrue(runtime_cls.call_args.kwargs["disable_memories"])
        self.assertTrue(runtime_cls.call_args.kwargs["disable_apps_feature"])
        self.assertTrue(runtime_cls.call_args.kwargs["disable_plugins_feature"])
        self.assertTrue(runtime_cls.call_args.kwargs["disable_computer_use_feature"])
        self.assertEqual(runtime_cls.call_args.kwargs["reasoning_effort"], "medium")
        run_mock.assert_not_called()

    def test_app_server_progress_aggregates_delta_chunks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            skill_name = "app-server-custom-skill"
            skill_dir = root / ".ai" / "skills" / skill_name
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: App Server Custom Skill\ndescription: app-server test.\n---\n\n# App Server\n",
                encoding="utf-8",
            )
            config = BridgeConfig(
                dry_run=False,
                data_dir=Path(tmp) / "data",
                workspace_root=root,
                codex_app_server=CodexAppServerOptions(enabled=True, use_for_file_agent=True, fallback_to_exec=True),
            )
            config.bug_analysis.provider = "codex"
            config.bug_analysis.command = "codex"
            config.source_investigation.repo_roots = [root]
            runner = BugAnalysisRunner(config)
            log_root = Path(tmp) / "logs"
            self._write_matching_log(log_root, "2026-05-25 16:50:41")
            progress_events: list[dict[str, object]] = []
            runtime = mock.Mock()

            def _run_turn(_prompt_text, *, on_event=None):
                if on_event is not None:
                    on_event({"method": "item/agentMessage/delta", "params": {"itemId": "msg-1", "delta": "故"}})
                    on_event({"method": "item/agentMessage/delta", "params": {"itemId": "msg-1", "delta": "障"}})
                    on_event({"method": "item/agentMessage/delta", "params": {"itemId": "msg-1", "delta": "正常。"}})
                    on_event(
                        {
                            "method": "item/started",
                            "params": {"item": {"type": "commandExecution", "command": "/bin/zsh -lc \"rg scene\""}},
                        }
                    )
                return CodexAppServerResult(
                    ok=True,
                    final_text="## 结论摘要\n- ok\n\n## 关键证据\n- L1\n",
                    command=["codex", "app-server"],
                    stdout="Codex command rg scene",
                    stderr="",
                    thread_id="thread-app-server",
                    turn_id="turn-app-server",
                    duration_seconds=2.0,
                    events=[],
                    usage={"totalTokens": 10, "inputTokens": 8, "outputTokens": 2},
                    completion_state=CompletionState.COMPLETE,
                )

            runtime.run_turn.side_effect = _run_turn

            with (
                mock.patch(
                    "lark_agent_bridge.agents.bug.custom_skill.check_codex_app_server_available",
                    return_value=(True, "0.134.0"),
                ),
                mock.patch(
                    "lark_agent_bridge.agents.bug.custom_skill.CodexAppServerRuntime",
                    return_value=runtime,
                ),
                mock.patch.object(
                    runner,
                    "_prepare_codex_app_server_minimal_home",
                    return_value=Path(tmp) / "codex_home",
                ),
            ):
                runner._run_custom_skill_agent_analysis(
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
                    progress_callback=progress_events.append,
                    timeout=30,
                )

        messages = [str(event.get("message") or "") for event in progress_events]
        self.assertTrue(any("Codex 文本 故障正常。" in message for message in messages))
        self.assertFalse(any("Codex delta 故" in message for message in messages))

    def test_app_server_policy_honors_disable_node_repl_under_minimal_home(self):
        # Regression: 1219603 flipped disable_node_repl to False whenever
        # use_minimal_home prepared a home, silently overriding user config.
        # Root cause: minimal home config.toml has no node_repl section, so the
        # disable FLAG is meaningless there; the user's value must still be honored.
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(data_dir=Path(tmp), workspace_root=Path(tmp))
            config.codex_app_server = CodexAppServerOptions(
                enabled=True, use_for_file_agent=True,
                disable_node_repl=True, use_minimal_home=True,
            )
            runner = BugAnalysisRunner(config)
            with mock.patch.object(
                runner, "_prepare_codex_app_server_minimal_home",
                return_value=Path(tmp) / "codex_home",
            ):
                policy = runner.build_codex_app_server_execution_policy(cwd=Path(tmp), timeout=300)
        self.assertTrue(policy.disable_node_repl)       # config honored
        self.assertFalse(policy.emit_node_repl_flag)     # flag not emitted under minimal home
        self.assertIsInstance(policy.env, dict)
        self.assertNotEqual(policy.env, {})              # never falls back to inherit-all
        self.assertEqual(policy.env.get("CODEX_HOME"), str(Path(tmp) / "codex_home"))

    def test_minimal_home_injects_bridge_codegraph_mcp(self):
        with tempfile.TemporaryDirectory() as tmp:
            root_a = Path(tmp) / "guideengine"
            root_b = Path(tmp) / "Napa5"
            root_a.mkdir()
            root_b.mkdir()
            config = BridgeConfig(data_dir=Path(tmp) / "data", workspace_root=Path(tmp))
            config.source_investigation.codegraph_enabled = True
            config.source_investigation.codegraph_command = "codegraph-test"
            config.source_investigation.codegraph_timeout_seconds = 7
            config.source_investigation.repo_roots = [root_a, root_b]
            runner = BugAnalysisRunner(config)

            body = "\n".join(runner._codex_app_server_codegraph_mcp_config_lines())
            parsed = tomllib.loads(body)

        server = parsed["mcp_servers"]["bridge_codegraph"]
        self.assertEqual(server["command"], sys.executable)
        self.assertEqual(server["args"], ["-m", "lark_agent_bridge.mcp_codegraph_server"])
        env = server["env"]
        self.assertIn(str(root_a.resolve()), env["LARK_AGENT_BRIDGE_CODEGRAPH_ROOTS"].splitlines())
        self.assertIn(str(root_b.resolve()), env["LARK_AGENT_BRIDGE_CODEGRAPH_ROOTS"].splitlines())
        self.assertEqual(env["LARK_AGENT_BRIDGE_CODEGRAPH_COMMAND"], "codegraph-test")
        self.assertEqual(env["LARK_AGENT_BRIDGE_CODEGRAPH_TIMEOUT_SECONDS"], "7.0")
    def test_codex_app_server_reinjects_proxy_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            skill_name = "app-server-proxy-skill"
            skill_dir = root / ".ai" / "skills" / skill_name
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: App Server Proxy Skill\ndescription: proxy env test.\n---\n\n# Proxy\n",
                encoding="utf-8",
            )
            config = BridgeConfig(
                dry_run=False,
                data_dir=Path(tmp) / "data",
                workspace_root=root,
                internal_network_env=InternalNetworkEnvOptions(
                    inherit_env=["PATH", "HOME"],
                    unset_env=["HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy"],
                ),
                codex_app_server=CodexAppServerOptions(
                    enabled=True,
                    use_for_file_agent=True,
                    fallback_to_exec=True,
                    preserve_proxy_env=True,
                ),
            )
            config.bug_analysis.provider = "codex"
            config.bug_analysis.command = "codex"
            config.source_investigation.repo_roots = [root]
            runner = BugAnalysisRunner(config)
            log_root = Path(tmp) / "logs"
            self._write_matching_log(log_root, "2026-05-25 16:50:41")
            runtime = mock.Mock()
            runtime.run_turn.return_value = CodexAppServerResult(
                ok=True,
                final_text="## 结论摘要\n- ok\n\n## 关键证据\n- L1\n",
            )

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
                mock.patch(
                    "lark_agent_bridge.agents.bug.custom_skill.check_codex_app_server_available",
                    return_value=(True, "0.134.0"),
                ),
                mock.patch(
                    "lark_agent_bridge.agents.bug.custom_skill.CodexAppServerRuntime",
                    return_value=runtime,
                ) as runtime_cls,
            ):
                runner._run_custom_skill_agent_analysis(
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

        env = runtime_cls.call_args.kwargs["env"]
        self.assertEqual(env.get("HTTP_PROXY"), "http://127.0.0.1:7897")
        self.assertEqual(env.get("HTTPS_PROXY"), "http://127.0.0.1:7897")
        self.assertEqual(env.get("NO_PROXY"), "localhost")
