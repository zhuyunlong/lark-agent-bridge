# Requirement Link Source Analysis V2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a generic `requirement_analysis` route for Feishu Project workitem links that can fetch a requirement, constrain source-code analysis with a prompt contract, and output an HTML report that separates source-proven facts from unresolved items without relying on one completed-feature example.

**Architecture:** Deterministic routing identifies non-bug Project workitem links and layers onto the current route stack that already includes explicit `app_server_investigation`, repository-only `source_analysis`, and `diagram_report_followup`. The runner fetches workitem data through Meegle, converts it into neutral requirement facts, invokes existing repository source analysis with a strict prompt/output contract, then renders a Chinese HTML report with verdict, evidence matrix, unresolved items, architecture impact, and swimlane/flow diagrams. The source analysis stage is agent-assisted but bounded by prompt, structured output, read-only execution, and explicit evidence requirements.

**Tech Stack:** Python dataclasses, existing `BridgeApp` route dispatch, `meegle` CLI through tracked subprocess, existing `RepositorySourceAnalysisRunner`, HTML report helpers, focused `pytest` tests, no production hardcoded feature keywords.

---

## Design Answers

### 1. Source Analysis Is Not Free-Form Agent Guessing

The source comparison step should use an agent only as a bounded code-reading executor. The bridge must provide:

- A fixed prompt template that includes workitem title, status, description, wiki URL, comments count, user request, and normalized requirement facts.
- A required output contract:
  - `verdict`: `implemented`, `partially_implemented`, `not_found_in_current_repo`, `insufficient_evidence`, or `blocked`.
  - `matched_items`: requirement points with file/line evidence.
  - `gap_items`: requirement points not found or not fully implemented.
  - `unknown_items`: points that need owner/user/downstream confirmation.
  - `architecture_impact`: `none`, `low`, `medium`, `high`, or `unknown`, with evidence.
  - `source_evidence`: file, line, symbol, evidence text.
  - `diagram_notes`: actors and hops for report diagrams.
- A hard rule that missing evidence is reported as unknown or not found in the current repo, not inferred.
- Read-only source analysis. No file edits, no hidden production constants, no special-case business keyword mapping.

This means the agent can search and reason over source, but cannot decide the problem framing freely.

### 2. The Barrier-Gate Story Is Only a Smoke Case

The implementation must not encode assumptions from `6979403058`. That case already has source-side work and is useful only to prove the plumbing can fetch a requirement and compare with code.

The generic behavior must handle at least four outcomes:

- `implemented`: source evidence covers all main requirement facts.
- `partially_implemented`: some facts match code, some are gaps.
- `not_found_in_current_repo`: reasonable search found no implementation in configured repos.
- `insufficient_evidence`: requirement text is too vague, Meegle/wiki fields are incomplete, or source search cannot prove a conclusion.

Reports must avoid wording like “没做” unless the scope is explicitly “current configured repo did not show evidence”.

### 3. Current Route Stack Must Stay Stable

Recent code already added:

- explicit `app_server_investigation` for `auto` / `全技能自主分析`;
- repository-only `source_analysis`;
- context-based `diagram_report_followup`;
- unified HTML publish and conversation-context persistence.

This changes the requirement route design:

- `auto` / `全技能自主分析` must still be claimed by `app_server_investigation`, even when the payload is a requirement link.
- `requirement_analysis` should be inserted immediately before `_route_source_analysis`, not near the top of the dispatcher.
- requirement results must set `source_mode`, `context_profile`, and `classification_source` so the existing diagram follow-up route works without extra glue.
- external-group exception policy should not be widened in this slice. Requirement links are not log-analysis exceptions and should still be blocked in unauthorized groups unless they are normal follow-up replies to an existing analysis thread.
- requirement requests must also count as a formal analysis trigger for stale-light-interaction filtering; otherwise a valid requirement message sent before listener ready can be skipped as stale chat traffic.

---

## File Structure

- Modify: `lark_agent_bridge/models.py`
  - Add requirement-analysis config and data models:
    - `RequirementAnalysisOptions`
    - `RequirementWorkItemRef`
    - `RequirementFact`
    - `RequirementWorkItemSnapshot`
    - `RequirementSourceComparison`
    - `RequirementAnalysisRequest`
- Modify: `lark_agent_bridge/parser.py`
  - Add Project workitem URL recognition and `parse_requirement_analysis_request`.
  - Exclude requirement workitem links from generic `direct_analysis`.
- Modify: `lark_agent_bridge/config.py`
  - Load optional `[requirement_analysis]` settings.
- Modify: `config/config.example.toml`
  - Document safe defaults.
- Modify: `lark_agent_bridge/app/_shared.py`
  - Import request/parser/runner and add `requirement_analysis_request` to `_RouteContext`.
- Modify: `lark_agent_bridge/app/handle_event.py`
  - Build and route requirement analysis before source-only analysis but after higher-priority explicit routes already present in the current dispatcher.
  - Extend stale-light-interaction formal-trigger detection to include requirement analysis.
- Create: `lark_agent_bridge/requirement_analysis.py`
  - Meegle fetch client, requirement fact extraction, source prompt builder, comparison parsing, runner orchestration.
- Create: `lark_agent_bridge/reporting/requirement_report_html.py`
  - Generic Chinese HTML report renderer built on top of existing `source_report_html` helper functions.
- Modify: `tests/test_parser.py`
  - Add route-protection parser tests.
- Create: `tests/test_requirement_analysis_runner.py`
  - Unit tests for fetch, prompt constraints, structured comparison parsing, and runner orchestration.
- Modify: `tests/test_config.py`
  - Ensure the new config section is actually loaded.
- Create: `tests/test_requirement_report_html.py`
  - Unit tests for generic report sections.
- Create: `tests/test_app_requirement_analysis.py`
  - App route integration tests.

---

## Task 1: Add Requirement Models and Config

**Files:**
- Modify: `lark_agent_bridge/models.py`
- Modify: `lark_agent_bridge/config.py`
- Modify: `config/config.example.toml`
- Test: `tests/test_requirement_analysis_runner.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write failing model tests**

Create `tests/test_requirement_analysis_runner.py` with:

```python
import unittest

from lark_agent_bridge.models import (
    RequirementAnalysisRequest,
    RequirementFact,
    RequirementSourceComparison,
    RequirementWorkItemRef,
)


class RequirementModelTests(unittest.TestCase):
    def test_request_and_comparison_models_are_generic(self):
        ref = RequirementWorkItemRef(
            url="https://project.feishu.cn/demo/story/detail/12345",
            project_key="demo",
            work_item_type="story",
            work_item_id="12345",
        )
        fact = RequirementFact(
            fact_id="REQ-1",
            text="支持某功能的进入条件",
            source_field="description",
        )
        request = RequirementAnalysisRequest(
            prompt="结合源码分析是否可行",
            workitem=ref,
            raw_text="@bot https://project.feishu.cn/demo/story/detail/12345 结合源码分析是否可行",
            triggered=True,
        )
        comparison = RequirementSourceComparison(
            verdict="insufficient_evidence",
            summary="需求描述不足，当前源码证据不能证明。",
            matched_items=[],
            gap_items=[],
            unknown_items=[fact],
            architecture_impact="unknown",
            architecture_impact_reason="源码证据不足。",
            source_evidence=[],
            diagram_notes=[],
            parse_warnings=[],
        )

        self.assertEqual(request.source_mode, "requirement_source")
        self.assertEqual(request.workitem.work_item_type, "story")
        self.assertEqual(comparison.verdict, "insufficient_evidence")
        self.assertEqual(comparison.unknown_items[0].fact_id, "REQ-1")
