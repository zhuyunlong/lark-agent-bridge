"""Parse Feishu message text into signal lifecycle requests and basic chat replies."""

from __future__ import annotations

import re

from .config import DEFAULT_SIGNAL_ALIASES
from .models import (
    Addr2LineRequest,
    BugRequest,
    ClaudeSkillRequest,
    DirectAnalysisRequest,
    DownloadResource,
    PerceptionSummaryRequest,
    RomVersionLookupRequest,
    SignalRequest,
)
from .signal_resolver import SignalResolver


URL_RE = re.compile(r"https?://[^\s<>\"]+")
DRIVE_FOLDER_URL_RE = re.compile(r"https?://[^\s<>\"]*/drive/folder/(?P<token>[^/?#\s<>\"]+)")
_DEFAULT_BUG_URL_DOMAINS = ("project.feishu.cn", "meegle.com")


def build_bug_url_re(domains: tuple[str, ...] | list[str] = _DEFAULT_BUG_URL_DOMAINS) -> re.Pattern[str]:
    """Build a bug URL regex from a list of domain patterns."""
    escaped = [re.escape(d) if "." in d else d for d in domains]
    # Allow optional www. prefix for domains that don't already specify it
    parts = [f"(?:www\\.)?{d}" if not d.startswith("www\\.") and not d.startswith("(?:") else d for d in escaped]
    domain_alt = "|".join(parts)
    return re.compile(
        rf"https?://(?:{domain_alt})[^\s<>\"]*/buglo/detail/\d+"
        r"(?:[/?#][^\s<>\"，。；;、)）\]】}]*)?"
    )


BUG_URL_RE = build_bug_url_re()
SIGNAL_ENUM_RE = re.compile(r"(?<![A-Za-z0-9_])SIGNAL_[A-Z0-9_]+(?![A-Za-z0-9_])")
SIGNAL_BARE_NAME_RE = re.compile(r"(?<![A-Za-z0-9_])([A-Z][A-Z0-9]+(?:_[A-Z0-9]+)+)(?![A-Za-z0-9_])")
SIGNAL_CODE_RE = re.compile(r"(?<![A-Za-z0-9_])\d{5,6}(?![A-Za-z0-9_])")
ROM_VERSION_RE = re.compile(
    r"(?<![A-Za-z0-9_])"
    r"([A-Z0-9]+_V\d+\.\d+\.\d+(?:\.\d+)?_\d{14}(?:\.\d+)?_[A-Z0-9]+_[A-Z]+(?:_[A-Za-z0-9]+)?)"
    r"(?![A-Za-z0-9_])"
)
APK_VERSION_RE = re.compile(
    r"(?<![A-Za-z0-9_])"
    r"(V\d+\.\d+\.\d+(?:\.\d+)?_\d{14}(?:\.\d+)?_[A-Za-z0-9]+)"
    r"(?![A-Za-z0-9_])"
)
NAPA_VERSION_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])"
    r"(\d+\.\d+\.\d+-\d{14}-[A-Za-z0-9_.-]+)"
    r"(?![A-Za-z0-9_.-])"
)
TOMBSTONE_PC_RE = re.compile(r"#\d+\s+pc\s+[0-9a-fA-F]{8,16}\s+\S*lib[\w.-]+\.so\b", re.I)
SO_ADDR_RE = re.compile(r"\blib[\w.-]+\.so\b|0x[0-9a-fA-F]{4,}|\bpc\s+[0-9a-fA-F]{8,16}\b", re.I)
ADDR2LINE_ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{4,}|\bpc\s+[0-9a-fA-F]{8,16}\b", re.I)
FILE_KEY_RE = re.compile(r"\bfile_[A-Za-z0-9_-]+\b")
IMAGE_KEY_RE = re.compile(r"\bimg_[A-Za-z0-9_-]+\b")
FOLDER_XML_RE = re.compile(r"<folder\b[^>]*\b(?:folder_token|token)=\"(?P<token>[A-Za-z0-9_-]+)\"", re.I)
DATE_RANGE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\s+\d{1,2}\s*-\s*\d{1,2}\b")
HOUR_RANGE_RE = re.compile(r"(?<!\d)(\d{1,2})\s*-\s*(\d{1,2})\s*点")

