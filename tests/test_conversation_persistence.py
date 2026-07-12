from types import SimpleNamespace

from lark_agent_bridge import conversation_input
from lark_agent_bridge.models import DownloadResource, TaskResult
from lark_agent_bridge.state import AgentActivityStore
from tests._app_base import event


def test_conversation_snapshot_is_versioned_and_keeps_normalized_provenance():
    current_event = event(
        message_id="om_followup",
        root_id="",
        reply_to="",
        content="@bot 时间点 15:11 继续分析",
        raw={"token": "must-not-be-persisted"},
    )
    resource = DownloadResource(
        kind="file",
        value="file_log",
        source_message_id="om_file",
        display_name="logs.zip",
    )
    resource_view = conversation_input.resolve_conversation_resources(
        current_message=[],
        reply_chain=[resource],
        session=[],
        bug_attachments=[],
    )
    resolved = conversation_input.build_resolved_conversation_input(
        event=current_event,
        route_text="时间点 15:11 继续分析",
        direct_reply_to="om_bot_reply",
        followup_context=SimpleNamespace(root_message_id="om_root"),
        referenced_resources=[resource],
        is_new_chain=False,
        route_text_source="bot_alias_reply",
        context_source="bot_alias",
        conversation_root_message_id="om_root",
        resource_view=resource_view,
    )

    snapshot = conversation_input.conversation_input_snapshot(resolved)

    assert snapshot["version"] == 1
    assert snapshot["conversation_root_message_id"] == "om_root"
    assert snapshot["followup_action"] == "continue"
    assert snapshot["followup_payload"] == "时间点 15:11 继续分析"
    assert snapshot["resources"]["selected_source"] == "reply_chain"
    assert snapshot["resources"]["preferred"][0]["source_message_id"] == "om_file"
    assert "raw" not in snapshot
    assert "token" not in str(snapshot)


def test_activity_result_preserves_conversation_snapshot(tmp_path):
    store = AgentActivityStore(tmp_path / "activity.json")
    current_event = event(message_id="om_followup", content="继续分析")
    snapshot = {
        "version": 1,
        "conversation_root_message_id": "om_root",
        "route_text": "继续分析",
    }
    store.record_event(current_event)

    store.record_conversation_input("om_root", current_event, snapshot)
    store.record_result(
        current_event,
        TaskResult(
            success=True,
            message="完成",
            details={"mode": "direct_analysis", "conversation_root_message_id": "om_root"},
        ),
    )

    session = store.get_session("om_root")
    assert session is not None
    assert session["details"]["conversation_input"] == snapshot


def test_legacy_session_without_snapshot_remains_supported():
    assert conversation_input.resource_view_from_snapshot({"mode": "legacy"}) is None
