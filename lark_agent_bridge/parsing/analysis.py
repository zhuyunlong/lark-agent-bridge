"""直传日志 / app-server / 源码 / 需求 / 报告追问请求解析。"""

from __future__ import annotations

import re

from ..models import (
    AppServerInvestigationRequest,
    DirectAnalysisRequest,
    ReportFollowupRequest,
    RequirementAnalysisRequest,
    RequirementWorkItemRef,
    SourceAnalysisRequest,
)
from .patterns import (
    DIRECT_ANALYSIS_DEICTIC_RE,
    PROJECT_WORKITEM_URL_RE,
    SOURCE_IDENTIFIER_RE,
    URL_RE,
)
from .terms import (
    APP_SERVER_INVESTIGATION_AUTO_TERMS,
    APP_SERVER_INVESTIGATION_FREE_TERMS,
    DIRECT_ANALYSIS_ACTION_TERMS,
    DIRECT_ANALYSIS_DOMAIN_TERMS,
    REPORT_FOLLOWUP_TERMS,
    REQUIREMENT_ANALYSIS_ACTION_TERMS,
    REQUIREMENT_ANALYSIS_TERMS,
    SOURCE_ANALYSIS_ACTION_TERMS,
    SOURCE_ANALYSIS_CHAIN_TERMS,
    SOURCE_ANALYSIS_TERMS,
    TRAILING_URL_PUNCTUATION,
    _SOURCE_TARGET_STOP_WORDS,
)
from .textutils import (
    _contains_any,
    _independent_term_pattern,
    _strip_leading_mentions,
)
from .resources import (
    _find_resources,
)
from .bug import (
    parse_bug_request,
)
from .chat import (
    build_basic_chat_reply,
)


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
