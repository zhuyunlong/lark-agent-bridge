"""信号模拟问题识别、模板匹配与模拟类答案。"""

from __future__ import annotations

import re
from typing import Any
from ...models import BridgeConfig, KnowledgeSourceOptions, TaskResult
from ..ingestors import KnowledgeIngestError, ingest_source, source_ref, source_title
from ..models import KnowledgeChunk, SearchHit
from ..template_data import load_simulation_template_chunks, load_simulation_templates
from .answer_lowconf import (
    _SIGNAL_DOMAIN_TERMS,
    _candidate_intro,
)


_SIMULATION_INTENT_TERMS = ("模拟", "怎么", "如何", "命令", "指令", "广播", "mock", "造")


_MOCK_ACTION = "com.xiaopeng.guide.action.mock.datacenter"


_SIMULATION_TEMPLATES: tuple[dict[str, Any], ...] = tuple(load_simulation_templates())


_DERIVED_SOURCE_ID = "derived-adb-simulations"


_TEMPLATE_MATCH_SEPARATOR_RE = re.compile(r"[\s\-_./:：,，、|()（）\[\]【】{}]+")


def _looks_like_signal_simulation_question(text: str) -> bool:
    lowered = text.casefold()
    has_intent = any(term in lowered for term in _SIMULATION_INTENT_TERMS) or any(
        term in text for term in _SIMULATION_INTENT_TERMS
    )
    if not has_intent:
        return False
    return (
        any(term in lowered for term in _SIGNAL_DOMAIN_TERMS)
        or "信号" in text
        or any(_template_positive_match(lowered, template) for template in _SIMULATION_TEMPLATES)
    )


def _build_simulation_result(question: str, hits: list[SearchHit]) -> TaskResult | None:
    matches, exact_signal = _matching_simulation_templates(question)
    if not matches:
        if _looks_like_complex_simulation_question(question):
            return TaskResult(
                success=True,
                message=_complex_simulation_policy_answer(),
                details={
                    "mode": "knowledge_qa",
                    "question": question,
                    "answer_type": "adb_simulation_policy",
                    "knowledge_hits": [hit.to_dict(include_content=False) for hit in hits],
                },
            )
        return None
    if not exact_signal and _looks_like_complex_simulation_question(question) and all(
        template.get("category") == "complex" for template in matches
    ):
        return TaskResult(
            success=True,
            message=_complex_simulation_policy_answer(),
            details={
                "mode": "knowledge_qa",
                "question": question,
                "answer_type": "adb_simulation_policy",
                "knowledge_hits": [hit.to_dict(include_content=False) for hit in _prefer_template_hits(hits, matches)],
            },
        )
    if exact_signal and len(matches) == 1 and not bool(matches[0].get("direct")):
        template = matches[0]
        return TaskResult(
            success=True,
            message=_custom_required_signal_answer(template),
            details={
                "mode": "knowledge_qa",
                "question": question,
                "answer_type": "adb_signal_custom_required",
                "exact_signal": exact_signal,
                "canonical_key": str(template.get("canonical_key") or ""),
                "candidates": [_candidate_payload(template)],
                "knowledge_hits": [
                    hit.to_dict(include_content=False) for hit in _primary_template_hits(hits, matches)
                ],
            },
        )
    if len(matches) == 1 and bool(matches[0].get("direct")):
        template = matches[0]
        return TaskResult(
            success=True,
            message=_direct_simulation_answer(template),
            details={
                "mode": "knowledge_qa",
                "question": question,
                "answer_type": "adb_signal_template",
                "exact_signal": exact_signal,
                "canonical_key": str(template.get("canonical_key") or ""),
                "knowledge_hits": [
                    hit.to_dict(include_content=False) for hit in _primary_template_hits(hits, matches)
                ],
            },
        )
    return TaskResult(
        success=True,
        message=_simulation_candidates_answer(matches),
        details={
            "mode": "knowledge_qa",
            "question": question,
            "answer_type": "adb_signal_candidates",
            "exact_signal": exact_signal,
            "candidates": [_candidate_payload(template) for template in matches],
            "knowledge_hits": [hit.to_dict(include_content=False) for hit in _prefer_template_hits(hits, matches)],
        },
    )


def _candidate_payload(template: dict[str, Any]) -> dict[str, Any]:
    return {
        "signal": str(template.get("signal") or ""),
        "code": str(template.get("code") or ""),
        "title": str(template.get("title") or ""),
        "direct": bool(template.get("direct")),
        "support": str(template.get("support") or ""),
    }


