"""Discord integration (Phase 1: text only — no voice or music code here)."""

from .config import DiscordConfig  # noqa: F401
from .ratelimit import FailureBackoff, RateLimiter, chunk_message  # noqa: F401
from .router import DiscordRouter, MessageView, RouteResult  # noqa: F401
from .store import ChannelStore  # noqa: F401

__all__ = [
    "ChannelStore",
    "DiscordConfig",
    "DiscordRouter",
    "FailureBackoff",
    "MessageView",
    "RateLimiter",
    "RouteResult",
    "chunk_message",
]