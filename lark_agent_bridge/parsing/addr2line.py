"""addr2line / ROM 版本请求解析。"""

from __future__ import annotations

import re

from ..models import (
    Addr2LineRequest,
    RomVersionLookupRequest,
)
from .patterns import (
    ADDR2LINE_ADDRESS_RE,
    APK_VERSION_RE,
    BARE_SO_STACK_RE,
    LOG_FOLDER_RE,
    NAPA_VERSION_RE,
    ROM_VERSION_RE,
    SO_ADDR_RE,
    STACK_ANALYSIS_ACTION_RE,
    STACK_ANALYSIS_CONTEXT_RE,
    STACK_ANALYSIS_OBJECT_RE,
    SYMBOLISH_RE,
    TOMBSTONE_MAP_LINE_RE,
    TOMBSTONE_PC_RE,
)
from .terms import (
    ADDR2LINE_TERMS,
    ROM_LOOKUP_TERMS,
    SOURCE_ANALYSIS_TERMS,
)
from .textutils import (
    _contains_any,
    _strip_leading_mentions,
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