```

- [ ] **Step 1.5: Write failing config-load test**

Add to `tests/test_config.py`:

```python
    def test_load_requirement_analysis_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                """
[requirement_analysis]
enabled = true
timeout_seconds = 180
fetch_comments = false
fetch_wiki_body = true
max_requirement_chars = 9000
max_fact_count = 12
""",
                encoding="utf-8",
            )

            config = load_config(config_path)

        opts = config.requirement_analysis
        self.assertTrue(opts.enabled)
        self.assertEqual(opts.timeout_seconds, 180)
        self.assertFalse(opts.fetch_comments)
        self.assertTrue(opts.fetch_wiki_body)
        self.assertEqual(opts.max_requirement_chars, 9000)
        self.assertEqual(opts.max_fact_count, 12)
```

- [ ] **Step 2: Run failing test**

Run:

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_requirement_analysis_runner.py::RequirementModelTests::test_request_and_comparison_models_are_generic tests/test_config.py::ConfigTests::test_load_requirement_analysis_options -q
```

Expected: fail because models do not exist.

- [ ] **Step 3: Implement models**

Add to `lark_agent_bridge/models.py` near related request models:

```python
@dataclass(slots=True)
class RequirementAnalysisOptions:
    enabled: bool = True
    timeout_seconds: int = 120
    fetch_comments: bool = True
    fetch_wiki_body: bool = False
    max_requirement_chars: int = 12000
    max_fact_count: int = 30


@dataclass(slots=True)
class RequirementWorkItemRef:
    url: str
    project_key: str
    work_item_type: str
    work_item_id: str


@dataclass(slots=True)
class RequirementFact:
    fact_id: str
    text: str
    source_field: str = ""


@dataclass(slots=True)
class RequirementWorkItemSnapshot:
    ref: RequirementWorkItemRef
    title: str = ""
    status: str = ""
    item_type_name: str = ""
    priority: str = ""
    wiki_url: str = ""
    description: str = ""
    create_time: str = ""
    update_time: str = ""
    comments_count: int | None = None
    facts: list[RequirementFact] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    fetch_warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RequirementSourceComparison:
    verdict: str
    summary: str
    matched_items: list[RequirementFact] = field(default_factory=list)
    gap_items: list[RequirementFact] = field(default_factory=list)
    unknown_items: list[RequirementFact] = field(default_factory=list)
    architecture_impact: str = "unknown"
    architecture_impact_reason: str = ""
    source_evidence: list[dict[str, Any]] = field(default_factory=list)
    diagram_notes: list[str] = field(default_factory=list)
    parse_warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RequirementAnalysisRequest:
    prompt: str
    workitem: RequirementWorkItemRef
    raw_text: str = ""
    triggered: bool = False
    error: str | None = None
    source_mode: str = "requirement_source"
    diagram_kinds: list[str] = field(default_factory=lambda: ["swimlane"])
    output_html: bool = True
    reason: str = ""
```

Add to `BridgeConfig`:

```python
    requirement_analysis: RequirementAnalysisOptions = field(default_factory=RequirementAnalysisOptions)
```

- [ ] **Step 4: Load config**

In `lark_agent_bridge/config.py`, load `[requirement_analysis]` with the same pattern used by nearby option blocks:

```python
    requirement_analysis=RequirementAnalysisOptions(
        enabled=bool(requirement_section.get("enabled", True)),
        timeout_seconds=int(requirement_section.get("timeout_seconds", 120)),
        fetch_comments=bool(requirement_section.get("fetch_comments", True)),
        fetch_wiki_body=bool(requirement_section.get("fetch_wiki_body", False)),
        max_requirement_chars=int(requirement_section.get("max_requirement_chars", 12000)),
        max_fact_count=int(requirement_section.get("max_fact_count", 30)),
    ),
```

Use the local variable name that matches the existing config loader style.

- [ ] **Step 5: Document config**

Add to `config/config.example.toml`:

```toml
[requirement_analysis]
enabled = true
timeout_seconds = 120
fetch_comments = true
fetch_wiki_body = false
max_requirement_chars = 12000
max_fact_count = 30
```

- [ ] **Step 6: Run model/config tests**

Run:

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_requirement_analysis_runner.py::RequirementModelTests::test_request_and_comparison_models_are_generic tests/test_config.py::ConfigTests::test_load_requirement_analysis_options -q
```

Expected: pass.

---

## Task 2: Generic Workitem Parser and Non-Interference

**Files:**
- Modify: `lark_agent_bridge/parser.py`
- Test: `tests/test_parser.py`

- [ ] **Step 1: Add failing parser tests**

Append tests to `tests/test_parser.py`:

```python
def test_story_link_with_requirement_wording_routes_to_requirement_analysis_not_direct():
    text = (
        "@bot https://project.feishu.cn/adcvehicleroject/story/detail/6979403058 "
        "这是需求链接，结合源码分析是否可行"
    )

    requirement = parse_requirement_analysis_request(text)
    direct = parse_direct_analysis_request(text)

    assert requirement.triggered is True
    assert requirement.workitem.project_key == "adcvehicleroject"
    assert requirement.workitem.work_item_type == "story"
    assert requirement.workitem.work_item_id == "6979403058"
    assert direct.triggered is False


def test_bug_link_with_source_wording_still_routes_to_bug_not_requirement():
    text = "@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6993883118 基于源码分析 UnityReady"

    requirement = parse_requirement_analysis_request(text)
    bug = parse_bug_request(text)

    assert bug.triggered is True
    assert requirement.triggered is False


def test_generic_url_direct_analysis_is_not_blocked_by_requirement_parser():
    text = "@bot https://example.com/log.zip 分析这份日志"

    requirement = parse_requirement_analysis_request(text)
    direct = parse_direct_analysis_request(text)

    assert requirement.triggered is False
    assert direct.triggered is True


def test_workitem_link_without_analysis_intent_does_not_trigger_requirement_analysis():
    text = "@bot https://project.feishu.cn/demo/story/detail/12345"

    requirement = parse_requirement_analysis_request(text)

    assert requirement.triggered is False
```

- [ ] **Step 2: Run failing parser tests**

Run:

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_parser.py -q
```

Expected: new tests fail because parser does not exist and generic direct still catches story URL.

- [ ] **Step 3: Implement parser**

In `lark_agent_bridge/parser.py`, add:

```python
PROJECT_WORKITEM_URL_RE = re.compile(
    r"https?://project\.feishu\.cn/"
    r"(?P<project_key>[^/\s<>\"]+)/"
    r"(?P<work_item_type>story|task|issue|requirement)/detail/"
    r"(?P<work_item_id>\d+)"
    r"(?:[/?#][^\s<>\"，。；;、)）\]】}]*)?",
    re.I,
)

REQUIREMENT_ANALYSIS_TERMS = (
    "需求",
    "需求链接",
    "需求分析",
    "story",
    "结合源码",
    "基于源码",
    "源码分析",
    "源代码",
    "源码对比",
    "可行",
    "可行性",
    "实现方案",
)
```

Add:

