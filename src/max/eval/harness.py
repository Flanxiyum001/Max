"""Evaluation harness: reproducible scenario suite with recorded results.

Scenario files are YAML under ``eval/suite/``. Two runners:

* ``agent``  — build an isolated MAX agent with a scripted mock provider,
  execute user steps, then evaluate expectations,
* ``improve`` — build a temporary git repo, generate a patch from inline
  edits, run the self-improvement pipeline, evaluate its report.

Every run writes a machine-readable JSON record under ``eval/runs/``.
No network access is required; real inference is never used.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml

from ..config import Config
from ..core.factory import build_agent
from ..providers import MockProvider
from ..skills.registry import SkillError

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SUITE = REPO_ROOT / "eval" / "suite"

# Subsystem gates (set to 0 to measure a reduced configuration, e.g. bench A)
REQUIREMENT_FLAGS = {
    "skills": "MAX_ENABLE_SKILLS",
    "improve": "MAX_ENABLE_IMPROVE",
    "mcp": "MAX_ENABLE_MCP",
}


def env_flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


# ----------------------------------------------------------------- helpers
def _write_skill(skills_dir: Path, name: str, body: str, frontmatter: dict | None = None, version: str = "1.0") -> Path:
    target = skills_dir / name
    target.mkdir(parents=True, exist_ok=True)
    meta = frontmatter or {
        "name": name,
        "description": "Evaluation fixture skill.",
        "metadata": {"version": version},
    }
    fm = yaml.safe_dump(meta, sort_keys=False)
    (target / "SKILL.md").write_text(f"---\n{fm}---\n\n{body}\n", encoding="utf-8")
    return target


def _git(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {proc.stderr.strip()[:300]}")
    return proc.stdout.strip()


def _rss_kb() -> int:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except OSError:
        pass
    return 0


# ----------------------------------------------------------------- checks
def _check(check: dict, ctx: dict) -> tuple[bool, str]:
    kind = check.get("type")
    reply = ctx.get("last_reply")
    if check.get("index") is not None:
        replies = ctx.get("replies") or []
        idx = int(check["index"])
        reply = replies[idx] if 0 <= idx < len(replies) else None
    agent = ctx.get("agent")

    if kind == "reply_contains":
        ok = check["value"] in (reply.text if reply else "")
        return ok, f"expected {check['value']!r} in reply"
    if kind == "reply_not_contains":
        ok = check["value"] not in (reply.text if reply else "")
        return ok, f"expected {check['value']!r} absent from reply"
    if kind == "reply_ok":
        return bool(reply and reply.ok), "last reply should succeed"
    if kind == "reply_error":
        ok = bool(reply and reply.error == check["value"])
        return ok, f"expected error {check['value']}, got {getattr(reply, 'error', None)}"
    if kind == "tool_allowed":
        ok = any(e["tool"] == check["name"] and e.get("allowed") for e in (reply.tool_events if reply else []))
        return ok, f"expected allowed tool call {check['name']}"
    if kind == "tool_rejected":
        events = [e for e in (reply.rejected if reply else []) if e["tool"] == check["name"]]
        if not events:
            return False, f"expected rejection of {check['name']}"
        needle = check.get("reason_contains")
        if needle and needle not in str(events[0].get("reason", "")):
            return False, f"rejection reason missing {needle!r}"
        return True, f"{check['name']} rejected"
    if kind == "injection_flagged":
        return bool(reply and reply.flagged), "expected an injection flag"
    if kind == "audit_event":
        event_name = check.get("value") or check.get("event")
        events = agent.audit.events(event_name) if agent else []
        return bool(events), f"expected audit event {event_name}"
    if kind == "provider_context_contains":
        provider = ctx.get("provider")
        if not provider or not provider.requests:
            return False, "provider was never called"
        blob = json.dumps(provider.requests[-1]["messages"])
        return check["value"] in blob, f"expected {check['value']!r} in model context"
    if kind == "skill_index_contains":
        index = agent.skill_index() if agent else ""
        return check["value"] in index, f"expected {check['value']!r} in skill index"
    if kind == "skill_listed":
        registry = ctx.get("registry")
        records = {r.name: r for r in registry.discover()} if registry else {}
        rec = records.get(check["name"])
        ok = rec is not None and rec.valid and rec.safe
        return ok, f"expected skill {check['name']} discoverable and clean"
    if kind == "skill_blocked":
        registry = ctx.get("registry")
        if registry is None:
            return False, "skill registry not available in this configuration"
        try:
            registry.load(check["name"])
        except SkillError as exc:
            return True, f"blocked: {exc}"
        return False, f"skill {check['name']} should not be loadable"
    if kind == "skill_promote_refused":
        registry = ctx.get("registry")
        if registry is None or ctx.get("config") is None:
            return False, "no registry in context"
        src = ctx["config"].skills_dirs[0] / check["name"]
        if not src.is_dir():
            return False, f"skill {check['name']} not present"
        try:
            registry.stage(src)
            registry.evaluate(check["name"], {"passed": True, "checks": []})
            registry.promote(check["name"])
        except SkillError as exc:
            return True, f"promote refused: {exc}"
        return False, "promote unexpectedly succeeded"
    if kind == "file_exists":
        path = ctx["config"].workspace_dir / check["path"]
        return path.is_file(), f"expected file {check['path']}"
    if kind == "file_contains":
        path = ctx["config"].workspace_dir / check["path"]
        if not path.is_file():
            return False, f"missing file {check['path']}"
        return check["value"] in path.read_text(encoding="utf-8"), f"{check['path']} should contain value"
    if kind == "session_contains":
        from ..core.session import SessionStore

        store = SessionStore(ctx["config"].data_dir)
        blob = json.dumps(store.load(check.get("session", "default")))
        return check["value"] in blob, f"session should contain {check['value']!r}"
    if kind == "reply_count":
        return len(ctx.get("replies", [])) == check["value"], "unexpected number of replies"

    # improvement-pipeline checks
    if kind == "pipeline_status":
        report = ctx.get("report") or {}
        return report.get("status") == check["value"], f"status={report.get('status')}"
    if kind == "pipeline_verdict":
        report = ctx.get("report") or {}
        return report.get("verdict") == check["value"], f"verdict={report.get('verdict')}"
    if kind == "reason_contains":
        report = ctx.get("report") or {}
        joined = " | ".join(report.get("reasons", []))
        return check["value"] in joined, f"reasons={joined[:200]}"
    if kind == "check_state":
        report = ctx.get("report") or {}
        match = [c for c in report.get("checks", []) if c["name"] == check["name"]]
        if not match:
            return False, f"missing check {check['name']}"
        want = check.get("passed", False)
        return match[0]["passed"] is want, f"check {check['name']} passed={match[0]['passed']}"
    if kind == "file_after":
        repo = ctx["repo"]
        path = repo / check["path"]
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
        if "contains" in check and check["contains"] not in text:
            return False, f"{check['path']} should contain {check['contains']!r}"
        if "not_contains" in check and check["not_contains"] in text:
            return False, f"{check['path']} should not contain {check['not_contains']!r}"
        if "contains" not in check and "not_contains" not in check:
            return path.is_file(), f"expected {check['path']}"
        return True, "file content ok"

    return False, f"unknown check type {kind!r}"


# ------------------------------------------------------------------ harness
class Harness:
    def __init__(self, suite_dir: Path | None = None, work_root: Path | None = None, repo_root: Path | None = None):
        self.repo_root = Path(repo_root or REPO_ROOT)
        self.suite_dir = Path(suite_dir or DEFAULT_SUITE)
        self.work_root = Path(work_root or (self.repo_root / "var" / "eval-work"))

    def discover(self) -> list[Path]:
        return sorted(self.suite_dir.glob("*.yaml"))

    def run_all(self, paths: list[Path] | None = None, label: str = "run") -> dict:
        paths = paths or self.discover()
        started = time.monotonic()
        results = [self.run_path(p) for p in paths]
        duration = time.monotonic() - started
        executed = [r for r in results if not r.get("skipped")]
        passed = sum(1 for r in executed if r["passed"])
        summary = {
            "label": label,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "duration_s": round(duration, 3),
            "total": len(results),
            "executed": len(executed),
            "skipped": len(results) - len(executed),
            "passed": passed,
            "failed": len(executed) - passed,
            "pass_rate": round(passed / len(executed), 4) if executed else 0.0,
            "python": sys.version.split()[0],
            "results": results,
        }
        out_dir = self.repo_root / "eval" / "runs"
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        out_path = out_dir / f"{stamp}-{label}.json"
        out_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
        summary["out_path"] = str(out_path)
        return summary

    def run_path(self, path: Path) -> dict:
        scenario = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        sid = scenario.get("id", Path(path).stem)
        name = scenario.get("name", sid)
        runner = scenario.get("runner", "agent")

        # subsystem requirements: skip cleanly when a subsystem is disabled
        missing = [
            req
            for req in (scenario.get("requires") or [])
            if not env_flag(REQUIREMENT_FLAGS.get(req, req), "1")
        ]
        if missing:
            return {
                "id": sid,
                "name": name,
                "runner": runner,
                "passed": None,
                "skipped": True,
                "skip_reason": f"disabled subsystem(s): {', '.join(missing)}",
                "checks": [],
                "error": None,
                "latency_ms": 0.0,
                "rss_kb": _rss_kb(),
                "rss_delta_kb": 0,
                "reply": None,
            }

        rss_before = _rss_kb()
        started = time.monotonic()
        ctx: dict = {}
        checks_out: list[dict] = []
        error = None
        try:
            if runner == "improve":
                ctx = self._run_improve(scenario)
            else:
                ctx = self._run_agent(scenario)
            for check in scenario.get("expect", []):
                ok, detail = _check(check, ctx)
                checks_out.append({"type": check.get("type"), "passed": bool(ok), "detail": detail})
        except Exception as exc:  # noqa: BLE001 - a scenario crash is a failed scenario
            error = f"{type(exc).__name__}: {exc}"
        return {
            "id": sid,
            "name": name,
            "runner": runner,
            "passed": (error is None) and all(c["passed"] for c in checks_out) and bool(checks_out),
            "skipped": False,
            "checks": checks_out,
            "error": error,
            "latency_ms": round((time.monotonic() - started) * 1000, 2),
            "rss_kb": _rss_kb(),
            "rss_delta_kb": _rss_kb() - rss_before,
            "reply": ctx["last_reply"].to_dict() if ctx.get("last_reply") is not None else None,
        }

    # ----------------------------------------------------------- agent run
    def _run_agent(self, scenario: dict) -> dict:
        root = self.work_root / scenario["id"]
        if root.exists():
            shutil.rmtree(root)
        skills_dir = root / "skills"
        workspace = root / "workspace"
        skills_dir.mkdir(parents=True)
        workspace.mkdir(parents=True)

        setup = scenario.get("setup", {}) or {}
        for rel, content in (setup.get("files") or {}).items():
            target = workspace / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        for name in setup.get("shipped_skills", []) or []:
            src = self.repo_root / "skills" / name
            shutil.copytree(src, skills_dir / name)
        for spec in setup.get("skills", []) or []:
            _write_skill(
                skills_dir,
                spec["name"],
                spec.get("body", "fixture body"),
                frontmatter=spec.get("frontmatter"),
                version=str(spec.get("version", "1.0")),
            )

        config = Config(
            provider="mock",
            data_dir=root / "var",
            skills_dirs=[skills_dir],
            workspace_dir=workspace,
            repo_root=root,
            enable_skills=env_flag("MAX_ENABLE_SKILLS"),
        )
        config.ensure_dirs()

        script = [dict(step["reply"]) for step in scenario.get("steps", []) if "reply" in step]
        provider = MockProvider(script=script)
        agent = build_agent(config, provider=provider)

        mcp_client = None
        if setup.get("mcp"):
            from ..mcp import MCPClient, register_mcp_tools

            server = self.repo_root / "tests" / "fixtures" / "mock_mcp_server.py"
            mcp_client = MCPClient([sys.executable, str(server)])
            mcp_client.start(timeout=10)
            register_mcp_tools(agent.registry, mcp_client)

        ctx: dict = {
            "config": config,
            "provider": provider,
            "agent": agent,
            "registry": agent.skill_registry,
            "replies": [],
            "mcp_client": mcp_client,
        }
        try:
            for step in scenario.get("steps", []):
                if "restart" in step:
                    provider = MockProvider(script=provider.script)
                    agent = build_agent(config, provider=provider)
                    if mcp_client:
                        from ..mcp import register_mcp_tools

                        register_mcp_tools(agent.registry, mcp_client)
                    ctx["agent"] = agent
                    ctx["provider"] = provider
                elif "user" in step:
                    reply = agent.ask(step["user"], session_id=scenario.get("session", "default"))
                    ctx["replies"].append(reply)
                    ctx["last_reply"] = reply
            return ctx
        finally:
            if mcp_client:
                mcp_client.close()

    # -------------------------------------------------------- improve run
    def _run_improve(self, scenario: dict) -> dict:
        from ..improve import Policy, Proposal, run as run_pipeline

        root = self.work_root / scenario["id"]
        if root.exists():
            shutil.rmtree(root)
        repo = root / "repo"
        repo.mkdir(parents=True)

        files = scenario.get("repo") or {}
        if not files:
            files = {"README.md": "# eval repo\n"}
        baseline_names = set(files)
        for rel, content in files.items():
            target = repo / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        if "test_probe.py" not in files:
            (repo / "test_probe.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
            baseline_names.add("test_probe.py")

        _git(["init", "-q"], repo)
        _git(["config", "user.email", "eval@max.invalid"], repo)
        _git(["config", "user.name", "MAX Eval"], repo)
        _git(["config", "commit.gpgsign", "false"], repo)
        _git(["add", "-A"], repo)
        _git(["commit", "-q", "-m", "baseline"], repo)

        # generate a real patch from inline edits
        edits = scenario.get("edits") or {}
        for rel, content in edits.items():
            target = repo / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        _git(["add", "-A"], repo)
        patch = subprocess.run(
            ["git", "diff", "--cached", "HEAD"], cwd=str(repo), capture_output=True, text=True
        ).stdout
        if patch and not patch.endswith("\n"):
            patch += "\n"
        _git(["reset", "-q", "--hard", "HEAD"], repo)
        for rel in edits:
            if rel not in baseline_names and (repo / rel).exists():
                (repo / rel).unlink()

        proposal_data = dict(scenario.get("proposal") or {})
        proposal = Proposal(
            id=proposal_data.get("id", scenario["id"]),
            title=proposal_data.get("title", "eval proposal"),
            category=proposal_data.get("category", "docs"),
            patch=patch,
            rationale=proposal_data.get("rationale", "generated by evaluation suite"),
        )
        report = run_pipeline(proposal, repo, policy=Policy.load())
        return {"repo": repo, "report": report, "config": None, "last_reply": None}