TRIGGER_TERMS = (
    "信号生命周期",
    "调查信号",
    "信号链",
    "信号链路",
    "有没有到 Unity",
    "有没有到Unity",
    "生命周期",
)
SIGNAL_CONTEXT_TERMS = (
    "信号",
    "signal",
    "Signal",
    "生命周期",
    "链路",
    "有没有到 Unity",
    "有没有到Unity",
)
ROM_LOOKUP_TERMS = (
    "rom-version",
    "rom version",
    "ROM版本",
    "rom版本",
    "导航版本",
    "APK版本",
    "apk版本",
    "查打包版本",
    "查导航版本",
    "Maven",
    "maven",
    "Jenkins",
    "jenkins",
    "Napa",
    "napa",
    "符号表",
)
ADDR2LINE_TERMS = (
    "addr2line",
    "地址反解",
    "反解地址",
    "反解符号表",
    "反解导航符号表",
    "分解符号表",
    "分解导航符号表",
    "符号表分解",
    "符号表反解",
    "堆栈解析",
    "解析堆栈",
    "反解堆栈",
    "反解crash",
    "反解 crash",
    "crash分析",
    "crash 分析",
    "tombstone",
)
STACK_ANALYSIS_ACTION_RE = re.compile(
    r"(?:反解|解析|分析|解码|定位|还原|符号化|帮(?:我)?看(?:下|一下)?|看(?:下|一下)?)",
    re.I,
)
STACK_ANALYSIS_OBJECT_RE = re.compile(
    r"(?:堆栈|调用栈|backtrace|stack|tombstone|crash|addr2line|lib[\w.-]+\.so)",
    re.I,
)
STACK_ANALYSIS_CONTEXT_RE = re.compile(
    r"(?:unity|3d|导航|envirodrive|montecarlo|libunity)",
    re.I,
)
SYMBOLISH_RE = re.compile(r"(?:symbol|符.{0,2}(?:号|合|表))", re.I)

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
FOLLOWUP_RETRY_TERMS = (
    "重试",
    "重试一次",
    "再来一次",
    "重新跑",
    "重新执行",
    "再跑一次",
    "重跑",
)
FOLLOWUP_CONTINUE_TERMS = (
    "继续",
    "继续分析",
    "接着查",
    "继续查",
)
DIRECT_ANALYSIS_ACTION_TERMS = (
    "分析",
    "排查",
    "调查",
    "定位",
    "检查",
    "看下",
    "看一下",
    "看看",
    "查下",
    "查一下",
    "帮我看",
    "帮我分析",
)
DIRECT_ANALYSIS_DOMAIN_TERMS = (
    "日志",
    "附件",
    "文件",
    "压缩包",
    "目录",
    "启动",
    "卡顿",
    "黑屏",
    "闪退",
    "crash",
    "tombstone",
    "堆栈",
    "3d",
    "unity",
    "源码",
    "超速",
    "状态",
)
DIRECT_ANALYSIS_DEICTIC_RE = re.compile(
    r"^(?:看下|看一下|看看|帮我看(?:下|一下)?|分析(?:下|一下)?|排查(?:下|一下)?|调查(?:下|一下)?|查(?:下|一下)?)"
    r"(?:这个|这份|这个文件|这个日志|这份日志|这个附件|这份附件)?[？?]?$"
)


def parse_signal_request(
    text: str,
    *,
    signal_aliases: dict[str, str] | None = None,
    command_prefixes: list[str] | None = None,
    signal_resolver: SignalResolver | None = None,
) -> SignalRequest:
    aliases = signal_aliases or DEFAULT_SIGNAL_ALIASES
    normalized_text = text or ""
    cleaned = _strip_leading_mentions(normalized_text).strip()
    prefixes = [] if command_prefixes is None else command_prefixes
    prefix_prompt = extract_first_keyword_payload(cleaned, prefixes) if prefixes else None
    unsupported_slash_command = cleaned.startswith("/") and prefix_prompt is None

    triggered = prefix_prompt is not None or (not unsupported_slash_command and any(term in normalized_text for term in TRIGGER_TERMS))
    signal = _find_signal(normalized_text, aliases, signal_resolver=signal_resolver)
    resources = _find_resources(normalized_text)
    since = _find_since(normalized_text)
    error = "missing_signal" if triggered and not signal else None
    if signal and not unsupported_slash_command:
        triggered = True

    return SignalRequest(
        signal=signal,
        resources=resources,
        since=since,
        raw_text=normalized_text,
        triggered=triggered,
        error=error,
    )


