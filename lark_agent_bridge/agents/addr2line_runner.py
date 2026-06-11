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


_STACK_LINE_RE = re.compile(r"#\d+\s+pc\s+[0-9a-fA-F]{8,16}\s+\S*lib[\w.-]+\.so\b.*", re.I)
_ADDR_SO_PAIR_RE = re.compile(
    r"(?:#\d+\s+pc\s+)?(?<![0-9a-fA-F-])(?P<addr>[0-9a-fA-F]{8,16})(?![0-9a-fA-F])"
    r"\s+(?P<path>\S*?(?P<so>lib[\w.-]+\.so)\b)",
    re.I,
)
_MEMORY_MAP_LINE_RE = re.compile(r"^\s*[0-9a-fA-F]+-[0-9a-fA-F]+\s+[r-][w-][x-][ps]\s", re.I)
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
_THREAD_SUMMARY_SYSTEM_LIBS = {
    "libc.so",
    "libc++_shared.so",
    "libart.so",
    "libutils.so",
    "libandroid_runtime.so",
    "boot-framework.oat",
    "boot.oat",
    "app_process64",
}
_SCENE_SO_HINT_TERMS = (
    "unity",
    "render",
    "surface",
    "baidumap",
    "scene",
    "autodice",
    "gplatform",
    "gbl",
)
_NAVI_SYMBOL_SO_NAMES = {
    "libRenderExtend.so",
    "libxdata_sdk.so",
    "libxdata_client.so",
    "libxdata_native.so",
}
_BUILTIN_UNITY_SO_NAMES = {"libunity.so", "libmain.so"}
_NAPA5_SYMBOL_SO_NAMES = {"libil2cpp.so", "libunity.so", "libmain.so"}


@dataclass(slots=True)
class _TimePoint:
    hour: int
    minute: int
    second: int = 0
    month: int | None = None
    day: int | None = None
    year: int | None = None


@dataclass(slots=True)
class _CrashStackSelection:
    addr_text: str
    source_path: Path
    log_folder: str = ""
    timestamp: _TimePoint | None = None


@dataclass(slots=True)
class _PropSelection:
    rom_version: str
    source_path: Path
    log_folder: str = ""


@dataclass(slots=True)
class _PreparedResolveRequest:
    request: Addr2LineRequest
    addr_source: str = ""
    prop_source: str = ""
    inferred_rom_version: str = ""


@dataclass(slots=True)
class _SymbolSourceSelection:
    so_name: str
    addresses: list[str]
    source: str
    symbol_path: Path | None = None


@dataclass(slots=True)
class _ThreadTraceBlock:
    name: str
    tid: str
    pid: str = ""
    section_time: str = ""
    frames: list[str] = None

    def __post_init__(self) -> None:
        if self.frames is None:
            self.frames = []


@dataclass(slots=True)
class _FrameSummary:
    frame_no: int
    so_name: str
    address: str
    symbol: str = ""
    path: str = ""


