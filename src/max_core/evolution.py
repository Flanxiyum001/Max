"""MAX Evolution: learn from tool outcomes through owner-approved lessons.

Observe -> propose -> owner approves -> apply -> monitor -> retire.

* Every finished tool step (executed / failed / denied) is recorded in a small
  local SQLite ledger: tool name, status and a fixed error code only - never
  arguments, prompts or outputs.
* ``analyze`` turns repeated patterns into *candidate* lessons with evidence
  counts (high failure rate, a repeated error code, repeated owner denials).
* A candidate only becomes active after ``approve`` (exposed as an EFFECTFUL
  tool, so the existing owner-approval gate applies). Active lessons are plain
  guidance text the assistant reads; they can never change code, permissions,
  Trust, vault or payment behaviour.
* ``monitor`` retires an active lesson automatically when the tool has been
  healthy since it was approved, so guidance does not rot.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from max_core.tools import Tool, ToolError, ToolParam, ToolRisk

_STATUSES = ("executed", "failed", "denied")
MIN_CALLS = 5
FAILURE_RATE = 0.5
REPEAT_CODE = 3
REPEAT_DENIALS = 3
MAX_LESSONS = 50
HEALTHY_RUN = 10


@dataclass(frozen=True, slots=True)
class Lesson:
    id: int
    tool: str
    kind: str
    text: str
    evidence: int
    state: str  # candidate | active | rejected | retired


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class OutcomeLedger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS outcomes(
                  id INTEGER PRIMARY KEY, at TEXT NOT NULL, tool TEXT NOT NULL,
                  status TEXT NOT NULL, code TEXT NOT NULL DEFAULT '');
                CREATE INDEX IF NOT EXISTS outcomes_tool ON outcomes(tool, id);
                CREATE TABLE IF NOT EXISTS lessons(
                  id INTEGER PRIMARY KEY, tool TEXT NOT NULL, kind TEXT NOT NULL,
                  text TEXT NOT NULL, evidence INTEGER NOT NULL, state TEXT NOT NULL,
                  since_outcome INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
                  UNIQUE(tool, kind));
                """
            )

    def _db(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=5)

    # observe ---------------------------------------------------------------
    def record(self, tool: str, status: str, code: str | None = None) -> None:
        if status not in _STATUSES or not tool or tool.startswith("evolution."):
            return
        with self._lock, self._db() as db:
            db.execute(
                "INSERT INTO outcomes(at, tool, status, code) VALUES (?,?,?,?)",
                (_now(), tool[:100], status, (code or "")[:64]),
            )
            db.execute("DELETE FROM outcomes WHERE id <= (SELECT MAX(id) FROM outcomes) - 20000")

    def stats(self) -> dict[str, dict[str, int]]:
        with self._db() as db:
            rows = db.execute(
                "SELECT tool, status, COUNT(*) FROM outcomes GROUP BY tool, status"
            ).fetchall()
        out: dict[str, dict[str, int]] = {}
        for tool, status, count in rows:
            out.setdefault(tool, {s: 0 for s in _STATUSES})[status] = count
        return out

    # propose ---------------------------------------------------------------
    def analyze(self) -> list[Lesson]:
        """Create or refresh candidate lessons; returns all candidates."""
        found: list[tuple[str, str, str, int]] = []
        for tool, counts in self.stats().items():
            total = sum(counts.values())
            ran = counts["executed"] + counts["failed"]
            if ran >= MIN_CALLS and counts["failed"] / ran >= FAILURE_RATE:
                found.append(
                    (
                        tool,
                        "failure_rate",
                        f"{tool} failed {counts['failed']} of {ran} times. Check inputs first, "
                        "prefer an alternative tool, and tell the owner if it keeps failing.",
                        counts["failed"],
                    )
                )
            if counts["denied"] >= REPEAT_DENIALS and counts["denied"] * 2 >= total:
                found.append(
                    (
                        tool,
                        "denials",
                        f"The owner usually declines {tool} ({counts['denied']} denials). "
                        "Ask in plain words before proposing it.",
                        counts["denied"],
                    )
                )
        with self._db() as db:
            for tool, code, count in db.execute(
                "SELECT tool, code, COUNT(*) FROM outcomes WHERE status='failed' AND code!='' "
                "GROUP BY tool, code HAVING COUNT(*) >= ?",
                (REPEAT_CODE,),
            ).fetchall():
                found.append(
                    (
                        tool,
                        f"code:{code}",
                        f"{tool} keeps failing with '{code}' ({count}x). Avoid the input that "
                        "causes it or use another route.",
                        count,
                    )
                )
            for tool, kind, text, evidence in found:
                db.execute(
                    "INSERT INTO lessons(tool, kind, text, evidence, state, updated_at) "
                    "VALUES (?,?,?,?, 'candidate', ?) ON CONFLICT(tool, kind) DO UPDATE SET "
                    "evidence=excluded.evidence, text=CASE WHEN state='candidate' "
                    "THEN excluded.text ELSE text END, "
                    "state=CASE WHEN state='retired' THEN 'candidate' ELSE state END, "
                    "updated_at=excluded.updated_at",
                    (tool, kind, text[:280], evidence, _now()),
                )
            db.execute(
                "DELETE FROM lessons WHERE id NOT IN "
                "(SELECT id FROM lessons ORDER BY updated_at DESC LIMIT ?)",
                (MAX_LESSONS,),
            )
        return self.lessons("candidate")

    def lessons(self, state: str | None = None) -> list[Lesson]:
        query = "SELECT id, tool, kind, text, evidence, state FROM lessons"
        params: tuple[Any, ...] = ()
        if state:
            query, params = query + " WHERE state=?", (state,)
        with self._db() as db:
            return [Lesson(*row) for row in db.execute(query + " ORDER BY id", params)]

    # apply -----------------------------------------------------------------
    def decide(self, lesson_id: int, approve: bool) -> Lesson:
        with self._db() as db:
            row = db.execute("SELECT state FROM lessons WHERE id=?", (lesson_id,)).fetchone()
            if row is None or row[0] != "candidate":
                raise ToolError("lesson_not_candidate")
            last = db.execute("SELECT COALESCE(MAX(id),0) FROM outcomes").fetchone()[0]
            db.execute(
                "UPDATE lessons SET state=?, since_outcome=?, updated_at=? WHERE id=?",
                ("active" if approve else "rejected", last, _now(), lesson_id),
            )
        return next(item for item in self.lessons() if item.id == lesson_id)

    def guidance(self) -> str:
        active = self.lessons("active")
        if not active:
            return ""
        return "Lessons the owner approved from past outcomes:\n" + "\n".join(
            f"- {lesson.text}" for lesson in active
        )

    # monitor ---------------------------------------------------------------
    def monitor(self) -> list[Lesson]:
        """Retire active lessons whose tool has run cleanly HEALTHY_RUN times since."""
        retired: list[int] = []
        with self._db() as db:
            for lesson_id, tool, since in db.execute(
                "SELECT id, tool, since_outcome FROM lessons WHERE state='active'"
            ).fetchall():
                rows = db.execute(
                    "SELECT status FROM outcomes WHERE tool=? AND id>? ORDER BY id",
                    (tool, since),
                ).fetchall()
                if len(rows) >= HEALTHY_RUN and all(r[0] == "executed" for r in rows):
                    db.execute(
                        "UPDATE lessons SET state='retired', updated_at=? WHERE id=?",
                        (_now(), lesson_id),
                    )
                    retired.append(lesson_id)
        return [item for item in self.lessons() if item.id in retired]


