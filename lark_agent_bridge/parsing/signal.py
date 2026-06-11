"""信号生命周期请求解析。"""

from __future__ import annotations

import re

from ..config import DEFAULT_SIGNAL_ALIASES
from ..models import (
    SignalRequest,
)
from ..signal_resolver import SignalResolver
from .patterns import (
    ROM_VERSION_RE,
    SIGNAL_BARE_NAME_RE,
    SIGNAL_CODE_RE,
    SIGNAL_ENUM_RE,
    URL_RE,
)
from .terms import (
    SIGNAL_CONTEXT_TERMS,
    TRIGGER_TERMS,
)
from .textutils import (
    _contains_any,
    _strip_leading_mentions,
)
from .resources import (
    _find_resources,
    _find_since,
)
from .chat import (
    extract_first_keyword_payload,
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
