from pathlib import Path
import tempfile
import unittest

from lark_agent_bridge.models import LarkEvent, TaskResult
from lark_agent_bridge.state import AgentActivityStore


class AgentActivityStoreTests(unittest.TestCase):
    def test_records_event_progress_and_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "activity.json"
            store = AgentActivityStore(path)
            event = LarkEvent(
                event_id="evt_1",
                message_id="om_1",
                chat_id="oc_1",
                chat_type="group",
                sender_id="ou_1",
                message_type="text",
                content="@bot 分析 bug",
            )

            store.record_event(event)
            store.record_progress(
                {
                    "event_id": "evt_1",
                    "message_id": "om_1",
                    "chat_id": "oc_1",
                    "chat_type": "group",
                    "stage": "bug_fetch_data",
                    "message": "拉取 bug 详情",
                    "details": {"bug_url": "https://project.feishu.cn/xpfailuremgmt/buglo/detail/1"},
                }
            )
            store.record_result(
                event,
                TaskResult(
                    success=True,
                    message="分析完成",
                    job_id="job_1",
                    details={"mode": "bug_analysis", "published_report_url": "http://10.0.0.1:8765/reports/job_1/"},
                ),
            )

            reloaded = AgentActivityStore(path)
            sessions = reloaded.list_sessions()
            detail = reloaded.get_session("om_1")

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["status"], "succeeded")
        self.assertEqual(sessions[0]["progress_count"], 1)
        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual(detail["mode"], "bug_analysis")
        self.assertEqual(detail["report_url"], "http://10.0.0.1:8765/reports/job_1/")
        self.assertEqual(detail["progress"][0]["stage"], "bug_fetch_data")

    def test_records_daemon_status_separately_from_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "activity.json"
            store = AgentActivityStore(path)

            store.record_daemon_status(
                {
                    "stage": "event_consumer_ready",
                    "event_key": "im.message.receive_v1",
                    "ready": True,
                    "process_id": 12345,
                }
            )
            reloaded = AgentActivityStore(path)
            status = reloaded.get_daemon_status()

        self.assertEqual(reloaded.list_sessions(), [])
        self.assertEqual(status["stage"], "event_consumer_ready")
        self.assertEqual(status["event_key"], "im.message.receive_v1")
        self.assertTrue(status["ready"])

    def test_find_session_by_job_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "activity.json"
            store = AgentActivityStore(path)
            event = LarkEvent(
                event_id="evt_1",
                message_id="om_1",
                chat_id="oc_1",
                chat_type="group",
                sender_id="ou_1",
                message_type="text",
                content="@bot 分析 bug",
            )

            store.record_result(
                event,
                TaskResult(
                    success=True,
                    message="分析完成",
                    job_id="job_1",
                    details={"mode": "bug_analysis"},
                ),
            )

            reloaded = AgentActivityStore(path)
            detail = reloaded.find_session_by_job_id("job_1")

        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual(detail["session_id"], "om_1")


if __name__ == "__main__":
    unittest.main()
