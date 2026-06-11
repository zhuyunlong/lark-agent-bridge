"""卡片状态标签、配色、标题模板与限额常量。"""

from __future__ import annotations



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


CARD_INLINE_CODE_MAX_CHARS = 40


HEADER_TEMPLATES: dict[str, str] = {
    "queued": "blue",
    "downloading": "blue",
    "analyzing": "blue",
    "completed": "green",
    "failed": "red",
}
