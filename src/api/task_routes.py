from fastapi import APIRouter, HTTPException
from typing import List
from loguru import logger

from src.api.tasks import task_manager, TaskStatus


task_router = APIRouter()


@task_router.get("/tasks")
def list_tasks(limit: int = 20):
    tasks = task_manager.list_tasks(limit=limit)
    return {"tasks": [t.to_dict() for t in tasks]}


@task_router.get("/tasks/active")
def get_active_task():
    if task_manager.active_task_id:
        task = task_manager.get_task(task_manager.active_task_id)
        if task:
            return task.to_dict()
    return None


@task_router.get("/tasks/{task_id}")
def get_task(task_id: str):
    task = task_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task.to_dict()


@task_router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str):
    ok = await task_manager.cancel_task(task_id)
    if not ok:
        raise HTTPException(status_code=400, detail="Task cannot be cancelled")
    return {"message": "Task cancelled"}
