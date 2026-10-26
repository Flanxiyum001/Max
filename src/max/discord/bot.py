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
        self.audit.record(
            "discord_ready", user_id=user_id, guilds=guilds,
            allowed_channels=len(self.config.allowed_channels),
        )
        log.info("MAX discord ready as %s in %s guild(s)", user_id, guilds)

    async def on_message(self, message: Any) -> None:
        if not _is_sendable(message.channel):
            return
        if message.author.id == (self.user.id if self.user else -1):
            return
        view = self._view(message)
        # the agent is blocking (HTTP/IO) — keep the event loop free
        result = await asyncio.to_thread(self.router.handle, view)
        for chunk in result.replies:
            try:
                sent = await message.channel.send(chunk)
            except Exception as exc:  # noqa: BLE001 - transport failure, audited
                self.audit.record(
                    "discord_send_failed", channel=view.channel_id,
                    error=type(exc).__name__,
                )
                break
            if sent is not None:
                self.router.remember_own_message(sent.id)

    async def on_error(self, event: str, *args: Any) -> None:
        self.audit.record("discord_event_error", event=event)
        log.exception("discord error in %s", event)