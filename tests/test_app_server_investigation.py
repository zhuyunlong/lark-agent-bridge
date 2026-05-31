import tempfile
import unittest
from pathlib import Path

from lark_agent_bridge.app import BridgeApp
from lark_agent_bridge.models import TaskResult

from tests._app_base import BridgeConfig, FakeBugRunner, FakeLarkClient, event


class FakeAppServerInvestigationRunner:
    def __init__(self, html_path: Path):
        self.html_path = html_path
        self.requests = []

    def run(self, request, *, event=None, progress_callback=None):
        self.requests.append({"request": request, "event": event, "progress_callback": progress_callback})
        self.html_path.write_text("<html><body>app-server investigation</body></html>", encoding="utf-8")
        return TaskResult(
            success=True,
            message="AI 自主分析完成",
            job_id=event.event_id if event else "app_server_job",
            html_report=self.html_path,
            details={
                "mode": "app_server_investigation",
                "source_mode": "app_server_autonomous",
                "context_profile": "app_server_autonomous",
                "classification_source": "configured_app_server_investigation",
                "files_to_send": [self.html_path],
                "trigger_term": request.trigger_term,
                "trigger_mode": request.trigger_mode,
                "bug_url": request.bug_url,
            },
        )


class AppServerInvestigationRouteTests(unittest.TestCase):
    BUG_URL = "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6993883118"

    def _config(self, tmp: str):
        config = BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"])
        config.bug_analysis.app_server_investigation.enabled = True
        config.bug_analysis.app_server_investigation.prompt_template = "context={context_path}\ninventory={skill_inventory_path}\nout={output_path}"
        return config

    def test_auto_bug_route_uses_app_server_investigation_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "analysis.md"
            html = Path(tmp) / "analysis.html"
            metadata.write_text("analysis", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_bug = FakeBugRunner(metadata, html)
            fake_lark = FakeLarkClient()
            fake_runner = FakeAppServerInvestigationRunner(Path(tmp) / "app_server.html")
            app = BridgeApp(
                self._config(tmp),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                app_server_investigation_runner=fake_runner,
            )

            result = app.handle_event(
                event(
                    event_id="evt_app_auto",
                    message_id="om_app_auto",
                    content=f"@bot auto {self.BUG_URL} 调查下 3D 生命周期",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "app_server_investigation")
        self.assertEqual(len(fake_runner.requests), 1)
        self.assertEqual(fake_bug.requests, [])
        request = fake_runner.requests[0]["request"]
        self.assertEqual(request.bug_url, self.BUG_URL)
        self.assertEqual(request.prompt, "调查下 3D 生命周期")
        self.assertEqual(request.trigger_mode, "auto")

    def test_free_term_bug_route_uses_app_server_investigation_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "analysis.md"
            html = Path(tmp) / "analysis.html"
            metadata.write_text("analysis", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_bug = FakeBugRunner(metadata, html)
            fake_lark = FakeLarkClient()
            fake_runner = FakeAppServerInvestigationRunner(Path(tmp) / "app_server.html")
            app = BridgeApp(
                self._config(tmp),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                app_server_investigation_runner=fake_runner,
            )

            result = app.handle_event(
                event(
                    event_id="evt_app_free",
                    message_id="om_app_free",
                    content=f"@bot {self.BUG_URL} 全技能分析 调查下 3D 生命周期",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "app_server_investigation")
        self.assertEqual(len(fake_runner.requests), 1)
        self.assertEqual(fake_bug.requests, [])
        self.assertEqual(fake_runner.requests[0]["request"].trigger_term, "全技能分析")

    def test_non_independent_free_term_falls_back_to_normal_bug_analysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "analysis.md"
            html = Path(tmp) / "analysis.html"
            metadata.write_text("analysis", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_bug = FakeBugRunner(metadata, html)
            fake_lark = FakeLarkClient()
            fake_runner = FakeAppServerInvestigationRunner(Path(tmp) / "app_server.html")
            app = BridgeApp(
                self._config(tmp),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                app_server_investigation_runner=fake_runner,
            )

            result = app.handle_event(
                event(
                    event_id="evt_app_free_non_independent",
                    message_id="om_app_free_non_independent",
                    content=f"@bot {self.BUG_URL} 全技能分析一下 调查下 3D 生命周期",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "bug_analysis")
        self.assertEqual(len(fake_bug.requests), 1)
        self.assertEqual(fake_runner.requests, [])

    def test_file_key_with_auto_trigger_routes_to_app_server_investigation_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "analysis.md"
            html = Path(tmp) / "analysis.html"
            metadata.write_text("analysis", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_bug = FakeBugRunner(metadata, html)
            fake_lark = FakeLarkClient()
            fake_runner = FakeAppServerInvestigationRunner(Path(tmp) / "app_server.html")
            app = BridgeApp(
                self._config(tmp),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                app_server_investigation_runner=fake_runner,
            )

            result = app.handle_event(
                event(
                    event_id="evt_app_file",
                    message_id="om_app_file",
                    content="@bot auto 问题现象：进入 SR 后黑屏 问题时间：2026-05-25 16:50:41 file_abc123",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "app_server_investigation")
        self.assertEqual(len(fake_runner.requests), 1)
        self.assertEqual(fake_bug.requests, [])
        request = fake_runner.requests[0]["request"]
        self.assertEqual([item.value for item in request.resources], ["file_abc123"])
        self.assertEqual(request.trigger_mode, "auto")


if __name__ == "__main__":
    unittest.main()
