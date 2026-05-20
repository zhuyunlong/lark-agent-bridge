"""Feishu interactive card builder for structured bot replies.

Builds card JSON payloads compatible with lark-cli ``--card`` flag.
Cards replace plain-text replies with rich, structured messages that show
status, analysis type, links, and action buttons.

Card types
----------
- **Status card** – progress indicator during analysis (queued / downloading /
  analyzing / completed / failed).
- **Result card** – final analysis result with report link, summary, metadata,
  and action buttons (open report, re-analyze, escalate).
- **Confirmation card** – asks user to approve a high-risk operation before
  execution.

Usage example::

    from lark_agent_bridge.cards import build_status_card, build_result_card

    card_json = build_status_card(
        title="信号生命周期分析",
        status="analyzing",
        details={"signal": "SIGNAL_X3D_LD_NORMAL_OVER_ALL_DATA"},
    )
"""

from __future__ import annotations

from datetime import datetime
import json
from typing import Any


# ---------------------------------------------------------------------------
# Status definitions
# ---------------------------------------------------------------------------

STATUS_LABELS: dict[str, str] = {
    "queued": "⏳ 排队中",
    "downloading": "⬇️ 下载中",
    "analyzing": "🔍 分析中",
    "completed": "✅ 已完成",
    "failed": "❌ 失败",
}

STATUS_COLORS: dict[str, str] = {
    "queued": "orange",
    "downloading": "blue",
    "analyzing": "blue",
    "completed": "green",
    "failed": "red",
}

CARD_PROGRESS_PREVIEW_LIMIT = 4
CARD_STREAM_PREVIEW_LIMIT = 3
CARD_STREAM_MAX_LINE_CHARS = 120
CARD_RESULT_NOTE_MAX_CHARS = 520

HEADER_TEMPLATES: dict[str, str] = {
    "queued": "blue",
    "downloading": "blue",
    "analyzing": "blue",
    "completed": "green",
    "failed": "red",
}


# ---------------------------------------------------------------------------
# Low-level element builders
# ---------------------------------------------------------------------------

def _md_element(content: str) -> dict[str, Any]:
    """A Markdown text block."""
    return {
        "tag": "div",
        "text": {"tag": "lark_md", "content": content},
    }


def _field_element(label: str, value: str) -> dict[str, Any]:
    """A single field (label + value) as a Markdown column."""
    return {
        "is_short": True,
        "text": {"tag": "lark_md", "content": f"**{label}**\n{value}"},
    }


def _fields_block(fields: list[dict[str, Any]]) -> dict[str, Any]:
    """A multi-column field block."""
    return {"tag": "div", "fields": fields}


def _divider() -> dict[str, Any]:
    return {"tag": "hr"}


def _button(text: str, *, url: str | None = None, value: dict[str, Any] | None = None, button_type: str = "default") -> dict[str, Any]:
    btn: dict[str, Any] = {
        "tag": "button",
        "text": {"tag": "lark_md", "content": text},
        "type": button_type,
    }
    if url:
        btn["url"] = url
    if value:
        btn["value"] = value
    return btn


def _action_block(buttons: list[dict[str, Any]]) -> dict[str, Any]:
    return {"tag": "action", "actions": buttons}


def _input_element(
    name: str,
    *,
    placeholder: str,
    default_value: str = "",
    required: bool = False,
) -> dict[str, Any]:
    element: dict[str, Any] = {
        "tag": "input",
        "name": name,
        "required": required,
        "placeholder": {"tag": "plain_text", "content": placeholder},
    }
    if default_value:
        element["default_value"] = default_value
    return element


def _form_block(name: str, elements: list[dict[str, Any]]) -> dict[str, Any]:
    return {"tag": "form", "name": name, "elements": elements}


def _note_block(text: str) -> dict[str, Any]:
    return {
        "tag": "note",
        "elements": [{"tag": "lark_md", "content": text}],
    }


def _header(title: str, *, color: str = "blue") -> dict[str, Any]:
    return {
        "title": {"tag": "plain_text", "content": title},
        "template": color,
    }


