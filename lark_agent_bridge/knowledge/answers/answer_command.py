"""命令查询识别与命令命中类答案。"""

from __future__ import annotations

import json
from typing import Any
from ..models import KnowledgeChunk, SearchHit


_COMMAND_MARKERS = ("adb ", "fastboot ")


def _is_command_lookup_question(question: str) -> bool:
    lowered = question.casefold()
    command_terms = ("命令", "指令", "adb", "broadcast", "fastboot")
    return any(term in lowered for term in command_terms) or any(term in question for term in command_terms)


def _command_hits_answer(command_hits: list[tuple[SearchHit, list[str]]]) -> str:
    lines = ["知识库命中可执行命令："]
    for index, (hit, commands) in enumerate(command_hits, start=1):
        command_lines = "\n".join(commands[:3])
        lines.append(f"{index}. {hit.title}\n{command_lines}")
    return "\n\n".join(lines)


def _extract_command_lines(content: str) -> list[str]:
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        parsed = None
    raw_values = _flatten_string_values(parsed) if parsed is not None else [content]

    commands: list[str] = []
    for value in raw_values:
        for line in str(value).splitlines():
            command = _extract_command_from_line(line)
            if not command or command in commands:
                continue
            commands.append(command)
    return commands


def _flatten_string_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        values: list[str] = []
        for item in value.values():
            values.extend(_flatten_string_values(item))
        return values
    if isinstance(value, list):
        values = []
        for item in value:
            values.extend(_flatten_string_values(item))
        return values
    return []


def _extract_command_from_line(line: str) -> str:
    text = line.strip().strip("`")
    lowered = text.casefold()
    positions = [lowered.find(marker) for marker in _COMMAND_MARKERS if lowered.find(marker) >= 0]
    if not positions:
        return ""
    command = text[min(positions) :].strip()
    return command.rstrip("`,")
