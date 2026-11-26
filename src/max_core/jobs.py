"""Background jobs: scheduled tool runs that work on their own.

A job is ``(name, cron schedule, tool, exact arguments)``. Creating a job is an
EFFECTFUL tool call, so the owner approves it once through the normal approval
gate; that approval is the standing grant for *exactly that tool with exactly
those arguments*. After that the runner executes it on schedule with no further
prompts.

Hard limits enforced here in code, not by prompt text:
* tools that control the PC, run shell commands, place calls, create other jobs
  or change lessons can never be scheduled;
* at most 20 jobs and 48 runs per job per day;
* 3 consecutive failures disable the job (it does not loop on a broken tool);
* every run is recorded (status + short output) and fed to the evolution ledger.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from max_core.tools import Tool, ToolError, ToolParam, ToolRegistry, ToolRisk
from max_core.triggers import TriggerError, cron_matches, minute_key, parse_cron

MAX_JOBS = 20
MAX_RUNS_PER_DAY = 48
MAX_FAILURES = 3
OUTPUT_CHARS = 2_000
NEVER_SCHEDULE = ("shell.", "pc.", "apps.", "jobs.", "evolution.", "comms.call")


@dataclass(frozen=True, slots=True)
class Job:
    id: int
    name: str
    schedule: str
    tool: str
    arguments: dict[str, Any]
    enabled: bool
    failures: int
    last_status: str
    last_output: str
    last_run: str


def schedulable(tool: Tool) -> bool:
    return not any(tool.name == p or tool.name.startswith(p) for p in NEVER_SCHEDULE)


class JobStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs(
                  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, schedule TEXT NOT NULL,
                  tool TEXT NOT NULL, arguments TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                  failures INTEGER NOT NULL DEFAULT 0, last_status TEXT NOT NULL DEFAULT '',
                  last_output TEXT NOT NULL DEFAULT '', last_run TEXT NOT NULL DEFAULT '',
                  last_minute TEXT NOT NULL DEFAULT '', day TEXT NOT NULL DEFAULT '',
                  runs_today INTEGER NOT NULL DEFAULT 0);
                """
            )

    def _db(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=5)

    @staticmethod
    def _job(row: tuple[Any, ...]) -> Job:
        return Job(row[0], row[1], row[2], row[3], json.loads(row[4]), bool(row[5]), *row[6:10])

    def add(self, name: str, schedule: str, tool: str, arguments: dict[str, Any]) -> Job:
        normalized = " ".join(schedule.split())
        try:
            parse_cron(normalized)
        except TriggerError:
            raise ToolError("invalid_schedule") from None
        with self._lock, self._db() as db:
            if db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] >= MAX_JOBS:
                raise ToolError("too_many_jobs")
            try:
                db.execute(
                    "INSERT INTO jobs(name, schedule, tool, arguments) VALUES (?,?,?,?)",
                    (name, normalized, tool, json.dumps(arguments, sort_keys=True)),
                )
            except sqlite3.IntegrityError:
                raise ToolError("duplicate_job_name") from None
        return self.get(name)

    def get(self, name: str) -> Job:
        with self._db() as db:
            row = db.execute(
                "SELECT id,name,schedule,tool,arguments,enabled,failures,last_status,"
                "last_output,last_run FROM jobs WHERE name=?",
                (name,),
            ).fetchone()
        if row is None:
            raise ToolError("unknown_job")
        return self._job(row)

    def all(self) -> list[Job]:
        with self._db() as db:
            rows = db.execute(
                "SELECT id,name,schedule,tool,arguments,enabled,failures,last_status,"
                "last_output,last_run FROM jobs ORDER BY id"
            ).fetchall()
        return [self._job(row) for row in rows]

    def remove(self, name: str) -> None:
        with self._lock, self._db() as db:
            if db.execute("DELETE FROM jobs WHERE name=?", (name,)).rowcount == 0:
                raise ToolError("unknown_job")

    def set_enabled(self, name: str, enabled: bool) -> None:
        with self._lock, self._db() as db:
            if (
                db.execute(
                    "UPDATE jobs SET enabled=?, failures=0 WHERE name=?", (int(enabled), name)
                ).rowcount
                == 0
            ):
                raise ToolError("unknown_job")

    def claim_due(self, moment: datetime) -> list[Job]:
        """Mark every due job as claimed for this minute (exactly-once per minute)."""
        minute, day = minute_key(moment), moment.strftime("%Y-%m-%d")
        due: list[Job] = []
        with self._lock, self._db() as db:
            for job in self.all():
                if not job.enabled or not cron_matches(job.schedule, moment):
                    continue
                row = db.execute(
                    "SELECT last_minute, day, runs_today FROM jobs WHERE id=?", (job.id,)
                ).fetchone()
                runs = row[2] if row[1] == day else 0
                if row[0] == minute or runs >= MAX_RUNS_PER_DAY:
                    continue
                db.execute(
                    "UPDATE jobs SET last_minute=?, day=?, runs_today=? WHERE id=?",
                    (minute, day, runs + 1, job.id),
                )
                due.append(job)
        return due

    def finish(self, job: Job, ok: bool, output: str) -> None:
        with self._lock, self._db() as db:
            failures = 0 if ok else job.failures + 1
            db.execute(
                "UPDATE jobs SET failures=?, enabled=?, last_status=?, last_output=?, last_run=? "
                "WHERE id=?",
                (
                    failures,
                    int(failures < MAX_FAILURES),
                    "ok" if ok else ("disabled" if failures >= MAX_FAILURES else "failed"),
                    output[:OUTPUT_CHARS],
                    datetime.now(UTC).isoformat(timespec="seconds"),
                    job.id,
                ),
            )


