"""Persistent task state (JSON, atomic replace, no secrets).

Task records are operational state, not conversation history: descriptions and
results are bounded in ``models`` before they ever reach disk, and nothing
secret-shaped is written because the supervisor never copies environment
values or provider credentials into a task.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .models import Task


class TaskStore:
    def __init__(self, directory: Path, max_tasks: int = 200) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.max_tasks = int(max_tasks)

    def _path(self, task_id: str) -> Path:
        safe = "".join(c for c in task_id if c.isalnum() or c in "-_")
        if not safe or safe != task_id:
            raise ValueError(f"invalid task id {task_id!r}")
        return self.directory / f"{safe}.json"

    def save(self, task: Task) -> None:
        path = self._path(task.id)
        payload = json.dumps(task.to_dict(), indent=2, sort_keys=True, default=str)
        fd, tmp = tempfile.mkstemp(dir=str(self.directory), prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        self._prune()

    def load(self, task_id: str) -> Task | None:
        path = self._path(task_id)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return None
        if not isinstance(data, dict):
            return None
        return Task.from_dict(data)

    def delete(self, task_id: str) -> bool:
        path = self._path(task_id)
        if path.is_file():
            path.unlink()
            return True
        return False

    def list(self) -> list[Task]:
        tasks: list[Task] = []
        for path in sorted(self.directory.glob("*.json")):
            task = self.load(path.stem)
            if task is not None:
                tasks.append(task)
        tasks.sort(key=lambda t: t.created_at, reverse=True)
        return tasks

    def list_for_owner(self, owner_id: int) -> list[Task]:
        return [t for t in self.list() if t.owner_id == owner_id]

    def _prune(self) -> None:
        tasks = self.list()
        if len(tasks) <= self.max_tasks:
            return
        for task in tasks[self.max_tasks:]:
            if task.is_terminal:
                self.delete(task.id)
