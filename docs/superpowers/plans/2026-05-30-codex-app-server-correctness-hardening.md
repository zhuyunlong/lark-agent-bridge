# Codex App-Server Correctness Hardening Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the correctness and maintainability gaps in the already-shipped `codex app-server` source-stage runtime (commit `1219603`) without regressing the runtime/reanalysis fixes added afterward (`d67f265`, `d264efe`). Specifically: stop user config from being silently overridden, stop timeout/interrupt turns from being treated as complete results, stop long-reasoning turns from being killed by the post-tool quiet timeout, and converge the scattered env/CODEX_HOME/context logic into explicit, testable policies.

**Architecture:** Keep `codex exec` fallback and the disabled-by-default posture untouched. Introduce a single `build_codex_app_server_execution_policy()` that is the only place env/CODEX_HOME/cwd/runtime-flags are decided. Replace the implicit "ok + two headings" completion judgment with an explicit `completion_state` enum on `CodexAppServerResult`, and gate `source_stage_direct` reuse on `completion_state == complete` only. Replace the single post-tool quiet timer with a progress-aware stall detector. Move source-stage followup/cwd/priority/prompt logic behind a `SourceStageContextPolicy` registry, preserving the `d67f265` original-request-text behavior. The progress-card stream throttle moves from a string-suffix convention to an explicit stage set.

**Tech Stack:** Python 3.13 stdlib (`subprocess`, `threading`, `queue`, `enum`, `shutil`, `pathlib`), existing `BridgeConfig` / `CodexAppServerOptions` / `BugAnalysisRunner` / `CodexAppServerRuntime`, `lark_agent_bridge.token_usage`, `pytest`/`unittest.mock`.

---

## Context

Baseline reality (verified against current `HEAD`):

- The codex app-server runtime is **already implemented and shipped** (`1219603 feat: stabilize codex app-server source-stage runtime`). This plan is a follow-up hardening pass, not the original integration.
- Two later commits touch the same files and constrain this plan:
  - `d67f265 fix: harden bug reanalysis and codegraph warmup` added `BugAnalysisRunner._strip_bug_followup_suffix()` (bug_runner.py:1134) and `_original_bug_request_text()` (bug_runner.py:1142) to **preserve the original bug request text across follow-up reanalysis**. Any context-policy refactor (Task 5) must route through these helpers, not bypass them.
  - `d67f265` also added `lark_agent_bridge/token_usage.py` with `normalize_token_usage()` / `coerce_token_count()`. App-server usage reporting (Task 4) should normalize through this module instead of returning raw `thread/tokenUsage` shapes.
  - `d264efe` reworked `source_investigation.py` codegraph warmup; it does not change the app-server path but shares the source-investigation surface, so Task 5 must not reintroduce duplicate warmups.

Current problem anchors (verified line numbers at plan authoring time; re-confirm before editing):

1. `lark_agent_bridge/agents/bug_runner.py:9960` — `disable_node_repl = False` silently overrides the user's `disable_node_repl` config whenever `use_minimal_home` prepares a home (bug_runner.py:9956-9960).
2. `lark_agent_bridge/agents/bug_runner.py:9952-9953` — env is built by clearing proxies via `build_internal_network_env(...)` then re-adding them via `_merge_codex_app_server_proxy_env(...)` ("先清后加").
3. `lark_agent_bridge/agents/codex_app_server_runtime.py:694` — `_looks_like_complete_agent_markdown()` only checks that `## 结论摘要` and `## 关键证据` substrings exist; used as an early-complete judge at runtime lines 511 and 603.
4. `lark_agent_bridge/agents/codex_app_server_runtime.py:507-519` — post-tool quiet timeout interrupts a turn when `last_tool_completion_at` is stale, with no credit for ongoing reasoning/token/stderr progress.
5. `lark_agent_bridge/agents/bug_runner.py:3260` — `use_source_stage_direct_reply` reuses the app-server markdown as the final answer and skips the summary stage; today it only requires `ok` + `_validate_custom_skill_analysis` (bug_runner.py:11588) non-empty `## 关键证据`, so a timeout-truncated body with a non-empty evidence section can still be served.
6. `lark_agent_bridge/agents/bug_runner.py:9826` — `_prepare_codex_app_server_minimal_home()` writes a shared home and hardcodes the model fallback `gpt-5.4` borrowed from `bug_analysis.model`; `CodexAppServerOptions` (models.py:180) has no `model` field.
7. `lark_agent_bridge/app.py:1637-1654` — progress-card stream throttle keys off `stage.endswith("_stream")`.

Non-goals:

