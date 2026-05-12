"""Parse Feishu message text into signal lifecycle requests and basic chat replies."""

from __future__ import annotations

import re

from .config import DEFAULT_SIGNAL_ALIASES
from .models import BugRequest, ClaudeSkillRequest, DirectAnalysisRequest, DownloadResource, PerceptionSummaryRequest, SignalRequest


URL_RE = re.compile(r"https?://[^\s<>\"]+")
BUG_URL_RE = re.compile(r"https?://(?:project\.feishu\.cn|(?:www\.)?meegle\.com)[^\s<>\"]*/buglo/detail/\d+[^\s<>\"]*")
SIGNAL_ENUM_RE = re.compile(r"\bSIGNAL_[A-Z0-9_]+\b")
SIGNAL_CODE_RE = re.compile(r"(?<!\d)\d{5,6}(?!\d)")
FILE_KEY_RE = re.compile(r"\bfile_[A-Za-z0-9_]+\b")
IMAGE_KEY_RE = re.compile(r"\bimg_[A-Za-z0-9_]+\b")
DATE_RANGE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\s+\d{1,2}\s*-\s*\d{1,2}\b")
HOUR_RANGE_RE = re.compile(r"(?<!\d)(\d{1,2})\s*-\s*(\d{1,2})\s*点")

TRIGGER_TERMS = (
    "信号生命周期",
    "调查信号",
    "有没有到 Unity",
    "有没有到Unity",
    "生命周期",
)

TRAILING_URL_PUNCTUATION = "，。；;,.、)）]】}"
IDENTITY_TERMS = (
    "你是谁",
    "你是誰",
    "你是什么",
    "你是干嘛的",
    "who are you",
    "what are you",
)
HELP_TERMS = (
    "help",
    "/help",
    "帮助",
    "怎么用",
    "如何用",
    "你会什么",
    "你能做什么",
)
GREETING_TERMS = (
    "你好",
    "您好",
    "hi",
    "hello",
    "在吗",
    "在嘛",
)
CLAUDE_TRIGGER_TERMS = (
    "用claude code分析",
    "用 Claude Code 分析",
    "让claude code分析",
    "让 Claude Code 分析",
    "claude code 帮我分析",
    "Claude Code 帮我分析",
    "skill分析",
    "skill 分析",
    "技能分析",
)
PERCEPTION_TRIGGER_TERMS = (
    "当前感知数据",
    "感知数据总结",
    "perception summary",
    "perception-summary",
    "感知统计",
    "vhalhelper",
    "mapdatahandler",
    "x3dcb",
    "xdatanativeproxy",
)
OMLX_CHAT_TERMS = (
    "是什么",
    "为什么",
    "怎么",
    "如何",
    "能不能",
    "可以",
    "解释",
    "介绍",
    "翻译",
    "写一段",
    "帮我想",
    "聊聊",
    "what",
    "why",
    "how",
    "can you",
    "could you",
)
TASK_TERMS = (
    "日志",
    "排查",
    "分析",
    "报告",
    "仓库",
    "代码",
    "文件",
    "执行",
    "命令",
    "权限",
)
DIRECT_ANALYSIS_TERMS = (
    "分析",
    "排查",
    "调查",
    "总结",
    "启动",
    "卡顿",
    "黑屏",
    "闪退",
    "crash",
    "tombstone",
    "信号",
    "感知数据",
)


def parse_signal_request(
    text: str,
    *,
    signal_aliases: dict[str, str] | None = None,
    command_prefixes: list[str] | None = None,
) -> SignalRequest:
    aliases = signal_aliases or DEFAULT_SIGNAL_ALIASES
    prefixes = command_prefixes or ["/signal"]
    normalized_text = text or ""

    triggered = any(prefix in normalized_text for prefix in prefixes) or any(
        term in normalized_text for term in TRIGGER_TERMS
    )
    signal = _find_signal(normalized_text, aliases)
    resources = _find_resources(normalized_text)
    since = _find_since(normalized_text)
    error = "missing_signal" if triggered and not signal else None
    if signal:
        triggered = True

    return SignalRequest(
        signal=signal,
        resources=resources,
        since=since,
        raw_text=normalized_text,
        triggered=triggered,
        error=error,
    )


