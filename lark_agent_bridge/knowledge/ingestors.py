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
    specs = [
        (
            "DataCenterBroadcastReceiver 通用信号模拟入口",
            repo
            / "module_floorcenter/module_datacenter/src/main/java/com/xiaopeng/guideengine/debug/broadcast/DataCenterBroadcastReceiver.java",
            ["ACTION_MOCK", "mock.datacenter", "code", "format", "value", "adb", "信号模拟"],
        ),
        (
            "DataCenterBroadcastReceiver mockXTheme 主题模拟",
            repo
            / "module_floorcenter/module_datacenter/src/main/java/com/xiaopeng/guideengine/debug/broadcast/DataCenterBroadcastReceiver.java",
            ["SIGNAL_SR_XTHEME", "SIGNAL_SR_XTHEME_VALUE", "mockXTheme", "TimePeriod", "ThemeMode", "XTheme"],
        ),
        (
            "DataCenterBroadcastReceiver PB/ByteArray 自定义模拟",
            repo
            / "module_floorcenter/module_datacenter/src/main/java/com/xiaopeng/guideengine/debug/broadcast/DataCenterBroadcastReceiver.java",
            [
                "mockDataFactory",
                "mockRoadMarkData",
                "mockConstructionData",
                "mockObstacleData",
                "toByteArray",
                "ByteArray",
                "makeValue",
            ],
        ),
        (
            "ReplayReceiver/ProtocolFile record 回放入口",
            repo / "module_floorcenter/module_datacenter/src/main/java/com/xiaopeng/guideengine/debug/broadcast/ReplayReceiver.kt",
            ["REPLAY_FILE", "REPLAY_FATH", "START_RECORD", "STOP_RECORD", "record", "replay"],
        ),
        (
            "ProtocolFile ByteArray 回放调度",
            repo / "module_floorcenter/module_datacenter/src/main/java/com/xiaopeng/guideengine/debug/protolFile/ProtocolFile.java",
            ["ByteArray", "convertBytesToMsg", "mockSignal", "XDATA_ANDROID_REPLAY", "record", "Replay"],
        ),
        (
            "SrOtaService OtaCampaign/OtaUpgrade 枚举",
            repo / "module_core/subreality_biz/src/main/java/com/xiaopeng/ainavi/subreality_biz/ota/SrOtaService.kt",
            ["SIGNAL_OTA_ST", "OtaCampaign", "OtaUpgrade", "OTA_CAMPAIGN_SHOW", "OTA_UPGRADE_AFTER_VIDEO"],
        ),
        (
            "signal.proto SIGNAL_OTA_ST 定义",
            repo / "module_floorcenter/module_proto/src/main/proto/signal.proto",
            ["SIGNAL_OTA_ST", "105003", "OTA状态"],
        ),
        (
            "signal.proto 主题相关信号定义",
            repo / "module_floorcenter/module_proto/src/main/proto/signal.proto",
            [
                "SIGNAL_SR_XTHEME",
                "SIGNAL_SR_XTHEME_MSG",
                "SIGNAL_CLOUD_HIDE_SR_THEME",
                "SIGNAL_CLOUD_HIDE_CAR_MODEL_THEME",
                "JavaObject",
                "ByteArray",
                "ProtoObject",
                "themeMode",
                "timePeriod",
                "主题",
                "SIGNAL_SR_PROPERTY",
            ],
        ),
    ]
    chunks: list[KnowledgeChunk] = []
    for index, (title, path, keywords) in enumerate(specs):
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
    chunks.extend([_ota_template_chunk(source.id), _xtheme_template_chunk(source.id)])
    return chunks


def _focused_signal_excerpt(text: str, keywords: list[str], *, radius: int = 18) -> str:
    lines = text.splitlines()
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


def _ota_template_chunk(source_id: str) -> KnowledgeChunk:
    content = (
        "SIGNAL_OTA_ST ADB 模拟模板\n"
        "code: 105003\n"
        "format: 7 String 类型\n"
        "value: [campaign, upgrade]\n"
        "campaign: 0 OTA_CAMPAIGN_NONE, 1 OTA_CAMPAIGN_SHOW\n"
        "upgrade: 0 OTA_UPGRADE_NONE, 1 OTA_UPGRADE_SHOW, 2 OTA_UPGRADE_AFTER_VIDEO\n"
        "action: com.xiaopeng.guide.action.mock.datacenter"
    )
    return KnowledgeChunk(
        id=_chunk_id(source_id, 10000, content),
        source_id=source_id,
        title="SIGNAL_OTA_ST ADB 四种常见组合",
        content=content,
        source_ref="generated:guideengine_signal_template",
        kind="adb_signal_template",
        metadata={
            "signal": "SIGNAL_OTA_ST",
            "code": 105003,
            "format": 7,
            "keywords": ["SIGNAL_OTA_ST", "105003", "OTA", "adb", "模拟", "指令"],
        },
    )


def _xtheme_template_chunk(source_id: str) -> KnowledgeChunk:
    content = (
        "SIGNAL_SR_XTHEME ADB 模拟模板\n"
        "code: 105004\n"
        "format: 18 JavaObject 类型\n"
        "value: timePeriod,themeMode\n"
        "timePeriod: 0 早晨, 1 白天, 2 傍晚, 3 夜晚\n"
        "themeMode: 0 白天模式, 1 黑夜模式\n"
        "action: com.xiaopeng.guide.action.mock.datacenter\n"
        "相关主题候选: SIGNAL_SR_XTHEME_MSG, SIGNAL_CLOUD_HIDE_SR_THEME, SIGNAL_CLOUD_HIDE_CAR_MODEL_THEME"
    )
    return KnowledgeChunk(
        id=_chunk_id(source_id, 10001, content),
        source_id=source_id,
        title="SIGNAL_SR_XTHEME 主题 ADB 常见组合",
        content=content,
        source_ref="generated:guideengine_signal_template",
        kind="adb_signal_template",
        metadata={
            "signal": "SIGNAL_SR_XTHEME",
            "code": 105004,
            "format": 18,
            "keywords": ["SIGNAL_SR_XTHEME", "105004", "主题", "XTheme", "adb", "模拟", "指令"],
        },
    )


def _ingest_feishu_doc(source: KnowledgeSourceOptions) -> list[KnowledgeChunk]:
    if not source.url:
        raise KnowledgeIngestError("feishu_doc source requires url")
    payload = _run_lark_json(["lark-cli", "docs", "+fetch", "--api-version", "v2", "--doc", source.url])
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
    command = ["lark-cli", "base", "+record-list", "--base-token", source.url, "--table-id", table_id]
    if view_id:
        command.extend(["--view-id", view_id])
    payload = _run_lark_json(command)
    records = payload.get("items") or payload.get("records") or payload.get("data") or []
    if isinstance(records, dict):
        records = records.get("items") or records.get("records") or []
    if not isinstance(records, list):
        records = []
    chunks: list[KnowledgeChunk] = []
    for index, record in enumerate(records):
        text = json.dumps(record, ensure_ascii=False, indent=2)
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
    return chunks


def _text_chunks(source_id: str, title: str, text: str, source_ref: str, *, kind: str) -> list[KnowledgeChunk]:
    paragraphs = [item.strip() for item in re.split(r"\n{2,}", text) if item.strip()]
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
            for key in ("标题", "名称", "问题", "name", "title"):
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


def _chunk_id(source_id: str, index: int, content: str) -> str:
    digest = hashlib.sha1(content.encode("utf-8", errors="ignore")).hexdigest()[:16]
    return f"{source_id}:{index}:{digest}"
