# Requirement Link Source Analysis Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a deterministic `requirement_analysis` route for Feishu Project workitem links so a story/requirement link can be fetched through Meegle, compared against source code, and delivered as a Chinese HTML report without disturbing bug, direct-file, source-only, or follow-up routes.

**Architecture:** Add a narrow parser and request model for non-bug Project workitem links, route it before `direct_analysis`, fetch workitem fields through a small Meegle client, then run a source comparison and render one combined HTML report. The report must separate fetched requirement facts, source-proven findings, and unresolved items; failures or missing permissions are reported as evidence gaps, not inferred.

**Tech Stack:** Python dataclasses, existing `BridgeApp` route dispatch, `meegle` CLI via tracked subprocess, `RepositorySourceAnalysisRunner`, existing report publisher and source report HTML style, focused `pytest` tests.

---

## File Structure

- Modify: `lark_agent_bridge/models.py`
  - Add `RequirementAnalysisRequest`, `RequirementWorkItemRef`, `RequirementWorkItemSnapshot`.
- Modify: `lark_agent_bridge/parser.py`
  - Add Project workitem URL regex and `parse_requirement_analysis_request`.
  - Exclude recognized requirement workitem links from generic `direct_analysis`.
- Modify: `lark_agent_bridge/app/_shared.py`
  - Import and expose new request, parser, and runner.
  - Add `requirement_analysis_request` to `_RouteContext`.
- Modify: `lark_agent_bridge/app/handle_event.py`
  - Build requirement request before direct route dispatch.
  - Add `_route_requirement_analysis` before source/direct analysis.
  - Add result label for `requirement_analysis`.
- Create: `lark_agent_bridge/requirement_analysis.py`
  - Fetch requirement data.
  - Build source comparison prompt.
  - Call `RepositorySourceAnalysisRunner`.
  - Render combined HTML report and return `TaskResult`.
- Create: `lark_agent_bridge/reporting/requirement_report_html.py`
  - Render Chinese HTML report using the same visual contract as current source reports.
- Modify: `tests/test_parser.py`
  - Add parser and route-protection tests.
- Create: `tests/test_requirement_analysis_runner.py`
  - Unit-test fetch/source/report orchestration with fakes.
- Create: `tests/test_requirement_report_html.py`
  - Unit-test report sections and unresolved item rendering.
- Create: `tests/test_app_requirement_analysis.py`
  - End-to-end app dispatch tests with fake runner.
- Optional docs update: `README.md`
  - Add one short example after implementation is verified.

---

## Task 1: Request Models

**Files:**
- Modify: `lark_agent_bridge/models.py`

- [ ] **Step 1: Add failing model import test**

Add to a new `tests/test_requirement_analysis_runner.py`:

```python
import unittest

from lark_agent_bridge.models import RequirementAnalysisRequest, RequirementWorkItemRef


class RequirementModelTests(unittest.TestCase):
    def test_requirement_request_carries_workitem_identity(self):
        ref = RequirementWorkItemRef(
            url="https://project.feishu.cn/adcvehicleroject/story/detail/6979403058",
            project_key="adcvehicleroject",
            work_item_type="story",
            work_item_id="6979403058",
        )
        request = RequirementAnalysisRequest(
            prompt="这是需求链接，结合源码分析是否可行",
            workitem=ref,
            raw_text="@bot https://project.feishu.cn/adcvehicleroject/story/detail/6979403058 结合源码分析是否可行",
            triggered=True,
        )

        self.assertTrue(request.triggered)
        self.assertEqual(request.workitem.project_key, "adcvehicleroject")
        self.assertEqual(request.workitem.work_item_type, "story")
        self.assertEqual(request.workitem.work_item_id, "6979403058")
        self.assertEqual(request.source_mode, "requirement_source")
        self.assertIn("swimlane", request.diagram_kinds)
```

- [ ] **Step 2: Run the failing test**

Run:

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_requirement_analysis_runner.py::RequirementModelTests::test_requirement_request_carries_workitem_identity -q
```

Expected: fail with `ImportError` or missing dataclass.

- [ ] **Step 3: Add dataclasses**

Add near `SourceAnalysisRequest` in `lark_agent_bridge/models.py`:

```python
@dataclass(slots=True)
class RequirementWorkItemRef:
    url: str
    project_key: str
    work_item_type: str
    work_item_id: str


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
    raw: dict[str, Any] = field(default_factory=dict)
    fetch_warnings: list[str] = field(default_factory=list)


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

- [ ] **Step 4: Run the model test**

Run:

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_requirement_analysis_runner.py::RequirementModelTests::test_requirement_request_carries_workitem_identity -q
```

Expected: pass.

---

## Task 2: Parser and Direct-Route Protection

**Files:**
- Modify: `lark_agent_bridge/parser.py`
- Modify: `tests/test_parser.py`

- [ ] **Step 1: Add failing parser tests**

Append to `tests/test_parser.py`:

```python
from lark_agent_bridge.parser import (
    parse_requirement_analysis_request,
    parse_direct_analysis_request,
)


def test_project_story_link_routes_to_requirement_analysis_not_direct():
    text = (
        "@bot https://project.feishu.cn/adcvehicleroject/story/detail/6979403058 "
        "这是需求链接，尝试结合源码分析是否可行"
    )

    requirement = parse_requirement_analysis_request(text)
    direct = parse_direct_analysis_request(text)

    assert requirement.triggered is True
    assert requirement.workitem.project_key == "adcvehicleroject"
    assert requirement.workitem.work_item_type == "story"
    assert requirement.workitem.work_item_id == "6979403058"
    assert requirement.workitem.url == "https://project.feishu.cn/adcvehicleroject/story/detail/6979403058"
    assert requirement.source_mode == "requirement_source"
    assert "swimlane" in requirement.diagram_kinds
    assert direct.triggered is False


