# Bug Skill Conflict Confirmation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Bug skill conflict confirmation a complete interaction protocol so explicit `3D生命周期` requests never silently run or rerun the wrong `3d-stuck-investigate` skill.

**Architecture:** Treat skill confirmation as a first-class pre-run Bug state, not as ordinary Bug reanalysis. The runner returns `mode=bug_skill_confirmation` with stable numbered options; the app renders it as a card/text decision point, keeps it in threaded reply context, parses user text/card choices, and resumes the original Bug analysis with `plans_override` from the user's selection.

**Tech Stack:** Python 3, existing `BridgeApp`, `BugAnalysisRunner`, Feishu card action model, `pytest`.

---

## 2026-06-04 Draft Evaluation

Current draft status:

- Focused runner/routing checks pass:

```bash
PYTHONPATH=. python -m pytest \
  tests/test_agents_intent_analysis.py::AgentsIntentAnalysisTests::test_classify_3d_lifecycle_routes_to_startup \
  tests/test_agents_intent_analysis.py::AgentsIntentAnalysisTests::test_classify_3d_lifecycle_with_black_screen_returns_startup_and_stuck \
  tests/test_agents_recent.py::AgentsRecentRefactorTests::test_lifecycle_stuck_conflict_requires_skill_confirmation_with_options \
  tests/test_agents_recent.py::AgentsRecentRefactorTests::test_lifecycle_startup_with_stuck_description_requires_skill_confirmation \
  tests/test_agents_recent.py::AgentsRecentRefactorTests::test_skill_confirmation_result_contains_intent_options \
  tests/test_agents_bug_reanalysis.py::AgentsBugReanalysisTests::test_reanalysis_3d_lifecycle_followup_selects_startup_not_previous_stuck \
  tests/test_agents_bug_reanalysis.py::AgentsBugReanalysisTests::test_bug_analysis_respects_plans_override_after_skill_confirmation -q
```

Observed: `7 passed`.

- Focused app interaction checks pass:

```bash
PYTHONPATH=. python -m pytest tests/test_app_bug_request.py -k "bug_skill_confirmation" -q
```

Observed before post-draft closeout: `8 passed, 40 deselected`.

- Static checks pass:

```bash
python -m py_compile \
  lark_agent_bridge/agents/bug/resolve_source.py \
  lark_agent_bridge/agents/bug/run_primary.py \
  lark_agent_bridge/app/result_bug.py \
  lark_agent_bridge/app/context_from.py \
  lark_agent_bridge/app/handle_event.py \
  lark_agent_bridge/app/log_resources.py
git diff --check
```

Observed: exits 0.

Post-draft gaps found during evaluation:

- Invalid confirmation replies return a `TaskResult` message but are not delivered to Feishu; user sees no correction prompt.
- `_bug_skill_confirmation_options(...)` always emits the 3D lifecycle/stuck three-option set, so unrelated low-confidence skill confirmations would show misleading 3D options.

Current implementation closes both gaps in Task 9. Verification after closeout:

```bash
PYTHONPATH=. python -m pytest \
  tests/test_app_bug_request.py::AppBugRequestTests::test_bug_skill_confirmation_invalid_reply_is_sent_back_to_user \
  tests/test_agents_recent.py::AgentsRecentRefactorTests::test_non_lifecycle_low_confidence_confirmation_does_not_show_3d_options -q
```

Observed: `2 passed`.

```bash
PYTHONPATH=. python -m pytest tests/test_app_bug_request.py -k "bug_skill_confirmation" -q
```

Observed after closeout: `9 passed, 40 deselected`.

```bash
PYTHONPATH=. python -m pytest tests/test_app_bug_request.py tests/test_agents_bug_reanalysis.py tests/test_agents_recent.py -q
```

Observed: `83 passed`.

### 2026-06-04 Real Group Verification Closeout

Real Feishu group test target:

- Group: `oc_d977fe30a92c7ac81e3e6b543d99ef5b`
- Trigger: `@朱云龙的飞书 CLI https://project.feishu.cn/xpfailuremgmt/buglo/detail/6995380113 3D生命周期`
- Confirmation reply: reply to the confirmation card with `@朱云龙的飞书 CLI 1`

Findings from the real group test:

- First live run exposed a remaining fallback gap: when the LLM intent call returned HTTP 502, preflight fallback selected `startup` and bypassed the confirmation gate. Fixed in Task 10 by applying lifecycle/stuck conflict downgrade to non-user-selected `plans_override` after bug title/description are fetched.
- Second live run confirmed the fallback path now returns `Bug Skill 确认`, but the final progress card kept the initial `分类来源=preflight_rules` even after the user selected option `1`. Fixed in Task 10 by letting final result metadata override the progress card's initial classification source.
- Option `1` live run passed end-to-end:
  - trigger message: `om_x100b6d319950f0a0c4f0a3dc7e86ff0`
  - confirmation card: `om_x100b6d31991300a0c3cd45eb284567f`
  - user reply: `om_x100b6d3194766ca0c05d755288281f4`
  - final result card: `om_x100b6d31954684a8c2176c94324367c`
  - final card shows `分类来源=user_selected_reply`
  - final card shows `命中 Skill=3D启动时序分析`
  - final report URL: `http://192.168.71.18:8765/reports/3d07e7cfe244ca5a1c549fcc84425426/v11/report.html`
- Invalid text reply live run passed:
  - trigger message: `om_x100b6d324327aca4c324c6ce94fa415`
  - confirmation card: `om_x100b6d3240f9a8a4c4a0f06c0a25984`
  - invalid reply `9`: `om_x100b6d325e4efca4c29b035d9b7e694`
  - correction reply: `om_x100b6d325f9ecca0c2411444036d746`
  - correction text contains `没有识别到确认选项`
- Option `2` live run passed:
  - trigger message: `om_x100b6d325c2840a0c0862e41b2f4d61`
  - confirmation card: `om_x100b6d325de158a0c2736b1d6e12411`
  - user reply: `om_x100b6d325bb5d530c018610e83c3e96`
  - final result card: `om_x100b6d32588b40acc4aaa06af76dbec`
  - final card shows `分类来源=user_selected_reply`
  - final card shows `命中 Skill=3D卡顿分析`
  - final report URL: `http://192.168.71.18:8765/reports/70fcd5586ba0d9c0c1aa4c6202f072ee/v12/report.html`
- Option `3` live run initially exposed a display-only gap: the combined `startup+stuck` run produced both startup and stuck summaries and uploaded `bug_startup_stuck_report.html`, but the final progress card still displayed the first plan label `3D启动时序分析`.
  Fixed by giving `startup+stuck` a dedicated label and allowing final result metadata to override the progress card's initial `命中 Skill`.
- Option `3` after-fix live run passed:
  - trigger message: `om_x100b6d32647df4a0c36a9dec72a611c`
  - confirmation card: `om_x100b6d326434e4a8c14c2919cfdb826`
  - user reply: `om_x100b6d3260d6a8a4c09d9b52c3c348e`
  - final result card: `om_x100b6d326025f0a0c18ea4a9b4c19a8`
  - final card shows `分类来源=user_selected_reply`
  - final card shows `命中 Skill=3D启动卡顿综合分析`
  - final card includes both `类型: 3D启动时序分析` and `类型: 3D卡顿分析`
  - final report URL: `http://192.168.71.18:8765/reports/7d8de257ba0723261ab7433f6755dff7/v14/report.html`

Verification after Task 10:

```bash
PYTHONPATH=. python -m pytest tests/test_app_bug_request.py -k "bug_skill_confirmation" -q
```

Observed: `9 passed, 40 deselected`.

```bash
PYTHONPATH=. python -m pytest \
  tests/test_agents_bug_reanalysis.py::AgentsBugReanalysisTests::test_bug_analysis_respects_plans_override_after_skill_confirmation \
  tests/test_agents_bug_reanalysis.py::AgentsBugReanalysisTests::test_preflight_lifecycle_override_with_stuck_bug_context_requires_confirmation \
  tests/test_app_bug_followup.py::AppBugFollowupTests::test_finished_progress_card_overrides_initial_classification_source \
  tests/test_app_bug_followup.py::AppBugFollowupTests::test_finished_progress_card_offers_skill_correction_choices -q
```

