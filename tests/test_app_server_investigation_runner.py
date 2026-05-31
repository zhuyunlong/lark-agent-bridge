import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from lark_agent_bridge.app_server_investigation import AppServerInvestigationRunner, PreparedAppServerInvestigation
from lark_agent_bridge.models import AppServerInvestigationOptions, AppServerInvestigationRequest, BugAnalysisOptions, BridgeConfig, CodexAppServerOptions, create_job_context
from lark_agent_bridge.skill_manager import SkillManager
from tests._app_base import event


class FakeBugRunnerForAppServer:
    def __init__(self):
        self.prompts = []

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
    ):
        self.prompts.append(
            {
                "analysis_kind": analysis_kind,
                "skill_name": skill_name,
                "prompt_text": prompt_text,
                "cwd": cwd,
                "timeout": timeout,
                "bridge_session_id": bridge_session_id,
            }
        )
        stdout_path.write_text("stdout", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        command_path.write_text("[]", encoding="utf-8")
        events_path.write_text("", encoding="utf-8")
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
    def test_runner_uses_config_prompt_and_writes_context_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            skills_root = root / ".ai" / "skills"
            skills_root.mkdir(parents=True, exist_ok=True)
            for name in ("scene-signal-diagnosis", "3d-stuck-investigate"):
                skill_dir = skills_root / name
                skill_dir.mkdir(parents=True, exist_ok=True)
                (skill_dir / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
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


if __name__ == "__main__":
    unittest.main()
