# Analysis Follow-up Replay Compass Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every analysis follow-up correctly decide between existing-report Q&A and reanalysis, rebuild the full original context, recover prepared logs local-first, re-run intent recognition from the latest user correction, and execute the right analysis pipeline without mode-specific retry shortcuts.

**Architecture:** Add a single replay decision layer before deterministic routes. It builds an `AnalysisReplayContext` from reply-chain context, activity session details, bug metadata, previous artifacts, local prepared logs, current resources, history, and the current user message. A `ReplayPlan` then dispatches to small mode adapters (`bug`, `direct_analysis`, `signal_lifecycle`, `perception_summary`, `addr2line`, `rom_version_lookup`) that reuse existing runners; adapters execute but do not decide whether reanalysis is needed.

**Tech Stack:** Python 3.13 stdlib, existing `BridgeApp` routing, `ConversationContextStore`, `ActivityStore`, `IntentAnalysisRunner`, `BugAnalysisRunner`, existing signal/perception/direct runners, pytest/unittest.

---

## Compass

### Implementation Status

2026-05-31 Round 1 status:

- Completed: `lark_agent_bridge/replay.py` replay dataclasses and resource bundle normalization.
- Completed: `IntentAnalysisRunner.decide_replay(...)` replay LLM decision scaffolding and parser/prompt tests.
- Completed: critical red regression tests for signal/perception prepared-log replay and ordinary report questions.
- Completed: first app integration for `direct_analysis`, `signal_lifecycle`, and `perception_summary` replay gate before deterministic fresh routes.
- Completed: removed legacy `_maybe_handle_signal_perception_replay()` after replay gate replacement.
- Verified: targeted replay/follow-up routing subset passes after integration.
- Verified: full touched test suite `tests/test_app.py tests/test_parser.py tests/test_agents.py tests/test_replay.py` passes (`470 passed, 23 subtests passed`).
- Not complete yet: addr2line, rom lookup, and bug follow-up enrichment/migration to unified replay adapters.

### User Contract

The user-facing contract is:

- A follow-up that asks a question about the previous result should answer from the existing report/context without re-downloading or re-running.
- A follow-up that says `重新分析`, corrects the time, corrects the signal, changes the analysis direction, or provides missing information should re-run analysis.
- Reanalysis must use the correct context: original bug link, original bug title/body when available, previous downloaded/decrypted log directory, previous user requests, current user request, and reply-chain history.
- Reanalysis must validate resources: if local prepared logs exist, reuse them; if missing, retry download/decrypt through the original resource or bug link; if impossible, fail with a precise missing-resource/clarification result.
- Reanalysis must re-run intent recognition with the latest request as primary text, not blindly reuse the old mode/kind.
- Existing mode-specific runners and report generation should be reused; the fix is in routing, context, decision, and resource recovery.

### Current Problems Confirmed In Source

- `BridgeApp._dispatch_route()` is first-match-wins; deterministic signal/perception/direct routes can claim a follow-up before the late intent router has a chance to classify it.
- `_maybe_handle_signal_perception_replay()` only applies to `signal_lifecycle` and `perception_summary`, only when `parse_followup_action(route_content) == "retry"`, and rebuilds requests only from historical `request_text`.
- Signal fresh route can recover reply-chain resources via `_contextual_signal_resources()`, but signal replay bypasses that recovery.
- `ConversationContext` carries request/report/history, but not prepared logs or bug metadata; those live in activity-session details and job artifacts.
- `bug` follow-up has a richer decision and reanalysis path, but that behavior is not generalized to direct/signal/perception/addr2line/rom.
- `direct_analysis`, `addr2line`, and `rom_version_lookup` have their own retry branches, which duplicate replay semantics in route-local code.

### Keep vs Change

