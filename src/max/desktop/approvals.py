"""Human-in-the-loop approval book for desktop control.

An approval is a small JSON record on disk that binds *one* exact action to
*one* operator decision. The model can only ever create a request; granting
lives exclusively at the console entry point (``max desktop approve``), which
is a different process and a different principal.

Two kinds:

``term``  one-shot. The grant is bound to a SHA-256 fingerprint of the exact
          argv + cwd, consumed on first use, and deleted afterwards. An
          approval for ``make`` can never run ``rm``.
``gui``   session. One grant authorises a bounded number of input events
          (``max_actions``) inside a short TTL, because approving every
          individual mouse click would be theatre rather than safety.

Every record carries its own expiry; expired or unknown ids simply fail.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

MAX_PENDING = 64


class ApprovalError(Exception):
    """An approval could not be used (missing, denied, expired, mismatched)."""


def fingerprint(kind: str, argv: list[str] | tuple[str, ...], cwd: str) -> str:
    payload = json.dumps([kind, list(argv), str(cwd)], sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class ApprovalBook:
    state_dir: Path
    ttl_s: float = 600.0
    gui_ttl_s: float = 300.0
    gui_max_actions: int = 50
    clock: Callable[[], float] = time.time

    def __post_init__(self) -> None:
        self.dir = Path(self.state_dir) / "approvals"
        self.dir.mkdir(parents=True, exist_ok=True)

    # -------------------------------------------------------------- helpers
    def _path(self, approval_id: str) -> Path:
        safe = str(approval_id or "").strip()
        if not safe or not safe.isalnum() or len(safe) > 32:
            raise ApprovalError("malformed approval id")
        return self.dir / f"{safe}.json"

    def _read(self, approval_id: str) -> dict:
        path = self._path(approval_id)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ApprovalError(f"unknown approval id '{approval_id}'") from None
        except ValueError:
            raise ApprovalError("approval record is unreadable") from None

    def _write(self, record: dict) -> dict:
        path = self.dir / f"{record['id']}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)  # atomic: never a half-written record
        return record

    def _prune(self) -> None:
        """Bound disk use: drop expired/used records beyond a fixed ceiling."""
        files = sorted(self.dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
        for path in files[: max(0, len(files) - MAX_PENDING)]:
            try:
                path.unlink()
            except OSError:
                pass

    # ------------------------------------------------------------- requests
    def request(
        self,
        kind: str,
        argv: list[str] | tuple[str, ...],
        cwd: str,
        reason: str = "",
        max_actions: int | None = None,
    ) -> dict:
        """Create (or reuse) a pending approval bound to this exact action."""
        if kind not in {"term", "gui"}:
            raise ApprovalError(f"unknown approval kind '{kind}'")
        fp = fingerprint(kind, argv, cwd)
        # idempotent: repeated requests for the same action share one record
        for path in self.dir.glob("*.json"):
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if (
                existing.get("fingerprint") == fp
                and existing.get("status") == "pending"
                and existing.get("expires_ts", 0) > self.clock()
            ):
                return existing

        now = self.clock()
        ttl = self.ttl_s if kind == "term" else self.gui_ttl_s
        record = {
            "id": secrets.token_hex(6),
            "kind": kind,
            "status": "pending",
            "fingerprint": fp,
            "argv": list(argv),
            "cwd": str(cwd),
            "reason": str(reason)[:300],
            "max_actions": int(max_actions or self.gui_max_actions) if kind == "gui" else 1,
            "uses": 0,
            "created_ts": now,
            "expires_ts": now + ttl,
        }
        self._prune()
        return self._write(record)

    # ------------------------------------------------------------- listing
    def pending(self) -> list[dict]:
        now = self.clock()
        out = []
        for path in sorted(self.dir.glob("*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if record.get("status") == "pending" and record.get("expires_ts", 0) > now:
                out.append(record)
        return out

    def get(self, approval_id: str) -> dict:
        return self._read(approval_id)

    # -------------------------------------------------------------- gating
    def grant(self, approval_id: str, granted_by: str) -> dict:
        record = self._read(approval_id)
        if record.get("status") != "pending":
            raise ApprovalError(f"approval is {record.get('status')}, not pending")
        if record.get("expires_ts", 0) <= self.clock():
            record["status"] = "expired"
            self._write(record)
            raise ApprovalError("approval request has expired — ask MAX to request it again")
        record["status"] = "granted"
        record["granted_by"] = str(granted_by)[:80]
        record["granted_ts"] = self.clock()
        return self._write(record)

    def deny(self, approval_id: str, denied_by: str) -> dict:
        record = self._read(approval_id)
        if record.get("status") not in {"pending", "granted"}:
            raise ApprovalError(f"approval is {record.get('status')}")
        record["status"] = "denied"
        record["denied_by"] = str(denied_by)[:80]
        return self._write(record)

    def consume(self, approval_id: str, fp: str, kind: str) -> dict:
        """Validate and spend an approval. Raises :class:`ApprovalError`.

        ``term`` grants are deleted on use (one-shot). ``gui`` grants are
        counted against ``max_actions`` and expire on a short TTL.
        """
        record = self._read(approval_id)
        status = record.get("status")
        if status == "pending":
            raise ApprovalError("approval has been requested but not granted yet")
        if status != "granted":
            raise ApprovalError(f"approval is {status}")
        if record.get("expires_ts", 0) <= self.clock():
            record["status"] = "expired"
            self._write(record)
            raise ApprovalError("approval has expired")
        if record.get("kind") != kind:
            raise ApprovalError(f"approval is for a {record.get('kind')} action, not {kind}")
        if not secrets.compare_digest(str(record.get("fingerprint", "")), str(fp)):
            raise ApprovalError("approval does not match this exact action")

        if kind == "term":
            try:
                self._path(approval_id).unlink()  # one-shot: spent
            except OSError:
                record["status"] = "consumed"
                self._write(record)
            return record

        uses = int(record.get("uses", 0)) + 1
        if uses > int(record.get("max_actions", 1)):
            record["status"] = "consumed"
            self._write(record)
            raise ApprovalError("the approved input session has been used up")
        record["uses"] = uses
        self._write(record)
        return record
