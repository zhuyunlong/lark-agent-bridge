from pathlib import Path
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest import mock

from lark_agent_bridge.case_store import CaseRecord, CaseStore
from lark_agent_bridge.health import ProcessWatchdog
from lark_agent_bridge.knowledge import KnowledgeService
from lark_agent_bridge.models import BridgeConfig, KnowledgeOptions, LarkEvent, ReportServerOptions, TaskResult
from lark_agent_bridge.report_server import HtmlReportPublisher, ReportHttpServer
from lark_agent_bridge.skill_manager import SkillManager
from lark_agent_bridge.state import AgentActivityStore


class ReportServerTests(unittest.TestCase):
    def test_publish_result_creates_single_link_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            html_path = Path(tmp) / "bug_report.html"
            html_path.write_text("<html><body><h1>根因分析</h1><p>首帧超时</p></body></html>", encoding="utf-8")
            config = BridgeConfig(
                dry_run=False,
                data_dir=Path(tmp),
                report_server=ReportServerOptions(bind_host="0.0.0.0"),
            )
            publisher = HtmlReportPublisher(config)

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

    def test_publish_result_with_version_uses_versioned_url_and_preserves_older_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            html_path = Path(tmp) / "bug_report.html"
            html_path.write_text("<html><body><h1>根因分析</h1><p>首帧超时</p></body></html>", encoding="utf-8")
            config = BridgeConfig(
                dry_run=False,
                data_dir=Path(tmp),
                report_server=ReportServerOptions(bind_host="0.0.0.0"),
            )
            publisher = HtmlReportPublisher(config)

            with mock.patch("lark_agent_bridge.report_server._detect_lan_ip", return_value="10.2.3.4"):
                first = publisher.publish_result(
                    TaskResult(
                        success=True,
                        message="bug 分析完成",
                        job_id="evt_bug_1",
                        details={"mode": "bug_analysis", "files_to_send": [html_path]},
                    ),
                    version=1,
                )
                second = publisher.publish_result(
                    TaskResult(
                        success=True,
                        message="bug 重新分析完成",
                        job_id="evt_bug_1",
                        details={"mode": "bug_reanalysis", "files_to_send": [html_path]},
                    ),
                    version=2,
                )

                self.assertIsNotNone(first)
                self.assertIsNotNone(second)
                assert first is not None and second is not None
                self.assertEqual(first.url, "http://10.2.3.4:8765/reports/evt_bug_1/v1/")
                self.assertEqual(second.url, "http://10.2.3.4:8765/reports/evt_bug_1/v2/")
                self.assertTrue(first.index_path.exists())
                self.assertTrue(second.index_path.exists())
                self.assertNotEqual(first.index_path, second.index_path)

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
                        "agent_summary_prompt_tokens": 321,
                        "agent_summary_cached_input_tokens": 280,
                        "agent_summary_completion_tokens": 54,
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
        self.assertIn("321 / 280 / 54 / 375", index_html)
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
        self.assertNotIn("<iframe", index_html)

    def test_publish_result_index_links_reports_without_embedding_html(self):
        with tempfile.TemporaryDirectory() as tmp:
            html_path = Path(tmp) / "bug_report.html"
            html_path.write_text("<html><body><h1>3D Unity 启动生命周期报告</h1></body></html>", encoding="utf-8")
            publisher = HtmlReportPublisher(BridgeConfig(dry_run=False, data_dir=Path(tmp)))

            published = publisher.publish_result(
                TaskResult(
                    success=True,
                    message="结论：已生成报告。",
                    job_id="evt_bug_link",
                    details={"mode": "bug_analysis", "files_to_send": [html_path]},
                )
            )

            assert published is not None
            index_html = published.index_path.read_text(encoding="utf-8")

        self.assertIn('href="report.html"', index_html)
        self.assertIn("在新窗口打开完整报告", index_html)
        self.assertNotIn("<iframe", index_html)
        self.assertIn("3D Unity 启动生命周期报告", index_html)

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

        self.assertIn("结论 当前只能确认两点。", index_html)
        self.assertIn("第一，当前日志和现场时间不一致。", index_html)
        self.assertIn("第二，源码上电量信号会影响准入判断。", index_html)
        self.assertNotIn("**结论**", index_html)

    def test_publish_result_index_uses_embedded_report_titles_for_multiple_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            first_html = Path(tmp) / "scene_signal.html"
            second_html = Path(tmp) / "source_stage.html"
            first_html.write_text(
                "<html><head><title>3D场景信号分析报告</title></head><body><h1>3D场景信号分析报告</h1></body></html>",
                encoding="utf-8",
            )
            second_html.write_text(
                "<html><head><title>源码分析阶段</title></head><body><h1>源码分析阶段</h1></body></html>",
                encoding="utf-8",
            )
            publisher = HtmlReportPublisher(BridgeConfig(dry_run=False, data_dir=Path(tmp)))

            published = publisher.publish_result(
                TaskResult(
                    success=True,
                    message="结论：已生成两份报告。",
                    job_id="evt_bug_multi",
                    details={"mode": "bug_analysis", "files_to_send": [first_html, second_html]},
                )
            )

            assert published is not None
            index_html = published.index_path.read_text(encoding="utf-8")

        self.assertIn("3D场景信号分析报告", index_html)
        self.assertIn("源码分析阶段", index_html)
        self.assertNotIn("bug_analysis 报告 1", index_html)
        self.assertNotIn("bug_analysis 报告 2", index_html)

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
                route_request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/skills/http-debug-skill/route",
                    data=json.dumps(
                        {"role": "primary", "kind": "general", "requires_logs": True, "executor": "file_agent"}
                    ).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(route_request, timeout=5) as response:
                    routed_skill = json.loads(response.read().decode("utf-8"))
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
        self.assertIn("报告卡片可选", admin_html)
        self.assertIn("未接入主路由", admin_html)
        self.assertIn("skill-route-filter", admin_html)
        self.assertIn("进 Bug 分析", admin_html)
        self.assertIn("/route", admin_html)
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
        general_skill = next(item for item in skills["skills"] if item["name"] == "general")
        self.assertEqual(general_skill["route_status"], "fallback")
        self.assertFalse(general_skill["selectable_in_report_card"])
        self.assertEqual(created_skill["skill"]["name"], "http-debug-skill")
        self.assertEqual(created_skill["skill"]["route_status"], "custom_unrouted")
        self.assertFalse(created_skill["skill"]["selectable_in_report_card"])
        self.assertEqual(routed_skill["skill"]["route_status"], "bug_primary_agent_ready")
        self.assertTrue(routed_skill["skill"]["selectable_in_report_card"])
        self.assertEqual(routed_skill["skill"]["executor"], "file_agent")
        self.assertIn("文件 Agent", routed_skill["skill"]["routing_note"])
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

    def test_http_server_exposes_knowledge_search_and_sync(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            config = BridgeConfig(
                dry_run=False,
                data_dir=data_dir,
                report_server=ReportServerOptions(enabled=True, bind_host="127.0.0.1", port=0),
                knowledge=KnowledgeOptions(enabled=True, storage=data_dir / "knowledge.sqlite"),
            )
            knowledge = KnowledgeService(config)
            knowledge.add_text(
                source_id="manual",
                title="打开Debug面板",
                content="adb shell am start -a com.xiaopeng.intent.action.DEV_BOARD",
            )
            server = ReportHttpServer(config, knowledge_service=knowledge)
            try:
                server.start()
            except PermissionError as exc:
                self.skipTest(f"local HTTP bind is not permitted in this environment: {exc}")
            assert server._server is not None
            port = server._server.server_address[1]
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/knowledge/search?q=Debug", timeout=5) as response:
                    search_payload = json.loads(response.read().decode("utf-8"))
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/knowledge/sync",
                    data=b"{}",
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    sync_payload = json.loads(response.read().decode("utf-8"))
            finally:
                server.stop()

        self.assertEqual(search_payload["hits"][0]["title"], "打开Debug面板")
        self.assertEqual(sync_payload["source_count"], 0)

    def test_http_server_embeds_live_knowledge_export_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            report_path = data_dir / "knowledge" / "knowledge_export_report.html"
            report_path.parent.mkdir(parents=True)
            report_path.write_text("<html><body>knowledge export v1</body></html>", encoding="utf-8")
            config = BridgeConfig(
                dry_run=False,
                data_dir=data_dir,
                report_server=ReportServerOptions(enabled=True, bind_host="127.0.0.1", port=0),
            )
            server = ReportHttpServer(config)
            try:
                server.start()
            except PermissionError as exc:
                self.skipTest(f"local HTTP bind is not permitted in this environment: {exc}")
            assert server._server is not None
            port = server._server.server_address[1]
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/admin", timeout=5) as response:
                    admin_html = response.read().decode("utf-8")
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/knowledge/export-report",
                    timeout=5,
                ) as response:
                    first_status = json.loads(response.read().decode("utf-8"))
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/knowledge/export-report", timeout=5) as response:
                    report_html = response.read().decode("utf-8")
                    cache_control = response.headers.get("Cache-Control")
                report_path.write_text("<html><body>knowledge export v2</body></html>", encoding="utf-8")
                next_mtime = report_path.stat().st_mtime + 2
                os.utime(report_path, (next_mtime, next_mtime))
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/knowledge/export-report",
                    timeout=5,
                ) as response:
                    second_status = json.loads(response.read().decode("utf-8"))
            finally:
                server.stop()

        self.assertIn("知识库报告", admin_html)
        self.assertIn("knowledge-report-frame", admin_html)
        self.assertIn("/api/knowledge/export-report", admin_html)
        self.assertIn("isKnowledgeReportActive", admin_html)
        self.assertIn("isFilePreviewMode", admin_html)
        self.assertIn("../../data/knowledge/knowledge_export_report.html", admin_html)
        self.assertIn('tab.dataset.view === "knowledge-report-view"', admin_html)
        self.assertTrue(first_status["exists"])
        self.assertEqual(first_status["url"], "/knowledge/export-report")
        self.assertGreater(first_status["size_bytes"], 0)
        self.assertIn("knowledge export v1", report_html)
        self.assertEqual(cache_control, "no-store")
        self.assertNotEqual(first_status["version"], second_status["version"])


