from __future__ import annotations

import pytest

from max_core.voice import BargeInGate


def test_requires_mic_playback_aec_and_three_consecutive_frames() -> None:
    gate = BargeInGate()

    def observe(score: float, **kwargs: bool) -> bool:
        return gate.observe(
            score,
            microphone_enabled=kwargs.get("mic", True),
            playback_active=kwargs.get("playing", True),
            echo_suppression_verified=kwargs.get("aec", True),
        )

    assert not observe(0.99, aec=False)
    assert not observe(0.9)
    assert not observe(0.9)
    assert observe(0.9)
    assert not observe(0.9, mic=False)
    assert not observe(0.9)
    assert not observe(0.4)
    assert not observe(0.9)


def test_invalid_signal_and_settings_fail_closed() -> None:
    with pytest.raises(ValueError):
        BargeInGate(minimum_voice_frames=0)
    with pytest.raises(ValueError):
        BargeInGate(threshold=float("nan"))
    gate = BargeInGate(minimum_voice_frames=1)
    assert not gate.observe(
        float("nan"), microphone_enabled=True, playback_active=True, echo_suppression_verified=True
    )