- Keep `PLAN_KIND_REGISTRY` in `bug_runner.py`: it is a valid capability table for bug analysis kinds, not accidental hardcoding.
- Keep existing runners and report pipelines: this plan does not rewrite signal/perception/direct/bug scripts.
- Keep bug-specific `decide_bug_followup()` initially, but call it from the unified replay layer so routing semantics are consistent.
- Change route precedence: follow-up replay decision runs before deterministic analysis routes.
- Change resource recovery: one shared local-first resolver is used by all replay adapters.
- Replace `_maybe_handle_signal_perception_replay()` with the unified replay pipeline. It may remain temporarily as a thin adapter only during migration, but must not own decision/resource policy after Task 5.

### Multi-Agent Execution Split

This plan is designed for parallel execution with disjoint write scopes:

- Agent A owns replay models/context/resource recovery in `lark_agent_bridge/replay.py` and focused unit tests.
- Agent B owns `BridgeApp` routing integration and migration/removal of local retry shortcuts in `lark_agent_bridge/app.py`.
- Agent C owns intent decision integration and bug-runner metadata/context improvements in `lark_agent_bridge/agents/intent_runner.py` and `lark_agent_bridge/agents/bug_runner.py`.
- Agent D owns adapter execution tests and real regression scenarios in `tests/test_app.py`, `tests/test_parser.py`, and any small fake-runner updates.
- Main integrator owns final conflict resolution, targeted verification, and cleanup.

Agents must not revert each other's edits. If a worker sees concurrent changes, it must re-read the touched file and adapt.

---

## File Map

- Create `lark_agent_bridge/replay.py`
  - Dataclasses: `AnalysisReplayContext`, `ReplayDecision`, `ReplayPlan`, `ReplayResourceBundle`.
  - Pure helpers for mode normalization, action normalization, and resource descriptors.
- Modify `lark_agent_bridge/app.py`
  - Build replay context before deterministic route dispatch.
  - Add early replay decision gate.
  - Move direct/signal/perception/addr2line/rom retry behavior into replay adapters.
  - Delete or thin out `_maybe_handle_signal_perception_replay()`.
- Modify `lark_agent_bridge/agents/intent_runner.py`
  - Add a replay-specific decision prompt or classify method that receives full replay context.
  - Keep existing general intent fallback behavior unchanged.
- Modify `lark_agent_bridge/agents/bug_runner.py`
  - Let bug follow-up decision consume richer context where available.
  - Ensure reanalysis classification considers original title/body, prior requests, current correction, prepared logs, and current resource status.
- Modify `tests/test_app.py`
  - Add end-to-end routing/resource/replay tests.
  - Update fakes so perception tests fail if resources are missing when the behavior requires resources.
- Modify `tests/test_parser.py`
  - Add only parser-level retry/correction terms if needed.
- Add `tests/test_replay.py`
  - Unit tests for replay context building and resource bundle normalization.

---

## Task 1: Add Replay Data Model And Pure Context Helpers

**Owner:** Agent A

**Files:**
- Create: `lark_agent_bridge/replay.py`
- Test: `tests/test_replay.py`

- [ ] **Step 1: Add failing tests for replay model normalization**

Create `tests/test_replay.py` with tests for:

```python
from pathlib import Path

from lark_agent_bridge.models import DownloadResource
from lark_agent_bridge.replay import (
    AnalysisReplayContext,
    ReplayDecision,
    ReplayPlan,
    ReplayResourceBundle,
    normalize_replay_mode,
)


def test_normalize_replay_mode_accepts_known_analysis_modes():
    assert normalize_replay_mode("bug_reanalysis") == "bug"
    assert normalize_replay_mode("bug_analysis") == "bug"
    assert normalize_replay_mode("direct_analysis") == "direct_analysis"
    assert normalize_replay_mode("signal_lifecycle") == "signal_lifecycle"
    assert normalize_replay_mode("perception_summary") == "perception_summary"
    assert normalize_replay_mode("addr2line_resolve") == "addr2line_resolve"
    assert normalize_replay_mode("rom_version_lookup") == "rom_version_lookup"


def test_resource_bundle_prefers_existing_local_paths(tmp_path):
    prepared = tmp_path / "prepared"
    selected = tmp_path / "selected"
    prepared.mkdir()
    selected.mkdir()
    missing = tmp_path / "missing"

    bundle = ReplayResourceBundle.from_candidates(
        current=[],
        reply_chain=[],
        session=[
            DownloadResource(kind="local", value=str(missing)),
            DownloadResource(kind="local", value=str(prepared)),
            DownloadResource(kind="local", value=str(selected)),
        ],
    )

    assert [Path(item.value) for item in bundle.local_existing] == [
        prepared.resolve(),
        selected.resolve(),
    ]
    assert bundle.best_effort
    assert bundle.missing_local_values == [str(missing)]
```

- [ ] **Step 2: Implement `lark_agent_bridge/replay.py`**

Add:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .models import DownloadResource

ReplayMode = Literal[
    "bug",
    "direct_analysis",
    "signal_lifecycle",
    "perception_summary",
    "addr2line_resolve",
    "rom_version_lookup",
    "unsupported",
]

ReplayAction = Literal[
    "answer_from_existing",
    "reanalyze",
    "clarify",
    "new_request",
    "unsupported",
]


def normalize_replay_mode(mode: str) -> ReplayMode:
    value = (mode or "").casefold()
    if "bug" in value:
        return "bug"
    if value == "direct_analysis":
        return "direct_analysis"
    if value == "signal_lifecycle":
        return "signal_lifecycle"
    if value == "perception_summary":
        return "perception_summary"
    if value == "addr2line_resolve":
        return "addr2line_resolve"
    if value == "rom_version_lookup":
        return "rom_version_lookup"
    return "unsupported"


@dataclass(slots=True)
class ReplayResourceBundle:
    current: list[DownloadResource] = field(default_factory=list)
    reply_chain: list[DownloadResource] = field(default_factory=list)
    session: list[DownloadResource] = field(default_factory=list)
    local_existing: list[DownloadResource] = field(default_factory=list)
    remote: list[DownloadResource] = field(default_factory=list)
    missing_local_values: list[str] = field(default_factory=list)

    @classmethod
    def from_candidates(
        cls,
        *,
        current: list[DownloadResource],
        reply_chain: list[DownloadResource],
        session: list[DownloadResource],
    ) -> "ReplayResourceBundle":
        ordered = [*current, *reply_chain, *session]
        local_existing: list[DownloadResource] = []
        remote: list[DownloadResource] = []
        missing_local_values: list[str] = []
        seen: set[tuple[str, str]] = set()
        for item in ordered:
            key = (item.kind, item.value)
            if key in seen:
                continue
            seen.add(key)
            if item.kind == "local":
                path = Path(item.value).expanduser()
                if path.exists():
                    local_existing.append(
                        DownloadResource(
                            kind="local",
                            value=str(path.resolve()),
                            source_message_id=item.source_message_id,
                            display_name=item.display_name,
                        )
                    )
                else:
                    missing_local_values.append(item.value)
            else:
                remote.append(item)
        return cls(
            current=current,
            reply_chain=reply_chain,
            session=session,
            local_existing=local_existing,
            remote=remote,
            missing_local_values=missing_local_values,
        )

    @property
    def best_effort(self) -> list[DownloadResource]:
        return [*self.local_existing, *self.remote]


@dataclass(slots=True)
class AnalysisReplayContext:
    root_message_id: str
    chat_id: str
    mode: ReplayMode
    previous_mode: str
    original_request_text: str
    current_text: str
    history: list[dict[str, str]]
    summary_text: str = ""
    report_excerpt: str = ""
    report_url: str = ""
    bug_url: str = ""
    bug_title: str = ""
    bug_description: str = ""
    previous_session: dict[str, object] = field(default_factory=dict)
    resources: ReplayResourceBundle = field(default_factory=ReplayResourceBundle)