class JobRunner:
    def __init__(
        self,
        store: JobStore,
        registry: Callable[[], ToolRegistry],
        *,
        on_outcome: Callable[[str, str, str | None], None] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.store = store
        self._registry = registry
        self._on_outcome = on_outcome
        self._clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def tick(self, moment: datetime | None = None) -> int:
        ran = 0
        for job in self.store.claim_due(moment or self._clock()):
            self.run(job)
            ran += 1
        return ran

    def run(self, job: Job) -> bool:
        tool = self._registry().get(job.tool)
        code: str | None = None
        if tool is None or not schedulable(tool):
            ok, output, code = False, "tool_unavailable", "tool_unavailable"
        else:
            try:
                output = str(tool.handler(tool.validate(job.arguments)))
                ok = True
            except ToolError as error:
                ok, output, code = False, error.code, error.code
            except Exception:
                ok, output, code = False, "tool_failed", "tool_failed"
        self.store.finish(job, ok, output)
        if self._on_outcome is not None:
            try:
                self._on_outcome(job.tool, "executed" if ok else "failed", code)
            except Exception:
                pass
        return ok

    def start(self, interval: float = 20.0) -> None:
        if self._thread is not None:
            return

        def loop() -> None:
            while not self._stop.wait(interval):
                try:
                    self.tick()
                except Exception:
                    pass  # one bad tick never kills the runner

        self._thread = threading.Thread(target=loop, name="max-jobs", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


def job_tools(store: JobStore, registry: Callable[[], ToolRegistry]) -> list[Tool]:
    def create(args: Mapping[str, Any]) -> str:
        tool = registry().get(args["tool"])
        if tool is None:
            raise ToolError("unknown_tool")
        if not schedulable(tool):
            raise ToolError("tool_not_schedulable")
        try:
            arguments = json.loads(args.get("arguments") or "{}")
        except ValueError:
            raise ToolError("arguments_not_json") from None
        tool.validate(arguments)
        job = store.add(args["name"], args["schedule"], tool.name, arguments)
        return f"Job '{job.name}' scheduled ({job.schedule}) to run {job.tool} on its own."

    def listing(_: Mapping[str, Any]) -> str:
        jobs = store.all()
        if not jobs:
            return "No background jobs."
        return "\n".join(
            f"{j.name}: {j.tool} at '{j.schedule}' - {'on' if j.enabled else 'off'}"
            f"{', last ' + j.last_status + ' ' + j.last_run if j.last_run else ''}"
            f"{chr(10) + '  ' + j.last_output[:300] if j.last_output else ''}"
            for j in jobs
        )

    def remove(args: Mapping[str, Any]) -> str:
        store.remove(args["name"])
        return f"Job '{args['name']}' removed."

    def toggle(args: Mapping[str, Any]) -> str:
        store.set_enabled(args["name"], bool(args["enabled"]))
        return f"Job '{args['name']}' {'enabled' if args['enabled'] else 'paused'}."

    name = ToolParam("name", str, "short job name", max_length=60)
    return [
        Tool(
            "jobs.create",
            "Schedule a background job that runs a tool by itself on a cron schedule "
            "(minute hour day month weekday, UTC), e.g. '0 7 * * *' daily at 07:00. "
            "arguments is the tool's arguments as a JSON object string. Approving this "
            "approves every future run of exactly this call.",
            ToolRisk.EFFECTFUL,
            (
                name,
                ToolParam("schedule", str, "5-field cron", max_length=64),
                ToolParam("tool", str, "tool name", max_length=100),
                ToolParam("arguments", str, "JSON object", required=False, max_length=2_000),
            ),
            create,
        ),
        Tool(
            "jobs.list",
            "List background jobs and their last results.",
            ToolRisk.READ_ONLY,
            (),
            listing,
        ),
        Tool("jobs.remove", "Delete a background job.", ToolRisk.EFFECTFUL, (name,), remove),
        Tool(
            "jobs.enable",
            "Pause (enabled=false) or resume a background job.",
            ToolRisk.EFFECTFUL,
            (name, ToolParam("enabled", bool, "true to resume")),
            toggle,
        ),
    ]
