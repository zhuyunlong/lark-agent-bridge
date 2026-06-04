"""Thread-safety regression tests for the persistent state stores (Phase 1).

These exercise the locks added for the concurrency refactor. Without those
locks the concurrent dict iteration / test-and-set below would intermittently
raise (e.g. "dictionary changed size during iteration") or lose updates.
"""

from __future__ import annotations

import threading

from lark_agent_bridge.models import LarkEvent
from lark_agent_bridge.state import ConversationContextStore, EventStateStore


def _event(event_id: str) -> LarkEvent:
    return LarkEvent(
        event_id=event_id,
        message_id="m",
        chat_id="c",
        chat_type="p2p",
        sender_id="s",
        message_type="text",
        content="hi",
    )


class TestEventStateStoreConcurrency:
    def test_concurrent_mark_seen_is_a_single_winner(self, tmp_path):
        store = EventStateStore(tmp_path / "seen.jsonl")
        event = _event("dup")
        results: list[bool] = []
        rlock = threading.Lock()
        start = threading.Event()

        def worker():
            start.wait(5)
            won = store.mark_seen(event)
            with rlock:
                results.append(won)

        threads = [threading.Thread(target=worker) for _ in range(25)]
        for t in threads:
            t.start()
        start.set()
        for t in threads:
            t.join(5)

        # mark_seen is a test-and-set: exactly one thread may claim the event.
        assert len(results) == 25
        assert sum(1 for r in results if r) == 1

    def test_distinct_events_all_recorded(self, tmp_path):
        store = EventStateStore(tmp_path / "seen.jsonl")
        events = [_event(f"e{i}") for i in range(50)]
        errors: list[Exception] = []

        def worker(ev):
            try:
                store.mark_seen(ev)
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(ev,)) for ev in events]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)

        assert not errors
        for ev in events:
            assert store.has_seen(ev.event_id)


class TestConversationContextStoreConcurrency:
    def test_concurrent_read_write_no_crash(self, tmp_path):
        store = ConversationContextStore(tmp_path / "ctx.json")
        store.remember(
            root_message_id="root",
            chat_id="c1",
            mode="bug",
            request_text="",
            summary_text="",
            report_url="",
            report_excerpt="",
        )
        errors: list[Exception] = []

        def writer():
            try:
                for i in range(80):
                    store.append_exchange("root", user_text=f"u{i}", assistant_text=f"a{i}")
                    store.remember(
                        root_message_id=f"r{i}",
                        chat_id="c1",
                        mode="bug",
                        request_text="",
                        summary_text="",
                        report_url="",
                        report_excerpt="",
                    )
            except Exception as exc:
                errors.append(exc)

        def reader():
            try:
                for _ in range(80):
                    store.latest_for_chat("c1")
                    store.lookup("root")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=writer) for _ in range(3)]
        threads += [threading.Thread(target=reader) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)

        assert not errors
        assert store.lookup("root") is not None
