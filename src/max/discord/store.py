"""Persisted channel allowlist overlay + configuration-change audit.

The effective allowlist is:

    (env base ∪ admin additions) − admin removals,   ignored_channels always win

Environment is the operator's baseline; runtime changes made by admins via
`!max allow/deny` are stored in a JSON file under the data directory (never
in `.env`) and every change is audited with actor, target and outcome.
"""

from __future__ import annotations

import json
import os
import time
from threading import RLock
from pathlib import Path

from ..core.tools import AuditLog
from .config import DiscordConfig


class ChannelStore:
    def __init__(self, config: DiscordConfig, audit: AuditLog | None = None):
        self.config = config
        self.path = Path(config.allowlist_file)
        self.audit = audit
        self._lock = RLock()
        # A missing "awake" entry means sleeping; old allowlist files remain valid.
        self._data: dict = {"added": {}, "removed": {}, "awake": {}}
        self._load()

    # ------------------------------------------------------------- state
    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except ValueError:
            return
        if isinstance(data, dict):
            self._data["added"] = dict(data.get("added") or {})
            self._data["removed"] = dict(data.get("removed") or {})
            awake = data.get("awake")
            if isinstance(awake, dict):
                self._data["awake"] = {
                    k: v for k, v in awake.items()
                    if k.isdecimal() and isinstance(v, dict) and isinstance(v.get("guild"), int)
                }

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)

    # -------------------------------------------------------------- query
    def is_allowed(self, channel_id: int, guild_id: int | None) -> bool:
        """Single authorization point used by the router."""
        if channel_id in self.config.ignored_channels:
            return False
        if not self.config.guild_allowed(guild_id):
            return False
        base = channel_id in self.config.allowed_channels
        added = str(channel_id) in self._data["added"]
        removed = str(channel_id) in self._data["removed"]
        if removed:
            return False
        return base or added

    def effective_allowlist(self) -> set[int]:
        base = set(self.config.allowed_channels)
        base |= {int(k) for k in self._data["added"]}
        base -= {int(k) for k in self._data["removed"]}
        base -= set(self.config.ignored_channels)
        return base

    def is_awake(self, channel_id: int, guild_id: int | None) -> bool:
        """Activation is per guild channel and never bypasses the allowlist."""
        if not self.is_allowed(channel_id, guild_id):
            return False
        with self._lock:
            record = self._data["awake"].get(str(channel_id))
            return isinstance(record, dict) and record.get("guild") == guild_id

    def set_awake(
        self, channel_id: int, guild_id: int | None, actor_id: int, awake: bool
    ) -> bool:
        """Persist the explicit conversational opt-in; deny unapproved channels."""
        if guild_id is None or not self.is_allowed(channel_id, guild_id):
            return False
        key = str(channel_id)
        with self._lock:
            if awake:
                if not self.is_awake(channel_id, guild_id):
                    self._data["awake"][key] = {
                        "guild": guild_id, "actor": actor_id, "ts": _now()
                    }
                    self._save()
            elif self._data["awake"].pop(key, None) is not None:
                self._save()
        self._record(
            "discord_wake_changed", channel=channel_id, guild=guild_id,
            action="wake" if awake else "sleep", actor=actor_id,
        )
        return True

    def changes(self) -> dict:
        return {"added": dict(self._data["added"]), "removed": dict(self._data["removed"])}

    # ------------------------------------------------------------ mutation
    def add(self, channel_id: int, actor_id: int, *, guild_id: int | None = None) -> bool:
        if channel_id in self.config.ignored_channels:
            self._record("discord_config_change", actor=actor_id, channel=channel_id,
                         outcome="denied", reason="channel is on the ignore list")
            return False
        key = str(channel_id)
        changed = False
        if key not in self._data["added"]:
            self._data["added"][key] = {"ts": _now(), "actor": actor_id, "guild": guild_id}
            changed = True
        if self._data["removed"].pop(key, None) is not None:
            changed = True
        if changed:
            self._save()
        self._record("discord_config_change", actor=actor_id, channel=channel_id,
                     action="allow", outcome="applied", changed=changed)
        return True

    def remove(self, channel_id: int, actor_id: int, *, guild_id: int | None = None) -> bool:
        key = str(channel_id)
        changed = False
        if key in self._data["added"]:
            self._data["added"].pop(key)
            changed = True
        if self._data["awake"].pop(key, None) is not None:
            changed = True
        if key not in self.config.allowed_channels and key not in self._data["removed"]:
            # never in the base list: record an explicit deny anyway (belt and braces)
            pass
        if key not in self._data["removed"]:
            self._data["removed"][key] = {"ts": _now(), "actor": actor_id, "guild": guild_id}
            changed = True
        if changed:
            self._save()
        self._record("discord_config_change", actor=actor_id, channel=channel_id,
                     action="deny", outcome="applied", changed=changed)
        return True

    # -------------------------------------------------------------- audit
    def _record(self, event: str, **detail) -> None:
        if self.audit is not None:
            self.audit.record(event, **detail)


def _now() -> float:
    return round(time.time(), 3)