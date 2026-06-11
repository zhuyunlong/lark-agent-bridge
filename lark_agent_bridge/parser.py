"""Parse Feishu message text into signal lifecycle requests and basic chat replies."""

from __future__ import annotations

import re

from .config import DEFAULT_SIGNAL_ALIASES
from .models import (
    AppServerInvestigationRequest,
    Addr2LineRequest,
    BugRequest,
    ClaudeSkillRequest,
    DirectAnalysisRequest,
    DownloadResource,
    PerceptionSummaryRequest,
    RequirementAnalysisRequest,
    RequirementWorkItemRef,
    ReportFollowupRequest,
    RomVersionLookupRequest,
    SignalRequest,
    SourceAnalysisRequest,
)
from .signal_resolver import SignalResolver


URL_RE = re.compile(r"https?://[^\s<>\"]+")
DRIVE_FOLDER_URL_RE = re.compile(r"https?://[^\s<>\"]*/drive/folder/(?P<token>[^/?#\s<>\"]+)")
PROJECT_WORKITEM_URL_RE = re.compile(
    r"https?://project\.feishu\.cn/"
    r"(?P<project_key>[^/\s<>\"]+)/"
    r"(?P<work_item_type>story|task|issue|requirement)/detail/"
    r"(?P<work_item_id>\d+)"
    r"(?:[/?#][^\s<>\"，。；;、)）\]】}]*)?",
    re.I,
)
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
BARE_SO_STACK_RE = re.compile(
    r"(?m)(?:^|\s)(?:#\d+\s+)?(?<![0-9a-fA-F-])[0-9a-fA-F]{8,16}(?![0-9a-fA-F])\s+\S*lib[\w.-]+\.so\b",
    re.I,
)
TOMBSTONE_MAP_LINE_RE = re.compile(r"^\s*[0-9a-fA-F]+-[0-9a-fA-F]+\s+[r-][w-][x-][ps]\s", re.I)
SO_ADDR_RE = re.compile(r"\blib[\w.-]+\.so\b|0x[0-9a-fA-F]{4,}|\bpc\s+[0-9a-fA-F]{8,16}\b", re.I)
ADDR2LINE_ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{4,}|\bpc\s+[0-9a-fA-F]{8,16}\b", re.I)
LOG_FOLDER_RE = re.compile(r"(?<![A-Za-z0-9_])(log\d+)(?![A-Za-z0-9_])", re.I)
FILE_KEY_RE = re.compile(r"\bfile_[A-Za-z0-9_-]+\b")
IMAGE_KEY_RE = re.compile(r"\bimg_[A-Za-z0-9_-]+\b")
FILE_XML_RE = re.compile(r"<file\b[^>]*>", re.I)
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
    "感知数据链路",
    "感知链路",
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
APP_SERVER_INVESTIGATION_AUTO_TERMS = ("auto模式", "auto")
APP_SERVER_INVESTIGATION_FREE_TERMS = ("全技能自主分析", "全技能分析", "自主分析")
SOURCE_ANALYSIS_TERMS = (
    "源码",
    "源代码",
    "基于源码",
    "根据源码",
    "从源码",
    "看源码",
    "读源码",
    "查源码",
    "源码分析",
    "源码调查",
    "source code",
)
SOURCE_ANALYSIS_ACTION_TERMS = (
    "分析",
    "调查",
    "定位",
    "解释",
    "梳理",
    "看下",
    "查下",
    "链路",
    "流程",
    "时序",
    "监听",
    "注册",
    "分发",
    "调用",
    "怎么",
    "如何",
)
SOURCE_ANALYSIS_CHAIN_TERMS = (
    "链路",
    "流程",
    "时序",
    "数据流",
    "泳道",
    "监听",
    "注册",
    "分发",
    "调用",
    "回调",
)
REPORT_FOLLOWUP_TERMS = (
    "HTML 报告",
    "html 报告",
    "HTML输出",
    "HTML 输出",
    "输出报告",
    "整理成 HTML",
    "整理成html",
    "泳道图",
    "时序图",
    "流程图",
    "数据流图",
    "链路图",
    "画图",
    "画出",
)
REQUIREMENT_ANALYSIS_TERMS = (
    "需求",
    "需求链接",
    "需求分析",
    "story",
)
REQUIREMENT_ANALYSIS_ACTION_TERMS = (
    "结合源码",
    "基于源码",
    "源码分析",
    "源代码",
    "源码对比",
    "可行",
    "可行性",
    "实现方案",
)
SOURCE_IDENTIFIER_RE = re.compile(
    r"(?<![A-Za-z0-9_])("
    r"SIGNAL_[A-Z0-9_]+"
    r"|[A-Za-z_][A-Za-z0-9_]*(?:Ready|Service|Manager|Handler|Signal|Flow|Msg|Message|Callback|Listener|State|Context|Adapter|ViewModel|Repository|Controller|Biz|Proxy)[A-Za-z0-9_]*"
    r"|[a-z_]+[A-Z][A-Za-z0-9_]*"
    r")(?![A-Za-z0-9_])"
)

