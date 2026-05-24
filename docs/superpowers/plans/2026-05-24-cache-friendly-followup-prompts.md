# Cache-Friendly Followup Prompts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve answer quality while maximizing prompt-cache reuse for repeated bug followups and repeated source-investigation questions, without bloating single-shot requests.

**Architecture:** Add a shared prompt snapshot layer that stores only stable facts, evidence references, and open questions for multi-turn scopes. Bug followup/reanalysis and source-investigation will both render prompts as `stable prefix + small incremental tail`, while single-shot requests will continue to build prompts directly without writing a long-lived snapshot.

**Tech Stack:** Python 3, `unittest` / `pytest`, existing `BugAnalysisRunner`, `KnowledgeService`, `SourceInvestigationRunner`, OpenAI-compatible chat completions

---

## File Map

- Create: `lark_agent_bridge/prompt_snapshots.py`
  - Shared snapshot dataclasses, JSON read/write helpers, invalidation rules, and prompt-prefix rendering.
- Create: `tests/test_prompt_snapshots.py`
  - Narrow unit tests for snapshot persistence, conflict handling, and stable-prefix rendering.
- Modify: `lark_agent_bridge/agents/bug_runner.py`
  - Build/update bug followup snapshots, stop embedding prior long-form summaries as stable context, and feed snapshot-backed prompt builders for both direct API and file-capable agents.
- Modify: `lark_agent_bridge/knowledge/source_investigation.py`
  - Build/update source-investigation snapshots keyed by canonical question family and use them when the same investigation repeats.
- Modify: `tests/test_agents.py`
  - Bug-followup prompt assembly and snapshot invalidation regressions.
- Modify: `tests/test_knowledge.py`
  - Source-investigation repeat-question prompt reuse regressions.
- Modify: `docs/configuration.md`
  - Document the new snapshot behavior and its “facts only, not conclusions” constraint.

---

### Task 1: Add shared snapshot primitives and failing unit tests

**Files:**
- Create: `lark_agent_bridge/prompt_snapshots.py`
- Create: `tests/test_prompt_snapshots.py`

- [ ] **Step 1: Write the failing unit tests**

```python
def test_bug_snapshot_persists_only_facts_and_evidence_refs(tmp_path):
    snapshot = BugPromptSnapshot(
        scope_key="bug:6995823164:om_root",
        analysis_kind="startup",
        stable_facts=[
            SnapshotFact(label="bug_url", value="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6995823164"),
            SnapshotFact(label="target_time", value="2026-05-21 03:38:17"),
        ],
        evidence_refs=[
            SnapshotEvidence(title="unity_context", path="module_core/.../UnityPlayerStateContext.java", locator="L297-L311"),
        ],
        open_questions=["03:38:17 时是否再次出现 displayChanged(surface=null)"],
    )

    path = tmp_path / "conversation_facts.json"
    write_prompt_snapshot(path, snapshot)
    loaded = read_prompt_snapshot(path)

    assert loaded.analysis_kind == "startup"
    assert "结论摘要" not in path.read_text(encoding="utf-8")
    assert loaded.open_questions == ["03:38:17 时是否再次出现 displayChanged(surface=null)"]


def test_snapshot_conflict_terms_drop_prior_hypothesis_from_prefix():
    snapshot = BugPromptSnapshot(
        scope_key="bug:1:root",
        analysis_kind="startup",
        stable_facts=[SnapshotFact(label="target_time", value="2026-05-21 03:38:17")],
        evidence_refs=[],
        open_questions=["displayChanged 是否是运行期关键线索"],
        prior_hypothesis="怀疑 displayChanged 是主因",
    )

    rendered = render_bug_snapshot_prefix(
        snapshot,
        followup_text="上一轮不对，重新从源码看 displayChanged",
    )

    assert "怀疑 displayChanged 是主因" not in rendered
    assert "target_time" in rendered
```

- [ ] **Step 2: Run the snapshot tests to verify they fail**

Run: `PYTHONPATH=. pytest -q tests/test_prompt_snapshots.py`
Expected: FAIL because the snapshot module and dataclasses do not exist yet.

