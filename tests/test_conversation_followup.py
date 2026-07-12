import pytest

from lark_agent_bridge import conversation_input


@pytest.mark.parametrize(
    ("text", "action", "payload"),
    [
        ("重新分析", "retry", ""),
        ("继续自主分析", "continue", ""),
        ("时间点2026-07-03 15:11分 继续自主分析", "continue", "时间点2026-07-03 15:11分 继续自主分析"),
        ("黑屏仍然存在，继续分析", "continue", "黑屏仍然存在，继续分析"),
        ("改为从 Android 到 Unity 方向重新分析", "retry", "改为从 Android 到 Unity 方向重新分析"),
        ("为什么会这样？", "ask", "为什么会这样？"),
    ],
)
def test_followup_action_and_payload_are_resolved_once_without_losing_mixed_text(text, action, payload):
    resolved_action, resolved_payload = conversation_input.resolve_followup_action_payload(
        text,
        has_followup_context=True,
    )

    assert resolved_action == action
    assert resolved_payload == payload


def test_text_without_followup_context_is_a_new_request():
    action, payload = conversation_input.resolve_followup_action_payload(
        "继续自主分析",
        has_followup_context=False,
    )

    assert action == "new"
    assert payload == "继续自主分析"
