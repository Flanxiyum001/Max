#!/usr/bin/env python3
"""Single-agent vs. supervised multi-agent benchmark for MAX.

Compares three configurations on the same three workloads, in one process,
with the mock provider — no network, no API keys, no model credits:

  A  single-agent MAX        (existing `agent.ask` path, delegation declined)
  B  MAX + one specialist    (researcher only)
  C  MAX + parallel workers  (researcher + coder + tester + reviewer, cap 2)

Workloads:
  simple     a short question — delegation must be *skipped* (measures overhead)
  research   repository investigation
  coding     an edit directive: isolated worktree, real pytest run, review

Reported metrics are measured, not estimated: wall-clock ms, task counts,
worker tool calls, test outcomes, diff produced (yes/no), peak RSS.

Usage:
  .venv/bin/python benchmarks/orch_bench.py [--iterations 3] [--json out.json]
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

SIMPLE = "what is a python decorator?"
RESEARCH = "investigate this repository and explain how the add() function is implemented"
EDIT_JSON = json.dumps(
    {
        "edits": [
            {
                "path": "app.py",
                "content": "def add(a, b):\n    \"\"\"Return the sum.\"\"\"\n    return a + b\n",
            }
        ],
        "test_argv": ["python", "-m", "pytest", "-q"],
    }
)
CODING = "inspect this repository, fix the failing test and produce a diff max:edit " + EDIT_JSON


def fixture_repo(root: Path) -> Path:
    repo = root / "app"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (repo / "test_app.py").write_text(
        "from app import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    (repo / "README.md").write_text("# fixture app\n", encoding="utf-8")
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(root),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    for args in (
        ["init", "-q"],
        ["config", "user.email", "max@example.invalid"],
        ["config", "user.name", "MAX Bench"],
        ["config", "commit.gpgsign", "false"],
        ["add", "-A"],
        ["commit", "-q", "-m", "baseline"],
    ):
        subprocess.run(["git", *args], cwd=str(repo), env=env, check=True)
    return repo


def build(config, repo: Path):
    from max.core.factory import build_agent
    from max.orchestration import build_supervisor
    from max.providers import MockProvider

    agent = build_agent(config, provider=MockProvider())
    supervisor = build_supervisor(config, agent=agent)
    return agent, supervisor


def run_configuration(name: str, prompt: str, config, repo: Path) -> dict:
    """name: A (single agent) | B (one worker) | C (parallel workers) |
    D (same plan as C but concurrency forced to 1, i.e. serial workers)."""
    agent, supervisor = build(config, repo)
    started = time.perf_counter()
    if name == "A":
        reply = agent.ask(prompt, session_id="bench-single")
        result = {
            "reply_chars": len(reply.text),
            "delegated": False,
            "tasks": 0,
            "tool_calls": 0,
            "diff": False,
            "tests": "none",
        }
    else:
        if name == "B":
            tasks = supervisor.plan(RESEARCH, owner_id=1, forced=True)
            tasks = [t for t in tasks if t.profile == "researcher"] or tasks[:1]
        else:
            tasks = supervisor.plan(CODING, owner_id=1, forced=True)
        if not tasks:
            raise SystemExit(f"no plan produced for configuration {name}")
        report = supervisor.execute(tasks)
        result = {
            "reply_chars": len(supervisor.render(report)),
            "delegated": True,
            "tasks": len(tasks),
            "tool_calls": sum(int(t.resources.get("calls", 0)) for t in tasks),
            "diff": any(t.result and t.result.diff for t in tasks),
            "tests": next(
                (
                    t.result.tests_run[0]["outcome"]
                    for t in tasks
                    if t.result and t.result.tests_run
                ),
                "none",
            ),
            "completed": report.completed,
            "failed": report.failed,
            "conflicts": len(report.conflicts),
        }
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    peak_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return {            "config": name,
            "workload": prompt.split(" max:edit")[0][:44],
        "wall_ms": round(elapsed_ms, 1),
        "peak_rss_kb": peak_kb,
        **result,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=1)
    parser.add_argument("--json", type=str, default="")
    args = parser.parse_args()

    os.environ.setdefault("MAX_PROVIDER", "mock")
    venv_bin = str(Path(sys.executable).parent)
    os.environ["PATH"] = venv_bin + os.pathsep + os.environ.get("PATH", "")

    root = Path(tempfile.mkdtemp(prefix="max-orch-bench-"))
    try:
        repo = fixture_repo(root)

        def config_for(tag: str):
            from max.config import Config

            os.environ["MAX_DATA_DIR"] = str(root / f"var-{tag}")
            os.environ["MAX_WORKSPACE"] = str(root / f"workspace-{tag}")
            os.environ["MAX_SKILLS_DIRS"] = str(root / f"skills-{tag}")
            os.environ["MAX_WORKER_ENABLE"] = "1"
            os.environ["MAX_WORKER_REPOS"] = str(repo)
            os.environ["MAX_ORCH_ENABLE"] = "1"
            os.environ["MAX_ORCH_EXEC"] = "1"
            os.environ["MAX_ORCH_MAX_WORKERS"] = os.environ.get("BENCH_MAX_WORKERS", "2")
            cfg = Config.from_env(repo_root=root, apply_dotenv=False)
            cfg.ensure_dirs()
            return cfg

        rows = []
        for iteration in range(args.iterations):
            for label, prompt in (
                ("simple", SIMPLE),
                ("research", RESEARCH),
                ("coding", CODING),
            ):
                for config_name in ("A", "B", "C", "D"):
                    if label == "simple" and config_name in {"B", "C", "D"}:
                        # delegation must be skipped for simple requests
                        cfg = config_for(f"{config_name}-{iteration}")
                        agent, supervisor = build(cfg, repo)
                        t0 = time.perf_counter()
                        declined = supervisor.handle(prompt, owner_id=1) is None
                        ms = (time.perf_counter() - t0) * 1000.0
                        rows.append(
                            {
                                "config": config_name + "+decline",
                                "workload": "simple",
                                "wall_ms": round(ms, 1),
                                "peak_rss_kb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                                "reply_chars": 0,
                                "delegated": not declined,
                                "tasks": 0,
                                "tool_calls": 0,
                                "diff": False,
                                "tests": "none",
                            }
                        )
                        continue
                    if label == "research" and config_name in {"C", "D"}:
                        continue  # C/D are exercised on the coding workload
                    if label == "coding" and config_name == "B":
                        continue
                    cfg = config_for(f"{config_name}-{iteration}-{label}")
                    if config_name == "D":
                        os.environ["BENCH_MAX_WORKERS"] = "1"
                    rows.append(run_configuration(config_name, prompt, cfg, repo))
                    os.environ["BENCH_MAX_WORKERS"] = "2"

        payload = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "provider": "mock (no network, no model credits)",
            "iterations": args.iterations,
            "rows": rows,
        }
        out = args.json or str(
            REPO_ROOT / "benchmarks" / "results" / f"orch-{payload['generated_at'].replace(':', '')}.json"
        )
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(json.dumps(payload, indent=2), encoding="utf-8")

        header = f"{'config':<14} {'workload':<46} {'wall ms':>9} {'tasks':>6} {'calls':>6} {'diff':>5} {'tests':>7} {'rss KB':>8}"
        print(header)
        print("-" * len(header))
        for row in rows:
            print(
                f"{row['config']:<14} {row['workload']:<46} {row['wall_ms']:>9.1f} "
                f"{row['tasks']:>6} {row['tool_calls']:>6} {str(row['diff']):>5} "
                f"{row['tests']:>7} {row['peak_rss_kb']:>8}"
            )
        print(f"\nresults: {out}")
        return 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
