"""消息中资源（文件/文件夹/时间范围）提取。"""

from __future__ import annotations

import re

from ..models import (
    DownloadResource,
)
from .patterns import (
    DATE_RANGE_RE,
    DRIVE_FOLDER_URL_RE,
    FILE_KEY_RE,
    FILE_XML_RE,
    FOLDER_XML_RE,
    HOUR_RANGE_RE,
    IMAGE_KEY_RE,
    URL_RE,
)
from .terms import (
    TRAILING_URL_PUNCTUATION,
)


def find_resources(text: str, *, source_message_id: str = "") -> list[DownloadResource]:
    resources: list[DownloadResource] = []
    for match in URL_RE.findall(text):
        value = match.rstrip(TRAILING_URL_PUNCTUATION)
        folder_match = DRIVE_FOLDER_URL_RE.match(value)
        if folder_match:
            resources.append(DownloadResource(kind="folder", value=folder_match.group("token"), source_message_id=source_message_id))
            continue
        resources.append(DownloadResource(kind="url", value=value, source_message_id=source_message_id))
    file_keys_from_tags: set[str] = set()
    for match in FILE_XML_RE.finditer(text):
        tag = match.group(0)
        key = _extract_xml_attr(tag, "key")
        if not key or not FILE_KEY_RE.fullmatch(key):
            continue
        file_keys_from_tags.add(key)
        resources.append(
            DownloadResource(
                kind="file",
                value=key,
                source_message_id=source_message_id,
                display_name=_extract_xml_attr(tag, "name"),
            )
        )
    for match in FILE_KEY_RE.findall(text):
        if match in file_keys_from_tags:
            continue
        resources.append(DownloadResource(kind="file", value=match, source_message_id=source_message_id))
    for match in IMAGE_KEY_RE.findall(text):
        resources.append(DownloadResource(kind="image", value=match, source_message_id=source_message_id))
    for match in FOLDER_XML_RE.finditer(text):
        resources.append(DownloadResource(kind="folder", value=match.group("token"), source_message_id=source_message_id))
    return resources


def _find_resources(text: str) -> list[DownloadResource]:
    return find_resources(text)


def _extract_xml_attr(tag: str, attr: str) -> str:
    match = re.search(rf"\b{re.escape(attr)}=\"([^\"]+)\"", tag, re.I)
    return match.group(1).strip() if match else ""


def _find_since(text: str) -> str | None:
    date_match = DATE_RANGE_RE.search(text)
    if date_match:
        return re.sub(r"\s*-\s*", "-", date_match.group(0))
    hour_match = HOUR_RANGE_RE.search(text)
    if hour_match:
        return f"{hour_match.group(1)}-{hour_match.group(2)}"
    return None
