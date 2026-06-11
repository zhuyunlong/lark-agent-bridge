"""卡片文本处理：进度行、流式摘要、截断与代码压缩。"""

from __future__ import annotations

from datetime import datetime
import json
import re
from typing import Any
from .texts import (
    CARD_INLINE_CODE_MAX_CHARS,
)


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
    cached_input_tokens = token_usage.get("cached_input_tokens")
    output_tokens = token_usage.get("output_tokens")
    total_tokens = token_usage.get("total_tokens")
    parts: list[str] = []
    if isinstance(input_tokens, int):
        parts.append(f"输入 {input_tokens}")
    if isinstance(cached_input_tokens, int):
        parts.append(f"缓存命中输入 {cached_input_tokens}")
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


def _truncate_card_body(text: str, *, max_chars: int = 1600) -> str:
    cleaned = (text or "").strip()
    if len(cleaned) <= max_chars:
        return cleaned
    truncated = cleaned[:max_chars]
    last_newline = truncated.rfind("\n")
    if last_newline >= max_chars // 2:
        truncated = truncated[:last_newline]
    return truncated.rstrip() + "\n\n(内容较长，已截断显示)"


def _card_result_note_preview(text: str, *, max_chars: int) -> str:
    """Build a readable Feishu-card preview for analysis result markdown."""
    cleaned = _first_markdown_section_without_heading(text)
    cleaned = _plain_inline_code_for_card(cleaned)
    return _truncate_summary(cleaned, max_chars=max_chars)


def _first_markdown_section_without_heading(text: str) -> str:
    lines = [line.rstrip() for line in text.splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    section: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not section and re.match(r"^#{1,6}\s+", stripped):
            continue
        if section and re.match(r"^#{1,6}\s+", stripped):
            break
        section.append(line)
    return "\n".join(section).strip() or text.strip()


def _plain_inline_code_for_card(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        return _compact_card_code_token(match.group(1))

    return re.sub(r"`([^`\n]+)`", replace, text)


def _compact_card_code_token(token: str) -> str:
    value = token.strip()
    if len(value) <= CARD_INLINE_CODE_MAX_CHARS:
        return value
    if " " in value or "/" in value:
        return re.sub(r"[A-Z0-9]+(?:_[A-Z0-9]+){3,}(?:\([^)]+\))?", _compact_card_code_match, value)
    if len(value) <= 72 and not re.fullmatch(r"[A-Z0-9_()]+", value):
        return value
    parts = [part for part in value.split("_") if part]
    if len(parts) >= 4:
        prefix = "_".join(parts[:3])
        suffix = parts[-1]
        compact = f"{prefix}_..._{suffix}"
        if len(compact) <= 64:
            return compact
    head = max(16, CARD_INLINE_CODE_MAX_CHARS // 2)
    tail = max(12, CARD_INLINE_CODE_MAX_CHARS - head - 3)
    return f"{value[:head]}...{value[-tail:]}"


def _compact_card_code_match(match: re.Match[str]) -> str:
    return _compact_card_code_token(match.group(0))
