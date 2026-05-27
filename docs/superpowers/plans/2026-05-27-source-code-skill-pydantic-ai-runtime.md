# Source Code Skill and Pydantic AI Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Consolidate bug analysis routing around a unified decision protocol, promote source-code analysis into the strongest first-class capability, and replace fragile CLI file-agent execution with an in-process `pydantic-ai` runtime.

**Architecture:** Keep `bug_runner` as the outer business orchestrator. Replace the current split decision path and subprocess-heavy execution path with `UnifiedBugAnalysisDecision -> AnalysisPlan -> pydantic-ai runtime -> report/card/state`.

**Tech Stack:** Python, `pydantic-ai`, existing `LLMClient`, bridge-owned read-only tools, current report/state/card infrastructure.

---

## 1. Background

The bridge has gone through several fast iterations around bug analysis, source analysis, and skill routing.

The original problematic path was:

1. User provides a bug link and asks for real analysis.
2. Classifier selects a skill successfully.
3. The selected skill falls into `custom_skill`.
4. `custom_skill` either has no executor, or runs a fragile CLI file-agent subprocess.
5. The final summary may still proceed from weak or missing execution artifacts.

This caused two serious product failures:

- The system could produce conclusions without actual log/source execution evidence.
- Follow-up questions such as "是否分析了日志" or "重新分析" could enter a fast summary path or time clarification path instead of preserving the real prior context.

Several fixes have already landed:

- `ld-lane-level-log-analysis-portable` has been promoted to builtin `ld_lane_level`.
- `source_stage` has been introduced as an additional analysis stage.
- `SourceAnalysisDecision`, `AnalysisDecision`, and `AnalysisPlan` now exist.
- Initial bug analysis, reanalysis, and direct-analysis can append source analysis.
- CLI file-agent failure now preserves better sidecar artifacts and failure details.
- `pydantic-ai` is already integrated for intent classification.

However, the architecture is not yet fully consolidated.

## 2. Current Source State

### 2.1 Builtin LD Skill

`ld-lane-level-log-analysis-portable` is now builtin:

- File: `lark_agent_bridge/skill_registry.py`
- Mapping: `ld-lane-level-log-analysis-portable -> ld_lane_level`

This should remain a domain skill. It must not fall back to `custom_skill`.

### 2.2 Source Stage Exists, But Naming Is Not Final

The code currently defines:

```python
SOURCE_STAGE_KIND = "source_stage"
SOURCE_STAGE_KINDS = {SOURCE_STAGE_KIND}
```

The current implementation has:

- `AnalysisDecision`
- `AnalysisStage`
- `AnalysisPlan`
- `SourceAnalysisDecision`

These are a real shift toward a stage pipeline, but naming is inconsistent with the intended product concept `source_code_skill`.

### 2.3 Classification Is Still Split

Current flow:

1. `_classify_bug_request_with_agent()` selects primary skill and domain kind.
2. `_decide_source_analysis_request()` decides whether source analysis is requested.
3. `_augment_plans_for_source_analysis()` appends `source_stage` to old `BugAnalysisPlan` lists.

This works, but it is not a unified protocol. It makes debugging harder because source intent, debug shortcut, source targets, domain skill, and final plans are spread across multiple objects.

### 2.4 Direct Analysis Still Has Planning Logic In `app.py`

`_direct_analysis_preflight()` currently does more than UI preflight:

- It classifies plans.
- It calls `_decide_source_analysis_request()`.
- It creates `plans_override`.

This duplicates planning responsibility outside `bug_runner`.

The preflight layer should remain for card/UI interaction, but final plan generation must move to a unified decision service.

### 2.5 `custom_skill` Still Leaks

`custom_skill` remains in:

- `skill_manager._ROUTE_KINDS`
- route status logic
- report names
- error codes
- file-agent helper names
- artifacts such as `custom_skill_analysis.md`
- reports such as `bug_custom_skill_report.*`

This is now architectural debt. The project is fast-iterating, so compatibility aliases should not be kept longer than needed.

