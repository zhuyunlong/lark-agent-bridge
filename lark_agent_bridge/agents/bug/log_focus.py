from __future__ import annotations

from ._shared import *  # noqa: F401,F403


_FOCUS_LOOKBACK_SECONDS = 3600
_FOCUS_FORWARD_BUFFER_SECONDS = 300
_FOCUS_MAX_LINES_PER_FILE = 15000
_FOCUS_MIN_LINES_PER_FILE = 2000
_FOCUS_MAX_LOOKBACK_SECONDS = 21600
_FOCUS_EXPAND_STEP_SECONDS = 3600
_FOCUS_LOGD_PREFIXES = ("kernel", "main", "events", "crash")


class _LogFocusMixin:
    """日志聚焦：主导 PID 判定、按故障时间裁剪日志、montecarlo/logd 焦点目录构建（与 CustomSkillMixin 共享 self 状态）。"""

    def _detect_dominant_pid(
        self,
        lines: list[str],
        fault_dt: "datetime | None",
        *,
        reference_year: int,
        window_seconds: int = 120,
    ) -> "str | None":
        """故障时刻 ±window_seconds 内出现最频繁的 PID（时间戳后第一个数字）。"""
        if fault_dt is None:
            return None
        counts: dict[str, int] = {}
        for line in lines:
            line_dt = self._parse_log_line_datetime(line, reference_year=reference_year)
            if line_dt is None:
                continue
            if abs((line_dt - fault_dt).total_seconds()) > window_seconds:
                continue
            match = re.search(r"\d{2}:\d{2}:\d{2}(?:\.\d+)?\s+(\d+)\s+\d+", line)
            if not match:
                continue
            pid = match.group(1)
            counts[pid] = counts.get(pid, 0) + 1
        if not counts:
            return None
        return max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]
    def _clip_log_lines(
        self,
        lines: list[str],
        fault_dt: "datetime | None",
        *,
        is_main_log: bool,
    ) -> list[str]:
        """按时间窗 + 单文件上限末段 + （主日志）PID 连续性裁行。

        fault_dt 为 None 时退化为「保留末段最多 _FOCUS_MAX_LINES_PER_FILE 行」。
        """
        if fault_dt is None:
            return lines[-_FOCUS_MAX_LINES_PER_FILE:]
        ref_year = fault_dt.year
        fault_ts = fault_dt.timestamp()
        upper = fault_ts + _FOCUS_FORWARD_BUFFER_SECONDS

        parsed: list[tuple[str, "float | None", "str | None"]] = []
        for line in lines:
            ldt = self._parse_log_line_datetime(line, reference_year=ref_year)
            ts = ldt.timestamp() if ldt is not None else None
            pid = None
            pm = re.search(r"\d{2}:\d{2}:\d{2}(?:\.\d+)?\s+(\d+)\s+\d+", line)
            if pm:
                pid = pm.group(1)
            parsed.append((line, ts, pid))

        target_pid = (
            self._detect_dominant_pid(lines, fault_dt, reference_year=ref_year)
            if is_main_log
            else None
        )

        def _window(lookback: int) -> list[tuple[str, "float | None", "str | None"]]:
            lower = fault_ts - lookback
            sel = []
            for item in parsed:
                _, ts, pid = item
                if ts is None:
                    continue
                if ts < lower or ts > upper:
                    continue
                if target_pid is not None and pid is not None and pid != target_pid:
                    continue
                sel.append(item)
            return sel

        def _expanded_range_hits_other_pid(previous_lookback: int, next_lookback: int) -> bool:
            if target_pid is None:
                return False
            previous_lower = fault_ts - previous_lookback
            next_lower = fault_ts - next_lookback
            for _, ts, pid in parsed:
                if ts is None or pid is None:
                    continue
                if next_lower <= ts < previous_lower and ts <= upper and pid != target_pid:
                    return True
            return False

        lookback = _FOCUS_LOOKBACK_SECONDS
        selected = _window(lookback)
        while (
            len(selected) < _FOCUS_MIN_LINES_PER_FILE
            and lookback < _FOCUS_MAX_LOOKBACK_SECONDS
        ):
            next_lookback = min(lookback + _FOCUS_EXPAND_STEP_SECONDS, _FOCUS_MAX_LOOKBACK_SECONDS)
            if is_main_log and _expanded_range_hits_other_pid(lookback, next_lookback):
                break
            lookback = next_lookback
            selected = _window(lookback)

        out = [item[0] for item in selected]
        if len(out) > _FOCUS_MAX_LINES_PER_FILE:
            out = out[-_FOCUS_MAX_LINES_PER_FILE:]
        return out
    def _is_montecarlo_path(self, path: Path) -> bool:
        parts = [part.casefold() for part in path.parts]
        return any("montecarlo" in part for part in parts)
    def _is_key_logd_path(self, path: Path) -> bool:
        parts = [part.casefold() for part in path.parts]
        if not any(part == "logd" for part in parts):
            return False
        name = path.name.casefold()
        return any(name.startswith(prefix) for prefix in _FOCUS_LOGD_PREFIXES)
    def _is_key_logd_coverage_file(self, path: Path) -> bool:
        if not self._is_key_logd_path(path):
            return False
        if self._is_log_coverage_file(path):
            return True
        name = path.name.casefold()
        return bool(re.match(r"^(kernel|main|events|crash)(?:[._-].*)?$", name))
    def _key_logd_prefix(self, path: Path) -> str:
        if not self._is_key_logd_path(path):
            return ""
        name = path.name.casefold()
        for prefix in _FOCUS_LOGD_PREFIXES:
            if name.startswith(prefix):
                return prefix
        return ""
    def _iter_key_logd_files(self, input_path: Path | None) -> list[Path]:
        if input_path is None or not input_path.exists():
            return []
        if input_path.is_file():
            return [input_path] if self._is_key_logd_coverage_file(input_path) else []
        matches: list[Path] = []
        try:
            for logd_dir in input_path.rglob("logd"):
                if not logd_dir.is_dir():
                    continue
                for path in logd_dir.iterdir():
                    if path.is_file() and self._is_key_logd_coverage_file(path):
                        matches.append(path)
        except OSError:
            return matches
        return sorted(matches, key=lambda item: str(item))
    def _find_uncalibrated_montecarlo_logs(
        self,
        all_files: list[Path],
        fault_dt: "datetime | None",
    ) -> list[Path]:
        """Return montecarlo logs whose filename year is clearly uncalibrated (>2yr off)."""
        if fault_dt is None:
            return []
        result: list[Path] = []
        for path in all_files:
            if not self._is_montecarlo_path(path):
                continue
            file_dt = self._parse_log_file_datetime(path.name)
            if file_dt is None:
                continue
            if abs(file_dt.tm_year - fault_dt.year) > 2:
                result.append(path)
        return result
    def _events_log_confirms_process_start(
        self,
        events_files: list[Path],
        package: str = "com.xiaopeng.montecarlo",
    ) -> bool:
        """Check whether events log contains am_proc_start for the given package."""
        pattern = re.compile(rf"am_proc_start:.*{re.escape(package)}")
        for path in events_files:
            try:
                with path.open(encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        if pattern.search(line):
                            return True
            except OSError:
                continue
        return False
    def _confirmed_uncalibrated_montecarlo_logs(
        self,
        *,
        all_files: list[Path],
        key_logd_files: list[Path],
        fault_dt: "datetime | None",
    ) -> list[Path]:
        uncalibrated = self._find_uncalibrated_montecarlo_logs(all_files, fault_dt)
        if not uncalibrated:
            return []
        events_files = [p for p in key_logd_files if p.name.casefold().startswith("events")]
        if not events_files:
            # Flat backslash paths: filename like "ALLlog\log1\logd\events.txt"
            events_files = [
                p for p in all_files
                if "logd" in p.name.casefold() and "events" in p.name.casefold()
            ]
        if events_files and self._events_log_confirms_process_start(events_files):
            return uncalibrated
        return []
    def _file_agent_focus_rank(self, path: Path) -> int:
        name = path.name.casefold()
        if self._is_montecarlo_path(path):
            return 0
        if self._is_key_logd_path(path):
            if name.startswith(("kernel", "main")):
                return 1
            return 2
        if name in {"prop.txt", "dfx.txt"}:
            return 3
        if self._is_montecarlo_or_logd_path(path):
            return 4
        return 5
    def _file_agent_focus_candidates(
        self,
        *,
        input_path: Path | None,
        fault_time: str,
        analysis_kind: str,
    ) -> list[Path]:
        if input_path is None or not input_path.exists():
            return []
        if input_path.is_file():
            return [input_path]
        fault_dt = self._parse_bug_datetime(fault_time)
        all_files = self._iter_log_coverage_files(input_path)
        key_logd_files = self._iter_key_logd_files(input_path)
        if key_logd_files:
            files_by_resolved = {path.resolve(): path for path in all_files}
            for path in key_logd_files:
                files_by_resolved.setdefault(path.resolve(), path)
            all_files = sorted(files_by_resolved.values(), key=lambda item: str(item))
        if not all_files:
            return []
        selected: list[Path] = []
        decoded_aux: list[Path] = []
        for path in all_files:
            if path.name in {"prop.txt", "dfx.txt"}:
                decoded_aux.append(path)
                continue
            file_dt = self._parse_log_file_datetime(path.name)
            if file_dt is None:
                # 主日志（logd/montecarlo）文件名常无时间戳；裁行阶段会按故障时间窗收窄，故此处直接纳入
                if self._is_montecarlo_or_logd_path(path):
                    selected.append(path)
                continue
            if fault_dt is None:
                continue
            candidate_dt = datetime.fromtimestamp(time.mktime(file_dt))
            if abs((candidate_dt - fault_dt).total_seconds()) > 3600:
                continue
            selected.append(path)
        # Include montecarlo logs with uncalibrated timestamps (e.g. year 2010)
        # when events log confirms the process was running.
        uncalibrated = self._confirmed_uncalibrated_montecarlo_logs(
            all_files=all_files,
            key_logd_files=key_logd_files,
            fault_dt=fault_dt,
        )
        if uncalibrated:
            selected_resolved = {p.resolve() for p in selected}
            pending = [p for p in uncalibrated if p.resolve() not in selected_resolved]
            if pending:
                selected.extend(pending)
        if not selected and fault_dt is not None:
            for path in all_files:
                file_dt = self._parse_log_file_datetime(path.name)
                if file_dt is None:
                    continue
                candidate_dt = datetime.fromtimestamp(time.mktime(file_dt))
                if abs((candidate_dt - fault_dt).total_seconds()) <= 3600:
                    selected.append(path)
        ranked = sorted(
            [*decoded_aux, *selected],
            key=lambda item: (
                self._file_agent_focus_rank(item.resolve()),
                self._log_file_priority(item.resolve()),
                str(item.resolve()),
            ),
        )
        # Guarantee each key logd family is not squeezed out when one family or
        # MonteCarlo files dominate the list.
        key_logd = [p for p in ranked if self._is_key_logd_path(p.resolve())]
        reserved: list[Path] = []
        reserved_resolved: set[Path] = set()
        for prefix in _FOCUS_LOGD_PREFIXES:
            for path in key_logd:
                resolved = path.resolve()
                if resolved in reserved_resolved or self._key_logd_prefix(resolved) != prefix:
                    continue
                reserved.append(path)
                reserved_resolved.add(resolved)
                break
        rest = [p for p in ranked if p.resolve() not in reserved_resolved]
        return reserved + rest[:24 - len(reserved)]
    def _build_file_agent_focus_dir(
        self,
        *,
        input_path: Path | None,
        fault_time: str,
        analysis_kind: str,
        analysis_dir: Path,
    ) -> tuple[Path | None, Path | None, list[Path]]:
        candidates = self._file_agent_focus_candidates(
            input_path=input_path,
            fault_time=fault_time,
            analysis_kind=analysis_kind,
        )
        if not candidates:
            return input_path, None, []
        focus_dir = analysis_dir / "log_focus"
        manifest_path = analysis_dir / "log_focus.md"
        focus_dir.mkdir(parents=True, exist_ok=True)
        copied: list[Path] = []
        fault_dt = self._parse_bug_datetime(fault_time)
        uncalibrated_resolved: set[Path] = set()
        if input_path is not None and input_path.exists():
            all_files = self._iter_log_coverage_files(input_path)
            key_logd_files = self._iter_key_logd_files(input_path)
            if key_logd_files:
                files_by_resolved = {path.resolve(): path for path in all_files}
                for path in key_logd_files:
                    files_by_resolved.setdefault(path.resolve(), path)
                all_files = sorted(files_by_resolved.values(), key=lambda item: str(item))
            uncalibrated_resolved = {
                path.resolve()
                for path in self._confirmed_uncalibrated_montecarlo_logs(
                    all_files=all_files,
                    key_logd_files=key_logd_files,
                    fault_dt=fault_dt,
                )
            }
        for source in candidates:
            if input_path is not None and input_path.exists() and input_path.is_dir():
                try:
                    relative = source.relative_to(input_path)
                except ValueError:
                    relative = Path(source.name)
            else:
                relative = Path(source.name)
            dest = focus_dir / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                with source.open(encoding="utf-8", errors="replace") as handle:
                    raw_lines = [line.rstrip("\n") for line in handle]
            except OSError:
                continue
            if source.resolve() in uncalibrated_resolved:
                clipped = raw_lines[-_FOCUS_MAX_LINES_PER_FILE:]
            else:
                clipped = self._clip_log_lines(
                    raw_lines,
                    fault_dt,
                    is_main_log=self._is_montecarlo_path(source),
                )
            if not clipped:
                continue
            try:
                dest.write_text("\n".join(clipped) + "\n", encoding="utf-8")
            except OSError:
                continue
            copied.append(dest)
        lines = [
            "# Log Focus",
            "",
            f"- 原始日志输入: `{input_path}`" if input_path else "- 原始日志输入: 未提供",
            f"- 聚焦目录: `{focus_dir}`",
            f"- 故障时间: `{fault_time or '未识别'}`",
            "",
            "## 已挑选文件",
        ]
        if copied:
            lines.extend(f"- `{path}`" for path in copied)
        else:
            lines.append("- 未复制出聚焦文件，继续使用原始输入目录。")
        manifest_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        return (focus_dir if copied else input_path), manifest_path, copied
