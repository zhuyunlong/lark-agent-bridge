# App Server Skill Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move app-server investigation report/search differences out of global prompt and renderer into per-skill contracts, then verify no similar common-layer business coupling remains.

**Architecture:** Skill-specific report/search requirements are declared in skill metadata and exported through `skill_inventory.json`. The app-server prompt stays orchestration-only, while HTML rendering consumes the selected skill's contract and never hardcodes business skill names.

**Tech Stack:** Python dataclasses, Markdown frontmatter parsing, pytest, existing `.ai/skills/*/SKILL.md` metadata.

---

### Task 1: Export Skill Contracts

**Files:**
- Modify: `lark_agent_bridge/skill_registry.py`
- Modify: `lark_agent_bridge/skill_manager.py`
- Modify: `lark_agent_bridge/app_server_investigation.py`
- Test: `tests/test_skill_manager.py`
- Test: `tests/test_app_server_investigation_runner.py`

- [x] Extend frontmatter parsing to read simple scalar/list report contract keys.
- [x] Add `metadata`/`report_contract` to `SkillRecord.to_dict()`.
- [x] Include the contract in app-server skill inventory Markdown and JSON.
- [x] Verify inventory contains contract data for a synthetic skill and selected-skill lookup remains inventory-validated.

### Task 2: Make HTML Report Contract-Driven

**Files:**
- Modify: `lark_agent_bridge/reporting/app_server_report_html.py`
- Test: `tests/test_app_server_report_html.py`

- [x] Remove hardcoded Android/Unity skill-name set and business regex trigger.
- [x] Render `Android 最终状态` / `责任边界` missing warnings only when the selected skill report contract requires them.
- [x] Keep rendering explicit Markdown sections even when no contract exists.
- [x] Verify non-contract reports with `3D`/`Surface` text do not get forced boundary warnings.

### Task 3: Move Business Prompt Rules Into Skills

**Files:**
- Modify: `config/config.example.toml`
- Modify: `/Users/zhuyl/Documents/workspace/.ai/skills/3d-stuck-investigate/SKILL.md`
- Modify: `/Users/zhuyl/Documents/workspace/.ai/skills/scene-signal-diagnosis/SKILL.md`
- Modify: `/Users/zhuyl/Documents/workspace/.ai/skills/signal-chain-analyzer/SKILL.md`
- Modify: `/Users/zhuyl/Documents/workspace/.ai/skills/unity-startup-lifecycle-check/SKILL.md`
- Modify: `/Users/zhuyl/Documents/workspace/.ai/skills/xtheme-analyzer/SKILL.md`
- Test: `tests/test_config.py`

- [x] Rewrite global app-server prompt to describe orchestration and contract consumption only.
- [x] Add per-skill frontmatter contract for Android/Unity boundary, primary log globs, system log globs, and graphics keywords where applicable.
- [x] Verify global prompt no longer contains MonteCarlo package, GSL/SurfaceFlinger keyword list, or concrete skill names.

### Task 4: Harden Log Focus Key Logd Inclusion

**Files:**
- Modify: `lark_agent_bridge/agents/bug/custom_skill.py`
- Test: `tests/test_agents_custom_skill.py`

- [x] Replace total key-logd cap with per-prefix reservation for `kernel`, `main`, `events`, and `crash`.
- [x] Preserve the existing total candidate cap.
- [x] Verify each prefix survives when one prefix has many rotated files and MonteCarlo exceeds the cap.

### Task 5: Similarity Sweep and Verification

**Files:**
- Inspect: `lark_agent_bridge/`, `config/`, `tests/`

- [x] Search for common-layer hardcoded report contracts, business package names, and graphics keyword rules.
- [x] Classify remaining hits as intended routing/fixtures/skill-owned or fix them.
- [x] Run focused pytest for changed modules.
- [x] Run `git diff --check` in bridge repo and `.ai`.
- [x] Do memory closeout and summarize modified files.
