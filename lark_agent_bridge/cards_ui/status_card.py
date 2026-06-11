"""进度状态卡片构建。"""

from __future__ import annotations

from typing import Any
from .texts import (
    CARD_PROGRESS_PREVIEW_LIMIT,
    CARD_RESULT_NOTE_MAX_CHARS,
    CARD_STREAM_MAX_LINE_CHARS,
    CARD_STREAM_PREVIEW_LIMIT,
    HEADER_TEMPLATES,
    STATUS_LABELS,
)
from .elements import (
    _action_block,
    _button,
    _divider,
    _field_element,
    _fields_block,
    _form_block,
    _header,
    _input_element,
    _md_element,
    _note_block,
)
from .textutils import (
    _card_result_note_preview,
    _format_token_usage,
    _progress_lines,
    _stream_lines,
)
from .choice_blocks import (
    _agent_choice_action_blocks,
    _skill_choice_action_blocks,
    _valid_agent_choices,
)


def build_status_card(
    *,
    title: str,
    status: str,
    details: dict[str, str] | None = None,
    progress: list[dict[str, Any]] | None = None,
    elapsed_seconds: float | None = None,
    token_usage: dict[str, int] | None = None,
    report_url: str | None = None,
    live_url: str | None = None,
    note: str | None = None,
    job_id: str | None = None,
    root_message_id: str | None = None,
    show_followup_actions: bool = False,
    bug_skill_choices: list[dict[str, Any]] | None = None,
    bug_skill_choice_note: str | None = None,
    bug_agent_choices: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a status progress card.

    Parameters
    ----------
    title:
        Card header title, e.g. "Bug 分析".
    status:
        One of ``queued``, ``downloading``, ``analyzing``, ``completed``,
        ``failed``.
    details:
        Optional key-value pairs to display as fields.
    note:
        Optional footnote.
    """
    status_label = STATUS_LABELS.get(status, status)
    color = HEADER_TEMPLATES.get(status, "blue")

    elements: list[dict[str, Any]] = [
        _md_element(f"**当前状态：** {status_label}"),
    ]

    runtime_details = dict(details or {})
    if elapsed_seconds is not None:
        runtime_details["耗时"] = f"{elapsed_seconds:.1f} 秒"
    if token_usage:
        runtime_details["Token"] = _format_token_usage(token_usage)
    if runtime_details:
        fields = [_field_element(k, v) for k, v in runtime_details.items()]
        elements.append(_fields_block(fields))

    result_note = (note or "").strip()
    note_rendered_as_summary = False
    if result_note and status in {"completed", "failed"}:
        elements.append(_divider())
        heading = "失败原因" if status == "failed" else "结论摘要"
        elements.append(
            _md_element(
                f"**{heading}**\n"
                f"{_card_result_note_preview(result_note, max_chars=CARD_RESULT_NOTE_MAX_CHARS)}"
            )
        )
        note_rendered_as_summary = True

    progress_lines = _progress_lines(progress or [], limit=CARD_PROGRESS_PREVIEW_LIMIT)
    if progress_lines:
        elements.append(_divider())
        elements.append(_md_element("**后台进度**\n" + "\n".join(progress_lines)))

    stream_lines = _stream_lines(
        progress or [],
        limit=CARD_STREAM_PREVIEW_LIMIT,
        max_line_chars=CARD_STREAM_MAX_LINE_CHARS,
    )
    if stream_lines:
        elements.append(_divider())
        elements.append(
            _md_element(
                "**深度分析输出（最近 3 条）**\n"
                + "\n".join(f"- {line}" for line in stream_lines)
            )
        )

    action_buttons: list[dict[str, Any]] = []
    if report_url:
        action_buttons.append(_button("📄 打开报告", url=report_url, button_type="primary"))
    if live_url:
        action_buttons.append(_button("📡 实时进度", url=live_url))
    if action_buttons:
        elements.append(_divider())
        elements.append(_action_block(action_buttons))

    if bug_skill_choices and (job_id or root_message_id):
        context = {
            "job_id": job_id or "",
            "root_message_id": root_message_id or "",
        }
        choice_note = (
            bug_skill_choice_note.strip()
            if isinstance(bug_skill_choice_note, str) and bug_skill_choice_note.strip()
            else "如果当前意图或命中的 Skill 不符合预期，可以补充要求后改选一个支持的 Skill，系统会基于已有日志重新分析。"
        )
        valid_skill_choices: list[dict[str, Any]] = []
        for skill in bug_skill_choices[:8]:
            label = str(skill.get("label") or skill.get("name") or "").strip()
            name = str(skill.get("name") or "").strip()
            if not label or not name:
                continue
            valid_skill_choices.append(skill)
        if valid_skill_choices:
            elements.append(_divider())
            elements.append(
                _md_element(
                    "**意图/Skill 校正**\n"
                    f"{choice_note}\n"
                    "本区的 Skill 按钮会按所选 Skill 基于已有日志重新分析，不代表当前报告已使用这些 Skill；选择前建议先在输入框写清楚新的分析要求。"
                )
            )
            selected_labels = [
                str(skill.get("label") or skill.get("name") or "").strip()
                for skill in valid_skill_choices
                if bool(skill.get("selected")) and str(skill.get("label") or skill.get("name") or "").strip()
            ]
            if selected_labels:
                elements.append(_note_block("当前命中：" + "、".join(selected_labels)))
            elements.append(
                _form_block(
                    "bug_skill_choice_form",
                    [
                        _input_element(
                            "followup_prompt",
                            placeholder="可选：补充分析要求，例如：重点看问题时间前 5 分钟的 SR 生命周期",
                            required=False,
                        ),
                        *_skill_choice_action_blocks(valid_skill_choices, context),
                    ],
                )
            )

    if show_followup_actions and (job_id or root_message_id):
        context = {
            "job_id": job_id or "",
            "root_message_id": root_message_id or "",
        }
        valid_agent_choices = _valid_agent_choices(bug_agent_choices or [])
        elements.append(
            _note_block(
                "提示：先输入追问或重跑要求，再选择回答/重跑/续 Agent；输入为空时不会执行。"
            )
        )
        form_elements: list[dict[str, Any]] = [
            _input_element(
                "followup_prompt",
                placeholder="请输入追问或重跑要求，例如：根据源码分析问题时刻前的生命周期",
                required=False,
            ),
            _action_block(
                [
                    _button(
                        "📌 按输入从报告回答",
                        value={"action": "answer_from_report", **context},
                    ),
                    _button(
                        "🔄 按输入重跑日志",
                        value={"action": "reanalyze", **context},
                        button_type="primary",
                    ),
                    _button(
                        "🧠 按输入续 Agent",
                        value={"action": "continue_agent", **context},
                    ),
                ]
            ),
        ]
        if valid_agent_choices:
            elements.append(
                _note_block("换 Agent 会先弹出确认卡；确认后会基于同一个 bug 会话重新分析。")
            )
            form_elements.extend(_agent_choice_action_blocks(valid_agent_choices, context))
        elements.append(
            _form_block(
                "followup_prompt_form",
                form_elements,
            )
        )
        feedback_context = {**context, "followup_text": ""}
        elements.append(
            _action_block(
                [
                    _button(
                        "👍 有用(记录)",
                        value={"action": "feedback_helpful", **feedback_context},
                    ),
                    _button(
                        "👎 不准(记录)",
                        value={"action": "feedback_unhelpful", **feedback_context},
                        button_type="danger",
                    ),
                ]
            )
        )

    if note and not note_rendered_as_summary:
        elements.append(_divider())
        elements.append(_note_block(note))

    return {
        "config": {"wide_screen_mode": True},
        "header": _header(f"📋 {title}", color=color),
        "elements": elements,
    }
