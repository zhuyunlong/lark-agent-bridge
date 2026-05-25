# CodeIndex Source Analysis Integration

> **Superpower category:** Code intelligence
> **Status:** Planned
> **Branch:** `feat/pydantic-ai-agents`

## Review Log (2026-05-26)

原始计划引用了 `@colbymchenry/codegraph` 作为代码智能后端。经过审查发现以下 **致命问题**：

1. **工具完全不匹配** — `@colbymchenry/codegraph` 是一个 TypeScript/JavaScript
   可视化工具，生成交互式 HTML 图形。它 **不支持** Kotlin/Java，**没有** `search`、
   `explore`、`context`、`init -i` 等 CLI 命令，**不能** 输出 JSON，**没有**
   `.codegraph/` 索引目录。计划中描述的整个 CLI 接口都不存在。
2. **安装脚本不存在** — `curl … install.sh` URL 是虚构的。
3. **性能数据虚构** — "~35% cost reduction, ~70% fewer tool calls (per CodeGraph
   benchmarks)" 没有任何来源。
4. **语言不兼容** — 项目分析的是 Kotlin/Java Android 仓库 (guideengine, napa5)，
   而 codegraph 只处理 TypeScript/JavaScript。

**修正方案**：使用 **Universal Ctags**（符号索引）+ **ripgrep**（引用搜索）替代
虚构的 codegraph CLI。这两个工具：
- 都原生支持 Kotlin/Java
- 都能输出 JSON
- 都是广泛部署的标准工具（brew install universal-ctags, brew install ripgrep）
- ripgrep 已经在本项目的 prompt 中被引用（`rg 定位 -> 读关键片段`）

索引结果缓存到 SQLite（项目已使用 SQLite 做知识存储），避免重复扫描。

---

## Overview

Integrate a local code index layer (Universal Ctags + ripgrep + SQLite cache)
into lark-agent-bridge's `source_investigation` pipeline, enabling:

1. **Fast symbol lookup** — query pre-built ctags index instead of spawning Agent subprocesses
2. **Reference tracing** — instant callers/usage search for any symbol via ripgrep
3. **Prompt enrichment** — inject indexed symbol context into LLM prompts to reduce token waste
4. **Multi-repo support** — index both guideengine and napa5 with the same query layer
5. **Zero-degradation fallback** — index miss silently falls through to existing API/CLI paths

## Architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│                     Source Investigation Pipeline                        │
│                                                                        │
│  Path 0 (existing):  local signal probe → direct answer                │
│  Path 1 (NEW):       Code index query (ctags + rg) → direct answer     │
│                      (if high confidence) or context enrichment        │
│  Path 2 (existing):  LLM API call → JSON parse → answer               │
│  Path 3 (existing):  CLI subprocess (codex exec) → JSON parse → answer │
│                                                                        │
│  Fallback chain: 0 → 1 → 2 → 3                                       │
│  Path 1 enriches Path 2/3 prompts even when it cannot answer directly  │
└──────────────────────────────────────────────────────────────────────────┘
```

### Toolchain

| Tool | Role | Install |
|------|------|---------|
| Universal Ctags | Symbol definition indexing (class, function, method, field) | `brew install universal-ctags` |
| ripgrep (rg) | Reference/usage search, call-site finding | `brew install ripgrep` (usually pre-installed) |
| SQLite | Cache ctags index for fast repeated queries | stdlib (`sqlite3`), no install needed |

## New Files

| File | Purpose |
|------|---------|
| `knowledge/code_index.py` | Ctags + ripgrep wrapper: symbol search, reference lookup, context building (~200 lines) |
| `tests/test_code_index.py` | Unit tests for code index client |

## Modified Files

| File | Changes |
|------|---------|
| `models.py` | Add 4 fields to `SourceInvestigationOptions`: `code_index_enabled`, `ctags_command`, `code_index_timeout_seconds`, `code_index_min_confidence` |
| `config.py` | Parse new `code_index_*` fields from `[source_investigation]` TOML section |
| `knowledge/source_investigation.py` | Insert code index fast path in `run()`, add `_try_code_index()`, enrich `_prompt_without_snapshot()` with indexed context |
| `config/config.example.toml` | Document new `code_index_*` config keys |

## Implementation Details

### Step 1: `code_index.py`

Pure-Python wrapper around `ctags` and `rg` CLIs. Zero external Python
dependencies (uses `subprocess` + `json` + `sqlite3` from stdlib, same
pattern as `_run_via_cli`).

```python
@dataclass(slots=True)
class SymbolHit:
    name: str
    kind: str          # "class", "method", "function", "field", etc.
    path: str          # relative path within repo
    line: int
    language: str      # "Kotlin", "Java"
    scope: str = ""    # e.g. "DataCenter" for a method inside DataCenter class
    signature: str = ""

@dataclass(slots=True)
class ReferenceHit:
    path: str
    line: int
    text: str          # matching line content

@dataclass(slots=True)
class CodeIndexContext:
    definitions: list[SymbolHit]
    references: list[ReferenceHit]