_SOURCE_TARGET_STOP_WORDS = {
    "source",
    "code",
    "html",
    "unity",
    "signal",
}


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
    has_bare_stack_payload = _has_bare_so_stack(cleaned)
    has_stack_payload = TOMBSTONE_PC_RE.search(cleaned) is not None or has_bare_stack_payload
    has_addr_payload = (ADDR2LINE_ADDRESS_RE.search(cleaned) is not None or has_bare_stack_payload) and (
        "lib" in lowered or "pc " in lowered
    )
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
        log_folder=_extract_addr2line_log_folder(cleaned),
        fault_time=_extract_addr2line_fault_time(cleaned),
        target=target,
        prompt=cleaned,
        triggered=True,
        error=error,
    )


def _extract_addr2line_log_folder(text: str) -> str:
    match = LOG_FOLDER_RE.search(text or "")
    if not match:
        return ""
    return match.group(1).casefold()


def _extract_addr2line_fault_time(text: str) -> str:
    normalized = _normalize_fault_time_text(text or "")
    full_match = re.search(
        r"(20\d{2})-(\d{1,2})-(\d{1,2})\s*(\d{1,2}):(\d{2})(?::(\d{2}))?",
        normalized,
    )
    if full_match:
        seconds = full_match.group(6)
        base = (
            f"{int(full_match.group(1)):04d}-{int(full_match.group(2)):02d}-{int(full_match.group(3)):02d} "
            f"{int(full_match.group(4)):02d}:{int(full_match.group(5)):02d}"
        )
        return f"{base}:{int(seconds):02d}" if seconds is not None else base
    month_day_match = re.search(
        r"(?<!\d)(\d{1,2})-(\d{1,2})\s*(\d{1,2}):(\d{2})(?::(\d{2}))?(?!\d)",
        normalized,
    )
    if month_day_match:
        seconds = month_day_match.group(5)
        base = (
            f"{int(month_day_match.group(1)):02d}-{int(month_day_match.group(2)):02d} "
            f"{int(month_day_match.group(3)):02d}:{int(month_day_match.group(4)):02d}"
        )
        return f"{base}:{int(seconds):02d}" if seconds is not None else base
    if not re.search(r"(?:时间点|时间|故障时间|问题时间|发生时间)", normalized):
        return ""
    short_match = re.search(r"(?<!\d)(\d{1,2}):(\d{2})(?::(\d{2}))?(?!\d)", normalized)
    if not short_match:
        return ""
    seconds = short_match.group(3)
    base = f"{int(short_match.group(1)):02d}:{int(short_match.group(2)):02d}"
    return f"{base}:{int(seconds):02d}" if seconds is not None else base