- Do not remove or alter the `codex exec` fallback contract.
- Do not enable app-server by default or change `[codex_app_server]` defaults' on/off posture.
- Do not implement Phase 2 session/thread reuse (separate future plan).
- Do not change the 5s stream-throttle value or the "full progress still persisted" behavior; only change how throttled stages are identified.

## Decision Log (resolves open questions from review)

- **disable_node_repl (Task 1):** Root cause must be confirmed first. The likely reason the override exists is that the minimal home `config.toml` has no `[mcp_servers.node_repl]` section, so emitting `-c mcp_servers.node_repl.enabled=false` may be redundant or rejected. Resolution: the policy decides whether to *emit the flag*, while the user's `disable_node_repl` value is always honored semantically. Never flip the boolean behind the user's back.
- **completion_state partial (Task 2):** `partial` must **downgrade to the normal summary path**, not hard-fail. The user still gets an answer; it just won't be a raw direct-reply reuse. `failed` is reserved for `turn/completed status!=completed`, subprocess exit, and empty output.
- **stall detection (Task 3):** Whether app-server emits events during long internal reasoning is **unverified**. Task 3 implements the progress-aware detector AND Task 6 captures a real reasoning-heavy event trace to confirm the assumption; if reasoning is truly silent, the fix degrades to a longer `no_event_timeout` rather than a smarter one.
- **env=spawn_env or None:** Downgraded from "critical bug" to "harden in passing" — current `setdefault("RUST_LOG", ...)` keeps it non-empty. Fixed for free inside the Task 1 policy (always pass an explicit dict).

## File Map

- Modify: `lark_agent_bridge/models.py`
  - Add `CodexAppServerOptions.model: str = ""`; add stall-timeout knobs (`no_event_timeout_seconds`, `no_output_timeout_seconds`).
- Modify: `lark_agent_bridge/config.py`
  - Load the new fields with safe defaults.
- Modify: `lark_agent_bridge/agents/codex_app_server_runtime.py`
  - Add `CompletionState` enum + `completion_state` on `CodexAppServerResult`; replace early-complete heuristic; replace post-tool quiet timer with progress-aware stall detection.
- Modify: `lark_agent_bridge/agents/bug_runner.py`
  - Add `build_codex_app_server_execution_policy()`; consume `completion_state` for `use_source_stage_direct_reply`; template+per-run CODEX_HOME; `SourceStageContextPolicy` registry; normalize usage via `token_usage`.
- Modify: `lark_agent_bridge/app.py`
  - Replace `_stream` suffix throttle check with an explicit throttled-stage set.
- Modify: `config/config.example.toml`, `docs/configuration.md`
  - Document new fields and the policy/state model.
- Tests: `tests/test_codex_app_server_runtime.py`, `tests/test_agents.py`, `tests/test_app.py`, `tests/test_config.py`.

---

## Task 1: App-Server Execution Policy Independence (correctness)

Collapse the scattered env / CODEX_HOME / `disable_node_repl` / proxy logic at bug_runner.py:9952-9960 into one pure function so user config is authoritative and env is always explicit.

**Files:** Modify `lark_agent_bridge/agents/bug_runner.py`, `tests/test_agents.py`.

- [ ] **Step 1: Confirm root cause of the `disable_node_repl = False` override**

Read git history for the override before changing it:

```bash
git log -S "disable_node_repl = False" -- lark_agent_bridge/agents/bug_runner.py
git show 1219603 -- lark_agent_bridge/agents/bug_runner.py | grep -n -B3 -A8 "disable_node_repl = False"
```

Record the finding inline in the test docstring (Step 2). If the reason is "minimal home config has no node_repl section", the policy must omit the `-c mcp_servers.node_repl.enabled=false` flag in minimal-home mode WITHOUT changing the user's configured value.

- [ ] **Step 2: Write failing test that user `disable_node_repl=True` is honored under minimal home**

Add to `tests/test_agents.py`:

```python
def test_app_server_policy_honors_disable_node_repl_under_minimal_home(self):
    # Regression: 1219603 flipped disable_node_repl to False whenever
    # use_minimal_home prepared a home, silently overriding user config.
    with tempfile.TemporaryDirectory() as tmp:
        config = BridgeConfig(data_dir=Path(tmp), workspace_root=Path(tmp))
        config.codex_app_server = CodexAppServerOptions(
            enabled=True, use_for_file_agent=True,
            disable_node_repl=True, use_minimal_home=True,
        )
        runner = BugAnalysisRunner(config)
        policy = runner.build_codex_app_server_execution_policy(
            cwd=Path(tmp), timeout=300,
        )
    self.assertTrue(policy.disable_node_repl)
    self.assertIsInstance(policy.env, dict)
    self.assertNotEqual(policy.env, {})  # never falls back to inherit-all
```

