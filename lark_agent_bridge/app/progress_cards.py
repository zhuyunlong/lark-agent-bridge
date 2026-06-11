from __future__ import annotations

from datetime import datetime, timezone
import html
from pathlib import Path
import re
import threading
from typing import Callable

from ._shared import *  # noqa: F401,F403


class _ProgressCardsMixin:
    """实时进度卡片的创建、节流与更新（与 HandleEventMixin 共享 self 状态）。"""

    def _event_progress_callback(self, event: LarkEvent, *, session_id: str | None = None) -> Callable[[dict[str, object]], None]:
        def _callback(progress: dict[str, object]) -> None:
            stage = str(progress.get("stage", "progress"))
            message = str(progress.get("message", ""))
            details = progress.get("details", {})
            if not isinstance(details, dict):
                details = {"value": details}
            details = dict(details)
            nested_session_id = details.pop("session_id", None)
            if nested_session_id and "provider_session_id" not in details:
                details["provider_session_id"] = nested_session_id
            for key in ("stage", "message", "event"):
                if key in details:
                    details[f"progress_{key}"] = details.pop(key)
            self._notify_progress(stage, message, event=event, session_id=session_id, **details)

        return _callback
    def send_status_card(
        self,
        event: LarkEvent,
        *,
        title: str,
        status: str,
        details: dict[str, str] | None = None,
        note: str | None = None,
        session_id: str | None = None,
    ) -> None:
        """Send a status progress card to the user during long operations."""
        if self.config.dry_run:
            return
        if event.chat_type not in {"group", "p2p"}:
            return
        self._prune_stale_progress_cards()
        key = self._progress_card_key(event, session_id=session_id)
        with self._progress_cards_lock:
            existing = self._progress_cards.get(key)
            has_existing = bool(existing and existing.get("message_id"))
            if has_existing:
                existing["title"] = title
                existing["status"] = status
                existing["details"] = {
                    **dict(existing.get("details") or {}),
                    **dict(details or {}),
                }
                existing["last_active_at"] = datetime.now(timezone.utc)
            else:
                self._progress_cards[key] = {
                    "title": title,
                    "status": status,
                    "details": dict(details or {}),
                    "started_at": datetime.now(timezone.utc),
                    "message_id": "",
                }
        if has_existing:
            self._update_progress_card(event, status=status, note=note, session_id=session_id)
            return
        card = self._build_progress_card(event, key=key, status=status, note=note)
        card_json_str = card_to_json(card)
        if event.message_id:
            send_result = self.lark_client.reply_card(event.message_id, card_json_str)
        else:
            send_result = self.lark_client.send_card_response(event, card_json_str)
        card_message_id = self._card_message_id_from_result(send_result)
        if card_message_id:
            with self._progress_cards_lock:
                if key in self._progress_cards:
                    self._progress_cards[key]["message_id"] = card_message_id
            self._remember_conversation_alias(card_message_id, key)
        else:
            with self._progress_cards_lock:
                self._progress_cards.pop(key, None)
            self._notify_progress(
                "status_card_send_failed",
                "进度卡发送失败，退回文字确认",
                event=event,
                title=title,
                status=status,
                stderr=(getattr(send_result, "stderr", "") or getattr(send_result, "stdout", ""))[:MAX_STDERR_PREVIEW],
            )
            fallback_text = f"已收到，{title}处理中。"
            if note:
                fallback_text = f"{fallback_text}\n{note}"
            if event.message_id:
                self.lark_client.reply(event.message_id, self._reply_payload(event, fallback_text))
            else:
                self.lark_client.send_response(event, fallback_text)
    def _progress_card_key(self, event: LarkEvent | None, *, session_id: str | None = None) -> str:
        if session_id:
            return session_id
        if event is None:
            return ""
        return event.message_id or event.event_id
    def _progress_card_state_key(self, event: LarkEvent, *, session_id: str | None = None) -> str:
        candidates = [
            self._progress_card_key(event, session_id=session_id),
            event.message_id,
            event.event_id,
        ]
        with self._progress_cards_lock:
            for candidate in candidates:
                if candidate and candidate in self._progress_cards:
                    return candidate
        return candidates[0] if candidates else ""
    def _has_progress_card(self, event: LarkEvent, *, session_id: str | None = None) -> bool:
        key = self._progress_card_state_key(event, session_id=session_id)
        with self._progress_cards_lock:
            card_state = self._progress_cards.get(key)
        return bool(card_state and card_state.get("message_id"))
    def _should_update_progress_card_from_progress(
        self,
        event: LarkEvent,
        *,
        stage: str,
        session_id: str | None = None,
    ) -> bool:
        key = self._progress_card_state_key(event, session_id=session_id)
        with self._progress_cards_lock:
            card_state = self._progress_cards.get(key)
            if not card_state or not card_state.get("message_id"):
                return False
            now = datetime.now(timezone.utc)
            card_state["last_active_at"] = now
            # Throttle only the known high-frequency streaming families (Codex
            # app-server deltas + agent summary stream), not every "*_stream" stage.
            if not stage.endswith(_THROTTLED_PROGRESS_STAGE_SUFFIXES):
                return True
            last_update = card_state.get("last_card_update_at")
            if isinstance(last_update, datetime):
                if (now - last_update).total_seconds() < self._progress_card_stream_update_interval_seconds:
                    return False
            return True
    def _update_progress_card(
        self,
        event: LarkEvent,
        *,
        status: str = "analyzing",
        session_id: str | None = None,
        result: TaskResult | None = None,
        note: str | None = None,
    ) -> bool:
        key = self._progress_card_state_key(event, session_id=session_id)
        with self._progress_cards_lock:
            card_state = self._progress_cards.get(key)
            message_id = str(card_state.get("message_id") or "") if card_state else ""
        if not card_state:
            return False
        if not message_id:
            return False
        try:
            card = self._build_progress_card(event, key=key, status=status, result=result, note=note)
            send_result = self.lark_client.update_card(message_id, card_to_json(card))
        except Exception as exc:
            logger.debug("failed to update progress card %s: %s", message_id, exc, exc_info=True)
            self._record_progress_card_update_failure(event, session_id=session_id, error=exc)
            return False
        if send_result.returncode == 0:
            now = datetime.now(timezone.utc)
            with self._progress_cards_lock:
                # Re-fetch under the lock: the entry may have been pruned/popped
                # by another thread during the (unlocked) network call above.
                # Writing the stale `card_state` reference would land on an
                # orphaned dict and silently miss the TTL refresh.
                current = self._progress_cards.get(key)
                if current is not None:
                    current["last_card_update_at"] = now
                    current["last_active_at"] = now
        return send_result.returncode == 0
