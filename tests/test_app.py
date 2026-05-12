from pathlib import Path
import tempfile
import unittest

from lark_agent_bridge.app import BridgeApp
from lark_agent_bridge.models import BridgeConfig, LarkEvent, LarkOptions


class FakeLarkClient:
    def __init__(self):
        self.sent = []
        self.files = []

    def send_response(self, event, text, *, markdown=False):
        self.sent.append({"event": event, "text": text, "markdown": markdown})
        return None

    def send_file_response(self, event, path):
        self.files.append({"event": event, "path": path})
        return None

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

    def reply(self, prompt):
        self.prompts.append(prompt)
        return __import__("lark_agent_bridge.models", fromlist=["TaskResult"]).TaskResult(
            success=True,
            message="omlx 模型回复",
            details={"mode": "omlx_chat"},
        )


class FakeBugRunner:
    def __init__(self, metadata_path: Path, html_path: Path):
        self.metadata_path = metadata_path
        self.html_path = html_path
        self.requests = []

    def run_bug_analysis(self, request, *, event=None):
        self.requests.append(request)
        return __import__("lark_agent_bridge.models", fromlist=["TaskResult"]).TaskResult(
            success=True,
            message="bug 分析完成",
            details={"mode": "bug_analysis", "files_to_send": [self.metadata_path, self.html_path]},
        )

    def run_direct_analysis(self, request, *, event=None):
        self.requests.append(request)
        return __import__("lark_agent_bridge.models", fromlist=["TaskResult"]).TaskResult(
            success=True,
            message="直传文件分析完成",
            details={"mode": "direct_analysis", "files_to_send": [self.metadata_path, self.html_path]},
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
    def test_policy_rejection_still_sends_message(self):
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

            result = app.handle_event(event(content="@bot /signal 132002 https://example.com/log.zip"))

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "chat_not_allowed")
        self.assertEqual(len(fake_lark.sent), 1)
        self.assertIn("当前群未加入允许列表", fake_lark.sent[0]["text"])

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

    def test_bug_request_sends_two_result_files(self):
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

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "bug_analysis")
        self.assertEqual(fake_bug.requests[0].bug_url, "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722")
        self.assertEqual(fake_bug.requests[0].prompt, "调查3D启动时序")
        self.assertEqual(len(fake_lark.sent), 1)
        self.assertEqual(len(fake_lark.files), 2)

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
        self.assertEqual(len(fake_lark.sent), 1)
        self.assertEqual(len(fake_lark.files), 1)

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
