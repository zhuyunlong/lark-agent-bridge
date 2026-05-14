from pathlib import Path
import tempfile
import unittest

from lark_agent_bridge.models import BridgeConfig, LarkEvent
from lark_agent_bridge.policy import evaluate_event_policy
from lark_agent_bridge.state import EventStateStore


def event(**overrides):
    values = {
        "event_id": "evt_1",
        "message_id": "om_1",
        "chat_id": "oc_1",
        "chat_type": "group",
        "sender_id": "ou_1",
        "message_type": "text",
        "content": "/signal 132002 https://example.com/log.zip",
    }
    values.update(overrides)
    return LarkEvent(**values)


class PolicyTests(unittest.TestCase):
    def test_empty_allowlists_are_allowed_in_dry_run(self):
        decision = evaluate_event_policy(BridgeConfig(dry_run=True), event())

        self.assertTrue(decision.allowed)

    def test_p2p_bypasses_group_and_user_allowlists(self):
        decision = evaluate_event_policy(
            BridgeConfig(
                dry_run=False,
                allowed_chats=["oc_group_only"],
                allowed_users=["ou_only_me"],
            ),
            event(chat_type="p2p", chat_id="ou_some_chat", sender_id="ou_other_user"),
        )

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, "p2p_allowed")

    def test_group_allowlists_still_apply(self):
        decision = evaluate_event_policy(
            BridgeConfig(
                dry_run=False,
                allowed_chats=["oc_allowed"],
                allowed_users=["ou_allowed"],
            ),
            event(chat_type="group", chat_id="oc_not_allowed", sender_id="ou_other"),
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "chat_not_allowed")

    def test_group_allows_any_sender_inside_allowed_chat(self):
        decision = evaluate_event_policy(
            BridgeConfig(
                dry_run=False,
                allowed_chats=["oc_1"],
                allowed_users=["ou_only_me"],
            ),
            event(chat_type="group", chat_id="oc_1", sender_id="ou_other"),
        )

        self.assertTrue(decision.allowed)

    def test_allowed_user_bypasses_group_allowlist(self):
        decision = evaluate_event_policy(
            BridgeConfig(
                dry_run=False,
                allowed_chats=["oc_allowed"],
                allowed_users=["ou_super"],
            ),
            event(chat_type="group", chat_id="oc_other", sender_id="ou_super"),
        )

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, "allowed_user_bypass")

    def test_state_skips_duplicate_event_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = EventStateStore(Path(tmp) / "seen.jsonl")
            sample_event = event()

            self.assertTrue(store.mark_seen(sample_event))
            self.assertFalse(store.mark_seen(sample_event))


if __name__ == "__main__":
    unittest.main()
