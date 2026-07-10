"""Concurrency dispatcher: route incoming Lark payloads to inline (light) or
worker-pool (heavy) execution.

The dispatcher is a thin scheduling layer. It NEVER changes *how* a payload is
handled — every payload still flows through ``app.handle_payload`` unchanged
(card-action payloads included, because handle_payload itself splits card
actions from message events). The only decision made here is *which thread*
runs it:

* light → handled inline on the producer thread (sub-100ms, no shared state)
* heavy → enqueued and handled by a worker thread, serialized per
  conversation-chain ROOT (so independent requests in the same chat run
  concurrently while a followup stays serialized behind its chain)

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

    def is_app_server_control_payload(self, payload: dict[str, object]) -> bool: ...


class ChatSessionLock:
    """Per-key lock so heavy tasks sharing a serialization key stay serialized
    while different keys run in parallel (design goals G1/G2). The key is the
    conversation-chain ROOT (see ``EventDispatcher._resolve_lock_key``): two
    independent chains in the same chat get different keys and run concurrently;
    a followup resolves to its chain's root and serializes behind it."""

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
        max_concurrent_per_chat: int = 0,
        on_result: Callable[[TaskResult], None] | None = None,
    ) -> None:
        self._app = app
        self._max_workers = max(1, int(max_workers))
        # Fairness cap: max heavy tasks one chat may run concurrently (0 = no
        # cap, i.e. up to max_workers). Set < max_workers to reserve workers for
        # other chats so a burst in one group can't starve the rest.
        self._max_per_chat = max(0, int(max_concurrent_per_chat))
        self._heavy_queue: queue.Queue = queue.Queue(maxsize=max(1, int(max_queue_size)))
        # Retained for config/observability parity. Python threads cannot be
        # force-killed, so per-task timeout is enforced inside handle_payload
        # (runner timeouts) and ProcessWatchdog, not here.
        self._heavy_timeout = heavy_timeout_seconds
        self._light_inline = bool(light_inline)
        self._on_result = on_result
        self._chat_locks = ChatSessionLock()
        # Eager in-flight chain registry: message_id -> chain-root lock key, for
        # heavy tasks that are dispatched but not yet delivered. Lets a followup
        # arriving while its parent is still running resolve to the parent's
        # chain root (the conversation store has no entry yet at that point) and
        # serialize behind it instead of racing ahead as an independent chain.
        self._inflight_lock = threading.Lock()
        self._inflight_root: dict[str, str] = {}
        # Per-chat fairness semaphores (lazy, only when a cap is configured).
        self._chat_sems: dict[str, threading.BoundedSemaphore] = {}
        self._chat_sem_meta = threading.Lock()
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
            # lock so it can never be blocked behind a heavy task (G3).
            self._handle(payload, lock_key=None)
        else:
            # Serialize per conversation-chain ROOT (not per chat): independent
            # chains in the same group run concurrently, a followup serializes
            # behind its chain. Resolved in-memory only (producer thread must
            # never block on fetch_message).
            lock_key, inflight_key = self._resolve_lock_key(payload, chat_id)
            # Eagerly register this task's message so a followup arriving before
            # it delivers can resolve to its chain root. Registered on the serial
            # producer thread (before the next payload is classified), removed in
            # _handle once the task finishes.
            if inflight_key:
                with self._inflight_lock:
                    self._inflight_root[inflight_key] = lock_key
            # Blocks the producer when the queue is full: natural backpressure,
            # never drops an event.
            lifecycle = self._begin_lifecycle(payload, chat_id)
            self._heavy_queue.put((payload, lock_key, chat_id, lifecycle, inflight_key))

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
            is_control = getattr(self._app, "is_app_server_control_payload", None)
            if callable(is_control) and is_control(payload):
                return "light", event.chat_id
            if self._is_light_event(event):
                return "light", event.chat_id
            return "heavy", event.chat_id
        except Exception:
            logger.exception("dispatcher: classification failed, treating as heavy")
            return "heavy", ""

    def _resolve_lock_key(self, payload: dict[str, object], chat_id: str) -> tuple[str, str]:
        """Return ``(lock_key, inflight_key)`` for a heavy task.

        ``lock_key`` is the conversation-chain ROOT, so two INDEPENDENT requests
        in the same chat run concurrently while a followup stays serialized
        behind its chain (design goal G2 refined from per-chat to per-chain).
        ``inflight_key`` is the task's own message id, registered so a later
        followup can resolve to this chain while it is still running.

        Resolution is in-memory ONLY — the producer thread must never block on a
        ``fetch_message`` network call. A followup that cannot be resolved in
        memory (cold cache / cross-restart) falls back to the chat_id, the old
        conservative behavior (serialize the whole chat) rather than risk a
        followup racing its parent.
        """
        try:
            if self._app._looks_like_card_action_payload(payload):
                # Card actions (reanalyze/continue) act on an existing chain —
                # key them on that chain's ROOT so they serialize with message
                # followups in the same chain (the card carries the root in its
                # value). Fall back to chat_id only when no root is present.
                try:
                    action_event = CardActionEvent.from_dict(payload)
                    return (action_event.root_message_id or chat_id or "", "")
                except Exception:
                    return (chat_id or "", "")
            event = LarkEvent.from_dict(payload)
            message_id = event.message_id
            refs = [ref for ref in (event.reply_to, event.root_id, event.parent_id) if ref]
            if not refs:
                # Fresh new chain: the triggering message itself is the root.
                return (message_id or chat_id or "", message_id)
            # 1) In-flight chain registry: parent still running, no store entry yet.
            with self._inflight_lock:
                for ref in refs:
                    root = self._inflight_root.get(ref)
                    if root:
                        return (root, message_id)
            # 2) Delivered chain in the conversation store.
            store = getattr(self._app, "conversation_store", None)
            if store is not None:
                for ref in refs:
                    try:
                        context = store.lookup(ref)
                    except Exception:
                        context = None
                    if context is not None and context.root_message_id:
                        return (context.root_message_id, message_id)
            # 3) Unresolved followup: conservative chat-level serialization.
            return (chat_id or "", message_id)
        except Exception:
            logger.exception("dispatcher: lock-key resolution failed, using chat_id")
            return (chat_id or "", "")

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

    def _handle(
        self,
        payload: dict[str, object],
        *,
        lock_key: str | None,
        chat_id: str | None = None,
        lifecycle=None,
        inflight_key: str = "",
    ) -> None:
        # Fairness cap first (acquire BEFORE the chain-root lock, release after,
        # so the ordering is always sem -> root lock and never deadlocks). A task
        # blocked here is bounded by the per-chat slot count, so one chat cannot
        # run more than _max_per_chat heavy tasks at once.
        sem = self._get_chat_sem(chat_id) if (self._max_per_chat and chat_id) else None
        if sem is not None:
            sem.acquire()
        lock = self._chat_locks.get_lock(lock_key) if lock_key else None
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
            if sem is not None:
                sem.release()
            if inflight_key:
                with self._inflight_lock:
                    self._inflight_root.pop(inflight_key, None)

    def _get_chat_sem(self, chat_id: str) -> threading.BoundedSemaphore:
        with self._chat_sem_meta:
            sem = self._chat_sems.get(chat_id)
            if sem is None:
                sem = threading.BoundedSemaphore(self._max_per_chat)
                self._chat_sems[chat_id] = sem
            return sem

    def _worker_loop(self, worker_id: int) -> None:
        while True:
            item = self._heavy_queue.get()
            if item is None:  # shutdown sentinel
                self._heavy_queue.task_done()
                break
            payload, lock_key, chat_id, lifecycle, inflight_key = item
            with self._metrics_lock:
                self._active_workers += 1
            try:
                self._handle(
                    payload,
                    lock_key=lock_key,
                    chat_id=chat_id,
                    lifecycle=lifecycle,
                    inflight_key=inflight_key,
                )
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
