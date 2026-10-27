"""Framework-free Discord message router — all decisions happen here.

`bot.py` is only a transport: it converts a discord.py message into a
`MessageView`, calls `DiscordRouter.handle`, and sends back the returned
replies. Because the router has no discord dependency, every authorization,
loop-protection, rate-limit and privacy rule is unit-testable with fakes.

Decision order (fail closed):
  1. ignore self / other bots / duplicate message ids
  2. explicit commands (authorization checked at this execution layer)
  3. conversation: guild allowlist → DM policy → response mode → rate limits
     → provider backoff → agent → chunked replies
"""

from __future__ import annotations

import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any, Callable

from ..core.tools import AuditLog
from ..core.usermemory import MemoryError_, UserMemoryStore
from .config import DiscordConfig
from .ratelimit import FailureBackoff, RateLimiter, chunk_message
from .store import ChannelStore

# --------------------------------------------------------------- data model


@dataclass
class MessageView:
    message_id: int
    author_id: int
    author_name: str
    channel_id: int
    guild_id: int | None
    content: str
    is_bot: bool = False
    is_dm: bool = False
    mentioned_ids: set[int] = field(default_factory=set)
    bot_id: int | None = None
    reference_message_id: int | None = None
    author_roles: set[int] = field(default_factory=set)


@dataclass
class Command:
    name: str
    args: str


@dataclass
class RouteResult:
    action: str  # reply | command | suppressed | ignored
    replies: list[str] = field(default_factory=list)
    reason: str = ""
    session_id: str | None = None
    command: str | None = None
    error: bool = False
    audit_event: str | None = None


# Commands available to everyone (in permitted contexts)
PUBLIC_COMMANDS = {"help", "health", "status", "perms", "memory", "task", "tasks"}
# Commands that require an administrator (checked at execution time)
ADMIN_COMMANDS = {"channels", "allow", "deny"}

USAGE = (
    "MAX commands:\n"
    "  !max help — this list\n"
    "  !max health | status | perms — service and permission diagnostics\n"
    "  !max memory show|set <key> <value>|forget <key>|clear — your own memory\n"
    "  !max tasks — delegated task list (yours)\n"
    "  !max task run <description> — plan and run a delegated task\n"
    "  !max task <id> | diff <id> | cancel <id> — status, diff, cancellation\n"
    "Admin only:\n"
    "  !max channels — effective channel allowlist\n"
    "  !max allow <#channel|id> / !max deny <#channel|id> — edit allowlist"
)