# ---------------------------------------------------------------------------
# Public card builders
# ---------------------------------------------------------------------------

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
        elements.append(_md_element(f"**{heading}**\n{_truncate_summary(result_note, max_chars=CARD_RESULT_NOTE_MAX_CHARS)}"))
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

    if show_followup_actions and (job_id or root_message_id):
        context = {
            "job_id": job_id or "",
            "root_message_id": root_message_id or "",
        }
        elements.append(
            _note_block(
                "提示：先输入追问或重跑要求，再选择回答/重跑/续 Agent；输入为空时不会执行。"
            )
        )
        elements.append(
            _form_block(
                "followup_prompt_form",
                [
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
                ],
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


def build_followup_result_card(
    *,
    title: str,
    summary: str,
    report_url: str | None = None,
    root_message_id: str | None = None,
    job_id: str | None = None,
    followup_text: str = "",
    answer_confidence: float | None = None,
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
    if report_url:
        elements.append(_action_block([_button("📄 打开报告", url=report_url, button_type="primary")]))
    elements.append(
        _form_block(
            "followup_prompt_form",
            [
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
            ],
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


def card_to_json(card: dict[str, Any]) -> str:
    """Serialize a card dict to compact JSON for ``lark-cli --card``."""
    return json.dumps(card, ensure_ascii=False, separators=(",", ":"))


def build_text_fallback(card: dict[str, Any]) -> str:
    """Extract a plain-text fallback from a card for clients that don't render cards."""
    header = card.get("header", {})
    title = header.get("title", {}).get("content", "")
    parts = [title] if title else []
    for element in card.get("elements", []):
        if element.get("tag") == "div":
            text = element.get("text", {})
            if isinstance(text, dict):
                content = text.get("content", "")
                if content:
                    parts.append(content)
            fields = element.get("fields", [])
            for f in fields:
                if isinstance(f, dict):
                    ft = f.get("text", {})
                    if isinstance(ft, dict):
                        parts.append(ft.get("content", ""))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _progress_lines(progress: list[dict[str, Any]], *, limit: int = 6) -> list[str]:
    lines: list[str] = []
    normal_items = [
        item
        for item in progress
        if isinstance(item, dict)
        and not str(item.get("stage") or "").strip().startswith("bug_agent_summary_stream")
    ]
    for item in normal_items[-limit:]:
        if not isinstance(item, dict):
            continue
        stage = str(item.get("stage") or "progress").strip()
        time_prefix = _progress_time_prefix(item.get("timestamp"))
        message = str(item.get("message") or "").strip()
        details = item.get("details")
        suffix = ""
        if isinstance(details, dict):
            executor = details.get("executor")
            provider = details.get("provider")
            if executor:
                suffix = f" ({executor})"
            elif provider:
                suffix = f" ({provider})"
        prefix = f"{time_prefix} " if time_prefix else ""
        if message:
            lines.append(f"- {prefix}`{stage}`: {message}{suffix}")
        else:
            lines.append(f"- {prefix}`{stage}`{suffix}")
    return lines


def _stream_lines(progress: list[dict[str, Any]], *, limit: int = 6, max_line_chars: int = 180) -> list[str]:
    lines: list[str] = []
    for item in progress:
        if not isinstance(item, dict):
            continue
        stage = str(item.get("stage") or "").strip()
        if not stage.startswith("bug_agent_summary_stream"):
            continue
        details = item.get("details")
        stream_text = ""
        if isinstance(details, dict):
            stream_text = str(details.get("stream_preview") or details.get("stream_text") or "").strip()
        if not stream_text:
            stream_text = str(item.get("message") or "").strip()
        if not stream_text:
            continue
        time_prefix = _progress_time_prefix(item.get("timestamp"))
        stream_text = _compact_stream_text(_sanitize_code_block_text(stream_text))
        if not stream_text:
            continue
        if len(stream_text) > max_line_chars:
            stream_text = stream_text[: max_line_chars - 1].rstrip() + "…"
        lines.append(f"{time_prefix} {stream_text}".strip())
    return lines[-limit:]


def _sanitize_code_block_text(value: str) -> str:
    return value.replace("```", "'''")


def _compact_stream_text(value: str) -> str:
    text = " ".join(value.split())
    if not text:
        return ""
    if text == "error 已完成":
        return ""
    if text == "command_execution 已完成":
        return ""
    marker = "item.started:"
    if marker not in text:
        return text
    payload = text.split(marker, 1)[1].strip()
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return "开始执行工具调用"
    item = data.get("item") if isinstance(data, dict) else None
    if not isinstance(item, dict):
        return "开始执行工具调用"
    item_type = str(item.get("type") or "").strip()
    if item_type == "command_execution":
        command = str(item.get("command") or "").strip()
        command = command.replace("\\n", " ")
        if command.startswith('/bin/zsh -lc "'):
            command = command[len('/bin/zsh -lc "') :].rstrip('"')
        return f"工具调用：执行命令 {command}" if command else "工具调用：执行命令"
    if item_type:
        return f"开始处理 {item_type}"
    return "开始执行工具调用"


def _progress_time_prefix(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        timestamp = datetime.fromisoformat(normalized)
    except ValueError:
        return raw[:8] if len(raw) >= 8 else raw
    if timestamp.tzinfo is not None:
        timestamp = timestamp.astimezone()
    return timestamp.strftime("%H:%M:%S")


def _format_token_usage(token_usage: dict[str, int]) -> str:
    input_tokens = token_usage.get("input_tokens")
    output_tokens = token_usage.get("output_tokens")
    total_tokens = token_usage.get("total_tokens")
    parts: list[str] = []
    if isinstance(input_tokens, int):
        parts.append(f"输入 {input_tokens}")
    if isinstance(output_tokens, int):
        parts.append(f"输出 {output_tokens}")
    if isinstance(total_tokens, int):
        parts.append(f"总计 {total_tokens}")
    return " / ".join(parts) if parts else ""


def _truncate_summary(text: str, *, max_chars: int = 800) -> str:
    """Truncate summary to fit in card, preserving line boundaries."""
    if len(text) <= max_chars:
        return text
    truncated = text[:max_chars]
    last_newline = truncated.rfind("\n")
    if last_newline > max_chars // 2:
        truncated = truncated[:last_newline]
    return truncated + "\n\n_(完整内容请查看报告)_"
