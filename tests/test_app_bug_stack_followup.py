from _app_base import *  # noqa: F401,F403
from _app_base import _AppTestBase


_TOMBSTONE = "#00 pc 0000000000123450 /system/app/demo/lib/arm64/libil2cpp.so"


class AppBugStackFollowupTests(_AppTestBase):
    def test_stack_clarification_backtrace_reply_runs_fresh_bug_analysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            fake_chat = FakeOmlxChatClient()
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"]),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                chat_client=fake_chat,
            )
            bug_url = "https://project.feishu.cn/xpfailuremgmt/buglo/detail/7008711"
            original_text = f"{bug_url} 反解堆栈"
            clarification_event = event(
                event_id="evt_stack_missing",
                message_id="om_stack_missing",
                content=f"@bot {original_text}",
            )
            app.activity_store.record_event(clarification_event)
            details = {"mode": "bug_stack_clarification", "bug_url": bug_url, "user_request_text": original_text, "stack_gate_status": "missing_stack_payload"}
            app.activity_store.record_result(clarification_event, TaskResult(success=True, message="缺少可反解堆栈", job_id="job_missing_stack", job_dir=Path(tmp) / "jobs" / "job_missing_stack", skipped=True, details=details))
            followup = app.handle_event(event(event_id="evt_stack_reply", message_id="om_stack_reply", root_id="om_stack_missing", parent_id="om_bot_reply", content=f"@bot {_TOMBSTONE}"))
        self.assertTrue(followup.success)
        self.assertEqual(followup.details["mode"], "bug_analysis")
        self.assertEqual(len(fake_bug.analysis_calls), 1)
        self.assertEqual(fake_bug.reanalysis_calls, [])
        self.assertEqual(followup.details["conversation_root_message_id"], "om_stack_missing")
        request = fake_bug.analysis_calls[0]["request"]
        self.assertEqual(request.bug_url, bug_url)
        self.assertIn("反解堆栈", request.prompt)
        self.assertIn("libil2cpp.so", request.prompt)


    def test_stack_clarification_non_stack_reply_keeps_asking_without_analysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"]),
                lark_client=fake_lark,
                bug_runner=fake_bug,
            )
            bug_url = "https://project.feishu.cn/xpfailuremgmt/buglo/detail/7008711"
            original_text = f"{bug_url} 反解堆栈"
            clarification_event = event(event_id="evt_stack_missing2", message_id="om_stack_missing2", content=f"@bot {original_text}")
            app.activity_store.record_event(clarification_event)
            details = {"mode": "bug_stack_clarification", "bug_url": bug_url, "user_request_text": original_text, "stack_gate_status": "missing_stack_payload"}
            app.activity_store.record_result(clarification_event, TaskResult(success=True, message="缺少可反解堆栈", job_id="job_missing_stack2", job_dir=Path(tmp) / "jobs" / "job_missing_stack2", skipped=True, details=details))
            followup = app.handle_event(event(event_id="evt_stack_reply2", message_id="om_stack_reply2", root_id="om_stack_missing2", parent_id="om_bot_reply", content="@bot 好的谢谢"))
        self.assertTrue(followup.success)
        self.assertTrue(followup.skipped)
        self.assertEqual(followup.details["mode"], "bug_stack_clarification")
        self.assertEqual(fake_bug.analysis_calls, [])
        self.assertEqual(fake_bug.reanalysis_calls, [])
