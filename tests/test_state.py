from pathlib import Path
import tempfile
import unittest

from lark_agent_bridge.models import LarkEvent, TaskResult
from lark_agent_bridge.state import AgentActivityStore, ConversationContextStore


class ConversationContextStoreTests(unittest.TestCase):
    def test_lookup_alias_keeps_snapshot_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "contexts.json"
            store = ConversationContextStore(path)
            store.remember(
                root_message_id="om_root",
                chat_id="oc_1",
                mode="omlx_chat",
                request_text="你好",
                summary_text="第一轮回答",
                report_url="",
                report_excerpt="",
            )
            store.append_exchange("om_root", user_text="第一问", assistant_text="第一答")
            store.remember_alias(alias_message_id="om_bot_reply_1", root_message_id="om_root")
            store.append_exchange("om_root", user_text="第二问", assistant_text="第二答")

            alias_context = store.lookup("om_bot_reply_1")
            root_context = store.lookup("om_root")

        self.assertIsNotNone(alias_context)
        assert alias_context is not None
        self.assertEqual(
            alias_context.history,
            [
                {"role": "user", "content": "第一问"},
                {"role": "assistant", "content": "第一答"},
            ],
        )
        self.assertIsNotNone(root_context)
        assert root_context is not None
        self.assertEqual(
            root_context.history,
            [
                {"role": "user", "content": "第一问"},
                {"role": "assistant", "content": "第一答"},
                {"role": "user", "content": "第二问"},
                {"role": "assistant", "content": "第二答"},
            ],
        )


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

    def test_daemon_restart_marks_stale_running_sessions_failed(self):
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
                    "stage": "bug_agent_summary_stream",
                    "message": "深度分析输出更新",
                }
            )
            store.record_daemon_status(
                {
                    "stage": "event_consumer_ready",
                    "event_key": "im.message.receive_v1",
                    "ready": True,
                    "process_id": 12345,
                }
            )

            reloaded = AgentActivityStore(path)
            reloaded.record_daemon_status(
                {
                    "stage": "event_consumer_ready",
                    "event_key": "im.message.receive_v1",
                    "ready": True,
                    "process_id": 67890,
                }
            )
            detail = reloaded.get_session("om_1")

        assert detail is not None
        self.assertEqual(detail["status"], "failed")
        self.assertEqual(detail["error_code"], "session_orphaned_after_restart")
        self.assertEqual(detail["progress"][-1]["stage"], "daemon_restart_orphan_cleanup")

    def test_list_sessions_hides_silent_skipped_sessions_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "activity.json"
            store = AgentActivityStore(path)
            not_addressed = LarkEvent(
                event_id="evt_silent",
                message_id="om_silent",
                chat_id="oc_1",
                chat_type="group",
                sender_id="ou_1",
                message_type="text",
                content="普通群消息",
            )
            duplicate = LarkEvent(
                event_id="evt_duplicate",
                message_id="om_duplicate",
                chat_id="oc_1",
                chat_type="group",
                sender_id="ou_1",
                message_type="text",
                content="@bot 重复事件",
            )
            unsupported = LarkEvent(
                event_id="evt_unsupported",
                message_id="om_unsupported",
                chat_id="oc_1",
                chat_type="group",
                sender_id="ou_1",
                message_type="text",
                content="@bot 未支持请求",
            )

            store.record_result(
                not_addressed,
                TaskResult(
                    success=True,
                    message="group message not addressed to this bot",
                    skipped=True,
                    details={"mode": "not_addressed"},
                ),
            )
            store.record_result(
                duplicate,
                TaskResult(
                    success=True,
                    message="duplicate event skipped: evt_duplicate",
                    skipped=True,
                ),
            )
            store.record_result(
                unsupported,
                TaskResult(
                    success=True,
                    message="not a handled request",
                    skipped=True,
                    details={"mode": "unsupported"},
                ),
            )

            visible = store.list_sessions()
            all_sessions = store.list_sessions(include_hidden=True)
            silent_detail = store.get_session("om_silent")

        self.assertEqual([item["session_id"] for item in visible], ["om_unsupported"])
        self.assertEqual({item["session_id"] for item in all_sessions}, {"om_silent", "om_duplicate", "om_unsupported"})
        self.assertIsNotNone(silent_detail)
        assert silent_detail is not None
        self.assertFalse(silent_detail["visible_in_admin"])

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

    def test_delete_session_removes_persisted_history_record(self):
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
            store.record_progress({"message_id": "om_1", "stage": "bug_run_analysis", "message": "执行日志分析"})
            store.record_result(
                event,
                TaskResult(
                    success=True,
                    message="分析完成",
                    job_id="job_1",
                    details={"mode": "bug_analysis"},
                ),
            )

            deleted = store.delete_session("om_1", actor_id="ou_admin", actor_role="admin")
            reloaded = AgentActivityStore(path)

        self.assertIsNotNone(deleted)
        assert deleted is not None
        self.assertEqual(deleted["session_id"], "om_1")
        self.assertEqual(deleted["job_id"], "job_1")
        self.assertEqual(deleted["delete_authorization"]["actor_id"], "ou_admin")
        self.assertEqual(deleted["delete_authorization"]["actor_role"], "admin")
        self.assertEqual(deleted["delete_authorization"]["scope"], "analysis_history.delete")
        self.assertIsNone(reloaded.get_session("om_1"))

    def test_cancel_session_marks_running_session_and_preserves_cancelled_status(self):
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
                    "stage": "bug_run_analysis",
                    "message": "执行日志分析",
                }
            )
            cancelled = store.cancel_session(
                "om_1",
                reason="后台管理页请求终止任务。",
                terminated_processes=[{"pid": 123, "terminated": True}],
            )
            store.record_result(
                event,
                TaskResult(
                    success=False,
                    message="Bug 分析失败：子进程被终止",
                    error_code="bug_analysis_failed",
                    details={"mode": "bug_analysis"},
                ),
            )
            detail = store.get_session("om_1")

        self.assertIsNotNone(cancelled)
        assert detail is not None
        self.assertEqual(detail["status"], "cancelled")
        self.assertEqual(detail["error_code"], "cancelled_by_admin")
        self.assertFalse(detail["can_terminate"])
        self.assertEqual(detail["progress"][-1]["stage"], "admin_task_terminate_requested")

    def test_user_cancel_reason_survives_late_task_result(self):
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
                content="@bot auto 分析 bug",
            )

            store.record_event(event)
            store.record_progress(
                {
                    "event_id": "evt_1",
                    "message_id": "om_1",
                    "chat_id": "oc_1",
                    "chat_type": "group",
                    "stage": "app_server_investigation_control_registered",
                    "message": "AI 自主分析已支持运行中补充指令和取消",
                }
            )
            store.cancel_session(
                "om_1",
                reason="用户取消 AI 自主分析：停止",
                stage="app_server_investigation_cancel_requested",
                executor="用户回复",
                error_code="cancelled_by_user",
            )
            store.record_result(
                event,
                TaskResult(
                    success=False,
                    message="AI 自主分析已取消：用户取消 AI 自主分析：停止",
                    error_code="app_server_investigation_cancelled",
                    details={"mode": "app_server_investigation"},
                ),
            )
            detail = store.get_session("om_1")

        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual(detail["status"], "cancelled")
        self.assertEqual(detail["error_code"], "cancelled_by_user")
        self.assertEqual(detail["message"], "用户取消 AI 自主分析：停止")
        self.assertEqual(detail["progress"][-1]["stage"], "app_server_investigation_cancel_requested")


if __name__ == "__main__":
    unittest.main()
