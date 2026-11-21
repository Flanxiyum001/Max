"""Activity trace shows only real, bounded Core journal metadata."""

from __future__ import annotations

import pytest

from max_core.activity_view import task_activity
from max_core.native_desktop.backend import NativeDesktopCore, NativeDesktopError
from max_core.store import TaskStore


def test_queued_trace_is_not_reported_as_coding_or_tool_execution(store: TaskStore) -> None:
    backend = NativeDesktopCore(store=store)
    try:
        task = backend.create_task("Please build a new coding feature")
        activity = backend.activity(task["id"])
        assert activity.headline == "QUEUED"
        assert "Waiting for authorized execution" in activity.description
        assert activity.rows[0].heading == "Task queued"
        assert "no work started" in activity.rows[0].explanation
        assert "coding feature" not in repr(activity)
    finally:
        backend.close()


def test_activity_uses_only_known_metadata_labels_never_raw_payloads() -> None:
    malicious = "PRIVATE_ARGS_AND_SECRET"
    raw_events = (
        {"kind": "task.running", "task_revision": 2, "payload": malicious},
        {"kind": "model.executed " + malicious, "task_revision": 3},
        {"kind": "task.awaiting_verification", "task_revision": 4},
    )
    activity = task_activity("awaiting_verification", raw_events)
    assert activity.headline == "VERIFYING"
    assert "not independently verified" in activity.description
    assert activity.rows[0].heading == "Worker claimed task"
    assert activity.rows[1].heading == "Core event recorded"
    assert "worker submitted" in activity.rows[2].explanation.lower()
    assert malicious not in repr(activity)


def test_unknown_state_never_claims_execution() -> None:
    activity = task_activity("totally_unspecified", ())
    assert activity.headline == "UNKNOWN"
    assert not activity.rows


@pytest.mark.parametrize(
    "state,events",
    [
        ("queued", ({"kind": "task.queued", "task_revision": True},)),
        ("queued", ({"kind": 9, "task_revision": 1},)),
        ("queued", ({"kind": "task.running", "task_revision": -1},)),
        ("queued", tuple({"kind": "task.queued", "task_revision": i} for i in range(21))),
        (42, ()),
    ],
)
def test_malformed_events_fail_closed(state: object, events: object) -> None:
    with pytest.raises(ValueError):
        task_activity(state, events)  # type: ignore[arg-type]


def test_activity_rejects_invalid_identifier_without_leaking_user_input(
    store: TaskStore,
) -> None:
    backend = NativeDesktopCore(store=store)
    try:
        with pytest.raises(NativeDesktopError, match="invalid_task_identifier"):
            backend.activity("something/secret")
    finally:
        backend.close()
