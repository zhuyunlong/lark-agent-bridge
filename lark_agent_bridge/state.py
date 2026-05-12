"""Persistent event de-duplication state."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

from .models import LarkEvent


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

