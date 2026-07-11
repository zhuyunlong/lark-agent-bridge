# Group Bug/File Follow-up Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make group-chat Bug analysis, replied files, failed-analysis supplements, and later follow-ups behave consistently across ZIP and single-file inputs.

**Architecture:** Keep the existing router and execution paths. Fix each broken boundary in place: structured message resource extraction, follow-up payload recovery, explicit resource propagation for Bug requests, diagnostic-question routing, and conversation-root propagation.

**Tech Stack:** Python 3.13, dataclasses, unittest-style pytest tests, Lark message payloads.

## Global Constraints

- Preserve explicit user-entered `file_*` parsing; only fetched structured message payloads must stop mining arbitrary card text.
- A pure `continue` or `retry` control reuses the prior request; mixed control plus time, symptom, or direction preserves the complete follow-up text.
- When a Bug URL and replied group file coexist, Bug metadata remains authoritative and the replied file is the preferred log input.
- File diagnostics without an explicit action term must be narrowly routed; generic questions such as `这个是什么？` remain chat.
- Clarification and recovery results remain on the original conversation root.

---

### Task 1: Structured message resources

**Files:**
- Modify: `lark_agent_bridge/app/resources.py`
- Test: `tests/test_app_followup_reply.py`

- [x] Add a failing test with an interactive result card containing `file_uploading` and `file_uploaded`, followed by an original file message.
- [x] Verify the test fails because the card labels are returned as file resources.
- [x] Restrict bare resource-token parsing to resource message types while retaining structured `file_key`, `image_key`, folder fields, and XML resources.
- [x] Verify the result card yields no fake resource and the original replied file is recovered.

### Task 2: Follow-up payload and root continuity

**Files:**
- Modify: `lark_agent_bridge/app/followup_clarify.py`
- Modify: `lark_agent_bridge/app/context_from.py`
- Modify: `lark_agent_bridge/app/request_exec.py`
- Test: `tests/test_app_bug_followup.py`
- Test: `tests/test_app_followup_reply.py`

- [x] Add failing tests for pure `继续`, mixed `时间点...继续自主分析`, and recovered direct analysis root continuity.
- [x] Verify mixed follow-up text is currently discarded and the result root changes.
- [x] Add a narrow pure-control predicate and append all non-pure follow-up text to the stored request.
- [x] Thread optional `root_message_id` through direct-analysis and app-server progress, status, callbacks, controls, and delivery.
- [x] Pass the stored follow-up root from clarification, replay, app-server, and skill-confirmation recovery call sites.
- [x] Verify the focused tests pass.

### Task 3: Bug URL plus replied file

**Files:**
- Modify: `lark_agent_bridge/models.py`
- Modify: `lark_agent_bridge/app/request_build.py`
- Modify: `lark_agent_bridge/app/routes.py`
- Modify: `lark_agent_bridge/app/intent_dispatch.py`
- Modify: `lark_agent_bridge/app_server_investigation.py`
- Modify: `lark_agent_bridge/agents/bug/run_primary.py`
- Test: `tests/test_app_group_bug.py`
- Test: `tests/test_app_server_investigation_runner.py`
- Test: `tests/test_agents_bug_analysis.py`

- [x] Add failing normal-Bug and app-server tests proving replied resources are ignored when a Bug URL exists.
- [x] Add `resources` to `BugRequest` and merge referenced resources at the route boundary.
- [x] Download explicit group resources into the current job and select them before Bug attachments; keep Bug data fetching for metadata and use attachments as fallback.
- [x] Keep the chosen input and source observable in result details and progress data.
- [x] Verify normal Bug, app-server, time-clarification, and skill-confirmation paths preserve the replied resource.

### Task 4: Diagnostic questions without action words

**Files:**
- Modify: `lark_agent_bridge/parsing/analysis.py`
- Modify: `lark_agent_bridge/parsing/terms.py`
- Test: `tests/test_app_followup_reply.py`
- Test: `tests/test_parser.py`

- [x] Add a failing test for `时间点15:11左右 为啥退出有图进入了无图？` while replying to a file.
- [x] Keep the existing generic-question chat test as the negative boundary.
- [x] Add parser diagnostic-question terms and require a replied resource plus diagnostic context.
- [x] Verify the diagnostic question enters direct analysis and the generic question remains chat.

### Task 5: Verification and delivery

**Files:**
- Modify: `scripts/test_group_chat.py` only if a reusable safe combination case is missing.
- Update: `docs/superpowers/plans/2026-07-12-group-bug-file-followup-fixes.md`

- [x] Run the focused parser, follow-up, direct-analysis, group-Bug, app-server, downloader, and archive tests.
- [x] Run the complete affected app/Bug/parser/downloader slice and `git diff --check`.
- [x] Inspect the final diff for unrelated changes and confirm `.codegraph/daemon.pid` remains untouched.
- [x] Commit and push the verified fix according to the repository workflow.
- [x] Update the Obsidian project note and close the matching open loop.

Verification note: the affected slice completed with `541 passed, 28 subtests passed`. An unfiltered full-suite run was stopped after more than 12 minutes in an unrelated long-sleep test, so completion is based on the complete affected slice rather than a fresh full-suite result.