@dataclass(slots=True)
class ReplayDecision:
    action: ReplayAction
    mode: ReplayMode
    reason: str
    confidence: str = "medium"
    analysis_kind: str = ""
    skill_name: str = ""
    signal_hint: str = ""
    retry_download_if_missing: bool = False
    normalized_request_text: str = ""


@dataclass(slots=True)
class ReplayPlan:
    decision: ReplayDecision
    context: AnalysisReplayContext
```

- [ ] **Step 3: Run replay unit tests**

Run:

```bash
PYTHONPATH=. python -m pytest tests/test_replay.py -q
```

Expected: pass.

---

## Task 2: Build Replay Context In BridgeApp

**Owner:** Agent A

**Files:**
- Modify: `lark_agent_bridge/app.py`
- Test: `tests/test_app.py`
- Test: `tests/test_replay.py`

- [ ] **Step 1: Add app-level failing tests for context completeness**

Add tests that create an activity session with:

- `details["bug_url"]`
- `details["prepared_log_input"]`
- `details["selected_log_input"]`
- `details["user_request_text"]`
- a `ConversationContext` with report summary/history

The test should call a new private helper such as `_build_analysis_replay_context(event, route_content, followup_context, referenced_resources)` and assert:

- `ctx.bug_url` is populated from details or original request.
- `ctx.resources.local_existing` contains the prepared directory.
- `ctx.original_request_text` comes from `details["user_request_text"]` before `ConversationContext.request_text`.
- `ctx.current_text` equals the current follow-up message.

- [ ] **Step 2: Implement `_build_analysis_replay_context(...)`**

Add imports:

```python
from .replay import AnalysisReplayContext, ReplayResourceBundle, normalize_replay_mode
```

Implement helper in `BridgeApp` near existing follow-up/context helpers:

- Read `previous_session = self.activity_store.get_session(followup_context.root_message_id) or {}`.
- Read `details = previous_session.get("details") if isinstance(..., dict) else {}`.
- Original request priority:
  - `details["user_request_text"]`
  - `followup_context.request_text`
  - previous session `content`
- Bug URL priority:
  - `details["bug_url"]`
  - `_bug_url_from_request_text(original_request_text)`
- Resources:
  - `current = referenced_resources`
  - `reply_chain = self._reference_chain_log_resources(event)`
  - `session = self._log_resources_from_session(previous_session)`
  - `ReplayResourceBundle.from_candidates(...)`
- Bug title:
  - If previous session has `job_dir`, read `job_dir/output/bug_metadata.md` using existing bug runner helper if reachable, or add a small app helper that extracts `- 标题:`.
- Bug description:
  - If `bug_metadata.md` contains the `缺陷描述` fenced block, extract a clipped version.

- [ ] **Step 3: Ensure helper is side-effect light**

The helper must not:

- mark events seen
- send cards
- download files
- mutate conversation store

- [ ] **Step 4: Run focused tests**

Run:

```bash
PYTHONPATH=. python -m pytest tests/test_replay.py tests/test_app.py -k "replay_context" -q
```

Expected: pass.

---

## Task 3: Add Unified Replay Decision Gate Before Deterministic Routes

**Owner:** Agent B

**Files:**
- Modify: `lark_agent_bridge/app.py`
- Test: `tests/test_app.py`

- [ ] **Step 1: Add failing route precedence tests**

Add tests for:

- A reply-chain signal follow-up `132003 重新分析` does not get treated as a fresh signal request without previous resources.
- A reply-chain signal follow-up `重新分析` with previous prepared logs reaches replay execution, not OMLX chat.
- A non-reply group message containing a signal is still routed as a new signal only when addressed and allowed.

- [ ] **Step 2: Add `_route_analysis_replay_decision(ctx)` as first route handler**

Insert this handler before `_route_bug_followup` in `_dispatch_route()`.

The handler should:

- return `None` when `ctx.followup_context is None`
- return `None` when `normalize_replay_mode(ctx.followup_context.mode) == "unsupported"`
- build replay context using Task 2 helper
- call `_decide_analysis_replay(replay_context)`
- dispatch via `_execute_analysis_replay_plan(...)`
- return `None` only if the replay decision is explicitly `new_request`

- [ ] **Step 3: Add deterministic fallback decision**

Implement `_decide_analysis_replay(...)` with this order:

1. If bug mode, delegate to existing bug path through a bug-specific bridge helper.
2. If `parse_followup_action(current_text) == "retry"`, return `ReplayDecision(action="reanalyze", ...)`.
3. If current text contains correction markers like `修正`, `更正`, `改成`, `问题时间`, `signal`, `SIGNAL_`, or a new explicit signal code and the previous mode is analysis-capable, return `reanalyze`.
4. If current text is a normal question and report/summary exists, return `answer_from_existing`.
5. Otherwise return `clarify` or defer to LLM decision from Task 4 when available.

This fallback is only a safe backup. Task 4 adds LLM decision for ambiguous cases.

- [ ] **Step 4: Preserve existing bug behavior during migration**

For bug mode, the initial implementation may call existing `_handle_followup(...)` to preserve current behavior. Do not duplicate `run_bug_reanalysis(...)` in this task.

- [ ] **Step 5: Run focused route tests**

Run:

```bash
PYTHONPATH=. python -m pytest tests/test_app.py -k "signal_followup or perception_followup or replay_route_precedence" -q
```

Expected: pass, with old behavior preserved where not migrated yet.

---

## Task 4: Add Replay-Specific LLM Decision

**Owner:** Agent C

**Files:**
- Modify: `lark_agent_bridge/agents/intent_runner.py`
- Modify: `lark_agent_bridge/app.py`
- Test: `tests/test_app.py`
- Test: `tests/test_parser.py`

- [ ] **Step 1: Add tests with fake replay decisions**

Extend the fake intent runner or add a fake replay decision runner so tests can force:

- `answer_from_existing`
- `reanalyze`
- `clarify`
- `new_request`

Assert `BridgeApp` respects the decision and records `classification_source` or equivalent debug details.

- [ ] **Step 2: Add `IntentAnalysisRunner.decide_replay(...)`**

Signature:

```python
def decide_replay(self, *, context: AnalysisReplayContext) -> ReplayDecision:
```

Prompt payload must include:

- previous mode
- original request
- current user text
- history
- summary/report excerpt
- report URL
- bug URL
- bug title
- bug description clipped
- prepared/selected/local resource descriptors
- remote resource descriptors
- missing local resource descriptors

Prompt rules:

- Latest user text has priority for corrections.
- Choose `answer_from_existing` only when the user is asking about existing conclusion/evidence.
- Choose `reanalyze` when user says retry/reanalyze, corrects time, corrects signal, changes target, asks to regenerate report, or provides missing input.
- Choose `new_request` only when the message clearly starts an unrelated analysis.
- Choose `clarify` when reanalysis is requested but essential information is missing and cannot be recovered.

- [ ] **Step 3: Wire LLM decision into `_decide_analysis_replay(...)`**

Use LLM decision when `self.intent_runner.is_enabled()` is true.

Fallback behavior:

- If LLM fails, log progress and use deterministic fallback from Task 3.
- Do not fail the user request only because replay LLM failed.

- [ ] **Step 4: Keep bug-specific decision as source of skill selection**

When replay mode is bug and action is reanalysis:

- Call existing `_bug_reanalysis_decision(...)`.
- Let bug decision decide `plans_override`, skill, provider, and force rerun.
- Do not let generic replay LLM override bug skill selection unless it has explicit support for bug plans.

- [ ] **Step 5: Run decision tests**

Run:

```bash
PYTHONPATH=. python -m pytest tests/test_app.py tests/test_parser.py -k "replay_decision or followup_action" -q
```

Expected: pass.

---

## Task 5: Implement Replay Execution Adapters

**Owner:** Agent B

**Files:**
- Modify: `lark_agent_bridge/app.py`
- Test: `tests/test_app.py`

- [ ] **Step 1: Add `_execute_analysis_replay_plan(...)`**

Dispatch by `plan.decision.mode`:

- `bug` -> existing bug follow-up/reanalysis path
- `direct_analysis` -> direct replay adapter
- `signal_lifecycle` -> signal replay adapter
- `perception_summary` -> perception replay adapter
- `addr2line_resolve` -> addr2line replay adapter
- `rom_version_lookup` -> rom replay adapter

If action is `answer_from_existing`, use existing context chat or bug existing-answer path.

- [ ] **Step 2: Direct replay adapter**

Build a `DirectAnalysisRequest` using:

- original request plus `追问/修正：{current_text}` when action is reanalysis and current text is not plain retry
- `ReplayResourceBundle.best_effort`
- existing `_build_direct_analysis_request(...)`

Then call `_run_direct_analysis_request(...)`.

- [ ] **Step 3: Signal replay adapter**

Build signal text from:

- `decision.signal_hint`
- current text
- original request

Parse with `parse_signal_request(...)`.

Resource order:

- `ReplayResourceBundle.best_effort`
- if empty, return a precise missing-log result with `details["resource_recovery"]`.

Then call `_run_signal_request(...)`.

- [ ] **Step 4: Perception replay adapter**

Build prompt from original request plus current correction when useful.

Use `ReplayResourceBundle.best_effort`.

If resources are empty, return `missing_log` before calling fake/real runner, so tests cannot pass with an empty-resource fake.

Then call `_run_perception_request(...)`.

- [ ] **Step 5: Addr2line/ROM adapters**

Move the logic currently in `_followup_addr2line_request(...)` and `_followup_rom_lookup_request(...)` behind replay execution.

Keep existing functions temporarily as wrappers if needed, but route-level retry should call the adapter.

- [ ] **Step 6: Run adapter tests**

Run:

```bash
PYTHONPATH=. python -m pytest tests/test_app.py -k "direct_analysis_followup or signal_followup or perception_followup or addr2line_followup or rom_lookup_followup" -q
```

Expected: pass.

---

## Task 6: Remove Local Retry Shortcuts After Migration

**Owner:** Agent B

**Files:**
- Modify: `lark_agent_bridge/app.py`
- Test: `tests/test_app.py`

- [ ] **Step 1: Delete or thin `_maybe_handle_signal_perception_replay()`**

Preferred final state:

- Delete `_maybe_handle_signal_perception_replay()`.
- Remove its call from `_handle_followup()`.

Acceptable transitional state:

- Function only calls the unified replay adapter and contains no mode/action/resource policy.

- [ ] **Step 2: Reduce `_maybe_handle_direct_analysis_followup()` scope**

Keep only:

- clarification option selection
- explicit skill/source selection from previous clarification card

Move generic retry/continue replay into the unified replay gate.

- [ ] **Step 3: Stop route-local addr2line/rom retry from bypassing replay**

Either remove `_followup_addr2line_request()` / `_followup_rom_lookup_request()` from route handlers or make them call the replay adapter.

- [ ] **Step 4: Ensure `_route_intent_router` remains fallback**

The general intent router should still handle new unresolved tasks. It should not be the only place where follow-up replay gets classified.

- [ ] **Step 5: Run shortcut regression tests**

Run:

```bash
PYTHONPATH=. python -m pytest tests/test_app.py -k "followup or replay or retry" -q
```

Expected: pass.

---

## Task 7: Improve Bug Reanalysis Context Completeness

**Owner:** Agent C

**Files:**
- Modify: `lark_agent_bridge/agents/bug_runner.py`
- Modify: `lark_agent_bridge/app.py`
- Test: `tests/test_app.py`
- Test: `tests/test_agents.py`

- [ ] **Step 1: Add failing test for bug title/body in follow-up decision payload**

Use a fake bug decision agent that captures prompt JSON. Assert it receives:

- original bug request
- current follow-up text
- bug title
- bug description
- prepared log input
- selected log input
- previous history or report excerpt

- [ ] **Step 2: Extend bug follow-up decision payload**

Modify `decide_bug_followup(...)` or add a wrapper so it can receive optional:

```python
bug_title: str = ""
bug_description: str = ""
history: list[dict[str, str]] | None = None
resource_status: dict[str, object] | None = None
```

Keep default values so existing callers do not break.

- [ ] **Step 3: Pass replay context into bug decision**

When unified replay gate handles bug mode, pass title/body/resource status from `AnalysisReplayContext`.

- [ ] **Step 4: Reclassification rule**

For bug reanalysis, ensure the effective classification text is:

- original request
- bug title
- bug description
- prior user messages
- current follow-up text as highest-priority correction

If existing internals already do part of this, add test coverage and avoid duplicate prompt text.

- [ ] **Step 5: Run bug tests**

Run:

```bash
PYTHONPATH=. python -m pytest tests/test_app.py tests/test_agents.py -k "bug_followup or bug_reanalysis or bug_decision" -q
```

Expected: pass.

---

## Task 8: Add Critical End-To-End Regression Cases

**Owner:** Agent D

**Files:**
- Modify: `tests/test_app.py`
- Modify: `tests/test_parser.py`

- [ ] **Step 1: Update `FakePerceptionRunner`**

Make it mirror real runner resource requirements for tests that expect execution:

```python
if not request.resources:
    return TaskResult(
        success=False,
        message="缺少日志",
        error_code="missing_log",
        details={"mode": "perception_summary"},
    )
