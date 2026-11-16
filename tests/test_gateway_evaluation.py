"""Offline and credential-free quality-evidence contract tests."""

from __future__ import annotations

from decimal import Decimal

import pytest

from max_core.gateway.catalogue import EvidenceSource, ModelIdentity, RouteIdentity
from max_core.gateway.contracts import Dialect
from max_core.gateway.evaluation import (
    EvaluationError,
    OfflineEvaluationBook,
    QualityObservation,
)


def route(provider: str = "a") -> RouteIdentity:
    return RouteIdentity(
        ModelIdentity("gateway", provider, "model-a"),
        "route-" + provider,
        Dialect.OPENAI_CHAT,
    )


def measurement(
    *,
    quality: Decimal = Decimal("0.80"),
    samples: int = 50,
    latency: int = 1000,
    revision: str = "reviewed-revision",
    source: EvidenceSource = EvidenceSource.OWNER_REVIEWED,
) -> QualityObservation:
    return QualityObservation(
        route("a"),
        "coding",
        revision,
        quality,
        samples,
        latency,
        source,
    )


def test_reviewed_evidence_is_stable_immutable_and_qualified() -> None:
    item = measurement()
    book = OfflineEvaluationBook((item,))
    assert book.lookup(route("a"), task_id="coding", catalogue_revision="reviewed-revision") == item
    assert book.lookup(route("b"), task_id="coding", catalogue_revision="reviewed-revision") is None
    assert book.lookup(route("a"), task_id="chat", catalogue_revision="reviewed-revision") is None
    assert book.lookup(route("a"), task_id="coding", catalogue_revision="new-revision") is None
    assert not hasattr(book, "learn")
    assert not hasattr(book, "record_prompt")


@pytest.mark.parametrize(
    ("quality", "samples", "latency"),
    [
        (Decimal("NaN"), 50, 1000),
        (Decimal("Infinity"), 50, 1000),
        (Decimal("-0.01"), 50, 1000),
        (Decimal("1.01"), 50, 1000),
        (Decimal("0.5"), 0, 1000),
        (Decimal("0.5"), 4, 1000),
        (Decimal("0.5"), 50, 0),
        (Decimal("0.5"), 50, -1),
        (Decimal("0.5"), 50, 3_600_001),
    ],
)
def test_invalid_metrics_are_rejected(quality: Decimal, samples: int, latency: int) -> None:
    with pytest.raises(EvaluationError):
        measurement(quality=quality, samples=samples, latency=latency)


@pytest.mark.parametrize(
    "source",
    [EvidenceSource.UNKNOWN, EvidenceSource.GATEWAY_REPORTED],
)
def test_unreviewed_metrics_cannot_bias_model_ranking(source: EvidenceSource) -> None:
    with pytest.raises(EvaluationError, match="unverified_evaluation_provenance"):
        measurement(source=source)


def test_duplicate_metric_for_route_task_revision_rejected() -> None:
    item = measurement()
    with pytest.raises(EvaluationError, match="duplicate_evaluation_record"):
        OfflineEvaluationBook((item, item))


def test_distinct_revisions_can_be_retained_but_not_used_interchangeably() -> None:
    book = OfflineEvaluationBook((measurement(), measurement(revision="future-review")))
    assert len(book.observations) == 2
    assert book.lookup(route(), task_id="coding", catalogue_revision="future-review") is not None
