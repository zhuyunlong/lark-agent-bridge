"""Concurrency dispatcher: route incoming Lark payloads to inline (light) or
worker-pool (heavy) execution.

The dispatcher is a thin scheduling layer. It NEVER changes *how* a payload is
handled — every payload still flows through ``app.handle_payload`` unchanged
(card-action payloads included, because handle_payload itself splits card
actions from message events). The only decision made here is *which thread*
runs it:

* light → handled inline on the producer thread (sub-100ms, no shared state)
* heavy → enqueued and handled by a worker thread, serialized per ``chat_id``

This preserves the existing trigger matrix, permission semantics and public
method signatures (the red line of the concurrency design). A wrong weight
guess only changes the executing thread, never correctness — de-duplication is
still enforced by the per-path ``state_store.mark_seen`` test-and-set inside
handle_payload, not here.
"""

from __future__ import annotations

import queue
import threading
from typing import Callable, Protocol

from .lifecycle import AnalysisState, AnalysisType
from .log import get_logger
from .models import CardActionEvent, LarkEvent, TaskResult
from .parser import build_basic_chat_reply

logger = get_logger("dispatcher")

# Card actions that re-run analysis through bug_runner subprocesses (30-300s).
# They must run on a worker, not inline on the producer thread. Every other card
# action (approve/reject, feedback, escalate, select_bug_agent, answer_from_report,
# cancel_bug_agent_reanalysis) is an instant in-memory state op and stays light.
_HEAVY_CARD_ACTIONS = frozenset(
    {"reanalyze", "continue_agent", "confirm_bug_agent_reanalysis"}
)


class _DispatchApp(Protocol):
    """Minimal surface the dispatcher needs from BridgeApp (duck-typed to avoid a
    circular import)."""

    config: object

    def handle_payload(self, payload: dict[str, object]) -> TaskResult: ...

    def _looks_like_card_action_payload(self, payload: dict[str, object]) -> bool: ...


