from pathlib import Path
import json
import os
import tempfile
import time
import unittest

from lark_agent_bridge.app import BridgeApp
from lark_agent_bridge.agents import BugFollowupSelection
from lark_agent_bridge.lark_client import CommandResult
from lark_agent_bridge.models import (
    ApprovalOptions,
    BridgeConfig as RealBridgeConfig,
    DualAgentOptions,
    IntentDecision,
    LarkEvent,
    LarkOptions,
    NotificationOptions,
    WorkflowArchiveOptions,
)


def BridgeConfig(*args, **kwargs):
    kwargs.setdefault("approval", ApprovalOptions(enabled=False))
    return RealBridgeConfig(*args, **kwargs)


def _all_reply_message_ids(fake_lark):
    """Collect message IDs from both text replies and card replies."""
    ids = [r["message_id"] for r in fake_lark.replies]
    ids += [r["message_id"] for r in fake_lark.card_replies]
    return ids


def _last_reply_message_id(fake_lark):
    """Get the last replied message ID (text or card)."""
    all_ids = _all_reply_message_ids(fake_lark)
    return all_ids[-1] if all_ids else None


class FakeLarkClient:
    def __init__(self):
        self.sent = []
        self.replies = []
        self.files = []
        self.cards = []
        self.card_replies = []
        self.updated_cards = []
        self.fetched_messages = {}

    def send_response(self, event, text, *, markdown=False):
        self.sent.append({"event": event, "text": text, "markdown": markdown})
        return CommandResult(command=["send"], returncode=0)

    def reply(self, message_id, text, *, markdown=False):
        self.replies.append({"message_id": message_id, "text": text, "markdown": markdown})
        return CommandResult(command=["reply"], returncode=0)

    def reply_card(self, message_id, card_json):
        card_message_id = f"om_card_{len(self.card_replies) + 1}"
        self.card_replies.append({"message_id": message_id, "card_message_id": card_message_id, "card_json": card_json})
        return CommandResult(
            command=["reply-card"],
            returncode=0,
            stdout=f'{{"data":{{"message_id":"{card_message_id}"}}}}',
        )

    def send_card_response(self, event, card_json):
        self.cards.append({"event": event, "card_json": card_json})
        return CommandResult(command=["send-card"], returncode=0)

    def update_card(self, message_id, card_json):
        self.updated_cards.append({"message_id": message_id, "card_json": card_json})
        return CommandResult(command=["update-card"], returncode=0)

    def send_file_response(self, event, path):
        self.files.append({"event": event, "path": path})
        return CommandResult(command=["send-file"], returncode=0)

    def fetch_message(self, message_id):
        payload = self.fetched_messages.get(message_id)
        if payload is None:
            return CommandResult(command=["fetch"], returncode=1, stderr="not found")
        return CommandResult(command=["fetch"], returncode=0, stdout=payload)

    def check_environment(self):
        return {}

    def download_resource(self, **kwargs):
        raise AssertionError("download_resource should not be called in these tests")


class FakeClaudeRunner:
    def __init__(self, artifact_path: Path):
        self.artifact_path = artifact_path
        self.requests = []

    def run_skill_analysis(self, request, *, event=None):
        self.requests.append(request)
        return __import__("lark_agent_bridge.models", fromlist=["TaskResult"]).TaskResult(
            success=True,
            message="Claude Code skill 分析完成\n摘录:\n结论",
            details={"mode": "claude_skill", "files_to_send": [self.artifact_path]},
        )


class FakeOmlxChatClient:
    def __init__(self):
        self.prompts = []
        self.context_calls = []

    def reply(self, prompt):
        self.prompts.append(prompt)
        return __import__("lark_agent_bridge.models", fromlist=["TaskResult"]).TaskResult(
            success=True,
            message="omlx 模型回复",
            details={"mode": "omlx_chat"},
        )

    def reply_with_context(self, question, **kwargs):
        self.context_calls.append({"question": question, **kwargs})
        return __import__("lark_agent_bridge.models", fromlist=["TaskResult"]).TaskResult(
            success=True,
            message="基于上下文的回复",
            details={"mode": "analysis_followup"},
        )


class FakeIntentRunner:
    def __init__(self, decisions=None, *, enabled=True):
        self.decisions = decisions or {}
        self.enabled = enabled
        self.calls = []

    def is_enabled(self):
        return self.enabled

    def classify(self, **kwargs):
        self.calls.append(kwargs)
        route_content = kwargs["route_content"]
        decision = self.decisions.get(route_content)
        if callable(decision):
            decision = decision(**kwargs)
        if decision is None:
            decision = IntentDecision(route="chat", reason="default test route", confidence="high")
        return decision


