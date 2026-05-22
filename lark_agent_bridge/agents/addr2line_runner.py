"""addr2line symbol reverse lookup runner."""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any
import zipfile

from ..downloader import DownloadError, LogDownloader
from ..health import ProcessWatchdog, run_tracked_process
from ..lark_client import LarkClient
from ..models import Addr2LineRequest, BridgeConfig, DownloadResource, LarkEvent, TaskResult, create_job_context


_STACK_LINE_RE = re.compile(r"#\d+\s+pc\s+[0-9a-fA-F]{8,16}\s+\S*lib[\w.-]+\.so\b.*", re.I)
_CRASH_FILE_NAME_RE = re.compile(r"(?:^|[\\/])(?:crash[^\\/]*|tombstone[^\\/]*)$", re.I)
_NAVIGATION_CRASH_TERMS = (
    "xp_envirodrive",
    "com.xiaopeng.montecarlo",
    "envirodrive",
    "montecarlo",
    "libunity.so",
    "unity",
)
_MAX_TEXT_BYTES = 64 * 1024 * 1024


class _CrashStackSelection:
    def __init__(self, addr_text: str, source_path: Path) -> None:
        self.addr_text = addr_text
        self.source_path = source_path


class Addr2LineRunner:
    def __init__(
        self,
        config: BridgeConfig,
        *,
        lark_client: LarkClient | None = None,
        process_watchdog: ProcessWatchdog | None = None,
    ) -> None:
        self.config = config
        self.lark_client = lark_client
        self.process_watchdog = process_watchdog

    def run_resolve(self, request: Addr2LineRequest, *, event: LarkEvent | None = None) -> TaskResult:
        missing_version = not (request.rom_version.strip() or request.napa_version.strip() or request.apk_version.strip())
        if missing_version:
            return TaskResult(
                success=False,
                message=(
                    "缺少符号表版本：请补充 ROM/Napa/APK 版本号后重试。\n"
                    "示例：`@机器人 XMART..._V6.x.x_... 反解地址 <tombstone 堆栈>`，"
                    "或先发 ROM 查询，再在同一群内继续发送堆栈地址。"
                ),
                error_code="missing_symbol_version",
                details={"mode": "addr2line_resolve"},
            )
        script = self._script_path()
        if script is None:
            return TaskResult(
                success=False,
                message="addr2line-resolve skill 不可用：没有找到 addr2line_resolve.py。",
                error_code="addr2line_skill_missing",
                details={"mode": "addr2line_resolve"},
            )

        context = create_job_context(self.config.data_dir, event=event)
        addr_text = request.addr_text.strip()
        addr_source = ""
        if not addr_text and request.resources:
            selected_stack = self._extract_stack_from_resources(request.resources, context=context, event=event)
            if selected_stack is None:
                return TaskResult(
                    success=False,
                    message="缺少待反解地址：未在回复文件中找到导航最后一次 crash 堆栈（`#xx pc ... lib*.so`）。",
                    job_id=context.job_id,
                    job_dir=context.job_dir,
                    error_code="missing_address",
                    details={"mode": "addr2line_resolve", "resource_count": len(request.resources)},
                )
            addr_text = selected_stack.addr_text
            addr_source = str(selected_stack.source_path)
        if not addr_text:
            return TaskResult(
                success=False,
                message="缺少待反解地址：请贴出 tombstone backtrace 中的 `#xx pc ... lib*.so` 行。",
                job_id=context.job_id,
                job_dir=context.job_dir,
                error_code="missing_address",
                details={"mode": "addr2line_resolve"},
            )
        command = [sys.executable, str(script)]
        version_kind, version_value = self._version_arg(request)
        command.extend([version_kind, version_value])
        command.extend(["--target", request.target or "auto", "--addr", addr_text, "--json"])
        started = time.monotonic()
        if self.config.dry_run:
            return TaskResult(
                success=True,
                message=f"dry-run: addr2line 反解命令已规划\n版本: {version_value}\n目标: {request.target or 'auto'}",
                skipped=False,
                job_id=context.job_id,
                job_dir=context.job_dir,
                command=command,
                details={
                    "mode": "addr2line_resolve",
                    "symbol_version": version_value,
                    "symbol_version_kind": version_kind.lstrip("-"),
                    "target": request.target or "auto",
                    **({"addr_source": addr_source} if addr_source else {}),
                },
            )
        try:
            completed = run_tracked_process(
                command,
                watchdog=self.process_watchdog,
                name="addr2line-resolve",
                cwd=script.parent,
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds(),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return TaskResult(
                success=False,
                message="addr2line 反解超时，请确认符号表服务可访问或稍后重试。",
                job_id=context.job_id,
                job_dir=context.job_dir,
                command=command,
                duration_seconds=time.monotonic() - started,
                error_code="addr2line_resolve_timeout",
                stdout=exc.stdout or "",
                stderr=exc.stderr or "",
                details={"mode": "addr2line_resolve", "symbol_version": version_value},
            )
        except OSError as exc:
            return TaskResult(
                success=False,
                message=f"addr2line 反解启动失败: {exc}",
                job_id=context.job_id,
                job_dir=context.job_dir,
                command=command,
                duration_seconds=time.monotonic() - started,
                error_code="addr2line_resolve_failed_to_start",
                stderr=str(exc),
                details={"mode": "addr2line_resolve", "symbol_version": version_value},
            )

        if completed.returncode != 0:
            return TaskResult(
                success=False,
                message=self._failure_message(completed.stderr),
                job_id=context.job_id,
                job_dir=context.job_dir,
                command=command,
                duration_seconds=time.monotonic() - started,
                error_code="addr2line_resolve_failed",
                stdout=completed.stdout,
                stderr=completed.stderr,
                details={"mode": "addr2line_resolve", "symbol_version": version_value},
            )

        payload = self._parse_json(completed.stdout)
        if payload is None:
            return TaskResult(
                success=True,
                message=completed.stdout.strip() or "addr2line 反解完成，但脚本没有输出内容。",
                job_id=context.job_id,
                job_dir=context.job_dir,
                command=command,
                duration_seconds=time.monotonic() - started,
                stdout=completed.stdout,
                stderr=completed.stderr,
                details={"mode": "addr2line_resolve", "symbol_version": version_value},
            )
        output_path = context.output_dir / "addr2line_resolve.json"
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return TaskResult(
            success=True,
            message=self._format_message(version_value, payload),
            job_id=context.job_id,
            job_dir=context.job_dir,
            json_report=output_path,
            command=command,
            duration_seconds=time.monotonic() - started,
            stdout=completed.stdout,
            stderr=completed.stderr,
            details={
                "mode": "addr2line_resolve",
                "symbol_version": version_value,
                "symbol_version_kind": version_kind.lstrip("-"),
                "target": request.target or "auto",
                "result_count": len(payload.get("results", [])) if isinstance(payload.get("results"), list) else 0,
                **({"addr_source": addr_source} if addr_source else {}),
            },
        )

    def _script_path(self) -> Path | None:
        candidates = [
            self.config.workspace_root / ".ai/skills/addr2line-resolve/scripts/addr2line_resolve.py",
            self.config.guideengine_repo / ".ai/skills/addr2line-resolve/scripts/addr2line_resolve.py",
            self.config.guideengine_repo / ".github/skills/addr2line-resolve/scripts/addr2line_resolve.py",
        ]
        for path in candidates:
            if path.exists():
                return path
        return None

    def _timeout_seconds(self) -> int:
        return max(60, min(600, int(self.config.runner_timeout_seconds or 180)))

    def _version_arg(self, request: Addr2LineRequest) -> tuple[str, str]:
        if request.apk_version.strip():
            return "--apk", request.apk_version.strip()
        if request.napa_version.strip():
            return "--napa", request.napa_version.strip()
        return "--rom", request.rom_version.strip()

    def _parse_json(self, text: str) -> dict[str, Any] | None:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    def _extract_stack_from_resources(
        self,
        resources: list[DownloadResource],
        *,
        context,
        event: LarkEvent | None,
    ) -> _CrashStackSelection | None:
        downloader = LogDownloader(self.config, self.lark_client)
        try:
            downloaded = downloader.download_all(
                resources,
                context=context,
                message_id=event.message_id if event else "",
            )
        except DownloadError:
            return None
        roots = [item.path for item in downloaded]
        return self._select_last_navigation_stack(roots, extract_root=context.input_dir)

    def _select_last_navigation_stack(self, roots: list[Path], *, extract_root: Path) -> _CrashStackSelection | None:
        candidates: list[tuple[int, bool, int, list[str], Path]] = []
        sequence = 0
        for path in self._iter_text_candidates(roots, extract_root=extract_root):
            text = self._read_text_tail(path)
            if not text:
                continue
            for _, stack_lines, context_text in self._stack_candidates(text):
                lowered_context = context_text.casefold()
                is_navigation = any(term in lowered_context for term in _NAVIGATION_CRASH_TERMS)
                candidates.append((self._stack_source_priority(path), is_navigation, sequence, stack_lines, path))
                sequence += 1
        if not candidates:
            return None
        navigation_candidates = [item for item in candidates if item[1]]
        pool = navigation_candidates or candidates
        best_source_priority = min(item[0] for item in pool)
        selected = [item for item in pool if item[0] == best_source_priority][-1]
        return _CrashStackSelection("\n".join(selected[3]), selected[4])

    def _iter_text_candidates(self, roots: list[Path], *, extract_root: Path) -> list[Path]:
        files: list[Path] = []
        for root in roots:
            files.extend(self._expand_candidate_root(root, extract_root=extract_root))
        return sorted(files, key=lambda item: (self._stack_source_priority(item), str(item)))

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

    def _stack_candidates(self, text: str) -> list[tuple[int, list[str], str]]:
        lines = text.splitlines()
        candidates: list[tuple[int, list[str], str]] = []
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
            candidates.append((start, stack_lines, "\n".join(lines[context_start:context_end])))
        return candidates

    def _format_message(self, version: str, payload: dict[str, Any]) -> str:
        results = payload.get("results", [])
        if not isinstance(results, list):
            results = []
        lines = [
            "addr2line 反解完成",
            f"符号版本: {payload.get('apk_version') or version}",
            f"符号产物: {payload.get('so_artifact') or 'N/A'}",
            "说明: 地址反解只定位函数/源码位置，不等同于完整 crash 根因分析。",
        ]
        if not results:
            lines.append("结果: 未反解到有效函数，请检查符号版本是否匹配。")
            return "\n".join(lines)
        lines.append("反解结果:")
        for item in results[:20]:
            if not isinstance(item, dict):
                continue
            so_name = str(item.get("so") or "unknown")
            addr = str(item.get("addr") or "")
            function = str(item.get("function") or "??")
            location = str(item.get("location") or "??:0")
            lines.append(f"- {so_name} {addr} -> {function} ({location})")
        if len(results) > 20:
            lines.append(f"... 其余 {len(results) - 20} 条请看 JSON 结果。")
        return "\n".join(lines)

    def _failure_message(self, stderr: str) -> str:
        preview = "\n".join(line for line in stderr.splitlines()[-8:] if line.strip())
        if preview:
            return f"addr2line 反解失败\n{preview}"
        return "addr2line 反解失败"