Observed: `4 passed`.

```bash
PYTHONPATH=. python -m pytest tests/test_app_bug_request.py tests/test_agents_bug_reanalysis.py tests/test_agents_recent.py -q
```

Observed: `84 passed`.

After the option `3` display fix:

```bash
PYTHONPATH=. python -m pytest tests/test_app_bug_request.py -k "bug_skill_confirmation" -q
```

Observed: `9 passed, 40 deselected`.

```bash
PYTHONPATH=. python -m pytest \
  tests/test_app_bug_request.py \
  tests/test_agents_bug_reanalysis.py \
  tests/test_agents_recent.py \
  tests/test_app_bug_followup.py::AppBugFollowupTests::test_finished_progress_card_overrides_initial_classification_source -q
```

Observed: `86 passed`.

```bash
python -m py_compile \
  lark_agent_bridge/agents/bug/run_primary.py \
  lark_agent_bridge/app/result_bug.py
git diff --check
```

Observed: exits 0.

## File Structure

- Modify `config/routing_terms.toml`: add coupled lifecycle route terms such as `3d生命周期`; do not add broad `生命周期`.
- Modify `lark_agent_bridge/agents/bug/resolve_source.py`: detect explicit 3D lifecycle intent, downgrade lifecycle-vs-stuck conflicts to confirmation, generate `intent_options`, and make reanalysis followups select lifecycle before force-rerun fallback.
- Modify `lark_agent_bridge/agents/bug/run_primary.py`: allow confirmed Bug runs to pass `plans_override` and classification metadata into `run_bug_analysis`.
- Modify `lark_agent_bridge/app/result_bug.py`: add Bug skill confirmation option matching and shared execution helper; extend `_run_bug_request`; make card skill actions resume initial Bug analysis when the current context is `bug_skill_confirmation`.
- Modify `lark_agent_bridge/app/context_from.py`: resolve `bug_skill_confirmation` text replies before ordinary Bug followup/reanalysis.
- Modify `lark_agent_bridge/app/handle_event.py`: render `bug_skill_confirmation` as a card-only skill clarification result.
- Modify `lark_agent_bridge/app/log_resources.py`: include `bug_skill_confirmation` in threaded reply context modes.
- Modify tests in `tests/_app_base.py`, `tests/test_agents_intent_analysis.py`, `tests/test_agents_recent.py`, `tests/test_agents_bug_reanalysis.py`, `tests/test_app_bug_followup.py`, and `tests/test_app_bug_request.py`.

---

### Task 1: Add Explicit 3D Lifecycle Routing Terms

**Files:**
- Modify: `config/routing_terms.toml`
- Test: `tests/test_agents_intent_analysis.py`

- [ ] **Step 1: Write failing routing tests**

Add these tests to `tests/test_agents_intent_analysis.py`:

```python
def test_classify_3d_lifecycle_routes_to_startup():
    with tempfile.TemporaryDirectory() as tmp:
        config = BridgeConfig(dry_run=True, data_dir=Path(tmp), workspace_root=Path(tmp))
        runner = BugAnalysisRunner(config)

        plans = runner.classify_requests(prompt_text="3D生命周期", title="", description="")

    assert [plan.kind for plan in plans] == ["startup"]


def test_classify_3d_lifecycle_with_black_screen_returns_startup_and_stuck():
    with tempfile.TemporaryDirectory() as tmp:
        config = BridgeConfig(dry_run=True, data_dir=Path(tmp), workspace_root=Path(tmp))
        runner = BugAnalysisRunner(config)

        plans = runner.classify_requests(
            prompt_text="3D生命周期",
            title="sr底图黑屏，不显示内容",
            description="问题时间：05-19 14:33",
        )

    assert [plan.kind for plan in plans] == ["startup", "stuck"]
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
PYTHONPATH=. python -m pytest \
  tests/test_agents_intent_analysis.py::AgentsIntentAnalysisTests::test_classify_3d_lifecycle_routes_to_startup \
  tests/test_agents_intent_analysis.py::AgentsIntentAnalysisTests::test_classify_3d_lifecycle_with_black_screen_returns_startup_and_stuck -q
```

Expected: first test fails because current routing returns `general` for bare `3D生命周期`.

- [ ] **Step 3: Add coupled lifecycle terms only**

Append these entries to `[startup].terms` in `config/routing_terms.toml`:

```toml
    "3d生命周期", "3d 生命周期",
    "unity生命周期", "unity 生命周期",
    "surface生命周期", "surface 生命周期",
    "sr生命周期", "sr 生命周期",
```

Do not add plain `生命周期`; it collides with signal lifecycle and source lifecycle requests.

- [ ] **Step 4: Run tests and verify pass**

Run the command from Step 2.

Expected: both tests pass.

---

### Task 2: Build Confirmation Options in the Bug Runner

**Files:**
- Modify: `lark_agent_bridge/agents/bug/resolve_source.py`
- Test: `tests/test_agents_recent.py`

- [ ] **Step 1: Write failing confirmation option tests**

Add these tests to `tests/test_agents_recent.py`:

```python
def test_lifecycle_stuck_conflict_requires_skill_confirmation_with_options():
    with tempfile.TemporaryDirectory() as tmp:
        config = BridgeConfig(data_dir=Path(tmp), workspace_root=Path(tmp))
        runner = BugAnalysisRunner(config)
        selection = runner._selection_from_plans(
            [BugAnalysisPlan(kind="stuck")],
            source="agent",
            reason="SR底图黑屏/不显示内容属于3D渲染黑屏问题",
            provider="claude",
        )
        selection.confidence = "high"

        normalized = runner._downgrade_lifecycle_stuck_conflict(
            selection,
            prompt_text="3D生命周期",
            title="sr底图黑屏，不显示内容",
            description="问题时间：05-19 14:33",
        )
        options = runner._bug_skill_confirmation_options(normalized)

    assert normalized.confidence == "low"
    assert "生命周期" in normalized.reason
    assert runner._needs_skill_confirmation(normalized)
    assert options[0]["skill_name"] == "unity-startup-lifecycle-check"
    assert options[1]["skill_name"] == "3d-stuck-investigate"
    assert options[2]["type"] == "plans"
    assert options[2]["plan_kinds"] == ["startup", "stuck"]


def test_skill_confirmation_result_contains_intent_options():
    with tempfile.TemporaryDirectory() as tmp:
        config = BridgeConfig(data_dir=Path(tmp), workspace_root=Path(tmp))
        runner = BugAnalysisRunner(config)
        context = create_job_context(Path(tmp))
        selection = runner.selection_for_skill_name(
            "3d-stuck-investigate",
            source="agent",
            reason="黑屏描述命中卡顿 skill，但用户要求 3D 生命周期。",
        )
        assert selection is not None
        selection.confidence = "low"

        result = runner._skill_confirmation_needed_result(
            context=context,
            selection=selection,
            started=time.monotonic(),
            request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6995380113 3D生命周期",
            bug_url="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6995380113",
            title="sr底图黑屏，不显示内容",
            progress_callback=None,
        )

    assert result.details["mode"] == "bug_skill_confirmation"
    assert result.details["needs_user_direction"] is True
    assert result.details["intent_options"]
    assert "1." in result.message
    assert "3D启动" in result.message
```

If imports are missing, add them near the top of `tests/test_agents_recent.py`:

```python
import time
from lark_agent_bridge.models import create_job_context
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
PYTHONPATH=. python -m pytest \
  tests/test_agents_recent.py::AgentsRecentRefactorTests::test_lifecycle_stuck_conflict_requires_skill_confirmation_with_options \
  tests/test_agents_recent.py::AgentsRecentRefactorTests::test_skill_confirmation_result_contains_intent_options -q
```

Expected: fails because `_downgrade_lifecycle_stuck_conflict` and `_bug_skill_confirmation_options` do not exist, and the confirmation result has no `intent_options`.

- [ ] **Step 3: Implement lifecycle conflict helpers**

In `lark_agent_bridge/agents/bug/resolve_source.py`, add these methods near `_normalize_agent_bug_selection`:

