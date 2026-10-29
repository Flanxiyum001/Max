"""Explicit scoped wake coverage; no channel/guild privacy expansion by default."""

from max.core.tools import AuditLog
from max.discord import ChannelStore, DiscordConfig, DiscordRouter, MessageView


def build(tmp_path, **overrides):
    values = dict(
        response_mode="wake",
        allowed_guilds={7},
        allow_all_guild_channels=True,
        allowlist_file=tmp_path / "allowlist.json",
        cooldown_s=0,
    )
    values.update(overrides)
    config = DiscordConfig(**values)
    audit = AuditLog(tmp_path / "audit.jsonl")
    store = ChannelStore(config, audit)
    calls = []
    def ask(message, session):
        calls.append((message, session))
        return type("Reply", (), {"text": "online", "ok": True})()
    router = DiscordRouter(config, store, audit, ask_fn=ask)
    return router, store, calls


def message(num, text, channel=111, guild=7, *, dm=False):
    return MessageView(
        message_id=num, author_id=91, author_name="tester",
        channel_id=channel, guild_id=guild, content=text,
        is_dm=dm, bot_id=22,
    )


def test_opt_in_wake_cycle_works_without_explicit_channel_allowlist(tmp_path):
    router, store, calls = build(tmp_path)
    assert router.handle(message(1, "hello")).reason == "sleeping"
    assert router.handle(message(2, "hi max")).reason == "awakened"
    assert store.is_awake(111, 7)
    assert router.handle(message(3, "what is happening?")).replies == ["online"]
    assert len(calls) == 1
    assert router.handle(message(4, "bye max")).reason == "fell_asleep"
    assert router.handle(message(5, "hello again")).reason == "sleeping"
    assert len(calls) == 1
    assert not store.is_awake(111, 7)


def test_wake_flag_never_crosses_server_dm_ignore_or_admin_deny(tmp_path):
    router, store, calls = build(tmp_path, ignored_channels={200})
    assert router.handle(message(10, "hi max", guild=99)).reason == "channel_not_allowlisted"
    assert router.handle(message(11, "hi max", channel=200)).reason == "ignored_channel"
    assert router.handle(message(12, "hi max", dm=True, guild=None)).reason == "dm_disabled"
    store.remove(300, 1, guild_id=7)
    assert router.handle(message(13, "hi max", channel=300)).reason == "channel_not_allowlisted"
    assert router.handle(message(14, "hi max", channel=400)).reason == "awakened"
    assert not store.is_awake(401, 7)
    assert calls == []


def test_unscoped_or_non_wake_flag_does_not_enable_new_channels(tmp_path):
    for opts in [
        {"allowed_guilds": set()},
        {"response_mode": "all"},
        {"response_mode": "mention"},
        {"allow_all_guild_channels": False},
    ]:
        router, store, calls = build(tmp_path, **opts)
        assert not store.is_allowed(444, 7)
        assert router.handle(message(50, "hi max", channel=444)).reason == "channel_not_allowlisted"
        assert calls == []


def test_explicit_legacy_allowlist_still_works_without_broad_flag(tmp_path):
    router, store, calls = build(
        tmp_path, allowed_channels={777}, allowed_guilds=set(),
        allow_all_guild_channels=False,
    )
    assert store.is_allowed(777, 7)
    assert not store.is_allowed(111, 7)
    assert router.handle(message(1, "hi max", channel=777)).reason == "awakened"


def test_environment_flag_is_off_by_default_and_visible_in_redacted_status(tmp_path, monkeypatch):
    for name in ("MAX_DISCORD_ALLOW_ALL_GUILD_CHANNELS", "MAX_DISCORD_GUILD_IDS", "MAX_DISCORD_RESPONSE_MODE"):
        monkeypatch.delenv(name, raising=False)
    cfg = DiscordConfig.from_env(repo_root=tmp_path)
    assert not cfg.allow_all_guild_channels
    monkeypatch.setenv("MAX_DISCORD_ALLOW_ALL_GUILD_CHANNELS", "1")
    monkeypatch.setenv("MAX_DISCORD_GUILD_IDS", "7")
    monkeypatch.setenv("MAX_DISCORD_RESPONSE_MODE", "wake")
    opted = DiscordConfig.from_env(repo_root=tmp_path)
    assert opted.allow_all_guild_channels
    assert opted.allowed_guilds == {7}
    assert opted.redacted()["allow_all_guild_channels"] is True
