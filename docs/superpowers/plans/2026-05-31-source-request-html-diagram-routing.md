# Source Request HTML Diagram Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Support user requests with no file upload and no bug link when they explicitly ask for source-code analysis, while ensuring complex analysis and diagram followups produce a Chinese HTML report with a link/file and do not interfere with existing bug, direct file analysis, signal lifecycle, perception, addr2line, or ROM routes.

**Architecture:** Add a narrow deterministic source-analysis route plus a deterministic diagram/report followup route. The new source route only matches `no bug URL + no referenced resources + explicit source intent + concrete source target`, and is inserted after existing bug/direct high-priority routes but before generic signal lifecycle so requests like `基于源码分析 UnityReady 信号链路如何监听` no longer fall into missing-signal handling. The report followup route is context-only by default and generates a report from the stored conversation context unless the user starts a fresh source request.

**Tech Stack:** Python 3 standard library, existing `BridgeApp` route mixins, existing `SourceInvestigationRunner`, existing `HtmlReportPublisher`, existing `TaskResult`/`ConversationContextStore`, `unittest`/`pytest`, Chinese HTML report CSS adapted from the repo-local `3d-stuck-investigate` report primitives.

---

## Task 1: Add Parser Models And Intent Detection

- [ ] Add `SourceAnalysisRequest` and `ReportFollowupRequest` dataclasses to `lark_agent_bridge/models.py`.
- [ ] Add parser functions to `lark_agent_bridge/parser.py`:
  - `parse_source_analysis_request(text, bug_url_re=None)`
  - `parse_report_followup_request(text)`
  - `looks_like_source_analysis_prompt(text, bug_url_re=None, resources_present=False)`
  - `looks_like_report_or_diagram_request(text)`
- [ ] Detection rules:
  - Source request requires explicit source terms such as `源码`, `源代码`, `基于源码`, `从源码`, `source code`.
  - Source request requires an analysis/action term or a chain term such as `分析`, `调查`, `定位`, `链路`, `流程`, `时序`, `监听`, `注册`, `分发`.
  - Source request requires a target symbol/topic extracted from code-like identifiers, `SIGNAL_*`, camel case tokens, function-like tokens, or Chinese domain nouns after source terms.
  - Source request must not trigger when a bug URL is present.
  - Source request must not trigger when referenced resources are present; those remain direct file/log analysis.
  - Report followup triggers on `HTML 报告`, `输出报告`, `整理成 HTML`, `泳道图`, `时序图`, `流程图`, `数据流图`, `链路图`, `画图`, `画出`.
- [ ] Add parser tests in `tests/test_parser.py`:
  - `@bot 基于源码分析 UnityReady 信号链路如何监听` triggers source analysis with target `UnityReady`.
  - `@bot UnityReady 是什么` does not trigger source analysis.
  - A bug URL plus source wording does not trigger source analysis.
  - A file key plus source wording does not trigger source analysis.
  - Diagram/report wording triggers report followup intent.
- [ ] Verification command:
  - `PYTHONPATH=. .venv/bin/python -m pytest tests/test_parser.py -q`

## Task 2: Add HTML Diagram Report Renderer

- [ ] Create `lark_agent_bridge/reporting/source_report_html.py`.
- [ ] Use Chinese-first report structure:
  - one-line verdict
  - overview cards
  - red/yellow/green issue list
  - root/chain card sequence
  - swimlane diagram section
  - data-flow/process-flow section when applicable
  - evidence table
  - collapsible evidence/raw context
- [ ] Adapt CSS and helper shape from `/Users/zhuyl/Documents/workspace/xp/guideengine/.worktrees/os6_xpdev/.ai/skills/3d-stuck-investigate/scripts/report_html.py`, because the AGENTS-requested `.github/skills/shared/HTML_REPORT_STYLE.md` path is absent in this checkout.
- [ ] Ensure generated HTML is self-contained and valid UTF-8.
- [ ] Add focused renderer tests in a new `tests/test_source_report_html.py`:
  - generated report contains `泳道图`, `证据`, `结论摘要`
  - all user-provided text is escaped
  - empty evidence still renders a bounded/yellow report instead of failing
- [ ] Verification command:
  - `PYTHONPATH=. .venv/bin/python -m pytest tests/test_source_report_html.py -q`

## Task 3: Add Source Analysis Runner

