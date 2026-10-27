"""discord.py transport. All decisions live in `router.py` — this is I/O only."""

from __future__ import annotations

import asyncio
import logging
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
    ]


def startup_summary(
    config: DiscordConfig,
    store,
    *,
    provider_label: str = "unknown",
    guild_count: int = 0,
) -> dict:
    """Effective, secret-free runtime configuration for the startup banner.

    Pure function so it is unit-testable without discord.py: counts,
    policies and a sanitized provider name, never a token or key.
    """
    return {
        "prefix": config.command_prefix,
        "response_mode": config.response_mode,
        "dm_mode": config.dm_mode,
        "allowlisted_channels": len(store.effective_allowlist()),
        "configured_channels": len(config.allowed_channels),
        "ignored_channels": len(config.ignored_channels),
        "admin_users": len(config.admin_users),
        "admin_roles": len(config.admin_roles),
        "guild_allowlist": len(config.allowed_guilds),
        "provider": provider_label,
        "guilds": guild_count,
        "intents": ["guilds", "message_content"],
    }


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

    def __init__(
        self,
        config: DiscordConfig,
        router: DiscordRouter,
        audit: AuditLog,
        provider_label: str = "unknown",
    ):
        if discord is None:
            raise RuntimeError("discord.py is not installed; pip install discord.py>=2.3")
        super().__init__(intents=build_intents())
        self.config = config
        self.router = router
        self.audit = audit
        self.provider_label = provider_label

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
        )

    # ------------------------------------------------------------ events
    async def on_ready(self) -> None:
        user_id = self.user.id if self.user else None
        guilds = len(getattr(self, "guilds", []) or [])
        summary = startup_summary(
            self.config, self.store, provider_label=self.provider_label, guild_count=guilds
        )
        self.audit.record("discord_ready", user_id=user_id, **summary)
        log.info(
            "MAX discord READY as %s | prefix=%s | response_mode=%s | dm_mode=%s | "
            "allowlist=%s channel(s) configured=%s ignored=%s | admins=%s/%s | "
            "provider=%s | guilds=%s | intents=%s",
            user_id,
            summary["prefix"],
            summary["response_mode"],
            summary["dm_mode"],
            summary["allowlisted_channels"],
            summary["configured_channels"],
            summary["ignored_channels"],
            summary["admin_users"],
            summary["admin_roles"],
            summary["provider"],
            summary["guilds"],
            ",".join(summary["intents"]),
        )
        if not summary["allowlisted_channels"] and summary["dm_mode"] == "off":
            log.warning(
                "channel allowlist is empty and DMs are off — MAX will not "
                "reply to anyone until an admin runs '%s allow <#channel>'",
                summary["prefix"],
            )

    @property
    def store(self):
        # the router owns the channel store; keep `on_ready` readable
        return self.router.store

    async def on_message(self, message: Any) -> None:
        if not _is_sendable(message.channel):
            self.audit.record(
                "discord_message_outcome", action="ignored", reason="unsentable_channel",
                channel=message.channel.id,
            )
            return
        if message.author.id == (self.user.id if self.user else -1):
            return
        view = self._view(message)
        # the agent is blocking (HTTP/IO) — keep the event loop free
        try:
            async with message.channel.typing():
                result = await asyncio.to_thread(self.router.handle, view)
        except Exception:  # noqa: BLE001 - typing indicator is best effort only
            result = await asyncio.to_thread(self.router.handle, view)
        for chunk in result.replies:
            try:
                sent = await message.channel.send(chunk)
            except Exception as exc:  # noqa: BLE001 - transport failure, audited
                self.audit.record(
                    "discord_send_failed", channel=view.channel_id,
                    error=type(exc).__name__,
                    action=result.action, reason=result.reason or None,
                )
                log.warning("discord send failed in %s: %s", view.channel_id, type(exc).__name__)
                break
            if sent is not None:
                self.router.remember_own_message(sent.id)

    async def on_message_edit(self, before: Any, after: Any) -> None:
        """Edits are traced but never answered — replying to edits loops."""
        self.audit.record(
            "discord_message_edited",
            channel=after.channel.id,
            has_text=bool((getattr(after, "content", "") or "").strip()),
        )

    async def on_error(self, event: str, *args: Any) -> None:
        self.audit.record("discord_event_error", event=event)
        log.exception("discord error in %s", event)