def parse_rom_version_lookup_request(text: str) -> RomVersionLookupRequest:
    normalized_text = text or ""
    cleaned = _strip_leading_mentions(normalized_text).strip()
    match = ROM_VERSION_RE.search(cleaned)
    if not match:
        return RomVersionLookupRequest(rom_version="", raw_text=normalized_text, triggered=False)
    lowered = cleaned.casefold()
    if not _contains_any(cleaned, lowered, ROM_LOOKUP_TERMS):
        return RomVersionLookupRequest(rom_version=match.group(1), raw_text=normalized_text, triggered=False)
    prompt = f"{cleaned[:match.start()]}{cleaned[match.end():]}".strip()
    return RomVersionLookupRequest(
        rom_version=match.group(1),
        prompt=prompt,
        raw_text=normalized_text,
        triggered=True,
    )


def parse_addr2line_request(text: str, *, allow_missing_address: bool = False) -> Addr2LineRequest:
    normalized_text = text or ""
    cleaned = _strip_leading_mentions(normalized_text).strip()
    lowered = cleaned.casefold()
    has_stack_payload = TOMBSTONE_PC_RE.search(cleaned) is not None
    has_addr_payload = ADDR2LINE_ADDRESS_RE.search(cleaned) is not None and ("lib" in lowered or "pc " in lowered)
    has_trigger = _looks_like_addr2line_intent(
        cleaned,
        lowered,
        has_stack_payload=has_stack_payload,
        has_addr_payload=has_addr_payload,
        allow_missing_address=allow_missing_address,
    )
    if not (has_trigger and (has_stack_payload or has_addr_payload or allow_missing_address)):
        return Addr2LineRequest(addr_text="", raw_text=normalized_text, triggered=False)

    rom_match = ROM_VERSION_RE.search(cleaned)
    apk_match = APK_VERSION_RE.search(cleaned)
    napa_match = NAPA_VERSION_RE.search(cleaned)
    target = _detect_addr2line_target(cleaned)
    error = None
    if not _has_addr2line_address(cleaned):
        error = "missing_address"
    elif rom_match is None and apk_match is None and napa_match is None:
        error = "missing_symbol_version"
    return Addr2LineRequest(
        addr_text=_extract_addr2line_payload(cleaned),
        raw_text=normalized_text,
        rom_version=rom_match.group(1) if rom_match else "",
        napa_version=napa_match.group(1) if napa_match else "",
        apk_version=apk_match.group(1) if apk_match else "",
        target=target,
        prompt=cleaned,
        triggered=True,
        error=error,
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
    lowered = normalized_text.casefold()

    if _contains_any(normalized_text, lowered, IDENTITY_TERMS):
        return (
            "我是本地运行的 Lark Agent Bridge。"
            "我负责在飞书里接收消息、下载日志或附件，并调用本地分析脚本回传报告；"
            "现在支持 Bug 链接分析、附件/URL 直传分析、信号链路分析、当前感知数据总结、"
            "个人知识库问答、历史报告续聊、普通聊天和帮助回复。\n"
            "发送 `help` 可以查看常用触发示例。"
        )

    if _contains_any(normalized_text, lowered, HELP_TERMS):
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
            "| 附件/日志分析 | 回复文件、文件夹、压缩包或日志：`@机器人 分析启动和卡顿 file_xxx 11:30` |\n"
            "| 信号链分析 | `@机器人 /signal 132002 日志 https://.../Log.zip`，或 `@机器人 调查 SIGNAL_X3D_LD_NORMAL_OVER_ALL_DATA 日志 file_xxx`，触发 `signal-chain-analyzer` |\n"
            "| 个人知识库 | `@机器人 知识库 OTA信号如何模拟`、`@机器人 查知识 主题信号如何模拟`，/kb 仍兼容 |\n"
            "| ROM/导航版本 | `@机器人 XMARTM3EUD03E5_V6.2.2.6808_20260424002644.3_REV01_USERDEBUG 找下导航版本` |\n"
            "| 续聊/重跑 | 回复上一条报告卡片或报告文件：`@机器人 基于源码重新分析` |\n"
            "| 普通聊天 | 群里 `@机器人 /chat 讲个笑话`，私聊可直接提问 |\n"
            "\n"
            "只有明确的 Bug 链接、日志/附件分析、信号/感知、知识库、ROM 查询等请求会进入卡片式处理；普通问答会直接文本回复。"
        )

    if _contains_any(normalized_text, lowered, GREETING_TERMS):
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


