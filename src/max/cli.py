"""MAX command line interface.

    max health                 report service health (JSON with --json)
    max ask "question"         one-shot conversation
    max skills list|show|stage|eval|promote|rollback
    max eval run               run the evaluation suite
    max improve scan|run|list|show|approve|rollback|trust-policy
    max desktop status|approvals|approve|deny
    max bench                  resource + latency benchmarks
"""

from __future__ import annotations

import argparse
import json
import shlex
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
def build_health_payload(config: Config) -> dict:
    """Health payload shared by `max health` and the Discord `!max health`.

    Never includes secret values — only presence flags.
    """
    from .improve import Policy, PolicyError

    status = {
        "status": "ok",
        "version": __version__,
        "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "provider": config.redacted(),
    }

    from .providers import ProviderError, build_provider

    try:
        provider = build_provider(config)
        status["provider_check"] = provider.health()
        status["provider_check"]["key_configured"] = bool(config.api_key)
        status["provider_check"].pop("key", None)
    except ProviderError as exc:
        status["status"] = "degraded"
        status["provider_check"] = {"ok": False, "error": str(exc)}

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

    try:
        policy = Policy.load()
        status["policy"] = {"version": policy.policy_version, "digest": policy.digest[:16], "ok": True}
    except PolicyError as exc:
        status["status"] = "degraded"
        status["policy"] = {"ok": False, "error": str(exc)}

    try:
        probe = config.data_dir / ".healthcheck"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        status["data_dir_writable"] = True
    except OSError as exc:
        status["status"] = "degraded"
        status["data_dir_writable"] = False
        status["data_dir_error"] = str(exc)

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
    return status


def cmd_health(args) -> int:
    config = Config.from_env()
    config.ensure_dirs()
    status = build_health_payload(config)
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


# ---------------------------------------------------------------- discord
def discord_check_report(config: "DiscordConfig", store) -> dict:
    """Secret-free configuration report for `max discord check`.

    Pure function (unit-testable) that separates *misconfiguration* — exit
    status 1 — from advisory warnings, and never includes the token.
    """
    from .discord.bot import required_permissions

    warnings: list[str] = []
    if config.enable and not config.token:
        warnings.append("MAX_DISCORD_ENABLE=1 but MAX_DISCORD_TOKEN is empty")
    if not config.token:
        warnings.append("no bot token configured (MAX_DISCORD_TOKEN)")
    if config.token and not config.enable:
        warnings.append(
            "MAX_DISCORD_ENABLE is 0 while a token is set — `max discord run` "
            "still starts (advisory switch); set MAX_DISCORD_ENABLE=1 to silence this"
        )
    if not store.effective_allowlist() and config.dm_mode == "off":
        warnings.append("channel allowlist is empty and DMs are off — the bot will never reply")
    if config.dm_mode == "everyone":
        warnings.append("dm_mode=everyone allows DMs from any user — tighten for privacy")
    if not config.allowed_guilds:
        warnings.append("no guild allowlist configured (channel allowlist still applies)")
    if not config.admin_users and not config.admin_roles:
        warnings.append("no administrators configured — allow/deny/channels commands will be denied")
    return {
        "enabled": config.enable,
        "token_configured": bool(config.token),
        "token_env_var": "MAX_DISCORD_TOKEN",
        "command_prefix": config.command_prefix,
        "response_mode": config.response_mode,
        "dm_mode": config.dm_mode,
        "effective_allowlist": sorted(store.effective_allowlist()),
        "ignored_channels": sorted(config.ignored_channels),
        "admin_users": len(config.admin_users),
        "admin_roles": len(config.admin_roles),
        "required_intents": ["message_content (privileged — enable in the Developer Portal)", "guilds"],
        "least_privilege_permissions": required_permissions(),
        "voice": "not enabled in Phase 1",
        "music": "not enabled in Phase 1",
        "warnings": warnings,
        "misconfigured": bool(config.enable and not config.token),
    }