- [ ] **Step 3: Run and verify failure**

```bash
.venv/bin/python -m pytest -q tests/test_agents.py -k app_server_policy_honors
```

Expected: `AttributeError: ... has no attribute 'build_codex_app_server_execution_policy'`.

- [ ] **Step 4: Implement the policy dataclass + builder**

In `bug_runner.py`, add near the other app-server helpers:

```python
@dataclass(slots=True)
class CodexAppServerExecutionPolicy:
    env: dict[str, str]
    codex_home: Path | None
    cwd: Path
    disable_node_repl: bool
    emit_node_repl_flag: bool
```

```python
def build_codex_app_server_execution_policy(
    self, *, cwd: Path, timeout: int,
) -> CodexAppServerExecutionPolicy:
    options = self.config.codex_app_server
    # Single env builder: start from internal-network base, then add proxy
    # only when the app-server explicitly needs it (no clear-then-readd).
    env = build_internal_network_env(self.config.internal_network_env)
    if options.preserve_proxy_env:
        env = self._merge_codex_app_server_proxy_env(env)
    env.setdefault("RUST_LOG", "warn")
    codex_home: Path | None = None
    if options.use_minimal_home:
        codex_home = self._prepare_codex_app_server_minimal_home()
        if codex_home is not None:
            env["CODEX_HOME"] = str(codex_home)
    # User config is authoritative; minimal-home only decides whether the
    # node_repl disable FLAG is emitted (its config has no node_repl section).
    emit_node_repl_flag = options.disable_node_repl and codex_home is None
    return CodexAppServerExecutionPolicy(
        env=env,
        codex_home=codex_home,
        cwd=cwd,
        disable_node_repl=options.disable_node_repl,
        emit_node_repl_flag=emit_node_repl_flag,
    )
```

- [ ] **Step 5: Route `_run_custom_skill_agent_via_codex_app_server` through the policy**

Replace the inline block at bug_runner.py:9952-9960 with a single call:

```python
policy = self.build_codex_app_server_execution_policy(cwd=cwd, timeout=timeout)
subprocess_env = policy.env
```

Pass `policy.disable_node_repl` (semantic value) to `CodexAppServerRuntime`, and thread `emit_node_repl_flag` down so `_build_app_server_command` only appends the `-c mcp_servers.node_repl.enabled=false` token when `emit_node_repl_flag` is true. Remove the standalone `disable_node_repl = False` line.

- [ ] **Step 6: Harden `_build_app_server_command` flag emission**

In `codex_app_server_runtime.py`, change `_build_app_server_command` (and the `CodexAppServerRuntime`/`CodexAppServerClient` kwargs) so node_repl flag emission is controlled by an explicit `emit_node_repl_flag` argument rather than inferring it from `disable_node_repl`. Keep existing tests green by defaulting `emit_node_repl_flag = disable_node_repl`.

- [ ] **Step 7: Run focused tests**

```bash
.venv/bin/python -m pytest -q tests/test_agents.py -k "app_server" tests/test_codex_app_server_runtime.py
```

Expected: new test passes; existing proxy-reinject and command-build tests still pass.

- [ ] **Step 8: Commit**

```bash
git add lark_agent_bridge/agents/bug_runner.py lark_agent_bridge/agents/codex_app_server_runtime.py tests/test_agents.py
git commit -m "runtime: centralize codex app-server execution policy"
```

## Task 2: Explicit Completion State, No More Heading-Guessing (correctness)

Replace the `ok + two headings` judgment with a tri-state `completion_state`, and only allow `source_stage_direct` reuse on `complete`.

**Files:** Modify `lark_agent_bridge/agents/codex_app_server_runtime.py`, `lark_agent_bridge/agents/bug_runner.py`, `tests/test_codex_app_server_runtime.py`, `tests/test_agents.py`.

- [ ] **Step 1: Write failing runtime test for tri-state result**

Add to `tests/test_codex_app_server_runtime.py`:

