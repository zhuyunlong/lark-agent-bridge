"""Persistent event de-duplication state."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
from typing import Any

from .models import LarkEvent, TaskResult


class EventStateStore:
    def __init__(self, state_file: str | Path) -> None:
        self.state_file = Path(state_file)
        self._seen = self._load_seen()

    def has_seen(self, event_id: str) -> bool:
        return bool(event_id) and event_id in self._seen

    def mark_seen(self, event: LarkEvent) -> bool:
        if not event.event_id:
            return True
        if event.event_id in self._seen:
            return False
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "event_id": event.event_id,
            "message_id": event.message_id,
            "sender_id": event.sender_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        with self.state_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._seen.add(event.event_id)
        return True

    def _load_seen(self) -> set[str]:
        if not self.state_file.exists():
            return set()
        seen: set[str] = set()
        with self.state_file.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                event_id = record.get("event_id")
                if isinstance(event_id, str):
                    seen.add(event_id)
        return seen


@dataclass(slots=True)
class ConversationContext:
    root_message_id: str
    chat_id: str
    mode: str
    request_text: str
    summary_text: str
    report_url: str
    report_excerpt: str
    history: list[dict[str, str]] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""


class ConversationContextStore:
    def __init__(self, state_file: str | Path, *, max_history_turns: int = 6) -> None:
        self.state_file = Path(state_file)
        self.max_history_turns = max(0, int(max_history_turns))
        self._contexts = self._load_contexts()

    def find(self, event: LarkEvent) -> ConversationContext | None:
        for key in self._candidate_keys(event):
            context = self.lookup(key)
            if context is not None:
                return context
        return None

    def lookup(self, key: str) -> ConversationContext | None:
        return self._contexts.get(key.strip()) if key and key.strip() else None

    def remember(
        self,
        *,
        root_message_id: str,
        chat_id: str,
        mode: str,
        request_text: str,
        summary_text: str,
        report_url: str,
        report_excerpt: str,
    ) -> ConversationContext | None:
        key = root_message_id.strip()
        if not key:
            return None
        now = datetime.now(timezone.utc).isoformat()
        previous = self._contexts.get(key)
        context = ConversationContext(
            root_message_id=key,
            chat_id=chat_id,
            mode=mode,
            request_text=request_text,
            summary_text=summary_text,
            report_url=report_url,
            report_excerpt=report_excerpt,
            history=list(previous.history) if previous is not None else [],
            created_at=previous.created_at if previous is not None and previous.created_at else now,
            updated_at=now,
        )
        self._contexts[key] = context
        self._save()
        return context

    def append_exchange(self, root_message_id: str, *, user_text: str, assistant_text: str) -> None:
        context = self._contexts.get(root_message_id)
        if context is None:
            return
        now = datetime.now(timezone.utc).isoformat()
        if user_text.strip():
            context.history.append({"role": "user", "content": user_text.strip()})
        if assistant_text.strip():
            context.history.append({"role": "assistant", "content": assistant_text.strip()})
        max_entries = self.max_history_turns * 2
        if max_entries > 0:
            context.history = context.history[-max_entries:]
        else:
            context.history = []
        context.updated_at = now
        self._save()

    def clear(self) -> int:
        count = len(self._contexts)
        self._contexts = {}
        try:
            self.state_file.unlink()
        except FileNotFoundError:
            pass
        return count

    def prune_expired(self, *, max_age_hours: int, now: datetime | None = None) -> int:
        if max_age_hours <= 0:
            return 0
        reference_time = now or datetime.now(timezone.utc)
        removed = 0
        cutoff_seconds = max_age_hours * 3600
        for key, context in list(self._contexts.items()):
            updated_at = _parse_timestamp(context.updated_at)
            if updated_at is None:
                continue
            age_seconds = reference_time.timestamp() - updated_at.timestamp()
            if age_seconds <= cutoff_seconds:
                continue
            self._contexts.pop(key, None)
            removed += 1
        if removed:
            self._save()
        return removed

    def latest_for_chat(self, chat_id: str, *, modes: set[str] | None = None) -> ConversationContext | None:
        chat = chat_id.strip()
        if not chat:
            return None
        candidates = [
            context
            for context in self._contexts.values()
            if context.chat_id == chat and (modes is None or context.mode in modes)
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda item: item.updated_at or item.created_at, reverse=True)
        return candidates[0]

    def _candidate_keys(self, event: LarkEvent) -> list[str]:
        values = [event.reply_to, event.root_id, event.parent_id, event.message_id]
        return [value for value in values if value]

    def _load_contexts(self) -> dict[str, ConversationContext]:
        if not self.state_file.exists():
            return {}
        try:
            payload = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        contexts: dict[str, ConversationContext] = {}
        for key, value in payload.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                continue
            contexts[key] = ConversationContext(
                root_message_id=key,
                chat_id=str(value.get("chat_id", "")),
                mode=str(value.get("mode", "")),
                request_text=str(value.get("request_text", "")),
                summary_text=str(value.get("summary_text", "")),
                report_url=str(value.get("report_url", "")),
                report_excerpt=str(value.get("report_excerpt", "")),
                history=_coerce_history(value.get("history")),
                created_at=str(value.get("created_at", "")),
                updated_at=str(value.get("updated_at", "")),
            )
        return contexts

    def _save(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            key: {
                "chat_id": context.chat_id,
                "mode": context.mode,
                "request_text": context.request_text,
                "summary_text": context.summary_text,
                "report_url": context.report_url,
                "report_excerpt": context.report_excerpt,
                "history": context.history,
                "created_at": context.created_at,
                "updated_at": context.updated_at,
            }
            for key, context in self._contexts.items()
        }
        self.state_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


class AgentActivityStore:
    def __init__(self, state_file: str | Path, *, max_progress_events: int = 200) -> None:
        self.state_file = Path(state_file)
        self.max_progress_events = max(1, int(max_progress_events))
        self._lock = threading.RLock()
        self._sessions = self._load_sessions()

    def record_event(self, event: LarkEvent, *, content: str | None = None) -> None:
        key = self._session_key(event)
        if not key:
            return
        with self._lock:
            now = _now_iso()
            session = self._sessions.get(key, {})
            session.update(
                {
                    "session_id": key,
                    "event_id": event.event_id,
                    "message_id": event.message_id,
                    "chat_id": event.chat_id,
                    "chat_type": event.chat_type,
                    "sender_id": event.sender_id,
                    "message_type": event.message_type,
                    "content": _trim_text(content if content is not None else event.content, 4000),
                    "create_time": event.create_time,
                    "timestamp": event.timestamp,
                    "reply_to": event.reply_to,
                    "parent_id": event.parent_id,
                    "root_id": event.root_id,
                    "thread_id": event.thread_id,
                    "status": session.get("status") or "running",
                    "started_at": session.get("started_at") or now,
                    "updated_at": now,
                }
            )
            session.setdefault("progress", [])
            self._sessions[key] = session
            self._save()

    def record_progress(self, payload: dict[str, object]) -> None:
        with self._lock:
            key = self._session_key_from_payload(payload)
            if not key:
                return
            now = _now_iso()
            session = self._sessions.get(key)
            if session is None:
                session = {
                    "session_id": key,
                    "event_id": str(payload.get("event_id") or ""),
                    "message_id": str(payload.get("message_id") or ""),
                    "chat_id": str(payload.get("chat_id") or ""),
                    "chat_type": str(payload.get("chat_type") or ""),
                    "status": "running",
                    "started_at": now,
                    "progress": [],
                }
            progress = session.setdefault("progress", [])
            if not isinstance(progress, list):
                progress = []
                session["progress"] = progress
            progress.append(
                {
                    "timestamp": now,
                    "stage": str(payload.get("stage") or "progress"),
                    "message": _trim_text(str(payload.get("message") or ""), 1200),
                    "details": _jsonable_limited(payload.get("details") or {}),
                }
            )
            session["progress"] = progress[-self.max_progress_events :]
            if session.get("status") not in {"succeeded", "failed", "skipped"}:
                session["status"] = "running"
            session["updated_at"] = now
            self._sessions[key] = session
            self._save()

    def record_result(self, event: LarkEvent, result: TaskResult) -> None:
        root_key = ""
        if isinstance(result.details, dict):
            root_key = str(result.details.get("conversation_root_message_id") or "").strip()
        key = root_key or self._session_key(event)
        if not key:
            return
        with self._lock:
            now = _now_iso()
            session = self._sessions.get(key, {"session_id": key, "progress": [], "started_at": now})
            details = _jsonable_limited(result.details)
            report_url = ""
            if isinstance(details, dict):
                report_url = str(details.get("published_report_url") or "")
            session.update(
                {
                    "event_id": event.event_id,
                    "message_id": event.message_id,
                    "chat_id": event.chat_id,
                    "chat_type": event.chat_type,
                    "sender_id": event.sender_id,
                    "content": _trim_text(event.content, 4000),
                    "status": _result_status(result),
                    "mode": str(result.details.get("mode") or ""),
                    "success": result.success,
                    "skipped": result.skipped,
                    "error_code": result.error_code or "",
                    "message": _trim_text(result.message, 4000),
                    "job_id": result.job_id or "",
                    "job_dir": str(result.job_dir or ""),
                    "html_report": str(result.html_report or ""),
                    "json_report": str(result.json_report or ""),
                    "report_url": report_url,
                    "duration_seconds": result.duration_seconds,
                    "details": details,
                    "updated_at": now,
                    "finished_at": now,
                }
            )
            session.setdefault("started_at", now)
            session.setdefault("progress", [])
            self._sessions[key] = session
            event_key = self._session_key(event)
            if root_key and event_key and event_key != key:
                self._sessions.pop(event_key, None)
            self._save()

    def record_error(self, event: LarkEvent, error: BaseException) -> None:
        key = self._session_key(event)
        if not key:
            return
        with self._lock:
            now = _now_iso()
            session = self._sessions.get(key, {"session_id": key, "progress": [], "started_at": now})
            session.update(
                {
                    "event_id": event.event_id,
                    "message_id": event.message_id,
                    "chat_id": event.chat_id,
                    "chat_type": event.chat_type,
                    "sender_id": event.sender_id,
                    "content": _trim_text(event.content, 4000),
                    "status": "failed",
                    "success": False,
                    "error_code": type(error).__name__,
                    "message": _trim_text(str(error), 4000),
                    "updated_at": now,
                    "finished_at": now,
                }
            )
            session.setdefault("progress", [])
            self._sessions[key] = session
            self._save()

    def list_sessions(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        with self._lock:
            sessions = sorted(
                self._sessions.values(),
                key=lambda item: str(item.get("updated_at") or item.get("started_at") or ""),
                reverse=True,
            )
            if limit is not None and limit >= 0:
                sessions = sessions[:limit]
            return [_public_session(item, include_progress=False) for item in sessions]

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            session = self._sessions.get(session_id.strip())
            if session is None:
                return None
            return _public_session(session, include_progress=True)

    def clear(self) -> int:
        with self._lock:
            count = len(self._sessions)
            self._sessions = {}
            try:
                self.state_file.unlink()
            except FileNotFoundError:
                pass
            return count

    def prune_expired(self, *, max_age_hours: int, now: datetime | None = None) -> int:
        if max_age_hours <= 0:
            return 0
        with self._lock:
            reference_time = now or datetime.now(timezone.utc)
            removed = 0
            cutoff_seconds = max_age_hours * 3600
            for key, session in list(self._sessions.items()):
                updated_at = _parse_timestamp(str(session.get("updated_at") or session.get("started_at") or ""))
                if updated_at is None:
                    continue
                age_seconds = reference_time.timestamp() - updated_at.timestamp()
                if age_seconds <= cutoff_seconds:
                    continue
                self._sessions.pop(key, None)
                removed += 1
            if removed:
                self._save()
            return removed

    def _session_key(self, event: LarkEvent) -> str:
        return (event.message_id or event.event_id).strip()

    def _session_key_from_payload(self, payload: dict[str, object]) -> str:
        session_id = str(payload.get("session_id") or "").strip()
        if session_id:
            return session_id
        message_id = str(payload.get("message_id") or "").strip()
        if message_id:
            return message_id
        event_id = str(payload.get("event_id") or "").strip()
        if event_id:
            for key, session in self._sessions.items():
                if str(session.get("event_id") or "") == event_id:
                    return key
            return event_id
        return ""

    def _load_sessions(self) -> dict[str, dict[str, Any]]:
        if not self.state_file.exists():
            return {}
        try:
            payload = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        raw_sessions = payload.get("sessions")
        if not isinstance(raw_sessions, dict):
            return {}
        sessions: dict[str, dict[str, Any]] = {}
        for key, value in raw_sessions.items():
            if isinstance(key, str) and isinstance(value, dict):
                sessions[key] = value
        return sessions

    def _save(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {"sessions": self._sessions}
        self.state_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _coerce_history(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    history: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role", "")).strip()
        content = str(item.get("content", "")).strip()
        if not role or not content:
            continue
        history.append({"role": role, "content": content})
    return history


def _parse_timestamp(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _result_status(result: TaskResult) -> str:
    if result.skipped:
        return "skipped"
    if result.success:
        return "succeeded"
    return "failed"


def _public_session(session: dict[str, Any], *, include_progress: bool) -> dict[str, Any]:
    result = _jsonable_limited(session, max_text=4000)
    if not isinstance(result, dict):
        return {}
    if not include_progress:
        progress = result.get("progress")
        result["progress_count"] = len(progress) if isinstance(progress, list) else 0
        result.pop("progress", None)
        result.pop("details", None)
    return result


def _jsonable_limited(value: Any, *, max_text: int = 2000) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable_limited(item, max_text=max_text) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable_limited(item, max_text=max_text) for item in value]
    if isinstance(value, str):
        return _trim_text(value, max_text)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _trim_text(str(value), max_text)


def _trim_text(value: object, max_chars: int) -> str:
    text = "" if value is None else str(value)
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"
