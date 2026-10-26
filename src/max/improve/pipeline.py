"""Autonomous self-improvement pipeline with trusted policy enforcement.

Flow (see docs/ARCHITECTURE.md):
  baseline tests -> isolated git worktree + branch -> apply patch ->
  static analysis -> security scan (protected paths, deny patterns, secrets)
  -> candidate tests -> compare with baseline -> structured report ->
  promote (auto only if policy allows) or reject -> rollback on demand.

Nothing in the proposal influences the policy: policy.yaml is integrity
pinned and re-read by this module on every run.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .analysis import (
    Finding,
    changed_paths_and_stats,
    scan_added_lines,
    scan_python_file,
)
from .policy import Policy, PolicyError

PYTEST_CMD = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]


class ImproveError(Exception):
    pass


# ----------------------------------------------------------------- proposal
@dataclass
class Proposal:
    id: str
    title: str
    category: str  # code | skill | docs | config | credentials | ...
    patch: str
    rationale: str = ""
    targets: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "Proposal":
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        for key in ("id", "category", "patch"):
            if key not in data:
                raise ImproveError(f"proposal missing required field '{key}'")
        # allow `patch_file:` relative to the proposal
        if not data.get("patch") and data.get("patch_file"):
            patch_path = Path(path).parent / data["patch_file"]
            data["patch"] = patch_path.read_text(encoding="utf-8")
        return cls(
            id=str(data["id"]),
            title=str(data.get("title") or data["id"]),
            category=str(data["category"]).lower(),
            patch=str(data["patch"]),
            rationale=str(data.get("rationale") or ""),
            targets=[str(t) for t in (data.get("targets") or [])],
        )


# ------------------------------------------------------------- test runner
def run_test_suite(repo_root: Path, cmd: list[str] | None = None, timeout: int = 600) -> dict:
    cmd = list(cmd or PYTEST_CMD)
    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "passed": 0, "failed": 0, "errors": 1, "duration_s": timeout, "tail": "test run timed out"}
    except FileNotFoundError as exc:
        return {"ok": False, "passed": 0, "failed": 0, "errors": 1, "duration_s": 0.0, "tail": str(exc)}
    output = (proc.stdout or "") + (proc.stderr or "")
    tail = "\n".join(output.splitlines()[-25:])
    passed = int(m.group(1)) if (m := re.search(r"(\d+) passed", output)) else 0
    failed = int(m.group(1)) if (m := re.search(r"(\d+) failed", output)) else 0
    errors = int(m.group(1)) if (m := re.search(r"(\d+) error", output)) else 0
    if proc.returncode != 0 and not (failed or errors):
        errors = 1  # collection/import failure
    return {
        "ok": proc.returncode == 0,
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "returncode": proc.returncode,
        "duration_s": round(time.monotonic() - started, 3),
        "tail": tail,
        "cmd": cmd,
    }


# ---------------------------------------------------------------- git utils
def _with_trailing_newline(text: str) -> str:
    return text if not text or text.endswith("\n") else text + "\n"


def _is_test_path(path: str) -> bool:
    """True for test files: tests/**, test_*.py, *_test.py."""
    name = path.rsplit("/", 1)[-1]
    return path.startswith("tests/") or name.startswith("test_") or name.endswith("_test.py")


def _git(args: list[str], cwd: Path, timeout: int = 120) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=timeout
    )
    if proc.returncode != 0:
        raise ImproveError(f"git {' '.join(args)} failed: {proc.stderr.strip()[:400]}")
    return proc.stdout.strip()


def _git_ok(args: list[str], cwd: Path, timeout: int = 120) -> tuple[bool, str]:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=timeout
    )
    return proc.returncode == 0, (proc.stdout + proc.stderr).strip()


# -------------------------------------------------------------------- checks
def static_checks(worktree: Path, paths: list[str]) -> list[dict]:
    results: list[dict] = []
    py_files = [worktree / p for p in paths if p.endswith(".py")]
    compile_ok = True
    for path in py_files:
        if not path.is_file():
            continue
        try:
            compile(path.read_text(encoding="utf-8"), str(path), "exec")
        except SyntaxError as exc:
            compile_ok = False
            results.append(
                {
                    "name": f"compile:{path.name}",
                    "passed": False,
                    "detail": f"SyntaxError line {exc.lineno}: {exc.msg}",
                }
            )
    if compile_ok:
        results.append({"name": "compile", "passed": True, "detail": f"{len(py_files)} python file(s) compile"})

    findings: list[Finding] = []
    for path in py_files:
        if path.is_file():
            findings.extend(scan_python_file(path, worktree))
    blocking = [f for f in findings if f.severity == "error"]
    results.append(
        {
            "name": "static-analysis",
            "passed": not blocking,
            "detail": "no blocking findings"
            if not blocking
            else "; ".join(f"{f.file}:{f.line} {f.rule}" for f in blocking[:5]),
            "findings": [f.to_dict() for f in findings],
        }
    )
    return results


def security_checks(diff_text: str, paths: list[str], policy: Policy, worktree: Path) -> list[dict]:
    results: list[dict] = []

    protected = policy.touches_protected(paths)
    results.append(
        {
            "name": "protected-paths",
            "passed": not protected,
            "detail": "ok" if not protected else f"touches: {', '.join(protected)}",
        }
    )

    denied = []
    for line in diff_text.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            hit = policy.matches_any(line[1:], policy.deny_line_patterns)
            if hit:
                denied.append(f"{hit} in: {line.strip()[:80]}")
    results.append(
        {
            "name": "deny-patterns",
            "passed": not denied,
            "detail": "ok" if not denied else "; ".join(denied[:5]),
        }
    )

    secrets = scan_added_lines(diff_text)
    results.append(
        {
            "name": "secret-scan",
            "passed": not secrets,
            "detail": "ok" if not secrets else "; ".join(f"{s.file}:{s.line} {s.rule}" for s in secrets[:5]),
            "findings": [s.to_dict() for s in secrets],
        }
    )
    return results


# --------------------------------------------------------------------- run
def run(
    proposal: Proposal,
    repo_root: Path,
    policy: Policy | None = None,
    test_cmd: list[str] | None = None,
    work_root: Path | None = None,
    keep_worktree: bool = False,
    registry=None,
    approved: bool = False,
) -> dict:
    """Run the pipeline. ``approved=True`` is the human approval path: it can
    override an ``awaiting_approval`` decision but never a ``reject``.
    """
    repo_root = Path(repo_root).resolve()
    policy = policy or Policy.load()
    work_root = Path(work_root or (repo_root / ".max" / "worktrees"))
    reports_dir = repo_root / "eval" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    if not (repo_root / ".git").exists():
        raise ImproveError("not a git repository; the pipeline needs git for isolation and rollback")

    report: dict = {
        "id": proposal.id,
        "proposal": {
            "id": proposal.id,
            "title": proposal.title,
            "category": proposal.category,
            "rationale": proposal.rationale,
            "targets": proposal.targets,
            "patch": proposal.patch,
        },
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "policy": policy.to_dict(),
        "checks": [],
        "verdict": "rejected",
        "status": "rejected",
        "reasons": [],
        "commit": None,
        "registry_action": None,
    }

    # 1. baseline ---------------------------------------------------------
    baseline = run_test_suite(repo_root, test_cmd)
    report["baseline"] = baseline

    # 2. isolated worktree ------------------------------------------------
    branch = f"max/auto/{proposal.id}"
    wt_path = work_root / proposal.id
    _git_ok(["worktree", "remove", "--force", str(wt_path)], repo_root)
    if wt_path.exists():
        shutil.rmtree(wt_path, ignore_errors=True)
    work_root.mkdir(parents=True, exist_ok=True)
    ok, out = _git_ok(["worktree", "add", "-b", branch, str(wt_path), "HEAD"], repo_root)
    if not ok:
        # branch may already exist from a previous attempt
        ok2, out2 = _git_ok(["branch", "-D", branch], repo_root)
        ok, out = _git_ok(["worktree", "add", "-b", branch, str(wt_path), "HEAD"], repo_root)
        if not ok:
            report["reasons"] = [f"worktree creation failed: {out or out2}"]
            _finish(reports_dir, report)
            return report

    # 3. apply patch ------------------------------------------------------
    patch_file = wt_path / ".max-proposal.patch"
    patch_file.write_text(_with_trailing_newline(proposal.patch), encoding="utf-8")
    ok, out = _git_ok(["apply", "--whitespace=nowarn", str(patch_file)], wt_path)
    patch_file.unlink(missing_ok=True)
    report["checks"].append({"name": "patch-applies", "passed": ok, "detail": "ok" if ok else out[:400]})
    if not ok:
        report["reasons"] = ["patch does not apply cleanly to HEAD"]
        _cleanup_worktree(repo_root, wt_path, keep=False)
        _finish(reports_dir, report)
        return report

    # Stage everything so brand-new files appear in the diff (plain `git diff`
    # ignores untracked files), then read the full change set vs HEAD.
    _git_ok(["add", "-A"], wt_path)
    diff_ok, diff_text = _git_ok(["diff", "HEAD"], wt_path)
    diff_text = _with_trailing_newline(diff_text)
    paths, added, removed = changed_paths_and_stats(diff_text)
    report["diff_stats"] = {"paths": paths, "added": added, "removed": removed}

    # 4. static + security ------------------------------------------------
    report["checks"].extend(static_checks(wt_path, paths))
    report["checks"].extend(security_checks(diff_text, paths, policy, wt_path))

    # 5. candidate tests --------------------------------------------------
    candidate = run_test_suite(wt_path, test_cmd)
    report["candidate"] = candidate

    # 6. compare ----------------------------------------------------------
    regressions = candidate["failed"] > baseline["failed"] or candidate["errors"] > baseline["errors"]
    lost_passes = candidate["passed"] < baseline["passed"]
    zero_fail = policy.require_zero_failures and (candidate["failed"] or candidate["errors"])
    report["checks"].append(
        {
            "name": "no-test-regression",
            "passed": not (regressions or lost_passes or zero_fail),
            "detail": (
                f"baseline {baseline['passed']}p/{baseline['failed']}f/{baseline['errors']}e -> "
                f"candidate {candidate['passed']}p/{candidate['failed']}f/{candidate['errors']}e"
            ),
        }
    )

    checks_ok = all(c["passed"] for c in report["checks"])
    existing_tests_modified = any(
        _is_test_path(p) and (repo_root / p).is_file() for p in paths
    )
    action, reasons = policy.decide(
        category=proposal.category,
        checks_ok=checks_ok,
        changed_paths=paths,
        changed_lines=added + removed,
        existing_tests_modified=existing_tests_modified,
    )
    if approved and action == "human":
        action = "auto"
        reasons = list(reasons) + ["manually approved by operator"]
        report["approved"] = True
    report["reasons"] = reasons

    # 7. verdict ----------------------------------------------------------
    if action == "reject":
        report["verdict"] = "rejected"
        report["status"] = "rejected"
        _cleanup_worktree(repo_root, wt_path, keep=keep_worktree)
        _finish(reports_dir, report)
        return report

    if action == "human":
        report["verdict"] = "awaiting_approval"
        report["status"] = "pending_approval"
        _cleanup_worktree(repo_root, wt_path, keep=False)
        _finish(reports_dir, report)
        return report

    # 8. auto-promote -----------------------------------------------------
    promoted = _promote(
        proposal=proposal,
        repo_root=repo_root,
        diff_text=diff_text,
        paths=paths,
        test_cmd=test_cmd,
        report=report,
        reports_dir=reports_dir,
        registry=registry,
    )
    _cleanup_worktree(repo_root, wt_path, keep=False)
    _finish(reports_dir, report)
    if not promoted:
        report["verdict"] = "rejected"
        report["status"] = "rejected"
        _save(reports_dir, report)
    return report


def _promote(*, proposal, repo_root, diff_text, paths, test_cmd, report, reports_dir, registry) -> bool:
    """Apply the patch to the primary working tree, re-test, commit."""
    # archive originals for rollback without touching unrelated user changes
    archive = reports_dir / proposal.id / "baseline"
    archive.mkdir(parents=True, exist_ok=True)
    originals: dict[str, bytes] = {}
    for path in paths:
        full = repo_root / path
        if full.is_file():
            originals[path] = full.read_bytes()
            target = archive / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(originals[path])

    patch_file = repo_root / ".max-promote.patch"
    patch_file.write_text(_with_trailing_newline(diff_text), encoding="utf-8")
    ok, out = _git_ok(["apply", "--whitespace=nowarn", str(patch_file)], repo_root)
    patch_file.unlink(missing_ok=True)
    if not ok:
        report["checks"].append({"name": "promote-apply", "passed": False, "detail": out[:400]})
        report["reasons"].append("patch could not be applied to the working tree")
        return False
    report["checks"].append({"name": "promote-apply", "passed": True, "detail": "ok"})

    result = run_test_suite(repo_root, test_cmd)
    report["post_promote_tests"] = result
    if not result["ok"]:
        # roll back immediately: restore archived originals
        for path, data in originals.items():
            (repo_root / path).write_bytes(data)
        report["checks"].append(
            {"name": "post-promote-tests", "passed": False, "detail": "restored baseline files"}
        )
        report["reasons"].append("tests failed after promotion; changes reverted")
        return False
    report["checks"].append({"name": "post-promote-tests", "passed": True, "detail": f"{result['passed']} passed"})

    # commit only the proposal's files
    _git(["add", "--", *paths], repo_root)
    message = f"max(improve): {proposal.id} — {proposal.title}\n\nCategory: {proposal.category}\nRationale: {proposal.rationale[:200]}\n"
    ok, out = _git_ok(["commit", "-m", message], repo_root)
    if ok:
        report["commit"] = _git(["rev-parse", "HEAD"], repo_root)
    else:
        report["commit"] = None
        report["reasons"].append(f"changes applied but commit skipped: {out[:200]}")
    report["verdict"] = "promoted"
    report["status"] = "promoted"
    report["archive_dir"] = str(archive)
    return True


# ----------------------------------------------------------- approve/rollback
def approve(proposal_id: str, repo_root: Path, policy: Policy | None = None, test_cmd=None) -> dict:
    """Human action: re-run a pending proposal with approval granted.

    The re-run re-validates everything (patch, static analysis, security
    scan, tests); approval only overrides the 'needs human review' gate.
    """
    repo_root = Path(repo_root)
    report = load_report(proposal_id, repo_root)
    if report["status"] == "rejected":
        raise ImproveError("rejected proposals cannot be approved; fix and re-submit")
    if report["status"] not in {"pending_approval"}:
        raise ImproveError(f"proposal {proposal_id} is {report['status']}, not awaiting approval")
    proposal_data = report["proposal"]
    if not proposal_data.get("patch"):
        raise ImproveError("report has no stored patch; re-submit the proposal file")
    proposal = Proposal(
        id=report["id"],
        title=proposal_data["title"],
        category=proposal_data["category"],
        patch=proposal_data["patch"],
        rationale=proposal_data.get("rationale", ""),
        targets=proposal_data.get("targets", []),
    )
    return run(proposal, repo_root, policy=policy, test_cmd=test_cmd, approved=True)


def rollback(proposal_id: str, repo_root: Path) -> dict:
    repo_root = Path(repo_root)
    report = load_report(proposal_id, repo_root)
    if report["status"] == "rolled_back":
        raise ImproveError("already rolled back")

    if report.get("commit"):
        ok, out = _git_ok(["revert", "--no-edit", report["commit"]], repo_root)
        if ok:
            report["status"] = "rolled_back"
            report["rollback"] = {"method": "git-revert", "detail": out.splitlines()[-1] if out else ""}
            _save(repo_root / "eval" / "reports", report)
            return report
        report["rollback"] = {"method": "git-revert", "detail": f"failed: {out[:300]}"}

    # fallback: restore archived originals
    archive = report.get("archive_dir")
    restored = []
    if archive and Path(archive).is_dir():
        for path in report.get("diff_stats", {}).get("paths", []):
            src = Path(archive) / path
            if src.is_file():
                (repo_root / path).write_bytes(src.read_bytes())
                restored.append(path)
    report["status"] = "rolled_back"
    report["rollback"] = {"method": "archive-restore", "files": restored}
    _save(repo_root / "eval" / "reports", report)
    return report


# ------------------------------------------------------------------ reports
def _finish(reports_dir: Path, report: dict) -> None:
    report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _save(reports_dir, report)


def _save(reports_dir: Path, report: dict) -> None:
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"{report['id']}.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")


def load_report(proposal_id: str, repo_root: Path) -> dict:
    path = Path(repo_root) / "eval" / "reports" / f"{proposal_id}.json"
    if not path.is_file():
        raise ImproveError(f"no report for '{proposal_id}' at {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def list_reports(repo_root: Path) -> list[dict]:
    reports = sorted((Path(repo_root) / "eval" / "reports").glob("*.json"))
    out = []
    for path in reports:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        out.append(
            {
                "id": data.get("id"),
                "status": data.get("status"),
                "verdict": data.get("verdict"),
                "category": data.get("proposal", {}).get("category"),
                "reasons": data.get("reasons", []),
            }
        )
    return out


def _cleanup_worktree(repo_root: Path, path: Path, keep: bool) -> None:
    if keep:
        return
    _git_ok(["worktree", "remove", "--force", str(path)], repo_root)
    _git_ok(["branch", "-D", f"max/auto/{path.name}"], repo_root)
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