- [ ] **Step 3: Add the snapshot dataclasses and persistence helpers**

```python
@dataclass(slots=True)
class SnapshotFact:
    label: str
    value: str


@dataclass(slots=True)
class SnapshotEvidence:
    title: str
    path: str
    locator: str


@dataclass(slots=True)
class BugPromptSnapshot:
    scope_key: str
    analysis_kind: str
    stable_facts: list[SnapshotFact]
    evidence_refs: list[SnapshotEvidence]
    open_questions: list[str]
    prior_hypothesis: str = ""
```

```python
def write_prompt_snapshot(path: Path, snapshot: BugPromptSnapshot) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(snapshot), ensure_ascii=False, indent=2), encoding="utf-8")


def render_bug_snapshot_prefix(snapshot: BugPromptSnapshot, *, followup_text: str) -> str:
    conflict = any(term in followup_text for term in ("不对", "结果不合理", "重新从源码看", "不要沿用"))
    lines = ["### 会话事实快照"]
    lines.extend(f"- {fact.label}: {fact.value}" for fact in snapshot.stable_facts)
    lines.append("### 证据目录")
    lines.extend(f"- {item.title}: {item.path} {item.locator}" for item in snapshot.evidence_refs)
    if snapshot.open_questions:
        lines.append("### 未决问题")
        lines.extend(f"- {question}" for question in snapshot.open_questions)
    if snapshot.prior_hypothesis and not conflict:
        lines.append("### 候选假设")
        lines.append(f"- {snapshot.prior_hypothesis}")
    return "\n".join(lines)
```

- [ ] **Step 4: Re-run the snapshot tests**

Run: `PYTHONPATH=. pytest -q tests/test_prompt_snapshots.py`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add lark_agent_bridge/prompt_snapshots.py tests/test_prompt_snapshots.py
git commit -m "feat: add prompt snapshot primitives"
```

### Task 2: Snapshot bug followup/reanalysis facts instead of replaying long summaries

**Files:**
- Modify: `lark_agent_bridge/agents/bug_runner.py`
- Modify: `tests/test_agents.py`
- Test: `tests/test_agents.py`

- [ ] **Step 1: Write the failing bug-followup prompt tests**

```python
def test_bug_reanalysis_prompt_for_api_uses_snapshot_prefix_before_incremental_tail(self):
    prompt = runner._build_bug_agent_summary_prompt_for_api(
        request_text="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6995823164 调查3D启动生命周期",
        request_artifact=request_file,
        metadata_path=metadata_file,
        followup_text="重新源码分析 重点看 displaychange",
        previous_summary_path=previous_summary,
    )

    assert "### 会话事实快照" in prompt
    assert "### 本次追问/修正\n重新源码分析 重点看 displaychange" in prompt
    assert "### 上一轮 Agent 总结" not in prompt


def test_bug_reanalysis_snapshot_rebuilds_when_analysis_kind_changes(self):
    snapshot = runner._build_or_refresh_bug_prompt_snapshot(
        details={"analysis_kind": "startup", "report_version": 7},
        followup_text="改查信号链路 SIGNAL_VCU_ELECTRICIT_PERCENT",
        plans_override=[BugAnalysisPlan(kind="signal", signal_code="SIGNAL_VCU_ELECTRICIT_PERCENT")],
    )

    assert snapshot.analysis_kind == "signal"
```

- [ ] **Step 2: Run the bug-followup prompt tests to verify they fail**

Run: `PYTHONPATH=. pytest -q tests/test_agents.py -k "snapshot_prefix_before_incremental_tail or snapshot_rebuilds_when_analysis_kind_changes"`
Expected: FAIL because bug followup still embeds previous summaries and has no snapshot builder.

- [ ] **Step 3: Add bug snapshot persistence and prompt assembly**

```python
def _bug_prompt_snapshot_path(self, output_dir: Path) -> Path:
    return output_dir / "conversation_facts.json"