def build_router(config: "DiscordConfig"):
    """Wire the Discord router to the real MAX core (agent, audit, memory)."""
    from .core.factory import build_agent
    from .core.tools import AuditLog
    from .core.usermemory import UserMemoryStore
    from .discord import ChannelStore, DiscordRouter

    core = Config.from_env()
    core.ensure_dirs()
    audit = AuditLog(core.data_dir / "audit" / "audit.jsonl")
    agent = build_agent(core)
    store = ChannelStore(config, audit)
    memory = UserMemoryStore(core.data_dir, scope="user")
    # optional multi-agent layer: a failure here must never stop text chat
    orch_kwargs: dict = {}
    try:
        from .orchestration import build_supervisor

        supervisor = build_supervisor(core, agent=agent, audit=audit)
    except Exception:  # noqa: BLE001 - degrade to single-agent MAX
        supervisor = None
    if supervisor is not None and supervisor.enabled():
        def _delegate(view, text, _sup=supervisor):  # noqa: ANN001 - router passes a MessageView
            return _sup.handle(
                text,
                owner_id=view.author_id,
                session_id=f"g{view.guild_id}_c{view.channel_id}" if view.guild_id else f"dm_u{view.author_id}",
                guild_id=view.guild_id,
                channel_id=view.channel_id,
            )

        def _orch(name, args, view, _sup=supervisor):  # noqa: ANN001
            return _sup.run_command(
                name,
                args,
                view.author_id,
                is_admin=config.is_admin(view.author_id, view.author_roles),
                session_id=f"g{view.guild_id}_c{view.channel_id}" if view.guild_id else f"dm_u{view.author_id}",
                guild_id=view.guild_id,
                channel_id=view.channel_id,
            )

        orch_kwargs = {"delegate_fn": _delegate, "orch_fn": _orch}

    router = DiscordRouter(
        config,
        store,
        audit,
        ask_fn=lambda text, session: agent.ask(text, session_id=session),
        memory=memory,
        health_fn=lambda: build_health_payload(core),
        **orch_kwargs,
    )
    return router, store, audit, core


def _provider_label(config: Config) -> str:
    """Sanitized provider identity for banners and audit: name only.

    Never includes a base URL, model credential or key — the startup line
    and the Discord audit event are both safe to paste into an issue.
    """
    name = str(getattr(config, "provider", "") or "unknown")
    try:
        from .providers import build_provider

        name = str(build_provider(config).health().get("provider") or name)
    except Exception:  # noqa: BLE001 - label must never break startup
        pass
    return name[:40] or "unknown"


def cmd_desktop(args) -> int:
    """Console human-in-the-loop gate for local desktop control."""
    from .desktop import ApprovalBook, ApprovalError

    config = Config.from_env()
    config.ensure_dirs()
    state = config.data_dir / "desktop"
    book = ApprovalBook(
        state,
        ttl_s=config.desktop_approval_ttl_s,
        gui_ttl_s=config.desktop_gui_ttl_s,
        gui_max_actions=config.desktop_gui_actions,
    )

    if args.desktop_cmd == "status":
        try:
            import pyautogui  # noqa: F401

            input_backend = "available"
        except Exception:  # noqa: BLE001
            input_backend = "not installed (pip install 'max-assistant[desktop]')"
        _print(
            {
                "desktop_enabled": config.desktop_enabled,
                "workspace": str(config.workspace_dir),
                "state_dir": str(state),
                "timeout_s": config.desktop_timeout_s,
                "approval_ttl_s": config.desktop_approval_ttl_s,
                "input_session_ttl_s": config.desktop_gui_ttl_s,
                "input_events_per_grant": config.desktop_gui_actions,
                "input_backend": input_backend,
                "pending_approvals": len(book.pending()),
            },
            args.json,
        )
        return 0

    if args.desktop_cmd == "approvals":
        rows = book.pending()
        if args.json:
            _print({"pending": rows}, True)
            return 0
        if not rows:
            print("no pending desktop approvals")
            return 0
        for row in rows:
            left = max(0, int(row.get("expires_ts", 0) - time.time()))
            argv = row.get("argv") or []
            shown = " ".join(shlex.quote(a) for a in argv) or row.get("reason", "")
            print(f"{row['id']}  {row.get('kind', '?')}  expires in {left}s")
            print(f"    {row.get('reason') or '(no reason recorded)'}")
            print(f"    {shown}")
        return 0

    # approve / deny ------------------------------------------------------
    approval_id = str(args.id)
    try:
        record = book.get(approval_id)
    except ApprovalError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    argv = record.get("argv") or []
    shown = " ".join(shlex.quote(a) for a in argv) or record.get("kind", "?")
    print(f"approval {approval_id} ({record.get('kind')}) — {record.get('status')}")
    print(f"    requested: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(record.get('created_ts', 0)))}")
    print(f"    reason:    {record.get('reason') or '(none)'}")
    print(f"    cwd:       {record.get('cwd') or '-'}")
    print(f"    action:    {shown}")

    verb = "approve" if args.desktop_cmd == "approve" else "deny"
    if not args.yes:
        if not sys.stdin.isatty():
            print(
                f"error: non-interactive terminal — review the action above and re-run with "
                f"`max desktop {verb} {approval_id} --yes`",
                file=sys.stderr,
            )
            return 2
        answer = input(f"type 'yes' to {verb} {approval_id}: ").strip().lower()
        if answer != "yes":
            print("not changed — approval left pending")
            return 1

    try:
        if args.desktop_cmd == "approve":
            book.grant(approval_id, granted_by="console")
        else:
            book.deny(approval_id, denied_by="console")
    except ApprovalError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.desktop_cmd == "approve":
        print(f"granted {approval_id} — MAX may now run that exact command once.")
    else:
        print(f"denied {approval_id} — MAX will not run that command.")
    return 0