def step_recorder(
    ledger: OutcomeLedger, inner: Callable[[Any], None] | None
) -> Callable[[Any], None]:
    """Wrap a ToolLoop/harness ``on_step`` callback so outcomes are recorded."""

    def on_step(step: Any) -> None:
        try:
            ledger.record(str(step.tool_name), str(step.status), step.error_code)
        except Exception:
            pass  # learning never breaks a turn
        if inner is not None:
            inner(step)

    return on_step


def evolution_tools(ledger: OutcomeLedger) -> list[Tool]:
    def review(_: Mapping[str, Any]) -> str:
        ledger.monitor()
        candidates = ledger.analyze()
        active = ledger.lessons("active")
        lines = [f"Active lessons: {len(active)}"] + [f"  - {a.text}" for a in active]
        lines.append(f"Candidate lessons awaiting approval: {len(candidates)}")
        lines += [f"  #{c.id} ({c.evidence}x) {c.text}" for c in candidates]
        return "\n".join(lines)

    def decide(args: Mapping[str, Any]) -> str:
        lesson = ledger.decide(int(args["lesson_id"]), bool(args.get("approve", True)))
        return f"Lesson #{lesson.id} is now {lesson.state}."

    return [
        Tool(
            "evolution.review",
            "Show what MAX has learned from past tool outcomes: active lessons and "
            "candidate lessons waiting for the owner.",
            ToolRisk.READ_ONLY,
            (),
            review,
        ),
        Tool(
            "evolution.decide",
            "Approve (or reject with approve=false) a candidate lesson by its number.",
            ToolRisk.EFFECTFUL,
            (
                ToolParam("lesson_id", int, "lesson number from evolution.review"),
                ToolParam("approve", bool, "true to approve, false to reject", required=False),
            ),
            decide,
        ),
    ]
