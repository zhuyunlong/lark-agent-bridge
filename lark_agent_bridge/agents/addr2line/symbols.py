"""addr2line 符号解析：集成符号解析、符号源选择与下载。"""

from __future__ import annotations

import glob
import json
from pathlib import Path
import re
import shutil
import struct
import subprocess
import time
from typing import Any
import urllib.error
import urllib.request
from ...models import Addr2LineRequest, BridgeConfig, DownloadResource, LarkEvent, RomVersionLookupRequest, TaskResult, create_job_context
from ...network_env import build_internal_network_env
from .common import (
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


class _SymbolResolveMixin:
    """addr2line 符号解析：集成符号解析、符号源选择与下载。（与 Addr2LineRunner 共享 self 状态）。"""

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

