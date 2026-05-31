import tempfile
import unittest
from pathlib import Path

from lark_agent_bridge.models import TaskResult

from tests._app_base import BridgeConfig, FakeBugRunner, FakeLarkClient, FakeSignalHandler, event
from lark_agent_bridge.app import BridgeApp


class FakeSourceAnalysisRunner:
    def __init__(self, html_path: Path):
        self.html_path = html_path
        self.requests = []

    def run(self, request, event=None, *, progress_callback=None):
        self.requests.append({"request": request, "event": event, "progress_callback": progress_callback})
        self.html_path.write_text("<html><body>源码报告</body></html>", encoding="utf-8")
        return TaskResult(
            success=True,
            message="源码分析完成",
            job_id=event.event_id if event else "source_job",
            html_report=self.html_path,
            details={
                "mode": "source_analysis",
                "source_mode": "repository_only",
                "context_profile": "source_analysis",
                "classification_source": "deterministic_source_request",
                "files_to_send": [self.html_path],
                "target": request.target,
            },
        )


class AppSourceAnalysisTests(unittest.TestCase):
    def test_no_file_no_bug_source_request_routes_to_source_runner_not_signal(self):
        with tempfile.TemporaryDirectory() as tmp:
            html = Path(tmp) / "source.html"
            fake_lark = FakeLarkClient()
            fake_source = FakeSourceAnalysisRunner(html)
            fake_signal = FakeSignalHandler()
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"]),
                lark_client=fake_lark,
                source_analysis_runner=fake_source,
                handler=fake_signal,
            )

            result = app.handle_event(
                event(
                    event_id="evt_source_request",
                    message_id="om_source_request",
                    content="@bot 基于源码分析 UnityReady 信号链路如何监听",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "source_analysis")
        self.assertEqual(len(fake_source.requests), 1)
        self.assertEqual(fake_source.requests[0]["request"].target, "UnityReady")
        self.assertEqual(fake_signal.requests, [])
        self.assertIn("published_report_url", result.details)

    def test_bug_link_with_source_wording_still_routes_to_bug_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "analysis.md"
            html = Path(tmp) / "analysis.html"
            metadata.write_text("analysis", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            fake_source = FakeSourceAnalysisRunner(Path(tmp) / "source.html")
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"]),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                source_analysis_runner=fake_source,
            )

            result = app.handle_event(
                event(
                    event_id="evt_source_bug",
                    content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6993883118 基于源码分析 UnityReady",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "bug_analysis")
        self.assertEqual(len(fake_bug.requests), 1)
        self.assertEqual(fake_source.requests, [])

    def test_file_key_with_source_wording_still_routes_to_direct_analysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "analysis.md"
            html = Path(tmp) / "analysis.html"
            metadata.write_text("analysis", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            fake_source = FakeSourceAnalysisRunner(Path(tmp) / "source.html")
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"]),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                source_analysis_runner=fake_source,
            )

            result = app.handle_event(
                event(
                    event_id="evt_source_file",
                    content="@bot 基于源码分析 UnityReady file_abc123",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "direct_analysis")
        self.assertEqual(len(fake_bug.requests), 1)
        self.assertEqual(fake_source.requests, [])

    def test_report_followup_generates_context_html_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_source = FakeSourceAnalysisRunner(Path(tmp) / "source.html")
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"]),
                lark_client=fake_lark,
                source_analysis_runner=fake_source,
            )
            first = app.handle_event(
                event(
                    event_id="evt_source_first",
                    message_id="om_source_first",
                    content="@bot 基于源码分析 UnityReady 信号链路如何监听",
                )
            )

            followup = app.handle_event(
                event(
                    event_id="evt_source_diagram",
                    message_id="om_source_diagram",
                    reply_to="om_source_first",
                    content="@bot 基于这个回复画出泳道图",
                )
            )

        self.assertTrue(first.success)
        self.assertTrue(followup.success)
        self.assertEqual(followup.details["mode"], "diagram_report_followup")
        self.assertIn("published_report_url", followup.details)
        self.assertEqual(len(fake_source.requests), 1)
        self.assertGreaterEqual(len(fake_lark.files), 2)


if __name__ == "__main__":
    unittest.main()
