from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from . import _shared


class _BugLogInputSelectMixin:
    """Bug 日志输入挑选：附件下载重试、归档识别与启动/最佳日志输入选择（与 ArchiveExtractMixin 共享 self 状态）。"""

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