def _build_or_refresh_bug_prompt_snapshot(... ) -> BugPromptSnapshot:
    return BugPromptSnapshot(
        scope_key=f"{report_group_key}:{root_message_id}",
        analysis_kind=current_kind,
        stable_facts=[
            SnapshotFact(label="bug_url", value=bug_url),
            SnapshotFact(label="target_time", value=target_time),
            SnapshotFact(label="report_version", value=str(report_version)),
            SnapshotFact(label="prepared_log_input", value=str(prepared_input)),
        ],
        evidence_refs=_bug_snapshot_evidence_refs(...),
        open_questions=_bug_snapshot_open_questions(...),
    )
```

```python
snapshot = self._build_or_refresh_bug_prompt_snapshot(...)
write_prompt_snapshot(self._bug_prompt_snapshot_path(output_dir), snapshot)
prompt = (
    self._bug_summary_prompt_header(...)
    + render_bug_snapshot_prefix(snapshot, followup_text=followup_text)
    + self._bug_summary_incremental_tail(followup_text=followup_text, metadata_path=metadata_path)
)
```

- [ ] **Step 4: Re-run the focused bug-followup tests**

Run: `PYTHONPATH=. pytest -q tests/test_agents.py -k "snapshot_prefix_before_incremental_tail or snapshot_rebuilds_when_analysis_kind_changes"`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add lark_agent_bridge/agents/bug_runner.py tests/test_agents.py
git commit -m "feat: snapshot multi-turn bug followup facts"
```

### Task 3: Prevent prior conclusions from becoming stable prefix context

**Files:**
- Modify: `lark_agent_bridge/agents/bug_runner.py`
- Modify: `tests/test_agents.py`
- Test: `tests/test_agents.py`

- [ ] **Step 1: Write the failing conclusion-isolation tests**

```python
def test_bug_snapshot_prefix_keeps_open_questions_but_not_prior_conclusion_paragraphs(self):
    prompt = runner._bug_summary_incremental_tail(
        followup_text="重新源码分析 displaychange",
        metadata_path=metadata_file,
        previous_summary_path=previous_summary,
    )

    assert "### 上一轮 Agent 总结" not in prompt
    assert "### 旧结论（仅供参考）" not in render_bug_snapshot_prefix(snapshot, followup_text="重新源码分析")


def test_bug_snapshot_tail_reads_previous_summary_only_for_explicit_compare_request(self):
    prompt = runner._build_bug_agent_summary_prompt_for_api(
        request_text="...",
        request_artifact=request_file,
        metadata_path=metadata_file,
        followup_text="你上次为什么判断 displayChanged 是关键线索",
        previous_summary_path=previous_summary,
    )

    assert "### 上一轮 Agent 总结" in prompt
```

- [ ] **Step 2: Run the conclusion-isolation tests to verify they fail**

Run: `PYTHONPATH=. pytest -q tests/test_agents.py -k "conclusion_paragraphs or explicit_compare_request"`
Expected: FAIL because previous summaries are still always eligible input for followup prompts.

- [ ] **Step 3: Gate old-summary injection behind explicit compare intent**

```python
def _followup_needs_previous_summary_text(self, followup_text: str) -> bool:
    lowered = followup_text.casefold()
    return any(term in lowered for term in ("上次为什么", "上一轮为什么", "新旧结论", "对比上一轮"))
```

```python
if previous_summary_path is not None and self._followup_needs_previous_summary_text(followup_text):
    prompt += f"### 上一轮 Agent 总结\n{prev_text}\n\n"
```

- [ ] **Step 4: Re-run the conclusion-isolation tests**

Run: `PYTHONPATH=. pytest -q tests/test_agents.py -k "conclusion_paragraphs or explicit_compare_request"`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add lark_agent_bridge/agents/bug_runner.py tests/test_agents.py
git commit -m "fix: keep prior conclusions out of stable prompt prefix"
```

### Task 4: Add repeat-question snapshots for source investigation

**Files:**
- Modify: `lark_agent_bridge/knowledge/source_investigation.py`
- Modify: `tests/test_knowledge.py`
- Test: `tests/test_knowledge.py`

- [ ] **Step 1: Write the failing source-investigation tests**

```python
def test_source_investigation_repeat_question_uses_fact_snapshot_prefix(self):
    first = runner.run("如何模拟 前车起步信号 源码分析", hits=hits)
    second_prompt = runner._prompt("如何模拟 前车起步信号 源码分析", hits=hits)

    assert "### 调查事实快照" in second_prompt
    assert "### 当前问题增量" in second_prompt