```python
def _project_workitem_match(text: str) -> re.Match[str] | None:
    return PROJECT_WORKITEM_URL_RE.search(_strip_leading_mentions(text or "").strip())


def parse_requirement_analysis_request(
    text: str,
    *,
    bug_url_re: re.Pattern[str] | None = None,
) -> RequirementAnalysisRequest:
    normalized_text = text or ""
    cleaned = _strip_leading_mentions(normalized_text).strip()
    empty_ref = RequirementWorkItemRef(url="", project_key="", work_item_type="", work_item_id="")
    if parse_bug_request(cleaned, bug_url_re=bug_url_re).triggered:
        return RequirementAnalysisRequest(prompt="", workitem=empty_ref, raw_text=normalized_text, triggered=False)
    match = _project_workitem_match(cleaned)
    if match is None:
        return RequirementAnalysisRequest(prompt="", workitem=empty_ref, raw_text=normalized_text, triggered=False)
    lowered = cleaned.casefold()
    if not _contains_any(cleaned, lowered, REQUIREMENT_ANALYSIS_TERMS):
        return RequirementAnalysisRequest(prompt="", workitem=empty_ref, raw_text=normalized_text, triggered=False)
    ref = RequirementWorkItemRef(
        url=match.group(0).rstrip("，。；;、)）]】}"),
        project_key=match.group("project_key"),
        work_item_type=match.group("work_item_type").lower(),
        work_item_id=match.group("work_item_id"),
    )
    return RequirementAnalysisRequest(
        prompt=cleaned,
        workitem=ref,
        raw_text=normalized_text,
        triggered=True,
        source_mode="requirement_source",
        diagram_kinds=_diagram_kinds_from_text(cleaned, default_for_chain=True),
        output_html=True,
        reason="feishu_project_workitem_source_analysis",
    )
```

In `looks_like_direct_analysis_prompt`, before checking direct action terms:

```python
    if _project_workitem_match(cleaned) is not None:
        return False
```

- [ ] **Step 4: Run parser regression**

Run:

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_parser.py -q
```

Expected: pass.

---

## Task 3: Meegle Fetch and Neutral Requirement Facts

**Files:**
- Create: `lark_agent_bridge/requirement_analysis.py`
- Test: `tests/test_requirement_analysis_runner.py`

- [ ] **Step 1: Add failing fetch/fact tests**

Add tests:

```python
import json
import subprocess
import tempfile
from pathlib import Path

from lark_agent_bridge.models import RequirementWorkItemRef
from lark_agent_bridge.requirement_analysis import MeegleWorkItemClient, extract_requirement_facts


class FakeCommandRunner:
    def __init__(self, stdout_by_prefix):
        self.stdout_by_prefix = stdout_by_prefix
        self.commands = []

    def __call__(self, command, **kwargs):
        self.commands.append(list(command))
        key = tuple(command[:3])
        stdout = self.stdout_by_prefix[key]
        return subprocess.CompletedProcess(command, 0, stdout, "")


class RequirementFetchTests(unittest.TestCase):
    def test_fetch_snapshot_and_extracts_generic_facts(self):
        ref = RequirementWorkItemRef(
            url="https://project.feishu.cn/demo/story/detail/12345",
            project_key="demo",
            work_item_type="story",
            work_item_id="12345",
        )
        runner = FakeCommandRunner(
            {
                ("meegle", "workitem", "get"): json.dumps(
                    {
                        "work_item_name": "通用功能需求",
                        "work_item_status": {"name": "设计中"},
                        "work_item_type": {"name": "需求管理"},
                        "description": {"text": "进入条件：满足 A。\n退出条件：满足 B。\n风险：依赖下游服务。"},
                        "wiki": "https://xiaopeng.feishu.cn/wiki/example",
                    },
                    ensure_ascii=False,
                ),
                ("meegle", "comment", "list"): json.dumps({"items": [], "total": 0}, ensure_ascii=False),
            }
        )
        client = MeegleWorkItemClient(command_runner=runner, fetch_comments=True)

        snapshot = client.fetch(ref, job_dir=Path(tempfile.mkdtemp()))

        self.assertEqual(snapshot.title, "通用功能需求")
        self.assertEqual(snapshot.status, "设计中")
        self.assertEqual(snapshot.comments_count, 0)
        self.assertGreaterEqual(len(snapshot.facts), 3)
        self.assertEqual(snapshot.facts[0].fact_id, "REQ-1")
        self.assertIn("进入条件", snapshot.facts[0].text)


class RequirementFactExtractionTests(unittest.TestCase):
    def test_extract_facts_is_not_business_keyword_specific(self):
        facts = extract_requirement_facts(
            "第一条：支持新入口。\n第二条：异常时保持旧链路。\n第三条：输出 HTML 报告。",
            max_fact_count=10,
        )

        self.assertEqual([fact.fact_id for fact in facts], ["REQ-1", "REQ-2", "REQ-3"])
        self.assertIn("支持新入口", facts[0].text)
        self.assertIn("HTML 报告", facts[2].text)
```

- [ ] **Step 2: Run failing tests**

Run:

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_requirement_analysis_runner.py::RequirementFetchTests tests/test_requirement_analysis_runner.py::RequirementFactExtractionTests -q
```

Expected: fail because client/extractor does not exist.

- [ ] **Step 3: Implement fetch client and fact extractor**

Create `lark_agent_bridge/requirement_analysis.py` with:

```python
"""Generic Feishu Project requirement source-analysis orchestration."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Callable

from .health import run_tracked_process
from .models import RequirementFact, RequirementWorkItemRef, RequirementWorkItemSnapshot

CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


class MeegleWorkItemClient:
    def __init__(self, *, command_runner: CommandRunner | None = None, timeout_seconds: int = 120, fetch_comments: bool = True) -> None:
        self.command_runner = command_runner or self._default_runner
        self.timeout_seconds = timeout_seconds
        self.fetch_comments = fetch_comments

    def fetch(self, ref: RequirementWorkItemRef, *, job_dir: Path, max_fact_count: int = 30) -> RequirementWorkItemSnapshot:
        raw = self._run_json(
            ["meegle", "workitem", "get", "--project-key", ref.project_key, "--work-item-id", ref.work_item_id, "--format", "json"],
            job_dir=job_dir,
            debug_name="requirement_workitem_get",
        )
        warnings: list[str] = []
        comments_count = self._fetch_comments_count(ref, job_dir=job_dir, warnings=warnings) if self.fetch_comments else None
        description = _plain_text(raw.get("description"))
        facts = extract_requirement_facts(description, max_fact_count=max_fact_count)
        return RequirementWorkItemSnapshot(
            ref=ref,
            title=str(raw.get("work_item_name") or raw.get("name") or ""),
            status=_nested_str(raw, "work_item_status", "name"),
            item_type_name=_nested_str(raw, "work_item_type", "name"),
            priority=_nested_str(raw, "priority", "label") or _nested_str(raw, "priority", "name"),
            wiki_url=str(raw.get("wiki") or ""),
            description=description,
            create_time=str(raw.get("create_time") or ""),
            update_time=str(raw.get("update_time") or ""),
            comments_count=comments_count,
            facts=facts,
            raw=raw,
            fetch_warnings=warnings,
        )

    def _fetch_comments_count(self, ref: RequirementWorkItemRef, *, job_dir: Path, warnings: list[str]) -> int | None:
        try:
            payload = self._run_json(
                ["meegle", "comment", "list", "--project-key", ref.project_key, "--work-item-id", ref.work_item_id, "--format", "json"],
                job_dir=job_dir,
                debug_name="requirement_comment_list",
            )
        except Exception as exc:
            warnings.append(f"评论拉取失败：{type(exc).__name__}: {exc}")
            return None
        total = payload.get("total")
        if isinstance(total, int):
            return total
        items = payload.get("items")
        return len(items) if isinstance(items, list) else None

    def _run_json(self, command: list[str], *, job_dir: Path, debug_name: str) -> dict[str, object]:
        completed = self.command_runner(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self.timeout_seconds,
            check=False,
            debug_log_path=job_dir / f"{debug_name}.debug.log",
        )
        if completed.returncode != 0:
            raise RuntimeError((completed.stderr or completed.stdout or "meegle command failed").strip())
        parsed = json.loads(completed.stdout or "{}")
        return parsed if isinstance(parsed, dict) else {"items": parsed}

    @staticmethod
    def _default_runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return run_tracked_process(command, watchdog=None, name="requirement_meegle", **kwargs)


def extract_requirement_facts(text: str, *, max_fact_count: int) -> list[RequirementFact]:
    normalized = re.sub(r"\r\n?", "\n", text or "").strip()
    if not normalized:
        return []
    candidates: list[str] = []
    for line in normalized.splitlines():
        stripped = re.sub(r"^\s*(?:[-*•]|\d+[.、)]|[一二三四五六七八九十]+[、.])\s*", "", line).strip()
        if stripped:
            candidates.append(stripped)
    if len(candidates) <= 1:
        candidates = [part.strip() for part in re.split(r"[。；;]\s*", normalized) if part.strip()]
    facts = []
    for index, item in enumerate(candidates[:max_fact_count], start=1):
        facts.append(RequirementFact(fact_id=f"REQ-{index}", text=item, source_field="description"))
    return facts


def _nested_str(data: dict[str, object], key: str, child: str) -> str:
    value = data.get(key)
    if isinstance(value, dict):
        return str(value.get(child) or "")
    return ""


def _plain_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return "\n".join(text for item in value.values() if (text := _plain_text(item)))
    if isinstance(value, list):
        return "\n".join(text for item in value if (text := _plain_text(item)))
    return str(value)
```