def parse_direct_analysis_request(text: str) -> DirectAnalysisRequest:
    normalized_text = text or ""
    cleaned = _strip_leading_mentions(normalized_text).strip()
    resources = _find_resources(cleaned)
    if not resources:
        return DirectAnalysisRequest(prompt="", resources=[], raw_text=normalized_text, triggered=False)
    if not looks_like_direct_analysis_prompt(cleaned, resources_present=True):
        return DirectAnalysisRequest(prompt="", resources=resources, raw_text=normalized_text, triggered=False)
    return DirectAnalysisRequest(
        prompt=cleaned,
        resources=resources,
        raw_text=normalized_text,
        triggered=True,
        error=None if cleaned else "missing_prompt",
    )


def looks_like_direct_analysis_prompt(
    text: str, *, resources_present: bool = False, bug_url_re: re.Pattern[str] | None = None
) -> bool:
    normalized_text = text or ""
    cleaned = _strip_leading_mentions(normalized_text).strip()
    lowered = cleaned.casefold()
    if not cleaned:
        return False
    if build_basic_chat_reply(cleaned) is not None:
        return False
    if parse_bug_request(cleaned, bug_url_re=bug_url_re).triggered:
        return False
    has_action = _contains_any(cleaned, lowered, DIRECT_ANALYSIS_ACTION_TERMS)
    has_domain = _contains_any(cleaned, lowered, DIRECT_ANALYSIS_DOMAIN_TERMS)
    if resources_present:
        return has_action or bool(DIRECT_ANALYSIS_DEICTIC_RE.fullmatch(cleaned))
    return has_action and has_domain


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
    prompt = prompt_source.strip(" \t\r\n，。；;")
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


def _find_signal(text: str, aliases: dict[str, str], *, signal_resolver: SignalResolver | None = None) -> str | None:
    enum_match = SIGNAL_ENUM_RE.search(text)
    if enum_match:
        return _resolve_signal_value(enum_match.group(0), signal_resolver)
    bare_match = SIGNAL_BARE_NAME_RE.search(text)
    if bare_match and _is_bare_signal_candidate(bare_match.group(1), text):
        resolved_bare = _resolve_bare_signal_name(bare_match.group(1), signal_resolver)
        if resolved_bare:
            return resolved_bare
    lowered = text.casefold()
    for alias, signal in sorted(aliases.items(), key=lambda item: len(item[0]), reverse=True):
        if alias.casefold() in lowered:
            return signal
    code_match = SIGNAL_CODE_RE.search(_signal_code_search_text(text))
    if code_match:
        return _resolve_signal_value(code_match.group(0), signal_resolver)
    return None


def _is_bare_signal_candidate(value: str, text: str) -> bool:
    # Build/version identifiers such as XMART..._V6 are not signal names even if
    # fuzzy source search can map them to a nearby enum by accident.
    if ROM_VERSION_RE.search(text):
        return False
    if value.count("_") >= 2:
        return True
    return _has_signal_context(text)


def _has_signal_context(text: str) -> bool:
    lowered = text.casefold()
    return _contains_any(text, lowered, SIGNAL_CONTEXT_TERMS)