if __name__ == "__main__":
    unittest.main()


class ResolvePublicBaseUrlTests(unittest.TestCase):
    """Verify bind_host affects public URL generation."""

    def test_loopback_bind_uses_loopback_public_url(self):
        from lark_agent_bridge.report_server import resolve_public_base_url
        with mock.patch("lark_agent_bridge.report_server._detect_lan_ip", return_value="10.2.3.4"):
            url = resolve_public_base_url("", port=8765, bind_host="127.0.0.1")
        self.assertEqual(url, "http://127.0.0.1:8765/reports")

    def test_all_interfaces_bind_uses_lan_ip(self):
        from lark_agent_bridge.report_server import resolve_public_base_url
        with mock.patch("lark_agent_bridge.report_server._detect_lan_ip", return_value="10.2.3.4"):
            url = resolve_public_base_url("", port=8765, bind_host="0.0.0.0")
        self.assertEqual(url, "http://10.2.3.4:8765/reports")

    def test_empty_bind_defaults_to_loopback(self):
        from lark_agent_bridge.report_server import resolve_public_base_url
        with mock.patch("lark_agent_bridge.report_server._detect_lan_ip", return_value="10.2.3.4"):
            url = resolve_public_base_url("", port=8765, bind_host="")
        self.assertEqual(url, "http://127.0.0.1:8765/reports")

    def test_explicit_public_url_is_preserved(self):
        from lark_agent_bridge.report_server import resolve_public_base_url
        url = resolve_public_base_url(
            "https://bridge.example.com/reports",
            port=8765,
            bind_host="127.0.0.1",
        )
        self.assertEqual(url, "https://bridge.example.com/reports")

    def test_localhost_bind_uses_loopback_public_url(self):
        from lark_agent_bridge.report_server import resolve_public_base_url
        with mock.patch("lark_agent_bridge.report_server._detect_lan_ip", return_value="10.2.3.4"):
            url = resolve_public_base_url("", port=8765, bind_host="localhost")
        # localhost is loopback, should not use LAN IP
        self.assertNotIn("10.2.3.4", url)


