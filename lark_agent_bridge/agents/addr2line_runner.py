"""addr2line symbol reverse lookup runner."""

from __future__ import annotations

from dataclasses import dataclass
import glob
import json
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import time
from typing import Any
import urllib.error
import urllib.request
import zipfile

from ..downloader import DownloadError, LogDownloader
from ..health import ProcessWatchdog, run_tracked_process
from ..lark_client import LarkClient
from ..models import Addr2LineRequest, BridgeConfig, DownloadResource, LarkEvent, RomVersionLookupRequest, TaskResult, create_job_context
from ..network_env import build_internal_network_env
from ..parser import ROM_VERSION_RE

from .addr2line import _RequestPrepareMixin, _SymbolResolveMixin, _TraceReportMixin
from .addr2line.common import (
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

class Addr2LineRunner(_RequestPrepareMixin, _SymbolResolveMixin, _TraceReportMixin):
    def __init__(
        self,
        config: BridgeConfig,
        *,
        lark_client: LarkClient | None = None,
        rom_version_runner=None,
        process_watchdog: ProcessWatchdog | None = None,
    ) -> None:
        self.config = config
        self.lark_client = lark_client
        self.rom_version_runner = rom_version_runner
        self.process_watchdog = process_watchdog
        self._llvm_addr2line_cache: str | None = None

    def run_resolve(self, request: Addr2LineRequest, *, event: LarkEvent | None = None) -> TaskResult:
        context = create_job_context(self.config.data_dir, event=event)
        prepared = self._prepare_request(request, context=context, event=event)
        if isinstance(prepared, TaskResult):
            return prepared
        request = prepared.request
        if self._should_use_integrated_symbol_resolve(request):
            return self._run_integrated_symbol_resolve(request, prepared=prepared, context=context)
        script = self._script_path()
        if script is None:
            return TaskResult(
                success=False,
                message="addr2line-resolve skill 不可用：没有找到 addr2line_resolve.py。",
                job_id=context.job_id,
                job_dir=context.job_dir,
                error_code="addr2line_skill_missing",
                details={"mode": "addr2line_resolve"},
            )
        return self._run_external_resolve(request, script=script, prepared=prepared, context=context)

    def _run_external_resolve(
        self,
        request: Addr2LineRequest,
        *,
        script: Path,
        prepared: _PreparedResolveRequest,
        context,
    ) -> TaskResult:
        addr_source = prepared.addr_source
        version_kind, version_value = self._version_arg(request)
        command = self._external_command(request, script)
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
                    **({"symbol_table_url": request.symbol_table_url} if request.symbol_table_url else {}),
                    **({"napa5_download_url": request.napa5_download_url} if request.napa5_download_url else {}),
                    **({"addr_source": addr_source} if addr_source else {}),
                    **({"prop_source": prepared.prop_source} if prepared.prop_source else {}),
                    **({"inferred_rom_version": prepared.inferred_rom_version} if prepared.inferred_rom_version else {}),
                },
            )
        try:
            completed = run_tracked_process(
                command,
                watchdog=self.process_watchdog,
                name="addr2line-resolve",
                cwd=script.parent,
                env=build_internal_network_env(self.config.internal_network_env),
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
        self._annotate_payload_symbol_sources(payload, request)
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        symbol_sources = payload.get("symbol_sources") if isinstance(payload.get("symbol_sources"), dict) else {}
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
                **({"symbol_sources": symbol_sources} if symbol_sources else {}),
                **({"symbol_table_url": request.symbol_table_url} if request.symbol_table_url else {}),
                **({"napa5_download_url": request.napa5_download_url} if request.napa5_download_url else {}),
                **({"addr_source": addr_source} if addr_source else {}),
                **({"prop_source": prepared.prop_source} if prepared.prop_source else {}),
                **({"inferred_rom_version": prepared.inferred_rom_version} if prepared.inferred_rom_version else {}),
            },
        )

    def _external_command(self, request: Addr2LineRequest, script: Path) -> list[str]:
        version_kind, version_value = self._version_arg(request)
        command = [sys.executable, str(script)]
        command.extend([version_kind, version_value])
        command.extend(["--target", request.target or "auto", "--addr", request.addr_text.strip(), "--json"])
        return command

    def _should_use_integrated_symbol_resolve(self, request: Addr2LineRequest) -> bool:
        if (request.target or "auto") != "auto":
            return False
        if not request.symbol_table_url.strip() and not request.napa5_download_url.strip():
            return False
        return bool(self._addr_groups_by_so(request.addr_text))

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

    def _payload_from_result(self, result: TaskResult) -> dict[str, Any]:
        if result.json_report and result.json_report.exists():
            try:
                payload = json.loads(result.json_report.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = None
            if isinstance(payload, dict):
                return payload
        return self._parse_json(result.stdout) or {}

    def _payload_results(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        raw = payload.get("results")
        if not isinstance(raw, list):
            return []
        return [item for item in raw if isinstance(item, dict)]

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
        symbol_sources = payload.get("symbol_sources")
        if isinstance(symbol_sources, dict) and symbol_sources:
            source_labels = {
                "navi": "navi/envirodrive_so 符号表",
                "builtin_unity": "内置 libunity/libmain 符号表",
                "napa5": "Napa5 下载目录符号表",
                "addr2line_script": "addr2line 脚本兜底",
            }
            rendered_sources = [
                f"{so}: {source_labels.get(str(source), str(source))}"
                for so, source in sorted(symbol_sources.items())
            ]
            lines.append("符号来源: " + "; ".join(rendered_sources))
        if not results:
            lines.append("结果: 未反解到有效函数，请检查符号版本是否匹配。")
            return "\n".join(lines)
        lines.append("结论: 已按 so 类型选择对应符号来源完成反解；源码行只能说明 crash 位置，根因仍需结合寄存器、反汇编和源码上下文确认。")
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