class Addr2LineRunner:
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

    def _run_integrated_symbol_resolve(
        self,
        request: Addr2LineRequest,
        *,
        prepared: _PreparedResolveRequest,
        context,
    ) -> TaskResult:
        started = time.monotonic()
        groups = self._addr_groups_by_so(request.addr_text)
        selections = self._symbol_source_selections(groups, request)
        combined_results: list[dict[str, Any]] = []
        symbol_sources = {selection.so_name: selection.source for selection in selections}
        command: list[str] | None = None
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []

        if self.config.dry_run:
            version_kind, version_value = self._version_arg(request)
            external_so_names = {selection.so_name for selection in selections if selection.source in {"navi", "addr2line_script"}}
            external_addr_text = self._addr_text_for_so_names(request.addr_text, external_so_names)
            if external_addr_text.strip():
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
                command = self._external_command(self._copy_request(request, addr_text=external_addr_text), script)
            return TaskResult(
                success=True,
                message="dry-run: addr2line 符号源分发已规划",
                skipped=False,
                job_id=context.job_id,
                job_dir=context.job_dir,
                command=command,
                duration_seconds=time.monotonic() - started,
                details={
                    "mode": "addr2line_resolve",
                    "symbol_version": version_value,
                    "symbol_version_kind": version_kind.lstrip("-"),
                    "target": request.target or "auto",
                    "result_count": 0,
                    "symbol_sources": symbol_sources,
                    **({"symbol_table_url": request.symbol_table_url} if request.symbol_table_url else {}),
                    **({"napa5_download_url": request.napa5_download_url} if request.napa5_download_url else {}),
                    **({"addr_source": prepared.addr_source} if prepared.addr_source else {}),
                    **({"prop_source": prepared.prop_source} if prepared.prop_source else {}),
                    **({"inferred_rom_version": prepared.inferred_rom_version} if prepared.inferred_rom_version else {}),
                },
            )

        external_so_names = {selection.so_name for selection in selections if selection.source in {"navi", "addr2line_script"}}
        external_addr_text = self._addr_text_for_so_names(request.addr_text, external_so_names)
        if external_addr_text.strip():
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
            external_request = self._copy_request(request, addr_text=external_addr_text)
            external_result = self._run_external_resolve(external_request, script=script, prepared=prepared, context=context)
            if not external_result.success:
                return external_result
            command = external_result.command
            stdout_parts.append(external_result.stdout)
            stderr_parts.append(external_result.stderr)
            external_payload = self._payload_from_result(external_result)
            for item in self._payload_results(external_payload):
                combined_results.append(item)
                so_name = str(item.get("so") or "")
                if so_name:
                    symbol_sources.setdefault(so_name, self._symbol_source_for_result_so(so_name, request))

        local_selections = [selection for selection in selections if selection.source in {"builtin_unity", "napa5"}]
        if local_selections:
            llvm_path = self._find_llvm_addr2line_binary()
            if not llvm_path:
                return TaskResult(
                    success=False,
                    message="addr2line 反解失败：未找到 llvm-addr2line，无法反解本地符号表。",
                    job_id=context.job_id,
                    job_dir=context.job_dir,
                    duration_seconds=time.monotonic() - started,
                    error_code="addr2line_llvm_missing",
                    details={"mode": "addr2line_resolve", "so": [selection.so_name for selection in local_selections]},
                )
            for selection in local_selections:
                try:
                    sym_path = self._symbol_path_for_selection(selection, request, context)
                except OSError as exc:
                    return TaskResult(
                        success=False,
                        message=f"addr2line 反解失败：{selection.so_name} 符号表获取失败: {exc}",
                        job_id=context.job_id,
                        job_dir=context.job_dir,
                        duration_seconds=time.monotonic() - started,
                        error_code="addr2line_symbol_download_failed",
                        stderr=str(exc),
                        details={
                            "mode": "addr2line_resolve",
                            "so": selection.so_name,
                            **({"napa5_download_url": request.napa5_download_url} if selection.source == "napa5" else {}),
                        },
                    )
                resolved = self._run_local_addr2line(llvm_path, sym_path, selection.addresses)
                for item in resolved:
                    combined_results.append(
                        {
                            "so": selection.so_name,
                            "addr": item.get("addr", ""),
                            "function": item.get("function", "??"),
                            "location": item.get("location", "??:0"),
                        }
                    )
                symbol_sources[selection.so_name] = selection.source

        self._sort_results_by_input_order(combined_results, groups)

        version_kind, version_value = self._version_arg(request)
        payload = {
            "apk_version": request.apk_version or version_value,
            "so_artifact": "mixed",
            "symbol_sources": symbol_sources,
            "results": combined_results,
        }
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
            stdout="\n".join(part for part in stdout_parts if part),
            stderr="\n".join(part for part in stderr_parts if part),
            details={
                "mode": "addr2line_resolve",
                "symbol_version": version_value,
                "symbol_version_kind": version_kind.lstrip("-"),
                "target": request.target or "auto",
                "result_count": len(combined_results),
                "symbol_sources": symbol_sources,
                **({"symbol_table_url": request.symbol_table_url} if request.symbol_table_url else {}),
                **({"napa5_download_url": request.napa5_download_url} if request.napa5_download_url else {}),
                **({"addr_source": prepared.addr_source} if prepared.addr_source else {}),
                **({"prop_source": prepared.prop_source} if prepared.prop_source else {}),
                **({"inferred_rom_version": prepared.inferred_rom_version} if prepared.inferred_rom_version else {}),
            },
        )

    def _symbol_source_selections(
        self,
        groups: dict[str, list[str]],
        request: Addr2LineRequest,
    ) -> list[_SymbolSourceSelection]:
        selections: list[_SymbolSourceSelection] = []
        for so_name, addresses in groups.items():
            builtin_path = self._find_builtin_unity_symbol_path(so_name) if so_name in _BUILTIN_UNITY_SO_NAMES else None
            if builtin_path is not None:
                selections.append(
                    _SymbolSourceSelection(
                        so_name=so_name,
                        addresses=addresses,
                        source="builtin_unity",
                        symbol_path=builtin_path,
                    )
                )
                continue
            if so_name in _NAPA5_SYMBOL_SO_NAMES and request.napa5_download_url.strip():
                selections.append(_SymbolSourceSelection(so_name=so_name, addresses=addresses, source="napa5"))
                continue
            if so_name in _NAVI_SYMBOL_SO_NAMES:
                selections.append(_SymbolSourceSelection(so_name=so_name, addresses=addresses, source="navi"))
                continue
            selections.append(_SymbolSourceSelection(so_name=so_name, addresses=addresses, source="addr2line_script"))
        return selections

    def _symbol_path_for_selection(
        self,
        selection: _SymbolSourceSelection,
        request: Addr2LineRequest,
        context,
    ) -> Path:
        if selection.source == "builtin_unity":
            if selection.symbol_path is None:
                raise OSError("missing builtin unity symbol")
            return selection.symbol_path
        if selection.source == "napa5":
            return self._download_napa_so(request.napa5_download_url, selection.so_name, context)
        raise OSError(f"unsupported local symbol source: {selection.source}")

    def _sort_results_by_input_order(self, results: list[dict[str, Any]], groups: dict[str, list[str]]) -> None:
        order: dict[tuple[str, str], int] = {}
        index = 0
        for so_name, addresses in groups.items():
            for addr in addresses:
                order[(so_name, addr.lower())] = index
                index += 1
        results.sort(
            key=lambda item: order.get(
                (str(item.get("so") or ""), str(item.get("addr") or "").lower()),
                index,
            )
        )

    def _annotate_payload_symbol_sources(self, payload: dict[str, Any], request: Addr2LineRequest) -> None:
        raw_sources = payload.get("symbol_sources")
        symbol_sources = raw_sources if isinstance(raw_sources, dict) else {}
        for item in self._payload_results(payload):
            so_name = str(item.get("so") or "")
            if so_name:
                symbol_sources.setdefault(so_name, self._symbol_source_for_result_so(so_name, request))
        if symbol_sources:
            payload["symbol_sources"] = symbol_sources

    def _symbol_source_for_result_so(self, so_name: str, request: Addr2LineRequest) -> str:
        if so_name in _NAVI_SYMBOL_SO_NAMES:
            return "navi"
        if so_name in _BUILTIN_UNITY_SO_NAMES:
            return "builtin_unity"
        if so_name in _NAPA5_SYMBOL_SO_NAMES and request.napa5_download_url.strip():
            return "napa5"
        return "addr2line_script"

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

    def _addr_groups_by_so(self, text: str) -> dict[str, list[str]]:
        groups: dict[str, list[str]] = {}
        for line in text.splitlines():
            if _MEMORY_MAP_LINE_RE.search(line):
                continue
            for match in _ADDR_SO_PAIR_RE.finditer(line):
                so_name = match.group("so")
                addr = "0x" + (match.group("addr").lstrip("0") or "0")
                groups.setdefault(so_name, []).append(addr)
        return groups

    def _addr_text_for_so_names(self, text: str, so_names: set[str]) -> str:
        if not so_names:
            return ""
        kept: list[str] = []
        for line in text.splitlines():
            if _MEMORY_MAP_LINE_RE.search(line):
                continue
            matched_pairs = [
                match.group(0)
                for match in _ADDR_SO_PAIR_RE.finditer(line)
                if match.group("so") in so_names
            ]
            if matched_pairs:
                kept.extend(matched_pairs)
                continue
            match = re.search(r"\S*lib[\w.-]+\.so\b", line)
            if match is not None and Path(match.group(0)).name in so_names:
                kept.append(line)
        return "\n".join(kept)

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

    def _build_subrealitytrace_thread_report(
        self,
        *,
        roots: list[Path],
        extract_root: Path,
        source_hint: Path | None,
    ) -> dict[str, Any] | None:
        source_path = self._pick_subrealitytrace_source(roots, extract_root=extract_root, source_hint=source_hint)
        if source_path is None:
            return None
        text = self._read_text_tail(source_path)
        if not text:
            return None
        blocks = self._extract_subrealitytrace_blocks(text)
        if not blocks:
            return None
        selected = self._select_subrealitytrace_categories(blocks)
        if not selected["all"]:
            return None
        scene_source_blocks: list[_ThreadTraceBlock] = []
        seen_scene_blocks: set[tuple[str, str]] = set()
        for item_list in selected["categories"].values():
            for item in item_list:
                key = (item.name, item.tid)
                if key in seen_scene_blocks:
                    continue
                seen_scene_blocks.add(key)
                scene_source_blocks.append(item)
        return self._format_subrealitytrace_report(
            source_path=source_path,
            blocks=selected["all"],
            scene_source_blocks=scene_source_blocks,
            categories=selected["categories"],
            section_time=selected["section_time"],
        )

    def _pick_subrealitytrace_source(
        self,
        roots: list[Path],
        *,
        extract_root: Path,
        source_hint: Path | None,
    ) -> Path | None:
        if source_hint is not None:
            hint_text = self._read_text_tail(source_hint)
            if self._looks_like_subrealitytrace(source_hint, hint_text):
                return source_hint
        for path in self._iter_text_candidates(roots, extract_root=extract_root):
            text = self._read_text_tail(path)
            if self._looks_like_subrealitytrace(path, text):
                return path
        return None

    def _looks_like_subrealitytrace(self, path: Path, text: str) -> bool:
        if "subrealitytrace" in path.name.casefold():
            return True
        if not text:
            return False
        if "----- Waiting Channels:" in text and "sysTid=" in text:
            return True
        if "----- pid " in text and '"UnityMain" sysTid=' in text:
            return True
        return False

    def _extract_subrealitytrace_blocks(self, text: str) -> list[_ThreadTraceBlock]:
        section_re = re.compile(r"^-----\s+pid\s+(?P<pid>\d+)\s+at\s+(?P<time>.+?)\s+-----\s*$")
        header_re = re.compile(r'^\s*"(?P<name>[^"]+)"\s+sysTid=(?P<tid>\d+)\s*$')
        frame_re = re.compile(r"^\s*#\d+\s+pc\s+[0-9a-fA-F]{8,16}\s+\S+.*$")
        lines = text.splitlines()
        blocks: list[_ThreadTraceBlock] = []
        current_pid = ""
        current_time = ""
        index = 0
        while index < len(lines):
            line = lines[index]
            section_match = section_re.match(line)
            if section_match:
                current_pid = section_match.group("pid")
                current_time = section_match.group("time").strip()
                index += 1
                continue
            header_match = header_re.match(line)
            if header_match:
                block = _ThreadTraceBlock(
                    name=header_match.group("name"),
                    tid=header_match.group("tid"),
                    pid=current_pid,
                    section_time=current_time,
                    frames=[],
                )
                index += 1
                while index < len(lines) and frame_re.match(lines[index]):
                    block.frames.append(lines[index].strip())
                    index += 1
                if block.frames:
                    blocks.append(block)
                continue
            index += 1
        return blocks

    def _select_subrealitytrace_categories(self, blocks: list[_ThreadTraceBlock]) -> dict[str, Any]:
        latest_section_time = ""
        for block in reversed(blocks):
            if block.section_time:
                latest_section_time = block.section_time
                break
        scoped = [item for item in blocks if latest_section_time and item.section_time == latest_section_time] or blocks
        categories: dict[str, list[_ThreadTraceBlock]] = {
            "UnityMain": [],
            "XPD_*": [],
            "JniSurfaceTex*": [],
            "主线程": [],
            "渲染相关线程": [],
        }
        for block in scoped:
            name_cf = block.name.casefold()
            frame_text = "\n".join(block.frames).casefold()
            if name_cf == "unitymain":
                categories["UnityMain"].append(block)
            if block.name.startswith("XPD_"):
                categories["XPD_*"].append(block)
            if name_cf.startswith("jnisurfacetex"):
                categories["JniSurfaceTex*"].append(block)
            if name_cf == "main" or (block.pid and block.tid == block.pid) or "android.app.activitythread.main" in frame_text:
                categories["主线程"].append(block)
            if (
                "renderthread" in name_cf
                or "glthread" in name_cf
                or "jnisurfacetex" in name_cf
                or "unitymain" in name_cf
                or "renderthread::threadloop" in frame_text
                or "glsurfaceview$glthread" in frame_text
                or "libhwui.so" in frame_text
                or "renderextend::" in frame_text
            ):
                categories["渲染相关线程"].append(block)

        deduped_categories: dict[str, list[_ThreadTraceBlock]] = {}
        for label, items in categories.items():
            deduped_categories[label] = sorted(
                self._dedupe_trace_blocks(items),
                key=lambda item, category=label: self._trace_block_priority(category, item),
            )

        ordered: list[_ThreadTraceBlock] = []
        seen: set[tuple[str, str]] = set()
        order_limits = {
            "UnityMain": 2,
            "JniSurfaceTex*": 3,
            "渲染相关线程": 4,
            "主线程": 2,
            "XPD_*": 4,
        }
        for label in ("UnityMain", "JniSurfaceTex*", "渲染相关线程", "主线程", "XPD_*"):
            for block in deduped_categories[label][: order_limits[label]]:
                key = (block.name, block.tid)
                if key in seen:
                    continue
                seen.add(key)
                ordered.append(block)
        return {"all": ordered, "categories": deduped_categories, "section_time": latest_section_time}

    def _dedupe_trace_blocks(self, blocks: list[_ThreadTraceBlock]) -> list[_ThreadTraceBlock]:
        seen: set[tuple[str, str]] = set()
        result: list[_ThreadTraceBlock] = []
        for item in blocks:
            key = (item.name, item.tid)
            if key in seen:
                continue
            seen.add(key)
            result.append(item)
        return result

    def _trace_block_priority(self, category: str, block: _ThreadTraceBlock) -> tuple[int, int, str]:
        name_cf = block.name.casefold()
        frame_text = "\n".join(block.frames).casefold()
        if category == "UnityMain":
            return (0, 0, name_cf)
        if category == "JniSurfaceTex*":
            return (0, 0, name_cf)
        if category == "渲染相关线程":
            interesting_count = sum(
                1
                for line in block.frames[:8]
                if any(term in line.casefold() for term in ("render", "glthread", "libhwui", "libunity", "surface"))
            )
            if "jnisurfacetex" in name_cf:
                return (0, -interesting_count, name_cf)
            if "renderthread" in name_cf:
                return (1, -interesting_count, name_cf)
            if "glthread" in name_cf:
                return (2, -interesting_count, name_cf)
            if "renderextend" in frame_text:
                return (3, -interesting_count, name_cf)
            return (4, -interesting_count, name_cf)
        if category == "主线程":
            if block.pid and block.tid == block.pid:
                return (0, 0, name_cf)
            return (1, 0, name_cf)
        if category == "XPD_*":
            name_rank = 9
            for index, token in enumerate(("jni", "x3d", "ld", "map", "surface", "render")):
                if token in name_cf or token in frame_text:
                    name_rank = index
                    break
            interesting_count = sum(1 for line in block.frames[:6] if any(term in line.casefold() for term in _SCENE_SO_HINT_TERMS))
            return (name_rank, -interesting_count, name_cf)
        return (9, 0, name_cf)

    def _format_subrealitytrace_report(
        self,
        *,
        source_path: Path,
        blocks: list[_ThreadTraceBlock],
        scene_source_blocks: list[_ThreadTraceBlock],
        categories: dict[str, list[_ThreadTraceBlock]],
        section_time: str,
    ) -> dict[str, Any]:
        scene_summaries = self._collect_scene_so_summaries(scene_source_blocks)
        current_time_label = self._subrealitytrace_display_time(source_path, section_time)
        previous_source = self._find_previous_subrealitytrace_source(source_path)
        previous_blocks = self._extract_subrealitytrace_blocks(self._read_text_tail(previous_source)) if previous_source else []
        current_focus = self._select_focus_trace_blocks(blocks)
        previous_focus = self._map_focus_trace_blocks(previous_blocks)
        thread_summaries = [
            self._build_focus_thread_summary(
                block,
                current_time_label=current_time_label,
                previous_block=previous_focus.get(self._focus_thread_key(block)),
                previous_time_label=self._subrealitytrace_display_time(previous_source, previous_blocks[0].section_time if previous_blocks else "") if previous_source else "",
            )
            for block in current_focus
        ]
        lines = [
            "单文件线程解析完成（subrealitytrace）",
            "说明: 当前输入缺少 ROM/Napa/APK 版本；已优先使用 trace 自带符号，并对 libunity.so / libmain.so 使用本地 Unity 符号表做补充反解。",
            f"文件: {source_path}",
        ]
        if section_time:
            lines.append(f"采样时间: {section_time}")
        counts = {label: len(items) for label, items in categories.items()}
        if scene_summaries:
            lines.append("场景/渲染相关 so:")
            for item in scene_summaries[:8]:
                lines.append(f"- {item['so_name']}: {item['summary']}")
        if counts:
            lines.append(
                "线程分类统计: "
                + ", ".join(
                    f"{label}={counts[label]}"
                    for label in ("UnityMain", "JniSurfaceTex*", "主线程", "渲染相关线程", "XPD_*")
                )
            )
        lines.append("结果如下：")
        for item in thread_summaries:
            lines.append("---")
            lines.append(item["title"])
            lines.extend(item["frame_lines"])
            if item["delta"]:
                lines.append("")
                lines.append(item["delta"])
        payload = {
            "source_path": str(source_path),
            "section_time": section_time,
            "current_time_label": current_time_label,
            "previous_source_path": str(previous_source) if previous_source else "",
            "scene_so_summaries": scene_summaries,
            "categories": {
                label: [
                    {
                        "name": block.name,
                        "sys_tid": block.tid,
                        "pid": block.pid,
                        "frames": block.frames[:12],
                    }
                    for block in items
                ]
                for label, items in categories.items()
            },
            "selected_threads": thread_summaries,
        }
        return {
            "message": "\n".join(lines),
            "payload": payload,
            "counts": counts,
            "source_path": str(source_path),
            "section_time": section_time,
        }

    def _select_focus_trace_blocks(self, blocks: list[_ThreadTraceBlock]) -> list[_ThreadTraceBlock]:
        picks: list[_ThreadTraceBlock] = []
        for block in blocks:
            key = self._focus_thread_key(block)
            if key == "unitymain" and not any(self._focus_thread_key(item) == key for item in picks):
                picks.append(block)
        for block in blocks:
            key = self._focus_thread_key(block)
            if key == "jnisurfacetextu" and not any(self._focus_thread_key(item) == key for item in picks):
                picks.append(block)
        if not picks:
            picks.extend(blocks[:2])
        return picks[:2]

    def _map_focus_trace_blocks(self, blocks: list[_ThreadTraceBlock]) -> dict[str, _ThreadTraceBlock]:
        mapped: dict[str, _ThreadTraceBlock] = {}
        for block in blocks:
            key = self._focus_thread_key(block)
            if key and key not in mapped:
                mapped[key] = block
        return mapped

    def _focus_thread_key(self, block: _ThreadTraceBlock) -> str:
        name_cf = block.name.casefold()
        if name_cf == "unitymain":
            return "unitymain"
        if name_cf.startswith("jnisurfacetextu"):
            return "jnisurfacetextu"
        if name_cf.startswith("jnisurfacetex"):
            return "jnisurfacetextu"
        return name_cf

    def _build_focus_thread_summary(
        self,
        block: _ThreadTraceBlock,
        *,
        current_time_label: str,
        previous_block: _ThreadTraceBlock | None,
        previous_time_label: str,
    ) -> dict[str, Any]:
        current_frames = self._resolve_trace_block_frames(block)
        previous_frames = self._resolve_trace_block_frames(previous_block) if previous_block is not None else []
        return {
            "name": block.name,
            "sys_tid": block.tid,
            "pid": block.pid,
            "title": f'{block.name}  sysTid={block.tid}' + (f"  ({current_time_label})" if current_time_label else ""),
            "frame_lines": [self._render_full_frame_line(item) for item in current_frames],
            "frames": [self._frame_to_dict(item) for item in current_frames],
            "delta": self._describe_trace_transition(current_frames, previous_frames, previous_time_label),
        }

    def _resolve_trace_block_frames(self, block: _ThreadTraceBlock | None) -> list[_FrameSummary]:
        if block is None:
            return []
        frames = [self._parse_trace_frame(line) for line in block.frames]
        return self._resolve_unity_frame_symbols(frames)

    def _parse_trace_frame(self, line: str) -> _FrameSummary:
        match = re.match(r"^\s*#(?P<frame_no>\d+)\s+pc\s+(?P<address>[0-9a-fA-F]{8,16})\s+(?P<path>\S+)(?P<rest>.*)$", line)
        if not match:
            return _FrameSummary(frame_no=-1, so_name="", address="", path="", symbol="")
        frame_no = int(match.group("frame_no"))
        address = match.group("address")
        path = match.group("path")
        remainder = match.group("rest")
        groups = self._extract_parenthetical_groups(remainder)
        symbol = ""
        for candidate in groups:
            candidate_text = candidate.strip()
            candidate_cf = candidate_text.casefold()
            if (
                not candidate_text
                or candidate_cf.startswith("buildid:")
                or candidate_cf.startswith("offset ")
                or candidate_cf == "deleted"
            ):
                continue
            symbol = candidate_text
            break
        return _FrameSummary(
            frame_no=frame_no,
            so_name=self._trace_display_so_name(path),
            address=address.lower(),
            path=path,
            symbol=symbol,
        )

    def _trace_display_so_name(self, path: str) -> str:
        if "jit-cache" in path:
            return "jit-cache"
        name = Path(path).name
        return name or path

    def _extract_parenthetical_groups(self, text: str) -> list[str]:
        groups: list[str] = []
        current: list[str] = []
        depth = 0
        for char in text:
            if char == "(":
                if depth > 0:
                    current.append(char)
                depth += 1
                continue
            if char == ")":
                if depth == 0:
                    continue
                depth -= 1
                if depth == 0:
                    groups.append("".join(current))
                    current = []
                else:
                    current.append(char)
                continue
            if depth > 0:
                current.append(char)
        return groups

    def _render_frame_summary(self, item: _FrameSummary) -> str:
        display = self._display_frame_symbol(item)
        return f"{item.so_name}: {display}"

    def _frame_to_dict(self, item: _FrameSummary) -> dict[str, Any]:
        return {
            "frame_no": item.frame_no,
            "so_name": item.so_name,
            "address": item.address,
            "symbol": item.symbol,
            "path": item.path,
            "display_symbol": self._display_frame_symbol(item),
        }

    def _render_full_frame_line(self, item: _FrameSummary) -> str:
        return f"#{item.frame_no:02d}  {self._display_frame_symbol(item)}  {item.so_name}"

    def _display_frame_symbol(self, item: _FrameSummary) -> str:
        if not item.symbol:
            return f"+0x{item.address}"
        symbol = re.sub(r"\+\d+$", "", item.symbol).strip()
        if "." in symbol and "::" not in symbol:
            parts = symbol.split(".")
            if len(parts) >= 2:
                symbol = ".".join(parts[-2:])
        if symbol.startswith("art::"):
            symbol = symbol[5:]
        symbol = re.sub(r"\([^)]*\)", "", symbol).strip()
        symbol = re.sub(r"<[^>]+>", "", symbol).strip()
        return symbol or item.symbol

    def _resolve_unity_frame_symbols(self, frames: list[_FrameSummary]) -> list[_FrameSummary]:
        by_so: dict[str, list[_FrameSummary]] = {}
        for frame in frames:
            if frame.so_name in {"libunity.so", "libmain.so"} and not frame.symbol:
                by_so.setdefault(frame.so_name, []).append(frame)
        if not by_so:
            return frames
        llvm_path = self._find_llvm_addr2line_binary()
        if not llvm_path:
            return frames
        for so_name, items in by_so.items():
            sym_path = self._find_builtin_unity_symbol_path(so_name)
            if sym_path is None:
                continue
            resolved = self._run_local_addr2line(llvm_path, sym_path, [f"0x{item.address.lstrip('0') or '0'}" for item in items])
            resolved_map = {entry["addr"].lower(): entry["function"] for entry in resolved if entry.get("function")}
            for item in items:
                key = f"0x{item.address.lstrip('0') or '0'}".lower()
                symbol = str(resolved_map.get(key) or "").strip()
                if symbol and symbol != "??":
                    item.symbol = symbol
        return frames

    def _find_llvm_addr2line_binary(self) -> str | None:
        if self._llvm_addr2line_cache:
            return self._llvm_addr2line_cache
        patterns = [
            str(Path("~/Library/Android/sdk/ndk/*/toolchains/llvm/prebuilt/*/bin/llvm-addr2line").expanduser()),
            str(Path("~/Android/Sdk/ndk/*/toolchains/llvm/prebuilt/*/bin/llvm-addr2line").expanduser()),
        ]
        for pattern in patterns:
            matches = sorted(glob.glob(pattern))
            if matches:
                self._llvm_addr2line_cache = matches[-1]
                return self._llvm_addr2line_cache
        return None

    def _find_builtin_unity_symbol_path(self, so_name: str) -> Path | None:
        symbol_file = "libunity.sym.so" if so_name == "libunity.so" else "libmain.sym.so"
        base_candidates: list[Path] = []
        search_roots = [self.config.workspace_root, self.config.guideengine_repo, Path.cwd()]
        for root in search_roots:
            for parent in [root, *root.parents[:5]]:
                base_candidates.append(parent / ".ai/skills/addr2line-resolve/references/unity-symbols")
                base_candidates.append(parent / ".github/skills/addr2line-resolve/references/unity-symbols")
        for base in base_candidates:
            path = base / symbol_file
            if path.exists():
                return path
        return None

    def _download_napa_so(self, download_url: str, so_name: str, context) -> Path:
        base_url = download_url.strip().rstrip("/")
        if not base_url:
            raise OSError("missing napa5_download_url")
        target_dir = context.input_dir / "napa5_symbols"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / so_name
        if self._is_reusable_elf(target):
            return target
        url = f"{base_url}/{so_name}"
        partial = target.with_name(f"{target.name}.part")
        try:
            partial.unlink()
        except OSError:
            pass
        request = urllib.request.Request(url, headers={"User-Agent": "lark-agent-bridge/addr2line"})
        try:
            with self._open_internal_url(request, timeout=self._timeout_seconds()) as response, partial.open("wb") as dst:
                response_headers = getattr(response, "headers", None)
                shutil.copyfileobj(response, dst)
            expected_size = self._content_length(response_headers)
            actual_size = partial.stat().st_size
            if expected_size is not None and actual_size != expected_size:
                raise OSError(f"incomplete download: expected {expected_size} bytes, got {actual_size}")
            if actual_size <= 0:
                raise OSError("empty download")
            partial.replace(target)
        except (urllib.error.URLError, OSError) as exc:
            try:
                partial.unlink()
            except OSError:
                pass
            raise OSError(str(exc)) from exc
        return target

    def _content_length(self, headers: object) -> int | None:
        try:
            value = headers.get("Content-Length")  # type: ignore[attr-defined]
        except AttributeError:
            return None
        if value is None:
            return None
        try:
            length = int(str(value).strip())
        except ValueError:
            return None
        return length if length >= 0 else None

    def _is_reusable_elf(self, path: Path) -> bool:
        try:
            size = path.stat().st_size
            if size <= 0:
                return False
            with path.open("rb") as fh:
                header = fh.read(64)
        except OSError:
            return False
        if len(header) < 16 or header[:4] != b"\x7fELF":
            return False
        elf_class = header[4]
        endian_flag = header[5]
        endian = "<" if endian_flag == 1 else ">" if endian_flag == 2 else ""
        if not endian:
            return False
        try:
            if elf_class == 2:
                if len(header) < 64:
                    return False
                e_shoff = struct.unpack_from(f"{endian}Q", header, 0x28)[0]
                e_shentsize = struct.unpack_from(f"{endian}H", header, 0x3A)[0]
                e_shnum = struct.unpack_from(f"{endian}H", header, 0x3C)[0]
            elif elf_class == 1:
                if len(header) < 52:
                    return False
                e_shoff = struct.unpack_from(f"{endian}I", header, 0x20)[0]
                e_shentsize = struct.unpack_from(f"{endian}H", header, 0x2E)[0]
                e_shnum = struct.unpack_from(f"{endian}H", header, 0x30)[0]
            else:
                return False
        except struct.error:
            return False
        if not e_shoff or not e_shentsize or not e_shnum:
            return True
        return e_shoff + (e_shentsize * e_shnum) <= size

    def _open_internal_url(self, request: urllib.request.Request, *, timeout: int):
        env = build_internal_network_env(self.config.internal_network_env)
        proxies: dict[str, str] = {}
        for scheme in ("http", "https"):
            value = env.get(f"{scheme}_proxy") or env.get(f"{scheme.upper()}_PROXY")
            if value:
                proxies[scheme] = value
        opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies))
        return opener.open(request, timeout=timeout)

    def _run_local_addr2line(self, llvm_path: str, sym_path: Path, addresses: list[str]) -> list[dict[str, str]]:
        if not addresses:
            return []
        try:
            result = subprocess.run(
                [llvm_path, "-f", "-C", "-e", str(sym_path), *addresses],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return []
        lines = [line.rstrip() for line in result.stdout.splitlines()]
        resolved: list[dict[str, str]] = []
        for index, addr in enumerate(addresses):
            func = lines[index * 2] if index * 2 < len(lines) else ""
            loc = lines[index * 2 + 1] if index * 2 + 1 < len(lines) else ""
            resolved.append({"addr": addr, "function": func, "location": loc})
        return resolved

    def _subrealitytrace_display_time(self, source_path: Path | None, section_time: str) -> str:
        if source_path is not None:
            match = re.search(r"subrealitytrace_(\d{4}-\d{2}-\d{2})-(\d{2})-(\d{2})-(\d{2})$", source_path.name)
            if match:
                return f"{match.group(2)}:{match.group(3)}:{match.group(4)}"
        time_match = re.search(r"(\d{2}:\d{2}:\d{2})", section_time)
        return time_match.group(1) if time_match else ""

    def _find_previous_subrealitytrace_source(self, source_path: Path) -> Path | None:
        match = re.search(r"subrealitytrace_(\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2})$", source_path.name)
        if not match:
            return None
        current_key = match.group(1)
        siblings = sorted(source_path.parent.glob("subrealitytrace_*"))
        previous: Path | None = None
        for candidate in siblings:
            candidate_match = re.search(r"subrealitytrace_(\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2})$", candidate.name)
            if not candidate_match:
                continue
            if candidate_match.group(1) < current_key:
                previous = candidate
            elif candidate == source_path:
                break
        return previous

    def _describe_trace_transition(
        self,
        current_frames: list[_FrameSummary],
        previous_frames: list[_FrameSummary],
        previous_time_label: str,
    ) -> str:
        if not current_frames or not previous_frames or not previous_time_label:
            return ""
        current_key = self._top_meaningful_frame(current_frames)
        previous_key = self._top_meaningful_frame(previous_frames)
        if current_key is None or previous_key is None:
            return ""
        current_text = self._display_frame_symbol(current_key)
        previous_text = self._display_frame_symbol(previous_key)
        if current_text == previous_text:
            return ""
        if "WaitVSync" in current_text:
            return f"▎ 与上一帧 {previous_time_label} 不同：当前已进入 {current_text}，说明 UnityMain 在等待上一帧 present / VSync。"
        if "ThreadedStreamBuffer::HandleOutOfBufferToReadFrom" in current_text:
            return f"▎ 与上一帧 {previous_time_label} 不同：当前已从 GPU 提交路径切到 {current_text}，说明渲染线程在等新的命令缓冲。"
        return f"▎ 与上一帧 {previous_time_label} 不同：顶部关键帧从 {previous_text} 切换为 {current_text}。"

    def _top_meaningful_frame(self, frames: list[_FrameSummary]) -> _FrameSummary | None:
        for item in frames:
            display = self._display_frame_symbol(item)
            if not display:
                continue
            if display in {"syscall", "__futex_wait_ex", "pthread_cond_wait", "__pthread_start", "__start_thread", "sem_wait"}:
                continue
            if display.startswith("UnityClassic::Baselib_SystemFutex_Wait") or display.startswith("Semaphore::WaitForSignal"):
                continue
            return item
        return frames[0] if frames else None

    def _collect_scene_so_summaries(self, blocks: list[_ThreadTraceBlock]) -> list[dict[str, str]]:
        buckets: dict[str, list[str]] = {}
        for block in blocks:
            for frame in self._resolve_unity_frame_symbols([self._parse_trace_frame(line) for line in block.frames]):
                so_cf = frame.so_name.casefold()
                if not so_cf or not any(term in so_cf for term in _SCENE_SO_HINT_TERMS):
                    continue
                buckets.setdefault(frame.so_name, [])
                rendered = self._display_frame_symbol(frame)
                if rendered not in buckets[frame.so_name]:
                    buckets[frame.so_name].append(rendered)
        ranked = sorted(
            buckets.items(),
            key=lambda item: (-len(item[1]), item[0].casefold()),
        )
        return [
            {
                "so_name": so_name,
                "summary": "; ".join(entries[:3]),
            }
            for so_name, entries in ranked
        ]

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

    def _resolve_navigation_symbol_outputs_from_rom(self, rom_version: str, *, event: LarkEvent | None) -> dict[str, object] | TaskResult:
        if self.rom_version_runner is None:
            return {}
        lookup_result = self.rom_version_runner.run_lookup(
            RomVersionLookupRequest(
                rom_version=rom_version,
                prompt="从日志 prop.txt 推断 ROM 后继续 addr2line",
                raw_text=rom_version,
                triggered=True,
            ),
            event=event,
        )
        if not lookup_result.success:
            return TaskResult(
                success=False,
                message=(
                    "ROM 查询失败，无法确定导航符号表版本；"
                    "当前不会再按日期近似猜测符号表，请先修复 ROM 查询或直接提供 APK 版本后重试。\n"
                    f"{lookup_result.message}"
                ),
                error_code="addr2line_symbol_version_lookup_failed",
                stdout=lookup_result.stdout,
                stderr=lookup_result.stderr,
                details={
                    "mode": "addr2line_resolve",
                    "rom_version": rom_version,
                    "lookup_error_code": lookup_result.error_code,
                },
            )
        details = lookup_result.details if isinstance(lookup_result.details, dict) else {}
        required_outputs = details.get("required_outputs")
        navigation_version = ""
        if isinstance(required_outputs, dict):
            navigation_version = str(required_outputs.get("navigation_version") or "").strip()
        if navigation_version:
            return required_outputs if isinstance(required_outputs, dict) else {}
        return TaskResult(
            success=False,
            message=(
                "ROM 查询没有返回导航版本，无法确定导航符号表版本；"
                "当前不会再按日期近似猜测符号表，请先修复 ROM 查询或直接提供 APK 版本后重试。"
            ),
            error_code="addr2line_symbol_version_lookup_failed",
            stdout=lookup_result.stdout,
            stderr=lookup_result.stderr,
            details={
                "mode": "addr2line_resolve",
                "rom_version": rom_version,
                "lookup_error_code": lookup_result.error_code,
            },
        )

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
