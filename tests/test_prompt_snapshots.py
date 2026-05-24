import json
from pathlib import Path

import pytest

from lark_agent_bridge.prompt_snapshots import (
    BugPromptSnapshot,
    SnapshotEvidence,
    SnapshotFact,
    read_prompt_snapshot,
    render_bug_snapshot_prefix,
    write_prompt_snapshot,
)


def _build_snapshot() -> BugPromptSnapshot:
    return BugPromptSnapshot(
        scope_key="bug:6995823164:om_root",
        analysis_kind="startup",
        stable_facts=[
            SnapshotFact(
                label="bug_url",
                value="https://project.feishu.cn/xpfailuremgmt/buglo/detail/6995823164",
            ),
            SnapshotFact(label="target_time", value="2026-05-21 03:38:17"),
        ],
        evidence_refs=[
            SnapshotEvidence(
                title="unity_context",
                path="module_core/.../UnityPlayerStateContext.java",
                locator="L297-L311",
            ),
        ],
        open_questions=["03:38:17 时是否再次出现 displayChanged(surface=null)"],
        prior_hypothesis="怀疑 displayChanged 是主因",
    )


def test_write_prompt_snapshot_uses_atomic_replace(tmp_path, monkeypatch):
    snapshot = _build_snapshot()
    path = tmp_path / "conversation_facts.json"
    calls: list[tuple[Path, Path]] = []
    original_replace = Path.replace

    def tracking_replace(self: Path, target: Path) -> Path:
        calls.append((self, target))
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", tracking_replace)

    write_prompt_snapshot(path, snapshot)

    assert len(calls) == 1
    assert calls[0][1] == path


def test_bug_snapshot_round_trip_preserves_all_fields(tmp_path):
    snapshot = _build_snapshot()
    path = tmp_path / "conversation_facts.json"
    write_prompt_snapshot(path, snapshot)
    loaded = read_prompt_snapshot(path)

    assert loaded == snapshot
    assert "结论摘要" not in path.read_text(encoding="utf-8")


def test_read_prompt_snapshot_rejects_invalid_json(tmp_path):
    path = tmp_path / "conversation_facts.json"
    path.write_text("{not valid json", encoding="utf-8")

    with pytest.raises(ValueError, match="Invalid snapshot JSON"):
        read_prompt_snapshot(path)


@pytest.mark.parametrize(
    ("field_name", "field_value"),
    [
        ("open_questions", "not-a-list"),
        ("stable_facts", [{"label": "bug_url", "value": 123}]),
        ("evidence_refs", [{"title": "unity", "path": "foo.java", "locator": 1}]),
    ],
)
def test_read_prompt_snapshot_rejects_invalid_field_shapes(tmp_path, field_name, field_value):
    path = tmp_path / "conversation_facts.json"
    payload = {
        "scope_key": "bug:6995823164:om_root",
        "analysis_kind": "startup",
        "stable_facts": [{"label": "bug_url", "value": "https://example.test/bug/1"}],
        "evidence_refs": [{"title": "unity", "path": "foo.java", "locator": "L1-L2"}],
        "open_questions": ["question"],
        "prior_hypothesis": "hypothesis",
    }
    payload[field_name] = field_value
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    with pytest.raises(ValueError, match=field_name):
        read_prompt_snapshot(path)


def test_snapshot_conflict_terms_drop_prior_hypothesis_from_prefix():
    snapshot = _build_snapshot()

    rendered = render_bug_snapshot_prefix(
        snapshot,
        followup_text="上一轮不对，重新从源码看 displayChanged",
    )

    assert "怀疑 displayChanged 是主因" not in rendered
    assert "target_time" in rendered


def test_snapshot_conflict_terms_drop_prior_hypothesis_for_reinspect_wording():
    snapshot = _build_snapshot()

    rendered = render_bug_snapshot_prefix(
        snapshot,
        followup_text="重新源码分析 displaychange",
    )

    assert "怀疑 displayChanged 是主因" not in rendered


def test_render_bug_snapshot_prefix_includes_hypothesis_and_evidence_without_conflict():
    snapshot = _build_snapshot()

    rendered = render_bug_snapshot_prefix(
        snapshot,
        followup_text="继续补充同一条线索的证据",
    )

    assert "### 证据目录" in rendered
    assert "- unity_context: module_core/.../UnityPlayerStateContext.java L297-L311" in rendered
    assert "### 候选假设" in rendered
    assert "- 怀疑 displayChanged 是主因" in rendered
