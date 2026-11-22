"""Bounded proactive triggers: cron matcher, deterministic observations, promote/dismiss."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from max_core.api import create_app
from max_core.config import Settings
from max_core.models import TriggerRule, TriggerRun
from max_core.planner import PlanService
from max_core.store import TaskStore
from max_core.triggers import (
    TriggerError,
    TriggerLoop,
    TriggerService,
    cron_matches,
    minute_key,
    parse_cron,
)
from max_core.trust import ActorContext, RiskTier, ToolPolicy, TrustError, TrustService
from tests.conftest import TEST_TOKEN

MOMENT = datetime(2026, 2, 9, 9, 30, 0, tzinfo=UTC)  # a Monday


def _service(
    store: TaskStore,
    *,
    trust: TrustService | None = None,
    notes_dir: Path | None = None,
    workspace_root: Path | None = None,
) -> TriggerService:
    return TriggerService(
        store.engine,
        trust,
        notes_dir=notes_dir,
        workspace_root=workspace_root,
    )


def test_parse_cron_accepts_bounded_field_matrix() -> None:
    assert parse_cron("*/5 * * * *") == (
        tuple(range(0, 60, 5)),
        tuple(range(0, 24)),
        tuple(range(1, 32)),
        tuple(range(1, 13)),
        tuple(range(0, 7)),
    )
    fields = parse_cron("15,45 8-18/2 1,15 * 1-5")
    assert 15 in fields[0] and 45 in fields[0]
    assert fields[1] == (8, 10, 12, 14, 16, 18)
    assert fields[2] == (1, 15)
    assert fields[4] == (1, 2, 3, 4, 5)


@pytest.mark.parametrize(
    "spec",
    [
        "61 * * * *",
        "* 24 * * *",
        "* * 0 * *",
        "* * 32 * *",
        "* * * 0 *",
        "* * * 13 *",
        "* * * * 7",
        "a * * * *",
        "-1 * * * *",
        "*/0 * * * *",
        "1-5-7 * * * *",
        "* * * *",
        "* * * * * *",
        "",
        "* * * * * extra",
    ],
)
def test_parse_cron_rejects_invalid_specs(spec: str) -> None:
    with pytest.raises(TriggerError, match="invalid_trigger_schedule"):
        parse_cron(spec)


def test_cron_matches_uses_monday_zero_weekday() -> None:
    assert cron_matches("30 9 * * 1", MOMENT) is True
    assert cron_matches("30 9 * * 0", MOMENT) is False
    assert cron_matches("*/15 * * * *", MOMENT) is True
    assert cron_matches("*/15 * * * *", MOMENT.replace(minute=31)) is False
    naive = datetime(2026, 2, 9, 9, 30)
    assert cron_matches("30 9 * * *", naive) is True


def test_minute_key_is_bounded_and_stable() -> None:
    assert minute_key(MOMENT) == "2026-02-09T09:30"
    assert minute_key(MOMENT.replace(second=59)) == "2026-02-09T09:30"


def test_add_rule_validates_owner_kind_and_schedule(store: TaskStore) -> None:
    service = _service(store)
    rule = service.add_rule("owner", "notes.digest", "0 9 * * *")
    assert rule.enabled is True
    assert rule.schedule == "0 9 * * *"
    with pytest.raises(TriggerError, match="invalid_trigger_owner"):
        service.add_rule("bad owner!", "notes.digest", "0 9 * * *")
    with pytest.raises(TriggerError, match="unknown_trigger_kind"):
        service.add_rule("owner", "shell.run", "0 9 * * *")
    with pytest.raises(TriggerError, match="invalid_trigger_schedule"):
        service.add_rule("owner", "notes.digest", "not a cron")


def test_duplicate_kind_rejected(store: TaskStore) -> None:
    service = _service(store)
    service.add_rule("owner", "briefing.daily", "0 9 * * *")
    with pytest.raises(TriggerError, match="duplicate_trigger_kind"):
        service.add_rule("owner", "briefing.daily", "30 9 * * *")
    assert len(service.list_rules("owner")) == 1


def test_rule_cap_counts_only_owner_rows(store: TaskStore) -> None:
    service = _service(store)
    sessions = sessionmaker(bind=store.engine, expire_on_commit=False)
    with sessions.begin() as session:
        for index in range(10):
            session.add(
                TriggerRule(
                    id=str(uuid4()),
                    owner_id="owner",
                    kind=f"synthetic.{index}",
                    schedule="* * * * *",
                )
            )
    with pytest.raises(TriggerError, match="too_many_trigger_rules"):
        service.add_rule("owner", "briefing.daily", "0 9 * * *")
    service.add_rule("someone-else", "briefing.daily", "0 9 * * *")


def test_remove_rule_and_unknown_rule(store: TaskStore) -> None:
    service = _service(store)
    rule = service.add_rule("owner", "notes.digest", "0 9 * * *")
    service.remove_rule("owner", rule.id)
    assert service.list_rules("owner") == ()
    with pytest.raises(TriggerError, match="unknown_trigger_rule"):
        service.remove_rule("owner", rule.id)
    with pytest.raises(TriggerError, match="unknown_trigger_rule"):
        service.remove_rule("attacker", rule.id)


def test_tick_fires_once_per_minute_and_dedupes(store: TaskStore) -> None:
    service = _service(store)
    service.add_rule("owner", "briefing.daily", "30 9 * * *")
    assert service.tick("owner", MOMENT.replace(minute=29)) == 0
    assert service.tick("owner", MOMENT) == 1
    assert service.tick("owner", MOMENT) == 0
    assert service.tick("owner", MOMENT.replace(second=59)) == 0
    runs = service.runs("owner")
    assert len(runs) == 1
    assert runs[0].state == "pending_review"
    assert runs[0].kind == "briefing.daily"
    assert runs[0].approval_id is None
    assert service.tick("owner", MOMENT.replace(hour=10)) == 0


def test_tick_respects_pending_run_cap(store: TaskStore) -> None:
    service = _service(store)
    rule = service.add_rule("owner", "briefing.daily", "* * * * *")
    sessions = sessionmaker(bind=store.engine, expire_on_commit=False)
    with sessions.begin() as session:
        for _ in range(20):
            session.add(
                TriggerRun(
                    id=str(uuid4()),
                    owner_id="owner",
                    rule_id=rule.id,
                    kind="briefing.daily",
                    state="pending_review",
                    output="backlog",
                )
            )
    assert service.tick("owner", MOMENT) == 0
    with sessions.begin() as session:
        oldest = session.scalar(
            select(TriggerRun).order_by(TriggerRun.created_at.asc(), TriggerRun.id.asc()).limit(1)
        )
        assert oldest is not None
        oldest.state = "dismissed"
    assert service.tick("owner", MOMENT) == 1


def test_disabled_rules_never_fire(store: TaskStore) -> None:
    service = _service(store)
    rule = service.add_rule("owner", "briefing.daily", "* * * * *")
    sessions = sessionmaker(bind=store.engine, expire_on_commit=False)
    with sessions.begin() as session:
        record = session.get(TriggerRule, rule.id)
        assert record is not None
        record.enabled = False
    assert service.tick("owner", MOMENT) == 0


def test_briefing_observation_counts_states_and_bounds_proposal(store: TaskStore) -> None:
    service = _service(store)
    store.create("owner", "Active one", "brief-active-1")
    store.create("owner", "Active two", "brief-active-2")
    store.create("owner", "Done one", "brief-done-1")
    planner = PlanService(store.engine, store)
    parent, steps, _ = planner.create_plan("owner", "step one; step two", "brief-plan-01")
    action = service._briefing("owner")
    assert "queued=6" in action.output
    assert action.proposal_json is not None
    assert len(action.proposal_json) <= 1000
    assert "task.create" in action.proposal_json
    assert steps[0].task_id != parent.id


def test_notes_digest_lists_recent_titles_without_leaking_content(
    store: TaskStore, tmp_path: Path
) -> None:
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "standup.txt").write_text("secret details that must not appear", encoding="utf-8")
    (notes / "ideas.md").write_text("ignored non-txt", encoding="utf-8")
    service = _service(store, notes_dir=notes)
    action = service._notes_digest()
    assert action.output.startswith("1 bounded note(s); latest: standup.")
    assert "secret details" not in action.output
    assert action.proposal_json is not None


def test_notes_digest_without_directory_fails_closed(store: TaskStore) -> None:
    action = _service(store, notes_dir=None)._notes_digest()
    assert action.output == "Notes directory unavailable; nothing summarized."
    assert action.proposal_json is None


def test_repo_status_report_non_git_workspace(store: TaskStore, tmp_path: Path) -> None:
    action = _service(store, workspace_root=tmp_path)._repo_status()
    assert "Repository status unavailable" in action.output
    assert action.proposal_json is None


def test_repo_status_report_clean_tree(store: TaskStore, tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    subprocess.run(
        ["git", "-C", str(workspace), "config", "user.email", "t@example.com"], check=True
    )
    subprocess.run(["git", "-C", str(workspace), "config", "user.name", "t"], check=True)
    action = _service(store, workspace_root=workspace)._repo_status()
    assert action.output == "git status: clean working tree."
    assert action.proposal_json is None


def _task_create_trust(store: TaskStore) -> TrustService:
    def _validate(_context: ActorContext, args: dict[str, Any]) -> dict[str, Any]:
        if set(args) != {"objective"} or not isinstance(args["objective"], str):
            raise TrustError("invalid_action_parameters")
        return {"objective": args["objective"]}

    return TrustService(
        store.engine,
        registry=(ToolPolicy(name="task.create", risk=RiskTier.R1, validate_arguments=_validate),),
    )


def test_promote_run_queues_trust_inbox_grant_end_to_end(store: TaskStore) -> None:
    trust = _task_create_trust(store)
    service = _service(store, trust=trust)
    service.add_rule("owner", "briefing.daily", "30 9 * * *")
    assert service.tick("owner", MOMENT) == 1
    run = service.runs("owner")[0]

    promoted = service.promote_run("owner", run.id)
    assert promoted.state == "promoted"
    assert promoted.approval_id is not None
    pending = trust.list_pending("owner")
    assert [grant.id for grant in pending] == [promoted.approval_id]

    grant = trust.get_grant("owner", promoted.approval_id or "")
    assert grant.tool_name == "task.create"
    assert grant.state == "pending"

    decision = trust.decide("owner", grant.id, True)
    assert decision.state == "approved"
    with pytest.raises(TriggerError, match="run_not_promotable"):
        service.promote_run("owner", run.id)


def test_promote_run_is_idempotent_replay_for_pending_grant(store: TaskStore) -> None:
    trust = _task_create_trust(store)
    service = _service(store, trust=trust)
    service.add_rule("owner", "briefing.daily", "30 9 * * *")
    service.tick("owner", MOMENT)
    run = service.runs("owner")[0]
    first = service.promote_run("owner", run.id)
    second = service.promote_run("owner", run.id)
    assert second.approval_id == first.approval_id
    assert len(trust.list_pending("owner")) == 1


def test_promote_run_without_trust_fails_closed(store: TaskStore) -> None:
    service = _service(store, trust=None)
    service.add_rule("owner", "briefing.daily", "30 9 * * *")
    service.tick("owner", MOMENT)
    run = service.runs("owner")[0]
    assert run.state == "pending_review"
    with pytest.raises(TriggerError, match="trust_unavailable"):
        service.promote_run("owner", run.id)
    assert run.approval_id is None


def test_promote_unknown_or_unpromotable_run(store: TaskStore) -> None:
    service = _service(store, trust=_task_create_trust(store))
    service.add_rule("owner", "notes.digest", "30 9 * * *")
    service.tick("owner", MOMENT)
    with pytest.raises(TriggerError, match="unknown_trigger_run"):
        service.promote_run("owner", str(uuid4()))
    with pytest.raises(TriggerError, match="unknown_trigger_run"):
        service.promote_run("attacker", service.runs("owner")[0].id)
    service2 = _service(store, trust=_task_create_trust(store))
    action = service2._notes_digest()
    sessions = sessionmaker(bind=store.engine, expire_on_commit=False)
    with sessions.begin() as session:
        record = TriggerRun(
            id=str(uuid4()),
            owner_id="owner",
            rule_id=str(uuid4()),
            kind="notes.digest",
            state="pending_review",
            output=action.output,
        )
        session.add(record)
        run_id = record.id
    with pytest.raises(TriggerError, match="run_not_promotable"):
        service2.promote_run("owner", run_id)


def test_dismiss_run_states(store: TaskStore) -> None:
    service = _service(store, trust=_task_create_trust(store))
    service.add_rule("owner", "briefing.daily", "30 9 * * *")
    service.tick("owner", MOMENT)
    run = service.runs("owner")[0]
    assert service.dismiss_run("owner", run.id).state == "dismissed"
    with pytest.raises(TriggerError, match="run_not_pending"):
        service.dismiss_run("owner", run.id)
    with pytest.raises(TriggerError, match="run_not_promotable"):
        service.promote_run("owner", run.id)
    with pytest.raises(TriggerError, match="unknown_trigger_run"):
        service.dismiss_run("owner", "missing")


def test_runs_limit_validation(store: TaskStore) -> None:
    service = _service(store)
    with pytest.raises(TriggerError, match="invalid_run_limit"):
        service.runs("owner", 0)
    with pytest.raises(TriggerError, match="invalid_run_limit"):
        service.runs("owner", 51)


def test_trigger_loop_interval_validation(store: TaskStore) -> None:
    service = _service(store)
    with pytest.raises(TriggerError, match="invalid_loop_interval"):
        TriggerLoop(service, interval_seconds=14)
    with pytest.raises(TriggerError, match="invalid_loop_interval"):
        TriggerLoop(service, interval_seconds=3601)
    loop = TriggerLoop(service, interval_seconds=15)
    loop.start()
    try:
        assert loop._thread is not None and loop._thread.is_alive()
    finally:
        loop.stop()
    assert loop._thread is None


AUTH = {"Authorization": f"Bearer {TEST_TOKEN}"}


def _api(store: TaskStore, *, notes_dir: Path | None = None) -> TestClient:
    client = TestClient(
        create_app(Settings(bearer_token=TEST_TOKEN), store=store), base_url="http://localhost"
    )
    if notes_dir is not None:
        client.app.state.trigger_service._notes_dir = notes_dir  # type: ignore[union-attr]
    return client


def test_trigger_api_requires_persistence() -> None:
    client = TestClient(create_app(Settings(bearer_token=TEST_TOKEN)), base_url="http://localhost")
    assert client.get("/v1/triggers", headers=AUTH).status_code == 503
    payload = {"kind": "notes.digest", "schedule": "0 9 * * *"}
    assert client.post("/v1/triggers", headers=AUTH, json=payload).status_code == 503


def test_trigger_api_crud_and_runs(store: TaskStore, tmp_path: Path) -> None:
    client = _api(store, notes_dir=tmp_path)
    made = client.post(
        "/v1/triggers", headers=AUTH, json={"kind": "notes.digest", "schedule": "30 9 * * *"}
    )
    assert made.status_code == 201
    rule = made.json()
    assert rule["kind"] == "notes.digest"
    assert rule["enabled"] is True

    listed_rules = client.get("/v1/triggers", headers=AUTH).json()
    assert [item["id"] for item in listed_rules] == [rule["id"]]

    duplicate = client.post(
        "/v1/triggers", headers=AUTH, json={"kind": "notes.digest", "schedule": "0 10 * * *"}
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == "duplicate_trigger_kind"

    invalid = client.post(
        "/v1/triggers", headers=AUTH, json={"kind": "notes.digest", "schedule": "oopsy"}
    )
    assert invalid.status_code == 422

    assert client.app.state.trigger_service.tick("owner", MOMENT) == 1  # type: ignore[union-attr]

    runs = client.get("/v1/triggers/runs", headers=AUTH).json()
    assert len(runs) == 1
    assert runs[0]["state"] == "pending_review"
    assert client.get("/v1/triggers/runs", headers=AUTH, params={"limit": 1}).status_code == 200

    dismissed = client.post(f"/v1/triggers/runs/{runs[0]['id']}/dismiss", headers=AUTH)
    assert dismissed.status_code == 200
    assert dismissed.json()["state"] == "dismissed"
    assert (
        client.post(f"/v1/triggers/runs/{runs[0]['id']}/dismiss", headers=AUTH).status_code == 409
    )

    removed = client.delete(f"/v1/triggers/{rule['id']}", headers=AUTH)
    assert removed.status_code == 204
    assert client.delete(f"/v1/triggers/{rule['id']}", headers=AUTH).status_code == 404


def test_trigger_api_promote_flows_through_trust_inbox(store: TaskStore) -> None:
    client = _api(store)
    made = client.post(
        "/v1/triggers", headers=AUTH, json={"kind": "briefing.daily", "schedule": "30 9 * * *"}
    )
    assert made.status_code == 201
    assert client.app.state.trigger_service.tick("owner", MOMENT) == 1  # type: ignore[union-attr]
    runs = client.get("/v1/triggers/runs", headers=AUTH).json()

    promoted = client.post(f"/v1/triggers/runs/{runs[0]['id']}/promote", headers=AUTH)
    assert promoted.status_code == 200
    assert promoted.json()["state"] == "promoted"
    assert promoted.json()["approval_id"] is not None

    pending = client.get("/v1/approvals", headers=AUTH).json()
    assert [item["id"] for item in pending] == [promoted.json()["approval_id"]]

    decision = client.post(
        f"/v1/approvals/{promoted.json()['approval_id']}/decision",
        headers=AUTH,
        json={"approve": True},
    )
    assert decision.status_code == 200
    assert decision.json()["state"] == "approved"


def test_trigger_api_promote_run_without_proposal_409(store: TaskStore) -> None:
    client = _api(store)
    made = client.post(
        "/v1/triggers", headers=AUTH, json={"kind": "notes.digest", "schedule": "30 9 * * *"}
    )
    assert made.status_code == 201
    client.app.state.trigger_service._notes_dir = None  # type: ignore[union-attr]
    assert client.app.state.trigger_service.tick("owner", MOMENT) == 1  # type: ignore[union-attr]
    runs = client.get("/v1/triggers/runs", headers=AUTH).json()
    assert runs[0]["proposal_json"] is None
    promoted = client.post(f"/v1/triggers/runs/{runs[0]['id']}/promote", headers=AUTH)
    assert promoted.status_code == 409
    assert promoted.json()["detail"] == "run_not_promotable"
