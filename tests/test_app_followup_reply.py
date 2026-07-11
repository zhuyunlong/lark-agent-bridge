from _app_base import *  # noqa: F401,F403
from _app_base import _AppTestBase


class AppFollowupReplyTests(_AppTestBase):
    def test_failed_fresh_result_is_threaded_reply_to_triggering_message(self):
        # A fresh request that fails before any progress card (e.g. addr2line
        # missing_address) must be delivered as a threaded reply to the
        # triggering message, NOT a standalone send. Otherwise the bot's reply
        # carries no reply_to and a later "reply to the bot" cannot walk the
        # chain back to the original input.
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp) / "data"),
                lark_client=fake_lark,
            )
            ev = event(message_id="om_fresh_fail", content="@bot 源码分析 找crash原因")
            result = TaskResult(
                success=False,
                message="缺少待反解地址：未找到 crash 堆栈。",
                error_code="missing_address",
                details={"mode": "addr2line_resolve"},
            )
            app._send_result(ev, result)

        self.assertIn("om_fresh_fail", [r["message_id"] for r in fake_lark.replies])
        self.assertEqual(fake_lark.sent, [])

    def test_failed_fresh_delivery_registers_bot_reply_alias_for_group_followup(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp) / "data"),
                lark_client=fake_lark,
            )
            original = event(
                event_id="evt_failed_source",
                message_id="om_failed_source",
                reply_to="om_file_msg",
                content="@bot 源码分析 找出最后一次crash的原因",
            )
            failed = TaskResult(
                success=False,
                message="缺少待反解地址：未找到 crash 堆栈。",
                error_code="missing_address",
                details={"mode": "addr2line_resolve"},
            )

            finalized = app._deliver_result(
                original,
                failed,
                request_text="源码分析 找出最后一次crash的原因",
            )

            self.assertEqual(fake_lark.replies[0]["message_id"], "om_failed_source")
            bot_reply_id = "om_reply_1"
            context = app._lookup_bot_alias_context(bot_reply_id)

        self.assertEqual(bot_reply_id, "om_reply_1")
        self.assertEqual(finalized.details["conversation_root_message_id"], "om_failed_source")
        self.assertIsNotNone(context)
        self.assertEqual(context.root_message_id, "om_failed_source")
        self.assertEqual(context.mode, "addr2line_resolve")

    def test_group_reply_without_mention_to_failed_bot_reply_routes_with_reply_chain_resources(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "analysis.md"
            html = Path(tmp) / "analysis.html"
            metadata.write_text("analysis", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_allowed"],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
            )
            original = event(
                event_id="evt_failed_source",
                message_id="om_failed_source",
                chat_id="oc_denied",
                reply_to="om_file_msg",
                content="@bot 源码分析 找出最后一次crash的原因",
            )
            app._deliver_result(
                original,
                TaskResult(
                    success=False,
                    message="缺少待反解地址：未找到 crash 堆栈。",
                    error_code="missing_address",
                    details={"mode": "addr2line_resolve"},
                ),
                request_text="源码分析 找出最后一次crash的原因",
            )
            self.assertEqual(fake_lark.replies[0]["message_id"], "om_failed_source")
            bot_reply_id = "om_reply_1"
            fake_lark.fetched_messages[bot_reply_id] = json.dumps(
                {
                    "data": {
                        "messages": [
                            {
                                "message_id": bot_reply_id,
                                "reply_to": "om_failed_source",
                            }
                        ]
                    }
                },
                ensure_ascii=False,
            )
            fake_lark.fetched_messages["om_failed_source"] = json.dumps(
                {
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_failed_source",
                                "reply_to": "om_file_msg",
                                "content": "@bot 源码分析 找出最后一次crash的原因",
                            }
                        ]
                    }
                },
                ensure_ascii=False,
            )
            fake_lark.fetched_messages["om_file_msg"] = json.dumps(
                {
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_file_msg",
                                "content": '{"file_key":"file_log7z"}',
                            }
                        ]
                    }
                },
                ensure_ascii=False,
            )

            result = app.handle_event(
                event(
                    event_id="evt_no_at_followup_to_failed_reply",
                    message_id="om_no_at_followup_to_failed_reply",
                    chat_id="oc_denied",
                    reply_to=bot_reply_id,
                    content="那你看下3d生命周期 时间点 15:00",
                )
            )

        self.assertNotEqual(result.details.get("mode"), "not_addressed")
        self.assertFalse(result.skipped and result.details.get("mode") == "not_addressed")
        self.assertNotEqual(result.error_code, "missing_address")
        self.assertNotEqual(result.error_code, "missing_signal")
        self.assertNotEqual(result.details.get("mode"), "signal_lifecycle")
        self.assertIn(result.details.get("mode"), {"direct_analysis", "bug_clarification"})
        if fake_bug.requests:
            self.assertEqual(fake_bug.requests[0].resources[0].value, "file_log7z")

    def test_source_analysis_without_mention_after_failed_bot_reply_does_not_return_missing_address(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "analysis.md"
            html = Path(tmp) / "analysis.html"
            metadata.write_text("analysis", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_allowed"],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
            )
            app._deliver_result(
                event(
                    event_id="evt_failed_source_again",
                    message_id="om_failed_source_again",
                    chat_id="oc_denied",
                    reply_to="om_file_msg",
                    content="@bot 源码分析 找出最后一次crash的原因",
                ),
                TaskResult(
                    success=False,
                    message="缺少待反解地址：未找到 crash 堆栈。",
                    error_code="missing_address",
                    details={"mode": "addr2line_resolve"},
                ),
                request_text="源码分析 找出最后一次crash的原因",
            )
            bot_reply_id = "om_reply_1"
            fake_lark.fetched_messages[bot_reply_id] = json.dumps(
                {
                    "data": {
                        "messages": [
                            {
                                "message_id": bot_reply_id,
                                "reply_to": "om_failed_source_again",
                            }
                        ]
                    }
                },
                ensure_ascii=False,
            )
            fake_lark.fetched_messages["om_failed_source_again"] = json.dumps(
                {
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_failed_source_again",
                                "reply_to": "om_file_msg",
                                "content": "@bot 源码分析 找出最后一次crash的原因",
                            }
                        ]
                    }
                },
                ensure_ascii=False,
            )
            fake_lark.fetched_messages["om_file_msg"] = json.dumps(
                {
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_file_msg",
                                "content": '{"file_key":"file_log7z"}',
                            }
                        ]
                    }
                },
                ensure_ascii=False,
            )

            result = app.handle_event(
                event(
                    event_id="evt_no_at_source_followup_to_failed_reply",
                    message_id="om_no_at_source_followup_to_failed_reply",
                    chat_id="oc_denied",
                    reply_to=bot_reply_id,
                    content="源码分析 找出最后一次crash的原因",
                )
            )

        self.assertNotEqual(result.details.get("mode"), "not_addressed")
        self.assertNotEqual(result.error_code, "missing_address")
        self.assertEqual(result.details.get("mode"), "direct_analysis")
        self.assertEqual(len(fake_bug.requests), 1)
        self.assertEqual(fake_bug.requests[0].resources[0].value, "file_log7z")

    def test_signal_request_without_reply_chain_does_not_reuse_same_chat_latest_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp) / "logs"
            log_dir.mkdir()
            (log_dir / "main.log").write_text("05-20 12:00:00 signal 132002", encoding="utf-8")
            route_content = "看132002 信号吧"
            fake_handler = FakeSignalHandler()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp) / "data",
                    allowed_chats=["oc_denied"],
                ),
                lark_client=FakeLarkClient(),
                handler=fake_handler,
                intent_runner=FakeIntentRunner(
                    {
                        route_content: IntentDecision(
                            route="signal",
                            reason="查看具体信号",
                            confidence="high",
                        )
                    }
                ),
            )
            app.conversation_store.remember(
                root_message_id="om_unrelated_bug",
                chat_id="oc_denied",
                mode="bug_reanalysis",
                request_text="同群另一轮 bug 分析",
                summary_text="已有日志",
                report_url="",
                report_excerpt="",
            )
            app.activity_store.record_result(
                event(message_id="om_unrelated_bug", content="@bot unrelated"),
                TaskResult(
                    success=True,
                    message="上一轮分析完成",
                    details={
                        "mode": "bug_reanalysis",
                        "prepared_log_input": str(log_dir),
                        "selected_log_input": str(log_dir),
                    },
                ),
            )

            result = app.handle_event(
                event(
                    event_id="evt_signal_no_chain",
                    message_id="om_signal_no_chain",
                    content=f"@bot {route_content}",
                )
            )

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "missing_log")
        self.assertEqual(len(fake_handler.requests), 1)
        self.assertEqual(fake_handler.requests[0].resources, [])
    def test_signal_followup_recovers_resources_from_previous_missing_signal_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            route_content = "那就看132002 信号吧"
            fake_lark = FakeLarkClient()
            fake_lark.fetched_messages["om_missing_signal"] = json.dumps(
                {
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_missing_signal",
                                "reply_to": "om_file_msg",
                            }
                        ]
                    }
                }
            )
            fake_lark.fetched_messages["om_file_msg"] = json.dumps(
                {
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_file_msg",
                                "content": "{\"file_key\":\"file_v3_log_abc\"}",
                            }
                        ]
                    }
                }
            )
            fake_handler = FakeSignalHandler()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp) / "data",
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                handler=fake_handler,
                intent_runner=FakeIntentRunner(
                    {
                        route_content: IntentDecision(
                            route="signal",
                            reason="补充具体信号继续分析",
                            confidence="high",
                        )
                    }
                ),
            )
            previous_event = event(
                event_id="evt_missing_signal",
                message_id="om_missing_signal",
                content="@bot 基于日志 分析上下电信号",
            )
            app.activity_store.record_event(previous_event)
            app.activity_store.record_result(
                previous_event,
                TaskResult(
                    success=False,
                    message="缺少 signal",
                    error_code="missing_signal",
                    details={},
                ),
            )

            result = app.handle_event(
                event(
                    event_id="evt_signal_after_missing_signal",
                    message_id="om_signal_after_missing_signal",
                    reply_to="om_missing_signal",
                    content=f"@bot {route_content}",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(len(fake_handler.requests), 1)
        resources = fake_handler.requests[0].resources
        self.assertEqual(resources[0].kind, "file")
        self.assertEqual(resources[0].value, "file_v3_log_abc")
        self.assertEqual(resources[0].source_message_id, "om_file_msg")
    def test_direct_analysis_reply_to_file_in_non_allowlisted_group_routes_to_bug_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "analysis.md"
            html = Path(tmp) / "analysis.html"
            metadata.write_text("analysis", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            fake_lark.fetched_messages["om_file_msg"] = """
{
  "ok": true,
  "data": {
    "messages": [
      {
        "message_id": "om_file_msg",
        "content": "{\\"file_key\\":\\"file_abc123\\"}"
      }
    ]
  }
}
"""
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_allowed"],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
            )

            result = app.handle_event(
                event(
                    chat_id="oc_denied",
                    reply_to="om_file_msg",
                    content="@bot 分析启动和卡顿",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "direct_analysis")
        self.assertEqual(len(fake_bug.requests), 1)
        self.assertEqual(fake_bug.requests[0].resources[0].kind, "file")
        self.assertEqual(fake_bug.requests[0].resources[0].value, "file_abc123")
        self.assertEqual(fake_bug.requests[0].resources[0].source_message_id, "om_file_msg")
    def test_source_analysis_find_crash_reply_to_file_routes_to_direct_analysis(self):
        # Regression for the group incident: "源码分析 找出最后一次crash的原因"
        # replying to a log file must run a source/log analysis on that log,
        # NOT be hijacked by addr2line (which would fail missing_address because
        # the log has no #xx pc lib*.so backtrace).
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "analysis.md"
            html = Path(tmp) / "analysis.html"
            metadata.write_text("analysis", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            fake_lark.fetched_messages["om_file_msg"] = """
{
  "ok": true,
  "data": {
    "messages": [
      {
        "message_id": "om_file_msg",
        "content": "{\\"file_key\\":\\"file_log7z\\"}"
      }
    ]
  }
}
"""
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_allowed"],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
            )

            result = app.handle_event(
                event(
                    chat_id="oc_denied",
                    reply_to="om_file_msg",
                    content="@bot 源码分析 找出最后一次crash的原因",
                )
            )

        self.assertNotEqual(result.error_code, "missing_address")
        self.assertEqual(result.details["mode"], "direct_analysis")
        self.assertEqual(len(fake_bug.requests), 1)
        self.assertEqual(fake_bug.requests[0].resources[0].value, "file_log7z")

    def test_3d_lifecycle_reply_to_file_routes_to_direct_analysis_not_missing_signal(self):
        # Regression for the group incident msg3: "看下3d生命周期 时间点 15:00"
        # replying to a log file must analyse that log, NOT dead-end on
        # signal_lifecycle's missing_signal (the user gave a timestamp, not a
        # signal code).
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "analysis.md"
            html = Path(tmp) / "analysis.html"
            metadata.write_text("analysis", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            fake_lark.fetched_messages["om_file_msg"] = """
{
  "ok": true,
  "data": {
    "messages": [
      {
        "message_id": "om_file_msg",
        "content": "{\\"file_key\\":\\"file_log7z\\"}"
      }
    ]
  }
}
"""
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_allowed"],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
            )

            result = app.handle_event(
                event(
                    chat_id="oc_denied",
                    reply_to="om_file_msg",
                    content="@bot 那你看下3d生命周期 时间点 15:00",
                )
            )

        # Core fix: it must NOT dead-end on signal_lifecycle's missing_signal.
        # "3d生命周期" is ambiguous (startup vs stuck), so engaging the direct
        # log-analysis flow — running it or asking the user to clarify the
        # direction — is the correct outcome, not a signal-code demand.
        self.assertNotEqual(result.error_code, "missing_signal")
        self.assertNotEqual(result.details.get("mode"), "signal_lifecycle")
        self.assertIn(result.details.get("mode"), {"direct_analysis", "bug_clarification"})

    def test_reply_to_file_routes_to_direct_analysis_even_when_prompt_has_no_keyword(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "analysis.md"
            html = Path(tmp) / "analysis.html"
            metadata.write_text("analysis", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            fake_lark.fetched_messages["om_current"] = json.dumps(
                {
                    "ok": True,
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_current",
                                "content": "@bot 看下这个",
                                "reply_to": "om_file_msg",
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )
            fake_lark.fetched_messages["om_file_msg"] = json.dumps(
                {
                    "ok": True,
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_file_msg",
                                "content": '<file key="file_v3_0011s_6d5d723c-ec0b-44f3-9908-a02be496b54g" name="Log.zip"/>',
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                intent_runner=FakeIntentRunner(enabled=False),
            )

            result = app.handle_event(event(message_id="om_current", content="@bot 看下这个"))

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "direct_analysis")
        self.assertEqual(len(fake_bug.requests), 1)
        self.assertEqual(fake_bug.requests[0].prompt, "看下这个")
        self.assertEqual(len(fake_bug.requests[0].resources), 1)
        self.assertEqual(
            fake_bug.requests[0].resources[0].value,
            "file_v3_0011s_6d5d723c-ec0b-44f3-9908-a02be496b54g",
        )
    def test_reply_to_folder_routes_to_direct_analysis_even_when_prompt_has_no_keyword(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "analysis.md"
            html = Path(tmp) / "analysis.html"
            metadata.write_text("analysis", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            fake_lark.fetched_messages["om_current"] = json.dumps(
                {
                    "ok": True,
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_current",
                                "content": "@bot 看下这个",
                                "reply_to": "om_folder_msg",
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )
            fake_lark.fetched_messages["om_folder_msg"] = json.dumps(
                {
                    "ok": True,
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_folder_msg",
                                "content": '<folder token="fldcnlog123" name="LogFolder"/>',
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                intent_runner=FakeIntentRunner(enabled=False),
            )

            result = app.handle_event(event(message_id="om_current", content="@bot 看下这个"))

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "direct_analysis")
        self.assertEqual(len(fake_bug.requests), 1)
        self.assertEqual(fake_bug.requests[0].resources[0].kind, "folder")
        self.assertEqual(fake_bug.requests[0].resources[0].value, "fldcnlog123")
        self.assertEqual(fake_bug.requests[0].resources[0].source_message_id, "om_folder_msg")
    def test_reply_to_file_generic_question_prefers_chat_not_direct_analysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "analysis.md"
            html = Path(tmp) / "analysis.html"
            metadata.write_text("analysis", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            fake_chat = FakeOmlxChatClient()
            fake_lark.fetched_messages["om_current"] = json.dumps(
                {
                    "ok": True,
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_current",
                                "content": "@bot 这个是什么？",
                                "reply_to": "om_file_msg",
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )
            fake_lark.fetched_messages["om_file_msg"] = json.dumps(
                {
                    "ok": True,
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_file_msg",
                                "content": '<file key="file_v3_0011s_6d5d723c-ec0b-44f3-9908-a02be496b54g" name="Log.zip"/>',
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                chat_client=fake_chat,
                intent_runner=FakeIntentRunner(enabled=False),
            )

            result = app.handle_event(event(message_id="om_current", content="@bot 这个是什么？"))

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "omlx_chat")
        self.assertEqual(len(fake_bug.requests), 0)
        self.assertEqual(len(fake_chat.prompts), 1)
    def test_reply_to_file_timed_diagnostic_question_routes_to_direct_analysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "analysis.md"
            html = Path(tmp) / "analysis.html"
            metadata.write_text("analysis", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            fake_lark.fetched_messages["om_diagnostic_file"] = json.dumps(
                {
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_diagnostic_file",
                                "msg_type": "file",
                                "content": {"file_key": "file_v3_diagnostic_log", "file_name": "Log.alog"},
                            }
                        ]
                    }
                },
                ensure_ascii=False,
            )
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"]),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                intent_runner=FakeIntentRunner(enabled=False),
            )

            result = app.handle_event(
                event(
                    event_id="evt_timed_diagnostic",
                    message_id="om_timed_diagnostic",
                    reply_to="om_diagnostic_file",
                    content="@bot 时间点15:11左右 为啥退出有图进入了无图？",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "direct_analysis")
        self.assertEqual(fake_bug.requests[0].resources[0].value, "file_v3_diagnostic_log")
    def test_result_card_progress_labels_do_not_hide_original_replied_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_lark.fetched_messages["om_result_card"] = json.dumps(
                {
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_result_card",
                                "msg_type": "interactive",
                                "reply_to": "om_analysis_request",
                                "content": json.dumps(
                                    {
                                        "elements": [
                                            {"tag": "markdown", "content": "file_uploading"},
                                            {"tag": "markdown", "content": "file_uploaded"},
                                        ]
                                    },
                                    ensure_ascii=False,
                                ),
                            }
                        ]
                    }
                },
                ensure_ascii=False,
            )
            fake_lark.fetched_messages["om_analysis_request"] = json.dumps(
                {
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_analysis_request",
                                "reply_to": "om_original_file",
                                "content": "@bot 分析这个文件",
                            }
                        ]
                    }
                },
                ensure_ascii=False,
            )
            fake_lark.fetched_messages["om_original_file"] = json.dumps(
                {
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_original_file",
                                "msg_type": "file",
                                "content": json.dumps(
                                    {"file_key": "file_v3_original_log", "file_name": "Log.zip"},
                                    ensure_ascii=False,
                                ),
                            }
                        ]
                    }
                },
                ensure_ascii=False,
            )
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp)),
                lark_client=fake_lark,
            )

            resources = app._fetch_referenced_message_resources(
                event(message_id="om_followup", reply_to="om_result_card", content="@bot 继续分析"),
                route_content="继续分析",
            )

        self.assertEqual([(item.kind, item.value) for item in resources], [("file", "file_v3_original_log")])
        self.assertEqual(resources[0].source_message_id, "om_original_file")
        self.assertEqual(resources[0].display_name, "Log.zip")
    def test_followup_reply_uses_saved_analysis_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html><body>根因是首帧超时</body></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            fake_chat = FakeOmlxChatClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                chat_client=fake_chat,
            )

            first = app.handle_event(
                event(
                    content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查3D启动时序"
                )
            )
            followup = app.handle_event(
                event(
                    event_id="evt_followup",
                    message_id="om_followup",
                    root_id="om_1",
                    parent_id="om_bot_reply",
                    content="@bot 这个结论的根因是什么",
                )
            )

        self.assertTrue(first.success)
        self.assertTrue(followup.success)
        self.assertEqual(followup.details["mode"], "bug_agent_followup")
        self.assertEqual(len(fake_bug.agent_followup_calls), 1)
        self.assertEqual(fake_bug.agent_followup_calls[0]["followup_text"], "这个结论的根因是什么")
        self.assertEqual(fake_chat.context_calls, [])
        self.assertIn("om_followup", _all_reply_message_ids(fake_lark))
        # Card replies don't include at-mention text, so only check text replies for that
        text_replies_for_followup = [r for r in fake_lark.replies if r["message_id"] == "om_followup"]
        if text_replies_for_followup:
            self.assertTrue(text_replies_for_followup[-1]["text"].startswith('<at user_id="ou_1"></at> '))
    def test_followup_reply_resolves_context_via_reply_to_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html><body>根因是首帧超时</body></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            fake_chat = FakeOmlxChatClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                chat_client=fake_chat,
            )

            first = app.handle_event(
                event(
                    message_id="om_original_request",
                    content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查3D启动时序"
                )
            )
            fake_lark.fetched_messages["om_bot_analysis_reply"] = """
{
  "ok": true,
  "data": {
    "messages": [
      {
        "message_id": "om_bot_analysis_reply",
        "reply_to": "om_original_request"
      }
    ]
  }
}
"""
            followup = app.handle_event(
                event(
                    event_id="evt_followup_chain",
                    message_id="om_followup_chain",
                    reply_to="om_bot_analysis_reply",
                    content="@bot 问题时间是2026-05-11 23:12分左右",
                )
            )

        self.assertTrue(first.success)
        self.assertTrue(followup.success)
        self.assertEqual(followup.details["mode"], "bug_agent_followup")
        self.assertEqual(len(fake_bug.agent_followup_calls), 1)
        self.assertEqual(fake_bug.agent_followup_calls[0]["followup_text"], "问题时间是2026-05-11 23:12分左右")
        self.assertIn("om_followup_chain", _all_reply_message_ids(fake_lark))
    def test_chat_followup_from_middle_reply_ignores_later_branch_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_chat = FakeOmlxChatClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                chat_client=fake_chat,
                intent_runner=FakeIntentRunner(enabled=False),
            )
            app.conversation_store.remember(
                root_message_id="om_root_chat",
                chat_id="ou_chat_1",
                mode="omlx_chat",
                request_text="第一问",
                summary_text="第一答",
                report_url="",
                report_excerpt="",
            )
            app.conversation_store.append_exchange("om_root_chat", user_text="第一问", assistant_text="第一答")
            app.conversation_store.remember_alias(alias_message_id="om_bot_reply_1", root_message_id="om_root_chat")
            app.conversation_store.append_exchange("om_root_chat", user_text="第二问", assistant_text="第二答")

            result = app.handle_event(
                event(
                    event_id="evt_mid_branch_followup",
                    message_id="om_mid_branch_followup",
                    chat_id="ou_chat_1",
                    chat_type="p2p",
                    reply_to="om_bot_reply_1",
                    content="第三问",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "analysis_followup")
        self.assertEqual(len(fake_chat.context_calls), 1)
        self.assertEqual(
            fake_chat.context_calls[0]["history"],
            [
                {"role": "user", "content": "第一问"},
                {"role": "assistant", "content": "第一答"},
            ],
        )
    def test_followup_reply_to_progress_card_resolves_original_bug_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html><body>SceneType=Main</body></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            config = BridgeConfig(
                dry_run=False,
                data_dir=Path(tmp),
                allowed_chats=["oc_denied"],
            )
            app = BridgeApp(
                config,
                lark_client=fake_lark,
                bug_runner=fake_bug,
                chat_client=FakeOmlxChatClient(),
            )

            first = app.handle_event(
                event(
                    message_id="om_original_request",
                    content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查3D启动时序",
                )
            )
            card_message_id = fake_lark.card_replies[0]["card_message_id"]
            followup = app.handle_event(
                event(
                    event_id="evt_followup_card_alias",
                    message_id="om_followup_card_alias",
                    reply_to=card_message_id,
                    content="@bot 最后的Unity 场景 SceneType是什么",
                )
            )

        self.assertTrue(first.success)
        self.assertTrue(followup.success)
        self.assertEqual(followup.details["mode"], "bug_agent_followup")
        self.assertEqual(followup.details["conversation_root_message_id"], "om_original_request")
        self.assertEqual(len(fake_bug.agent_followup_calls), 1)
        self.assertEqual(fake_bug.agent_followup_calls[0]["followup_text"], "最后的Unity 场景 SceneType是什么")
    def test_followup_reply_to_uploaded_html_resolves_context_when_event_lacks_reply_to(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html><body>SceneType=Main</body></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            config = BridgeConfig(
                dry_run=False,
                data_dir=Path(tmp),
                allowed_chats=["oc_denied"],
            )
            app = BridgeApp(
                config,
                lark_client=fake_lark,
                bug_runner=fake_bug,
                chat_client=FakeOmlxChatClient(),
            )

            first = app.handle_event(
                event(
                    message_id="om_original_request",
                    content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查3D启动时序",
                )
            )
            uploaded_html_message_id = fake_lark.files[-1]["message_id"]
            restarted_lark = FakeLarkClient()
            restarted_bug = FakeBugRunner(metadata, html)
            restarted_app = BridgeApp(
                config,
                lark_client=restarted_lark,
                bug_runner=restarted_bug,
                chat_client=FakeOmlxChatClient(),
            )
            restarted_lark.fetched_messages["om_followup_html"] = f"""
{{
  "ok": true,
  "data": {{
    "messages": [
      {{
        "message_id": "om_followup_html",
        "reply_to": "{uploaded_html_message_id}"
      }}
    ]
  }}
}}
"""
            followup = restarted_app.handle_event(
                event(
                    event_id="evt_followup_html",
                    message_id="om_followup_html",
                    content="@bot 最后的Unity 场景 SceneType是什么",
                )
            )

        self.assertTrue(first.success)
        self.assertTrue(followup.success)
        self.assertEqual(followup.details["mode"], "bug_agent_followup")
        self.assertEqual(followup.details["conversation_root_message_id"], "om_original_request")
        self.assertEqual(len(restarted_bug.agent_followup_calls), 1)
        self.assertEqual(restarted_bug.agent_followup_calls[0]["followup_text"], "最后的Unity 场景 SceneType是什么")
    def test_p2p_followup_reply_uses_saved_analysis_context_without_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html><body>根因是首帧超时</body></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            fake_chat = FakeOmlxChatClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                chat_client=fake_chat,
            )

            first = app.handle_event(
                event(
                    message_id="om_original_request",
                    chat_id="ou_chat_1",
                    chat_type="p2p",
                    content="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查3D启动时序",
                )
            )
            fake_lark.fetched_messages["om_bot_analysis_reply"] = """
{
  "ok": true,
  "data": {
    "messages": [
      {
        "message_id": "om_bot_analysis_reply",
        "reply_to": "om_original_request"
      }
    ]
  }
}
"""
            followup = app.handle_event(
                event(
                    event_id="evt_followup_p2p",
                    message_id="om_followup_p2p",
                    chat_id="ou_chat_1",
                    chat_type="p2p",
                    reply_to="om_bot_analysis_reply",
                    content="这个结论的根因是什么",
                )
            )

        self.assertTrue(first.success)
        self.assertTrue(followup.success)
        self.assertEqual(followup.details["mode"], "bug_agent_followup")
        self.assertEqual(len(fake_bug.agent_followup_calls), 1)
        self.assertEqual(fake_bug.agent_followup_calls[0]["followup_text"], "这个结论的根因是什么")
        self.assertEqual(fake_chat.context_calls, [])
        self.assertIn("om_followup_p2p", _all_reply_message_ids(fake_lark))
        text_replies_for_p2p = [r for r in fake_lark.replies if r["message_id"] == "om_followup_p2p"]
        if text_replies_for_p2p:
            self.assertFalse(text_replies_for_p2p[-1]["text"].startswith("<at "))
    def test_group_addr2line_followup_in_reply_chain_without_mention(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_addr2line = FakeAddr2LineRunner()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                addr2line_runner=fake_addr2line,
            )
            app.conversation_store.remember(
                root_message_id="om_addr_root",
                chat_id="oc_denied",
                mode="addr2line_resolve",
                request_text="@bot ROM版本号XMARTQGZHE29E5_V6.1.0.8810_20260327200937.9_REV01_USER_Release 反解堆栈",
                summary_text="addr2line 反解完成",
                report_url="",
                report_excerpt="",
            )
            app.conversation_store.remember_alias(
                alias_message_id="om_addr_reply",
                root_message_id="om_addr_root",
            )
            app.activity_store.record_result(
                event(message_id="om_addr_root", content="@bot ROM版本号XMARTQGZHE29E5_V6.1.0.8810_20260327200937.9_REV01_USER_Release 反解堆栈"),
                TaskResult(
                    success=True,
                    message="addr2line 反解完成",
                    details={
                        "mode": "addr2line_resolve",
                        "rom_version": "XMARTQGZHE29E5_V6.1.0.8810_20260327200937.9_REV01_USER_Release",
                        "symbol_version": "V6.1.0_20260327175820_Release",
                        "symbol_version_kind": "apk",
                        "resources": [
                            {"kind": "local", "value": str(Path(tmp) / "crash_bundle.zip")},
                        ],
                    },
                ),
            )

            result = app.handle_event(
                event(
                    event_id="evt_group_addr_followup_no_mention",
                    message_id="om_group_addr_followup_no_mention",
                    reply_to="om_addr_reply",
                    content="再反解一次",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "addr2line_resolve")
        self.assertEqual(len(fake_addr2line.requests), 1)
        self.assertEqual(
            fake_addr2line.requests[0].rom_version,
            "XMARTQGZHE29E5_V6.1.0.8810_20260327200937.9_REV01_USER_Release",
        )
        self.assertEqual(fake_addr2line.requests[0].resources[0].value, str(Path(tmp) / "crash_bundle.zip"))
    def test_group_addr2line_followup_retry_once_reuses_addr2line_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_addr2line = FakeAddr2LineRunner()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                addr2line_runner=fake_addr2line,
                chat_client=FakeOmlxChatClient(),
            )
            app.conversation_store.remember(
                root_message_id="om_addr_root_retry",
                chat_id="oc_denied",
                mode="addr2line_resolve",
                request_text="@bot ROM版本号XMARTQGZHE29E5_V6.1.0.8810_20260327200937.9_REV01_USER_Release 反解堆栈",
                summary_text="addr2line 反解完成",
                report_url="",
                report_excerpt="",
            )
            app.conversation_store.remember_alias(
                alias_message_id="om_addr_reply_retry",
                root_message_id="om_addr_root_retry",
            )
            app.activity_store.record_result(
                event(message_id="om_addr_root_retry", content="@bot ROM版本号XMARTQGZHE29E5_V6.1.0.8810_20260327200937.9_REV01_USER_Release 反解堆栈"),
                TaskResult(
                    success=True,
                    message="addr2line 反解完成",
                    details={
                        "mode": "addr2line_resolve",
                        "rom_version": "XMARTQGZHE29E5_V6.1.0.8810_20260327200937.9_REV01_USER_Release",
                        "symbol_version": "V6.1.0_20260327175820_Release",
                        "symbol_version_kind": "apk",
                        "resources": [
                            {"kind": "local", "value": str(Path(tmp) / "retry_bundle.zip")},
                        ],
                    },
                ),
            )

            result = app.handle_event(
                event(
                    event_id="evt_group_addr_retry_once",
                    message_id="om_group_addr_retry_once",
                    reply_to="om_addr_reply_retry",
                    content="重试一次",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "addr2line_resolve")
        self.assertEqual(len(fake_addr2line.requests), 1)
        self.assertEqual(fake_addr2line.requests[0].resources[0].value, str(Path(tmp) / "retry_bundle.zip"))
    def test_group_rom_lookup_followup_in_reply_chain_without_mention(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_rom = FakeRomVersionRunner()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                rom_version_runner=fake_rom,
            )
            app.conversation_store.remember(
                root_message_id="om_rom_root",
                chat_id="oc_denied",
                mode="rom_version_lookup",
                request_text="@bot ROM版本号XMARTQGZHE29E5_V6.1.0.8810_20260327200937.9_REV01_USER_Release 查导航版本",
                summary_text="ROM 版本查询完成",
                report_url="",
                report_excerpt="",
            )
            app.conversation_store.remember_alias(
                alias_message_id="om_rom_reply",
                root_message_id="om_rom_root",
            )
            app.activity_store.record_result(
                event(message_id="om_rom_root", content="@bot ROM版本号XMARTQGZHE29E5_V6.1.0.8810_20260327200937.9_REV01_USER_Release 查导航版本"),
                TaskResult(
                    success=True,
                    message="ROM 版本查询完成",
                    details={
                        "mode": "rom_version_lookup",
                        "rom_version": "XMARTQGZHE29E5_V6.1.0.8810_20260327200937.9_REV01_USER_Release",
                        "required_outputs": {
                            "navigation_version": "V6.1.0_20260327175820_Release",
                        },
                    },
                ),
            )

            result = app.handle_event(
                event(
                    event_id="evt_group_rom_followup_no_mention",
                    message_id="om_group_rom_followup_no_mention",
                    reply_to="om_rom_reply",
                    content="再查一次",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "rom_version_lookup")
        self.assertEqual(len(fake_rom.requests), 1)
        self.assertEqual(
            fake_rom.requests[0].rom_version,
            "XMARTQGZHE29E5_V6.1.0.8810_20260327200937.9_REV01_USER_Release",
        )

    def test_group_bare_hex_stack_reply_to_rom_lookup_routes_to_addr2line(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_addr2line = FakeAddr2LineRunner()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                addr2line_runner=fake_addr2line,
            )
            rom = "XMARTQGZHE29E5_V6.1.0.8810_20260327200937.9_REV01_USER_Release"
            app.conversation_store.remember(
                root_message_id="om_rom_root_for_addr",
                chat_id="oc_denied",
                mode="rom_version_lookup",
                request_text=f"@bot {rom} 查导航版本",
                summary_text="ROM 版本查询完成",
                report_url="",
                report_excerpt="",
            )
            app.conversation_store.remember_alias(
                alias_message_id="om_rom_reply_for_addr",
                root_message_id="om_rom_root_for_addr",
            )
            app.activity_store.record_result(
                event(message_id="om_rom_root_for_addr", content=f"@bot {rom} 查导航版本"),
                TaskResult(
                    success=True,
                    message="ROM 版本查询完成",
                    details={
                        "mode": "rom_version_lookup",
                        "rom_version": rom,
                        "required_outputs": {
                            "navigation_version": "V6.1.0_20260327175820_Release",
                            "symbol_table_url": (
                                "http://maven.xiaopeng.local/service/rest/repository/browse/"
                                "xp_android_release/com/xiaopeng/lib/envirodrive_so/V6.1.0_20260327175820_Release/"
                            ),
                            "napa5_download_url": "http://10.99.26.55/rom/napa/lib_napa5/6.1.0-test",
                        },
                    },
                ),
            )

            result = app.handle_event(
                event(
                    event_id="evt_group_bare_hex_stack_after_rom",
                    message_id="om_group_bare_hex_stack_after_rom",
                    reply_to="om_rom_reply_for_addr",
                    content=(
                        "0000000000085304 /apex/com.android.runtime/lib64/bionic/libc.so (__memcpy+276)\n"
                        "00000000049f7194 /system/app/xp_envirodrive-mainland/lib/arm64/libil2cpp.so\n"
                        "0000000004872b6c /system/app/xp_envirodrive-mainland/lib/arm64/libil2cpp.so\n"
                        "堆栈解析"
                    ),
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "addr2line_resolve")
        self.assertEqual(len(fake_addr2line.requests), 1)
        request = fake_addr2line.requests[0]
        self.assertEqual(request.rom_version, rom)
        self.assertEqual(request.apk_version, "V6.1.0_20260327175820_Release")
        self.assertEqual(request.napa5_download_url, "http://10.99.26.55/rom/napa/lib_napa5/6.1.0-test")
        self.assertIn("00000000049f7194", request.addr_text)
        self.assertIn("libil2cpp.so", request.addr_text)
    def test_followup_reanalysis_fetches_current_message_reply_to_when_event_lacks_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html><body>根因是首帧超时</body></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            fake_chat = FakeOmlxChatClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                chat_client=fake_chat,
            )

            first = app.handle_event(
                event(
                    message_id="om_original_request",
                    content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查3D启动时序"
                )
            )
            fake_lark.fetched_messages["om_followup_missing_reply"] = """
{
  "ok": true,
  "data": {
    "messages": [
      {
        "message_id": "om_followup_missing_reply",
        "reply_to": "om_bot_analysis_reply"
      }
    ]
  }
}
"""
            fake_lark.fetched_messages["om_bot_analysis_reply"] = """
{
  "ok": true,
  "data": {
    "messages": [
      {
        "message_id": "om_bot_analysis_reply",
        "reply_to": "om_original_request"
      }
    ]
  }
}
"""
            followup = app.handle_event(
                event(
                    event_id="evt_followup_missing_reply",
                    message_id="om_followup_missing_reply",
                    content="@bot 修复问题时间 23:12分 重新分析下",
                )
            )
            context = app.conversation_store.lookup("om_original_request")

        self.assertTrue(first.success)
        self.assertTrue(followup.success)
        self.assertEqual(followup.details["mode"], "bug_reanalysis")
        self.assertEqual(len(fake_bug.requests), 1)
        self.assertEqual(len(fake_bug.reanalysis_calls), 1)
        self.assertEqual(fake_bug.reanalysis_calls[0]["followup_text"], "修复问题时间 23:12分 重新分析下")
        self.assertEqual(followup.details["conversation_root_message_id"], "om_original_request")
        self.assertIsNotNone(context)
        assert context is not None
        self.assertEqual(context.root_message_id, "om_original_request")
        self.assertTrue(context.history)
        session = app.activity_store.get_session("om_original_request")
        self.assertIsNotNone(session)
        assert session is not None
        agent_progress = [item for item in session["progress"] if item["stage"] == "bug_agent_summary"]
        self.assertTrue(agent_progress)
        self.assertEqual(agent_progress[-1]["details"]["provider_session_id"], "sess_123")
    def test_bug_reanalysis_keeps_original_request_text_in_conversation_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html><body>根因是首帧超时</body></html>", encoding="utf-8")
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=FakeLarkClient(),
                bug_runner=FakeBugRunner(metadata, html),
            )

            original = app.handle_event(
                event(
                    event_id="evt_bug_original",
                    message_id="om_bug_original",
                    content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查3D启动生命周期",
                )
            )
            self.assertTrue(original.success)
            initial_context = app.conversation_store.lookup("om_bug_original")
            self.assertIsNotNone(initial_context)
            assert initial_context is not None
            original_request_text = initial_context.request_text

            followup = app.handle_event(
                event(
                    event_id="evt_bug_reanalysis",
                    message_id="om_bug_reanalysis",
                    reply_to="om_bug_original",
                    root_id="om_bug_original",
                    content="@bot 重新分析",
                )
            )
            updated_context = app.conversation_store.lookup("om_bug_original")

        self.assertTrue(followup.success)
        self.assertIsNotNone(updated_context)
        assert updated_context is not None
        self.assertEqual(updated_context.request_text, original_request_text)
        self.assertNotIn("追问/修正：", updated_context.request_text)
    def test_followup_reanalysis_fetches_current_message_reply_to_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html><body>根因是首帧超时</body></html>", encoding="utf-8")
            config = BridgeConfig(
                dry_run=False,
                data_dir=Path(tmp),
                allowed_chats=["oc_denied"],
                job_retention=RealBridgeConfig().job_retention.__class__(
                    enabled=True,
                    max_age_hours=6,
                    bug_cache_max_age_hours=24,
                    purge_all_on_listen_start=True,
                    cleanup_interval_seconds=60,
                ),
            )

            first_lark = FakeLarkClient()
            first_app = BridgeApp(config, lark_client=first_lark, bug_runner=FakeBugRunner(metadata, html))
            first = first_app.handle_event(
                event(
                    message_id="om_original_request",
                    content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查3D启动时序"
                )
            )

            second_lark = FakeLarkClient()
            second_lark.fetched_messages["om_followup_missing_reply"] = """
{
  "ok": true,
  "data": {
    "messages": [
      {
        "message_id": "om_followup_missing_reply",
        "reply_to": "om_bot_analysis_reply"
      }
    ]
  }
}
"""
            second_lark.fetched_messages["om_bot_analysis_reply"] = """
{
  "ok": true,
  "data": {
    "messages": [
      {
        "message_id": "om_bot_analysis_reply",
        "reply_to": "om_original_request"
      }
    ]
  }
}
"""
            second_bug = FakeBugRunner(metadata, html)
            second_app = BridgeApp(config, lark_client=second_lark, bug_runner=second_bug)
            followup = second_app.handle_event(
                event(
                    event_id="evt_followup_missing_reply_restart",
                    message_id="om_followup_missing_reply",
                    content="@bot 修复问题时间 23:12分 重新分析下",
                )
            )

        self.assertTrue(first.success)
        self.assertTrue(followup.success)
        self.assertEqual(followup.details["mode"], "bug_reanalysis")
        self.assertEqual(len(second_bug.reanalysis_calls), 1)
        self.assertEqual(followup.details["conversation_root_message_id"], "om_original_request")
    def test_group_followup_reply_chain_is_resolved_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html><body>根因是首帧超时</body></html>", encoding="utf-8")
            config = BridgeConfig(
                dry_run=False,
                data_dir=Path(tmp),
                allowed_chats=["oc_denied"],
                job_retention=RealBridgeConfig().job_retention.__class__(
                    enabled=True,
                    max_age_hours=6,
                    bug_cache_max_age_hours=24,
                    purge_all_on_listen_start=True,
                    cleanup_interval_seconds=60,
                ),
            )

            first_lark = FakeLarkClient()
            first_app = BridgeApp(config, lark_client=first_lark, bug_runner=FakeBugRunner(metadata, html))
            first = first_app.handle_event(
                event(
                    message_id="om_original_request",
                    content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查3D启动时序"
                )
            )

            second_lark = FakeLarkClient()
            second_lark.fetched_messages["om_followup_group_restart"] = """
{
  "ok": true,
  "data": {
    "messages": [
      {
        "message_id": "om_followup_group_restart",
        "reply_to": "om_bot_analysis_reply"
      }
    ]
  }
}
"""
            second_lark.fetched_messages["om_bot_analysis_reply"] = """
{
  "ok": true,
  "data": {
    "messages": [
      {
        "message_id": "om_bot_analysis_reply",
        "reply_to": "om_original_request"
      }
    ]
  }
}
"""
            second_bug = FakeBugRunner(metadata, html)
            second_app = BridgeApp(config, lark_client=second_lark, bug_runner=second_bug)
            followup = second_app.handle_event(
                event(
                    event_id="evt_followup_group_restart",
                    message_id="om_followup_group_restart",
                    content="@bot 继续把刚才那批日志往下查",
                )
            )

        self.assertTrue(first.success)
        self.assertTrue(followup.success)
        self.assertEqual(followup.details["mode"], "bug_agent_followup")
        self.assertEqual(len(second_bug.agent_followup_calls), 1)
        self.assertEqual(followup.details["conversation_root_message_id"], "om_original_request")
    def test_followup_reanalysis_without_reply_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html><body>根因是首帧超时</body></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
            )

            first = app.handle_event(
                event(
                    message_id="om_original_request",
                    content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查3D启动时序"
                )
            )
            followup = app.handle_event(
                event(
                    event_id="evt_followup_latest",
                    message_id="om_followup_latest",
                    content="@bot 修正问题时间为 23:12分 重新分析",
                )
            )

        self.assertTrue(first.success)
        self.assertFalse(followup.success)
        self.assertEqual(followup.error_code, "missing_followup_reply")
        self.assertEqual(len(fake_bug.requests), 1)
        self.assertEqual(len(fake_bug.reanalysis_calls), 0)
    def test_p2p_followup_without_reply_is_rejected_without_at_hint(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html><body>根因是首帧超时</body></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
            )

            first = app.handle_event(
                event(
                    message_id="om_original_request",
                    chat_id="ou_chat_1",
                    chat_type="p2p",
                    content="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查3D启动时序",
                )
            )
            followup = app.handle_event(
                event(
                    event_id="evt_followup_p2p_latest",
                    message_id="om_followup_p2p_latest",
                    chat_id="ou_chat_1",
                    chat_type="p2p",
                    content="修正问题时间为 23:12分 重新分析",
                )
            )

        self.assertTrue(first.success)
        self.assertFalse(followup.success)
        self.assertEqual(followup.error_code, "missing_followup_reply")
        self.assertIn("直接回复对应那条分析消息", followup.message)
        self.assertNotIn("@机器人", followup.message)
        self.assertEqual(len(fake_bug.reanalysis_calls), 0)
    def test_followup_reply_with_bug_link_stays_in_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html><body>根因是首帧超时</body></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            fake_chat = FakeOmlxChatClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                chat_client=fake_chat,
            )

            first = app.handle_event(
                event(
                    message_id="om_original_request",
                    content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查3D启动时序"
                )
            )
            followup = app.handle_event(
                event(
                    event_id="evt_followup_bug_link",
                    message_id="om_followup_bug_link",
                    root_id="om_original_request",
                    parent_id="om_bot_reply",
                    content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 时间点修正为23:12分",
                )
            )

        self.assertTrue(first.success)
        self.assertTrue(followup.success)
        self.assertEqual(followup.details["mode"], "bug_reanalysis")
        self.assertEqual(len(fake_bug.requests), 1)
        self.assertEqual(len(fake_bug.reanalysis_calls), 1)
        self.assertEqual(fake_bug.reanalysis_calls[0]["followup_text"], "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 时间点修正为23:12分")
        self.assertEqual(fake_chat.context_calls, [])
    def test_bug_followup_reply_routes_to_bug_agent_instead_of_context_chat(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html><body>根因是首帧超时</body></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            fake_chat = FakeOmlxChatClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                chat_client=fake_chat,
            )

            first = app.handle_event(
                event(
                    message_id="om_original_request",
                    content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查3D启动时序"
                )
            )
            followup = app.handle_event(
                event(
                    event_id="evt_followup_logd",
                    message_id="om_followup_logd",
                    root_id="om_original_request",
                    parent_id="om_bot_reply",
                    content="@bot 没有数据 你不会分析 logd里面的日志吗",
                )
            )

        self.assertTrue(first.success)
        self.assertTrue(followup.success)
        self.assertEqual(followup.details["mode"], "bug_agent_followup")
        self.assertEqual(len(fake_bug.agent_followup_calls), 1)
        self.assertEqual(fake_bug.agent_followup_calls[0]["followup_text"], "没有数据 你不会分析 logd里面的日志吗")
        self.assertEqual(fake_chat.context_calls, [])
        all_reply_ids = [r["message_id"] for r in fake_lark.replies] + [r["message_id"] for r in fake_lark.card_replies]
        self.assertIn("om_followup_logd", all_reply_ids)