```python
    def _has_explicit_3d_lifecycle_intent(self, text: str) -> bool:
        lowered = (text or "").casefold().replace(" ", "")
        return any(
            term in lowered
            for term in (
                "3d生命周期",
                "unity生命周期",
                "surface生命周期",
                "sr生命周期",
            )
        )

    def _downgrade_lifecycle_stuck_conflict(
        self,
        selection: "BugAnalysisSelection",
        *,
        prompt_text: str,
        title: str,
        description: str,
    ) -> "BugAnalysisSelection":
        if not self._has_explicit_3d_lifecycle_intent(prompt_text):
            return selection
        if not any(plan.kind == "stuck" for plan in selection.plans):
            return selection
        if not any(term in f"{title}\n{description}".casefold() for term in ("黑屏", "不显示", "卡顿", "卡住")):
            return selection
        selection.confidence = "low"
        reason = selection.reason.strip()
        suffix = "用户明确要求 3D 生命周期，但标题/描述也包含黑屏/不显示/卡顿，存在 lifecycle 与 stuck skill 冲突，需要用户确认。"
        selection.reason = f"{reason}；{suffix}" if reason else suffix
        return selection
```

- [ ] **Step 4: Generate stable confirmation options**

Still in `resolve_source.py`, add:

```python
    def _bug_skill_confirmation_options(self, selection: "BugAnalysisSelection") -> list[dict[str, object]]:
        options: list[dict[str, object]] = []

        def add_skill(index: int, skill_name: str, label: str, aliases: list[str]) -> None:
            options.append(
                {
                    "index": index,
                    "type": "skill",
                    "skill_name": skill_name,
                    "label": label,
                    "aliases": aliases,
                }
            )

        add_skill(
            1,
            "unity-startup-lifecycle-check",
            "3D启动/Surface生命周期分析",
            ["3D启动时序分析", "生命周期", "3D生命周期", "启动时序", "Surface生命周期"],
        )
        add_skill(
            2,
            "3d-stuck-investigate",
            "3D卡顿/黑屏渲染分析",
            ["3D卡顿分析", "卡顿", "黑屏", "渲染黑屏"],
        )
        options.append(
            {
                "index": 3,
                "type": "plans",
                "label": "两个方向都跑",
                "plan_kinds": ["startup", "stuck"],
                "skill_name": "startup+stuck",
                "aliases": ["都跑", "两个都跑", "全部", "两个方向都跑", "启动和卡顿"],
            }
        )
        return options
```

Keep this helper intentionally narrow for this incident. Do not build a broad configurable wizard in this fix.

- [ ] **Step 5: Add options to the confirmation result**

In `_skill_confirmation_needed_result(...)`, replace the current free-form message construction with numbered options from `_bug_skill_confirmation_options(selection)`. The result details must include:

```python
"intent_options": options,
```

The message must include:

```text
请直接回复下面任一选项的序号，或直接回复对应文本：
1. 3D启动/Surface生命周期分析
2. 3D卡顿/黑屏渲染分析
3. 两个方向都跑
```

- [ ] **Step 6: Wire conflict downgrade into classification**

In `_unified_classify_and_decide(...)`, immediately after `_normalize_agent_bug_selection(...)` returns a non-`None` selection, call `_downgrade_lifecycle_stuck_conflict(...)` with `prompt_text`, `title`, and `description`.

- [ ] **Step 7: Run tests and verify pass**

Run the command from Step 2.

Expected: both tests pass.

---

### Task 3: Make Reanalysis Followups Respect Explicit Lifecycle Direction

**Files:**
- Modify: `lark_agent_bridge/agents/bug/resolve_source.py`
- Test: `tests/test_agents_bug_reanalysis.py`

- [ ] **Step 1: Write failing followup decision test**

Add this test to `tests/test_agents_bug_reanalysis.py`:

```python
def test_reanalysis_3d_lifecycle_followup_selects_startup_not_previous_stuck():
    with tempfile.TemporaryDirectory() as tmp:
        config = BridgeConfig(data_dir=Path(tmp), workspace_root=Path(tmp))
        runner = BugAnalysisRunner(config)
        previous_context = SimpleNamespace(
            request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6995380113 3D生命周期",
            summary_text="上一轮误跑 stuck",
            report_excerpt="",
        )
        previous_session = {
            "details": {
                "analysis_kinds": ["stuck"],
                "analysis_skill": "3d-stuck-investigate",
                "prepared_log_input": "/tmp/logs",
                "selected_log_input": "/tmp/logs",
            }
        }

        decision = runner.decide_bug_followup(
            followup_text="重新分析 3D生命周期问题",
            previous_context=previous_context,
            previous_session=previous_session,
        )

    assert decision is not None
    assert decision.should_reanalyze is True
    assert decision.force_rerun is True
    assert [plan.kind for plan in decision.plans] == ["startup"]
    assert decision.skill_name == "unity-startup-lifecycle-check"
```

If needed, add:

```python
from types import SimpleNamespace
```

- [ ] **Step 2: Run test and verify failure**

Run:

```bash
PYTHONPATH=. python -m pytest \
  tests/test_agents_bug_reanalysis.py::AgentsBugReanalysisTests::test_reanalysis_3d_lifecycle_followup_selects_startup_not_previous_stuck -q
```

Expected: fails because the current force-reanalysis branch returns no selected plan and later reuses old `stuck`.

- [ ] **Step 3: Select startup before explicit-route gating**

In `_manual_followup_selection_if_explicit(...)`, before:

```python
if not self._followup_has_explicit_bug_route(followup_text):
```

add:

```python
        if self._has_explicit_3d_lifecycle_intent(followup_text):
            return self.selection_for_skill_name(
                "unity-startup-lifecycle-check",
                source="deterministic_fallback",
                reason="用户在续聊中明确要求重新分析 3D 生命周期。",
            )
```

- [ ] **Step 4: Run test and verify pass**

Run the command from Step 2.

Expected: pass.

---

### Task 4: Let Confirmed Initial Bug Runs Use `plans_override`

**Files:**
- Modify: `lark_agent_bridge/agents/bug/run_primary.py`
- Modify: `lark_agent_bridge/app/result_bug.py`
- Test: `tests/test_agents_bug_reanalysis.py`

- [ ] **Step 1: Write failing runner override test**

Add this test to `tests/test_agents_bug_reanalysis.py`:

```python
def test_bug_analysis_respects_plans_override_after_skill_confirmation():
    with tempfile.TemporaryDirectory() as tmp:
        config = BridgeConfig(dry_run=True, data_dir=Path(tmp), workspace_root=Path(tmp))
        runner = BugAnalysisRunner(config)

        with mock.patch.object(
            runner,
            "classify_requests",
            side_effect=AssertionError("classify_requests should not run when plans_override is provided"),
        ):
            result = runner.run_bug_analysis(
                BugRequest(
                    bug_url="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6995380113",
                    prompt="3D生命周期",
                    raw_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6995380113 3D生命周期",
                    triggered=True,
                ),
                plans_override=[BugAnalysisPlan(kind="startup")],
                classification_skill="unity-startup-lifecycle-check",
                classification_source="user_selected_reply",
                classification_reason="用户通过确认回复选择 3D 生命周期分析。",
            )

    assert result.success
    assert result.details["analysis_kind"] == "startup"
    assert result.details["analysis_skill"] == "unity-startup-lifecycle-check"
    assert result.details["classification_source"] == "user_selected_reply"
```

Add imports if missing:

```python
from lark_agent_bridge.models import BugRequest
```

- [ ] **Step 2: Run test and verify failure**

Run:

```bash
PYTHONPATH=. python -m pytest \
  tests/test_agents_bug_reanalysis.py::AgentsBugReanalysisTests::test_bug_analysis_respects_plans_override_after_skill_confirmation -q
```

Expected: fails because `run_bug_analysis()` does not accept `plans_override` and `_run_bug_request()` cannot pass it.

- [ ] **Step 3: Extend `run_bug_analysis` signature**

In `lark_agent_bridge/agents/bug/run_primary.py`, change the method signature to:

```python
    def run_bug_analysis(
        self,
        request: BugRequest,
        *,
        event: LarkEvent | None = None,
        progress_callback: Callable[[dict[str, object]], None] | None = None,
        plans_override: list["BugAnalysisPlan"] | None = None,
        classification_skill: str = "",
        classification_source: str = "",
        classification_reason: str = "",
        classification_provider: str = "",
    ) -> TaskResult:
```

Use:

