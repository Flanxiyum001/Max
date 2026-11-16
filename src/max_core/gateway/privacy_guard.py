"""Conservative, stateless pre-egress checks for high-confidence credential text.

The detector is not a PII classifier and cannot guarantee absence of secrets.
It never logs or persists input, and intentionally blocks rather than silently
rewriting material that might change the meaning of an owner's request.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from max_core.gateway.contracts import ChatMessage

# Keep patterns bounded and focused on unambiguous credential syntax. Detection
# failures remain possible, so callers must retain consent and privacy gates.
_CREDENTIAL_MARKERS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\b(?:AIza[0-9A-Za-z_-]{35}|GOCSPX-[0-9A-Za-z_-]{20,})\b"),
    re.compile(r"\b(?:Bearer|Basic)[ \t]+[A-Za-z0-9._~+/=-]{16,}\b", re.IGNORECASE),
    re.compile(
        r"(?:^|[ \t])(?:export[ \t]+)?(?:[A-Z][A-Z0-9_]*_)?"
        r"(?:API_KEY|ACCESS_TOKEN|AUTH_TOKEN|REFRESH_TOKEN|CLIENT_SECRET|"
        r"PRIVATE_KEY|SECRET_KEY|PASSWORD|DB_PASSWORD|DATABASE_URL)"
        r"[ \t]*[:=][ \t]*['\"]?[^\s'\";#]{8,}",
        re.IGNORECASE | re.MULTILINE,
    ),
)


def contains_high_confidence_secret(messages: Sequence[ChatMessage]) -> bool:
    """Return only a boolean; no matches, spans, raw text or loggable secrets."""
    return any(
        pattern.search(message.content) is not None
        for message in messages
        for pattern in _CREDENTIAL_MARKERS
    )
