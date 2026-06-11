"""通用文本匹配辅助。"""

from __future__ import annotations

import re



def _independent_term_pattern(terms: tuple[str, ...] | list[str], *, leading_only: bool) -> re.Pattern[str]:
    unique_terms = [term.strip() for term in terms if str(term or "").strip()]
    unique_terms.sort(key=len, reverse=True)
    if not unique_terms:
        return re.compile(r"$^")
    alternation = "|".join(re.escape(term) for term in unique_terms)
    if leading_only:
        return re.compile(rf"^(?P<term>{alternation})(?=\s|$)", re.I)
    return re.compile(rf"(^|\s)(?P<term>{alternation})(?=\s|$)")


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