class ChatSessionLock:
    """Per-``chat_id`` lock so heavy tasks (and followups) for one chat stay
    serialized while different chats run in parallel (design goals G1/G2)."""

    def __init__(self) -> None:
        self._locks: dict[str, threading.Lock] = {}
        self._meta = threading.Lock()

    def get_lock(self, chat_id: str) -> threading.Lock:
        with self._meta:
            lock = self._locks.get(chat_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[chat_id] = lock
            return lock

    def held_count(self) -> int:
        with self._meta:
            return sum(1 for lock in self._locks.values() if lock.locked())


class EventDispatcher:
    def __init__(
        self,
        app: _DispatchApp,
        *,
        max_workers: int = 3,
        max_queue_size: int = 32,
        heavy_timeout_seconds: float = 1800,
        light_inline: bool = True,
        on_result: Callable[[TaskResult], None] | None = None,
    ) -> None:
        self._app = app
        self._max_workers = max(1, int(max_workers))
        self._heavy_queue: queue.Queue = queue.Queue(maxsize=max(1, int(max_queue_size)))
        # Retained for config/observability parity. Python threads cannot be
        # force-killed, so per-task timeout is enforced inside handle_payload
        # (runner timeouts) and ProcessWatchdog, not here.
        self._heavy_timeout = heavy_timeout_seconds
        self._light_inline = bool(light_inline)
        self._on_result = on_result
        self._chat_locks = ChatSessionLock()
        self._workers: list[threading.Thread] = []
        self._shutdown = threading.Event()
        self._metrics_lock = threading.Lock()
        self._output_lock = threading.Lock()
        self._active_workers = 0
        self._total_dispatched = 0
        self._total_light = 0
        self._total_heavy = 0

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        for i in range(self._max_workers):
            t = threading.Thread(
                target=self._worker_loop,
                args=(i,),
                name=f"bridge-worker-{i}",
                daemon=True,
            )
            t.start()
            self._workers.append(t)
        logger.info("dispatcher started with %d worker(s)", self._max_workers)

    def shutdown(self, *, timeout: float = 30) -> None:
        self._shutdown.set()
        for _ in self._workers:
            self._heavy_queue.put(None)  # one sentinel per worker
        for t in self._workers:
            t.join(timeout=timeout)
        remaining = self._heavy_queue.qsize()
        if remaining:
            logger.warning(
                "dispatcher shutdown with %d queued event(s) unprocessed", remaining
            )

    # -- producer side ---------------------------------------------------

    def dispatch(self, payload: dict[str, object]) -> None:
        weight, chat_id = self._classify(payload)
        with self._metrics_lock:
            self._total_dispatched += 1
            if weight == "light":
                self._total_light += 1
            else:
                self._total_heavy += 1
        logger.info("dispatch weight=%s chat=%s", weight, chat_id)
        if weight == "light":
            # light is stateless (basic_chat templates) — run inline WITHOUT a
            # chat lock so it can never be blocked behind a heavy task (G3).
            self._handle(payload, chat_id=None)
        else:
            # Blocks the producer when the queue is full: natural backpressure,
            # never drops an event.
            lifecycle = self._begin_lifecycle(payload, chat_id)
            self._heavy_queue.put((payload, chat_id, lifecycle))

    # -- classification --------------------------------------------------

    def _classify(self, payload: dict[str, object]) -> tuple[str, str]:
        """Return ``(weight, chat_id)``. Heuristic pre-classification only — the
        authoritative routing still happens inside handle_payload/_dispatch_route."""
        try:
            if self._app._looks_like_card_action_payload(payload):
                action_event = CardActionEvent.from_dict(payload)
                action = action_event.action.strip()
                weight = "heavy" if action in _HEAVY_CARD_ACTIONS else "light"
                return weight, action_event.chat_id
            event = LarkEvent.from_dict(payload)
            if self._is_light_event(event):
                return "light", event.chat_id
            return "heavy", event.chat_id
        except Exception:
            logger.exception("dispatcher: classification failed, treating as heavy")
            return "heavy", ""

    def _is_light_event(self, event: LarkEvent) -> bool:
        if not self._light_inline:
            return False
        # Only p2p, plain-text, non-reply messages that hit a deterministic
        # basic_chat template are safe to inline. Everything else — group msgs
        # (need mention-strip before classification), attachments, replies,
        # omlx/knowledge chat, addr2line/rom subprocess routes — goes to a
        # worker. A heavy task wrongly inlined would block the consume loop.
        if event.chat_type != "p2p":
            return False
        if event.message_type != "text":
            return False
        if event.reply_to:
            return False
        command_prefixes = getattr(self._app.config, "command_prefixes", None)
        return build_basic_chat_reply(event.content, command_prefixes=command_prefixes) is not None

    # -- execution -------------------------------------------------------

    def _begin_lifecycle(self, payload: dict[str, object], chat_id: str):
        """Create a QUEUED lifecycle record for a heavy task (best-effort).

        Coarse-grained on purpose: the dispatcher does not know the concrete
        analysis type (decided later in _dispatch_route), so type is UNKNOWN.
        Returns None if the app has no lifecycle_store or on any error —
        lifecycle tracking must never break event handling.
        """
        store = getattr(self._app, "lifecycle_store", None)
        if store is None:
            return None
        try:
            event = LarkEvent.from_dict(payload)
            lc = store.create(
                AnalysisType.UNKNOWN,
                chat_id=chat_id,
                message_id=event.message_id,
                request_text=event.content[:200],
            )
            lc.transition_to(AnalysisState.QUEUED, reason="enqueued")
            return lc
        except Exception:
            logger.exception("dispatcher: lifecycle begin failed")
            return None

    def _finish_lifecycle(self, lifecycle, *, success: bool) -> None:
        # If handle_payload failed before ANALYZING was reached, the lifecycle is
        # still QUEUED — QUEUED->FAILED is valid per the transition table, so this
        # stays correct; COMPLETED only ever follows ANALYZING.
        if lifecycle is None:
            return
        try:
            lifecycle.transition_to(
                AnalysisState.COMPLETED if success else AnalysisState.FAILED,
                reason="completed" if success else "failed",
            )
        except Exception:
            logger.debug("dispatcher: lifecycle finish transition skipped", exc_info=True)

    def _handle(self, payload: dict[str, object], *, chat_id: str | None, lifecycle=None) -> None:
        lock = self._chat_locks.get_lock(chat_id) if chat_id else None
        if lock is not None:
            lock.acquire()
        if lifecycle is not None:
            try:
                lifecycle.transition_to(AnalysisState.ANALYZING, reason="worker_started")
            except Exception:
                logger.debug("dispatcher: lifecycle ANALYZING transition skipped", exc_info=True)
        try:
            result = self._app.handle_payload(payload)
            if self._on_result is not None and result is not None:
                with self._output_lock:
                    self._on_result(result)
            self._finish_lifecycle(lifecycle, success=bool(result and result.success))
        except Exception:
            logger.exception("dispatcher: handle_payload failed")
            self._finish_lifecycle(lifecycle, success=False)
        finally:
            if lock is not None:
                lock.release()

    def _worker_loop(self, worker_id: int) -> None:
        while True:
            item = self._heavy_queue.get()
            if item is None:  # shutdown sentinel
                self._heavy_queue.task_done()
                break
            payload, chat_id, lifecycle = item
            with self._metrics_lock:
                self._active_workers += 1
            try:
                self._handle(payload, chat_id=chat_id, lifecycle=lifecycle)
            finally:
                with self._metrics_lock:
                    self._active_workers -= 1
                self._heavy_queue.task_done()

    # -- observability ---------------------------------------------------

    def metrics(self) -> dict[str, object]:
        with self._metrics_lock:
            return {
                "max_workers": self._max_workers,
                "active_workers": self._active_workers,
                "queue_depth": self._heavy_queue.qsize(),
                "chat_locks_held": self._chat_locks.held_count(),
                "total_dispatched": self._total_dispatched,
                "total_light": self._total_light,
                "total_heavy": self._total_heavy,
            }
