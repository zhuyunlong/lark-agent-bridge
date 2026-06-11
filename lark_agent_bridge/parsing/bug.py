"""Bug 链接请求解析。"""

from __future__ import annotations

import re

from ..models import (
    BugRequest,
)
from .patterns import (
    BUG_URL_RE,
)
from .terms import (
    TRAILING_URL_PUNCTUATION,
)
from .textutils import (
    _strip_bug_message_shell_prefix,
    _strip_leading_mentions,
)


def parse_bug_request(text: str, *, bug_url_re: re.Pattern[str] | None = None) -> BugRequest:
    normalized_text = text or ""
    cleaned = _strip_leading_mentions(normalized_text).strip()
    match = (bug_url_re or BUG_URL_RE).search(cleaned)
    if not match:
        return BugRequest(bug_url="", prompt="", raw_text=normalized_text, triggered=False)
    bug_url = match.group(0).rstrip(TRAILING_URL_PUNCTUATION)
    link_span = _markdown_link_span_for_url(cleaned, match.start(), match.end())
    if link_span is not None:
        prompt_source = f"{cleaned[:link_span[0]]}{cleaned[link_span[1]:]}"
    else:
        prompt_source = cleaned.replace(match.group(0), "", 1)
    prompt = _strip_bug_message_shell_prefix(prompt_source.strip(" \t\r\n，。；;"))
    return BugRequest(
        bug_url=bug_url,
        prompt=prompt,
        raw_text=normalized_text,
        triggered=True,
        error=None if bug_url else "missing_bug_url",
    )


def _markdown_link_span_for_url(text: str, url_start: int, url_end: int) -> tuple[int, int] | None:
    label_end = text.rfind("](", 0, url_start)
    if label_end < 0:
        return None
    if text[label_end + 2 : url_start].strip():
        return None
    link_end = url_end
    while link_end < len(text) and text[link_end].isspace():
        link_end += 1
    if link_end >= len(text) or text[link_end] != ")":
        return None
    depth = 0
    for index in range(label_end, -1, -1):
        char = text[index]
        if char == "]":
            depth += 1
        elif char == "[":
            depth -= 1
            if depth == 0:
                start = index - 1 if index > 0 and text[index - 1] == "!" else index
                return start, link_end + 1
    return None
