# CodeGraph Source Analysis Integration

> **Superpower category:** Code intelligence
> **Status:** Planned
> **Branch:** `feat/pydantic-ai-agents`

## Overview

Integrate CodeGraph's pre-indexed code knowledge graph into lark-agent-bridge's
`source_investigation` pipeline, enabling:

1. **Fast symbol lookup** — query pre-built indexes instead of spawning Agent subprocesses
2. **Call graph tracing** — instant callers/callees/impact analysis for any symbol
3. **Prompt enrichment** — inject CodeGraph context into LLM prompts to reduce token waste
4. **Multi-repo support** — index both guideengine and napa5 with the same query layer
5. **Zero-degradation fallback** — CodeGraph miss silently falls through to existing API/CLI paths

### Why CodeGraph (not agentmemory)

- This project's knowledge is **code structure** (signal definitions, call chains), not conversation memory
- Existing `knowledge.sqlite` + `derived-adb-simulations` already covers persistent memory
- CodeGraph directly solves the source analysis efficiency problem: ~35% cost reduction, ~70% fewer tool calls (per CodeGraph benchmarks)
- agentmemory would overlap with existing state management without adding code intelligence

## Architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│                     Source Investigation Pipeline                        │
│                                                                        │
│  Path 0 (existing):  local signal probe → direct answer                │
│  Path 1 (NEW):       CodeGraph index query → direct answer (if high    │
│                      confidence) or context enrichment                 │
│  Path 2 (existing):  LLM API call → JSON parse → answer               │
│  Path 3 (existing):  CLI subprocess (codex exec) → JSON parse → answer │
│                                                                        │
│  Fallback chain: 0 → 1 → 2 → 3                                       │
│  Path 1 enriches Path 2/3 prompts even when it cannot answer directly  │
└──────────────────────────────────────────────────────────────────────────┘
```

## New Files

| File | Purpose |
|------|---------|
| `knowledge/codegraph_client.py` | CodeGraph CLI wrapper: symbol search, call graph, context building (~150 lines) |
| `tests/test_codegraph_client.py` | Unit tests for CodeGraph client |

## Modified Files

| File | Changes |
|------|---------|
| `models.py` | Add 4 fields to `SourceInvestigationOptions`: `codegraph_enabled`, `codegraph_command`, `codegraph_timeout_seconds`, `codegraph_min_confidence` |
| `config.py` | Parse new `codegraph_*` fields from `[source_investigation]` TOML section |
| `knowledge/source_investigation.py` | Insert CodeGraph fast path in `run()`, add `_try_codegraph()`, enrich `_prompt_without_snapshot()` with CodeGraph context |
| `config.example.toml` | Document new `codegraph_*` config keys |

## Implementation Details

### Step 1: `codegraph_client.py`

Pure-Python wrapper around the `codegraph` CLI. Zero external dependencies
(uses `subprocess` + `json` from stdlib, same pattern as `_run_via_cli`).

```python
class CodeGraphClient:
    def __init__(self, repo_roots, command="codegraph", timeout=10.0)
    def is_available(self, repo: Path) -> bool
        # Check .codegraph/ directory exists under repo
    def search_symbol(self, query: str, repo: Path) -> list[SymbolHit]
        # codegraph search --repo <path> --query <q> --json
    def get_callers(self, symbol: str, repo: Path) -> list[SymbolHit]
        # codegraph explore --symbol <s> --direction callers --json
    def get_callees(self, symbol: str, repo: Path) -> list[SymbolHit]
    def get_context(self, query: str, repo: Path) -> CodeGraphContext
        # codegraph context --query <q> --json
        # Returns: entry_points, related_symbols, code_snippets
    def enrich_prompt(self, query: str, repo_roots: list[Path]) -> list[str]
        # Returns prompt lines for injection
```

Implementation strategy: **CLI subprocess** (not direct SQLite reads).
Rationale: avoids coupling to CodeGraph's internal schema; CLI is the stable interface.

### Step 2: `models.py` — Config Fields

```python
@dataclass(slots=True)
class SourceInvestigationOptions:
    # ... existing fields ...
    codegraph_enabled: bool = True
    codegraph_command: str = "codegraph"
    codegraph_timeout_seconds: float = 10.0
    codegraph_min_confidence: float = 0.6
