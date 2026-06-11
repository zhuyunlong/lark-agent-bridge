"""消息解析正则模式。"""

from __future__ import annotations

import re



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


DIRECT_ANALYSIS_DEICTIC_RE = re.compile(
    r"^(?:看下|看一下|看看|帮我看(?:下|一下)?|分析(?:下|一下)?|排查(?:下|一下)?|调查(?:下|一下)?|查(?:下|一下)?)"
    r"(?:这个|这份|这个文件|这个日志|这份日志|这个附件|这份附件)?[？?]?$"
)


SOURCE_IDENTIFIER_RE = re.compile(
    r"(?<![A-Za-z0-9_])("
    r"SIGNAL_[A-Z0-9_]+"
    r"|[A-Za-z_][A-Za-z0-9_]*(?:Ready|Service|Manager|Handler|Signal|Flow|Msg|Message|Callback|Listener|State|Context|Adapter|ViewModel|Repository|Controller|Biz|Proxy)[A-Za-z0-9_]*"
    r"|[a-z_]+[A-Z][A-Za-z0-9_]*"
    r")(?![A-Za-z0-9_])"
)
