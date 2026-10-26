"""Repeatable resource/latency benchmarks for the MAX configurations.

Configurations compared (see docs/BENCHMARKS.md):

  A  MAX Core only (skills registry disabled)
  B  MAX Core + OpenClaw gateway integration   (needs a running gateway)
  C  lightweight alternative (nanobot)         (needs `nanobot` installed)
  D  MAX with skills + evaluation improvements (full configuration)

Each MAX configuration runs in its own subprocess so memory numbers are
isolated. Everything uses the mock provider — no network, no API keys.
"""

from __future__ import annotations

import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS_ROOT = REPO_ROOT / "benchmarks" / "results"

CONFIG_DESCRIPTIONS = {
    "A": "MAX Core (skills disabled, no MCP extra work)",
    "B": "MAX Core + OpenClaw gateway",
    "C": "nanobot (HKUDS) as alternative assistant",
    "D": "MAX full: skills + evaluation + self-improvement",
}


# ------------------------------------------------------------------- worker
def _worker(config_name: str, iterations: int) -> dict:
    """Runs inside a fresh interpreter for one MAX configuration."""
    from .config import Config
    from .core.factory import build_agent

    if config_name == "A":
        # Baseline "MAX Core": agent loop, providers, tools, sessions only.
        os.environ["MAX_ENABLE_SKILLS"] = "0"
        os.environ["MAX_ENABLE_IMPROVE"] = "0"
        os.environ["MAX_ENABLE_MCP"] = "0"
    else:
        os.environ.setdefault("MAX_ENABLE_SKILLS", "1")
        os.environ.setdefault("MAX_ENABLE_IMPROVE", "1")
        os.environ.setdefault("MAX_ENABLE_MCP", "1")

    t0 = time.monotonic()
    config = Config.from_env()
    agent = build_agent(config)
    startup_ms = (time.monotonic() - t0) * 1000.0

    latencies = []
    for i in range(iterations):
        reply = agent.ask(f"ping {i}", session_id=f"bench-{config_name}")
        latencies.append(reply.latency_ms)
        if not reply.ok:
            break

    # evaluation suite (same suite for A and D; skill scenarios need D)
    from .eval import Harness

    harness = Harness(work_root=config.data_dir / "eval-work")
    summary = harness.run_all(label=f"bench-{config_name}")

    time.sleep(0.5)  # settle so idle RSS is representative

    return {
        "config": config_name,
        "status": "ok",
        "startup_ms": round(startup_ms, 1),
        "latency_ms_median": round(statistics.median(latencies), 2) if latencies else None,
        "latency_ms_p95": round(sorted(latencies)[int(0.95 * (len(latencies) - 1))], 2) if latencies else None,
        "iterations": len(latencies),
        "eval_pass_rate": summary["pass_rate"],
        "eval_total": summary["total"],
        "eval_executed": summary["executed"],
        "eval_skipped": summary["skipped"],
        "eval_failed": summary["failed"],
        "idle_rss_kb": current_rss_kb(),
        "peak_rss_kb": peak_rss_kb(),
        "eval_duration_s": summary["duration_s"],
    }


def current_rss_kb() -> int:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except OSError:
        pass
    return 0


def peak_rss_kb() -> int:
    import resource

    self_peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss  # KiB on Linux
    children_peak = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    return int(max(self_peak, children_peak))


# ----------------------------------------------------------- subprocess run
def _run_worker(config_name: str, iterations: int, timeout: int = 300) -> dict:
    env = dict(os.environ)
    env["MAX_PROVIDER"] = "mock"
    env["MAX_DATA_DIR"] = str(REPO_ROOT / "var" / "bench" / config_name)
    cmd = [sys.executable, "-m", "max.bench", "--worker", config_name, "--iterations", str(iterations)]
    started = time.monotonic()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env, cwd=str(REPO_ROOT))
    except subprocess.TimeoutExpired:
        return {"config": config_name, "status": "failed", "reason": "worker timed out"}
    wall = time.monotonic() - started
    if proc.returncode != 0:
        return {
            "config": config_name,
            "status": "failed",
            "reason": f"worker exit {proc.returncode}",
            "stderr": (proc.stderr or "")[-600:],
        }
    try:
        # worker prints one JSON object on the last line
        line = [l for l in proc.stdout.splitlines() if l.strip().startswith("{")][-1]
        data = json.loads(line)
    except (ValueError, IndexError):
        return {"config": config_name, "status": "failed", "reason": f"unreadable worker output: {proc.stdout[-300:]}"}
    data["wall_s"] = round(wall, 2)
    return data


