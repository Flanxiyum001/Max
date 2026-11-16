"""Fail-closed signal gate for hardware barge-in (not an AEC/VAD engine)."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite


@dataclass(slots=True)
class BargeInGate:
    """Requires independently verified echo suppression and consecutive VAD frames.

    The actual AEC and VAD implementations belong in device-local adapters.
    This gate must never treat playback echo as proof of the owner's speech.
    """

    minimum_voice_frames: int = 3
    threshold: float = 0.7
    _consecutive: int = 0

    def __post_init__(self) -> None:
        if (
            self.minimum_voice_frames < 1
            or not isfinite(self.threshold)
            or not 0 < self.threshold <= 1
        ):
            raise ValueError("invalid_barge_in_configuration")

    def observe(
        self,
        vad_probability: float,
        *,
        microphone_enabled: bool,
        playback_active: bool,
        echo_suppression_verified: bool,
    ) -> bool:
        if (
            not microphone_enabled
            or not playback_active
            or not echo_suppression_verified
            or not isfinite(vad_probability)
            or not 0 <= vad_probability <= 1
        ):
            self._consecutive = 0
            return False
        self._consecutive = self._consecutive + 1 if vad_probability >= self.threshold else 0
        if self._consecutive >= self.minimum_voice_frames:
            self._consecutive = 0
            return True
        return False

    def reset(self) -> None:
        self._consecutive = 0
