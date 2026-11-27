"""MAX harness core (M35): a Python adaptation of DeepSeek Harness design ideas.

Not a port or a vendored copy. MAX keeps its own trust gates; this package only
adds an append-only session log, a plugin context with reversible effects, and
an evented turn/step agent loop. See docs/M35_HARNESS_CORE.md.
"""

from max_core.harness_core.agent_loop import AgentLoop, TurnResult
from max_core.harness_core.context import Context, Plugin
from max_core.harness_core.replay import RecordingGateway, ReplayGateway
from max_core.harness_core.session import SessionEvent, SessionLog, derive_messages

__all__ = [
    "AgentLoop",
    "Context",
    "Plugin",
    "RecordingGateway",
    "ReplayGateway",
    "SessionEvent",
    "SessionLog",
    "TurnResult",
    "derive_messages",
]
