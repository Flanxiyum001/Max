"""User-scoped and project-scoped persistent memory with privacy guardrails.

Rules enforced here (not by the model):
* one user's memory is stored separately and can only be read/written with
  that user's id — no cross-user retrieval path exists,
* values that look like credentials/secrets are refused,
* hard caps on entry count and total size (no unbounded growth),
* `forget`/`clear` delete the actual stored data (deletion scope: this
  user's JSON file only — documented in docs/DISCORD.md).
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

MAX_ENTRIES = 40
MAX_VALUE_CHARS = 400
MAX_TOTAL_CHARS = 4000
_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,47}$")
_SECRET_RE = re.compile(
    r"(\bsk-[a-z0-9_\-]{16,}|\bgh[pousr]_[a-z0-9]{16,}|\bAKIA[0-9A-Z]{16}\b"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|\b(password|passwd|secret|token|api[_-]?key)\b\s*[=:]\s*\S{6,})",
    re.IGNORECASE,
)


class MemoryError_(ValueError):
    """Invalid memory operation (message is user-safe)."""


def _looks_secret(value: str) -> str | None:
    m = _SECRET_RE.search(value or "")
    return m.group(0)[:40] if m else None


class UserMemoryStore:
    def __init__(self, data_dir: Path, scope: str = "user", clock=time.time):
        if scope not in {"user", "project"}:
            raise ValueError("scope must be 'user' or 'project'")
        self.root = Path(data_dir) / f"{scope}-memory"
        self.root.mkdir(parents=True, exist_ok=True)
        self._clock = clock

    # ------------------------------------------------------------- paths
    def _path(self, subject_id: int | str) -> Path:
        safe = re.sub(r"[^0-9a-zA-Z_\-]", "", str(subject_id))
        if not safe:
            raise MemoryError_("invalid subject id")
        return self.root / f"{safe}.json"

    def _read(self, subject_id: int | str) -> dict:
        path = self._path(subject_id)
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, subject_id: int | str, data: dict) -> None:
        path = self._path(subject_id)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
        tmp.replace(path)

    # ------------------------------------------------------------ actions
    @staticmethod
    def _visible(data: dict) -> dict:
        return {k: v for k, v in data.items() if not k.startswith("_")}

    def entries(self, subject_id: int | str) -> dict:
        return self._visible(self._read(subject_id))

    def show(self, subject_id: int | str) -> str:
        data = self._visible(self._read(subject_id))
        if not data:
            return "(no saved memories)"
        lines = [f"- {k}: {v}" for k, v in sorted(data.items())]
        return "\n".join(lines)

    def set(self, subject_id: int | str, key: str, value: str) -> str:
        key = (key or "").strip().lower()
        value = " ".join((value or "").split())
        if not _KEY_RE.match(key):
            raise MemoryError_("key must be 1-48 chars: lowercase letters, digits, _ or -")
        if not value:
            raise MemoryError_("value must not be empty")
        if len(value) > MAX_VALUE_CHARS:
            raise MemoryError_(f"value too long (max {MAX_VALUE_CHARS} chars)")
        secret = _looks_secret(value)
        if secret:
            raise MemoryError_(
                "refusing to store what looks like a credential/secret; "
                "keep secrets in the environment or a secret store"
            )
        data = self._read(subject_id)
        data[key] = value
        visible = self._visible(data)
        if len(visible) > MAX_ENTRIES:
            raise MemoryError_(f"memory limit reached ({MAX_ENTRIES} entries); forget something first")
        total = sum(len(k) + len(v) for k, v in visible.items())
        if total > MAX_TOTAL_CHARS:
            raise MemoryError_(f"memory size limit reached ({MAX_TOTAL_CHARS} chars)")
        data["_updated"] = str(int(self._clock()))
        self._write(subject_id, data)
        return f"saved '{key}' ({len(visible)} entries)"

    def forget(self, subject_id: int | str, key: str) -> str:
        data = self._read(subject_id)
        key = (key or "").strip().lower()
        if key.startswith("_") or key not in data:
            raise MemoryError_(f"no entry named '{key}'")
        data.pop(key)
        self._write(subject_id, data)
        return f"forgot '{key}'"

    def clear(self, subject_id: int | str) -> str:
        path = self._path(subject_id)
        if path.is_file():
            path.unlink()
            return "all memories for this subject deleted"
        return "(nothing to delete)"

    def stats(self, subject_id: int | str) -> dict:
        data = self._visible(self._read(subject_id))
        return {
            "entries": len(data),
            "chars": sum(len(k) + len(v) for k, v in data.items()),
            "path_scope": str(self.root.name),
        }