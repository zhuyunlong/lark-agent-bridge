# Codex vs Pydantic-AI Source Analysis Comparison Report

## Task: 3D场景信号分析 — 主题切换后3D场景未展示

### Metrics Comparison

| Metric | Pydantic-AI Agent | Codex Exec |
|---|---|---|
| **Duration** | 337.6s (5.6min) | 419s (7.0min) |
| **Tool Calls** | 24 | 9 |
| **Evidence Items** | 8 | N/A (narrative) |
| **Runtime** | In-process (agent_runtime.py) | Subprocess (codex CLI) |
| **Real-time Progress** | ✅ Streaming to Feishu | ❌ Batch output only |
| **Codegraph Tools** | ✅ search_codegraph ×5, get_code_context ×1 | ❌ Not available |
| **Memory Access** | ❌ None | ✅ MEMORY.md (3 reads) |
| **Model** | deepseek (via cc-switch) | gpt-5.4 (via yybb) |

### Analysis Quality

| Aspect | Pydantic-AI | Codex |
|---|---|---|
| Root cause identified | ✅ specialSceneTypeFlow振荡 + SRDataManagerService不触发渲染 | ✅ SCENE_CHANGED同值去重 + UnityReady恢复不完整 |
| Source references | 8 evidence w/ line numbers | 9 file references w/ paths |
| Actionable fix | ⚠️ General direction | ✅ Specific: reset SCENE_CHANGED cache + authoritative replay |
| Depth of analysis | Signal chain tracing + timing | Code path + architectural root cause |

### Architecture Comparison

**Pydantic-AI Agent (In-Process)**
- ✅ Real-time progress: tool calls visible in Feishu as they happen
- ✅ Codegraph integration: semantic code search, not just grep
- ✅ Structured output: BugSummaryOutput model with fields
- ✅ No subprocess overhead, direct Python API calls
- ✅ Configurable tool budget and timeout
- ⚠️ No persistent memory across sessions
- ⚠️ Bound to single model provider per run

**Codex Exec (Subprocess)**
- ✅ MEMORY.md: persistent cross-session knowledge
- ✅ Codex plugins/skills: systematic-debugging skill loaded
- ✅ Deeper architectural insights (leverages memory)
- ❌ No real-time progress to Feishu (batch only)
- ❌ No codegraph tools (grep/rg only)
- ❌ Subprocess overhead: process spawn, shell wrapping
- ❌ Harder to control: read-only sandbox, no custom tools
- ❌ Slower: 419s vs 337.6s (24% slower)

### Conclusion

| Winner | Category |
|---|---|
| **Pydantic-AI** | Speed (337s vs 419s, 20% faster) |
| **Pydantic-AI** | Observability (real-time streaming) |
| **Pydantic-AI** | Tool richness (codegraph + custom tools) |
| **Pydantic-AI** | Integration (in-process, structured output) |
| **Codex** | Analysis depth (memory + systematic debugging) |
| **Codex** | Actionable fixes (more specific recommendations) |

**Recommendation**: Pydantic-AI as primary runtime (already implemented). 
Codex as optional fallback for deep analysis when pydantic-ai tool budget exhausted.
Future: integrate codex-style persistent memory into pydantic-ai agent.
