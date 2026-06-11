"""飞书卡片低级元素构建器。"""

from __future__ import annotations

from typing import Any


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