```python
def test_turn_completed_event_yields_complete_state(self):
    client = _FakeClient(notifications=[
        {"method": "item/completed", "params": {"item": {"type": "agentMessage",
            "phase": "final_answer",
            "text": "## 结论摘要\n- ok\n\n## 关键证据\n- L1 evidence\n"}}},
        {"method": "turn/completed", "params": {"turn": {"status": "completed"}}},
    ])
    runtime = _runtime(client)  # existing helper shape used by sibling tests
    result = runtime.run_turn("分析")
    self.assertEqual(result.completion_state, CompletionState.COMPLETE)

def test_timeout_with_partial_markdown_is_partial_not_complete(self):
    # Evidence section present but no turn/completed: must be partial.
    client = _FakeClient(notifications=[
        {"method": "item/started", "params": {"item": {"type": "agentMessage",
            "id": "m1", "phase": "final_answer", "text": ""}}},
        {"method": "item/agentMessage/delta", "params": {"itemId": "m1",
            "delta": "## 结论摘要\n- x\n\n## 关键证据\n- L1\n"}},
    ])
    runtime = _runtime(client, turn_timeout_seconds=0.01, post_tool_quiet_timeout_seconds=0.01)
    result = runtime.run_turn("分析")
    self.assertEqual(result.completion_state, CompletionState.PARTIAL)
    self.assertFalse(result.ok)
    self.assertTrue(result.final_text)  # partial text preserved for audit
```

- [ ] **Step 2: Run and verify failure**

```bash
.venv/bin/python -m pytest -q tests/test_codex_app_server_runtime.py -k "completion or partial"
```

Expected: `ImportError`/`AttributeError` on `CompletionState` / `completion_state`.

- [ ] **Step 3: Add the enum and result field**

In `codex_app_server_runtime.py`:

```python
from enum import Enum

class CompletionState(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"
```

Add `completion_state: CompletionState = CompletionState.FAILED` to `CodexAppServerResult`.

- [ ] **Step 4: Assign state at each exit and strengthen the structure check**

In `run_turn`:

- `turn/completed` with `status == completed` and non-empty final text → `COMPLETE`.
- `turn/completed` with `status != completed`, subprocess exit, or empty output → `FAILED`.
- quiet-timeout / turn-timeout / interrupt exits → `PARTIAL` (keep `final_text` for audit; `ok=False`).

Replace `_looks_like_complete_agent_markdown` with `_has_all_required_sections_nonempty(text)` that requires every section in `("## 结论摘要", "## 关键证据", "## 建议动作")` to be present **with non-empty body lines** (reuse the same body-scan logic as `bug_runner._validate_custom_skill_analysis`). Only use it as a *secondary* COMPLETE signal when `turn/completed` was received; never use it to short-circuit a timeout into COMPLETE.

Set `ok = (completion_state == CompletionState.COMPLETE)`.

- [ ] **Step 5: Write failing bug_runner test that partial does NOT direct-reply**

Add to `tests/test_agents.py`:

```python
def test_source_stage_partial_falls_back_to_summary_not_direct_reply(self):
    # A partial app-server result must run the summary path, not reuse raw md.
    ...
    fake = CodexAppServerResult(ok=False, completion_state=CompletionState.PARTIAL,
        final_text="## 结论摘要\n- x\n\n## 关键证据\n- L1\n")
    # patch _run_custom_skill_agent_via_codex_app_server to return this,
    # patch _run_bug_agent_summary, assert summary IS called and
    # use_source_stage_direct_reply path is NOT taken.
```

- [ ] **Step 6: Gate `use_source_stage_direct_reply` on completion_state**

At bug_runner.py:3260, add the completion-state condition. The execution result dict returned by `_run_custom_skill_agent_via_codex_app_server` must surface `completion_state` (string value). Update the guard:

```python
use_source_stage_direct_reply = (
    explicit_source_followup
    and skill_file_agent_execution_result is not None
    and bool(skill_file_agent_execution_result.get("ok"))
    and str(skill_file_agent_execution_result.get("executor") or "") == "codex_app_server"
    and str(skill_file_agent_execution_result.get("completion_state") or "") == "complete"
    and any(_kind_spec(plan.kind).is_source_stage for plan in plans)
)
```

When this is false but a partial markdown exists, the existing `else` branch (`_run_bug_agent_summary`) already provides the fallback — confirm partial markdown is still passed as prior evidence, not dropped.

- [ ] **Step 7: Run focused tests**

```bash
.venv/bin/python -m pytest -q tests/test_codex_app_server_runtime.py tests/test_agents.py -k "completion or partial or source_stage_direct or app_server"
```

Expected: pass. Note: removing the early-complete shortcut means some turns now run to `turn/completed`; verify no existing test asserted early-return timing.

- [ ] **Step 8: Commit**

```bash
git add lark_agent_bridge/agents/codex_app_server_runtime.py lark_agent_bridge/agents/bug_runner.py tests/test_codex_app_server_runtime.py tests/test_agents.py
git commit -m "runtime: gate source-stage direct reply on explicit completion state"
```

## Task 3: Progress-Aware Stall Detection (correctness)