def _normalize_fault_time_text(value: str) -> str:
    normalized = (
        value.strip()
        .replace("：", ":")
        .replace("年", "-")
        .replace("月", "-")
        .replace("日", " ")
        .replace("/", "-")
        .replace("_", " ")
        .replace("点", ":")
        .replace("时", ":")
        .replace("分", "")
    )
    return re.sub(r"\s+", " ", normalized)


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


def parse_app_server_investigation_request(
    text: str,
    *,
    bug_url_re: re.Pattern[str] | None = None,
    auto_terms: tuple[str, ...] | list[str] = APP_SERVER_INVESTIGATION_AUTO_TERMS,
    free_terms: tuple[str, ...] | list[str] = APP_SERVER_INVESTIGATION_FREE_TERMS,
) -> AppServerInvestigationRequest:
    normalized_text = text or ""
    cleaned = _strip_leading_mentions(normalized_text).strip()
    match = _app_server_investigation_trigger(cleaned, auto_terms=auto_terms, free_terms=free_terms)
    if match is None:
        return AppServerInvestigationRequest(prompt="", raw_text=normalized_text, triggered=False)
    stripped = match["remaining"].strip()
    bug_request = parse_bug_request(stripped, bug_url_re=bug_url_re)
    resources = _find_resources(stripped)
    if bug_request.triggered:
        resources = [
            item for item in resources
            if not (item.kind == "url" and item.value.rstrip(TRAILING_URL_PUNCTUATION) == bug_request.bug_url)
        ]
    prompt = bug_request.prompt if bug_request.triggered else stripped
    prompt = re.sub(r"\s+", " ", prompt).strip()
    return AppServerInvestigationRequest(
        prompt=prompt,
        bug_url=bug_request.bug_url if bug_request.triggered else "",
        resources=resources,
        raw_text=normalized_text,
        triggered=True,
        error=None,
        trigger_mode=str(match["mode"]),
        trigger_term=str(match["term"]),
    )


def parse_requirement_analysis_request(
    text: str,
    *,
    bug_url_re: re.Pattern[str] | None = None,
) -> RequirementAnalysisRequest:
    normalized_text = text or ""
    cleaned = _strip_leading_mentions(normalized_text).strip()
    empty_ref = RequirementWorkItemRef(url="", project_key="", work_item_type="", work_item_id="")
    if parse_bug_request(cleaned, bug_url_re=bug_url_re).triggered:
        return RequirementAnalysisRequest(prompt="", workitem=empty_ref, raw_text=normalized_text, triggered=False)
    match = _project_workitem_match(cleaned)
    if match is None:
        return RequirementAnalysisRequest(prompt="", workitem=empty_ref, raw_text=normalized_text, triggered=False)
    lowered = cleaned.casefold()
    if not _contains_any(cleaned, lowered, REQUIREMENT_ANALYSIS_TERMS):
        return RequirementAnalysisRequest(prompt="", workitem=empty_ref, raw_text=normalized_text, triggered=False)
    if not _contains_any(cleaned, lowered, REQUIREMENT_ANALYSIS_ACTION_TERMS):
        return RequirementAnalysisRequest(prompt="", workitem=empty_ref, raw_text=normalized_text, triggered=False)
    ref = RequirementWorkItemRef(
        url=match.group(0).rstrip(TRAILING_URL_PUNCTUATION),
        project_key=match.group("project_key"),
        work_item_type=match.group("work_item_type").lower(),
        work_item_id=match.group("work_item_id"),
    )
    return RequirementAnalysisRequest(
        prompt=cleaned,
        workitem=ref,
        raw_text=normalized_text,
        triggered=True,
        source_mode="requirement_source",
        diagram_kinds=_diagram_kinds_from_text(cleaned, default_for_chain=True),
        output_html=True,
        reason="feishu_project_workitem_source_analysis",
    )