class BindHostTests(unittest.TestCase):
    """Verify resolve_bind_host behavior."""

    def test_empty_defaults_to_loopback(self):
        from lark_agent_bridge.report_server import resolve_bind_host
        self.assertEqual(resolve_bind_host(""), "127.0.0.1")

    def test_explicit_zero_preserved(self):
        from lark_agent_bridge.report_server import resolve_bind_host
        self.assertEqual(resolve_bind_host("0.0.0.0"), "0.0.0.0")

    def test_explicit_loopback_preserved(self):
        from lark_agent_bridge.report_server import resolve_bind_host
        self.assertEqual(resolve_bind_host("127.0.0.1"), "127.0.0.1")


class ViewerRoleTests(unittest.TestCase):
    """Verify viewer role cannot perform write operations."""

    def test_viewer_role_returned_from_check_token(self):
        from lark_agent_bridge.auth import AdminAuth
        import tempfile as tf
        with tf.TemporaryDirectory() as tmp:
            auth = AdminAuth(Path(tmp), admin_token="admin-secret")
            auth.create_user("viewer1", "password123", role="viewer")
            session = auth.login("viewer1", "password123")
            self.assertIsNotNone(session)
            info = auth.check_token(session.token)
            self.assertEqual(info["role"], "viewer")

    def test_admin_role_returned_from_check_token(self):
        from lark_agent_bridge.auth import AdminAuth
        import tempfile as tf
        with tf.TemporaryDirectory() as tmp:
            auth = AdminAuth(Path(tmp), admin_token="admin-secret")
            auth.create_user("admin1", "password123", role="admin")
            session = auth.login("admin1", "password123")
            info = auth.check_token(session.token)
            self.assertEqual(info["role"], "admin")

    def test_static_token_has_admin_role(self):
        from lark_agent_bridge.auth import AdminAuth
        import tempfile as tf
        with tf.TemporaryDirectory() as tmp:
            auth = AdminAuth(Path(tmp), admin_token="secret")
            info = auth.check_token("secret")
            self.assertEqual(info["role"], "admin")
