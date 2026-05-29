# CodeGraph Warmup Node Storm Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate startup-triggered duplicate `codegraph sync` warmups that spawn multiple high-CPU `node` processes, while preserving CodeGraph availability for real source-investigation requests.

**Architecture:** Replace the current in-process-only warmup guard with a repo-scoped cross-process coordinator plus a cooldown/staleness policy. Startup paths should do only cheap readiness checks; the expensive `sync_index()` path should run at most once per repo per cooldown window, and only after winning a host-level lock.

**Tech Stack:** Python 3.13, `threading`, `pathlib`, JSON state files under `data/state`, existing `SourceInvestigationRunner`, `KnowledgeService`, CLI/app wiring, `pytest`/`unittest.mock`.

---

## File Map

- Modify: `lark_agent_bridge/knowledge/source_investigation.py`
  - Add cross-process warmup coordination, cooldown metadata, and “cheap probe vs expensive sync” split.
- Modify: `lark_agent_bridge/knowledge/service.py`
  - Keep startup hook simple and make the warmup call explicitly best-effort.
- Modify: `lark_agent_bridge/models.py`
  - Add source-investigation warmup policy knobs if needed (`cooldown`, `stale threshold`, `startup mode`).
- Modify: `lark_agent_bridge/config.py`
  - Load any new warmup policy fields with safe defaults.
- Modify: `config/config.example.toml`
  - Document the new warmup policy values if config fields are added.
- Test: `tests/test_knowledge.py`
  - Lock down startup warmup behavior, cross-process lock semantics (mocked), and cooldown policy.
- Test: `tests/test_app.py` or `tests/test_cli.py`
  - Lock down listen vs non-listen startup wiring if the control flow changes.

---

### Task 1: Reproduce the Real Failure Mode in Tests

**Files:**
- Modify: `tests/test_knowledge.py`
- Read: `lark_agent_bridge/knowledge/source_investigation.py`

- [ ] **Step 1: Add a failing test for duplicate warmup launches across runner instances**

Create a test that instantiates two `SourceInvestigationRunner` objects with the same repo root, mocks `_get_codegraph()` and `sync_index()`, calls `warmup_codegraph()` twice, and currently observes duplicate background launch attempts.

```python
def test_warmup_codegraph_deduplicates_across_runner_instances(self):
    config = BridgeConfig()
    repo = Path("/tmp/repo")
    config.source_investigation.repo_roots = [repo]

    sync_calls = []

    class FakeCg:
        def is_indexed(self, path):
            return True

        def sync_index(self, path):
            sync_calls.append(path)
            return True

    runner1 = SourceInvestigationRunner(config)
    runner2 = SourceInvestigationRunner(config)
```

- [ ] **Step 2: Add a failing test for startup cooldown**

Create a test that simulates a recent successful warmup stamp and asserts that a new startup call to `warmup_codegraph()` does not call `sync_index()` again.

```python
def test_warmup_codegraph_skips_recent_repo_sync(self):
    config = BridgeConfig()
    runner = SourceInvestigationRunner(config)
    # prewrite recent warmup state, then assert sync_index not called
```

- [ ] **Step 3: Run targeted tests to verify they fail for the right reason**

Run:

```bash
python -m pytest tests/test_knowledge.py -k "warmup_codegraph and (deduplicates or recent_repo_sync)" -v
```

Expected: failures showing duplicate launch or missing cooldown guard.

---

### Task 2: Introduce a Repo-Scoped Cross-Process Warmup Coordinator

**Files:**
- Modify: `lark_agent_bridge/knowledge/source_investigation.py`
- Test: `tests/test_knowledge.py`

- [ ] **Step 1: Add a small warmup state model and file-path helpers**

Add helpers that derive per-repo state and lock paths under bridge-managed state, for example:

```python
def _codegraph_warmup_state_dir(config: BridgeConfig) -> Path:
    return config.data_dir / "state" / "codegraph_warmup"

def _codegraph_repo_slug(repo: Path) -> str:
    return hashlib.sha1(str(_warmup_repo_key(repo)).encode("utf-8")).hexdigest()[:16]

def _codegraph_repo_state_path(config: BridgeConfig, repo: Path) -> Path:
    return _codegraph_warmup_state_dir(config) / f"{_codegraph_repo_slug(repo)}.json"

def _codegraph_repo_lock_path(config: BridgeConfig, repo: Path) -> Path:
    return _codegraph_warmup_state_dir(config) / f"{_codegraph_repo_slug(repo)}.lock"
```

- [ ] **Step 2: Add a host-level lock acquisition helper**

Use an OS lock (`fcntl.flock` on this macOS host) so different bridge processes cannot start the same repo warmup concurrently.

```python
@contextmanager
def _try_repo_warmup_lock(config: BridgeConfig, repo: Path):
    lock_path = _codegraph_repo_lock_path(config, repo)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield None
            return
        yield fh
```

- [ ] **Step 3: Run the new failing dedup test**

Run:

```bash
python -m pytest tests/test_knowledge.py -k "deduplicates_across_runner_instances" -v
```