```

If existing tests rely on resource-less perception success, update those tests to pass explicit resources.

- [ ] **Step 2: Add signal prepared-log replay test**

Scenario:

- Previous signal session has `request_text="132002"` without URL.
- Activity result details contain existing `prepared_log_input` and `selected_log_input`.
- User replies `重新分析`.

Expected:

- signal handler called once
- request signal is `132002`
- request resources include the local prepared dir
- result succeeds

- [ ] **Step 3: Add perception prepared-log replay test**

Same structure as signal, but mode is `perception_summary`.

Expected:

- perception runner called once
- request resources include local prepared dir
- result succeeds

- [ ] **Step 4: Add answer-from-existing test**

Scenario:

- Previous signal/perception report has summary/report excerpt.
- User asks `这个结论什么意思`.

Expected:

- no runner call
- chat/context reply called
- details indicate existing-context answer or context chat

- [ ] **Step 5: Add current correction test**

Scenario:

- Previous signal request was `132002`.
- User replies `改成 132003 重新分析`.

Expected:

- replay adapter parses `132003`
- resources are inherited from previous session
- runner called with signal `132003`

- [ ] **Step 6: Add route-precedence test**

Scenario:

- User replies in an existing signal thread with a restated signal and no new resources.

Expected:

- replay gate handles it using inherited resources
- `_route_signal_request` does not create a fresh missing-log result

- [ ] **Step 7: Run critical test subset**

Run:

```bash
PYTHONPATH=. python -m pytest tests/test_app.py tests/test_parser.py -k "signal_followup or perception_followup or replay or followup_action" -q
```

Expected: pass.

---

## Task 9: Final Integration, Cleanup, And Verification

**Owner:** Main integrator

**Files:**
- Verify all touched files.

- [ ] **Step 1: Review concurrent changes**

Run:

```bash
git status --short
git diff -- lark_agent_bridge/app.py lark_agent_bridge/replay.py lark_agent_bridge/agents/intent_runner.py lark_agent_bridge/agents/bug_runner.py tests/test_app.py tests/test_replay.py tests/test_parser.py
```

Expected:

- No unrelated files changed.
- No worker reverted another worker's edits.
- `_maybe_handle_signal_perception_replay()` no longer owns policy.

- [ ] **Step 2: Run targeted full follow-up suite**

Run:

```bash
PYTHONPATH=. python -m pytest tests/test_app.py tests/test_replay.py tests/test_parser.py -k "followup or replay or retry or signal or perception or direct_analysis_failure_card" -q
```

Expected: pass.

- [ ] **Step 3: Run broader routing regression**

Run:

```bash
PYTHONPATH=. python -m pytest tests/test_app.py tests/test_parser.py tests/test_agents.py -q
```

Expected: pass. If this is too slow or fails outside touched areas, record the exact failing tests and run the smallest meaningful subset that covers changed behavior.

- [ ] **Step 4: Diff hygiene**

Run:

```bash
git diff --check
```

Expected: no output.

- [ ] **Step 5: Requirement closeout**

Check the final implementation against these requirements:

- Follow-up distinguishes existing-report Q&A vs reanalysis.
- Reanalysis uses original bug link, downloaded/decrypted logs, history, current request.
- Intent recognition reruns with title/body/history/current correction when available.
- Missing logs trigger local-first recovery before download retry.
- Signal/perception/direct/addr2line/rom do not rely on isolated retry shortcuts.
- No production hardcoded signal names, bug IDs, paths, host lists, or default mappings were introduced.

---

## Parallel Execution Order

### Round 1: Independent Foundations

Run in parallel:

- Agent A: Task 1 and Task 2.
- Agent C: Task 4 prompt/decision design tests, without touching `app.py` until Agent A lands replay model.
- Agent D: Task 8 test design, using skipped/failing tests if implementation is not ready.

Main integrator:

- Reviews Task 1/2 APIs.
- Avoids editing `app.py` until Agent A finishes context helper.

### Round 2: Routing And Adapters

Run in parallel:

- Agent B: Task 3 and Task 5 in `app.py`.
- Agent C: Task 7 in `bug_runner.py`.
- Agent D: Expand regression tests from Task 8 after adapter APIs settle.

Main integrator:

- Resolves `app.py` conflicts.
- Ensures replay gate does not regress group mention policy.

### Round 3: Cleanup

Run in parallel only if no merge conflicts remain:

- Agent B: Task 6 cleanup.
- Agent D: Regression stabilization.

Main integrator:

- Runs Task 9 verification.
- Performs requirement closeout.

---

## Known Risks

- `app.py` is large and routing-sensitive. Only one worker should own production edits in `app.py` at a time; other agents should contribute tests or clearly bounded helpers.
- `IntentAnalysisRunner` may be unavailable in some runtime configs. Replay must have deterministic fallback and should not fail solely because LLM decision fails.
- Bug title/body recovery from `bug_metadata.md` is best-effort unless the original bug fetch payload is persisted in session details. If exact body is unavailable, replay decision must mark it missing instead of fabricating it.
- Some existing tests use fakes that are more permissive than real runners. Fake runner behavior must be tightened where it hides missing-resource bugs.
- Existing direct/addr2line/rom retry behavior is useful. Migrate it, do not delete semantics.

---

## Completion Criteria

- The plan is implemented with replay decision before deterministic analysis routes.
- `_maybe_handle_signal_perception_replay()` is removed or policy-free.
- Signal and perception can replay from previous prepared local logs when current text is only `重新分析`.
- Current correction such as `改成 132003 重新分析` changes the executed request.
- Ordinary questions about prior reports do not run analysis.
- Bug reanalysis still works and has richer decision context.
- Targeted tests and diff hygiene pass.
