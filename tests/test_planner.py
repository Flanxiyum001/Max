"""Deterministic plan decomposition and readiness chains; steps never auto-execute."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from max_core.api import create_app
from max_core.config import Settings
from max_core.models import Task, utc_now
from max_core.planner import PlanError, PlanService, decompose
from max_core.store import TaskStore
from tests.conftest import TEST_TOKEN


def test_labeled_lines_are_split_and_cleaned() -> None:
    objective = "1) Check the failing test\n2) Run the whole suite\n- Review the diff"
    assert decompose(objective) == [
        "Check the failing test",
        "Run the whole suite",
        "Review the diff",
    ]


def test_labeled_lines_win_over_unlabeled_neighbors() -> None:
    objective = "Free-form preamble\n2) Run the whole suite\n- Review the diff"
    assert decompose(objective) == ["Run the whole suite", "Review the diff"]


def test_semicolon_then_and_newline_separators() -> None:
    assert decompose("write docs; ship them") == ["write docs", "ship them"]
    assert decompose("write docs then ship them") == ["write docs", "ship them"]
    assert decompose("write docs Then ship them") == ["write docs", "ship them"]
    assert decompose("write docs THEN ship them") == ["write docs", "ship them"]
    assert decompose("write docs\nship them") == ["write docs", "ship them"]


def test_labeled_lists_take_priority_over_separators() -> None:
    assert decompose("1) alpha; beta\n2) gamma") == ["alpha; beta", "gamma"]


def test_steps_are_whitespace_normalized_and_deduped_case_insensitively() -> None:
    assert decompose("  spaced   out  ;  tight ") == ["spaced out", "tight"]
    assert decompose("alpha; Alpha") == []
    assert decompose("alpha; alpha; beta") == ["alpha", "beta"]


def test_single_step_objective_makes_no_plan() -> None:
    assert decompose("just one thing") == []


def test_too_many_steps_rejected() -> None:
    objective = "\n".join(f"{index}) step {index}" for index in range(1, 10))
    with pytest.raises(PlanError, match="plan_too_many_steps"):
        decompose(objective)


def test_control_characters_and_bounds_rejected() -> None:
    with pytest.raises(PlanError, match="invalid_task_objective"):
        decompose("alpha\x07; beta")
    with pytest.raises(PlanError, match="invalid_task_objective"):
        decompose("x" * 501)
    with pytest.raises(PlanError, match="invalid_task_objective"):
        decompose(12)  # type: ignore[arg-type]
    with pytest.raises(PlanError, match="invalid_task_objective"):
        decompose("   ")


def test_oversize_step_text_is_dropped_by_cleaning() -> None:
    long_step = "word " * 45
    assert decompose(f"{long_step}; short") == []


def test_create_plan_builds_ordered_ready_chain(store: TaskStore) -> None:
    service = PlanService(store.engine, store)
    parent, steps, created = service.create_plan(
        "owner", "first step; second step; third step", "plan-chain-01"
    )
    assert created is True
    assert parent.objective == "first step; second step; third step"
    assert [step.position for step in steps] == [1, 2, 3]
    assert [step.ready for step in steps] == [True, False, False]
    for step in steps:
        assert store.get("owner", step.task_id).state == "queued"


def test_create_plan_replay_is_idempotent(store: TaskStore) -> None:
    service = PlanService(store.engine, store)
    parent, steps, created = service.create_plan("owner", "alpha; beta", "plan-replay-01")
    again_parent, again_steps, again_created = service.create_plan(
        "owner", "alpha; beta", "plan-replay-01"
    )
    assert created is True
    assert again_created is False
    assert again_parent.id == parent.id
    assert [step.task_id for step in again_steps] == [step.task_id for step in steps]


def test_unstructured_objective_creates_parent_without_steps(store: TaskStore) -> None:
    service = PlanService(store.engine, store)
    parent, steps, created = service.create_plan("owner", "one single objective", "plan-solo-01")
    assert created is True
    assert steps == ()


def test_complete_step_flips_ready_chain_and_marks_parent_view(store: TaskStore) -> None:
    service = PlanService(store.engine, store)
    _parent, steps, _ = service.create_plan("owner", "alpha; beta; gamma", "plan-complete-01")
    view = service.complete_step("owner", steps[0].task_id)
    assert [step.state for step in view.steps] == ["completed", "queued", "queued"]
    assert [step.ready for step in view.steps] == [False, True, False]
    completed = service.complete_step("owner", steps[1].task_id)
    assert [step.ready for step in completed.steps] == [False, False, True]


def test_complete_step_unknown_or_wrong_owner_rejected(store: TaskStore) -> None:
    service = PlanService(store.engine, store)
    _parent, steps, _ = service.create_plan("owner", "alpha; beta", "plan-owner-01")
    with pytest.raises(PlanError, match="task_not_found"):
        service.complete_step("attacker", steps[0].task_id)
    with pytest.raises(PlanError, match="task_not_found"):
        service.complete_step("owner", "00000000-0000-0000-0000-000000000000")


def test_complete_step_parent_id_falls_back_to_parent(store: TaskStore) -> None:
    service = PlanService(store.engine, store)
    parent, steps, _ = service.create_plan("owner", "alpha; beta", "plan-parent-01")
    view = service.complete_step("owner", parent.id)
    assert view.parent.id == parent.id
    assert [step.task_id for step in view.steps] == [step.task_id for step in steps]


def test_cancelled_step_is_not_advanceable(store: TaskStore) -> None:
    service = PlanService(store.engine, store)
    _parent, steps, _ = service.create_plan("owner", "alpha; beta", "plan-cancel-01")
    sessions = sessionmaker(bind=store.engine, expire_on_commit=False)
    with sessions.begin() as session:
        record = session.get(Task, steps[0].task_id)
        assert record is not None
        record.state = "cancelled"
        record.updated_at = utc_now()
    with pytest.raises(PlanError, match="step_not_advanceable"):
        service.complete_step("owner", steps[0].task_id)


def test_plan_view_of_unknown_task_is_404(store: TaskStore) -> None:
    service = PlanService(store.engine, store)
    with pytest.raises(PlanError, match="task_not_found"):
        service.plan_view("owner", str(uuid.uuid4()))


def test_next_ready_step_walks_the_chain(store: TaskStore) -> None:
    service = PlanService(store.engine, store)
    parent, steps, _ = service.create_plan("owner", "alpha; beta; gamma", "plan-next-01")
    ready = service.next_ready_step("owner", parent.id)
    assert ready is not None and ready.task_id == steps[0].task_id
    service.complete_step("owner", steps[0].task_id)
    service.complete_step("owner", steps[1].task_id)
    ready = service.next_ready_step("owner", parent.id)
    assert ready is not None and ready.task_id == steps[2].task_id
    service.complete_step("owner", steps[2].task_id)
    assert service.next_ready_step("owner", parent.id) is None


def test_step_key_overflow_rejected_without_leaking_steps(store: TaskStore) -> None:
    service = PlanService(store.engine, store)
    with pytest.raises(PlanError, match="invalid_task_idempotency_key"):
        service.create_plan("owner", "alpha; beta", "p" * 100)


AUTH = {"Authorization": f"Bearer {TEST_TOKEN}"}


def _api(store: TaskStore) -> TestClient:
    return TestClient(
        create_app(Settings(bearer_token=TEST_TOKEN), store=store), base_url="http://localhost"
    )


def test_plan_api_requires_persistence() -> None:
    client = TestClient(create_app(Settings(bearer_token=TEST_TOKEN)), base_url="http://localhost")
    response = client.post("/v1/tasks/plan", headers=AUTH, json={"objective": "a; b"})
    assert response.status_code == 503


def test_plan_api_create_get_and_complete_roundtrip(store: TaskStore) -> None:
    client = _api(store)
    made = client.post(
        "/v1/tasks/plan",
        headers=AUTH | {"Idempotency-Key": "plan-api-01"},
        json={"objective": "alpha step; beta step"},
    )
    assert made.status_code == 200
    body = made.json()
    assert body["task"]["objective"] == "alpha step; beta step"
    assert [step["position"] for step in body["steps"]] == [1, 2]
    assert body["steps"][0]["ready"] is True

    replay = client.get(f"/v1/tasks/{body['task']['id']}/plan", headers=AUTH)
    assert replay.status_code == 200
    assert [step["task_id"] for step in replay.json()["steps"]] == [
        step["task_id"] for step in body["steps"]
    ]

    complete = client.post(f"/v1/tasks/{body['steps'][0]['task_id']}/complete", headers=AUTH)
    assert complete.status_code == 200
    assert [step["state"] for step in complete.json()["steps"]] == ["completed", "queued"]
    assert complete.json()["steps"][1]["ready"] is True


def test_plan_api_replay_returns_same_parent(store: TaskStore) -> None:
    client = _api(store)
    headers = AUTH | {"Idempotency-Key": "plan-api-02"}
    payload = {"objective": "alpha; beta"}
    first = client.post("/v1/tasks/plan", headers=headers, json=payload)
    second = client.post("/v1/tasks/plan", headers=headers, json=payload)
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["task"]["id"] == second.json()["task"]["id"]


def test_plan_api_missing_idempotency_key_422(store: TaskStore) -> None:
    client = _api(store)
    response = client.post("/v1/tasks/plan", headers=AUTH, json={"objective": "a; b"})
    assert response.status_code == 422


def test_plan_api_unknown_task_404(store: TaskStore) -> None:
    client = _api(store)
    missing = client.get("/v1/tasks/00000000-0000-0000-0000-000000000000/plan", headers=AUTH)
    assert missing.status_code == 404
    assert missing.json()["detail"] == "task_not_found"