```python
plans = plans_override or self.classify_requests(prompt_text=prompt_text, title="", description="")
```

When `plans_override` is provided, skip `_unified_classify_and_decide(...)` and build the selection from `plans_override`:

```python
selection = self._selection_from_plans(
    plans,
    source=classification_source or "user_selected_reply",
    reason=classification_reason or "用户确认后按指定 Bug skill 执行。",
    provider=classification_provider,
)
if classification_skill:
    selection.skill_name = classification_skill
    selection.skill_label = self._skill_label_for_name(classification_skill, plans[0].kind if plans else "general")
source_decision = self._decide_source_analysis_request(
    request_text=request_text,
    prompt_text=prompt_text,
    title=title,
    description=description,
    plans=plans,
    skill_name=selection.skill_name,
)
```

Do not call `_needs_general_direction()` or `_needs_skill_confirmation()` when `plans_override` is provided; the user already selected a direction. Keep the existing time clarification check.

- [ ] **Step 4: Include classification fields in dry-run details**

In the dry-run `TaskResult.details`, add:

```python
"analysis_skill": selection.skill_name,
"analysis_skill_label": selection.skill_label,
"classification_source": selection.source,
"classification_reason": selection.reason,
"classification_provider": selection.provider,
```

- [ ] **Step 5: Extend `_run_bug_request`**

In `lark_agent_bridge/app/result_bug.py`, change `_run_bug_request(...)` to accept:

```python
        *,
        plans_override: list[BugAnalysisPlan] | None = None,
        classification_skill: str = "",
        classification_source: str = "",
        classification_reason: str = "",
        classification_provider: str = "",
```

Pass those fields to `self.bug_runner.run_bug_analysis(...)`.

- [ ] **Step 6: Run test and verify pass**

Run the command from Step 2.

Expected: pass.

---

### Task 5: Render `bug_skill_confirmation` as a Card Decision Point

**Files:**
- Modify: `lark_agent_bridge/app/handle_event.py`
- Modify: `lark_agent_bridge/app/log_resources.py`
- Modify: `lark_agent_bridge/app/result_bug.py`
- Test: `tests/test_app_bug_request.py`

- [ ] **Step 1: Write failing card and threaded-context tests**

Add these tests to `tests/test_app_bug_request.py`:

```python
def test_bug_skill_confirmation_result_sends_skill_choice_card(self):
    with tempfile.TemporaryDirectory() as tmp:
        fake_lark = FakeLarkClient()
        app = BridgeApp(
            BridgeConfig(
                dry_run=False,
                data_dir=Path(tmp),
                allowed_chats=["oc_denied"],
                event_consumer=EventConsumerOptions(event_key="card.action.trigger"),
            ),
            lark_client=fake_lark,
        )
        original = event(
            event_id="evt_bug_confirm_root",
            message_id="om_bug_confirm_root",
            content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6995380113 3D生命周期",
            chat_type="group",
        )
        result = TaskResult(
            success=True,
            skipped=True,
            message="请确认分析方向\n1. 3D启动/Surface生命周期分析\n2. 3D卡顿/黑屏渲染分析",
            job_id="job_confirm",
            details={
                "mode": "bug_skill_confirmation",
                "needs_user_direction": True,
                "bug_url": "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6995380113",
                "user_request_text": "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6995380113 3D生命周期",
                "analysis_skill": "3d-stuck-investigate",
                "analysis_skill_label": "3D卡顿分析",
                "classification_source": "agent",
                "supported_bug_skills": app.bug_runner.supported_primary_bug_skills(),
                "intent_options": [
                    {"index": 1, "type": "skill", "skill_name": "unity-startup-lifecycle-check", "label": "3D启动/Surface生命周期分析"},
                    {"index": 2, "type": "skill", "skill_name": "3d-stuck-investigate", "label": "3D卡顿/黑屏渲染分析"},
                ],
            },
        )

        finalized = app._deliver_result(original, result, request_text=result.details["user_request_text"])

    assert finalized.success
    assert len(fake_lark.card_replies) == 1
    assert "Bug Skill 确认" in fake_lark.card_replies[0]["card_json"]
    assert "select_bug_skill" in fake_lark.card_replies[0]["card_json"]


def test_bug_skill_confirmation_is_threaded_reply_context_mode():
    app = BridgeApp(BridgeConfig())

    assert "bug_skill_confirmation" in app._threaded_reply_context_modes()
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
PYTHONPATH=. python -m pytest \
  tests/test_app_bug_request.py::AppBugRequestTests::test_bug_skill_confirmation_result_sends_skill_choice_card \
  tests/test_app_bug_request.py::AppBugRequestTests::test_bug_skill_confirmation_is_threaded_reply_context_mode -q
```

Expected: first test fails because `_try_send_result_card()` only treats `bug_clarification` as a card-only decision point; second fails because `bug_skill_confirmation` is not in `_threaded_reply_context_modes()`.

- [ ] **Step 3: Update result-card mode handling**

In `lark_agent_bridge/app/handle_event.py`, change:

```python
is_skill_clarification = mode == "bug_clarification" and bool(result.details.get("needs_user_direction"))
```

to:

```python
is_skill_clarification = mode in {"bug_clarification", "bug_skill_confirmation"} and bool(result.details.get("needs_user_direction"))
```

Add to `mode_labels`:

```python
"bug_skill_confirmation": "Bug Skill 确认",
```

- [ ] **Step 4: Update progress card label**

In `lark_agent_bridge/app/result_bug.py`, add to `_progress_mode_label(...)`:

```python
"bug_skill_confirmation": "Bug Skill 确认",
```

- [ ] **Step 5: Add threaded reply context mode**

In `lark_agent_bridge/app/log_resources.py`, add `"bug_skill_confirmation"` to `_threaded_reply_context_modes()`.

- [ ] **Step 6: Improve confirmation card note**

In `_result_bug_skill_choice_note(...)`, branch on `mode == "bug_skill_confirmation"` and return:

```python
"当前 Skill 分类存在冲突。可以直接回复序号/方向，也可以点击下面的 Skill 按钮继续；若要两个方向都跑，请文本回复“3”或“都跑”。"
```

- [ ] **Step 7: Run tests and verify pass**

Run the command from Step 2.

Expected: both tests pass. Because `_try_send_result_card()` already calls `_remember_delivery_alias_from_result(...)` on success, this also verifies the card path uses the existing alias mechanism.

---

### Task 6: Parse Bug Skill Confirmation Text Replies

**Files:**
- Modify: `lark_agent_bridge/app/result_bug.py`
- Modify: `lark_agent_bridge/app/context_from.py`
- Modify: `tests/_app_base.py`
- Test: `tests/test_app_bug_request.py`

- [ ] **Step 1: Extend `FakeBugRunner` for confirmation tests**

In `tests/_app_base.py`, update `FakeBugRunner.selection_for_skill_name(...)` mapping to include:

```python
"unity-startup-lifecycle-check": ("startup", "3D启动时序分析"),
```

Update `FakeBugRunner.supported_primary_bug_skills()` to include:

```python
{
    "name": "unity-startup-lifecycle-check",
    "kind": "startup",
    "label": "3D启动时序分析",
    "requires_logs": True,
    "role": "primary",
    "description": "分析 3D 启动、Surface、UnityReady、首帧生命周期。",
},
```

Update `FakeBugRunner._skill_name_for_kind(...)` so `"startup"` maps to `"unity-startup-lifecycle-check"` instead of `"3d-stuck-investigate"`.

In `FakeBugRunner.__init__`, add:

```python
self.analysis_calls = []
```

In `FakeBugRunner.run_bug_analysis(...)`, accept and record the new keyword arguments:

```python
def run_bug_analysis(
    self,
    request,
    *,
    event=None,
    progress_callback=None,
    plans_override=None,
    classification_skill="",
    classification_source="",
    classification_reason="",
    classification_provider="",
):
    self.analysis_calls.append(
        {
            "request": request,
            "plans_override": plans_override or [],
            "classification_skill": classification_skill,
            "classification_source": classification_source,
            "classification_reason": classification_reason,
            "classification_provider": classification_provider,
        }
    )
    ...
```

Keep the existing behavior that appends `request` to `self.requests`.

- [ ] **Step 2: Write failing option matcher tests**

