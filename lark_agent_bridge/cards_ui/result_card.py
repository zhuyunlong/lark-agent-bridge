"""结果/确认/追问/知识答案卡片构建。"""

from __future__ import annotations

import re
from typing import Any
from .texts import (
    HEADER_TEMPLATES,
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
    _truncate_card_body,
    _truncate_summary,
)
from .choice_blocks import (
    _agent_choice_action_blocks,
    _valid_agent_choices,
)


def build_result_card(
    *,
    title: str,
    success: bool,
    summary: str,
    report_url: str | None = None,
    metadata: dict[str, str] | None = None,
    show_actions: bool = True,
    job_id: str | None = None,
    root_message_id: str | None = None,
    duration_seconds: float | None = None,
) -> dict[str, Any]:
    """Build a final analysis result card.

    Parameters
    ----------
    title:
        Card header title.
    success:
        Whether the analysis succeeded.
    summary:
        Human-readable conclusion (Markdown).
    report_url:
        Link to the full HTML report.
    metadata:
        Key-value metadata fields (analysis type, fault time, etc.).
    show_actions:
        Whether to render action buttons.
    job_id:
        Optional job identifier for re-analysis.
    duration_seconds:
        Optional duration of analysis.
    """
    status = "completed" if success else "failed"
    color = HEADER_TEMPLATES.get(status, "blue")

    elements: list[dict[str, Any]] = []

    # Metadata fields
    if metadata:
        fields = [_field_element(k, v) for k, v in metadata.items()]
        elements.append(_fields_block(fields))

    # Duration
    if duration_seconds is not None:
        elements.append(_md_element(f"**耗时：** {duration_seconds:.1f} 秒"))

    elements.append(_divider())

    # Summary (truncate for card display)
    truncated = _truncate_summary(summary, max_chars=800)
    elements.append(_md_element(truncated))

    # Action buttons
    if show_actions:
        buttons: list[dict[str, Any]] = []
        if report_url:
            buttons.append(_button("📄 打开报告", url=report_url, button_type="primary"))
        if job_id:
            buttons.append(
                _button(
                    "🔄 重新分析",
                    value={
                        "action": "reanalyze",
                        "job_id": job_id,
                        "root_message_id": root_message_id or "",
                    },
                )
            )
        buttons.append(
            _button(
                "🆘 升级人工",
                value={
                    "action": "escalate",
                    "job_id": job_id or "",
                    "root_message_id": root_message_id or "",
                },
            )
        )
        if buttons:
            elements.append(_action_block(buttons))

    return {
        "config": {"wide_screen_mode": True},
        "header": _header(f"{'✅' if success else '❌'} {title}", color=color),
        "elements": elements,
    }


def build_agent_reanalysis_confirmation_card(
    *,
    agent_label: str,
    agent_provider: str,
    job_id: str = "",
    root_message_id: str = "",
    followup_text: str = "",
) -> dict[str, Any]:
    """Build a confirmation card before re-running bug analysis with another agent."""
    provider = agent_provider.strip()
    context = {
        "job_id": job_id.strip(),
        "root_message_id": root_message_id.strip(),
        "agent_provider": provider,
        "followup_text": followup_text.strip()[:1000],
    }
    prompt = followup_text.strip() or f"换用 {agent_label} 基于已有日志/报告重新分析"
    elements = [
        _md_element(
            f"即将使用 **{agent_label}** 重新分析当前 bug 会话。\n\n"
            f"**重分析要求**\n{_truncate_summary(prompt, max_chars=360)}"
        ),
        _action_block(
            [
                _button(
                    f"确认使用 {agent_label}",
                    value={"action": "confirm_bug_agent_reanalysis", **context},
                    button_type="primary",
                ),
                _button(
                    "取消",
                    value={"action": "cancel_bug_agent_reanalysis", **context},
                    button_type="default",
                ),
            ]
        ),
    ]
    return {
        "config": {"wide_screen_mode": True},
        "header": _header("确认换 Agent 重分析", color="orange"),
        "elements": elements,
    }