### 2.6 `pydantic-ai` Is Already Present

Current `pydantic-ai` integration:

- `lark_agent_bridge/agents/pydantic_agents.py`
- `lark_agent_bridge/agents/pydantic_models.py`
- `lark_agent_bridge/agents/intent_runner.py`

It is already used for intent classification, then falls back to direct API and subprocess.

Therefore the next runtime should reuse this foundation instead of building a separate custom JSON ReAct loop.

### 2.7 Existing Evidence Layers Should Be Reused

`source_investigation.py` already provides local source investigation layers:

- local probe
- codegraph
- code index
- direct API
- CLI fallback

The new source analysis runtime should reuse these as tools or evidence providers.

### 2.8 Summary Has Its Own Policy Layer

`bug_summary_policy.py` already chooses summary backend. This layer should stay.

The implementation should replace the execution backend, not remove the policy.

## 3. Target Architecture

Final intended flow:

```text
UnifiedBugAnalysisDecision
-> AnalysisPlan
-> DomainStage
-> SourceCodeStage
-> SummaryStage
-> Report / Card / State
```

Execution backend priority:

```text
pydantic-ai in-process agent
-> LLMClient direct API fallback
-> CLI subprocess fallback
```

Naming target:

```text
Product capability: source_code_skill
Internal stage: source_code
Temporary legacy name: source_stage
Deprecated: custom_skill
```

The project should move toward `source_code_skill` as the canonical capability, while avoiding further spread of `source_stage` and deleting `custom_skill` compatibility.

## 4. Unified Decision Schema

Add a canonical decision model:

```json
{
  "analysis_kind": "ld_lane_level|xtheme|scene_signal|general|source_code_skill",
  "skill": "ld-lane-level-log-analysis-portable|xtheme-analyzer|general|source_analysis",
  "source_analysis_requested": true,
  "source_targets": ["Foo.kt", "Bar::baz"],
  "debug_shortcut": false,
  "signal_hint": "",
  "reason": "",
  "decision_source": "agent|deterministic|debug_shortcut|fallback",
  "decision_confidence": "high|medium|low"
}
```

Execution plan generation must only produce:

```text
[domain]
[domain, source_code]
[source_code]
```

## 5. Provider Compatibility Model

Do not assume every OpenAI-compatible or Anthropic-compatible endpoint supports the same agent features.

Add a provider capability matrix with:

- `supports_structured_output`
- `supports_function_tools`
- `supports_multi_step_tools`
- `supports_stream_events`
- `supports_message_history`
- `supports_resume`

Runtime selection:

1. Full `pydantic-ai` agent path when structured output and function tools work.
2. Structured-only path when tools are unavailable.
3. `LLMClient` direct API fallback.
4. CLI subprocess fallback.

## 6. Tool Compatibility Model

First version tools are bridge-owned Python tools only:

- `read_file`
- `grep_text`
- `glob_paths`
- `list_dir`
- `read_bug_context`
- `read_skill_context`
- `read_report_artifact`
- `search_code_index`
- `search_codegraph`
- `read_prepared_log_metadata`

Do not expose general Bash or write tools in phase 1.

OpenAI and Anthropic native tool differences must be hidden behind `pydantic-ai` and bridge-owned tool adapters.

## 7. Skill Compatibility Model

Add a `SkillAdapter`.

Responsibilities:

- Parse `SKILL.md`.
- Convert skill metadata into system prompt fragments.
- Provide tool whitelist.
- Provide path whitelist.
- Provide output schema profile.
- Provide context profile for source analysis.

Provider-native skill mechanisms must not own bridge skill semantics.

## 8. Implementation Phases

### Phase 0: Capability Matrix

**Goal:** Know which presets can run structured output, function tools, and multi-step tool loops.

**Files:**

- Create: `lark_agent_bridge/agents/provider_capabilities.py`
- Modify: `lark_agent_bridge/models.py`
- Modify: `lark_agent_bridge/config.py`
- Test: `tests/test_provider_capabilities.py`

**Work:**

