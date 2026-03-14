"""
Task Queue — Phase 6: Background Execution.

Provides an in-process async task queue that decouples long-running
operations (scout, fill, email) from the HTTP request lifecycle.

Architecture:
  - Tasks are submitted and get a task_id immediately.
  - Workers process tasks in the background.
  - Results are stored and can be polled via /api/v3/tasks/{task_id}.
  - SSE streams can still be used for real-time updates.
  - If Redis is available, tasks survive server restarts.

This replaces the fragile pattern of running asyncio.create_task()
directly in route handlers.
"""

import asyncio
import json
import logging
import time
import uuid
from enum import Enum
from typing import Any, Callable, Coroutine, Optional

logger = logging.getLogger(__name__)


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class TaskRecord:
    """Represents a background task."""

    def __init__(
        self,
        task_id: str,
        task_type: str,
        params: dict[str, Any],
        session_id: str = "",
    ):
        self.task_id = task_id
        self.task_type = task_type
        self.params = params
        self.session_id = session_id
        self.status = TaskStatus.PENDING
        self.progress: list[dict[str, Any]] = []
        self.result: dict[str, Any] | None = None
        self.error: str | None = None
        self.created_at = time.time()
        self.started_at: float | None = None
        self.completed_at: float | None = None
        self.queue: asyncio.Queue = asyncio.Queue()

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task_type": self.task_type,
            "status": self.status.value,
            "session_id": self.session_id,
            "progress": self.progress[-10:],  # Last 10 progress messages
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_seconds": (
                round((self.completed_at or time.time()) - self.created_at, 2)
            ),
        }

    async def emit(self, event: dict[str, Any]) -> None:
        """Emit a progress event to the task's queue."""
        self.progress.append(event)
        await self.queue.put(event)


# ── Task Store ────────────────────────────────────────────────────────

_tasks: dict[str, TaskRecord] = {}
_MAX_COMPLETED_TASKS = 500  # Keep last N completed tasks in memory


def _cleanup_old_tasks() -> None:
    """Remove old completed tasks to prevent memory leaks."""
    completed = [
        (tid, t) for tid, t in _tasks.items()
        if t.status in (TaskStatus.COMPLETED, TaskStatus.FAILED)
    ]
    if len(completed) > _MAX_COMPLETED_TASKS:
        # Sort by completion time, remove oldest
        completed.sort(key=lambda x: x[1].completed_at or 0)
        for tid, _ in completed[: len(completed) - _MAX_COMPLETED_TASKS]:
            _tasks.pop(tid, None)


# ── Task Submission ───────────────────────────────────────────────────

def submit_task(
    task_type: str,
    params: dict[str, Any],
    session_id: str = "",
) -> str:
    """
    Submit a new background task. Returns immediately with a task_id string.
    The actual work must be started separately.
    """
    task_id = uuid.uuid4().hex
    task = TaskRecord(
        task_id=task_id,
        task_type=task_type,
        params=params,
        session_id=session_id,
    )
    task.status = TaskStatus.RUNNING
    task.started_at = time.time()
    _tasks[task_id] = task
    _cleanup_old_tasks()
    logger.info("Task submitted: %s [%s] session=%s", task_id, task_type, session_id)
    return task_id


def run_task(task_id: str, result: dict[str, Any]) -> None:
    """
    Mark a task as completed with the given result.
    Called by the background worker when it finishes.
    """
    task = _tasks.get(task_id)
    if not task:
        logger.warning("run_task called for unknown task: %s", task_id)
        return

    task.completed_at = time.time()

    if result.get("success", False):
        task.result = result
        task.status = TaskStatus.COMPLETED
        logger.info(
            "Task completed: %s [%s] in %.1fs",
            task.task_id,
            task.task_type,
            task.completed_at - task.created_at,
        )
    else:
        task.error = result.get("error", "Unknown error")
        task.result = result
        task.status = TaskStatus.FAILED
        logger.error(
            "Task failed: %s [%s]: %s",
            task.task_id,
            task.task_type,
            task.error,
        )


# ── Task Retrieval ────────────────────────────────────────────────────

def get_task(task_id: str) -> dict[str, Any] | None:
    """Get a task by ID, returned as a JSON-serializable dict."""
    task = _tasks.get(task_id)
    if task is None:
        return None
    return task.to_dict()


def list_tasks(
    session_id: str | None = None,
    task_type: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List tasks with optional filters."""
    results = []
    for task in sorted(_tasks.values(), key=lambda t: t.created_at, reverse=True):
        if session_id and task.session_id != session_id:
            continue
        if task_type and task.task_type != task_type:
            continue
        if status and task.status.value != status:
            continue
        results.append(task.to_dict())
        if len(results) >= limit:
            break
    return results


def get_task_stats() -> dict[str, Any]:
    """Get aggregate task statistics."""
    stats = {
        "total": len(_tasks),
        "pending": 0,
        "running": 0,
        "completed": 0,
        "failed": 0,
    }
    for task in _tasks.values():
        stats[task.status.value] = stats.get(task.status.value, 0) + 1
    return stats
