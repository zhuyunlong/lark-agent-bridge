"""Tests for Feishu card action event parsing."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from lark_agent_bridge.app import BridgeApp
from lark_agent_bridge.models import CardActionEvent
from lark_agent_bridge.models import ApprovalOptions, BridgeConfig, LarkEvent


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


class BridgePayloadRoutingTests(unittest.TestCase):
    def test_handle_payload_routes_card_action_callback(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = BridgeApp(BridgeConfig(data_dir=Path(tmp), approval=ApprovalOptions(enabled=True)))
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


if __name__ == "__main__":
    unittest.main()
