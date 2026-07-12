from collections import Counter
import json

from tests._app_base import (
    BridgeApp,
    BridgeConfig,
    FakeLarkClient,
    LarkOptions,
    TaskResult,
    event,
)


class CountingLarkClient(FakeLarkClient):
    def __init__(self):
        super().__init__()
        self.fetch_counts: Counter[str] = Counter()

    def fetch_message(self, message_id):
        self.fetch_counts[message_id] += 1
        return super().fetch_message(message_id)


def _capture_route_context(app, *, stub_resources=True):
    captured = {}

    def capture(ctx):
        captured["ctx"] = ctx
        return TaskResult(success=True, message="captured", details={"mode": "captured"})

    app._dispatch_route = capture
    if stub_resources:
        app._fetch_referenced_message_resources = lambda *args, **kwargs: []
    return captured


def test_group_event_without_reply_metadata_fetches_current_message_once(tmp_path):
    lark = CountingLarkClient()
    current_message_id = "om_current"
    bot_alias_id = "om_bot_alias"
    lark.fetched_messages[current_message_id] = json.dumps(
        {
            "data": {
                "messages": [
                    {
                        "message_id": current_message_id,
                        "reply_to": bot_alias_id,
                        "mentions": [{"type": "bot", "name": "Bridge Bot"}],
                    }
                ]
            }
        },
        ensure_ascii=False,
    )
    app = BridgeApp(
        BridgeConfig(
            dry_run=False,
            data_dir=tmp_path,
            allowed_chats=["oc_allowed"],
            lark=LarkOptions(bot_name="", bot_open_id=""),
        ),
        lark_client=lark,
    )
    app.conversation_store.remember(
        root_message_id="om_root",
        chat_id="oc_allowed",
        mode="bug_analysis",
        request_text="原始请求",
        summary_text="原始结论",
        report_url="",
        report_excerpt="",
    )
    app.conversation_store.remember_alias(
        alias_message_id=bot_alias_id,
        root_message_id="om_root",
    )
    captured = _capture_route_context(app, stub_resources=False)

    result = app.handle_event(
        event(
            event_id="evt_current",
            message_id=current_message_id,
            chat_id="oc_allowed",
            content="@Bridge Bot 继续分析",
            reply_to="",
            parent_id="",
            root_id="",
            raw={},
        )
    )

    assert result.success
    assert lark.fetch_counts[current_message_id] == 1
    resolved = captured["ctx"].conversation_input
    assert resolved.route_text == "继续分析"
    assert resolved.direct_reply_to == bot_alias_id
    assert resolved.context_source == "bot_alias"
    assert resolved.conversation_root_message_id == "om_root"


def test_group_reply_to_bot_alias_without_mention_remains_addressed(tmp_path):
    app = BridgeApp(
        BridgeConfig(
            dry_run=False,
            data_dir=tmp_path,
            allowed_chats=["oc_allowed"],
        ),
        lark_client=FakeLarkClient(),
    )
    app.conversation_store.remember(
        root_message_id="om_root",
        chat_id="oc_allowed",
        mode="direct_analysis",
        request_text="原始请求",
        summary_text="原始结论",
        report_url="",
        report_excerpt="",
    )
    app.conversation_store.remember_alias(
        alias_message_id="om_bot_alias",
        root_message_id="om_root",
    )
    captured = _capture_route_context(app)

    result = app.handle_event(
        event(
            event_id="evt_alias",
            message_id="om_alias_followup",
            chat_id="oc_allowed",
            content="继续查这个问题",
            reply_to="om_bot_alias",
        )
    )

    assert result.success
    resolved = captured["ctx"].conversation_input
    assert resolved.route_text == "继续查这个问题"
    assert resolved.context_source == "bot_alias"
    assert resolved.conversation_root_message_id == "om_root"


def test_p2p_reply_recovers_persisted_context_after_restart(tmp_path):
    config = BridgeConfig(dry_run=False, data_dir=tmp_path)
    first_app = BridgeApp(config, lark_client=FakeLarkClient())
    first_app.conversation_store.remember(
        root_message_id="om_root",
        chat_id="ou_p2p",
        mode="bug_analysis",
        request_text="原始请求",
        summary_text="原始结论",
        report_url="",
        report_excerpt="",
    )

    restarted_app = BridgeApp(config, lark_client=FakeLarkClient())
    captured = _capture_route_context(restarted_app)
    result = restarted_app.handle_event(
        event(
            event_id="evt_p2p_restart",
            message_id="om_p2p_followup",
            chat_id="ou_p2p",
            chat_type="p2p",
            content="继续分析",
            reply_to="om_root",
        )
    )

    assert result.success
    resolved = captured["ctx"].conversation_input
    assert resolved.context_source == "reply_chain"
    assert resolved.conversation_root_message_id == "om_root"
    assert not resolved.is_new_chain


def test_group_message_not_addressed_result_is_unchanged(tmp_path):
    app = BridgeApp(
        BridgeConfig(
            dry_run=False,
            data_dir=tmp_path,
            allowed_chats=["oc_allowed"],
        ),
        lark_client=FakeLarkClient(),
    )

    result = app.handle_event(
        event(
            event_id="evt_not_addressed",
            message_id="om_not_addressed",
            chat_id="oc_allowed",
            content="普通群消息",
        )
    )

    assert result.success
    assert result.skipped
    assert result.message == "group message not addressed to this bot"
    assert result.details == {"mode": "not_addressed"}
