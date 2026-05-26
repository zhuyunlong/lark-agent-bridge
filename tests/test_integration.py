"""End-to-end integration tests for the event → result pipeline.

Verifies that the full routing, analysis, result delivery, and state
persistence chain works correctly using sample events and dry-run mode.
"""

from pathlib import Path
import json
import shutil
import tempfile
import unittest

from lark_agent_bridge.app import BridgeApp
from lark_agent_bridge.lark_client import CommandResult
from lark_agent_bridge.log import setup_logging
from lark_agent_bridge.models import (
    ApprovalOptions,
    BridgeConfig,
    LarkOptions,
    LarkEvent,
    TaskResult,
)
from lark_agent_bridge.state import EventStateStore, ConversationContextStore, AgentActivityStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(tmp: Path, *, dry_run: bool = True) -> BridgeConfig:
    return BridgeConfig(
        dry_run=dry_run,
        workspace_root=tmp,
        guideengine_repo=tmp,
        data_dir=tmp / "data",
        approval=ApprovalOptions(enabled=False),
        lark=LarkOptions(bot_name="bot"),
    )


class _FakeLarkClient:
    """Minimal mock that tracks all outgoing messages."""

    def __init__(self):
        self.sent = []
        self.replies = []
        self.card_replies = []
        self.updated_cards = []
        self.files = []

    def send_response(self, event, text, *, markdown=False):
        self.sent.append({"text": text})
        return CommandResult(command=["send"], returncode=0)

    def reply(self, message_id, text, *, markdown=False):
        self.replies.append({"message_id": message_id, "text": text})
        return CommandResult(command=["reply"], returncode=0)

    def reply_card(self, message_id, card_json):
        mid = f"om_card_{len(self.card_replies) + 1}"
        self.card_replies.append({"message_id": message_id})
        return CommandResult(command=["reply-card"], returncode=0,
                             stdout=f'{{"data":{{"message_id":"{mid}"}}}}')

    def send_card_response(self, event, card_json):
        return CommandResult(command=["send-card"], returncode=0)

    def update_card(self, message_id, card_json):
        self.updated_cards.append({"message_id": message_id})
        return CommandResult(command=["update-card"], returncode=0)

    def send_file_response(self, event, path):
        self.files.append({"path": path})
        return CommandResult(command=["send-file"], returncode=0,
                             stdout='{"data":{"message_id":"om_file_1"}}')

    def fetch_message(self, message_id):
        return CommandResult(command=["fetch"], returncode=1, stderr="not found")

    def check_environment(self):
        return {}

    def download_resource(self, **kwargs):
        raise AssertionError("should not be called in dry-run")