Replace the single post-tool quiet timer (runtime:507-519) so long reasoning is not killed, while genuinely wedged turns still stop.

**Files:** Modify `lark_agent_bridge/models.py`, `lark_agent_bridge/config.py`, `lark_agent_bridge/agents/codex_app_server_runtime.py`, `tests/test_codex_app_server_runtime.py`, `tests/test_config.py`.

- [ ] **Step 1: Write failing test that reasoning/token/stderr progress resets the stall clock**

Add to `tests/test_codex_app_server_runtime.py`:

```python
def test_ongoing_progress_prevents_post_tool_stall(self):
    # tool completes, then only reasoning + token-usage events arrive:
    # the turn must NOT be interrupted as "quiet after tool".
    client = _FakeClient(notifications=[
        {"method": "item/completed", "params": {"item": {"type": "commandExecution", "command": "rg x"}}},
        {"method": "item/started", "params": {"item": {"type": "reasoning"}}},
        {"method": "thread/tokenUsage/updated", "params": {"tokenUsage": {"total": {"totalTokens": 10}}}},
        {"method": "item/completed", "params": {"item": {"type": "agentMessage", "phase": "final_answer",
            "text": "## 结论摘要\n- ok\n\n## 关键证据\n- L1\n\n## 建议动作\n- go\n"}}},
        {"method": "turn/completed", "params": {"turn": {"status": "completed"}}},
    ])
    runtime = _runtime(client, no_output_timeout_seconds=0.05, no_event_timeout_seconds=5.0)
    result = runtime.run_turn("分析")
    self.assertEqual(result.completion_state, CompletionState.COMPLETE)
```

- [ ] **Step 2: Add config knobs**

In `models.py` `CodexAppServerOptions`, add:

```python
no_event_timeout_seconds: float = 120.0   # hard stall: no events at all
no_output_timeout_seconds: float = 0.0    # 0 disables; reserved for output-only stalls
```

Keep `post_tool_quiet_timeout_seconds` for back-compat but treat it as the legacy fallback when `no_event_timeout_seconds` is unset. Load both in `config.py` with defaults. Add a `tests/test_config.py` assertion for the new defaults.

- [ ] **Step 3: Implement progress-aware detection**

In `run_turn`, track `last_progress_at` updated by ANY of: tool completion, `reasoning` item started/updated, `agentMessage` delta, `thread/tokenUsage/updated`, and stderr growth (compare `len(client.stderr_tail(...))` or a stderr counter). Replace the `last_tool_completion_at` quiet check with:

```python
if no_event_timeout_seconds > 0 and (now - last_progress_at) > no_event_timeout_seconds:
    self._interrupt_turn(...)
    result_error_code = "codex_app_server_no_event_timeout"
    completion_state = CompletionState.PARTIAL
    break
```

Remove the `_looks_like_complete_agent_markdown` early-COMPLETE branch from the timeout path (already handled by Task 2 — timeout is always PARTIAL).

- [ ] **Step 4: Keep a genuine-stall test**

Add a test where NO events arrive and assert `completion_state == PARTIAL`, `error_code == "codex_app_server_no_event_timeout"`, `should_retire is True`.

- [ ] **Step 5: Run tests**

```bash
.venv/bin/python -m pytest -q tests/test_codex_app_server_runtime.py tests/test_config.py -k "stall or progress or timeout or codex_app_server"
```

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add lark_agent_bridge/models.py lark_agent_bridge/config.py lark_agent_bridge/agents/codex_app_server_runtime.py tests/test_codex_app_server_runtime.py tests/test_config.py
git commit -m "runtime: progress-aware stall detection for codex app-server"
```

## Task 4: CODEX_HOME Template + Per-Run Isolation + Own Model Field (maintainability)

Stop reusing one shared home, give app-server its own `model`, and normalize usage reporting through `token_usage`.

**Files:** Modify `lark_agent_bridge/models.py`, `lark_agent_bridge/config.py`, `lark_agent_bridge/agents/bug_runner.py`, `config/config.example.toml`, `docs/configuration.md`, `tests/test_agents.py`, `tests/test_config.py`.

- [ ] **Step 1: Verify whether codex writes runtime state into CODEX_HOME**

Before building isolation, confirm the need:

```bash
ls -la ~/.codex
# After one app-server run, diff the home to see what codex writes back.
```

Record the finding in the test docstring. If codex writes session/history/cache into CODEX_HOME, per-run isolation is justified; if not, isolation is hygiene-only and the template alone suffices. Either way, proceed with template + per-run because it also fixes config residue.

- [ ] **Step 2: Add `model` field + failing config test**

In `models.py`, add `model: str = ""` to `CodexAppServerOptions`. Load it in `config.py`. Add `tests/test_config.py` assertions: default `model == ""`, and a custom value round-trips.

- [ ] **Step 3: Write failing test: empty model omits `model=` line; per-run dir is isolated**

Add to `tests/test_agents.py`:

```python
def test_minimal_home_omits_model_when_unset_and_isolates_per_run(self):
    # model unset -> no model= line (codex uses its own default).
    # two runs -> two distinct run dirs under data/codex_app_server_home/runs.
    ...
    self.assertNotIn("model =", config_text)
    self.assertNotEqual(run_dir_a, run_dir_b)
