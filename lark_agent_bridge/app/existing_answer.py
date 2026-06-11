from __future__ import annotations

import html
import json
import os
from pathlib import Path
import re
import shutil
import stat
import unicodedata

from ._shared import *  # noqa: F401,F403


class _ExistingAnswerMixin:
    """基于既有分析产物的快速追问回答与置信评分（与 ContextFromMixin 共享 self 状态）。"""

    def _answer_bug_followup_from_existing(
        self,
        route_content: str,
        followup_context,
        *,
        min_confidence: float = _FAST_EXISTING_ANSWER_MIN_CONFIDENCE,
    ) -> TaskResult | None:
        question = route_content.strip()
        if not question or self._bug_followup_requires_fresh_analysis(question):
            return None
        evidence_lines = self._rank_existing_bug_evidence(question, followup_context)
        if not evidence_lines:
            return None
        confidence, confidence_reason = self._existing_bug_answer_confidence(question, evidence_lines)
        if confidence < min_confidence:
            return None
        top_evidence = evidence_lines[:5]
        primary = top_evidence[0]
        bullets = "\n".join(f"- {line}" for line in top_evidence)
        message = (
            "## 结论摘要\n"
            f"- {primary}\n"
            "- 基于已有报告/摘要可直接回答，本轮未重新下载日志，也未重新调用本地 Agent。\n\n"
            "## 关键证据\n"
            f"{bullets}\n\n"
            "## 建议动作\n"
            "- 如果你希望重新跑日志、补源码证据或修正既有结论，请明确回复“重新分析/基于源码重跑”。"
        )
        return TaskResult(
            success=True,
            message=message,
            details={
                "mode": "bug_followup_existing_answer",
                "answer_source": "existing_context",
                "answer_confidence": confidence,
                "answer_confidence_reason": confidence_reason,
                "followup_text": route_content,
                "delivery": "reply",
            },
        )
    def _bug_followup_requires_fresh_analysis(self, text: str) -> bool:
        lowered = text.casefold()
        force_terms = tuple(term.casefold() for term in self.config.bug_analysis.force_reanalysis_terms)
        return any(term in lowered for term in force_terms)
    def _rank_existing_bug_evidence(self, question: str, followup_context) -> list[str]:
        tokens = self._fast_answer_tokens(question)
        if not tokens:
            return []
        sources: list[tuple[int, str]] = [
            (3, str(getattr(followup_context, "summary_text", "") or "")),
            (2, str(getattr(followup_context, "report_excerpt", "") or "")),
        ]
        for item in getattr(followup_context, "history", []) or []:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").casefold()
            if role == "assistant":
                sources.append((1, str(item.get("content") or "")))
        ranked: list[tuple[int, int, int, str]] = []
        seen: set[str] = set()
        for source_weight, context_text in sources:
            for index, raw_line in enumerate(context_text.splitlines()):
                line = self._clean_existing_answer_line(raw_line)
                if not line or line in seen or self._is_question_echo(line, question):
                    continue
                score = self._score_existing_answer_line(line, tokens)
                if score <= 0 and source_weight >= 2 and self._looks_like_existing_evidence(line):
                    score = 1
                if score <= 0:
                    continue
                seen.add(line)
                ranked.append((score, source_weight, -index, line))
        ranked.sort(reverse=True)
        return [line for score, _source_weight, _index, line in ranked if score >= 1][:8]
    def _existing_bug_answer_confidence(self, question: str, evidence_lines: list[str]) -> tuple[float, str]:
        evidence_text = "\n".join(evidence_lines).casefold()
        domain_terms = self._fast_answer_domain_terms(question)
        missing_domain_terms = [term for term in domain_terms if term.casefold() not in evidence_text]
        if missing_domain_terms:
            return 0.45, f"missing_domain_terms={','.join(missing_domain_terms[:4])}"
        tokens = self._fast_answer_tokens(question)
        if not tokens:
            return 0.0, "no_question_tokens"
        matched = [token for token in tokens if token.casefold() in evidence_text]
        coverage = len(matched) / max(1, len(tokens))
        if len(evidence_lines) >= 2 and coverage >= 0.35:
            return 0.9, f"coverage={coverage:.2f};domain_terms={len(domain_terms)}"
        if coverage >= 0.5:
            return 0.82, f"coverage={coverage:.2f};domain_terms={len(domain_terms)}"
        return 0.6, f"coverage={coverage:.2f};domain_terms={len(domain_terms)}"
    def _fast_answer_domain_terms(self, question: str) -> list[str]:
        terms: list[str] = []
        for match in re.finditer(r"SIGNAL_[A-Za-z0-9_]+|[A-Z][A-Z0-9]+(?:_[A-Z0-9]+)+", question):
            self._append_answerability_term(terms, match.group(0))
        for match in re.finditer(r"[A-Za-z][A-Za-z0-9_]{2,}", question):
            token = match.group(0)
            if token.casefold() not in _FAST_ANSWER_LATIN_DOMAIN_STOP_TERMS:
                self._append_answerability_term(terms, token)
        cleaned = question
        for stop in sorted(_FAST_ANSWER_DOMAIN_STOP_TERMS, key=len, reverse=True):
            cleaned = cleaned.replace(stop, " ")
        for match in re.finditer(r"[\u4e00-\u9fff]{2,12}", cleaned):
            self._append_answerability_term(terms, match.group(0))
        return terms[:8]
    def _is_question_echo(self, line: str, question: str) -> bool:
        line_norm = self._normalize_fast_answer_text(line)
        question_norm = self._normalize_fast_answer_text(question)
        if not line_norm or not question_norm:
            return False
        return line_norm == question_norm or line_norm in question_norm or question_norm in line_norm
    def _looks_like_existing_evidence(self, line: str) -> bool:
        lowered = line.casefold()
        return bool(
            re.search(r"\d{1,2}:\d{2}:\d{2}", line)
            or "`" in line
            or "/" in line
            or "true" in lowered
            or "false" in lowered
            or any(marker in line for marker in ("证据", "日志", "记录", "显示", "命中", "字段"))
        )
    def _normalize_fast_answer_text(self, text: str) -> str:
        cleaned = re.sub(r"<[^>]+>", " ", text.casefold())
        cleaned = re.sub(r"[`*_#>\[\]（）()，,。！？!?：:；;、/|\s\-–—0-9.]+", "", cleaned)
        return cleaned.strip()
    def _fast_answer_tokens(self, question: str) -> list[str]:
        normalized = re.sub(r"[`*_#>\[\]（）()，,。！？!?：:；;、/|]+", " ", question.casefold())
        tokens: set[str] = set()
        for token in re.findall(r"[a-z0-9_+\-.]{2,}|[\u4e00-\u9fff]{2,}", normalized):
            if token in _FAST_ANSWER_STOP_TOKENS:
                continue
            if re.fullmatch(r"[\u4e00-\u9fff]+", token):
                for i in range(max(1, len(token) - 1)):
                    gram = token[i : i + 2]
                    if gram and gram not in _FAST_ANSWER_STOP_TOKENS:
                        tokens.add(gram)
            else:
                tokens.add(token)
        return sorted(tokens, key=len, reverse=True)
    def _clean_existing_answer_line(self, line: str) -> str:
        cleaned = re.sub(r"<[^>]+>", " ", line.strip())
        cleaned = re.sub(r"^[\s>*#\-–—0-9.、]+", "", cleaned).strip()
        cleaned = re.sub(r"\s+", " ", cleaned)
        if not cleaned or len(cleaned) < 8:
            return ""
        if len(cleaned) > 500:
            cleaned = cleaned[:499].rstrip() + "…"
        if cleaned.count("{") + cleaned.count("}") > 2:
            return ""
        return cleaned
    def _score_existing_answer_line(self, line: str, tokens: list[str]) -> int:
        lowered = line.casefold()
        score = 0
        for token in tokens:
            if token and token in lowered:
                score += 2 if len(token) >= 3 else 1
        if any(marker in lowered for marker in ("结论", "证据", "原因", "置信", "根因")):
            score += 1
        return score
    def _existing_bug_context_can_answer(self, route_content: str, followup_context) -> bool:
        question = route_content.strip()
        if not question:
            return True
        lowered = question.casefold()
        if not any(term.casefold() in lowered for term in _FOLLOWUP_INTENT_TERMS):
            return True
        context_text = "\n".join(
            [
                str(getattr(followup_context, "request_text", "") or ""),
                str(getattr(followup_context, "summary_text", "") or ""),
                str(getattr(followup_context, "report_excerpt", "") or ""),
            ]
        )
        if not context_text.strip():
            return False
        context_folded = context_text.casefold()
        missing = [
            term
            for term in self._followup_answerability_terms(question)
            if term.casefold() not in context_folded
        ]
        return not missing
    def _followup_answerability_terms(self, text: str) -> list[str]:
        terms: list[str] = []
        for match in re.finditer(r"SIGNAL_[A-Za-z0-9_]+|[A-Z][A-Z0-9]+(?:_[A-Z0-9]+)+", text):
            self._append_answerability_term(terms, match.group(0))
        for match in re.finditer(r"[A-Za-z][A-Za-z0-9_]{2,}", text):
            token = match.group(0)
            if token.upper() == "SIGNAL" or token.isdigit():
                continue
            self._append_answerability_term(terms, token)
        for match in re.finditer(r"[\u4e00-\u9fff]{2,12}", text):
            token = match.group(0)
            if token in _FOLLOWUP_STOP_TERMS:
                continue
            if any(stop in token for stop in _FOLLOWUP_STOP_TERMS) and len(token) <= 4:
                continue
            self._append_answerability_term(terms, token)
        return terms[:8]
    def _append_answerability_term(self, terms: list[str], term: str) -> None:
        normalized = term.strip(" \t\r\n，。；;,.、:：?？!！()（）[]【】")
        if not normalized:
            return
        if normalized.casefold() in {item.casefold() for item in terms}:
            return
        terms.append(normalized)
