from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from . import _shared


class _BugFaultTimeMixin:
    """Bug 故障时间：标题/描述/LLM 提取、时间上下文解析与日志时间覆盖扫描（与 ArchiveExtractMixin 共享 self 状态）。"""

    def _fault_time_to_datetime(self, fault_time: str) -> datetime | None:
        return self._parse_bug_datetime(fault_time)
    def _parse_fault_datetime(self, fault_time: str) -> "time.struct_time | None":
        short_match = re.search(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)", fault_time)
        if short_match and "20" not in fault_time:
            # Bare HH:MM without a date cannot be reliably resolved —
            # using the current date would silently mis-select logs when
            # analysing historical bugs.  Return None so callers fall back
            # to the original input directory.
            return None
        match = re.search(
            r"(20\d{2})[-_/年](\d{1,2})[-_/月](\d{1,2})[日_\s-]*(\d{1,2}):(\d{2})",
            fault_time,
        )
        if not match:
            return None
        try:
            return time.strptime(
                f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d} "
                f"{int(match.group(4)):02d}:{int(match.group(5)):02d}",
                "%Y-%m-%d %H:%M",
            )
        except ValueError:
            return None
    def _parse_log_file_datetime(self, name: str) -> "time.struct_time | None":
        match = re.search(r"(20\d{2}-\d{2}-\d{2})_(\d{2})-(\d{2})(?=\D|$)", name)
        if not match:
            return None
        try:
            return time.strptime(
                f"{match.group(1)} {match.group(2)}:{match.group(3)}",
                "%Y-%m-%d %H:%M",
            )
        except ValueError:
            return None
    def _resolve_bug_time_context(
        self,
        *,
        request_text: str,
        title: str,
        description: str,
        reference_time: str = "",
    ) -> BugTimeContext:
        sources = [
            ("user", self._strip_urls_for_time_parse(request_text)),
            ("title", title or ""),
            ("description", description or ""),
        ]
        reference_year = self._reference_year_from_text(reference_time)
        fallback_reference_date = self._reference_date_from_text(reference_time)
        candidates: list[dict[str, str]] = []
        for source, text in sources:
            candidate = self._extract_time_candidate(text, reference_year=reference_year)
            if candidate:
                candidates.append({"source": source, **candidate})

        if not candidates:
            llm_candidate = self._extract_time_via_llm(
                request_text=request_text,
                title=title,
                description=description,
                reference_time=reference_time,
            )
            if llm_candidate:
                candidates.append(llm_candidate)

        if not candidates:
            return BugTimeContext(
                fault_time="",
                source="",
                note="用户输入、标题和缺陷描述中都未识别到几月几日几点几分的问题时间。",
                has_full_datetime=False,
                candidates=[],
            )

        reference_date = self._select_reference_date(candidates) or fallback_reference_date
        override_source = self._preferred_bug_time_override_source(
            candidates,
            authoritative_date=self._authoritative_bug_time_date(candidates, fallback_reference_date),
        )
        preferred_sources = [override_source] if override_source else []
        preferred_sources.extend(source for source in ("user", "title", "description", "llm") if source != override_source)
        for preferred_source in preferred_sources:
            for candidate in candidates:
                if candidate["source"] != preferred_source:
                    continue
                fault_time = candidate["value"]
                has_full_datetime = bool(candidate.get("date"))
                if not has_full_datetime and reference_date:
                    fault_time = f"{reference_date} {candidate['time']}"
                    has_full_datetime = True
                note = self._bug_time_context_note(candidate, reference_date=reference_date, completed=has_full_datetime)
                if override_source and preferred_source == override_source:
                    label = {"title": "标题", "description": "缺陷描述", "llm": "AI识别"}.get(preferred_source, preferred_source)
                    note = f"用户输入时间与 bug 标题/创建时间明显冲突，优先采用{label}中的完整问题时间。"
                return BugTimeContext(
                    fault_time=fault_time,
                    source=preferred_source,
                    note=note,
                    has_full_datetime=has_full_datetime,
                    candidates=candidates,
                )

        return BugTimeContext(
            fault_time="",
            source="",
            note="已找到时间片段，但无法补齐到几月几日几点几分。",
            has_full_datetime=False,
            candidates=candidates,
        )
    def _authoritative_bug_time_date(
        self,
        candidates: list[dict[str, str]],
        fallback_reference_date: str,
    ) -> str:
        if fallback_reference_date:
            return fallback_reference_date
        for source in ("title", "description", "llm"):
            for candidate in candidates:
                if candidate.get("source") == source and candidate.get("date"):
                    return str(candidate["date"])
        return ""
    def _preferred_bug_time_override_source(
        self,
        candidates: list[dict[str, str]],
        *,
        authoritative_date: str,
    ) -> str:
        user_candidate = next(
            (
                candidate
                for candidate in candidates
                if candidate.get("source") == "user" and candidate.get("date")
            ),
            None,
        )
        if user_candidate is None or not authoritative_date or user_candidate.get("date") == authoritative_date:
            return ""
        user_dt = self._parse_bug_datetime(str(user_candidate.get("value") or ""))
        authoritative_dt = self._parse_bug_datetime(f"{authoritative_date} 00:00")
        if user_dt is None or authoritative_dt is None:
            return ""
        if user_dt.year == authoritative_dt.year and abs((user_dt - authoritative_dt).days) < 30:
            return ""
        for source in ("title", "description", "llm"):
            for candidate in candidates:
                if candidate.get("source") == source and candidate.get("date") == authoritative_date:
                    return source
        return ""
    def _strip_urls_for_time_parse(self, text: str) -> str:
        return re.sub(r"https?://\S+", " ", text or "")
    def _reference_year_from_text(self, text: str) -> int | None:
        match = re.search(r"\b(20\d{2})\b", text or "")
        return int(match.group(1)) if match else None
    def _reference_date_from_text(self, text: str) -> str:
        normalized = (text or "").replace("：", ":").replace("/", "-")
        match = re.search(r"\b(20\d{2})[-年](\d{1,2})[-月](\d{1,2})", normalized)
        if not match:
            return ""
        return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
    def _extract_time_candidate(self, text: str, *, reference_year: int | None = None) -> dict[str, str] | None:
        normalized = (text or "").replace("：", ":")
        full_match = re.search(
            r"(20\d{2})[-_/年](\d{1,2})[-_/月](\d{1,2})[日_\s-]*(\d{1,2}):(\d{2})(?::(\d{2}))?",
            normalized,
        )
        if full_match:
            date = f"{int(full_match.group(1)):04d}-{int(full_match.group(2)):02d}-{int(full_match.group(3)):02d}"
            time_text = self._format_time_parts(full_match.group(4), full_match.group(5), full_match.group(6))
            return {"value": f"{date} {time_text}", "date": date, "time": time_text, "raw": full_match.group(0)}
        md_match = re.search(
            r"(?<!\d)\[?(\d{1,2})[-/](\d{1,2})\]?(?:[\]\[日_\s-]+)(\d{1,2}):(\d{2})(?::(\d{2}))?",
            normalized,
        )
        if md_match:
            year = reference_year or datetime.now().year
            date = f"{year:04d}-{int(md_match.group(1)):02d}-{int(md_match.group(2)):02d}"
            time_text = self._format_time_parts(md_match.group(3), md_match.group(4), md_match.group(5))
            return {"value": f"{date} {time_text}", "date": date, "time": time_text, "raw": md_match.group(0)}
        cn_match = re.search(
            r"(?<!\d)(\d{1,2})月(\d{1,2})日[^\d]{0,8}(\d{1,2}):(\d{2})(?::(\d{2}))?",
            normalized,
        )
        if cn_match:
            year = reference_year or datetime.now().year
            date = f"{year:04d}-{int(cn_match.group(1)):02d}-{int(cn_match.group(2)):02d}"
            time_text = self._format_time_parts(cn_match.group(3), cn_match.group(4), cn_match.group(5))
            return {"value": f"{date} {time_text}", "date": date, "time": time_text, "raw": cn_match.group(0)}
        short_match = re.search(r"(?<!\d)(\d{1,2}):(\d{2})(?::(\d{2}))?(?!\d)", normalized)
        if short_match:
            time_text = self._format_time_parts(short_match.group(1), short_match.group(2), short_match.group(3))
            return {"value": time_text, "date": "", "time": time_text, "raw": short_match.group(0)}
        return None
    def _format_time_parts(self, hour: str, minute: str, second: str | None = None) -> str:
        base = f"{int(hour):02d}:{int(minute):02d}"
        if second is not None:
            return f"{base}:{int(second):02d}"
        return base
    def _extract_time_via_llm(
        self,
        *,
        request_text: str,
        title: str,
        description: str,
        reference_time: str = "",
    ) -> dict[str, str] | None:
        """Use LLM (fast_model) to extract fault time when regex fails."""
        from ..llm_client import LLMClient, LLMClientError

        client = LLMClient(self.config.ai_provider)
        if not client.is_available():
            return None

        parts: list[str] = []
        stripped = self._strip_urls_for_time_parse(request_text or "")
        if stripped.strip():
            parts.append(f"用户输入: {stripped.strip()}")
        if title:
            parts.append(f"标题: {title}")
        if description:
            parts.append(f"描述: {description}")
        if reference_time:
            parts.append(f"参考时间（创建时间）: {reference_time}")
        if not parts:
            return None

        now = datetime.now()
        system_prompt = (
            "你是一个时间提取器。从用户提供的文本中提取问题发生的精确时间。\n"
            f"当前时间: {now:%Y-%m-%d %H:%M}。\n"
            "规则:\n"
            "1. 优先取用户明确给出的时间，其次标题，最后描述。\n"
            "2. '5月19日_18点35分' 应解析为 2026-05-19 18:35。\n"
            "3. '昨天下午3点' 等相对时间，基于当前时间推算为绝对时间。\n"
            "4. 如果只有日期没有具体时间，found 设为 false。\n"
            "5. 年份缺失时用当前年份补全。\n"
            '返回 JSON: {"found": true/false, "datetime": "YYYY-MM-DD HH:MM", "source": "从哪段文字提取的", "reason": "简短说明"}\n'
            "只返回 JSON，不要其他文字。"
        )

        try:
            response = client.chat(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": "\n".join(parts)},
                ],
                model="",
                temperature=0.0,
                max_tokens=256,
                timeout_seconds=15,
                response_format={"type": "json_object"} if client._effective_api_format() == "openai" else None,
            )
        except (LLMClientError, Exception):  # noqa: BLE001
            logger.debug("LLM time extraction failed, falling back to regex-only")
            return None

        try:
            data = json.loads(response.content)
            if not data.get("found"):
                return None
            dt_str = str(data.get("datetime", ""))
            datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
            return {
                "value": dt_str,
                "date": dt_str.split(" ")[0],
                "time": dt_str.split(" ")[1],
                "raw": str(data.get("source", "")),
                "source": "llm",
                "note": str(data.get("reason", "")),
            }
        except (json.JSONDecodeError, ValueError, KeyError):
            logger.debug("LLM time extraction returned invalid JSON or format")
            return None
    def _select_reference_date(self, candidates: list[dict[str, str]]) -> str:
        for source in ("user", "title", "description", "llm"):
            for candidate in candidates:
                if candidate.get("source") == source and candidate.get("date"):
                    return str(candidate["date"])
        return ""
    def _bug_time_context_note(self, candidate: dict[str, str], *, reference_date: str, completed: bool) -> str:
        labels = {"user": "用户输入", "title": "标题", "description": "缺陷描述", "llm": "AI识别"}
        source = labels.get(candidate.get("source", ""), candidate.get("source", ""))
        if candidate.get("date"):
            return f"从{source}提取完整问题时间。"
        if completed and reference_date:
            return f"从{source}提取时分，并用 {reference_date} 补齐日期。"
        return f"从{source}只提取到时分，缺少日期。"
    def _scan_log_time_coverage(self, input_path: Path, *, fault_time: str) -> LogCoverage:
        fault_dt = self._parse_bug_datetime(fault_time)
        if fault_dt is None:
            return LogCoverage(
                has_time_evidence=False,
                covers_fault_time=False,
                reason="fault_time_not_full_datetime",
            )
        reference_year = fault_dt.year
        timestamps: list[datetime] = []
        scanned_files = 0
        scanned_lines = 0
        sample_file = ""
        for path in self._iter_log_coverage_files(input_path):
            scanned_files += 1
            file_dt = self._parse_log_file_datetime(path.name)
            if file_dt is not None:
                file_start = datetime(
                    file_dt.tm_year,
                    file_dt.tm_mon,
                    file_dt.tm_mday,
                    file_dt.tm_hour,
                    file_dt.tm_min,
                )
                timestamps.extend([file_start, file_start + timedelta(minutes=59, seconds=59)])
                sample_file = sample_file or str(path)
            try:
                with path.open("r", encoding="utf-8", errors="replace") as handle:
                    for index, line in enumerate(handle):
                        if index >= _BUG_LOG_COVERAGE_MAX_LINES_PER_FILE:
                            break
                        scanned_lines += 1
                        line_dt = self._parse_log_line_datetime(line, reference_year=reference_year)
                        if line_dt is None:
                            continue
                        timestamps.append(line_dt)
                        sample_file = sample_file or str(path)
            except OSError:
                continue
        if not timestamps:
            return LogCoverage(
                has_time_evidence=False,
                covers_fault_time=False,
                scanned_files=scanned_files,
                scanned_lines=scanned_lines,
                reason="no_log_time_found",
            )
        timestamps.sort()
        start = timestamps[0]
        end = timestamps[-1]
        window = timedelta(minutes=_BUG_LOG_COVERAGE_WINDOW_MINUTES)
        covers = start - window <= fault_dt <= end + window
        return LogCoverage(
            has_time_evidence=True,
            covers_fault_time=covers,
            start_time=self._format_bug_datetime_minute(start),
            end_time=self._format_bug_datetime_minute(end),
            scanned_files=scanned_files,
            scanned_lines=scanned_lines,
            sample_file=sample_file,
            reason="covered" if covers else "not_covering_fault_time",
        )
    def _iter_log_coverage_files(self, input_path: Path) -> list[Path]:
        if input_path.is_file():
            return [input_path] if self._is_log_coverage_file(input_path) else []
        candidates: list[Path] = []
        try:
            for path in input_path.rglob("*"):
                if len(candidates) >= _BUG_LOG_COVERAGE_MAX_FILES:
                    break
                if path.is_file() and self._is_log_coverage_file(path):
                    candidates.append(path)
        except OSError:
            return candidates
        candidates.sort(key=lambda item: str(item))
        return candidates
    def _is_log_coverage_file(self, path: Path) -> bool:
        lower_name = path.name.lower()
        return any(lower_name.endswith(suffix) for suffix in _BUG_LOG_COVERAGE_SUFFIXES)
    def _parse_log_line_datetime(self, line: str, *, reference_year: int) -> datetime | None:
        full_match = re.search(
            r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})[ T](\d{1,2}):(\d{2}):(\d{2})(?:\.\d+)?\b",
            line,
        )
        if full_match:
            return self._safe_datetime(
                int(full_match.group(1)),
                int(full_match.group(2)),
                int(full_match.group(3)),
                int(full_match.group(4)),
                int(full_match.group(5)),
                int(full_match.group(6)),
            )
        bracket_match = re.search(r"\[(20\d{2}-\d{2}-\d{2}) \+\d{4} (\d{2}:\d{2}:\d{2})\]", line)
        if bracket_match:
            try:
                return datetime.strptime(f"{bracket_match.group(1)} {bracket_match.group(2)}", "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return None
        md_match = re.search(r"(?<!\d)(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})(?:\.\d+)?", line)
        if md_match:
            return self._safe_datetime(
                reference_year,
                int(md_match.group(1)),
                int(md_match.group(2)),
                int(md_match.group(3)),
                int(md_match.group(4)),
                int(md_match.group(5)),
            )
        return None
    def _safe_datetime(self, year: int, month: int, day: int, hour: int, minute: int, second: int = 0) -> datetime | None:
        try:
            return datetime(year, month, day, hour, minute, second)
        except ValueError:
            return None
    def _parse_bug_datetime(self, value: str) -> datetime | None:
        normalized = self._normalize_fault_time_text(value)
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                return datetime.strptime(normalized, fmt)
            except ValueError:
                continue
        return None
    def _format_bug_datetime_minute(self, value: datetime) -> str:
        return value.strftime("%Y-%m-%d %H:%M")
    def _extract_fault_time(self, title: str, description: str) -> tuple[str, str]:
        match = re.search(r"(?:故障|发生|出现|问题|异常)?时间[：:]\s*(.+?)(?:\n|$)", description)
        if match:
            return self._normalize_fault_time_text(match.group(1)), "从缺陷描述提取"
        direct_match = re.search(r"(20\d{2}[-_/年]\d{1,2}[-_/月]\d{1,2}[日_\s-]*\d{1,2}:\d{2}(?::\d{2})?)", description)
        if direct_match:
            return self._normalize_fault_time_text(direct_match.group(1)), "从文本中的完整时间戳提取"
        short_match = re.search(r"(?<!\d)(\d{1,2}:\d{2})(?!\d)", description)
        if short_match:
            return self._normalize_fault_time_text(short_match.group(1)), "从文本中的时分提取"
        title_match = re.search(r"(20\d{2})年_(\d{1,2})月(\d{1,2})日_(\d{1,2}:\d{2})", title)
        if title_match:
            return (
                self._normalize_fault_time_text(
                    f"{title_match.group(1)}-{int(title_match.group(2)):02d}-{int(title_match.group(3)):02d} {title_match.group(4)}"
                ),
                "缺陷描述中未显式提供故障时间，退回使用标题中的时间戳",
            )
        return "", "缺陷描述和标题中都未识别到明确故障时间"
    def _normalize_fault_time_text(self, value: str) -> str:
        normalized = (
            value.strip()
            .replace("：", ":")
            .replace("年", "-")
            .replace("月", "-")
            .replace("日", " ")
            .replace("/", "-")
            .replace("_", " ")
        )
        normalized = re.sub(r"\s+", " ", normalized)
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
            if seconds is not None:
                return f"{base}:{int(seconds):02d}"
            return base
        short_match = re.search(r"(?<!\d)(\d{1,2}):(\d{2})(?::(\d{2}))?(?!\d)", normalized)
        if short_match:
            seconds = short_match.group(3)
            base = f"{int(short_match.group(1)):02d}:{int(short_match.group(2)):02d}"
            if seconds is not None:
                return f"{base}:{int(seconds):02d}"
            return base
        return normalized
