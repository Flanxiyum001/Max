"""Self-healing supervisor: a separate process that runs MAX and restarts it.

The supervisor owns no MAX state and never calls a model. It starts one child
command, watches it (process exit plus an optional HTTP health URL), and
restarts it with exponential backoff. A crash loop (too many restarts inside a
window) stops restarting and writes an alert file instead of looping forever.

    python -m max_core.supervisor --health http://127.0.0.1:8765/health -- python -m max_core
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path


@dataclass(slots=True)
class SupervisorPolicy:
    max_restarts: int = 5  # within window_seconds, then stop (crash loop)
    window_seconds: float = 600.0
    backoff_initial: float = 2.0
    backoff_max: float = 60.0
    health_interval: float = 15.0
    health_timeout: float = 5.0
    health_failures: int = 3  # consecutive failed checks = hung, restart
    startup_grace: float = 45.0  # no health checks right after a start


@dataclass(slots=True)
class SupervisorState:
    restarts: deque[float] = field(default_factory=deque)
    total_restarts: int = 0
    stopped_reason: str | None = None


def http_health(url: str, timeout: float) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
            return bool(200 <= response.status < 300)
    except Exception:
        return False


class Supervisor:
    def __init__(
        self,
        command: Sequence[str],
        *,
        policy: SupervisorPolicy | None = None,
        health_url: str | None = None,
        log_path: Path | None = None,
        spawn: Callable[[Sequence[str]], subprocess.Popen[bytes]] | None = None,
        health: Callable[[str, float], bool] = http_health,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not command:
            raise ValueError("empty_command")
        self.command = list(command)
        self.policy = policy or SupervisorPolicy()
        self.health_url = health_url
        self.log_path = log_path
        self._spawn = spawn or (lambda cmd: subprocess.Popen(list(cmd)))  # noqa: S603
        self._health = health
        self._clock = clock
        self._sleep = sleep
        self.state = SupervisorState()
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def log(self, event: str, **detail: object) -> None:
        line = json.dumps(
            {"at": datetime.now(UTC).isoformat(timespec="seconds"), "event": event, **detail}
        )
        print(f"[max-supervisor] {line}", file=sys.stderr, flush=True)
        if self.log_path is not None:
            try:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                with self.log_path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError:
                pass

    def _crash_loop(self) -> bool:
        now = self._clock()
        window = self.state.restarts
        while window and now - window[0] > self.policy.window_seconds:
            window.popleft()
        return len(window) >= self.policy.max_restarts

    def _watch(self, child: subprocess.Popen[bytes]) -> str:
        """Block until the child exits or is judged hung; return the reason."""
        started = self._clock()
        failures = 0
        while not self._stop:
            code = child.poll()
            if code is not None:
                return "clean_exit" if code == 0 else f"exit_{code}"
            if self.health_url and self._clock() - started >= self.policy.startup_grace:
                if self._health(self.health_url, self.policy.health_timeout):
                    failures = 0
                else:
                    failures += 1
                    if failures >= self.policy.health_failures:
                        return "health_failed"
            self._sleep(self.policy.health_interval if self.health_url else 1.0)
        return "stopped"

    @staticmethod
    def _terminate(child: subprocess.Popen[bytes]) -> None:
        if child.poll() is not None:
            return
        child.terminate()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=10)

    def run(self) -> int:
        backoff = self.policy.backoff_initial
        while not self._stop:
            self.log("start", command=self.command[:1], restarts=self.state.total_restarts)
            child = self._spawn(self.command)
            reason = self._watch(child)
            self._terminate(child)
            if reason in ("clean_exit", "stopped"):
                self.log(reason)
                return 0
            self.log("crashed", reason=reason)
            if self._crash_loop():
                self.state.stopped_reason = "crash_loop"
                self.log("gave_up", reason="crash_loop", restarts=self.state.total_restarts)
                return 1
            self.state.restarts.append(self._clock())
            self.state.total_restarts += 1
            self._sleep(backoff)
            backoff = min(backoff * 2, self.policy.backoff_max)
        return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="max-supervisor")
    parser.add_argument("--health", help="HTTP health URL to poll")
    parser.add_argument("--log", type=Path, help="JSON-lines log file")
    parser.add_argument("--max-restarts", type=int, default=5)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = [c for c in args.command if c != "--"] or [sys.executable, "-m", "max_core"]
    policy = SupervisorPolicy(max_restarts=max(1, args.max_restarts))
    supervisor = Supervisor(command, policy=policy, health_url=args.health, log_path=args.log)
    try:
        return supervisor.run()
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
