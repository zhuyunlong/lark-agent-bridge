import pytest

from lark_agent_bridge import conversation_input
from lark_agent_bridge.models import DownloadResource


def resource(kind, value, *, source="", name=""):
    return DownloadResource(
        kind=kind,
        value=value,
        source_message_id=source,
        display_name=name,
    )


@pytest.mark.parametrize(
    ("current", "reply", "session", "bug", "expected_source", "expected_values"),
    [
        (
            [],
            [resource("file", "file_zip", source="om_file", name="logs.zip")],
            [resource("local", "/tmp/prepared")],
            [resource("file", "bug_attachment")],
            "reply_chain",
            ["file_zip"],
        ),
        (
            [],
            [],
            [resource("local", "/tmp/prepared")],
            [resource("file", "bug_attachment")],
            "session",
            ["/tmp/prepared"],
        ),
        (
            [],
            [],
            [],
            [resource("file", "bug_attachment")],
            "bug_attachment",
            ["bug_attachment"],
        ),
    ],
)
def test_resource_precedence(current, reply, session, bug, expected_source, expected_values):
    resolved = conversation_input.resolve_conversation_resources(
        current_message=current,
        reply_chain=reply,
        session=session,
        bug_attachments=bug,
    )

    assert resolved.selected_source == expected_source
    assert [item.value for item in resolved.preferred_resources] == expected_values


def test_current_message_resources_precede_reply_and_preserve_provenance():
    current = resource("file", "file_single", source="om_current", name="main.alog")
    replied_folder = resource("folder", "fld_logs", source="om_reply", name="logs")

    resolved = conversation_input.resolve_conversation_resources(
        current_message=[current],
        reply_chain=[replied_folder],
        session=[],
        bug_attachments=[],
    )

    assert resolved.selected_source == "current_message"
    assert resolved.preferred_resources == (current,)
    assert resolved.reply_chain == (replied_folder,)
    assert resolved.source_message_ids == ("om_current", "om_reply")


def test_empty_resource_view_has_no_selected_source():
    resolved = conversation_input.resolve_conversation_resources(
        current_message=[],
        reply_chain=[],
        session=[],
        bug_attachments=[],
    )

    assert resolved.selected_source == "none"
    assert resolved.preferred_resources == ()