- [ ] **Step 4: Run fetch/fact tests**

Run:

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_requirement_analysis_runner.py::RequirementFetchTests tests/test_requirement_analysis_runner.py::RequirementFactExtractionTests -q
```

Expected: pass.

---

## Task 4: Constrained Source Prompt and Structured Comparison Parsing

**Files:**
- Modify: `lark_agent_bridge/requirement_analysis.py`
- Test: `tests/test_requirement_analysis_runner.py`

- [ ] **Step 1: Add failing prompt/parser tests**

Add:

```python
from lark_agent_bridge.models import RequirementWorkItemSnapshot
from lark_agent_bridge.requirement_analysis import build_requirement_source_prompt, parse_requirement_source_comparison


class RequirementSourcePromptTests(unittest.TestCase):
    def test_prompt_contains_constraints_and_no_special_case_terms(self):
        ref = RequirementWorkItemRef("https://project.feishu.cn/demo/story/detail/12345", "demo", "story", "12345")
        snapshot = RequirementWorkItemSnapshot(
            ref=ref,
            title="通用功能需求",
            status="设计中",
            description="支持新入口。\n异常时保持旧链路。",
            facts=[
                RequirementFact("REQ-1", "支持新入口", "description"),
                RequirementFact("REQ-2", "异常时保持旧链路", "description"),
            ],
        )

        prompt = build_requirement_source_prompt("结合源码分析是否可行", snapshot)

        self.assertIn("不要猜测", prompt)
        self.assertIn("verdict", prompt)
        self.assertIn("matched_items", prompt)
        self.assertIn("unknown_items", prompt)
        self.assertIn("REQ-1", prompt)
        self.assertNotIn("闸机", prompt)

    def test_parse_structured_json_block(self):
        answer = '''
结论摘要。
```json
{
  "verdict": "partially_implemented",
  "summary": "部分需求已有源码证据。",
  "matched_items": [{"fact_id": "REQ-1", "text": "支持新入口", "source_field": "description"}],
  "gap_items": [],
  "unknown_items": [{"fact_id": "REQ-2", "text": "异常时保持旧链路", "source_field": "description"}],
  "architecture_impact": "medium",
  "architecture_impact_reason": "涉及路由层。",
  "source_evidence": [{"file": "router.py", "line": 10, "symbol": "route", "text": "route()"}],
  "diagram_notes": ["用户 -> 路由 -> 执行器"]
}
```
'''

        comparison = parse_requirement_source_comparison(answer)

        self.assertEqual(comparison.verdict, "partially_implemented")
        self.assertEqual(comparison.matched_items[0].fact_id, "REQ-1")
        self.assertEqual(comparison.unknown_items[0].fact_id, "REQ-2")
        self.assertEqual(comparison.source_evidence[0]["file"], "router.py")
```

- [ ] **Step 2: Run failing tests**

Run:

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_requirement_analysis_runner.py::RequirementSourcePromptTests -q
```

Expected: fail because prompt/parser functions do not exist.

- [ ] **Step 3: Implement constrained prompt and parser**

Add to `lark_agent_bridge/requirement_analysis.py`:

```python
JSON_BLOCK_RE = re.compile(r"```json\s*(?P<body>\{.*?\})\s*```", re.S | re.I)
VALID_VERDICTS = {"implemented", "partially_implemented", "not_found_in_current_repo", "insufficient_evidence", "blocked"}
VALID_IMPACTS = {"none", "low", "medium", "high", "unknown"}


def build_requirement_source_prompt(user_prompt: str, snapshot: RequirementWorkItemSnapshot) -> str:
    facts_text = "\n".join(f"- {fact.fact_id} [{fact.source_field}]: {fact.text}" for fact in snapshot.facts)
    return "\n".join(
        part
        for part in (
            user_prompt.strip(),
            "",
            "这是一个 Feishu Project 需求链接结合源码分析请求。请只读源码，不修改文件。",
            "你必须基于当前仓库源码证据回答；不要猜测，不要把没有证据的点写成确定结论。",
            "",
            f"需求链接：{snapshot.ref.url}",
            f"需求标题：{snapshot.title}",
            f"需求状态：{snapshot.status}",
            f"需求类型：{snapshot.item_type_name}",
            f"优先级：{snapshot.priority}",
            f"Wiki：{snapshot.wiki_url}",
            "",
            "需求事实列表：",
            facts_text or "(未能从描述中提取明确事实)",
            "",
            "请输出中文分析，并在末尾输出一个 ```json fenced block，字段必须包括：",
            '- verdict: one of implemented, partially_implemented, not_found_in_current_repo, insufficient_evidence, blocked',
            "- summary: one-sentence conclusion",
            "- matched_items: array of {fact_id, text, source_field}",
            "- gap_items: array of {fact_id, text, source_field}",
            "- unknown_items: array of {fact_id, text, source_field}",
            "- architecture_impact: one of none, low, medium, high, unknown",
            "- architecture_impact_reason: string",
            "- source_evidence: array of {file, line, symbol, text}",
            "- diagram_notes: array of strings for swimlane/flow diagram hops",
            "",
            "如果源码只证明部分链路，verdict 使用 partially_implemented。",
            "如果当前配置仓库没有发现实现，只能写 not_found_in_current_repo。",
            "如果需求或源码证据不足，写 insufficient_evidence，并把原因放入 unknown_items。",
        )
        if part
    )


def parse_requirement_source_comparison(answer: str) -> RequirementSourceComparison:
    match = JSON_BLOCK_RE.search(answer or "")
    if match is None:
        return RequirementSourceComparison(
            verdict="insufficient_evidence",
            summary=_first_line(answer) or "源码分析未返回结构化结果。",
            unknown_items=[],
            architecture_impact="unknown",
            architecture_impact_reason="缺少结构化 JSON 输出。",
            parse_warnings=["source_comparison_json_missing"],
        )
    try:
        data = json.loads(match.group("body"))
    except json.JSONDecodeError as exc:
        return RequirementSourceComparison(
            verdict="insufficient_evidence",
            summary=_first_line(answer) or "源码分析结构化结果无法解析。",
            architecture_impact="unknown",
            architecture_impact_reason=f"JSON 解析失败：{exc}",
            parse_warnings=["source_comparison_json_invalid"],
        )
    verdict = str(data.get("verdict") or "insufficient_evidence")
    if verdict not in VALID_VERDICTS:
        verdict = "insufficient_evidence"
    impact = str(data.get("architecture_impact") or "unknown")
    if impact not in VALID_IMPACTS:
        impact = "unknown"
    return RequirementSourceComparison(
        verdict=verdict,
        summary=str(data.get("summary") or _first_line(answer) or ""),
        matched_items=_facts_from_json(data.get("matched_items")),
        gap_items=_facts_from_json(data.get("gap_items")),
        unknown_items=_facts_from_json(data.get("unknown_items")),
        architecture_impact=impact,
        architecture_impact_reason=str(data.get("architecture_impact_reason") or ""),
        source_evidence=data.get("source_evidence") if isinstance(data.get("source_evidence"), list) else [],
        diagram_notes=[str(item) for item in data.get("diagram_notes", [])] if isinstance(data.get("diagram_notes"), list) else [],
        parse_warnings=[],
    )


