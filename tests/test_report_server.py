from pathlib import Path
import json
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest import mock

from lark_agent_bridge.case_store import CaseRecord, CaseStore
from lark_agent_bridge.health import ProcessWatchdog
from lark_agent_bridge.models import BridgeConfig, LarkEvent, ReportServerOptions, TaskResult
from lark_agent_bridge.report_server import HtmlReportPublisher, ReportHttpServer
from lark_agent_bridge.skill_manager import SkillManager
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

    def test_publish_result_index_includes_agent_runtime_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            html_path = Path(tmp) / "bug_report.html"
            html_path.write_text("<html><body><h1>根因分析</h1></body></html>", encoding="utf-8")
            publisher = HtmlReportPublisher(BridgeConfig(dry_run=False, data_dir=Path(tmp)))

            published = publisher.publish_result(
                TaskResult(
                    success=True,
                    message="bug 分析完成",
                    job_id="evt_bug_1",
                    duration_seconds=45.6,
                    details={
                        "mode": "bug_analysis",
                        "files_to_send": [html_path],
                        "agent_summary_provider": "codex",
                        "agent_summary_duration_seconds": 12.5,
                        "agent_summary_total_tokens": 375,
                        "agent_summary_usage_scope": "cumulative",
                    },
                )
            )

            assert published is not None
            index_html = published.index_path.read_text(encoding="utf-8")

        self.assertIn("Agent 类型", index_html)
        self.assertIn("codex", index_html)
        self.assertIn("累计 Agent Token", index_html)
        self.assertIn("- / - / 375", index_html)
        self.assertIn("总耗时", index_html)
        self.assertIn("45.6", index_html)

    def test_publish_result_index_does_not_render_raw_agent_summary_details(self):
        with tempfile.TemporaryDirectory() as tmp:
            html_path = Path(tmp) / "bug_report.html"
            html_path.write_text("<html><body><h1>根因分析</h1></body></html>", encoding="utf-8")
            publisher = HtmlReportPublisher(BridgeConfig(dry_run=False, data_dir=Path(tmp)))

            published = publisher.publish_result(
                TaskResult(
                    success=True,
                    message="结论：当前日志只覆盖 montecarlo 进程内链路。\n\n详细分析一\n详细分析二\n详细分析三",
                    job_id="evt_bug_2",
                    details={"mode": "bug_analysis", "files_to_send": [html_path]},
                )
            )

            assert published is not None
            index_html = published.index_path.read_text(encoding="utf-8")

        self.assertNotIn("原始 Agent 总结", index_html)
        self.assertNotIn("<details", index_html)
        self.assertIn("打开 HTML 报告", index_html)

    def test_publish_result_index_keeps_multiple_summary_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            html_path = Path(tmp) / "bug_report.html"
            html_path.write_text("<html><body><h1>根因分析</h1></body></html>", encoding="utf-8")
            publisher = HtmlReportPublisher(BridgeConfig(dry_run=False, data_dir=Path(tmp)))
            summary = "\n".join(
                [
                    "Bug 分析完成",
                    "",
                    "**结论** 当前只能确认两点。",
                    "- 第一，当前日志和现场时间不一致。",
                    "- 第二，源码上电量信号会影响准入判断。",
                    "## 证据",
                    "- 证据一",
                ]
            )

            published = publisher.publish_result(
                TaskResult(
                    success=True,
                    message=summary,
                    job_id="evt_bug_3",
                    details={"mode": "bug_analysis", "files_to_send": [html_path]},
                )
            )

            assert published is not None
            index_html = published.index_path.read_text(encoding="utf-8")

        self.assertIn("**结论** 当前只能确认两点。", index_html)
        self.assertIn("第一，当前日志和现场时间不一致。", index_html)
        self.assertIn("第二，源码上电量信号会影响准入判断。", index_html)

    def test_publish_result_index_skips_repeated_agent_request_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            html_path = Path(tmp) / "bug_report.html"
            html_path.write_text("<html><body><h1>根因分析</h1></body></html>", encoding="utf-8")
            publisher = HtmlReportPublisher(BridgeConfig(dry_run=False, data_dir=Path(tmp)))
            summary = "\n".join(
                [
                    "## 结论摘要",
                    "- 诉求：`分析3D生命周期`",
                    "  结论：生命周期已经 Ready。",
                    "- 诉求：`分析3D生命周期`",
                    "  结论：故障时间和主会话存在偏差。",
                    "## 关键证据",
                    "- 证据一",
                ]
            )

            published = publisher.publish_result(
                TaskResult(
                    success=True,
                    message=summary,
                    job_id="evt_bug_4",
                    details={"mode": "bug_analysis", "files_to_send": [html_path]},
                )
            )

            assert published is not None
            index_html = published.index_path.read_text(encoding="utf-8")

        self.assertNotIn("诉求：", index_html)
        self.assertIn("生命周期已经 Ready", index_html)
        self.assertIn("故障时间和主会话存在偏差", index_html)

    def test_http_server_serves_sessions_api_and_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            job_dir = data_dir / "jobs" / "job_1"
            job_dir.mkdir(parents=True)
            (job_dir / "artifact.txt").write_text("job artifact", encoding="utf-8")
            report_dir = data_dir / "published_reports" / "job_1"
            report_dir.mkdir(parents=True)
            (report_dir / "index.html").write_text("<html><body>report ok</body></html>", encoding="utf-8")
            activity_store = AgentActivityStore(data_dir / "state" / "agent_activity.json")
            case_store = CaseStore(data_dir / "state" / "cases.json")
            case_store.save(
                CaseRecord(
                    case_id="case_1",
                    bug_url="https://meegle.example.com/bug/1",
                    conclusion="最后一次报告",
                    report_url="http://127.0.0.1:8765/reports/job_1/",
                    analysis_mode="bug_analysis",
                    job_id="job_1",
                )
            )
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
                    job_dir=job_dir,
                    details={
                        "mode": "bug_analysis",
                        "published_report_url": "http://127.0.0.1:8765/reports/job_1/",
                        "evidence_log_bundle": str(job_dir / "evidence_logs"),
                    },
                ),
            )
            activity_store.record_daemon_status(
                {"stage": "event_consumer_ready", "event_key": "im.message.receive_v1", "ready": True}
            )
            config = BridgeConfig(
                dry_run=False,
                data_dir=data_dir,
                workspace_root=data_dir,
                report_server=ReportServerOptions(
                    enabled=True,
                    bind_host="127.0.0.1",
                    port=0,
                    public_base_url="http://127.0.0.1:0/reports",
                ),
            )
            server = ReportHttpServer(
                config,
                activity_store=activity_store,
                case_store=case_store,
                skill_manager=SkillManager(config),
            )
            try:
                server.start()
            except PermissionError as exc:
                self.skipTest(f"local HTTP bind is not permitted in this environment: {exc}")
            assert server._server is not None
            port = server._server.server_address[1]
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/sessions", timeout=5) as response:
                    html = response.read().decode("utf-8")
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/admin", timeout=5) as response:
                    admin_html = response.read().decode("utf-8")
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/sessions", timeout=5) as response:
                    sessions = json.loads(response.read().decode("utf-8"))
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/sessions/om_1", timeout=5) as response:
                    detail = json.loads(response.read().decode("utf-8"))
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/cases", timeout=5) as response:
                    cases = json.loads(response.read().decode("utf-8"))
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/analysis-history", timeout=5) as response:
                    history = json.loads(response.read().decode("utf-8"))
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/analysis-history/om_1", timeout=5) as response:
                    history_detail = json.loads(response.read().decode("utf-8"))
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/skills", timeout=5) as response:
                    skills = json.loads(response.read().decode("utf-8"))
                create_request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/skills",
                    data=json.dumps({"name": "http-debug-skill", "description": "debug skill"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(create_request, timeout=5) as response:
                    created_skill = json.loads(response.read().decode("utf-8"))
                debug_request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/skills/http-debug-skill/debug",
                    data=json.dumps({"sample_text": "debug skill"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(debug_request, timeout=5) as response:
                    debug_skill = json.loads(response.read().decode("utf-8"))
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/daemon", timeout=5) as response:
                    daemon = json.loads(response.read().decode("utf-8"))
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/reports/job_1/", timeout=5) as response:
                    report = response.read().decode("utf-8")
                delete_request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/analysis-history/om_1",
                    method="DELETE",
                )
                with urllib.request.urlopen(delete_request, timeout=5) as response:
                    deleted = json.loads(response.read().decode("utf-8"))
                with self.assertRaises(urllib.error.HTTPError) as missing_history:
                    urllib.request.urlopen(f"http://127.0.0.1:{port}/api/analysis-history/om_1", timeout=5)
            finally:
                server.stop()

        self.assertIn("会话控制台", html)
        self.assertIn("后台管理", admin_html)
        self.assertIn("分析历史", admin_html)
        self.assertIn("删除记录", admin_html)
        self.assertIn("/api/analysis-history", admin_html)
        self.assertIn("终止任务", admin_html)
        self.assertIn("/terminate", admin_html)
        self.assertEqual(sessions["sessions"][0]["session_id"], "om_1")
        self.assertEqual(detail["session"]["progress"][0]["stage"], "agent_running")
        self.assertEqual(cases["cases"][0]["case_id"], "case_1")
        self.assertEqual(history["items"][0]["session_id"], "om_1")
        self.assertEqual(history_detail["item"]["progress"][0]["stage"], "agent_running")
        self.assertEqual(history_detail["item"]["details"]["evidence_log_bundle"], str(job_dir / "evidence_logs"))
        self.assertTrue(any(item["name"] == "general" for item in skills["skills"]))
        self.assertEqual(created_skill["skill"]["name"], "http-debug-skill")
        self.assertIn("summary", debug_skill)
        self.assertEqual(daemon["daemon"]["stage"], "event_consumer_ready")
        self.assertIn("report ok", report)
        self.assertTrue(deleted["ok"])
        self.assertEqual(deleted["item"]["session_id"], "om_1")
        self.assertEqual(deleted["authorization"]["scope"], "analysis_history.delete")
        self.assertFalse(job_dir.exists())
        self.assertFalse(report_dir.exists())
        self.assertIsNone(activity_store.get_session("om_1"))
        self.assertIsNone(case_store.get("case_1"))
        self.assertEqual(missing_history.exception.code, 404)

    def test_http_server_can_terminate_running_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            activity_store = AgentActivityStore(data_dir / "state" / "agent_activity.json")
            event = LarkEvent(
                event_id="evt_running",
                message_id="om_running",
                chat_id="oc_1",
                chat_type="group",
                sender_id="ou_1",
                message_type="text",
                content="@bot 分析 bug",
            )
            activity_store.record_event(event)
            activity_store.record_progress(
                {
                    "event_id": "evt_running",
                    "message_id": "om_running",
                    "chat_id": "oc_1",
                    "chat_type": "group",
                    "stage": "bug_run_analysis",
                    "message": "执行日志分析",
                }
            )
            watchdog = ProcessWatchdog()
            watchdog.track(12345, "bug-analysis-startup", session_id="om_running")
            config = BridgeConfig(
                dry_run=False,
                data_dir=data_dir,
                report_server=ReportServerOptions(enabled=True, bind_host="127.0.0.1", port=0),
            )
            server = ReportHttpServer(config, activity_store=activity_store, process_watchdog=watchdog)
            try:
                server.start()
            except PermissionError as exc:
                self.skipTest(f"local HTTP bind is not permitted in this environment: {exc}")
            assert server._server is not None
            port = server._server.server_address[1]
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/sessions/om_running/terminate",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with (
                    mock.patch("lark_agent_bridge.health._process_alive", return_value=True),
                    mock.patch("lark_agent_bridge.health._safe_terminate", return_value=True) as terminate,
                    urllib.request.urlopen(request, timeout=5) as response,
                ):
                    payload = json.loads(response.read().decode("utf-8"))
            finally:
                server.stop()

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["session"]["status"], "cancelled")
        self.assertFalse(payload["session"]["can_terminate"])
        self.assertEqual(payload["terminated"][0]["session_id"], "om_running")
        terminate.assert_called_once_with(12345)


if __name__ == "__main__":
    unittest.main()
