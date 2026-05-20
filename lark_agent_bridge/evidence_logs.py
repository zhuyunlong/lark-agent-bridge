"""Preserve focused evidence logs for completed log investigations."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
from typing import Any, Iterable


_LOG_SEGMENT_RE = re.compile(r"^log[0-9]+$", re.IGNORECASE)
_NAVIGATION_TERMS = (
    "montecarlo",
    "navi",
    "navigation",
    "baidumap",
    "mapauto",
    "ldnavi",
    "guideengine",
)
_VEHICLE_TERMS = ("vehicle", "vhal", "carcontrol", "car_control")


def preserve_evidence_log_bundle(
    *,
    output_dir: Path,
    source_roots: Iterable[Path | str | None],
    reference_files: Iterable[Path | str | None] = (),
    reference_texts: Iterable[str] = (),
    bundle_name: str = "evidence_logs",
) -> dict[str, Any] | None:
    """Copy the focused navigation/logd/vehicle evidence logs into a job output.

    The focus log directory is inferred from report/metadata references. When a
    report cites ``log1`` under the extracted log package, the bundle keeps only
    evidence under that ``log1`` rather than copying every ``log0/log1/log2``.
    """

    roots = _existing_directories(source_roots)
    if not roots:
        return None

    files_by_root = _collect_files(roots)
    if not files_by_root:
        return None

    reference_blob = _reference_blob(reference_files, reference_texts)
    referenced: set[Path] = set()
    focus_logs: set[str] = set()
    for root, files in files_by_root.items():
        for source in files:
            rel = _relative_path(source, root)
            if _path_is_referenced(source, rel, reference_blob):
                referenced.add(source)
                log_name = _log_segment(rel)
                if log_name:
                    focus_logs.add(log_name)

    if not focus_logs:
        discovered = sorted({_log_segment(_relative_path(path, root)) for root, files in files_by_root.items() for path in files})
        discovered = [item for item in discovered if item]
        if len(set(discovered)) == 1:
            focus_logs.add(discovered[0])

    if not focus_logs and referenced:
        focus_logs.add("")
    if not focus_logs:
        return None

    destination_root = output_dir / bundle_name
    if destination_root.exists():
        shutil.rmtree(destination_root)
    copied: list[dict[str, Any]] = []
    seen_destinations: set[Path] = set()

    for root, files in files_by_root.items():
        for source in files:
            rel = _relative_path(source, root)
            log_name = _log_segment(rel)
            if "" not in focus_logs and log_name not in focus_logs:
                continue
            reasons = _copy_reasons(source, rel, referenced)
            if not reasons:
                continue
            destination = destination_root / rel
            if destination in seen_destinations:
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            seen_destinations.add(destination)
            copied.append(
                {
                    "source_path": str(source),
                    "relative_path": rel.as_posix(),
                    "bundle_path": str(destination),
                    "reason": "+".join(reasons),
                    "size_bytes": source.stat().st_size,
                }
            )

    if not copied:
        return None

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_roots": [str(root) for root in roots],
        "focus_logs": sorted(item for item in focus_logs if item),
        "files": copied,
    }
    manifest_path = destination_root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "bundle_dir": str(destination_root),
        "manifest_path": str(manifest_path),
        "focus_logs": manifest["focus_logs"],
        "file_count": len(copied),
    }


def _existing_directories(values: Iterable[Path | str | None]) -> list[Path]:
    roots: list[Path] = []
    seen: set[Path] = set()
    for value in values:
        if value is None:
            continue
        path = Path(value).expanduser()
        if path.is_file():
            path = path.parent
        if not path.is_dir():
            continue
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if resolved in seen:
            continue
        seen.add(resolved)
        roots.append(resolved)
    return roots


def _collect_files(roots: Iterable[Path]) -> dict[Path, list[Path]]:
    collected: dict[Path, list[Path]] = {}
    for root in roots:
        files: list[Path] = []
        for path in root.rglob("*"):
            if path.is_file():
                try:
                    files.append(path.resolve())
                except OSError:
                    files.append(path)
        if files:
            collected[root] = files
    return collected


def _reference_blob(reference_files: Iterable[Path | str | None], reference_texts: Iterable[str]) -> str:
    parts: list[str] = []
    for value in reference_files:
        if value is None:
            continue
        path = Path(value)
        if not path.is_file():
            continue
        try:
            parts.append(path.read_text(encoding="utf-8", errors="replace")[:2_000_000])
        except OSError:
            continue
    parts.extend(str(item) for item in reference_texts if str(item).strip())
    return "\n".join(parts)


def _path_is_referenced(source: Path, relative_path: Path, reference_blob: str) -> bool:
    if not reference_blob:
        return False
    candidates = {
        str(source),
        source.as_posix(),
        relative_path.as_posix(),
        str(relative_path),
    }
    return any(candidate and candidate in reference_blob for candidate in candidates)


def _relative_path(source: Path, root: Path) -> Path:
    try:
        return source.relative_to(root)
    except ValueError:
        return Path(source.name)


def _log_segment(relative_path: Path) -> str:
    for part in relative_path.parts:
        if _LOG_SEGMENT_RE.fullmatch(part):
            return part.lower()
    return ""


def _copy_reasons(source: Path, relative_path: Path, referenced: set[Path]) -> list[str]:
    reasons: list[str] = []
    if source in referenced:
        reasons.append("referenced")
    if _is_navigation_log(relative_path):
        reasons.append("navigation")
    if _is_logd(relative_path):
        reasons.append("logd")
    if _is_vehicle_log(relative_path):
        reasons.append("vehicle")
    return reasons


def _is_navigation_log(relative_path: Path) -> bool:
    text = relative_path.as_posix().casefold()
    return any(term in text for term in _NAVIGATION_TERMS)


def _is_logd(relative_path: Path) -> bool:
    return any(part.casefold() == "logd" for part in relative_path.parts)


def _is_vehicle_log(relative_path: Path) -> bool:
    text = relative_path.as_posix().casefold()
    return any(term in text for term in _VEHICLE_TERMS)
