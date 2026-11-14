"""Bounded development-team controller with trusted host-injected fixture adapters.

This does not provision agents, execute code, attest tests, or authorize a release.
Callbacks must be trusted, cooperative, isolated by the host, and have no release
permissions. Different actor IDs establish assignment separation, not process or
model independence. Model opinions never substitute for host-observed checks.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import ParamSpec, TypeVar

_T = TypeVar("_T")
_P = ParamSpec("_P")


async def _call(callback: Callable[_P, Awaitable[_T]], *args: _P.args, **kwargs: _P.kwargs) -> _T:
    try:
        return await callback(*args, **kwargs)
    except asyncio.CancelledError:
        raise
    except Exception:
        raise TeamError("team_adapter_failed") from None


class TeamError(ValueError):
    """Stable sanitized controller errors."""


class SpecialistRole(StrEnum):
    IMPLEMENTATION = "implementation"
    REVIEW = "review"
    QA = "qa"
    SECURITY = "security"
    UX = "ux"


class CheckStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


_REQUIRED_CHECKS = frozenset(
    {"lint", "format", "typing", "unit_security", "postgres", "dependency_audit"}
)


def _identity(value: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", value):
        raise TeamError("invalid_identity")


def _revision(value: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise TeamError("invalid_revision")


@dataclass(frozen=True, slots=True)
class Assignment:
    task_id: str
    actor_id: str
    role: SpecialistRole
    revision: str
    round_number: int
    objective: str
    prior_issues: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SpecialistResult:
    revision: str
    issues: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _revision(self.revision)
        if (
            not isinstance(self.issues, tuple)
            or len(self.issues) > 50
            or any(
                not isinstance(issue, str) or not issue or len(issue) > 1000
                for issue in self.issues
            )
        ):
            raise TeamError("invalid_specialist_result")


@dataclass(frozen=True, slots=True)
class CheckEvidence:
    """Host-observer output, never deserialize directly from a specialist's reply.

    The adapter is responsible for authoritative check collection and exact-head
    coverage. This value is a contract, not cryptographic attestation.
    """

    name: str
    revision: str
    status: CheckStatus
    evidence_id: str

    def __post_init__(self) -> None:
        _identity(self.name)
        _identity(self.evidence_id)
        _revision(self.revision)
        if not isinstance(self.status, CheckStatus):
            raise TeamError("invalid_check_status")


Specialist = Callable[[Assignment], Awaitable[SpecialistResult]]
CheckObserver = Callable[[str], Awaitable[tuple[CheckEvidence, ...]]]


@dataclass(frozen=True, slots=True)
class SpecialistAdapter:
    actor_id: str
    role: SpecialistRole
    run: Specialist

    def __post_init__(self) -> None:
        _identity(self.actor_id)
        if not isinstance(self.role, SpecialistRole) or not callable(self.run):
            raise TeamError("invalid_specialist_adapter")


@dataclass(frozen=True, slots=True)
class TeamReport:
    task_id: str
    revision: str
    rounds: int
    review_roles: tuple[SpecialistRole, ...]
    issues: tuple[str, ...]
    check_evidence: tuple[CheckEvidence, ...]
    ready_for_owner_review: bool
    # There is deliberately no released/merged/completed property or executor.


class DevelopmentTeam:
    """One explicitly invoked task; no recursive delegation or tool grant registry."""

    def __init__(
        self,
        adapters: tuple[SpecialistAdapter, ...],
        observe_checks: CheckObserver,
        *,
        max_rounds: int = 2,
        deadline_seconds: float = 60,
    ) -> None:
        if not isinstance(adapters, tuple) or not 4 <= len(adapters) <= 5:
            raise TeamError("invalid_team")
        if any(not isinstance(adapter, SpecialistAdapter) for adapter in adapters):
            raise TeamError("invalid_team")
        if len({adapter.actor_id for adapter in adapters}) != len(adapters):
            raise TeamError("independent_actors_required")
        if len({adapter.role for adapter in adapters}) != len(adapters):
            raise TeamError("duplicate_role")
        if not {
            SpecialistRole.IMPLEMENTATION,
            SpecialistRole.REVIEW,
            SpecialistRole.QA,
            SpecialistRole.SECURITY,
        } <= {adapter.role for adapter in adapters}:
            raise TeamError("missing_required_role")
        if type(max_rounds) is not int or not 1 <= max_rounds <= 3:
            raise TeamError("invalid_round_budget")
        if type(deadline_seconds) not in {int, float} or not 0 < deadline_seconds <= 300:
            raise TeamError("invalid_deadline")
        if not callable(observe_checks):
            raise TeamError("invalid_observer")
        self._adapters = {adapter.role: adapter for adapter in adapters}
        self._observe_checks = observe_checks
        self._max_rounds = max_rounds
        self._deadline_seconds = deadline_seconds

    async def run(
        self, task_id: str, revision: str, objective: str, *, has_ui: bool = False
    ) -> TeamReport:
        _identity(task_id)
        _revision(revision)
        if not isinstance(objective, str) or not objective or len(objective) > 8000:
            raise TeamError("invalid_objective")
        if type(has_ui) is not bool:
            raise TeamError("invalid_ui_flag")
        if has_ui and SpecialistRole.UX not in self._adapters:
            raise TeamError("missing_ux_reviewer")
        roles: tuple[SpecialistRole, ...] = (
            SpecialistRole.REVIEW,
            SpecialistRole.QA,
            SpecialistRole.SECURITY,
        )
        if has_ui:
            roles += (SpecialistRole.UX,)
        issues: tuple[str, ...] = ()
        try:
            async with asyncio.timeout(self._deadline_seconds):
                for round_number in range(1, self._max_rounds + 1):
                    implementation = self._adapters[SpecialistRole.IMPLEMENTATION]
                    result = await _call(
                        implementation.run,
                        Assignment(
                            task_id,
                            implementation.actor_id,
                            implementation.role,
                            revision,
                            round_number,
                            objective,
                            issues,
                        ),
                    )
                    if not isinstance(result, SpecialistResult):
                        raise TeamError("invalid_implementation_result")
                    # Implementation may change revision; every review and check must bind to it.
                    revision = result.revision
                    findings: list[str] = list(result.issues)
                    for role in roles:
                        adapter = self._adapters[role]
                        reviewed = await _call(
                            adapter.run,
                            Assignment(
                                task_id,
                                adapter.actor_id,
                                role,
                                revision,
                                round_number,
                                objective,
                            ),
                        )
                        if (
                            not isinstance(reviewed, SpecialistResult)
                            or reviewed.revision != revision
                        ):
                            raise TeamError("stale_or_invalid_review")
                        findings.extend(reviewed.issues)
                    issues = tuple(findings)
                    if len(issues) > 250:
                        raise TeamError("issue_budget_exceeded")
                    evidence = await _call(self._observe_checks, revision)
                    if (
                        not isinstance(evidence, tuple)
                        or len(evidence) > 32
                        or any(not isinstance(check, CheckEvidence) for check in evidence)
                    ):
                        raise TeamError("invalid_check_evidence")
                    if len({check.name for check in evidence}) != len(evidence):
                        raise TeamError("duplicate_check_evidence")
                    if any(check.revision != revision for check in evidence):
                        raise TeamError("stale_check_evidence")
                    checks_passed = _REQUIRED_CHECKS <= {check.name for check in evidence} and all(
                        check.status == CheckStatus.PASSED for check in evidence
                    )
                    ready = not issues and checks_passed
                    if ready or round_number == self._max_rounds or not issues:
                        return TeamReport(
                            task_id, revision, round_number, roles, issues, evidence, ready
                        )
        except asyncio.CancelledError:
            # Preserve cancellation; no further specialist or observer is dispatched.
            raise
        except TimeoutError:
            raise TeamError("team_deadline_exceeded") from None
        except TeamError:
            raise
        except Exception:
            raise TeamError("team_adapter_failed") from None
        raise TeamError("team_incomplete")
