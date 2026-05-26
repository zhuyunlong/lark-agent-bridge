# Custom Skill File-Agent Executor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the second-stage architecture so `custom_skill` can execute through a real file-capable Agent only when an explicit executor is configured, and final summarization only happens after a validated execution artifact exists.

**Architecture:** Keep the flow as `Classification -> Execution -> Summarization`. `custom_skill` remains `executor_not_ready` unless its route explicitly declares `executor: file_agent`. The file-agent execution step produces `custom_skill_analysis.md`, `bug_custom_skill_report.json`, and `bug_custom_skill_report.html`; summary consumes these artifacts only after structural evidence validation passes.

**Tech Stack:** Python stdlib, existing `BugAnalysisRunner`, `SkillManager`, report server admin API, `unittest`/`pytest`.

---

## Task 1: Executor Readiness Contract

**Files:**
- Modify: `lark_agent_bridge/skill_manager.py`
- Modify: `lark_agent_bridge/report_server.py`
- Modify: `lark_agent_bridge/templates/admin.html`
- Modify: `tests/test_skill_manager.py`
- Modify: `tests/test_report_server.py`
- Modify: `tests/test_agents.py`

- [x] Add route field `executor`, supporting `""` and `"file_agent"` only.
- [x] Keep `custom_skill` without executor as `bug_primary_unready`.
- [x] Mark `custom_skill` with `executor=file_agent` as `bug_primary_agent_ready` and report-card selectable.
- [x] Filter `supported_primary_bug_skills()` to return only skills that are actually selectable.
- [x] Add admin API/UI payload support for `executor`.

## Task 2: File-Agent Execution Artifact

**Files:**
- Modify: `lark_agent_bridge/agents/bug_runner.py`
- Modify: `tests/test_agents.py`

- [x] Add `_build_custom_skill_agent_command(...)`.
- [x] Add `_run_custom_skill_agent_analysis(...)`.
- [x] Add `_validate_custom_skill_analysis(...)`.
- [x] Agent execution must produce `custom_skill_analysis.md`.
- [x] Bridge must convert validated markdown into `bug_custom_skill_report.json` and `bug_custom_skill_report.html`.
- [x] Validation must require a non-empty `## 关键证据` section and must not use fragile marker counting.

## Task 3: Runtime Path Integration

**Files:**
- Modify: `lark_agent_bridge/agents/bug_runner.py`
- Modify: `tests/test_agents.py`

- [x] Initial bug analysis: if `custom_skill` has no executor, keep `custom_skill_executor_not_ready`; if it has `file_agent`, run execution artifact first.
- [x] Bug reanalysis: same execution rule, with reanalysis-specific error codes.
- [x] Direct analysis: same execution rule, with direct-analysis-specific error codes.
- [x] Final `_run_bug_agent_summary(...)` can run only after the artifact validation succeeds.

## Task 4: Group-Chat Regression

**Files:**
- Modify: `tests/test_app.py`

- [x] Keep the existing `6998107767` group regression proving LD custom skill does not emit fake conclusions.
- [x] Add a simulated group-chat path where a test custom skill is `file_agent` ready and the result is delivered only after execution artifact success.

## Task 5: Verification

Run:

```bash
PYTHONPATH=. pytest -q tests/test_agents.py -k "custom_skill or bug_agent_summary or direct_analysis or reanalysis"
PYTHONPATH=. pytest -q tests/test_app.py -k "custom_skill or group_bug"
PYTHONPATH=. pytest -q tests/test_skill_manager.py tests/test_report_server.py -k "skill"
PYTHONPATH=. python3 scripts/validate_refactor_scenarios.py
python3 -m py_compile lark_agent_bridge/agents/bug_runner.py lark_agent_bridge/skill_manager.py lark_agent_bridge/report_server.py
git diff --check -- lark_agent_bridge/agents/bug_runner.py lark_agent_bridge/skill_manager.py lark_agent_bridge/report_server.py lark_agent_bridge/templates/admin.html tests/test_agents.py tests/test_app.py tests/test_skill_manager.py tests/test_report_server.py docs/superpowers/plans/2026-05-26-custom-skill-file-agent-executor.md
```

## Completion Criteria

- [x] Existing LD route for `6998107767` remains `executor_not_ready` until explicitly configured.
- [x] Test custom skill with `executor=file_agent` runs through real Execution artifact before summary.
- [x] Empty/invalid `## 关键证据` blocks fail and skip summary.
- [x] Admin readiness distinguishes unready custom skills from file-agent-ready custom skills.
