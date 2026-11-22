"""Pick a model per task, for routers such as FreeLLMAPI that expose ``auto`` profiles.

The classifier is a cheap, deterministic keyword heuristic run on the owner's
own text; it never calls a model. Routing only changes *which* model id is
requested from the one configured endpoint, so it cannot widen where data goes.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Literal

TaskKind = Literal["quick", "chat", "code", "reasoning"]
KINDS: tuple[TaskKind, ...] = ("quick", "chat", "code", "reasoning")

_CODE = re.compile(
    r"```|\b(code|coding|python|javascript|typescript|java|rust|golang|c\+\+|html|css|sql|"
    r"regex|function|class|script|bug|debug|stack ?trace|traceback|compile|refactor|api|"
    r"json|git|docker|npm|pip|exception|syntax)\b",
    re.IGNORECASE,
)
_REASONING = re.compile(
    r"\b(why|explain|analy[sz]e|analysis|compare|comparison|plan|strategy|step by step|"
    r"pros and cons|trade-?offs?|reason|prove|proof|calculate|solve|math|essay|summari[sz]e|"
    r"research|evaluate|decide|should i|in detail|detailed)\b",
    re.IGNORECASE,
)
_QUICK = re.compile(
    r"^(hi|hey|hello|yo|thanks|thank you|ok|okay|good (morning|night|evening)|"
    r"what('s| is) the (time|date|weather)|how are you)\b",
    re.IGNORECASE,
)


def classify(text: str) -> TaskKind:
    t = text.strip()
    if _CODE.search(t):
        return "code"
    if len(t) > 600 or _REASONING.search(t):
        return "reasoning"
    if len(t) <= 80 and (_QUICK.search(t) or len(t.split()) <= 4):
        return "quick"
    return "chat"


def is_router_model(model: str) -> bool:
    return model == "auto" or model.startswith("auto:")


# FreeLLMAPI strategy names (documented): auto:fast, auto:smart, auto:balanced, ...
_AUTO_DEFAULTS: dict[TaskKind, str] = {
    "quick": "auto:fast",
    "chat": "",  # "" means: the configured base model
    "code": "auto:smart",
    "reasoning": "auto:smart",
}


def route_table(base_model: str, env: Mapping[str, str]) -> dict[TaskKind, str]:
    """Model id per task kind. ``MAX_ROUTE_<KIND>`` overrides; ``MAX_ROUTING=off`` disables."""
    if env.get("MAX_ROUTING", "on").strip().lower() in {"off", "no", "0", "false"}:
        return dict.fromkeys(KINDS, base_model)
    table: dict[TaskKind, str] = {}
    for kind in KINDS:
        override = env.get(f"MAX_ROUTE_{kind.upper()}", "").strip()
        default = _AUTO_DEFAULTS[kind] if is_router_model(base_model) else ""
        table[kind] = override or default or base_model
    return table
