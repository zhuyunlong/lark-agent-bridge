from types import SimpleNamespace

from tests._app_base import BridgeApp, BridgeConfig, DownloadResource, FakeLarkClient, TaskResult, event

from lark_agent_bridge.conversation_input import build_resolved_conversation_input


def test_new_chain_input_uses_event_root_and_preserves_resource_provenance(tmp_path):
    resource = DownloadResource(
        kind="file",
        value="file_v3_log",
        source_message_id="om_file",
        display_name="Log.zip",
    )
    current_event = event(message_id="om_new", content="@bot 分析日志")

    resolved = build_resolved_conversation_input(
        event=current_event,
        route_text="分析日志",
        direct_reply_to="",
        followup_context=None,
        referenced_resources=[resource],
        is_new_chain=True,
        route_text_source="group_mention",
        context_source="none",
    )

    assert resolved.conversation_root_message_id == "om_new"
    assert resolved.followup_action == "new"
    assert resolved.followup_payload == "分析日志"
    assert resolved.referenced_resources == (resource,)
    assert resolved.resource_source_message_ids == ("om_file",)
    assert resolved.route_text_source == "group_mention"


def test_mixed_continue_keeps_complete_followup_payload():
    context = SimpleNamespace(root_message_id="om_root")
    current_event = event(message_id="om_followup", content="@bot 时间点2026-07-03 15:11分 继续自主分析")

    resolved = build_resolved_conversation_input(
        event=current_event,
        route_text="时间点2026-07-03 15:11分 继续自主分析",
        direct_reply_to="om_bot_result",
        followup_context=context,
        referenced_resources=[],
        is_new_chain=False,
        route_text_source="bot_alias_reply",
        context_source="bot_alias",
    )

    assert resolved.conversation_root_message_id == "om_root"
    assert resolved.followup_action == "continue"
    assert resolved.followup_payload == "时间点2026-07-03 15:11分 继续自主分析"


def test_pure_continue_has_empty_followup_payload():
    context = SimpleNamespace(root_message_id="om_root")

    resolved = build_resolved_conversation_input(
        event=event(message_id="om_followup", content="@bot 继续自主分析"),
        route_text="继续自主分析",
        direct_reply_to="om_bot_result",
        followup_context=context,
        referenced_resources=[],
        is_new_chain=False,
        route_text_source="bot_alias_reply",
        context_source="bot_alias",
    )

    assert resolved.followup_action == "continue"
    assert resolved.followup_payload == ""


def test_handle_event_attaches_compatible_resolved_input_to_route_context(tmp_path):
    app = BridgeApp(
        BridgeConfig(dry_run=False, data_dir=tmp_path),
        lark_client=FakeLarkClient(),
    )
    captured = {}

    def capture(ctx):
        captured["ctx"] = ctx
        return TaskResult(success=True, message="captured", details={"mode": "captured"})

    app._dispatch_route = capture
    current_event = event(
        event_id="evt_resolved_input",
        message_id="om_resolved_input",
        chat_id="ou_p2p",
        chat_type="p2p",
        content="这个问题是什么？",
    )

    result = app.handle_event(current_event)

    assert result.success
    ctx = captured["ctx"]
    assert ctx.conversation_input is not None
    assert ctx.conversation_input.event is current_event
    assert ctx.conversation_input.route_text == ctx.route_content
    assert list(ctx.conversation_input.referenced_resources) == ctx.referenced_resources
    assert ctx.conversation_input.conversation_root_message_id == "om_resolved_input"
    assert ctx.conversation_input.followup_action == "new"


def test_route_context_has_no_duplicated_conversation_fields(tmp_path):
    app = BridgeApp(
        BridgeConfig(dry_run=False, data_dir=tmp_path),
        lark_client=FakeLarkClient(),
    )
    captured = {}

    def capture(ctx):
        captured["ctx"] = ctx
        return TaskResult(success=True, message="captured", details={"mode": "captured"})

    app._dispatch_route = capture
    app.handle_event(
        event(
            event_id="evt_no_legacy_fields",
            message_id="om_no_legacy_fields",
            chat_id="ou_p2p",
            chat_type="p2p",
            content="普通问题",
        )
    )

    ctx = captured["ctx"]
    assert "route_content" not in ctx.__dict__
    assert "followup_context" not in ctx.__dict__
    assert "referenced_resources" not in ctx.__dict__
    assert ctx.route_content == ctx.conversation_input.route_text
    assert ctx.followup_context is ctx.conversation_input.followup_context
    assert ctx.referenced_resources == list(ctx.conversation_input.referenced_resources)