```

- [ ] **Step 4: Refactor `_prepare_codex_app_server_minimal_home` into template + per-run copy**

Split into:

```python
def _ensure_codex_app_server_home_template(self) -> Path | None:
    template = self.config.data_dir / "codex_app_server_home" / "template"
    # copy auth.json / installation_id / models_cache.json from ~/.codex
    # write minimal config.toml; only include `model = "..."` when
    # self.config.codex_app_server.model.strip() is non-empty.
    ...

def _prepare_codex_app_server_minimal_home(self, *, run_id: str) -> Path | None:
    template = self._ensure_codex_app_server_home_template()
    if template is None:
        return None
    run_home = self.config.data_dir / "codex_app_server_home" / "runs" / run_id
    shutil.copytree(template, run_home, dirs_exist_ok=True)
    return run_home
```

Remove the hardcoded `gpt-5.4` fallback entirely. `run_id` should be the job/turn id available at call time; if none, use a short uuid.

- [ ] **Step 5: Clean up per-run homes**

Wire run-home cleanup into the existing `[job_retention]` cleanup path so `runs/<id>` (each containing a copied `auth.json`) does not accumulate. At minimum, delete the run home in the `finally` of the app-server helper after the turn completes; add age-based sweeping of orphaned run dirs alongside the temp-cache cleanup. Add a test asserting the run home is removed after a run.

- [ ] **Step 6: Normalize usage through `token_usage`**

Where the app-server result usage is consumed (helper return + `use_source_stage_direct_reply` usage passthrough at bug_runner.py:3260 block), pass it through `normalize_token_usage(...)` from `lark_agent_bridge.token_usage` so app-server token reporting matches the `d67f265` normalized shape. Add an assertion in an existing app-server test that `usage` has `input_tokens`/`output_tokens`/`total_tokens` keys.

- [ ] **Step 7: Document config**

Append `model`, `no_event_timeout_seconds`, `no_output_timeout_seconds` to the `[codex_app_server]` block in `config/config.example.toml` and add a short note in `docs/configuration.md` explaining template+per-run home and "empty model = codex default".

- [ ] **Step 8: Run focused tests + commit**

```bash
.venv/bin/python -m pytest -q tests/test_agents.py tests/test_config.py -k "minimal_home or model or app_server or codex_app_server"
git add lark_agent_bridge/models.py lark_agent_bridge/config.py lark_agent_bridge/agents/bug_runner.py config/config.example.toml docs/configuration.md tests/test_agents.py tests/test_config.py
git commit -m "runtime: template+per-run codex home, own model field, normalized usage"
```

## Task 5: Source-Stage Context Policy Registry + Explicit Throttle Stages (maintainability)

Converge scattered followup/cwd/priority/prompt logic into a registry without regressing `d67f265`, and replace the `_stream` suffix throttle with an explicit set.

**Files:** Modify `lark_agent_bridge/agents/bug_runner.py`, `lark_agent_bridge/app.py`, `tests/test_agents.py`, `tests/test_app.py`.

- [ ] **Step 1: Add characterization tests BEFORE refactor (lock current behavior)**

Pin the behaviors that already work so the refactor cannot silently break them. Add to `tests/test_agents.py`:

- `scene-signal-diagnosis` is selected for an explicit source followup that previously carried polluted xtheme context (the `1219603` fix).
- `_domain_priority_files("scene-signal-diagnosis")` returns the expected guideengine/napa5 candidates.
- Reanalysis preserves the original bug request text via `_original_bug_request_text` (the `d67f265` fix): given `previous_context.request_text` with a `\n\n追问/修正：` suffix, the rebuilt request equals the pre-suffix original.

Run them green against current code first:

```bash
.venv/bin/python -m pytest -q tests/test_agents.py -k "scene_signal or domain_priority or original_bug_request or source_stage"
```

- [ ] **Step 2: Define the policy structure**

In `bug_runner.py`:

```python
@dataclass(slots=True)
class SourceStageContextPolicy:
    domain_kind: str
    skill_name: str
    cwd_root_selector: Callable[["BugAnalysisRunner"], Path | None]
    priority_files: Callable[["BugAnalysisRunner"], list[Path]]
    request_text_builder: Callable[..., str]
    reference_text_builder: Callable[..., str]
    search_budget: int = 0
