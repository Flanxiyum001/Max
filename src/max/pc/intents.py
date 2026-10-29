"""Voice-first observation intents: explicit user requests, never LLM-generated tools.

Narrowly classify requests to look at a visible app. Text from screenshots,
external agents, or the phone companion must never enter this parser as a
trusted local microphone event.
"""
from __future__ import annotations

import re

_APP_NAMES = (
    (r"\b(?:adobe\s+)?premiere(?:\s+pro)?\b", "premiere"),
    (r"\b(?:visual\s+studio\s+code|vs\s*code|vscode)\b", "vscode"),
    (r"\bnotepad\b", "notepad"),
)

_OBSERVE = re.compile(
    r"^(?:(?:can|could|would)\s+you\s+)?"
    r"(?:(?:take\s+a\s+look|look|see|check|inspect|analy[sz]e|describe|"
    r"read|examine|watch|tell\s+me\s+what(?:'s|\s+is)?|"
    r"what(?:'s|\s+is)\s+(?:happening|wrong|going\s+on|on|in)|"
    r"why\s+is|help\s+me\s+(?:with|understand))\b)",
    re.I,
)


def _clean(message: str) -> str:
    text = re.sub(r"\s+", " ", message.strip())
    text = re.sub(r"^(?:hey|hi|hello)\s+max\b[,!:.]?\s*", "", text, flags=re.I)
    text = re.sub(r"^max\b[,!:.]?\s*", "", text, flags=re.I)
    text = re.sub(r"^please\s+", "", text, flags=re.I)
    return text.strip().rstrip("!?.")


def observation_target(message: str) -> str | None:
    """Return a named app or 'active' for a user-requested safe foreground app."""
    if not isinstance(message, str) or len(message) > 4000:
        return None
    text = _clean(message)
    if re.match(r"^(?:do\s+not|don't|stop|never|cancel)\b", text, re.I):
        return None
    if not _OBSERVE.match(text):
        return None
    # Strong intent needs a visible desktop target. Ordinary questions
    # like "what is Premiere Pro?" or "search Premiere tutorials" stay chat.
    for pattern, name in _APP_NAMES:
        if re.search(pattern, text, re.I):
            return name
    if re.search(
        r"\b(?:my|this|the)\s+(?:screen|desktop|window|display)\b"
        r"|\bwhat\s+you\s+(?:can\s+)?see\b",
        text, re.I,
    ):
        return "active"
    return None


def is_visual_confirmation(message: str) -> bool:
    return bool(re.fullmatch(
        r"(?:hey\s+)?max[,!:.]?\s+confirm\s+(?:screen\s+share|screenshot)[.!]?",
        str(message).strip(), re.I,
    ))


def is_voice_action_confirmation(message: str) -> bool:
    return bool(re.fullmatch(
        r"(?:hey\s+)?max[,!:.]?\s+confirm\s+action[.!]?",
        str(message).strip(), re.I,
    ))


def is_voice_cancel(message: str) -> bool:
    return bool(re.fullmatch(
        r"(?:hey\s+)?max[,!:.]?\s+(?:cancel|abort|stop)\s+(?:that|action|screen\s+share)[.!]?",
        str(message).strip(), re.I,
    ))