- [ ] Create `lark_agent_bridge/source_analysis.py`.
- [ ] Implement `RepositorySourceAnalysisRunner`:
  - Accept `BridgeConfig`.
  - Prefer the existing Codex app-server/file-agent source-stage path when `codex_app_server.enabled` and `codex_app_server.use_for_file_agent` are configured.
  - Fall back to existing `SourceInvestigationRunner` for read-only source investigation when app-server execution is unavailable or not configured.
  - Write report files under `create_job_context(config.data_dir, event).output_dir`.
  - Generate `source_analysis_report.html` with `source_report_html`.
  - Return `TaskResult` with `mode=source_analysis`, `source_mode=repository_only`, `context_profile=source_analysis`, `classification_source=deterministic_source_request`, and `files_to_send=[html_path]`.
  - Include source evidence, coverage boundary, backend command/provider details, and `user_request_text` in details.
- [ ] Treat a failed source investigation as a successful bounded report only if it still produced a useful boundary/explanation; otherwise return a failed `TaskResult` without report upload.
- [ ] Add runner tests in `tests/test_source_analysis_runner.py` by faking the source investigation backend:
  - successful source result creates HTML report and `files_to_send`.
  - failed source backend returns `source_analysis_failed` and does not publish a fake conclusion.
- [ ] Verification command:
  - `PYTHONPATH=. .venv/bin/python -m pytest tests/test_source_analysis_runner.py -q`

## Task 4: Wire Routes Without Disturbing Existing Chains

- [ ] Update `lark_agent_bridge/app/_shared.py` imports for the new parser/model/runner.
- [ ] Update `BridgeApp.__init__` in `lark_agent_bridge/app/handle_event.py`:
  - Accept optional `source_analysis_runner`.
  - Default to `RepositorySourceAnalysisRunner(config)`.
- [ ] Build `source_analysis_request` and `report_followup_request` during event parsing.
- [ ] Extend `_RouteContext` with both request objects.
- [ ] Insert `_route_report_followup` before bug/direct/general followup so explicit `画泳道图` replies do not get consumed by generic chat followup.
- [ ] Insert `_route_source_analysis` after existing bug/direct high-priority routes and before `_route_signal_request`, so code-only chain questions beat missing signal lifecycle but bug/file routes still win.
- [ ] Add `_route_source_analysis`:
  - mark event seen
  - emit progress
  - run source runner
  - deliver with `_deliver_result`
- [ ] Add `_route_report_followup`:
  - require a `followup_context`
  - require report/diagram intent
  - generate an HTML report from context only
  - set `mode=diagram_report_followup`, preserve `conversation_root_message_id`, and deliver/publish through existing publisher
- [ ] Add `source_analysis` and `diagram_report_followup` to `_analysis_context_modes()` / `_threaded_reply_context_modes()`.
- [ ] Add card display labels for `source_analysis` and `diagram_report_followup`.
- [ ] Add app routing tests in a new `tests/test_app_source_analysis.py`:
  - no-file/no-bug source request routes to fake source runner, not signal lifecycle.
  - bug URL plus source wording still routes to fake bug runner.
  - file key plus source wording still routes to direct analysis fake bug runner.
  - followup `基于这个回复画出泳道图` generates HTML from context and publishes a report.
- [ ] Verification command:
  - `PYTHONPATH=. .venv/bin/python -m pytest tests/test_app_source_analysis.py -q`

## Task 5: Guard Existing Behavior With Regression Tests

- [ ] Run existing focused tests that cover touched surfaces:
  - `PYTHONPATH=. .venv/bin/python -m pytest tests/test_parser.py tests/test_app_direct_analysis.py::DirectAnalysisTests::test_direct_analysis_request_with_file_routes_to_bug_runner tests/test_app_direct_analysis.py::DirectAnalysisTests::test_direct_analysis_with_explicit_source_clue_sends_preflight_card_then_runs tests/test_report_server.py::ReportServerTests::test_publish_result_creates_single_link_bundle -q`
- [ ] Run import/compile validation:
  - `PYTHONPATH=. .venv/bin/python -m compileall -q lark_agent_bridge tests`
- [ ] Run whitespace diff check:
  - `git diff --check`

## Task 6: Closeout

- [ ] Review `git diff --stat` and make sure only planned files were changed.
- [ ] Confirm existing unrelated dirty files were not modified unless already changed by this task.
- [ ] Do Obsidian memory closeout:
  - If durable, update `/Users/zhuyl/Documents/Obsidian Vault/Codex记忆/项目/lark-agent-bridge.md` with the new routing boundary and report rule.
  - If no durable new rule beyond implementation, state no memory update.
- [ ] Final response must include:
  - Plan file path.
  - Changed files summary.
  - Verification commands and results.
  - Any known limitations, especially whether the runtime used app-server or the source-investigation fallback for fresh source-only requests.
