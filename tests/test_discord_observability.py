"""Phase A observability: every routed message must leave a trace.

Covers the failure mode reported against MAX — "ordinary messages are
silently ignored while commands work" — by asserting that *every* routing
outcome is audited (reply, command, suppressed, ignored, provider error),
that empty-content messages are diagnosed as a Message Content Intent
problem, and that the startup banner/check report expose effective
configuration without leaking the token.
"""

from __future__ import annotations

import time

from max.core.tools import AuditLog
from max.discord import ChannelStore, DiscordConfig, DiscordRouter, MessageView

BOT_ID = 42
ADMIN = 5
PLAIN = 6
GUILD = 7
CHANNEL = 10


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


def reply(text="ok", ok=True, error=None):
    class R:
        pass

    r = R()
    r.text, r.ok, r.error, r.flagged, r.rejected = text, ok, error, [], []
    return r


def make_router(tmp_path, **kw):
    config = kw.pop(
        "config",
        DiscordConfig(
            allowed_channels={CHANNEL},
            admin_users={ADMIN},
            allowlist_file=tmp_path / "allowlist.json",
            cooldown_s=0.0,
        ),
    )
    clock = kw.pop("clock", FakeClock())
    audit = AuditLog(tmp_path / "audit.jsonl")
    store = ChannelStore(config, audit)
    router = DiscordRouter(
        config,
        store,
        audit,
        ask_fn=kw.pop("ask", lambda text, session: reply(f"echo:{text}")),
        clock=clock,
        **kw,
    )
    return router, audit, config, store, clock


def view(mid=1, content="hi max", channel=CHANNEL, author=PLAIN, **kw):
    base = dict(
        message_id=mid,
        author_id=author,
        author_name="alice",
        channel_id=channel,
        guild_id=GUILD,
        content=content,
        bot_id=BOT_ID,
    )
    base.update(kw)
    return MessageView(**base)


# --------------------------------------------------------------- outcome trace
def test_reply_outcome_is_audited(tmp_path):
    router, audit, *_ = make_router(tmp_path)
    router.handle(view())
    events = audit.events("discord_message_outcome")
    assert len(events) == 1
    assert events[-1]["action"] == "reply"
    assert events[-1]["channel"] == CHANNEL
    assert "hi max" not in str(events[-1])  # contents are never recorded


def test_non_allowlisted_channel_outcome_is_audited(tmp_path):
    router, audit, *_ = make_router(tmp_path)
    result = router.handle(view(channel=999))
    assert result.reason == "channel_not_allowlisted"
    events = audit.events("discord_message_outcome")
    assert any(e["action"] == "suppressed" and e["reason"] == "channel_not_allowlisted" for e in events)


def test_ignored_channel_outcome_is_audited(tmp_path):
    config = DiscordConfig(
        allowed_channels={CHANNEL},
        ignored_channels={55},
        admin_users={ADMIN},
        allowlist_file=tmp_path / "allowlist.json",
        cooldown_s=0.0,
    )
    router, audit, *_ = make_router(tmp_path, config=config)
    assert router.handle(view(channel=55)).reason == "ignored_channel"
    assert any(
        e["reason"] == "ignored_channel"
        for e in audit.events("discord_message_outcome")
    )


def test_command_outcome_is_audited(tmp_path):
    router, audit, *_ = make_router(tmp_path)
    result = router.handle(view(content="!max status", author=ADMIN))
    assert result.action == "command"
    assert any(e["command"] == "status" for e in audit.events("discord_message_outcome"))


def test_empty_message_outcome_is_audited(tmp_path):
    router, audit, *_ = make_router(tmp_path)
    result = router.handle(view(content=""))
    assert result.reason == "empty_message"
    assert any(
        e["reason"] == "empty_message" for e in audit.events("discord_message_outcome")
    )


def test_cooldown_and_backoff_suppressions_are_audited(tmp_path):
    router, audit, _, _, clock = make_router(tmp_path)
    # cooldown path
    router.limiter.cooldown_s = 5.0
    assert router.handle(view(mid=1)).action == "reply"
    second = router.handle(view(mid=2))
    assert second.reason == "cooldown"
    assert any(
        e["reason"] == "cooldown" for e in audit.events("discord_message_outcome")
    )
    # provider backoff path
    clock.advance(60)
    router.limiter.cooldown_s = 0.0
    router.backoff.note_failure()
    third = router.handle(view(mid=3))
    assert third.reason == "provider_backoff"
    assert any(
        e["reason"] == "provider_backoff" for e in audit.events("discord_message_outcome")
    )


def test_own_message_and_duplicate_outcomes_are_audited(tmp_path):
    router, audit, *_ = make_router(tmp_path)
    router.handle(view(mid=7, author=BOT_ID))
    router.handle(view(mid=8, content="first"))
    router.handle(view(mid=8, content="again"))
    reasons = [e["reason"] for e in audit.events("discord_message_outcome")]
    assert "own_message" in reasons
    assert "duplicate_message" in reasons


