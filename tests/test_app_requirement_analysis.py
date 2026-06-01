import tempfile
import unittest
from pathlib import Path

from lark_agent_bridge.app import BridgeApp
from lark_agent_bridge.models import TaskResult
from tests._app_base import BridgeConfig, FakeBugRunner, FakeLarkClient, event


class FakeRequirementRunner:
    def __init__(self, html_path: Path):
        self.html_path = html_path
        self.requests = []

    def run(self, request, event=None, *, progress_callback=None):
        self.requests.append(request)
        self.html_path.write_text("<html><body>需求源码报告</body></html>", encoding="utf-8")
        return TaskResult(
            success=True,
            message="部分需求已有源码证据。",
            job_id=event.event_id,
            html_report=self.html_path,
            details={
                "mode": "requirement_analysis",
                "source_mode": "requirement_source",
                "context_profile": "requirement_analysis",
                "classification_source": "deterministic_requirement_workitem",
                "files_to_send": [self.html_path],
                "work_item_id": request.workitem.work_item_id,
                "requirement_verdict": "partially_implemented",
            },
        )


class FakeAppServerInvestigationRunner:
    def __init__(self, html_path: Path):
        self.html_path = html_path
        self.requests = []

    def run(self, request, *, event=None, progress_callback=None):
        self.requests.append({"request": request, "event": event, "progress_callback": progress_callback})
        self.html_path.write_text("<html><body>auto investigation</body></html>", encoding="utf-8")
        return TaskResult(
            success=True,
            message="AI 自主分析完成",
            job_id=event.event_id if event else "auto_job",
            html_report=self.html_path,
            details={
                "mode": "app_server_investigation",
                "source_mode": "app_server_autonomous",
                "context_profile": "app_server_autonomous",
                "classification_source": "configured_app_server_investigation",
                "files_to_send": [self.html_path],
            },
        )


class AppRequirementAnalysisTests(unittest.TestCase):
    def test_story_link_routes_to_requirement_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_runner = FakeRequirementRunner(Path(tmp) / "requirement.html")
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"]),
                lark_client=FakeLarkClient(),
                requirement_analysis_runner=fake_runner,
            )

            result = app.handle_event(
                event(
                    event_id="evt_requirement",
                    message_id="om_requirement",
                    content="@bot https://project.feishu.cn/demo/story/detail/12345 这是需求链接，结合源码分析是否可行",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "requirement_analysis")
        self.assertEqual(fake_runner.requests[0].workitem.work_item_id, "12345")
        self.assertIn("published_report_url", result.details)

    def test_auto_requirement_request_still_routes_to_app_server_investigation(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"])
            config.bug_analysis.app_server_investigation.enabled = True
            fake_runner = FakeRequirementRunner(Path(tmp) / "requirement.html")
            fake_auto = FakeAppServerInvestigationRunner(Path(tmp) / "auto.html")
            app = BridgeApp(
                config,
                lark_client=FakeLarkClient(),
                requirement_analysis_runner=fake_runner,
                app_server_investigation_runner=fake_auto,
            )

            result = app.handle_event(
                event(
                    event_id="evt_requirement_auto",
                    message_id="om_requirement_auto",
                    content="@bot auto https://project.feishu.cn/demo/story/detail/12345 结合源码分析是否可行",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "app_server_investigation")
        self.assertEqual(len(fake_auto.requests), 1)
        self.assertEqual(fake_runner.requests, [])

    def test_bug_link_still_routes_to_bug_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "analysis.md"
            html = Path(tmp) / "analysis.html"
            metadata.write_text("analysis", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_bug = FakeBugRunner(metadata, html)
            fake_runner = FakeRequirementRunner(Path(tmp) / "requirement.html")
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"]),
                lark_client=FakeLarkClient(),
                bug_runner=fake_bug,
                requirement_analysis_runner=fake_runner,
            )

            result = app.handle_event(
                event(
                    event_id="evt_bug",
                    content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6993883118 基于源码分析 UnityReady",
                )
            )

        self.assertEqual(result.details["mode"], "bug_analysis")
        self.assertEqual(len(fake_bug.requests), 1)
        self.assertEqual(fake_runner.requests, [])

    def test_requirement_report_followup_uses_existing_diagram_report_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_runner = FakeRequirementRunner(Path(tmp) / "requirement.html")
            fake_lark = FakeLarkClient()
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"]),
                lark_client=fake_lark,
                requirement_analysis_runner=fake_runner,
            )

            first = app.handle_event(
                event(
                    event_id="evt_requirement_first",
                    message_id="om_requirement_first",
                    content="@bot https://project.feishu.cn/demo/story/detail/12345 这是需求链接，结合源码分析是否可行",
                )
            )
            followup = app.handle_event(
                event(
                    event_id="evt_requirement_diagram",
                    message_id="om_requirement_diagram",
                    reply_to="om_requirement_first",
                    content="@bot 基于这个回复画出泳道图",
                )
            )

        self.assertTrue(first.success)
        self.assertTrue(followup.success)
        self.assertEqual(followup.details["mode"], "diagram_report_followup")
        self.assertEqual(len(fake_runner.requests), 1)

    def test_requirement_link_remains_blocked_in_unauthorized_external_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_runner = FakeRequirementRunner(Path(tmp) / "requirement.html")
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_allowed"]),
                lark_client=FakeLarkClient(),
                requirement_analysis_runner=fake_runner,
            )

            result = app.handle_event(
                event(
                    event_id="evt_requirement_denied",
                    message_id="om_requirement_denied",
                    chat_id="oc_external",
                    chat_type="group",
                    content="@bot https://project.feishu.cn/demo/story/detail/12345 这是需求链接，结合源码分析是否可行",
                )
            )

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "chat_not_allowed")
        self.assertEqual(fake_runner.requests, [])

    def test_requirement_link_is_not_skipped_as_stale_light_interaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_runner = FakeRequirementRunner(Path(tmp) / "requirement.html")
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"])
            config.event_consumer.drop_stale_light_interactions = True
            config.event_consumer.stale_light_interaction_grace_seconds = 60
            app = BridgeApp(
                config,
                lark_client=FakeLarkClient(),
                requirement_analysis_runner=fake_runner,
            )
            app.activity_store.record_daemon_status(
                {
                    "ready": True,
                    "stage": "event_consumer_ready",
                    "updated_at": "2026-06-01T10:10:00+00:00",
                    "process_id": 123,
                }
            )

            result = app.handle_event(
                event(
                    event_id="evt_requirement_stale_guard",
                    message_id="om_requirement_stale_guard",
                    create_time="2026-06-01T10:00:00+00:00",
                    content="@bot https://project.feishu.cn/demo/story/detail/12345 这是需求链接，结合源码分析是否可行",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "requirement_analysis")
        self.assertEqual(len(fake_runner.requests), 1)


if __name__ == "__main__":
    unittest.main()