Add to `tests/test_app_bug_request.py`:

```python
def test_bug_skill_confirmation_option_matcher_accepts_index_label_alias_and_plan_group(self):
    app = BridgeApp(BridgeConfig())
    previous_session = {
        "details": {
            "intent_options": [
                {
                    "index": 1,
                    "type": "skill",
                    "skill_name": "unity-startup-lifecycle-check",
                    "label": "3D启动/Surface生命周期分析",
                    "aliases": ["生命周期", "3D生命周期"],
                },
                {
                    "index": 2,
                    "type": "skill",
                    "skill_name": "3d-stuck-investigate",
                    "label": "3D卡顿/黑屏渲染分析",
                    "aliases": ["卡顿", "黑屏"],
                },
                {
                    "index": 3,
                    "type": "plans",
                    "skill_name": "startup+stuck",
                    "label": "两个方向都跑",
                    "plan_kinds": ["startup", "stuck"],
                    "aliases": ["都跑", "两个都跑"],
                },
            ]
        }
    }

    assert app._match_bug_skill_confirmation_option("1", previous_session)["skill_name"] == "unity-startup-lifecycle-check"
    assert app._match_bug_skill_confirmation_option("3D启动/Surface生命周期分析", previous_session)["skill_name"] == "unity-startup-lifecycle-check"
    assert app._match_bug_skill_confirmation_option("生命周期", previous_session)["skill_name"] == "unity-startup-lifecycle-check"
    assert app._match_bug_skill_confirmation_option("都跑", previous_session)["plan_kinds"] == ["startup", "stuck"]
```

- [ ] **Step 3: Write failing text reply recovery test**

Add:

```python
def test_bug_skill_confirmation_reply_recovers_original_bug_and_selected_skill(self):
    with tempfile.TemporaryDirectory() as tmp:
        metadata = Path(tmp) / "bug_metadata.md"
        html = Path(tmp) / "bug_report.html"
        metadata.write_text("bug", encoding="utf-8")
        html.write_text("<html></html>", encoding="utf-8")
        fake_lark = FakeLarkClient()
        fake_bug = FakeBugRunner(metadata, html)
        fake_lark.fetched_messages["om_followup_choice"] = json.dumps(
            {"data": {"messages": [{"message_id": "om_followup_choice", "reply_to": "om_confirm_root"}]}},
            ensure_ascii=False,
        )
        app = BridgeApp(
            BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"]),
            lark_client=fake_lark,
            bug_runner=fake_bug,
        )
        root_event = event(
            event_id="evt_confirm_root",
            message_id="om_confirm_root",
            content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6995380113 3D生命周期",
            chat_type="group",
        )
        app.activity_store.record_event(root_event)
        app.activity_store.record_result(
            root_event,
            TaskResult(
                success=True,
                skipped=True,
                message="请确认分析方向",
                details={
                    "mode": "bug_skill_confirmation",
                    "conversation_root_message_id": "om_confirm_root",
                    "bug_url": "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6995380113",
                    "user_request_text": "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6995380113 3D生命周期",
                    "needs_user_direction": True,
                    "intent_options": [
                        {"index": 1, "type": "skill", "skill_name": "unity-startup-lifecycle-check", "label": "3D启动/Surface生命周期分析", "aliases": ["生命周期"]},
                        {"index": 2, "type": "skill", "skill_name": "3d-stuck-investigate", "label": "3D卡顿/黑屏渲染分析", "aliases": ["卡顿"]},
                    ],
                },
            ),
        )

        result = app.handle_event(
            event(
                event_id="evt_followup_choice",
                message_id="om_followup_choice",
                content="@bot 1",
                chat_type="group",
            )
        )

    assert result.success
    assert result.details["mode"] == "bug_analysis"
    assert len(fake_bug.analysis_calls) == 1
    call = fake_bug.analysis_calls[0]
    assert call["request"].bug_url == "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6995380113"
    assert call["request"].prompt == "3D生命周期"
    assert [plan.kind for plan in call["plans_override"]] == ["startup"]
    assert call["classification_skill"] == "unity-startup-lifecycle-check"
    assert call["classification_source"] == "user_selected_reply"
```

- [ ] **Step 4: Run tests and verify failure**

Run:

```bash
PYTHONPATH=. python -m pytest \
  tests/test_app_bug_request.py::AppBugRequestTests::test_bug_skill_confirmation_option_matcher_accepts_index_label_alias_and_plan_group \
  tests/test_app_bug_request.py::AppBugRequestTests::test_bug_skill_confirmation_reply_recovers_original_bug_and_selected_skill -q
```

Expected: fails because there is no Bug confirmation matcher or recovery path.

- [ ] **Step 5: Add option matcher**

In `lark_agent_bridge/app/result_bug.py`, add:

```python
    def _match_bug_skill_confirmation_option(
        self,
        followup_text: str,
        previous_session: dict[str, object],
    ) -> dict[str, object] | None:
        details = previous_session.get("details", {})
        if not isinstance(details, dict):
            return None
        raw_options = details.get("intent_options")
        if not isinstance(raw_options, list):
            return None
        normalized = followup_text.strip()
        if not normalized:
            return None
        folded = normalized.casefold()
        for option in raw_options:
            if not isinstance(option, dict):
                continue
            candidates = [
                str(option.get("index") or "").strip(),
                str(option.get("label") or "").strip(),
                str(option.get("skill_name") or "").strip(),
            ]
            aliases = option.get("aliases")
            if isinstance(aliases, list):
                candidates.extend(str(item or "").strip() for item in aliases)
            if any(candidate and folded == candidate.casefold() for candidate in candidates):
                return option
        return None
```

- [ ] **Step 6: Add shared execution helper**

In `result_bug.py`, add:

```python
    def _execute_bug_skill_confirmation_choice(
        self,
        event: LarkEvent,
        followup_context,
        previous_session: dict[str, object],
        selected_option: dict[str, object],
        *,
        source: str,
        reason: str,
    ) -> TaskResult:
        details = previous_session.get("details", {}) if isinstance(previous_session, dict) else {}
        if not isinstance(details, dict):
            details = {}
        request_text = str(
            details.get("user_request_text")
            or getattr(followup_context, "request_text", "")
            or ""
        ).strip()
        bug_url = str(details.get("bug_url") or self._bug_url_from_request_text(request_text)).strip()
        if not bug_url:
            return TaskResult(
                success=False,
                message="找不到原始 Bug 链接，请重新发送 Bug 链接和分析方向。",
                error_code="missing_bug_url_for_skill_confirmation",
                details={"mode": "bug_skill_confirmation"},
            )

        parsed = parse_bug_request(request_text, bug_url_re=self.bug_url_re)
        prompt = parsed.prompt if parsed.triggered else request_text
        option_type = str(selected_option.get("type") or "").strip()
        if option_type == "skill":
            selected = self.bug_runner.selection_for_skill_name(
                str(selected_option.get("skill_name") or "").strip(),
                source=source,
                reason=reason,
            )
            if selected is None:
                return TaskResult(
                    success=False,
                    message="回复中的 Skill 选项无效，请重新选择。",
                    error_code="invalid_bug_skill_selection",
                    details={"mode": "bug_skill_confirmation"},
                )
            plans = selected.plans
            classification_skill = selected.skill_name
            classification_reason = selected.reason
        elif option_type == "plans":
            plan_kinds = [
                str(item or "").strip()
                for item in selected_option.get("plan_kinds", [])
                if str(item or "").strip()
            ]
            plans = [BugAnalysisPlan(kind=kind) for kind in plan_kinds]
            classification_skill = str(selected_option.get("skill_name") or "startup+stuck")
            classification_reason = reason
        else:
            return TaskResult(
                success=False,
                message="回复中的选项类型无效，请重新选择。",
                error_code="invalid_bug_skill_confirmation_option",
                details={"mode": "bug_skill_confirmation"},
            )

        bug_request = BugRequest(
            bug_url=bug_url,
            prompt=prompt,
            raw_text=request_text,
            triggered=True,
        )
        return self._run_bug_request(
            event,
            bug_request,
            bug_request.raw_text,
            plans_override=plans,
            classification_skill=classification_skill,
            classification_source=source,
            classification_reason=classification_reason,
        )
```

