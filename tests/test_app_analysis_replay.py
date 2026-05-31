from _app_base import *  # noqa: F401,F403
from _app_base import _AppTestBase


class AppAnalysisReplayTests(_AppTestBase):
    def test_perception_followup_replay_gate_preempts_fresh_missing_log_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            html = Path(tmp) / "report.html"
            html.write_text("<html></html>", encoding="utf-8")
            prepared_dir = Path(tmp) / "prepared"
            prepared_dir.mkdir()
            (prepared_dir / "perception.log").write_text("perception data", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_runner = FakePerceptionRunner(html)
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp) / "data", allowed_chats=["oc_denied"]),
                lark_client=fake_lark,
                perception_runner=fake_runner,
            )
            original_event = event(
                event_id="evt_perception_route_root",
                message_id="om_perception_route_root",
                content="@bot 感知数据总结",
            )
            app.activity_store.record_event(original_event)
            app.activity_store.record_result(
                original_event,
                TaskResult(
                    success=True,
                    message="感知数据总结完成",
                    details={"mode": "perception_summary", "prepared_log_input": str(prepared_dir)},
                ),
            )
            app.conversation_store.remember(
                root_message_id="om_perception_route_root",
                chat_id="oc_denied",
                mode="perception_summary",
                request_text="感知数据总结",
                summary_text="感知数据总结完成",
                report_url="http://report",
                report_excerpt="感知数据已有摘要",
            )
            app.conversation_store.remember_alias(
                alias_message_id="om_perception_route_reply",
                root_message_id="om_perception_route_root",
            )

            result = app.handle_event(
                event(
                    event_id="evt_perception_route_retry",
                    message_id="om_perception_route_retry",
                    reply_to="om_perception_route_reply",
                    content="总结当前感知数据 重新分析",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "perception_summary")
        self.assertEqual(len(fake_runner.requests), 1)
        resource_paths = [Path(item.value) for item in fake_runner.requests[0].resources if item.kind == "local"]
        self.assertIn(prepared_dir.resolve(), resource_paths)

    def test_perception_followup_replay_reuses_prepared_local_log_without_original_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            html = Path(tmp) / "report.html"
            html.write_text("<html></html>", encoding="utf-8")
            prepared_dir = Path(tmp) / "prepared"
            selected_dir = Path(tmp) / "selected"
            prepared_dir.mkdir()
            selected_dir.mkdir()
            (prepared_dir / "perception.log").write_text("perception data", encoding="utf-8")
            (selected_dir / "selected.log").write_text("selected", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_runner = FakePerceptionRunner(html)
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp) / "data", allowed_chats=["oc_denied"]),
                lark_client=fake_lark,
                perception_runner=fake_runner,
            )
            original_event = event(
                event_id="evt_perception_prepared_root",
                message_id="om_perception_prepared_root",
                content="@bot 感知数据总结",
            )
            app.activity_store.record_event(original_event)
            app.activity_store.record_result(
                original_event,
                TaskResult(
                    success=True,
                    message="感知数据总结完成",
                    details={
                        "mode": "perception_summary",
                        "prepared_log_input": str(prepared_dir),
                        "selected_log_input": str(selected_dir),
                    },
                ),
            )
            app.conversation_store.remember(
                root_message_id="om_perception_prepared_root",
                chat_id="oc_denied",
                mode="perception_summary",
                request_text="感知数据总结",
                summary_text="感知数据总结完成",
                report_url="http://report",
                report_excerpt="感知数据已有摘要",
            )
            app.conversation_store.remember_alias(
                alias_message_id="om_perception_prepared_reply",
                root_message_id="om_perception_prepared_root",
            )

            result = app.handle_event(
                event(
                    event_id="evt_perception_prepared_retry",
                    message_id="om_perception_prepared_retry",
                    reply_to="om_perception_prepared_reply",
                    content="重新分析",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "perception_summary")
        self.assertEqual(len(fake_runner.requests), 1)
        resource_paths = [Path(item.value) for item in fake_runner.requests[0].resources if item.kind == "local"]
        self.assertIn(prepared_dir.resolve(), resource_paths)

    def test_perception_followup_retry_reruns_perception_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            html = Path(tmp) / "report.html"
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_runner = FakePerceptionRunner(html)
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"]),
                lark_client=fake_lark,
                perception_runner=fake_runner,
            )
            app.conversation_store.remember(
                root_message_id="om_perception_root",
                chat_id="oc_denied",
                mode="perception_summary",
                request_text="感知数据总结 https://example.com/log.zip",
                summary_text="感知数据总结完成",
                report_url="http://report",
                report_excerpt="",
            )
            app.conversation_store.remember_alias(
                alias_message_id="om_perception_reply",
                root_message_id="om_perception_root",
            )

            result = app.handle_event(
                event(
                    event_id="evt_perception_retry",
                    message_id="om_perception_retry",
                    reply_to="om_perception_reply",
                    content="重新分析",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "perception_summary")
        self.assertEqual(len(fake_runner.requests), 1)

    def test_replay_ordinary_question_uses_context_chat_without_rerunning_signal_or_perception(self):
        scenarios = (
            ("signal_lifecycle", "132002 https://example.com/log.zip", FakeSignalHandler(), None),
            ("perception_summary", "感知数据总结 https://example.com/log.zip", None, "report.html"),
        )
        for mode, request_text, signal_handler, html_name in scenarios:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                fake_lark = FakeLarkClient()
                fake_chat = FakeOmlxChatClient()
                html = Path(tmp) / html_name if html_name else Path(tmp) / "unused.html"
                html.write_text("<html></html>", encoding="utf-8")
                fake_perception = FakePerceptionRunner(html)
                app = BridgeApp(
                    BridgeConfig(dry_run=False, data_dir=Path(tmp) / "data", allowed_chats=["oc_denied"]),
                    lark_client=fake_lark,
                    handler=signal_handler or FakeSignalHandler(),
                    perception_runner=fake_perception,
                    chat_client=fake_chat,
                )
                app.conversation_store.remember(
                    root_message_id=f"om_{mode}_question_root",
                    chat_id="oc_denied",
                    mode=mode,
                    request_text=request_text,
                    summary_text="上一轮分析完成",
                    report_url="http://report",
                    report_excerpt="关键结论：日志显示链路正常",
                )
                app.conversation_store.remember_alias(
                    alias_message_id=f"om_{mode}_question_reply",
                    root_message_id=f"om_{mode}_question_root",
                )

                result = app.handle_event(
                    event(
                        event_id=f"evt_{mode}_question",
                        message_id=f"om_{mode}_question",
                        reply_to=f"om_{mode}_question_reply",
                        content="这个结论什么意思",
                    )
                )

                self.assertTrue(result.success)
                self.assertEqual(result.details["mode"], "analysis_followup")
                self.assertEqual(len(fake_chat.context_calls), 1)
                if signal_handler is not None:
                    self.assertEqual(signal_handler.requests, [])
                self.assertEqual(fake_perception.requests, [])

    def test_signal_followup_correction_replays_current_signal_with_prepared_local_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            prepared_dir = Path(tmp) / "prepared"
            prepared_dir.mkdir()
            (prepared_dir / "main.log").write_text("05-20 12:00:00 signal 132003", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_handler = FakeSignalHandler()
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp) / "data", allowed_chats=["oc_denied"]),
                lark_client=fake_lark,
                handler=fake_handler,
            )
            original_event = event(
                event_id="evt_signal_correction_root",
                message_id="om_signal_correction_root",
                content="@bot 132002",
            )
            app.activity_store.record_event(original_event)
            app.activity_store.record_result(
                original_event,
                TaskResult(
                    success=True,
                    message="信号分析完成",
                    details={"mode": "signal_lifecycle", "prepared_log_input": str(prepared_dir)},
                ),
            )
            app.conversation_store.remember(
                root_message_id="om_signal_correction_root",
                chat_id="oc_denied",
                mode="signal_lifecycle",
                request_text="132002",
                summary_text="信号分析完成",
                report_url="http://report",
                report_excerpt="132002 已完成分析",
            )
            app.conversation_store.remember_alias(
                alias_message_id="om_signal_correction_reply",
                root_message_id="om_signal_correction_root",
            )

            result = app.handle_event(
                event(
                    event_id="evt_signal_correction_retry",
                    message_id="om_signal_correction_retry",
                    reply_to="om_signal_correction_reply",
                    content="改成 132003 重新分析",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "signal_lifecycle")
        self.assertEqual(len(fake_handler.requests), 1)
        self.assertEqual(fake_handler.requests[0].signal, "132003")
        resource_paths = [Path(item.value) for item in fake_handler.requests[0].resources if item.kind == "local"]
        self.assertIn(prepared_dir.resolve(), resource_paths)

    def test_signal_followup_non_retry_falls_through_to_chat(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_handler = FakeSignalHandler()
            fake_chat = FakeOmlxChatClient()
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"]),
                lark_client=fake_lark,
                handler=fake_handler,
                chat_client=fake_chat,
            )
            app.conversation_store.remember(
                root_message_id="om_signal_chat_root",
                chat_id="oc_denied",
                mode="signal_lifecycle",
                request_text="132002 https://example.com/log.zip",
                summary_text="信号分析完成",
                report_url="http://report",
                report_excerpt="这个信号在16:06到达Unity",
            )
            app.conversation_store.remember_alias(
                alias_message_id="om_signal_chat_reply",
                root_message_id="om_signal_chat_root",
            )

            result = app.handle_event(
                event(
                    event_id="evt_signal_chat",
                    message_id="om_signal_chat",
                    reply_to="om_signal_chat_reply",
                    content="这个结论什么意思",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(len(fake_handler.requests), 0)
        self.assertEqual(len(fake_chat.context_calls), 1)

    def test_signal_followup_replay_reuses_prepared_local_log_without_original_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            prepared_dir = Path(tmp) / "prepared"
            selected_dir = Path(tmp) / "selected"
            prepared_dir.mkdir()
            selected_dir.mkdir()
            (prepared_dir / "main.log").write_text("05-20 12:00:00 signal 132002", encoding="utf-8")
            (selected_dir / "selected.log").write_text("selected", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_handler = FakeSignalHandler()
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp) / "data", allowed_chats=["oc_denied"]),
                lark_client=fake_lark,
                handler=fake_handler,
            )
            original_event = event(
                event_id="evt_signal_prepared_root",
                message_id="om_signal_prepared_root",
                content="@bot 132002",
            )
            app.activity_store.record_event(original_event)
            app.activity_store.record_result(
                original_event,
                TaskResult(
                    success=True,
                    message="信号分析完成",
                    details={
                        "mode": "signal_lifecycle",
                        "prepared_log_input": str(prepared_dir),
                        "selected_log_input": str(selected_dir),
                    },
                ),
            )
            app.conversation_store.remember(
                root_message_id="om_signal_prepared_root",
                chat_id="oc_denied",
                mode="signal_lifecycle",
                request_text="132002",
                summary_text="信号分析完成",
                report_url="http://report",
                report_excerpt="132002 已完成分析",
            )
            app.conversation_store.remember_alias(
                alias_message_id="om_signal_prepared_reply",
                root_message_id="om_signal_prepared_root",
            )

            result = app.handle_event(
                event(
                    event_id="evt_signal_prepared_retry",
                    message_id="om_signal_prepared_retry",
                    reply_to="om_signal_prepared_reply",
                    content="重新分析",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "signal_lifecycle")
        self.assertEqual(len(fake_handler.requests), 1)
        self.assertEqual(fake_handler.requests[0].signal, "132002")
        resource_paths = [Path(item.value) for item in fake_handler.requests[0].resources if item.kind == "local"]
        self.assertIn(prepared_dir.resolve(), resource_paths)

    def test_signal_followup_retry_reruns_signal_analysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_handler = FakeSignalHandler()
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"]),
                lark_client=fake_lark,
                handler=fake_handler,
            )
            app.conversation_store.remember(
                root_message_id="om_signal_root",
                chat_id="oc_denied",
                mode="signal_lifecycle",
                request_text="132002 https://example.com/log.zip",
                summary_text="信号分析完成",
                report_url="http://report",
                report_excerpt="",
            )
            app.conversation_store.remember_alias(
                alias_message_id="om_signal_reply",
                root_message_id="om_signal_root",
            )

            result = app.handle_event(
                event(
                    event_id="evt_signal_retry",
                    message_id="om_signal_retry",
                    reply_to="om_signal_reply",
                    content="重新分析",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "signal_lifecycle")
        self.assertEqual(len(fake_handler.requests), 1)
        self.assertEqual(fake_handler.requests[0].signal, "132002")
