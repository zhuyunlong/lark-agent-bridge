"""Analysis-history search text and deletion-path resolution."""

from __future__ import annotations

from pathlib import Path
import shutil

from .paths import _safe_slug

_ANALYSIS_HISTORY_MODES = {
    "bug_analysis",
    "direct_analysis",
    "signal_lifecycle",
    "perception_summary",
}


def _is_analysis_history_mode(mode: str) -> bool:
    return mode.strip() in _ANALYSIS_HISTORY_MODES


def _history_search_text(session: dict[str, object]) -> str:
    values = [
        session.get("session_id"),
        session.get("event_id"),
        session.get("chat_id"),
        session.get("mode"),
        session.get("status"),
        session.get("content"),
        session.get("message"),
        session.get("job_id"),
        session.get("report_url"),
    ]
    return "\n".join(str(value or "") for value in values)


def _delete_authorization_placeholder() -> dict[str, object]:
    return {
        "checked": True,
        "scope": "analysis_history.delete",
        "note": "写操作已通过 admin_auth 鉴权保护。",
    }


def _history_paths_to_delete(session: dict[str, object], *, root_dir: Path) -> list[Path]:
    data_dir = root_dir.parent
    jobs_root = data_dir / "jobs"
    raw_candidates: list[Path] = []
    job_dir = str(session.get("job_dir") or "").strip()
    job_id = str(session.get("job_id") or "").strip()
    if job_dir:
        raw_candidates.append(Path(job_dir))
    if job_id:
        raw_candidates.append(jobs_root / job_id)
        raw_candidates.append(root_dir / _safe_slug(job_id))
    for key in ("published_report_index", "published_report_dir"):
        details = session.get("details")
        if isinstance(details, dict):
            value = str(details.get(key) or "").strip()
            if value:
                raw_candidates.append(Path(value))

    candidates: list[Path] = []
    seen: set[Path] = set()
    for path in raw_candidates:
        try:
            resolved = path.expanduser().resolve()
        except OSError:
            resolved = path.expanduser()
        if not (_path_within(resolved, jobs_root) or _path_within(resolved, root_dir)):
            continue
        if resolved in {jobs_root.resolve(), root_dir.resolve()}:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        candidates.append(resolved)
    return candidates


def _path_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _remove_history_path(path: Path) -> bool:
    try:
        if not path.exists():
            return False
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        return True
    except OSError:
        return False
