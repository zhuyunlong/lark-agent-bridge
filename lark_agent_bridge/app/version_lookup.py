from __future__ import annotations

import json
from pathlib import Path
import re

from ._shared import *  # noqa: F401,F403


class _VersionLookupMixin:
    """ROM/导航版本回溯查询（与 LogResourcesMixin 共享 self 状态）。"""

    def _followup_rom_lookup_request(self, ctx: _RouteContext) -> RomVersionLookupRequest | None:
        followup_context = ctx.followup_context
        if followup_context is None or str(getattr(followup_context, "mode", "") or "") != "rom_version_lookup":
            return None
        action = ctx.conversation_input.followup_action
        if action != "retry":
            return None
        previous_request = parse_rom_version_lookup_request(str(getattr(followup_context, "request_text", "") or ""))
        rom_version = previous_request.rom_version or self._recent_rom_version_for_chat(ctx.event)
        if not rom_version:
            return None
        return RomVersionLookupRequest(
            rom_version=rom_version,
            prompt=ctx.route_content.strip(),
            raw_text=ctx.route_content,
            triggered=True,
        )
    def _recent_rom_version_for_chat(self, event: LarkEvent) -> str:
        session = self._recent_rom_lookup_session_for_chat(event)
        if not session:
            return ""
        candidate = parse_rom_version_lookup_request(str(session.get("content") or ""))
        return candidate.rom_version
    def _recent_navigation_version_for_chat(self, event: LarkEvent, rom_version: str = "") -> str:
        return self._navigation_version_from_required_outputs(self._recent_required_outputs_for_chat(event, rom_version=rom_version))
    def _recent_required_outputs_for_chat(self, event: LarkEvent, rom_version: str = "") -> dict[str, object]:
        session = self._recent_rom_lookup_session_for_chat(event, rom_version=rom_version)
        if not session:
            return {}
        details = session.get("details")
        if not isinstance(details, dict):
            return {}
        required = details.get("required_outputs")
        if not isinstance(required, dict):
            return {}
        return required
    def _navigation_version_from_lookup_result(self, result: TaskResult) -> str:
        return self._navigation_version_from_required_outputs(self._required_outputs_from_lookup_result(result))
    def _required_outputs_from_lookup_result(self, result: TaskResult) -> dict[str, object]:
        if not result.success or not isinstance(result.details, dict):
            return {}
        required = result.details.get("required_outputs")
        if not isinstance(required, dict):
            return {}
        return required
    def _navigation_version_from_required_outputs(self, required: dict[str, object]) -> str:
        navigation_version = str(required.get("navigation_version") or "").strip()
        if self._looks_like_apk_version(navigation_version):
            return navigation_version
        symbol_url = str(required.get("symbol_table_url") or "").strip()
        match = re.search(r"/(V\d+\.\d+\.\d+(?:\.\d+)?_\d{14}(?:\.\d+)?_[A-Za-z0-9]+)/?$", symbol_url)
        return match.group(1) if match else ""
    def _recent_rom_lookup_session_for_chat(self, event: LarkEvent, rom_version: str = "") -> dict[str, object] | None:
        chat_id = event.chat_id.strip()
        if not chat_id:
            return None
        for session in self.activity_store.list_sessions(limit=30, include_hidden=True):
            if str(session.get("chat_id") or "") != chat_id:
                continue
            if str(session.get("session_id") or "") == event.message_id:
                continue
            if str(session.get("mode") or "") != "rom_version_lookup":
                continue
            if str(session.get("status") or "") != "succeeded":
                continue
            candidate = parse_rom_version_lookup_request(str(session.get("content") or ""))
            if candidate.rom_version:
                if rom_version and candidate.rom_version != rom_version:
                    continue
                session_id = str(session.get("session_id") or "").strip()
                return self.activity_store.get_session(session_id) or session
        return None
    def _looks_like_apk_version(self, value: str) -> bool:
        return re.match(r"^V\d+\.\d+\.\d+(?:\.\d+)?_\d{14}(?:\.\d+)?_[A-Za-z0-9]+$", value) is not None
