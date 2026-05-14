"""Tests for the case_store module."""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

from lark_agent_bridge.case_store import (
    CaseRecord,
    CaseStore,
    _extract_root_cause_tags,
    _infer_confidence,
    _infer_problem_type,
)


class TestCaseRecord:
    def test_to_dict_roundtrip(self):
        case = CaseRecord(
            case_id="test-001",
            problem_type="启动卡顿",
            analysis_mode="bug_analysis",
            conclusion="根因是内存泄漏",
            root_cause_tags=["memory_leak"],
        )
        d = case.to_dict()
        restored = CaseRecord.from_dict(d)
        assert restored.case_id == "test-001"
        assert restored.problem_type == "启动卡顿"
        assert restored.root_cause_tags == ["memory_leak"]
        assert restored.conclusion == "根因是内存泄漏"

    def test_from_dict_defaults(self):
        case = CaseRecord.from_dict({"case_id": "x"})
        assert case.case_id == "x"
        assert case.conclusion_confidence == "unknown"
        assert case.reproduced is False
        assert case.human_confirmed is False
        assert case.root_cause_tags == []


class TestCaseStore:
    def test_save_and_get(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CaseStore(Path(tmp) / "cases.json")
            case = CaseRecord(case_id="c1", problem_type="crash")
            store.save(case)
            assert store.count == 1
            assert store.get("c1") is not None
            assert store.get("c1").problem_type == "crash"

    def test_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cases.json"
            store1 = CaseStore(path)
            store1.save(CaseRecord(case_id="c1", conclusion="test"))
            store2 = CaseStore(path)
            assert store2.count == 1
            assert store2.get("c1").conclusion == "test"

    def test_search_by_problem_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CaseStore(Path(tmp) / "cases.json")
            store.save(CaseRecord(case_id="c1", problem_type="启动卡顿"))
            store.save(CaseRecord(case_id="c2", problem_type="crash"))
            results = store.search(problem_type="启动")
            assert len(results) == 1
            assert results[0].case_id == "c1"

    def test_search_by_keyword(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CaseStore(Path(tmp) / "cases.json")
            store.save(CaseRecord(case_id="c1", conclusion="内存泄漏导致OOM"))
            store.save(CaseRecord(case_id="c2", conclusion="网络超时"))
            results = store.search(keyword="内存")
            assert len(results) == 1
            assert results[0].case_id == "c1"

    def test_search_by_root_cause_tag(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CaseStore(Path(tmp) / "cases.json")
            store.save(CaseRecord(case_id="c1", root_cause_tags=["memory_leak"]))
            store.save(CaseRecord(case_id="c2", root_cause_tags=["timeout"]))
            results = store.search(root_cause_tag="memory_leak")
            assert len(results) == 1

    def test_search_by_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CaseStore(Path(tmp) / "cases.json")
            store.save(CaseRecord(case_id="c1", analysis_mode="bug_analysis"))
            store.save(CaseRecord(case_id="c2", analysis_mode="signal_lifecycle"))
            results = store.search(analysis_mode="bug_analysis")
            assert len(results) == 1

    def test_save_from_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CaseStore(Path(tmp) / "cases.json")

            class FakeResult:
                success = True
                skipped = False
                job_id = "job-001"
                message = "根因是内存泄漏导致OOM"
                duration_seconds = 42.0
                details = {
                    "mode": "bug_analysis",
                    "published_report_url": "http://example.com/report",
                    "provider": "claude",
                }

            class FakeEvent:
                chat_id = "oc_123"
                sender_id = "ou_456"

            case = store.save_from_result(FakeResult(), event=FakeEvent(), request_text="调查OOM")
            assert case is not None
            assert case.case_id == "job-001"
            assert case.analysis_mode == "bug_analysis"
            assert "memory_leak" in case.root_cause_tags
            assert case.conclusion_confidence == "high"  # "根因" matches high confidence
            assert case.chat_id == "oc_123"

    def test_save_from_result_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CaseStore(Path(tmp) / "cases.json")

            class FakeResult:
                success = True
                skipped = True
                job_id = "j1"
                message = ""
                details = {"mode": "chat"}

            assert store.save_from_result(FakeResult()) is None

    def test_update_human_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CaseStore(Path(tmp) / "cases.json")
            store.save(CaseRecord(case_id="c1"))
            case = store.update_human_confirmation("c1", confirmed=True, notes="已确认")
            assert case is not None
            assert case.human_confirmed is True
            assert case.extra["human_notes"] == "已确认"

    def test_list_recent(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CaseStore(Path(tmp) / "cases.json")
            for i in range(5):
                store.save(CaseRecord(case_id=f"c{i}"))
            recent = store.list_recent(limit=3)
            assert len(recent) == 3

    def test_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CaseStore(Path(tmp) / "cases.json")
            store.save(CaseRecord(case_id="c1"))
            removed = store.clear()
            assert removed == 1
            assert store.count == 0

    def test_prune_expired(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CaseStore(Path(tmp) / "cases.json")
            old_time = (datetime.now(timezone.utc) - timedelta(hours=10)).isoformat()
            case = CaseRecord(case_id="old")
            store.save(case)
            # Manually backdate
            store._cases["old"].created_at = old_time
            store._cases["old"].updated_at = old_time
            store._persist()
            store.save(CaseRecord(case_id="new"))
            removed = store.prune_expired(max_age_hours=6)
            assert removed == 1
            assert store.count == 1
            assert store.get("new") is not None

    def test_enforce_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CaseStore(Path(tmp) / "cases.json", max_cases=12)
            for i in range(15):
                store.save(CaseRecord(case_id=f"c{i}"))
            assert store.count == 12


class TestInferHelpers:
    def test_infer_problem_type_startup(self):
        assert _infer_problem_type("启动超时问题", "bug_analysis") == "启动异常"

    def test_infer_problem_type_crash(self):
        assert _infer_problem_type("app crash due to null pointer", "bug_analysis") == "闪退/Crash"

    def test_infer_problem_type_default(self):
        assert _infer_problem_type("unknown issue", "signal_lifecycle") == "信号异常"

    def test_infer_confidence_high(self):
        assert _infer_confidence("根因已确定") == "high"

    def test_infer_confidence_low(self):
        assert _infer_confidence("信息不足，无法判断") == "low"

    def test_infer_confidence_unknown(self):
        assert _infer_confidence("分析完成") == "unknown"

    def test_extract_root_cause_tags(self):
        tags = _extract_root_cause_tags("内存泄漏导致超时，可能有死锁")
        assert "memory_leak" in tags
        assert "timeout" in tags
        assert "deadlock" in tags

    def test_extract_root_cause_tags_empty(self):
        assert _extract_root_cause_tags("一切正常") == []
