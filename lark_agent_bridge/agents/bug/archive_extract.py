from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from . import _shared


class _ArchiveExtractMixin:
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

    def _should_download_bug_attachment(self, name: str) -> bool:
        lower_name = name.strip().casefold()
        return bool(lower_name) and any(lower_name.endswith(suffix) for suffix in _BUG_ATTACHMENT_DOWNLOAD_SUFFIXES)

    def _extract_downloaded_zip(self, archive_path: Path, logs_dir: Path) -> bool:
        try:
            with zipfile.ZipFile(archive_path) as zf:
                zf.extractall(logs_dir)
        except (OSError, zipfile.BadZipFile):
            return False
        self._normalize_tree_permissions(logs_dir)
        try:
            archive_path.unlink()
        except OSError:
            pass
        return True

    def _select_log_input(self, bug_dir: Path, fetched: dict[str, object]) -> Path | None:
        attachments_dir = bug_dir / "attachments"
        logs_dir = bug_dir / "logs"
        attachments: list[Path] = []
        for item in fetched.get("attachments", []):
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if isinstance(name, str):
                candidate = attachments_dir / name
                if candidate.exists():
                    attachments.append(candidate)

        if self._has_meaningful_log_tree(logs_dir):
            return logs_dir
        for suffix in _BUG_LOG_INPUT_PRIORITY_SUFFIXES:
            for candidate in attachments:
                if candidate.name.lower().endswith(suffix) and self._is_usable_log_attachment(candidate):
                    return candidate
        return None

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
                timeout=900,
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

    def _prepare_log_input(self, selected_input: Path) -> Path:
        lower_name = selected_input.name.lower()
        if lower_name.endswith(".xp"):
            prepared = self._expand_xp_file(selected_input)
        elif self._is_archive_log_attachment(selected_input) and not lower_name.endswith(".xp.zip.001"):
            prepared = self._extract_log_archive(selected_input)
        elif selected_input.is_dir():
            archive = self._select_best_archive(selected_input)
            prepared = self._prepare_log_input(archive) if archive else selected_input
        else:
            prepared = selected_input
        self._ensure_decoded_in_place(prepared)
        return prepared

    def _fault_time_to_datetime(self, fault_time: str) -> datetime | None:
        return self._parse_bug_datetime(fault_time)

    def _select_best_archive(self, directory: Path) -> Path | None:
        try:
            children = sorted(directory.iterdir())
        except OSError:
            return None
        for suffix in _BUG_LOG_INPUT_PRIORITY_SUFFIXES:
            for child in children:
                if child.is_file() and child.name.lower().endswith(suffix) and self._is_usable_log_attachment(child):
                    if child.name.lower().endswith(".xp.zip.001"):
                        continue
                    return child
        return None

    def _analyze_logs_intelligently(
        self,
        log_dir: Path,
        problem_time: datetime | None,
        plan: "BugAnalysisPlan",
    ) -> dict[str, object] | None:
        """智能日志分析

        根据问题时间点定位进程号，基于进程号找出相关日志，反推日志线索

        Args:
            log_dir: 日志目录
            problem_time: 问题时间点
            plan: 分析计划

        Returns:
            智能分析结果，包含 target_pid, log_files, timeline, suggested_skill
            如果分析失败或不适用，返回 None
        """
        if not problem_time:
            logger.info("智能日志分析: 跳过（无问题时间）")
            return None

        if not self.log_analyzer:
            logger.info("智能日志分析: 跳过（分析器未初始化）")
            return None

        # 只对需要日志的分析类型启用智能分析
        if not _kind_spec(plan.kind).log_dependent:
            logger.info("智能日志分析: 跳过（类型 %s 不需要日志）", plan.kind)
            return None

        logger.info("智能日志分析: 开始, type=%s, time=%s", plan.kind, problem_time)

        try:
            # Step 1: 根据问题时间定位进程号
            target_pid = self.log_analyzer.find_target_pid(log_dir, problem_time)
            if not target_pid:
                logger.warning("智能日志分析: 未找到目标进程号")
                return None

            # Step 2: 基于进程号找出所有相关日志
            log_files = self.log_analyzer.find_all_log_files(log_dir, target_pid)
            if not log_files:
                logger.warning("智能日志分析: 未找到相关日志文件")
                return None

            # Step 3: 反推日志线索
            timeline = self.log_analyzer.reverse_trace(log_files, problem_time, target_pid)

            # Step 4: 识别问题类型（可选，用于验证）
            suggested_skill = self.log_analyzer.identify_problem_type(timeline) if timeline else plan.kind

            result = {
                "target_pid": target_pid,
                "log_files": [str(f) for f in log_files],
                "timeline": timeline,
                "suggested_skill": suggested_skill,
                "event_count": len(timeline),
            }

            logger.info(
                "智能日志分析: 完成, pid=%d, files=%d, events=%d, suggested=%s",
                target_pid,
                len(log_files),
                len(timeline),
                suggested_skill,
            )

            return result

        except Exception as e:
            logger.error("智能日志分析失败: %s", e, exc_info=True)
            return None

    def _is_archive_log_attachment(self, path: Path) -> bool:
        lower_name = path.name.lower()
        return any(lower_name.endswith(suffix) for suffix in _BUG_ARCHIVE_SUFFIXES)

    def _archive_extract_dir(self, archive_path: Path) -> Path:
        lower_name = archive_path.name.lower()
        for suffix in sorted(_BUG_ARCHIVE_SUFFIXES, key=len, reverse=True):
            if lower_name.endswith(suffix):
                return archive_path.with_name(archive_path.name[: -len(suffix)])
        return archive_path.with_suffix("")

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
            timeout=600,
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

    def _retry_bug_log_download(
        self,
        *,
        previous_session: dict[str, object],
        job_dir: Path,
        progress_callback: Callable[[dict[str, object]], None] | None,
        bridge_session_id: str = "",
    ) -> dict[str, object]:
        details = previous_session.get("details", {}) if isinstance(previous_session, dict) else {}
        if not isinstance(details, dict):
            details = {}
        bridge_kwargs = {"bridge_session_id": bridge_session_id} if bridge_session_id else {}
        bug_url = str(details.get("bug_url") or "").strip()
        if not bug_url:
            return {"ok": False, "message": "缺少 bug_url，无法重新下载日志。"}
        try:
            env_status = self._run_json_command(
                [str(self._bug_fetcher_script()), "check-env"],
                timeout=60,
                **bridge_kwargs,
            )
        except Exception as exc:
            return {"ok": False, "message": f"重新检查 meegle 环境失败：{exc}"}
        if not env_status.get("meegle_installed", False):
            return {"ok": False, "message": "重新下载日志失败：本机未安装 meegle CLI。"}
        if not env_status.get("auth_ok", False):
            host = str(env_status.get("host") or "project.feishu.cn")
            return {
                "ok": False,
                "message": f"重新下载日志失败：meegle 未登录或授权已失效（host={host}）。",
                "reason": "AUTH_REQUIRED",
            }
        try:
            resolved = self._run_json_command(
                [str(self._bug_fetcher_script()), "resolve-url", bug_url],
                timeout=60,
                **bridge_kwargs,
            )
        except Exception as exc:
            return {"ok": False, "message": f"重新解析 bug 链接失败：{exc}"}
        project_key = str(resolved.get("project_key") or "").strip()
        work_item_id = str(resolved.get("work_item_id") or "").strip()
        if not project_key or not work_item_id:
            return {"ok": False, "message": "重新下载日志失败：bug 链接未解析出 project_key/work_item_id。"}
        bug_dir = self._bug_cache_dir(project_key, work_item_id)
        bug_dir.mkdir(parents=True, exist_ok=True)
        self._emit_progress(
            progress_callback,
            stage="bug_reanalysis_retry_download",
            message="上一轮没有可复用日志，尝试重新下载 bug 附件",
            project_key=project_key,
            work_item_id=work_item_id,
            bug_cache_dir=str(bug_dir),
        )
        try:
            fetched = self._run_json_command(
                [str(self._bug_fetcher_script()), "fetch-data", project_key, work_item_id],
                timeout=120,
                **bridge_kwargs,
            )
        except Exception as exc:
            return {"ok": False, "message": f"重新拉取 bug 附件列表失败：{exc}"}
        download = self._download_bug_attachments(
            project_key,
            work_item_id,
            bug_dir,
            fetched.get("attachments", []),
            timeout=self.config.bug_analysis.timeout_seconds,
            **bridge_kwargs,
        )
        selected_input = self._select_log_input(bug_dir, fetched)
        prepared_input = self._reuse_prepared_bug_input(selected_input) if selected_input else None
        if prepared_input is None and selected_input is not None:
            prepared_input = self._prepare_log_input(selected_input)
        self._write_bug_cache_metadata(
            bug_dir,
            bug_url=bug_url,
            project_key=project_key,
            work_item_id=work_item_id,
            selected_input=selected_input,
            prepared_input=prepared_input,
        )
        if prepared_input is not None:
            return {
                "ok": True,
                "message": "已重新下载并准备日志输入。",
                "selected_input": selected_input,
                "prepared_input": prepared_input,
                "download": download,
            }
        attachment_lines = self._render_attachment_lines(fetched.get("attachments", []), download)
        return {
            "ok": False,
            "message": f"重新下载日志后仍未拿到可用日志输入。\n附件结果：\n{attachment_lines}",
            "download": download,
        }

    def _has_meaningful_log_tree(self, root: Path) -> bool:
        if not root.exists():
            return False
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            lower_name = path.name.lower()
            if lower_name in {"prop.txt", "dfx.txt"}:
                continue
            if lower_name.endswith((".alog", ".xlog", ".log", ".txt", ".xp", ".zip", ".001")):
                return True
        return False

    def _is_usable_log_attachment(self, path: Path) -> bool:
        lower_name = path.name.lower()
        if lower_name.endswith(".zip") and not lower_name.endswith(".xp.zip.001"):
            return zipfile.is_zipfile(path)
        if lower_name.endswith(".xp"):
            return not self._looks_like_non_log_xp_payload(path)
        return True

    def _looks_like_non_log_xp_payload(self, path: Path) -> bool:
        try:
            head = path.read_bytes()[:16]
        except OSError:
            return False
        if any(head.startswith(prefix) for prefix in _NON_LOG_XP_MAGIC_HEADERS):
            return True
        if len(head) >= 12 and head.startswith(b"RIFF") and head[8:12] == b"WEBP":
            return True
        if len(head) >= 8 and head[4:8] == b"ftyp":
            return True
        return False

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
            timeout=600,
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

    def _normalize_tree_permissions(self, root: Path) -> None:
        try:
            root.chmod(root.stat().st_mode | stat.S_IRWXU)
        except OSError:
            return
        for path in root.rglob("*"):
            try:
                mode = path.stat().st_mode
                if path.is_dir():
                    path.chmod(mode | stat.S_IRWXU)
                else:
                    path.chmod(mode | stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                continue

    # Package whose app log should be preferred when selecting startup input.
    _STARTUP_TARGET_PACKAGE = "com.xiaopeng.montecarlo"

    def _select_startup_input(self, input_path: Path, fault_time: str) -> Path:
        if input_path.is_file():
            return input_path
        fault_dt = self._parse_fault_datetime(fault_time)
        if fault_dt is None:
            return input_path
        fault_epoch = time.mktime(fault_dt)
        all_candidates = [
            path
            for path in input_path.rglob("*")
            if path.is_file()
            and path.suffix.lower() in {".alog", ".xlog", ".log", ".txt"}
            and self._parse_log_file_datetime(path.name) is not None
        ]
        # Prefer files inside the target app package directory; fall back to
        # all candidates so the script can emit a meaningful warning itself.
        target_pkg = self._STARTUP_TARGET_PACKAGE
        montecarlo = [p for p in all_candidates if target_pkg in str(p)]
        candidates = montecarlo if montecarlo else all_candidates
        ranked: list[tuple[float, int, Path]] = []
        for candidate in candidates:
            file_dt = self._parse_log_file_datetime(candidate.name)
            if file_dt is None:
                continue
            score = abs(time.mktime(file_dt) - fault_epoch)
            ranked.append((score, self._log_file_priority(candidate), candidate))
        if not ranked:
            return input_path
        ranked.sort(key=lambda item: (item[0], item[1], str(item[2])))
        return ranked[0][2]

    def _startup_analysis_input(self, input_path: Path, fault_time: str) -> Path:
        if input_path.is_file():
            return input_path
        return self._select_startup_input(input_path, fault_time)

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

    def _log_file_priority(self, path: Path) -> int:
        lower = path.name.lower()
        if lower.endswith((".alog.log", ".xlog.log")):
            return 0
        if lower.endswith(".log"):
            return 1
        if lower.endswith(".txt"):
            return 2
        if lower.endswith(".alog"):
            return 3
        if lower.endswith(".xlog"):
            return 4
        return 5

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
            timeout=900,
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
            return lower.endswith((".alog", ".xlog")) and not Path(str(path) + ".log").exists()

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

    def _bug_description(self, fetched: dict[str, object]) -> str:
        fields = fetched.get("fields", {})
        fallback = fetched.get("description", "")
        if not isinstance(fields, dict):
            return fallback if isinstance(fallback, str) else ""
        value = fields.get("field_204366", "")
        if isinstance(value, str) and value:
            return value
        return fallback if isinstance(fallback, str) else ""

    def _bug_reference_time(self, fetched: dict[str, object], full_item: dict[str, object]) -> str:
        for value in (
            fetched.get("create_time"),
            fetched.get("created_at"),
            fetched.get("createTime"),
            self._nested_string(full_item, "work_item_attribute", "create_time"),
            self._nested_string(full_item, "work_item_attribute", "created_at"),
            self._nested_string(full_item, "data", "work_item_attribute", "create_time"),
            self._nested_string(full_item, "data", "work_item_attribute", "created_at"),
            full_item.get("create_time"),
            full_item.get("created_at"),
        ):
            text = str(value or "").strip()
            if text:
                return text
        return ""

    def _nested_string(self, payload: object, *keys: str) -> str:
        current = payload
        for key in keys:
            if not isinstance(current, dict):
                return ""
            current = current.get(key)
        return str(current or "").strip()

    def _build_bug_outputs(
        self,
        *,
        plans: list["BugAnalysisPlan"],
        work_item_id: str,
        fetched: dict[str, object],
        full_item: dict[str, object],
        option_map: dict[str, str],
        request_text: str,
        prompt_text: str,
        selected_input: Path | None,
        report_jsons: dict[str, Path | None],
        download: dict[str, object],
        html_paths: list[Path],
        classification_skill: str = "",
        classification_source: str = "",
        classification_reason: str = "",
        classification_provider: str = "",
        fault_time: str | None = None,
        fault_time_note: str | None = None,
        log_coverage: LogCoverage | None = None,
    ) -> tuple[str, str]:
        title = str(fetched.get("title", ""))
        description = self._bug_description(fetched)
        status = str(fetched.get("status", ""))
        create_time = self._bug_reference_time(fetched, full_item)
        create_by = str(fetched.get("create_by", ""))
        owner = self._extract_owner(full_item)
        bug_source = self._map_option(option_map, fetched.get("fields", {}), "field_24095d")
        found_version = self._string_field(fetched.get("fields", {}), "field_010122")
        probability = self._map_option(option_map, fetched.get("fields", {}), "field_45dc84")
        if fault_time is None or fault_time_note is None:
            extracted_fault_time, extracted_fault_time_note = self._extract_fault_time(title, description)
            fault_time = extracted_fault_time if fault_time is None else fault_time
            fault_time_note = extracted_fault_time_note if fault_time_note is None else fault_time_note
        summary_blocks: list[str] = []
        for plan in plans:
            html_path = next((path for path in html_paths if path.name == self._report_name(plan.kind, "html")), None)
            if html_path is None:
                continue
            summary_blocks.append(
                self._build_summary_from_report(
                    plan=plan,
                    report_json=report_jsons.get(plan.kind),
                    prompt_text=prompt_text,
                    fault_time=fault_time,
                    html_path=html_path,
                    selected_input=selected_input,
                )
            )
        summary = "\n\n".join(summary_blocks)
        attachment_lines = self._render_attachment_lines(fetched.get("attachments", []), download)
        analysis_lines = "\n".join(
            f"  - `{self._analysis_label(plan.kind)}` -> `{self._report_name(plan.kind, 'html')}`"
            for plan in plans
        )
        report_artifact_lines: list[str] = []
        for plan in plans:
            html_path = next((path for path in html_paths if path.name == self._report_name(plan.kind, "html")), None)
            json_path = report_jsons.get(plan.kind)
            if html_path is not None:
                report_artifact_lines.append(f"  - HTML `{self._analysis_label(plan.kind)}`: `{html_path}`")
            if json_path is not None:
                report_artifact_lines.append(f"  - JSON `{self._analysis_label(plan.kind)}`: `{json_path}`")
            analysis_md_name = self._skill_agent_analysis_markdown_name(plan.kind)
            analysis_md_path = (html_path.parent / f"{plan.kind}_analysis" / analysis_md_name) if html_path else None
            if analysis_md_path and analysis_md_path.exists():
                report_artifact_lines.append(f"  - Analysis `{self._analysis_label(plan.kind)}`: `{analysis_md_path}`")
        report_artifacts = "\n".join(report_artifact_lines) if report_artifact_lines else "  - 无"
        metadata = (
            "# Bug Metadata\n\n"
            f"- Bug ID: `{work_item_id}`\n"
            f"- 标题: `{title}`\n"
            f"- 当前状态: `{status or '未返回 / 未设置'}`\n"
            f"- 创建时间: `{create_time or '未返回 / 未设置'}`\n"
            f"- 创建人: `{create_by or '未返回 / 未设置'}`\n"
            f"- 当前负责人: `{owner}`\n"
            f"- 缺陷来源: `{bug_source}`\n"
            f"- 发现版本: `{found_version}`\n"
            f"- 发生概率: `{probability}`\n"
            f"- 分析类型:\n{analysis_lines}\n"
            f"- 命中 Skill: `{classification_skill or self._skill_name_for_kind(plans[0].kind if plans else 'general')}`\n"
            f"- 分类来源: `{classification_source or 'manual_fallback'}`\n"
            f"- 分类 Agent: `{classification_provider or '无'}`\n"
            f"- 分类理由: `{classification_reason or '未记录'}`\n"
            f"- Skill 规范:\n{self._render_skill_context_lines(classification_skill)}"
            f"- 信号代码: `{', '.join(plan.signal_code for plan in plans if plan.signal_code) or '无'}`\n"
            f"- 故障时间: `{fault_time or '未识别'}`\n"
            f"  说明: {fault_time_note}\n"
            f"- 日志覆盖范围: `{self._format_log_coverage_for_metadata(log_coverage)}`\n"
            "- 用户原始请求:\n\n```text\n"
            f"{request_text}\n"
            "```\n"
            f"- 分析请求: `{prompt_text}`\n"
            f"- 选中日志输入: `{selected_input or '无，可静态分析'}`\n"
            f"- 报告产物:\n{report_artifacts}\n"
            f"- 附件:\n{attachment_lines}\n"
            "- 缺陷描述:\n\n```text\n"
            f"{description.strip() or '(无描述)'}\n"
            "```\n\n"
            "- 本轮脚本初步摘要（仅代表本轮自动脚本输出，不代表上一轮分析结论；若与源码证据冲突，以源码与原始输入为准）:\n\n"
            f"{summary}\n"
        )
        return metadata, summary

    def _skill_context_paths(self, skill_name: str) -> list[Path]:
        normalized = skill_name.strip()
        if not normalized or normalized == "general":
            return []
        try:
            record = self.skill_manager.get_skill(normalized, include_content=False)
        except Exception:
            return []
        paths: list[Path] = []
        skill_md = Path(record.skill_md_path).expanduser() if record.skill_md_path else Path()
        if skill_md and skill_md.exists() and skill_md.is_file():
            paths.append(skill_md)
            references_dir = skill_md.parent / "references"
            if references_dir.exists():
                for path in sorted(references_dir.glob("*.md"))[:4]:
                    if path.is_file():
                        paths.append(path)
        return paths

    def _render_skill_context_lines(self, skill_name: str) -> str:
        paths = self._skill_context_paths(skill_name)
        if not paths:
            return "  - 无\n"
        return "".join(f"  - `{path}`\n" for path in paths)

    def _extract_owner(self, full_item: dict[str, object]) -> str:
        current_nodes = full_item.get("work_item_current_node", [])
        if isinstance(current_nodes, list) and current_nodes:
            owners = current_nodes[0].get("owners", []) if isinstance(current_nodes[0], dict) else []
            if isinstance(owners, list) and owners:
                owner = owners[0]
                if isinstance(owner, dict):
                    return str(owner.get("name", "未返回 / 未设置"))
        return "未返回 / 未设置"

    def _map_option(self, option_map: dict[str, str], fields: object, key: str) -> str:
        if not isinstance(fields, dict):
            return "未返回 / 未设置"
        raw = fields.get(key, "")
        if isinstance(raw, str) and raw:
            return option_map.get(raw, raw)
        return "未返回 / 未设置"

    def _string_field(self, fields: object, key: str) -> str:
        if not isinstance(fields, dict):
            return "未返回 / 未设置"
        value = fields.get(key, "")
        if isinstance(value, str) and value:
            return value
        return "未返回 / 未设置"

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

    def _build_direct_analysis_summary(
        self,
        plans: list["BugAnalysisPlan"],
        prompt_text: str,
        html_paths: list[Path],
    ) -> str:
        lines = ["直传文件分析完成", f"描述: {prompt_text}", "报告:"]
        for plan, html_path in zip(plans, html_paths):
            lines.append(f"- {self._analysis_label(plan.kind)}: {html_path}")
        return "\n".join(lines)

    def _render_attachment_lines(self, attachments: object, download: dict[str, object]) -> str:
        downloaded = set(str(name) for name in download.get("downloaded", []) if isinstance(name, str))
        skipped = set(str(name) for name in download.get("skipped", []) if isinstance(name, str))
        errors = set(str(name) for name in download.get("errors", []) if isinstance(name, str))
        error_detail_map = {
            str(item.get("name") or ""): str(item.get("reason") or "")
            for item in download.get("error_details", [])
            if isinstance(item, dict)
        }
        lines: list[str] = []
        if isinstance(attachments, list):
            for item in attachments:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name", ""))
                size = str(item.get("size", ""))
                status = (
                    "已下载"
                    if name in downloaded
                    else "已复用缓存日志"
                    if download.get("reused")
                    else "已跳过"
                    if name in skipped
                    else "下载失败"
                    if name in errors
                    else "未下载"
                )
                reason = error_detail_map.get(name, "")
                suffix = f" ({reason})" if reason and status == "下载失败" else ""
                lines.append(f"  - `{name}` (`{size}`) - {status}{suffix}")
        return "\n".join(lines) if lines else "  - (无附件)"