def parse_source_analysis_request(
    text: str,
    *,
    bug_url_re: re.Pattern[str] | None = None,
) -> SourceAnalysisRequest:
    normalized_text = text or ""
    cleaned = _strip_leading_mentions(normalized_text).strip()
    resources = _find_resources(cleaned)
    if resources:
        return SourceAnalysisRequest(prompt="", raw_text=normalized_text, triggered=False)
    if parse_bug_request(cleaned, bug_url_re=bug_url_re).triggered:
        return SourceAnalysisRequest(prompt="", raw_text=normalized_text, triggered=False)
    target = _source_analysis_target_from_text(cleaned)
    if not looks_like_source_analysis_prompt(cleaned, bug_url_re=bug_url_re, resources_present=False):
        return SourceAnalysisRequest(prompt="", target=target, raw_text=normalized_text, triggered=False)
    return SourceAnalysisRequest(
        prompt=cleaned,
        target=target,
        raw_text=normalized_text,
        triggered=True,
        error=None if target else "missing_source_target",
        source_mode="repository_only",
        diagram_kinds=_diagram_kinds_from_text(cleaned, default_for_chain=True),
        output_html=True,
        reason="explicit_source_request_without_bug_or_resource",
    )


def parse_report_followup_request(text: str) -> ReportFollowupRequest:
    normalized_text = text or ""
    cleaned = _strip_leading_mentions(normalized_text).strip()
    if not looks_like_report_or_diagram_request(cleaned):
        return ReportFollowupRequest(prompt="", raw_text=normalized_text, triggered=False)
    return ReportFollowupRequest(
        prompt=cleaned,
        raw_text=normalized_text,
        triggered=True,
        diagram_kinds=_diagram_kinds_from_text(cleaned, default_for_chain=True),
        output_html=True,
        reason="explicit_report_or_diagram_followup",
    )


def looks_like_source_analysis_prompt(
    text: str,
    *,
    bug_url_re: re.Pattern[str] | None = None,
    resources_present: bool = False,
) -> bool:
    normalized_text = text or ""
    cleaned = _strip_leading_mentions(normalized_text).strip()
    if not cleaned:
        return False
    if resources_present or _find_resources(cleaned):
        return False
    if parse_bug_request(cleaned, bug_url_re=bug_url_re).triggered:
        return False
    if build_basic_chat_reply(cleaned) is not None:
        return False
    lowered = cleaned.casefold()
    has_source = _contains_any(cleaned, lowered, SOURCE_ANALYSIS_TERMS)
    has_action = _contains_any(cleaned, lowered, SOURCE_ANALYSIS_ACTION_TERMS)
    if not has_source or not has_action:
        return False
    return bool(_source_analysis_target_from_text(cleaned))


def looks_like_report_or_diagram_request(text: str) -> bool:
    cleaned = _strip_leading_mentions(text or "").strip()
    if not cleaned:
        return False
    return _contains_any(cleaned, cleaned.casefold(), REPORT_FOLLOWUP_TERMS)


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
    if _project_workitem_match(cleaned) is not None:
        return False
    has_action = _contains_any(cleaned, lowered, DIRECT_ANALYSIS_ACTION_TERMS)
    has_domain = _contains_any(cleaned, lowered, DIRECT_ANALYSIS_DOMAIN_TERMS)
    if resources_present:
        return has_action or bool(DIRECT_ANALYSIS_DEICTIC_RE.fullmatch(cleaned))
    return has_action and has_domain


def _source_analysis_target_from_text(text: str) -> str:
    cleaned = URL_RE.sub(" ", _strip_leading_mentions(text or "")).strip()
    for match in SOURCE_IDENTIFIER_RE.finditer(cleaned):
        candidate = match.group(1).strip("`'\"“”‘’")
        if not candidate:
            continue
        if candidate.casefold() in _SOURCE_TARGET_STOP_WORDS:
            continue
        return candidate
    chinese_target = _source_analysis_chinese_target(cleaned)
    return chinese_target