def test_source_investigation_single_question_does_not_write_snapshot(self):
    runner.run("一次性源码调查", hits=hits)
    assert not (data_dir / "source_investigations" / "snapshots").exists()
```

- [ ] **Step 2: Run the focused source-investigation tests to verify they fail**

Run: `PYTHONPATH=. pytest -q tests/test_knowledge.py -k "fact_snapshot_prefix or single_question_does_not_write_snapshot"`
Expected: FAIL because source investigation has no repeat-question snapshot logic.

- [ ] **Step 3: Add canonical-keyed source snapshots**

```python
def _source_snapshot_key(self, question: str, hits: list[SearchHit]) -> str:
    return slugify(_canonical_question_family(question))


def _source_snapshot_path(self, key: str) -> Path:
    return (self.config.data_dir / "source_investigations" / "snapshots" / f"{key}.json").resolve()
```

```python
if self._is_repeat_source_investigation(question, hits):
    snapshot = self._load_or_build_source_snapshot(question, hits)
    prompt = self._render_source_snapshot_prefix(snapshot) + self._render_source_increment(question, hits)
else:
    prompt = self._prompt_without_snapshot(question, hits)
```

- [ ] **Step 4: Re-run the focused source-investigation tests**

Run: `PYTHONPATH=. pytest -q tests/test_knowledge.py -k "fact_snapshot_prefix or single_question_does_not_write_snapshot"`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add lark_agent_bridge/knowledge/source_investigation.py tests/test_knowledge.py
git commit -m "feat: reuse source investigation fact snapshots"
```

### Task 5: Document the cache strategy and run focused regressions

**Files:**
- Modify: `docs/configuration.md`
- Modify: `tests/test_agents.py`
- Modify: `tests/test_knowledge.py`
- Modify: `tests/test_prompt_snapshots.py`

- [ ] **Step 1: Add the docs section**

```markdown
## Prompt Snapshot Reuse

- 单次独立请求不会写长期快照。
- 同一 `root_message_id` 或同一 bug/report group 的多轮追问会写 `conversation_facts.json`。
- 快照只缓存事实、证据定位和未决问题；不会把上一轮自然语言结论当作稳定前缀。
- 如果追问出现“结果不合理 / 重新从源码看 / 不要沿用上次”等冲突词，旧假设会自动降级或跳过。
```

- [ ] **Step 2: Run the bug snapshot regression slice**

Run: `PYTHONPATH=. pytest -q tests/test_prompt_snapshots.py tests/test_agents.py -k "snapshot or followup or previous_summary"`
Expected: PASS

- [ ] **Step 3: Run the source-investigation regression slice**

Run: `PYTHONPATH=. pytest -q tests/test_knowledge.py -k "source_investigation or fact_snapshot_prefix"`
Expected: PASS

- [ ] **Step 4: Run the combined focused suite**

Run: `PYTHONPATH=. pytest -q tests/test_prompt_snapshots.py tests/test_agents.py tests/test_knowledge.py`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add docs/configuration.md tests/test_prompt_snapshots.py tests/test_agents.py tests/test_knowledge.py
git commit -m "docs: describe cache-friendly prompt snapshot strategy"
```

---

## Self-Review

- Spec coverage: this plan covers the approved scope only — multi-turn bug followups and same-bug / same-report-group reuse, plus repeat source-investigation questions. It intentionally leaves single-shot bug requests unchanged.
- Placeholder scan: every code-touching step includes a concrete file path, concrete test target, and concrete code skeleton; no TODO/TBD markers remain.
- Type consistency: the shared unit is `BugPromptSnapshot` / `SnapshotFact` / `SnapshotEvidence`, with `BugAnalysisRunner` and `SourceInvestigationRunner` both consuming the same renderer pattern rather than inventing separate prompt formats.
