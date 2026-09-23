"""referral_nurse_roster 领域资料的基础结构。"""

from __future__ import annotations

EVENT_KINDS = ['SHIFT_OPENED', 'REFERRAL_ARRIVAL_UPDATED', 'WORK_ASSIGNED', 'EMERGENCY_PREEMPTED', 'HANDOVER_ACCEPTED']
REQUIRED_FIELDS = ("event_id", "kind", "occurred_at", "subject_id", "payload")

def validate_event(record: dict) -> list[str]:
    """检查样例事件是否具备可交换的最小字段。"""
    problems = [name for name in REQUIRED_FIELDS if name not in record]
    if record.get("kind") not in EVENT_KINDS:
        problems.append("kind")
    return problems
