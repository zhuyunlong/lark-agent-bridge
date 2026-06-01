from _app_base import *  # noqa: F401,F403
from _app_base import _AppTestBase


class AppBugRequestTests(_AppTestBase):
    def test_progress_token_usage_normalizes_prompt_completion_aliases(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = BridgeApp(BridgeConfig(data_dir=Path(tmp)))
            result = TaskResult(
                success=True,
                message="ok",
                details={
                    "agent_summary_prompt_tokens": 321,
                    "agent_summary_cached_input_tokens": 280,
                    "agent_summary_completion_tokens": 54,
                    "agent_summary_total_tokens": 375,
                },
            )

            usage = app._progress_token_usage(result)

        self.assertEqual(
            usage,
            {
                "input_tokens": 321,
                "cached_input_tokens": 280,
                "output_tokens": 54,
                "total_tokens": 375,
            },
        )
    def test_record_daemon_status_updates_health_monitor_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = BridgeApp(BridgeConfig(data_dir=Path(tmp)))

            app.record_daemon_status({"stage": "event_consumer_ready", "process_id": os.getpid()})

            health = app.check()["health"]
        self.assertEqual(health["components"]["event_consumer"]["pid"], os.getpid())
        self.assertTrue(health["components"]["event_consumer"]["alive"])
    def test_health_monitor_restores_daemon_pid_from_saved_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(data_dir=Path(tmp))
            app = BridgeApp(config)
            app.record_daemon_status({"stage": "event_consumer_ready", "process_id": os.getpid()})

            restored = BridgeApp(config)
            health = restored.check()["health"]

        self.assertEqual(health["components"]["event_consumer"]["pid"], os.getpid())
        self.assertTrue(health["components"]["event_consumer"]["alive"])
    def test_health_maintenance_records_stuck_process_cleanup(self):
        class FakeWatchdog:
            def terminate_stuck(self):
                return [{"pid": 123, "name": "agent", "terminated": True, "idle_seconds": 99}]

        with tempfile.TemporaryDirectory() as tmp:
            app = BridgeApp(BridgeConfig(data_dir=Path(tmp)))
            app.process_watchdog = FakeWatchdog()

            cleaned = app.run_health_maintenance()
            session = app.activity_store.get_session("daemon")

        self.assertEqual(cleaned[0]["pid"], 123)
        self.assertEqual(session["progress"][0]["stage"], "stuck_process_cleanup")
    def test_default_runners_share_process_watchdog(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = BridgeApp(BridgeConfig(data_dir=Path(tmp)))

            self.assertIs(app.claude_runner.process_watchdog, app.process_watchdog)
            self.assertIs(app.bug_runner.process_watchdog, app.process_watchdog)
            self.assertIs(app.perception_runner.process_watchdog, app.process_watchdog)
            self.assertIs(app.intent_runner.process_watchdog, app.process_watchdog)
            self.assertIs(app.handler.runner.process_watchdog, app.process_watchdog)
    def test_reanalysis_keeps_same_bug_report_version_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_chat = FakeOmlxChatClient()
            fake_chat = FakeOmlxChatClient()
            fake_bug = FakeBugRunner(metadata, html)
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"]),
                lark_client=fake_lark,
                bug_runner=fake_bug,
            )
            bug_url = "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722"

            first = app.handle_event(event(content=f"@bot {bug_url} 调查3D启动和卡顿"))
            followup = app.handle_event(
                event(
                    event_id="evt_reanalysis_version",
                    message_id="om_reanalysis_version",
                    content="@bot 重新分析，故障时间改成 11:30",
                    reply_to=first.details["conversation_root_message_id"],
                )
            )

        self.assertEqual(first.details["report_group_key"], f"bug:{bug_url}")
        self.assertEqual(followup.details["report_group_key"], f"bug:{bug_url}")
        self.assertEqual(followup.details["report_version"], 2)
        self.assertNotEqual(first.details["published_report_url"], followup.details["published_report_url"])
        self.assertTrue(first.details["published_report_url"].endswith("/v1/report.html"))
        self.assertTrue(followup.details["published_report_url"].endswith("/v2/report.html"))
    def test_report_ready_notification_pushes_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    notifications=NotificationOptions(enabled=True),
                ),
                lark_client=fake_lark,
                bug_runner=FakeBugRunner(metadata, html),
            )

            result = app.handle_event(
                event(content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/1 调查3D启动")
            )

        self.assertTrue(result.success)
        self.assertTrue(any("分析报告已生成" in item["text"] for item in fake_lark.sent))
    def test_report_ready_notification_dedup_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            config = BridgeConfig(
                dry_run=False,
                data_dir=Path(tmp) / "data",
                allowed_chats=["oc_denied"],
                notifications=NotificationOptions(enabled=True),
            )

            first_lark = FakeLarkClient()
            first_app = BridgeApp(config, lark_client=first_lark, bug_runner=FakeBugRunner(metadata, html))
            first_app.handle_event(
                event(content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/1 调查3D启动")
            )

            second_lark = FakeLarkClient()
            second_app = BridgeApp(config, lark_client=second_lark, bug_runner=FakeBugRunner(metadata, html))
            second_app.handle_event(
                event(
                    event_id="evt_restart_notification",
                    message_id="om_restart_notification",
                    content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/1 调查3D启动",
                )
            )

        self.assertTrue(any("分析报告已生成" in item["text"] for item in first_lark.sent))
        self.assertFalse(any("分析报告已生成" in item["text"] for item in second_lark.sent))
    def test_dual_agent_arbitration_is_added_when_secondary_summary_exists(self):
        class DualFakeBugRunner(FakeBugRunner):
            def run_bug_analysis(self, request, *, event=None, progress_callback=None):
                result = super().run_bug_analysis(request, event=event, progress_callback=progress_callback)
                result.message = "根因：网络超时"
                result.details["agent_summary_provider"] = "codex"
                result.details["secondary_agent_summary"] = "根因：内存泄漏"
                result.details["secondary_agent_provider"] = "claude"
                return result

        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    dual_agent=DualAgentOptions(enabled=True),
                ),
                lark_client=FakeLarkClient(),
                bug_runner=DualFakeBugRunner(metadata, html),
            )

            result = app.handle_event(
                event(content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/1 调查3D启动")
            )

        self.assertIn("arbitration", result.details)
        self.assertIn("双 Agent 裁决", result.message)
    def test_bug_request_waits_for_card_approval_then_runs_after_approve_action(self):
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
                    approval=ApprovalOptions(enabled=True),
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
            )

            pending = app.handle_event(
                event(
                    content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查3D启动和卡顿"
                )
            )
            self.assertFalse(pending.success)
            self.assertEqual(pending.error_code, "approval_pending")
            self.assertEqual(app.activity_store.get_session("om_1")["status"], "pending")
            self.assertEqual(len(fake_bug.requests), 0)
            self.assertEqual(len(fake_lark.cards), 1)

            import json
            card = json.loads(fake_lark.cards[0]["card_json"])
            request_id = card["elements"][-1]["actions"][0]["value"]["request_id"]
            approved = app.handle_card_action_payload(
                {
                    "event": {
                        "operator": {"operator_id": {"open_id": "ou_1"}},
                        "action": {"value": {"action": "approve", "request_id": request_id}},
                    }
                }
            )

        self.assertTrue(approved.success)
        self.assertEqual(approved.details["mode"], "bug_analysis")
        self.assertEqual(len(fake_bug.requests), 1)
    def test_expired_approval_action_does_not_run_operation(self):
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
                    approval=ApprovalOptions(enabled=True),
                ),
                lark_client=FakeLarkClient(),
                bug_runner=fake_bug,
            )
            app.approval_store.default_ttl_seconds = 0.01
            pending = app.handle_event(
                event(content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查3D启动")
            )
            time.sleep(0.02)

            approved = app.handle_card_action_payload(
                {
                    "event": {
                        "action": {
                            "value": {
                                "action": "approve",
                                "request_id": pending.details["approval_request_id"],
                            }
                        }
                    }
                }
            )

        self.assertFalse(approved.success)
        self.assertEqual(approved.error_code, "approval_not_available")
        self.assertEqual(len(fake_bug.requests), 0)
    def test_reject_card_action_does_not_run_pending_operation(self):
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
                    approval=ApprovalOptions(enabled=True),
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
            )
            pending = app.handle_event(
                event(
                    content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查3D启动时序"
                )
            )
            request_id = pending.details["approval_request_id"]

            rejected = app.handle_card_action_payload(
                {"event": {"action": {"value": {"action": "reject", "request_id": request_id}}}}
            )

        self.assertFalse(rejected.success)
        self.assertEqual(rejected.error_code, "approval_rejected")
        self.assertEqual(len(fake_bug.requests), 0)
    def test_direct_analysis_waits_for_approval_then_runs_after_approve_action(self):
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
                    approval=ApprovalOptions(enabled=True),
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
            )

            pending = app.handle_event(event(content="@bot 分析启动和卡顿 file_log_123"))
            self.assertFalse(pending.success)
            self.assertEqual(pending.error_code, "approval_pending")
            self.assertEqual(len(fake_bug.requests), 0)

            approved = app.handle_card_action_payload(
                {
                    "event": {
                        "action": {
                            "value": {
                                "action": "approve",
                                "request_id": pending.details["approval_request_id"],
                            }
                        }
                    }
                }
            )

        self.assertTrue(approved.success)
        self.assertEqual(approved.details["mode"], "direct_analysis")
        self.assertEqual(len(fake_bug.requests), 1)
    def test_followup_reanalysis_waits_for_approval_then_runs_after_approve_action(self):
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
                    approval=ApprovalOptions(enabled=True),
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
            )
            app.conversation_store.remember(
                root_message_id="om_root",
                chat_id="oc_denied",
                mode="bug_analysis",
                request_text="原始 bug 分析",
                summary_text="原始结论",
                report_url="http://report",
                report_excerpt="报告摘录",
            )

            pending = app.handle_event(
                event(
                    event_id="evt_reanalysis_pending",
                    message_id="om_followup",
                    reply_to="om_root",
                    content="@bot 重新分析",
                )
            )
            self.assertFalse(pending.success)
            self.assertEqual(pending.error_code, "approval_pending")
            self.assertEqual(len(fake_bug.reanalysis_calls), 0)

            approved = app.handle_card_action_payload(
                {
                    "event": {
                        "action": {
                            "value": {
                                "action": "approve",
                                "request_id": pending.details["approval_request_id"],
                            }
                        }
                    }
                }
            )

        self.assertTrue(approved.success)
        self.assertEqual(approved.details["mode"], "bug_reanalysis")
        self.assertEqual(len(fake_bug.reanalysis_calls), 1)
    def test_card_reanalysis_rejects_missing_context_before_chat_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = BridgeApp(BridgeConfig(data_dir=Path(tmp)))

            result = app.handle_card_action_payload(
                {
                    "event": {
                        "context": {"open_message_id": "om_card"},
                        "action": {
                            "value": {
                                "action": "reanalyze",
                                "root_message_id": "om_root",
                            }
                        },
                    }
                }
            )

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "missing_reanalysis_context")
    def test_card_reanalysis_resolves_context_via_job_id(self):
        class JobAwareBugRunner(FakeBugRunner):
            def run_bug_analysis(self, request, *, event=None, progress_callback=None):
                result = super().run_bug_analysis(request, event=event, progress_callback=progress_callback)
                result.job_id = "job_bug_1"
                return result

        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html><body>根因是首帧超时</body></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = JobAwareBugRunner(metadata, html)
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
                    content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查3D启动时序",
                )
            )
            result = app.handle_card_action_payload(
                {
                    "header": {"event_id": "evt_card_reanalyze"},
                    "event": {
                        "context": {
                            "open_message_id": "om_card",
                            "open_chat_id": "oc_denied",
                        },
                        "operator": {"operator_id": {"open_id": "ou_1"}},
                        "action": {
                            "value": {
                                "action": "reanalyze",
                                "job_id": "job_bug_1",
                            },
                            "form_value": {
                                "followup_prompt": "基于已下载日志和源码重新检查启动时序",
                            },
                        },
                    },
                }
            )

        self.assertTrue(first.success)
        self.assertTrue(result.success)
        self.assertEqual(len(fake_bug.reanalysis_calls), 1)
        self.assertEqual(
            fake_bug.reanalysis_calls[0]["followup_text"],
            "基于已下载日志和源码重新检查启动时序",
        )
        self.assertEqual(result.details["mode"], "bug_reanalysis")
    def test_workflow_archive_failure_does_not_block_delivery(self):
        class FailingArchiver:
            def archive(self, *args, **kwargs):
                raise RuntimeError("archive permission denied")

        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    workflow_archive=WorkflowArchiveOptions(enabled=True),
                ),
                lark_client=fake_lark,
                bug_runner=FakeBugRunner(metadata, html),
            )
            app.workflow_archiver = FailingArchiver()

            result = app.handle_event(
                event(content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/1 调查3D启动")
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["workflow_archive"]["error_type"], "RuntimeError")
        self.assertTrue(fake_lark.replies or fake_lark.card_replies)
    def test_bug_request_emits_progress_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = FakeBugRunner(metadata, html)
            progress_events = []
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                progress_callback=progress_events.append,
            )

            result = app.handle_event(
                event(
                    content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查3D启动和卡顿"
                )
            )

        self.assertTrue(result.success)
        self.assertGreaterEqual(len(progress_events), 4)
        self.assertEqual(progress_events[0]["stage"], "bug_request_received")
        self.assertEqual(progress_events[1]["stage"], "bug_fetch_data")
        self.assertEqual(progress_events[-1]["stage"], "file_uploaded")
        self.assertTrue(all(event.get("details", {}).get("executor") for event in progress_events))
        self.assertEqual(progress_events[0]["details"]["executor"], "Bridge 编排器")
    def test_bug_request_creates_updates_and_finalizes_progress_card(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                bug_runner=FakeBugRunner(metadata, html),
            )

            result = app.handle_event(
                event(content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/1 调查3D启动")
            )

        self.assertTrue(result.success)
        self.assertGreaterEqual(len(fake_lark.card_replies), 1)
        self.assertGreaterEqual(len(fake_lark.updated_cards), 1)
        self.assertTrue(any("bug_fetch_data" in item["card_json"] for item in fake_lark.updated_cards))
        self.assertTrue(any("飞书/Meegle CLI" in item["card_json"] for item in fake_lark.updated_cards))
        self.assertTrue(any("已完成" in item["card_json"] for item in fake_lark.updated_cards))
    def test_progress_live_url_uses_lan_ip_when_report_server_binds_all_interfaces(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "lark_agent_bridge.app.result_bug.resolve_public_base_url"
        ) as resolve_url:
            resolve_url.return_value = "http://10.2.3.4:8765/reports"
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    report_server=ReportServerOptions(
                        enabled=True,
                        bind_host="0.0.0.0",
                        port=8765,
                        public_base_url="",
                    ),
                ),
                lark_client=FakeLarkClient(),
            )

            live_url = app._progress_live_url("om_1")

        self.assertEqual(live_url, "http://10.2.3.4:8765/sessions?session=om_1")
        resolve_url.assert_called_with("", port=8765, bind_host="0.0.0.0")
    def test_completed_progress_card_keeps_result_followup_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    event_consumer=EventConsumerOptions(event_key="card.action.trigger"),
                ),
                lark_client=fake_lark,
                bug_runner=FakeBugRunner(metadata, html),
            )

            result = app.handle_event(
                event(content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/1 调查3D启动")
            )

        self.assertTrue(result.success)
        self.assertGreaterEqual(len(fake_lark.updated_cards), 1)
        final_card = fake_lark.updated_cards[-1]["card_json"]
        self.assertIn("followup_prompt", final_card)
        self.assertIn("answer_from_report", final_card)
        self.assertIn("reanalyze", final_card)
        self.assertIn("continue_agent", final_card)
        self.assertIn("feedback_helpful", final_card)
        self.assertIn("feedback_unhelpful", final_card)
    def test_completed_progress_card_offers_alternate_agent_reanalysis_choices(self):
        class CodexBugRunner(FakeBugRunner):
            def run_bug_analysis(self, request, *, event=None, progress_callback=None):
                result = super().run_bug_analysis(request, event=event, progress_callback=progress_callback)
                result.details["agent_summary_provider"] = "codex"
                return result

        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    event_consumer=EventConsumerOptions(event_key="card.action.trigger"),
                ),
                lark_client=fake_lark,
                bug_runner=CodexBugRunner(metadata, html),
            )

            app.handle_event(event(content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/1 调查3D启动"))

        final_card = fake_lark.updated_cards[-1]["card_json"]
        self.assertIn("select_bug_agent", final_card)
        self.assertIn("换 Claude 重分析", final_card)
        self.assertIn("用 OMLX 本地模型", final_card)
        self.assertIn('"agent_provider":"claude"', final_card)
        self.assertIn('"agent_provider":"omlx"', final_card)
        self.assertNotIn('"agent_provider":"codex"', final_card)
    def test_select_bug_agent_sends_confirmation_card_and_confirm_runs_selected_agent(self):
        class JobAwareBugRunner(FakeBugRunner):
            def run_bug_analysis(self, request, *, event=None, progress_callback=None):
                result = super().run_bug_analysis(request, event=event, progress_callback=progress_callback)
                result.job_id = "job_bug_1"
                result.details["agent_summary_provider"] = "codex"
                return result

        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_bug = JobAwareBugRunner(metadata, html)
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    event_consumer=EventConsumerOptions(event_key="card.action.trigger"),
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
            )
            first = app.handle_event(
                event(
                    message_id="om_original_request",
                    content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/1 调查3D启动",
                )
            )

            selected = app.handle_card_action_payload(
                {
                    "header": {"event_id": "evt_select_agent"},
                    "event": {
                        "context": {
                            "open_message_id": "om_result_card",
                            "open_chat_id": "oc_denied",
                            "chat_type": "group",
                        },
                        "operator": {"operator_id": {"open_id": "ou_1"}},
                        "action": {
                            "value": {
                                "action": "select_bug_agent",
                                "job_id": "job_bug_1",
                                "root_message_id": first.details["conversation_root_message_id"],
                                "agent_provider": "claude",
                            },
                            "form_value": {"followup_prompt": "换 Claude 重点看源码证据"},
                        },
                    },
                }
            )
            confirmed = app.handle_card_action_payload(
                {
                    "header": {"event_id": "evt_confirm_agent"},
                    "event": {
                        "context": {
                            "open_message_id": "om_confirm_card",
                            "open_chat_id": "oc_denied",
                            "chat_type": "group",
                        },
                        "operator": {"operator_id": {"open_id": "ou_1"}},
                        "action": {
                            "value": {
                                "action": "confirm_bug_agent_reanalysis",
                                "job_id": "job_bug_1",
                                "root_message_id": first.details["conversation_root_message_id"],
                                "agent_provider": "claude",
                                "followup_text": "换 Claude 重点看源码证据",
                            }
                        },
                    },
                }
            )

        self.assertTrue(selected.success)
        self.assertTrue(any("confirm_bug_agent_reanalysis" in item["card_json"] for item in fake_lark.card_replies))
        self.assertTrue(confirmed.success)
        self.assertEqual(len(fake_bug.reanalysis_calls), 1)
        self.assertEqual(fake_bug.reanalysis_calls[0]["agent_provider_override"], "claude")
        self.assertEqual(fake_bug.reanalysis_calls[0]["followup_text"], "换 Claude 重点看源码证据")
    def test_p2p_bug_request_updates_progress_card_without_group_mention(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp)),
                lark_client=fake_lark,
                bug_runner=FakeBugRunner(metadata, html),
            )

            result = app.handle_event(
                event(
                    chat_id="ou_chat_1",
                    chat_type="p2p",
                    content="https://project.feishu.cn/xpfailuremgmt/buglo/detail/1 调查3D启动",
                )
            )

        self.assertTrue(result.success)
        self.assertGreaterEqual(len(fake_lark.card_replies), 1)
        self.assertGreaterEqual(len(fake_lark.updated_cards), 1)
        self.assertTrue(any("已完成" in item["card_json"] for item in fake_lark.updated_cards))
    def test_progress_card_update_failure_falls_back_to_text_reply(self):
        class FailingUpdateLarkClient(FakeLarkClient):
            def update_card(self, message_id, card_json):
                self.updated_cards.append({"message_id": message_id, "card_json": card_json})
                return CommandResult(command=["update-card"], returncode=1, stderr="update failed")

        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FailingUpdateLarkClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                bug_runner=FakeBugRunner(metadata, html),
            )

            result = app.handle_event(
                event(content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/1 调查3D启动")
            )

        self.assertTrue(result.success)
        self.assertGreaterEqual(len(fake_lark.card_replies), 1)
        self.assertGreaterEqual(len(fake_lark.updated_cards), 1)
        self.assertTrue(any(reply["message_id"] == "om_1" for reply in fake_lark.replies))
        self.assertTrue(any("bug 分析完成" in reply["text"] for reply in fake_lark.replies))
    def test_stream_progress_card_updates_are_throttled(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"]),
                lark_client=fake_lark,
            )
            app._progress_card_stream_update_interval_seconds = 60.0
            evt = event(message_id="om_stream_progress", content="@bot 继续分析")

            app.send_status_card(
                evt,
                title="Bug 重新分析",
                status="analyzing",
                session_id="om_stream_progress",
            )
            self.assertEqual(len(fake_lark.card_replies), 1)

            app._notify_progress(
                "source_stage_agent_analysis_stream",
                "Codex app-server: Codex delta A",
                event=evt,
                session_id="om_stream_progress",
                provider="codex",
            )
            app._notify_progress(
                "source_stage_agent_analysis_stream",
                "Codex app-server: Codex delta B",
                event=evt,
                session_id="om_stream_progress",
                provider="codex",
            )
            self.assertEqual(len(fake_lark.updated_cards), 1)

            app._notify_progress(
                "bug_reanalysis_run_analysis",
                "基于已准备日志重新执行源码分析阶段",
                event=evt,
                session_id="om_stream_progress",
            )
            self.assertEqual(len(fake_lark.updated_cards), 2)
    def test_progress_card_send_failure_falls_back_to_received_text(self):
        class FailingCardLarkClient(FakeLarkClient):
            def reply_card(self, message_id, card_json):
                self.card_replies.append({"message_id": message_id, "card_json": card_json})
                return CommandResult(command=["reply-card"], returncode=1, stderr="card failed")

        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FailingCardLarkClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                bug_runner=FakeBugRunner(metadata, html),
            )

            result = app.handle_event(
                event(content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/1 调查3D启动")
            )

        self.assertTrue(result.success)
        self.assertGreaterEqual(len(fake_lark.replies), 1)
        self.assertIn("已收到", fake_lark.replies[0]["text"])
        self.assertTrue(any("status_card_send_failed" == item["stage"] for item in app.activity_store.get_session("om_1")["progress"]))
    def test_failed_progress_card_update_failure_falls_back_to_text_reply(self):
        class FailingUpdateLarkClient(FakeLarkClient):
            def update_card(self, message_id, card_json):
                self.updated_cards.append({"message_id": message_id, "card_json": card_json})
                return CommandResult(command=["update-card"], returncode=1, stderr="update failed")

        class FailingBugRunner(FakeBugRunner):
            def run_bug_analysis(self, request, *, event=None, progress_callback=None):
                self.requests.append(request)
                self.progress_callbacks.append(progress_callback)
                return __import__("lark_agent_bridge.models", fromlist=["TaskResult"]).TaskResult(
                    success=False,
                    message="bug 分析失败",
                    error_code="bug_analysis_failed",
                    details={"mode": "bug_analysis"},
                )

        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FailingUpdateLarkClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                bug_runner=FailingBugRunner(metadata, html),
            )

            result = app.handle_event(
                event(content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/1 调查3D启动")
            )

        self.assertFalse(result.success)
        self.assertGreaterEqual(len(fake_lark.card_replies), 1)
        self.assertGreaterEqual(len(fake_lark.updated_cards), 1)
        self.assertTrue(any(reply["message_id"] == "om_1" for reply in fake_lark.replies))
        self.assertTrue(any("bug 分析失败" in reply["text"] for reply in fake_lark.replies))
    def test_non_analysis_request_outside_allowed_chat_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_allowed"],
                ),
                lark_client=fake_lark,
            )

            result = app.handle_event(event(content="@bot /chat 讲个笑话"))

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "chat_not_allowed")
        self.assertEqual(len(fake_lark.sent), 1)
        self.assertIn("当前群未加入允许列表", fake_lark.sent[0]["text"])
    def test_group_chat_is_allowed_by_default_without_chat_allowlist(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_chat = FakeOmlxChatClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=[],
                ),
                lark_client=FakeLarkClient(),
                chat_client=fake_chat,
            )

            result = app.handle_event(event(chat_id="oc_any_group", content="@bot /chat 讲个笑话"))

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "omlx_chat")
        self.assertEqual(fake_chat.prompts, ["讲个笑话"])
    def test_bug_request_in_non_allowlisted_group_is_allowed_when_addressed(self):
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
                    allowed_chats=["oc_allowed"],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
            )

            result = app.handle_event(
                event(
                    chat_id="oc_denied",
                    content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查3D启动时序",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "bug_analysis")
        self.assertEqual(len(fake_bug.requests), 1)
        self.assertEqual(_last_reply_message_id(fake_lark), "om_1")
    def test_unsupported_request_still_sends_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    lark=LarkOptions(bot_name="bot"),
                ),
                lark_client=fake_lark,
            )

            result = app.handle_event(event(content="@bot 这条消息当前没有实现对应能力"))

        self.assertTrue(result.success)
        self.assertTrue(result.skipped)
        self.assertEqual(result.message, "not a handled request")
        self.assertEqual(len(fake_lark.sent), 1)
        self.assertEqual(fake_lark.sent[0]["text"], "not a handled request")
    def test_intent_unsupported_reanalysis_without_context_prompts_for_reply(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_intent = FakeIntentRunner(
                {
                    "重新分析": IntentDecision(
                        route="unsupported",
                        reason="没有上下文",
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
                    allowed_chats=[],
                ),
                lark_client=fake_lark,
                intent_runner=fake_intent,
            )

            result = app.handle_event(event(content="@bot 重新分析"))

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "missing_followup_reply")
        self.assertIn("回复对应那条分析消息", result.message)
        self.assertEqual(fake_intent.calls, [])
        self.assertEqual(fake_lark.card_replies, [])
        self.assertEqual(fake_lark.updated_cards, [])
        self.assertTrue(any("回复对应那条分析消息" in item["text"] for item in fake_lark.sent))
    def test_reply_to_failed_bug_link_session_uses_activity_context_before_intent(self):
        class FailingIntentRunner(FakeIntentRunner):
            def classify(self, **kwargs):
                raise AssertionError("reply follow-up with recovered bug context should bypass intent classification")

        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_lark.fetched_messages["om_followup"] = json.dumps(
                {
                    "data": {
                        "messages": [
                            {
                                "message_id": "om_followup",
                                "reply_to": "om_failed_original",
                            }
                        ]
                    }
                }
            )
            fake_bug = FakeBugRunner(metadata, html)
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=[],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                intent_runner=FailingIntentRunner(enabled=True),
            )
            original_event = event(
                event_id="evt_failed_original",
                message_id="om_failed_original",
                content=(
                    "@bot [ [缺陷] 【F01】车机大屏页面卡住-SB174577]"
                    "(https://project.feishu.cn/xpfailuremgmt/buglo/detail/6979499593) 分析3D生命周期"
                ),
            )
            app.activity_store.record_event(original_event)
            failed_result = __import__("lark_agent_bridge.models", fromlist=["TaskResult"]).TaskResult(
                success=False,
                message="Claude Code skill 分析失败",
                job_id="job_failed_original",
                job_dir=Path(tmp) / "jobs" / "job_failed_original",
                error_code="claude_failed",
                details={"mode": "claude_skill"},
            )
            app.activity_store.record_result(original_event, failed_result)

            result = app.handle_event(
                event(
                    event_id="evt_followup_reanalysis",
                    message_id="om_followup",
                    content="@bot 重新分析",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "bug_reanalysis")
        self.assertEqual(len(fake_bug.reanalysis_calls), 1)
        call = fake_bug.reanalysis_calls[0]
        self.assertEqual(call["previous_context"].root_message_id, "om_failed_original")
        self.assertIn("分析3D生命周期", call["previous_context"].request_text)
        self.assertEqual(
            call["previous_session"]["details"]["bug_url"],
            "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6979499593",
        )
    def test_reply_to_fetched_bug_link_message_without_local_session_starts_fresh_bug_analysis(self):
        class FailingIntentRunner(FakeIntentRunner):
            def classify(self, **kwargs):
                raise AssertionError("reply follow-up with fetched bug context should bypass intent classification")

        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            original_message = {
                "message_id": "om_original_from_lark",
                "chat_id": "oc_denied",
                "content": (
                    "@bot [ [缺陷] 【F01】车机大屏页面卡住-SB174577]"
                    "(https://project.feishu.cn/xpfailuremgmt/buglo/detail/6979499593) 分析3D生命周期"
                ),
            }
            fake_lark.fetched_messages["om_followup"] = json.dumps(
                {"data": {"messages": [{"message_id": "om_followup", "reply_to": "om_original_from_lark"}]}}
            )
            fake_lark.fetched_messages["om_original_from_lark"] = json.dumps(
                {"data": {"messages": [original_message]}}
            )
            fake_bug = FakeBugRunner(metadata, html)
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=[],
                ),
                lark_client=fake_lark,
                bug_runner=fake_bug,
                intent_runner=FailingIntentRunner(enabled=True),
            )

            result = app.handle_event(
                event(
                    event_id="evt_followup_fetched_reanalysis",
                    message_id="om_followup",
                    content="@bot 重新分析",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "bug_analysis")
        self.assertEqual(len(fake_bug.requests), 1)
        self.assertEqual(
            fake_bug.requests[0].bug_url,
            "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6979499593",
        )
        self.assertIn("分析3D生命周期", fake_bug.requests[0].prompt)
        self.assertIn("重新分析", fake_bug.requests[0].prompt)
    def test_claude_skill_request_is_unsupported_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_claude = FakeClaudeRunner(Path(tmp) / "skill-result.md")
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                ),
                lark_client=fake_lark,
                claude_runner=fake_claude,
            )

            result = app.handle_event(event(content="@bot /skill 分析下这个 skill 场景"))

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "unsupported")
        self.assertEqual(fake_claude.requests, [])
    def test_claude_skill_request_sends_text_and_result_file_when_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp) / "skill-result.md"
            artifact.write_text("结论", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_claude = FakeClaudeRunner(artifact)
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    claude_agent=ClaudeAgentOptions(trigger_prefixes=["/skill"]),
                ),
                lark_client=fake_lark,
                claude_runner=fake_claude,
            )

            result = app.handle_event(event(content="@bot /skill 分析下这个 skill 场景"))

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "claude_skill")
        self.assertEqual(fake_claude.requests[0].prompt, "分析下这个 skill 场景")
        self.assertEqual(len(fake_lark.sent), 1)
        self.assertEqual(len(fake_lark.files), 1)
        self.assertEqual(fake_lark.files[0]["path"], artifact)
    def test_claude_skill_request_accepts_bot_name_with_spaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp) / "skill-result.md"
            artifact.write_text("结论", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_claude = FakeClaudeRunner(artifact)
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    lark=LarkOptions(bot_name="Test Bot"),
                    claude_agent=ClaudeAgentOptions(trigger_prefixes=["/skill"]),
                ),
                lark_client=fake_lark,
                claude_runner=fake_claude,
            )

            result = app.handle_event(event(content="@Test Bot /skill 分析下这个 skill 场景"))

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "claude_skill")
        self.assertEqual(fake_claude.requests[0].prompt, "分析下这个 skill 场景")
        self.assertEqual(len(fake_lark.sent), 1)
        self.assertEqual(len(fake_lark.files), 1)
    def test_claude_skill_request_accepts_prefix_without_slash(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp) / "skill-result.md"
            artifact.write_text("结论", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_claude = FakeClaudeRunner(artifact)
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    claude_agent=ClaudeAgentOptions(trigger_prefixes=["skill"]),
                ),
                lark_client=fake_lark,
                claude_runner=fake_claude,
            )

            result = app.handle_event(event(content="@bot skill 分析下这个 skill 场景"))

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "claude_skill")
        self.assertEqual(fake_claude.requests[0].prompt, "分析下这个 skill 场景")
    def test_bug_request_replies_with_published_link(self):
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
                    content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查3D启动时序"
                )
            )
            session = app.activity_store.get_session("om_1")

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "bug_analysis")
        self.assertEqual(fake_bug.requests[0].bug_url, "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722")
        self.assertEqual(fake_bug.requests[0].prompt, "调查3D启动时序")
        self.assertIn("published_report_url", result.details)
        # Card or text reply should have been sent
        total_replies = len(fake_lark.replies) + len(fake_lark.card_replies)
        self.assertGreaterEqual(total_replies, 1)
        self.assertEqual(len(fake_lark.files), 1)
        self.assertEqual(Path(fake_lark.files[0]["path"]).resolve(), html.resolve())
        # If text reply was sent, check content; if card was sent, check card data
        if fake_lark.replies:
            self.assertTrue(fake_lark.replies[0]["text"].startswith('<at user_id="ou_1"></at> '))
            self.assertIn("报告链接：", fake_lark.replies[0]["text"])
        else:
            import json
            card_data = json.loads(fake_lark.card_replies[0]["card_json"])
            self.assertIn("header", card_data)
        self.assertIsNotNone(session)
        assert session is not None
        self.assertEqual(session["mode"], "bug_analysis")
        self.assertEqual(session["status"], "succeeded")
        self.assertEqual(session["report_url"], result.details["published_report_url"])
        self.assertTrue(any(item["stage"] == "bug_fetch_data" for item in session["progress"]))
