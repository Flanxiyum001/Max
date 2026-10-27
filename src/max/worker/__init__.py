"""Isolated coding worker: task lifecycle, bounded execution, diffs.

The worker never gives the model a shell. Every command goes through
:class:`~max.worker.policy.CommandPolicy`; every task lives in its own
git worktree under the MAX workspace; every action is audited.
"""

from __future__ import annotations

from .policy import CommandPolicy, CommandVerdict
from .tasks import Task, TaskManager

__all__ = ["CommandPolicy", "CommandVerdict", "Task", "TaskManager"]