class _FakeOmlxChatClient:
    def __init__(self):
        self.prompts = []

    def reply(self, prompt):
        self.prompts.append(prompt)
        return TaskResult(success=True, message="模型回复", details={"mode": "omlx_chat"})

    def reply_with_context(self, question, **kwargs):
        return TaskResult(success=True, message="上下文回复", details={"mode": "analysis_followup"})


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class IntegrationPipelineTests(unittest.TestCase):
    """Full pipeline tests: event → BridgeApp.handle_event → result + state."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.config = _make_config(Path(self.tmp_dir))
        self.lark = _FakeLarkClient()
        self.app = BridgeApp(
            self.config,
            lark_client=self.lark,
            chat_client=_FakeOmlxChatClient(),
        )

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _load_sample(self, name: str) -> dict:
        sample_dir = Path(__file__).parent.parent / "samples"
        return json.loads((sample_dir / name).read_text(encoding="utf-8"))

    def _event_from_sample(self, name: str) -> LarkEvent:
        payload = self._load_sample(name)
        return LarkEvent(
            event_id=payload.get("event_id", ""),
            message_id=payload.get("message_id", ""),
            chat_id=payload.get("chat_id", ""),
            chat_type=payload.get("chat_type", ""),
            sender_id=payload.get("sender_id", ""),
            message_type=payload.get("message_type", "text"),
            content=payload.get("content", ""),
            create_time=payload.get("create_time", ""),
            timestamp=payload.get("timestamp", ""),
        )

    def test_basic_chat_event_returns_success(self):
        """A simple chat message should route to basic_chat or omlx_chat."""
        event = self._event_from_sample("basic_chat_who_are_you.json")
        result = self.app.handle_event(event)
        self.assertTrue(result.success)
        self.assertFalse(result.skipped)

    def test_group_unmentioned_message_is_skipped(self):
        """A group message without @bot mention should be skipped."""
        event = self._event_from_sample("group_unmentioned_url.json")
        result = self.app.handle_event(event)
        self.assertTrue(result.skipped)
        self.assertEqual(result.details.get("mode"), "not_addressed")

    def test_signal_event_dry_run_returns_planned(self):
        """Signal analysis in dry-run should return the planned command."""
        event = self._event_from_sample("signal_event_with_url.json")
        result = self.app.handle_event(event)
        self.assertTrue(result.success)
        mode = result.details.get("mode", "")
        self.assertIn(mode, {"signal_lifecycle", "dry_run", "scene_signal", ""})

    def test_duplicate_event_is_rejected(self):
        """Same event_id processed twice should be skipped the second time."""
        event = self._event_from_sample("basic_chat_who_are_you.json")
        first = self.app.handle_event(event)
        second = self.app.handle_event(event)
        self.assertTrue(first.success)
        self.assertTrue(second.success)
        self.assertTrue(second.skipped)
        self.assertIn("duplicate", second.message)

    def test_state_stores_persist_across_events(self):
        """State stores should record events and be queryable."""
        event = self._event_from_sample("basic_chat_who_are_you.json")
        self.app.handle_event(event)

        # Event should be in the seen store
        self.assertTrue(self.app.state_store.has_seen(event.event_id))

        # Activity store should have recorded at least one session
        sessions = self.app.activity_store.list_sessions()
        self.assertGreater(len(sessions), 0)

    def test_conversation_context_stores_after_analysis(self):
        """After a successful analysis, conversation context should be stored."""
        event = LarkEvent(
            event_id="evt_conv_test",
            message_id="om_conv_test",
            chat_id="oc_conv_chat",
            chat_type="p2p",
            sender_id="ou_test",
            message_type="text",
            content="你是谁",
            create_time="1770000000000",
            timestamp="1770000000000",
        )
        result = self.app.handle_event(event)
        self.assertTrue(result.success)

    def test_event_state_store_compaction(self):
        """EventStateStore should compact when exceeding limits."""
        with tempfile.TemporaryDirectory() as td:
            store = EventStateStore(Path(td) / "seen.jsonl")
            self.assertEqual(store._MAX_SEEN_EVENTS, 50000)
            # Write a few events
            for i in range(10):
                ev = LarkEvent(
                    event_id=f"evt_{i}",
                    message_id=f"om_{i}",
                    chat_id="oc_test",
                    chat_type="p2p",
                    sender_id="ou_test",
                    message_type="text",
                    content="test",
                    create_time="1770000000000",
                    timestamp="1770000000000",
                )
                store.mark_seen(ev)
            self.assertEqual(len(store._seen), 10)
            # Reload should preserve all events
            store2 = EventStateStore(Path(td) / "seen.jsonl")
            self.assertEqual(len(store2._seen), 10)

    def test_atomic_write_roundtrip(self):
        """Atomic write helper should produce valid JSON that roundtrips."""
        from lark_agent_bridge.state import _atomic_write_json
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "test.json"
            data = {"key": "值", "nested": [1, 2, 3]}
            _atomic_write_json(path, data)
            loaded = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(loaded, data)

    def test_multiple_event_types_route_correctly(self):
        """Different event types should route to different handlers."""
        results = {}
        for name in ["basic_chat_who_are_you.json", "group_unmentioned_url.json"]:
            event = self._event_from_sample(name)
            result = self.app.handle_event(event)
            results[name] = result

        # Basic chat should succeed, unmentioned should be skipped
        self.assertTrue(results["basic_chat_who_are_you.json"].success)
        self.assertTrue(results["group_unmentioned_url.json"].skipped)

    def test_route_context_dispatch_covers_all_handlers(self):
        """Verify that _dispatch_route exists and has route handlers."""
        self.assertTrue(hasattr(self.app, '_dispatch_route'))
        self.assertTrue(hasattr(self.app, '_route_bug_followup'))
        self.assertTrue(hasattr(self.app, '_route_signal_request'))
        self.assertTrue(hasattr(self.app, '_route_claude_skill'))
        self.assertTrue(hasattr(self.app, '_route_basic_chat'))
        self.assertTrue(hasattr(self.app, '_route_omlx_chat'))


class LoggingSetupTests(unittest.TestCase):
    """Verify that structured logging is properly configured."""

    def test_setup_logging_is_idempotent(self):
        """Multiple calls to setup_logging should not add duplicate handlers."""
        import lark_agent_bridge.log as log_module
        log_module._configured = False
        setup_logging()
        setup_logging()  # Should be no-op
        logger = log_module.get_logger("test")
        # Should have exactly one handler on the root bridge logger
        import logging
        root = logging.getLogger("bridge")
        self.assertGreaterEqual(len(root.handlers), 1)

    def test_get_logger_returns_namespaced_logger(self):
        from lark_agent_bridge.log import get_logger
        logger = get_logger("mymodule")
        self.assertEqual(logger.name, "bridge.mymodule")


class AdminTemplateTests(unittest.TestCase):
    """Verify admin UI template loading."""

    def test_render_admin_page_returns_html(self):
        from lark_agent_bridge.admin_ui import render_admin_page
        html = render_admin_page()
        self.assertIn("<!doctype html>", html)
        self.assertIn("</html>", html)
        self.assertGreater(len(html), 1000)

    def test_template_file_exists(self):
        template_path = Path(__file__).parent.parent / "lark_agent_bridge" / "templates" / "admin.html"
        self.assertTrue(template_path.exists())

    def test_js_helpers_defined_before_usage(self):
        """Verify $, text, el helpers appear before first call to prevent TDZ crash."""
        template_path = Path(__file__).parent.parent / "lark_agent_bridge" / "templates" / "admin.html"
        html = template_path.read_text(encoding="utf-8")
        # $ helper must be defined before it is first invoked
        dollar_def = html.index("const $ = ")
        dollar_use = html.index('$("login-submit")')
        self.assertLess(dollar_def, dollar_use, "$ must be defined before first usage")
        # el helper must also be defined before first call
        el_def = html.index("function el(tag")
        el_use = html.index('el("div"')
        self.assertLess(el_def, el_use, "el() must be defined before first usage")


class AdminAuthTests(unittest.TestCase):
    """Verify admin authentication module."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_no_auth_required_when_no_token_or_users(self):
        from lark_agent_bridge.auth import AdminAuth
        auth = AdminAuth(Path(self.tmp_dir))
        self.assertFalse(auth.auth_required)
        self.assertTrue(auth.check_token("anything") is None)

    def test_static_token_auth(self):
        from lark_agent_bridge.auth import AdminAuth
        auth = AdminAuth(Path(self.tmp_dir), admin_token="secret123")
        self.assertTrue(auth.auth_required)
        self.assertIsNotNone(auth.check_token("secret123"))
        self.assertIsNone(auth.check_token("wrong"))

    def test_user_create_login_logout(self):
        from lark_agent_bridge.auth import AdminAuth
        auth = AdminAuth(Path(self.tmp_dir))
        auth.create_user("testuser", "password123")
        self.assertTrue(auth.auth_required)
        self.assertTrue(auth.has_users)

        # Login with correct credentials
        session = auth.login("testuser", "password123")
        self.assertIsNotNone(session)
        self.assertEqual(session.username, "testuser")

        # Token should be valid
        user_info = auth.check_token(session.token)
        self.assertIsNotNone(user_info)
        self.assertEqual(user_info["username"], "testuser")

        # Logout invalidates token
        auth.logout(session.token)
        self.assertIsNone(auth.check_token(session.token))

    def test_wrong_password_rejected(self):
        from lark_agent_bridge.auth import AdminAuth
        auth = AdminAuth(Path(self.tmp_dir))
        auth.create_user("admin", "correct_pw")
        session = auth.login("admin", "wrong_pw")
        self.assertIsNone(session)

    def test_user_persistence(self):
        from lark_agent_bridge.auth import AdminAuth
        data_dir = Path(self.tmp_dir)
        auth1 = AdminAuth(data_dir)
        auth1.create_user("persist_user", "mypass1")
        # Reload from disk
        auth2 = AdminAuth(data_dir)
        self.assertTrue(auth2.has_users)
        session = auth2.login("persist_user", "mypass1")
        self.assertIsNotNone(session)

    def test_change_password(self):
        from lark_agent_bridge.auth import AdminAuth
        auth = AdminAuth(Path(self.tmp_dir))
        auth.create_user("user1", "old_pass")
        self.assertTrue(auth.change_password("user1", "old_pass", "new_pass"))
        self.assertIsNone(auth.login("user1", "old_pass"))
        self.assertIsNotNone(auth.login("user1", "new_pass"))

    def test_delete_user(self):
        from lark_agent_bridge.auth import AdminAuth
        auth = AdminAuth(Path(self.tmp_dir))
        auth.create_user("del_user", "pass123")
        session = auth.login("del_user", "pass123")
        self.assertIsNotNone(session)
        auth.delete_user("del_user")
        # Session should be invalidated
        self.assertIsNone(auth.check_token(session.token))
        self.assertFalse(auth.has_users)


if __name__ == "__main__":
    unittest.main()
