"""MAX Trust: deny-by-default, transactional, single-use, local-only approvals.

Only the hardcoded task.status read is executable here. Untrusted model tool
proposals never register capabilities or obtain grants by claiming to be an owner.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from hmac import compare_digest
from uuid import UUID, uuid4

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from max_core.gateway.contracts import ToolProposal
from max_core.models import ApprovalGrant, TrustAudit, utc_now
from max_core.store import _is_expired, _locked

_ACTOR = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_TOOL = "task.status"


class TrustError(Exception):
    """Never embeds an untrusted proposal, HTTP body or token."""

    def __init__(self, code: str, status_code: int = 403) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ActorContext:
    """Trusted host supplies this, never provider text."""

    owner_id: str
    task_id: str
    actor_id: str
    session_id: str
    device_id: str

    def __post_init__(self) -> None:
        for item in (self.owner_id, self.actor_id, self.session_id, self.device_id):
            if not _ACTOR.fullmatch(item):
                raise TrustError("invalid_actor_scope", 422)
        try:
            if str(UUID(self.task_id)) != self.task_id:
                raise ValueError("Noncanonical task UUID")
        except ValueError as exc:
            raise TrustError("invalid_task_scope", 422) from exc


@dataclass(frozen=True, slots=True)
class GrantView:
    id: str
    task_id: str
    tool_name: str
    actor_id: str
    session_id: str
    device_id: str
    proposal_id: str
    binding_digest: str
    state: str
    expires_at: datetime

    @classmethod
    def from_record(cls, approval: ApprovalGrant) -> GrantView:
        return cls(
            approval.id,
            approval.task_id,
            approval.tool_name,
            approval.actor_id,
            approval.session_id,
            approval.device_id,
            approval.proposal_id,
            approval.binding_digest,
            approval.state,
            approval.expires_at,
        )


@dataclass(frozen=True, slots=True)
class SafeTaskStatus:
    """Deliberately no objective, personal data, model output or secret fields."""

    task_id: str
    state: str
    revision: int


def _audit(
    session: Session, owner_id: str, task_id: str, approval_id: str | None,
    action: str, decision: str, reason: str,
) -> None:
    session.add(
        TrustAudit(
            id=str(uuid4()),
            owner_id=owner_id,
            task_id=task_id,
            approval_id=approval_id,
            action=action,
            decision=decision,
            reason=reason,
        )
    )


def _parameters(context: ActorContext, proposal: ToolProposal) -> str:
    """Pure schema validation. Never execute or import code from a tool name."""
    if not isinstance(proposal, ToolProposal) or proposal.name != _TOOL:
        raise TrustError("capability_denied")
    if (
        not isinstance(proposal.call_id, str)
        or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", proposal.call_id)
    ):
        raise TrustError("invalid_proposal")
    if (
        not isinstance(proposal.arguments, dict)
        or set(proposal.arguments) != {"task_id"}
        or not isinstance(proposal.arguments["task_id"], str)
        or proposal.arguments["task_id"] != context.task_id
    ):
        raise TrustError("invalid_action_parameters")
    # Owner, actor, session, device, task, call and arguments are bound together.
    serial = {
        "owner_id": context.owner_id,
        "task_id": context.task_id,
        "actor_id": context.actor_id,
        "session_id": context.session_id,
        "device_id": context.device_id,
        "call_id": proposal.call_id,
        "tool": proposal.name,
        "arguments": {"task_id": context.task_id},
    }
    canonical = json.dumps(serial, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


class TrustService:
    """Approval request is internal; only decisions are owner-authenticated HTTP."""

    def __init__(self, engine: Engine) -> None:
        self.sessions = sessionmaker(engine, expire_on_commit=False)

    def request(
        self, context: ActorContext, proposal: ToolProposal, ttl_seconds: int = 300
    ) -> GrantView:
        if not 15 <= ttl_seconds <= 900:
            raise TrustError("invalid_approval_expiry", 422)
        try:
            digest = _parameters(context, proposal)
        except TrustError as error:
            # Audit only when the referenced task actually belongs to the owner.
            with self.sessions.begin() as session:
                _locked(session, context.task_id, context.owner_id)
                _audit(
                    session, context.owner_id, context.task_id, None,
                    "request", "deny", error.code,
                )
            raise
        approval = ApprovalGrant(
            id=str(uuid4()),
            owner_id=context.owner_id,
            task_id=context.task_id,
            tool_name=_TOOL,
            actor_id=context.actor_id,
            session_id=context.session_id,
            device_id=context.device_id,
            proposal_id=proposal.call_id,
            binding_digest=digest,
            state="pending",
            expires_at=utc_now() + timedelta(seconds=ttl_seconds),
        )
        with self.sessions.begin() as session:
            _locked(session, context.task_id, context.owner_id)
            session.add(approval)
            _audit(
                session, context.owner_id, context.task_id, approval.id,
                "request", "pending", "owner_approval_required",
            )
        return GrantView.from_record(approval)

    def list_pending(self, owner_id: str) -> list[GrantView]:
        if not _ACTOR.fullmatch(owner_id):
            raise TrustError("invalid_actor_scope", 422)
        with self.sessions() as session:
            approvals = session.scalars(
                select(ApprovalGrant)
                .where(
                    ApprovalGrant.owner_id == owner_id,
                    ApprovalGrant.state == "pending",
                    ApprovalGrant.expires_at > utc_now(),
                )
                .order_by(ApprovalGrant.created_at, ApprovalGrant.id)
                .limit(50)
            ).all()
            return [GrantView.from_record(item) for item in approvals]

    def decide(self, owner_id: str, approval_id: str, approve: bool) -> GrantView:
        """Only call from authenticated owner controller, never a worker or model."""
        failure: TrustError | None = None
        view: GrantView | None = None
        with self.sessions.begin() as session:
            grant = session.scalar(
                select(ApprovalGrant)
                .where(ApprovalGrant.id == approval_id, ApprovalGrant.owner_id == owner_id)
                .with_for_update()
            )
            if grant is None:
                raise TrustError("approval_not_found", 404)
            if grant.state != "pending":
                _audit(
                    session, owner_id, grant.task_id, grant.id,
                    "decision", "deny", "approval_not_pending",
                )
                failure = TrustError("approval_not_pending", 409)
            elif _is_expired(grant.expires_at):
                grant.state = "expired"
                _audit(
                    session, owner_id, grant.task_id, grant.id, "decision", "deny", "approval_expired"
                )
                failure = TrustError("approval_expired", 409)
            else:
                grant.state = "approved" if approve else "denied"
                grant.decided_at = utc_now()
                _audit(
                    session, owner_id, grant.task_id, grant.id,
                    "decision", "approved" if approve else "denied", "owner_decision",
                )
            view = GrantView.from_record(grant)
        if failure:
            raise failure
        assert view is not None
        return view

    def execute(
        self, context: ActorContext, approval_id: str, proposal: ToolProposal
    ) -> SafeTaskStatus:
        """Atomic consume and fixed read-only task.status, never arbitrary tool dispatch."""
        try:
            digest = _parameters(context, proposal)
        except TrustError as error:
            with self.sessions.begin() as session:
                _locked(session, context.task_id, context.owner_id)
                _audit(
                    session, context.owner_id, context.task_id, None,
                    "execute", "deny", error.code,
                )
            raise
        failure: TrustError | None = None
        result: SafeTaskStatus | None = None
        with self.sessions.begin() as session:
            grant = session.scalar(
                select(ApprovalGrant)
                .where(ApprovalGrant.id == approval_id, ApprovalGrant.owner_id == context.owner_id)
                .with_for_update()
            )
            if grant is None:
                raise TrustError("approval_not_found", 404)
            task = _locked(session, context.task_id, context.owner_id)
            if (
                grant.task_id != context.task_id
                or grant.tool_name != _TOOL
                or not compare_digest(grant.binding_digest, digest)
            ):
                reason = "approval_binding_mismatch"
            elif grant.state != "approved":
                reason = "approval_not_active"
            elif _is_expired(grant.expires_at):
                reason = "approval_expired"
                grant.state = "expired"
            else:
                reason = ""
            if reason:
                _audit(
                    session, context.owner_id, task.id, grant.id,
                    "execute", "deny", reason,
                )
                failure = TrustError(reason, 409)
            else:
                grant.state = "consumed"
                grant.consumed_at = utc_now()
                _audit(
                    session, context.owner_id, task.id, grant.id,
                    "execute", "allow", "single_use_read",
                )
                result = SafeTaskStatus(task.id, task.state, task.revision)
        if failure:
            raise failure
        assert result is not None
        return result
