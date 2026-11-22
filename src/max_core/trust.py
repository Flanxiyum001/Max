"""MAX Trust: deny-by-default, transactional, single-use, local-only approvals.

Untrusted model tool proposals never register capabilities or obtain grants by
claiming to be an owner. By default only ``task.status`` is allowlisted unless
an explicit ``ToolPolicyRegistry`` is provided by trusted host code.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from hmac import compare_digest
from types import MappingProxyType
from typing import Any, TypeVar
from uuid import UUID, uuid4

from sqlalchemy import Engine, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from max_core.gateway.contracts import ToolProposal
from max_core.models import ApprovalGrant, TrustAudit, utc_now
from max_core.persistence import StoreError, _is_expired, _locked

_ACTOR = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_TOOL_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,99}$")
_CALL_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_AUDIT_REASON = re.compile(r"^[a-z0-9_]{1,60}$")
_MAX_CANONICAL_ARGS_BYTES = 131_072
_TOOL = "task.status"

_T = TypeVar("_T")


class RiskTier(StrEnum):
    R0 = "r0_read"
    R1 = "r1_local"
    R2 = "r2_write"
    R3 = "r3_high_risk"


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
            if not isinstance(item, str) or not _ACTOR.fullmatch(item):
                raise TrustError("invalid_actor_scope", 422)
        try:
            if not isinstance(self.task_id, str) or str(UUID(self.task_id)) != self.task_id:
                raise ValueError("Noncanonical task UUID")
        except ValueError as exc:
            raise TrustError("invalid_task_scope", 422) from exc


ArgValidator = Callable[[ActorContext, dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True, slots=True)
class ToolPolicy:
    """Host-declared policy binding a canonical tool name to its risk tier and validator."""

    name: str
    risk: RiskTier
    validate_arguments: ArgValidator

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or _TOOL_NAME.fullmatch(self.name) is None:
            raise TrustError("invalid_tool_registry", 422)
        raw_risk = self.risk.value if isinstance(self.risk, StrEnum) else self.risk
        try:
            normalized_risk = RiskTier(raw_risk)
        except ValueError:
            raise TrustError("invalid_tool_registry", 422) from None
        object.__setattr__(self, "risk", normalized_risk)
        if not callable(self.validate_arguments):
            raise TrustError("invalid_tool_registry", 422)


class ToolPolicyRegistry:
    """Immutable allowlist of host-registered tool policies."""

    def __init__(self, policies: Sequence[ToolPolicy] = ()) -> None:
        mapping: dict[str, ToolPolicy] = {}
        for policy in policies:
            if not isinstance(policy, ToolPolicy):
                raise TrustError("invalid_tool_registry", 422)
            if policy.name in mapping:
                raise TrustError("invalid_tool_registry", 422)
            mapping[policy.name] = policy
        self._policies: MappingProxyType[str, ToolPolicy] = MappingProxyType(mapping)

    def get(self, name: str) -> ToolPolicy | None:
        if not isinstance(name, str):
            return None
        return self._policies.get(name)

    def names(self) -> frozenset[str]:
        return frozenset(self._policies.keys())

    def policies(self) -> tuple[ToolPolicy, ...]:
        return tuple(self._policies.values())

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._policies

    def __len__(self) -> int:
        return len(self._policies)


def _validate_task_status_args(context: ActorContext, args: dict[str, Any]) -> dict[str, Any]:
    if (
        set(args) != {"task_id"}
        or not isinstance(args["task_id"], str)
        or args["task_id"] != context.task_id
    ):
        raise TrustError("invalid_action_parameters")
    return {"task_id": context.task_id}


def _validate_task_create_args(_context: ActorContext, args: dict[str, Any]) -> dict[str, Any]:
    """Mirror of the local task.create schema so shared-default grants validate identically."""
    if set(args) != {"objective"} or not isinstance(args["objective"], str):
        raise TrustError("invalid_action_parameters")
    normalized = " ".join(args["objective"].split())
    if not 1 <= len(normalized) <= 500:
        raise TrustError("invalid_action_parameters")
    return {"objective": normalized}


_DEFAULT_TASK_STATUS_POLICY = ToolPolicy(
    name=_TOOL,
    risk=RiskTier.R0,
    validate_arguments=_validate_task_status_args,
)

_DEFAULT_TASK_CREATE_POLICY = ToolPolicy(
    name="task.create",
    risk=RiskTier.R1,
    validate_arguments=_validate_task_create_args,
)

_DEFAULT_REGISTRY = ToolPolicyRegistry((_DEFAULT_TASK_STATUS_POLICY, _DEFAULT_TASK_CREATE_POLICY))


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
    session: Session,
    owner_id: str,
    task_id: str,
    approval_id: str | None,
    action: str,
    decision: str,
    reason: str,
) -> None:
    safe_reason = reason if _AUDIT_REASON.fullmatch(reason) else "policy_denied"
    session.add(
        TrustAudit(
            id=str(uuid4()),
            owner_id=owner_id,
            task_id=task_id,
            approval_id=approval_id,
            action=action,
            decision=decision,
            reason=safe_reason,
        )
    )


class TrustService:
    """Approval request is internal; only decisions are owner-authenticated HTTP."""

    def __init__(
        self,
        engine: Engine,
        *,
        registry: ToolPolicyRegistry | Sequence[ToolPolicy] | None = None,
    ) -> None:
        self.engine = engine
        self.sessions = sessionmaker(engine, expire_on_commit=False)
        self._has_explicit_registry = registry is not None
        if registry is None:
            self._registry = _DEFAULT_REGISTRY
        elif isinstance(registry, ToolPolicyRegistry):
            self._registry = registry
        else:
            self._registry = ToolPolicyRegistry(registry)

    @property
    def has_explicit_registry(self) -> bool:
        return self._has_explicit_registry

    def with_registry(self, registry: ToolPolicyRegistry | Sequence[ToolPolicy]) -> TrustService:
        return TrustService(self.engine, registry=registry)

    def policy_for(self, tool_name: str) -> ToolPolicy | None:
        return self._registry.get(tool_name)

    def _validate_and_bind(
        self, context: ActorContext, proposal: ToolProposal
    ) -> tuple[ToolPolicy, dict[str, Any], str]:
        """Pure schema validation and binding. Never imports or executes code from a tool name."""
        if not isinstance(context, ActorContext):
            raise TrustError("invalid_actor_scope", 422)
        if not isinstance(proposal, ToolProposal) or not isinstance(proposal.name, str):
            raise TrustError("capability_denied")
        policy = self._registry.get(proposal.name)
        if policy is None or policy.risk == RiskTier.R3:
            raise TrustError("capability_denied")
        if not isinstance(proposal.call_id, str) or _CALL_ID.fullmatch(proposal.call_id) is None:
            raise TrustError("invalid_proposal")
        if not isinstance(proposal.arguments, dict):
            raise TrustError("invalid_action_parameters")
        try:
            validated = policy.validate_arguments(context, dict(proposal.arguments))
        except TrustError:
            raise
        except (ValueError, TypeError, RecursionError):
            raise TrustError("invalid_action_parameters") from None
        if not isinstance(validated, dict):
            raise TrustError("invalid_action_parameters")
        try:
            canonical_args_json = json.dumps(
                validated,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            )
        except (TypeError, ValueError, RecursionError):
            raise TrustError("invalid_action_parameters") from None
        if len(canonical_args_json.encode("ascii")) > _MAX_CANONICAL_ARGS_BYTES:
            raise TrustError("invalid_action_parameters")
        canonical_args: dict[str, Any] = json.loads(canonical_args_json)

        # Owner, actor, session, device, task, call, tool, risk, and canonical arguments are bound.
        serial = {
            "owner_id": context.owner_id,
            "task_id": context.task_id,
            "actor_id": context.actor_id,
            "session_id": context.session_id,
            "device_id": context.device_id,
            "call_id": proposal.call_id,
            "tool": policy.name,
            "risk": policy.risk.value,
            "arguments": canonical_args,
        }
        canonical = json.dumps(
            serial, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        )
        digest = hashlib.sha256(canonical.encode("ascii")).hexdigest()
        return policy, canonical_args, digest

    def record_denial(self, context: ActorContext, action: str, reason: str) -> None:
        """Best-effort audit; never masks the caller's original error."""
        if not isinstance(context, ActorContext):
            raise TrustError("invalid_actor_scope", 422)
        if action not in {"request", "execute"}:
            raise TrustError("invalid_proposal", 422)
        safe_reason = reason if _AUDIT_REASON.fullmatch(reason) else "policy_denied"
        try:
            with self.sessions.begin() as session:
                try:
                    _locked(session, context.task_id, context.owner_id)
                except StoreError:
                    return
                _audit(
                    session,
                    context.owner_id,
                    context.task_id,
                    None,
                    action,
                    "deny",
                    safe_reason,
                )
        except SQLAlchemyError:
            return

    def request(
        self, context: ActorContext, proposal: ToolProposal, ttl_seconds: int = 300
    ) -> GrantView:
        if type(ttl_seconds) is not int or not 15 <= ttl_seconds <= 900:
            raise TrustError("invalid_approval_expiry", 422)
        try:
            policy, _canonical_args, digest = self._validate_and_bind(context, proposal)
        except TrustError as error:
            # Audit only when the referenced task actually belongs to the owner.
            self.record_denial(context, "request", error.code)
            raise
        approval = ApprovalGrant(
            id=str(uuid4()),
            owner_id=context.owner_id,
            task_id=context.task_id,
            tool_name=policy.name,
            actor_id=context.actor_id,
            session_id=context.session_id,
            device_id=context.device_id,
            proposal_id=proposal.call_id,
            binding_digest=digest,
            state="pending",
            expires_at=utc_now() + timedelta(seconds=ttl_seconds),
        )
        for attempt in range(2):
            try:
                with self.sessions.begin() as session:
                    _locked(session, context.task_id, context.owner_id)
                    session.add(approval)
                    _audit(
                        session,
                        context.owner_id,
                        context.task_id,
                        approval.id,
                        "request",
                        "pending",
                        "owner_approval_required",
                    )
                return GrantView.from_record(approval)
            except IntegrityError:
                if attempt:
                    raise TrustError("duplicate_proposal", 409) from None
                with self.sessions.begin() as session:
                    stale = session.scalars(
                        select(ApprovalGrant).where(
                            ApprovalGrant.owner_id == context.owner_id,
                            ApprovalGrant.task_id == context.task_id,
                            ApprovalGrant.proposal_id == proposal.call_id,
                            ApprovalGrant.state == "pending",
                            ApprovalGrant.expires_at <= utc_now(),
                        )
                    ).all()
                    if not stale:
                        raise TrustError("duplicate_proposal", 409) from None
                    for grant in stale:
                        grant.state = "expired"
        raise TrustError("duplicate_proposal", 409)

    def list_pending(self, owner_id: str) -> list[GrantView]:
        if not isinstance(owner_id, str) or not _ACTOR.fullmatch(owner_id):
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

    def get_grant(self, owner_id: str, approval_id: str) -> GrantView:
        if not isinstance(owner_id, str) or not _ACTOR.fullmatch(owner_id):
            raise TrustError("invalid_actor_scope", 422)
        if not isinstance(approval_id, str) or not 1 <= len(approval_id) <= 36:
            raise TrustError("approval_not_found", 404)
        with self.sessions() as session:
            grant = session.scalar(
                select(ApprovalGrant).where(
                    ApprovalGrant.id == approval_id,
                    ApprovalGrant.owner_id == owner_id,
                )
            )
            if grant is None:
                raise TrustError("approval_not_found", 404)
            return GrantView.from_record(grant)

    def decide(self, owner_id: str, approval_id: str, approve: bool) -> GrantView:
        """Only call from authenticated owner controller, never a worker or model."""
        if not isinstance(owner_id, str) or not _ACTOR.fullmatch(owner_id):
            raise TrustError("invalid_actor_scope", 422)
        if not isinstance(approval_id, str) or not 1 <= len(approval_id) <= 36:
            raise TrustError("approval_not_found", 404)
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
                    session,
                    owner_id,
                    grant.task_id,
                    grant.id,
                    "decision",
                    "deny",
                    "approval_not_pending",
                )
                failure = TrustError("approval_not_pending", 409)
            elif _is_expired(grant.expires_at):
                grant.state = "expired"
                _audit(
                    session,
                    owner_id,
                    grant.task_id,
                    grant.id,
                    "decision",
                    "deny",
                    "approval_expired",
                )
                failure = TrustError("approval_expired", 409)
            else:
                grant.state = "approved" if approve else "denied"
                grant.decided_at = utc_now()
                _audit(
                    session,
                    owner_id,
                    grant.task_id,
                    grant.id,
                    "decision",
                    "approved" if approve else "denied",
                    "owner_decision",
                )
            view = GrantView.from_record(grant)
        if failure:
            raise failure
        assert view is not None
        return view

    def _consume_grant(
        self,
        context: ActorContext,
        approval_id: str,
        proposal: ToolProposal,
        *,
        allow_reason: str | None = None,
    ) -> tuple[ToolPolicy, dict[str, Any], SafeTaskStatus]:
        """Consume under row locks, grant row first, then the task row.

        This is the only path that takes both locks; no other method holds a
        task lock while acquiring a grant lock, so the ordering cannot cycle.
        """
        if not isinstance(approval_id, str) or not 1 <= len(approval_id) <= 36:
            raise TrustError("approval_not_found", 404)
        try:
            policy, canonical_args, digest = self._validate_and_bind(context, proposal)
        except TrustError as error:
            self.record_denial(context, "execute", error.code)
            raise
        failure: TrustError | None = None
        task_status: SafeTaskStatus | None = None
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
                or grant.tool_name != policy.name
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
                    session,
                    context.owner_id,
                    task.id,
                    grant.id,
                    "execute",
                    "deny",
                    reason,
                )
                failure = TrustError(reason, 409)
            else:
                grant.state = "consumed"
                grant.consumed_at = utc_now()
                resolved_reason = (
                    allow_reason if allow_reason is not None else f"single_use_{policy.risk.value}"
                )
                _audit(
                    session,
                    context.owner_id,
                    task.id,
                    grant.id,
                    "execute",
                    "allow",
                    resolved_reason,
                )
                task_status = SafeTaskStatus(task.id, task.state, task.revision)
        if failure:
            raise failure
        assert task_status is not None
        return policy, canonical_args, task_status

    def execute(
        self, context: ActorContext, approval_id: str, proposal: ToolProposal
    ) -> SafeTaskStatus:
        """Atomic consume and fixed read-only task.status, never arbitrary tool dispatch."""
        if not isinstance(proposal, ToolProposal) or proposal.name != _TOOL:
            if isinstance(context, ActorContext):
                self.record_denial(context, "execute", "capability_denied")
            raise TrustError("capability_denied")
        _policy, _canonical_args, task_status = self._consume_grant(
            context,
            approval_id,
            proposal,
            allow_reason="single_use_read",
        )
        return task_status

    def execute_r0(
        self,
        context: ActorContext,
        proposal: ToolProposal,
        handler: Callable[[dict[str, Any]], _T],
    ) -> _T:
        """Validate, auto-audit R0 tool, commit DB lock, then invoke handler outside session."""
        try:
            policy, canonical_args, _digest = self._validate_and_bind(context, proposal)
        except TrustError as error:
            self.record_denial(context, "execute", error.code)
            raise
        if policy.risk != RiskTier.R0:
            self.record_denial(context, "execute", "approval_required")
            raise TrustError("approval_required", 403)
        with self.sessions.begin() as session:
            _locked(session, context.task_id, context.owner_id)
            _audit(
                session,
                context.owner_id,
                context.task_id,
                None,
                "execute",
                "allow",
                "r0_auto_approved",
            )
        try:
            return handler(canonical_args)
        except Exception:
            with self.sessions.begin() as session:
                _locked(session, context.task_id, context.owner_id)
                _audit(
                    session,
                    context.owner_id,
                    context.task_id,
                    None,
                    "execute_outcome",
                    "error",
                    "tool_execution_failed",
                )
            raise

    def consume_and_execute(
        self,
        context: ActorContext,
        approval_id: str,
        proposal: ToolProposal,
        handler: Callable[[dict[str, Any]], _T],
    ) -> _T:
        """Atomically consume single-use grant, commit DB lock, then run handler outside session."""
        _policy, canonical_args, _task_status = self._consume_grant(
            context,
            approval_id,
            proposal,
        )
        try:
            return handler(canonical_args)
        except Exception:
            with self.sessions.begin() as session:
                _locked(session, context.task_id, context.owner_id)
                _audit(
                    session,
                    context.owner_id,
                    context.task_id,
                    approval_id,
                    "execute_outcome",
                    "error",
                    "tool_execution_failed",
                )
            raise
