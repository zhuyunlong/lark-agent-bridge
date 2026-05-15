"""Lark Base/Doc/Drive archival workflow for completed analyses."""

from __future__ import annotations

from datetime import datetime, timezone
import html
import json
from pathlib import Path
from typing import Any

from .lark_client import LarkClient
from .models import BridgeConfig, LarkEvent, TaskResult


class WorkflowArchiver:
    """Archive completed analysis results into Lark workspace objects.

    The workflow is intentionally best-effort. Archive failures are returned in
    the result details, but they do not turn a successful analysis into a failed
    analysis.
    """

    def __init__(self, config: BridgeConfig, lark_client: LarkClient) -> None:
        self.config = config
        self.lark_client = lark_client

    def archive(self, result: TaskResult, *, event: LarkEvent, request_text: str) -> dict[str, Any]:
        options = self.config.workflow_archive
        if not options.enabled:
            return {"enabled": False, "skipped": True, "reason": "disabled"}
        if not result.success or result.skipped or not result.job_id:
            return {"enabled": True, "skipped": True, "reason": "not_archivable"}

        commands: list[dict[str, Any]] = []
        archive: dict[str, Any] = {
            "enabled": True,
            "skipped": False,
            "commands": commands,
            "doc": {"planned": False},
            "drive": {"uploads": []},
            "base": {"planned": False},
        }

        doc_result = self.lark_client.create_doc(
            content=self._doc_xml(result, event=event, request_text=request_text),
            parent_token=options.doc_parent_token,
        )
        commands.append(doc_result.to_dict())
        doc_url = _extract_url(doc_result.stdout)
        archive["doc"] = {
            "planned": True,
            "returncode": doc_result.returncode,
            "url": doc_url,
            "dry_run": doc_result.dry_run,
        }

        uploaded_files: list[dict[str, Any]] = []
        if options.drive_folder_token:
            for path in self._report_files(result):
                upload_result = self.lark_client.upload_drive_file(
                    path=path,
                    folder_token=options.drive_folder_token,
                    name=f"{result.job_id}_{path.name}",
                )
                upload_item = {
                    "path": str(path),
                    "returncode": upload_result.returncode,
                    "dry_run": upload_result.dry_run,
                    "file_token": _extract_file_token(upload_result.stdout),
                }
                uploaded_files.append(upload_item)
                commands.append(upload_result.to_dict())
        archive["drive"] = {
            "uploads": uploaded_files,
            "folder_token": options.drive_folder_token,
        }

        if options.base_token and options.table_id:
            fields = self._base_fields(
                result,
                event=event,
                request_text=request_text,
                doc_url=doc_url,
                uploaded_files=uploaded_files,
            )
            base_result = self.lark_client.upsert_base_record(
                base_token=options.base_token,
                table_id=options.table_id,
                fields=fields,
            )
            commands.append(base_result.to_dict())
            archive["base"] = {
                "planned": True,
                "returncode": base_result.returncode,
                "dry_run": base_result.dry_run,
                "fields": fields,
                "record_id": _extract_record_id(base_result.stdout),
            }
        return archive

    def _doc_xml(self, result: TaskResult, *, event: LarkEvent, request_text: str) -> str:
        details = result.details or {}
        title = f"Lark Agent 分析归档 - {result.job_id}"
        report_url = str(details.get("published_report_url") or "")
        mode = str(details.get("mode") or "")
        provider = str(details.get("provider") or "")
        version = str(details.get("report_version") or "")
        rows = [
            ("任务ID", result.job_id or ""),
            ("分析类型", mode),
            ("状态", "成功" if result.success else "失败"),
            ("Agent", provider),
            ("报告版本", version),
            ("报告链接", report_url),
            ("群ID", event.chat_id),
            ("发起人", event.sender_id),
            ("归档时间", datetime.now(timezone.utc).isoformat()),
        ]
        table_rows = "".join(
            "<tr><td>{}</td><td>{}</td></tr>".format(_xml_text(key), _xml_text(value))
            for key, value in rows
            if str(value).strip()
        )
        report_block = f'<p><a type="url-preview" href="{html.escape(report_url, quote=True)}">打开 HTML 报告</a></p>' if report_url else ""
        return (
            f"<title>{_xml_text(title)}</title>"
            '<callout emoji="✅" background-color="light-green" border-color="green">'
            "<p>分析已完成，以下内容由 Lark Agent Bridge 自动归档。</p>"
            "</callout>"
            "<h1>结论摘要</h1>"
            f"<p>{_xml_text(result.message)}</p>"
            "<h1>请求上下文</h1>"
            f"<p>{_xml_text(request_text)}</p>"
            "<h1>元数据</h1>"
            f"<table><tbody>{table_rows}</tbody></table>"
            f"{report_block}"
        )

    def _report_files(self, result: TaskResult) -> list[Path]:
        candidates = [result.html_report, result.json_report]
        files: list[Path] = []
        seen: set[Path] = set()
        for candidate in candidates:
            if candidate is None:
                continue
            path = Path(candidate)
            if path in seen or not path.is_file():
                continue
            seen.add(path)
            files.append(path)
        return files

    def _base_fields(
        self,
        result: TaskResult,
        *,
        event: LarkEvent,
        request_text: str,
        doc_url: str,
        uploaded_files: list[dict[str, Any]],
    ) -> dict[str, object]:
        details = result.details or {}
        values: dict[str, object] = {
            "job_id": result.job_id or "",
            "mode": str(details.get("mode") or ""),
            "status": "成功" if result.success else "失败",
            "summary": _truncate(result.message, 4000),
            "report_url": str(details.get("published_report_url") or ""),
            "bug_url": str(details.get("bug_url") or ""),
            "provider": str(details.get("provider") or ""),
            "duration_seconds": result.duration_seconds or 0,
            "chat_id": event.chat_id,
            "sender_id": event.sender_id,
            "doc_url": doc_url,
            "report_version": details.get("report_version") or "",
            "request_text": _truncate(request_text, 2000),
            "drive_file_count": len(uploaded_files),
        }
        mapped: dict[str, object] = {}
        for key, field_name in self.config.workflow_archive.base_field_map.items():
            if not field_name:
                continue
            value = values.get(key)
            if value is None or value == "":
                continue
            mapped[field_name] = value
        return mapped


def _xml_text(value: object) -> str:
    return html.escape(str(value), quote=False).replace("\n", "<br/>")


def _truncate(value: str, limit: int) -> str:
    text = value or ""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _extract_url(stdout: str) -> str:
    payload = _json_dict(stdout)
    data = payload.get("data")
    if isinstance(data, dict):
        document = data.get("document")
        if isinstance(document, dict):
            return str(document.get("url") or "")
    return str(payload.get("url") or "")


def _extract_file_token(stdout: str) -> str:
    payload = _json_dict(stdout)
    data = payload.get("data")
    if isinstance(data, dict):
        return str(data.get("file_token") or data.get("token") or "")
    return str(payload.get("file_token") or payload.get("token") or "")


def _extract_record_id(stdout: str) -> str:
    payload = _json_dict(stdout)
    data = payload.get("data")
    if isinstance(data, dict):
        record = data.get("record")
        if isinstance(record, dict):
            return str(record.get("record_id") or "")
    record = payload.get("record")
    if isinstance(record, dict):
        return str(record.get("record_id") or "")
    return ""


def _json_dict(stdout: str) -> dict[str, Any]:
    if not stdout.strip():
        return {}
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
