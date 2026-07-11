import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from lark_agent_bridge.app_server_investigation import AppServerInvestigationRunner, PreparedAppServerInvestigation, _detect_selected_skill
from lark_agent_bridge.models import AppServerInvestigationOptions, AppServerInvestigationRequest, BugAnalysisOptions, BridgeConfig, CodexAppServerOptions, DownloadResource, DownloadedResource, create_job_context
from lark_agent_bridge.skill_manager import SkillManager
from tests._app_base import event


class FakeBugRunnerForAppServer:
    def __init__(self):
        self.prompts = []
        self.result_override = None

    def _emit_progress(self, progress_callback, **payload):
        if progress_callback is not None:
            progress_callback(payload)

    def _run_custom_skill_agent_via_codex_app_server(
        self,
        *,
        analysis_kind,
        skill_name,
        prompt_text,
        cwd,
        command_path,
        stdout_path,
        stderr_path,
        events_path,
        progress_callback,
        timeout,
        bridge_session_id,
        model_override="",
        reasoning_effort_override="",
    ):
        self.prompts.append(
            {
                "analysis_kind": analysis_kind,
                "skill_name": skill_name,
                "prompt_text": prompt_text,
                "cwd": cwd,
                "timeout": timeout,
                "bridge_session_id": bridge_session_id,
                "model_override": model_override,
                "reasoning_effort_override": reasoning_effort_override,
            }
        )
        stdout_path.write_text("stdout", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        command_path.write_text("[]", encoding="utf-8")
        events_path.write_text("", encoding="utf-8")
        if self.result_override is not None:
            return dict(self.result_override)
        return {
            "ok": True,
            "stdout": "## 结论摘要\n3D 生命周期卡在 displayChanged 之后。",
            "stderr": "",
            "final_text": "## 结论摘要\n3D 生命周期卡在 displayChanged 之后。\n\n## 关键证据\n- displayChanged 后无后续。\n",
            "command": ["codex", "app-server"],
            "thread_id": "thread_1",
            "turn_id": "turn_1",
            "app_server_version": "0.125.0",
            "usage": {
                "input_tokens": 1200,
                "cached_input_tokens": 800,
                "output_tokens": 120,
                "total_tokens": 1320,
            },
        }


class AppServerInvestigationRunnerTests(unittest.TestCase):
    def test_bug_url_with_explicit_resource_prefers_replied_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            explicit_log = root / "BugLog.alog"
            explicit_log.write_text("2026-07-03 15:11:00 explicit", encoding="utf-8")
            bug_log = root / "BugAttachment.alog"
            bug_log.write_text("2026-07-03 15:11:00 bug", encoding="utf-8")
            resource = DownloadResource(
                kind="file",
                value="file_v3_bug_log",
                source_message_id="om_group_log",
                display_name="BugLog.alog",
            )
            config = BridgeConfig(dry_run=False, data_dir=root / "data")
            bug_runner = SimpleNamespace()
            bug_runner.signal_resolver = SimpleNamespace(_load_catalog=lambda: None)
            bug_runner._request_text = lambda **kwargs: kwargs["raw_text"]
            bug_runner._bridge_session_id = lambda event: ""
            bug_runner._emit_progress = lambda callback, **payload: None
            bug_runner._bug_fetcher_script = lambda: Path("bug-fetcher")
            bug_runner._run_json_command = lambda command, **kwargs: (
                {"meegle_installed": True, "auth_ok": True}
                if "check-env" in command
                else {"project_key": "xpfailuremgmt", "work_item_id": "1"}
                if "resolve-url" in command
                else {"title": "3D异常", "description": "问题时间 2026-07-03 15:11", "attachments": []}
                if "fetch-data" in command
                else {}
            )
            bug_runner._bug_cache_dir = lambda *args: root / "bug_cache"
            bug_runner._is_bug_cache_fresh = lambda *args, **kwargs: True
            bug_runner._remove_tree = lambda *args: None
            bug_runner._bug_description = lambda fetched: str(fetched.get("description") or "")
            bug_runner._bug_reference_time = lambda *args: None
            bug_runner._resolve_bug_time_context = lambda **kwargs: SimpleNamespace(
                has_full_datetime=True,
                fault_time="2026-07-03 15:11:00",
                source="request",
                note="",
            )
            bug_runner._select_log_input = lambda *args: bug_log
            bug_runner._has_bug_cache_content = lambda *args: False
            bug_runner._download_bug_attachments = lambda *args, **kwargs: {"ok": True}
            bug_runner._reuse_prepared_bug_input = lambda path: None
            bug_runner._prepare_log_input = lambda path: path
            bug_runner._scan_log_time_coverage = lambda *args, **kwargs: SimpleNamespace(
                has_time_evidence=True,
                covers_fault_time=True,
            )
            bug_runner._build_file_agent_focus_dir = lambda **kwargs: (kwargs["input_path"], None, None)
            downloader = SimpleNamespace(
                download_all=lambda resources, **kwargs: [DownloadedResource(resource=resource, path=explicit_log)]
            )
            runner = AppServerInvestigationRunner(
                config,
                bug_runner=bug_runner,
                skill_manager=SkillManager(config),
                downloader=downloader,
            )

            prepared = runner._prepare_bug_request(
                AppServerInvestigationRequest(
                    prompt="调查3D异常",
                    bug_url="https://project.feishu.cn/xpfailuremgmt/buglo/detail/1",
                    resources=[resource],
                    raw_text="自主分析 2026-07-03 15:11 调查3D异常",
                    triggered=True,
                ),
                event=event(message_id="om_bug_with_file"),
                progress_callback=None,
            )

        self.assertIsInstance(prepared, PreparedAppServerInvestigation)
        assert isinstance(prepared, PreparedAppServerInvestigation)
        self.assertEqual(prepared.selected_input, explicit_log)

    def test_detect_selected_skill_ignores_business_log_words(self):
        markdown = (
            "## 结论摘要\n"
            "- Primary skill: `scene-signal-diagnosis`。\n"
            "## 关键证据\n"
            "- 日志显示命中 SKILL isNeedAutoFold，但这只是业务字段。\n"
        )
        inventory = {"skills": [{"name": "scene-signal-diagnosis"}, {"name": "unity-startup-lifecycle-check"}]}

        self.assertEqual(_detect_selected_skill(markdown, {}, inventory), "scene-signal-diagnosis")
        self.assertEqual(
            _detect_selected_skill("## 关键证据\n- 日志显示命中 SKILL isNeedAutoFold。\n", {}, inventory),
            "",
        )

    def test_runner_uses_config_prompt_and_writes_context_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            skills_root = root / ".ai" / "skills"
            skills_root.mkdir(parents=True, exist_ok=True)
            for name in ("scene-signal-diagnosis", "3d-stuck-investigate"):
                skill_dir = skills_root / name
                skill_dir.mkdir(parents=True, exist_ok=True)
                content = f"# {name}\n"
                if name == "scene-signal-diagnosis":
                    content = (
                        "---\n"
                        "name: scene-signal-diagnosis\n"
                        "description: scene signal\n"
                        "report_requires_android_unity_boundary: true\n"
                        "report_primary_log_globs: app/com.xiaopeng.montecarlo/*\n"
                        "---\n\n"
                        "# scene-signal-diagnosis\n"
                    )
                (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
            guideengine = root / "guideengine"
            guideengine.mkdir(parents=True, exist_ok=True)
            prepared_logs = Path(tmp) / "prepared_logs"
            prepared_logs.mkdir(parents=True, exist_ok=True)

            config = BridgeConfig(
                dry_run=False,
                workspace_root=root,
                guideengine_repo=guideengine,
                data_dir=Path(tmp) / "data",
                source_investigation=SimpleNamespace(repo_roots=[guideengine]),
                bug_analysis=BugAnalysisOptions(
                    app_server_investigation=AppServerInvestigationOptions(
                        enabled=True,
                        prompt_template=(
                            "CTX={context_path}\n"
                            "CTX_JSON={context_json_path}\n"
                            "INV={skill_inventory_path}\n"
                            "INV_JSON={skill_inventory_json_path}\n"
                            "OUT={output_path}\n"
                            "PREP={prepared_input}\n"
                            "FOCUS={focused_log_input}\n"
                        ),
                    )
                ),
                codex_app_server=CodexAppServerOptions(enabled=True, command="codex", turn_timeout_seconds=321),
            )
            bug_runner = FakeBugRunnerForAppServer()
            runner = AppServerInvestigationRunner(config, bug_runner=bug_runner, skill_manager=SkillManager(config))
            context = create_job_context(config.data_dir, event=event(event_id="evt1", chat_id="oc", message_id="om"))
            analysis_dir = context.output_dir / "app_server_investigation"
            analysis_dir.mkdir(parents=True, exist_ok=True)
            prepared = PreparedAppServerInvestigation(
                context=context,
                request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/1 全技能分析 调查 3D 生命周期",
                prompt_text="调查 3D 生命周期",
                bug_url="https://project.feishu.cn/xpfailuremgmt/buglo/detail/1",
                title="3D 生命周期异常",
                description="displayChanged 后没有后续回调",
                trigger_mode="free",
                trigger_term="全技能分析",
                source_roots=[guideengine],
                fault_time="2026-05-25 16:50:41",
                fault_time_source="bug_title",
                fault_time_note="from title",
                selected_input=prepared_logs,
                prepared_input=prepared_logs,
                focused_log_input=prepared_logs,
                log_focus_manifest=analysis_dir / "log_focus.md",
                bridge_session_id="bridge_1",
                analysis_dir=analysis_dir,
            )
            runner._prepare_bug_request = lambda *args, **kwargs: prepared
            request = AppServerInvestigationRequest(
                prompt="调查 3D 生命周期",
                bug_url="https://project.feishu.cn/xpfailuremgmt/buglo/detail/1",
                raw_text="@bot 全技能分析 https://project.feishu.cn/xpfailuremgmt/buglo/detail/1 调查 3D 生命周期",
                triggered=True,
                trigger_mode="free",
                trigger_term="全技能分析",
            )

            result = runner.run(request, event=event(event_id="evt1", chat_id="oc", message_id="om"))

            self.assertTrue(result.success)
            self.assertEqual(result.details["mode"], "app_server_investigation")
            self.assertTrue(Path(result.details["analysis_markdown_path"]).is_file())
            self.assertTrue(Path(result.details["context_path"]).is_file())
            self.assertTrue(Path(result.details["skill_inventory_path"]).is_file())
            self.assertEqual(result.details["app_server_input_tokens"], 1200)
            self.assertEqual(result.details["app_server_cached_input_tokens"], 800)
            self.assertEqual(result.details["app_server_output_tokens"], 120)
            self.assertEqual(result.details["app_server_total_tokens"], 1320)
            prompt = bug_runner.prompts[0]["prompt_text"]
            self.assertIn("CTX=", prompt)
            self.assertIn("INV=", prompt)
            self.assertIn("OUT=", prompt)
            self.assertIn("PREP=", prompt)
            self.assertIn("FOCUS=", prompt)
            self.assertEqual(bug_runner.prompts[0]["timeout"], 321)
            inventory = json.loads(Path(result.details["skill_inventory_json_path"]).read_text(encoding="utf-8"))
            scene = next(item for item in inventory["skills"] if item["name"] == "scene-signal-diagnosis")
            self.assertTrue(scene["report_contract"]["requires_android_unity_boundary"])
            self.assertEqual(scene["report_contract"]["primary_log_globs"], ["app/com.xiaopeng.montecarlo/*"])
            inventory_md = Path(result.details["skill_inventory_path"]).read_text(encoding="utf-8")
            self.assertIn("报告契约", inventory_md)
            self.assertIn("requires_android_unity_boundary", inventory_md)

    def test_failed_runner_retains_app_server_usage_details(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            skills_root = root / ".ai" / "skills"
            skills_root.mkdir(parents=True, exist_ok=True)
            (skills_root / "3d-stuck-investigate").mkdir(parents=True, exist_ok=True)
            (skills_root / "3d-stuck-investigate" / "SKILL.md").write_text("# skill\n", encoding="utf-8")
            guideengine = root / "guideengine"
            guideengine.mkdir(parents=True, exist_ok=True)
            prepared_logs = Path(tmp) / "prepared_logs"
            prepared_logs.mkdir(parents=True, exist_ok=True)

            config = BridgeConfig(
                dry_run=False,
                workspace_root=root,
                guideengine_repo=guideengine,
                data_dir=Path(tmp) / "data",
                source_investigation=SimpleNamespace(repo_roots=[guideengine]),
                bug_analysis=BugAnalysisOptions(
                    app_server_investigation=AppServerInvestigationOptions(
                        enabled=True,
                        prompt_template="OUT={output_path}\n",
                    )
                ),
                codex_app_server=CodexAppServerOptions(enabled=True, command="codex"),
            )
            bug_runner = FakeBugRunnerForAppServer()
            bug_runner.result_override = {
                "ok": False,
                "message": "codex app-server turn timed out after 600.0s",
                "error_code": "codex_app_server_turn_timeout",
                "stdout": "Codex token usage token≈2621677 input=2606367 cache=2500000 output=15310",
                "stderr": "",
                "app_server_version": "0.136.0",
                "thread_id": "thread-timeout",
                "turn_id": "turn-timeout",
                "usage": {
                    "totalTokens": 2621677,
                    "inputTokens": 2606367,
                    "cachedInputTokens": 2500000,
                    "outputTokens": 15310,
                },
            }
            runner = AppServerInvestigationRunner(config, bug_runner=bug_runner, skill_manager=SkillManager(config))
            context = create_job_context(config.data_dir, event=event(event_id="evt-timeout", chat_id="oc", message_id="om-timeout"))
            analysis_dir = context.output_dir / "app_server_investigation"
            analysis_dir.mkdir(parents=True, exist_ok=True)
            prepared = PreparedAppServerInvestigation(
                context=context,
                request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/1 自主分析",
                prompt_text="",
                bug_url="https://project.feishu.cn/xpfailuremgmt/buglo/detail/1",
                title="3D 生命周期异常",
                description="displayChanged 后没有后续回调",
                trigger_mode="free",
                trigger_term="自主分析",
                source_roots=[guideengine],
                fault_time="2026-05-25 16:50:41",
                fault_time_source="bug_title",
                fault_time_note="from title",
                selected_input=prepared_logs,
                prepared_input=prepared_logs,
                focused_log_input=prepared_logs,
                log_focus_manifest=analysis_dir / "log_focus.md",
                bridge_session_id="bridge_timeout",
                analysis_dir=analysis_dir,
            )
            runner._prepare_bug_request = lambda *args, **kwargs: prepared
            request = AppServerInvestigationRequest(
                prompt="",
                bug_url="https://project.feishu.cn/xpfailuremgmt/buglo/detail/1",
                raw_text="@bot 自主分析",
                triggered=True,
                trigger_mode="free",
                trigger_term="自主分析",
            )

            result = runner.run(request, event=event(event_id="evt-timeout", chat_id="oc", message_id="om-timeout"))

            self.assertFalse(result.success)
            self.assertEqual(result.error_code, "codex_app_server_turn_timeout")
            self.assertEqual(result.details["app_server_usage_scope"], "cumulative")
            self.assertEqual(result.details["app_server_input_tokens"], 2606367)
            self.assertEqual(result.details["app_server_cached_input_tokens"], 2500000)
            self.assertEqual(result.details["app_server_output_tokens"], 15310)
            self.assertEqual(result.details["app_server_total_tokens"], 2621677)
            self.assertEqual(result.details["app_server_thread_id"], "thread-timeout")
            self.assertEqual(result.details["app_server_turn_id"], "turn-timeout")

    def test_runner_passes_app_server_model_and_reasoning_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            skills_root = root / ".ai" / "skills"
            skills_root.mkdir(parents=True, exist_ok=True)
            (skills_root / "3d-stuck-investigate").mkdir(parents=True, exist_ok=True)
            (skills_root / "3d-stuck-investigate" / "SKILL.md").write_text("# skill\n", encoding="utf-8")
            guideengine = root / "guideengine"
            guideengine.mkdir(parents=True, exist_ok=True)
            prepared_logs = Path(tmp) / "prepared_logs"
            prepared_logs.mkdir(parents=True, exist_ok=True)

            config = BridgeConfig(
                dry_run=False,
                workspace_root=root,
                guideengine_repo=guideengine,
                data_dir=Path(tmp) / "data",
                source_investigation=SimpleNamespace(repo_roots=[guideengine]),
                bug_analysis=BugAnalysisOptions(
                    app_server_investigation=AppServerInvestigationOptions(
                        enabled=True,
                        prompt_template="OUT={output_path}\n",
                        model="gpt-5.5",
                        reasoning_effort="xhigh",
                    )
                ),
                # 全局 codex 配置故意不同，证明自主分析走的是路径专属覆盖。
                codex_app_server=CodexAppServerOptions(
                    enabled=True, command="codex", model="gpt-5.4", reasoning_effort="medium"
                ),
            )
            bug_runner = FakeBugRunnerForAppServer()
            runner = AppServerInvestigationRunner(config, bug_runner=bug_runner, skill_manager=SkillManager(config))
            context = create_job_context(config.data_dir, event=event(event_id="evt2", chat_id="oc", message_id="om2"))
            analysis_dir = context.output_dir / "app_server_investigation"
            analysis_dir.mkdir(parents=True, exist_ok=True)
            prepared = PreparedAppServerInvestigation(
                context=context,
                request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/1 自主分析",
                prompt_text="",
                bug_url="https://project.feishu.cn/xpfailuremgmt/buglo/detail/1",
                title="3D 生命周期异常",
                description="displayChanged 后没有后续回调",
                trigger_mode="free",
                trigger_term="自主分析",
                source_roots=[guideengine],
                fault_time="2026-05-25 16:50:41",
                fault_time_source="bug_title",
                fault_time_note="from title",
                selected_input=prepared_logs,
                prepared_input=prepared_logs,
                focused_log_input=prepared_logs,
                log_focus_manifest=analysis_dir / "log_focus.md",
                bridge_session_id="bridge_2",
                analysis_dir=analysis_dir,
            )
            runner._prepare_bug_request = lambda *args, **kwargs: prepared
            request = AppServerInvestigationRequest(
                prompt="",
                bug_url="https://project.feishu.cn/xpfailuremgmt/buglo/detail/1",
                raw_text="@bot 自主分析",
                triggered=True,
                trigger_mode="free",
                trigger_term="自主分析",
            )

            result = runner.run(request, event=event(event_id="evt2", chat_id="oc", message_id="om2"))

            self.assertTrue(result.success)
            self.assertEqual(bug_runner.prompts[0]["model_override"], "gpt-5.5")
            self.assertEqual(bug_runner.prompts[0]["reasoning_effort_override"], "xhigh")


if __name__ == "__main__":
    unittest.main()