Import `BugRequest` in `lark_agent_bridge/app/_shared.py` if it is not already imported.

- [ ] **Step 7: Add text followup branch**

In `lark_agent_bridge/app/context_from.py`, add `_maybe_handle_bug_skill_confirmation_followup(...)` before `_maybe_handle_direct_analysis_followup(...)`:

```python
    def _maybe_handle_bug_skill_confirmation_followup(self, event: LarkEvent, followup_context, route_content: str) -> TaskResult | None:
        if str(getattr(followup_context, "mode", "") or "") != "bug_skill_confirmation":
            return None
        previous_session = self.activity_store.get_session(followup_context.root_message_id) or {}
        selected_option = self._match_bug_skill_confirmation_option(route_content, previous_session)
        if selected_option is None:
            return TaskResult(
                success=True,
                skipped=True,
                message="没有识别到确认选项，请回复序号 1/2/3，或回复对应方向文本。",
                details={"mode": "bug_skill_confirmation", "needs_user_direction": True},
            )
        return self._execute_bug_skill_confirmation_choice(
            event,
            followup_context,
            previous_session,
            selected_option,
            source="user_selected_reply",
            reason="用户通过文本回复确认 bug 分析 skill。",
        )
```

Then call it in `_handle_followup(...)` immediately after `had_previous_session_before_followup` is computed and before `_maybe_handle_direct_analysis_followup(...)`.

- [ ] **Step 8: Run tests and verify pass**

Run the command from Step 4.

Expected: both tests pass.

---

### Task 7: Make Card Skill Buttons Resume Initial Confirmation

**Files:**
- Modify: `lark_agent_bridge/app/result_bug.py`
- Test: `tests/test_app_bug_request.py`

- [ ] **Step 1: Write failing card action test**

Add:

```python
def test_bug_skill_confirmation_card_skill_action_runs_initial_bug_analysis_not_reanalysis(self):
    with tempfile.TemporaryDirectory() as tmp:
        metadata = Path(tmp) / "bug_metadata.md"
        html = Path(tmp) / "bug_report.html"
        metadata.write_text("bug", encoding="utf-8")
        html.write_text("<html></html>", encoding="utf-8")
        fake_lark = FakeLarkClient()
        fake_bug = FakeBugRunner(metadata, html)
        app = BridgeApp(
            BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"]),
            lark_client=fake_lark,
            bug_runner=fake_bug,
        )
        root_event = event(
            event_id="evt_confirm_root",
            message_id="om_confirm_root",
            content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6995380113 3D生命周期",
            chat_type="group",
        )
        app.activity_store.record_event(root_event)
        app.activity_store.record_result(
            root_event,
            TaskResult(
                success=True,
                skipped=True,
                message="请确认分析方向",
                job_id="job_confirm",
                details={
                    "mode": "bug_skill_confirmation",
                    "conversation_root_message_id": "om_confirm_root",
                    "bug_url": "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6995380113",
                    "user_request_text": "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6995380113 3D生命周期",
                    "needs_user_direction": True,
                    "intent_options": [
                        {"index": 1, "type": "skill", "skill_name": "unity-startup-lifecycle-check", "label": "3D启动/Surface生命周期分析"},
                    ],
                },
            ),
        )

        result = app.handle_card_action(
            CardActionEvent(
                event_id="evt_card_select_startup",
                action="select_bug_skill",
                root_message_id="om_confirm_root",
                skill_name="unity-startup-lifecycle-check",
                message_id="om_confirm_card",
                chat_id="oc_denied",
                chat_type="group",
            )
        )

    assert result.success
    assert result.details["mode"] == "bug_analysis"
    assert len(fake_bug.analysis_calls) == 1
    assert len(fake_bug.reanalysis_calls) == 0
    call = fake_bug.analysis_calls[0]
    assert [plan.kind for plan in call["plans_override"]] == ["startup"]
    assert call["classification_source"] == "user_selected_card"
```

Add import if missing:

```python
from lark_agent_bridge.models import CardActionEvent
```

- [ ] **Step 2: Run test and verify failure**

Run:

```bash
PYTHONPATH=. python -m pytest \
  tests/test_app_bug_request.py::AppBugRequestTests::test_bug_skill_confirmation_card_skill_action_runs_initial_bug_analysis_not_reanalysis -q
```

Expected: fails because `_handle_select_bug_skill_action(...)` always calls `_execute_approved_reanalysis(...)`.

- [ ] **Step 3: Special-case confirmation context**

In `_handle_select_bug_skill_action(...)`, after `previous_session` is available and before approval/reanalysis, add:

```python
        if str(getattr(followup_context, "mode", "") or "") == "bug_skill_confirmation":
            event = self._event_from_card_action(
                action_event,
                root_message_id=followup_context.root_message_id,
                fallback_chat_id=chat_id,
                fallback_chat_type=str(previous_session.get("chat_type") or ""),
            )
            if not self.state_store.mark_seen(event):
                return TaskResult(True, f"duplicate event skipped: {event.event_id}", skipped=True)
            selected_option = {
                "type": "skill",
                "skill_name": selected.skill_name,
                "label": selected.skill_label,
            }
            return self._execute_bug_skill_confirmation_choice(
                event,
                followup_context,
                previous_session,
                selected_option,
                source="user_selected_card",
                reason="用户通过卡片按钮确认 bug 分析 skill。",
            )
```

Keep existing reanalysis behavior unchanged for `bug_analysis`, `bug_reanalysis`, and report cards.

- [ ] **Step 4: Run test and verify pass**

Run the command from Step 2.

Expected: pass.

---

### Task 8: Group Chat Simulation Tests

**Files:**
- Modify: `tests/test_app_bug_request.py`

- [ ] **Step 1: Write a full group text-reply simulation**

Add:

```python
def test_group_chat_bug_skill_confirmation_card_then_text_reply_simulation(self):
    class ConfirmThenRunBugRunner(FakeBugRunner):
        def __init__(self, metadata_path, html_path):
            super().__init__(metadata_path, html_path)
            self.first = True

        def run_bug_analysis(self, request, **kwargs):
            if self.first:
                self.first = False
                return TaskResult(
                    success=True,
                    skipped=True,
                    message="请确认分析方向\n1. 3D启动/Surface生命周期分析\n2. 3D卡顿/黑屏渲染分析",
                    job_id="job_confirm",
                    details={
                        "mode": "bug_skill_confirmation",
                        "needs_user_direction": True,
                        "bug_url": request.bug_url,
                        "user_request_text": request.raw_text,
                        "analysis_skill": "3d-stuck-investigate",
                        "analysis_skill_label": "3D卡顿分析",
                        "classification_source": "agent",
                        "supported_bug_skills": self.supported_primary_bug_skills(),
                        "intent_options": [
                            {"index": 1, "type": "skill", "skill_name": "unity-startup-lifecycle-check", "label": "3D启动/Surface生命周期分析", "aliases": ["生命周期"]},
                            {"index": 2, "type": "skill", "skill_name": "3d-stuck-investigate", "label": "3D卡顿/黑屏渲染分析", "aliases": ["卡顿"]},
                        ],
                    },
                )
            return super().run_bug_analysis(request, **kwargs)

    with tempfile.TemporaryDirectory() as tmp:
        metadata = Path(tmp) / "bug_metadata.md"
        html = Path(tmp) / "bug_report.html"
        metadata.write_text("bug", encoding="utf-8")
        html.write_text("<html></html>", encoding="utf-8")
        fake_lark = FakeLarkClient()
        fake_bug = ConfirmThenRunBugRunner(metadata, html)
        app = BridgeApp(
            BridgeConfig(
                dry_run=False,
                data_dir=Path(tmp),
                allowed_chats=["oc_denied"],
                event_consumer=EventConsumerOptions(event_key="card.action.trigger"),
            ),
            lark_client=fake_lark,
            bug_runner=fake_bug,
        )

        first = app.handle_event(
            event(
                event_id="evt_group_bug_confirm_start",
                message_id="om_group_bug_confirm_start",
                chat_type="group",
                chat_id="oc_denied",
                content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6995380113 3D生命周期",
            )
        )
        card_message_id = fake_lark.card_replies[-1]["card_message_id"]
        followup = app.handle_event(
            event(
                event_id="evt_group_bug_confirm_reply",
                message_id="om_group_bug_confirm_reply",
                chat_type="group",
                chat_id="oc_denied",
                reply_to=card_message_id,
                content="@bot 1",
            )
        )

    assert first.details["mode"] == "bug_skill_confirmation"
    assert followup.success
    assert followup.details["mode"] == "bug_analysis"
    assert len(fake_bug.analysis_calls) == 1
    assert [plan.kind for plan in fake_bug.analysis_calls[0]["plans_override"]] == ["startup"]
```