def _cold_start_ms(trials: int = 3) -> float:
    times = []
    for _ in range(trials):
        started = time.monotonic()
        proc = subprocess.run(
            [sys.executable, "-m", "max", "health", "--json"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            env={**os.environ, "MAX_PROVIDER": "mock"},
        )
        times.append((time.monotonic() - started) * 1000.0)
        if proc.returncode not in (0, 1):
            break
    return round(statistics.median(times), 1) if times else -1.0


# ------------------------------------------------------------ other configs
def _bench_openclaw() -> dict:
    from .config import Config

    config = Config.from_env()
    url = config.openclaw_url
    if not url:
        return {
            "config": "B",
            "status": "unavailable",
            "reason": "no gateway configured (MAX_OPENCLAW_URL empty); "
            "run `max bench --configs B` after starting the isolated OpenClaw gateway",
        }
    import urllib.error
    import urllib.request

    started = time.monotonic()
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=5) as resp:
            body = resp.read(2048)
            code = resp.status
    except (urllib.error.URLError, OSError) as exc:
        return {"config": "B", "status": "unavailable", "reason": f"gateway not reachable: {exc}"}
    latency = round((time.monotonic() - started) * 1000, 1)
    return {
        "config": "B",
        "status": "ok",
        "gateway_health_status": code,
        "gateway_health_body": body.decode("utf-8", errors="replace")[:300],
        "health_latency_ms": latency,
        "note": "gateway health probe only; conversation probes require a configured model provider",
    }


def _bench_nanobot() -> dict:
    exe = shutil.which("nanobot")
    if not exe:
        return {
            "config": "C",
            "status": "unavailable",
            "reason": "nanobot CLI not installed (`uv tool install nanobot-ai`)",
        }
    started = time.monotonic()
    try:
        proc = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=60)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {"config": "C", "status": "failed", "reason": str(exc)}
    version_ms = round((time.monotonic() - started) * 1000, 1)
    return {
        "config": "C",
        "status": "ok",
        "version_output": (proc.stdout or proc.stderr).strip()[:200],
        "version_ms": version_ms,
        "note": "CLI availability + version probe; full parity suite not applicable to nanobot's own skill model",
    }


# ------------------------------------------------------------- orchestrator
def run_benchmarks(configs: list[str] | None = None, iterations: int = 20) -> dict:
    configs = [c.strip().upper() for c in (configs or ["A", "D"])]
    rows = []
    for name in configs:
        if name in {"A", "D"}:
            rows.append(_run_worker(name, iterations))
        elif name == "B":
            rows.append(_bench_openclaw())
        elif name == "C":
            rows.append(_bench_nanobot())
        else:
            rows.append({"config": name, "status": "unknown", "reason": "unknown configuration"})

    summary = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "python": sys.version.split()[0],
        "cpu_count": os.cpu_count(),
        "cold_start_ms": _cold_start_ms(),
        "iterations": iterations,
        "configs": {c: CONFIG_DESCRIPTIONS.get(c, "") for c in configs},
        "rows": rows,
    }
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out_dir = RESULTS_ROOT / stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "results.json"
    out_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    summary["out_path"] = str(out_path)
    return summary


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="max bench")
    parser.add_argument("--worker", default=None, help="internal: run one worker config")
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--configs", default="A,D")
    args = parser.parse_args(argv)
    if args.worker:
        print(json.dumps(_worker(args.worker, args.iterations), sort_keys=True))
        return 0
    summary = run_benchmarks(args.configs.split(","), iterations=args.iterations)
    for row in summary["rows"]:
        print(json.dumps(row, sort_keys=True))
    print(f"report: {summary['out_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
