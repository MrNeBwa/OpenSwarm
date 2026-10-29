"""Delayed & Scheduled Task Engine for OpenSwarm.

Provides:
- Scheduling prompts for execution at a specific future timestamp or relative delay.
- Background ticker that checks due tasks and triggers session runs.
- JSON state persistence under ~/.local/state/openswarm/scheduled.json.
- Full CRUD API (list, schedule, cancel, history).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Coroutine

from .state import state_path

logger = logging.getLogger("openswarm.scheduler")


@dataclass
class ScheduledTask:
    id: str
    prompt: str
    run_at: float  # Unix timestamp in seconds
    created_at: float = field(default_factory=time.time)
    mode: str = "chat"
    priority: str = "balanced"
    effort: str = "medium"
    session_id: str = ""
    target_agents: int = 1
    status: str = "pending"  # pending | running | completed | failed | cancelled
    result_snippet: str = ""
    error: str = ""

    @property
    def is_due(self) -> bool:
        return self.status == "pending" and time.time() >= self.run_at

    @property
    def run_at_iso(self) -> str:
        return datetime.fromtimestamp(self.run_at, tz=timezone.utc).isoformat()

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["run_at_iso"] = self.run_at_iso
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ScheduledTask":
        return cls(
            id=data.get("id") or uuid.uuid4().hex[:10],
            prompt=str(data.get("prompt", "")),
            run_at=float(data.get("run_at", time.time())),
            created_at=float(data.get("created_at", time.time())),
            mode=str(data.get("mode", "chat")),
            priority=str(data.get("priority", "balanced")),
            effort=str(data.get("effort", "medium")),
            session_id=str(data.get("session_id", "")),
            target_agents=int(data.get("target_agents", 1)),
            status=str(data.get("status", "pending")),
            result_snippet=str(data.get("result_snippet", "")),
            error=str(data.get("error", "")),
        )


class TaskScheduler:
    """Manages scheduled and delayed prompts with async background evaluation."""

    def __init__(
        self,
        dispatch_fn: Callable[[ScheduledTask], Coroutine[Any, Any, None]] | None = None,
        state_file: Path | None = None,
    ) -> None:
        self.dispatch_fn = dispatch_fn
        self.state_file = state_file or (state_path().parent / "scheduled.json")
        self.tasks: dict[str, ScheduledTask] = {}
        self._running = False
        self._loop_task: asyncio.Task | None = None
        self._load()

    def _load(self) -> None:
        if not self.state_file.exists():
            return
        try:
            raw = json.loads(self.state_file.read_text(encoding="utf-8"))
            for item in raw:
                task = ScheduledTask.from_dict(item)
                self.tasks[task.id] = task
        except Exception as exc:
            logger.warning("Could not load scheduled tasks: %s", exc)

    def _save(self) -> None:
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            data = [t.to_dict() for t in self.tasks.values()]
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.replace(tmp, self.state_file)
        except Exception as exc:
            logger.warning("Could not save scheduled tasks: %s", exc)

    def schedule(
        self,
        prompt: str,
        delay_seconds: float = 0,
        run_at: float | None = None,
        mode: str = "chat",
        priority: str = "balanced",
        effort: str = "medium",
        session_id: str = "",
        target_agents: int = 1,
    ) -> ScheduledTask:
        """Schedule a prompt to run in the future."""
        target_time = run_at if run_at is not None else (time.time() + max(0, delay_seconds))
        task = ScheduledTask(
            id=uuid.uuid4().hex[:10],
            prompt=prompt,
            run_at=target_time,
            mode=mode,
            priority=priority,
            effort=effort,
            session_id=session_id,
            target_agents=target_agents,
        )
        self.tasks[task.id] = task
        self._save()
        return task

    def cancel(self, task_id: str) -> bool:
        task = self.tasks.get(task_id)
        if not task:
            return False
        if task.status == "pending":
            task.status = "cancelled"
            self._save()
            return True
        return False

    def list_tasks(self, include_finished: bool = True) -> list[dict[str, Any]]:
        tasks = sorted(self.tasks.values(), key=lambda t: t.run_at)
        if not include_finished:
            tasks = [t for t in tasks if t.status == "pending"]
        return [t.to_dict() for t in tasks]

    def start(self) -> None:
        """Start the background tick loop. Safe to call outside an event loop;
        if no loop is running yet the task is deferred until ensure_started() is
        called from inside an async context (e.g. the first request handler)."""
        if self._running:
            return
        self._running = True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None and loop.is_running():
            self._loop_task = loop.create_task(self._tick_loop())

    async def ensure_started(self) -> None:
        """Call from inside an async context to actually schedule the tick loop
        if start() was called before the event loop was running."""
        if self._running and self._loop_task is None:
            self._loop_task = asyncio.create_task(self._tick_loop())

    def stop(self) -> None:
        self._running = False
        if self._loop_task and not self._loop_task.done():
            self._loop_task.cancel()

    async def _tick_loop(self) -> None:
        while self._running:
            try:
                now = time.time()
                due = [t for t in self.tasks.values() if t.is_due]
                for task in due:
                    task.status = "running"
                    self._save()
                    if self.dispatch_fn:
                        asyncio.create_task(self._execute_task(task))
                    else:
                        task.status = "completed"
                        task.result_snippet = "No dispatcher bound"
                        self._save()
                await asyncio.sleep(2.0)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Error in scheduler tick: %s", exc)
                await asyncio.sleep(5.0)

    async def _execute_task(self, task: ScheduledTask) -> None:
        try:
            if self.dispatch_fn:
                await self.dispatch_fn(task)
            task.status = "completed"
        except Exception as exc:
            task.status = "failed"
            task.error = str(exc)
        finally:
            self._save()
