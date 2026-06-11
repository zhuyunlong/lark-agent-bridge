from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from . import _shared


class _LdLogEvidenceMixin:
    """LD 日志定位与证据：日志文件发现/过滤、prepared 元数据、grep 证据提取、焦点日志候选与 zst 解压（与 LdExecutorMixin 共享 self 状态）。"""

    def _ld_executor_grep_pattern(self) -> str:
        return "|".join(self._LD_GREP_PATTERNS)
    def _normalize_log_locator(self, value: str | Path) -> str:
        return str(value).replace("\\", "/")
    def _log_basename_from_locator(self, value: str | Path) -> str:
        normalized = self._normalize_log_locator(value)
        return normalized.rsplit("/", 1)[-1]
    def _ld_prepared_log_metadata_path(
        self,
        *,
        prepared_input: Path | None,
        analysis_dir: Path,
        fault_time: str,
    ) -> Path | None:
        if prepared_input is None:
            return None
        analysis_dir.mkdir(parents=True, exist_ok=True)
        metadata_path = analysis_dir / "prepared_log_metadata.md"
        candidates: list[Path] = []
        seen: set[Path] = set()

        def _append(path: Path) -> None:
            if path in seen or not path.exists():
                return
            seen.add(path)
            candidates.append(path)

        _append(prepared_input)
        if prepared_input.is_file():
            lower_name = prepared_input.name.lower()
            if lower_name.endswith((".alog", ".xlog")):
                _append(prepared_input.with_name(prepared_input.name + ".log"))
            elif lower_name.endswith((".alog.log", ".xlog.log")):
                _append(prepared_input.with_suffix(""))
            fault_dt = self._parse_bug_datetime(fault_time)
            try:
                siblings = sorted(prepared_input.parent.iterdir())
            except OSError:
                siblings = []
            for sibling in siblings:
                if len(candidates) >= 8:
                    break
                if not sibling.is_file():
                    continue
                normalized = self._normalize_log_locator(sibling.name).lower()
                if not normalized.endswith((".alog", ".alog.log", ".xlog", ".xlog.log", ".log")):
                    continue
                if fault_dt is None:
                    _append(sibling)
                    continue
                basename = self._log_basename_from_locator(sibling.name)
                file_dt = self._parse_log_file_datetime(basename)
                if file_dt is None:
                    continue
                candidate_dt = datetime.fromtimestamp(time.mktime(file_dt))
                if abs((candidate_dt - fault_dt).total_seconds()) <= 7200:
                    _append(sibling)
        else:
            fault_dt = self._parse_bug_datetime(fault_time)
            for path in self._ld_focus_log_candidates(prepared_input, fault_dt, limit=8):
                _append(path)

        lines = [
            "# Prepared Log Metadata",
            "",
            f"- 故障时间: `{fault_time or '未识别'}`",
            f"- 主输入: `{prepared_input}`",
            f"- 主工作目录: `{prepared_input.parent if prepared_input.is_file() else prepared_input}`",
            "- 使用规则: 先调用 `read_prepared_log_metadata()`，首轮只允许围绕下面列出的路径检索。",
            "- 默认聚焦: 除非 Skill 另有建议，车道级/导航日志默认优先看 `com.xiaopeng.montecarlo` 与 `logd`，并以覆盖故障时间的文件为先（下方候选已按此排序，且 `.zst` 已解压为 `.log`）。",
            "- 限制: 不要扫描 `tools/lark-agent-bridge/data/bug_cache`、历史 job 输出或整个工作区；只有当这些候选文件明确不覆盖问题时间时，才允许扩到同目录相邻小时日志。",
            "",
            "## 候选日志",
        ]
        if candidates:
            lines.extend(f"- `{path}`" for path in candidates)
        else:
            lines.append("- 无可用候选文件")
        metadata_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        return metadata_path
    def _ld_executor_should_include_log_file(
        self,
        path: Path,
        *,
        locator: str | Path,
        fault_dt: "time.struct_time | None",
    ) -> bool:
        normalized = self._normalize_log_locator(locator).lower()
        if not normalized.endswith((".alog", ".alog.log", ".xlog", ".xlog.log", ".log")):
            return False
        if path.suffix == ".alog" and path.with_suffix(".alog.log").exists():
            return False
        basename = self._log_basename_from_locator(locator)
        file_dt = self._parse_log_file_datetime(basename)
        if fault_dt is None or file_dt is None:
            return True
        candidate_dt = datetime.fromtimestamp(time.mktime(file_dt))
        return abs((candidate_dt - fault_dt).total_seconds()) <= 7200
    def _ld_executor_find_log_files(
        self,
        *,
        cache_dir: Path,
        fault_time: str,
    ) -> list[Path]:
        """Find montecarlo/LD-relevant log files near fault time."""
        fault_dt = self._parse_bug_datetime(fault_time)
        results: list[Path] = []
        seen: set[Path] = set()
        logs_dir = cache_dir / "logs"

        def _append_candidate(path: Path, *, locator: str | Path) -> None:
            if path in seen or not path.is_file():
                return
            if not self._ld_executor_should_include_log_file(path, locator=locator, fault_dt=fault_dt):
                return
            seen.add(path)
            results.append(path)

        if logs_dir.exists():
            for pkg in self._LD_LOG_PACKAGES:
                for log_root in sorted(logs_dir.glob(f"data/Log/log*/app/{pkg}")):
                    for alog in sorted(log_root.glob("*.alog*")):
                        _append_candidate(alog, locator=alog.name)
            for path in sorted(logs_dir.rglob("*")):
                if not path.is_file():
                    continue
                normalized = self._normalize_log_locator(path.relative_to(logs_dir)).lower()
                if not any(pkg in normalized for pkg in self._LD_LOG_PACKAGES):
                    continue
                _append_candidate(path, locator=normalized)
        # Also check ZIP for montecarlo logs near fault time
        for zip_path in sorted((cache_dir / "attachments").glob("*.zip")) if (cache_dir / "attachments").exists() else []:
            try:
                with zipfile.ZipFile(zip_path, "r") as zf:
                    for info in zf.infolist():
                        if info.is_dir():
                            continue
                        name_lower = info.filename.lower()
                        if not any(pkg in name_lower for pkg in self._LD_LOG_PACKAGES):
                            continue
                        if not name_lower.endswith((".alog", ".alog.log", ".xlog", ".xlog.log", ".log")):
                            continue
                        basename = self._log_basename_from_locator(info.filename)
                        file_dt = self._parse_log_file_datetime(basename)
                        if fault_dt is not None and file_dt is not None:
                            candidate_dt = datetime.fromtimestamp(time.mktime(file_dt))
                            if abs((candidate_dt - fault_dt).total_seconds()) > 7200:
                                continue
                        # Extract to temp location for reading
                        extracted = cache_dir / "logs" / info.filename
                        if not extracted.exists():
                            extracted.parent.mkdir(parents=True, exist_ok=True)
                            try:
                                with zf.open(info) as src, open(extracted, "wb") as dst:
                                    shutil.copyfileobj(src, dst)
                            except OSError:
                                continue
                        results.append(extracted)
            except (zipfile.BadZipFile, OSError):
                continue
        return sorted(set(results))
    def _ld_executor_extract_evidence(
        self,
        *,
        log_files: list[Path],
        fault_time: str,
        max_lines: int = 300,
    ) -> str:
        """Extract LD-relevant lines from log files using subprocess grep for speed."""
        pattern = self._ld_executor_grep_pattern()
        fault_dt = self._parse_bug_datetime(fault_time)
        evidence_parts: list[str] = []
        total_lines = 0
        # Compute time window for grep pre-filter (fault ±3min)
        time_grep_pattern = ""
        if fault_dt is not None:
            hour = fault_dt.hour
            minute = fault_dt.minute
            time_parts: list[str] = []
            for offset in range(-3, 4):
                m = minute + offset
                h = hour + (m // 60)
                m = m % 60
                if h < 0 or h > 23:
                    continue
                time_parts.append(f" {h:02d}:{m:02d}:")
            if time_parts:
                time_grep_pattern = "|".join(time_parts)
        for log_file in log_files:
            if total_lines >= max_lines:
                break
            if not log_file.exists():
                continue
            # Skip binary files
            try:
                raw = log_file.read_bytes()[:512]
                if b"\x00" in raw:
                    continue
            except OSError:
                continue
            # Use subprocess grep for speed on large files
            # Pipeline: grep time window first (fast), then grep LD patterns
            try:
                if time_grep_pattern:
                    grep_cmd = f"grep -E {shlex.quote(time_grep_pattern)} {shlex.quote(str(log_file))} | grep -iE {shlex.quote(pattern)}"
                else:
                    grep_cmd = f"grep -iE {shlex.quote(pattern)} {shlex.quote(str(log_file))}"
                result = subprocess.run(
                    grep_cmd, shell=True, capture_output=True, text=True,
                    timeout=30, check=False,
                )
                lines = result.stdout.splitlines()
            except (subprocess.TimeoutExpired, OSError):
                continue
            remaining = max_lines - total_lines
            lines = lines[:remaining]
            if lines:
                relative_name = log_file.name
                try:
                    parts = log_file.parts
                    app_idx = next((i for i, p in enumerate(parts) if p == "app"), None)
                    if app_idx is not None and app_idx + 1 < len(parts):
                        relative_name = "/".join(parts[app_idx:])
                except (StopIteration, IndexError):
                    pass
                evidence_parts.append(
                    f"### {relative_name}\n"
                    f"匹配行数: {len(lines)}\n"
                    f"```\n" + "\n".join(lines) + "\n```\n"
                )
                total_lines += len(lines)
        if not evidence_parts:
            return (
                "## LD 日志证据提取结果\n\n"
                f"**未在 montecarlo 日志中找到 LD 相关证据行。**\n"
                f"已搜索 {len(log_files)} 个日志文件，grep 模式: `{pattern[:80]}...`\n"
                f"故障时间: {fault_time}\n\n"
                "可能原因：故障时间对应的 montecarlo 日志不在当前日志包中。\n"
            )
        return (
            f"## LD 日志证据提取结果\n\n"
            f"故障时间: {fault_time}\n"
            f"搜索文件数: {len(log_files)}\n"
            f"匹配行数: {total_lines}\n\n"
            + "\n".join(evidence_parts)
        )
    def _parse_any_log_datetime(self, name: str) -> datetime | None:
        """Parse a datetime from a log filename.

        Supports ``main_YYYY-MM-DD_HH-MM`` and the montecarlo
        ``DIAG_D-NNN-YYYYMMDD-HHMMSS_...`` naming.
        """
        st = self._parse_log_file_datetime(name)
        if st is not None:
            try:
                return datetime.fromtimestamp(time.mktime(st))
            except (OverflowError, ValueError, OSError):
                return None
        m = re.search(r"(20\d{2})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})", name)
        if m:
            return self._safe_datetime(*(int(m.group(i)) for i in range(1, 7)))
        return None
    def _is_montecarlo_or_logd_path(self, path: Path) -> bool:
        parts = [part.casefold() for part in path.parts]
        return any(part == "logd" for part in parts) or any("montecarlo" in part for part in parts)
    def _decompress_zst(self, src: Path) -> Path | None:
        """zstd-decompress ``src`` (``*.zst``) to the path without the suffix."""
        src = src.resolve()
        dst = src.with_suffix("")
        if dst.exists():
            return dst
        zstd = shutil.which("zstd")
        if not zstd:
            logger.warning("zstd not found; cannot decompress %s", src)
            return None
        try:
            result = _run_tracked_process(
                [zstd, "-d", "-q", "-k", "-o", str(dst), str(src)],
                watchdog=self.process_watchdog,
                name="prepare-zst-decompress",
                cwd=self._working_dir(),
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except Exception as exc:
            logger.warning("zstd decompress failed for %s: %s", src, exc)
            return None
        if getattr(result, "returncode", 1) != 0 or not dst.exists():
            logger.warning("zstd decompress returned non-zero for %s", src)
            return None
        return dst
    def _ld_focus_log_candidates(self, root: Path, fault_dt: datetime | None, *, limit: int = 8) -> list[Path]:
        """Rank readable log candidates, defaulting to montecarlo/logd near the fault time.

        Montecarlo/logd logs rank first, then by time distance to the fault. Any
        selected montecarlo/logd ``.zst`` nav log is decompressed in place so the
        agent gets readable ``.log`` files covering the fault time by default.
        """
        scored: list[tuple[int, float, str, Path]] = []
        preferred: list[tuple[float, str, Path]] = []
        others: list[tuple[float, str, Path]] = []
        window_seconds = float(6 * 3600)
        try:
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                is_zst = path.name.lower().endswith(".zst")
                montecarlo_or_logd = self._is_montecarlo_or_logd_path(path)
                if not (self._is_log_coverage_file(path) or (is_zst and montecarlo_or_logd)):
                    continue
                file_dt = self._parse_any_log_datetime(path.name)
                if fault_dt is not None and file_dt is not None:
                    distance = abs((file_dt - fault_dt).total_seconds())
                elif file_dt is None:
                    distance = float(10 ** 9)
                else:
                    distance = 0.0
                if montecarlo_or_logd:
                    # Bound montecarlo/logd to the fault window so the candidate
                    # list defaults to logs covering the problem time.
                    if fault_dt is None or file_dt is None or distance <= window_seconds:
                        preferred.append((distance, str(path), path))
                elif len(others) < _BUG_LOG_COVERAGE_MAX_FILES:
                    others.append((distance, str(path), path))
        except OSError:
            return []
        preferred.sort(key=lambda item: (item[0], item[1]))
        others.sort(key=lambda item: (item[0], item[1]))
        ordered = [path for _, _, path in preferred] + [path for _, _, path in others]
        result: list[Path] = []
        for path in ordered:
            if len(result) >= limit:
                break
            if path.name.lower().endswith(".zst"):
                decoded = self._decompress_zst(path)
                if decoded is None:
                    continue
                path = decoded
            result.append(path)
        return result
