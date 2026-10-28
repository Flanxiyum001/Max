"""discord.py transport. All decisions live in `router.py` — this is I/O only."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from ..core.tools import AuditLog
from .config import DiscordConfig
from .router import DiscordRouter, MessageView

log = logging.getLogger("max.discord")

try:  # discord.py is an optional runtime dependency (Phase 1 feature)
    import discord
except ImportError:  # pragma: no cover - exercised only without the extra
    discord = None  # type: ignore[assignment]


def build_intents():
    """Least-privilege intents: message content is the only privileged one."""
    if discord is None:
        raise RuntimeError("discord.py is not installed; pip install discord.py>=2.3")
    intents = discord.Intents.default()
    intents.message_content = True  # required for no-prefix conversation
    intents.members = False         # not needed; roles come with the message
    intents.presences = False
    return intents


def required_permissions() -> list[str]:
    """Least-privilege permission list documented in docs/DISCORD.md."""
    return [
        "View Channels",
        "Send Messages",
        "Send Messages in Threads",
        "Read Message History",
        "Use Slash/Focus? (not required in Phase 1)",
    ]


def _is_sendable(channel: Any) -> bool:
    if discord is None:
        return True
    return not isinstance(
        channel,
        (discord.VoiceChannel, discord.StageChannel, discord.CategoryChannel,
         discord.ForumChannel),
    )


class MaxDiscordBot(discord.Client if discord is not None else object):  # type: ignore[misc]
    """Text-only MAX bot: no voice or music dependencies are loaded."""

    def __init__(self, config: DiscordConfig, router: DiscordRouter, audit: AuditLog):
        if discord is None:
            raise RuntimeError("discord.py is not installed; pip install discord.py>=2.3")
        super().__init__(intents=build_intents())
        self.config = config
        self.router = router
        self.audit = audit
        self.tree = discord.app_commands.CommandTree(self)
        self._synced_guilds: set[int] = set()
        self._last_route_log: dict[tuple[int, str], float] = {}

        # Discord-native setup works without reading message content and
        # without any pre-authorized conversation channel.
        @self.tree.command(name="max_status", description="Check MAX's Discord setup and AI readiness")
        async def max_status(interaction: discord.Interaction) -> None:
            guild = interaction.guild
            channel_id = interaction.channel_id
            if guild is None or channel_id is None:
                await interaction.response.send_message(
                    "Run this in a server text channel, not a DM.", ephemeral=True
                )
                return
            owner = self._is_setup_admin(interaction.user.id, guild)
            allowed = self.router.store.is_allowed(channel_id, guild.id)
            provider = "unavailable"
            provider_ready = False
            if self.router.health_fn is not None:
                try:
                    health = await asyncio.to_thread(self.router.health_fn)
                    status = health.get("provider_check") or {}
                    provider = str(status.get("provider") or "unknown")
                    provider_ready = bool(status.get("key_configured")) or provider == "mock"
                except Exception as exc:
                    self.audit.record("discord_status_probe_failed", error=type(exc).__name__)
            lines = [
                f"MAX connected. Channel enabled: {allowed}.",
                f"Setup permission: {owner}. Provider: {provider}; key present: {provider_ready}.",
            ]
            if not allowed:
                lines.append("Server owner or configured MAX admin: run /max_enable here.")
            await interaction.response.send_message("\n".join(lines), ephemeral=True)

        @self.tree.command(name="max_enable", description="Allow MAX to converse in this channel (server owner/admin only)")
        async def max_enable(interaction: discord.Interaction) -> None:
            guild = interaction.guild
            channel_id = interaction.channel_id
            if guild is None or channel_id is None:
                await interaction.response.send_message(
                    "This command requires a server text channel.", ephemeral=True
                )
                return
            if not self.config.guild_allowed(guild.id):
                await interaction.response.send_message(
                    "This server is not authorized by MAX's guild allowlist.", ephemeral=True
                )
                return
            if not self._is_setup_admin(interaction.user.id, guild):
                self.audit.record("discord_setup_denied", guild=guild.id, channel=channel_id)
                await interaction.response.send_message(
                    "Only this server's owner or a configured MAX admin can enable a channel.",
                    ephemeral=True,
                )
                return
            try:
                enabled = await asyncio.to_thread(
                    self.router.store.add, channel_id, interaction.user.id, guild_id=guild.id
                )
            except OSError as exc:
                self.audit.record("discord_setup_write_failed", error=type(exc).__name__)
                await interaction.response.send_message(
                    "MAX could not save the channel settings. Check Railway volume permissions.",
                    ephemeral=True,
                )
                return
            if not enabled:
                await interaction.response.send_message(
                    "This channel is explicitly ignored by MAX's policy.", ephemeral=True
                )
                return
            self.audit.record("discord_slash_channel_enabled", guild=guild.id, channel=channel_id)
            await interaction.response.send_message(
                "This channel is enabled. Send `hi` in the channel to test Max.",
                ephemeral=True,
            )

    def _is_setup_admin(self, user_id: int, guild: Any) -> bool:
        return self.config.is_admin(user_id) or (
            getattr(guild, "owner_id", None) is not None
            and user_id == guild.owner_id
        )

    async def _sync_setup_commands(self, guild: Any) -> None:
        if guild.id in self._synced_guilds:
            return
        try:
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        except Exception as exc:  # noqa: BLE001 - never stop gateway if Discord API fails
            self.audit.record(
                "discord_slash_sync_failed", guild=guild.id, error=type(exc).__name__
            )
            log.warning("MAX slash setup unavailable for guild %s (%s)", guild.id, type(exc).__name__)
            print(f"MAX_SLASH_SYNC_FAILED guild={guild.id} error={type(exc).__name__}", flush=True)
        else:
            self._synced_guilds.add(guild.id)
            log.info("MAX_SETUP_COMMANDS_READY guild=%s", guild.id)
            print(f"MAX_SETUP_COMMANDS_READY guild={guild.id}", flush=True)

    async def on_guild_join(self, guild: Any) -> None:
        await self._sync_setup_commands(guild)

    # ------------------------------------------------------------- views
    def _view(self, message: Any) -> MessageView:
        author = message.author
        guild = message.guild
        reference = getattr(message, "reference", None)
        roles = {r.id for r in (getattr(author, "roles", None) or [])}
        return MessageView(
            message_id=message.id,
            author_id=author.id,
            author_name=str(author),
            channel_id=message.channel.id,
            guild_id=guild.id if guild is not None else None,
            content=getattr(message, "content", "") or "",
            is_bot=bool(getattr(author, "bot", False)),
            is_dm=guild is None,
            mentioned_ids={u.id for u in getattr(message, "mentions", []) or []},
            bot_id=self.user.id if self.user else None,
            reference_message_id=getattr(reference, "message_id", None),
            author_roles=roles,
            guild_owner_id=getattr(guild, "owner_id", None),
        )

    # ------------------------------------------------------------ events
    async def on_ready(self) -> None:
        user_id = self.user.id if self.user else None
        guilds = len(getattr(self, "guilds", []) or [])
        self.audit.record(
            "discord_ready", user_id=user_id, guilds=guilds,
            allowed_channels=len(self.config.allowed_channels),
        )
        log.info("MAX discord ready as %s in %s guild(s)", user_id, guilds)
        # A running container does not prove the Discord gateway connected.
        # Emit a non-secret readiness event to stdout for worker-only hosts.
        print(f"MAX_DISCORD_READY user_id={user_id} guilds={guilds}", flush=True)
        for guild in getattr(self, "guilds", []) or []:
            await self._sync_setup_commands(guild)

    async def on_message(self, message: Any) -> None:
        if not _is_sendable(message.channel):
            return
        if message.author.id == (self.user.id if self.user else -1):
            return
        view = self._view(message)
        # the agent is blocking (HTTP/IO) — keep the event loop free
        try:
            result = await asyncio.to_thread(self.router.handle, view)
        except Exception as exc:  # noqa: BLE001 - report unexpected router failures
            self.audit.record(
                "discord_router_crashed", channel=view.channel_id,
                error=type(exc).__name__,
            )
            log.exception("MAX failed to route Discord message")
            try:
                await message.channel.send(
                    "MAX encountered an internal error while handling your message."
                )
            except Exception:
                pass
            return
        # Log outcome metadata only (never message content, author IDs,
        # prompts or provider secrets). Rate-limit suppressed-message logs.
        route_key = (view.channel_id, result.reason or result.action)
        now = time.monotonic()
        last_log = self._last_route_log.get(route_key, -1e10)
        if result.replies or (now - last_log >= 60.0):
            self._last_route_log[route_key] = now
            print(
                f"MAX_MESSAGE_ROUTED channel={view.channel_id} "
                f"action={result.action} reason={result.reason or '-'} "
                f"chunks={len(result.replies)} error={result.error}",
                flush=True,
            )
        for chunk in result.replies:
            try:
                sent = await message.channel.send(chunk)
            except Exception as exc:  # noqa: BLE001 - transport failure, audited
                self.audit.record(
                    "discord_send_failed", channel=view.channel_id,
                    error=type(exc).__name__,
                )
                print(
                    f"MAX_MESSAGE_SEND_FAILED channel={view.channel_id} "
                    f"error={type(exc).__name__}",
                    flush=True,
                )
                break
            if sent is not None:
                self.router.remember_own_message(sent.id)

    async def on_error(self, event: str, *args: Any) -> None:
        self.audit.record("discord_event_error", event=event)
        log.exception("discord error in %s", event)