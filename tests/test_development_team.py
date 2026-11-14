"""Controller integration through deterministic adapters; no live model or execution."""

import asyncio

import pytest

from max_core.development_team import (
    Assignment,
    CheckEvidence,
    CheckStatus,
    DevelopmentTeam,
    SpecialistAdapter,
    SpecialistResult,
    SpecialistRole,
    TeamError,
)

BASE = "a" * 40
HEAD = "b" * 40
NEXT = "c" * 40
CHECKS = ("lint", "format", "typing", "unit_security", "postgres", "dependency_audit")


def checks(
    revision: str = HEAD, status: CheckStatus = CheckStatus.PASSED
) -> tuple[CheckEvidence, ...]:
    return tuple(CheckEvidence(name, revision, status, f"ci-{name}") for name in CHECKS)


class FixtureTeam:
    def __init__(self) -> None:
        self.calls: list[Assignment] = []
        self.observed: list[str] = []
        self.review_issues: tuple[str, ...] = ()
        self.repair = False
        self.stale_review = False
        self.evidence = checks()

    async def specialist(self, assignment: Assignment) -> SpecialistResult:
        self.calls.append(assignment)
        if assignment.role == SpecialistRole.IMPLEMENTATION:
            revision = NEXT if assignment.round_number > 1 else HEAD
            return SpecialistResult(revision)
        if assignment.role == SpecialistRole.REVIEW:
            issues = self.review_issues
            if self.repair and assignment.round_number > 1:
                issues = ()
            return SpecialistResult(BASE if self.stale_review else assignment.revision, issues)
        return SpecialistResult(assignment.revision)

    async def observe(self, revision: str) -> tuple[CheckEvidence, ...]:
        self.observed.append(revision)
        if self.repair:
            return checks(revision)
        return self.evidence

    def adapters(self, *, ux: bool = False) -> tuple[SpecialistAdapter, ...]:
        roles = list(SpecialistRole) if ux else list(SpecialistRole)[:-1]
        return tuple(SpecialistAdapter(f"actor-{role}", role, self.specialist) for role in roles)

    def controller(self, **kwargs: object) -> DevelopmentTeam:
        return DevelopmentTeam(self.adapters(), self.observe, **kwargs)  # type: ignore[arg-type]


def test_complete_controller_dispatches_independent_roles_and_observer() -> None:
    fixture = FixtureTeam()
    report = asyncio.run(fixture.controller().run("task-1", BASE, "Implement a bounded change"))
    assert report.ready_for_owner_review
    assert report.revision == HEAD
    assert report.rounds == 1
    assert [call.role for call in fixture.calls] == [
        SpecialistRole.IMPLEMENTATION,
        SpecialistRole.REVIEW,
        SpecialistRole.QA,
        SpecialistRole.SECURITY,
    ]
    assert fixture.calls[0].revision == BASE
    assert all(call.revision == HEAD for call in fixture.calls[1:])
    assert len({call.actor_id for call in fixture.calls}) == 4
    assert fixture.observed == [HEAD]
    assert not hasattr(report, "released")


@pytest.mark.parametrize("mode", ["missing", "failed", "unavailable"])
def test_agent_approvals_never_replace_required_successful_checks(mode: str) -> None:
    fixture = FixtureTeam()
    if mode == "missing":
        fixture.evidence = checks()[:-1]
    else:
        fixture.evidence = checks(status=CheckStatus(mode))
    report = asyncio.run(fixture.controller().run("task-1", BASE, "Code"))
    assert not report.ready_for_owner_review
    assert report.rounds == 1  # no blind implementation retry for missing/failed CI


def test_repair_round_rechecks_new_revision_and_passes_issues_to_implementer() -> None:
    fixture = FixtureTeam()
    fixture.review_issues = ("Add a negative authorization test",)
    fixture.repair = True
    report = asyncio.run(fixture.controller().run("task-1", BASE, "Code"))
    assert report.ready_for_owner_review
    assert report.rounds == 2
    assert report.revision == NEXT
    assert fixture.observed == [HEAD, NEXT]
    implementation = [call for call in fixture.calls if call.role == SpecialistRole.IMPLEMENTATION]
    assert implementation[1].prior_issues == fixture.review_issues
    assert implementation[1].revision == HEAD


def test_persistent_issues_stop_at_budget_and_escalate_in_report() -> None:
    fixture = FixtureTeam()
    fixture.review_issues = ("Security issue remains",)
    fixture.repair = True

    # Keep the issue for every round instead of using the fixture's repaired reviewer.
    async def specialist(assignment: Assignment) -> SpecialistResult:
        fixture.calls.append(assignment)
        return SpecialistResult(
            HEAD, fixture.review_issues if assignment.role == SpecialistRole.REVIEW else ()
        )

    adapters = tuple(
        SpecialistAdapter(str(role), role, specialist) for role in list(SpecialistRole)[:-1]
    )
    report = asyncio.run(
        DevelopmentTeam(adapters, fixture.observe, max_rounds=3).run("task", BASE, "Code")
    )
    assert not report.ready_for_owner_review
    assert report.rounds == 3
    assert report.issues == fixture.review_issues
    assert len(fixture.calls) == 12


