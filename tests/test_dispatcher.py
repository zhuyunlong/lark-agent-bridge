"""Tests for the concurrency EventDispatcher and ChatSessionLock (Phase 2)."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from lark_agent_bridge.dispatcher import (
    ChatSessionLock,
    EventDispatcher,
    _HEAVY_CARD_ACTIONS,
)
from lark_agent_bridge.models import TaskResult


def _wait(cond, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met within timeout")


def _looks_like_card(payload: dict) -> bool:
    """Mirror of BridgeApp._looks_like_card_action_payload."""
    event_body = payload.get("event") or payload
    action = event_body.get("action")
    if isinstance(action, dict):
        value = action.get("value")
        if isinstance(value, dict) and value.get("action"):
            return True
    value = payload.get("value")
    return isinstance(value, dict) and bool(value.get("action"))


class FakeApp:
    def __init__(self) -> None:
        self.config = SimpleNamespace(command_prefixes=[])
        self._lock = threading.Lock()
        self.calls: list[tuple[str, dict]] = []
        self.entered: threading.Event | None = None
        self.release: threading.Event | None = None

    def handle_payload(self, payload: dict) -> TaskResult:
        with self._lock:
            self.calls.append((threading.current_thread().name, dict(payload)))
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            self.release.wait(5)
        return TaskResult(success=True, message="ok")

    def _looks_like_card_action_payload(self, payload: dict) -> bool:
        return _looks_like_card(payload)


def _p2p_text(
    content: str,
    *,
    event_id: str = "e",
    chat_id: str = "c",
    message_id: str = "",
    reply_to: str = "",
) -> dict:
    return {
        "event_id": event_id,
        "chat_id": chat_id,
        "chat_type": "p2p",
        "message_type": "text",
        "content": content,
        "message_id": message_id,
        "reply_to": reply_to,
    }


def _group_text(
    content: str,
    *,
    event_id: str = "e",
    chat_id: str = "c",
    message_id: str = "",
    reply_to: str = "",
) -> dict:
    return {
        "event_id": event_id,
        "chat_id": chat_id,
        "chat_type": "group",
        "message_type": "text",
        "content": content,
        "message_id": message_id,
        "reply_to": reply_to,
    }


def _card(action: str, *, chat_id: str = "c") -> dict:
    return {"event_id": "e", "chat_id": chat_id, "action": {"value": {"action": action}}}


class TestClassification:
    def test_p2p_basic_chat_is_light(self):
        d = EventDispatcher(FakeApp(), max_workers=1)
        weight, _ = d._classify(_p2p_text("你是谁"))
        assert weight == "light"

    def test_p2p_non_template_is_heavy(self):
        d = EventDispatcher(FakeApp(), max_workers=1)
        weight, _ = d._classify(_p2p_text("帮我分析这个崩溃日志的根因"))
        assert weight == "heavy"

    def test_group_basic_chat_is_heavy(self):
        # Group messages need mention-strip before classification -> always heavy.
        d = EventDispatcher(FakeApp(), max_workers=1)
        weight, _ = d._classify(_group_text("你是谁"))
        assert weight == "heavy"

    def test_p2p_reply_is_heavy(self):
        d = EventDispatcher(FakeApp(), max_workers=1)
        payload = _p2p_text("你是谁")
        payload["reply_to"] = "m1"
        weight, _ = d._classify(payload)
        assert weight == "heavy"

    def test_p2p_attachment_is_heavy(self):
        d = EventDispatcher(FakeApp(), max_workers=1)
        payload = _p2p_text("你是谁")
        payload["message_type"] = "file"
        weight, _ = d._classify(payload)
        assert weight == "heavy"

    def test_light_inline_disabled_forces_heavy(self):
        d = EventDispatcher(FakeApp(), max_workers=1, light_inline=False)
        weight, _ = d._classify(_p2p_text("你是谁"))
        assert weight == "heavy"

    def test_heavy_card_actions_go_to_worker(self):
        d = EventDispatcher(FakeApp(), max_workers=1)
        for action in _HEAVY_CARD_ACTIONS:
            weight, _ = d._classify(_card(action))
            assert weight == "heavy", action

    def test_instant_card_actions_stay_light(self):
        d = EventDispatcher(FakeApp(), max_workers=1)
        for action in ("approve", "reject", "escalate"):
            weight, _ = d._classify(_card(action))
            assert weight == "light", action

    def test_heavy_card_action_locks_on_chain_root_not_chat(self):
        # A heavy card action (reanalyze/continue) carries the chain root in its
        # value. It must serialize on the SAME chain-root lock as message
        # followups in that chain — keying it on chat_id would let a reanalyze
        # and a message followup on one chain run concurrently and corrupt the
        # shared conversation/version state.
        d = EventDispatcher(FakeApp(), max_workers=1)
        payload = _card("reanalyze", chat_id="chatX")
        payload["action"]["value"]["root_message_id"] = "rootA"
        lock_key, inflight_key = d._resolve_lock_key(payload, "chatX")
        assert lock_key == "rootA"
        assert inflight_key == ""

    def test_heavy_card_action_without_root_falls_back_to_chat(self):
        d = EventDispatcher(FakeApp(), max_workers=1)
        lock_key, _ = d._resolve_lock_key(_card("reanalyze", chat_id="chatX"), "chatX")
        assert lock_key == "chatX"


class TestDispatchExecution:
    def test_light_runs_inline_on_producer_thread(self):
        app = FakeApp()
        d = EventDispatcher(app, max_workers=2)
        d.start()
        try:
            d.dispatch(_p2p_text("你是谁"))
            # inline == handled synchronously on the calling (producer) thread
            assert len(app.calls) == 1
            assert app.calls[0][0] == threading.current_thread().name
        finally:
            d.shutdown()

    def test_heavy_runs_on_worker_thread(self):
        app = FakeApp()
        d = EventDispatcher(app, max_workers=2)
        d.start()
        try:
            d.dispatch(_group_text("分析这个 bug 的根因"))
            _wait(lambda: len(app.calls) == 1)
            assert app.calls[0][0].startswith("bridge-worker-")
        finally:
            d.shutdown()

    def test_same_chat_independent_requests_run_in_parallel(self):
        # Two INDEPENDENT requests in the same group (distinct root messages, no
        # reply chain between them) must NOT block each other: the lock is keyed
        # by conversation-chain root, not chat_id.
        app = FakeApp()
        barrier = threading.Barrier(2, timeout=5)
        original = app.handle_payload

        def gated(payload):
            barrier.wait()  # both tasks must be in-flight together to pass
            return original(payload)

        app.handle_payload = gated
        d = EventDispatcher(app, max_workers=3)
        d.start()
        try:
            d.dispatch(_group_text("A", event_id="a", chat_id="chatX", message_id="mA"))
            d.dispatch(_group_text("B", event_id="b", chat_id="chatX", message_id="mB"))
            _wait(lambda: len(app.calls) == 2)
        finally:
            d.shutdown()

    def test_followup_to_inflight_parent_serializes(self):
        # B replies to A's trigger message while A is still in-flight — A has not
        # delivered, so the conversation store has no root for it yet. B must
        # still serialize behind A, resolved via the in-flight chain registry
        # (eager-register), NOT race ahead as an independent chain.
        app = FakeApp()
        app.entered = threading.Event()
        app.release = threading.Event()
        d = EventDispatcher(app, max_workers=3)
        d.start()
        try:
            d.dispatch(_group_text("A", event_id="a", chat_id="chatX", message_id="mA"))
            assert app.entered.wait(5)  # A in-flight, holding its chain-root lock
            d.dispatch(
                _group_text("B", event_id="b", chat_id="chatX", message_id="mB", reply_to="mA")
            )
            time.sleep(0.2)
            # B must be blocked behind A's in-flight chain — only A has run.
            assert len(app.calls) == 1
            app.release.set()
            _wait(lambda: len(app.calls) == 2)
        finally:
            app.release.set()
            d.shutdown()

    def test_per_chat_cap_serializes_independent_roots(self):
        # Fairness cap: with max_concurrent_per_chat=1, two INDEPENDENT same-chat
        # roots must NOT run concurrently (so one chat can't occupy every worker
        # and starve other chats), even though they have different root locks.
        app = FakeApp()
        app.entered = threading.Event()
        app.release = threading.Event()
        d = EventDispatcher(app, max_workers=3, max_concurrent_per_chat=1)
        d.start()
        try:
            d.dispatch(_group_text("A", event_id="a", chat_id="chatX", message_id="mA"))
            assert app.entered.wait(5)  # A in-flight, holding chatX's only slot
            d.dispatch(_group_text("B", event_id="b", chat_id="chatX", message_id="mB"))
            time.sleep(0.2)
            # B is an independent root but blocked by the per-chat cap.
            assert len(app.calls) == 1
            app.release.set()
            _wait(lambda: len(app.calls) == 2)
        finally:
            app.release.set()
            d.shutdown()

    def test_per_chat_cap_allows_other_chats(self):
        # The cap is per-chat: a different chat is never blocked by chatX's slot.
        app = FakeApp()
        barrier = threading.Barrier(2, timeout=5)
        original = app.handle_payload

        def gated(payload):
            barrier.wait()
            return original(payload)

        app.handle_payload = gated
        d = EventDispatcher(app, max_workers=3, max_concurrent_per_chat=1)
        d.start()
        try:
            d.dispatch(_group_text("A", event_id="a", chat_id="chatA", message_id="mA"))
            d.dispatch(_group_text("B", event_id="b", chat_id="chatB", message_id="mB"))
            _wait(lambda: len(app.calls) == 2)  # different chats run together
        finally:
            d.shutdown()

    def test_followup_to_delivered_parent_serializes_via_store(self):
        # A followup whose parent already delivered resolves to the parent's
        # chain root through the conversation store (branch 2, not the in-flight
        # registry) and serializes behind a task holding that root.
        app = FakeApp()
        app.conversation_store = SimpleNamespace(
            lookup=lambda key: SimpleNamespace(root_message_id="rootA")
            if key == "mDelivered"
            else None
        )
        app.entered = threading.Event()
        app.release = threading.Event()
        d = EventDispatcher(app, max_workers=3)
        d.start()
        try:
            d.dispatch(_group_text("A", event_id="a", chat_id="chatX", message_id="rootA"))
            assert app.entered.wait(5)  # A holds lock for chain root "rootA"
            d.dispatch(
                _group_text(
                    "B", event_id="b", chat_id="chatX", message_id="mB", reply_to="mDelivered"
                )
            )
            time.sleep(0.2)
            # B resolves reply_to "mDelivered" -> root "rootA" via the store and
            # blocks behind A.
            assert len(app.calls) == 1
            app.release.set()
            _wait(lambda: len(app.calls) == 2)
        finally:
            app.release.set()
            d.shutdown()

    def test_different_chats_run_in_parallel(self):
        app = FakeApp()
        barrier = threading.Barrier(2, timeout=5)
        original = app.handle_payload

        def gated(payload):
            barrier.wait()  # both tasks must be in-flight together to pass
            return original(payload)

        app.handle_payload = gated
        d = EventDispatcher(app, max_workers=3)
        d.start()
        try:
            d.dispatch(_group_text("A", event_id="a", chat_id="chatA"))
            d.dispatch(_group_text("B", event_id="b", chat_id="chatB"))
            _wait(lambda: len(app.calls) == 2)
        finally:
            d.shutdown()

    def test_shutdown_joins_workers(self):
        app = FakeApp()
        d = EventDispatcher(app, max_workers=2)
        d.start()
        d.dispatch(_group_text("x"))
        _wait(lambda: len(app.calls) == 1)
        d.shutdown(timeout=5)
        assert all(not t.is_alive() for t in d._workers)

    def test_metrics_counts(self):
        app = FakeApp()
        d = EventDispatcher(app, max_workers=2)
        d.start()
        try:
            d.dispatch(_p2p_text("你是谁"))            # light
            d.dispatch(_group_text("分析这个 bug"))    # heavy
            _wait(lambda: len(app.calls) == 2)
            m = d.metrics()
            assert m["total_dispatched"] == 2
            assert m["total_light"] == 1
            assert m["total_heavy"] == 1
            assert m["max_workers"] == 2
        finally:
            d.shutdown()


class TestChatSessionLock:
    def test_same_id_returns_same_lock(self):
        cl = ChatSessionLock()
        assert cl.get_lock("a") is cl.get_lock("a")

    def test_different_id_returns_different_lock(self):
        cl = ChatSessionLock()
        assert cl.get_lock("a") is not cl.get_lock("b")

    def test_held_count(self):
        cl = ChatSessionLock()
        lock = cl.get_lock("a")
        assert cl.held_count() == 0
        lock.acquire()
        try:
            assert cl.held_count() == 1
        finally:
            lock.release()


class TestLifecycleIntegration:
    def test_heavy_job_lifecycle_transitions(self):
        from lark_agent_bridge.lifecycle import LifecycleStore, AnalysisState

        app = FakeApp()
        app.lifecycle_store = LifecycleStore()
        d = EventDispatcher(app, max_workers=1)
        d.start()
        try:
            d.dispatch(_group_text("分析这个 bug", chat_id="cX"))
            _wait(lambda: app.lifecycle_store.count == 1
                  and app.lifecycle_store.list_recent(limit=1)[0].is_terminal)
            lc = app.lifecycle_store.list_recent(limit=1)[0]
            assert lc.state == AnalysisState.COMPLETED
            assert lc.chat_id == "cX"
            # QUEUED (enqueue) -> ANALYZING (worker) -> COMPLETED (result)
            assert [t.to_state for t in lc.transitions] == ["queued", "analyzing", "completed"]
        finally:
            d.shutdown()

    def test_light_job_creates_no_lifecycle(self):
        from lark_agent_bridge.lifecycle import LifecycleStore

        app = FakeApp()
        app.lifecycle_store = LifecycleStore()
        d = EventDispatcher(app, max_workers=1)
        d.start()
        try:
            d.dispatch(_p2p_text("你是谁"))  # light, inline — no lifecycle record
            assert len(app.calls) == 1
            assert app.lifecycle_store.count == 0
        finally:
            d.shutdown()

    def test_failed_job_transitions_to_failed(self):
        from lark_agent_bridge.lifecycle import LifecycleStore, AnalysisState
        from lark_agent_bridge.models import TaskResult

        app = FakeApp()
        app.lifecycle_store = LifecycleStore()

        def failing(payload):
            with app._lock:
                app.calls.append((threading.current_thread().name, dict(payload)))
            return TaskResult(success=False, message="boom")

        app.handle_payload = failing
        d = EventDispatcher(app, max_workers=1)
        d.start()
        try:
            d.dispatch(_group_text("分析失败用例", chat_id="cF"))
            _wait(lambda: app.lifecycle_store.count == 1
                  and app.lifecycle_store.list_recent(limit=1)[0].is_terminal)
            lc = app.lifecycle_store.list_recent(limit=1)[0]
            assert lc.state == AnalysisState.FAILED
        finally:
            d.shutdown()
