"""ROM version lookup runner."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

from ..health import ProcessWatchdog, run_tracked_process
from ..models import BridgeConfig, LarkEvent, RomVersionLookupRequest, TaskResult, create_job_context


class RomVersionLookupRunner:
    def __init__(self, config: BridgeConfig, process_watchdog: ProcessWatchdog | None = None) -> None:
        self.config = config
        self.process_watchdog = process_watchdog

    def run_lookup(self, request: RomVersionLookupRequest, *, event: LarkEvent | None = None) -> TaskResult:
        if not request.rom_version.strip():
            return TaskResult(
                success=False,
                message="缺少 ROM 版本号：请提供完整 ROM 版本号。",
                error_code="missing_rom_version",
                details={"mode": "rom_version_lookup"},
            )
        script = self._script_path()
        if script is None:
            return TaskResult(
                success=False,
                message="rom-version-lookup skill 不可用：没有找到 lookup_rom_version.py。",
                error_code="rom_version_skill_missing",
                details={"mode": "rom_version_lookup"},
            )

        context = create_job_context(self.config.data_dir, event=event)
        command = [sys.executable, str(script), request.rom_version, "--fetch", "--json"]
        started = time.monotonic()
        if self.config.dry_run:
            return TaskResult(
                success=True,
                message=f"dry-run: ROM 版本查询命令已规划\nROM: {request.rom_version}",
                skipped=False,
                job_id=context.job_id,
                job_dir=context.job_dir,
                command=command,
                details={"mode": "rom_version_lookup", "rom_version": request.rom_version},
            )
        try:
            completed = run_tracked_process(
                command,
                watchdog=self.process_watchdog,
                name="rom-version-lookup",
                cwd=script.parent,
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds(),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return TaskResult(
                success=False,
                message="ROM 版本查询超时，请稍后重试或检查内网访问。",
                job_id=context.job_id,
                job_dir=context.job_dir,
                command=command,
                duration_seconds=time.monotonic() - started,
                error_code="rom_version_lookup_timeout",
                stdout=exc.stdout or "",
                stderr=exc.stderr or "",
                details={"mode": "rom_version_lookup", "rom_version": request.rom_version},
            )
        except OSError as exc:
            return TaskResult(
                success=False,
                message=f"ROM 版本查询启动失败: {exc}",
                job_id=context.job_id,
                job_dir=context.job_dir,
                command=command,
                duration_seconds=time.monotonic() - started,
                error_code="rom_version_lookup_failed_to_start",
                stderr=str(exc),
                details={"mode": "rom_version_lookup", "rom_version": request.rom_version},
            )

        if completed.returncode != 0:
            return TaskResult(
                success=False,
                message="ROM 版本查询失败",
                job_id=context.job_id,
                job_dir=context.job_dir,
                command=command,
                duration_seconds=time.monotonic() - started,
                error_code="rom_version_lookup_failed",
                stdout=completed.stdout,
                stderr=completed.stderr,
                details={"mode": "rom_version_lookup", "rom_version": request.rom_version},
            )

        payload = self._parse_json(completed.stdout)
        if payload is None:
            return TaskResult(
                success=True,
                message=completed.stdout.strip() or "ROM 版本查询完成，但脚本没有输出内容。",
                job_id=context.job_id,
                job_dir=context.job_dir,
                command=command,
                duration_seconds=time.monotonic() - started,
                stdout=completed.stdout,
                stderr=completed.stderr,
                details={"mode": "rom_version_lookup", "rom_version": request.rom_version},
            )
        (context.output_dir / "rom_version_lookup.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return TaskResult(
            success=True,
            message=self._format_message(request.rom_version, payload),
            job_id=context.job_id,
            job_dir=context.job_dir,
            command=command,
            duration_seconds=time.monotonic() - started,
            stdout=completed.stdout,
            stderr=completed.stderr,
            details={
                "mode": "rom_version_lookup",
                "rom_version": request.rom_version,
                "required_outputs": payload.get("required_outputs", {}) if isinstance(payload, dict) else {},
            },
        )

    def _script_path(self) -> Path | None:
        candidates = [
            self.config.workspace_root / ".ai/skills/rom-version-lookup/scripts/lookup_rom_version.py",
            self.config.guideengine_repo / ".ai/skills/rom-version-lookup/scripts/lookup_rom_version.py",
            self.config.guideengine_repo / ".github/skills/rom-version-lookup/scripts/lookup_rom_version.py",
        ]
        for path in candidates:
            if path.exists():
                return path
        return None

    def _timeout_seconds(self) -> int:
        return max(30, min(300, int(self.config.runner_timeout_seconds or 120)))

    def _parse_json(self, text: str) -> dict[str, Any] | None:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    def _format_message(self, rom_version: str, payload: dict[str, Any]) -> str:
        required = payload.get("required_outputs", {})
        if not isinstance(required, dict):
            required = {}
        lines = [
            "ROM 版本查询完成",
            f"ROM: {rom_version}",
            f"导航版本: {self._value(required.get('navigation_version'))}",
            f"导航 Maven 地址: {self._value(required.get('navigation_maven_url'))}",
            f"Jenkins 路径: {self._value(required.get('jenkins_url'))}",
            f"符号表地址: {self._value(required.get('symbol_table_url'))}",
            f"Napa5 版本: {self._value(required.get('napa5_version'))}",
            f"Napa5 Maven 地址: {self._value(required.get('napa5_maven_url'))}",
            f"Napa5 下载地址: {self._value(required.get('napa5_download_url'))}",
        ]
        source = str(payload.get("source") or "").strip()
        if source:
            lines.append(f"数据来源: {source}")
        return "\n".join(lines)

    def _value(self, value: object) -> str:
        text = str(value or "").strip()
        return text or "N/A"
