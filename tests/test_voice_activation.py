"""Pure voice activation tests: no audio captured or sent."""

from __future__ import annotations

import pytest

from max_core.voice.local_activation import (
    ActivationError,
    ActivationMode,
    LocalActivationGate,
)


def arm(gate: LocalActivationGate) -> None:
    gate.arm(owner_opted_in=True, os_microphone_permission=True, foreground=True)


@pytest.mark.parametrize(
    "flags",
    [
        {"owner_opted_in": False, "os_microphone_permission": True, "foreground": True},
        {"owner_opted_in": True, "os_microphone_permission": False, "foreground": True},
        {"owner_opted_in": True, "os_microphone_permission": True, "foreground": False},
    ],
)
def test_foreground_consent_and_os_permission_required(flags: dict[str, bool]) -> None:
    gate = LocalActivationGate(ActivationMode.WAKE_WORD)
    with pytest.raises(ActivationError, match="microphone_or_consent_denied"):
        gate.arm(**flags)
    assert not gate.streaming_allowed(now_ms=1)


def test_local_wakeword_and_vad_must_recur_with_verified_local_inference() -> None:
    gate = LocalActivationGate(ActivationMode.WAKE_WORD)
    arm(gate)
    for now in (100, 200):
        assert not gate.observe_local_scores(
            wake_probability=0.9,
            vad_probability=0.9,
            local_inference_verified=True,
            now_ms=now,
        )
        assert not gate.streaming_allowed(now_ms=now)
    assert gate.observe_local_scores(
        wake_probability=0.9,
        vad_probability=0.9,
        local_inference_verified=True,
        now_ms=300,
    )
    assert gate.streaming_allowed(now_ms=301)
    assert gate.streaming_allowed(now_ms=15_300)
    assert not gate.streaming_allowed(now_ms=15_301)


def test_spoofed_remote_scores_and_nonvoice_cannot_activate() -> None:
    gate = LocalActivationGate(ActivationMode.WAKE_WORD, consecutive_frames=2)
    arm(gate)
    assert not gate.observe_local_scores(
        wake_probability=1,
        vad_probability=1,
        local_inference_verified=False,
        now_ms=1,
    )
    assert not gate.observe_local_scores(
        wake_probability=1,
        vad_probability=0.1,
        local_inference_verified=True,
        now_ms=2,
    )
    assert not gate.observe_local_scores(
        wake_probability=1,
        vad_probability=1,
        local_inference_verified=True,
        now_ms=3,
    )
    assert not gate.observe_local_scores(
        wake_probability=float("nan"),
        vad_probability=1,
        local_inference_verified=True,
        now_ms=4,
    )
    assert not gate.streaming_allowed(now_ms=4)


def test_push_to_talk_requires_real_gesture_and_stops_on_revoke() -> None:
    gate = LocalActivationGate(ActivationMode.PUSH_TO_TALK)
    arm(gate)
    assert not gate.push_to_talk(direct_user_gesture=False, now_ms=10)
    assert not gate.streaming_allowed(now_ms=10)
    assert gate.push_to_talk(direct_user_gesture=True, now_ms=11)
    assert gate.streaming_allowed(now_ms=12)
    gate.stop_turn()
    assert not gate.streaming_allowed(now_ms=13)
    gate.push_to_talk(direct_user_gesture=True, now_ms=14)
    gate.revoke()
    assert not gate.streaming_allowed(now_ms=15)
    assert not gate.push_to_talk(direct_user_gesture=True, now_ms=16)


def test_foreground_displacement_and_clock_regression_fail_closed() -> None:
    gate = LocalActivationGate(ActivationMode.PUSH_TO_TALK)
    arm(gate)
    gate.push_to_talk(direct_user_gesture=True, now_ms=200)
    with pytest.raises(ActivationError, match="monotonic_clock_regressed"):
        gate.streaming_allowed(now_ms=199)
    assert not gate.streaming_allowed(now_ms=300)
    arm(gate)
    gate.push_to_talk(direct_user_gesture=True, now_ms=400)
    with pytest.raises(ActivationError, match="microphone_or_consent_denied"):
        gate.arm(owner_opted_in=True, os_microphone_permission=True, foreground=False)
    assert not gate.streaming_allowed(now_ms=401)


@pytest.mark.parametrize("mode", [ActivationMode.PUSH_TO_TALK, ActivationMode.WAKE_WORD])
def test_armed_gate_starts_dormant(mode: ActivationMode) -> None:
    gate = LocalActivationGate(mode)
    assert not gate.streaming_allowed(now_ms=0)
    arm(gate)
    assert not gate.streaming_allowed(now_ms=0)


def test_invalid_configuration_denied() -> None:
    with pytest.raises(ActivationError, match="invalid_activation_configuration"):
        LocalActivationGate(ActivationMode.WAKE_WORD, consecutive_frames=0)
    with pytest.raises(ActivationError, match="invalid_activation_configuration"):
        LocalActivationGate(ActivationMode.WAKE_WORD, wake_threshold=float("nan"))
    with pytest.raises(ActivationError, match="invalid_monotonic_clock"):
        LocalActivationGate(ActivationMode.WAKE_WORD).streaming_allowed(now_ms=-1)