def _signal_code_search_text(text: str) -> str:
    without_urls = URL_RE.sub(" ", text)
    return re.sub(r"#[0-9A-Fa-f]{3,8}\b", " ", without_urls)


def _resolve_signal_value(value: str, signal_resolver: SignalResolver | None) -> str:
    if signal_resolver is None:
        return value
    resolved = signal_resolver.resolve(value)
    return resolved.signal if resolved is not None else value


def _resolve_bare_signal_name(value: str, signal_resolver: SignalResolver | None) -> str | None:
    if signal_resolver is None:
        return None
    resolved = signal_resolver.resolve(value)
    return resolved.signal if resolved is not None else None


def find_resources(text: str, *, source_message_id: str = "") -> list[DownloadResource]:
    resources: list[DownloadResource] = []
    for match in URL_RE.findall(text):
        value = match.rstrip(TRAILING_URL_PUNCTUATION)
        folder_match = DRIVE_FOLDER_URL_RE.match(value)
        if folder_match:
            resources.append(DownloadResource(kind="folder", value=folder_match.group("token"), source_message_id=source_message_id))
            continue
        resources.append(DownloadResource(kind="url", value=value, source_message_id=source_message_id))
    for match in FILE_KEY_RE.findall(text):
        resources.append(DownloadResource(kind="file", value=match, source_message_id=source_message_id))
    for match in IMAGE_KEY_RE.findall(text):
        resources.append(DownloadResource(kind="image", value=match, source_message_id=source_message_id))
    for match in FOLDER_XML_RE.finditer(text):
        resources.append(DownloadResource(kind="folder", value=match.group("token"), source_message_id=source_message_id))
    return resources


def _find_resources(text: str) -> list[DownloadResource]:
    return find_resources(text)


def _find_since(text: str) -> str | None:
    date_match = DATE_RANGE_RE.search(text)
    if date_match:
        return re.sub(r"\s*-\s*", "-", date_match.group(0))
    hour_match = HOUR_RANGE_RE.search(text)
    if hour_match:
        return f"{hour_match.group(1)}-{hour_match.group(2)}"
    return None


def _detect_addr2line_target(text: str) -> str:
    lowered = text.casefold()
    if "renderextend" in lowered or "librenderextend.so" in lowered:
        return "renderextend"
    if "libxdata_native.so" in lowered or "xdata native" in lowered:
        return "xdata_native"
    if "libxdata_client.so" in lowered or "xdata client" in lowered:
        return "xdata_client"
    if "libxdata_sdk.so" in lowered or "xdata sdk" in lowered:
        return "xdata_sdk"
    if "xdata" in lowered:
        return "xdata"
    if "所有符号" in text or "全部符号" in text or "all targets" in lowered:
        return "all"
    return "auto"


def _looks_like_addr2line_intent(
    text: str,
    lowered: str,
    *,
    has_stack_payload: bool,
    has_addr_payload: bool,
    allow_missing_address: bool,
) -> bool:
    if has_stack_payload:
        return True
    if _contains_any(text, lowered, ADDR2LINE_TERMS):
        return True

    has_action = STACK_ANALYSIS_ACTION_RE.search(text) is not None
    has_object = STACK_ANALYSIS_OBJECT_RE.search(text) is not None
    has_context = STACK_ANALYSIS_CONTEXT_RE.search(text) is not None
    has_symbolish = SYMBOLISH_RE.search(text) is not None

    if has_action and (has_object or has_symbolish or has_addr_payload):
        return True
    if allow_missing_address and has_object and has_context:
        return True
    if has_addr_payload and (has_object or has_context):
        return True
    return False


def _extract_addr2line_payload(text: str) -> str:
    lines = [line.strip() for line in text.splitlines()]
    stack_lines = [line for line in lines if TOMBSTONE_PC_RE.search(line)]
    if stack_lines:
        return "\n".join(stack_lines)
    if SO_ADDR_RE.search(text):
        return text
    return ""


def _has_addr2line_address(text: str) -> bool:
    return ADDR2LINE_ADDRESS_RE.search(text) is not None


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
