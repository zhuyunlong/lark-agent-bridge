"""Tests for report_version module."""

from __future__ import annotations

import tempfile
import threading
from pathlib import Path

from lark_agent_bridge.report_version import (
    ReportVersion,
    ReportVersionStore,
    VersionGroup,
    derive_group_key,
)


class TestReportVersion:
    def test_to_dict_roundtrip(self):
        v = ReportVersion(
            version=2,
            job_id="j1",
            report_url="http://example.com",
            summary="test",
            provider="claude",
            mode="bug_analysis",
            duration_seconds=42.0,
        )
        d = v.to_dict()
        restored = ReportVersion.from_dict(d)
        assert restored.version == 2
        assert restored.job_id == "j1"
        assert restored.provider == "claude"


class TestVersionGroup:
    def test_latest_version(self):
        g = VersionGroup(
            group_key="test",
            versions=[
                ReportVersion(version=1),
                ReportVersion(version=3),
                ReportVersion(version=2),
            ],
        )
        assert g.latest_version == 3
        assert g.latest is not None
        assert g.latest.version == 3

    def test_empty_group(self):
        g = VersionGroup(group_key="test")
        assert g.latest_version == 0
        assert g.latest is None

    def test_to_dict_roundtrip(self):
        g = VersionGroup(
            group_key="k",
            label="Test Group",
            versions=[ReportVersion(version=1, job_id="j1")],
            created_at="2024-01-01",
        )
        d = g.to_dict()
        restored = VersionGroup.from_dict(d)
        assert restored.group_key == "k"
        assert len(restored.versions) == 1


class TestReportVersionStore:
    def test_add_first_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReportVersionStore(Path(tmp) / "versions.json")
            v = store.add_version("group1", job_id="j1", report_url="http://test")
            assert v.version == 1
            assert store.group_count == 1

    def test_add_multiple_versions(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReportVersionStore(Path(tmp) / "versions.json")
            store.add_version("g1", job_id="j1")
            store.add_version("g1", job_id="j2")
            v3 = store.add_version("g1", job_id="j3")
            assert v3.version == 3
            group = store.get_group("g1")
            assert group is not None
            assert len(group.versions) == 3

    def test_get_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReportVersionStore(Path(tmp) / "versions.json")
            store.add_version("g1", job_id="j1")
            store.add_version("g1", job_id="j2")
            v = store.get_version("g1", 2)
            assert v is not None
            assert v.job_id == "j2"

    def test_get_version_not_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReportVersionStore(Path(tmp) / "versions.json")
            assert store.get_version("g1", 1) is None

    def test_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "versions.json"
            store1 = ReportVersionStore(path)
            store1.add_version("g1", job_id="j1", summary="test")

            store2 = ReportVersionStore(path)
            assert store2.group_count == 1
            v = store2.get_version("g1", 1)
            assert v is not None
            assert v.summary == "test"

    def test_compare_versions(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReportVersionStore(Path(tmp) / "versions.json")
            store.add_version("g1", provider="claude", summary="根因A")
            store.add_version("g1", provider="codex", summary="根因B")
            cmp = store.compare_versions("g1", 1, 2)
            assert cmp is not None
            assert "provider" in cmp["diff_fields"]
            assert "summary" in cmp["diff_fields"]

    def test_compare_not_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReportVersionStore(Path(tmp) / "versions.json")
            assert store.compare_versions("g1", 1, 2) is None

    def test_list_groups(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReportVersionStore(Path(tmp) / "versions.json")
            store.add_version("g1", job_id="j1")
            store.add_version("g2", job_id="j2")
            store.add_version("g3", job_id="j3")
            groups = store.list_groups(limit=2)
            assert len(groups) == 2

    def test_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReportVersionStore(Path(tmp) / "versions.json")
            store.add_version("g1")
            store.add_version("g2")
            removed = store.clear()
            assert removed == 2
            assert store.group_count == 0

    def test_prune_old_versions(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReportVersionStore(Path(tmp) / "versions.json")
            for i in range(10):
                store.add_version("g1", job_id=f"j{i}")
            removed = store.prune_old_versions(max_versions_per_group=5)
            assert removed == 5
            group = store.get_group("g1")
            assert group is not None
            assert len(group.versions) == 5
            # Should keep the latest 5 (versions 6-10)
            assert group.versions[0].version == 6

    def test_label_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReportVersionStore(Path(tmp) / "versions.json")
            store.add_version("g1", label="first")
            store.add_version("g1", label="updated")
            group = store.get_group("g1")
            assert group is not None
            assert group.label == "updated"


class TestReportVersionStoreConcurrency:
    def test_concurrent_distinct_groups_do_not_corrupt(self):
        # Narrowing the dispatcher lock from chat to chain-root lets two
        # same-chat analyses run concurrently, so add_version() can now be
        # called in parallel. Without a lock, _persist() iterates _groups while
        # another thread inserts a new group key -> "dictionary changed size
        # during iteration". A tiny switch interval forces the interleave so the
        # latent race is observed deterministically. Every add must survive.
        import sys

        old_interval = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                store = ReportVersionStore(Path(tmp) / "versions.json")
                n, iters = 16, 25
                barrier = threading.Barrier(n)
                errors: list[Exception] = []

                def worker(i: int) -> None:
                    try:
                        barrier.wait(timeout=5)
                        for j in range(iters):
                            store.add_version(f"bug:{i}-{j}", job_id=f"job-{i}-{j}")
                    except Exception as exc:
                        errors.append(exc)

                threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(timeout=15)

                assert not errors, errors
                # No lost updates: every distinct group is recorded.
                assert store.group_count == n * iters
                # And the persisted file stays valid / reloadable with all groups.
                reloaded = ReportVersionStore(store.state_file)
                assert reloaded.group_count == n * iters
        finally:
            sys.setswitchinterval(old_interval)

    def test_concurrent_same_group_assigns_unique_versions(self):
        # Concurrent adds to the SAME group must not collide on version numbers.
        import sys

        old_interval = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                store = ReportVersionStore(Path(tmp) / "versions.json")
                n = 40
                barrier = threading.Barrier(n)
                errors: list[Exception] = []

                def worker(i: int) -> None:
                    try:
                        barrier.wait(timeout=5)
                        store.add_version("bug:X", job_id=f"job-{i}")
                    except Exception as exc:
                        errors.append(exc)

                threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(timeout=10)

                assert not errors, errors
                group = store.get_group("bug:X")
                assert group is not None
                assert len(group.versions) == n
                numbers = sorted(v.version for v in group.versions)
                assert numbers == list(range(1, n + 1))
        finally:
            sys.setswitchinterval(old_interval)


class TestDeriveGroupKey:
    def test_bug_url_priority(self):
        key = derive_group_key(bug_url="http://bug/1", case_id="c1", root_message_id="m1")
        assert key == "bug:http://bug/1"

    def test_case_id_fallback(self):
        key = derive_group_key(case_id="c1", root_message_id="m1")
        assert key == "case:c1"

    def test_root_message_fallback(self):
        key = derive_group_key(root_message_id="m1")
        assert key == "msg:m1"

    def test_anonymous(self):
        key = derive_group_key()
        assert key.startswith("anon:")
