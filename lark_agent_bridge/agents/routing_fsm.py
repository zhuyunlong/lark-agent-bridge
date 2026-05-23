"""Lightweight state-machine router inspired by LangGraph.

Provides a declarative FSM for message routing that replaces the
deeply nested if/elif chains in app.py's handle_payload flow.

Design principles (borrowed from LangGraph, without the dependency):
- States are named steps in the processing pipeline
- Transitions are conditional edges based on step output
- Each state has a handler function that produces a result + next state
- The FSM is data-driven (dict config) not code-driven (if/elif)
- Supports "short-circuit" paths for high-confidence keyword matches

Flow diagram:
```
  ┌──────────┐    high confidence    ┌─────────┐
  │ keyword  │ ─────────────────────→│ execute │
  │  match   │                       └────┬────┘
  └────┬─────┘                            │
       │ low/no match                     ↓
       ↓                            ┌──────────┐
  ┌──────────┐                      │summarize │
  │   llm    │                      └────┬─────┘
  │ classify │                            │
  └────┬─────┘                            ↓
       │                            ┌─────────┐
       ↓                            │  reply  │
  ┌──────────┐                      └─────────┘
  │  route   │──→ execute ──→ summarize ──→ reply
  └──────────┘
```

Usage:
    fsm = RoutingFSM(config)
    result = fsm.run(event, route_content)
    # result.final_state tells you where processing ended
    # result.route tells you the classified route
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


class State(str, Enum):
    """Processing states in the routing FSM."""

    START = "start"
    KEYWORD_MATCH = "keyword_match"
    LLM_CLASSIFY = "llm_classify"
    ROUTE = "route"
    EXECUTE = "execute"
    SUMMARIZE = "summarize"
    REPLY = "reply"
    DONE = "done"
    ERROR = "error"


@dataclass(slots=True)
class FSMContext:
    """Mutable context passed through the FSM pipeline."""

    # Input
    message_text: str = ""
    chat_type: str = ""
    sender_id: str = ""
    event_data: dict[str, Any] = field(default_factory=dict)

    # Routing decision
    route: str = ""
    confidence: str = ""
    reason: str = ""
    followup_action: str = "none"
    context_source: str = "none"

    # Processing state
    current_state: State = State.START
    history: list[str] = field(default_factory=list)
    error: str = ""

    # Outputs
    result: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def transition(self, new_state: State, reason: str = "") -> None:
        """Record a state transition."""
        self.history.append(f"{self.current_state.value} -> {new_state.value}: {reason}")
        self.current_state = new_state


@dataclass(slots=True)
class Transition:
    """A conditional transition from one state to another."""

    target: State
    condition: Callable[[FSMContext], bool] | None = None
    label: str = ""


# Type for state handler: takes context, returns next state
StateHandler = Callable[[FSMContext], State]


class RoutingFSM:
    """Declarative routing state machine.

    Instead of nested if/elif, defines transitions as data:
    ```python
    fsm.add_state(State.KEYWORD_MATCH, keyword_handler)
    fsm.add_transition(State.KEYWORD_MATCH, State.EXECUTE, lambda ctx: ctx.confidence == "high")
    fsm.add_transition(State.KEYWORD_MATCH, State.LLM_CLASSIFY, lambda ctx: ctx.confidence != "high")
    ```
    """

    def __init__(self) -> None:
        self._handlers: dict[State, StateHandler] = {}
        self._transitions: dict[State, list[Transition]] = {}

    def add_state(self, state: State, handler: StateHandler) -> None:
        """Register a handler for a state."""
        self._handlers[state] = handler

    def add_transition(
        self,
        from_state: State,
        to_state: State,
        condition: Callable[[FSMContext], bool] | None = None,
        label: str = "",
    ) -> None:
        """Add a conditional transition between states."""
        if from_state not in self._transitions:
            self._transitions[from_state] = []
        self._transitions[from_state].append(Transition(target=to_state, condition=condition, label=label))

    def run(self, ctx: FSMContext, *, max_steps: int = 20) -> FSMContext:
        """Execute the FSM until DONE or ERROR state is reached."""
        steps = 0
        while ctx.current_state not in (State.DONE, State.ERROR) and steps < max_steps:
            handler = self._handlers.get(ctx.current_state)
            if handler is None:
                ctx.error = f"No handler for state: {ctx.current_state}"
                ctx.transition(State.ERROR, "missing handler")
                break

            # Execute the state handler
            next_state = handler(ctx)

            # If handler returned a specific state, use it
            if next_state and next_state != ctx.current_state:
                ctx.transition(next_state, "handler return")
            else:
                # Otherwise evaluate transitions
                resolved = self._resolve_transition(ctx)
                if resolved:
                    ctx.transition(resolved, "transition rule")
                else:
                    ctx.error = f"No valid transition from state: {ctx.current_state}"
                    ctx.transition(State.ERROR, "no transition")
                    break
            steps += 1

        if steps >= max_steps:
            ctx.error = f"FSM exceeded max steps ({max_steps})"
            ctx.transition(State.ERROR, "max steps")

        return ctx

    def _resolve_transition(self, ctx: FSMContext) -> State | None:
        """Find the first matching transition for the current state."""
        transitions = self._transitions.get(ctx.current_state, [])
        for t in transitions:
            if t.condition is None or t.condition(ctx):
                return t.target
        return None


# ---------------------------------------------------------------------------
# Factory: create a pre-configured routing FSM
# ---------------------------------------------------------------------------


def create_routing_fsm(
    *,
    keyword_handler: StateHandler | None = None,
    llm_classify_handler: StateHandler | None = None,
    route_handler: StateHandler | None = None,
) -> RoutingFSM:
    """Create a routing FSM with the standard message processing pipeline.

    Args:
        keyword_handler: Fast keyword matching (returns route + confidence)
        llm_classify_handler: LLM-based classification (for ambiguous messages)
        route_handler: Post-classification routing logic

    The FSM flow:
        START → KEYWORD_MATCH
            → (high confidence) → ROUTE → DONE
            → (low confidence) → LLM_CLASSIFY → ROUTE → DONE
    """
    fsm = RoutingFSM()

    # Default handlers
    def _default_start(ctx: FSMContext) -> State:
        return State.KEYWORD_MATCH

    def _default_keyword(ctx: FSMContext) -> State:
        if keyword_handler:
            return keyword_handler(ctx)
        # No keyword handler = always go to LLM
        ctx.confidence = "low"
        return State.LLM_CLASSIFY

    def _default_llm(ctx: FSMContext) -> State:
        if llm_classify_handler:
            return llm_classify_handler(ctx)
        ctx.route = "unsupported"
        ctx.confidence = "low"
        return State.ROUTE

    def _default_route(ctx: FSMContext) -> State:
        if route_handler:
            return route_handler(ctx)
        return State.DONE

    # Register states
    fsm.add_state(State.START, _default_start)
    fsm.add_state(State.KEYWORD_MATCH, _default_keyword)
    fsm.add_state(State.LLM_CLASSIFY, _default_llm)
    fsm.add_state(State.ROUTE, _default_route)
    fsm.add_state(State.DONE, lambda ctx: State.DONE)

    # Transitions (used when handlers don't return explicit next state)
    fsm.add_transition(State.START, State.KEYWORD_MATCH, label="always")
    fsm.add_transition(
        State.KEYWORD_MATCH,
        State.ROUTE,
        condition=lambda ctx: ctx.confidence == "high",
        label="high confidence keyword match",
    )
    fsm.add_transition(
        State.KEYWORD_MATCH,
        State.LLM_CLASSIFY,
        condition=lambda ctx: ctx.confidence != "high",
        label="low confidence, need LLM",
    )
    fsm.add_transition(State.LLM_CLASSIFY, State.ROUTE, label="classification done")
    fsm.add_transition(State.ROUTE, State.DONE, label="routing complete")

    return fsm