Expected: still failing until `warmup_codegraph()` is rewired to use the new lock.

---

### Task 3: Split “Startup Probe” from “Expensive Sync”

**Files:**
- Modify: `lark_agent_bridge/knowledge/source_investigation.py`
- Modify: `lark_agent_bridge/knowledge/service.py`
- Test: `tests/test_knowledge.py`

- [ ] **Step 1: Add a cheap repo eligibility check**

Refactor `warmup_codegraph()` so startup does only:
- `codegraph_enabled` check
- repo existence check
- `cg.is_indexed(repo)` check
- cooldown/state check
- cross-process lock acquisition

Only after all of those pass should it spawn the background sync thread.

- [ ] **Step 2: Persist warmup timestamps and outcome**

Write a small JSON state after each completed warmup:

```json
{
  "repo": "/abs/path/to/repo",
  "started_at": "...",
  "finished_at": "...",
  "success": true,
  "pid": 12345
}
```

Startup should skip warmup if the last successful run is within the cooldown window.

- [ ] **Step 3: Make `KnowledgeService` startup warmup explicitly best-effort**

Keep the call site simple:

```python
if warmup_codegraph:
    self._source_investigation_runner.warmup_codegraph()
```

But ensure runner internals cannot flood the machine on repeated startup.

- [ ] **Step 4: Run the two warmup tests again**

Run:

```bash
python -m pytest tests/test_knowledge.py -k "warmup_codegraph and (deduplicates or recent_repo_sync)" -v
```

Expected: PASS.

---

### Task 4: Add Warmup Policy Knobs Only If the Existing Defaults Are Not Enough

**Files:**
- Modify: `lark_agent_bridge/models.py`
- Modify: `lark_agent_bridge/config.py`
- Modify: `config/config.example.toml`
- Test: `tests/test_config.py`

- [ ] **Step 1: Decide whether policy needs to be configurable**

If hardcoded defaults are acceptable, skip this task. If not, add minimal fields such as:

```python
codegraph_warmup_enabled: bool = True
codegraph_warmup_cooldown_seconds: float = 1800.0
codegraph_warmup_stale_after_seconds: float = 7200.0
```

- [ ] **Step 2: Wire config loading with conservative defaults**

Add config parsing in `load_config()` and document in `config/config.example.toml`.

- [ ] **Step 3: Run config regression**

Run:

```bash
python -m pytest tests/test_config.py -k "codegraph or source_investigation" -v
```

Expected: PASS.

---

### Task 5: Lock Down CLI/App Startup Boundaries

**Files:**
- Read/Modify if needed: `lark_agent_bridge/cli.py`, `lark_agent_bridge/app.py`
- Test: `tests/test_cli.py` or `tests/test_app.py`

- [ ] **Step 1: Verify only `listen` still requests startup warmup**

Current code already does:

```python
app = BridgeApp(
    config,
    progress_callback=progress_callback,
    warmup_codegraph=args.command == "listen",
)
```

Add tests if coverage is missing so future refactors do not accidentally re-enable warmup on `check`, `handle-event`, `run-signal`, or `knowledge`.

- [ ] **Step 2: Run focused CLI/app tests**

Run:

```bash
python -m pytest tests/test_cli.py tests/test_app.py -k "warmup or listen" -v
```

Expected: PASS or no-op if there are no matching tests.

---

### Task 6: Verify Against the Original Symptom

**Files:**
- No code changes unless verification exposes a gap

- [ ] **Step 1: Run the full targeted suite for touched areas**

Run:

```bash
python -m pytest tests/test_knowledge.py tests/test_config.py tests/test_cli.py tests/test_app.py -k "codegraph or warmup or source_investigation" -v
```

Expected: PASS.

- [ ] **Step 2: Manual runtime verification on this Mac**

Run one listener start, then inspect processes:

```bash
ps -axo pid,ppid,%cpu,etime,command | rg "codegraph sync|node .*codegraph|codegraph.*sync"
```

Expected:
- At most one warmup-owned `node` process per indexed repo
- No duplicate syncs on immediate restart while cooldown is active
- No orphaned parallel warmups from repeated startup

- [ ] **Step 3: Commit**

```bash
git add lark_agent_bridge/knowledge/source_investigation.py \
        lark_agent_bridge/knowledge/service.py \
        lark_agent_bridge/models.py \
        lark_agent_bridge/config.py \
        config/config.example.toml \
        tests/test_knowledge.py \
        tests/test_config.py \
        tests/test_cli.py \
        tests/test_app.py
git commit -m "Throttle codegraph warmup across startups"
```

---

## Self-Review

- Spec coverage: covers duplicate startup warmups, cross-process duplication, CPU-heavy `node` storms, startup-vs-request boundary, and verification against real process behavior.
- Placeholder scan: no `TODO`/`TBD` placeholders remain; each task has explicit files and commands.
- Type consistency: plan assumes `fault_time` stays a string at the API boundary and is normalized internally, and warmup state is persisted under `data/state`.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-29-codegraph-warmup-node-storm.md`. Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

Which approach?
