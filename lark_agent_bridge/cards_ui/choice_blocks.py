"""Skill/Agent 选择按钮块（状态卡与追问卡共用）。"""

from __future__ import annotations

from typing import Any
from .elements import (
    _action_block,
    _button,
)


def _skill_choice_action_blocks(
    bug_skill_choices: list[dict[str, Any]],
    context: dict[str, str],
    *,
    row_size: int = 3,
) -> list[dict[str, Any]]:
    buttons: list[dict[str, Any]] = []
    for skill in bug_skill_choices[:8]:
        label = str(skill.get("label") or skill.get("name") or "").strip()
        name = str(skill.get("name") or "").strip()
        if not label or not name:
            continue
        buttons.append(
            _button(
                label,
                value={"action": "select_bug_skill", "skill_name": name, **context},
                button_type="primary",
            )
        )
    return [_action_block(buttons[index : index + row_size]) for index in range(0, len(buttons), row_size)]


def _valid_agent_choices(bug_agent_choices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    valid: list[dict[str, Any]] = []
    for agent in bug_agent_choices[:6]:
        provider = str(agent.get("provider") or agent.get("name") or "").strip()
        label = str(agent.get("label") or provider).strip()
        if not provider or not label:
            continue
        valid.append({"provider": provider, "label": label})
    return valid


def _agent_choice_action_blocks(
    bug_agent_choices: list[dict[str, Any]],
    context: dict[str, str],
    *,
    row_size: int = 3,
) -> list[dict[str, Any]]:
    buttons: list[dict[str, Any]] = []
    for agent in bug_agent_choices[:6]:
        provider = str(agent.get("provider") or agent.get("name") or "").strip()
        label = str(agent.get("label") or provider).strip()
        if not provider or not label:
            continue
        buttons.append(
            _button(
                label,
                value={"action": "select_bug_agent", "agent_provider": provider, **context},
                button_type="default",
            )
        )
    return [_action_block(buttons[index : index + row_size]) for index in range(0, len(buttons), row_size)]
