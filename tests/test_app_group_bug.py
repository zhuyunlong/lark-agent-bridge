from _app_base import *  # noqa: F401,F403
from _app_base import _AppTestBase


class AppGroupBugTests(_AppTestBase):
    def test_direct_analysis_failure_card_retry_reruns_direct_analysis_instead_of_omlx_followup(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "analysis.md"
            html = Path(tmp) / "analysis.html"
            metadata.write_text("analysis", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            fake_chat = FakeOmlxChatClient()
            fake_lark.fetched_messages["om_retry_msg"] = json.dumps(
                {
                    "ok": True,
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_retry_msg",
                                "reply_to": "om_failure_card",
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )
            fake_lark.fetched_messages["om_failure_card"] = json.dumps(
                {
                    "ok": True,
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_failure_card",
                                "reply_to": "om_direct_root",
                                "content": "<card title=\"📋 文件分析\">下载失败</card>",
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )
            fake_lark.fetched_messages["om_direct_root"] = json.dumps(
                {
                    "ok": True,
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_direct_root",
                                "reply_to": "om_file_msg",
                                "content": "@bot 基于SRViolationHandler.kt源码分析 时间点5月22日 7:46 分析超速状态",
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
                                "msg_type": "file",
                                "content": '<file key="file_v3_00120_d905735f-5edc-4122-91ef-ad8d306f401g" name="L1NSPGHB3SB010669log0.zip"/>',
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
            )
            root_event = event(
                event_id="evt_direct_root",
                message_id="om_direct_root",
                content="@bot 基于SRViolationHandler.kt源码分析 时间点5月22日 7:46 分析超速状态",
            )
            app.activity_store.record_event(root_event)
            app.activity_store.record_result(
                root_event,
                TaskResult(
                    success=False,
                    message="下载失败",
                    job_id="job_direct_failed",
                    job_dir=Path(tmp) / "jobs" / "job_direct_failed",
                    details={
                        "mode": "direct_analysis",
                        "conversation_root_message_id": "om_direct_root",
                        "user_request_text": "基于SRViolationHandler.kt源码分析 时间点5月22日 7:46 分析超速状态",
                    },
                ),
            )

            result = app.handle_event(
                event(
                    event_id="evt_retry_msg",
                    message_id="om_retry_msg",
                    content="@bot 重新分析",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "direct_analysis")
        self.assertEqual(len(fake_bug.requests), 1)
        self.assertEqual(fake_chat.context_calls, [])
    def test_group_message_without_bot_mention_is_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_chat = FakeOmlxChatClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    lark=LarkOptions(bot_name="bot"),
                ),
                lark_client=fake_lark,
                chat_client=fake_chat,
            )

            result = app.handle_event(event(content="https://gic-ai-center.xiaopeng.com/skills/122"))

        self.assertTrue(result.success)
        self.assertTrue(result.skipped)
        self.assertEqual(result.details["mode"], "not_addressed")
        self.assertEqual(fake_chat.prompts, [])
        self.assertEqual(fake_lark.sent, [])
    def test_p2p_plain_chat_uses_omlx_instead_of_unsupported(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_chat = FakeOmlxChatClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_allowed"],
                ),
                lark_client=fake_lark,
                chat_client=fake_chat,
            )

            result = app.handle_event(
                event(
                    event_id="evt_p2p_chat",
                    chat_id="ou_chat_1",
                    chat_type="p2p",
                    content="讲个笑话",
                )
            )

        self.assertTrue(result.success)
        self.assertFalse(result.skipped)
        self.assertEqual(result.details["mode"], "omlx_chat")
        self.assertEqual(fake_chat.prompts, ["讲个笑话"])
        self.assertEqual(fake_lark.replies[0]["message_id"], "om_1")
        self.assertEqual(fake_lark.replies[0]["text"], "omlx 模型回复")
    def test_group_chat_command_requires_mention(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_chat = FakeOmlxChatClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    lark=LarkOptions(bot_name="bot"),
                ),
                lark_client=fake_lark,
                chat_client=fake_chat,
            )

            result = app.handle_event(event(content="/chat 讲个笑话"))

        self.assertTrue(result.success)
        self.assertTrue(result.skipped)
        self.assertEqual(result.details["mode"], "not_addressed")
        self.assertEqual(fake_chat.prompts, [])
    def test_group_human_mention_is_not_treated_as_bot_when_identity_unconfigured(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_chat = FakeOmlxChatClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    lark=LarkOptions(bot_open_id="", bot_name=""),
                ),
                lark_client=fake_lark,
                chat_client=fake_chat,
            )

            result = app.handle_event(event(content="@邸立猛 这个现在正常了吗"))

        self.assertTrue(result.success)
        self.assertTrue(result.skipped)
        self.assertEqual(result.details["mode"], "not_addressed")
        self.assertEqual(fake_chat.prompts, [])
        self.assertEqual(fake_lark.replies, [])
        self.assertEqual(fake_lark.sent, [])
    def test_group_mentioned_chat_command_uses_omlx(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_chat = FakeOmlxChatClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    lark=LarkOptions(bot_name="bot"),
                ),
                lark_client=fake_lark,
                chat_client=fake_chat,
            )

            result = app.handle_event(event(content="@bot /chat 讲个笑话"))

        self.assertTrue(result.success)
        self.assertFalse(result.skipped)
        self.assertEqual(result.details["mode"], "omlx_chat")
        self.assertEqual(fake_chat.prompts, ["讲个笑话"])
        self.assertEqual(len(fake_lark.replies), 1)
        self.assertEqual(fake_lark.replies[0]["message_id"], "om_1")
    def test_group_mentioned_chat_command_accepts_prefix_without_slash(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_chat = FakeOmlxChatClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    lark=LarkOptions(bot_name="bot"),
                ),
                lark_client=fake_lark,
                chat_client=fake_chat,
            )

            result = app.handle_event(event(content="@bot chat 讲个笑话"))

        self.assertTrue(result.success)
        self.assertFalse(result.skipped)
        self.assertEqual(result.details["mode"], "omlx_chat")
        self.assertEqual(fake_chat.prompts, ["讲个笑话"])
    def test_group_mentioned_chat_command_accepts_bot_name_with_spaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_chat = FakeOmlxChatClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    lark=LarkOptions(bot_name="Test Bot"),
                ),
                lark_client=fake_lark,
                chat_client=fake_chat,
            )

            result = app.handle_event(event(content="@Test Bot /chat 讲个笑话"))

        self.assertTrue(result.success)
        self.assertFalse(result.skipped)
        self.assertEqual(result.details["mode"], "omlx_chat")
        self.assertEqual(fake_chat.prompts, ["讲个笑话"])
    def test_plain_question_bypasses_intent_card(self):
        class FailingIntentRunner(FakeIntentRunner):
            def classify(self, **kwargs):
                raise AssertionError("plain chat should not wait for intent classification")

        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_chat = FakeOmlxChatClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    lark=LarkOptions(bot_name="Test Bot"),
                ),
                lark_client=fake_lark,
                chat_client=fake_chat,
                bug_runner=FakeBugRunner(metadata, html),
                intent_runner=FailingIntentRunner(enabled=True),
            )

            result = app.handle_event(
                event(
                    event_id="evt_ack_before_intent",
                    content="@Test Bot 帮我解释一下 token",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "omlx_chat")
        self.assertEqual(fake_lark.card_replies, [])
        self.assertEqual(fake_lark.updated_cards, [])
        self.assertEqual(fake_lark.replies[0]["message_id"], "om_1")
        self.assertEqual(fake_lark.replies[0]["text"], '<at user_id="ou_1"></at> omlx 模型回复')
    def test_explicit_bug_link_bypasses_intent_classifier(self):
        class FailingIntentRunner(FakeIntentRunner):
            def classify(self, **kwargs):
                raise AssertionError("explicit bug links should not wait for intent classification")

        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_bug = FakeBugRunner(metadata, html)
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=FakeLarkClient(),
                bug_runner=fake_bug,
                intent_runner=FailingIntentRunner(enabled=True),
            )

            result = app.handle_event(
                event(
                    event_id="evt_explicit_bug_bypass_intent",
                    content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6979499593 分析3D生命周期",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "bug_analysis")
        self.assertEqual(len(fake_bug.requests), 1)
        self.assertEqual(fake_bug.requests[0].prompt, "分析3D生命周期")
    def test_markdown_bug_link_bypasses_intent_classifier(self):
        class FailingIntentRunner(FakeIntentRunner):
            def classify(self, **kwargs):
                raise AssertionError("markdown bug links should not wait for intent classification")

        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_bug = FakeBugRunner(metadata, html)
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    lark=LarkOptions(bot_name="朱云龙的飞书 CLI"),
                ),
                lark_client=FakeLarkClient(),
                bug_runner=fake_bug,
                intent_runner=FailingIntentRunner(enabled=True),
            )

            result = app.handle_event(
                event(
                    event_id="evt_markdown_bug_bypass_intent",
                    content="@朱云龙的飞书 CLI [ [缺陷] 【F01】车机大屏页面卡住-SB174577](https://project.feishu.cn/xpfailuremgmt/buglo/detail/6979499593) 分析3D生命周期",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "bug_analysis")
        self.assertEqual(len(fake_bug.requests), 1)
        self.assertEqual(fake_bug.requests[0].prompt, "分析3D生命周期")
    def test_intent_chat_updates_ack_card_with_answer(self):
        class FailingIntentRunner(FakeIntentRunner):
            def classify(self, **kwargs):
                raise AssertionError("plain chat should not wait for intent classification")

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
                intent_runner=FailingIntentRunner(enabled=True),
            )

            result = app.handle_event(event(content="@bot 帮我解释一下 token"))

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "omlx_chat")
        self.assertEqual(fake_lark.card_replies, [])
        self.assertEqual(fake_lark.updated_cards, [])
        self.assertEqual(fake_lark.replies[0]["message_id"], "om_1")
        self.assertEqual(fake_lark.replies[0]["text"], '<at user_id="ou_1"></at> omlx 模型回复')
    def test_group_bug_request_accepts_configured_bot_name_at_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("metadata", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    lark=LarkOptions(bot_name="Test Bot"),
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
            )

            result = app.handle_event(
                event(
                    event_id="evt_bot_name_at_end",
                    content="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6979499593 分析3D生命周期 @Test Bot",
                )
            )

        self.assertTrue(result.success)
        self.assertFalse(result.skipped)
        self.assertEqual(result.details["mode"], "bug_analysis")
        self.assertEqual(len(fake_bug.requests), 1)
        self.assertEqual(fake_bug.requests[0].prompt, "分析3D生命周期")
    def test_group_bug_request_source_stage_executor_not_ready_is_delivered_without_conclusion(self):
        class NotReadyBugRunner(FakeBugRunner):
            def run_bug_analysis(self, request, *, event=None, progress_callback=None):
                self.requests.append(request)
                self.progress_callbacks.append(progress_callback)
                if progress_callback is not None:
                    progress_callback(
                        {
                            "stage": "bug_run_analysis",
                            "message": "执行源码分析阶段",
                            "details": {"analysis_kind": "source_stage"},
                        }
                    )
                return TaskResult(
                    success=False,
                    message=(
                        "已命中专用 Skill `source-analysis-skill`，但当前没有可执行分析器，"
                        "尚未执行实际 Skill 分析。\n不会基于占位报告给出根因结论。"
                    ),
                    error_code="source_stage_executor_not_ready",
                    details={
                        "mode": "bug_analysis",
                        "analysis_kind": "source_stage",
                        "analysis_skill": "source-analysis-skill",
                        "source_stage_analysis_status": "executor_not_ready",
                    },
                )

        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("metadata", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = NotReadyBugRunner(metadata, html)
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
            )

            result = app.handle_event(
                event(
                    event_id="evt_source_stage_not_ready_group_bug",
                    message_id="om_source_stage_not_ready_group_bug",
                    content=(
                        "@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6998107767 "
                        "2026-05-22 19:46 退无图"
                    ),
                )
            )
            delivered_text = "\n".join(
                [item["text"] for item in fake_lark.replies]
                + [item["card_json"] for item in fake_lark.card_replies]
                + [item["card_json"] for item in fake_lark.updated_cards]
            )

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "source_stage_executor_not_ready")
        self.assertEqual(result.details["mode"], "bug_analysis")
        self.assertEqual(len(fake_bug.requests), 1)
        self.assertEqual(fake_bug.requests[0].bug_url, "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6998107767")
        self.assertEqual(fake_bug.requests[0].prompt, "2026-05-22 19:46 退无图")
        self.assertIn("当前没有可执行分析器", delivered_text)
        self.assertNotIn("最可能原因", delivered_text)
        self.assertNotIn("结论摘要", delivered_text)
    def test_group_bug_request_source_stage_file_agent_ready_delivers_after_execution(self):
        class ReadyBugRunner(FakeBugRunner):
            def run_bug_analysis(self, request, *, event=None, progress_callback=None):
                self.requests.append(request)
                self.progress_callbacks.append(progress_callback)
                if progress_callback is not None:
                    progress_callback(
                        {
                            "stage": "source_stage_agent_analysis",
                            "message": "执行源码分析文件 Agent 分析",
                            "details": {
                                "analysis_kind": "source_stage",
                                "analysis_skill": "agent-ready-source-skill",
                                "source_stage_analysis_status": "completed",
                            },
                        }
                    )
                    progress_callback(
                        {
                            "stage": "bug_agent_summary",
                            "message": "基于执行证据整理最终结论",
                            "details": {"provider": "codex", "session_id": "sess_ready"},
                        }
                    )
                return TaskResult(
                    success=True,
                    message="file agent summary：已基于 source_stage_analysis.md 的关键证据完成分析。",
                    job_id="job_custom_ready",
                    job_dir=self.html_path.parent,
                    details={
                        "mode": "bug_analysis",
                        "analysis_kind": "source_stage",
                        "analysis_skill": "agent-ready-source-skill",
                        "source_stage_executor": "file_agent",
                        "source_stage_analysis_status": "completed",
                        "source_stage_evidence_count": 2,
                        "files_to_send": [self.html_path],
                    },
                )

        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "source_stage_report.html"
            metadata.write_text("metadata", encoding="utf-8")
            html.write_text("<html>custom skill report</html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = ReadyBugRunner(metadata, html)
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
            )

            result = app.handle_event(
                event(
                    event_id="evt_source_stage_ready_group_bug",
                    message_id="om_source_stage_ready_group_bug",
                    content=(
                        "@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6998107767 "
                        "2026-05-22 19:46 退无图"
                    ),
                )
            )
            delivered_text = "\n".join(
                [item["text"] for item in fake_lark.replies]
                + [item["card_json"] for item in fake_lark.card_replies]
                + [item["card_json"] for item in fake_lark.updated_cards]
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "bug_analysis")
        self.assertEqual(result.details["analysis_kind"], "source_stage")
        self.assertEqual(result.details["source_stage_analysis_status"], "completed")
        self.assertEqual(len(fake_bug.requests), 1)
        self.assertEqual(fake_bug.requests[0].bug_url, "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6998107767")
        self.assertEqual(fake_bug.requests[0].prompt, "2026-05-22 19:46 退无图")
        self.assertIn("file agent summary", delivered_text)
        self.assertNotIn("当前没有可执行分析器", delivered_text)
        self.assertEqual([Path(item["path"]).name for item in fake_lark.files], ["source_stage_report.html"])

    def test_progress_card_update_error_does_not_crash_bug_delivery(self):
        class RecursingUpdateLarkClient(FakeLarkClient):
            def update_card(self, message_id, card_json):
                self.updated_cards.append({"message_id": message_id, "card_json": card_json})
                raise RecursionError("maximum recursion depth exceeded")

        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("metadata", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = RecursingUpdateLarkClient()
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

            result = app.handle_event(
                event(
                    event_id="evt_progress_card_recursion",
                    message_id="om_progress_card_recursion",
                    content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6998811703 分析3D场景信号",
                )
            )
            session = app.activity_store.get_session("om_progress_card_recursion")

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "bug_analysis")
        self.assertEqual(len(fake_bug.requests), 1)
        self.assertGreaterEqual(len(fake_lark.files), 1)
        self.assertIsNotNone(session)
        assert session is not None
        failed_updates = [item for item in session["progress"] if item["stage"] == "progress_card_update_failed"]
        self.assertTrue(failed_updates)
        self.assertEqual(failed_updates[-1]["details"]["error_type"], "RecursionError")

    def test_group_bug_request_accepts_rich_text_wrapped_bot_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("metadata", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    lark=LarkOptions(bot_name="朱云龙的飞书 CLI"),
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
            )

            result = app.handle_event(
                event(
                    event_id="evt_rich_text_bot_name",
                    content="<p>@朱云龙的飞书 CLI https://project.feishu.cn/xpfailuremgmt/buglo/detail/6979499593 分析3D生命周期</p>",
                )
            )

        self.assertTrue(result.success)
        self.assertFalse(result.skipped)
        self.assertEqual(result.details["mode"], "bug_analysis")
        self.assertEqual(len(fake_bug.requests), 1)
        self.assertEqual(fake_bug.requests[0].prompt, "分析3D生命周期")
    def test_group_bug_request_recovers_bot_mention_from_message_metadata_when_config_has_no_bot_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("metadata", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_lark.fetched_messages["om_mid_text_bot_mention"] = json.dumps(
                {
                    "ok": True,
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_mid_text_bot_mention",
                                "chat_id": "oc_denied",
                                "content": "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6995823164 @朱云龙的飞书 CLI 调查3D启动生命周期",
                                "mentions": [
                                    {
                                        "id": "cli_a976baa2cdfadcc7",
                                        "key": "@_user_1",
                                        "name": "朱云龙的飞书 CLI",
                                    }
                                ],
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )
            fake_bug = FakeBugRunner(metadata, html)
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    lark=LarkOptions(bot_open_id="", bot_name=""),
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
            )

            result = app.handle_event(
                event(
                    event_id="evt_mid_text_bot_mention",
                    message_id="om_mid_text_bot_mention",
                    content="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6995823164 @朱云龙的飞书 CLI 调查3D启动生命周期",
                )
            )

        self.assertTrue(result.success)
        self.assertFalse(result.skipped)
        self.assertEqual(result.details["mode"], "bug_analysis")
        self.assertEqual(len(fake_bug.requests), 1)
        self.assertEqual(fake_bug.requests[0].prompt, "调查3D启动生命周期")
    def test_group_chat_command_uses_configured_bot_mention_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_chat = FakeOmlxChatClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    lark=LarkOptions(bot_open_id="ou_bot", bot_name="bot"),
                ),
                lark_client=fake_lark,
                chat_client=fake_chat,
            )

            rejected = app.handle_event(
                event(
                    event_id="evt_wrong_bot",
                    content='<at user_id="ou_other"></at> /chat 讲个笑话',
                )
            )
            accepted = app.handle_event(
                event(
                    event_id="evt_right_bot",
                    content='<at user_id="ou_bot"></at> /chat 讲个笑话',
                )
            )

        self.assertTrue(rejected.skipped)
        self.assertEqual(rejected.details["mode"], "not_addressed")
        self.assertTrue(accepted.success)
        self.assertFalse(accepted.skipped)
        self.assertEqual(fake_chat.prompts, ["讲个笑话"])
    def test_group_chat_command_uses_configured_bot_name_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_chat = FakeOmlxChatClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    lark=LarkOptions(bot_name="Test Bot"),
                ),
                lark_client=fake_lark,
                chat_client=fake_chat,
            )

            rejected = app.handle_event(
                event(
                    event_id="evt_wrong_bot_name",
                    content="@其他机器人 /chat 讲个笑话",
                )
            )
            accepted = app.handle_event(
                event(
                    event_id="evt_right_bot_name",
                    content="@Test Bot /chat 讲个笑话",
                )
            )

        self.assertTrue(rejected.skipped)
        self.assertEqual(rejected.details["mode"], "not_addressed")
        self.assertTrue(accepted.success)
        self.assertFalse(accepted.skipped)
        self.assertEqual(fake_chat.prompts, ["讲个笑话"])
    def test_group_chat_command_accepts_configured_bot_name_without_spaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_chat = FakeOmlxChatClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    lark=LarkOptions(bot_name="朱云龙的飞书 CLI"),
                ),
                lark_client=fake_lark,
                chat_client=fake_chat,
            )

            rejected = app.handle_event(
                event(
                    event_id="evt_human_mention",
                    content="@朱云龙 /chat 讲个笑话",
                )
            )
            accepted = app.handle_event(
                event(
                    event_id="evt_bot_name_no_space",
                    content="@朱云龙的飞书CLI /chat 讲个笑话",
                )
            )

        self.assertTrue(rejected.skipped)
        self.assertEqual(rejected.details["mode"], "not_addressed")
        self.assertTrue(accepted.success)
        self.assertFalse(accepted.skipped)
        self.assertEqual(fake_chat.prompts, ["讲个笑话"])
