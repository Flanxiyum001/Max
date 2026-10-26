"""Rate limiting, failure backoff and message chunking (fake clocks)."""

from max.discord import FailureBackoff, RateLimiter, chunk_message


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, seconds: float):
        self.t += seconds


def test_per_key_cooldown():
    clock = FakeClock()
    rl = RateLimiter(cooldown_s=5, burst=100, clock=clock)
    assert rl.allow("ch:1") is True
    assert rl.allow("ch:1") is False          # same channel, inside cooldown
    assert rl.allow("ch:2") is True           # different channel unaffected
    clock.advance(5.1)
    assert rl.allow("ch:1") is True           # after cooldown


def test_global_burst_cap():
    clock = FakeClock()
    rl = RateLimiter(cooldown_s=0, burst=3, window_s=10, clock=clock)
    keys = [f"ch:{i}" for i in range(10)]
    allowed = [k for k in keys if rl.allow(k)]
    assert len(allowed) == 3
    clock.advance(11)  # window slides → budget restored
    assert rl.allow("ch:99") is True


def test_retry_after_counts_down():
    clock = FakeClock()
    rl = RateLimiter(cooldown_s=10, burst=100, clock=clock)
    rl.allow("ch:1")
    assert 9.0 < rl.retry_after("ch:1") <= 10.0
    clock.advance(10)
    assert rl.retry_after("ch:1") == 0.0


def test_backoff_grows_and_resets():
    clock = FakeClock()
    backoff = FailureBackoff(base_s=30, max_s=120, clock=clock)
    assert backoff.suppressed() is False
    span1 = backoff.note_failure()
    assert span1 == 30 and backoff.suppressed()
    clock.advance(31)
    assert not backoff.suppressed()
    span2 = backoff.note_failure()
    assert span2 == 60
    clock.advance(61)
    span3 = backoff.note_failure()
    assert span3 == 120           # capped
    backoff.note_success()
    assert not backoff.suppressed()
    assert backoff.note_failure() == 30  # reset after success


def test_chunk_respects_limit_and_keeps_lines():
    text = "\n".join(f"line {i} " + "x" * 40 for i in range(50))
    chunks = chunk_message(text, limit=200)
    assert len(chunks) > 1
    assert all(len(c) <= 200 for c in chunks)
    # content preserved in order
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


def test_chunk_handles_oversized_single_line():
    chunks = chunk_message("y" * 1000, limit=300)
    assert all(len(c) <= 300 for c in chunks)
    assert "".join(chunks) == "y" * 1000


def test_chunk_empty_input():
    assert chunk_message("") == []
    assert chunk_message(None) == []  # type: ignore[arg-type]