def test_stale_review_stops_before_checks() -> None:
    fixture = FixtureTeam()
    fixture.stale_review = True
    with pytest.raises(TeamError, match="stale_or_invalid_review"):
        asyncio.run(fixture.controller().run("task", BASE, "Code"))
    assert fixture.observed == []


@pytest.mark.parametrize("mode", ["stale", "duplicate"])
def test_stale_or_duplicate_host_evidence_is_denied(mode: str) -> None:
    fixture = FixtureTeam()
    fixture.evidence = checks(BASE) if mode == "stale" else checks() + checks()[:1]
    with pytest.raises(TeamError, match="stale_check_evidence|duplicate_check_evidence"):
        asyncio.run(fixture.controller().run("task", BASE, "Code"))


def test_ui_requires_and_runs_ux_reviewer() -> None:
    fixture = FixtureTeam()
    with pytest.raises(TeamError, match="missing_ux_reviewer"):
        asyncio.run(fixture.controller().run("task", BASE, "UI change", has_ui=True))
    assert fixture.calls == []
    controller = DevelopmentTeam(fixture.adapters(ux=True), fixture.observe)
    report = asyncio.run(controller.run("task", BASE, "UI change", has_ui=True))
    assert report.ready_for_owner_review
    assert SpecialistRole.UX in report.review_roles
    assert fixture.calls[-1].role == SpecialistRole.UX


def test_reused_actor_and_missing_role_denied_before_dispatch() -> None:
    fixture = FixtureTeam()
    repeated = tuple(
        SpecialistAdapter("same", role, fixture.specialist) for role in list(SpecialistRole)[:-1]
    )
    with pytest.raises(TeamError, match="independent_actors_required"):
        DevelopmentTeam(repeated, fixture.observe)
    with pytest.raises(TeamError, match="invalid_team"):
        DevelopmentTeam(fixture.adapters()[:-1], fixture.observe)
    assert fixture.calls == []


def test_cooperative_deadline_stops_dispatch_and_sanitizes_failure() -> None:
    fixture = FixtureTeam()

    async def waiting(assignment: Assignment) -> SpecialistResult:
        await asyncio.sleep(10)
        return SpecialistResult(HEAD)

    adapters = tuple(
        SpecialistAdapter(str(role), role, waiting) for role in list(SpecialistRole)[:-1]
    )
    with pytest.raises(TeamError, match="team_deadline_exceeded"):
        asyncio.run(
            DevelopmentTeam(adapters, fixture.observe, deadline_seconds=0.01).run(
                "task", BASE, "Code"
            )
        )
    assert fixture.observed == []


def test_external_cancellation_is_preserved_and_no_reviews_run() -> None:
    fixture = FixtureTeam()

    async def run_cancelled() -> None:
        entered = asyncio.Event()

        async def waiting(assignment: Assignment) -> SpecialistResult:
            fixture.calls.append(assignment)
            entered.set()
            await asyncio.sleep(10)
            return SpecialistResult(HEAD)

        adapters = tuple(
            SpecialistAdapter(str(role), role, waiting) for role in list(SpecialistRole)[:-1]
        )
        task = asyncio.create_task(
            DevelopmentTeam(adapters, fixture.observe).run("task", BASE, "Code")
        )
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run_cancelled())
    assert len(fixture.calls) == 1
    assert fixture.observed == []


@pytest.mark.parametrize("error_type", [RuntimeError, TeamError])
def test_adapter_error_never_leaks_private_message(error_type: type[Exception]) -> None:
    fixture = FixtureTeam()

    async def failed(assignment: Assignment) -> SpecialistResult:
        raise error_type("secret-api-token")

    adapters = tuple(
        SpecialistAdapter(str(role), role, failed) for role in list(SpecialistRole)[:-1]
    )
    with pytest.raises(TeamError) as error:
        asyncio.run(DevelopmentTeam(adapters, fixture.observe).run("task", BASE, "Code"))
    assert str(error.value) == "team_adapter_failed"
    assert error.value.__suppress_context__


@pytest.mark.parametrize("rounds", [0, 4, True])
def test_invalid_round_budget_rejected(rounds: int) -> None:
    fixture = FixtureTeam()
    with pytest.raises(TeamError, match="invalid_round_budget"):
        fixture.controller(max_rounds=rounds)


def test_observer_failure_is_sanitized() -> None:
    fixture = FixtureTeam()

    async def observer(revision: str) -> tuple[CheckEvidence, ...]:
        raise TeamError("private-check-url")

    with pytest.raises(TeamError, match="^team_adapter_failed$"):
        asyncio.run(DevelopmentTeam(fixture.adapters(), observer).run("task", BASE, "Code"))


def test_untyped_observer_output_rejected() -> None:
    fixture = FixtureTeam()

    async def observer(revision: str) -> tuple[CheckEvidence, ...]:
        return ("passed",)  # type: ignore[return-value]

    with pytest.raises(TeamError, match="invalid_check_evidence"):
        asyncio.run(DevelopmentTeam(fixture.adapters(), observer).run("task", BASE, "Code"))
