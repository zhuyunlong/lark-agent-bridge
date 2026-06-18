from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from . import _shared


from .fault_time import _BugFaultTimeMixin
from .log_input_select import _BugLogInputSelectMixin
from .bug_outputs import _BugOutputsMixin


class _ArchiveExtractMixin(_BugFaultTimeMixin, _BugLogInputSelectMixin, _BugOutputsMixin):
    _STARTUP_TARGET_PACKAGE = "com.xiaopeng.montecarlo"
    def _extract_process_error_message(self, completed: subprocess.CompletedProcess[str]) -> str:
        candidates = [completed.stderr or "", completed.stdout or ""]
        for text in candidates:
            stripped = text.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                return stripped.splitlines()[0][:400]
            if isinstance(payload, dict):
                error = payload.get("error")
                if isinstance(error, dict):
                    code = str(error.get("code") or "").strip()
                    message = str(error.get("message") or "").strip()
                    if code and message:
                        return f"{code}: {message}"
                    if message:
                        return message
                if payload.get("ok") is False:
                    return str(payload.get("error") or "command returned ok=false")
            return stripped[:400]
        return "unknown_error"
    def _ensure_decoded_in_place(self, prepared: Path) -> None:
        """If prepared still contains raw .alog/.xlog files, decode them in-place.
        Best-effort: failures only emit a warning, leaving _decode_raw_logs_before_analysis
        as the per-kind safety net.
        """
        try:
            if not prepared.exists():
                return
            if not self._has_raw_logs_needing_decode(prepared):
                return
        except OSError:
            return
        decoder = self.config.workspace_root / ".ai/skills/log-decoder/tools/alog_decoder.py"
        if not decoder.exists():
            logger.warning("log decoder not found: %s", decoder)
            return
        decoder_python = self._decoder_python()
        command = [decoder_python, str(decoder), str(prepared)]
        try:
            result = _run_tracked_process(
                command,
                watchdog=self.process_watchdog,
                name="prepare-log-decode",
                cwd=self._working_dir(),
                capture_output=True,
                text=True,
                timeout=self.config.bug_analysis.archive_extract_timeout_seconds,
                check=False,
            )
        except Exception as exc:  # subprocess startup/timeout
            logger.warning("alog decode during prepare failed: %s", exc)
            return
        if result.returncode != 0:
            logger.warning(
                "alog decode during prepare returned %s: %s",
                result.returncode,
                (result.stderr or result.stdout or "").strip()[:400],
            )
        else:
            logger.info("stage=prepare_log_input decoded prepared=%s", prepared)
    def _extract_log_archive(self, archive_path: Path) -> Path:
        lower_name = archive_path.name.lower()
        extract_dir = self._archive_extract_dir(archive_path)
        if lower_name.endswith(".zip"):
            if not zipfile.is_zipfile(archive_path):
                raise RuntimeError(f"日志附件不是有效 zip: {archive_path.name}")
            if not extract_dir.exists():
                with zipfile.ZipFile(archive_path) as zf:
                    zf.extractall(extract_dir)
                self._normalize_tree_permissions(extract_dir)
            return extract_dir
        if extract_dir.exists():
            return extract_dir
        extract_dir.mkdir(parents=True, exist_ok=True)
        command = self._archive_extract_command(archive_path, extract_dir)
        if command is None:
            raise RuntimeError(f"无法解压日志附件 {archive_path.name}: 未找到 bsdtar/7z/unar")
        completed = _run_tracked_process(
            command,
            watchdog=self.process_watchdog,
            name="bug-log-archive-extract",
            cwd=self._working_dir(),
            capture_output=True,
            text=True,
            timeout=self.config.bug_analysis.archive_decode_timeout_seconds,
            check=False,
            debug_log_path=self._subprocess_debug_log_path(
                archive_path.parent,
                f"extract-{archive_path.name}",
            ),
        )
        if completed.returncode != 0:
            shutil.rmtree(extract_dir, ignore_errors=True)
            reason = completed.stderr.strip() or completed.stdout.strip() or "archive extract failed"
            raise RuntimeError(f"日志附件解压失败 {archive_path.name}: {reason}")
        self._normalize_tree_permissions(extract_dir)
        return extract_dir
    def _archive_extract_command(self, archive_path: Path, extract_dir: Path) -> list[str] | None:
        bsdtar = shutil.which("bsdtar")
        if bsdtar:
            return [bsdtar, "-xf", str(archive_path), "-C", str(extract_dir)]
        seven_zip = shutil.which("7zz") or shutil.which("7z")
        if seven_zip:
            return [seven_zip, "x", "-y", f"-o{extract_dir}", str(archive_path)]
        unar = shutil.which("unar")
        if unar:
            return [unar, "-force-overwrite", "-output-directory", str(extract_dir), str(archive_path)]
        return None
    def _expand_xp_file(self, xp_path: Path) -> Path:
        jar_path = self.config.workspace_root / ".ai/skills/log-decoder/tools/decryptFile.jar"
        # Pass only the filename and run from the xp file's directory so that
        # DecryptFile.jar's String.replace("xp","zip") doesn't corrupt the
        # directory components of the path (e.g. "xpfailuremgmt" → "zipfailuremgmt").
        completed = _run_tracked_process(
            ["java", "-jar", str(jar_path), xp_path.name],
            watchdog=self.process_watchdog,
            name="xp-log-decrypt",
            cwd=xp_path.parent,
            capture_output=True,
            text=True,
            timeout=self.config.bug_analysis.archive_decode_timeout_seconds,
            check=False,
            debug_log_path=self._subprocess_debug_log_path(xp_path.parent, f"decrypt-{xp_path.name}"),
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "decrypt xp failed")
        inner_zip = xp_path.with_suffix(".zip")
        if not inner_zip.exists():
            raise RuntimeError(f"xp 解密后未产出 zip: {inner_zip}")
        extract_dir = xp_path.with_suffix("")
        if not extract_dir.exists():
            with zipfile.ZipFile(inner_zip) as zf:
                zf.extractall(extract_dir)
            self._normalize_tree_permissions(extract_dir)
        return extract_dir / "Log" if (extract_dir / "Log").exists() else extract_dir
    def _bridge_session_id(self, event: LarkEvent | None, fallback: str = "") -> str:
        if event is None:
            return fallback.strip()
        return (event.root_id or event.message_id or event.event_id or fallback).strip()
    def _run_analysis(
        self,
        *,
        plan: "BugAnalysisPlan",
        input_path: Path | None,
        html_path: Path,
        json_path: Path,
        analysis_dir: Path,
        timeout: int,
        target_time: str | None = None,
        request_text: str | None = None,
        bridge_session_id: str = "",
    ) -> subprocess.CompletedProcess[str]:
        decode_completed = self._decode_raw_logs_before_analysis(
            plan=plan,
            input_path=input_path,
            analysis_dir=analysis_dir,
            target_time=target_time,
            bridge_session_id=bridge_session_id,
        )
        if decode_completed is not None and decode_completed.returncode != 0:
            return decode_completed
        command = self.build_command(
            plan=plan,
            input_path=input_path or self.config.workspace_root,
            html_path=html_path,
            json_path=json_path,
            analysis_dir=analysis_dir,
            target_time=target_time,
            request_text=request_text,
        )
        completed = _run_tracked_process(
            command,
            watchdog=self.process_watchdog,
            name=f"bug-analysis-{plan.kind}",
            cwd=self._working_dir(),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            session_id=bridge_session_id,
            debug_log_path=self._subprocess_debug_log_path(
                analysis_dir,
                f"{plan.kind}-analysis",
                bridge_session_id=bridge_session_id,
            ),
        )
        if completed.returncode != 0 and plan.kind in _RETRY_KINDS:
            completed = _run_tracked_process(
                command,
                watchdog=self.process_watchdog,
                name=f"bug-analysis-{plan.kind}-retry",
                cwd=self._working_dir(),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                session_id=bridge_session_id,
                debug_log_path=self._subprocess_debug_log_path(
                    analysis_dir,
                    f"{plan.kind}-analysis-retry",
                    bridge_session_id=bridge_session_id,
                ),
            )
        if completed.returncode != 0:
            return completed

        generated_html, generated_json = BugReportAdapter.locate_report(
            kind=plan.kind,
            completed_stdout=completed.stdout or "",
            analysis_dir=analysis_dir,
            html_path=html_path,
            json_path=json_path,
        )
        if generated_html and generated_html.exists() and generated_html != html_path:
            shutil.copy2(generated_html, html_path)
        if generated_json and generated_json.exists() and generated_json != json_path:
            shutil.copy2(generated_json, json_path)
        return completed
    def _decode_raw_logs_before_analysis(
        self,
        *,
        plan: "BugAnalysisPlan",
        input_path: Path | None,
        analysis_dir: Path,
        target_time: str | None,
        bridge_session_id: str = "",
    ) -> subprocess.CompletedProcess[str] | None:
        if input_path is None or not input_path.exists():
            return None
        if not self._has_raw_logs_needing_decode(input_path):
            return None
        decoder = self.config.workspace_root / ".ai/skills/log-decoder/tools/alog_decoder.py"
        if not decoder.exists():
            logger.warning("log decoder not found: %s", decoder)
            return None
        decoder_python = self._decoder_python()
        command = [decoder_python, str(decoder), str(input_path)]
        if not input_path.is_file():
            time_arg = self._decoder_time_arg(target_time)
            if time_arg:
                command.append(time_arg)
        return _run_tracked_process(
            command,
            watchdog=self.process_watchdog,
            name=f"bug-analysis-{plan.kind}-decode-logs",
            cwd=self._working_dir(),
            capture_output=True,
            text=True,
            timeout=self.config.bug_analysis.archive_extract_timeout_seconds,
            check=False,
            session_id=bridge_session_id,
            debug_log_path=self._subprocess_debug_log_path(
                analysis_dir,
                f"{plan.kind}-log-decode",
                bridge_session_id=bridge_session_id,
            ),
        )
    def _decoder_python(self) -> str:
        override = os.environ.get("LARK_AGENT_BRIDGE_LOG_DECODER_PYTHON", "").strip()
        if override:
            return override
        return shutil.which("python3.11") or shutil.which("python3") or "python3"
    def _has_raw_logs_needing_decode(self, input_path: Path) -> bool:
        def needs_decode(path: Path) -> bool:
            lower = path.name.lower()
            return lower.endswith(_RAW_LOG_SUFFIXES) and not _has_decoded_log_sibling(path)

        if input_path.is_file():
            return needs_decode(input_path)
        try:
            return any(needs_decode(path) for path in input_path.rglob("*") if path.is_file())
        except OSError:
            return False
    def _decoder_time_arg(self, target_time: str | None) -> str:
        if not target_time:
            return ""
        parsed = self._parse_fault_datetime(target_time)
        if parsed is None:
            return ""
        return time.strftime("%Y-%m-%d %H", parsed)
    def _extract_report_path(self, output: str, pattern: str) -> Path | None:
        match = re.search(pattern, output, re.MULTILINE)
        if not match:
            return None
        return Path(match.group(1).strip())
