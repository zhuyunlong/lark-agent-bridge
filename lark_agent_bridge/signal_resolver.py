"""Resolve user-provided signal names against guideengine source definitions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import shutil
import subprocess


SIGNAL_ENUM_DEFINITION_RE = re.compile(
    r"(?<![A-Za-z0-9_])(SIGNAL_[A-Za-z0-9_]+)\s*(?:=\s*(\d+))?"
)
SIGNAL_SOURCE_SUFFIXES = {".kt", ".java", ".cpp", ".cc", ".c", ".h", ".hpp", ".proto", ".xml", ".md"}
MAX_SIGNAL_SOURCE_FILES = 256


@dataclass(frozen=True)
class SignalResolution:
    requested: str
    signal: str
    source: str


class SignalResolver:
    """Fuzzy resolver backed by signal definitions in the configured repo."""

    def __init__(self, repo: Path | str | None = None) -> None:
        self.repo = Path(repo).expanduser() if repo else None
        self._catalog: dict[str, str] | None = None

    def resolve(self, value: str) -> SignalResolution | None:
        requested = (value or "").strip()
        if not requested:
            return None
        if requested.isdigit():
            return SignalResolution(requested=requested, signal=requested, source="numeric")
        catalog = self._load_catalog()
        normalized = _normalize_signal_token(requested)
        if not normalized:
            return None
        exact = catalog.get(normalized)
        if exact:
            return SignalResolution(requested=requested, signal=exact, source="catalog_exact")
        suffix_matches = [
            signal for key, signal in catalog.items() if key.endswith(normalized) or normalized.endswith(key)
        ]
        unique_matches = sorted(set(suffix_matches), key=lambda item: (len(item), item))
        if len(unique_matches) == 1:
            return SignalResolution(requested=requested, signal=unique_matches[0], source="catalog_fuzzy")
        return None

    def _load_catalog(self) -> dict[str, str]:
        if self._catalog is not None:
            return self._catalog
        catalog: dict[str, str] = {}
        repo = self.repo
        if repo is None or not repo.exists():
            self._catalog = catalog
            return catalog
        for path in _candidate_signal_files(repo):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for match in SIGNAL_ENUM_DEFINITION_RE.finditer(text):
                name = match.group(1)
                catalog.setdefault(_normalize_signal_token(name), name)
                catalog.setdefault(_normalize_signal_token(name.removeprefix("SIGNAL_")), name)
                code = match.group(2)
                if code:
                    catalog.setdefault(code, name)
        self._catalog = catalog
        return catalog


def _candidate_signal_files(repo: Path) -> list[Path]:
    preferred = [
        repo / "module_floorcenter/module_proto/src/main/proto/signal.proto",
        repo / "module_foundation/module_proto/src/main/proto/signal.proto",
        repo / "module_floorcenter/module_xdata/src/main/cpp/core/generated/proto/signal.pb.h",
        repo / "module_floorcenter/module_xdata/src/main/cpp/core/generated/proto/win/signal.pb.h",
    ]
    files: list[Path] = []
    for path in preferred:
        _append_path(files, path)
    for path in repo.rglob("signal.proto"):
        if not _is_ignored_path(path):
            _append_path(files, path)
    for path in repo.rglob("signal.pb.h"):
        if not _is_ignored_path(path):
            _append_path(files, path)
    for path in _source_files_with_signal_refs(repo):
        _append_path(files, path)
    return files


def _source_files_with_signal_refs(repo: Path) -> list[Path]:
    rg_paths = _source_files_with_signal_refs_rg(repo)
    if rg_paths is not None:
        return rg_paths
    return _source_files_with_signal_refs_scan(repo)


def _source_files_with_signal_refs_rg(repo: Path) -> list[Path] | None:
    if shutil.which("rg") is None:
        return None
    command = [
        "rg",
        "--files-with-matches",
        "--color",
        "never",
        "--glob",
        "*.{kt,java,cpp,cc,c,h,hpp,proto,xml,md}",
        "--glob",
        "!.git/**",
        "--glob",
        "!.gradle/**",
        "--glob",
        "!build/**",
        "--glob",
        "!out/**",
        "--glob",
        "!.cxx/**",
        r"(?<![A-Za-z0-9_])SIGNAL_[A-Za-z0-9_]+",
        str(repo),
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode not in {0, 1}:
        return None
    paths: list[Path] = []
    for line in completed.stdout.splitlines():
        if len(paths) >= MAX_SIGNAL_SOURCE_FILES:
            break
        path = Path(line.strip())
        if path.exists() and not _is_ignored_path(path):
            paths.append(path)
    return paths


def _source_files_with_signal_refs_scan(repo: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(repo.rglob("*")):
        if len(files) >= MAX_SIGNAL_SOURCE_FILES:
            break
        if not path.is_file() or path.suffix not in SIGNAL_SOURCE_SUFFIXES or _is_ignored_path(path):
            continue
        try:
            if path.stat().st_size > 2_000_000:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "SIGNAL_" in text:
            files.append(path)
    return files


def _append_path(files: list[Path], path: Path) -> None:
    if not path.exists() or _is_ignored_path(path):
        return
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    if all((existing.resolve() if existing.exists() else existing) != resolved for existing in files):
        files.append(path)


def _is_ignored_path(path: Path) -> bool:
    return any(part in {".git", ".gradle", ".idea", "build", "out", ".cxx"} for part in path.parts)


def _normalize_signal_token(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", value or "").strip("_").upper()
