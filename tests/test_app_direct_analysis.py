from _app_base import *  # noqa: F401,F403
from _app_base import _AppTestBase


class AppDirectAnalysisTests(_AppTestBase):
    def test_6998811703_3D场景模式_shell_cleans_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
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

            result = app.handle_event(
                event(
                    content=(
                        "@bot @朱云龙的飞书 CLI "
                        "[ [缺陷] 2026-05-25 16:50:41 【d03】6.2.3】拾光主题切换成天玑主题，进入场景模式没有展示3D场景]"
                        "(https://project.feishu.cn/xpfailuremgmt/buglo/detail/6998811703) "
                        "分析 3D场景模式"
                    )
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(fake_bug.requests[0].bug_url, "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6998811703")
        self.assertEqual(fake_bug.requests[0].prompt, "分析 3D场景模式")
    def test_simple_question_uses_omlx_chat(self):
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

            result = app.handle_event(
                event(
                    chat_id="ou_chat_1",
                    chat_type="p2p",
                    content="帮我解释一下什么是 token？",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.message, "omlx 模型回复")
        self.assertEqual(fake_chat.prompts, ["帮我解释一下什么是 token？"])
        self.assertIn("om_1", _all_reply_message_ids(fake_lark))
    def test_knowledge_question_uses_knowledge_card_before_omlx(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_chat = FakeOmlxChatClient()
            fake_knowledge = FakeKnowledgeService()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    knowledge=KnowledgeOptions(enabled=True),
                ),
                lark_client=fake_lark,
                chat_client=fake_chat,
                intent_runner=FakeIntentRunner(enabled=False),
                knowledge_service=fake_knowledge,
            )

            result = app.handle_event(
                event(
                    event_id="evt_kb",
                    message_id="om_kb",
                    chat_id="oc_p2p",
                    chat_type="p2p",
                    content="知识库 OTA信号如何模拟",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "knowledge_qa")
        self.assertEqual(fake_knowledge.questions, ["知识库 OTA信号如何模拟"])
        self.assertEqual(fake_chat.prompts, [])
        self.assertEqual(len(fake_lark.card_replies), 1)
        self.assertEqual(fake_lark.card_replies[0]["message_id"], "om_kb")
        self.assertIn("知识库回答", fake_lark.card_replies[0]["card_json"])
        self.assertIn("SIGNAL_OTA_ST 定义", fake_lark.card_replies[0]["card_json"])
    def test_group_knowledge_followup_in_reply_chain_without_mention(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_chat = FakeOmlxChatClient()
            fake_knowledge = FakeKnowledgeService()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    knowledge=KnowledgeOptions(enabled=True),
                ),
                lark_client=fake_lark,
                chat_client=fake_chat,
                intent_runner=FakeIntentRunner(enabled=False),
                knowledge_service=fake_knowledge,
            )
            app.conversation_store.remember(
                root_message_id="om_root_request",
                chat_id="oc_denied",
                mode="knowledge_qa",
                request_text="知识库 OTA信号如何模拟",
                summary_text="第一轮知识回答",
                report_url="",
                report_excerpt="",
            )
            app.conversation_store.remember_alias(
                alias_message_id="om_bot_kb_reply",
                root_message_id="om_root_request",
            )

            result = app.handle_event(
                event(
                    event_id="evt_group_followup_no_mention",
                    message_id="om_group_followup_no_mention",
                    reply_to="om_bot_kb_reply",
                    content="知识库 OTA信号如何模拟",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "knowledge_qa")
        self.assertEqual(fake_knowledge.questions[-1], "知识库 OTA信号如何模拟")
        self.assertEqual(len(fake_lark.card_replies), 1)
        self.assertEqual(fake_lark.card_replies[0]["message_id"], "om_group_followup_no_mention")
    def test_group_bug_followup_in_reply_chain_without_mention(self):
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
                chat_client=FakeOmlxChatClient(),
            )
            app.conversation_store.remember(
                root_message_id="om_bug_root",
                chat_id="oc_denied",
                mode="bug_analysis",
                request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查3D启动时序",
                summary_text="bug 分析完成",
                report_url="http://report",
                report_excerpt="SceneType=Main",
            )
            app.conversation_store.remember_alias(
                alias_message_id="om_bug_reply",
                root_message_id="om_bug_root",
            )

            followup = app.handle_event(
                event(
                    event_id="evt_group_bug_followup_no_mention",
                    message_id="om_group_bug_followup_no_mention",
                    reply_to="om_bug_reply",
                    content="问题时间是2026-05-11 23:12分左右",
                )
            )

        self.assertTrue(followup.success)
        self.assertEqual(followup.details["mode"], "bug_agent_followup")
        self.assertEqual(len(fake_bug.agent_followup_calls), 1)
        self.assertEqual(fake_bug.agent_followup_calls[0]["followup_text"], "问题时间是2026-05-11 23:12分左右")
    def test_group_reply_in_thread_without_replying_to_bot_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(Path(tmp) / "m.md", Path(tmp) / "r.html")
            (Path(tmp) / "m.md").write_text("bug", encoding="utf-8")
            (Path(tmp) / "r.html").write_text("<html></html>", encoding="utf-8")
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    lark=LarkOptions(bot_open_id="ou_bot", bot_name="bot"),
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                chat_client=FakeOmlxChatClient(),
            )
            app.conversation_store.remember(
                root_message_id="om_thread_root",
                chat_id="oc_open",
                mode="bug_analysis",
                request_text="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/1 调查",
                summary_text="bug 分析完成",
                report_url="http://r",
                report_excerpt="",
            )
            app.conversation_store.remember_alias(
                alias_message_id="om_bot_reply",
                root_message_id="om_thread_root",
            )

            result = app.handle_event(
                event(
                    event_id="evt_reply_to_peer_in_thread",
                    message_id="om_reply_to_peer",
                    chat_id="oc_open",
                    reply_to="om_peer_msg",
                    root_id="om_thread_root",
                    parent_id="om_peer_msg",
                    content="@王忠华 M5MAX",
                )
            )

        self.assertTrue(result.skipped)
        self.assertEqual(result.details.get("mode"), "not_addressed")
        self.assertEqual(fake_bug.agent_followup_calls, [])
        self.assertEqual(fake_bug.requests, [])
    def test_group_reply_to_bot_root_without_mention_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(Path(tmp) / "m.md", Path(tmp) / "r.html")
            (Path(tmp) / "m.md").write_text("bug", encoding="utf-8")
            (Path(tmp) / "r.html").write_text("<html></html>", encoding="utf-8")
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    lark=LarkOptions(bot_open_id="ou_bot", bot_name="bot"),
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                chat_client=FakeOmlxChatClient(),
            )
            app.conversation_store.remember(
                root_message_id="om_user_triggering",
                chat_id="oc_open",
                mode="bug_analysis",
                request_text="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/1 调查",
                summary_text="bug 分析完成",
                report_url="http://r",
                report_excerpt="",
            )

            result = app.handle_event(
                event(
                    event_id="evt_reply_to_user_triggering_msg",
                    message_id="om_reply_to_root",
                    chat_id="oc_open",
                    reply_to="om_user_triggering",
                    content="继续追问",
                )
            )

        self.assertTrue(result.skipped)
        self.assertEqual(result.details.get("mode"), "not_addressed")
    def test_group_bug_followup_retry_once_routes_to_bug_reanalysis(self):
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
                chat_client=FakeOmlxChatClient(),
            )
            app.conversation_store.remember(
                root_message_id="om_bug_retry_root",
                chat_id="oc_denied",
                mode="bug_analysis",
                request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查3D启动时序",
                summary_text="bug 分析完成",
                report_url="http://report",
                report_excerpt="SceneType=Main",
            )
            app.conversation_store.remember_alias(
                alias_message_id="om_bug_retry_reply",
                root_message_id="om_bug_retry_root",
            )
            original = event(
                event_id="evt_bug_retry_original",
                message_id="om_bug_retry_root",
                content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查3D启动时序",
            )
            app.activity_store.record_event(original)
            app.activity_store.record_result(
                original,
                TaskResult(
                    success=True,
                    message="bug 分析完成",
                    details={
                        "mode": "bug_analysis",
                        "conversation_root_message_id": "om_bug_retry_root",
                    },
                ),
            )

            followup = app.handle_event(
                event(
                    event_id="evt_group_bug_retry_once",
                    message_id="om_group_bug_retry_once",
                    reply_to="om_bug_retry_reply",
                    content="重试一次",
                )
            )

        self.assertTrue(followup.success)
        self.assertEqual(followup.details["mode"], "bug_reanalysis")
        self.assertEqual(len(fake_bug.reanalysis_calls), 1)
        self.assertTrue(fake_bug.reanalysis_calls[0]["force_rerun"])
    def test_internal_operation_question_with_knowledge_hit_uses_knowledge_probe_before_chat(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_chat = FakeOmlxChatClient()
            fake_knowledge = FakeKnowledgeService(
                search_hits=[
                    SearchHit(
                        chunk_id="power:1",
                        source_id="guideengine-runbook",
                        title="上下电模拟 runbook",
                        content="上下电模拟需要使用车机测试广播或台架电源流程。",
                        source_ref="/kb/power.md",
                        score=12.0,
                    )
                ]
            )
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    knowledge=KnowledgeOptions(enabled=True),
                ),
                lark_client=fake_lark,
                chat_client=fake_chat,
                knowledge_service=fake_knowledge,
            )

            result = app.handle_event(event(content="@bot 上下电如何模拟"))

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "knowledge_qa")
        self.assertEqual(fake_knowledge.search_questions, ["上下电如何模拟"])
        self.assertEqual(fake_knowledge.questions, ["上下电如何模拟"])
        self.assertEqual(fake_chat.prompts, [])
        self.assertEqual(len(fake_lark.card_replies), 1)
    def test_power_cycle_question_uses_real_knowledge_answer_and_records_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_chat = FakeOmlxChatClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    knowledge=KnowledgeOptions(enabled=True, storage=Path(tmp) / "knowledge.sqlite"),
                ),
                lark_client=fake_lark,
                chat_client=fake_chat,
                intent_runner=FakeIntentRunner(enabled=False),
            )

            result = app.handle_event(event(content="@bot 上下电如何模拟"))
            hits = app.knowledge_service.search("上下电如何模拟")

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "knowledge_qa")
        self.assertIn("SIGNAL_MCU_IG_ST 上下电模拟指令", result.message)
        self.assertIn("--ei code 36001 --ei format 3 --es value 1", result.message)
        self.assertEqual(hits[0].source_id, "derived-adb-simulations")
        self.assertEqual(fake_chat.prompts, [])
        self.assertEqual(len(fake_lark.card_replies), 1)
    def test_internal_operation_question_without_knowledge_hit_does_not_fall_back_to_chat(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_chat = FakeOmlxChatClient()
            fake_knowledge = FakeKnowledgeService(search_hits=[])
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    knowledge=KnowledgeOptions(enabled=True),
                ),
                lark_client=fake_lark,
                chat_client=fake_chat,
                knowledge_service=fake_knowledge,
            )

            result = app.handle_event(event(content="@bot 上下电如何模拟"))

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "knowledge_probe_no_hits")
        self.assertEqual(result.details["mode"], "knowledge_probe")
        self.assertEqual(fake_knowledge.search_questions, ["上下电如何模拟"])
        self.assertEqual(fake_knowledge.questions, [])
        self.assertEqual(fake_chat.prompts, [])
        self.assertEqual(fake_lark.replies[0]["message_id"], "om_1")
        self.assertIn("知识库未命中", fake_lark.replies[0]["text"])
    def test_knowledge_probe_can_reply_with_low_confidence_command_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adb_path = root / "adb_data.json"
            adb_path.write_text(
                json.dumps(
                    {
                        "commands": [
                            {
                                "name": "直接发送文本给小P",
                                "command": (
                                    "adb shell am broadcast -a carspeechservice.ACTION_SEND_TEXT "
                                    "--es text \"打开车窗\" --ei soundArea 2"
                                ),
                                "group": "语音",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            fake_lark = FakeLarkClient()
            fake_chat = FakeOmlxChatClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=root,
                    allowed_chats=["oc_denied"],
                    knowledge=KnowledgeOptions(
                        enabled=True,
                        storage=root / "knowledge.sqlite",
                        sources=[
                            KnowledgeSourceOptions(
                                id="guideengine-adb",
                                type="local_json",
                                path=str(adb_path),
                            )
                        ],
                    ),
                ),
                lark_client=fake_lark,
                chat_client=fake_chat,
                intent_runner=FakeIntentRunner(enabled=False),
            )

            result = app.handle_event(event(content="@bot 车窗如何模拟"))

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "knowledge_qa")
        self.assertEqual(result.details["answer_type"], "low_confidence_candidates")
        self.assertIn("carspeechservice.ACTION_SEND_TEXT", result.message)
        self.assertEqual(fake_chat.prompts, [])
        self.assertEqual(len(fake_lark.card_replies), 1)
        self.assertIn("低置信候选", fake_lark.card_replies[0]["card_json"])
    def test_unrelated_chat_does_not_probe_knowledge(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_chat = FakeOmlxChatClient()
            fake_knowledge = FakeKnowledgeService(
                search_hits=[
                    SearchHit(
                        chunk_id="power:1",
                        source_id="guideengine-runbook",
                        title="上下电模拟 runbook",
                        content="上下电模拟流程。",
                        score=12.0,
                    )
                ]
            )
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    knowledge=KnowledgeOptions(enabled=True),
                ),
                lark_client=fake_lark,
                chat_client=fake_chat,
                knowledge_service=fake_knowledge,
            )

            result = app.handle_event(event(content="@bot /chat 讲个笑话"))

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "omlx_chat")
        self.assertEqual(fake_knowledge.search_questions, [])
        self.assertEqual(fake_chat.prompts, ["讲个笑话"])
    def test_broad_how_question_does_not_probe_knowledge(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_chat = FakeOmlxChatClient()
            fake_knowledge = FakeKnowledgeService(
                search_hits=[
                    SearchHit(
                        chunk_id="doc:1",
                        source_id="guideengine-runbook",
                        title="日报模板",
                        content="日报模板示例。",
                        score=12.0,
                    )
                ]
            )
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    knowledge=KnowledgeOptions(enabled=True),
                ),
                lark_client=fake_lark,
                chat_client=fake_chat,
                knowledge_service=fake_knowledge,
            )

            result = app.handle_event(event(content="@bot 怎么写日报"))

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "omlx_chat")
        self.assertEqual(fake_knowledge.search_questions, [])
        self.assertEqual(fake_chat.prompts, ["怎么写日报"])
    def test_internal_build_data_question_uses_knowledge_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_chat = FakeOmlxChatClient()
            fake_knowledge = FakeKnowledgeService(
                search_hits=[
                    SearchHit(
                        chunk_id="mock:1",
                        source_id="guideengine-runbook",
                        title="点火状态模拟",
                        content="点火状态可通过已验证模板模拟。",
                        score=12.0,
                    )
                ]
            )
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    knowledge=KnowledgeOptions(enabled=True),
                ),
                lark_client=fake_lark,
                chat_client=fake_chat,
                knowledge_service=fake_knowledge,
            )

            result = app.handle_event(event(content="@bot 点火状态怎么造"))

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "knowledge_qa")
        self.assertEqual(fake_knowledge.search_questions, ["点火状态怎么造"])
        self.assertEqual(fake_chat.prompts, [])
    def test_help_request_replies_as_plain_text_without_intent_or_card(self):
        class FailingIntentRunner(FakeIntentRunner):
            def classify(self, **kwargs):
                raise AssertionError("help should be answered locally")

        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                intent_runner=FailingIntentRunner(enabled=True),
            )

            result = app.handle_event(event(content="@bot help"))

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "basic_chat")
        self.assertEqual(fake_lark.card_replies, [])
        self.assertEqual(fake_lark.updated_cards, [])
        self.assertEqual(len(fake_lark.replies), 1)
        self.assertEqual(fake_lark.replies[0]["message_id"], "om_1")
        self.assertIn("常用触发方式", fake_lark.replies[0]["text"])
        self.assertIn("| Bug 分析 |", fake_lark.replies[0]["text"])
    def test_stale_help_replayed_before_listener_ready_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    event_consumer=EventConsumerOptions(
                        drop_stale_light_interactions=True,
                        stale_light_interaction_grace_seconds=60,
                    ),
                ),
                lark_client=fake_lark,
                intent_runner=FakeIntentRunner(enabled=True),
            )
            app.record_daemon_status({"stage": "event_consumer_ready", "ready": True})
            old_create_time = int((datetime.now(timezone.utc) - timedelta(minutes=5)).timestamp() * 1000)

            result = app.handle_event(
                event(
                    event_id="evt_stale_help",
                    message_id="om_stale_help",
                    content="@bot help",
                    create_time=str(old_create_time),
                )
            )

        self.assertTrue(result.success)
        self.assertTrue(result.skipped)
        self.assertEqual(result.details["mode"], "stale_light_interaction")
        self.assertEqual(fake_lark.sent, [])
        self.assertEqual(fake_lark.replies, [])
        self.assertEqual(fake_lark.card_replies, [])
    def test_stale_bug_request_is_not_dropped_by_light_interaction_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            metadata = tmp_path / "metadata.md"
            html = tmp_path / "report.html"
            metadata.write_text("metadata", encoding="utf-8")
            html.write_text("<html>report</html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            bug_runner = FakeBugRunner(metadata, html)
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=tmp_path,
                    allowed_chats=["oc_denied"],
                    event_consumer=EventConsumerOptions(
                        drop_stale_light_interactions=True,
                        stale_light_interaction_grace_seconds=60,
                    ),
                ),
                lark_client=fake_lark,
                bug_runner=bug_runner,
            )
            app.record_daemon_status({"stage": "event_consumer_ready", "ready": True})
            old_create_time = int((datetime.now(timezone.utc) - timedelta(minutes=5)).timestamp() * 1000)

            result = app.handle_event(
                event(
                    event_id="evt_stale_bug",
                    message_id="om_stale_bug",
                    content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6991604970 分析主题变化",
                    create_time=str(old_create_time),
                )
            )

        self.assertTrue(result.success)
        self.assertFalse(result.skipped)
        self.assertEqual(len(bug_runner.requests), 1)
        self.assertEqual(result.details["mode"], "bug_analysis")
    def test_identity_request_replies_as_plain_text_without_intent_or_card(self):
        class FailingIntentRunner(FakeIntentRunner):
            def classify(self, **kwargs):
                raise AssertionError("identity should be answered locally")

        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                intent_runner=FailingIntentRunner(enabled=True),
            )

            result = app.handle_event(event(content="@bot 你是谁"))

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "basic_chat")
        self.assertEqual(fake_lark.card_replies, [])
        self.assertEqual(fake_lark.updated_cards, [])
        self.assertEqual(len(fake_lark.replies), 1)
        self.assertEqual(fake_lark.replies[0]["message_id"], "om_1")
        self.assertIn("Lark Agent Bridge", fake_lark.replies[0]["text"])
    def test_agent_intent_routes_simple_question_to_chat(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_chat = FakeOmlxChatClient()
            fake_intent = FakeIntentRunner(
                {
                    "帮我解释一下什么是 token？": IntentDecision(
                        route="chat",
                        reason="普通聊天提问",
                        confidence="high",
                        followup_action="none",
                        context_source="none",
                    )
                }
            )
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                chat_client=fake_chat,
                intent_runner=fake_intent,
            )

            result = app.handle_event(
                event(
                    chat_id="ou_chat_1",
                    chat_type="p2p",
                    content="帮我解释一下什么是 token？",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "omlx_chat")
        self.assertEqual(fake_chat.prompts, ["帮我解释一下什么是 token？"])
        self.assertEqual(fake_intent.calls, [])
    def test_super_user_in_non_allowlisted_group_uses_intent_routing(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_chat = FakeOmlxChatClient()
            fake_intent = FakeIntentRunner(
                {
                    "帮我解释一下什么是 token？": IntentDecision(
                        route="chat",
                        reason="super user addressed chat",
                        confidence="high",
                        followup_action="none",
                        context_source="none",
                    )
                }
            )
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_allowed"],
                    allowed_users=["ou_super"],
                ),
                lark_client=fake_lark,
                chat_client=fake_chat,
                intent_runner=fake_intent,
            )

            result = app.handle_event(
                event(
                    chat_id="oc_denied",
                    sender_id="ou_super",
                    content="@bot 帮我解释一下什么是 token？",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "omlx_chat")
        self.assertEqual(fake_chat.prompts, ["帮我解释一下什么是 token？"])
        self.assertEqual(fake_intent.calls, [])
    def test_perception_summary_request_sends_html_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            html = Path(tmp) / "perception-summary.html"
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_runner = FakePerceptionRunner(html)
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                perception_runner=fake_runner,
            )

            result = app.handle_event(event(content="@bot 总结当前感知数据 https://example.com/log.zip"))

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "perception_summary")
        self.assertEqual(fake_runner.requests[0].prompt, "总结当前感知数据 https://example.com/log.zip")
        self.assertEqual(fake_runner.requests[0].resources[0].kind, "url")
        total_replies = len(fake_lark.replies) + len(fake_lark.card_replies)
        self.assertEqual(total_replies, 1)
        self.assertEqual(len(fake_lark.files), 1)
        self.assertEqual(Path(fake_lark.files[0]["path"]).resolve(), html.resolve())
        self.assertIn("published_report_url", result.details)

    def test_perception_data_chain_request_does_not_require_intent_router(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_intent = FakeIntentRunner(enabled=True)
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_d977fe30a92c7ac81e3e6b543d99ef5b"],
                ),
                lark_client=fake_lark,
                intent_runner=fake_intent,
            )

            result = app.handle_event(
                event(
                    chat_id="oc_d977fe30a92c7ac81e3e6b543d99ef5b",
                    content="@bot 时间点6月8日 19:17 感知数据链路调查",
                )
            )

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "missing_log")
        self.assertEqual(result.details["mode"], "perception_summary")
        self.assertEqual(fake_intent.calls, [])

    def test_intent_perception_reply_to_file_fetches_reply_resource_when_event_lacks_reply_to(self):
        with tempfile.TemporaryDirectory() as tmp:
            html = Path(tmp) / "perception-summary.html"
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_lark.fetched_messages["om_current"] = json.dumps(
                {
                    "ok": True,
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_current",
                                "content": "@bot 你也查下这个现状",
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
                                "msg_type": "file",
                                "content": '<file key="file_v3_0011s_6d5d723c-ec0b-44f3-9908-a02be496b54g" name="Log.zip"/>',
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )
            fake_runner = FakePerceptionRunner(html)
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                perception_runner=fake_runner,
                intent_runner=FakeIntentRunner(
                    {
                        "你也查下这个现状": IntentDecision(
                            route="perception_summary",
                            reason="用户要求基于被回复日志查看现状",
                            confidence="high",
                        )
                    }
                ),
            )

            result = app.handle_event(event(message_id="om_current", content="@bot 你也查下这个现状"))

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "perception_summary")
        self.assertEqual(len(fake_runner.requests), 1)
        self.assertEqual(len(fake_runner.requests[0].resources), 1)
        self.assertEqual(fake_runner.requests[0].resources[0].kind, "file")
        self.assertEqual(fake_runner.requests[0].resources[0].source_message_id, "om_file_msg")
        self.assertEqual(
            fake_runner.requests[0].resources[0].value,
            "file_v3_0011s_6d5d723c-ec0b-44f3-9908-a02be496b54g",
        )
    def test_perception_followup_recovers_original_file_from_reply_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            html = Path(tmp) / "perception-summary.html"
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_lark.fetched_messages["om_current"] = json.dumps(
                {
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_current",
                                "reply_to": "om_previous_text",
                            }
                        ]
                    }
                }
            )
            fake_lark.fetched_messages["om_previous_text"] = json.dumps(
                {
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_previous_text",
                                "reply_to": "om_file_msg",
                                "content": {"text": "上一轮感知数据总结"},
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
                                "content": {"file_key": "file_perception_zip"},
                            }
                        ]
                    }
                }
            )
            fake_runner = FakePerceptionRunner(html)
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                perception_runner=fake_runner,
            )

            result = app.handle_event(
                event(
                    event_id="evt_perception_followup",
                    message_id="om_current",
                    content="@bot 总结当前感知数据",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "perception_summary")
        self.assertEqual(len(fake_runner.requests), 1)
        resources = fake_runner.requests[0].resources
        self.assertEqual(resources[0].kind, "file")
        self.assertEqual(resources[0].value, "file_perception_zip")
        self.assertEqual(resources[0].source_message_id, "om_file_msg")
    def test_direct_analysis_request_with_file_routes_to_bug_runner(self):
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
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
            )

            result = app.handle_event(event(content="@bot 分析启动和卡顿 file_abc123 11:30"))

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "direct_analysis")
        self.assertEqual(len(fake_bug.requests), 1)
        self.assertIn("published_report_url", result.details)
        total_replies = len(fake_lark.replies) + len(fake_lark.card_replies)
        self.assertEqual(total_replies, 1)
        self.assertEqual(len(fake_lark.files), 1)
        self.assertEqual(Path(fake_lark.files[0]["path"]).resolve(), html.resolve())
    def test_direct_analysis_with_explicit_source_clue_sends_preflight_card_then_runs(self):
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
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
            )

            result = app.handle_event(
                event(
                    event_id="evt_direct_source_preflight",
                    message_id="om_direct_source_preflight",
                    content="@bot 基于SRViolationHandler.kt源码分析 时间点2026-05-22 07:46 分析超速状态 file_abc123",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "direct_analysis")
        self.assertEqual(len(fake_bug.requests), 1)
        self.assertGreaterEqual(len(fake_lark.card_replies), 1)
        initial_card = fake_lark.card_replies[0]["card_json"]
        self.assertIn("意图分析", initial_card)
        self.assertIn("高置信度", initial_card)
        self.assertIn("源码导向文件分析", initial_card)
    def test_generic_file_request_returns_clarification_before_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(Path(tmp) / "analysis.md", Path(tmp) / "analysis.html")
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
                    event_id="evt_direct_need_direction",
                    message_id="om_direct_need_direction",
                    content="@bot 时间点2026-05-22 07:46 调查3D生命周期 file_abc123",
                )
            )

        self.assertTrue(result.success)
        self.assertTrue(result.skipped)
        self.assertEqual(result.details["mode"], "bug_clarification")
        self.assertTrue(result.details["needs_user_direction"])
        self.assertEqual(fake_bug.requests, [])
        self.assertIn("直接源码分析", result.message)
        self.assertIn("1.", result.message)
        self.assertGreaterEqual(len(fake_lark.card_replies), 1)
        self.assertIn("意图分析", fake_lark.card_replies[0]["card_json"])
    def test_direct_analysis_followup_recovers_original_file_from_reply_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "analysis.md"
            html = Path(tmp) / "analysis.html"
            metadata.write_text("analysis", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            fake_lark.fetched_messages["om_current"] = json.dumps(
                {
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_current",
                                "reply_to": "om_previous_text",
                            }
                        ]
                    }
                }
            )
            fake_lark.fetched_messages["om_previous_text"] = json.dumps(
                {
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_previous_text",
                                "reply_to": "om_file_msg",
                                "content": {"text": "上一轮直传分析"},
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
                                "content": {
                                    "file_key": "file_direct_zip",
                                    "file_name": "L1NSPGHB3SB010669log0.zip",
                                },
                            }
                        ]
                    }
                }
            )
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
                    event_id="evt_direct_followup",
                    message_id="om_current",
                    content="@bot 分析启动和卡顿",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "direct_analysis")
        self.assertEqual(len(fake_bug.requests), 1)
        resources = fake_bug.requests[0].resources
        self.assertEqual(resources[0].kind, "file")
        self.assertEqual(resources[0].value, "file_direct_zip")
        self.assertEqual(resources[0].source_message_id, "om_file_msg")
        self.assertEqual(resources[0].display_name, "L1NSPGHB3SB010669log0.zip")
    def test_direct_analysis_mixed_continue_preserves_followup_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp)),
                lark_client=FakeLarkClient(),
            )
            app._reference_chain_log_resources = lambda _event: [
                DownloadResource(kind="file", value="file_v3_followup_log", source_message_id="om_file")
            ]
            context = SimpleNamespace(
                root_message_id="om_direct_root",
                request_text="调查3D生命周期",
            )

            request = app._recovered_direct_analysis_request_from_followup_context(
                event(message_id="om_followup", content="@bot 时间点2026-07-03 15:11分 继续自主分析"),
                context,
                followup_text="时间点2026-07-03 15:11分 继续自主分析",
            )

        self.assertIsNotNone(request)
        assert request is not None
        self.assertIn("追问/修正：时间点2026-07-03 15:11分 继续自主分析", request.raw_text)

    def test_direct_analysis_pure_continue_reuses_original_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp)),
                lark_client=FakeLarkClient(),
            )
            app._reference_chain_log_resources = lambda _event: [
                DownloadResource(kind="file", value="file_v3_followup_log", source_message_id="om_file")
            ]
            context = SimpleNamespace(
                root_message_id="om_direct_root",
                request_text="调查3D生命周期",
            )

            request = app._recovered_direct_analysis_request_from_followup_context(
                event(message_id="om_followup", content="@bot 继续自主分析"),
                context,
                followup_text="继续自主分析",
            )

        self.assertIsNotNone(request)
        assert request is not None
        self.assertEqual(request.raw_text, "调查3D生命周期")

    def test_recovered_direct_analysis_keeps_original_conversation_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "analysis.md"
            html = Path(tmp) / "analysis.html"
            metadata.write_text("analysis", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            fake_lark.fetched_messages["om_bot_result"] = json.dumps(
                {
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_bot_result",
                                "reply_to": "om_direct_root",
                            }
                        ]
                    }
                }
            )
            fake_lark.fetched_messages["om_direct_root"] = json.dumps(
                {
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_direct_root",
                                "reply_to": "om_file_msg",
                                "content": "@bot 调查3D生命周期",
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
                                "msg_type": "file",
                                "content": {"file_key": "file_v3_followup_log", "file_name": "Log.alog"},
                            }
                        ]
                    }
                }
            )
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"]),
                lark_client=fake_lark,
                bug_runner=fake_bug,
            )
            app.conversation_store.remember(
                root_message_id="om_direct_root",
                chat_id="oc_denied",
                mode="direct_analysis",
                request_text="调查3D生命周期",
                summary_text="第一次分析失败",
                report_url="",
                report_excerpt="",
            )
            app.conversation_store.remember_alias(
                alias_message_id="om_bot_result",
                root_message_id="om_direct_root",
            )

            result = app.handle_event(
                event(
                    event_id="evt_direct_followup_root",
                    message_id="om_direct_followup",
                    reply_to="om_bot_result",
                    content="@bot 时间点2026-07-03 15:11分 继续自主分析",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "direct_analysis")
        self.assertEqual(result.details["conversation_root_message_id"], "om_direct_root")
        self.assertIsNone(app.activity_store.get_session("om_direct_followup"))
        self.assertIn("时间点2026-07-03 15:11分", fake_bug.requests[0].raw_text)
    def test_bug_clarification_reply_direct_source_analysis_recovers_direct_analysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "analysis.md"
            html = Path(tmp) / "analysis.html"
            metadata.write_text("analysis", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            fake_chat = FakeOmlxChatClient()
            fake_lark.fetched_messages["om_followup_choice"] = json.dumps(
                {
                    "ok": True,
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_followup_choice",
                                "reply_to": "om_clarification_root",
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )
            fake_lark.fetched_messages["om_clarification_root"] = json.dumps(
                {
                    "ok": True,
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_clarification_root",
                                "reply_to": "om_file_msg",
                                "content": "@bot 时间点2026-05-22 07:46 调查3D生命周期",
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
                                "content": '<file key="file_v3_0011v_33d1772b-86ac-4789-b6e1-35c77ec29a5g" name="L1NSPGHB3SB010669log0.zip"/>',
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
            clarification_event = event(
                event_id="evt_clarification_root",
                message_id="om_clarification_root",
                content="@bot 时间点2026-05-22 07:46 调查3D生命周期",
            )
            app.activity_store.record_event(clarification_event)
            app.activity_store.record_result(
                clarification_event,
                TaskResult(
                    success=True,
                    message="当前未自动命中专用 Skill。\n1. 3D启动时序分析\n2. 当前感知数据总结\n3. 直接源码分析",
                    details={
                        "mode": "bug_clarification",
                        "conversation_root_message_id": "om_clarification_root",
                        "user_request_text": "时间点2026-05-22 07:46 调查3D生命周期",
                        "needs_user_direction": True,
                        "intent_options": [
                            {"index": 1, "type": "skill", "skill_name": "3d-stuck-investigate", "label": "3D启动时序分析"},
                            {"index": 2, "type": "skill", "skill_name": "perception-data-summary", "label": "当前感知数据总结"},
                            {"index": 3, "type": "source_analysis", "label": "直接源码分析"},
                        ],
                    },
                ),
            )

            result = app.handle_event(
                event(
                    event_id="evt_followup_choice",
                    message_id="om_followup_choice",
                    content="@bot 直接源码分析",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "direct_analysis")
        self.assertEqual(len(fake_bug.requests), 1)
        self.assertEqual(fake_chat.context_calls, [])
    def test_bug_clarification_reply_numeric_choice_recovers_direct_analysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "analysis.md"
            html = Path(tmp) / "analysis.html"
            metadata.write_text("analysis", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            fake_lark.fetched_messages["om_followup_choice"] = json.dumps(
                {
                    "ok": True,
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_followup_choice",
                                "reply_to": "om_clarification_root",
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )
            fake_lark.fetched_messages["om_clarification_root"] = json.dumps(
                {
                    "ok": True,
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_clarification_root",
                                "reply_to": "om_file_msg",
                                "content": "@bot 时间点2026-05-22 07:46 调查3D生命周期",
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
                                "content": '<file key="file_v3_0011v_33d1772b-86ac-4789-b6e1-35c77ec29a5g" name="L1NSPGHB3SB010669log0.zip"/>',
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
            )
            clarification_event = event(
                event_id="evt_clarification_root",
                message_id="om_clarification_root",
                content="@bot 时间点2026-05-22 07:46 调查3D生命周期",
            )
            app.activity_store.record_event(clarification_event)
            app.activity_store.record_result(
                clarification_event,
                TaskResult(
                    success=True,
                    message="当前未自动命中专用 Skill。\n1. 3D启动时序分析\n2. 当前感知数据总结\n3. 直接源码分析",
                    details={
                        "mode": "bug_clarification",
                        "conversation_root_message_id": "om_clarification_root",
                        "user_request_text": "时间点2026-05-22 07:46 调查3D生命周期",
                        "needs_user_direction": True,
                        "intent_options": [
                            {"index": 1, "type": "skill", "skill_name": "3d-stuck-investigate", "label": "3D启动时序分析"},
                            {"index": 2, "type": "skill", "skill_name": "perception-data-summary", "label": "当前感知数据总结"},
                            {"index": 3, "type": "source_analysis", "label": "直接源码分析"},
                        ],
                    },
                ),
            )

            result = app.handle_event(
                event(
                    event_id="evt_followup_choice",
                    message_id="om_followup_choice",
                    content="@bot 1",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "direct_analysis")
        self.assertEqual(len(fake_bug.requests), 1)