class CodeIndexClient:
    def __init__(self, repo_roots: list[Path], *,
                 ctags_command: str = "ctags",
                 rg_command: str = "rg",
                 timeout: float = 10.0,
                 cache_dir: Path | None = None)

    def is_available(self, repo: Path) -> bool
        # Check ctags and rg are on PATH via shutil.which()

    def ensure_index(self, repo: Path) -> bool
        # Run ctags if index is stale (mtime-based); cache to SQLite
        # ctags -R --languages=Java,Kotlin --output-format=json \
        #   --fields=+nSs -f - <repo>
        # Parse JSON lines, upsert into .code_index/symbols.sqlite

    def search_symbol(self, query: str, repo: Path) -> list[SymbolHit]
        # SELECT from cached SQLite index WHERE name LIKE/= query

    def find_references(self, symbol: str, repo: Path, *,
                        max_results: int = 20) -> list[ReferenceHit]
        # rg --json -w <symbol> <repo> --glob '*.{kt,java}'

    def get_context(self, query: str, repo: Path) -> CodeIndexContext
        # Combines search_symbol + find_references

    def enrich_prompt(self, query: str, repo_roots: list[Path]) -> list[str]
        # Returns prompt lines for injection into LLM context
```

Implementation strategy: **CLI subprocess** for both ctags and rg (not direct
file parsing). Rationale: reuses battle-tested tools; avoids reimplementing
language parsers.

### Step 2: `models.py` — Config Fields

```python
@dataclass(slots=True)
class SourceInvestigationOptions:
    # ... existing fields ...
    code_index_enabled: bool = True
    ctags_command: str = "ctags"
    code_index_timeout_seconds: float = 10.0
    code_index_min_confidence: float = 0.6
```

### Step 3: `config.py` — Parse New Fields

Add parsing for the 4 new fields inside the `source_investigation` section,
following the same pattern as existing fields (bool, str, float, float).

### Step 4: `source_investigation.py` — Fast Path + Prompt Enrichment

#### 4a. Insert code index fast path in `run()`

```python
def run(self, question, *, hits=None):
    # 1. local probe (existing)
    local_result = _try_local_signal_probe(...)
    if local_result is not None:
        return local_result

    # 2. Code index fast path (NEW)
    if self.config.source_investigation.code_index_enabled:
        ci_result = self._try_code_index(question, hits=hits, repo_roots=repo_roots)
        if ci_result is not None:
            self._record_successful_non_local_snapshot(question, hits or [], ci_result)
            return ci_result

    # 3. Direct API (existing, enriched with code index context)
    api_result = self._run_via_api(question, hits=hits, repo_roots=repo_roots)
    ...
```

#### 4b. `_try_code_index()` method

```python
def _try_code_index(self, question, *, hits, repo_roots):
    symbols = _extract_query_symbols(question, hits)
    if not symbols:
        return None

    all_contexts = []
    for repo in repo_roots:
        if not self._code_index.is_available(repo):
            continue
        self._code_index.ensure_index(repo)
        for symbol in symbols[:3]:
            ctx = self._code_index.get_context(symbol, repo)
            if ctx and ctx.definitions:
                all_contexts.append((repo, ctx))

    if not all_contexts:
        return None

    confidence = _code_index_confidence(all_contexts, question, hits)
    if confidence < self.config.source_investigation.code_index_min_confidence:
        self._last_code_index_context = all_contexts
        return None

    return _result_from_code_index(all_contexts, question, hits)
```

#### 4c. Prompt enrichment

In `_prompt_without_snapshot()`, if code index context is cached from a
low-confidence miss, append symbol information to help the LLM:

```
预索引符号信息（优先使用，减少搜索）：
- method DataCenter.mockSignal @ module_datacenter/DataCenter.kt:142
- class DataCenterBroadcastReceiver @ DataCenterBroadcastReceiver.java:28
  引用: DataCenterBroadcastReceiver.java:95: case ACTION_MOCK -> mockSignal(...)
```

### Step 5: Multi-repo Strategy

`repo_roots` already supports multiple paths. Code index queries each repo:

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
code_index_enabled = true
ctags_command = "ctags"
code_index_timeout_seconds = 10
code_index_min_confidence = 0.6
```

## Prerequisites (Operations)

```bash
# 1. Install Universal Ctags (one-time)
brew install universal-ctags

# 2. Verify ripgrep is available (usually pre-installed on dev machines)
rg --version

# 3. No explicit index initialization needed — CodeIndexClient.ensure_index()
#    runs ctags on first query and caches to .code_index/symbols.sqlite
#    under each repo root.
```

## Performance Estimates

| Scenario | Current | With Code Index | Improvement |
|----------|---------|-----------------|-------------|
| Signal definition lookup | 3-8s (API) / 30-120s (CLI) | ~100-300ms (ctags index hit) | 10-400x faster |
| Reference/usage search | 8-30s (API) | 0.5-2s (rg search) | 4-15x faster |
| Complex cross-module question | Full Agent search | Code index narrows scope → faster API convergence | 20-40% token savings (estimate) |
| Index-miss question | Same as current | Falls through to existing paths | Zero degradation |

## Risk Mitigation

| Risk | Mitigation |
|------|------------|
| ctags not installed | `is_available()` checks `shutil.which("ctags")`; silent skip; `code_index_enabled = false` to disable |
| Stale index after refactor | mtime-based invalidation in `ensure_index()`; worst case falls through to API/CLI |
| Query timeout | `code_index_timeout_seconds = 10` hard limit; timeout → fallback |
| Symbol extraction misses | Reuses existing `_signal_candidates_from_hits` + hits metadata signal/code fields |
| ctags version incompatibility | Universal Ctags JSON output is stable since v5.9; fallback to text parsing if needed |

## Change Summary

- **New code**: ~300 lines across 2 new files
- **Modified**: 4 existing files (~20 lines each)
- **New Python dependencies**: 0 (stdlib only — subprocess, json, sqlite3 — consistent with project policy)
- **External tool dependency**: Universal Ctags + ripgrep (optional, graceful degradation if absent)