- Add capability dataclass.
- Add lightweight probe API.
- Add cached results under `data/state/provider_capabilities.json`.
- Add tests for OpenAI-like, Anthropic-like, unsupported, and fallback presets.

### Phase 1: Naming and Compatibility Cleanup

**Goal:** Stop expanding `custom_skill` and define the canonical source-analysis naming.

**Files:**

- Modify: `lark_agent_bridge/agents/bug_runner.py`
- Modify: `lark_agent_bridge/skill_manager.py`
- Modify: `lark_agent_bridge/skill_registry.py`
- Modify: `tests/test_agents.py`
- Modify: `tests/test_skill_manager.py`
- Modify: `scripts/validate_refactor_scenarios.py`

**Work:**

- Add canonical `SOURCE_CODE_KIND = "source_code_skill"` or internal `SOURCE_CODE_STAGE_KIND = "source_code"`.
- Stop returning `custom_skill` for newly normalized primary routes.
- Update report names and status keys for source code analysis.
- Keep temporary migration shims only where existing tests still require old artifacts, then delete them in Phase 10.

### Phase 2: Unified Classification

**Goal:** Replace split domain/source classification with one decision object.

**Files:**

- Modify: `lark_agent_bridge/agents/bug_runner.py`
- Modify: `lark_agent_bridge/agents/pydantic_models.py`
- Test: `tests/test_agents.py`

**Work:**

- Add `UnifiedBugAnalysisDecision`.
- Merge `_classify_bug_request_with_agent()` and `_decide_source_analysis_request()` into a single semantic decision path.
- Preserve deterministic follow-up gates.
- Preserve explicit `debug` shortcut as deterministic override.
- Produce source targets in the same decision.

### Phase 3: Plan Generation Ownership

**Goal:** Make `bug_runner` the single owner of executable plans.

**Files:**

- Modify: `lark_agent_bridge/agents/bug_runner.py`
- Modify: `lark_agent_bridge/app.py`
- Test: `tests/test_app.py`

**Work:**

- Replace `_augment_plans_for_source_analysis()` with `build_analysis_plan(decision)`.
- Make direct-analysis preflight UI-only.
- Remove plan synthesis from `_direct_analysis_preflight()`.
- Ensure initial bug, reanalysis, and direct-analysis all consume the same plan builder.

### Phase 4: Context Propagation

**Goal:** Make source-analysis context auditable everywhere.

**Files:**

- Modify: `lark_agent_bridge/agents/bug_runner.py`
- Modify: `lark_agent_bridge/state.py`
- Modify: `lark_agent_bridge/cards.py`
- Modify: `lark_agent_bridge/reporting/planners.py`
- Test: `tests/test_app.py`
- Test: `tests/test_agents.py`

**Work:**

- Persist `source_targets`.
- Persist `source_mode`.
- Persist `context_profile`.
- Persist `decision_source`.
- Include these in summary input and report JSON/HTML.

### Phase 5: Pydantic AI Runtime

**Goal:** Add reusable in-process agent runtime.

**Files:**

- Create: `lark_agent_bridge/agents/agent_runtime.py`
- Create: `lark_agent_bridge/agents/agent_tools.py`
- Create: `lark_agent_bridge/agents/agent_output_models.py`
- Modify: `lark_agent_bridge/agents/pydantic_agents.py`
- Test: `tests/test_agent_runtime.py`

**Work:**

- Build `PydanticAiAgentRuntime`.
- Inject deps with `RunContext`.
- Register bridge-owned read-only tools.
- Return structured Pydantic outputs.
- Record tool trace, usage, retries, duration, and failure reason.

### Phase 6: Source Code Skill Runtime

**Goal:** Make source analysis the strongest first-class capability.

**Files:**

- Modify: `lark_agent_bridge/agents/bug_runner.py`
- Modify: `lark_agent_bridge/agents/agent_output_models.py`
- Test: `tests/test_agents.py`
- Test: `tests/test_app.py`

**Work:**