def build_followup_result_card(
    *,
    title: str,
    summary: str,
    report_url: str | None = None,
    root_message_id: str | None = None,
    job_id: str | None = None,
    followup_text: str = "",
    answer_confidence: float | None = None,
    bug_agent_choices: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a bug follow-up card with explicit next-step choices."""
    prompt_hint = followup_text.strip()
    metadata: dict[str, str] = {
        "处理方式": "先输入追问后执行",
        "上次追问": _truncate_summary(prompt_hint, max_chars=80) if prompt_hint else "未提供",
    }
    if answer_confidence is not None:
        metadata["置信度"] = f"{answer_confidence:.2f}"

    elements: list[dict[str, Any]] = []
    elements.append(_fields_block([_field_element(k, v) for k, v in metadata.items()]))
    elements.append(_divider())
    elements.append(_md_element(_truncate_summary(summary, max_chars=800)))
    elements.append(
        _note_block(
            "提示：先输入追问/重跑提示词，再点下面按钮。输入为空时不会执行；也可以直接回复这张卡片继续补充。"
        )
    )

    context = {
        "job_id": job_id or "",
        "root_message_id": root_message_id or "",
    }
    valid_agent_choices = _valid_agent_choices(bug_agent_choices or [])
    if report_url:
        elements.append(_action_block([_button("📄 打开报告", url=report_url, button_type="primary")]))
    form_elements: list[dict[str, Any]] = [
        _input_element(
            "followup_prompt",
            placeholder="请输入追问或重跑要求，例如：根据导航源码分析 P 挡生命周期",
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
        elements.append(_note_block("换 Agent 会先弹出确认卡；确认后会基于同一个 bug 会话重新分析。"))
        form_elements.extend(_agent_choice_action_blocks(valid_agent_choices, context))
    elements.append(
        _form_block(
            "followup_prompt_form",
            form_elements,
        )
    )

    feedback_context = {**context, "followup_text": followup_text[:1000]}
    secondary_buttons = [
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
    elements.append(_action_block(secondary_buttons))

    return {
        "config": {"wide_screen_mode": True},
        "header": _header(f"💬 {title}", color="blue"),
        "elements": elements,
    }


def build_knowledge_answer_card(
    *,
    title: str,
    answer: str,
    hits: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a card for knowledge-base QA answers."""
    elements: list[dict[str, Any]] = [
        _md_element(_truncate_card_body(answer, max_chars=1600)),
    ]
    valid_hits = [hit for hit in hits or [] if isinstance(hit, dict)]
    if valid_hits:
        source_lines: list[str] = []
        seen_sources: set[tuple[str, str]] = set()
        for hit in valid_hits:
            title_text = str(hit.get("title") or hit.get("chunk_id") or "").strip()
            source_ref = str(hit.get("source_ref") or "").strip()
            source_id = str(hit.get("source_id") or "").strip()
            display_title = title_text or source_id
            if not display_title:
                continue
            source_key = (display_title, source_ref)
            if source_key in seen_sources:
                continue
            seen_sources.add(source_key)
            line = f"{len(source_lines) + 1}. **{_truncate_summary(display_title, max_chars=80)}**"
            if source_ref:
                line += f"\n来源：`{_truncate_summary(source_ref, max_chars=120)}`"
            source_lines.append(line)
            if len(source_lines) >= 3:
                break
        if source_lines:
            elements.append(_divider())
            elements.append(_md_element("**参考来源**\n" + "\n".join(source_lines)))
    return {
        "config": {"wide_screen_mode": True},
        "header": _header(f"📚 {title}", color="blue"),
        "elements": elements,
    }


def build_confirmation_card(
    *,
    title: str,
    description: str,
    risk_level: str = "medium",
    action_id: str = "",
    metadata: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build a confirmation card for high-risk operations.

    Parameters
    ----------
    title:
        Card header title.
    description:
        What the operation will do.
    risk_level:
        ``low``, ``medium``, or ``high``.
    action_id:
        Identifier for the pending action.
    metadata:
        Extra context fields.
    """
    risk_labels = {"low": "🟢 低", "medium": "🟡 中", "high": "🔴 高"}
    risk_label = risk_labels.get(risk_level, risk_level)

    elements: list[dict[str, Any]] = [
        _md_element(f"**风险等级：** {risk_label}"),
        _md_element(description),
    ]

    if metadata:
        fields = [_field_element(k, v) for k, v in metadata.items()]
        elements.append(_fields_block(fields))

    elements.append(_divider())
    elements.append(
        _action_block([
            _button(
                "✅ 确认执行",
                value={"action": "approve", "request_id": action_id, "action_id": action_id},
                button_type="primary",
            ),
            _button(
                "❌ 取消",
                value={"action": "reject", "request_id": action_id, "action_id": action_id},
                button_type="danger",
            ),
        ])
    )

    return {
        "config": {"wide_screen_mode": True},
        "header": _header(f"⚠️ {title}", color="orange"),
        "elements": elements,
    }