```

### Step 3: `config.py` — Parse New Fields

Add parsing for the 4 new fields inside the `source_investigation` section,
following the same pattern as existing fields.

### Step 4: `source_investigation.py` — Fast Path + Prompt Enrichment

#### 4a. Insert CodeGraph fast path in `run()`

```python
def run(self, question, *, hits=None):
    # 1. local probe (existing)
    local_result = _try_local_signal_probe(...)
    if local_result is not None:
        return local_result

    # 2. CodeGraph fast path (NEW)
    if self.config.source_investigation.codegraph_enabled:
        cg_result = self._try_codegraph(question, hits=hits, repo_roots=repo_roots)
        if cg_result is not None:
            self._record_successful_non_local_snapshot(question, hits or [], cg_result)
            return cg_result

    # 3. Direct API (existing, enriched with CodeGraph context)
    api_result = self._run_via_api(question, hits=hits, repo_roots=repo_roots)
    ...
```

#### 4b. `_try_codegraph()` method

```python
def _try_codegraph(self, question, *, hits, repo_roots):
    # Extract symbols from question + hits (signal names, class names, function names)
    symbols = _extract_query_symbols(question, hits)
    if not symbols:
        return None

    all_contexts = []
    for repo in repo_roots:
        if not self._codegraph.is_available(repo):
            continue
        for symbol in symbols[:3]:
            ctx = self._codegraph.get_context(symbol, repo)
            if ctx and ctx.symbols:
                all_contexts.append((repo, ctx))

    if not all_contexts:
        return None

    confidence = _codegraph_confidence(all_contexts, question, hits)
    if confidence < self.config.source_investigation.codegraph_min_confidence:
        # Cache context for prompt enrichment in API/CLI paths
        self._last_codegraph_context = all_contexts
        return None

    return _result_from_codegraph(all_contexts, question, hits)
```

#### 4c. Prompt enrichment

In `_prompt_without_snapshot()`, if CodeGraph context is cached from a
low-confidence miss, append symbol information to help the LLM:

```
CodeGraph 预索引符号信息（优先使用，减少搜索）：
- function DataCenter.mockSignal @ module_datacenter/DataCenter.kt:142
- class DataCenterBroadcastReceiver @ module_datacenter/DataCenterBroadcastReceiver.java:28
  DataCenterBroadcastReceiver.java:95: case ACTION_MOCK -> mockSignal(...)
```

### Step 5: Multi-repo Strategy

`repo_roots` already supports multiple paths. CodeGraph queries each repo:

```toml
[source_investigation]
repo_roots = [
    "/path/to/guideengine",
    "/path/to/napa5",
]
```

Query strategy:
1. Extract primary repo hint from question keywords (signal/Unity → guideengine, native/crash/render → napa5)
2. Query primary repo first, return on hit
3. On miss, query remaining repos and merge results

### Step 6: Config Example Update

```toml
[source_investigation]
enabled = true
# ... existing fields ...
codegraph_enabled = true
codegraph_command = "codegraph"
codegraph_timeout_seconds = 10
codegraph_min_confidence = 0.6
```

## Prerequisites (Operations)

```bash
# 1. Install CodeGraph CLI (one-time)
curl -fsSL https://raw.githubusercontent.com/colbymchenry/codegraph/main/install.sh | sh
# or: npm i -g @colbymchenry/codegraph

# 2. Initialize indexes for each repo (one-time per repo)
cd /path/to/guideengine && codegraph init -i
cd /path/to/napa5 && codegraph init -i

# 3. File watcher keeps indexes fresh automatically — no cron needed
```

## Performance Estimates

| Scenario | Current | With CodeGraph | Improvement |
|----------|---------|----------------|-------------|
| Signal definition lookup | 3-8s (API) / 30-120s (CLI) | ~200ms (direct hit) | 15-600x faster |
| Call chain analysis | 8-30s (API) | 1-3s (callers/callees) | 3-10x faster |
| Complex cross-module question | Full Agent search | CodeGraph narrows scope → faster API convergence | 30-50% token savings |
| Index-miss question | Same as current | Falls through to existing paths | Zero degradation |

## Risk Mitigation

| Risk | Mitigation |
|------|------------|
| CodeGraph CLI not installed | `is_available()` check; silent skip; `codegraph_enabled = false` to disable |
| Stale index after large refactor | CodeGraph file watcher auto-updates; worst case falls through to API/CLI |
| Query timeout | `codegraph_timeout_seconds = 10` hard limit; timeout → fallback |
| Symbol extraction misses | Reuses existing `_signal_candidates_from_hits` + hits metadata signal/code fields |
| CodeGraph schema changes | CLI subprocess approach avoids direct SQLite coupling |

## Change Summary

- **New code**: ~250 lines across 2 new files
- **Modified**: 4 existing files (~20 lines each)
- **New Python dependencies**: 0 (stdlib only, consistent with project policy)
- **External tool dependency**: CodeGraph CLI (optional, graceful degradation if absent)
