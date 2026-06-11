"""Feishu card builders (facade over cards_ui/)."""

from __future__ import annotations

from .cards_ui.texts import (
    STATUS_LABELS,
    STATUS_COLORS,
    CARD_PROGRESS_PREVIEW_LIMIT,
    CARD_STREAM_PREVIEW_LIMIT,
    CARD_STREAM_MAX_LINE_CHARS,
    CARD_RESULT_NOTE_MAX_CHARS,
    CARD_INLINE_CODE_MAX_CHARS,
    HEADER_TEMPLATES,
)
from .cards_ui.elements import (
    _md_element,
    _field_element,
    _fields_block,
    _divider,
    _button,
    _action_block,
    _input_element,
    _form_block,
    _note_block,
    _header,
)
from .cards_ui.textutils import (
    _progress_lines,
    _stream_lines,
    _sanitize_code_block_text,
    _compact_stream_text,
    _progress_time_prefix,
    _format_token_usage,
    _truncate_summary,
    _truncate_card_body,
    _card_result_note_preview,
    _first_markdown_section_without_heading,
    _plain_inline_code_for_card,
    _compact_card_code_token,
    _compact_card_code_match,
)
from .cards_ui.choice_blocks import (
    _skill_choice_action_blocks,
    _valid_agent_choices,
    _agent_choice_action_blocks,
)
from .cards_ui.status_card import (
    build_status_card,
)
from .cards_ui.result_card import (
    build_result_card,
    build_agent_reanalysis_confirmation_card,
    build_followup_result_card,
    build_knowledge_answer_card,
    build_confirmation_card,
)
from .cards_ui.serialize import (
    card_to_json,
    build_text_fallback,
)

__all__ = [
    "STATUS_LABELS",
    "STATUS_COLORS",
    "CARD_PROGRESS_PREVIEW_LIMIT",
    "CARD_STREAM_PREVIEW_LIMIT",
    "CARD_STREAM_MAX_LINE_CHARS",
    "CARD_RESULT_NOTE_MAX_CHARS",
    "CARD_INLINE_CODE_MAX_CHARS",
    "HEADER_TEMPLATES",
    "_md_element",
    "_field_element",
    "_fields_block",
    "_divider",
    "_button",
    "_action_block",
    "_input_element",
    "_form_block",
    "_note_block",
    "_header",
    "_progress_lines",
    "_stream_lines",
    "_sanitize_code_block_text",
    "_compact_stream_text",
    "_progress_time_prefix",
    "_format_token_usage",
    "_truncate_summary",
    "_truncate_card_body",
    "_card_result_note_preview",
    "_first_markdown_section_without_heading",
    "_plain_inline_code_for_card",
    "_compact_card_code_token",
    "_compact_card_code_match",
    "_skill_choice_action_blocks",
    "_valid_agent_choices",
    "_agent_choice_action_blocks",
    "build_status_card",
    "build_result_card",
    "build_agent_reanalysis_confirmation_card",
    "build_followup_result_card",
    "build_knowledge_answer_card",
    "build_confirmation_card",
    "card_to_json",
    "build_text_fallback",
]
