"""Owner-reviewed offline golden-output comparisons for MAX Evolution.

No model invocation, task timeline extraction, telemetry, logging, side effects,
prompt retention or automatic route selection happens in this module.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from max_core.gateway.catalogue import RouteIdentity
from max_core.gateway.contracts import GatewayResponse, ToolProposal
from max_core.gateway.task_planner import TaskKind

_CASE_ID = re.compile(r"[a-z][a-z0-9_.-]{0,63}")
_REVISION = re.compile(r"[A-Za-z0-9_.:-]{1,100}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_TOOL_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,99}")

FailureCode = Literal[
    "missing_observation",
    "route_mismatch",
    "catalogue_revision_mismatch",
    "provider_model_mismatch",
    "text_mismatch",
    "tool_names_mismatch",
    "prohibited_tool",
]


class GoldenReplayError(ValueError):
    """Fixed error codes only: no prompt, response or model content."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True, repr=False)
class GoldenCase:
    """An explicitly reviewed fixture using only hashed expected text.

    owner_reviewed is an assertion from the *trusted caller*, not identity
    verification or an approval grant. Never derive it from LLM output.
    """

    case_id: str
    task: TaskKind
    route: RouteIdentity
    catalogue_revision: str
    expected_text_sha256: str | None = None
    expected_tool_names: tuple[str, ...] | None = None
    prohibited_tool_names: tuple[str, ...] = ()
    owner_reviewed: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.case_id, str) or not _CASE_ID.fullmatch(self.case_id):
            raise GoldenReplayError("invalid_case_id")
        if not isinstance(self.task, TaskKind) or not isinstance(self.route, RouteIdentity):
            raise GoldenReplayError("invalid_case_scope")
        if (
            not isinstance(self.catalogue_revision, str)
            or not _REVISION.fullmatch(self.catalogue_revision)
        ):
            raise GoldenReplayError("invalid_catalogue_revision")
        if self.expected_text_sha256 is not None and (
            not isinstance(self.expected_text_sha256, str)
            or not _DIGEST.fullmatch(self.expected_text_sha256)
        ):
            raise GoldenReplayError("invalid_expected_digest")
        if self.expected_tool_names is not None and not _valid_names(self.expected_tool_names):
            raise GoldenReplayError("invalid_expected_tools")
        if not _valid_names(self.prohibited_tool_names):
            raise GoldenReplayError("invalid_prohibited_tools")
        if (
            self.expected_tool_names is not None
            and set(self.expected_tool_names) & set(self.prohibited_tool_names)
        ):
            raise GoldenReplayError("conflicting_tool_assertions")
        if (
            self.expected_text_sha256 is None
            and self.expected_tool_names is None
            and not self.prohibited_tool_names
        ):
            raise GoldenReplayError("empty_assertions")
        if self.owner_reviewed is not True:
            raise GoldenReplayError("owner_review_required")


def _valid_names(names: tuple[str, ...]) -> bool:
    return (
        isinstance(names, tuple)
        and len(names) <= 16
        and all(
            isinstance(name, str) and _TOOL_NAME.fullmatch(name) is not None for name in names
        )
        and len(names) == len(set(names))
    )


@dataclass(frozen=True, slots=True, repr=False)
class ReplayObservation:
    case_id: str
    route: RouteIdentity
    catalogue_revision: str
    response: GatewayResponse = field(repr=False)


@dataclass(frozen=True, slots=True)
class ReplayVerdict:
    case_id: str
    passed: bool
    failures: tuple[FailureCode, ...]


@dataclass(frozen=True, slots=True)
class ReplayReport:
    total: int
    passed: int
    verdicts: tuple[ReplayVerdict, ...]

    @property
    def all_passed(self) -> bool:
        return self.total > 0 and self.total == self.passed


@dataclass(frozen=True, slots=True)
class OfflineGoldenSuite:
    """Comparisons are deterministic and require complete case coverage."""

    cases: tuple[GoldenCase, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.cases, tuple)
            or not 1 <= len(self.cases) <= 500
            or any(not isinstance(case, GoldenCase) for case in self.cases)
        ):
            raise GoldenReplayError("invalid_golden_suite")
        case_ids = [case.case_id for case in self.cases]
        if len(set(case_ids)) != len(case_ids):
            raise GoldenReplayError("duplicate_golden_case")

    def compare(self, observations: Sequence[ReplayObservation]) -> ReplayReport:
        by_id: dict[str, ReplayObservation] = {}
        known_ids = {case.case_id for case in self.cases}
        if len(observations) > 500:
            raise GoldenReplayError("too_many_observations")
        for observation in observations:
            if (
                not isinstance(observation, ReplayObservation)
                or observation.case_id not in known_ids
                or not isinstance(observation.route, RouteIdentity)
                or not isinstance(observation.response, GatewayResponse)
                or not isinstance(observation.response.content, str)
                or len(observation.response.content) > 128_000
                or not isinstance(observation.response.tool_proposals, tuple)
                or any(
                    not isinstance(proposal, ToolProposal)
                    for proposal in observation.response.tool_proposals
                )
            ):
                raise GoldenReplayError("invalid_observation")
            if observation.case_id in by_id:
                raise GoldenReplayError("duplicate_observation")
            by_id[observation.case_id] = observation

        verdicts: list[ReplayVerdict] = []
        for case in self.cases:
            observed = by_id.get(case.case_id)
            if observed is None:
                verdicts.append(ReplayVerdict(case.case_id, False, ("missing_observation",)))
                continue
            failures: list[FailureCode] = []
            if observed.route != case.route:
                failures.append("route_mismatch")
            if observed.catalogue_revision != case.catalogue_revision:
                failures.append("catalogue_revision_mismatch")
            if (
                observed.response.provider_id != case.route.model.provider_id
                or observed.response.model_id != case.route.model.model_id
            ):
                failures.append("provider_model_mismatch")
            if case.expected_text_sha256 is not None:
                actual_digest = hashlib.sha256(
                    observed.response.content.encode("utf-8")
                ).hexdigest()
                if actual_digest != case.expected_text_sha256:
                    failures.append("text_mismatch")
            names = tuple(proposal.name for proposal in observed.response.tool_proposals)
            if case.expected_tool_names is not None and names != case.expected_tool_names:
                failures.append("tool_names_mismatch")
            if set(names) & set(case.prohibited_tool_names):
                failures.append("prohibited_tool")
            verdicts.append(ReplayVerdict(case.case_id, not failures, tuple(failures)))
        passed = sum(verdict.passed for verdict in verdicts)
        return ReplayReport(len(self.cases), passed, tuple(verdicts))
