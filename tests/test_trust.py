"""MAX Trust negative tests. No external tool, model provider or credential is used."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from max_core.api import create_app
from max_core.config import Settings
from max_core.gateway.contracts import ToolProposal
from max_core.models import ApprovalGrant, TrustAudit, utc_now
from max_core.store import TaskStore
from max_core.trust import ActorContext, TrustError, TrustService
from tests.conftest import TEST_TOKEN


def context(task_id: str, **kwargs: str) -> ActorContext:
    fields = {
        "owner_id": "owner",
        "task_id": task_id,
        "actor_id": "worker-1",
        "session_id": "session-1",
        "device_id": "device-1",
    }
    fields.update(kwargs)
    return ActorContext(**fields)


def proposal(task_id: str) -> ToolProposal:
    return ToolProposal(
        call_id="call-001",
        name="task.status",
        arguments={"task_id": task_id},
    )


def test_owner_approval_exact_match_single_use_and_no_objective(store: TaskStore) -> None:
    task, _ = store.create("owner", "Do not disclose this objective", "trust-task-001")
    service = TrustService(store.engine)
    scope = context(task.id)
    grant = service.request(scope, proposal(task.id))
    assert grant.state == "pending"
    assert grant.actor_id == "worker-1"
    assert service.list_pending("owner")[0].id == grant.id
    with pytest.raises(TrustError, match="approval_not_active"):
        service.execute(scope, grant.id, proposal(task.id))
    decision = service.decide("owner", grant.id, True)
    assert decision.state == "approved"
    assert service.list_pending("owner") == []
    result = service.execute(scope, grant.id, proposal(task.id))
    assert result.task_id == task.id
    assert result.state == "queued"
    assert "objective" not in result.__dataclass_fields__
    assert "Do not disclose" not in repr(result)
    with pytest.raises(TrustError, match="approval_not_active"):
        service.execute(scope, grant.id, proposal(task.id))
    with Session(store.engine) as session:
        audit = session.scalars(select(TrustAudit).where(TrustAudit.task_id == task.id)).all()
        assert [x.decision for x in audit].count("allow") == 1
        assert [x.decision for x in audit].count("deny") >= 2


@pytest.mark.parametrize("change", ["actor_id", "session_id", "device_id", "owner_id"])
def test_grant_cannot_cross_actor_session_device_owner(
    store: TaskStore, change: str,
) -> None:
    task, _ = store.create("owner", "Scope", "trust-scope-001")
    service = TrustService(store.engine)
    scope = context(task.id)
    grant = service.request(scope, proposal(task.id))
    service.decide("owner", grant.id, True)
    if change == "owner_id":
        with pytest.raises(TrustError, match="approval_not_found"):
            service.execute(replace(scope, owner_id="different-owner"), grant.id, proposal(task.id))
    else:
        with pytest.raises(TrustError, match="approval_binding_mismatch"):
            service.execute(replace(scope, **{change: "impersonator"}), grant.id, proposal(task.id))
    assert service.execute(scope, grant.id, proposal(task.id)).state == "queued"


def test_altered_parameters_or_proposal_id_do_not_consume_grant(store: TaskStore) -> None:
    task, _ = store.create("owner", "Scope", "trust-args-001")
    service = TrustService(store.engine)
    scope = context(task.id)
    grant = service.request(scope, proposal(task.id))
    service.decide("owner", grant.id, True)
    with pytest.raises(TrustError, match="invalid_action_parameters"):
        service.execute(scope, grant.id, ToolProposal("call-001", "task.status", {"task_id": task.id, "admin": True}))
    with pytest.raises(TrustError, match="approval_binding_mismatch"):
        service.execute(scope, grant.id, ToolProposal("changed-call", "task.status", {"task_id": task.id}))
    assert service.execute(scope, grant.id, proposal(task.id)).task_id == task.id


@pytest.mark.parametrize(
    "action",
    ["shell.run", "payment.transfer", "git.merge", "deploy.production", "vault.share"],
)
def test_high_risk_tools_denied_and_audited(store: TaskStore, action: str) -> None:
    task, _ = store.create("owner", "Sensitive", "trust-deny-" + action)
    service = TrustService(store.engine)
    with pytest.raises(TrustError, match="capability_denied"):
        service.request(context(task.id), ToolProposal("call-001", action, {"task_id": task.id}))
    with Session(store.engine) as session:
        latest = session.scalar(
            select(TrustAudit).where(TrustAudit.task_id == task.id).order_by(TrustAudit.created_at.desc())
        )
        assert latest is not None and latest.decision == "deny"
        assert latest.reason == "capability_denied"


def test_missing_grant_and_denial_replay(store: TaskStore) -> None:
    task, _ = store.create("owner", "Denied", "trust-deny-001")
    service = TrustService(store.engine)
    scope = context(task.id)
    with pytest.raises(TrustError, match="approval_not_found"):
        service.execute(scope, str(uuid4()), proposal(task.id))
    grant = service.request(scope, proposal(task.id))
    assert service.decide("owner", grant.id, False).state == "denied"
    with pytest.raises(TrustError, match="approval_not_active"):
        service.execute(scope, grant.id, proposal(task.id))
    with pytest.raises(TrustError, match="approval_not_pending"):
        service.decide("owner", grant.id, True)


def test_expiry_blocks_owner_decision_and_consumption(store: TaskStore) -> None:
    task, _ = store.create("owner", "Expirable", "trust-expiry-001")
    service = TrustService(store.engine)
    scope = context(task.id)
    grant = service.request(scope, proposal(task.id), ttl_seconds=15)
    with Session(store.engine) as session, session.begin():
        row = session.get(ApprovalGrant, grant.id)
        assert row
        row.expires_at = utc_now() - timedelta(seconds=1)
    with pytest.raises(TrustError, match="approval_expired"):
        service.decide("owner", grant.id, True)
    with Session(store.engine) as session:
        assert session.get(ApprovalGrant, grant.id).state == "expired"

    second = service.request(scope, proposal(task.id))
    service.decide("owner", second.id, True)
    with Session(store.engine) as session, session.begin():
        row = session.get(ApprovalGrant, second.id)
        assert row
        row.expires_at = utc_now() - timedelta(seconds=1)
    with pytest.raises(TrustError, match="approval_expired"):
        service.execute(scope, second.id, proposal(task.id))


def test_request_rejects_cross_owner_task(store: TaskStore) -> None:
    task, _ = store.create("owner", "Private", "trust-owner-001")
    service = TrustService(store.engine)
    with pytest.raises(Exception, match="Task not found"):
        service.request(context(task.id, owner_id="attacker"), proposal(task.id))
    assert service.list_pending("attacker") == []


def test_authenticated_owner_only_routes_and_strict_decision(store: TaskStore) -> None:
    task, _ = store.create("owner", "Review", "trust-api-001")
    service = TrustService(store.engine)
    grant = service.request(context(task.id), proposal(task.id))
    client = TestClient(
        create_app(Settings(bearer_token=TEST_TOKEN), store=store),
        base_url="http://localhost",
    )
    assert client.get("/v1/approvals").status_code == 401
    assert client.post(f"/v1/approvals/{grant.id}/decision", json={"approve": True}).status_code == 401
    auth = {"Authorization": f"Bearer {TEST_TOKEN}"}
    pending = client.get("/v1/approvals", headers=auth)
    assert pending.status_code == 200
    assert pending.json()[0]["id"] == grant.id
    assert pending.json()[0]["device_id"] == "device-1"
    assert client.post(f"/v1/approvals/{grant.id}/decision", headers=auth, json={"approve": "yes"}).status_code == 422
    assert client.post(f"/v1/approvals/{grant.id}/decision", headers=auth, json={"approve": True, "role": "admin"}).status_code == 422
    response = client.post(f"/v1/approvals/{grant.id}/decision", headers=auth, json={"approve": True})
    assert response.status_code == 200
    assert response.json()["state"] == "approved"
    assert client.get("/v1/approvals", headers=auth).json() == []
    assert client.post(f"/v1/approvals/{grant.id}/decision", headers=auth, json={"approve": True}).status_code == 409


def test_missing_db_owner_approval_route_fails_closed(client: TestClient) -> None:
    assert client.get("/v1/approvals").status_code == 401
    auth = {"Authorization": f"Bearer {TEST_TOKEN}"}
    assert client.get("/v1/approvals", headers=auth).status_code == 503


def test_invalid_actor_identity_denied() -> None:
    with pytest.raises(TrustError, match="invalid_actor_scope"):
        context(str(uuid4()), actor_id="worker with spaces")


def test_cannot_use_approved_grant_for_unknown_tool(store: TaskStore) -> None:
    task, _ = store.create("owner", "No shell", "trust-noshell-001")
    service = TrustService(store.engine)
    scope = context(task.id)
    grant = service.request(scope, proposal(task.id))
    service.decide("owner", grant.id, True)
    with pytest.raises(TrustError, match="capability_denied"):
        service.execute(scope, grant.id, ToolProposal("call-001", "shell.run", {"task_id": task.id}))
    assert service.execute(scope, grant.id, proposal(task.id)).task_id == task.id
