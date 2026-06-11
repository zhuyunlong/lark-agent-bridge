"""本地信号探针：候选提取、排序、证据与摘录。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Any, TYPE_CHECKING
from ..models import SearchHit
from .investigation_types import (
    SourceInvestigationResult,
)
from .prompt_lines import (
    _priority_modules,
)
from .path_utils import (
    _exact_excerpt_tokens,
    _excerpt_tokens_for_module,
    _resolve_module_path,
)


def _try_local_signal_probe(
    question: str,
    hits: list[SearchHit],
    repo_roots: list[Path],
) -> SourceInvestigationResult | None:
    candidates = _signal_candidates_from_hits(hits)
    if not candidates:
        return None
    modules = _priority_modules(question, hits, repo_roots)
    if not modules:
        return None
    records = _collect_priority_records(question, hits, repo_roots, modules)
    if not records:
        return None
    datacenter_action = _extract_datacenter_action(records)
    if not datacenter_action:
        return None
    ranked = _rank_signal_candidates(candidates, records)
    primary = ranked[0] if ranked else None
    if primary is None or not primary["has_mapping"] or not primary["has_producer"]:
        return None
    downstream = [item for item in ranked[1:] if item["has_consumer"]]
    command_lines = _local_probe_commands(primary, datacenter_action)
    if not command_lines:
        return None
    answer = _local_probe_answer(question, primary, downstream, datacenter_action, command_lines)
    evidence = _local_probe_evidence(primary, downstream, datacenter_action)
    if len(evidence) < 4:
        return None
    coverage_boundary = _local_probe_coverage_boundary(primary, downstream)
    return SourceInvestigationResult(
        success=True,
        answer=answer,
        canonical_key="guideengine.signal.front_car_start_remind.mock_path" if "前车起步" in question else "",
        confidence=0.82 if downstream else 0.74,
        commands=command_lines,
        source_evidence=evidence[:12],
        coverage_boundary=coverage_boundary,
        writeback_allowed=False,
    )


def _signal_candidates_from_hits(hits: list[SearchHit]) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    seen: set[str] = set()
    for hit in hits:
        if hit.kind != "signal_proto_entry":
            continue
        signal = str(hit.metadata.get("signal") or "").strip()
        if not signal or signal in seen:
            continue
        seen.add(signal)
        candidates.append(
            {
                "signal": signal,
                "code": str(hit.metadata.get("code") or "").strip(),
                "title": hit.title.strip(),
                "comment": _extract_signal_proto_field(hit.content, "comment"),
                "source_ref": str(hit.source_ref or "").strip(),
                "line": str(hit.metadata.get("line") or "").strip(),
            }
        )
    return candidates


def _extract_signal_proto_field(content: str, field: str) -> str:
    prefix = f"{field}:"
    for line in (content or "").splitlines():
        stripped = line.strip()
        if stripped.startswith(prefix):
            return stripped[len(prefix) :].strip()
    return ""


def _collect_priority_records(
    question: str,
    hits: list[SearchHit],
    repo_roots: list[Path],
    modules: list[str],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for module in modules:
        full_path = _resolve_module_path(module, repo_roots)
        if not full_path or not full_path.is_file():
            continue
        records.extend(_collect_file_excerpt_records(full_path, module, question, hits))
    return records


def _collect_file_excerpt_records(
    full_path: Path,
    module: str,
    question: str,
    hits: list[SearchHit],
) -> list[dict[str, Any]]:
    try:
        lines = full_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    exact_tokens = _exact_excerpt_tokens(question, hits)
    tokens = _excerpt_tokens_for_module(module, question, hits)
    records: list[dict[str, Any]] = []
    seen: set[int] = set()
    exact_match_lines = [
        lineno
        for lineno, line in enumerate(lines, start=1)
        if any(token and token in line for token in exact_tokens)
    ]
    for lineno in exact_match_lines[-2:]:
        for near in (lineno - 1, lineno, lineno + 1):
            if near < 1 or near > len(lines) or near in seen:
                continue
            seen.add(near)
            records.append({"file": module, "line": near, "text": lines[near - 1].strip()})
            if len(records) >= 6:
                break
        if len(records) >= 6:
            break
    if len(records) < 4:
        for lineno, line in enumerate(lines, start=1):
            if not any(token and token in line for token in tokens):
                continue
            if lineno in seen:
                continue
            seen.add(lineno)
            records.append({"file": module, "line": lineno, "text": line.strip()})
            if len(records) >= 6:
                break
    return records


def _rank_signal_candidates(candidates: list[dict[str, str]], records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ranked: list[dict[str, Any]] = []
    for candidate in candidates:
        signal = candidate["signal"]
        candidate_files = {
            str(record.get("file") or "")
            for record in records
            if signal in str(record.get("text") or "")
        }
        matched = [
            record
            for record in records
            if signal in str(record.get("text") or "") or str(record.get("file") or "") in candidate_files
        ]
        has_mapping = any("SignalMapping.kt" in str(record.get("file") or "") for record in matched)
        has_producer = any(
            any(token in str(record.get("text") or "") for token in ("onNextData(", "SignalFormat.", "setFormat("))
            and any(part in str(record.get("file") or "") for part in ("Helper", "DataCenter", "Transport"))
            for record in matched
        )
        has_consumer = any(
            any(part in str(record.get("file") or "") for part in ("TipsBizService", "TipsServiceRepository", "TipsMsgHelper", "Proxy"))
            for record in matched
        )
        format_name = _detect_signal_format(candidate, matched)
        score = (
            (4 if has_mapping else 0)
            + (5 if has_producer else 0)
            + (2 if has_consumer else 0)
            + (1 if signal.startswith("SIGNAL_CTL_") else 0)
        )
        ranked.append(
            {
                **candidate,
                "matched_records": matched,
                "has_mapping": has_mapping,
                "has_producer": has_producer,
                "has_consumer": has_consumer,
                "format_name": format_name,
                "score": score,
            }
        )
    return sorted(ranked, key=lambda item: (-int(item["score"]), item["signal"]))


def _detect_signal_format(candidate: dict[str, Any], matched: list[dict[str, Any]]) -> str:
    for record in matched:
        text = str(record.get("text") or "")
        for name in ("Int32", "Int64", "String", "Boolean", "Float", "Double"):
            if f"SignalFormat.{name}" in text or f"Signal.SignalFormat.{name}" in text:
                return name
    comment = str(candidate.get("comment") or "").casefold()
    if " int" in comment or comment.endswith("int"):
        return "Int32"
    return ""


def _extract_datacenter_action(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    for record in records:
        text = str(record.get("text") or "")
        if "ACTION_MOCK" in text and "mock.datacenter" in text:
            match = re.search(r'"([^"]+mock\.datacenter[^"]*)"', text)
            action = match.group(1) if match else "com.xiaopeng.guide.action.mock.datacenter"
            return {"action": action, "record": record}
    return None


def _local_probe_commands(primary: dict[str, Any], datacenter_action: dict[str, Any]) -> list[str]:
    code = str(primary.get("code") or "").strip()
    format_name = str(primary.get("format_name") or "").strip()
    format_code = _signal_format_code(format_name)
    action = str(datacenter_action.get("action") or "").strip()
    if not code or not format_code or not action:
        return []
    comment = str(primary.get("comment") or "")
    if _looks_like_binary_enable_signal(comment):
        return [
            f"adb shell am broadcast -a {action} --ei code {code} --ei format {format_code} --es value 1",
            f"adb shell am broadcast -a {action} --ei code {code} --ei format {format_code} --es value 0",
        ]
    return [f"adb shell am broadcast -a {action} --ei code {code} --ei format {format_code} --es value <value>"]


def _signal_format_code(format_name: str) -> str:
    mapping = {
        "Int32": "3",
        "Int64": "4",
        "Float": "5",
        "Double": "6",
        "String": "7",
        "Boolean": "8",
    }
    return mapping.get(format_name, "")


def _looks_like_binary_enable_signal(comment: str) -> bool:
    text = (comment or "").casefold()
    return ("0" in text and "1" in text) and any(token in text for token in ("关闭", "开启", "off", "on"))


def _local_probe_answer(
    question: str,
    primary: dict[str, Any],
    downstream: list[dict[str, Any]],
    datacenter_action: dict[str, Any],
    command_lines: list[str],
) -> str:
    lines = []
    if downstream:
        lines.append(
            f"源码里“{question.replace('源码调查', '').strip()}”至少涉及 2 条相关信号。更适合作为模拟入口的是 "
            f"{primary['signal']} ({primary['code']})；"
            + "、".join(f"{item['signal']} ({item['code']})" for item in downstream)
            + " 更像下游消费/展示信号。"
        )
    else:
        lines.append(f"源码里更适合作为模拟入口的信号是 {primary['signal']} ({primary['code']})。")
    lines.append(
        f"推荐用 DataCenter mock 广播注入 {primary['signal']}：action={datacenter_action['action']}，"
        f"format={primary.get('format_name') or '未知'}。"
    )
    if _looks_like_binary_enable_signal(str(primary.get("comment") or "")):
        lines.append("值语义：0 关闭，1 开启。")
    lines.extend(command_lines)
    if downstream:
        lines.append(
            "下游消费侧证据："
            + "；".join(f"{item['signal']} 在直接消费模块出现" for item in downstream[:2])
            + "。"
        )
    return "\n".join(lines)


def _local_probe_evidence(
    primary: dict[str, Any],
    downstream: list[dict[str, Any]],
    datacenter_action: dict[str, Any],
) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    evidence.append(_candidate_definition_evidence(primary))
    evidence.extend(primary.get("matched_records", []))
    if datacenter_action.get("record"):
        evidence.append(datacenter_action["record"])
    for item in downstream[:2]:
        evidence.append(_candidate_definition_evidence(item))
        evidence.extend(item.get("matched_records", [])[:2])
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, int, str]] = set()
    for record in evidence:
        file = str(record.get("file") or "")
        line = int(record.get("line") or 0)
        text = str(record.get("text") or "")
        key = (file, line, text)
        if file and text and key not in seen:
            seen.add(key)
            deduped.append({"file": file, "line": line, "text": text})
    return deduped


def _candidate_definition_evidence(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "file": str(candidate.get("source_ref") or ""),
        "line": int(candidate.get("line") or 0),
        "text": f"{candidate.get('signal')} = {candidate.get('code')}; {candidate.get('comment')}".strip(),
    }


def _local_probe_coverage_boundary(primary: dict[str, Any], downstream: list[dict[str, Any]]) -> str:
    if downstream:
        return (
            f"已证明 {primary['signal']} 的定义、映射/生产链和 DataCenter mock 注入入口；"
            f"也已证明 {downstream[0]['signal']} 存在直接消费侧证据。"
            "当前固定链路未直接证明两者之间全部中间转换节点，因此结论用于指导模拟入口选择，"
            "不等同于完整运行时链路全证。"
        )
    return (
        f"已证明 {primary['signal']} 的定义、映射/生产链和 DataCenter mock 注入入口；"
        "当前未覆盖更下游的完整消费链。"
    )
