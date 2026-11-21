"""Metadata-only task activity views for the local MAX desktop.

These descriptions are derived solely from authenticated Core task state and
signed journal metadata. They never claim model, coding or tool execution.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

_KINDS: dict[str, tuple[str, str]] = {
    "task.queued": ("Task queued", "Core saved the objective; no work started"),
    "task.running": ("Worker claimed task", "A lease was issued, not proof of coding"),
    "task.checkpointed": ("Checkpoint recorded", "State metadata only; not a tool transcript"),
    "task.awaiting_verification": (
        "Result awaiting verification",
        "A worker submitted an unverified digest",
    ),
    "task.verified": ("Verification recorded", "Trust accepted independent evidence"),
    "task.cancel_requested": ("Cancellation requested", "Stop has not been confirmed"),
    "task.cancelled": ("Task cancelled", "Core recorded cancellation"),
    "task.needs_reconciliation": (
        "Manual recovery needed",
        "Uncertain worker outcome; no automatic replay",
    ),
}
_STATES: dict[str, tuple[str, str]] = {
    "queued": ("QUEUED", "Waiting for authorized execution"),
    "running": ("RUNNING", "Worker lease active; activity is not proof of coding"),
    "cancelling": ("STOP REQUESTED", "Cancellation has not been acknowledged"),
    "cancelled": ("CANCELLED", "Core recorded cancellation"),
    "awaiting_verification": ("VERIFYING", "Worker result is not independently verified yet"),
    "completed": ("VERIFIED", "Core completed the task following verification"),
    "needs_reconciliation": ("REVIEW NEEDED", "Uncertain outcome requires owner review"),
}


@dataclass(frozen=True, slots=True)
class ActivityRow:
    heading: str
    explanation: str
    revision: int


@dataclass(frozen=True, slots=True)
class TaskActivity:
    headline: str
    description: str
    rows: tuple[ActivityRow, ...]


def task_activity(state: str, events: Sequence[Mapping[str, object]]) -> TaskActivity:
    """Summarize bounded journal data, without raw objective/tool/event payloads."""
    if not isinstance(state, str) or len(state) > 60:
        raise ValueError("invalid_task_state")
    if not isinstance(events, (tuple, list)) or len(events) > 20:
        raise ValueError("invalid_activity_page")
    headline, description = _STATES.get(state, ("UNKNOWN", "Refresh authoritative Core state"))
    rows: list[ActivityRow] = []
    for event in events:
        if not isinstance(event, Mapping):
            raise ValueError("invalid_activity_event")
        kind = event.get("kind")
        revision = event.get("task_revision")
        if not isinstance(kind, str) or type(revision) is not int or not 1 <= revision <= 1_000_000:
            raise ValueError("invalid_activity_event")
        title, detail = _KINDS.get(
            kind, ("Core event recorded", "Unknown event kind; inspect verified source")
        )
        rows.append(ActivityRow(title, detail, revision))
    return TaskActivity(headline, description, tuple(rows))