def _project_workitem_match(text: str) -> re.Match[str] | None:
    cleaned = _strip_leading_mentions(text or "").strip()
    return PROJECT_WORKITEM_URL_RE.search(cleaned)


def _source_analysis_chinese_target(text: str) -> str:
    normalized = text
    for term in (*SOURCE_ANALYSIS_TERMS, *SOURCE_ANALYSIS_ACTION_TERMS, *REPORT_FOLLOWUP_TERMS):
        normalized = normalized.replace(term, " ")
    normalized = re.sub(r"[\s,，。；;：:、!?？!()\[\]【】{}<>`'\"“”‘’]+", " ", normalized)
    for token in normalized.split():
        stripped = token.strip()
        if len(stripped) < 2:
            continue
        if stripped.casefold() in _SOURCE_TARGET_STOP_WORDS:
            continue
        if stripped in {"信号", "链路", "流程", "时序", "报告", "源码", "源代码"}:
            continue
        return stripped[:80]
    return ""


def _app_server_investigation_trigger(
    text: str,
    *,
    auto_terms: tuple[str, ...] | list[str],
    free_terms: tuple[str, ...] | list[str],
) -> dict[str, str] | None:
    cleaned = text.strip()
    if not cleaned:
        return None
    auto_pattern = _independent_term_pattern(auto_terms, leading_only=True)
    auto_match = auto_pattern.match(cleaned)
    if auto_match:
        return {
            "mode": "auto",
            "term": auto_match.group("term"),
            "remaining": cleaned[auto_match.end("term") :].strip(),
        }
    free_pattern = _independent_term_pattern(free_terms, leading_only=False)
    free_match = free_pattern.search(cleaned)
    if not free_match:
        return None
    remaining = f"{cleaned[: free_match.start('term')]} {cleaned[free_match.end('term') :]}"
    return {
        "mode": "free",
        "term": free_match.group("term"),
        "remaining": re.sub(r"\s+", " ", remaining).strip(),
    }


def _independent_term_pattern(terms: tuple[str, ...] | list[str], *, leading_only: bool) -> re.Pattern[str]:
    unique_terms = [term.strip() for term in terms if str(term or "").strip()]
    unique_terms.sort(key=len, reverse=True)
    if not unique_terms:
        return re.compile(r"$^")
    alternation = "|".join(re.escape(term) for term in unique_terms)
    if leading_only:
        return re.compile(rf"^(?P<term>{alternation})(?=\s|$)", re.I)
    return re.compile(rf"(^|\s)(?P<term>{alternation})(?=\s|$)")


def _diagram_kinds_from_text(text: str, *, default_for_chain: bool = False) -> list[str]:
    cleaned = text or ""
    lowered = cleaned.casefold()
    kinds: list[str] = []

    def add(kind: str) -> None:
        if kind not in kinds:
            kinds.append(kind)

    if "泳道" in cleaned:
        add("swimlane")
    if "时序" in cleaned or "sequence" in lowered:
        add("sequence")
    if "数据流" in cleaned or "data flow" in lowered:
        add("data_flow")
    if "流程" in cleaned or "process" in lowered:
        add("process")
    if "链路" in cleaned or "调用链" in cleaned or "chain" in lowered:
        add("chain")
    if default_for_chain and _contains_any(cleaned, lowered, SOURCE_ANALYSIS_CHAIN_TERMS):
        add("swimlane")
    return kinds


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
    file_keys_from_tags: set[str] = set()
    for match in FILE_XML_RE.finditer(text):
        tag = match.group(0)
        key = _extract_xml_attr(tag, "key")
        if not key or not FILE_KEY_RE.fullmatch(key):
            continue
        file_keys_from_tags.add(key)
        resources.append(
            DownloadResource(
                kind="file",
                value=key,
                source_message_id=source_message_id,
                display_name=_extract_xml_attr(tag, "name"),
            )
        )
    for match in FILE_KEY_RE.findall(text):
        if match in file_keys_from_tags:
            continue
        resources.append(DownloadResource(kind="file", value=match, source_message_id=source_message_id))
    for match in IMAGE_KEY_RE.findall(text):
        resources.append(DownloadResource(kind="image", value=match, source_message_id=source_message_id))
    for match in FOLDER_XML_RE.finditer(text):
        resources.append(DownloadResource(kind="folder", value=match.group("token"), source_message_id=source_message_id))
    return resources