def _matching_simulation_templates(text: str) -> tuple[list[dict[str, Any]], bool]:
    lowered = text.casefold()
    exact_matches = [
        template
        for template in _SIMULATION_TEMPLATES
        if not _template_negative_match(lowered, template)
        and (
            _contains_signal_token(lowered, str(template.get("signal") or "").casefold())
            or (str(template.get("code") or "") and str(template.get("code")) in lowered)
        )
    ]
    if exact_matches:
        return _dedupe_templates(exact_matches), True

    matches: list[dict[str, Any]] = []
    for template in _SIMULATION_TEMPLATES:
        if _template_negative_match(lowered, template):
            continue
        if _template_positive_match(lowered, template):
            matches.append(template)
    if _looks_like_complex_simulation_question(text):
        matches.extend(template for template in _SIMULATION_TEMPLATES if template.get("category") == "complex")
    return _dedupe_templates(matches), False


def _template_positive_match(lowered_text: str, template: dict[str, Any]) -> bool:
    return any(_template_term_matches(lowered_text, term) for term in _template_terms(template))


def _template_negative_match(lowered_text: str, template: dict[str, Any]) -> bool:
    return any(_template_term_matches(lowered_text, term) for term in _template_terms(template, key="negative_aliases"))


def _template_terms(template: dict[str, Any], *, key: str = "aliases") -> list[str]:
    terms: list[str] = []
    if key == "aliases":
        for field in ("signal", "code", "title", "summary"):
            value = template.get(field)
            if value not in (None, ""):
                terms.append(str(value).casefold())
    for field in (key, "keywords") if key == "aliases" else (key,):
        values = template.get(field)
        if isinstance(values, list):
            terms.extend(str(value).casefold() for value in values if str(value).strip())
    return [term for term in terms if term]


def _template_term_matches(text: str, term: str) -> bool:
    lowered_text = (text or "").casefold()
    lowered_term = (term or "").casefold().strip()
    if not lowered_text or not lowered_term:
        return False
    if lowered_term in lowered_text:
        return True
    normalized_term = _normalize_template_match_text(lowered_term)
    if not normalized_term:
        return False
    return normalized_term in _normalize_template_match_text(lowered_text)


def _normalize_template_match_text(value: str) -> str:
    return _TEMPLATE_MATCH_SEPARATOR_RE.sub("", value.casefold())


def _looks_like_complex_simulation_question(text: str) -> bool:
    lowered = text.casefold()
    return any(term in lowered for term in ("pb", "proto", "protoobject", "bytearray", "byte array"))


def _contains_signal_token(text: str, signal: str) -> bool:
    return bool(signal) and re.search(rf"(?<![a-z0-9_]){re.escape(signal)}(?![a-z0-9_])", text) is not None


