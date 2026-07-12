from __future__ import annotations

import json
from pathlib import Path
import re
from typing import TYPE_CHECKING

from ._shared import *  # noqa: F401,F403

if TYPE_CHECKING:
    from .conversation_resolver import MessageFetchCache


class _ResourcesMixin:
    """日志资源收集、继承与本地授权（与 LogResourcesMixin 共享 self 状态）。"""

    def _resource_descriptors(self, resources: list[DownloadResource]) -> list[dict[str, str]]:
        return [
            {
                "kind": item.kind,
                "value": item.value,
                "source_message_id": item.source_message_id,
            }
            for item in resources
        ]
    def _log_resources_from_session(self, session: dict[str, object] | None) -> list[DownloadResource]:
        if not session:
            return []
        resources: list[DownloadResource] = []
        details = session.get("details")
        if not isinstance(details, dict):
            details = {}
        for key in ("prepared_log_input", "selected_log_input"):
            value = str(details.get(key) or "").strip()
            if not value:
                continue
            candidate = Path(value).expanduser()
            if candidate.exists():
                resources.append(DownloadResource(kind="local", value=str(candidate.resolve())))
        downloads = details.get("downloads")
        if isinstance(downloads, list):
            for item in downloads:
                if not isinstance(item, dict):
                    continue
                path = str(item.get("path") or "").strip()
                if path:
                    candidate = Path(path).expanduser()
                    if candidate.exists():
                        resources.append(DownloadResource(kind="local", value=str(candidate.resolve())))
                        continue
                kind = str(item.get("kind") or "").strip()
                value = str(item.get("value") or "").strip()
                if kind and value:
                    resources.append(DownloadResource(kind=kind, value=value))
        raw_resources = details.get("resources")
        if isinstance(raw_resources, list):
            for item in raw_resources:
                if not isinstance(item, dict):
                    continue
                kind = str(item.get("kind") or "").strip()
                value = str(item.get("value") or "").strip()
                source_message_id = str(item.get("source_message_id") or "").strip()
                if kind and value:
                    resources.append(DownloadResource(kind=kind, value=value, source_message_id=source_message_id))
        return self._merge_resources([], resources)
    def _log_resources_from_context(self, context) -> list[DownloadResource]:
        if context is None:
            return []
        return self._log_resources_from_session(self.activity_store.get_session(context.root_message_id))
    def _should_inherit_signal_resources(self, event: LarkEvent, route_content: str) -> bool:
        if event.reply_to or event.parent_id or event.root_id or event.thread_id:
            return True
        if self._is_followup_intent(route_content):
            return True
        lowered = route_content.casefold()
        return any(
            term in lowered
            for term in (
                "基于日志",
                "用日志",
                "看日志",
                "日志",
                "那就",
                "继续",
                "刚才",
                "上次",
                "上轮",
                "上一",
                "之前",
                "已有",
                "这个",
                "这些",
                "同样",
            )
        )
    def _fetch_resources_from_session_message(self, session: dict[str, object]) -> list[DownloadResource]:
        message_id = str(session.get("message_id") or session.get("session_id") or "").strip()
        if not message_id:
            return []
        synthetic_event = LarkEvent(
            event_id=str(session.get("event_id") or ""),
            message_id=message_id,
            chat_id=str(session.get("chat_id") or ""),
            chat_type=str(session.get("chat_type") or ""),
            sender_id=str(session.get("sender_id") or ""),
            message_type="text",
            content=str(session.get("content") or ""),
            reply_to=str(session.get("reply_to") or ""),
            parent_id=str(session.get("parent_id") or ""),
            root_id=str(session.get("root_id") or ""),
            thread_id=str(session.get("thread_id") or ""),
        )
        resources = self._fetch_referenced_message_resources(
            synthetic_event,
            route_content=synthetic_event.content,
            force_current_lookup=True,
        )
        fetched = self.lark_client.fetch_message(message_id)
        if fetched.returncode == 0:
            resources = self._merge_resources(
                resources,
                self._extract_resources_from_message_payload(fetched.stdout, fallback_message_id=message_id),
            )
        return resources
    def _reference_chain_log_resources(self, event: LarkEvent) -> list[DownloadResource]:
        reference_ids = self._fetch_followup_reference_ids(event)
        for message_id in self._followup_context_candidate_ids(event, reference_ids):
            resources = self._log_resources_from_reference_id(message_id)
            if resources:
                return resources
        return []
    def _log_resources_from_reference_id(self, message_id: str) -> list[DownloadResource]:
        normalized = message_id.strip()
        if not normalized:
            return []
        session = self.activity_store.get_session(normalized)
        resources = self._log_resources_from_session(session)
        if resources:
            return resources
        if session:
            resources = self._fetch_resources_from_session_message(session)
            if resources:
                return resources
        context = self.conversation_store.lookup(normalized)
        resources = self._log_resources_from_context(context)
        if resources:
            return resources
        fetched = self.lark_client.fetch_message(normalized)
        if fetched.returncode == 0:
            resources = self._extract_resources_from_message_payload(fetched.stdout, fallback_message_id=normalized)
            if resources:
                return resources
        return []
    def _contextual_signal_resources(
        self,
        event: LarkEvent,
        route_content: str,
        *,
        explicit_followup_context,
        latest_chat_context,
    ) -> list[DownloadResource]:
        resources = self._reference_chain_log_resources(event)
        if resources:
            return resources
        if not self._should_inherit_signal_resources(event, route_content):
            return []
        resources = self._log_resources_from_context(explicit_followup_context)
        if resources:
            return resources
        _ = latest_chat_context
        return []
    def _contextual_app_server_bug_url(self, *, explicit_followup_context, latest_chat_context) -> str:
        """续聊「自主分析」缺少 bug 链接时，从回复上下文或本群最近一次分析继承。"""
        for context in (explicit_followup_context, latest_chat_context):
            if context is None:
                continue
            bug_url = self._bug_url_from_request_text(getattr(context, "request_text", "") or "")
            if bug_url:
                return bug_url
        return ""
    def _authorized_local_download_resources(self, event: LarkEvent | None, route_content: str) -> list[DownloadResource]:
        options = self.config.local_resources
        if not options.enabled or event is None:
            return []
        if options.require_allowed_user and event.sender_id not in set(self.config.allowed_users):
            return []
        lowered_content = route_content.casefold()
        if not any(term.casefold() in lowered_content for term in LOCAL_DOWNLOAD_AUTH_TERMS):
            return []
        resources: list[DownloadResource] = []
        seen: set[Path] = set()
        for file_name in LOCAL_RESOURCE_NAME_RE.findall(route_content):
            safe_name = Path(file_name).name
            if safe_name != file_name:
                continue
            for base_dir in options.allowed_dirs:
                candidate = (Path(base_dir).expanduser() / safe_name).resolve()
                if candidate in seen:
                    continue
                if not self._is_path_under_allowed_local_dir(candidate, options.allowed_dirs):
                    continue
                if not candidate.is_file():
                    continue
                seen.add(candidate)
                resources.append(DownloadResource(kind="local", value=str(candidate)))
        return resources
    def _is_path_under_allowed_local_dir(self, path: Path, allowed_dirs: list[Path]) -> bool:
        try:
            resolved_path = path.expanduser().resolve()
        except OSError:
            return False
        for base_dir in allowed_dirs:
            try:
                resolved_base = Path(base_dir).expanduser().resolve()
            except OSError:
                continue
            if resolved_path == resolved_base or resolved_base in resolved_path.parents:
                return True
        return False
    def _merge_resources(
        self,
        primary: list[DownloadResource],
        extra: list[DownloadResource],
    ) -> list[DownloadResource]:
        merged: list[DownloadResource] = []
        seen: dict[tuple[str, str, str], int] = {}
        for item in [*primary, *extra]:
            key = (item.kind, item.value, item.source_message_id.strip())
            existing_index = seen.get(key)
            if existing_index is not None:
                if not merged[existing_index].display_name and item.display_name:
                    merged[existing_index] = item
                continue
            seen[key] = len(merged)
            merged.append(item)
        return merged
    def _extract_resource_display_name(self, value: dict[str, object]) -> str:
        for key in ("file_name", "fileName", "filename", "name"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return ""
    def _fetch_referenced_message_resources(
        self,
        event: LarkEvent,
        *,
        route_content: str,
        force_current_lookup: bool = False,
        message_cache: MessageFetchCache | None = None,
    ) -> list[DownloadResource]:
        resources: list[DownloadResource] = []
        candidate_ids = self._candidate_reference_message_ids(
            event,
            route_content=route_content,
            force_current_lookup=force_current_lookup,
            message_cache=message_cache,
        )
        for message_id in candidate_ids:
            if message_cache is not None:
                payload = message_cache.payload(message_id)
            else:
                fetched = self.lark_client.fetch_message(message_id)
                payload = fetched.stdout if fetched.returncode == 0 else None
            if payload is None:
                continue
            resources = self._merge_resources(
                resources,
                self._extract_resources_from_message_payload(payload, fallback_message_id=message_id),
            )
        if resources:
            return resources
        seen = set(candidate_ids)
        reference_ids = self._fetch_followup_reference_ids(event, message_cache=message_cache)
        for message_id in self._followup_context_candidate_ids(event, reference_ids):
            if message_id in seen:
                continue
            seen.add(message_id)
            if message_cache is not None:
                payload = message_cache.payload(message_id)
            else:
                fetched = self.lark_client.fetch_message(message_id)
                payload = fetched.stdout if fetched.returncode == 0 else None
            if payload is None:
                continue
            resources = self._merge_resources(
                resources,
                self._extract_resources_from_message_payload(payload, fallback_message_id=message_id),
            )
        return resources
    def _candidate_reference_message_ids(
        self,
        event: LarkEvent,
        *,
        route_content: str,
        force_current_lookup: bool = False,
        message_cache: MessageFetchCache | None = None,
    ) -> list[str]:
        candidates = [value for value in [event.reply_to, event.parent_id, event.root_id] if value]
        should_lookup_current = force_current_lookup or self._should_lookup_current_message_for_resources(event, route_content)
        if not candidates and should_lookup_current and event.message_id:
            if message_cache is not None:
                current_payload = message_cache.payload(event.message_id)
            else:
                fetched_current = self.lark_client.fetch_message(event.message_id)
                current_payload = fetched_current.stdout if fetched_current.returncode == 0 else None
            if current_payload is not None:
                candidates.extend(
                    candidate
                    for candidate in self._extract_message_reference_ids(current_payload)
                    if candidate and candidate != event.message_id
                )
        unique: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            value = candidate.strip()
            if not value or value in seen:
                continue
            seen.add(value)
            unique.append(value)
        return unique[:3]
    def _should_lookup_current_message_for_resources(self, event: LarkEvent, route_content: str) -> bool:
        if event.reply_to or event.parent_id or event.root_id:
            return True
        inline_app_server = parse_app_server_investigation_request(
            route_content,
            bug_url_re=self.bug_url_re,
            auto_terms=self.config.bug_analysis.app_server_investigation.auto_terms,
            free_terms=self.config.bug_analysis.app_server_investigation.free_terms,
        )
        if inline_app_server.triggered:
            return not inline_app_server.resources and not inline_app_server.bug_url
        inline_direct = parse_direct_analysis_request(route_content)
        if inline_direct.triggered:
            return not inline_direct.resources
        if self._looks_like_direct_analysis_prompt(route_content):
            return True
        inline_perception = parse_perception_summary_request(route_content)
        if inline_perception.triggered:
            return not inline_perception.resources
        inline_signal = parse_signal_request(
            route_content,
            signal_aliases=self.config.signal_aliases,
            command_prefixes=self.config.command_prefixes,
            signal_resolver=self.signal_resolver,
        )
        if inline_signal.triggered:
            return not inline_signal.resources
        return False
    def _looks_like_direct_analysis_prompt(self, route_content: str) -> bool:
        return looks_like_direct_analysis_prompt(route_content, resources_present=True, bug_url_re=self.bug_url_re)
    def _extract_resources_from_message_payload(self, payload_text: str, *, fallback_message_id: str = "") -> list[DownloadResource]:
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, dict):
            return []
        data = payload.get("data", {})
        if not isinstance(data, dict):
            return []
        messages = data.get("messages")
        if isinstance(messages, dict):
            messages = [messages]
        if not isinstance(messages, list):
            return []
        resources: list[DownloadResource] = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            message_id = str(message.get("message_id") or fallback_message_id).strip()
            message_type = str(message.get("msg_type") or message.get("message_type") or "").strip().casefold()
            extracted = self._extract_resources_from_message_value(
                message,
                source_message_id=message_id,
                allow_bare_resource_tokens=message_type in {"file", "folder", "image"},
            )
            resources = self._merge_resources(resources, extracted)
        return resources
    def _extract_resources_from_message_value(
        self,
        value: object,
        *,
        source_message_id: str,
        allow_bare_resource_tokens: bool = False,
    ) -> list[DownloadResource]:
        resources: list[DownloadResource] = []
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                parsed = None
            if parsed is not None and parsed is not value:
                return self._extract_resources_from_message_value(
                    parsed,
                    source_message_id=source_message_id,
                    allow_bare_resource_tokens=allow_bare_resource_tokens,
                )
            candidates = [
                item
                for item in find_resources(value, source_message_id=source_message_id)
                if item.kind in {"file", "folder", "image"}
            ]
            if allow_bare_resource_tokens:
                return candidates
            structured: list[DownloadResource] = [item for item in candidates if item.kind == "folder"]
            for match in re.finditer(r"<file\b[^>]*>", value, re.I):
                structured = self._merge_resources(
                    structured,
                    [
                        item
                        for item in find_resources(match.group(0), source_message_id=source_message_id)
                        if item.kind == "file"
                    ],
                )
            return structured
        if isinstance(value, dict):
            file_display_name = self._extract_resource_display_name(value)
            for key, nested in value.items():
                if key == "file_key" and isinstance(nested, str) and nested.strip():
                    resources = self._merge_resources(
                        resources,
                        [
                            DownloadResource(
                                kind="file",
                                value=nested.strip(),
                                source_message_id=source_message_id,
                                display_name=file_display_name,
                            )
                        ],
                    )
                    continue
                if key == "image_key" and isinstance(nested, str) and nested.strip():
                    resources = self._merge_resources(
                        resources,
                        [DownloadResource(kind="image", value=nested.strip(), source_message_id=source_message_id)],
                    )
                    continue
                if key in {"folder_token", "folderToken"} and isinstance(nested, str) and nested.strip():
                    resources = self._merge_resources(
                        resources,
                        [DownloadResource(kind="folder", value=nested.strip(), source_message_id=source_message_id)],
                    )
                    continue
                resources = self._merge_resources(
                    resources,
                    self._extract_resources_from_message_value(
                        nested,
                        source_message_id=source_message_id,
                        allow_bare_resource_tokens=allow_bare_resource_tokens and key == "content",
                    ),
                )
            return resources
        if isinstance(value, list):
            for item in value:
                resources = self._merge_resources(
                    resources,
                    self._extract_resources_from_message_value(
                        item,
                        source_message_id=source_message_id,
                        allow_bare_resource_tokens=allow_bare_resource_tokens,
                    ),
                )
        return resources
