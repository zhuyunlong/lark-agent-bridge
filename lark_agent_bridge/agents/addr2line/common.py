"""addr2line 共享常量与数据结构。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from ...models import Addr2LineRequest, BridgeConfig, DownloadResource, LarkEvent, RomVersionLookupRequest, TaskResult, create_job_context


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


