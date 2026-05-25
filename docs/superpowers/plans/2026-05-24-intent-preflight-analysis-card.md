# Intent Preflight Analysis Card Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an intent-analysis card before analysis execution, auto-run high-confidence requests, and keep ambiguous file-backed requests inside an analysis clarification loop instead of falling back to OMLX chat.

**Architecture:** Reuse the existing status/result card surface instead of creating a new protocol. Add a lightweight preflight decision layer in `BridgeApp`, persist clarification options in activity-session details, and extend direct-analysis follow-up handling so text replies like `1`, `2`, `直接源码分析`, and `重新分析` can recover and rerun file-based analysis.

**Tech Stack:** Python, existing `BridgeApp` routing, `BugAnalysisRunner`, Feishu card builders, pytest/unittest.

---

### Task 1: Add intent-preflight regression tests

**Files:**
- Modify: `tests/test_app.py`
- Modify: `tests/test_agents.py`

- [ ] **Step 1: Write failing app-level tests for preflight and clarification**

Add tests that cover:
- initial file-backed request with explicit source clue sends intent-preflight card then runs direct analysis
- initial file-backed generic prompt returns clarification instead of immediate execution
- text follow-up `直接源码分析` on clarification context recovers direct analysis
- text follow-up `1` on clarification context maps to a selected skill / source strategy
- direct-analysis failure-card follow-up `重新分析` reruns direct analysis instead of falling to OMLX

- [ ] **Step 2: Run targeted tests to verify red state**

Run:
```bash
PYTHONPATH=. pytest -q tests/test_app.py -k "intent_preflight or clarification or direct_analysis_followup"
```

Expected: failures showing missing preflight metadata / wrong follow-up mode.

- [ ] **Step 3: Write failing direct-analysis override test**

Add a focused `tests/test_agents.py` case asserting `run_direct_analysis(...)` respects explicit `plans_override` when supplied.

- [ ] **Step 4: Run targeted direct-analysis test**

Run:
```bash
PYTHONPATH=. pytest -q tests/test_agents.py -k "plans_override and direct_analysis"
```

Expected: fail because `run_direct_analysis(...)` ignores override plans today.

### Task 2: Implement intent-preflight decision and initial card behavior

**Files:**
- Modify: `lark_agent_bridge/app.py`

- [ ] **Step 1: Add a lightweight preflight decision helper**

Implement helpers in `BridgeApp` to:
- detect explicit source-clue file requests
- detect generic file-backed prompts that need user direction
- build preflight metadata (`intent_label`, `confidence`, `strategy`, `reason`)

- [ ] **Step 2: Reuse `send_status_card(...)` for high-confidence preflight**

Before `_run_bug_request`, `_run_direct_analysis_request`, `_run_signal_request`, and `_run_perception_request` start execution, send/update a queued card that includes intent-analysis details.

- [ ] **Step 3: Return clarification result for low-confidence file-backed requests**

For ambiguous file-backed requests, return a `TaskResult` in `bug_clarification` mode with:
- `needs_user_direction = True`
- `supported_bug_skills`
- textual numbered options in `message`
- persisted option metadata in `details`

- [ ] **Step 4: Run focused app tests**

Run:
```bash
PYTHONPATH=. pytest -q tests/test_app.py -k "intent_preflight or needs_user_direction"
```

Expected: pass.

### Task 3: Add text-reply clarification option handling

**Files:**
- Modify: `lark_agent_bridge/app.py`

- [ ] **Step 1: Add clarification-option parsing**

Implement helpers that read persisted option metadata from the previous session and map:
- `1`, `2`, ...
- exact skill labels / names
- `直接源码分析`

to a structured choice.

- [ ] **Step 2: Handle clarification replies before generic follow-up chat**

In `_handle_followup(...)`, for file-backed clarification contexts without a bug URL:
- if the reply matches a clarification option, recover the original file request and continue analysis
- if the reply is a retry term (`重新分析` etc.), recover and rerun direct analysis
- if the reply is free-form clarification text, append it to the original request and continue direct analysis

- [ ] **Step 3: Preserve existing bug clarification behavior**

Keep bug-URL-backed clarification sessions on the existing bug reanalysis path; only divert no-bug-url file-backed clarification sessions to direct-analysis recovery.

- [ ] **Step 4: Run focused follow-up tests**

Run:
```bash
PYTHONPATH=. pytest -q tests/test_app.py -k "clarification and direct_analysis"
```

Expected: pass.

### Task 4: Add direct-analysis override support

**Files:**
- Modify: `lark_agent_bridge/agents/bug_runner.py`
- Modify: `lark_agent_bridge/app.py`

- [ ] **Step 1: Extend `run_direct_analysis(...)` signature**

Add optional parameters for:
- `plans_override`
- `classification_skill`
- `classification_source`
- `classification_reason`

Use override plans when provided instead of `classify_requests(...)`.

- [ ] **Step 2: Wire clarification choice execution into direct-analysis override**

When the user selects a skill or source-analysis strategy from clarification:
- translate the selection into `plans_override`
- call direct-analysis recovery with the override metadata

- [ ] **Step 3: Run focused agent/app tests**

Run:
```bash
PYTHONPATH=. pytest -q tests/test_agents.py tests/test_app.py -k "plans_override or direct_analysis_followup or select_source_analysis"
```

Expected: pass.

### Task 5: Full regression sweep for touched routing paths

**Files:**
- Verify only

- [ ] **Step 1: Run full targeted regression suite**

Run:
```bash
PYTHONPATH=. pytest -q tests/test_app.py tests/test_agents.py tests/test_lark_client.py tests/test_downloader.py tests/test_parser.py
```

Expected: all pass.

- [ ] **Step 2: Run diff hygiene**

Run:
```bash
git diff --check -- lark_agent_bridge/app.py lark_agent_bridge/agents/bug_runner.py tests/test_app.py tests/test_agents.py
```

Expected: no output.
