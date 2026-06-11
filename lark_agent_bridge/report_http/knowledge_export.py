"""Knowledge export report location and status."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from ..models import BridgeConfig

_KNOWLEDGE_EXPORT_REPORT_ROUTES = {"/knowledge/export-report", "/knowledge/export-report.html"}
_KNOWLEDGE_EXPORT_REPORT_NAME = "knowledge_export_report.html"


def _knowledge_export_report_path(config: BridgeConfig) -> Path:
    return config.data_dir / "knowledge" / _KNOWLEDGE_EXPORT_REPORT_NAME


def _knowledge_export_report_status(config: BridgeConfig) -> dict[str, object]:
    report_path = _knowledge_export_report_path(config)
    payload: dict[str, object] = {
        "exists": False,
        "url": "/knowledge/export-report",
        "path": str(report_path),
        "updated_at": "",
        "size_bytes": 0,
        "version": "",
    }
    if not report_path.is_file():
        return payload
    stat = report_path.stat()
    payload.update(
        {
            "exists": True,
            "updated_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            "size_bytes": stat.st_size,
            "version": f"{stat.st_mtime_ns}-{stat.st_size}",
        }
    )
    return payload
