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
    note: str | None = None,
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

    progress_lines = _progress_lines(progress or [])
    if progress_lines:
        elements.append(_divider())
        elements.append(_md_element("**后台进度**\n" + "\n".join(progress_lines)))

    if report_url:
        elements.append(_divider())
        elements.append(_action_block([_button("📄 打开报告", url=report_url, button_type="primary")]))

    if note:
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
    metadata: dict[str, str] = {"处理方式": "基于当前上下文追问"}
    if answer_confidence is not None:
        metadata["置信度"] = f"{answer_confidence:.2f}"

    elements: list[dict[str, Any]] = []
    elements.append(_fields_block([_field_element(k, v) for k, v in metadata.items()]))
    elements.append(_divider())
    elements.append(_md_element(_truncate_summary(summary, max_chars=800)))

    context = {
        "job_id": job_id or "",
        "root_message_id": root_message_id or "",
        "followup_text": followup_text[:1000],
    }
    primary_buttons: list[dict[str, Any]] = []
    if report_url:
        primary_buttons.append(_button("📄 打开报告", url=report_url, button_type="primary"))
    primary_buttons.append(
        _button(
            "📌 基于当前报告回答",
            value={"action": "answer_from_report", **context},
        )
    )
    primary_buttons.append(
        _button(
            "🔄 基于已有日志重新分析",
            value={"action": "reanalyze", **context},
            button_type="primary",
        )
    )
    elements.append(_action_block(primary_buttons))

    secondary_buttons = [
        _button(
            "🧠 继续原 Agent",
            value={"action": "continue_agent", **context},
        ),
        _button(
            "👍 有用",
            value={"action": "feedback_helpful", **context},
        ),
        _button(
            "👎 不准",
            value={"action": "feedback_unhelpful", **context},
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
    for item in progress[-limit:]:
        if not isinstance(item, dict):
            continue
        stage = str(item.get("stage") or "progress").strip()
        message = str(item.get("message") or "").strip()
        details = item.get("details")
        suffix = ""
        if isinstance(details, dict):
            provider = details.get("provider")
            if provider:
                suffix = f" ({provider})"
        if message:
            lines.append(f"- `{stage}`: {message}{suffix}")
        else:
            lines.append(f"- `{stage}`{suffix}")
    return lines


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
