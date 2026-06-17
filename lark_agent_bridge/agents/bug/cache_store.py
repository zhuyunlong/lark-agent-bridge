from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from . import _shared


class _BugCacheStoreMixin:
    """Bug 缓存存储：缓存目录/新鲜度/元数据、缓存日志输入恢复、过期清理与附件下载（与 BugCacheMixin 共享 self 状态）。"""

    def _bug_cache_root(self) -> Path:
        return Path(self.config.data_dir).expanduser().resolve() / "bug_cache"
    def _bug_cache_dir(self, project_key: str, work_item_id: str) -> Path:
        key = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{project_key}_{work_item_id}").strip("._-")
        return self._bug_cache_root() / (key or str(work_item_id))
    def _has_bug_cache_content(self, bug_dir: Path) -> bool:
        for subdir in ("attachments", "logs"):
            path = bug_dir / subdir
            if not path.exists():
                continue
            for child in path.rglob("*"):
                if child.is_file():
                    return True
        return False
    def _is_bug_cache_fresh(self, bug_dir: Path, *, max_age_hours: int, now: datetime | None = None) -> bool:
        if max_age_hours <= 0 or not bug_dir.exists():
            return False
        reference_time = now or datetime.now(timezone.utc)
        age_seconds = reference_time.timestamp() - self._latest_path_mtime(bug_dir)
        return age_seconds <= max_age_hours * 3600
    def _reuse_prepared_bug_input(self, selected_input: Path | None) -> Path | None:
        if selected_input is None:
            return None
        if selected_input.is_dir():
            return selected_input
        lower_name = selected_input.name.lower()
        if lower_name.endswith(".xp"):
            extract_dir = selected_input.with_suffix("")
            if (extract_dir / "Log").exists():
                return extract_dir / "Log"
            if extract_dir.exists():
                return extract_dir
        if lower_name.endswith(".zip") and not lower_name.endswith(".xp.zip.001"):
            extract_dir = selected_input.with_suffix("")
            if extract_dir.exists():
                return extract_dir
        return None
    def _write_bug_cache_metadata(
        self,
        bug_dir: Path,
        *,
        bug_url: str,
        project_key: str,
        work_item_id: str,
        selected_input: Path | None,
        prepared_input: Path | None,
    ) -> None:
        bug_dir.mkdir(parents=True, exist_ok=True)
        metadata_path = bug_dir / "cache.json"
        now = datetime.now(timezone.utc).isoformat()
        payload = {
            "bug_url": bug_url,
            "project_key": project_key,
            "work_item_id": work_item_id,
            "selected_log_input": str(selected_input) if selected_input else "",
            "prepared_log_input": str(prepared_input) if prepared_input else "",
            "updated_at": now,
        }
        if metadata_path.exists():
            try:
                existing = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                existing = {}
            if isinstance(existing, dict) and existing.get("created_at"):
                payload["created_at"] = str(existing.get("created_at"))
        payload.setdefault("created_at", now)
        metadata_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    def _recover_bug_title_from_metadata(self, output_dir: Path) -> str:
        """Recover bug title from bug_metadata.md in the job output.

        This is needed for follow-ups like "请根据BUG问题实际时间继续分析"
        where the agent needs the title to extract the correct fault time.
        """
        metadata_path = output_dir / "bug_metadata.md"
        if not metadata_path.exists():
            return ""
        try:
            text = metadata_path.read_text(encoding="utf-8")[:2000]
        except OSError:
            return ""
        match = re.search(r"[标題标题]:\s*`([^`]+)`", text)
        if match:
            return match.group(1).strip()
        match = re.search(r"[标題标题]:\s*(.+)", text)
        if match:
            return match.group(1).strip()
        return ""
    def _recover_cached_bug_log_input(
        self,
        details: dict[str, object],
        *,
        request_text: str,
    ) -> tuple[Path | None, Path | None]:
        bug_dir = self._bug_cache_dir_from_context(details, request_text=request_text)
        if bug_dir is None or not bug_dir.exists():
            return None, None
        selected_input, prepared_input = self._read_bug_cache_log_input(bug_dir)
        if prepared_input is not None:
            return selected_input or prepared_input, prepared_input
        selected_input = self._select_existing_bug_cache_input(bug_dir)
        if selected_input is None:
            return None, None
        try:
            prepared_input = self._reuse_prepared_bug_input(selected_input)
            if prepared_input is None:
                prepared_input = self._prepare_log_input(selected_input)
        except (OSError, RuntimeError, zipfile.BadZipFile):
            if selected_input.is_dir():
                prepared_input = selected_input
            else:
                return None, None
        return selected_input, prepared_input
    def _bug_cache_dir_from_context(self, details: dict[str, object], *, request_text: str) -> Path | None:
        for key in ("bug_cache_dir", "bug_dir"):
            value = details.get(key)
            if isinstance(value, str) and value.strip():
                return Path(value).expanduser()
        bug_url = str(details.get("bug_url") or "").strip()
        if not bug_url:
            match = re.search(r"https?://project\.feishu\.cn/\S+", request_text)
            bug_url = match.group(0).rstrip("`，。；;、)") if match else ""
        identity = self._bug_identity_from_url_or_text(bug_url or request_text)
        if identity is None:
            return None
        project_key, work_item_id = identity
        return self._bug_cache_dir(project_key, work_item_id)
    def _bug_identity_from_url_or_text(self, text: str) -> tuple[str, str] | None:
        match = re.search(r"project\.feishu\.cn/([^/\s]+)/buglo/detail/(\d+)", text)
        if not match:
            return None
        return match.group(1), match.group(2)
    def _read_bug_cache_log_input(self, bug_dir: Path) -> tuple[Path | None, Path | None]:
        metadata_path = bug_dir / "cache.json"
        if not metadata_path.exists():
            return None, None
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None, None
        if not isinstance(payload, dict):
            return None, None
        selected_input = self._existing_path_from_text(payload.get("selected_log_input"))
        prepared_input = self._existing_path_from_text(payload.get("prepared_log_input"))
        if prepared_input is not None and self._split_xp_zip_part_match(prepared_input.name):
            prepared_input = None
        if prepared_input is None and selected_input is not None:
            try:
                prepared_input = self._reuse_prepared_bug_input(selected_input)
                if prepared_input is None:
                    prepared_input = self._prepare_log_input(selected_input)
            except (OSError, RuntimeError, zipfile.BadZipFile):
                prepared_input = selected_input if selected_input.is_dir() else None
        return selected_input, prepared_input
    def _existing_path_from_text(self, value: object) -> Path | None:
        if not isinstance(value, str) or not value.strip():
            return None
        path = Path(value).expanduser()
        return path if path.exists() else None
    def _select_existing_bug_cache_input(self, bug_dir: Path) -> Path | None:
        logs_dir = bug_dir / "logs"
        attachments_dir = bug_dir / "attachments"
        if self._has_meaningful_log_tree(logs_dir):
            return logs_dir
        if self._has_decoded_log_tree(attachments_dir):
            return attachments_dir
        candidates: list[Path] = []
        for root in (attachments_dir, logs_dir):
            if not root.exists():
                continue
            try:
                candidates.extend(path for path in root.rglob("*") if path.is_file())
            except OSError:
                continue
        for suffix in _BUG_LOG_INPUT_PRIORITY_SUFFIXES:
            for candidate in sorted(candidates, key=lambda item: str(item)):
                if candidate.name.lower().endswith(suffix) and self._is_usable_log_attachment(candidate):
                    return candidate
        if self._has_meaningful_log_tree(attachments_dir):
            return attachments_dir
        if self._has_meaningful_log_tree(bug_dir):
            return bug_dir
        return None
    def _has_decoded_log_tree(self, root: Path) -> bool:
        if not root.exists():
            return False
        try:
            for path in root.rglob("*"):
                if path.is_file() and self._is_log_coverage_file(path):
                    return True
        except OSError:
            return False
        return False
    def cleanup_expired_bug_cache(self, *, max_age_hours: int, now: datetime | None = None) -> int:
        if max_age_hours <= 0:
            return 0
        root = self._bug_cache_root()
        if not root.exists():
            return 0
        reference_time = now or datetime.now(timezone.utc)
        cutoff_seconds = max_age_hours * 3600
        removed = 0
        for bug_dir in root.iterdir():
            if not bug_dir.is_dir():
                continue
            age_seconds = reference_time.timestamp() - self._latest_path_mtime(bug_dir)
            if age_seconds <= cutoff_seconds:
                continue
            if self._remove_tree(bug_dir):
                removed += 1
        return removed
    def _latest_path_mtime(self, root: Path) -> float:
        latest = 0.0
        try:
            for child in root.rglob("*"):
                if not child.is_file():
                    continue
                try:
                    child_mtime = child.stat().st_mtime
                except OSError:
                    continue
                if child_mtime > latest:
                    latest = child_mtime
        except OSError:
            return latest
        return latest or root.stat().st_mtime
    def _remove_tree(self, root: Path) -> bool:
        try:
            shutil.rmtree(root)
        except (FileNotFoundError, PermissionError, OSError):
            return False
        return True
    def _download_bug_attachments(
        self,
        project_key: str,
        work_item_id: str,
        bug_dir: Path,
        attachments: object,
        *,
        timeout: int,
        bridge_session_id: str = "",
    ) -> dict[str, object]:
        attachments_dir = bug_dir / "attachments"
        logs_dir = bug_dir / "logs"
        attachments_dir.mkdir(parents=True, exist_ok=True)
        logs_dir.mkdir(parents=True, exist_ok=True)
        downloaded: list[str] = []
        unzipped: list[str] = []
        errors: list[str] = []
        error_details: list[dict[str, str]] = []
        skipped: list[str] = []
        if not isinstance(attachments, list):
            return {
                "ok": True,
                "downloaded": downloaded,
                "unzipped": unzipped,
                "errors": errors,
                "error_details": error_details,
                "skipped": skipped,
            }

        for item in attachments:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            url = str(item.get("url") or "").strip()
            if not name:
                continue
            if not self._should_download_bug_attachment(name):
                skipped.append(name)
                continue
            if not url:
                errors.append(name)
                error_details.append({"name": name, "reason": "missing_attachment_url"})
                continue
            output_path = (attachments_dir / name).expanduser().resolve()
            completed = _run_tracked_process(
                [
                    "meegle",
                    "attachment",
                    "+download",
                    url,
                    "--project-key",
                    project_key,
                    "--work-item-id",
                    work_item_id,
                    "--output",
                    str(output_path),
                    "--overwrite",
                    "--format",
                    "json",
                ],
                watchdog=self.process_watchdog,
                name="bug-attachment-download",
                cwd=self._working_dir(),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                session_id=bridge_session_id,
                debug_log_path=self._subprocess_debug_log_path(
                    attachments_dir,
                    f"download-{output_path.name}",
                    bridge_session_id=bridge_session_id,
                ),
            )
            if completed.returncode != 0:
                errors.append(name)
                error_details.append({"name": name, "reason": self._extract_process_error_message(completed)})
                continue
            downloaded.append(name)
            if output_path.name.lower().endswith(".zip") and zipfile.is_zipfile(output_path):
                if self._extract_downloaded_zip(output_path, logs_dir):
                    unzipped.append(name)
        return {
            "ok": True,
            "downloaded": downloaded,
            "unzipped": unzipped,
            "errors": errors,
            "error_details": error_details,
            "skipped": skipped,
        }