class FakeBugRunner:
    def __init__(self, metadata_path: Path, html_path: Path):
        self.metadata_path = metadata_path
        self.html_path = html_path
        self.requests = []
        self.reanalysis_calls = []
        self.agent_followup_calls = []
        self.progress_callbacks = []

    def run_bug_analysis(self, request, *, event=None, progress_callback=None):
        self.requests.append(request)
        self.progress_callbacks.append(progress_callback)
        if progress_callback is not None:
            progress_callback({"stage": "bug_fetch_data", "message": "拉取 bug 详情", "details": {"bug_url": request.bug_url}})
        return __import__("lark_agent_bridge.models", fromlist=["TaskResult"]).TaskResult(
            success=True,
            message="bug 分析完成",
            details={"mode": "bug_analysis", "files_to_send": [self.metadata_path, self.html_path]},
        )

    def run_direct_analysis(self, request, *, event=None, progress_callback=None):
        self.requests.append(request)
        self.progress_callbacks.append(progress_callback)
        return __import__("lark_agent_bridge.models", fromlist=["TaskResult"]).TaskResult(
            success=True,
            message="直传文件分析完成",
            details={"mode": "direct_analysis", "files_to_send": [self.metadata_path, self.html_path]},
        )

    def run_bug_reanalysis(self, **kwargs):
        self.reanalysis_calls.append(kwargs)
        progress_callback = kwargs.get("progress_callback")
        if progress_callback is not None:
            progress_callback({"stage": "bug_reanalysis_reuse_context", "message": "复用上下文"})
            progress_callback(
                {
                    "stage": "bug_agent_summary",
                    "message": "继续调用本地 Agent",
                    "details": {"session_id": "sess_123", "provider": "codex"},
                }
            )
        return __import__("lark_agent_bridge.models", fromlist=["TaskResult"]).TaskResult(
            success=True,
            message="bug 续聊重分析完成",
            job_id="job_reused",
            job_dir=self.html_path.parent.parent,
            details={"mode": "bug_reanalysis", "files_to_send": [self.html_path]},
        )

    def run_bug_agent_followup(self, **kwargs):
        self.agent_followup_calls.append(kwargs)
        progress_callback = kwargs.get("progress_callback")
        if progress_callback is not None:
            progress_callback(
                {
                    "stage": "bug_agent_followup_prepare",
                    "message": "继续调用本地 Agent",
                    "details": {"session_id": "sess_123", "provider": "codex"},
                }
            )
        return __import__("lark_agent_bridge.models", fromlist=["TaskResult"]).TaskResult(
            success=True,
            message="bug 智能体续聊完成",
            job_id="job_reused",
            job_dir=self.html_path.parent.parent,
            details={
                "mode": "bug_agent_followup",
                "agent_summary_session_id": "sess_123",
                "agent_summary_provider": "codex",
                "agent_summary_resumed": True,
            },
        )


class FakePerceptionRunner:
    def __init__(self, html_path: Path):
        self.html_path = html_path
        self.requests = []

    def run_summary(self, request, *, event=None):
        self.requests.append(request)
        return __import__("lark_agent_bridge.models", fromlist=["TaskResult"]).TaskResult(
            success=True,
            message="感知数据总结完成",
            details={"mode": "perception_summary", "files_to_send": [self.html_path]},
        )


def event(**overrides):
    values = {
        "event_id": "evt_1",
        "message_id": "om_1",
        "chat_id": "oc_denied",
        "chat_type": "group",
        "sender_id": "ou_1",
        "message_type": "text",
        "content": "/signal 132002 https://example.com/log.zip",
    }
    values.update(overrides)
    return LarkEvent(**values)