- Replace source-stage file-agent subprocess main path with `PydanticAiAgentRuntime`.
- Read bug metadata, prepared logs, previous domain report, skill context, source targets, and source evidence.
- Output structured result.
- Render Markdown/JSON/HTML from Python.
- Require evidence references before allowing final summary.

### Phase 7: LD Lane Level Runtime

**Goal:** Move `ld_lane_level` from CLI file-agent execution to in-process agent execution.

**Files:**

- Modify: `lark_agent_bridge/agents/bug_runner.py`
- Modify: `lark_agent_bridge/agents/agent_output_models.py`
- Test: `tests/test_agents.py`
- Test: `scripts/validate_refactor_scenarios.py`

**Work:**

- Use the same runtime.
- Use LD-specific prompt and output schema.
- Use prepared log focus and existing domain inputs.
- Wrap stable domain scripts as tools when available.

### Phase 8: Summary Runtime

**Goal:** Keep summary policy but replace subprocess execution.

**Files:**

- Modify: `lark_agent_bridge/agents/bug_summary_policy.py`
- Modify: `lark_agent_bridge/agents/bug_runner.py`
- Modify: `lark_agent_bridge/reporting/planners.py`
- Test: `tests/test_agents.py`

**Work:**

- Add `BugSummaryAgent`.
- Consume domain report, source report, metadata, and follow-up context.
- Produce structured summary output.
- Render final card/report summary from Python.

### Phase 9: Skill Management UI Governance

**Goal:** Prevent future false readiness.

**Files:**

- Modify: `lark_agent_bridge/skill_manager.py`
- Modify: report server/admin UI files if present
- Test: `tests/test_skill_manager.py`

**Work:**

- Show whether a skill is builtin domain, source-code profile, auxiliary, or unrouted.
- Show runtime readiness.
- Show whether executor is in-process, subprocess fallback, or not ready.
- Do not mark a skill as bug-analysis-ready merely because it is categorized as primary.

### Phase 10: Delete Legacy `custom_skill`

**Goal:** Remove the old semantic layer.

**Files:**

- Modify: `lark_agent_bridge/agents/bug_runner.py`
- Modify: `lark_agent_bridge/skill_manager.py`
- Modify: tests and docs

**Work:**

- Delete `custom_skill` kind.
- Delete `custom_skill_analysis.md`.
- Delete `bug_custom_skill_report.*`.
- Delete `custom_skill_agent_*` error/status names.
- Rename helpers from `_run_custom_skill_agent_analysis` to source/domain-stage names or remove them if replaced.

## 9. Verification Plan

Minimum verification:

```bash
python3 -m pytest tests/test_skill_manager.py tests/test_validate_refactor_scenarios.py -q
python3 -m pytest tests/test_agents.py -k "source_stage or source_code or ld_lane_level or followup or direct_analysis"
python3 -m pytest tests/test_app.py -k "source_analysis or direct_analysis or group_bug or followup"
PYTHONPATH=. python3 scripts/validate_refactor_scenarios.py
```

Required scenario coverage:

- Bug `6998107767` still routes to `ld_lane_level`.
- `ld_lane_level + source request` produces domain plus source stages.
- `debug 描述` enters source analysis directly.
- Follow-up `重新分析` preserves prior time, selected log input, prepared log input, and source targets.
- Direct-analysis no longer builds plans in `app.py`.
- Provider without tool calling falls back predictably.

## 10. Non-Goals For First Implementation

- Do not rewrite the whole `bug_runner` into `pydantic_graph`.
- Do not introduce general Bash tools.
- Do not grant write tools to the model.
- Do not depend on `claude-offi` or `codex-offi`.
- Do not adopt Clawd-Code as the core runtime.
- Do not preserve `custom_skill` as a long-term alias.

## 11. Final Decision

The next architecture should not keep improving CLI file-agent subprocesses as the primary path.

The final direction is:

```text
source_code_skill as first-class capability
+ unified classification protocol
+ pydantic-ai in-process runtime
+ bridge-owned read-only tools
+ structured outputs rendered by Python
+ CLI subprocess only as fallback
```

