# Route Arbitration Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace fixed-order winner selection for the highest-conflict business routes with explicit candidate collection and arbitration, while preserving existing hard gates and followup sequencing.

**Architecture:** Keep the existing parsers and most route handlers intact, but split dispatch into two phases for a scoped set of routes: candidate collection and winner selection. The first phase builds `RouteCandidate` objects for `knowledge_qa`, `source_analysis`, `bug_request`, and `direct_analysis`; the second applies explicit priority bands and tie-break rules so fixed list order no longer decides those conflicts.

**Tech Stack:** Python, unittest/pytest, existing `BridgeApp` routing stack

---

### Task 1: Add regression tests for arbitration behavior

**Files:**
- Modify: `tests/test_app_source_analysis.py`
- Modify: `tests/test_integration.py`

- [ ] Add tests that prove explicit source requests beat knowledge heuristics unless the user explicitly asks for knowledge lookup.
- [ ] Add tests that prove bug links still beat repository-only source analysis and that resource-backed direct analysis still beats repository-only source analysis.
- [ ] Add a dispatch-focused test that asserts the app exposes a dedicated arbitration entrypoint instead of relying only on the old fixed-order list.

### Task 2: Introduce candidate data structures

**Files:**
- Modify: `lark_agent_bridge/app/_shared.py`
- Modify: `lark_agent_bridge/app/handle_event.py`

- [ ] Add a small `RouteCandidate` dataclass plus a constrained priority-band model for the scoped business routes.
- [ ] Extend `_RouteContext` only as needed so candidate collection can reuse already-parsed requests without recomputing them.

### Task 3: Implement scoped arbitration

**Files:**
- Modify: `lark_agent_bridge/app/handle_event.py`

- [ ] Add candidate builder helpers for `knowledge_qa`, `source_analysis`, `bug_request`, and `direct_analysis`.
- [ ] Add a winner-selection helper that prefers `explicit` over `heuristic`, then uses route-specific scores/tie-breakers.
- [ ] Update `_dispatch_route()` so hard-gate routes still run in order, but the four scoped business routes go through the arbitration helper before falling back to the remaining ordered handlers.

### Task 4: Verify regressions

**Files:**
- Test: `tests/test_app_source_analysis.py`
- Test: `tests/test_integration.py`

- [ ] Run focused tests for source-vs-knowledge and bug/direct/source conflicts.
- [ ] Run integration smoke coverage for dispatch structure.
- [ ] Run `git diff --check` on modified files.
