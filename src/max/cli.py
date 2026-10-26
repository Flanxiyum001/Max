"""MAX command line interface.

    max health                 report service health (JSON with --json)
    max ask "question"         one-shot conversation
    max skills list|show|stage|eval|promote|rollback
    max eval run               run the evaluation suite
    max improve scan|run|list|show|approve|rollback|trust-policy
    max bench                  resource + latency benchmarks
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import __version__
from .config import REPO_ROOT, Config


def _print(data, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, indent=2, sort_keys=True, default=str))
    else:
        for key, value in data.items():
            if isinstance(value, (dict, list)):
                print(f"{key}: {json.dumps(value, default=str)}")
            else:
                print(f"{key}: {value}")


# ------------------------------------------------------------------ health
def cmd_health(args) -> int:
    from .improve import Policy, PolicyError

    config = Config.from_env()
    config.ensure_dirs()
    status = {
        "status": "ok",
        "version": __version__,
        "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "provider": config.redacted(),
    }

    # provider build check (never raises out of here)
    from .providers import ProviderError, build_provider

    try:
        provider = build_provider(config)
        status["provider_check"] = provider.health()
        status["provider_check"]["key_configured"] = bool(config.api_key)
        if "key" in status["provider_check"]:
            status["provider_check"].pop("key")
    except ProviderError as exc:
        status["status"] = "degraded"
        status["provider_check"] = {"ok": False, "error": str(exc)}

    # skills
    try:
        from .skills import SkillRegistry

        registry = SkillRegistry(
            active_dirs=config.skills_dirs,
            staging_dir=config.staging_dir,
            state_path=config.registry_state_path,
            versions_dir=config.versions_dir,
        )
        records = registry.discover()
        status["skills"] = {
            "total": len(records),
            "valid": sum(1 for r in records if r.valid and r.safe),
            "flagged": sum(1 for r in records if r.flags),
            "invalid": sum(1 for r in records if r.errors),
            "names": [r.name for r in records],
        }
    except Exception as exc:  # noqa: BLE001
        status["status"] = "degraded"
        status["skills"] = {"error": str(exc)}

    # policy integrity
    try:
        policy = Policy.load()
        status["policy"] = {"version": policy.policy_version, "digest": policy.digest[:16], "ok": True}
    except PolicyError as exc:
        status["status"] = "degraded"
        status["policy"] = {"ok": False, "error": str(exc)}

    # writable data dir
    try:
        probe = config.data_dir / ".healthcheck"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        status["data_dir_writable"] = True
    except OSError as exc:
        status["status"] = "degraded"
        status["data_dir_writable"] = False
        status["data_dir_error"] = str(exc)

    # last eval run
    runs = sorted((config.repo_root / "eval" / "runs").glob("*.json"))
    if runs:
        try:
            last = json.loads(runs[-1].read_text(encoding="utf-8"))
            status["last_eval"] = {
                "run": runs[-1].name,
                "pass_rate": last.get("pass_rate"),
                "total": last.get("total"),
                "failed": last.get("failed"),
            }
        except ValueError:
            status["last_eval"] = {"run": runs[-1].name, "error": "unreadable"}

    _print(status, args.json)
    return 0 if status["status"] == "ok" else 1


# -------------------------------------------------------------------- chat
def cmd_ask(args) -> int:
    from .core.factory import build_agent

    config = Config.from_env()
    agent = build_agent(config)
    reply = agent.ask(args.text, session_id=args.session)
    if args.json:
        print(json.dumps(reply.to_dict(), indent=2, sort_keys=True))
    else:
        print(reply.text)
    return 0 if reply.ok else 1


# ----------------------------------------------------------------- skills
def cmd_skills(args) -> int:
    from .skills import SkillError, SkillRegistry

    config = Config.from_env()
    config.ensure_dirs()
    registry = SkillRegistry(
        active_dirs=config.skills_dirs,
        staging_dir=config.staging_dir,
        state_path=config.registry_state_path,
        versions_dir=config.versions_dir,
    )

    try:
        if args.skills_cmd == "list":
            records = registry.discover()
            if args.json:
                _print({"skills": [r.to_dict() for r in records]}, True)
            else:
                if not records:
                    print("no skills found")
                for r in records:
                    marks = []
                    if r.errors:
                        marks.append(f"INVALID: {'; '.join(r.errors)}")
                    if r.flags:
                        marks.append(f"FLAGGED: {', '.join(r.flags)}")
                    print(f"{r.name:24} v{r.version:6} {r.status:10} {r.description[:60]}")
                    for mark in marks:
                        print(f"{'':24} -> {mark}")
            return 0

        if args.skills_cmd == "show":
            record = registry.get(args.name)
            if record is None:
                print(f"skill '{args.name}' not found", file=sys.stderr)
                return 1
            data = record.to_dict()
            data["history"] = registry.history(args.name)
            _print(data, True)
            return 0

        if args.skills_cmd == "stage":
            record = registry.stage(Path(args.path))
            _print({"staged": record.to_dict()}, args.json)
            return 0

        if args.skills_cmd == "eval":
            entry = registry.evaluate(args.name, {"passed": args.passed, "checks": [], "notes": args.notes})
            _print({"evaluation": entry}, True)
            return 0 if entry["status"] == "good" else 1

        if args.skills_cmd == "promote":
            record = registry.promote(args.name)
            _print({"promoted": record.to_dict()}, args.json)
            return 0

        if args.skills_cmd == "rollback":
            record = registry.rollback(args.name)
            _print({"rolled_back_to": record.to_dict()}, args.json)
            return 0

        print("unknown skills subcommand", file=sys.stderr)
        return 2
    except SkillError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


# -------------------------------------------------------------------- eval
def cmd_eval(args) -> int:
    from .eval import Harness

    harness = Harness()
    paths = harness.discover()
    if args.filter:
        paths = [p for p in paths if args.filter in p.stem]
        if not paths:
            print(f"no scenarios match '{args.filter}'", file=sys.stderr)
            return 2
    summary = harness.run_all(paths, label=args.label)
    if args.json:
        _print(summary, True)
    else:
        for result in summary["results"]:
            if result.get("skipped"):
                print(f"SKIP  {result['id']:34} {result.get('skip_reason', '')}")
                continue
            mark = "PASS" if result["passed"] else "FAIL"
            detail = "" if result["passed"] else f" ({result['error'] or ', '.join(c['detail'] for c in result['checks'] if not c['passed'])})"
            print(f"{mark}  {result['id']:34} {result['latency_ms']:8.1f} ms{detail}")
        print(
            f"\n{summary['passed']}/{summary['executed']} executed scenarios passed "
            f"({summary['pass_rate']:.0%}), {summary['skipped']} skipped, "
            f"in {summary['duration_s']}s"
        )
        print(f"report: {summary['out_path']}")
    return 0 if summary["failed"] == 0 else 1


# ---------------------------------------------------------------- improve
def cmd_improve(args) -> int:
    from .improve import (
        ImproveError,
        Policy,
        Proposal,
        approve,
        list_reports,
        load_report,
        rollback,
        run,
        scan_repo,
        write_integrity,
    )

    try:
        if args.improve_cmd == "trust-policy":
            digest = write_integrity()
            print(f"pinned config/policy.yaml sha256={digest}")
            print("Only run this after reviewing the policy change.")
            return 0

        if args.improve_cmd == "scan":
            findings = scan_repo(Config.from_env().repo_root, subdirs=("src",))
            blocking = [f for f in findings if f.severity == "error"]
            if args.json:
                _print({"findings": [f.to_dict() for f in findings], "blocking": len(blocking)}, True)
            else:
                for f in findings:
                    print(f"{f.severity:8} {f.file}:{f.line} {f.rule} — {f.message}")
                print(f"\n{len(findings)} finding(s), {len(blocking)} blocking")
            return 1 if blocking else 0

        if args.improve_cmd == "run":
            proposal = Proposal.load(Path(args.proposal))
            report = run(proposal, Config.from_env().repo_root, policy=Policy.load())
            _print(
                {
                    "id": report["id"],
                    "verdict": report["verdict"],
                    "status": report["status"],
                    "reasons": report["reasons"],
                    "commit": report.get("commit"),
                    "baseline": report["baseline"],
                    "candidate": report.get("candidate"),
                },
                args.json,
            )
            return {"promoted": 0, "pending_approval": 0}.get(report["status"], 1)

        if args.improve_cmd == "list":
            _print({"reports": list_reports(Config.from_env().repo_root)}, args.json)
            return 0

        if args.improve_cmd == "show":
            _print(load_report(args.id, Config.from_env().repo_root), True)
            return 0

        if args.improve_cmd == "approve":
            report = approve(args.id, Config.from_env().repo_root, policy=Policy.load())
            _print({"id": report["id"], "status": report["status"], "commit": report.get("commit")}, args.json)
            return 0 if report["status"] == "promoted" else 1

        if args.improve_cmd == "rollback":
            report = rollback(args.id, Config.from_env().repo_root)
            _print({"id": report["id"], "status": report["status"], "rollback": report.get("rollback")}, args.json)
            return 0

        print("unknown improve subcommand", file=sys.stderr)
        return 2
    except ImproveError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - CLI must not traceback at users
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


# ------------------------------------------------------------------- bench
def cmd_bench(args) -> int:
    from .bench import run_benchmarks

    summary = run_benchmarks(configs=args.configs.split(",") if args.configs else None)
    if args.json:
        _print(summary, True)
    else:
        for row in summary["rows"]:
            print(json.dumps(row, sort_keys=True))
        print(f"\nreport: {summary['out_path']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="max", description="MAX personal AI assistant")
    parser.add_argument("--version", action="version", version=f"max {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_health = sub.add_parser("health", help="service health report")
    p_health.add_argument("--json", action="store_true")
    p_health.set_defaults(func=cmd_health)

    p_ask = sub.add_parser("ask", help="one-shot conversation")
    p_ask.add_argument("text")
    p_ask.add_argument("--session", default="cli")
    p_ask.add_argument("--json", action="store_true")
    p_ask.set_defaults(func=cmd_ask)

    p_skills = sub.add_parser("skills", help="skill registry")
    p_skills.set_defaults(func=cmd_skills)
    skills_sub = p_skills.add_subparsers(dest="skills_cmd", required=True)
    s_list = skills_sub.add_parser("list")
    s_list.add_argument("--json", action="store_true")
    s_show = skills_sub.add_parser("show")
    s_show.add_argument("name")
    s_stage = skills_sub.add_parser("stage")
    s_stage.add_argument("path")
    s_stage.add_argument("--json", action="store_true")
    s_eval = skills_sub.add_parser("eval")
    s_eval.add_argument("name")
    group = s_eval.add_mutually_exclusive_group(required=True)
    group.add_argument("--passed", dest="passed", action="store_true")
    group.add_argument("--failed", dest="passed", action="store_false")
    s_eval.add_argument("--notes", default="")
    s_promote = skills_sub.add_parser("promote")
    s_promote.add_argument("name")
    s_promote.add_argument("--json", action="store_true")
    s_rollback = skills_sub.add_parser("rollback")
    s_rollback.add_argument("name")
    s_rollback.add_argument("--json", action="store_true")

    p_eval = sub.add_parser("eval", help="evaluation suite")
    eval_sub = p_eval.add_subparsers(dest="eval_cmd", required=True)
    e_run = eval_sub.add_parser("run")
    e_run.add_argument("--filter", default=None)
    e_run.add_argument("--label", default="run")
    e_run.add_argument("--json", action="store_true")
    e_run.set_defaults(func=cmd_eval)

    p_improve = sub.add_parser("improve", help="self-improvement pipeline")
    improve_sub = p_improve.add_subparsers(dest="improve_cmd", required=True)
    i_scan = improve_sub.add_parser("scan")
    i_scan.add_argument("--json", action="store_true")
    i_scan.set_defaults(func=cmd_improve)
    i_run = improve_sub.add_parser("run")
    i_run.add_argument("proposal")
    i_run.add_argument("--json", action="store_true")
    i_run.set_defaults(func=cmd_improve)
    i_list = improve_sub.add_parser("list")
    i_list.add_argument("--json", action="store_true")
    i_list.set_defaults(func=cmd_improve)
    i_show = improve_sub.add_parser("show")
    i_show.add_argument("id")
    i_show.set_defaults(func=cmd_improve)
    i_approve = improve_sub.add_parser("approve")
    i_approve.add_argument("id")
    i_approve.add_argument("--json", action="store_true")
    i_approve.set_defaults(func=cmd_improve)
    i_rollback = improve_sub.add_parser("rollback")
    i_rollback.add_argument("id")
    i_rollback.add_argument("--json", action="store_true")
    i_rollback.set_defaults(func=cmd_improve)
    i_trust = improve_sub.add_parser("trust-policy")
    i_trust.set_defaults(func=cmd_improve)

    p_bench = sub.add_parser("bench", help="resource and latency benchmarks")
    p_bench.add_argument("--configs", default="A,D")
    p_bench.add_argument("--json", action="store_true")
    p_bench.set_defaults(func=cmd_bench)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
