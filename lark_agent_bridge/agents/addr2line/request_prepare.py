"""addr2line 请求准备：资源下载、栈/prop 选择、时间解析。"""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import zipfile
from ...downloader import DownloadError, LogDownloader
from ...models import Addr2LineRequest, BridgeConfig, DownloadResource, LarkEvent, RomVersionLookupRequest, TaskResult, create_job_context
from ...parser import ROM_VERSION_RE
from .common import (
    _STACK_LINE_RE,
    _ADDR_SO_PAIR_RE,
    _MEMORY_MAP_LINE_RE,
    _CRASH_FILE_NAME_RE,
    _NAVIGATION_CRASH_TERMS,
    _MAX_TEXT_BYTES,
    _THREAD_SUMMARY_SYSTEM_LIBS,
    _SCENE_SO_HINT_TERMS,
    _NAVI_SYMBOL_SO_NAMES,
    _BUILTIN_UNITY_SO_NAMES,
    _NAPA5_SYMBOL_SO_NAMES,
    _TimePoint,
    _CrashStackSelection,
    _PropSelection,
    _PreparedResolveRequest,
    _SymbolSourceSelection,
    _ThreadTraceBlock,
    _FrameSummary,
)


class _RequestPrepareMixin:
    """addr2line 请求准备：资源下载、栈/prop 选择、时间解析。（与 Addr2LineRunner 共享 self 状态）。"""

    def _prepare_request(
        self,
        request: Addr2LineRequest,
        *,
        context,
        event: LarkEvent | None,
    ) -> _PreparedResolveRequest | TaskResult:
        addr_text = request.addr_text.strip()
        missing_version = not self._has_symbol_version(request)
        addr_source = ""
        prop_source = ""
        inferred_rom_version = ""
        selected_stack: _CrashStackSelection | None = None
        roots: list[Path] = []

        if request.resources and (not addr_text or missing_version or bool(request.log_folder.strip()) or bool(request.fault_time.strip())):
            roots = self._download_resource_roots(request.resources, context=context, event=event)
            if roots:
                selected_stack = self._select_navigation_stack(
                    roots,
                    extract_root=context.input_dir,
                    preferred_log_folder=request.log_folder,
                    fault_time=request.fault_time,
                )
                if not addr_text:
                    if selected_stack is None:
                        return TaskResult(
                            success=False,
                            message="缺少待反解地址：未在回复文件中找到可用的导航 crash 堆栈（`#xx pc ... lib*.so`）。",
                            job_id=context.job_id,
                            job_dir=context.job_dir,
                            error_code="missing_address",
                            details={"mode": "addr2line_resolve", "resource_count": len(request.resources)},
                        )
                    addr_text = selected_stack.addr_text
                    addr_source = str(selected_stack.source_path)
                preferred_log_folder = request.log_folder.strip().casefold() or (selected_stack.log_folder if selected_stack else "")
                if missing_version:
                    prop_selection = self._select_prop_file(
                        roots,
                        extract_root=context.input_dir,
                        preferred_log_folder=preferred_log_folder,
                    )
                    if prop_selection is not None:
                        inferred_rom_version = prop_selection.rom_version
                        prop_source = str(prop_selection.source_path)
            elif not addr_text:
                return TaskResult(
                    success=False,
                    message="缺少待反解地址：下载或展开日志后未找到可读的 crash 文件。",
                    job_id=context.job_id,
                    job_dir=context.job_dir,
                    error_code="missing_address",
                    details={"mode": "addr2line_resolve", "resource_count": len(request.resources)},
                )

        if not addr_text:
            return TaskResult(
                success=False,
                message="缺少待反解地址：请贴出 tombstone backtrace 中的 `#xx pc ... lib*.so` 行。",
                job_id=context.job_id,
                job_dir=context.job_dir,
                error_code="missing_address",
                details={"mode": "addr2line_resolve"},
            )

        effective_request = request
        if inferred_rom_version:
            effective_request = self._copy_request(
                request,
                addr_text=addr_text,
                rom_version=inferred_rom_version,
            )
        elif addr_text != request.addr_text:
            effective_request = self._copy_request(request, addr_text=addr_text)

        if inferred_rom_version and not effective_request.apk_version.strip() and not effective_request.napa_version.strip():
            symbol_outputs = self._resolve_navigation_symbol_outputs_from_rom(inferred_rom_version, event=event)
            if isinstance(symbol_outputs, TaskResult):
                return symbol_outputs
            navigation_version = str(symbol_outputs.get("navigation_version") or "").strip()
            if navigation_version:
                effective_request = self._copy_request(
                    effective_request,
                    apk_version=navigation_version,
                    symbol_table_url=str(symbol_outputs.get("symbol_table_url") or "").strip(),
                    napa5_download_url=str(symbol_outputs.get("napa5_download_url") or "").strip(),
                )

        if not self._has_symbol_version(effective_request):
            trace_report = self._build_subrealitytrace_thread_report(
                roots=roots,
                extract_root=context.input_dir,
                source_hint=selected_stack.source_path if selected_stack is not None else None,
            )
            if trace_report is not None:
                json_path = context.output_dir / "subrealitytrace_thread_parse.json"
                json_path.write_text(json.dumps(trace_report["payload"], ensure_ascii=False, indent=2), encoding="utf-8")
                return TaskResult(
                    success=True,
                    message=trace_report["message"],
                    job_id=context.job_id,
                    job_dir=context.job_dir,
                    json_report=json_path,
                    details={
                        "mode": "addr2line_resolve",
                        "analysis_mode": "single_trace_thread_parse",
                        "trace_source": trace_report["source_path"],
                        "thread_category_counts": trace_report["counts"],
                        "thread_section_time": trace_report["section_time"],
                    },
                )
            return TaskResult(
                success=False,
                message=(
                    "缺少符号表版本：请补充 ROM/Napa/APK 版本号，或确保附件里有可解析的 `prop.txt`。\n"
                    "如果要按时间定位，请尽量提供具体的几月几日、几点几分。"
                ),
                job_id=context.job_id,
                job_dir=context.job_dir,
                error_code="missing_symbol_version",
                details={"mode": "addr2line_resolve", "resource_count": len(request.resources)},
            )

        return _PreparedResolveRequest(
            request=effective_request,
            addr_source=addr_source,
            prop_source=prop_source,
            inferred_rom_version=inferred_rom_version,
        )

    def _download_resource_roots(
        self,
        resources: list[DownloadResource],
        *,
        context,
        event: LarkEvent | None,
    ) -> list[Path]:
        downloader = LogDownloader(self.config, self.lark_client)
        try:
            downloaded = downloader.download_all(
                resources,
                context=context,
                message_id=event.message_id if event else "",
            )
        except DownloadError:
            return []
        return [item.path for item in downloaded]

    def _select_navigation_stack(
        self,
        roots: list[Path],
        *,
        extract_root: Path,
        preferred_log_folder: str = "",
        fault_time: str = "",
    ) -> _CrashStackSelection | None:
        candidates: list[tuple[int, bool, int, list[str], Path, str, int | None, _TimePoint | None]] = []
        preferred_log_folder = preferred_log_folder.strip().casefold()
        target_time = self._parse_time_point(fault_time)
        sequence = 0
        for path in self._iter_text_candidates(roots, extract_root=extract_root):
            text = self._read_text_tail(path)
            if not text:
                continue
            log_folder = self._log_folder_name(path)
            log_index = self._log_folder_index(path)
            for _, stack_lines, context_text, timestamp in self._stack_candidates(text):
                lowered_context = context_text.casefold()
                is_navigation = any(term in lowered_context for term in _NAVIGATION_CRASH_TERMS)
                candidates.append(
                    (
                        self._stack_source_priority(path),
                        is_navigation,
                        sequence,
                        stack_lines,
                        path,
                        log_folder,
                        log_index,
                        timestamp,
                    )
                )
                sequence += 1
        if not candidates:
            return None
        navigation_candidates = [item for item in candidates if item[1]]
        pool = navigation_candidates or candidates
        if preferred_log_folder:
            matching_log = [item for item in pool if item[5] == preferred_log_folder]
            if matching_log:
                pool = matching_log
            elif not target_time:
                log_candidates = [item for item in pool if item[6] is not None]
                if log_candidates:
                    smallest_log_index = min(item[6] for item in log_candidates if item[6] is not None)
                    pool = [item for item in log_candidates if item[6] == smallest_log_index]
        elif not target_time:
            log_candidates = [item for item in pool if item[6] is not None]
            if log_candidates:
                smallest_log_index = min(item[6] for item in log_candidates if item[6] is not None)
                pool = [item for item in log_candidates if item[6] == smallest_log_index]
        if target_time:
            timed_pool = [item for item in pool if item[7] is not None]
            if timed_pool:
                selected = min(
                    timed_pool,
                    key=lambda item: (
                        self._time_distance(target_time, item[7]),
                        item[0],
                        -item[2],
                    ),
                )
                return _CrashStackSelection(
                    addr_text="\n".join(selected[3]),
                    source_path=selected[4],
                    log_folder=selected[5],
                    timestamp=selected[7],
                )
        best_source_priority = min(item[0] for item in pool)
        selected = [item for item in pool if item[0] == best_source_priority][-1]
        return _CrashStackSelection(
            addr_text="\n".join(selected[3]),
            source_path=selected[4],
            log_folder=selected[5],
            timestamp=selected[7],
        )

    def _select_prop_file(
        self,
        roots: list[Path],
        *,
        extract_root: Path,
        preferred_log_folder: str = "",
    ) -> _PropSelection | None:
        preferred_log_folder = preferred_log_folder.strip().casefold()
        candidates = sorted(
            (
                path
                for path in self._iter_text_candidates(roots, extract_root=extract_root)
                if path.name.casefold() == "prop.txt"
            ),
            key=lambda path: (self._prop_priority(path, preferred_log_folder), str(path)),
        )
        for path in candidates:
            rom_version = self._extract_rom_from_prop(path)
            if rom_version:
                return _PropSelection(
                    rom_version=rom_version,
                    source_path=path,
                    log_folder=self._log_folder_name(path),
                )
        return None

    def _iter_text_candidates(self, roots: list[Path], *, extract_root: Path) -> list[Path]:
        files: list[Path] = []
        for root in roots:
            files.extend(self._expand_candidate_root(root, extract_root=extract_root))
        return sorted(files, key=lambda item: (self._stack_source_priority(item), str(item)))

    def _prop_priority(self, path: Path, preferred_log_folder: str) -> tuple[int, int]:
        log_folder = self._log_folder_name(path)
        log_index = self._log_folder_index(path)
        if preferred_log_folder:
            if log_folder == preferred_log_folder:
                return (0, log_index if log_index is not None else 0)
            if log_index is not None:
                return (1, log_index)
            return (2, 10_000)
        if log_index is not None:
            return (0, log_index)
        return (1, 10_000)

    def _stack_source_priority(self, path: Path) -> int:
        parts = [part.casefold() for part in path.parts]
        name = path.name.casefold()
        if "logd" in parts and name.startswith("crash"):
            return 0
        if _CRASH_FILE_NAME_RE.search(str(path)):
            return 1
        return 2

    def _expand_candidate_root(self, root: Path, *, extract_root: Path) -> list[Path]:
        if root.is_dir():
            return [path for path in root.rglob("*") if path.is_file() and self._looks_text_candidate(path)]
        if root.is_file() and zipfile.is_zipfile(root):
            extract_dir = extract_root / f"{root.name}_unzipped"
            if not extract_dir.exists():
                self._safe_extract_zip(root, extract_dir)
            return [path for path in extract_dir.rglob("*") if path.is_file() and self._looks_text_candidate(path)]
        if root.is_file() and self._looks_text_candidate(root):
            return [root]
        return []

    def _looks_text_candidate(self, path: Path) -> bool:
        lower_name = path.name.casefold()
        if lower_name.endswith((".pb", ".png", ".jpg", ".jpeg", ".mp4", ".db", ".dat")):
            return False
        if _CRASH_FILE_NAME_RE.search(str(path)):
            return True
        if lower_name.endswith((".txt", ".log", ".alog", ".xlog", ".trace")):
            return True
        return "." not in path.name

    def _safe_extract_zip(self, archive_path: Path, extract_dir: Path) -> None:
        extract_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive_path) as zf:
            root = extract_dir.resolve()
            for info in zf.infolist():
                if info.is_dir():
                    continue
                target = (extract_dir / info.filename).resolve()
                if root != target and root not in target.parents:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)

    def _read_text_tail(self, path: Path) -> str:
        try:
            size = path.stat().st_size
            with path.open("rb") as fh:
                if size > _MAX_TEXT_BYTES:
                    fh.seek(size - _MAX_TEXT_BYTES)
                data = fh.read(_MAX_TEXT_BYTES)
        except OSError:
            return ""
        return data.decode("utf-8", errors="ignore")

    def _stack_candidates(self, text: str) -> list[tuple[int, list[str], str, _TimePoint | None]]:
        lines = text.splitlines()
        candidates: list[tuple[int, list[str], str, _TimePoint | None]] = []
        index = 0
        while index < len(lines):
            if _STACK_LINE_RE.search(lines[index]) is None:
                index += 1
                continue
            start = index
            stack_lines: list[str] = []
            while index < len(lines) and _STACK_LINE_RE.search(lines[index]) is not None:
                stack_lines.append(lines[index].strip())
                index += 1
            context_start = max(0, start - 40)
            context_end = min(len(lines), index + 5)
            candidates.append(
                (
                    start,
                    stack_lines,
                    "\n".join(lines[context_start:context_end]),
                    self._parse_time_point(lines[start]),
                )
            )
        return candidates

    def _copy_request(self, request: Addr2LineRequest, **overrides: str) -> Addr2LineRequest:
        return Addr2LineRequest(
            addr_text=overrides.get("addr_text", request.addr_text),
            resources=request.resources,
            raw_text=request.raw_text,
            rom_version=overrides.get("rom_version", request.rom_version),
            napa_version=overrides.get("napa_version", request.napa_version),
            apk_version=overrides.get("apk_version", request.apk_version),
            symbol_table_url=overrides.get("symbol_table_url", request.symbol_table_url),
            napa5_download_url=overrides.get("napa5_download_url", request.napa5_download_url),
            log_folder=overrides.get("log_folder", request.log_folder),
            fault_time=overrides.get("fault_time", request.fault_time),
            target=overrides.get("target", request.target),
            prompt=request.prompt,
            triggered=request.triggered,
            error=request.error,
        )

    def _has_symbol_version(self, request: Addr2LineRequest) -> bool:
        return bool(request.rom_version.strip() or request.napa_version.strip() or request.apk_version.strip())

    def _extract_rom_from_prop(self, path: Path) -> str:
        text = self._read_text_tail(path)
        match = ROM_VERSION_RE.search(text)
        return match.group(1) if match else ""

    def _log_folder_name(self, path: Path) -> str:
        for part in path.parts:
            lowered = part.casefold()
            if re.fullmatch(r"log\d+", lowered):
                return lowered
        return ""

    def _log_folder_index(self, path: Path) -> int | None:
        log_folder = self._log_folder_name(path)
        if not log_folder:
            return None
        try:
            return int(log_folder[3:])
        except ValueError:
            return None

    def _parse_time_point(self, text: str) -> _TimePoint | None:
        full_match = re.search(
            r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?\b",
            text,
        )
        if full_match:
            return _TimePoint(
                year=int(full_match.group(1)),
                month=int(full_match.group(2)),
                day=int(full_match.group(3)),
                hour=int(full_match.group(4)),
                minute=int(full_match.group(5)),
                second=int(full_match.group(6) or 0),
            )
        month_day_match = re.search(
            r"(?<!\d)(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?",
            text,
        )
        if month_day_match:
            return _TimePoint(
                month=int(month_day_match.group(1)),
                day=int(month_day_match.group(2)),
                hour=int(month_day_match.group(3)),
                minute=int(month_day_match.group(4)),
                second=int(month_day_match.group(5) or 0),
            )
        short_match = re.search(r"(?<!\d)(\d{1,2}):(\d{2})(?::(\d{2}))?(?!\d)", text)
        if short_match:
            return _TimePoint(
                hour=int(short_match.group(1)),
                minute=int(short_match.group(2)),
                second=int(short_match.group(3) or 0),
            )
        return None

    def _time_distance(self, target: _TimePoint, candidate: _TimePoint | None) -> float:
        if candidate is None:
            return float("inf")
        if target.month is not None and target.day is not None and candidate.month is not None and candidate.day is not None:
            return abs(self._date_time_seconds(target) - self._date_time_seconds(candidate))
        return abs(self._time_of_day_seconds(target) - self._time_of_day_seconds(candidate))

    def _time_of_day_seconds(self, value: _TimePoint) -> int:
        return value.hour * 3600 + value.minute * 60 + value.second

    def _date_time_seconds(self, value: _TimePoint) -> int:
        month = value.month or 0
        day = value.day or 0
        return (((month * 32) + day) * 24 * 3600) + self._time_of_day_seconds(value)
