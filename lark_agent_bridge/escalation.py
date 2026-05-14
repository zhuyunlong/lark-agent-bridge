"""Proactive push and escalation for the bridge.

Provides automatic notifications and escalation when:

- An analysis times out or takes too long → escalate to human.
- Analysis status changes → push update to the requesting chat.
- Required information is missing → prompt user to supply materials.
- A report is generated → notify relevant stakeholders.

The module is designed to be callable from the main app during event
processing and background monitoring.  It does *not* manage its own
timers; callers (e.g., health monitor, app loop) invoke checks.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

class EscalationLevel(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ESCALATE = "escalate"
    CRITICAL = "critical"


class PushReason(str, Enum):
    STATUS_CHANGE = "status_change"
    ANALYSIS_TIMEOUT = "analysis_timeout"
    ANALYSIS_COMPLETED = "analysis_completed"
    MISSING_MATERIAL = "missing_material"
    REPORT_READY = "report_ready"
    RETRY_LIMIT = "retry_limit"
    MANUAL_ESCALATION = "manual_escalation"


@dataclass
class PushNotification:
    """A notification to be sent proactively."""

    reason: PushReason
    level: EscalationLevel
    title: str
    message: str
    target_chat_id: str = ""
    target_user_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason.value,
            "level": self.level.value,
            "title": self.title,
            "message": self.message,
            "target_chat_id": self.target_chat_id,
            "target_user_id": self.target_user_id,
            "metadata": self.metadata,
            "created_at": self.created_at,
        }


@dataclass
class EscalationRule:
    """A rule that triggers escalation."""

    name: str
    timeout_seconds: float = 0.0
    max_retries: int = 0
    level: EscalationLevel = EscalationLevel.WARNING
    message_template: str = ""


# ---------------------------------------------------------------------------
# Default rules
# ---------------------------------------------------------------------------

DEFAULT_RULES: list[EscalationRule] = [
    EscalationRule(
        name="analysis_slow",
        timeout_seconds=300,  # 5 min
        level=EscalationLevel.WARNING,
        message_template="分析已运行 {elapsed:.0f} 秒，超过预期时间。",
    ),
    EscalationRule(
        name="analysis_timeout",
        timeout_seconds=900,  # 15 min
        level=EscalationLevel.ESCALATE,
        message_template="分析已超时 ({elapsed:.0f} 秒)，建议人工介入。",
    ),
    EscalationRule(
        name="retry_limit",
        max_retries=3,
        level=EscalationLevel.ESCALATE,
        message_template="已重试 {retries} 次仍失败，请人工检查。",
    ),
]


# ---------------------------------------------------------------------------
# Escalation checker
# ---------------------------------------------------------------------------

class EscalationChecker:
    """Evaluates jobs against escalation rules.

    Stateless: callers provide job metadata and the checker returns
    any triggered notifications.
    """

    def __init__(self, rules: list[EscalationRule] | None = None) -> None:
        self.rules = rules if rules is not None else list(DEFAULT_RULES)

    def check_timeout(
        self,
        *,
        elapsed_seconds: float,
        job_id: str = "",
        chat_id: str = "",
        user_id: str = "",
    ) -> list[PushNotification]:
        """Check if elapsed time triggers any timeout rules."""
        notifications: list[PushNotification] = []
        for rule in self.rules:
            if rule.timeout_seconds <= 0:
                continue
            if elapsed_seconds >= rule.timeout_seconds:
                msg = rule.message_template.format(elapsed=elapsed_seconds)
                notifications.append(
                    PushNotification(
                        reason=PushReason.ANALYSIS_TIMEOUT,
                        level=rule.level,
                        title=f"⏰ {rule.name}",
                        message=msg,
                        target_chat_id=chat_id,
                        target_user_id=user_id,
                        metadata={"job_id": job_id, "rule": rule.name},
                        created_at=time.time(),
                    )
                )
        return notifications

    def check_retries(
        self,
        *,
        retry_count: int,
        job_id: str = "",
        chat_id: str = "",
        user_id: str = "",
    ) -> list[PushNotification]:
        """Check if retry count triggers any rules."""
        notifications: list[PushNotification] = []
        for rule in self.rules:
            if rule.max_retries <= 0:
                continue
            if retry_count >= rule.max_retries:
                msg = rule.message_template.format(retries=retry_count)
                notifications.append(
                    PushNotification(
                        reason=PushReason.RETRY_LIMIT,
                        level=rule.level,
                        title=f"🔄 {rule.name}",
                        message=msg,
                        target_chat_id=chat_id,
                        target_user_id=user_id,
                        metadata={"job_id": job_id, "rule": rule.name, "retries": retry_count},
                        created_at=time.time(),
                    )
                )
        return notifications


# ---------------------------------------------------------------------------
# Notification builders
# ---------------------------------------------------------------------------

def build_status_change_notification(
    *,
    old_status: str,
    new_status: str,
    job_id: str = "",
    chat_id: str = "",
    summary: str = "",
) -> PushNotification:
    """Build a notification for an analysis status change."""
    STATUS_EMOJI = {
        "queued": "⏳",
        "downloading": "⬇️",
        "analyzing": "🔍",
        "completed": "✅",
        "failed": "❌",
    }
    emoji = STATUS_EMOJI.get(new_status, "📋")
    return PushNotification(
        reason=PushReason.STATUS_CHANGE,
        level=EscalationLevel.INFO,
        title=f"{emoji} 状态变更: {old_status} → {new_status}",
        message=summary or f"分析任务 {job_id} 状态已更新。",
        target_chat_id=chat_id,
        metadata={"job_id": job_id, "old_status": old_status, "new_status": new_status},
        created_at=time.time(),
    )


def build_missing_material_notification(
    *,
    missing_fields: list[str],
    chat_id: str = "",
    user_id: str = "",
    job_id: str = "",
) -> PushNotification:
    """Build a notification prompting user to supply missing info."""
    fields_str = "、".join(missing_fields)
    return PushNotification(
        reason=PushReason.MISSING_MATERIAL,
        level=EscalationLevel.WARNING,
        title="📝 请补充分析材料",
        message=f"以下信息缺失，可能影响分析准确性：{fields_str}。请在群内补充。",
        target_chat_id=chat_id,
        target_user_id=user_id,
        metadata={"job_id": job_id, "missing_fields": missing_fields},
        created_at=time.time(),
    )


def build_report_ready_notification(
    *,
    report_url: str,
    summary: str = "",
    chat_id: str = "",
    job_id: str = "",
) -> PushNotification:
    """Build a notification that a report is ready."""
    return PushNotification(
        reason=PushReason.REPORT_READY,
        level=EscalationLevel.INFO,
        title="📄 分析报告已生成",
        message=summary or f"报告链接：{report_url}",
        target_chat_id=chat_id,
        metadata={"job_id": job_id, "report_url": report_url},
        created_at=time.time(),
    )


def build_manual_escalation_notification(
    *,
    reason: str = "",
    chat_id: str = "",
    user_id: str = "",
    job_id: str = "",
) -> PushNotification:
    """Build a manual escalation notification (user clicked 'escalate')."""
    return PushNotification(
        reason=PushReason.MANUAL_ESCALATION,
        level=EscalationLevel.CRITICAL,
        title="🆘 人工升级请求",
        message=reason or "用户请求人工介入处理。",
        target_chat_id=chat_id,
        target_user_id=user_id,
        metadata={"job_id": job_id},
        created_at=time.time(),
    )


# ---------------------------------------------------------------------------
# Notification history (in-memory ring buffer)
# ---------------------------------------------------------------------------

class NotificationHistory:
    """Track recently sent notifications to avoid duplicates."""

    def __init__(self, *, max_size: int = 200, dedup_window_seconds: float = 60.0) -> None:
        self.max_size = max(10, max_size)
        self.dedup_window_seconds = dedup_window_seconds
        self._history: list[PushNotification] = []

    def should_send(self, notification: PushNotification) -> bool:
        """Check if a similar notification was sent recently."""
        now = time.time()
        key = self._dedup_key(notification)
        for past in reversed(self._history):
            if now - past.created_at > self.dedup_window_seconds:
                break
            if self._dedup_key(past) == key:
                return False
        return True

    def record(self, notification: PushNotification) -> None:
        self._history.append(notification)
        if len(self._history) > self.max_size:
            self._history = self._history[-self.max_size:]

    @property
    def count(self) -> int:
        return len(self._history)

    def recent(self, limit: int = 20) -> list[PushNotification]:
        return list(reversed(self._history[-limit:]))

    def _dedup_key(self, n: PushNotification) -> str:
        job_id = n.metadata.get("job_id", "")
        return f"{n.reason.value}:{n.target_chat_id}:{job_id}:{n.level.value}"