def build_basic_chat_reply(text: str, *, command_prefixes: list[str] | None = None) -> str | None:
    normalized_text = (text or "").strip()
    lowered = normalized_text.casefold()
    primary_prefix = (command_prefixes or ["/signal"])[0]

    if _contains_any(normalized_text, lowered, IDENTITY_TERMS):
        return (
            "我是本地运行的 Lark Agent Bridge。"
            "我负责在飞书里接收消息、下载日志或附件，并调用本地分析脚本回传报告；"
            "现在支持 signal 调查、bug 链接分析、附件直传通用分析、当前感知数据总结、普通聊天和帮助回复。"
        )

    if _contains_any(normalized_text, lowered, HELP_TERMS):
        return (
            "我现在支持：\n"
            f"1. 信号生命周期调查：`{primary_prefix} 132002 日志 https://...`\n"
            "2. 使用枚举名或别名，例如 `SIGNAL_X3D_LD_NORMAL_OVER_ALL_DATA`、`LD normal`\n"
            "3. 飞书附件或 URL 直传通用分析，例如 `分析启动和卡顿 file_xxx 11:30`\n"
            "4. 飞书项目 bug 链接分析，例如 `https://project.feishu.cn/.../buglo/detail/... 调查3D启动时序`\n"
            "5. bug 链接 + 当前感知数据总结，例如 `https://project.feishu.cn/.../buglo/detail/... 总结当前感知数据`\n"
            "6. 感知数据总结，例如 `/perception-summary 总结当前感知数据 file_xxx`\n"
            "7. Claude Code 只读分析，例如 `/skill 分析这个目录`\n"
            "8. 群里简单聊天，例如 `@机器人 /chat 讲个笑话`；私聊可直接提问\n"
            "9. 基础问答，例如 `你好`、`你是谁`、`帮助`"
        )

    if _contains_any(normalized_text, lowered, GREETING_TERMS):
        return (
            "你好，我是 Lark Agent Bridge。"
            f"你可以直接发 `{primary_prefix} 132002 日志 https://...`、"
            "`分析启动和卡顿 file_xxx`、"
            "`/perception-summary 总结当前感知数据 file_xxx`，"
            "也可以问我“你是谁”或“帮助”。"
        )

    return None


def parse_claude_skill_request(
    text: str,
    *,
    trigger_prefixes: list[str] | None = None,
) -> ClaudeSkillRequest:
    normalized_text = text or ""
    cleaned = _strip_leading_mentions(normalized_text).strip()
    prefixes = trigger_prefixes or ["/skill", "/claude"]
    prompt = extract_first_keyword_payload(cleaned, prefixes)
    if prompt is not None:
        return ClaudeSkillRequest(
            prompt=prompt,
            raw_text=normalized_text,
            triggered=True,
            error="missing_prompt" if not prompt else None,
        )

    if _contains_any(cleaned, cleaned.casefold(), CLAUDE_TRIGGER_TERMS):
        return ClaudeSkillRequest(
            prompt=cleaned,
            raw_text=normalized_text,
            triggered=True,
            error=None if cleaned else "missing_prompt",
        )

    return ClaudeSkillRequest(prompt="", raw_text=normalized_text, triggered=False)


def parse_perception_summary_request(
    text: str,
    *,
    trigger_prefixes: list[str] | None = None,
) -> PerceptionSummaryRequest:
    normalized_text = text or ""
    cleaned = _strip_leading_mentions(normalized_text).strip()
    prefixes = trigger_prefixes or ["/perception-summary", "/perception"]
    prompt = extract_first_keyword_payload(cleaned, prefixes)
    if prompt is not None:
        return PerceptionSummaryRequest(
            prompt=prompt,
            resources=_find_resources(cleaned),
            raw_text=normalized_text,
            triggered=True,
            error="missing_prompt" if not prompt else None,
        )

    lowered = cleaned.casefold()
    if _contains_any(cleaned, lowered, PERCEPTION_TRIGGER_TERMS):
        return PerceptionSummaryRequest(
            prompt=cleaned,
            resources=_find_resources(cleaned),
            raw_text=normalized_text,
            triggered=True,
            error=None if cleaned else "missing_prompt",
        )

    return PerceptionSummaryRequest(prompt="", resources=[], raw_text=normalized_text, triggered=False)