def cmd_discord(args) -> int:
    from .discord import DiscordConfig

    config = DiscordConfig.from_env()

    if args.discord_cmd == "check":
        from .discord import ChannelStore
        from .core.tools import AuditLog

        core = Config.from_env()
        audit = AuditLog(core.data_dir / "audit" / "audit.jsonl")
        store = ChannelStore(config, audit)
        report = discord_check_report(config, store)
        warnings = report["warnings"]
        _print(report, args.json)
        return 1 if report["misconfigured"] else 0

    # ------------------------------------------------------------- run
    if not config.token:
        print("error: MAX_DISCORD_TOKEN is not set — see docs/DISCORD.md", file=sys.stderr)
        return 1
    try:
        router, store, audit, core = build_router(config)
    except Exception as exc:  # noqa: BLE001
        print(f"error: failed to initialize MAX core: {exc}", file=sys.stderr)
        return 1
    try:
        from .discord.bot import MaxDiscordBot
    except Exception as exc:  # noqa: BLE001
        print(f"error: discord.py unavailable: {exc}", file=sys.stderr)
        return 1
    bot = MaxDiscordBot(
        config, router, audit, provider_label=_provider_label(core)
    )
    if config.token and not config.enable:
        print(
            "warning: MAX_DISCORD_ENABLE is 0 — starting anyway because a token "
            "is configured (advisory switch; see docs/CONFIGURATION.md)",
            file=sys.stderr,
        )
        audit.record("discord_enable_flag_off", token_configured=True)
    print(
        f"starting MAX discord bot | prefix={config.command_prefix} "
        f"| allowlist={len(store.effective_allowlist())} channel(s) "
        f"| dm_mode={config.dm_mode} | response_mode={config.response_mode} "
        f"| provider={_provider_label(core)}"
    )
    try:
        bot.run(config.token, log_handler=None)  # token never printed
    except KeyboardInterrupt:
        print("\nstopped")
    except Exception as exc:  # noqa: BLE001 - login/network failures
        print(f"error: discord login/session failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        audit.record("discord_start_failed", error=type(exc).__name__)
        return 1
    return 0


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

    p_discord = sub.add_parser("discord", help="Discord bot (Phase 1: text only)")
    p_discord.set_defaults(func=cmd_discord)
    discord_sub = p_discord.add_subparsers(dest="discord_cmd", required=True)
    d_check = discord_sub.add_parser("check", help="validate configuration, intents and permissions")
    d_check.add_argument("--json", action="store_true")
    d_check.set_defaults(func=cmd_discord)
    d_run = discord_sub.add_parser("run", help="start the bot (requires MAX_DISCORD_TOKEN)")
    d_run.set_defaults(func=cmd_discord)

    p_desktop = sub.add_parser(
        "desktop", help="local desktop control: console approval gate"
    )
    p_desktop.set_defaults(func=cmd_desktop)
    desktop_sub = p_desktop.add_subparsers(dest="desktop_cmd", required=True)
    dd_status = desktop_sub.add_parser("status", help="configuration and pending approvals")
    dd_status.add_argument("--json", action="store_true")
    dd_approvals = desktop_sub.add_parser("approvals", help="list pending approval requests")
    dd_approvals.add_argument("--json", action="store_true")
    dd_approve = desktop_sub.add_parser("approve", help="grant one pending approval")
    dd_approve.add_argument("id")
    dd_approve.add_argument("--yes", action="store_true", help="skip the interactive confirmation")
    dd_deny = desktop_sub.add_parser("deny", help="refuse one pending approval")
    dd_deny.add_argument("id")
    dd_deny.add_argument("--yes", action="store_true", help="skip the interactive confirmation")

    p_bench = sub.add_parser("bench", help="resource and latency benchmarks")
    p_bench.add_argument("--configs", default="A,D")
    p_bench.add_argument("--json", action="store_true")
    p_bench.set_defaults(func=cmd_bench)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