def _find_resources(text: str) -> list[DownloadResource]:
    return find_resources(text)


def _extract_xml_attr(tag: str, attr: str) -> str:
    match = re.search(rf"\b{re.escape(attr)}=\"([^\"]+)\"", tag, re.I)
    return match.group(1).strip() if match else ""


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
    # Explicit source-code investigation ("源码分析 找crash原因") is not a
    # tombstone stack reverse lookup. Without a real address/backtrace payload,
    # defer to the source/direct analysis route instead of claiming it here on
    # the weak "分析"+"crash" heuristic below.
    if not has_addr_payload and _contains_any(text, lowered, SOURCE_ANALYSIS_TERMS):
        return False

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
    stack_lines = [
        line
        for line in lines
        if not _is_tombstone_map_line(line) and (TOMBSTONE_PC_RE.search(line) or BARE_SO_STACK_RE.search(line))
    ]
    if stack_lines:
        return "\n".join(stack_lines)
    if ADDR2LINE_ADDRESS_RE.search(text) and SO_ADDR_RE.search(text):
        return text
    return ""


def _has_addr2line_address(text: str) -> bool:
    return ADDR2LINE_ADDRESS_RE.search(text) is not None or _has_bare_so_stack(text)


def _has_bare_so_stack(text: str) -> bool:
    return any(
        not _is_tombstone_map_line(line) and BARE_SO_STACK_RE.search(line)
        for line in text.splitlines()
    )


def _is_tombstone_map_line(line: str) -> bool:
    return TOMBSTONE_MAP_LINE_RE.search(line or "") is not None


_ASCII_TERM_RE_CACHE: dict[str, "re.Pattern[str]"] = {}


def _ascii_word_pattern(term: str) -> "re.Pattern[str]":
    """Compile (and cache) a word-boundary regex for an ASCII term.

    Word boundary matching prevents short tokens like ``hi`` / ``help``
    from matching inside larger words such as ``vehicle`` or URLs.
    """
    pattern = _ASCII_TERM_RE_CACHE.get(term)
    if pattern is None:
        pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])", re.IGNORECASE)
        _ASCII_TERM_RE_CACHE[term] = pattern
    return pattern


def _contains_any(original: str, lowered: str, terms: tuple[str, ...]) -> bool:
    """Match any term in *original* / *lowered*.

    For ASCII-only terms we require word boundaries to avoid false
    positives inside URLs or longer English words. Terms containing any
    non-ASCII character (CJK, etc.) fall back to substring matching since
    word boundaries are not meaningful for them.
    """
    for term in terms:
        if not term:
            continue
        if term.isascii():
            if _ascii_word_pattern(term).search(original):
                return True
        else:
            if term in original or term.casefold() in lowered:
                return True
    return False


def _strip_leading_mentions(text: str) -> str:
    cleaned = text.strip()
    cleaned = re.sub(r"^(?:<at\s+[^>]+></at>\s*)+", "", cleaned).strip()
    cleaned = re.sub(r"^(?:@\S+\s*)+", "", cleaned).strip()
    return cleaned


def _strip_bug_message_shell_prefix(text: str) -> str:
    cleaned = text.strip()
    cleaned = re.sub(r"^(?:CLI|飞书\s*CLI)\s+", "", cleaned, flags=re.I).strip()
    return cleaned


def _keyword_variants(keyword: str) -> set[str]:
    base = keyword.strip().lstrip("/").casefold()
    if not base:
        return {keyword.casefold()}
    return {base, f"/{base}"}
