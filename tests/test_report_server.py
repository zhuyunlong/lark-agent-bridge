from pathlib import Path
import json
import tempfile
import unittest
import urllib.request
from unittest import mock

from lark_agent_bridge.models import BridgeConfig, LarkEvent, ReportServerOptions, TaskResult
from lark_agent_bridge.report_server import HtmlReportPublisher, ReportHttpServer
from lark_agent_bridge.state import AgentActivityStore


class ReportServerTests(unittest.TestCase):
    def test_publish_result_creates_single_link_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            html_path = Path(tmp) / "bug_report.html"
            html_path.write_text("<html><body><h1>根因分析</h1><p>首帧超时</p></body></html>", encoding="utf-8")
            publisher = HtmlReportPublisher(BridgeConfig(dry_run=False, data_dir=Path(tmp)))

            with mock.patch("lark_agent_bridge.report_server._detect_lan_ip", return_value="10.2.3.4"):
                published = publisher.publish_result(
                    TaskResult(
                        success=True,
                        message="bug 分析完成",
                        job_id="evt_bug_1",
                        details={"mode": "bug_analysis", "files_to_send": [html_path]},
                    )
                )

                self.assertIsNotNone(published)
                assert published is not None
                self.assertEqual(published.url, "http://10.2.3.4:8765/reports/evt_bug_1/")
                self.assertTrue(published.index_path.exists())
                self.assertTrue(published.report_paths[0].exists())
                self.assertEqual(published.source_report_paths, [html_path.resolve()])
                self.assertIn("首帧超时", published.context_excerpt)

    def test_http_server_serves_sessions_api_and_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            report_dir = data_dir / "published_reports" / "job_1"
            report_dir.mkdir(parents=True)
            (report_dir / "index.html").write_text("<html><body>report ok</body></html>", encoding="utf-8")
            activity_store = AgentActivityStore(data_dir / "state" / "agent_activity.json")
            event = LarkEvent(
                event_id="evt_1",
                message_id="om_1",
                chat_id="oc_1",
                chat_type="group",
                sender_id="ou_1",
                message_type="text",
                content="@bot 分析 bug",
            )
            activity_store.record_event(event)
            activity_store.record_progress({"message_id": "om_1", "stage": "agent_running", "message": "处理中"})
            activity_store.record_result(
                event,
                TaskResult(
                    success=True,
                    message="分析完成",
                    job_id="job_1",
                    details={"mode": "bug_analysis", "published_report_url": "http://127.0.0.1:8765/reports/job_1/"},
                ),
            )
            config = BridgeConfig(
                dry_run=False,
                data_dir=data_dir,
                report_server=ReportServerOptions(
                    enabled=True,
                    bind_host="127.0.0.1",
                    port=0,
                    public_base_url="http://127.0.0.1:0/reports",
                ),
            )
            server = ReportHttpServer(config, activity_store=activity_store)
            server.start()
            assert server._server is not None
            port = server._server.server_address[1]
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/sessions", timeout=5) as response:
                    html = response.read().decode("utf-8")
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/sessions", timeout=5) as response:
                    sessions = json.loads(response.read().decode("utf-8"))
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/sessions/om_1", timeout=5) as response:
                    detail = json.loads(response.read().decode("utf-8"))
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/reports/job_1/", timeout=5) as response:
                    report = response.read().decode("utf-8")
            finally:
                server.stop()

        self.assertIn("会话控制台", html)
        self.assertEqual(sessions["sessions"][0]["session_id"], "om_1")
        self.assertEqual(detail["session"]["progress"][0]["stage"], "agent_running")
        self.assertIn("report ok", report)


if __name__ == "__main__":
    unittest.main()