def test_bug_link_never_routes_to_requirement_analysis():
    text = "@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6993883118 基于源码分析 UnityReady"

    requirement = parse_requirement_analysis_request(text)

    assert requirement.triggered is False


def test_generic_url_direct_analysis_is_not_blocked():
    text = "@bot https://example.com/log.zip 分析这份日志"

    direct = parse_direct_analysis_request(text)

    assert direct.triggered is True
```

- [ ] **Step 2: Run parser tests to see the current misroute**

Run:

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_parser.py -q
```

Expected: new requirement parser tests fail because the parser does not exist or direct still triggers.

- [ ] **Step 3: Implement workitem parser**

In `lark_agent_bridge/parser.py`, import the new models and add regex/constants near URL regexes:

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
    "story",
    "需求链接",
    "需求分析",
    "可行",
    "可行性",
    "结合源码",
    "源码分析",
    "基于源码",
    "源代码",
    "实现方案",
    "源码对比",
)
```

Add helper and parser:

```python
def _project_workitem_match(text: str) -> re.Match[str] | None:
    cleaned = _strip_leading_mentions(text or "").strip()
    return PROJECT_WORKITEM_URL_RE.search(cleaned)


def parse_requirement_analysis_request(
    text: str,
    *,
    bug_url_re: re.Pattern[str] | None = None,
) -> RequirementAnalysisRequest:
    normalized_text = text or ""
    cleaned = _strip_leading_mentions(normalized_text).strip()
    bug_request = parse_bug_request(cleaned, bug_url_re=bug_url_re)
    if bug_request.triggered:
        return RequirementAnalysisRequest(
            prompt="",
            workitem=RequirementWorkItemRef(url="", project_key="", work_item_type="", work_item_id=""),
            raw_text=normalized_text,
            triggered=False,
        )
    match = _project_workitem_match(cleaned)
    if match is None:
        return RequirementAnalysisRequest(
            prompt="",
            workitem=RequirementWorkItemRef(url="", project_key="", work_item_type="", work_item_id=""),
            raw_text=normalized_text,
            triggered=False,
        )
    lowered = cleaned.casefold()
    if not _contains_any(cleaned, lowered, REQUIREMENT_ANALYSIS_TERMS):
        return RequirementAnalysisRequest(
            prompt="",
            workitem=RequirementWorkItemRef(url="", project_key="", work_item_type="", work_item_id=""),
            raw_text=normalized_text,
            triggered=False,
        )
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
        error=None,
        source_mode="requirement_source",
        diagram_kinds=_diagram_kinds_from_text(cleaned, default_for_chain=True),
        output_html=True,
        reason="feishu_project_workitem_source_analysis",
    )
```

Update `looks_like_direct_analysis_prompt` before `has_action`:

```python
    if _project_workitem_match(cleaned) is not None:
        return False
```

- [ ] **Step 4: Run parser tests**

Run:

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_parser.py -q
```

Expected: parser tests pass; existing bug/direct tests still pass.

---

## Task 3: Meegle Workitem Fetch and Normalization

**Files:**
- Create: `lark_agent_bridge/requirement_analysis.py`
- Modify: `tests/test_requirement_analysis_runner.py`

- [ ] **Step 1: Add failing Meegle client tests**

Extend `tests/test_requirement_analysis_runner.py`:

```python
from pathlib import Path
import json
import subprocess
import tempfile

from lark_agent_bridge.models import RequirementWorkItemRef
from lark_agent_bridge.requirement_analysis import MeegleWorkItemClient


class FakeCommandRunner:
    def __init__(self):
        self.commands = []

    def __call__(self, command, **kwargs):
        self.commands.append(list(command))
        if command[:3] == ["meegle", "workitem", "get"]:
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps(
                    {
                        "work_item_name": "VLA闸机位置开放窄路辅助功能",
                        "work_item_status": {"name": "用例评审中"},
                        "work_item_type": {"name": "需求管理"},
                        "priority": {"label": "P1"},
                        "wiki": "https://xiaopeng.feishu.cn/wiki/MB1xwvdD8i5kN8kgZ93cM1HMnYb",
                        "description": {"text": "新增 NGP、LCC 场景下的闸机标志位触发窄路辅助影像。"},
                        "create_time": "2026-04-27T17:14:35+08:00",
                        "update_time": "2026-05-30T11:26:56+08:00",
                    },
                    ensure_ascii=False,
                ),
                "",
            )
        if command[:3] == ["meegle", "comment", "list"]:
            return subprocess.CompletedProcess(command, 0, json.dumps({"items": [], "total": 0}), "")
        raise AssertionError(command)


class MeegleWorkItemClientTests(unittest.TestCase):
    def test_fetch_snapshot_normalizes_basic_fields(self):
        runner = FakeCommandRunner()
        client = MeegleWorkItemClient(command_runner=runner)
        ref = RequirementWorkItemRef(
            url="https://project.feishu.cn/adcvehicleroject/story/detail/6979403058",
            project_key="adcvehicleroject",
            work_item_type="story",
            work_item_id="6979403058",
        )

        snapshot = client.fetch(ref, job_dir=Path(tempfile.mkdtemp()))

        self.assertEqual(snapshot.title, "VLA闸机位置开放窄路辅助功能")
        self.assertEqual(snapshot.status, "用例评审中")
        self.assertEqual(snapshot.item_type_name, "需求管理")
        self.assertEqual(snapshot.priority, "P1")
        self.assertIn("闸机标志位", snapshot.description)
        self.assertEqual(snapshot.comments_count, 0)
        self.assertEqual(runner.commands[0][:3], ["meegle", "workitem", "get"])
```

