"""Tests for the lifecycle module."""

from __future__ import annotations

import time
import unittest

from lark_agent_bridge.lifecycle import (
    AnalysisLifecycle,
    AnalysisState,
    AnalysisType,
    LifecycleStore,
    StateTransition,
    mode_to_analysis_type,
)


class TestAnalysisLifecycle(unittest.TestCase):
    def test_create_defaults(self):
        lc = AnalysisLifecycle()
        assert lc.lifecycle_id.startswith("lc_")
        assert lc.state == AnalysisState.CREATED
        assert lc.created_at > 0
        assert lc.is_terminal is False

    def test_valid_transition(self):
        lc = AnalysisLifecycle()
        t = lc.transition_to(AnalysisState.QUEUED, reason="test")
        assert lc.state == AnalysisState.QUEUED
        assert t.from_state == "created"
        assert t.to_state == "queued"
        assert len(lc.transitions) == 1

    def test_invalid_transition_raises(self):
        lc = AnalysisLifecycle(state=AnalysisState.COMPLETED)
        with self.assertRaisesRegex(ValueError, "Invalid transition"):
            lc.transition_to(AnalysisState.ANALYZING)

    def test_full_lifecycle(self):
        lc = AnalysisLifecycle(analysis_type=AnalysisType.BUG)
        lc.transition_to(AnalysisState.QUEUED)
        lc.transition_to(AnalysisState.DOWNLOADING)
        lc.transition_to(AnalysisState.ANALYZING)
        lc.mark_completed(conclusion="OOM", report_url="http://r", duration_seconds=42)
        assert lc.state == AnalysisState.COMPLETED
        assert lc.is_terminal is True
        assert lc.conclusion == "OOM"
        assert len(lc.transitions) == 4

    def test_mark_failed(self):
        lc = AnalysisLifecycle()
        lc.transition_to(AnalysisState.QUEUED)
        lc.transition_to(AnalysisState.ANALYZING)
        lc.mark_failed(error_message="timeout")
        assert lc.state == AnalysisState.FAILED
        assert lc.error_message == "timeout"

    def test_retry(self):
        lc = AnalysisLifecycle()
        lc.transition_to(AnalysisState.QUEUED)
        lc.transition_to(AnalysisState.ANALYZING)
        lc.mark_failed(error_message="err")
        lc.retry()
        assert lc.state == AnalysisState.QUEUED
        assert lc.retry_count == 1

    def test_can_transition_to(self):
        lc = AnalysisLifecycle()
        assert lc.can_transition_to(AnalysisState.QUEUED) is True
        assert lc.can_transition_to(AnalysisState.COMPLETED) is False

    def test_to_dict_roundtrip(self):
        lc = AnalysisLifecycle(
            analysis_type=AnalysisType.BUG,
            request_text="test",
            metadata={"key": "val"},
        )
        lc.transition_to(AnalysisState.QUEUED)
        d = lc.to_dict()
        restored = AnalysisLifecycle.from_dict(d)
        assert restored.lifecycle_id == lc.lifecycle_id
        assert restored.analysis_type == AnalysisType.BUG
        assert restored.state == AnalysisState.QUEUED
        assert len(restored.transitions) == 1

    def test_elapsed_seconds(self):
        lc = AnalysisLifecycle()
        assert lc.elapsed_seconds >= 0

    def test_cancel_from_created(self):
        lc = AnalysisLifecycle()
        lc.transition_to(AnalysisState.CANCELLED)
        assert lc.is_terminal is True


class TestStateTransition(unittest.TestCase):
    def test_to_dict_roundtrip(self):
        t = StateTransition(from_state="created", to_state="queued", timestamp=1000.0, reason="test")
        d = t.to_dict()
        restored = StateTransition.from_dict(d)
        assert restored.from_state == "created"
        assert restored.timestamp == 1000.0


class TestLifecycleStore(unittest.TestCase):
    def test_create_and_get(self):
        store = LifecycleStore()
        lc = store.create(AnalysisType.BUG, request_text="test")
        assert store.count == 1
        found = store.get(lc.lifecycle_id)
        assert found is not None
        assert found.request_text == "test"

    def test_find_by_job_id(self):
        store = LifecycleStore()
        lc = store.create(AnalysisType.BUG)
        lc.job_id = "j123"
        assert store.find_by_job_id("j123") is lc

    def test_find_by_message_id(self):
        store = LifecycleStore()
        lc = store.create(AnalysisType.BUG, message_id="m456")
        assert store.find_by_message_id("m456") is lc

    def test_list_active(self):
        store = LifecycleStore()
        lc1 = store.create(AnalysisType.BUG)
        lc2 = store.create(AnalysisType.CHAT)
        lc2.transition_to(AnalysisState.QUEUED)
        lc2.transition_to(AnalysisState.ANALYZING)
        lc2.mark_completed()
        active = store.list_active()
        assert len(active) == 1
        assert active[0].lifecycle_id == lc1.lifecycle_id

    def test_list_recent(self):
        store = LifecycleStore()
        for i in range(5):
            store.create(AnalysisType.BUG, request_text=f"test{i}")
        recent = store.list_recent(limit=3)
        assert len(recent) == 3

    def test_remove(self):
        store = LifecycleStore()
        lc = store.create(AnalysisType.BUG)
        assert store.remove(lc.lifecycle_id) is True
        assert store.count == 0

    def test_active_count(self):
        store = LifecycleStore()
        store.create(AnalysisType.BUG)
        lc2 = store.create(AnalysisType.CHAT)
        lc2.transition_to(AnalysisState.CANCELLED)
        assert store.active_count == 1

    def test_cleanup_terminal(self):
        store = LifecycleStore()
        lc = store.create(AnalysisType.BUG)
        lc.transition_to(AnalysisState.CANCELLED)
        lc.updated_at = time.time() - 7200
        removed = store.cleanup_terminal(max_age_seconds=3600)
        assert removed == 1
        assert store.count == 0

    def test_enforce_limit(self):
        store = LifecycleStore(max_active=12)
        for _ in range(15):
            lc = store.create(AnalysisType.BUG)
            lc.transition_to(AnalysisState.CANCELLED)
        assert store.count == 12


class TestModeToAnalysisType(unittest.TestCase):
    def test_known_modes(self):
        assert mode_to_analysis_type("bug_analysis") == AnalysisType.BUG
        assert mode_to_analysis_type("signal_lifecycle") == AnalysisType.SIGNAL
        assert mode_to_analysis_type("direct_analysis") == AnalysisType.DIRECT
        assert mode_to_analysis_type("chat") == AnalysisType.CHAT
        assert mode_to_analysis_type("basic_chat") == AnalysisType.CHAT

    def test_unknown_mode(self):
        assert mode_to_analysis_type("xyz") == AnalysisType.UNKNOWN


if __name__ == "__main__":
    unittest.main()
