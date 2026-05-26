# Custom Skill Executor Readiness Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent bug-analysis primary skills that fall to `custom_skill` from publishing fake conclusions, and make executor readiness visible in runtime and admin UI.

**Architecture:** Keep the current bug-analysis model as `Classification -> Execution -> Summarization`. In this change, `custom_skill` without a concrete executor is classified as `executor_not_ready`, so Execution fails explicitly and Summarization is skipped. A later enhancement can add a file-capable Agent executor for `custom_skill`, but this stopgap must first eliminate false conclusions.

**Tech Stack:** Python stdlib, `unittest`/`pytest`, existing `BugAnalysisRunner`, `SkillManager`, report server admin API.

---

## Current Failure

For `https://project.feishu.cn/xpfailuremgmt/buglo/detail/6998107767`:

1. Bug route and intent succeeded.
2. Skill selection succeeded: `ld-lane-level-log-analysis-portable`.
3. `data/state/skill_routes.json` maps that skill to `kind: custom_skill`.
4. `custom_skill` has no built-in script executor.
5. Current code writes `custom_skill_overview`, fakes `CompletedProcess(returncode=0)`, then direct API summarizes the placeholder.
6. The user receives a conclusion even though logs were not analyzed.

Correct behavior for this stopgap:

> If a route lands on `custom_skill` and no real executor is configured, reply that the skill is selected but execution is not ready. Do not run final summary and do not publish a root-cause conclusion.

## Scope

In scope:
- Initial bug-link analysis.
- Bug reanalysis/follow-up.
- Direct file/log analysis.
- Admin skill management readiness state.
- Focused and broad regression tests, including simulated group-chat paths and the real `6998107767` boundary.

Out of scope for this immediate fix:
- Building the full file-capable Agent executor for `custom_skill`.
- Generating real LD lane-level HTML analysis from logs.
- Changing `data/state/skill_routes.json` manually.

---

## Task 1: Add Failing Runner Tests For Custom Skill Not Ready

**Files:**
- Modify: `tests/test_agents.py`

- [x] **Step 1: Add initial bug-analysis regression**

Add a test asserting that a selected primary `custom_skill` fails explicitly and does not call final summary.

Expected behavior:
- `result.success is False`
- `result.error_code == "custom_skill_executor_not_ready"`
- message contains `已命中专用 Skill` and `当前没有可执行分析器`
- `_run_bug_agent_summary` is not called

- [x] **Step 2: Add reanalysis regression**

Use an existing previous session with:

```python
"analysis_kinds": ["custom_skill"],
"analysis_skill": "ld-lane-level-log-analysis-portable",
"prepared_log_input": str(log_root),
"selected_log_input": str(log_root),
"target_time": "2026-05-22 19:46",
```

Expected behavior:
- `result.success is False`
- `result.error_code == "custom_skill_reanalysis_executor_not_ready"`
- no final Agent summary call

- [x] **Step 3: Add direct-analysis regression**

Call `run_direct_analysis(..., plans_override=[BugAnalysisPlan(kind="custom_skill")])`.

Expected behavior:
- `result.success is False`
- `result.error_code == "direct_custom_skill_executor_not_ready"`
- no final Agent summary call

- [x] **Step 4: Verify tests fail before production changes**

Run:

```bash
PYTHONPATH=. pytest -q tests/test_agents.py -k "custom_skill and executor_not_ready"
```

Expected: FAIL because current code treats `custom_skill` as successful.

---

## Task 2: Block Custom Skill Success In Runner

**Files:**
- Modify: `lark_agent_bridge/agents/bug_runner.py`

- [x] **Step 1: Add message helper**

Add a helper that produces a consistent user-facing failure:

```python
def _custom_skill_executor_not_ready_message(self, skill_name: str, *, selected_input: Path | None) -> str:
    log_note = f"\n日志输入已准备：`{selected_input}`" if selected_input else ""
    return (
        f"已命中专用 Skill `{skill_name}`，但当前没有可执行分析器，尚未执行实际日志分析。"
        f"{log_note}\n不会基于占位报告给出根因结论。请为该 Skill 配置脚本执行器或文件 Agent 执行器后重试。"
    )
```

