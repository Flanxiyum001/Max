"""MAX Trust negative tests. No external tool, model provider or credential is used."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from max_core.api import create_app
from max_core.config import Settings
from max_core.gateway.contracts import ToolProposal
from max_core.models import ApprovalGrant, Base, TrustAudit, utc_now
from max_core.store import StoreError, TaskStore
from max_core.trust import (
    ActorContext,
    RiskTier,
    ToolPolicy,
    ToolPolicyRegistry,
    TrustError,
    TrustService,
)
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
    store: TaskStore,
    change: str,
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
        service.execute(
            scope,
            grant.id,
            ToolProposal("call-001", "task.status", {"task_id": task.id, "admin": True}),
        )
    with pytest.raises(TrustError, match="approval_binding_mismatch"):
        service.execute(
            scope, grant.id, ToolProposal("changed-call", "task.status", {"task_id": task.id})
        )
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
            select(TrustAudit)
            .where(TrustAudit.task_id == task.id)
            .order_by(TrustAudit.created_at.desc())
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
    assert (
        client.post(f"/v1/approvals/{grant.id}/decision", json={"approve": True}).status_code == 401
    )
    auth = {"Authorization": f"Bearer {TEST_TOKEN}"}
    pending = client.get("/v1/approvals", headers=auth)
    assert pending.status_code == 200
    assert pending.json()[0]["id"] == grant.id
    assert pending.json()[0]["device_id"] == "device-1"
    assert (
        client.post(
            f"/v1/approvals/{grant.id}/decision", headers=auth, json={"approve": "yes"}
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"/v1/approvals/{grant.id}/decision",
            headers=auth,
            json={"approve": True, "role": "admin"},
        ).status_code
        == 422
    )
    response = client.post(
        f"/v1/approvals/{grant.id}/decision", headers=auth, json={"approve": True}
    )
    assert response.status_code == 200
    assert response.json()["state"] == "approved"
    assert client.get("/v1/approvals", headers=auth).json() == []
    assert (
        client.post(
            f"/v1/approvals/{grant.id}/decision", headers=auth, json={"approve": True}
        ).status_code
        == 409
    )


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
        service.execute(
            scope, grant.id, ToolProposal("call-001", "shell.run", {"task_id": task.id})
        )
    assert service.execute(scope, grant.id, proposal(task.id)).task_id == task.id


def _m3_test_registry() -> ToolPolicyRegistry:
    def _no_args(_ctx: ActorContext, args: dict[str, Any]) -> dict[str, Any]:
        if args:
            raise ValueError("unexpected_args")
        return {}

    def _task_create(_ctx: ActorContext, args: dict[str, Any]) -> dict[str, Any]:
        if set(args) != {"objective"} or not isinstance(args["objective"], str):
            raise ValueError("bad_objective")
        cleaned = args["objective"].strip()
        if not 1 <= len(cleaned) <= 500:
            raise ValueError("bad_objective")
        return {"objective": cleaned}

    def _notes_save(_ctx: ActorContext, args: dict[str, Any]) -> dict[str, Any]:
        if set(args) != {"title", "content"}:
            raise ValueError("bad_note")
        if not isinstance(args["title"], str) or not isinstance(args["content"], str):
            raise ValueError("bad_note")
        if not args["title"].strip() or not args["content"].strip():
            raise ValueError("bad_note")
        return {"title": args["title"].strip(), "content": args["content"]}

    return ToolPolicyRegistry(
        (
            ToolPolicy("dev.health", RiskTier.R0, _no_args),
            ToolPolicy("task.create", RiskTier.R1, _task_create),
            ToolPolicy("notes.save", RiskTier.R2, _notes_save),
        )
    )


def _sqlite_immediate_engine(db_path: Path):
    engine = create_engine(
        f"sqlite+pysqlite:///{db_path.as_posix()}",
        connect_args={"check_same_thread": False, "timeout": 5.0},
    )

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn: Any, _rec: Any) -> None:
        dbapi_conn.isolation_level = None
        cur = dbapi_conn.cursor()
        try:
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=5000")
        finally:
            cur.close()

    @event.listens_for(engine, "begin")
    def _on_begin(conn: Any) -> None:
        conn.exec_driver_sql("BEGIN IMMEDIATE")

    Base.metadata.create_all(engine)
    return engine


def test_tool_policy_registry_validation_and_empty_fail_closed(store: TaskStore) -> None:
    with pytest.raises(TrustError, match="invalid_tool_registry"):
        ToolPolicy("bad name", RiskTier.R0, lambda _c, a: a)
    with pytest.raises(TrustError, match="invalid_tool_registry"):
        ToolPolicy("dev.health", "r9_unknown", lambda _c, a: a)  # type: ignore[arg-type]
    with pytest.raises(TrustError, match="invalid_tool_registry"):
        ToolPolicy("dev.health", RiskTier.R0, "not_callable")  # type: ignore[arg-type]

    p1 = ToolPolicy("dev.health", RiskTier.R0, lambda _c, a: a)
    with pytest.raises(TrustError, match="invalid_tool_registry"):
        ToolPolicyRegistry((p1, p1))
    with pytest.raises(TrustError, match="invalid_tool_registry"):
        ToolPolicyRegistry(("not-a-policy",))  # type: ignore[arg-type]

    reg = _m3_test_registry()
    assert len(reg) == 3
    assert "dev.health" in reg
    assert "shell.run" not in reg
    assert reg.names() == frozenset({"dev.health", "task.create", "notes.save"})
    assert len(reg.policies()) == 3

    task, _ = store.create("owner", "Empty registry", "trust-empty-reg-001")
    empty_svc = TrustService(store.engine, registry=ToolPolicyRegistry(()))
    with pytest.raises(TrustError, match="capability_denied"):
        empty_svc.request(context(task.id), proposal(task.id))

    r3_reg = ToolPolicyRegistry((ToolPolicy("danger.op", RiskTier.R3, lambda _c, a: a),))
    r3_svc = TrustService(store.engine, registry=r3_reg)
    with pytest.raises(TrustError, match="capability_denied"):
        r3_svc.request(context(task.id), ToolProposal("c1", "danger.op", {}))
    with pytest.raises(TrustError, match="capability_denied"):
        r3_svc.execute_r0(context(task.id), ToolProposal("c2", "danger.op", {}), lambda _a: "x")


def test_execute_r0_auto_audits_without_grant_and_commits_before_handler(
    tmp_path: Path,
) -> None:
    engine = _sqlite_immediate_engine(tmp_path / "r0_immediate.sqlite3")
    try:
        store = TaskStore(engine)
        task, _ = store.create("owner", "R0 test", "trust-r0-001")
        svc = TrustService(engine, registry=_m3_test_registry())
        scope = context(task.id)

        def _handler(_args: dict[str, Any]) -> dict[str, str]:
            # Proves TrustService released SQLite BEGIN IMMEDIATE lock before invoking handler.
            nested, _ = store.create("owner", "Created inside R0 handler", "nested-r0-task-01")
            return {"status": "ok", "nested_id": nested.id}

        res = svc.execute_r0(scope, ToolProposal("r0-call-1", "dev.health", {}), _handler)
        assert res["status"] == "ok"
        assert svc.list_pending("owner") == []

        with Session(engine) as session:
            grants = session.scalars(select(ApprovalGrant)).all()
            audits = session.scalars(select(TrustAudit).where(TrustAudit.task_id == task.id)).all()
            assert grants == []
            assert len(audits) == 1
            assert audits[0].action == "execute"
            assert audits[0].decision == "allow"
            assert audits[0].reason == "r0_auto_approved"
            assert audits[0].approval_id is None

        # R1 tool cannot bypass ApprovalGrant via execute_r0.
        with pytest.raises(TrustError, match="approval_required"):
            svc.execute_r0(
                scope,
                ToolProposal("r1-bypass", "task.create", {"objective": "Bypass"}),
                lambda _a: {},
            )

        # Handler failure audits execute_outcome error.
        def _boom(_args: dict[str, Any]) -> dict[str, str]:
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            svc.execute_r0(scope, ToolProposal("r0-fail", "dev.health", {}), _boom)
        with Session(engine) as session:
            err_audit = session.scalar(
                select(TrustAudit).where(
                    TrustAudit.task_id == task.id,
                    TrustAudit.action == "execute_outcome",
                )
            )
            assert err_audit is not None
            assert err_audit.decision == "error"
            assert err_audit.reason == "tool_execution_failed"
    finally:
        engine.dispose()


def test_r1_r2_lifecycle_persistence_reopen_single_use_and_handler_error(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "r1_reopen.sqlite3"
    engine1 = _sqlite_immediate_engine(db_path)
    try:
        store1 = TaskStore(engine1)
        task, _ = store1.create("owner", "Anchor", "trust-r1-reopen-001")
        svc1 = TrustService(engine1, registry=_m3_test_registry())
        scope = context(task.id)
        prop = ToolProposal("c-note-1", "notes.save", {"title": "plan", "content": "Ship M3"})
        grant = svc1.request(scope, prop)
        assert grant.tool_name == "notes.save"
        assert grant.state == "pending"
    finally:
        engine1.dispose()

    engine2 = _sqlite_immediate_engine(db_path)
    try:
        store2 = TaskStore(engine2)
        svc2 = TrustService(engine2, registry=_m3_test_registry())
        fetched = svc2.get_grant("owner", grant.id)
        assert fetched.id == grant.id
        assert fetched.state == "pending"
        assert svc2.list_pending("owner")[0].id == grant.id

        # Legacy execute() refuses non-task.status tools without consuming grant.
        decided = svc2.decide("owner", grant.id, True)
        assert decided.state == "approved"
        with pytest.raises(TrustError, match="capability_denied"):
            svc2.execute(scope, grant.id, prop)
        assert svc2.get_grant("owner", grant.id).state == "approved"

        # consume_and_execute commits grant consumption before running handler.
        def _run_note(args: dict[str, Any]) -> dict[str, str]:
            nested, _ = store2.create("owner", "Side task", "nested-r2-task-01")
            return {"title": str(args["title"]), "nested_id": nested.id}

        outcome = svc2.consume_and_execute(scope, grant.id, prop, _run_note)
        assert outcome["title"] == "plan"
        assert svc2.get_grant("owner", grant.id).state == "consumed"

        # Replay fails cleanly.
        with pytest.raises(TrustError, match="approval_not_active"):
            svc2.consume_and_execute(scope, grant.id, prop, _run_note)

        # Handler error after consumption keeps grant consumed and audits outcome error.
        prop2 = ToolProposal("c-task-2", "task.create", {"objective": "Fail handler"})
        g2 = svc2.request(scope, prop2)
        svc2.decide("owner", g2.id, True)

        def _fail(_args: dict[str, Any]) -> None:
            raise ValueError("disk_full")

        with pytest.raises(ValueError, match="disk_full"):
            svc2.consume_and_execute(scope, g2.id, prop2, _fail)
        assert svc2.get_grant("owner", g2.id).state == "consumed"
    finally:
        engine2.dispose()


def test_r1_tampering_expiry_ttl_cross_owner_and_prompt_injection(store: TaskStore) -> None:
    task, _ = store.create("owner", "Security", "trust-sec-m3-001")
    svc = TrustService(store.engine, registry=_m3_test_registry())
    scope = context(task.id)

    # TTL validation (including bool rejection)
    for bad_ttl in (14, 901, True):
        with pytest.raises(TrustError, match="invalid_approval_expiry"):
            svc.request(
                scope,
                ToolProposal("ttl-1", "task.create", {"objective": "Valid"}),
                ttl_seconds=bad_ttl,
            )

    # Prompt injection claiming owner_approved=True is rejected and secret never audited
    with pytest.raises(TrustError, match="invalid_action_parameters"):
        svc.request(
            scope,
            ToolProposal(
                "inj-args",
                "task.create",
                {
                    "objective": "Valid",
                    "owner_approved": True,
                    "secret": "sk-never-audit-this-999999",
                },
            ),
        )
    with pytest.raises(TrustError, match="capability_denied"):
        svc.request(
            scope,
            ToolProposal(
                "inj-tool",
                "os.exec",
                {"owner_approved": True, "secret": "sk-never-audit-this-999999"},
            ),
        )
    with Session(store.engine) as session:
        audits = session.scalars(select(TrustAudit).where(TrustAudit.task_id == task.id)).all()
        assert all("sk-never-audit-this-999999" not in repr(a.__dict__) for a in audits)

    # Tamper resistance on R2 grant
    prop = ToolProposal("tamper-1", "notes.save", {"title": "safe", "content": "Hello"})
    grant = svc.request(scope, prop)
    svc.decide("owner", grant.id, True)

    with pytest.raises(TrustError, match="approval_binding_mismatch"):
        svc.consume_and_execute(
            scope,
            grant.id,
            ToolProposal("tamper-1", "notes.save", {"title": "safe", "content": "TAMPERED"}),
            lambda _a: None,
        )
    with pytest.raises(TrustError, match="approval_binding_mismatch"):
        svc.consume_and_execute(
            replace(scope, session_id="other-session"),
            grant.id,
            prop,
            lambda _a: None,
        )
    assert svc.get_grant("owner", grant.id).state == "approved"

    # Cross-owner get_grant / decide / consume_and_execute
    with pytest.raises(TrustError, match="approval_not_found"):
        svc.get_grant("other_owner", grant.id)
    with pytest.raises(StoreError, match="Task not found"):
        svc.request(replace(scope, owner_id="other_owner"), prop)

    # Expiry on consume_and_execute
    with Session(store.engine) as session, session.begin():
        row = session.get(ApprovalGrant, grant.id)
        assert row is not None
        row.expires_at = utc_now() - timedelta(seconds=5)
    with pytest.raises(TrustError, match="approval_expired"):
        svc.consume_and_execute(scope, grant.id, prop, lambda _a: None)
    assert svc.get_grant("owner", grant.id).state == "expired"


def test_multibyte_canonical_args_fit_and_oversized_payload_rejected(store: TaskStore) -> None:
    task, _ = store.create("owner", "Canonical size bounds", "trust-size-bounds-001")
    svc = TrustService(store.engine, registry=_m3_test_registry())
    scope = context(task.id)

    # 32,768 UTF-8 bytes of 4-byte emojis -> 98,304 ASCII JSON bytes under ensure_ascii=True
    max_emoji_content = "\U0001f600" * 8_192
    prop_ok = ToolProposal(
        "size-ok",
        "notes.save",
        {"title": "emoji-max", "content": max_emoji_content},
    )
    grant = svc.request(scope, prop_ok)
    svc.decide("owner", grant.id, True)
    res = svc.consume_and_execute(
        scope, grant.id, prop_ok, lambda args: len(str(args["content"]).encode("utf-8"))
    )
    assert res == 32_768

    # Exceeding _MAX_CANONICAL_ARGS_BYTES (131,072 ASCII bytes) still fails closed
    too_big = ToolProposal(
        "size-too-big",
        "notes.save",
        {"title": "oversize", "content": "x" * 131_073},
    )
    with pytest.raises(TrustError, match="invalid_action_parameters"):
        svc.request(scope, too_big)