def _facts_from_json(value: object) -> list[RequirementFact]:
    if not isinstance(value, list):
        return []
    facts: list[RequirementFact] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        facts.append(
            RequirementFact(
                fact_id=str(item.get("fact_id") or ""),
                text=str(item.get("text") or ""),
                source_field=str(item.get("source_field") or ""),
            )
        )
    return facts


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()[:1200]
    return ""
```

- [ ] **Step 4: Run prompt/parser tests**

Run:

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_requirement_analysis_runner.py::RequirementSourcePromptTests -q
```

Expected: pass.

---

## Task 5: Generic HTML Report Renderer

**Files:**
- Create: `lark_agent_bridge/reporting/requirement_report_html.py`
- Test: `tests/test_requirement_report_html.py`

- [ ] **Step 1: Write failing renderer tests**

Create `tests/test_requirement_report_html.py`:

```python
import unittest

from lark_agent_bridge.models import (
    RequirementFact,
    RequirementSourceComparison,
    RequirementWorkItemRef,
    RequirementWorkItemSnapshot,
)
from lark_agent_bridge.reporting.requirement_report_html import render_requirement_analysis_report


class RequirementReportHtmlTests(unittest.TestCase):
    def test_report_renders_generic_statuses_and_unknowns(self):
        ref = RequirementWorkItemRef("https://project.feishu.cn/demo/story/detail/12345", "demo", "story", "12345")
        snapshot = RequirementWorkItemSnapshot(
            ref=ref,
            title="通用功能需求",
            status="设计中",
            facts=[
                RequirementFact("REQ-1", "支持新入口", "description"),
                RequirementFact("REQ-2", "异常时保持旧链路", "description"),
            ],
        )
        comparison = RequirementSourceComparison(
            verdict="partially_implemented",
            summary="部分需求已有源码证据。",
            matched_items=[snapshot.facts[0]],
            gap_items=[],
            unknown_items=[snapshot.facts[1]],
            architecture_impact="medium",
            architecture_impact_reason="涉及路由层。",
            source_evidence=[{"file": "router.py", "line": 10, "symbol": "route", "text": "route()"}],
            diagram_notes=["用户 -> 路由 -> 执行器"],
        )

        html = render_requirement_analysis_report(
            snapshot=snapshot,
            request_text="结合源码分析是否可行",
            raw_source_answer="原始源码分析输出",
            comparison=comparison,
            backend="source_investigation",
            warnings=["wiki 未读取"],
        )

        self.assertIn("一句话结论", html)
        self.assertIn("通用功能需求", html)
        self.assertIn("需求事实矩阵", html)
        self.assertIn("问题与未确认项", html)
        self.assertIn("泳道图", html)
        self.assertIn("router.py", html)
        self.assertIn("REQ-2", html)
        self.assertNotIn("闸机", html)
```

- [ ] **Step 2: Run failing renderer test**

Run:

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_requirement_report_html.py -q
```

Expected: fail because renderer does not exist.

- [ ] **Step 3: Implement renderer**

Create `lark_agent_bridge/reporting/requirement_report_html.py` using existing style helpers from `source_report_html`. Do not duplicate CSS. Required sections:

```python
"""HTML renderer for generic requirement source analysis reports."""

from __future__ import annotations

from .source_report_html import (
    H,
    render_cards,
    render_chain,
    render_details,
    render_document,
    render_issue_list,
    render_section,
    render_table,
)
from ..models import RequirementSourceComparison, RequirementWorkItemSnapshot


def render_requirement_analysis_report(
    *,
    snapshot: RequirementWorkItemSnapshot,
    request_text: str,
    raw_source_answer: str,
    comparison: RequirementSourceComparison,
    backend: str,
    warnings: list[str],
) -> str:
    severity = _severity(comparison.verdict)
    body = (
        '<div class="container">'
        f"<h1>{H(snapshot.title or '需求源码分析报告')}</h1>"
        f'<div class="sub">请求：{H(request_text)}<br>需求链接：{H(snapshot.ref.url)}</div>'
        f'<div class="verdict v-{severity}">一句话结论：{H(comparison.summary or "源码证据不足，详见未确认项。")}</div>'
        f'<div class="cards">{render_cards(_cards(snapshot, comparison, backend, warnings))}</div>'
        f'{render_section("需求解析结果", render_table(_snapshot_rows(snapshot), ("项目", "内容")))}'
        f'{render_section("需求事实矩阵", render_table(_fact_rows(comparison), ("状态", "Fact", "内容", "来源")))}'
        f'{render_section("源码证据", render_table(_evidence_rows(comparison.source_evidence), ("文件", "行号", "符号", "证据")))}'
        f'{render_section("问题与未确认项", render_issue_list(_issues(comparison, warnings)))}'
        f'{render_section("泳道图", _swimlane(snapshot, comparison))}'
        f'{render_chain(_chain_nodes(snapshot, comparison), title="卡点链路", description="按需求输入、源码证据、架构影响和未确认项组织。")}'
        f'{render_details("原始源码分析输出", "展开查看 agent 源码分析原文", raw_source_answer)}'
        "</div>"
    )
    return render_document(snapshot.title or "需求源码分析报告", body)
```

Also add helpers `_severity`, `_cards`, `_snapshot_rows`, `_fact_rows`, `_evidence_rows`, `_issues`, `_swimlane`, `_chain_nodes`. Keep them generic; do not include any barrier-gate-specific text.

- [ ] **Step 4: Run renderer tests**

Run:

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_requirement_report_html.py -q
```

Expected: pass.

---

## Task 6: Runner Orchestration

**Files:**
- Modify: `lark_agent_bridge/requirement_analysis.py`
- Modify: `lark_agent_bridge/source_analysis.py`
- Test: `tests/test_requirement_analysis_runner.py`

- [ ] **Step 1: Write failing runner tests for implemented and no-evidence outcomes**

Add:

```python
from lark_agent_bridge.models import BridgeConfig, RequirementAnalysisRequest, TaskResult
from lark_agent_bridge.requirement_analysis import RequirementAnalysisRunner
from tests._app_base import event


class FakeRequirementClient:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def fetch(self, ref, *, job_dir, max_fact_count=30):
        return self.snapshot


class FakeSourceRunner:
    def __init__(self, answer):
        self.answer = answer
        self.requests = []

    def run(self, request, event=None, *, progress_callback=None):
        self.requests.append(request)
        return TaskResult(
            success=True,
            message="源码分析完成",
            details={
                "mode": "source_analysis",
                "source_execution_backend": "source_investigation",
                "source_answer": self.answer,
            },
        )


class RequirementAnalysisRunnerTests(unittest.TestCase):
    def test_runner_outputs_partial_result_without_special_case_assumptions(self):
        with tempfile.TemporaryDirectory() as tmp:
            ref = RequirementWorkItemRef("https://project.feishu.cn/demo/story/detail/12345", "demo", "story", "12345")
            snapshot = RequirementWorkItemSnapshot(
                ref=ref,
                title="通用功能需求",
                status="设计中",
                facts=[
                    RequirementFact("REQ-1", "支持新入口", "description"),
                    RequirementFact("REQ-2", "异常时保持旧链路", "description"),
                ],
            )
            answer = '''
部分需求已有证据。
```json
{
  "verdict": "partially_implemented",
  "summary": "部分需求已有源码证据。",
  "matched_items": [{"fact_id": "REQ-1", "text": "支持新入口", "source_field": "description"}],
  "gap_items": [],
  "unknown_items": [{"fact_id": "REQ-2", "text": "异常时保持旧链路", "source_field": "description"}],
  "architecture_impact": "medium",
  "architecture_impact_reason": "涉及路由层。",
  "source_evidence": [{"file": "router.py", "line": 10, "symbol": "route", "text": "route()"}],
  "diagram_notes": ["用户 -> 路由 -> 执行器"]
}
```
'''
            runner = RequirementAnalysisRunner(
                BridgeConfig(dry_run=False, data_dir=Path(tmp)),
                workitem_client=FakeRequirementClient(snapshot),
                source_analysis_runner=FakeSourceRunner(answer),
            )
            request = RequirementAnalysisRequest(prompt="结合源码分析是否可行", workitem=ref, triggered=True)

            result = runner.run(request, event(event_id="evt_req_partial"))

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "requirement_analysis")
        self.assertEqual(result.details["requirement_verdict"], "partially_implemented")
        self.assertTrue(Path(result.html_report).is_file())
        self.assertIn("REQ-2", Path(result.html_report).read_text(encoding="utf-8"))
        self.assertIn("verdict", runner.source_analysis_runner.requests[0].prompt)

    def test_runner_marks_missing_json_as_insufficient_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            ref = RequirementWorkItemRef("https://project.feishu.cn/demo/story/detail/12345", "demo", "story", "12345")
            snapshot = RequirementWorkItemSnapshot(ref=ref, title="模糊需求", facts=[])
            runner = RequirementAnalysisRunner(
                BridgeConfig(dry_run=False, data_dir=Path(tmp)),
                workitem_client=FakeRequirementClient(snapshot),
                source_analysis_runner=FakeSourceRunner("我没有找到足够证据。"),
            )
            request = RequirementAnalysisRequest(prompt="结合源码分析是否可行", workitem=ref, triggered=True)

            result = runner.run(request, event(event_id="evt_req_insufficient"))

        self.assertTrue(result.success)
        self.assertEqual(result.details["requirement_verdict"], "insufficient_evidence")
        self.assertIn("source_comparison_json_missing", result.details["requirement_parse_warnings"])
```

- [ ] **Step 2: Run failing runner tests**

Run:

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_requirement_analysis_runner.py::RequirementAnalysisRunnerTests -q
```

Expected: fail because runner does not exist.

- [ ] **Step 3: Implement runner**

Add `RequirementAnalysisRunner` to `lark_agent_bridge/requirement_analysis.py`:

```python
import time

from .models import BridgeConfig, LarkEvent, RequirementAnalysisRequest, SourceAnalysisRequest, TaskResult, create_job_context
from .reporting.requirement_report_html import render_requirement_analysis_report
from .source_analysis import RepositorySourceAnalysisRunner


class RequirementAnalysisRunner:
    def __init__(
        self,
        config: BridgeConfig,
        *,
        workitem_client: MeegleWorkItemClient | None = None,
        source_analysis_runner: RepositorySourceAnalysisRunner | None = None,
    ) -> None:
        self.config = config
        self.workitem_client = workitem_client or MeegleWorkItemClient(
            timeout_seconds=config.requirement_analysis.timeout_seconds,
            fetch_comments=config.requirement_analysis.fetch_comments,
        )
        self.source_analysis_runner = source_analysis_runner or RepositorySourceAnalysisRunner(config)

    def run(self, request: RequirementAnalysisRequest, event: LarkEvent | None = None, *, progress_callback=None) -> TaskResult:
        started = time.monotonic()
        context = create_job_context(self.config.data_dir, event)
        try:
            snapshot = self.workitem_client.fetch(
                request.workitem,
                job_dir=context.logs_dir,
                max_fact_count=self.config.requirement_analysis.max_fact_count,
            )
        except Exception as exc:
            return TaskResult(
                success=False,
                message=f"需求拉取失败：{type(exc).__name__}: {exc}",
                job_id=context.job_id,
                job_dir=context.job_dir,
                error_code="requirement_fetch_failed",
                duration_seconds=time.monotonic() - started,
                details={
                    "mode": "requirement_analysis",
                    "requirement_url": request.workitem.url,
                    "work_item_id": request.workitem.work_item_id,
                },
            )
        source_prompt = build_requirement_source_prompt(request.prompt, snapshot)
        source_request = SourceAnalysisRequest(
            prompt=source_prompt[: self.config.requirement_analysis.max_requirement_chars],
            target=snapshot.title or request.workitem.work_item_id,
            raw_text=request.raw_text or request.prompt,
            triggered=True,
            source_mode="requirement_source",
            diagram_kinds=list(request.diagram_kinds or ["swimlane"]),
            output_html=True,
            reason="feishu_project_workitem_source_analysis",
        )
        source_result = self.source_analysis_runner.run(source_request, event, progress_callback=progress_callback)
        raw_answer = str(source_result.details.get("source_answer") or source_result.message or "")
        comparison = parse_requirement_source_comparison(raw_answer)
        warnings = list(snapshot.fetch_warnings) + list(comparison.parse_warnings)
        html_path = context.output_dir / "requirement_analysis_report.html"
        html_path.write_text(
            render_requirement_analysis_report(
                snapshot=snapshot,
                request_text=request.raw_text or request.prompt,
                raw_source_answer=raw_answer,
                comparison=comparison,
                backend=str(source_result.details.get("source_execution_backend") or "unknown"),
                warnings=warnings,
            ),
            encoding="utf-8",
        )
        return TaskResult(
            success=source_result.success,
            message=comparison.summary or _first_line(raw_answer) or "需求源码分析完成，详见 HTML 报告。",
            job_id=context.job_id,
            job_dir=context.job_dir,
            html_report=html_path,
            duration_seconds=time.monotonic() - started,
            error_code=None if source_result.success else "requirement_source_analysis_failed",
            details={
                "mode": "requirement_analysis",
                "source_mode": "requirement_source",
                "context_profile": "requirement_analysis",
                "classification_source": "deterministic_requirement_workitem",
                "requirement_url": request.workitem.url,
                "project_key": request.workitem.project_key,
                "work_item_type": request.workitem.work_item_type,
                "work_item_id": request.workitem.work_item_id,
                "requirement_title": snapshot.title,
                "requirement_status": snapshot.status,
                "requirement_verdict": comparison.verdict,
                "architecture_impact": comparison.architecture_impact,
                "requirement_parse_warnings": warnings,
                "source_evidence": comparison.source_evidence,
                "files_to_send": [html_path],
                "user_request_text": request.raw_text or request.prompt,
            },
        )
```

- [ ] **Step 4: Add `source_answer` details to source runner**

Modify `lark_agent_bridge/source_analysis.py`:

- In the `source_investigation` success result details, add:

```python
"source_answer": result.answer,
```

- In the app-server success result details, add:

```python
"source_answer": analysis_text,
```

- [ ] **Step 5: Run runner tests**

Run:

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_requirement_analysis_runner.py tests/test_source_analysis_runner.py -q
```

Expected: pass.

---

## Task 7: App Routing and Policy Boundaries

**Files:**
- Modify: `lark_agent_bridge/app/_shared.py`
- Modify: `lark_agent_bridge/app/handle_event.py`
- Test: `tests/test_app_requirement_analysis.py`

- [ ] **Step 1: Add failing app route tests**

Create `tests/test_app_requirement_analysis.py`:

```python
import tempfile
import unittest
from pathlib import Path

from lark_agent_bridge.app import BridgeApp
from lark_agent_bridge.models import TaskResult
from tests._app_base import BridgeConfig, FakeBugRunner, FakeLarkClient, event


class FakeRequirementRunner:
    def __init__(self, html_path: Path):
        self.html_path = html_path
        self.requests = []

    def run(self, request, event=None, *, progress_callback=None):
        self.requests.append(request)
        self.html_path.write_text("<html><body>需求源码报告</body></html>", encoding="utf-8")
        return TaskResult(
            success=True,
            message="部分需求已有源码证据。",
            job_id=event.event_id,
            html_report=self.html_path,
            details={
                "mode": "requirement_analysis",
                "source_mode": "requirement_source",
                "context_profile": "requirement_analysis",
                "classification_source": "deterministic_requirement_workitem",
                "files_to_send": [self.html_path],
                "work_item_id": request.workitem.work_item_id,
                "requirement_verdict": "partially_implemented",
            },
        )


class FakeAppServerInvestigationRunner:
    def __init__(self, html_path: Path):
        self.html_path = html_path
        self.requests = []

    def run(self, request, *, event=None, progress_callback=None):
        self.requests.append({"request": request, "event": event, "progress_callback": progress_callback})
        self.html_path.write_text("<html><body>auto investigation</body></html>", encoding="utf-8")
        return TaskResult(
            success=True,
            message="AI 自主分析完成",
            job_id=event.event_id if event else "auto_job",
            html_report=self.html_path,
            details={
                "mode": "app_server_investigation",
                "source_mode": "app_server_autonomous",
                "context_profile": "app_server_autonomous",
                "classification_source": "configured_app_server_investigation",
                "files_to_send": [self.html_path],
            },
        )


class AppRequirementAnalysisTests(unittest.TestCase):
    def test_story_link_routes_to_requirement_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_runner = FakeRequirementRunner(Path(tmp) / "requirement.html")
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"]),
                lark_client=FakeLarkClient(),
                requirement_analysis_runner=fake_runner,
            )

            result = app.handle_event(
                event(
                    event_id="evt_requirement",
                    message_id="om_requirement",
                    content="@bot https://project.feishu.cn/demo/story/detail/12345 这是需求链接，结合源码分析是否可行",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "requirement_analysis")
        self.assertEqual(fake_runner.requests[0].workitem.work_item_id, "12345")
        self.assertIn("published_report_url", result.details)

    def test_auto_requirement_request_still_routes_to_app_server_investigation(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"])
            config.bug_analysis.app_server_investigation.enabled = True
            fake_runner = FakeRequirementRunner(Path(tmp) / "requirement.html")
            fake_auto = FakeAppServerInvestigationRunner(Path(tmp) / "auto.html")
            app = BridgeApp(
                config,
                lark_client=FakeLarkClient(),
                requirement_analysis_runner=fake_runner,
                app_server_investigation_runner=fake_auto,
            )

            result = app.handle_event(
                event(
                    event_id="evt_requirement_auto",
                    message_id="om_requirement_auto",
                    content="@bot auto https://project.feishu.cn/demo/story/detail/12345 结合源码分析是否可行",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "app_server_investigation")
        self.assertEqual(len(fake_auto.requests), 1)
        self.assertEqual(fake_runner.requests, [])

    def test_bug_link_still_routes_to_bug_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / "analysis.md"
            html = Path(tmp) / "analysis.html"
            metadata.write_text("analysis", encoding="utf-8")
            html.write_text("<html></html>", encoding="utf-8")
            fake_bug = FakeBugRunner(metadata, html)
            fake_runner = FakeRequirementRunner(Path(tmp) / "requirement.html")
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"]),
                lark_client=FakeLarkClient(),
                bug_runner=fake_bug,
                requirement_analysis_runner=fake_runner,
            )

            result = app.handle_event(
                event(
                    event_id="evt_bug",
                    content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6993883118 基于源码分析 UnityReady",
                )
            )

        self.assertEqual(result.details["mode"], "bug_analysis")
        self.assertEqual(len(fake_bug.requests), 1)
        self.assertEqual(fake_runner.requests, [])

    def test_requirement_report_followup_uses_existing_diagram_report_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_runner = FakeRequirementRunner(Path(tmp) / "requirement.html")
            fake_lark = FakeLarkClient()
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"]),
                lark_client=fake_lark,
                requirement_analysis_runner=fake_runner,
            )

            first = app.handle_event(
                event(
                    event_id="evt_requirement_first",
                    message_id="om_requirement_first",
                    content="@bot https://project.feishu.cn/demo/story/detail/12345 这是需求链接，结合源码分析是否可行",
                )
            )
            followup = app.handle_event(
                event(
                    event_id="evt_requirement_diagram",
                    message_id="om_requirement_diagram",
                    reply_to="om_requirement_first",
                    content="@bot 基于这个回复画出泳道图",
                )
            )

        self.assertTrue(first.success)
        self.assertTrue(followup.success)
        self.assertEqual(followup.details["mode"], "diagram_report_followup")
        self.assertEqual(len(fake_runner.requests), 1)

    def test_requirement_link_remains_blocked_in_unauthorized_external_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_runner = FakeRequirementRunner(Path(tmp) / "requirement.html")
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_allowed"]),
                lark_client=FakeLarkClient(),
                requirement_analysis_runner=fake_runner,
            )

            result = app.handle_event(
                event(
                    event_id="evt_requirement_denied",
                    message_id="om_requirement_denied",
                    chat_id="oc_external",
                    chat_type="group",
                    content="@bot https://project.feishu.cn/demo/story/detail/12345 这是需求链接，结合源码分析是否可行",
                )
            )

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "chat_not_allowed")
        self.assertEqual(fake_runner.requests, [])

    def test_requirement_link_is_not_skipped_as_stale_light_interaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_runner = FakeRequirementRunner(Path(tmp) / "requirement.html")
            config = BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"])
            config.event_consumer.drop_stale_light_interactions = True
            config.event_consumer.stale_light_interaction_grace_seconds = 60
            app = BridgeApp(
                config,
                lark_client=FakeLarkClient(),
                requirement_analysis_runner=fake_runner,
            )
            app.activity_store.record_daemon_status(
                {
                    "ready": True,
                    "stage": "event_consumer_ready",
                    "updated_at": "2026-06-01T10:10:00+00:00",
                    "process_id": 123,
                }
            )

            result = app.handle_event(
                event(
                    event_id="evt_requirement_stale_guard",
                    message_id="om_requirement_stale_guard",
                    create_time="2026-06-01T10:00:00+00:00",
                    content="@bot https://project.feishu.cn/demo/story/detail/12345 这是需求链接，结合源码分析是否可行",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "requirement_analysis")
        self.assertEqual(len(fake_runner.requests), 1)
```

- [ ] **Step 2: Run failing app tests**

Run:

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_app_requirement_analysis.py -q
```

Expected: fail because app wiring does not exist.

- [ ] **Step 3: Wire shared imports and context**

In `lark_agent_bridge/app/_shared.py`, import:

```python
from ..models import RequirementAnalysisRequest
from ..parser import parse_requirement_analysis_request
from ..requirement_analysis import RequirementAnalysisRunner
```

Add to `_RouteContext`:

```python
    requirement_analysis_request: RequirementAnalysisRequest | None = None
```

Add these names to `__all__`.

- [ ] **Step 4: Wire app constructor**

In `_HandleEventMixin.__init__`, add parameter:

```python
        requirement_analysis_runner: RequirementAnalysisRunner | None = None,
```

After `self.source_analysis_runner`:

```python
        self.requirement_analysis_runner = requirement_analysis_runner or RequirementAnalysisRunner(
            config,
            source_analysis_runner=self.source_analysis_runner,
        )
