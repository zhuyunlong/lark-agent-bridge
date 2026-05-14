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

    if details:
        fields = [_field_element(k, v) for k, v in details.items()]
        elements.append(_fields_block(fields))

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
                _button("🔄 重新分析", value={"action": "reanalyze", "job_id": job_id})
            )
        buttons.append(
            _button("🆘 升级人工", value={"action": "escalate", "job_id": job_id or ""})
        )
        if buttons:
            elements.append(_action_block(buttons))

    return {
        "config": {"wide_screen_mode": True},
        "header": _header(f"{'✅' if success else '❌'} {title}", color=color),
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
            _button("✅ 确认执行", value={"action": "approve", "action_id": action_id}, button_type="primary"),
            _button("❌ 取消", value={"action": "reject", "action_id": action_id}, button_type="danger"),
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

def _truncate_summary(text: str, *, max_chars: int = 800) -> str:
    """Truncate summary to fit in card, preserving line boundaries."""
    if len(text) <= max_chars:
        return text
    truncated = text[:max_chars]
    last_newline = truncated.rfind("\n")
    if last_newline > max_chars // 2:
        truncated = truncated[:last_newline]
    return truncated + "\n\n_(完整内容请查看报告)_"
