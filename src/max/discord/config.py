"""Discord configuration — environment-driven, secrets never persisted here.

Design rules:
* off by default (`enable=False`) so existing deployments are unchanged,
* channel allowlist is explicit: conversation happens only where listed,
* DM handling requires an explicit privacy mode (`off` default),
* the token is read from the environment and never logged or serialized.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from ..config import REPO_ROOT

DM_MODES = ("off", "allowlist", "everyone")
RESPONSE_MODES = ("all", "mention", "command")


def _ids(raw: str) -> set[int]:
    """Parse a comma list of IDs, tolerating `<#123>`, `<@!444>`, spaces."""
    out: set[int] = set()
    strip = str.maketrans("", "", "<>@#!& \t")
    for part in (raw or "").split(","):
        cleaned = part.translate(strip)
        if cleaned.isdigit():
            out.add(int(cleaned))
    return out


@dataclass
class DiscordConfig:
    enable: bool = False
    token: str = ""  # env-only secret; excluded from repr/redacted output
    command_prefix: str = "!max"

    allowed_channels: set[int] = field(default_factory=set)
    ignored_channels: set[int] = field(default_factory=set)
    allowed_guilds: set[int] = field(default_factory=set)  # empty = any guild

    admin_users: set[int] = field(default_factory=set)
    admin_roles: set[int] = field(default_factory=set)

    dm_mode: str = "off"  # off | allowlist | everyone
    dm_users: set[int] = field(default_factory=set)

    response_mode: str = "all"  # all | mention | command
    cooldown_s: float = 2.0
    provider_error_cooldown_s: float = 30.0
    max_message_len: int = 1900
    history_limit: int = 12

    allowlist_file: Path = REPO_ROOT / "var" / "discord" / "allowlist.json"
    repo_root: Path = REPO_ROOT

    # ------------------------------------------------------------------ api
    def is_admin(self, user_id: int, role_ids: set[int] | None = None) -> bool:
        if user_id in self.admin_users:
            return True
        if role_ids and self.admin_roles and (set(role_ids) & self.admin_roles):
            return True
        return False

    def guild_allowed(self, guild_id: int | None) -> bool:
        if guild_id is None:
            return False
        if self.allowed_guilds and guild_id not in self.allowed_guilds:
            return False
        return True

    def redacted(self) -> dict:
        return {
            "enable": self.enable,
            "token_set": bool(self.token),
            "command_prefix": self.command_prefix,
            "allowed_channels": sorted(self.allowed_channels),
            "ignored_channels": sorted(self.ignored_channels),
            "allowed_guilds": sorted(self.allowed_guilds),
            "admin_users": sorted(self.admin_users),
            "admin_roles": sorted(self.admin_roles),
            "dm_mode": self.dm_mode,
            "dm_users": sorted(self.dm_users),
            "response_mode": self.response_mode,
            "cooldown_s": self.cooldown_s,
            "provider_error_cooldown_s": self.provider_error_cooldown_s,
            "max_message_len": self.max_message_len,
            "history_limit": self.history_limit,
            "allowlist_file": str(self.allowlist_file),
        }

    # -------------------------------------------------------------- factory
    @classmethod
    def from_env(cls, repo_root: Path | None = None) -> "DiscordConfig":
        root = (repo_root or REPO_ROOT).resolve()

        def env(name: str, default: str = "") -> str:
            return os.environ.get(name, default).strip()

        def envb(name: str, default: bool = False) -> bool:
            raw = env(name)
            if not raw:
                return default
            return raw.lower() in {"1", "true", "yes", "on"}

        dm_mode = (env("MAX_DISCORD_DM_MODE", "off") or "off").lower()
        if dm_mode not in DM_MODES:
            dm_mode = "off"
        response_mode = (env("MAX_DISCORD_RESPONSE_MODE", "all") or "all").lower()
        if response_mode not in RESPONSE_MODES:
            response_mode = "all"

        allowlist_file = env("MAX_DISCORD_ALLOWLIST_FILE", "var/discord/allowlist.json")
        path = Path(allowlist_file)
        if not path.is_absolute():
            path = root / path

        return cls(
            enable=envb("MAX_DISCORD_ENABLE", False),
            token=env("MAX_DISCORD_TOKEN"),
            command_prefix=env("MAX_DISCORD_COMMAND_PREFIX", "!max") or "!max",
            allowed_channels=_ids(env("MAX_DISCORD_ALLOWED_CHANNELS")),
            ignored_channels=_ids(env("MAX_DISCORD_IGNORED_CHANNELS")),
            allowed_guilds=_ids(env("MAX_DISCORD_GUILD_IDS")),
            # Existing Railway installations stored the owner IDs under
            # MAX_OWNER_DISCORD_IDS. Use them only when the new admin
            # allowlist is absent; never make all guild members admins.
            admin_users=_ids(env("MAX_DISCORD_ADMINS") or env("MAX_OWNER_DISCORD_IDS")),
            admin_roles=_ids(env("MAX_DISCORD_ADMIN_ROLES")),
            dm_mode=dm_mode,
            dm_users=_ids(env("MAX_DISCORD_DM_USERS")),
            response_mode=response_mode,
            cooldown_s=float(env("MAX_DISCORD_COOLDOWN_S", "2") or 2),
            provider_error_cooldown_s=float(env("MAX_DISCORD_ERROR_COOLDOWN_S", "30") or 30),
            max_message_len=min(2000, int(env("MAX_DISCORD_MAX_MESSAGE_LEN", "1900") or 1900)),
            history_limit=int(env("MAX_DISCORD_HISTORY_LIMIT", "12") or 12),
            allowlist_file=path,
            repo_root=root,
        )

    @classmethod
    def disabled(cls) -> "DiscordConfig":
        return cls()