- [ ] **Step 2: Run the failing client test**

Run:

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_requirement_analysis_runner.py::MeegleWorkItemClientTests::test_fetch_snapshot_normalizes_basic_fields -q
```

Expected: fail because `requirement_analysis.py` does not exist.

- [ ] **Step 3: Implement client**

Create `lark_agent_bridge/requirement_analysis.py` with these initial pieces:

```python
"""Feishu Project requirement source-analysis orchestration."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Callable

from .health import run_tracked_process
from .models import RequirementWorkItemRef, RequirementWorkItemSnapshot


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


class MeegleWorkItemClient:
    def __init__(self, *, command_runner: CommandRunner | None = None, timeout_seconds: int = 120) -> None:
        self.command_runner = command_runner or self._default_runner
        self.timeout_seconds = timeout_seconds

    def fetch(self, ref: RequirementWorkItemRef, *, job_dir: Path) -> RequirementWorkItemSnapshot:
        raw = self._run_json(
            [
                "meegle",
                "workitem",
                "get",
                "--project-key",
                ref.project_key,
                "--work-item-id",
                ref.work_item_id,
                "--format",
                "json",
            ],
            job_dir=job_dir,
            debug_name="requirement_workitem_get",
        )
        warnings: list[str] = []
        comments_count = self._try_fetch_comments_count(ref, job_dir=job_dir, warnings=warnings)
        return RequirementWorkItemSnapshot(
            ref=ref,
            title=str(raw.get("work_item_name") or raw.get("name") or ""),
            status=_nested_str(raw, "work_item_status", "name"),
            item_type_name=_nested_str(raw, "work_item_type", "name"),
            priority=_nested_str(raw, "priority", "label") or _nested_str(raw, "priority", "name"),
            wiki_url=str(raw.get("wiki") or ""),
            description=_plain_text(raw.get("description")),
            create_time=str(raw.get("create_time") or ""),
            update_time=str(raw.get("update_time") or ""),
            comments_count=comments_count,
            raw=raw,
            fetch_warnings=warnings,
        )

    def _try_fetch_comments_count(
        self,
        ref: RequirementWorkItemRef,
        *,
        job_dir: Path,
        warnings: list[str],
    ) -> int | None:
        try:
            comments = self._run_json(
                [
                    "meegle",
                    "comment",
                    "list",
                    "--project-key",
                    ref.project_key,
                    "--work-item-id",
                    ref.work_item_id,
                    "--format",
                    "json",
                ],
                job_dir=job_dir,
                debug_name="requirement_comment_list",
            )
        except Exception as exc:
            warnings.append(f"评论拉取失败：{type(exc).__name__}: {exc}")
            return None
        total = comments.get("total")
        if isinstance(total, int):
            return total
        items = comments.get("items")
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
        payload = json.loads(completed.stdout or "{}")
        return payload if isinstance(payload, dict) else {"items": payload}

    @staticmethod
    def _default_runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return run_tracked_process(command, watchdog=None, name="requirement_meegle", **kwargs)


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
        chunks: list[str] = []
        for item in value.values():
            text = _plain_text(item)
            if text:
                chunks.append(text)
        return "\n".join(chunks)
    if isinstance(value, list):
        return "\n".join(text for item in value if (text := _plain_text(item)))
    return str(value)
```

- [ ] **Step 4: Run client tests**

Run:

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_requirement_analysis_runner.py -q
```

Expected: model and client tests pass.

---

## Task 4: Requirement HTML Report Renderer

**Files:**
- Create: `lark_agent_bridge/reporting/requirement_report_html.py`
- Create: `tests/test_requirement_report_html.py`

- [ ] **Step 1: Add failing renderer test**

Create `tests/test_requirement_report_html.py`:

```python
import unittest

from lark_agent_bridge.models import RequirementWorkItemRef, RequirementWorkItemSnapshot
from lark_agent_bridge.reporting.requirement_report_html import render_requirement_analysis_report


class RequirementReportHtmlTests(unittest.TestCase):
    def test_report_contains_requirement_source_evidence_and_unknowns(self):
        ref = RequirementWorkItemRef(
            url="https://project.feishu.cn/adcvehicleroject/story/detail/6979403058",
            project_key="adcvehicleroject",
            work_item_type="story",
            work_item_id="6979403058",
        )
        snapshot = RequirementWorkItemSnapshot(
            ref=ref,
            title="VLA闸机位置开放窄路辅助功能",
            status="用例评审中",
            description="FOV 左右 ±60°，距离 < 5m 时触发。",
            wiki_url="https://xiaopeng.feishu.cn/wiki/MB1xwvdD8i5kN8kgZ93cM1HMnYb",
        )

        html = render_requirement_analysis_report(
            snapshot=snapshot,
            request_text="结合源码分析是否可行",
            source_answer="有条件可行：GuideEngine 已有闸机距离通道。",
            source_evidence=[{"file": "barrier_gate_handler.cpp", "line": 563, "text": "applyFsmAndEmit"}],
            coverage_boundary="未证明窄路影像触发策略。",
            issues=["距离阈值不在当前模块实现。"],
            backend="source_investigation",
            success=True,
        )

        self.assertIn("一句话结论", html)
        self.assertIn("VLA闸机位置开放窄路辅助功能", html)
        self.assertIn("需求与源码对比", html)
        self.assertIn("泳道图", html)
        self.assertIn("问题与未确认项", html)
        self.assertIn("barrier_gate_handler.cpp", html)
        self.assertIn("距离阈值不在当前模块实现", html)
```

- [ ] **Step 2: Run the failing renderer test**

Run:

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_requirement_report_html.py -q
```

Expected: fail because renderer does not exist.

- [ ] **Step 3: Implement renderer**

Create `lark_agent_bridge/reporting/requirement_report_html.py` using existing `source_report_html` helpers:

```python
"""HTML renderer for requirement-link source analysis."""

from __future__ import annotations

from typing import Iterable, Mapping

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
from ..models import RequirementWorkItemSnapshot


def render_requirement_analysis_report(
    *,
    snapshot: RequirementWorkItemSnapshot,
    request_text: str,
    source_answer: str,
    source_evidence: Iterable[Mapping[str, object]],
    coverage_boundary: str,
    issues: list[str],
    backend: str,
    success: bool,
) -> str:
    evidence = list(source_evidence or [])
    severity = "green" if success and evidence and not issues else "yellow" if success else "red"
    issue_rows = [{"sev": "yellow", "title": item, "detail": "该点未由当前源码证据闭环，需用户或模块 owner 判断。"} for item in issues]
    if not issue_rows:
        issue_rows = [{"sev": "green", "title": "未发现阻断项", "detail": "仍以源码证据范围为准。"}]
    body = (
        '<div class="container">'
        f"<h1>{H(snapshot.title or '需求源码分析报告')}</h1>"
        f'<div class="sub">请求：{H(request_text)}<br>需求链接：{H(snapshot.ref.url)}</div>'
        f'<div class="verdict v-{severity}">一句话结论：{H(_first_line(source_answer) or "已完成需求与源码对比，详见问题与未确认项。")}</div>'
        f'<div class="cards">{render_cards(_cards(snapshot, evidence, backend, severity))}</div>'
        f'{render_section("需求解析结果", render_table(_requirement_rows(snapshot), ("项目", "内容")))}'
        f'{render_section("需求与源码对比", f"<pre>{H(source_answer or "源码分析未返回摘要。")}</pre>")}'
        f'{render_section("泳道图", _swimlane(snapshot, evidence, issues))}'
        f'{render_chain(_chain_nodes(snapshot, evidence, issues), title="卡点链路", description="按需求输入、需求系统、源码证据和用户判断组织。")}'
        f'{render_section("问题与未确认项", render_issue_list(issue_rows))}'
        f'{render_section("源码证据", render_table(_evidence_rows(evidence), ("文件", "行号", "证据")))}'
        f'{render_details("边界说明", "展开查看源码覆盖范围与限制", coverage_boundary or "未返回覆盖边界。")}'
        "</div>"
    )
    return render_document(snapshot.title or "需求源码分析报告", body)
```

Also add small helpers in the same file:

```python
def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def _cards(snapshot: RequirementWorkItemSnapshot, evidence: list[Mapping[str, object]], backend: str, severity: str):
    return [
        ("需求状态", snapshot.status or "未知", "green" if snapshot.status else "yellow", ""),
        ("源码证据", str(len(evidence)), "green" if evidence else "yellow", ""),
        ("结论级别", "有条件" if severity == "yellow" else "通过" if severity == "green" else "失败", severity, ""),
        ("后端", backend or "unknown", "green" if backend else "yellow", ""),
    ]


def _requirement_rows(snapshot: RequirementWorkItemSnapshot):
    return [
        ("WorkItem", f"{snapshot.ref.project_key}/{snapshot.ref.work_item_type}/{snapshot.ref.work_item_id}"),
        ("标题", snapshot.title or "(空)"),
        ("状态", snapshot.status or "(空)"),
        ("类型", snapshot.item_type_name or "(空)"),
        ("优先级", snapshot.priority or "(空)"),
        ("Wiki", snapshot.wiki_url or "(空)"),
        ("更新时间", snapshot.update_time or "(空)"),
        ("描述摘录", (snapshot.description or "(空)")[:1600]),
    ]


def _evidence_rows(evidence: list[Mapping[str, object]]):
    return [
        (item.get("file", ""), item.get("line", ""), item.get("text", ""))
        for item in evidence
    ]


def _swimlane(snapshot: RequirementWorkItemSnapshot, evidence: list[Mapping[str, object]], issues: list[str]) -> str:
    return (
        '<div class="swimlane">'
        '<div class="lane"><div class="lane-title">用户输入</div>'
        f'<div class="lane-step"><div class="name">需求链接</div><div class="desc">{H(snapshot.ref.url)}</div></div></div>'
        '<div class="lane"><div class="lane-title">需求系统</div>'
        f'<div class="lane-step"><div class="name">WorkItem</div><div class="desc">{H(snapshot.title or snapshot.ref.work_item_id)}</div></div></div>'
        '<div class="lane"><div class="lane-title">源码仓</div>'
        f'<div class="lane-step"><div class="name">证据</div><div class="desc">{H(str(len(evidence)) + " 条源码证据")}</div></div></div>'
        '<div class="lane"><div class="lane-title">输出判断</div>'
        f'<div class="lane-step"><div class="name">未确认项</div><div class="desc">{H(str(len(issues)) + " 项")}</div></div></div>'
        '</div>'
    )


def _chain_nodes(snapshot: RequirementWorkItemSnapshot, evidence: list[Mapping[str, object]], issues: list[str]):
    return [
        {"sev": "green", "title": "需求链接解析", "evidence": snapshot.ref.url, "downstream": "获取需求标题、状态、描述和 wiki。"},
        {"sev": "green" if evidence else "yellow", "title": "源码对比", "evidence": f"{len(evidence)} 条源码证据", "downstream": "形成可行性判断和覆盖边界。"},
        {"sev": "yellow" if issues else "green", "title": "问题收口", "evidence": f"{len(issues)} 项未确认", "downstream": "不能由源码证明的点交给用户判断。"},
    ]
```

- [ ] **Step 4: Run renderer tests**

Run:

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_requirement_report_html.py -q
```

Expected: pass.

---

## Task 5: Requirement Analysis Runner

**Files:**
- Modify: `lark_agent_bridge/requirement_analysis.py`
- Modify: `tests/test_requirement_analysis_runner.py`

- [ ] **Step 1: Add failing runner orchestration test**

Extend `tests/test_requirement_analysis_runner.py`:

```python
from lark_agent_bridge.models import BridgeConfig, RequirementAnalysisRequest, SourceAnalysisRequest, TaskResult
from lark_agent_bridge.requirement_analysis import RequirementAnalysisRunner
from tests._app_base import event


class FakeRequirementClient:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.refs = []

    def fetch(self, ref, *, job_dir):
        self.refs.append(ref)
        return self.snapshot


class FakeRepositorySourceRunner:
    def __init__(self):
        self.requests = []

    def run(self, request, event=None, *, progress_callback=None):
        self.requests.append(request)
        return TaskResult(
            success=True,
            message="有条件可行：GuideEngine 已有闸机距离通道。",
            job_id=event.event_id if event else "source_job",
            html_report=None,
            details={
                "mode": "source_analysis",
                "source_execution_backend": "source_investigation",
                "source_answer": "有条件可行：GuideEngine 已有闸机距离通道。\n未证明窄路影像触发策略。",
                "source_evidence": [{"file": "barrier_gate_handler.cpp", "line": 563, "text": "applyFsmAndEmit"}],
                "coverage_boundary": "未证明窄路影像触发策略。",
            },
        )


class RequirementAnalysisRunnerTests(unittest.TestCase):
    def test_runner_fetches_requirement_runs_source_and_writes_combined_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            ref = RequirementWorkItemRef(
                url="https://project.feishu.cn/adcvehicleroject/story/detail/6979403058",
                project_key="adcvehicleroject",
                work_item_type="story",
                work_item_id="6979403058",
            )
            snapshot = RequirementWorkItemSnapshot(
                ref=ref,
                title="VLA闸机位置开放窄路辅助功能",
                status="用例评审中",
                description="FOV 左右 ±60°，距离 < 5m 时触发。",
            )
            fake_source = FakeRepositorySourceRunner()
            runner = RequirementAnalysisRunner(
                BridgeConfig(dry_run=False, data_dir=Path(tmp)),
                workitem_client=FakeRequirementClient(snapshot),
                source_analysis_runner=fake_source,
            )
            request = RequirementAnalysisRequest(
                prompt="这是需求链接，结合源码分析是否可行",
                workitem=ref,
                raw_text="@bot https://project.feishu.cn/adcvehicleroject/story/detail/6979403058 结合源码分析是否可行",
                triggered=True,
            )

            result = runner.run(request, event(event_id="evt_req_source"))

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "requirement_analysis")
        self.assertEqual(result.details["source_mode"], "requirement_source")
        self.assertEqual(result.details["work_item_id"], "6979403058")
        self.assertTrue(Path(result.html_report).is_file())
        html = Path(result.html_report).read_text(encoding="utf-8")
        self.assertIn("VLA闸机位置开放窄路辅助功能", html)
        self.assertIn("barrier_gate_handler.cpp", html)
        self.assertEqual(fake_source.requests[0].source_mode, "requirement_source")
        self.assertIn("需求描述", fake_source.requests[0].prompt)
```

- [ ] **Step 2: Run the failing runner test**

Run:

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_requirement_analysis_runner.py::RequirementAnalysisRunnerTests::test_runner_fetches_requirement_runs_source_and_writes_combined_report -q
```

Expected: fail because `RequirementAnalysisRunner` does not exist.

- [ ] **Step 3: Implement runner**

Append to `lark_agent_bridge/requirement_analysis.py`:

```python
import time
from typing import Any

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
        self.workitem_client = workitem_client or MeegleWorkItemClient()
        self.source_analysis_runner = source_analysis_runner or RepositorySourceAnalysisRunner(config)

    def run(
        self,
        request: RequirementAnalysisRequest,
        event: LarkEvent | None = None,
        *,
        progress_callback: Callable[[dict[str, object]], None] | None = None,
    ) -> TaskResult:
        started = time.monotonic()
        context = create_job_context(self.config.data_dir, event)
        self._emit(progress_callback, "requirement_fetch_started", "拉取需求详情", work_item_id=request.workitem.work_item_id)
        try:
            snapshot = self.workitem_client.fetch(request.workitem, job_dir=context.logs_dir)
        except Exception as exc:
            return TaskResult(
                success=False,
                message=f"需求拉取失败：{type(exc).__name__}: {exc}",
                job_id=context.job_id,
                job_dir=context.job_dir,
                duration_seconds=time.monotonic() - started,
                error_code="requirement_fetch_failed",
                details={
                    "mode": "requirement_analysis",
                    "source_mode": request.source_mode,
                    "work_item_id": request.workitem.work_item_id,
                    "requirement_url": request.workitem.url,
                },
            )

        source_request = SourceAnalysisRequest(
            prompt=_build_source_prompt(request, snapshot),
            target=snapshot.title or f"{snapshot.ref.work_item_type}-{snapshot.ref.work_item_id}",
            raw_text=request.raw_text or request.prompt,
            triggered=True,
            source_mode="requirement_source",
            diagram_kinds=list(request.diagram_kinds or ["swimlane"]),
            output_html=True,
            reason="feishu_project_workitem_source_analysis",
        )
        self._emit(progress_callback, "requirement_source_started", "结合源码分析需求", work_item_id=request.workitem.work_item_id)
        source_result = self.source_analysis_runner.run(source_request, event, progress_callback=progress_callback)
        details = dict(source_result.details or {})
        source_answer = str(details.get("source_answer") or source_result.message or "")
        source_evidence = details.get("source_evidence") if isinstance(details.get("source_evidence"), list) else []
        coverage_boundary = str(details.get("coverage_boundary") or "")
        issues = _derive_issues(snapshot, source_result, coverage_boundary)
        html_path = context.output_dir / "requirement_analysis_report.html"
        html_path.write_text(
            render_requirement_analysis_report(
                snapshot=snapshot,
                request_text=request.raw_text or request.prompt,
                source_answer=source_answer,
                source_evidence=source_evidence,
                coverage_boundary=coverage_boundary,
                issues=issues,
                backend=str(details.get("source_execution_backend") or "unknown"),
                success=source_result.success,
            ),
            encoding="utf-8",
        )
        self._emit(progress_callback, "requirement_analysis_completed", "需求源码分析完成", work_item_id=request.workitem.work_item_id)
        return TaskResult(
            success=source_result.success,
            message=_first_line(source_answer) or ("需求源码分析完成。" if source_result.success else "需求源码分析未完成，详见 HTML 报告。"),
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
                "source_backend": str(details.get("source_execution_backend") or "unknown"),
                "source_evidence": source_evidence,
                "coverage_boundary": coverage_boundary,
                "requirement_warnings": list(snapshot.fetch_warnings),
                "files_to_send": [html_path],
                "user_request_text": request.raw_text or request.prompt,
            },
        )

    def _emit(self, progress_callback: Callable[[dict[str, object]], None] | None, stage: str, message: str, **details: object) -> None:
        if progress_callback is not None:
            progress_callback({"stage": stage, "message": message, "details": details})
```

Add helper functions in the same file:

```python
def _build_source_prompt(request: RequirementAnalysisRequest, snapshot: RequirementWorkItemSnapshot) -> str:
    return "\n".join(
        part
        for part in (
            request.prompt.strip(),
            "",
            "这是一个 Feishu Project 需求链接结合源码分析请求。",
            f"需求标题：{snapshot.title}",
            f"需求状态：{snapshot.status}",
            f"需求类型：{snapshot.item_type_name}",
            f"优先级：{snapshot.priority}",
            f"Wiki：{snapshot.wiki_url}",
            "",
            "需求描述：",
            snapshot.description,
            "",
            "请结合当前源码分析该需求是否可行。输出必须区分：",
            "1. 需求中已经能由源码证明的点；",
            "2. 源码未证明、需要用户或模块 owner 判断的点；",
            "3. 对当前架构/路由/链路的影响；",
            "4. 关键源码文件和行号证据；",
            "5. 如果涉及数据流、时序、流程或链路，请给出泳道图/链路图描述。",
            "不要猜测；证据不足时明确写成未确认项。",
        )
        if part
    )


def _derive_issues(snapshot: RequirementWorkItemSnapshot, source_result: TaskResult, coverage_boundary: str) -> list[str]:
    issues = list(snapshot.fetch_warnings)
    if not source_result.success:
        issues.append(source_result.message or "源码分析执行失败。")
    lowered = (coverage_boundary or "").casefold()
    if "未证明" in coverage_boundary or "not proven" in lowered or "不足" in coverage_boundary:
        issues.append(coverage_boundary)
    if snapshot.wiki_url and "wiki" not in (snapshot.description or "").casefold():
        issues.append("需求存在 wiki 链接；当前实现只拉取 workitem 字段，不自动读取 wiki 正文。")
    return [item for item in issues if item]
```

- [ ] **Step 4: Expose source answer in `RepositorySourceAnalysisRunner`**

Modify `lark_agent_bridge/source_analysis.py` so both success branches include a `source_answer` detail:

```python
"source_answer": result.answer,
```

in the `source_investigation` success result, and:

```python
"source_answer": analysis_text,
```

in the app-server success result.

- [ ] **Step 5: Run runner tests**

Run:

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_requirement_analysis_runner.py tests/test_source_analysis_runner.py -q
```

Expected: pass.

---

## Task 6: App Route Wiring

**Files:**
- Modify: `lark_agent_bridge/app/_shared.py`
- Modify: `lark_agent_bridge/app/handle_event.py`
- Create: `tests/test_app_requirement_analysis.py`

- [ ] **Step 1: Add failing app dispatch tests**

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
            message="有条件可行：已结合源码输出需求分析。",
            job_id=event.event_id,
            html_report=self.html_path,
            details={
                "mode": "requirement_analysis",
                "source_mode": "requirement_source",
                "context_profile": "requirement_analysis",
                "classification_source": "deterministic_requirement_workitem",
                "files_to_send": [self.html_path],
                "work_item_id": request.workitem.work_item_id,
            },
        )


class AppRequirementAnalysisTests(unittest.TestCase):
    def test_story_link_routes_to_requirement_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_lark = FakeLarkClient()
            fake_runner = FakeRequirementRunner(Path(tmp) / "requirement.html")
            app = BridgeApp(
                BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"]),
                lark_client=fake_lark,
                requirement_analysis_runner=fake_runner,
            )

            result = app.handle_event(
                event(
                    event_id="evt_requirement",
                    message_id="om_requirement",
                    content="@bot https://project.feishu.cn/adcvehicleroject/story/detail/6979403058 这是需求链接，结合源码分析是否可行",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.details["mode"], "requirement_analysis")
        self.assertEqual(len(fake_runner.requests), 1)
        self.assertEqual(fake_runner.requests[0].workitem.work_item_id, "6979403058")
        self.assertIn("published_report_url", result.details)

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
                    event_id="evt_bug_still_bug",
                    content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6993883118 基于源码分析 UnityReady",
                )
            )

        self.assertEqual(result.details["mode"], "bug_analysis")
        self.assertEqual(len(fake_bug.requests), 1)
        self.assertEqual(fake_runner.requests, [])
```

- [ ] **Step 2: Run failing app test**

Run:

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_app_requirement_analysis.py -q
```

Expected: fail because `BridgeApp` does not accept or dispatch `requirement_analysis_runner`.

- [ ] **Step 3: Wire imports and context**

In `lark_agent_bridge/app/_shared.py`:

```python
from ..models import RequirementAnalysisRequest
from ..parser import parse_requirement_analysis_request
from ..requirement_analysis import RequirementAnalysisRunner
```

Add to `_RouteContext`:

```python
    requirement_analysis_request: RequirementAnalysisRequest | None = None
```

Add names to `__all__`.

- [ ] **Step 4: Wire `BridgeApp` initialization**

In `_HandleEventMixin.__init__` add parameter:

```python
        requirement_analysis_runner: RequirementAnalysisRunner | None = None,
```

After `self.source_analysis_runner` initialization:

```python
        self.requirement_analysis_runner = requirement_analysis_runner or RequirementAnalysisRunner(
            config,
            source_analysis_runner=self.source_analysis_runner,
        )
```

- [ ] **Step 5: Build request before direct dispatch**

In `handle_event`, after `bug_request` and before or next to direct request:

```python
        requirement_analysis_request = parse_requirement_analysis_request(route_content, bug_url_re=self.bug_url_re)
```

Pass into `_RouteContext`:

```python
            requirement_analysis_request=requirement_analysis_request,
```

- [ ] **Step 6: Add route handler before source/direct**

In `_ROUTE_HANDLERS`, place after `_route_bug_intent` and before addr2line/source/direct:

```python
            self._route_requirement_analysis,  # 7. Feishu Project requirement + source analysis
```

Add method:

```python
    def _route_requirement_analysis(self, ctx: _RouteContext) -> TaskResult | None:
        request = ctx.requirement_analysis_request
        if request is None or not request.triggered:
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

        def _progress(payload: dict[str, object]) -> None:
            details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
            progress_details = dict(details)
            progress_details.setdefault("mode", "requirement_analysis")
            self._notify_progress(
                str(payload.get("stage") or "requirement_analysis"),
                str(payload.get("message") or "需求源码分析"),
                event=ctx.event,
                **progress_details,
            )

        result = self.requirement_analysis_runner.run(request, ctx.event, progress_callback=_progress)
        return self._deliver_result(ctx.event, result, request_text=request.raw_text or request.prompt)
```

- [ ] **Step 7: Add mode label**

In `_mode_display_name` mapping, add:

```python
"requirement_analysis": "需求源码分析",
```

- [ ] **Step 8: Run app tests**

Run:

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_app_requirement_analysis.py tests/test_app_source_analysis.py tests/test_app_direct_analysis.py -q
```

Expected: pass; story links route to requirement, bug links still route to bug, file/direct routes remain unchanged.

---

## Task 7: Intent Fallback and Help Text Boundaries

**Files:**
- Modify: `lark_agent_bridge/parser.py`
- Optional modify: `README.md`

- [ ] **Step 1: Confirm deterministic route is enough**

Run this parser smoke check:

```bash
PYTHONPATH=. .venv/bin/python - <<'PY'
from lark_agent_bridge.parser import parse_requirement_analysis_request, parse_direct_analysis_request
text='https://project.feishu.cn/adcvehicleroject/story/detail/6979403058 这是需求链接，结合源码分析是否可行'
print(parse_requirement_analysis_request(text))
print(parse_direct_analysis_request(text))
PY
```

Expected: requirement triggered, direct not triggered.

- [ ] **Step 2: Keep intent fallback unchanged**

Do not teach the LLM intent fallback a new route in this slice unless deterministic tests show a missed case. The route is URL-shape based and should not depend on model classification.

- [ ] **Step 3: Add one README example after tests pass**

Add under existing usage examples:

```markdown
@My Feishu CLI Bot https://project.feishu.cn/<project>/story/detail/<id> 这是需求链接，结合源码分析是否可行
```

This is documentation only after route behavior is verified.

---

## Task 8: Focused Verification

**Files:**
- No source edits unless tests expose a defect.

- [ ] **Step 1: Run focused parser/app/report/runner tests**

Run:

```bash
PYTHONPATH=. .venv/bin/python -m pytest \
  tests/test_parser.py \
  tests/test_requirement_analysis_runner.py \
  tests/test_requirement_report_html.py \
  tests/test_app_requirement_analysis.py \
  tests/test_app_source_analysis.py \
  tests/test_app_direct_analysis.py \
  tests/test_source_report_html.py \
  -q
```

Expected: pass.

- [ ] **Step 2: Run compile check**

Run:

```bash
PYTHONPATH=. .venv/bin/python -m compileall lark_agent_bridge tests/test_requirement_analysis_runner.py tests/test_requirement_report_html.py tests/test_app_requirement_analysis.py
```

Expected: no compile errors.

- [ ] **Step 3: Run whitespace check**

Run:

```bash
git diff --check
```

Expected: no output.

- [ ] **Step 4: Manual Meegle smoke, no mutation**

Run:

```bash
meegle url decode --url 'https://project.feishu.cn/adcvehicleroject/story/detail/6979403058' --format json
meegle workitem get --project-key adcvehicleroject --work-item-id 6979403058 --format json
```

Expected:
- URL decode returns `url_kind=workitem_detail`, `work_item_type=story`, `work_item_id=6979403058`.
- Workitem get returns title/status/description fields.

Do not require `--fields _all` in verification because it was already observed to fail with a CLI `page_size` type error; if checked, record it as a warning, not a test failure.

---

## Task 9: Manual End-to-End Smoke

**Files:**
- No source edits unless smoke finds a defect.

- [ ] **Step 1: Start or reuse report server/listener**

If the listener is already running, do not restart unnecessarily. Verify:

```bash
curl -fsS http://127.0.0.1:8765/api/health
```

Expected: healthy response.

- [ ] **Step 2: Send or replay a representative event**

Use existing local event test harness or a dry-run handle-event payload equivalent to:

```text
@bot https://project.feishu.cn/adcvehicleroject/story/detail/6979403058 这是需求链接，结合源码分析是否可行
```

Expected:
- activity mode is `requirement_analysis`;
- progress includes `requirement_fetch_started`, `requirement_source_started`, `requirement_analysis_completed`;
- result contains `published_report_url`;
- HTML report includes requirement summary, source evidence, swimlane, and unresolved items.

- [ ] **Step 3: Regression smoke for non-interference**

Run or replay these messages:

```text
@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6993883118 基于源码分析 UnityReady
@bot 基于源码分析 UnityReady 信号链路如何监听
@bot https://example.com/log.zip 分析这份日志
```

Expected:
- first routes to `bug_analysis`;
- second routes to `source_analysis`;
- third routes to `direct_analysis`;
- no route falls into `requirement_analysis`.

---

## Task 10: Commit and Release Handoff

**Files:**
- All modified files from previous tasks.

- [ ] **Step 1: Inspect real diff**

Run:

```bash
git status -sb
git diff --stat
git diff -- lark_agent_bridge/models.py lark_agent_bridge/parser.py lark_agent_bridge/app/_shared.py lark_agent_bridge/app/handle_event.py lark_agent_bridge/requirement_analysis.py lark_agent_bridge/reporting/requirement_report_html.py tests/test_requirement_analysis_runner.py tests/test_requirement_report_html.py tests/test_app_requirement_analysis.py tests/test_parser.py
```

Expected: only intended requirement-analysis changes plus any previously existing unrelated dirty files.

- [ ] **Step 2: Commit only this feature scope**

Stage only files touched by this plan:

```bash
git add \
  lark_agent_bridge/models.py \
  lark_agent_bridge/parser.py \
  lark_agent_bridge/app/_shared.py \
  lark_agent_bridge/app/handle_event.py \
  lark_agent_bridge/requirement_analysis.py \
  lark_agent_bridge/reporting/requirement_report_html.py \
  tests/test_requirement_analysis_runner.py \
  tests/test_requirement_report_html.py \
  tests/test_app_requirement_analysis.py \
  tests/test_parser.py \
  README.md
git commit -m "feat: route requirement links to source analysis"
```

If `README.md` was not changed, omit it from `git add`.

- [ ] **Step 3: Push source branch**

Run:

```bash
git push
```

Expected: branch push succeeds. If release handoff is required after implementation, follow the repo's dated `release/pydantic-xpdev-YYYYMMDD` workflow separately.

---

## Self-Review Checklist

- Spec coverage:
  - Feishu Project requirement links are parsed deterministically.
  - Bug links remain bug links.
  - Generic URLs/files remain direct analysis.
  - Requirement data is fetched through Meegle and normalized.
  - Source comparison runs through existing repository-source machinery.
  - HTML report includes conclusion, cards, requirement summary, source evidence, swimlane/chain, and unresolved items.
  - Unknowns and fetch failures are listed without guessing.

- Placeholder scan:
  - No planned task depends on placeholder text.
  - Each test includes concrete assertions.
  - Each command has an expected result.

- Type consistency:
  - Parser returns `RequirementAnalysisRequest`.
  - Route context stores `requirement_analysis_request`.
  - Runner receives `RequirementAnalysisRequest` and returns `TaskResult`.
  - Report renderer accepts `RequirementWorkItemSnapshot` and source-analysis evidence.
