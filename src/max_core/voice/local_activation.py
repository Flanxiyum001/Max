"""On-device voice activation policy; no microphone, model or network I/O.

Adapters may pass local wake-word and VAD scores, but only the client OS can
authorize microphone capture. Neither detector scores nor model text grant tools.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from math import isfinite


class ActivationError(ValueError):
    """Sanitized local voice session policy error."""


class ActivationMode(StrEnum):
    PUSH_TO_TALK = "push_to_talk"
    WAKE_WORD = "wake_word"


def _clock(now_ms: int) -> None:
    if type(now_ms) is not int or now_ms < 0:
        raise ActivationError("invalid_monotonic_clock")


@dataclass(slots=True)
class LocalActivationGate:
    """Opt-in, foreground-only trigger with immediate revocation and expiry.

    This receives confidence scores from *local* detectors. It does not start
    capture, process frames or know what was said. The host must verify that
    wake-word and VAD inference is local, not trust a model/provider assertion.
    """

    mode: ActivationMode
    wake_threshold: float = 0.8
    vad_threshold: float = 0.65
    consecutive_frames: int = 3
    active_window_ms: int = 15_000
    _armed: bool = field(default=False, init=False, repr=False)
    _active_until_ms: int | None = field(default=None, init=False, repr=False)
    _consecutive: int = field(default=0, init=False, repr=False)
    _last_ms: int | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.mode, ActivationMode)
            or not isfinite(self.wake_threshold)
            or not isfinite(self.vad_threshold)
            or not 0 < self.wake_threshold <= 1
            or not 0 < self.vad_threshold <= 1
            or type(self.consecutive_frames) is not int
            or not 1 <= self.consecutive_frames <= 10
            or type(self.active_window_ms) is not int
            or not 1000 <= self.active_window_ms <= 30_000
        ):
            raise ActivationError("invalid_activation_configuration")

    def arm(
        self,
        *,
        owner_opted_in: bool,
        os_microphone_permission: bool,
        foreground: bool,
    ) -> None:
        self.revoke()
        if (
            owner_opted_in is not True
            or os_microphone_permission is not True
            or foreground is not True
        ):
            raise ActivationError("microphone_or_consent_denied")
        self._armed = True

    def _advance_clock(self, now_ms: int) -> None:
        _clock(now_ms)
        if self._last_ms is not None and now_ms < self._last_ms:
            self.revoke()
            raise ActivationError("monotonic_clock_regressed")
        self._last_ms = now_ms

    def push_to_talk(self, *, direct_user_gesture: bool, now_ms: int) -> bool:
        self._advance_clock(now_ms)
        if self.mode != ActivationMode.PUSH_TO_TALK or not self._armed:
            return False
        if direct_user_gesture is not True:
            return False
        self._active_until_ms = now_ms + self.active_window_ms
        return True

    def observe_local_scores(
        self,
        *,
        wake_probability: float,
        vad_probability: float,
        local_inference_verified: bool,
        now_ms: int,
    ) -> bool:
        self._advance_clock(now_ms)
        if (
            not self._armed
            or self.mode != ActivationMode.WAKE_WORD
            or local_inference_verified is not True
            or not isfinite(wake_probability)
            or not isfinite(vad_probability)
            or not 0 <= wake_probability <= 1
            or not 0 <= vad_probability <= 1
        ):
            self._consecutive = 0
            return False
        if (
            wake_probability >= self.wake_threshold
            and vad_probability >= self.vad_threshold
        ):
            self._consecutive += 1
        else:
            self._consecutive = 0
        if self._consecutive >= self.consecutive_frames:
            self._consecutive = 0
            self._active_until_ms = now_ms + self.active_window_ms
            return True
        return False

    def streaming_allowed(self, *, now_ms: int) -> bool:
        self._advance_clock(now_ms)
        return bool(
            self._armed
            and self._active_until_ms is not None
            and now_ms <= self._active_until_ms
        )

    def stop_turn(self) -> None:
        self._active_until_ms = None
        self._consecutive = 0

    def revoke(self) -> None:
        """On background, game/quiet mode, permission loss or owner mic-off."""
        self._armed = False
        self._active_until_ms = None
        self._consecutive = 0
        self._last_ms = None