```

- [ ] **Step 5: Build request and dispatch**

In `handle_event`, after `bug_request`:

```python
        requirement_analysis_request = parse_requirement_analysis_request(route_content, bug_url_re=self.bug_url_re)
```

Pass it into `_RouteContext`.

In `_ROUTE_HANDLERS`, place after `_route_knowledge_qa` and immediately before `_route_source_analysis`:

```python
            self._route_requirement_analysis,
```

Add method:

```python
    def _route_requirement_analysis(self, ctx: _RouteContext) -> TaskResult | None:
        request = ctx.requirement_analysis_request
        if request is None or not request.triggered:
            return None
        if not self.config.requirement_analysis.enabled:
            return None
        if ctx.bug_request is not None and getattr(ctx.bug_request, "triggered", False):
            return None
        if not self.state_store.mark_seen(ctx.event):
            return TaskResult(True, f"duplicate event skipped: {ctx.event.event_id}", skipped=True)
        self._notify_progress(
            "requirement_analysis_request_received",
            "收到需求源码分析请求",
            event=ctx.event,
            mode="requirement_analysis",
            work_item_id=request.workitem.work_item_id,
        )
        result = self.requirement_analysis_runner.run(request, ctx.event, progress_callback=lambda payload: self._notify_progress(
            str(payload.get("stage") or "requirement_analysis"),
            str(payload.get("message") or "需求源码分析"),
            event=ctx.event,
            mode="requirement_analysis",
        ))
        return self._deliver_result(ctx.event, result, request_text=request.raw_text or request.prompt)
```

Do not modify `_allow_log_analysis_in_external_group` in this feature. Requirement links should keep the existing external-group policy boundary.

Also update `_has_formal_analysis_trigger`:

```python
    def _has_formal_analysis_trigger(self, ctx: _RouteContext) -> bool:
        return any(
            bool(getattr(request, "triggered", False))
            for request in (
                ctx.bug_request,
                ctx.signal_request,
                ctx.direct_analysis_request,
                ctx.perception_request,
                ctx.rom_version_request,
                ctx.requirement_analysis_request,
            )
        )
```

Add mode label:

```python
"requirement_analysis": "需求源码分析",
```

- [ ] **Step 6: Run app route tests**

Run:

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_app_requirement_analysis.py tests/test_app_source_analysis.py tests/test_app_direct_analysis.py tests/test_app_server_investigation.py -q
```

Expected: pass. This verifies requirement routing, source-analysis non-interference, `auto` precedence, and stale-light-interaction protection.

---

## Task 8: Verification Matrix

**Files:**
- No code edits unless verification finds a regression.

- [ ] **Step 1: Focused test suite**

Run:

```bash
PYTHONPATH=. .venv/bin/python -m pytest \
  tests/test_parser.py \
  tests/test_requirement_analysis_runner.py \
  tests/test_config.py \
  tests/test_requirement_report_html.py \
  tests/test_app_requirement_analysis.py \
  tests/test_app_source_analysis.py \
  tests/test_app_direct_analysis.py \
  tests/test_source_analysis_runner.py \
  tests/test_source_report_html.py \
  -q
```

Expected: pass.

- [ ] **Step 2: Compile check**

Run:

```bash
PYTHONPATH=. .venv/bin/python -m compileall lark_agent_bridge tests/test_requirement_analysis_runner.py tests/test_requirement_report_html.py tests/test_app_requirement_analysis.py
```

Expected: no compile errors.

- [ ] **Step 3: Diff whitespace check**

Run:

```bash
git diff --check
```

Expected: no output.

- [ ] **Step 4: Meegle smoke**

Run:

```bash
meegle url decode --url 'https://project.feishu.cn/adcvehicleroject/story/detail/6979403058' --format json
meegle workitem get --project-key adcvehicleroject --work-item-id 6979403058 --format json
```

Expected:

- decode returns `workitem_detail`, `story`, and `6979403058`;
- workitem get returns title/status/description.

Do not require `--fields _all` for pass/fail. If checked and it fails with the known `page_size` type issue, record it as a warning in the report.

- [ ] **Step 5: Route smoke cases**

Use parser or app-level replay for:

```text
@bot https://project.feishu.cn/demo/story/detail/12345 这是需求链接，结合源码分析是否可行
@bot auto https://project.feishu.cn/demo/story/detail/12345 这是需求链接，结合源码分析是否可行
@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6993883118 基于源码分析 UnityReady
@bot 基于源码分析 UnityReady 信号链路如何监听
@bot https://example.com/log.zip 分析这份日志
```

Expected:

- first routes to `requirement_analysis`;
- second routes to `app_server_investigation` when the feature is enabled;
- third routes to `bug_analysis`;
- fourth routes to `source_analysis`;
- fifth routes to `direct_analysis`.

- [ ] **Step 6: Policy boundary smoke**

Verify a requirement link in an unauthorized external group is still denied unless it is a normal follow-up reply to an existing analysis context.

Expected:

- no widening of `_allow_log_analysis_in_external_group`;
- no accidental opening of generic requirement analysis in external groups.

---

## Task 9: Delivery and Release Handoff

**Files:**
- All changed files from this plan.

- [ ] **Step 1: Inspect diff scope**

Run:

```bash
git status -sb
git diff --stat
git diff -- lark_agent_bridge/models.py lark_agent_bridge/config.py lark_agent_bridge/parser.py lark_agent_bridge/app/_shared.py lark_agent_bridge/app/handle_event.py lark_agent_bridge/requirement_analysis.py lark_agent_bridge/reporting/requirement_report_html.py tests/test_requirement_analysis_runner.py tests/test_requirement_report_html.py tests/test_app_requirement_analysis.py tests/test_parser.py config/config.example.toml
```

Expected: only this feature scope plus any known pre-existing dirty files.

- [ ] **Step 2: Commit only feature files**

Run:

```bash
git add \
  lark_agent_bridge/models.py \
  lark_agent_bridge/config.py \
  lark_agent_bridge/parser.py \
  lark_agent_bridge/app/_shared.py \
  lark_agent_bridge/app/handle_event.py \
  lark_agent_bridge/requirement_analysis.py \
  lark_agent_bridge/reporting/requirement_report_html.py \
  tests/test_requirement_analysis_runner.py \
  tests/test_requirement_report_html.py \
  tests/test_app_requirement_analysis.py \
  tests/test_parser.py \
  config/config.example.toml
git commit -m "feat: add requirement link source analysis"
```

- [ ] **Step 3: Push source branch**

Run:

```bash
git push
```

Expected: push succeeds.

If release handoff is requested after implementation, use the repo's dated `release/pydantic-xpdev-YYYYMMDD` flow in a separate checked step.

---

## Self-Review Checklist

- The plan answers that source analysis is bounded by a fixed prompt/output contract, not free-form agent guessing.
- The barrier-gate story is used only as a smoke input; no source code or tests hardcode barrier-gate terms.
- Deterministic route order protects existing bug, source-only, direct-analysis, and follow-up behavior.
- Explicit `auto` / `全技能自主分析` precedence remains with `app_server_investigation`.
- Requirement analysis continues to participate in existing report follow-up generation through `context_profile` and `source_mode`.
- External-group exception policy is unchanged.
- Requirement analysis is treated as a formal analysis trigger, so it is not dropped by stale-light-interaction filtering.
- Requirement analysis can report implemented, partially implemented, not found in current repo, insufficient evidence, or blocked.
- Missing Meegle fields, wiki body not fetched, source evidence gaps, and JSON parse failures are visible warnings, not hidden assumptions.
- Complex output is an HTML report with verdict, cards, requirement facts, source evidence, unresolved items, architecture impact, and swimlane/flow content.
