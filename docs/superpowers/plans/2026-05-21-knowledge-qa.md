# Personal Knowledge QA Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a first-version personal knowledge base QA path to `lark-agent-bridge` that can index configured local/Feishu knowledge and answer ADB signal simulation questions with deterministic templates.

**Architecture:** Add a focused `lark_agent_bridge.knowledge` package backed by SQLite FTS. `BridgeApp` owns a `KnowledgeService`, routes explicit `/kb`/`/qa`/知识库 questions before ordinary `omlx_chat`, and sends a card response when possible.

**Tech Stack:** Python standard library, SQLite FTS5 when available with a LIKE fallback, existing `TaskResult`, `BridgeConfig`, `LarkClient`, and Feishu interactive cards.

---

### Task 1: Tests First

**Files:**
- Create: `tests/test_knowledge.py`
- Modify: `tests/test_app.py`
- Modify: `tests/test_cards.py`
- Modify: `tests/test_config.py`

- [ ] Write tests for local JSON ingestion, ADB OTA template answers, config parsing, card rendering, and BridgeApp routing.
- [ ] Run the new tests before implementation and confirm they fail because `knowledge` APIs are missing.

### Task 2: Knowledge Core

**Files:**
- Create: `lark_agent_bridge/knowledge/__init__.py`
- Create: `lark_agent_bridge/knowledge/models.py`
- Create: `lark_agent_bridge/knowledge/store.py`
- Create: `lark_agent_bridge/knowledge/ingestors.py`
- Create: `lark_agent_bridge/knowledge/service.py`

- [ ] Implement `KnowledgeStore` with `sources`, `chunks`, and FTS/fallback search.
- [ ] Implement source ingestion for `local_json`, `guideengine_signal`, `feishu_doc`, and `feishu_base`.
- [ ] Implement deterministic ADB signal answer generation for `SIGNAL_OTA_ST`.
- [ ] Keep OMLX optional; first-version generic answers can summarize retrieved hits without depending on network/model availability.

### Task 3: Bridge Integration

**Files:**
- Modify: `lark_agent_bridge/models.py`
- Modify: `lark_agent_bridge/config.py`
- Modify: `lark_agent_bridge/app.py`
- Modify: `lark_agent_bridge/cards.py`
- Modify: `lark_agent_bridge/cli.py`
- Modify: `lark_agent_bridge/report_server.py`

- [ ] Add `[knowledge]` config model and TOML parsing.
- [ ] Instantiate `KnowledgeService` in `BridgeApp`.
- [ ] Add `_route_knowledge_qa` before `_route_omlx_chat`.
- [ ] Add `build_knowledge_answer_card`.
- [ ] Add CLI commands: `knowledge sync`, `knowledge search`, `knowledge answer`.
- [ ] Add HTTP APIs: `GET /api/knowledge/sources`, `GET /api/knowledge/search`, `POST /api/knowledge/sync`.

### Task 4: Docs And Verification

**Files:**
- Modify: `README.md`
- Modify: `config.example.toml`

- [ ] Document trigger examples and the three provided knowledge sources.
- [ ] Run targeted tests for knowledge/config/cards/app/CLI/report server.