- [x] **Step 2: Change initial bug-analysis branch**

In `run_bug_analysis()`, replace the `plan.kind == "custom_skill"` branch that calls `_write_custom_skill_bug_report(...)` with an explicit failure before `_run_bug_agent_summary(...)`.

Return through `_failure(...)` with:

```python
error_code="custom_skill_executor_not_ready"
```

Do not append `html_paths`, do not write `custom_skill_overview`, do not run final summary.

- [x] **Step 3: Change reanalysis branch**

In `run_bug_reanalysis()`, replace the `plan.kind == "custom_skill"` branch with:

```python
TaskResult(
    success=False,
    message=self._custom_skill_executor_not_ready_message(skill_name, selected_input=selected_input),
    job_id=job_id,
    job_dir=job_dir,
    command=command,
    duration_seconds=time.monotonic() - started,
    error_code="custom_skill_reanalysis_executor_not_ready",
    details={
        "mode": "bug_reanalysis",
        "analysis_kind": "custom_skill",
        "analysis_skill": skill_name,
        "custom_skill_analysis_status": "executor_not_ready",
        "selected_log_input": str(selected_input or ""),
        "prepared_log_input": str(prepared_input or ""),
    },
)
```

- [x] **Step 4: Change direct-analysis branch**

In `run_direct_analysis()`, replace the `current_plan.kind == "custom_skill"` branch with explicit failure:

```python
error_code="direct_custom_skill_executor_not_ready"
```

Do not run final summary.

- [x] **Step 5: Re-run focused runner tests**

Run:

```bash
PYTHONPATH=. pytest -q tests/test_agents.py -k "custom_skill and executor_not_ready"
```

Expected: PASS.

---

## Task 3: Mark Admin-Routed Custom Skills As Not Ready

**Files:**
- Modify: `lark_agent_bridge/skill_manager.py`
- Modify: `lark_agent_bridge/templates/admin.html` if needed for wording
- Modify: `tests/test_skill_manager.py`
- Modify: `tests/test_report_server.py`

- [x] **Step 1: Add skill-manager regression**

Update `test_skill_manager_lists_primary_and_custom_skills`:

When `manager.set_skill_route("custom-check", role="primary")` maps to `custom_skill`, expected:

```python
self.assertEqual(routed.route_status, "bug_primary_unready")
self.assertFalse(routed.selectable_in_report_card)
self.assertIn("没有可执行分析器", routed.routing_note)
```

Keep `manager.primary_skill_map()["custom-check"][0] == "custom_skill"` so the route remains visible to classification, but not selectable as ready.

- [x] **Step 2: Implement route metadata**

Change `_route_metadata(...)` so:

- built-in executable primary kinds remain `bug_primary` and selectable
- configured custom primary with `kind == custom_skill` becomes:

```python
("bug_primary_unready", "Bug 分析未就绪", False, "已归类到 Bug 分析，但当前没有可执行分析器")
```

This may require passing `kind` into `_route_metadata(...)`.

- [x] **Step 3: Update report-server/admin test expectations**

In `tests/test_report_server.py`, route `http-debug-skill` to primary with `kind=general` currently normalizes to `custom_skill`.

Expected after change:

```python
self.assertEqual(routed_skill["skill"]["route_status"], "bug_primary_unready")
self.assertFalse(routed_skill["skill"]["selectable_in_report_card"])
```

- [x] **Step 4: Re-run skill/admin tests**

Run:

```bash
PYTHONPATH=. pytest -q tests/test_skill_manager.py tests/test_report_server.py -k "skill"
```

Expected: PASS, except report-server environment-specific bind issues should be called out if they occur.

---

## Task 4: Add App-Level Simulated Group-Chat Regression

**Files:**
- Modify: `tests/test_app.py`

- [x] **Step 1: Add group-chat regression**

Use existing fake Lark helpers in `tests/test_app.py` to simulate a group message:

