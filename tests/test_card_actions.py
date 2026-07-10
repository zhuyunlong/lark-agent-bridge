"""Tests for Feishu card action event parsing."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from lark_agent_bridge.app import BridgeApp
from lark_agent_bridge.dispatcher import EventDispatcher
from lark_agent_bridge.models import BotMenuEvent, CardActionEvent, MessageRecalledEvent, ReactionEvent
from lark_agent_bridge.models import ApprovalOptions, BridgeConfig, LarkEvent, LarkOptions, TaskResult


def message_event(**overrides):
    values = {
        "event_id": "evt_msg_1",
        "message_id": "om_1",
        "chat_id": "oc_1",
        "chat_type": "group",
        "sender_id": "ou_1",
        "message_type": "text",
        "content": "@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/123",
    }
    values.update(overrides)
    return LarkEvent(**values)


class RunningAppServerControl:
    def __init__(self) -> None:
        self.cancels = []
        self.steers = []

    def cancel(self, reason: str = "", *, source_message_id: str = "", actor_id: str = "") -> bool:
        self.cancels.append(
            {"reason": reason, "source_message_id": source_message_id, "actor_id": actor_id}
        )
        return True

    def steer(self, prompt: str, *, source_message_id: str = "", actor_id: str = "") -> bool:
        self.steers.append(
            {"prompt": prompt, "source_message_id": source_message_id, "actor_id": actor_id}
        )
        return True


class CardActionEventTests(unittest.TestCase):
    def test_from_nested_lark_callback_payload(self):
        event = CardActionEvent.from_dict(
            {
                "header": {"event_id": "evt_card_1"},
                "event": {
                    "context": {
                        "open_message_id": "om_card",
                        "open_chat_id": "oc_card",
                        "chat_type": "group",
                    },
                    "operator": {"operator_id": {"open_id": "ou_operator"}},
                    "action": {
                        "value": {
                            "action": "approve",
                            "request_id": "apr_123456abcdef",
                            "job_id": "job_1",
                            "root_message_id": "om_root",
                            "agent_provider": "claude",
                            "followup_text": "继续分析\u0000这个问题",
                        }
                    },
                },
            }
        )

        self.assertEqual(event.event_id, "evt_card_1")
        self.assertEqual(event.action, "approve")
        self.assertEqual(event.request_id, "apr_123456abcdef")
        self.assertEqual(event.job_id, "job_1")
        self.assertEqual(event.root_message_id, "om_root")
        self.assertEqual(event.agent_provider, "claude")
        self.assertEqual(event.message_id, "om_card")
        self.assertEqual(event.chat_id, "oc_card")
        self.assertEqual(event.chat_type, "group")
        self.assertEqual(event.operator_id, "ou_operator")
        self.assertEqual(event.followup_text, "继续分析 这个问题")

    def test_from_nested_lark_callback_payload_keeps_inbound_meta_and_revision(self):
        event = CardActionEvent.from_dict(
            {
                "request_id": "req_callback_1",
                "header": {
                    "event_id": "evt_card_meta",
                    "event_type": "card.action.trigger",
                    "create_time": "1710000000000",
                },
                "event": {
                    "context": {
                        "open_message_id": "om_card_meta",
                        "open_chat_id": "oc_card",
                    },
                    "operator": {"operator_id": {"open_id": "ou_operator"}},
                    "action": {
                        "value": {
                            "action": "reanalyze",
                            "root_message_id": "om_root",
                            "action_revision": 3,
                            "card_lifecycle_id": "life-1",
                        }
                    },
                },
            }
        )

        self.assertEqual(event.inbound_request_id, "req_callback_1")
        self.assertEqual(event.event_type, "card.action.trigger")
        self.assertEqual(event.event_create_time, "1710000000000")
        self.assertEqual(event.open_message_id, "om_card_meta")
        self.assertEqual(event.action_revision, 3)
        self.assertEqual(event.card_lifecycle_id, "life-1")

    def test_from_nested_lark_callback_payload_prefers_form_prompt(self):
        event = CardActionEvent.from_dict(
            {
                "header": {"event_id": "evt_card_form"},
                "event": {
                    "context": {
                        "open_message_id": "om_card",
                        "open_chat_id": "oc_card",
                        "chat_type": "group",
                    },
                    "operator": {"operator_id": {"open_id": "ou_operator"}},
                    "action": {
                        "value": {
                            "action": "reanalyze",
                            "job_id": "job_1",
                            "root_message_id": "om_root",
                            "followup_text": "旧追问不应覆盖输入框",
                        },
                        "form_value": {
                            "followup_prompt": "  根据导航源码重新分析 P 挡场景  ",
                        },
                    },
                },
            }
        )

        self.assertEqual(event.action, "reanalyze")
        self.assertEqual(event.followup_text, "根据导航源码重新分析 P 挡场景")

    def test_from_nested_lark_callback_payload_accepts_input_value(self):
        event = CardActionEvent.from_dict(
            {
                "header": {"event_id": "evt_card_input"},
                "event": {
                    "context": {"open_message_id": "om_card", "open_chat_id": "oc_card"},
                    "operator": {"operator_id": {"open_id": "ou_operator"}},
                    "action": {
                        "value": {"action": "answer_from_report", "root_message_id": "om_root"},
                        "input_value": "结合报告解释生命周期节点",
                    },
                },
            }
        )

        self.assertEqual(event.followup_text, "结合报告解释生命周期节点")

    def test_from_nested_lark_callback_payload_accepts_json_string_value(self):
        event = CardActionEvent.from_dict(
            {
                "header": {"event_id": "evt_card_json_value"},
                "event": {
                    "context": {
                        "open_message_id": "om_card",
                        "open_chat_id": "oc_card",
                        "chat_type": "group",
                    },
                    "operator": {"operator_id": {"open_id": "ou_operator"}},
                    "action": {
                        "value": json.dumps(
                            {
                                "action": "feedback_unhelpful",
                                "job_id": "job_1",
                                "root_message_id": "om_root",
                            }
                        )
                    },
                },
            }
        )

        self.assertEqual(event.action, "feedback_unhelpful")
        self.assertEqual(event.job_id, "job_1")
        self.assertEqual(event.root_message_id, "om_root")

    def test_rejects_malicious_callback_identifiers(self):
        event = CardActionEvent.from_dict(
            {
                "event": {
                    "context": {
                        "open_message_id": "../om",
                        "open_chat_id": "",
                    },
                    "operator": {"operator_id": {"open_id": "../../ou"}},
                    "action": {
                        "value": {
                            "action": "approve;rm",
                            "request_id": "../../etc/passwd",
                            "job_id": "../job",
                            "root_message_id": "om_root/../../x",
                        }
                    },
                },
            }
        )

        self.assertEqual(event.action, "")
        self.assertEqual(event.request_id, "")
        self.assertEqual(event.job_id, "")
        self.assertEqual(event.root_message_id, "")
        self.assertEqual(event.message_id, "")
        self.assertEqual(event.operator_id, "")


class BotMenuEventTests(unittest.TestCase):
    def test_from_application_menu_payload(self):
        event = BotMenuEvent.from_dict(
            {
                "request_id": "req_menu_1",
                "header": {
                    "event_id": "evt_menu_1",
                    "event_type": "application.bot.menu_v6",
                    "create_time": "1710000001000",
                },
                "event": {
                    "event_key": "menu",
                    "timestamp": 1710000001,
                    "operator": {"operator_id": {"open_id": "ou_menu"}},
                },
            }
        )

        self.assertEqual(event.event_id, "evt_menu_1")
        self.assertEqual(event.event_type, "application.bot.menu_v6")
        self.assertEqual(event.inbound_request_id, "req_menu_1")
        self.assertEqual(event.menu_key, "menu")
        self.assertEqual(event.operator_id, "ou_menu")
        self.assertTrue(event.is_valid)


class ReactionEventTests(unittest.TestCase):
    def test_from_reaction_created_payload(self):
        event = ReactionEvent.from_dict(
            {
                "request_id": "req_reaction_1",
                "header": {
                    "event_id": "evt_reaction_1",
                    "event_type": "im.message.reaction.created_v1",
                    "create_time": "1710000002000",
                },
                "event": {
                    "message_id": "om_app_root",
                    "reaction_type": {"emoji_type": "ThumbsUp"},
                    "operator": {"operator_id": {"open_id": "ou_reactor"}},
                },
            }
        )

        self.assertEqual(event.event_id, "evt_reaction_1")
        self.assertEqual(event.event_type, "im.message.reaction.created_v1")
        self.assertEqual(event.inbound_request_id, "req_reaction_1")
        self.assertEqual(event.message_id, "om_app_root")
        self.assertEqual(event.operator_id, "ou_reactor")
        self.assertEqual(event.reaction_type, "ThumbsUp")
        self.assertTrue(event.is_created)
        self.assertFalse(event.is_deleted)


class MessageRecalledEventTests(unittest.TestCase):
    def test_from_recalled_payload(self):
        event = MessageRecalledEvent.from_dict(
            {
                "request_id": "req_recall_1",
                "header": {
                    "event_id": "evt_recall_1",
                    "event_type": "im.message.recalled_v1",
                    "create_time": "1710000003000",
                },
                "event": {
                    "message_id": "om_app_root",
                    "chat_id": "oc_1",
                    "operator": {"operator_id": {"open_id": "ou_recaller"}},
                },
            }
        )

        self.assertEqual(event.event_id, "evt_recall_1")
        self.assertEqual(event.event_type, "im.message.recalled_v1")
        self.assertEqual(event.inbound_request_id, "req_recall_1")
        self.assertEqual(event.message_id, "om_app_root")
        self.assertEqual(event.chat_id, "oc_1")
        self.assertEqual(event.operator_id, "ou_recaller")


class BridgePayloadRoutingTests(unittest.TestCase):
    def test_handle_payload_routes_card_action_callback(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = BridgeApp(
                BridgeConfig(
                    data_dir=Path(tmp),
                    approval=ApprovalOptions(enabled=True),
                    lark=LarkOptions(bot_name="bot"),
                )
            )
            pending = app.handle_event(message_event())
            self.assertEqual(pending.error_code, "approval_pending")

            result = app.handle_payload(
                {
                    "header": {"event_id": "evt_card_2"},
                    "event": {
                        "context": {
                            "open_message_id": "om_card",
                            "open_chat_id": "oc_1",
                        },
                        "operator": {"operator_id": {"open_id": "ou_1"}},
                        "action": {
                            "value": {
                                "action": "reject",
                                "request_id": pending.details["approval_request_id"],
                            }
                        },
                    },
                }
            )

        self.assertEqual(result.error_code, "approval_rejected")

    def test_handle_payload_suppresses_duplicate_card_callback_request_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = BridgeApp(
                BridgeConfig(
                    data_dir=Path(tmp),
                    approval=ApprovalOptions(enabled=True),
                    lark=LarkOptions(bot_name="bot"),
                )
            )
            pending = app.handle_event(message_event())
            self.assertEqual(pending.error_code, "approval_pending")
            payload = {
                "request_id": "req_card_duplicate",
                "header": {"event_id": "evt_card_duplicate_1"},
                "event": {
                    "context": {
                        "open_message_id": "om_card",
                        "open_chat_id": "oc_1",
                    },
                    "operator": {"operator_id": {"open_id": "ou_1"}},
                    "action": {
                        "value": {
                            "action": "reject",
                            "request_id": pending.details["approval_request_id"],
                        }
                    },
                },
            }

            first = app.handle_payload(payload)
            second_payload = dict(payload)
            second_payload["header"] = {"event_id": "evt_card_duplicate_2"}
            second = app.handle_payload(second_payload)

        self.assertEqual(first.error_code, "approval_rejected")
        self.assertTrue(second.success)
        self.assertTrue(second.skipped)
        self.assertEqual(second.details["mode"], "duplicate_inbound")
        self.assertEqual(second.details["dedupe_key"], "request:req_card_duplicate")

    def test_handle_payload_skips_stale_card_action_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = BridgeApp(
                BridgeConfig(
                    data_dir=Path(tmp),
                    approval=ApprovalOptions(enabled=True),
                    lark=LarkOptions(bot_name="bot"),
                )
            )
            root_event = message_event(message_id="om_root")
            app.activity_store.record_event(root_event)
            app.activity_store.record_result(
                root_event,
                TaskResult(
                    success=True,
                    message="done",
                    details={
                        "mode": "bug_analysis",
                        "card_lifecycle_id": "life-current",
                        "action_revision": 2,
                    },
                ),
            )

            result = app.handle_payload(
                {
                    "request_id": "req_card_stale",
                    "header": {"event_id": "evt_card_stale"},
                    "event": {
                        "context": {
                            "open_message_id": "om_card",
                            "open_chat_id": "oc_1",
                        },
                        "operator": {"operator_id": {"open_id": "ou_1"}},
                        "action": {
                            "value": {
                                "action": "feedback_helpful",
                                "root_message_id": "om_root",
                                "card_lifecycle_id": "life-current",
                                "action_revision": 1,
                            }
                        },
                    },
                }
            )

        self.assertTrue(result.success)
        self.assertTrue(result.skipped)
        self.assertEqual(result.error_code, "stale_card_action")
        self.assertEqual(result.details["mode"], "stale_card_action")
        self.assertEqual(result.details["expected_action_revision"], 2)
        self.assertEqual(result.details["received_action_revision"], 1)

    def test_handle_payload_routes_bot_menu_help_to_basic_chat(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = BridgeApp(BridgeConfig(data_dir=Path(tmp), lark=LarkOptions(bot_name="bot")))

            result = app.handle_payload(
                {
                    "request_id": "req_menu_help",
                    "header": {
                        "event_id": "evt_menu_help",
                        "event_type": "application.bot.menu_v6",
                    },
                    "event": {
                        "event_key": "menu",
                        "operator": {"operator_id": {"open_id": "ou_menu"}},
                    },
                }
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "basic_chat")
        self.assertIn("常用触发方式", result.message)

    def test_handle_payload_routes_bot_menu_stop_to_running_app_server_control(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = BridgeApp(BridgeConfig(data_dir=Path(tmp), lark=LarkOptions(bot_name="bot")))
            control = RunningAppServerControl()
            app._register_app_server_control("om_app_root", control)

            result = app.handle_payload(
                {
                    "request_id": "req_menu_stop",
                    "header": {
                        "event_id": "evt_menu_stop",
                        "event_type": "application.bot.menu_v6",
                    },
                    "event": {
                        "event_key": "stop",
                        "operator": {"operator_id": {"open_id": "ou_menu"}},
                    },
                }
            )

        self.assertTrue(result.success)
        self.assertTrue(result.skipped)
        self.assertEqual(result.details["mode"], "bot_menu")
        self.assertEqual(result.details["action"], "stop")
        self.assertEqual(len(control.cancels), 1)
        self.assertEqual(control.cancels[0]["actor_id"], "ou_menu")
        self.assertEqual(app.activity_store.get_session("om_app_root")["status"], "cancelled")

    def test_interaction_control_payloads_are_dispatched_as_light(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = BridgeApp(BridgeConfig(data_dir=Path(tmp), lark=LarkOptions(bot_name="bot")))
            dispatcher = EventDispatcher(app, max_workers=1)
            payloads = (
                {
                    "header": {
                        "event_id": "evt_menu_light",
                        "event_type": "application.bot.menu_v6",
                    },
                    "event": {
                        "event_key": "stop",
                        "operator": {"operator_id": {"open_id": "ou_menu"}},
                    },
                },
                {
                    "header": {
                        "event_id": "evt_reaction_light",
                        "event_type": "im.message.reaction.created_v1",
                    },
                    "event": {
                        "message_id": "om_app_root",
                        "reaction_type": {"emoji_type": "ThumbsUp"},
                    },
                },
                {
                    "header": {
                        "event_id": "evt_recall_light",
                        "event_type": "im.message.recalled_v1",
                    },
                    "event": {"message_id": "om_app_root"},
                },
            )

            for payload in payloads:
                with self.subTest(event_type=payload["header"]["event_type"]):
                    weight, _chat_id = dispatcher._classify(payload)
                    self.assertEqual(weight, "light")

    def test_handle_payload_routes_thumbs_up_reaction_to_running_app_server_steer(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = BridgeApp(BridgeConfig(data_dir=Path(tmp), lark=LarkOptions(bot_name="bot")))
            control = RunningAppServerControl()
            app._register_app_server_control("om_app_root", control)

            result = app.handle_payload(
                {
                    "request_id": "req_reaction_steer",
                    "header": {
                        "event_id": "evt_reaction_steer",
                        "event_type": "im.message.reaction.created_v1",
                    },
                    "event": {
                        "message_id": "om_app_root",
                        "reaction_type": {"emoji_type": "ThumbsUp"},
                        "operator": {"operator_id": {"open_id": "ou_reactor"}},
                    },
                }
            )

        self.assertTrue(result.success)
        self.assertTrue(result.skipped)
        self.assertEqual(result.details["mode"], "app_server_reaction")
        self.assertEqual(result.details["action"], "steer")
        self.assertEqual(len(control.steers), 1)
        self.assertEqual(control.steers[0]["actor_id"], "ou_reactor")
        self.assertEqual(control.steers[0]["source_message_id"], "reaction:evt_reaction_steer")
        self.assertIn("认可当前方向", control.steers[0]["prompt"])

    def test_handle_payload_suppresses_duplicate_reaction_request_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = BridgeApp(BridgeConfig(data_dir=Path(tmp), lark=LarkOptions(bot_name="bot")))
            control = RunningAppServerControl()
            app._register_app_server_control("om_app_root", control)
            payload = {
                "request_id": "req_reaction_duplicate",
                "header": {
                    "event_id": "evt_reaction_duplicate_1",
                    "event_type": "im.message.reaction.created_v1",
                },
                "event": {
                    "message_id": "om_app_root",
                    "reaction_type": {"emoji_type": "ThumbsUp"},
                    "operator": {"operator_id": {"open_id": "ou_reactor"}},
                },
            }

            first = app.handle_payload(payload)
            second_payload = dict(payload)
            second_payload["header"] = {
                "event_id": "evt_reaction_duplicate_2",
                "event_type": "im.message.reaction.created_v1",
            }
            second = app.handle_payload(second_payload)

        self.assertEqual(first.details["action"], "steer")
        self.assertTrue(second.success)
        self.assertTrue(second.skipped)
        self.assertEqual(second.details["mode"], "duplicate_inbound")
        self.assertEqual(second.details["dedupe_key"], "request:req_reaction_duplicate")
        self.assertEqual(len(control.steers), 1)

    def test_handle_payload_routes_message_recall_of_running_root_to_cancel(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = BridgeApp(BridgeConfig(data_dir=Path(tmp), lark=LarkOptions(bot_name="bot")))
            control = RunningAppServerControl()
            app._register_app_server_control("om_app_root", control)

            result = app.handle_payload(
                {
                    "request_id": "req_recall_cancel",
                    "header": {
                        "event_id": "evt_recall_cancel",
                        "event_type": "im.message.recalled_v1",
                    },
                    "event": {
                        "message_id": "om_app_root",
                        "operator": {"operator_id": {"open_id": "ou_recaller"}},
                    },
                }
            )

        self.assertTrue(result.success)
        self.assertTrue(result.skipped)
        self.assertEqual(result.details["mode"], "message_recalled")
        self.assertEqual(result.details["action"], "cancel")
        self.assertEqual(len(control.cancels), 1)
        self.assertEqual(control.cancels[0]["actor_id"], "ou_recaller")
        self.assertEqual(control.cancels[0]["source_message_id"], "recall:evt_recall_cancel")
        self.assertEqual(app.activity_store.get_session("om_app_root")["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