This simulates the real group-chat flow: group mention -> confirmation card -> user replies to the card message -> app follows the card alias back to the original root session.

- [ ] **Step 2: Write a full group card-button simulation**

Add:

```python
def test_group_chat_bug_skill_confirmation_card_button_simulation(self):
    with tempfile.TemporaryDirectory() as tmp:
        metadata = Path(tmp) / "bug_metadata.md"
        html = Path(tmp) / "bug_report.html"
        metadata.write_text("bug", encoding="utf-8")
        html.write_text("<html></html>", encoding="utf-8")
        fake_lark = FakeLarkClient()
        fake_bug = FakeBugRunner(metadata, html)
        app = BridgeApp(
            BridgeConfig(
                dry_run=False,
                data_dir=Path(tmp),
                allowed_chats=["oc_denied"],
                event_consumer=EventConsumerOptions(event_key="card.action.trigger"),
            ),
            lark_client=fake_lark,
            bug_runner=fake_bug,
        )
        root_event = event(
            event_id="evt_group_card_button_root",
            message_id="om_group_card_button_root",
            chat_type="group",
            chat_id="oc_denied",
            content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6995380113 3D生命周期",
        )
        app.activity_store.record_event(root_event)
        app.activity_store.record_result(
            root_event,
            TaskResult(
                success=True,
                skipped=True,
                message="请确认分析方向",
                job_id="job_confirm",
                details={
                    "mode": "bug_skill_confirmation",
                    "conversation_root_message_id": "om_group_card_button_root",
                    "bug_url": "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6995380113",
                    "user_request_text": "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6995380113 3D生命周期",
                    "needs_user_direction": True,
                    "intent_options": [
                        {"index": 1, "type": "skill", "skill_name": "unity-startup-lifecycle-check", "label": "3D启动/Surface生命周期分析"},
                    ],
                },
            ),
        )

        result = app.handle_card_action_payload(
            {
                "event_id": "evt_group_card_button_select",
                "event": {
                    "action": {
                        "value": {
                            "action": "select_bug_skill",
                            "root_message_id": "om_group_card_button_root",
                            "skill_name": "unity-startup-lifecycle-check",
                        }
                    },
                    "context": {
                        "open_message_id": "om_group_card_button_card",
                        "open_chat_id": "oc_denied",
                        "chat_type": "group",
                    },
                },
            }
        )

    assert result.success
    assert result.details["mode"] == "bug_analysis"
    assert len(fake_bug.analysis_calls) == 1
    assert len(fake_bug.reanalysis_calls) == 0
    assert [plan.kind for plan in fake_bug.analysis_calls[0]["plans_override"]] == ["startup"]
```

- [ ] **Step 3: Run group simulation tests and verify failure before implementation**

Run:

```bash
PYTHONPATH=. python -m pytest tests/test_app_bug_request.py -k "bug_skill_confirmation_card_then_text_reply_simulation or bug_skill_confirmation_card_button_simulation" -q
```

Expected before implementation: at least one simulation fails because text replies are not recovered and card buttons route to reanalysis.

- [ ] **Step 4: Run group simulation tests after implementation**

Run the same command.

Expected after implementation: both simulations pass.

---

### Task 9: Post-Draft Interaction Closeout

**Files:**
- Modify: `lark_agent_bridge/app/context_from.py`
- Modify: `lark_agent_bridge/agents/bug/resolve_source.py`
- Test: `tests/test_app_bug_request.py`
- Test: `tests/test_agents_recent.py`

- [x] **Step 1: Write a failing test for invalid confirmation reply delivery**

Add this test to `tests/test_app_bug_request.py`:

```python
def test_bug_skill_confirmation_invalid_reply_is_sent_back_to_user(self):
    with tempfile.TemporaryDirectory() as tmp:
        metadata = Path(tmp) / "bug_metadata.md"
        html = Path(tmp) / "bug_report.html"
        metadata.write_text("bug", encoding="utf-8")
        html.write_text("<html></html>", encoding="utf-8")
        fake_lark = FakeLarkClient()
        fake_bug = FakeBugRunner(metadata, html)
        fake_lark.fetched_messages["om_bad_reply"] = json.dumps(
            {"data": {"messages": [{"message_id": "om_bad_reply", "reply_to": "om_confirm_root"}]}},
            ensure_ascii=False,
        )
        app = BridgeApp(
            BridgeConfig(dry_run=False, data_dir=Path(tmp), allowed_chats=["oc_denied"]),
            lark_client=fake_lark,
            bug_runner=fake_bug,
        )
        root_event = event(
            event_id="evt_confirm_root",
            message_id="om_confirm_root",
            content="@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6995380113 3D生命周期",
            chat_type="group",
        )
        app.activity_store.record_event(root_event)
        app.activity_store.record_result(
            root_event,
            TaskResult(
                success=True,
                skipped=True,
                message="请确认分析方向",
                details={
                    "mode": "bug_skill_confirmation",
                    "conversation_root_message_id": "om_confirm_root",
                    "bug_url": "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6995380113",
                    "user_request_text": "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6995380113 3D生命周期",
                    "needs_user_direction": True,
                    "intent_options": [
                        {
                            "index": 1,
                            "type": "skill",
                            "skill_name": "unity-startup-lifecycle-check",
                            "label": "3D启动/Surface生命周期分析",
                        }
                    ],
                },
            ),
        )

        result = app.handle_event(
            event(
                event_id="evt_bad_reply",
                message_id="om_bad_reply",
                content="@bot 不知道",
                chat_type="group",
            )
        )

    assert result.success
    assert result.skipped
    assert "没有识别到确认选项" in result.message
    assert len(fake_lark.replies) == 1
    assert fake_lark.replies[0]["message_id"] == "om_bad_reply"
    assert "没有识别到确认选项" in fake_lark.replies[0]["text"]
    assert len(fake_bug.analysis_calls) == 0
    assert len(fake_bug.reanalysis_calls) == 0
```

- [x] **Step 2: Run the invalid-reply test and verify failure**

Run:

```bash
PYTHONPATH=. python -m pytest \
  tests/test_app_bug_request.py::AppBugRequestTests::test_bug_skill_confirmation_invalid_reply_is_sent_back_to_user -q
```

Expected before implementation: fails because `fake_lark.replies` is empty even though the result message says `没有识别到确认选项`.

- [x] **Step 3: Deliver non-executing confirmation results**

In `lark_agent_bridge/app/context_from.py`, replace the current `bug_skill_confirmation_result` branch in `_handle_followup(...)`:

```python
        if bug_skill_confirmation_result is not None:
            return bug_skill_confirmation_result
```

with:

```python
        if bug_skill_confirmation_result is not None:
            result_mode = str(bug_skill_confirmation_result.details.get("mode") or "")
            if result_mode == "bug_skill_confirmation" and (
                bug_skill_confirmation_result.skipped or not bug_skill_confirmation_result.success
            ):
                return self._finalize_followup_reply(
                    event,
                    bug_skill_confirmation_result,
                    followup_context,
                    route_content,
                )
            return bug_skill_confirmation_result
```

This only sends correction/error prompts that have not already gone through `_run_bug_request(...)`; successful confirmed executions already deliver inside `_run_bug_request(...)`.

- [x] **Step 4: Run the invalid-reply test and verify pass**

Run the command from Step 2.

Expected: pass; the user gets a reply on the invalid confirmation message.

- [x] **Step 5: Write a failing test for non-lifecycle low-confidence confirmations**

Add this test to `tests/test_agents_recent.py`:

```python
def test_non_lifecycle_low_confidence_confirmation_does_not_show_3d_options(self):
    with tempfile.TemporaryDirectory() as tmp:
        config = BridgeConfig(data_dir=Path(tmp), workspace_root=Path(tmp))
        runner = BugAnalysisRunner(config)
        context = create_job_context(Path(tmp))
        selection = runner.selection_for_skill_name(
            "xtheme-analyzer",
            source="agent",
            reason="Agent 认为该请求可能是主题切换问题，但置信度偏低。",
        )
        assert selection is not None
        selection.confidence = "low"

        result = runner._skill_confirmation_needed_result(
            context=context,
            selection=selection,
            started=time.monotonic(),
            request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/7000000001 主题切换后界面异常",
            bug_url="https://project.feishu.cn/xpfailuremgmt/buglo/detail/7000000001",
            title="主题切换后界面异常",
            progress_callback=None,
        )

    assert result.details["mode"] == "bug_skill_confirmation"
    assert "3D启动/Surface生命周期分析" not in result.message
    assert "两个方向都跑" not in result.message
    assert result.details["intent_options"][0]["skill_name"] == "xtheme-analyzer"
    assert result.details["intent_options"][0]["label"] == "XTheme时光主题分析"
```

- [x] **Step 6: Run the non-lifecycle confirmation test and verify failure**

Run:

```bash
PYTHONPATH=. python -m pytest \
  tests/test_agents_recent.py::AgentsRecentRefactorTests::test_non_lifecycle_low_confidence_confirmation_does_not_show_3d_options -q
```

Expected before implementation: fails because `_skill_confirmation_needed_result(...)` currently always includes `3D启动/Surface生命周期分析` and `两个方向都跑`.

- [x] **Step 7: Scope confirmation options to the actual conflict**

In `lark_agent_bridge/agents/bug/resolve_source.py`, change `_bug_skill_confirmation_options(...)` to accept request text:

```python
    def _bug_skill_confirmation_options(
        self,
        selection: "BugAnalysisSelection",
        *,
        request_text: str = "",
    ) -> list[dict[str, object]]:
```

At the top of the method, keep the current 3D lifecycle option set only when the request explicitly has 3D lifecycle intent:

```python
        if self._has_explicit_3d_lifecycle_intent(request_text):
            options: list[dict[str, object]] = []

            def add_skill(index: int, skill_name: str, label: str, aliases: list[str]) -> None:
                options.append(
                    {
                        "index": index,
                        "type": "skill",
                        "skill_name": skill_name,
                        "label": label,
                        "aliases": aliases,
                    }
                )

            add_skill(
                1,
                "unity-startup-lifecycle-check",
                "3D启动/Surface生命周期分析",
                ["3D启动时序分析", "生命周期", "3D生命周期", "启动时序", "Surface生命周期"],
            )
            add_skill(
                2,
                "3d-stuck-investigate",
                "3D卡顿/黑屏渲染分析",
                ["3D卡顿分析", "卡顿", "黑屏", "渲染黑屏"],
            )
            options.append(
                {
                    "index": 3,
                    "type": "plans",
                    "label": "两个方向都跑",
                    "plan_kinds": ["startup", "stuck"],
                    "skill_name": "startup+stuck",
                    "aliases": ["都跑", "两个都跑", "全部", "两个方向都跑", "启动和卡顿"],
                }
            )
            return options
```

For all other low-confidence confirmations, return only the selected skill as the confirmation option:

```python
        label = selection.skill_label or selection.skill_name
        return [
            {
                "index": 1,
                "type": "skill",
                "skill_name": selection.skill_name,
                "label": label,
                "aliases": [label],
            }
        ]
```

This preserves the current low-confidence guard without showing 3D-specific choices for XTheme, perception, scene signal, or other unrelated skills.

- [x] **Step 8: Pass request text when building confirmation result**

In `_skill_confirmation_needed_result(...)`, change:

```python
options = self._bug_skill_confirmation_options(selection)
```

to:

```python
options = self._bug_skill_confirmation_options(selection, request_text=request_text)
```

- [x] **Step 9: Run the post-draft tests and verify pass**

Run:

```bash
PYTHONPATH=. python -m pytest \
  tests/test_app_bug_request.py::AppBugRequestTests::test_bug_skill_confirmation_invalid_reply_is_sent_back_to_user \
  tests/test_agents_recent.py::AgentsRecentRefactorTests::test_non_lifecycle_low_confidence_confirmation_does_not_show_3d_options -q
```

Expected: both tests pass.

---

### Task 10: Regression Verification

**Files:**
- No production file changes.

- [x] **Step 1: Run focused route tests**

Run:

```bash
PYTHONPATH=. python -m pytest \
  tests/test_agents_intent_analysis.py::AgentsIntentAnalysisTests::test_classify_3d_lifecycle_routes_to_startup \
  tests/test_agents_intent_analysis.py::AgentsIntentAnalysisTests::test_classify_3d_lifecycle_with_black_screen_returns_startup_and_stuck -q
```

Expected: pass.

- [x] **Step 2: Run focused confirmation and reanalysis tests**

Run:

```bash
PYTHONPATH=. python -m pytest \
  tests/test_agents_recent.py::AgentsRecentRefactorTests::test_lifecycle_stuck_conflict_requires_skill_confirmation_with_options \
  tests/test_agents_recent.py::AgentsRecentRefactorTests::test_lifecycle_startup_with_stuck_description_requires_skill_confirmation \
  tests/test_agents_recent.py::AgentsRecentRefactorTests::test_skill_confirmation_result_contains_intent_options \
  tests/test_agents_recent.py::AgentsRecentRefactorTests::test_non_lifecycle_low_confidence_confirmation_does_not_show_3d_options \
  tests/test_agents_bug_reanalysis.py::AgentsBugReanalysisTests::test_reanalysis_3d_lifecycle_followup_selects_startup_not_previous_stuck \
  tests/test_agents_bug_reanalysis.py::AgentsBugReanalysisTests::test_bug_analysis_respects_plans_override_after_skill_confirmation -q
```

Expected: pass.

- [x] **Step 3: Run focused app interaction tests**

Run:

```bash
PYTHONPATH=. python -m pytest tests/test_app_bug_request.py -k "bug_skill_confirmation" -q
```

Expected: all `bug_skill_confirmation` tests pass, including group chat simulations.

- [x] **Step 4: Run nearby smoke tests**

Run:

```bash
PYTHONPATH=. python -m pytest \
  tests/test_app_bug_request.py \
  tests/test_agents_bug_reanalysis.py \
  tests/test_agents_recent.py -q
```

Expected: pass. If this is too slow, run the focused selectors from Steps 1-3 and record the skipped broad run in the final note.

- [x] **Step 5: Run static checks**

Run:

```bash
python -m py_compile \
  lark_agent_bridge/agents/bug/resolve_source.py \
  lark_agent_bridge/agents/bug/run_primary.py \
  lark_agent_bridge/app/result_bug.py \
  lark_agent_bridge/app/context_from.py \
  lark_agent_bridge/app/handle_event.py \
  lark_agent_bridge/app/log_resources.py
git diff --check
```

Expected: `py_compile` exits 0 and `git diff --check` prints no whitespace errors.

---

## Self-Review

- Requirement 1 covered: initial explicit `3D生命周期` no longer silently runs `3d-stuck-investigate` when lifecycle and black-screen/stuck signals conflict; it asks for confirmation with concrete options.
- Requirement 2 covered: `重新分析 3D生命周期问题` selects `unity-startup-lifecycle-check` before the old force-rerun fallback can reuse `stuck`.
- Card sending covered: `bug_skill_confirmation` becomes a card-only decision point when card actions are enabled.
- Reply-chain whitelist covered: `bug_skill_confirmation` is a threaded reply context mode.
- Text reply parsing covered: index, label, skill name, alias, and `都跑` plan group are matched.
- Invalid text reply covered: unrecognized confirmation replies are sent back to the user instead of silently returning an internal skipped result.
- Combined option covered: `3`/`都跑` runs both startup and stuck reports and the final card labels it as `3D启动卡顿综合分析`.
- Non-lifecycle low-confidence confirmation covered: unrelated low-confidence skills do not display 3D lifecycle/stuck options.
- Card action covered: skill buttons on a confirmation card resume initial Bug analysis, not reanalysis.
- Group chat simulation covered: tests simulate group mention, confirmation card, reply-to-card text, and card button selection.
- Scope remains narrow: no broad `生命周期` term, no generic wizard framework, no production hardcoded host/config values.
