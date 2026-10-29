"""discord.py transport smoke tests (no network, no token)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from max.cli import main
from max.core.tools import AuditLog
from max.discord import ChannelStore, DiscordConfig, DiscordRouter
from max.discord.bot import MaxDiscordBot, build_intents, required_permissions

BOT_ID = 42


def make_bot(tmp_path, *, ask=None, send_impl=None, fail_send=False):
    config = DiscordConfig(
        allowed_channels={10},
        admin_users={5},
        allowlist_file=tmp_path / "allowlist.json",
        cooldown_s=0.0,
    )
    audit = AuditLog(tmp_path / "audit.jsonl")
    store = ChannelStore(config, audit)
    router = DiscordRouter(
        config, store, audit,
        ask_fn=ask or (lambda t, s: SimpleNamespace(text="hello there", ok=True,
                                                    error=None, flagged=[], rejected=[])),
    )
    bot = MaxDiscordBot(config, router, audit)
    # discord.py exposes the logged-in user via the connection state
    bot._connection.user = SimpleNamespace(id=BOT_ID, name="MAX")
    bot._sent: list[str] = []
    return bot, audit


def fake_message(*, message_id=1, content="hi", author_id=7, author_bot=False,
                 channel_id=10, guild_id=7, mentions=(), reference_id=None,
                 channel=None):
    author = SimpleNamespace(id=author_id, bot=author_bot, roles=[SimpleNamespace(id=33)],
                             __str__=lambda self: f"user{author_id}")
    return SimpleNamespace(
        id=message_id,
        author=author,
        channel=channel or SimpleNamespace(id=channel_id),
        guild=SimpleNamespace(id=guild_id) if guild_id is not None else None,
        content=content,
        mentions=[SimpleNamespace(id=m) for m in mentions],
        reference=SimpleNamespace(message_id=reference_id) if reference_id else None,
    )


# ------------------------------------------------------------------ config
def test_intents_and_permissions_are_least_privilege():
    intents = build_intents()
    assert intents.message_content is True   # needed for no-prefix chat
    assert intents.members is False          # not requested
    assert intents.presences is False        # not requested
    perms = required_permissions()
    assert "Administrator" not in perms and "Manage Server" not in perms
    assert "View Channels" in perms


def test_view_extraction_maps_discord_fields(tmp_path):
    bot, _ = make_bot(tmp_path)
    view = bot._view(fake_message(mentions={BOT_ID}, reference_id=99))
    assert view.author_id == 7
    assert view.channel_id == 10 and view.guild_id == 7
    assert view.bot_id == BOT_ID
    assert BOT_ID in view.mentioned_ids
    assert view.reference_message_id == 99
    assert view.is_dm is False
    assert view.author_roles == {33}

    dm_view = bot._view(fake_message(guild_id=None, channel_id=500))
    assert dm_view.is_dm is True


# ------------------------------------------------------------- dispatching
def test_on_message_sends_router_replies(tmp_path):
    bot, audit = make_bot(tmp_path)
    sent: list[str] = []

    class Channel:
        id = 10

        async def send(self, text):
            sent.append(text)
            return SimpleNamespace(id=len(sent) + 9000)

    msg = fake_message(content="hello max", channel=Channel())
    asyncio.run(bot.on_message(msg))
    assert sent == ["hello there"]
    assert audit.events("discord_reply_sent")


def test_on_message_handles_send_failure(tmp_path):
    bot, audit = make_bot(tmp_path)

    class BrokenChannel:
        id = 10

        async def send(self, text):
            raise RuntimeError("discord down")

    asyncio.run(bot.on_message(fake_message(channel=BrokenChannel())))
    assert audit.events("discord_send_failed")
    # the router itself must not have crashed
    assert audit.events("discord_reply_sent")


def test_on_message_runs_router_off_event_loop(tmp_path):
    """Router (blocking model call) must run in a worker thread."""
    import threading

    seen = {}

    def ask(text, session):
        seen["thread"] = threading.current_thread().name
        return SimpleNamespace(text="ok", ok=True, error=None, flagged=[], rejected=[])

    bot, _ = make_bot(tmp_path, ask=ask)

    class Channel:
        id = 10

        async def send(self, text):
            return SimpleNamespace(id=1)

    asyncio.run(bot.on_message(fake_message(channel=Channel())))
    assert "MainThread" not in seen["thread"]


# -------------------------------------------------------------------- CLI
def test_discord_check_reports_configuration(tmp_path, monkeypatch, capsys):
    for key in list(__import__("os").environ):
        if key.startswith("MAX_DISCORD_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MAX_DISCORD_ENABLE", "0")
    code = main(["discord", "check", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out["token_configured"] is False
    assert out["enabled"] is False
    assert out["dm_mode"] == "off"
    assert out["voice"] == "not enabled in Phase 1"
    assert any("allowlist is empty" in w for w in out["warnings"])
    assert "message_content" in " ".join(out["required_intents"])
    assert "Administrator" not in out["least_privilege_permissions"]


def test_discord_check_fails_when_enabled_without_token(tmp_path, monkeypatch, capsys):
    for key in list(__import__("os").environ):
        if key.startswith("MAX_DISCORD_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MAX_DISCORD_ENABLE", "1")
    monkeypatch.setenv("MAX_DISCORD_ALLOWED_CHANNELS", "10")
    assert main(["discord", "check"]) == 1
    capsys.readouterr()


def test_discord_run_requires_token(tmp_path, monkeypatch, capsys):
    for key in list(__import__("os").environ):
        if key.startswith("MAX_DISCORD_"):
            monkeypatch.delenv(key, raising=False)
    assert main(["discord", "run"]) == 1
    err = capsys.readouterr().err
    assert "MAX_DISCORD_TOKEN" in err and "docs/DISCORD.md" in err

def test_discord_native_setup_commands_are_registered(tmp_path):
    bot, _ = make_bot(tmp_path)
    names = {cmd.name for cmd in bot.tree.get_commands()}
    assert "max_status" in names and "max_enable" in names


def test_server_owner_can_enable_channel_via_slash(tmp_path):
    bot, audit = make_bot(tmp_path)
    guild = SimpleNamespace(id=7, owner_id=7001)
    sent = []

    class Response:
        async def send_message(self, body, *, ephemeral=False):
            sent.append((body, ephemeral))

    interaction = SimpleNamespace(
        guild=guild,
        channel_id=19,
        user=SimpleNamespace(id=7001),
        response=Response(),
    )
    assert not bot.router.store.is_allowed(19, 7)
    command = next(c for c in bot.tree.get_commands() if c.name == "max_enable")
    asyncio.run(command.callback(interaction))
    assert bot.router.store.is_allowed(19, 7)
    assert sent and sent[-1][1] is True
    assert audit.events("discord_slash_channel_enabled")


def test_slash_setup_denies_non_owner_unlisted_user(tmp_path):
    bot, _ = make_bot(tmp_path)
    guild = SimpleNamespace(id=7, owner_id=7001)
    sent = []

    class Response:
        async def send_message(self, body, *, ephemeral=False):
            sent.append(body)

    interaction = SimpleNamespace(
        guild=guild,
        channel_id=19,
        user=SimpleNamespace(id=123),
        response=Response(),
    )
    command = next(c for c in bot.tree.get_commands() if c.name == "max_enable")
    asyncio.run(command.callback(interaction))
    assert not bot.router.store.is_allowed(19, 7)
    assert "Only this server" in sent[0]