def parse_direct_analysis_request(text: str) -> DirectAnalysisRequest:
    normalized_text = text or ""
    cleaned = _strip_leading_mentions(normalized_text).strip()
    resources = _find_resources(cleaned)
    lowered = cleaned.casefold()
    if not resources:
        return DirectAnalysisRequest(prompt="", resources=[], raw_text=normalized_text, triggered=False)
    if not _contains_any(cleaned, lowered, DIRECT_ANALYSIS_TERMS):
        return DirectAnalysisRequest(prompt="", resources=resources, raw_text=normalized_text, triggered=False)
    return DirectAnalysisRequest(
        prompt=cleaned,
        resources=resources,
        raw_text=normalized_text,
        triggered=True,
        error=None if cleaned else "missing_prompt",
    )


def parse_bug_request(text: str) -> BugRequest:
    normalized_text = text or ""
    cleaned = _strip_leading_mentions(normalized_text).strip()
    match = BUG_URL_RE.search(cleaned)
    if not match:
        return BugRequest(bug_url="", prompt="", raw_text=normalized_text, triggered=False)
    bug_url = match.group(0).rstrip(TRAILING_URL_PUNCTUATION)
    prompt = (cleaned.replace(match.group(0), "", 1)).strip(" \t\r\n，。；;")
    return BugRequest(
        bug_url=bug_url,
        prompt=prompt,
        raw_text=normalized_text,
        triggered=True,
        error=None if bug_url else "missing_bug_url",
    )


def should_use_omlx_chat(text: str, *, max_chars: int = 2000) -> bool:
    normalized_text = _strip_leading_mentions(text or "").strip()
    if not normalized_text or len(normalized_text) > max_chars:
        return False
    if normalized_text.startswith("/"):
        return False
    if _find_resources(normalized_text):
        return False
    if SIGNAL_ENUM_RE.search(normalized_text) or SIGNAL_CODE_RE.search(normalized_text):
        return False
    lowered = normalized_text.casefold()
    if _contains_any(normalized_text, lowered, TASK_TERMS) and not _contains_any(
        normalized_text, lowered, OMLX_CHAT_TERMS
    ):
        return False
    return (
        normalized_text.endswith(("?", "？"))
        or _contains_any(normalized_text, lowered, OMLX_CHAT_TERMS)
        or _contains_any(normalized_text, lowered, GREETING_TERMS)
    )


def extract_first_keyword_payload(text: str, keywords: list[str] | tuple[str, ...]) -> str | None:
    cleaned = (text or "").strip()
    if not cleaned:
        return None
    parts = cleaned.split(maxsplit=1)
    first = parts[0]
    rest = parts[1].strip() if len(parts) > 1 else ""
    lowered_first = first.casefold()
    for keyword in keywords:
        if lowered_first in _keyword_variants(keyword):
            return rest
    return None


def _find_signal(text: str, aliases: dict[str, str]) -> str | None:
    enum_match = SIGNAL_ENUM_RE.search(text)
    if enum_match:
        return enum_match.group(0)
    code_match = SIGNAL_CODE_RE.search(text)
    if code_match:
        return code_match.group(0)
    lowered = text.casefold()
    for alias, signal in sorted(aliases.items(), key=lambda item: len(item[0]), reverse=True):
        if alias.casefold() in lowered:
            return signal
    return None


def _find_resources(text: str) -> list[DownloadResource]:
    resources: list[DownloadResource] = []
    for match in URL_RE.findall(text):
        resources.append(DownloadResource(kind="url", value=match.rstrip(TRAILING_URL_PUNCTUATION)))
    for match in FILE_KEY_RE.findall(text):
        resources.append(DownloadResource(kind="file", value=match))
    for match in IMAGE_KEY_RE.findall(text):
        resources.append(DownloadResource(kind="image", value=match))
    return resources


def _find_since(text: str) -> str | None:
    date_match = DATE_RANGE_RE.search(text)
    if date_match:
        return re.sub(r"\s*-\s*", "-", date_match.group(0))
    hour_match = HOUR_RANGE_RE.search(text)
    if hour_match:
        return f"{hour_match.group(1)}-{hour_match.group(2)}"
    return None


def _contains_any(original: str, lowered: str, terms: tuple[str, ...]) -> bool:
    return any(term in original or term.casefold() in lowered for term in terms)


def _strip_leading_mentions(text: str) -> str:
    cleaned = text.strip()
    cleaned = re.sub(r"^(?:<at\s+[^>]+></at>\s*)+", "", cleaned).strip()
    cleaned = re.sub(r"^(?:@\S+\s*)+", "", cleaned).strip()
    return cleaned


def _keyword_variants(keyword: str) -> set[str]:
    base = keyword.strip().lstrip("/").casefold()
    if not base:
        return {keyword.casefold()}
    return {base, f"/{base}"}
