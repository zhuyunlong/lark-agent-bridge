"""Report version tracking for the bridge.

Tracks multiple analysis versions for the same bug/case, enabling
re-analysis comparison and version history.

Each report is assigned a version number within a *version group* keyed
by the bug URL, case ID, or conversation root message.  When a user
re-analyzes the same bug, a new version is created in the same group.

The version store is a simple JSON file persisted alongside other state.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class ReportVersion:
    """A single versioned report entry."""

    version: int
    job_id: str = ""
    report_url: str = ""
    summary: str = ""
    provider: str = ""
    mode: str = ""
    created_at: str = ""
    duration_seconds: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "job_id": self.job_id,
            "report_url": self.report_url,
            "summary": self.summary,
            "provider": self.provider,
            "mode": self.mode,
            "created_at": self.created_at,
            "duration_seconds": self.duration_seconds,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ReportVersion:
        return cls(
            version=d.get("version", 1),
            job_id=d.get("job_id", ""),
            report_url=d.get("report_url", ""),
            summary=d.get("summary", ""),
            provider=d.get("provider", ""),
            mode=d.get("mode", ""),
            created_at=d.get("created_at", ""),
            duration_seconds=d.get("duration_seconds", 0.0),
            metadata=d.get("metadata", {}),
        )


@dataclass
class VersionGroup:
    """A group of report versions for the same bug/analysis target."""

    group_key: str
    label: str = ""
    versions: list[ReportVersion] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    @property
    def latest_version(self) -> int:
        if not self.versions:
            return 0
        return max(v.version for v in self.versions)

    @property
    def latest(self) -> ReportVersion | None:
        if not self.versions:
            return None
        return max(self.versions, key=lambda v: v.version)

    def to_dict(self) -> dict[str, Any]:
        return {
            "group_key": self.group_key,
            "label": self.label,
            "versions": [v.to_dict() for v in self.versions],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> VersionGroup:
        versions = [ReportVersion.from_dict(v) for v in d.get("versions", [])]
        return cls(
            group_key=d.get("group_key", ""),
            label=d.get("label", ""),
            versions=versions,
            created_at=d.get("created_at", ""),
            updated_at=d.get("updated_at", ""),
        )


class ReportVersionStore:
    """Persistent store for report version tracking."""

    def __init__(self, state_file: str | Path) -> None:
        self.state_file = Path(state_file)
        self._groups: dict[str, VersionGroup] = self._load()
        # RLock (not Lock): compare_versions() calls get_version() internally,
        # so the lock must be re-entrant. Guards every mutation/read of _groups
        # plus the whole-file _persist(); two same-chat analyses can now run
        # concurrently (chain-root lock), so add_version is called in parallel.
        self._lock = threading.RLock()

    # -- public API --

    def add_version(
        self,
        group_key: str,
        *,
        version: int | None = None,
        job_id: str = "",
        report_url: str = "",
        summary: str = "",
        provider: str = "",
        mode: str = "",
        duration_seconds: float = 0.0,
        label: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> ReportVersion:
        """Add a new version to a group, creating the group if needed.

        Returns the created ``ReportVersion``.
        """
        now = datetime.now(timezone.utc).isoformat()

        with self._lock:
            group = self._groups.get(group_key)
            if group is None:
                group = VersionGroup(
                    group_key=group_key,
                    label=label or group_key,
                    created_at=now,
                )
                self._groups[group_key] = group

            # Honour a caller-supplied version only if it is still free; under
            # concurrent same-group adds the peeked number may already be taken,
            # so fall back to the authoritative next number to avoid collisions.
            requested = int(version or 0)
            existing = {v.version for v in group.versions}
            next_version = requested if requested and requested not in existing else group.latest_version + 1
            version = ReportVersion(
                version=next_version,
                job_id=job_id,
                report_url=report_url,
                summary=_truncate(summary, 500),
                provider=provider,
                mode=mode,
                created_at=now,
                duration_seconds=duration_seconds,
                metadata=metadata or {},
            )
            group.versions.append(version)
            group.updated_at = now
            if label:
                group.label = label
            self._persist()
            return version

    def get_group(self, group_key: str) -> VersionGroup | None:
        with self._lock:
            return self._groups.get(group_key)

    def peek_next_version(self, group_key: str) -> int:
        with self._lock:
            group = self._groups.get(group_key)
            if group is None:
                return 1
            return group.latest_version + 1

    def get_version(self, group_key: str, version: int) -> ReportVersion | None:
        with self._lock:
            group = self._groups.get(group_key)
            if group is None:
                return None
            for v in group.versions:
                if v.version == version:
                    return v
            return None

    def list_groups(self, *, limit: int = 50) -> list[VersionGroup]:
        """Return most recently updated groups."""
        with self._lock:
            sorted_groups = sorted(
                self._groups.values(),
                key=lambda g: g.updated_at or g.created_at,
                reverse=True,
            )
            return sorted_groups[:limit]

    def delete_by_job_id(self, job_id: str) -> int:
        normalized = job_id.strip()
        if not normalized:
            return 0
        removed = 0
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            for group_key, group in list(self._groups.items()):
                kept = [version for version in group.versions if version.job_id != normalized]
                removed += len(group.versions) - len(kept)
                if len(kept) == len(group.versions):
                    continue
                if not kept:
                    del self._groups[group_key]
                    continue
                group.versions = kept
                group.updated_at = now
            if removed:
                self._persist()
        return removed

    def compare_versions(
        self,
        group_key: str,
        version_a: int,
        version_b: int,
    ) -> dict[str, Any] | None:
        """Compare two versions within a group.

        Returns a dict with both versions' data and a diff summary,
        or ``None`` if either version is not found.
        """
        with self._lock:
            va = self.get_version(group_key, version_a)
            vb = self.get_version(group_key, version_b)
            if va is None or vb is None:
                return None

            diff_fields: list[str] = []
            if va.provider != vb.provider:
                diff_fields.append("provider")
            if va.mode != vb.mode:
                diff_fields.append("mode")
            if va.summary != vb.summary:
                diff_fields.append("summary")

            return {
                "group_key": group_key,
                "version_a": va.to_dict(),
                "version_b": vb.to_dict(),
                "diff_fields": diff_fields,
                "duration_delta": vb.duration_seconds - va.duration_seconds,
            }

    @property
    def group_count(self) -> int:
        with self._lock:
            return len(self._groups)

    def clear(self) -> int:
        with self._lock:
            count = len(self._groups)
            self._groups.clear()
            self._persist()
            return count

    def prune_old_versions(self, *, max_versions_per_group: int = 20) -> int:
        """Remove oldest versions beyond the limit per group."""
        removed = 0
        with self._lock:
            for group in self._groups.values():
                if len(group.versions) <= max_versions_per_group:
                    continue
                group.versions.sort(key=lambda v: v.version)
                excess = len(group.versions) - max_versions_per_group
                group.versions = group.versions[excess:]
                removed += excess
            if removed:
                self._persist()
        return removed

    # -- persistence --

    def _persist(self) -> None:
        # Caller holds self._lock. Atomic write (tmp + replace) so a concurrent
        # reload never observes a half-written file.
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        data = {k: g.to_dict() for k, g in self._groups.items()}
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.state_file)

    def _load(self) -> dict[str, VersionGroup]:
        if not self.state_file.exists():
            return {}
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        groups: dict[str, VersionGroup] = {}
        for key, value in data.items():
            if isinstance(value, dict):
                groups[key] = VersionGroup.from_dict(value)
        return groups


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def derive_group_key(
    *,
    bug_url: str = "",
    case_id: str = "",
    root_message_id: str = "",
) -> str:
    """Derive a version group key from available identifiers.

    Priority: bug_url > case_id > root_message_id.
    """
    if bug_url:
        return f"bug:{bug_url}"
    if case_id:
        return f"case:{case_id}"
    if root_message_id:
        return f"msg:{root_message_id}"
    return f"anon:{int(time.time())}"


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"