class AppTests(unittest.TestCase):
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

    def test_card_reanalysis_rejects_missing_chat_id(self):
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
        self.assertEqual(result.error_code, "invalid_card_action_context")

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
                            }
                        },
                    },
                }
            )

        self.assertTrue(first.success)
        self.assertTrue(result.success)
        self.assertEqual(len(fake_bug.reanalysis_calls), 1)
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
        self.assertTrue(any("已完成" in item["card_json"] for item in fake_lark.updated_cards))

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
                ),
                lark_client=fake_lark,
            )

            result = app.handle_event(event(content="@bot 这条消息当前没有实现对应能力"))

        self.assertTrue(result.success)
        self.assertTrue(result.skipped)
        self.assertEqual(result.message, "not a handled request")
        self.assertEqual(len(fake_lark.sent), 1)
        self.assertEqual(fake_lark.sent[0]["text"], "not a handled request")

    def test_claude_skill_request_sends_text_and_result_file(self):
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

    def test_simple_question_uses_omlx_chat(self):
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
        self.assertEqual(len(fake_lark.sent), 1)

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
        self.assertEqual(len(fake_intent.calls), 1)
        self.assertEqual(fake_intent.calls[0]["route_content"], "帮我解释一下什么是 token？")

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
        self.assertEqual(len(fake_intent.calls), 1)

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

            result = app.handle_event(event(content="@bot /perception-summary 总结当前感知数据"))

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "perception_summary")
        self.assertEqual(fake_runner.requests[0].prompt, "总结当前感知数据")
        total_replies = len(fake_lark.replies) + len(fake_lark.card_replies)
        self.assertEqual(total_replies, 1)
        self.assertEqual(len(fake_lark.files), 1)
        self.assertEqual(Path(fake_lark.files[0]["path"]).resolve(), html.resolve())
        self.assertIn("published_report_url", result.details)

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
                    content="这个结论的根因是什么 @bot",
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
        self.assertIn("基于当前报告回答", rendered)
        self.assertIn("基于已有日志重新分析", rendered)
        self.assertIn("继续原 Agent", rendered)
        self.assertIn("有用", rendered)
        self.assertIn("不准", rendered)
        self.assertIn("http://127.0.0.1:8765/reports/om_bug_root/", rendered)

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

    def test_bug_followup_sends_ack_before_expensive_reanalysis(self):
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
        self.assertTrue(fake_lark.replies)
        self.assertIn("已收到", fake_lark.replies[0]["text"])
        self.assertEqual(fake_lark.replies[0]["message_id"], "om_reanalysis_followup")

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

    def test_group_message_without_bot_mention_is_silent(self):
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
        self.assertEqual(fake_lark.sent[0]["text"], "omlx 模型回复")

    def test_group_chat_command_requires_mention(self):
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
            )

            result = app.handle_event(event(content="/chat 讲个笑话"))

        self.assertTrue(result.success)
        self.assertTrue(result.skipped)
        self.assertEqual(result.details["mode"], "not_addressed")
        self.assertEqual(fake_chat.prompts, [])

    def test_group_mentioned_chat_command_uses_omlx(self):
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
            )

            result = app.handle_event(event(content="@bot /chat 讲个笑话"))

        self.assertTrue(result.success)
        self.assertFalse(result.skipped)
        self.assertEqual(result.details["mode"], "omlx_chat")
        self.assertEqual(fake_chat.prompts, ["讲个笑话"])
        self.assertEqual(len(fake_lark.sent), 1)

    def test_group_mentioned_chat_command_accepts_prefix_without_slash(self):
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
                ),
                lark_client=fake_lark,
                chat_client=fake_chat,
            )

            result = app.handle_event(event(content="@Test Bot /chat 讲个笑话"))

        self.assertTrue(result.success)
        self.assertFalse(result.skipped)
        self.assertEqual(result.details["mode"], "omlx_chat")
        self.assertEqual(fake_chat.prompts, ["讲个笑话"])

    def test_intent_routing_sends_ack_card_before_classification(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "bug_metadata.md"
            html = Path(tmp) / "bug_report.html"
            metadata.write_text("bug", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_lark = FakeLarkClient()
            fake_chat = FakeOmlxChatClient()

            def classify_after_ack(**kwargs):
                self.assertGreaterEqual(len(fake_lark.card_replies), 1)
                self.assertIn("意图分析", fake_lark.card_replies[0]["card_json"])
                return IntentDecision(
                    route="chat",
                    reason="普通聊天提问",
                    confidence="high",
                    followup_action="none",
                    context_source="none",
                )

            fake_intent = FakeIntentRunner(
                {
                    "帮我解释一下 token": classify_after_ack,
                }
            )
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
                intent_runner=fake_intent,
            )

            result = app.handle_event(
                event(
                    event_id="evt_ack_before_intent",
                    content="@Test Bot 帮我解释一下 token",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "omlx_chat")
        self.assertEqual(len(fake_lark.card_replies), 1)
        self.assertEqual(len(fake_intent.calls), 1)

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

    def test_intent_chat_updates_ack_card_with_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_chat = FakeOmlxChatClient()
            fake_intent = FakeIntentRunner(
                {
                    "帮我解释一下 token": IntentDecision(
                        route="chat",
                        reason="普通聊天",
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

            result = app.handle_event(event(content="@bot 帮我解释一下 token"))

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "omlx_chat")
        self.assertEqual(len(fake_lark.card_replies), 1)
        self.assertEqual(fake_lark.replies, [])
        self.assertTrue(any("omlx 模型回复" in item["card_json"] for item in fake_lark.updated_cards))

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

    def test_group_chat_command_uses_configured_bot_mention_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_chat = FakeOmlxChatClient()
            app = BridgeApp(
                BridgeConfig(
                    dry_run=False,
                    data_dir=Path(tmp),
                    allowed_chats=["oc_denied"],
                    lark=LarkOptions(bot_open_id="ou_bot"),
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


if __name__ == "__main__":
    unittest.main()
