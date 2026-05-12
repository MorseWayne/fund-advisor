from __future__ import annotations
import asyncio
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Dict, List, Optional, Callable, Any, Awaitable
from loguru import logger


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class TaskProgress:
    step: int = 0
    total_steps: int = 0
    label: str = ""
    detail: str = ""
    percent: float = 0.0
    timestamp: float = field(default_factory=time.time)


@dataclass
class TaskInfo:
    id: str
    name: str
    status: TaskStatus
    created_at: float
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    error: Optional[str] = None
    progress: TaskProgress = field(default_factory=TaskProgress)
    result: Optional[Dict[str, Any]] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        return d


class TaskManager:
    _instance: Optional[TaskManager] = None
    _lock: asyncio.Lock = asyncio.Lock()

    def __new__(cls) -> TaskManager:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init()
        return cls._instance

    def _init(self) -> None:
        self.tasks: Dict[str, TaskInfo] = {}
        self.active_task_id: Optional[str] = None
        self._cleanup_threshold = 3600  # 1 hour
        self._max_history = 50

    async def create_task(self, name: str) -> str:
        async with self._lock:
            task_id = str(uuid.uuid4())[:8]
            task = TaskInfo(
                id=task_id,
                name=name,
                status=TaskStatus.PENDING,
                created_at=time.time(),
            )
            self.tasks[task_id] = task
            self._cleanup_old()
            return task_id

    async def start_task(self, task_id: str) -> bool:
        async with self._lock:
            if self.active_task_id is not None and self.active_task_id != task_id:
                return False
            task = self.tasks.get(task_id)
            if not task or task.status != TaskStatus.PENDING:
                return False
            task.status = TaskStatus.RUNNING
            task.started_at = time.time()
            self.active_task_id = task_id
            return True

    async def update_progress(self, task_id: str, step: int, total: int, label: str = "", detail: str = "") -> None:
        async with self._lock:
            task = self.tasks.get(task_id)
            if not task:
                return
            task.progress = TaskProgress(
                step=step,
                total_steps=total,
                label=label,
                detail=detail,
                percent=round(step / total * 100, 1) if total > 0 else 0.0,
            )

    async def finish_task(self, task_id: str, result: Optional[Dict[str, Any]] = None, error: Optional[str] = None) -> None:
        async with self._lock:
            task = self.tasks.get(task_id)
            if not task:
                return
            task.finished_at = time.time()
            if error:
                task.status = TaskStatus.FAILED
                task.error = error
            else:
                task.status = TaskStatus.SUCCESS
                task.result = result
            if self.active_task_id == task_id:
                self.active_task_id = None

    async def cancel_task(self, task_id: str) -> bool:
        async with self._lock:
            task = self.tasks.get(task_id)
            if not task or task.status not in (TaskStatus.PENDING, TaskStatus.RUNNING):
                return False
            task.status = TaskStatus.CANCELLED
            task.finished_at = time.time()
            if self.active_task_id == task_id:
                self.active_task_id = None
            return True

    async def has_active_task(self) -> bool:
        async with self._lock:
            return self.active_task_id is not None

    def get_task(self, task_id: str) -> Optional[TaskInfo]:
        return self.tasks.get(task_id)

    def list_tasks(self, limit: int = 20) -> List[TaskInfo]:
        sorted_tasks = sorted(self.tasks.values(), key=lambda t: t.created_at, reverse=True)
        return sorted_tasks[:limit]

    def _cleanup_old(self) -> None:
        now = time.time()
        # Remove completed tasks older than threshold, keeping at least the last few
        completed = [
            tid for tid, t in self.tasks.items()
            if t.status in (TaskStatus.SUCCESS, TaskStatus.FAILED, TaskStatus.CANCELLED)
            and t.finished_at and (now - t.finished_at) > self._cleanup_threshold
        ]
        if len(self.tasks) - len(completed) > self._max_history:
            # Sort by finished time and keep the most recent ones
            sorted_completed = sorted(
                [(tid, self.tasks[tid]) for tid in completed],
                key=lambda x: x[1].finished_at or 0,
            )
            to_remove = len(self.tasks) - self._max_history
            for tid, _ in sorted_completed[:to_remove]:
                del self.tasks[tid]


task_manager = TaskManager()


async def run_task_with_progress(
    name: str,
    coro_factory: Callable[[str], Awaitable[None]],
) -> str:
    task_id = await task_manager.create_task(name)
    started = await task_manager.start_task(task_id)
    if not started:
        await task_manager.cancel_task(task_id)
        raise RuntimeError("Another task is already running")

    async def _wrapper():
        try:
            await coro_factory(task_id)
            await task_manager.finish_task(task_id, result={"ok": True})
        except asyncio.CancelledError:
            await task_manager.cancel_task(task_id)
            raise
        except Exception as e:
            logger.exception(f"Task {task_id} failed")
            await task_manager.finish_task(task_id, error=str(e))

    asyncio.create_task(_wrapper())
    return task_id