class DiscordRouter:
    def __init__(
        self,
        config: DiscordConfig,
        store: ChannelStore,
        audit: AuditLog,
        ask_fn: Callable[[str, str], Any],
        *,
        memory: UserMemoryStore | None = None,
        health_fn: Callable[[], dict] | None = None,
        delegate_fn: Callable[["MessageView", str], str | None] | None = None,
        orch_fn: Callable[[str, str, "MessageView"], list[str]] | None = None,
        clock=time.monotonic,
        limiter: RateLimiter | None = None,
        backoff: FailureBackoff | None = None,
        seen_size: int = 512,
    ):
        self.config = config
        self.store = store
        self.audit = audit
        self.ask_fn = ask_fn
        self.memory = memory
        self.health_fn = health_fn
        self.delegate_fn = delegate_fn
        self.orch_fn = orch_fn
        self._clock = clock
        self.limiter = limiter or RateLimiter(
            cooldown_s=config.cooldown_s, clock=clock
        )
        self.backoff = backoff or FailureBackoff(
            base_s=config.provider_error_cooldown_s, clock=clock
        )
        self._seen: OrderedDict[int, None] = OrderedDict()
        self._seen_size = max(16, seen_size)
        self._own_messages: deque[int] = deque(maxlen=256)
        self._suppress_audited: dict[tuple[int, str], float] = {}

    # ------------------------------------------------------------ helpers
    def remember_own_message(self, message_id: int) -> None:
        self._own_messages.append(message_id)

    def _seen_message(self, message_id: int) -> bool:
        if message_id in self._seen:
            return True
        self._seen[message_id] = None
        while len(self._seen) > self._seen_size:
            self._seen.popitem(last=False)
        return False

    def _audit_suppressed(self, view: MessageView, reason: str) -> None:
        # avoid audit flooding from busy non-allowlisted channels: at most one
        # entry per (channel, reason) per 60s — always allowed if mentioned
        key = (view.channel_id, reason)
        now = self._clock()
        force = view.bot_id in view.mentioned_ids
        last = self._suppress_audited.get(key)
        if not force and last is not None and now - last < 60.0:
            return
        self._suppress_audited[key] = now
        self.audit.record(
            "discord_reply_suppressed",
            channel=view.channel_id,
            guild=view.guild_id,
            reason=reason,
            author=view.author_id,
        )

    def session_id(self, view: MessageView) -> str:
        if view.is_dm or view.guild_id is None:
            return f"dm_u{view.author_id}"
        return f"g{view.guild_id}_c{view.channel_id}"

    # ------------------------------------------------------------- routing
    def handle(self, view: MessageView) -> RouteResult:
        # 1. loop and duplicate protection ----------------------------------
        if view.bot_id is not None and view.author_id == view.bot_id:
            return RouteResult("ignored", reason="own_message")
        if view.is_bot:
            self.audit.record("discord_ignored", reason="other_bot", channel=view.channel_id)
            return RouteResult("ignored", reason="other_bot")
        if self._seen_message(view.message_id):
            return RouteResult("ignored", reason="duplicate_message")

        content = (view.content or "").strip()
        if not content:
            return RouteResult("ignored", reason="empty_message")

        # 2. explicit commands ---------------------------------------------
        command = self._parse_command(content)
        if command is not None:
            return self._handle_command(command, view)

        # 3. conversation ---------------------------------------------------
        return self._handle_conversation(view, content)

    # ------------------------------------------------------------- commands
    def _parse_command(self, content: str) -> Command | None:
        prefix = self.config.command_prefix
        if not prefix:
            return None
        if not content.lower().startswith(prefix.lower()):
            return None
        rest = content[len(prefix):].strip()
        if not rest:
            return Command("help", "")
        name, _, args = rest.partition(" ")
        return Command(name.lower(), args.strip())

    def _dm_ok(self, view: MessageView, reason_prefix: str) -> bool:
        if view.is_dm and self.config.dm_mode == "off":
            self._audit_suppressed(view, f"{reason_prefix}:dm_disabled")
            return False
        return True

    def _handle_command(self, command: Command, view: MessageView) -> RouteResult:
        # ignored channels suppress everything (explicit escape hatch)
        if not view.is_dm and view.channel_id in self.config.ignored_channels:
            self._audit_suppressed(view, "ignored_channel")
            return RouteResult("suppressed", reason="ignored_channel")
        # DM privacy policy applies to every bot reply, commands included
        if view.is_dm and not self._dm_ok(view, "command"):
            return RouteResult("suppressed", reason="dm_disabled")

        name, args = command.name, command.args

        if name not in PUBLIC_COMMANDS and name not in ADMIN_COMMANDS:
            replies = [f"Unknown command '{name}'.\n{USAGE}"]
            self.audit.record("discord_command", command=name, outcome="unknown",
                              actor=view.author_id, channel=view.channel_id)
            return RouteResult("command", replies=replies, command=name,
                               audit_event="discord_command")

        # ---- central authorization for privileged commands ----------------
        if name in ADMIN_COMMANDS:
            is_admin = self.config.is_admin(view.author_id, view.author_roles)
            if not is_admin:
                self.audit.record(
                    "discord_command_denied", command=name, actor=view.author_id,
                    channel=view.channel_id, guild=view.guild_id,
                    reason="caller_is_not_admin",
                )
                return RouteResult(
                    "command",
                    replies=["Authorization denied: this command requires an administrator."],
                    command=name,
                    error=True,
                    audit_event="discord_command_denied",
                )
            result = self._admin_command(name, args, view)
        else:
            result = self._public_command(name, args, view)

        result.command = name
        self.audit.record(
            "discord_command", command=name, actor=view.author_id,
            channel=view.channel_id, outcome="executed", admin=name in ADMIN_COMMANDS,
        )
        return result

    def _public_command(self, name: str, args: str, view: MessageView) -> RouteResult:
        if name == "help":
            return RouteResult("command", replies=[USAGE])
        if name == "health":
            payload = self.health_fn() if self.health_fn else {"status": "unknown"}
            status = payload.get("status", "unknown")
            version = payload.get("version", "-")
            provider = (payload.get("provider_check") or {}).get("provider", "-")
            lines = [f"health: {status} | max {version} | provider: {provider}"]
            pol = payload.get("policy") or {}
            if pol:
                lines.append(f"policy integrity: {'ok' if pol.get('ok') else 'FAILED'}")
            skills = payload.get("skills") or {}
            if skills:
                lines.append(f"skills: {skills.get('valid', 0)} valid, {skills.get('flagged', 0)} flagged")
            return RouteResult("command", replies=lines)
        if name == "status":
            allowed = (
                self.store.is_allowed(view.channel_id, view.guild_id)
                if not view.is_dm
                else False
            )
            dm_ok = (not view.is_dm) or self.config.dm_mode != "off"
            lines = [
                f"response_mode: {self.config.response_mode}",
                f"this channel allowlisted: {allowed}",
                f"dm_mode: {self.config.dm_mode}",
                f"commands here: {'yes' if dm_ok else 'no (dm policy)'}",
                f"caller admin: {self.config.is_admin(view.author_id, view.author_roles)}",
                f"provider backoff active: {self.backoff.suppressed()}",
            ]
            return RouteResult("command", replies=lines)
        if name == "perms":
            lines = [
                f"you: id={view.author_id} name={view.author_name}",
                f"you are admin: {self.config.is_admin(view.author_id, view.author_roles)}",
                f"guild: {view.guild_id}",
                f"channel: {view.channel_id} allowlisted="
                f"{self.store.is_allowed(view.channel_id, view.guild_id) if not view.is_dm else 'dm'}",
            ]
            return RouteResult("command", replies=lines)
        if name == "memory":
            return self._memory_command(args, view)
        if name in {"task", "tasks"}:
            if self.orch_fn is None:
                return RouteResult(
                    "command",
                    replies=["multi-agent orchestration is disabled (MAX_ORCH_ENABLE=0)"],
                    error=True,
                )
            try:
                replies = self.orch_fn(name, args, view)
            except Exception as exc:  # noqa: BLE001 - orchestration must not kill the bot
                self.audit.record("discord_task_error", actor=view.author_id, error=type(exc).__name__)
                return RouteResult(
                    "command",
                    replies=[f"task operation failed ({type(exc).__name__})"],
                    error=True,
                )
            self.audit.record(
                "discord_task_command",
                sub=(args.split()[0] if args.split() else "list")[0],
                actor=view.author_id,
                channel=view.channel_id,
            )
            return RouteResult(
                "command",
                replies=[
                    part
                    for reply in replies
                    for part in chunk_message(reply, self.config.max_message_len)
                ]
                or ["no result"],
                error=False,
            )
        return RouteResult("command", replies=["unsupported command"], error=True)

    def _memory_command(self, args: str, view: MessageView) -> RouteResult:
        if self.memory is None:
            return RouteResult("command", replies=["memory is not enabled"], error=True)
        subject = view.author_id  # always caller-scoped; no way to address others
        parts = args.split(maxsplit=1)
        sub = (parts[0] if parts else "show").lower()
        rest = parts[1] if len(parts) > 1 else ""
        try:
            if sub == "show":
                body = self.memory.show(subject)
                stats = self.memory.stats(subject)
                reply = f"{body}\n({stats['entries']} entries, {stats['chars']} chars, yours only)"
            elif sub == "set":
                kv = rest.split(maxsplit=1)
                if len(kv) != 2:
                    return RouteResult("command", replies=["usage: !max memory set <key> <value>"])
                reply = self.memory.set(subject, kv[0], kv[1])
            elif sub == "forget":
                if not rest:
                    return RouteResult("command", replies=["usage: !max memory forget <key>"])
                reply = self.memory.forget(subject, rest.strip().split()[0])
            elif sub == "clear":
                reply = self.memory.clear(subject)
            else:
                return RouteResult(
                    "command",
                    replies=["usage: !max memory show|set <key> <value>|forget <key>|clear"],
                )
        except MemoryError_ as exc:
            self.audit.record("discord_memory_error", actor=view.author_id, detail=str(exc))
            return RouteResult("command", replies=[f"memory error: {exc}"], error=True)
        return RouteResult("command", replies=[reply])

    def _admin_command(self, name: str, args: str, view: MessageView) -> RouteResult:
        if name == "channels":
            effective = sorted(self.store.effective_allowlist())
            if not effective:
                body = "allowlist is empty — no channel can receive conversation replies"
            else:
                body = "allowlisted channels:\n" + "\n".join(f"- <#{cid}>" for cid in effective)
            ignored = sorted(self.config.ignored_channels)
            if ignored:
                body += "\nignored (never replied to):\n" + "\n".join(f"- <#{cid}>" for cid in ignored)
            return RouteResult("command", replies=chunk_message(body, self.config.max_message_len))

        target = self._parse_channel(args, view)
        if target is None:
            return RouteResult(
                "command",
                replies=["usage: !max allow <#channel|numeric id> (or pass the current channel id)"],
                error=True,
            )
        if name == "allow":
            ok = self.store.add(target, view.author_id, guild_id=view.guild_id)
            reply = (
                f"channel <#{target}> added to the conversation allowlist"
                if ok
                else f"refused: channel {target} is on the ignore list"
            )
        else:
            ok = self.store.remove(target, view.author_id, guild_id=view.guild_id)
            reply = f"channel <#{target}> removed from the conversation allowlist"
        return RouteResult("command", replies=[reply])

    @staticmethod
    def _parse_channel(args: str, view: MessageView) -> int | None:
        token = args.strip().split()[0] if args.strip() else ""
        token = token.strip("<#>").lstrip("#")
        if token.isdigit():
            return int(token)
        if not args.strip():
            # bare `allow`/`deny` in a guild channel means the current channel
            return view.channel_id if not view.is_dm else None
        return None

    # --------------------------------------------------------- conversation
    def _handle_conversation(self, view: MessageView, content: str) -> RouteResult:
        # channel authorization (fail closed)
        if view.is_dm:
            if self.config.dm_mode == "off":
                self._audit_suppressed(view, "dm_disabled")
                return RouteResult("suppressed", reason="dm_disabled")
            if self.config.dm_mode == "allowlist" and view.author_id not in self.config.dm_users:
                self._audit_suppressed(view, "dm_user_not_allowlisted")
                return RouteResult("suppressed", reason="dm_user_not_allowlisted")
            # dm_mode == "everyone" is an explicit operator opt-in
        else:
            if not self.store.is_allowed(view.channel_id, view.guild_id):
                # A fresh installation has no allowed channels. Give only an
                # explicitly configured administrator a setup hint;
                # all other users still receive no reply until allowlisted.
                if (
                    not self.store.effective_allowlist()
                    and view.channel_id not in self.config.ignored_channels
                    and self.config.guild_allowed(view.guild_id)
                    and self.config.is_admin(view.author_id, view.author_roles)
                ):
                    self.audit.record(
                        "discord_setup_required",
                        actor=view.author_id,
                        channel=view.channel_id,
                    )
                    return RouteResult(
                        "reply",
                        replies=[
                            "MAX is online, but no conversation channels are enabled. "
                            "Send `!max allow` here to enable this channel, then say hi again."
                        ],
                        reason="setup_required",
                    )
                self._audit_suppressed(view, "channel_not_allowlisted")
                return RouteResult("suppressed", reason="channel_not_allowlisted")

        # response behaviour
        mode = self.config.response_mode
        addressed = (
            view.bot_id in view.mentioned_ids
            if view.bot_id is not None
            else False
        ) or (view.reference_message_id in self._own_messages)
        if mode == "command":
            self._audit_suppressed(view, "response_mode_command")
            return RouteResult("suppressed", reason="response_mode_command")
        if mode == "mention" and not addressed:
            self._audit_suppressed(view, "not_addressed")
            return RouteResult("suppressed", reason="not_addressed")

        # rate limits and provider health
        key = f"dm:{view.author_id}" if view.is_dm else f"ch:{view.channel_id}"
        if not self.limiter.allow(key):
            return RouteResult("suppressed", reason="cooldown")
        if self.backoff.suppressed():
            return RouteResult("suppressed", reason="provider_backoff")

        session = self.session_id(view)

        # optional supervised multi-agent path — returns None for simple requests
        if self.delegate_fn is not None:
            try:
                delegated = self.delegate_fn(view, content)
            except Exception as exc:  # noqa: BLE001 - never break normal chat
                self.audit.record("discord_delegate_error", error=type(exc).__name__)
                delegated = None
            if isinstance(delegated, str) and delegated.strip():
                event = "discord_delegated_reply"
                self.audit.record(
                    event,
                    channel=view.channel_id,
                    guild=view.guild_id,
                    session=session,
                    actor=view.author_id,
                )
                return RouteResult(
                    "reply",
                    replies=chunk_message(delegated, self.config.max_message_len),
                    session_id=session,
                    audit_event=event,
                )

        author_line = f"[{view.author_name} in #{view.channel_id}] " if not view.is_dm else f"[{view.author_name}] "
        try:
            reply = self.ask_fn(author_line + content, session)
        except Exception as exc:  # noqa: BLE001 - a model failure must not kill the bot
            self.backoff.note_failure()
            self.audit.record("discord_provider_error", error=type(exc).__name__)
            return RouteResult(
                "reply",
                replies=["The model is temporarily unavailable; try again shortly."],
                session_id=session,
                error=True,
                audit_event="discord_provider_error",
            )

        text = getattr(reply, "text", str(reply))
        ok = getattr(reply, "ok", True)
        if not ok:
            span = self.backoff.note_failure()
            self.audit.record(
                "discord_provider_error",
                code=getattr(reply, "error", "unknown"),
                backoff_s=round(span, 1),
            )
            return RouteResult(
                "reply",
                replies=[f"Model provider error ({getattr(reply, 'error', 'unknown')}). "
                         f"Automatic retry window: {int(span)}s."],
                session_id=session,
                error=True,
                audit_event="discord_provider_error",
            )
        self.backoff.note_success()

        event = "discord_reply_sent"
        detail: dict = {"channel": view.channel_id, "guild": view.guild_id, "session": session}
        if getattr(reply, "rejected", None):
            detail["rejected_tools"] = [e.get("tool") for e in reply.rejected]
        if getattr(reply, "flagged", None):
            detail["injection_flagged"] = True
            text = "[flagged: possible prompt injection in source content]\n" + text
        self.audit.record(event, **detail)
        return RouteResult(
            "reply",
            replies=chunk_message(text, self.config.max_message_len),
            session_id=session,
            audit_event=event,
        )