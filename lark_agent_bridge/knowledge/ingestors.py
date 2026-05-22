"""Knowledge source ingestion helpers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any
from urllib.parse import parse_qs, urlsplit

from ..models import BridgeConfig, KnowledgeSourceOptions
from .models import KnowledgeChunk
from .template_data import load_simulation_template_chunks, load_source_specs


_OBSOLETE_DATACENTER_INTENT_MOCK_ACTION = "com.xiaopeng.intent.action.mock.datacenter"
_SIGNAL_TOKEN_ALIASES = {
    "show": ["展示", "显示"],
    "debug": ["调试"],
    "info": ["信息"],
}


class KnowledgeIngestError(RuntimeError):
    pass


def ingest_source(config: BridgeConfig, source: KnowledgeSourceOptions) -> list[KnowledgeChunk]:
    source_type = source.type.strip()
    if source_type == "local_json":
        return _ingest_local_json(source)
    if source_type == "guideengine_signal":
        return _ingest_guideengine_signal(config, source)
    if source_type == "feishu_doc":
        return _ingest_feishu_doc(source)
    if source_type == "feishu_base":
        return _ingest_feishu_base(source)
    raise KnowledgeIngestError(f"unsupported knowledge source type: {source_type}")


def source_title(source: KnowledgeSourceOptions) -> str:
    if source.title.strip():
        return source.title.strip()
    if source.type == "local_json" and source.path:
        return Path(source.path).name
    if source.type == "guideengine_signal":
        return "guideengine signal sources"
    if source.url:
        return source.url
    return source.id


def source_ref(source: KnowledgeSourceOptions) -> str:
    return source.path or source.url or source.id


def _ingest_local_json(source: KnowledgeSourceOptions) -> list[KnowledgeChunk]:
    path = Path(source.path).expanduser()
    if not path.is_file():
        raise KnowledgeIngestError(f"local json not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise KnowledgeIngestError(f"invalid json: {path}") from exc
    commands = payload.get("commands") if isinstance(payload, dict) else None
    if not isinstance(commands, list):
        raise KnowledgeIngestError(f"local json has no commands list: {path}")
    chunks: list[KnowledgeChunk] = []
    for index, item in enumerate(commands):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        command = str(item.get("command") or "").strip()
        if not name and not command:
            continue
        group = str(item.get("group") or "").strip()
        description = str(item.get("description") or "").strip()
        content = "\n".join(
            line
            for line in [
                f"名称: {name}",
                f"分组: {group}" if group else "",
                f"命令: {command}" if command else "",
                f"说明: {description}" if description else "",
            ]
            if line
        )
        if _is_obsolete_datacenter_intent_mock(content):
            continue
        chunks.append(
            KnowledgeChunk(
                id=_chunk_id(source.id, index, content),
                source_id=source.id,
                title=name or command[:80],
                content=content,
                source_ref=str(path),
                kind="adb_command",
                metadata={"group": group, "command": command, "keywords": [name, group, description]},
            )
        )
    return chunks


def _ingest_guideengine_signal(config: BridgeConfig, source: KnowledgeSourceOptions) -> list[KnowledgeChunk]:
    repo = config.guideengine_repo
    chunks: list[KnowledgeChunk] = []
    for index, spec in enumerate(load_source_specs()):
        title = str(spec.get("title") or "").strip()
        raw_path = str(spec.get("path") or "").strip()
        raw_keywords = spec.get("keywords") or []
        keywords = [str(keyword) for keyword in raw_keywords if str(keyword).strip()] if isinstance(raw_keywords, list) else []
        if not title or not raw_path or not keywords:
            continue
        path = repo / raw_path
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        content = _focused_signal_excerpt(text, keywords)
        chunks.append(
            KnowledgeChunk(
                id=_chunk_id(source.id, index, str(path) + content),
                source_id=source.id,
                title=title,
                content=content,
                source_ref=str(path),
                kind="guideengine_source",
                metadata={"keywords": keywords},
            )
        )
    chunks.extend(_ingest_signal_proto_entries(config, source, start_index=len(chunks)))
    chunks.extend(load_simulation_template_chunks(source.id))
    return chunks


def _ingest_signal_proto_entries(
    config: BridgeConfig,
    source: KnowledgeSourceOptions,
    *,
    start_index: int,
) -> list[KnowledgeChunk]:
    chunks: list[KnowledgeChunk] = []
    for path in _signal_proto_paths(config, source):
        text = path.read_text(encoding="utf-8", errors="replace")
        for entry in _parse_signal_proto_entries(text):
            content = "\n".join(
                line
                for line in [
                    f"signal: {entry['name']}",
                    f"code: {entry['code']}",
                    f"comment: {entry['comment']}" if entry["comment"] else "",
                    f"line: {entry['line']}",
                    f"tokens: {' '.join(_signal_name_terms(entry['name']))}",
                ]
                if line
            )
            index = start_index + len(chunks)
            chunks.append(
                KnowledgeChunk(
                    id=_chunk_id(source.id, index, str(path) + content),
                    source_id=source.id,
                    title=f"{entry['name']} ({entry['code']})",
                    content=content,
                    source_ref=str(path),
                    kind="signal_proto_entry",
                    metadata={
                        "signal": entry["name"],
                        "code": entry["code"],
                        "line": entry["line"],
                        "keywords": [entry["name"], entry["code"], entry["comment"], *_signal_name_terms(entry["name"])],
                    },
                )
            )
    return chunks


def _signal_proto_paths(config: BridgeConfig, source: KnowledgeSourceOptions) -> list[Path]:
    candidates: list[Path] = []
    if source.path:
        candidates.append(Path(source.path).expanduser())
    repo = config.guideengine_repo
    candidates.extend(
        [
            repo / "module_floorcenter/module_proto/src/main/proto/signal.proto",
            repo / "module_foundation/module_proto/src/main/proto/signal.proto",
        ]
    )
    paths: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        path = candidate.resolve()
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        paths.append(path)
    return paths


def _parse_signal_proto_entries(text: str) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    section_comment = ""
    leading_comments: list[str] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        comment_line = re.match(r"\s*//\s*(.*)", line)
        if comment_line:
            comment = comment_line.group(1).strip()
            if comment:
                if "===" in comment or re.search(r"\d+\s*-\s*\d+", comment):
                    section_comment = comment
                    leading_comments = []
                else:
                    leading_comments.append(comment)
            continue
        match = re.match(r"\s*(SIGNAL_[A-Za-z0-9_]+)\s*=\s*(\d+)\s*;\s*(?://\s*(.*))?", line)
        if not match:
            if line.strip():
                leading_comments = []
            continue
        comments = [section_comment, *leading_comments, (match.group(3) or "").strip()]
        comment = " | ".join(item for item in comments if item)
        leading_comments = []
        if _is_obsolete_datacenter_intent_mock(comment):
            continue
        entries.append(
            {
                "name": match.group(1).strip(),
                "code": match.group(2).strip(),
                "comment": comment,
                "line": str(line_number),
            }
        )
    return entries


def _signal_name_terms(signal_name: str) -> list[str]:
    terms: list[str] = []
    for item in re.split(r"_+", signal_name.casefold()):
        cleaned = item.strip()
        _append_unique(terms, cleaned)
        for alias in _SIGNAL_TOKEN_ALIASES.get(cleaned, []):
            _append_unique(terms, alias)
    return terms


def _append_unique(values: list[str], value: str) -> None:
    cleaned = value.strip()
    if cleaned and cleaned not in values:
        values.append(cleaned)


def _focused_signal_excerpt(text: str, keywords: list[str], *, radius: int = 18) -> str:
    lines = [line for line in text.splitlines() if not _is_obsolete_datacenter_intent_mock(line)]
    matched: set[int] = set()
    lowered_keywords = [keyword.casefold() for keyword in keywords]
    for index, line in enumerate(lines):
        lowered = line.casefold()
        if any(keyword.casefold() in lowered for keyword in lowered_keywords):
            start = max(0, index - radius)
            end = min(len(lines), index + radius + 1)
            matched.update(range(start, end))
    if not matched:
        return "\n".join(lines[:80])
    return "\n".join(lines[index] for index in sorted(matched))[:8000]


def _ingest_feishu_doc(source: KnowledgeSourceOptions) -> list[KnowledgeChunk]:
    if not source.url:
        raise KnowledgeIngestError("feishu_doc source requires url")
    payload = _run_lark_json(
        [
            "lark-cli",
            "docs",
            "+fetch",
            "--api-version",
            "v2",
            "--as",
            "user",
            "--doc",
            source.url,
            "--format",
            "json",
        ]
    )
    title = str(payload.get("title") or source.title or source.url)
    markdown = str(payload.get("markdown") or payload.get("content") or "")
    if not markdown.strip():
        markdown = json.dumps(payload, ensure_ascii=False, indent=2)
    return _text_chunks(source.id, title, markdown, source.url, kind="feishu_doc")


def _ingest_feishu_base(source: KnowledgeSourceOptions) -> list[KnowledgeChunk]:
    if not source.url:
        raise KnowledgeIngestError("feishu_base source requires url")
    table_id = source.table_id or _url_query(source.url, "table")
    view_id = source.view_id or _url_query(source.url, "view")
    if not table_id:
        raise KnowledgeIngestError("feishu_base source requires table_id or table= query")
    base_token = _resolve_feishu_base_token(source.url)
    chunks: list[KnowledgeChunk] = []
    limit = 200
    offset = 0
    while True:
        command = [
            "lark-cli",
            "base",
            "+record-list",
            "--as",
            "user",
            "--base-token",
            base_token,
            "--table-id",
            table_id,
            "--limit",
            str(limit),
            "--offset",
            str(offset),
            "--format",
            "json",
        ]
        if view_id:
            command.extend(["--view-id", view_id])
        payload = _run_lark_json(command)
        records, has_more = _extract_feishu_base_records(payload)
        for record in records:
            index = len(chunks)
            text = json.dumps(record, ensure_ascii=False, indent=2)
            if _is_obsolete_datacenter_intent_mock(text):
                continue
            title = _record_title(record, fallback=f"{source.id} record {index + 1}")
            chunks.append(
                KnowledgeChunk(
                    id=_chunk_id(source.id, index, text),
                    source_id=source.id,
                    title=title,
                    content=text,
                    source_ref=source.url,
                    kind="feishu_base_record",
                    metadata={"table_id": table_id, "view_id": view_id},
                )
            )
        if not has_more or not records:
            break
        offset += len(records)
    return chunks


def _extract_feishu_base_records(payload: dict[str, Any]) -> tuple[list[Any], bool]:
    direct_records = payload.get("items") or payload.get("records")
    if isinstance(direct_records, list):
        return direct_records, bool(payload.get("has_more"))
    data = payload.get("data")
    if isinstance(data, list):
        return data, bool(payload.get("has_more"))
    if not isinstance(data, dict):
        return [], False
    nested_records = data.get("items") or data.get("records")
    has_more = bool(data.get("has_more") or payload.get("has_more"))
    if isinstance(nested_records, list):
        return nested_records, has_more
    table_rows = data.get("data")
    field_names = data.get("fields")
    if not isinstance(table_rows, list) or not isinstance(field_names, list):
        return [], has_more
    record_ids = data.get("record_id_list")
    records: list[Any] = []
    fields = [str(field) for field in field_names]
    for index, row in enumerate(table_rows):
        if isinstance(row, dict):
            records.append(row)
            continue
        if not isinstance(row, list):
            continue
        record: dict[str, Any] = {"fields": {field: row[pos] for pos, field in enumerate(fields) if pos < len(row)}}
        if isinstance(record_ids, list) and index < len(record_ids):
            record["record_id"] = record_ids[index]
        records.append(record)
    return records, has_more


def _resolve_feishu_base_token(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise KnowledgeIngestError("feishu_base source requires url")
    parsed = urlsplit(cleaned)
    if parsed.scheme and parsed.netloc:
        segments = [segment for segment in parsed.path.split("/") if segment]
        for marker in ("base", "bitable"):
            if marker in segments:
                index = segments.index(marker)
                if index + 1 < len(segments):
                    return segments[index + 1]
        if "wiki" in segments:
            payload = _run_lark_json(
                [
                    "lark-cli",
                    "wiki",
                    "+node-get",
                    "--as",
                    "user",
                    "--token",
                    cleaned,
                    "--format",
                    "json",
                ]
            )
            return _extract_bitable_token_from_wiki_node(payload)
    return cleaned


def _extract_bitable_token_from_wiki_node(payload: dict[str, Any]) -> str:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise KnowledgeIngestError("wiki node response has no data")
    candidates = [data]
    node = data.get("node")
    if isinstance(node, dict):
        candidates.insert(0, node)
    for candidate in candidates:
        obj_type = str(candidate.get("obj_type") or candidate.get("object_type") or "").strip()
        obj_token = str(candidate.get("obj_token") or candidate.get("object_token") or "").strip()
        if obj_token and (not obj_type or obj_type == "bitable"):
            return obj_token
    raise KnowledgeIngestError("wiki node is not a bitable or has no obj_token")


def _text_chunks(source_id: str, title: str, text: str, source_ref: str, *, kind: str) -> list[KnowledgeChunk]:
    paragraphs = [
        item.strip()
        for item in re.split(r"\n{2,}", text)
        if item.strip() and not _is_obsolete_datacenter_intent_mock(item)
    ]
    if not paragraphs:
        return []
    chunks: list[KnowledgeChunk] = []
    current: list[str] = []
    current_len = 0
    for paragraph in paragraphs:
        if current and current_len + len(paragraph) > 1800:
            content = "\n\n".join(current)
            chunks.append(
                KnowledgeChunk(
                    id=_chunk_id(source_id, len(chunks), content),
                    source_id=source_id,
                    title=title if not chunks else f"{title} #{len(chunks) + 1}",
                    content=content,
                    source_ref=source_ref,
                    kind=kind,
                )
            )
            current = []
            current_len = 0
        current.append(paragraph)
        current_len += len(paragraph)
    if current:
        content = "\n\n".join(current)
        chunks.append(
            KnowledgeChunk(
                id=_chunk_id(source_id, len(chunks), content),
                source_id=source_id,
                title=title if not chunks else f"{title} #{len(chunks) + 1}",
                content=content,
                source_ref=source_ref,
                kind=kind,
            )
        )
    return chunks


def _run_lark_json(command: list[str]) -> dict[str, Any]:
    if shutil.which(command[0]) is None:
        raise KnowledgeIngestError(f"{command[0]} not found")
    completed = subprocess.run(command, text=True, capture_output=True, timeout=120, check=False)
    if completed.returncode != 0:
        raise KnowledgeIngestError((completed.stderr or completed.stdout or "lark-cli failed")[:1000])
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise KnowledgeIngestError("lark-cli returned non-json output") from exc
    if not isinstance(payload, dict):
        raise KnowledgeIngestError("lark-cli json output is not an object")
    return payload


def _record_title(record: Any, *, fallback: str) -> str:
    if isinstance(record, dict):
        fields = record.get("fields")
        if isinstance(fields, dict):
            for key in ("标题", "名称", "命令名称", "问题", "问题标题", "name", "title"):
                value = fields.get(key)
                if value:
                    return str(value)[:120]
        for key in ("record_id", "id"):
            if record.get(key):
                return str(record[key])
    return fallback


def _url_query(url: str, name: str) -> str:
    values = parse_qs(urlsplit(url).query).get(name)
    return values[0] if values else ""


def _is_obsolete_datacenter_intent_mock(text: str) -> bool:
    return _OBSOLETE_DATACENTER_INTENT_MOCK_ACTION in text


def _chunk_id(source_id: str, index: int, content: str) -> str:
    digest = hashlib.sha1(content.encode("utf-8", errors="ignore")).hexdigest()[:16]
    return f"{source_id}:{index}:{digest}"
