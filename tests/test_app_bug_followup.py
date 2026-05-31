from _app_base import *  # noqa: F401,F403
from _app_base import _AppTestBase


class AppBugFollowupTests(_AppTestBase):
    def test_bug_followup_reruns_when_existing_report_cannot_answer_new_source_request(self):
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
                    content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查3D启动时序",
                )
            )
            followup = app.handle_event(
                event(
                    event_id="evt_followup_source_gap",
                    message_id="om_followup_source_gap",
                    root_id="om_original_request",
                    parent_id="om_bot_reply",
                    content="@bot 这个结果不提 VCU_ELECTRICIT_PERCENT，基于源码重新看信号定义",
                )
            )

        self.assertTrue(first.success)
        self.assertTrue(followup.success)
        self.assertEqual(followup.details["mode"], "bug_reanalysis")
        self.assertEqual(len(fake_bug.reanalysis_calls), 1)
        self.assertTrue(fake_bug.reanalysis_calls[0]["force_rerun"])
        self.assertEqual(fake_bug.agent_followup_calls, [])
        self.assertEqual(fake_chat.context_calls, [])
    def test_generic_bug_reanalysis_keeps_previous_plan_despite_noisy_report_excerpt(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text(
                "<html><body>3D 生命周期报告 P. 上下电上下文 启动链路 SIGNAL_MCU_IG_ST</body></html>",
                encoding="utf-8",
            )
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            fake_chat = FakeOmlxChatClient()
            manual_calls = []

            def noisy_manual_selection(**kwargs):
                manual_calls.append(kwargs)
                return BugAnalysisSelection(
                    plans=[BugAnalysisPlan(kind="signal", signal_code="SIGNAL_MCU_IG_ST")],
                    skill_name="signal-chain-analyzer",
                    skill_label="信号链路分析",
                    source="manual_fallback",
                    reason="noisy report excerpt",
                )

            fake_bug._manual_bug_selection = noisy_manual_selection
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
                    message_id="om_original_3d_lifecycle",
                    content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6995380113 调查3D生命周期",
                )
            )
            followup = app.handle_event(
                event(
                    event_id="evt_generic_reanalysis",
                    message_id="om_generic_reanalysis",
                    root_id="om_original_3d_lifecycle",
                    parent_id="om_bot_reply",
                    content="@bot 重新分析一遍",
                )
            )

        self.assertTrue(first.success)
        self.assertTrue(followup.success)
        self.assertEqual(followup.details["mode"], "bug_reanalysis")
        self.assertEqual(manual_calls, [])
        call = fake_bug.reanalysis_calls[0]
        self.assertTrue(call["force_rerun"])
        self.assertIsNone(call["plans_override"])
        self.assertEqual(call["classification_skill"], "")
        self.assertEqual(call["classification_source"], "")
    def test_agent_intent_routes_bug_followup_to_same_agent_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html><body>根因是首帧超时</body></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            fake_chat = FakeOmlxChatClient()
            initial_bug_text = "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查3D启动时序"
            fake_intent = FakeIntentRunner(
                {
                    initial_bug_text: IntentDecision(
                        route="bug",
                        reason="新的 bug 链接分析请求",
                        confidence="high",
                        followup_action="none",
                        context_source="none",
                    ),
                    "继续把刚才那批日志往下查": IntentDecision(
                        route="analysis_followup",
                        reason="同一 bug 续聊",
                        confidence="high",
                        followup_action="continue_agent",
                        context_source="explicit",
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
                bug_runner=fake_bug,
                chat_client=fake_chat,
                intent_runner=fake_intent,
            )

            first = app.handle_event(
                event(
                    message_id="om_original_request",
                    content=f"@bot {initial_bug_text}"
                )
            )
            followup = app.handle_event(
                event(
                    event_id="evt_followup_agent",
                    message_id="om_followup_agent",
                    root_id="om_original_request",
                    parent_id="om_bot_reply",
                    content="@bot 继续把刚才那批日志往下查",
                )
            )

        self.assertTrue(first.success)
        self.assertTrue(followup.success)
        self.assertEqual(followup.details["mode"], "bug_agent_followup")
        self.assertEqual(len(fake_bug.agent_followup_calls), 1)
        self.assertEqual(fake_bug.agent_followup_calls[0]["followup_text"], "继续把刚才那批日志往下查")
        self.assertEqual(fake_chat.context_calls, [])
        self.assertEqual(fake_intent.calls, [])
    def test_bug_followup_without_reply_is_rejected(self):
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
                    event_id="evt_followup_latest_bug_agent",
                    message_id="om_followup_latest_bug_agent",
                    content="@bot 用你之前下载下来日志搜索 关键字看 卡顿skill 将23:10到23:15之间的系统卡顿报告发出来",
                )
            )

        self.assertTrue(first.success)
        self.assertFalse(followup.success)
        self.assertEqual(followup.error_code, "missing_followup_reply")
        self.assertEqual(len(fake_bug.agent_followup_calls), 0)
        self.assertEqual(fake_chat.context_calls, [])
    def test_bug_followup_existing_answer_replies_with_choice_card(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(Path(tmp) / "bug_metadata.md", Path(tmp) / "bug_report.html")
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    event_consumer=EventConsumerOptions(event_key="card.action.trigger"),
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                chat_client=FakeOmlxChatClient(),
            )
            app.conversation_store.remember(
                root_message_id="om_bug_root",
                chat_id="oc_denied",
                mode="bug_analysis",
                request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6994226322 分析主题相关bug",
                summary_text="- 问题时刻系统主题是黑夜，证据是 daynightMode=2、uiMode=35、Activity isNightMode=true。",
                report_url="http://127.0.0.1:8765/reports/om_bug_root/",
                report_excerpt="- 关键证据：系统 UI 已切到 Night，但 XTheme themeMode 还是 Day。",
            )

            followup = app.handle_event(
                event(
                    event_id="evt_existing_followup_card",
                    message_id="om_existing_followup_card",
                    root_id="om_bug_root",
                    content="@bot 问题时刻的 系统主题是白天还是黑夜",
                )
            )

        self.assertTrue(followup.success)
        self.assertEqual(followup.details["mode"], "bug_followup_existing_answer")
        self.assertEqual(len(fake_lark.card_replies), 1)
        self.assertEqual(fake_lark.replies, [])
        card = json.loads(fake_lark.card_replies[0]["card_json"])
        rendered = str(card)
        self.assertIn("先输入追问", rendered)
        self.assertIn("followup_prompt", rendered)
        self.assertIn("按输入从报告回答", rendered)
        self.assertIn("按输入重跑日志", rendered)
        self.assertIn("按输入续 Agent", rendered)
        self.assertIn("有用(记录)", rendered)
        self.assertIn("不准(记录)", rendered)
        self.assertIn("http://127.0.0.1:8765/reports/om_bug_root/", rendered)
    def test_answer_from_report_card_action_requires_input_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(Path(tmp) / "bug_metadata.md", Path(tmp) / "bug_report.html")
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"]),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                chat_client=FakeOmlxChatClient(),
            )
            app.conversation_store.remember(
                root_message_id="om_bug_root",
                chat_id="oc_denied",
                mode="bug_analysis",
                request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6994226322 分析主题相关bug",
                summary_text="- 系统主题是黑夜。",
                report_url="http://127.0.0.1:8765/reports/om_bug_root/",
                report_excerpt="- XTheme themeMode 还是 Day。",
            )

            result = app.handle_payload(
                {
                    "header": {"event_id": "evt_answer_no_prompt"},
                    "event": {
                        "context": {
                            "open_message_id": "om_card_answer",
                            "open_chat_id": "oc_denied",
                            "chat_type": "group",
                        },
                        "operator": {"operator_id": {"open_id": "ou_1"}},
                        "action": {
                            "value": {
                                "action": "answer_from_report",
                                "root_message_id": "om_bug_root",
                            }
                        },
                    },
                }
            )

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "missing_card_followup_prompt")
        self.assertEqual(fake_bug.agent_followup_calls, [])
        self.assertEqual(fake_bug.reanalysis_calls, [])
        self.assertTrue(fake_lark.replies)
        self.assertIn("请先在卡片输入框填写", fake_lark.replies[-1]["text"])
    def test_reanalysis_card_action_uses_form_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_bug = FakeBugRunner(Path(tmp) / "bug_metadata.md", Path(tmp) / "bug_report.html")
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"]),
                lark_client=FakeLarkClient(),
                bug_runner=fake_bug,
                chat_client=FakeOmlxChatClient(),
            )
            app.conversation_store.remember(
                root_message_id="om_bug_root",
                chat_id="oc_denied",
                mode="bug_analysis",
                request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6994226322 分析主题相关bug",
                summary_text="- 系统主题是黑夜。",
                report_url="http://127.0.0.1:8765/reports/om_bug_root/",
                report_excerpt="- XTheme themeMode 还是 Day。",
            )

            result = app.handle_payload(
                {
                    "header": {"event_id": "evt_reanalyze_form_prompt"},
                    "event": {
                        "context": {
                            "open_message_id": "om_card_reanalyze_form",
                            "open_chat_id": "oc_denied",
                            "chat_type": "group",
                        },
                        "operator": {"operator_id": {"open_id": "ou_1"}},
                        "action": {
                            "value": {
                                "action": "reanalyze",
                                "root_message_id": "om_bug_root",
                            },
                            "form_value": {
                                "followup_prompt": "根据导航源码分析 SR 页面生命周期",
                            },
                        },
                    },
                }
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "bug_reanalysis")
        self.assertEqual(len(fake_bug.reanalysis_calls), 1)
        self.assertEqual(fake_bug.reanalysis_calls[0]["followup_text"], "根据导航源码分析 SR 页面生命周期")
    def test_select_bug_skill_card_action_routes_reanalysis_to_selected_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_bug = FakeBugRunner(Path(tmp) / "bug_metadata.md", Path(tmp) / "bug_report.html")
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"]),
                lark_client=FakeLarkClient(),
                bug_runner=fake_bug,
                chat_client=FakeOmlxChatClient(),
            )
            request_text = "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6994226322 分析这个 bug"
            app.conversation_store.remember(
                root_message_id="om_bug_root",
                chat_id="oc_denied",
                mode="bug_clarification",
                request_text=request_text,
                summary_text="未命中专用 skill，请选择分析方向。",
                report_url="",
                report_excerpt="",
            )
            original = event(
                event_id="evt_bug_clarify",
                message_id="om_bug_root",
                content=request_text,
            )
            app.activity_store.record_event(original)
            app.activity_store.record_result(
                original,
                TaskResult(
                    success=True,
                    message="未命中专用 skill",
                    job_id="job_clarify",
                    job_dir=Path(tmp) / "jobs" / "job_clarify",
                    details={
                        "mode": "bug_clarification",
                        "conversation_root_message_id": "om_bug_root",
                        "bug_url": "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6994226322",
                        "user_request_text": request_text,
                    },
                ),
            )

            result = app.handle_payload(
                {
                    "header": {"event_id": "evt_select_xtheme"},
                    "event": {
                        "context": {
                            "open_message_id": "om_card_select",
                            "open_chat_id": "oc_denied",
                            "chat_type": "group",
                        },
                        "operator": {"operator_id": {"open_id": "ou_1"}},
                        "action": {
                            "value": {
                                "action": "select_bug_skill",
                                "root_message_id": "om_bug_root",
                                "job_id": "job_clarify",
                                "skill_name": "xtheme-analyzer",
                            },
                            "form_value": {"followup_prompt": "重点看问题时间前的 ThemeHelper 变化"},
                        },
                    },
                }
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "bug_reanalysis")
        self.assertEqual(len(fake_bug.reanalysis_calls), 1)
        call = fake_bug.reanalysis_calls[0]
        self.assertTrue(call["force_rerun"])
        self.assertEqual(call["classification_skill"], "xtheme-analyzer")
        self.assertEqual(call["classification_source"], "user_selected_card")
        self.assertEqual(call["plans_override"][0].kind, "xtheme")
        self.assertIn("ThemeHelper", call["followup_text"])
    def test_bug_result_card_offers_skill_correction_choices(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_bug = FakeBugRunner(Path(tmp) / "bug_metadata.md", Path(tmp) / "bug_report.html")
            fake_lark = FakeLarkClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    event_consumer=EventConsumerOptions(event_key="card.action.trigger"),
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                chat_client=FakeOmlxChatClient(),
            )
            result = TaskResult(
                success=True,
                message="Bug 分析完成\n结论：当前更像是感知链路问题。",
                job_id="job_bug_result",
                details={
                    "mode": "bug_analysis",
                    "published_report_url": "http://127.0.0.1:8765/reports/job_bug_result/",
                    "analysis_skill": "perception-data-summary",
                    "analysis_skill_label": "当前感知数据总结",
                    "classification_source": "agent",
                },
            )

            sent = app._try_send_result_card(
                event(message_id="om_bug_result"),
                result,
                delivery="reply",
                session_id="om_bug_result",
            )

        self.assertTrue(sent)
        self.assertEqual(len(fake_lark.card_replies), 1)
        rendered = fake_lark.card_replies[0]["card_json"]
        self.assertIn("意图/Skill 校正", rendered)
        self.assertIn("当前命中：当前感知数据总结", rendered)
        self.assertIn("select_bug_skill", rendered)
        self.assertIn("xtheme-analyzer", rendered)
        self.assertIn("perception-data-summary", rendered)
    def test_bug_result_card_hides_card_action_buttons_without_card_action_consumer(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_bug = FakeBugRunner(Path(tmp) / "bug_metadata.md", Path(tmp) / "bug_report.html")
            fake_lark = FakeLarkClient()
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"]),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                chat_client=FakeOmlxChatClient(),
            )
            result = TaskResult(
                success=True,
                message="Bug 分析完成\n结论：当前更像是启动时序问题。",
                job_id="job_bug_result",
                details={
                    "mode": "bug_analysis",
                    "published_report_url": "http://127.0.0.1:8765/reports/job_bug_result/",
                    "analysis_skill": "3d-stuck-investigate",
                    "analysis_skill_label": "3D启动时序分析",
                    "classification_source": "agent",
                    "agent_summary_provider": "direct_api",
                    "agent_summary_model": "mimo-v2.5-pro",
                },
            )

            sent = app._try_send_result_card(
                event(message_id="om_bug_result"),
                result,
                delivery="reply",
                session_id="om_bug_result",
            )

        self.assertTrue(sent)
        rendered = fake_lark.card_replies[0]["card_json"]
        self.assertIn("Agent 模型", rendered)
        self.assertIn("mimo-v2.5-pro", rendered)
        self.assertIn("命中 Skill", rendered)
        self.assertNotIn("意图/Skill 校正", rendered)
        self.assertNotIn("select_bug_skill", rendered)
        self.assertNotIn("feedback_helpful", rendered)
        self.assertNotIn("feedback_unhelpful", rendered)
        self.assertNotIn("下方按钮", rendered)
    def test_finished_progress_card_offers_skill_correction_choices(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_bug = FakeBugRunner(Path(tmp) / "bug_metadata.md", Path(tmp) / "bug_report.html")
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    event_consumer=EventConsumerOptions(event_key="card.action.trigger"),
                ),
                lark_client=FakeLarkClient(),
                bug_runner=fake_bug,
                chat_client=FakeOmlxChatClient(),
            )
            app._progress_cards["om_bug_result"] = {
                "title": "Bug 分析",
                "status": "analyzing",
                "details": {},
                "started_at": datetime.now(timezone.utc),
                "message_id": "om_card_1",
            }
            result = TaskResult(
                success=True,
                message="Bug 分析完成\n结论：当前更像是感知链路问题。",
                job_id="job_bug_result",
                details={
                    "mode": "bug_analysis",
                    "published_report_url": "http://127.0.0.1:8765/reports/job_bug_result/",
                    "analysis_skill": "perception-data-summary",
                    "analysis_skill_label": "当前感知数据总结",
                    "classification_source": "agent",
                    "agent_summary_provider": "direct_api",
                    "agent_summary_model": "mimo-v2.5-pro",
                    "conversation_root_message_id": "om_bug_result",
                },
            )

            card = app._build_progress_card(
                event(message_id="om_bug_result"),
                key="om_bug_result",
                status="completed",
                result=result,
                note=result.message,
            )

        rendered = str(card)
        self.assertIn("Agent 模型", rendered)
        self.assertIn("mimo-v2.5-pro", rendered)
        self.assertIn("意图/Skill 校正", rendered)
        self.assertIn("当前命中：当前感知数据总结", rendered)
        self.assertIn("select_bug_skill", rendered)
        self.assertIn("命中 Skill", rendered)
    def test_continue_agent_card_action_explicitly_resumes_saved_agent_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_bug = FakeBugRunner(Path(tmp) / "bug_metadata.md", Path(tmp) / "bug_report.html")
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"]),
                lark_client=FakeLarkClient(),
                bug_runner=fake_bug,
                chat_client=FakeOmlxChatClient(),
            )
            app.conversation_store.remember(
                root_message_id="om_bug_root",
                chat_id="oc_denied",
                mode="bug_analysis",
                request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6994226322 分析主题相关bug",
                summary_text="- 系统主题是黑夜。",
                report_url="http://127.0.0.1:8765/reports/om_bug_root/",
                report_excerpt="- XTheme themeMode 还是 Day。",
            )

            result = app.handle_payload(
                {
                    "header": {"event_id": "evt_continue_agent_card"},
                    "event": {
                        "context": {
                            "open_message_id": "om_card",
                            "open_chat_id": "oc_denied",
                            "chat_type": "group",
                        },
                        "operator": {"operator_id": {"open_id": "ou_1"}},
                        "action": {
                            "value": {
                                "action": "continue_agent",
                                "root_message_id": "om_bug_root",
                                "followup_text": "继续原 Agent 会话看一下刚才的判断",
                            }
                        },
                    },
                }
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "bug_agent_followup")
        self.assertEqual(len(fake_bug.agent_followup_calls), 1)
        self.assertTrue(fake_bug.agent_followup_calls[0]["resume_agent_session"])
        self.assertEqual(fake_bug.agent_followup_calls[0]["followup_text"], "继续原 Agent 会话看一下刚才的判断")
    def test_feedback_card_action_records_activity_progress_with_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"]),
                lark_client=fake_lark,
                bug_runner=FakeBugRunner(Path(tmp) / "bug_metadata.md", Path(tmp) / "bug_report.html"),
                chat_client=FakeOmlxChatClient(),
            )
            app.conversation_store.remember(
                root_message_id="om_bug_root",
                chat_id="oc_denied",
                mode="bug_analysis",
                request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6994226322 分析主题相关bug",
                summary_text="- 系统主题是黑夜。",
                report_url="http://127.0.0.1:8765/reports/om_bug_root/",
                report_excerpt="- XTheme themeMode 还是 Day。",
            )

            result = app.handle_payload(
                {
                    "header": {"event_id": "evt_feedback_card"},
                    "event": {
                        "context": {
                            "open_message_id": "om_card_feedback",
                            "open_chat_id": "oc_denied",
                            "chat_type": "group",
                        },
                        "operator": {"operator_id": {"open_id": "ou_1"}},
                        "action": {
                            "value": {
                                "action": "feedback_unhelpful",
                                "root_message_id": "om_bug_root",
                                "job_id": "job_1",
                                "followup_text": "根据导航源码分析",
                            }
                        },
                    },
                }
            )

            session = app.activity_store.get_session("om_bug_root") or {}
            feedback_events = [
                item for item in session.get("progress", []) if item.get("stage") == "followup_feedback_recorded"
            ]

        self.assertTrue(result.success)
        self.assertEqual(result.details["feedback"], "unhelpful")
        self.assertEqual(len(feedback_events), 1)
        self.assertEqual(feedback_events[0]["details"]["feedback"], "unhelpful")
        self.assertEqual(feedback_events[0]["details"]["followup_text"], "根据导航源码分析")
        self.assertEqual(feedback_events[0]["details"]["job_id"], "job_1")
        self.assertTrue(fake_lark.replies)
    def test_feedback_card_action_uses_saved_chat_context_when_callback_omits_chat_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"]),
                lark_client=fake_lark,
                bug_runner=FakeBugRunner(Path(tmp) / "bug_metadata.md", Path(tmp) / "bug_report.html"),
                chat_client=FakeOmlxChatClient(),
            )
            app.conversation_store.remember(
                root_message_id="om_bug_root",
                chat_id="oc_denied",
                mode="bug_analysis",
                request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6994226322 分析主题相关bug",
                summary_text="- 系统主题是黑夜。",
                report_url="http://127.0.0.1:8765/reports/om_bug_root/",
                report_excerpt="- XTheme themeMode 还是 Day。",
            )

            result = app.handle_payload(
                {
                    "header": {"event_id": "evt_feedback_no_chat_id"},
                    "event": {
                        "context": {
                            "open_message_id": "om_card_feedback",
                            "chat_type": "group",
                        },
                        "operator": {"operator_id": {"open_id": "ou_1"}},
                        "action": {
                            "value": {
                                "action": "feedback_helpful",
                                "root_message_id": "om_bug_root",
                                "job_id": "job_1",
                            }
                        },
                    },
                }
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["feedback"], "helpful")
        self.assertTrue(fake_lark.replies)
        self.assertEqual(fake_lark.replies[-1]["message_id"], "om_card_feedback")
    def test_reanalysis_card_action_passes_authorized_local_log_resources(self):
        with tempfile.TemporaryDirectory() as tmp:
            downloads = Path(tmp) / "downloads"
            downloads.mkdir()
            local_log = downloads / "Log.zip"
            local_log.write_text("log", encoding="utf-8")
            fake_bug = FakeBugRunner(Path(tmp) / "bug_metadata.md", Path(tmp) / "bug_report.html")
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp) / "data",
                    allowed_chats=["oc_denied"],
                    allowed_users=["ou_1"],
                    local_resources=LocalResourceOptions(allowed_dirs=[downloads]),
                ),
                lark_client=FakeLarkClient(),
                bug_runner=fake_bug,
                chat_client=FakeOmlxChatClient(),
            )
            app.conversation_store.remember(
                root_message_id="om_bug_root",
                chat_id="oc_denied",
                mode="bug_analysis",
                request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6994226322 分析主题相关bug",
                summary_text="- 上一轮未带日志。",
                report_url="http://127.0.0.1:8765/reports/om_bug_root/",
                report_excerpt="- 缺少日志。",
            )
            app.activity_store.record_result(
                event(message_id="om_bug_root", content="@bot bug"),
                __import__("lark_agent_bridge.models", fromlist=["TaskResult"]).TaskResult(
                    success=True,
                    message="上一轮完成",
                    job_id="job_1",
                    job_dir=Path(tmp) / "data" / "jobs" / "job_1",
                    details={"mode": "bug_analysis", "prepared_log_input": "", "selected_log_input": ""},
                ),
            )

            result = app.handle_payload(
                {
                    "header": {"event_id": "evt_reanalyze_local_log"},
                    "event": {
                        "context": {
                            "open_message_id": "om_card_reanalyze",
                            "open_chat_id": "oc_denied",
                            "chat_type": "group",
                        },
                        "operator": {"operator_id": {"open_id": "ou_1"}},
                        "action": {
                            "value": {
                                "action": "reanalyze",
                                "root_message_id": "om_bug_root",
                                "job_id": "job_1",
                            },
                            "form_value": {
                                "followup_prompt": "日志我下载到服务器的下载目录了 Log.zip 基于这个日志分析",
                            },
                        },
                    },
                }
            )

        self.assertTrue(result.success)
        self.assertEqual(len(fake_bug.reanalysis_calls), 1)
        local_resources = fake_bug.reanalysis_calls[0]["local_log_resources"]
        self.assertEqual(len(local_resources), 1)
        self.assertEqual(local_resources[0].kind, "local")
        self.assertEqual(Path(local_resources[0].value), local_log.resolve())
    def test_bug_followup_answers_from_existing_context_without_resuming_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(Path(tmp) / "bug_metadata.md", Path(tmp) / "bug_report.html")
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"]),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                chat_client=FakeOmlxChatClient(),
            )
            app.conversation_store.remember(
                root_message_id="om_bug_root",
                chat_id="oc_denied",
                mode="bug_analysis",
                request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6994226322 分析主题相关bug",
                summary_text=(
                    "## 结论摘要\n"
                    "- 问题时刻系统主题是黑夜，证据是 daynightMode=2、uiMode=35、Activity isNightMode=true。\n"
                    "- SR/XTheme 业务链路仍停在 Day。"
                ),
                report_url="http://127.0.0.1:8765/reports/om_bug_root/",
                report_excerpt="关键证据：系统 UI 已切到 Night，但 XTheme themeMode 还是 Day。",
            )

            followup = app.handle_event(
                event(
                    event_id="evt_existing_followup",
                    message_id="om_existing_followup",
                    root_id="om_bug_root",
                    content="@bot 问题时刻的 系统主题是白天还是黑夜",
                )
            )

        self.assertTrue(followup.success)
        self.assertEqual(followup.details["mode"], "bug_followup_existing_answer")
        self.assertGreaterEqual(followup.details["answer_confidence"], 0.8)
        self.assertEqual(fake_bug.agent_followup_calls, [])
        self.assertEqual(fake_bug.reanalysis_calls, [])
        self.assertIn("已有报告", followup.message)
        self.assertIn("黑夜", followup.message)
        self.assertIn("http://127.0.0.1:8765/reports/om_bug_root/", followup.message)
    def test_bug_followup_existing_answer_does_not_use_user_question_as_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(Path(tmp) / "bug_metadata.md", Path(tmp) / "bug_report.html")
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"]),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                chat_client=FakeOmlxChatClient(),
            )
            app.conversation_store.remember(
                root_message_id="om_bug_root",
                chat_id="oc_denied",
                mode="bug_analysis",
                request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6994226322 分析主题相关bug",
                summary_text="- 问题时刻 `2026-05-18 14:03:01.457` 的系统主题是黑夜。置信度：高。",
                report_url="http://127.0.0.1:8765/reports/om_bug_root/",
                report_excerpt="- main.txt 在 14:02:53.597 记录 `onDayNightModeChanged,isNightMode=true`。",
            )
            app.conversation_store.append_exchange(
                "om_bug_root",
                user_text="问题时刻的 系统主题是白天还是黑夜",
                assistant_text="问题时刻系统主题是黑夜。",
            )

            followup = app.handle_event(
                event(
                    event_id="evt_existing_followup_no_user_evidence",
                    message_id="om_existing_followup_no_user_evidence",
                    root_id="om_bug_root",
                    content="@bot 问题时刻的 系统主题是白天还是黑夜",
                )
            )

        self.assertTrue(followup.success)
        self.assertEqual(followup.details["mode"], "bug_followup_existing_answer")
        self.assertEqual(fake_bug.agent_followup_calls, [])
        self.assertIn("系统主题是黑夜", followup.message)
        self.assertIn("isNightMode=true", followup.message)
        self.assertNotIn("- 问题时刻的 系统主题是白天还是黑夜", followup.message)
    def test_bug_followup_low_confidence_existing_answer_delegates_to_agent(self):
        class DecidingBugRunner(FakeBugRunner):
            def decide_bug_followup(self, **kwargs):
                return BugFollowupSelection(
                    should_reanalyze=False,
                    force_rerun=False,
                    plans=[],
                    skill_name="xtheme-analyzer",
                    skill_label="XTheme时光主题分析",
                    source="agent",
                    reason="问题引入了已有摘要没有覆盖的新概念，交给 Agent 续聊。",
                    provider="codex",
                )

        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_bug = DecidingBugRunner(Path(tmp) / "bug_metadata.md", Path(tmp) / "bug_report.html")
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"]),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                chat_client=FakeOmlxChatClient(),
            )
            app.conversation_store.remember(
                root_message_id="om_bug_root",
                chat_id="oc_denied",
                mode="bug_analysis",
                request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6994226322 分析主题相关bug",
                summary_text="- 问题时刻系统主题是黑夜，证据是 daynightMode=2、uiMode=35、Activity isNightMode=true。",
                report_url="http://127.0.0.1:8765/reports/om_bug_root/",
                report_excerpt="- 关键证据：系统 UI 已切到 Night，但 XTheme themeMode 还是 Day。",
            )

            followup = app.handle_event(
                event(
                    event_id="evt_existing_followup_application_theme",
                    message_id="om_existing_followup_application_theme",
                    root_id="om_bug_root",
                    content="@bot 问题时刻 application主题 是白天还是黑夜",
                )
            )

        self.assertTrue(followup.success)
        self.assertEqual(followup.details["mode"], "bug_agent_followup")
        self.assertEqual(len(fake_bug.agent_followup_calls), 1)
        self.assertEqual(fake_bug.reanalysis_calls, [])
    def test_bug_followup_sends_progress_card_before_expensive_reanalysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html><body>根因是首帧超时</body></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"]),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                chat_client=FakeOmlxChatClient(),
            )
            app.conversation_store.remember(
                root_message_id="om_bug_root",
                chat_id="oc_denied",
                mode="bug_analysis",
                request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6994226322 分析主题相关bug",
                summary_text="已有摘要",
                report_url="http://127.0.0.1:8765/reports/om_bug_root/",
                report_excerpt="已有报告",
            )

            followup = app.handle_event(
                event(
                    event_id="evt_reanalysis_followup",
                    message_id="om_reanalysis_followup",
                    root_id="om_bug_root",
                    content="@bot 结果不合理，基于源码重新分析",
                )
            )

        self.assertTrue(followup.success)
        self.assertEqual(len(fake_bug.reanalysis_calls), 1)
        self.assertFalse(fake_lark.replies)
        self.assertTrue(fake_lark.card_replies)
        self.assertEqual(fake_lark.card_replies[0]["message_id"], "om_reanalysis_followup")
        self.assertIn("请求处理中", fake_lark.card_replies[0]["card_json"])
        self.assertTrue(any("bug_followup_decision_started" in item["card_json"] for item in fake_lark.updated_cards))
        self.assertTrue(any("已完成" in item["card_json"] for item in fake_lark.updated_cards))
        final_card = fake_lark.updated_cards[-1]["card_json"]
        self.assertIn("Bug 重新分析", final_card)
        self.assertNotIn("Bug 追问", final_card)
        self.assertNotIn("续聊判断", final_card)
    def test_agent_intent_followup_without_reply_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html><body>根因是首帧超时</body></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            fake_chat = FakeOmlxChatClient()
            initial_bug_text = "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查3D启动时序"
            followup_text = "以23:12为准，照旧日志再来一次"
            fake_intent = FakeIntentRunner(
                {
                    initial_bug_text: IntentDecision(
                        route="bug",
                        reason="新的 bug 链接分析请求",
                        confidence="high",
                        followup_action="none",
                        context_source="none",
                    ),
                    followup_text: IntentDecision(
                        route="analysis_followup",
                        reason="同群最近一次 bug 结果的时间修正续聊",
                        confidence="high",
                        followup_action="reanalysis",
                        context_source="latest_chat",
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
                bug_runner=fake_bug,
                chat_client=fake_chat,
                intent_runner=fake_intent,
            )

            first = app.handle_event(
                event(
                    message_id="om_original_request",
                    content=f"@bot {initial_bug_text}"
                )
            )
            followup = app.handle_event(
                event(
                    event_id="evt_followup_intent_latest",
                    message_id="om_followup_intent_latest",
                    content=f"@bot {followup_text}",
                )
            )

        self.assertTrue(first.success)
        self.assertFalse(followup.success)
        self.assertEqual(followup.error_code, "missing_followup_reply")
        self.assertEqual(len(fake_bug.reanalysis_calls), 0)
        self.assertEqual(fake_bug.agent_followup_calls, [])
        self.assertEqual(fake_chat.context_calls, [])
    def test_group_followup_intent_without_reply_chain_is_rejected(self):
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
                intent_runner=FakeIntentRunner(enabled=False),
            )
            followup = app.handle_event(
                event(
                    event_id="evt_group_latest_followup_no_mention",
                    message_id="om_group_latest_followup_no_mention",
                    content="重新分析一遍",
                )
            )

        self.assertTrue(followup.success)
        self.assertTrue(followup.skipped)
        self.assertEqual(followup.details["mode"], "not_addressed")
        self.assertEqual(len(fake_bug.reanalysis_calls), 0)
    def test_followup_reanalysis_fetches_current_message_reply_chain_through_clarification_to_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_lark.fetched_messages["om_followup_retry"] = json.dumps(
                {
                    "ok": True,
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_followup_retry",
                                "reply_to": "om_clarification",
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )
            fake_lark.fetched_messages["om_clarification"] = json.dumps(
                {
                    "ok": True,
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_clarification",
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
                                "content": '<file key="file_v3_0011v_33d1772b-86ac-4789-b6e1-35c77ec29a5g" name="L1NSPGHB3SB010669log0.zip"/>',
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
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                chat_client=FakeOmlxChatClient(),
            )
            clarification_event = event(
                event_id="evt_clarification",
                message_id="om_clarification",
                content="@bot 基于SRViolationHandler.kt源码分析 时间点5月22日 7:46 分析超速状态",
            )
            app.activity_store.record_event(clarification_event)
            app.activity_store.record_result(
                clarification_event,
                TaskResult(
                    success=True,
                    message="缺少明确问题时间",
                    details={
                        "mode": "bug_time_clarification",
                        "user_request_text": "基于SRViolationHandler.kt源码分析 时间点5月22日 7:46 分析超速状态",
                    },
                ),
            )

            followup = app.handle_event(
                event(
                    event_id="evt_followup_retry",
                    message_id="om_followup_retry",
                    content="@bot 重新分析",
                )
            )

        self.assertTrue(followup.success)
        self.assertEqual(followup.details["mode"], "direct_analysis")
        self.assertEqual(len(fake_bug.requests), 1)
        self.assertEqual(fake_bug.requests[0].prompt, "基于SRViolationHandler.kt源码分析 时间点5月22日 7:46 分析超速状态")
        self.assertEqual(len(fake_bug.requests[0].resources), 1)
        self.assertEqual(fake_bug.requests[0].resources[0].kind, "file")
        self.assertEqual(fake_bug.requests[0].resources[0].value, "file_v3_0011v_33d1772b-86ac-4789-b6e1-35c77ec29a5g")
    def test_followup_reanalysis_prefers_direct_analysis_recovery_when_clarification_has_job_but_no_bug_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_lark.fetched_messages["om_followup_retry"] = json.dumps(
                {
                    "ok": True,
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_followup_retry",
                                "reply_to": "om_clarification",
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )
            fake_lark.fetched_messages["om_clarification"] = json.dumps(
                {
                    "ok": True,
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_clarification",
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
                                "content": '<file key="file_v3_0011v_33d1772b-86ac-4789-b6e1-35c77ec29a5g" name="L1NSPGHB3SB010669log0.zip"/>',
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
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                chat_client=FakeOmlxChatClient(),
            )
            clarification_event = event(
                event_id="evt_clarification_with_job",
                message_id="om_clarification",
                content="@bot 基于SRViolationHandler.kt源码分析 时间点5月22日 7:46 分析超速状态",
            )
            app.activity_store.record_event(clarification_event)
            app.activity_store.record_result(
                clarification_event,
                TaskResult(
                    success=True,
                    message="缺少明确问题时间",
                    job_id="job_direct_pending",
                    job_dir=Path(tmp) / "jobs" / "job_direct_pending",
                    details={
                        "mode": "bug_time_clarification",
                        "user_request_text": "基于SRViolationHandler.kt源码分析 时间点5月22日 7:46 分析超速状态",
                        "bug_url": "",
                    },
                ),
            )

            followup = app.handle_event(
                event(
                    event_id="evt_followup_retry_with_job",
                    message_id="om_followup_retry",
                    content="@bot 重新分析",
                )
            )

        self.assertTrue(followup.success)
        self.assertEqual(followup.details["mode"], "direct_analysis")
        self.assertEqual(len(fake_bug.requests), 1)
        self.assertEqual(len(fake_bug.reanalysis_calls), 0)
        self.assertEqual(fake_bug.requests[0].prompt, "基于SRViolationHandler.kt源码分析 时间点5月22日 7:46 分析超速状态")