```

Register `scene-signal-diagnosis` as the first instance, wiring its callables to the EXISTING helpers (`_domain_priority_files`, `_source_stage_followup_request_text`, `_source_stage_followup_prompt_text`) and crucially routing `request_text_builder` through `_original_bug_request_text` / `_strip_bug_followup_suffix` so the `d67f265` behavior is preserved. Provide a default policy for unregistered domains that reproduces today's generic path.

- [ ] **Step 3: Route source-stage selection through the registry**

Replace the inline scene-signal `if/else` special-casing with a registry lookup (`_source_stage_policy_for(context_profile)`), falling back to the default policy. Do not change observable outputs — the characterization tests from Step 1 must stay green.

- [ ] **Step 4: Replace the `_stream` suffix throttle with an explicit set**

In `app.py`, add a module-level constant and use it at line 1654:

```python
_THROTTLED_PROGRESS_STAGES = frozenset({
    "source_stage_agent_analysis_stream",
    # add other high-frequency Codex delta stages here
})
```

```python
if stage in _THROTTLED_PROGRESS_STAGES:
    last_update = card_state.get("last_card_update_at")
    if isinstance(last_update, datetime) and (now - last_update).total_seconds() < self._progress_card_stream_update_interval_seconds:
        return False
return True
```

Keep the 5s interval and full-progress persistence unchanged. Update `tests/test_app.py::test_stream_progress_card_updates_are_throttled` to use a stage from the set, and add a test that a non-listed `*_stream` stage is NOT throttled (proves the behavior is now explicit, not suffix-based).

- [ ] **Step 5: Run focused tests + commit**

```bash
.venv/bin/python -m pytest -q tests/test_agents.py tests/test_app.py -k "scene_signal or domain_priority or original_bug_request or source_stage or throttle"
git add lark_agent_bridge/agents/bug_runner.py lark_agent_bridge/app.py tests/test_agents.py tests/test_app.py
git commit -m "runtime: source-stage context policy registry and explicit throttle stages"
```

## Task 6: Full Regression + Mandatory Real Feishu Source-Analysis Verification

This task is REQUIRED by the goal: after implementation, the change must pass a real group-chat test that actually produces a source-analysis result; on failure, investigate via logs rather than guessing.

**Files:** No code changes unless verification finds a bug. Read: `data/state/agent_activity.json`, job output dir, `source_stage_analysis.md`, `*_codex_app_server_events.jsonl`.

- [ ] **Step 1: Full focused regression**

```bash
.venv/bin/python -m pytest -q tests/test_config.py tests/test_codex_app_server_runtime.py tests/test_agents.py tests/test_app.py -k "codex or app_server or source_stage or completion or partial or stall or throttle or minimal_home or original_bug_request"
```

Expected: all pass. If too slow, run per-file and record any skipped boundary.

- [ ] **Step 2: Live app-server handshake check**

```bash
codex --version           # must be >= min_version (0.125.0)
codex app-server --help    # confirm stdio app-server is available
```

- [ ] **Step 3: Enable file-agent app-server in local (uncommitted) config**

In local `config.toml` only (never commit secrets):

```toml
[codex_app_server]
enabled = true
use_for_file_agent = true
use_for_bug_summary = false
fallback_to_exec = true
sandbox_mode = "read-only"
disable_node_repl = true   # must be HONORED now (Task 1)
use_minimal_home = true
no_event_timeout_seconds = 180
```

- [ ] **Step 4: Start the bridge and send a real source-analysis request**

```bash
./run.sh
```

In the test Feishu group (use the user-specified group; otherwise default `oc_d977fe30a92c7ac81e3e6b543d99ef5b`), send an explicit source-analysis followup against a known bug or direct log, e.g. reply to a prior scene-signal result and `@bot` with a request that explicitly asks for source/code evidence (so a domain-only request is not polluted). The request MUST drive `source_stage` + `codex_app_server`.

- [ ] **Step 5: Verify a source-analysis result was actually produced**

```bash
python3 - <<'PY'
import json
from pathlib import Path
data = json.loads(Path("data/state/agent_activity.json").read_text())
sessions = sorted((data.get("sessions") or {}).values(), key=lambda x: x.get("updated_at",""))
s = sessions[-1]
d = s.get("details", {})
print(json.dumps({
    "status": s.get("status"),
    "analysis_kinds": d.get("analysis_kinds"),
    "analysis_skill": d.get("analysis_skill"),
    "source_stage_executor": d.get("source_stage_executor"),
    "agent_summary_execution_backend": d.get("agent_summary_execution_backend"),
    "completion_state": d.get("source_stage_completion_state"),
    "duration_seconds": d.get("duration_seconds"),
    "progress_tail": s.get("progress", [])[-6:],
}, ensure_ascii=False, indent=2))
PY
```

Required outcome (success criteria):

- `status == "succeeded"`.
- `source_stage_executor == "codex_app_server"`.
- A non-empty `source_stage_analysis.md` with all required sections non-empty.
- If `agent_summary_execution_backend == "source_stage_direct"`, then `completion_state == "complete"` (the Task 2 invariant).
- `duration_seconds <= 300`.
- progress contains a throttled `source_stage_agent_analysis_stream` and the job dir has `*_codex_app_server_events.jsonl`.

- [ ] **Step 6: On failure, investigate from logs (do not guess)**

If the run fails or exceeds 300s, collect evidence in this priority order (per the agreed truth-layering): live listener progress → newly written `source_stage_analysis.md` → final `TaskResult` details. Then inspect:

```bash
# app-server stderr + event audit for the failing job
ls -t data/jobs/*/output/*/ 2>/dev/null | head
cat <job>/source_stage.debug.log 2>/dev/null | tail -50
cat <job>/*_codex_app_server_events.jsonl | tail -30
```

Diagnose against the known failure codes: `codex_app_server_no_event_timeout` (stall detector — check if reasoning was truly silent, feeding back into the Task 3 assumption), `codex_app_server_turn_failed`/`_exited` (env/proxy/home — check the Task 1 policy env actually carried proxy + CODEX_HOME), empty output, or unavailable/version gate. Fix the specific root cause, re-run Step 4, and record the failure→fix in the verification note.

- [ ] **Step 7: Capture a reasoning-heavy event trace (validates Task 3 assumption)**

From the successful run's `*_codex_app_server_events.jsonl`, confirm whether events arrive during long internal reasoning windows (reasoning items / periodic token usage / stderr growth). If reasoning windows are genuinely event-silent, raise `no_event_timeout_seconds` accordingly and note it; this is the one assumption the whole stall fix rests on.

- [ ] **Step 8: Verify ordinary (non-source) bug request is unchanged**

Send a normal domain-only bug request and confirm no app-server source-stage artifact is produced and existing summary behavior is intact.

- [ ] **Step 9: Record verification note**

Append a short result note to `review-report/` (new dated file) with timings and any failure→fix. Do not paste chat IDs, tokens, cookies, or full logs.

## Self-Review

Spec coverage:

- All 7 verified anchors map to a task: node_repl/proxy/env → Task 1; completion judgment + direct-reply gate → Task 2; stall detection → Task 3; minimal-home model/isolation + usage normalization → Task 4; context registry + throttle-set → Task 5.
- Recent-commit constraints are explicit: `d67f265` `_original_bug_request_text`/`_strip_bug_followup_suffix` preserved in Task 5; `token_usage.normalize_token_usage` adopted in Task 4; `d264efe` codegraph warmup not disturbed.
- The goal's hard requirements (real group test, must produce source-analysis result, investigate failures from logs) are Task 6.

Ordering: correctness first (Tasks 1-3), maintainability next (Tasks 4-5), verification last (Task 6). Phase 2 session/thread reuse is explicitly out of scope.

Risk gates / assumptions called out:

- Task 1 Step 1 forces confirming WHY `disable_node_repl=False` exists before deleting it.
- Task 2 fixes `partial` to downgrade to summary, never hard-fail, never direct-reply.
- Task 3 + Task 6 Step 7 jointly validate the "reasoning emits progress" assumption with a real event trace.
- Task 5 Step 1 adds characterization tests before any refactor to prevent regressing the `1219603`/`d67f265` fixes.

Type/name consistency: config field `codex_app_server`; new fields `model`, `no_event_timeout_seconds`, `no_output_timeout_seconds`; enum `CompletionState`; result field `completion_state`; policy types `CodexAppServerExecutionPolicy`, `SourceStageContextPolicy`; throttle set `_THROTTLED_PROGRESS_STAGES`.

## Execution Handoff

Plan complete. Recommended order: Task 1 → 2 → 3 (correctness, each TDD + commit), then Task 4 → 5 (maintainability), then Task 6 (full regression + mandatory real Feishu source-analysis verification with log-based failure investigation). Do not enable app-server by default; keep `codex exec` fallback intact throughout.