def _dedupe_templates(templates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for template in templates:
        template_id = str(template.get("id") or template.get("canonical_key") or template.get("signal") or "")
        if not template_id or template_id in seen:
            continue
        seen.add(template_id)
        deduped.append(template)
    return deduped


def _prefer_template_hits(hits: list[SearchHit], templates: list[dict[str, Any]]) -> list[SearchHit]:
    keywords: list[str] = []
    for template in templates:
        keywords.extend(_template_terms(template))
    lowered_keywords = [keyword.casefold() for keyword in keywords if keyword]
    preferred = [
        hit
        for hit in hits
        if any(_template_term_matches(hit.title + "\n" + hit.content, keyword) for keyword in lowered_keywords)
    ]
    return preferred or hits


def _primary_template_hits(hits: list[SearchHit], templates: list[dict[str, Any]]) -> list[SearchHit]:
    preferred = [*_prefer_template_hits(hits, templates), *_template_fallback_hits(templates)]
    return sorted(preferred, key=lambda hit: _template_reference_rank(hit, templates))[:1]


def _template_fallback_hits(templates: list[dict[str, Any]]) -> list[SearchHit]:
    fallback_hits: list[SearchHit] = []
    for template in templates:
        canonical_key = str(template.get("canonical_key") or template.get("id") or template.get("signal") or "").strip()
        if not canonical_key:
            continue
        title = str(template.get("title") or template.get("signal") or canonical_key).strip()
        content_parts = [
            title,
            str(template.get("summary") or "").strip(),
            str(template.get("signal") or "").strip(),
            str(template.get("code") or "").strip(),
        ]
        commands = template.get("commands")
        if isinstance(commands, list):
            content_parts.extend(str(command).strip() for command in commands)
        fallback_hits.append(
            SearchHit(
                chunk_id=f"template:{canonical_key}",
                source_id="bundled-adb-simulations",
                title=title,
                content="\n".join(part for part in content_parts if part),
                source_ref="generated:adb_simulation_templates",
                kind="adb_signal_template",
                score=0.0,
                metadata=dict(template),
            )
        )
    return fallback_hits


def _template_reference_rank(hit: SearchHit, templates: list[dict[str, Any]]) -> tuple[int, int, int, int, float, str]:
    signals = [str(template.get("signal") or "").casefold() for template in templates]
    codes = [str(template.get("code") or "").casefold() for template in templates]
    text = (hit.title + "\n" + hit.content).casefold()
    metadata_signal = str(hit.metadata.get("signal") or "").casefold()
    metadata_code = str(hit.metadata.get("code") or "").casefold()
    signal_match = metadata_signal in signals or any(_contains_signal_token(text, signal) for signal in signals)
    code_match = metadata_code in codes or any(code and code in text for code in codes)
    return (
        0 if signal_match or code_match else 1,
        0 if hit.kind == "adb_signal_template" else 1,
        0 if hit.source_id == _DERIVED_SOURCE_ID else 1,
        0 if hit.source_ref.startswith("generated:") else 1,
        -hit.score,
        hit.title,
    )


def _direct_simulation_answer(template: dict[str, Any]) -> str:
    answer_blocks = template.get("answer_blocks")
    if isinstance(answer_blocks, list) and answer_blocks:
        return "\n".join(str(block) for block in answer_blocks if str(block).strip())
    commands = template.get("commands")
    lines = [str(template.get("title") or template.get("signal") or "ADB 模拟指令")]
    if isinstance(commands, list):
        lines.extend(str(command) for command in commands if str(command).strip())
    return "\n".join(lines)


def _custom_required_signal_answer(template: dict[str, Any]) -> str:
    signal = str(template.get("signal") or "")
    code = str(template.get("code") or "")
    title = str(template.get("title") or signal)
    summary = str(template.get("summary") or "")
    return (
        f"{title}：{signal} ({code}) 当前不能生成通用 ADB 模拟命令。\n"
        f"{summary}\n\n"
        "原因：DataCenterBroadcastReceiver 的通用分支只把 value 字符串转换为基础类型；"
        "ByteArray/PB、ProtoObject、任意 JavaObject 需要源码里的 mockDataFactory 自定义构造对象。"
        "如果没有对应 factory，直接拼对象命令不会得到业务需要的 PB 对象。\n\n"
        "可行处理：\n"
        "1. 常用场景：新增 mockDataFactory 映射，根据简单 value 构造 PB/对象，再写入 dataCenter.mockSignal。\n"
        "2. 真实数据复现：用 ReplayReceiver/ProtocolFile 的 record 回放链路，把录制到的 ByteArray 按原始字节回灌。\n"
        "3. 知识库回答：没有源码 factory 或录制样本时，只能说明需要自定义，不能输出看似可执行的伪命令。"
    )


def _complex_simulation_policy_answer() -> str:
    return (
        "ADB 模拟要先按源码能力分层，不能只看 signal.proto 的 code/format：\n"
        "1. 基础类型可直接走通用广播：Int32、Int64、Float、Double、String、Boolean、Int32Array、FloatArray、DoubleArray。\n"
        f"示例形态：adb shell am broadcast -a {_MOCK_ACTION} --ei code <code> --ei format <format> --es value <value>\n"
        "2. 已有 mockDataFactory 的特殊对象可以给确定模板，例如源码里有 builder 的信号。\n"
        "3. ByteArray/PB、ProtoObject、任意 JavaObject 没有 factory 时不能通用模拟；value 字符串无法自动变成目标 PB 对象。\n"
        "4. 这类信号的解决方式是新增 mockDataFactory 自定义 builder，或使用 ReplayReceiver/ProtocolFile 的 record 回放链路灌真实 ByteArray。\n\n"
        "知识库输出策略：只对基础类型或已验证 factory 输出 ADB 命令；其他复杂类型明确提示“需要自定义”，不要生成看似通用的 PB ADB 命令。"
    )


def _simulation_candidates_answer(matches: list[dict[str, Any]]) -> str:
    if len(matches) == 1:
        lines = ["命中一个可能的 ADB 模拟信号，但当前还没有验证过的通用 ADB 模板："]
    else:
        intro = _candidate_intro(matches)
        lines = [intro or "命中多个可能的 ADB 模拟信号，先按用途选一个："]
    for index, template in enumerate(matches, start=1):
        signal = str(template.get("signal") or "")
        code = str(template.get("code") or "")
        title = str(template.get("title") or signal)
        summary = str(template.get("summary") or "")
        format_note = str(template.get("format_note") or "")
        lines.append(f"{index}. {title}：{signal} ({code})\n{summary}\n{format_note}".rstrip())
        example = str(template.get("example") or "")
        if example:
            lines.append(f"示例：{example}")
        lines.append(f"继续发：知识库 模拟 {signal}")
    return "\n\n".join(lines)


def _is_negative_template_question(question: str) -> bool:
    lowered = (question or "").casefold()
    positive_match = any(_template_positive_match(lowered, template) for template in _SIMULATION_TEMPLATES)
    return not positive_match and any(_template_negative_match(lowered, template) for template in _SIMULATION_TEMPLATES)
