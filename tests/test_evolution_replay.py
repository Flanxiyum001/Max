"""Fully synthetic, credential-free MAX Evolution regression suite tests."""

from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from max_core.evolution_replay import (
    GoldenCase,
    GoldenReplayError,
    OfflineGoldenSuite,
    ReplayObservation,
)
from max_core.gateway.catalogue import ModelIdentity, RouteIdentity
from max_core.gateway.contracts import Dialect, GatewayResponse, ToolProposal
from max_core.gateway.task_planner import TaskKind
from tests.test_gateway import OPENAI_DATA, local, respond_json, run_complete


def route(provider: str = "local") -> RouteIdentity:
    return RouteIdentity(
        ModelIdentity("gateway", provider, "demo-chat"), "route-a", Dialect.OPENAI_CHAT
    )


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def golden(**changes: object) -> GoldenCase:
    return replace(
        GoldenCase(
            case_id="fixture-a",
            task=TaskKind.CHAT,
            route=route(),
            catalogue_revision="reviewed-v1",
            expected_text_sha256=digest("expected synthetic reply"),
            expected_tool_names=(),
            owner_reviewed=True,
        ),
        **changes,
    )


def observed(
    *,
    content: str = "expected synthetic reply",
    actual_route: RouteIdentity | None = None,
    revision: str = "reviewed-v1",
    provider: str = "local",
    tools: tuple[ToolProposal, ...] = (),
) -> ReplayObservation:
    return ReplayObservation(
        case_id="fixture-a",
        route=actual_route if actual_route is not None else route(),
        catalogue_revision=revision,
        response=GatewayResponse(
            "synthetic-request",
            provider,
            "demo-chat",
            content,
            tools,
            "stop",
            None,
            None,
        ),
    )


def test_exact_reviewed_golden_case_passes_without_source_content_in_report() -> None:
    report = OfflineGoldenSuite((golden(),)).compare((observed(),))
    assert report.all_passed
    assert report.passed == 1
    assert report.verdicts[0].failures == ()
    assert "expected synthetic reply" not in repr(report)


def test_text_route_revision_and_model_drift_fail_independently() -> None:
    report = OfflineGoldenSuite((golden(),)).compare(
        (
            observed(
                content="changed result",
                actual_route=route("another"),
                revision="another-v1",
                provider="unexpected",
            ),
        )
    )
    assert not report.all_passed
    assert report.verdicts[0].failures == (
        "route_mismatch",
        "catalogue_revision_mismatch",
        "provider_model_mismatch",
        "text_mismatch",
    )


def test_tool_regression_rejects_unauthorized_proposal_names() -> None:
    case = golden(prohibited_tool_names=("execute_shell",))
    obs = observed(tools=(ToolProposal("fake-call", "execute_shell", {}),))
    verdict = OfflineGoldenSuite((case,)).compare((obs,)).verdicts[0]
    assert verdict.failures == ("tool_names_mismatch", "prohibited_tool")


def test_missing_cases_never_count_as_success() -> None:
    report = OfflineGoldenSuite((golden(),)).compare(())
    assert report.total == 1
    assert report.passed == 0
    assert report.verdicts[0].failures == ("missing_observation",)


def test_replay_rejects_unreviewed_empty_and_malformed_cases() -> None:
    with pytest.raises(GoldenReplayError, match="owner_review_required"):
        GoldenCase(
            "fixture-a",
            TaskKind.CHAT,
            route(),
            "reviewed-v1",
            expected_tool_names=(),
        )
    with pytest.raises(GoldenReplayError, match="empty_assertions"):
        golden(expected_text_sha256=None, expected_tool_names=None)
    with pytest.raises(GoldenReplayError, match="invalid_expected_digest"):
        golden(expected_text_sha256="not-a-digest")
    with pytest.raises(GoldenReplayError, match="conflicting_tool_assertions"):
        golden(
            expected_tool_names=("execute_shell",),
            prohibited_tool_names=("execute_shell",),
        )


def test_replay_rejects_duplicate_cases_and_unknown_or_duplicate_observations() -> None:
    case = golden()
    with pytest.raises(GoldenReplayError, match="duplicate_golden_case"):
        OfflineGoldenSuite((case, case))
    suite = OfflineGoldenSuite((case,))
    with pytest.raises(GoldenReplayError, match="duplicate_observation"):
        suite.compare((observed(), observed()))
    with pytest.raises(GoldenReplayError, match="invalid_observation"):
        suite.compare((replace(observed(), case_id="unreviewed-case"),))


def test_no_untrusted_output_is_exposed_in_errors_or_repr() -> None:
    synthetic = "SYNTHETIC_PRIVATE_OUTPUT_DO_NOT_LOG"
    observation = observed(content=synthetic)
    assert synthetic not in repr(observation)
    report = OfflineGoldenSuite((golden(),)).compare((observation,))
    assert report.verdicts[0].failures == ("text_mismatch",)
    assert synthetic not in repr(report)


def test_synthetic_http_mock_normalization_can_feed_offline_golden_comparator() -> None:
    result = run_complete([local()], respond_json(OPENAI_DATA))
    suite = OfflineGoldenSuite(
        (
            golden(
                expected_text_sha256=digest("Hello"),
                expected_tool_names=(),
            ),
        )
    )
    observation = replace(observed(), response=result)
    assert suite.compare((observation,)).all_passed


def test_malformed_observations_fail_closed_with_sanitized_errors() -> None:
    suite = OfflineGoldenSuite((golden(),))
    base = observed()
    malformed = (
        replace(base, case_id=["not-a-string"]),
        replace(base, response=replace(base.response, content=chr(0xD800))),
        replace(
            base,
            response=replace(
                base.response,
                tool_proposals=(ToolProposal("fake-call", ["not-a-tool-name"], {}),),
            ),
        ),
    )
    for value in malformed:
        with pytest.raises(GoldenReplayError, match="invalid_observation"):
            suite.compare((value,))
