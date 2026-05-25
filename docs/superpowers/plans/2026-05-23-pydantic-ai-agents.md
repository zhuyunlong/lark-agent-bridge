# Pydantic AI Agents Integration

> **Superpower category:** Model-driven development
> **Status:** Implemented (Phase 1 — structured output + FSM routing)
> **Branch:** `feat/pydantic-ai-agents`

## Overview

Integrate pydantic-ai's structured output validation and agent patterns into
lark-agent-bridge, enabling:

1. **Structured output validation** — LLM responses auto-validated against Pydantic schemas
2. **Automatic retry** — validation failures trigger retry with error feedback to model
3. **State machine routing** — declarative FSM replaces nested if/elif message routing
4. **Graceful degradation** — all new features are optional; base project runs without pydantic

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                     Intent Classification Pipeline                     │
│                                                                      │
│  Path 0 (best):   pydantic-ai Agent → IntentOutput (auto-validated)  │
│  Path 1 (good):   llm_client.py → JSON → manual parse → IntentDecision│
│  Path 2 (legacy): subprocess CLI → stdout → manual parse → IntentDecision│
│                                                                      │
│  Fallback chain: Path 0 fails → Path 1 fails → Path 2              │
└──────────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────┐
│                     Routing State Machine (FSM)                       │
│                                                                      │
│  START → KEYWORD_MATCH                                               │
│    → (high confidence) → ROUTE → DONE                                │
│    → (low confidence) → LLM_CLASSIFY → ROUTE → DONE                 │
│                                                                      │
│  Inspired by LangGraph — states, transitions, conditions — but       │
│  implemented as a lightweight 200-line module, no external dependency.│
└──────────────────────────────────────────────────────────────────────┘
```

## New Files

| File | Purpose |
|------|---------|
| `agents/pydantic_models.py` | Pydantic BaseModel definitions (IntentOutput, AgentDeps) |
| `agents/pydantic_agents.py` | IntentAgent wrapper around pydantic-ai |
| `agents/routing_fsm.py` | Lightweight state machine for message routing |
| `tests/test_pydantic_agents.py` | 35+ unit tests for all new modules |

## Modified Files

| File | Change |
|------|--------|
| `agents/intent_runner.py` | Added Path 0 (pydantic-ai) before existing Path 1/2 |
| `pyproject.toml` | Added `pydantic-ai` optional dependency group |

## Key Design Decisions

### 1. Pydantic is Optional (Graceful Degradation)

```python
try:
    from pydantic import BaseModel, Field, field_validator
    PYDANTIC_AVAILABLE = True
except ImportError:
    PYDANTIC_AVAILABLE = False
    # Stub BaseModel provided for basic functionality
```

This means:
- **Without pydantic**: project runs exactly as before (stdlib only)
- **With pydantic**: enables structured output validation, auto-retry
- **With pydantic-ai**: additionally enables Agent pattern with provider abstraction

### 2. Three-Tier Fallback Chain

```
pydantic-ai Agent (best: schema-validated, auto-retry)
    ↓ fails
llm_client.py (good: fast HTTP, manual JSON parse)
    ↓ fails
subprocess CLI (legacy: slow but reliable)
```

### 3. FSM vs LangGraph

We borrow LangGraph's concepts without the dependency:
- **States**: enum-based, not string-based
- **Transitions**: condition functions, not graph DSL
- **Handlers**: plain functions, not nodes
- **No persistence layer**: in-memory only (we have our own job_retention)

### 4. Provider Mapping

pydantic-ai supports providers natively:
```python
# OpenAI-compatible
OpenAIProvider(base_url="...", api_key="...")

# Anthropic-compatible
AnthropicProvider(base_url="...", api_key="...")
```

Our presets map directly:
- `openai`, `yybb-codex`, `panda-codex`, `cc-switch-yybb-codex` → OpenAIProvider
- `mimo-claude`, `deepseek-claude`, `yybb-claude`, `cc-switch-*-claude` → AnthropicProvider

## Installation

```bash
# Base (no pydantic, existing behavior)
pip install -e .

# With pydantic-ai structured output
pip install -e ".[pydantic-ai]"

# Everything
pip install -e ".[all]"
```

## Configuration

No config changes needed — pydantic-ai is auto-detected at runtime.
When available + `[ai_provider].enabled = true`, it becomes the primary path.

## Performance

| Path | Latency | Validation | Auto-Retry |
|------|---------|------------|------------|
| pydantic-ai Agent | 3-10s | ✅ Schema-validated | ✅ With error feedback |
| llm_client direct | 3-10s | ❌ Manual JSON parse | ⚠️ Simple retry |
| subprocess CLI | 120-300s | ❌ Manual parse | ❌ No retry |

## Testing

```bash
# Run all tests (pydantic not required)
python3 -m pytest tests/ -q

# Run pydantic-specific tests
python3 -m pytest tests/test_pydantic_agents.py -v
```

## Future Work (Phase 2+)

- [x] Freeze SummaryAgent path; bug summaries stay on the explicit backend policy instead of adding another agent layer
- [ ] Use FSM in app.py handle_payload for the main routing flow
- [ ] Add Tool Calling (knowledge base search as pydantic-ai tool)
- [ ] Explore pydantic-ai Graph for multi-step analysis workflows
- [ ] Add streaming structured output for long summaries
