# Resolved Conversation Input Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace route-specific message, reply-chain, resource, control-action, and conversation-root recovery with one normalized conversation input while preserving current behavior during migration.

**Architecture:** Introduce an immutable `ResolvedConversationInput` compatibility envelope at event ingress, then migrate consumers in small independent commits. Existing request parsers and route order remain authoritative until their phase is explicitly migrated.

**Tech Stack:** Python 3.13, dataclasses, existing BridgeApp mixins, pytest/unittest.

## Global Constraints

- Implement on `pydantic_xpdev`; `release/pydantic-xpdev-20260712` remains an unchanged milestone.
- Do not change group mention policy, route priority, approval policy, or current Bug/file precedence as part of the compatibility phase.
- Resolve reply-chain identity and resources once per event before migrated consumers use them.
- Preserve resource provenance: kind, value, display name, and source message ID.
- Persist enough normalized input to recover the same conversation root after restart.
- Every phase starts with a failing behavior test and ends with an independently usable commit.

---

### Task 1: Compatibility Input Envelope

**Files:**
- Create: `lark_agent_bridge/conversation_input.py`
- Modify: `lark_agent_bridge/app/_shared.py`
- Modify: `lark_agent_bridge/app/handle_event.py`
- Test: `tests/test_conversation_input.py`

**Interfaces:**
- Produces: `ResolvedConversationInput`, `build_resolved_conversation_input(...)`, `is_pure_followup_control(...)`.
- Preserves: all existing `_RouteContext` fields and dispatch behavior.

- [x] Add RED tests for new-chain input, mixed continue payload, pure control payload, resource provenance, and `_RouteContext` compatibility wiring.
- [x] Implement the frozen input dataclass and pure builder.
- [x] Populate the input after existing address/resource/context resolution and attach it to `_RouteContext`.
- [x] Run `tests/test_conversation_input.py` plus group/follow-up route tests.
- [x] Commit as the first migration checkpoint.

### Task 2: Single Address and Reply-Chain Resolver

**Files:**
- Create: `lark_agent_bridge/app/conversation_resolver.py`
- Modify: `lark_agent_bridge/app/handle_event.py`
- Modify: `lark_agent_bridge/app/context_lookup.py`
- Test: `tests/test_conversation_resolver.py`

**Interfaces:**
- Consumes: `LarkEvent` and existing conversation/activity stores.
- Produces: addressed route text, direct reply ID, follow-up context, root ID, and provenance without repeated message fetches.

- [x] Add RED tests that count `fetch_message` calls for events lacking `reply_to`.
- [x] Move mention filtering and reply identity recovery behind one resolver.
- [x] Cache fetched message records for the lifetime of the resolved input.
- [x] Keep not-addressed and external-group policy results byte-for-byte compatible.
- [x] Commit after group, p2p, restart, and bot-alias tests pass.

### Task 3: Single Resource Resolution and Precedence

**Files:**
- Modify: `lark_agent_bridge/conversation_input.py`
- Modify: `lark_agent_bridge/app/conversation_resolver.py`
- Modify: `lark_agent_bridge/app/resources.py`
- Modify: `lark_agent_bridge/app/request_build.py`
- Test: `tests/test_conversation_resources.py`

**Interfaces:**
- Produces: current-message, reply-chain, session, and Bug-attachment resource groups plus an ordered `preferred_resources` view.

- [x] Add RED matrix tests for interactive cards, ZIP, single files, folders, local prepared inputs, and Bug attachments.
- [x] Resolve structured resources once and record every candidate's provenance.
- [x] Apply the precedence `explicit replied file -> existing local prepared input -> Bug attachment` centrally.
- [x] Migrate Bug, direct analysis, app-server, signal, and perception request builders to consume the normalized resource view.
- [x] Commit after downloader and archive preparation regressions pass.

### Task 4: Control Action and Payload Migration

**Files:**
- Modify: `lark_agent_bridge/conversation_input.py`
- Modify: `lark_agent_bridge/app/followup_clarify.py`
- Modify: `lark_agent_bridge/app/replay_flow.py`
- Modify: `lark_agent_bridge/app/context_from.py`
- Test: `tests/test_conversation_followup.py`

**Interfaces:**
- Produces: one `followup_action` and one lossless `followup_payload` for every migrated follow-up path.

- [ ] Add RED tests for pure retry/continue and mixed time, symptom, source-direction, and control text.
- [ ] Replace route-local `parse_followup_action()` calls with normalized input fields.
- [ ] Remove the compatibility-only direct-analysis control predicate after all consumers migrate.
- [ ] Commit after Bug/direct/app-server replay matrices pass.

### Task 5: Executor and Persistence Migration

**Files:**
- Modify: `lark_agent_bridge/app/request_exec.py`
- Modify: `lark_agent_bridge/app/progress_notify.py`
- Modify: `lark_agent_bridge/state.py`
- Modify: `lark_agent_bridge/replay.py`
- Test: `tests/test_conversation_persistence.py`

**Interfaces:**
- Persists: normalized request text, root ID, action/payload, resource provenance, and selected input source.

- [ ] Add RED restart tests where live events omit `root_id` and `reply_to` must be reconstructed.
- [ ] Make all executors consume the normalized root and request snapshot.
- [ ] Persist a versioned, desensitized conversation-input snapshot in activity/session state.
- [ ] Restore replay input from the snapshot first and retain legacy fallback for old sessions.
- [ ] Commit after restart and historical-session compatibility tests pass.

### Task 6: Remove Compatibility Fields

**Files:**
- Modify: `lark_agent_bridge/app/_shared.py`
- Modify: `lark_agent_bridge/app/handle_event.py`
- Modify: route and request mixins still reading duplicated `_RouteContext` fields.
- Test: all `tests/test_app_*.py`, `tests/test_agents_bug*.py`, parser, downloader, and replay tests.

**Interfaces:**
- Makes `ResolvedConversationInput` the only source for route text, follow-up context/root, and referenced resources.

- [ ] Add a temporary assertion proving legacy fields equal normalized values at every dispatch.
- [ ] Migrate remaining route handlers and remove duplicated context fields.
- [ ] Remove duplicate fetch/recovery helpers only after call-site search reaches zero.
- [ ] Run the affected suite, full suite where practical, `compileall`, and `git diff --check`.
- [ ] Restart the listener and run the real group combination matrix before closing the migration.