```text
@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6998107767 2026-05-22 19:46 退无图
```

Patch the bug runner to return a `TaskResult` with:

```python
success=False
error_code="custom_skill_executor_not_ready"
message="已命中专用 Skill `ld-lane-level-log-analysis-portable`，但当前没有可执行分析器..."
```

Expected:
- The group card/reply contains the not-ready message.
- It does not contain root-cause wording like `最可能原因` or `结论摘要` from a placeholder summary.

- [x] **Step 2: Run group-chat regression**

Run:

```bash
PYTHONPATH=. pytest -q tests/test_app.py -k "custom_skill_executor_not_ready or group_bug"
```

Expected: PASS.

---

## Task 5: Real Bug Boundary Test For 6998107767

**Files:**
- Prefer test fixture changes in `tests/test_agents.py` or a small read-only validation command.

- [x] **Step 1: Add a focused test using the real route shape**

Test must simulate:

```python
skill_routes: ld-lane-level-log-analysis-portable -> custom_skill
bug_url: https://project.feishu.cn/xpfailuremgmt/buglo/detail/6998107767
prompt: 2026-05-22 19:46 退无图
```

Expected:

```python
result.error_code == "custom_skill_executor_not_ready"
result.message contains "ld-lane-level-log-analysis-portable"
```

- [x] **Step 2: Read-only historical check**

Run:

```bash
jq '.mode, .source_matches, .analysis_skill' data/jobs/c71ed6c1bb6c4a2f47c14868f7d7d9b3/output/bug_custom_skill_report.json
```

This is only to document the old bad boundary. Do not overwrite historical job output.

---

## Task 6: Run Broad Verification

**Files:**
- No production edits.

- [x] **Step 1: Focused runner tests**

Run:

```bash
PYTHONPATH=. pytest -q tests/test_agents.py -k "custom_skill or reanalysis or direct_analysis or bug_agent_summary"
```

- [x] **Step 2: Simulated group and route tests**

Run:

```bash
PYTHONPATH=. pytest -q tests/test_app.py -k "group or bug or reanalysis or followup"
```

- [x] **Step 3: Generic scenario validation script**

Run:

```bash
PYTHONPATH=. python3 scripts/validate_refactor_scenarios.py
```

This script uses stubbed Agent summary and local validation scenarios. If existing scenario expectations assume `custom_skill` success, update the expectation to `executor_not_ready`.

- [x] **Step 4: Syntax/static sanity**

Run:

```bash
python3 -m py_compile lark_agent_bridge/agents/bug_runner.py lark_agent_bridge/skill_manager.py lark_agent_bridge/report_server.py
```

- [x] **Step 5: Diff hygiene**

Run:

```bash
git diff --check -- lark_agent_bridge/agents/bug_runner.py lark_agent_bridge/skill_manager.py lark_agent_bridge/templates/admin.html tests/test_agents.py tests/test_app.py tests/test_skill_manager.py tests/test_report_server.py
```

---

## Future Phase: Real File-Agent Executor For Custom Skill

Do this only after the stopgap is merged.

Rules:

- `custom_skill` may succeed only when a concrete executor exists.
- File-agent executor must use Codex/Claude or another tool-capable runtime, not direct API.
- It must read `SKILL.md`, required references, logs, bug metadata, and fault time.
- It must produce an execution artifact before final summary:
  - `custom_skill_analysis.md`
  - `bug_custom_skill_report.json`
  - `bug_custom_skill_report.html`
- Success validation should check markdown structure:
  - `## 关键证据` exists
  - evidence section has non-empty bullet/list/text content
  - no fragile marker like single letter `L`
- Direct API may summarize only this completed artifact, never the pre-execution overview.

---

## Completion Criteria

- `custom_skill` no longer publishes fake conclusions.
- `6998107767` route returns executor-not-ready instead of a root-cause summary.
- Admin UI no longer marks custom primary skills as report-card-ready unless they have an executor.
- Focused tests, group simulation tests, scenario script, syntax checks, and diff checks pass or any environment-specific failure is explicitly documented.
