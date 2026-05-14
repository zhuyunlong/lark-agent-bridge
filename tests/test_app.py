from pathlib import Path
import tempfile
import unittest

from lark_agent_bridge.app import BridgeApp
from lark_agent_bridge.lark_client import CommandResult
from lark_agent_bridge.models import BridgeConfig, IntentDecision, LarkEvent, LarkOptions


class FakeLarkClient:
    def __init__(self):
        self.sent = []
        self.replies = []
        self.files = []
        self.fetched_messages = {}

    def send_response(self, event, text, *, markdown=False):
        self.sent.append({"event": event, "text": text, "markdown": markdown})
        return CommandResult(command=["send"], returncode=0)

    def reply(self, message_id, text, *, markdown=False):
        self.replies.append({"message_id": message_id, "text": text, "markdown": markdown})
        return CommandResult(command=["reply"], returncode=0)

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
        self.assertEqual(fake_lark.replies[-1]["message_id"], "om_1")

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
        self.assertEqual(len(fake_lark.replies), 1)
        self.assertEqual(len(fake_lark.files), 1)
        self.assertEqual(Path(fake_lark.files[0]["path"]).resolve(), html.resolve())
        self.assertTrue(fake_lark.replies[0]["text"].startswith('<at user_id="ou_1"></at> '))
        self.assertIn("报告链接：", fake_lark.replies[0]["text"])
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
        self.assertEqual(len(fake_lark.replies), 1)
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
        self.assertEqual(len(fake_lark.replies), 1)
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
        self.assertEqual(fake_lark.replies[-1]["message_id"], "om_followup")
        self.assertTrue(fake_lark.replies[-1]["text"].startswith('<at user_id="ou_1"></at> '))

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
        self.assertEqual(fake_lark.replies[-1]["message_id"], "om_followup_chain")

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
        self.assertEqual(fake_lark.replies[-1]["message_id"], "om_followup_p2p")
        self.assertFalse(fake_lark.replies[-1]["text"].startswith("<at "))

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
        self.assertEqual(fake_lark.replies[-1]["message_id"], "om_followup_logd")

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
        self.assertEqual(len(fake_intent.calls), 2)
        self.assertIsNotNone(fake_intent.calls[-1]["explicit_followup_context"])

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
