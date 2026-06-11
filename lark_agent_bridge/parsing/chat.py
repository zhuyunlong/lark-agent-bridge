"""基础聊天回复、Skill/感知/追问请求解析。"""

from __future__ import annotations

import re

from ..models import (
    ClaudeSkillRequest,
    PerceptionSummaryRequest,
)
from .patterns import (
    SIGNAL_CODE_RE,
    SIGNAL_ENUM_RE,
)
from .terms import (
    CLAUDE_TRIGGER_TERMS,
    FOLLOWUP_CONTINUE_TERMS,
    FOLLOWUP_RETRY_TERMS,
    GREETING_TERMS,
    HELP_TERMS,
    IDENTITY_TERMS,
    OMLX_CHAT_TERMS,
    PERCEPTION_TRIGGER_TERMS,
    TASK_TERMS,
)
from .textutils import (
    _contains_any,
    _keyword_variants,
    _strip_leading_mentions,
)
from .resources import (
    _find_resources,
)


def parse_followup_action(text: str) -> str:
    cleaned = _strip_leading_mentions(text or "").strip()
    lowered = cleaned.casefold()
    if (
        _contains_any(cleaned, lowered, FOLLOWUP_RETRY_TERMS)
        or re.search(r"^(?:再|重新).{0,12}(?:一次|一遍)$", cleaned)
        or re.search(r"^重新分析(?:下)?$", cleaned)
    ):
        return "retry"
    if _contains_any(cleaned, lowered, FOLLOWUP_CONTINUE_TERMS):
        return "continue"
    if cleaned:
        return "ask"
    return "unknown"


def build_basic_chat_reply(text: str, *, command_prefixes: list[str] | None = None) -> str | None:
    normalized_text = (text or "").strip()
    # Remove URLs (and the URL part of markdown links) before keyword matching so
    # URLs/paths cannot accidentally trigger greeting/help/identity intents.
    cleaned_text = re.sub(r"\[([^\]]*?)\]\((?:https?://|www\.)[^\s)]+\)", r"\1", normalized_text)
    cleaned_text = re.sub(r"https?://\S+", " ", cleaned_text)
    cleaned_text = re.sub(r"www\.\S+", " ", cleaned_text)
    cleaned_text = cleaned_text.strip()
    lowered = cleaned_text.casefold()

    if _contains_any(cleaned_text, lowered, IDENTITY_TERMS):
        return (
            "我是本地运行的 Lark Agent Bridge。"
            "我负责在飞书里接收消息、下载日志或附件，并调用本地分析脚本回传报告；"
            "现在支持 Bug 链接分析、需求链接源码分析、仓库源码分析、附件/URL 直传分析、"
            "信号链路分析、当前感知数据总结、"
            "个人知识库问答、历史报告续聊、普通聊天和帮助回复。\n"
            "发送 `help` 可以查看常用触发示例。"
        )

    if _contains_any(cleaned_text, lowered, HELP_TERMS):
        return (
            "常用触发方式（常见触发方式）：\n"
            "| 模式 | 怎么发 |\n"
            "| --- | --- |\n"
            "| Bug 分析 | `@机器人 https://project.feishu.cn/xpfailuremgmt/buglo/detail/6991604970 分析主题变化` |\n"
            "| Bug 卡顿/黑屏 | `@机器人 <bug链接> 调查3D卡顿黑屏` |\n"
            "| Bug 闪退 | `@机器人 <bug链接> 调查闪退 tombstone` |\n"
            "| Bug 信号链 | `@机器人 <bug链接> 分析132002为什么没到Unity` |\n"
            "| Bug XTheme | `@机器人 <bug链接> 分析 xtheme / 晨曦 / 主题切换` |\n"
            "| Bug 感知总结 | `@机器人 <bug链接> 总结当前感知数据` |\n"
            "| 需求源码分析 | `@机器人 https://project.feishu.cn/demo/story/detail/12345 结合源码分析是否可行` |\n"
            "| 仓库源码分析 | `@机器人 基于源码分析 UnityReady 信号链路如何监听` |\n"
            "| 附件/日志分析 | 回复文件、文件夹、压缩包或日志：`@机器人 分析启动和卡顿 file_xxx 11:30` |\n"
            "| 信号链分析 | `@机器人 /signal 132002 日志 https://.../Log.zip`，或 `@机器人 调查 SIGNAL_X3D_LD_NORMAL_OVER_ALL_DATA 日志 file_xxx`，触发 `signal-chain-analyzer` |\n"
            "| 个人知识库 | `@机器人 知识库 OTA信号如何模拟`、`@机器人 查知识 主题信号如何模拟`，/kb 仍兼容 |\n"
            "| ROM/导航版本 | `@机器人 XMARTM3EUD03E5_V6.2.2.6808_20260424002644.3_REV01_USERDEBUG 找下导航版本` |\n"
            "| 续聊/重跑 | 回复上一条报告卡片或报告文件：`@机器人 基于源码重新分析` |\n"
            "| 普通聊天 | 群里 `@机器人 /chat 讲个笑话`，私聊可直接提问 |\n"
            "\n"
            "只有明确的 Bug 链接、需求链接源码分析、仓库源码分析、日志/附件分析、信号/感知、知识库、ROM 查询等请求会进入卡片式处理；普通问答会直接文本回复。"
        )

    if _contains_any(cleaned_text, lowered, GREETING_TERMS):
        return (
            "你好，我是 Lark Agent Bridge。"
            "你可以直接发 `Bug链接 调查3D启动时序`、"
            "`分析启动和卡顿 file_xxx`、"
            "`调查 SIGNAL_X3D_LD_NORMAL_OVER_ALL_DATA 日志 file_xxx`、"
            "`总结当前感知数据 file_xxx`，"
            "`知识库 OTA信号如何模拟`，"
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
    prefixes = [] if trigger_prefixes is None else trigger_prefixes
    prompt = extract_first_keyword_payload(cleaned, prefixes) if prefixes else None
    if prompt is not None:
        return ClaudeSkillRequest(
            prompt=prompt,
            raw_text=normalized_text,
            triggered=True,
            error="missing_prompt" if not prompt else None,
        )

    if prefixes and _contains_any(cleaned, cleaned.casefold(), CLAUDE_TRIGGER_TERMS):
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
    prefixes = [] if trigger_prefixes is None else trigger_prefixes
    prompt = extract_first_keyword_payload(cleaned, prefixes) if prefixes else None
    if prompt is not None:
        return PerceptionSummaryRequest(
            prompt=prompt,
            resources=_find_resources(cleaned),
            raw_text=normalized_text,
            triggered=True,
            error="missing_prompt" if not prompt else None,
        )

    lowered = cleaned.casefold()
    if cleaned.startswith("/") and prompt is None:
        return PerceptionSummaryRequest(prompt="", resources=_find_resources(cleaned), raw_text=normalized_text, triggered=False)
    if _contains_any(cleaned, lowered, PERCEPTION_TRIGGER_TERMS):
        return PerceptionSummaryRequest(
            prompt=cleaned,
            resources=_find_resources(cleaned),
            raw_text=normalized_text,
            triggered=True,
            error=None if cleaned else "missing_prompt",
        )

    return PerceptionSummaryRequest(prompt="", resources=[], raw_text=normalized_text, triggered=False)


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
