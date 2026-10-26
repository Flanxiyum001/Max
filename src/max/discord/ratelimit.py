"""Rate limiting and provider-failure backoff (pure logic, injectable clock).

Two independent controls:

* ``RateLimiter``  — per-channel cooldown plus a small global token bucket,
  so bursts (or a runaway loop) cannot spam the model or Discord.
* ``FailureBackoff`` — after a provider failure (429/5xx), suppress repeats
  for a growing interval so a broken provider does not cause reply storms.
"""

from __future__ import annotations

import time
from collections import OrderedDict, deque


class RateLimiter:
    def __init__(
        self,
        cooldown_s: float = 2.0,
        burst: int = 5,
        window_s: float = 10.0,
        clock=time.monotonic,
    ):
        self.cooldown_s = max(0.0, float(cooldown_s))
        self.burst = max(1, int(burst))
        self.window_s = max(0.1, float(window_s))
        self._clock = clock
        self._last: dict[str, float] = {}
        self._hits: deque[float] = deque()

    def _prune(self, now: float) -> None:
        while self._hits and now - self._hits[0] > self.window_s:
            self._hits.popleft()

    def allow(self, key: str) -> bool:
        now = self._clock()
        # per-key cooldown
        if self.cooldown_s:
            last = self._last.get(key)
            if last is not None and now - last < self.cooldown_s:
                return False
        # global burst budget
        self._prune(now)
        if len(self._hits) >= self.burst:
            return False
        self._last[key] = now
        self._hits.append(now)
        return True

    def retry_after(self, key: str) -> float:
        now = self._clock()
        last = self._last.get(key)
        if last is None:
            return 0.0
        remaining = self.cooldown_s - (now - last)
        return max(0.0, remaining)


class FailureBackoff:
    """Exponential suppression window after provider failures."""

    def __init__(self, base_s: float = 30.0, max_s: float = 600.0, clock=time.monotonic):
        self.base_s = max(0.0, float(base_s))
        self.max_s = max(self.base_s, float(max_s))
        self._clock = clock
        self._failures = 0
        self._until = 0.0

    def note_failure(self) -> float:
        self._failures += 1
        span = min(self.base_s * (2 ** (self._failures - 1)), self.max_s)
        self._until = self._clock() + span
        return span

    def note_success(self) -> None:
        self._failures = 0
        self._until = 0.0

    def suppressed(self) -> bool:
        return self._clock() < self._until

    def seconds_left(self) -> float:
        return max(0.0, self._until - self._clock())


def chunk_message(text: str, limit: int = 1900) -> list[str]:
    """Split a reply into Discord-safe chunks, preferring line boundaries."""
    limit = max(50, int(limit))
    text = text or ""
    if not text:
        return []
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:  # pathological single line
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            chunks.append(current)
            current = line
        else:
            current += line
    if current:
        chunks.append(current)
    return chunks or [text[:limit]]