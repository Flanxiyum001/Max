"""Offline, owner-reviewed quality evidence for advisory task routing.

No prompt content, provider credentials, autonomous testing, background learning,
telemetry, or model execution is accepted or performed by this module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from max_core.gateway.catalogue import EvidenceSource, RouteIdentity


class EvaluationError(ValueError):
    """Sanitized evaluation-contract error, never includes private records."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class QualityObservation:
    route: RouteIdentity
    task_id: str
    catalogue_revision: str
    quality: Decimal
    sample_count: int
    p95_latency_ms: int
    provenance: EvidenceSource

    def __post_init__(self) -> None:
        if not isinstance(self.route, RouteIdentity):
            raise EvaluationError("invalid_evaluation_route")
        if not isinstance(self.task_id, str) or not re.fullmatch(
            r"[a-z][a-z0-9_.-]{0,63}", self.task_id
        ):
            raise EvaluationError("invalid_evaluation_task")
        if not isinstance(self.catalogue_revision, str) or not re.fullmatch(
            r"[A-Za-z0-9_.:-]{1,100}", self.catalogue_revision
        ):
            raise EvaluationError("invalid_evaluation_revision")
        if (
            not isinstance(self.quality, Decimal)
            or not self.quality.is_finite()
            or not (Decimal(0) <= self.quality <= Decimal(1))
        ):
            raise EvaluationError("invalid_evaluation_quality")
        if type(self.sample_count) is not int or not 5 <= self.sample_count <= 1_000_000:
            raise EvaluationError("insufficient_evaluation_samples")
        if type(self.p95_latency_ms) is not int or not 1 <= self.p95_latency_ms <= 3_600_000:
            raise EvaluationError("invalid_evaluation_latency")
        if not isinstance(self.provenance, EvidenceSource) or self.provenance not in {
            EvidenceSource.OWNER_REVIEWED,
            EvidenceSource.PROTOCOL_TESTED,
        }:
            raise EvaluationError("unverified_evaluation_provenance")


@dataclass(frozen=True, slots=True)
class OfflineEvaluationBook:
    """Immutable observation set; no ingestion, online updates, or hidden learning."""

    observations: tuple[QualityObservation, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.observations, tuple) or any(
            not isinstance(x, QualityObservation) for x in self.observations
        ):
            raise EvaluationError("invalid_evaluation_book")
        ids = [(x.route, x.task_id, x.catalogue_revision) for x in self.observations]
        if len(ids) != len(set(ids)):
            raise EvaluationError("duplicate_evaluation_record")

    def lookup(
        self,
        route: RouteIdentity,
        *,
        task_id: str,
        catalogue_revision: str,
    ) -> QualityObservation | None:
        for observation in self.observations:
            if (
                observation.route == route
                and observation.task_id == task_id
                and observation.catalogue_revision == catalogue_revision
            ):
                return observation
        return None