def test_suppression_tracing_is_throttled_but_mentions_are_never_throttled(tmp_path):
    router, audit, *_ = make_router(tmp_path)
    for i in range(10, 14):
        router.handle(view(mid=i, channel=999))
    assert sum(
        1 for e in audit.events("discord_message_outcome") if e["reason"] == "channel_not_allowlisted"
    ) == 1  # throttled to one per (channel, outcome) per window

    for i in range(20, 22):
        router.handle(view(mid=i, channel=999, mentioned_ids={BOT_ID}))
    assert (
        sum(
            1
            for e in audit.events("discord_message_outcome")
            if e["reason"] == "channel_not_allowlisted"
        )
        >= 3
    )  # addressing MAX is never swallowed silently


def test_replies_are_never_throttled(tmp_path):
    router, audit, *_ = make_router(tmp_path)
    for i in range(30, 34):
        router.handle(view(mid=i, content=f"hello {i}"))
    assert (
        sum(1 for e in audit.events("discord_message_outcome") if e["action"] == "reply")
        == 4
    )


# ----------------------------------------------------------- intent diagnostics
def test_status_explains_empty_content_as_intent_problem(tmp_path):
    router, audit, *_ = make_router(tmp_path)
    router.handle(view(mid=1, content=""))
    router.handle(view(mid=2, content=""))
    diag = router.diagnostics()
    assert diag["empty_content_total"] == 2
    assert "Message Content Intent" in diag["message_content_intent_warning"]

    result = router.handle(view(mid=3, content="!max status", author=ADMIN))
    body = "\n".join(result.replies)
    assert "empty-content messages: 2" in body
    assert "Message Content Intent" in body


def test_status_is_quiet_when_everything_arrives_with_text(tmp_path):
    router, *_ = make_router(tmp_path)
    router.handle(view(mid=1, content="hello"))
    assert router.diagnostics()["message_content_intent_warning"] is None
    body = "\n".join(
        router.handle(view(mid=2, content="!max status", author=ADMIN)).replies
    )
    assert "empty-content messages: 0" in body


# ------------------------------------------------------------ startup + check
def test_startup_summary_reports_effective_config_without_secrets(tmp_path):
    from max.discord.bot import startup_summary

    config = DiscordConfig(
        allowed_channels={CHANNEL, 11},
        ignored_channels={55},
        admin_users={ADMIN},
        admin_roles={9},
        allowlist_file=tmp_path / "allowlist.json",
        token="super-secret-token-value",
    )
    audit = AuditLog(tmp_path / "audit.jsonl")
    store = ChannelStore(config, audit)
    summary = startup_summary(config, store, provider_label="mock", guild_count=2)

    assert summary["response_mode"] == config.response_mode
    assert summary["dm_mode"] == "off"
    assert summary["allowlisted_channels"] == 2
    assert summary["configured_channels"] == 2
    assert summary["ignored_channels"] == 1
    assert summary["admin_users"] == 1 and summary["admin_roles"] == 1
    assert summary["provider"] == "mock"
    assert summary["guilds"] == 2
    assert "message_content" in summary["intents"]
    assert "super-secret-token-value" not in str(summary)
    assert "token" not in str(summary).lower()


def test_least_privilege_permission_list_is_exact(tmp_path):
    from max.discord.bot import required_permissions

    perms = required_permissions()
    assert perms == [
        "View Channels",
        "Send Messages",
        "Send Messages in Threads",
        "Read Message History",
    ]
    assert not any("slash" in p.lower() or "?" in p for p in perms)


def test_check_report_flags_missing_token_and_advisories(tmp_path):
    from max.cli import discord_check_report

    off = DiscordConfig(allowlist_file=tmp_path / "a.json")
    audit = AuditLog(tmp_path / "audit.jsonl")
    report = discord_check_report(off, ChannelStore(off, audit))
    assert report["misconfigured"] is False
    assert any("no bot token" in w for w in report["warnings"])
    assert any("never reply" in w for w in report["warnings"])
    assert any("administrators configured" in w for w in report["warnings"])

    enabled_no_token = DiscordConfig(
        enable=True, allowed_channels={CHANNEL}, allowlist_file=tmp_path / "b.json"
    )
    report = discord_check_report(enabled_no_token, ChannelStore(enabled_no_token, audit))
    assert report["misconfigured"] is True  # exit status 1

    token_but_disabled = DiscordConfig(
        enable=False, token="s3cr3t-token-xyz", allowlist_file=tmp_path / "c.json"
    )
    report = discord_check_report(token_but_disabled, ChannelStore(token_but_disabled, audit))
    assert any("MAX_DISCORD_ENABLE is 0" in w for w in report["warnings"])
    assert "s3cr3t-token-xyz" not in str(report)
    assert report["token_configured"] is True


def test_provider_error_is_traced_with_error_action(tmp_path):
    def boom(text, session):
        raise RuntimeError("provider exploded")

    router, audit, *_ = make_router(tmp_path, ask=boom)
    result = router.handle(view())
    assert result.error is True
    events = audit.events("discord_message_outcome")
    assert events[-1]["action"] == "reply" and events[-1]["error"] is True
    assert audit.events("discord_provider_error")
