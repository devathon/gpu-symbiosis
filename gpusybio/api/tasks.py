"""Task routes (the Tasks feature): one-shot commands scheduled during GPU idle
time.

``register`` binds the shared names from the :class:`ApiContext`. CRUD is
gated by the auth middleware (mutating routes); the list + config reads are
open (loopback). A ran task is frozen — its ``PUT`` returns 409 (copy it to make
a new one instead).
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException, Request
from pydantic import BaseModel

from gpusybio.database.models import Task
from gpusybio.scheduler.tasks import _hhmm_to_minutes


class TaskIn(BaseModel):
    name: str
    start_time: str  # "HH:MM"
    end_time: str  # "HH:MM"
    idle_time: int = 0  # minutes the GPU must be idle
    vram_requirement: int = 0  # MB
    command: str


def _validate_time(value: str, field: str) -> None:
    if _hhmm_to_minutes(value) is None:
        raise HTTPException(400, f"{field} must be 'HH:MM'")


def _task_view(row: Task) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "start_time": row.start_time,
        "end_time": row.end_time,
        "idle_time": row.idle_time,
        "vram_requirement": row.vram_requirement,
        "command": row.command,
        "ran": row.ran,
        "run_time": row.run_time.isoformat() if row.run_time else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _validate_fields(body) -> None:
    if not body.name or not body.name.strip():
        raise HTTPException(400, "name is required")
    _validate_time(body.start_time, "start_time")
    _validate_time(body.end_time, "end_time")
    if body.idle_time < 0:
        raise HTTPException(400, "idle_time must be >= 0")
    if body.vram_requirement < 0:
        raise HTTPException(400, "vram_requirement must be >= 0")
    if not body.command or not body.command.strip():
        raise HTTPException(400, "command is required")


def register(api, ctx) -> None:
    db = ctx.db
    config = ctx.config
    _audit = ctx.audit
    _actor = ctx.actor

    @api.get("/api/tasks")
    def list_tasks():
        with db() as s:
            rows = s.query(Task).order_by(Task.created_at.asc()).all()
            return [_task_view(r) for r in rows]

    @api.get("/api/tasks/config")
    def task_config():
        settings = getattr(config, "settings", None) if config is not None else None
        spec = getattr(settings, "tasks", None) if settings is not None else None
        return {
            "enabled": bool(getattr(spec, "enabled", False)),
            "gpu_idle_threshold": float(getattr(spec, "gpu_idle_threshold", 40.0)),
            "cooldown_time": float(getattr(spec, "cooldown_time", 2.0)),
        }

    @api.post("/api/tasks", status_code=201)
    async def create_task(body: TaskIn, request: Request):
        _validate_fields(body)
        row = Task(id=uuid.uuid4().hex, **body.model_dump())
        with db() as s:
            s.add(row)
        await _audit("task_created", actor=_actor(request), detail=body.name)
        return _task_view(row)

    @api.put("/api/tasks/{task_id}")
    async def update_task(task_id: str, body: dict, request: Request):
        editable = ("name", "start_time", "end_time", "idle_time",
                    "vram_requirement", "command")
        with db() as s:
            row = s.get(Task, task_id)
            if row is None:
                raise HTTPException(404, "not found")
            if row.ran:
                # A ran task is frozen (one-shot); copy it to make a new one.
                raise HTTPException(409, "task already ran — copy it to make a new one")
            # Merge the body over the current values, validate, then apply.
            merged = {k: getattr(row, k) for k in editable}
            for k, v in body.items():
                if k in editable:
                    merged[k] = v
            _validate_fields(TaskIn(
                name=merged["name"],
                start_time=merged["start_time"],
                end_time=merged["end_time"],
                idle_time=int(merged["idle_time"]),
                vram_requirement=int(merged["vram_requirement"]),
                command=merged["command"],
            ))
            for k, v in merged.items():
                setattr(row, k, v)
        await _audit("task_updated", actor=_actor(request), detail=task_id)
        with db() as s:
            return _task_view(s.get(Task, task_id))

    @api.delete("/api/tasks/{task_id}")
    async def delete_task(request: Request, task_id: str):
        with db() as s:
            row = s.get(Task, task_id)
            if row is None:
                raise HTTPException(404, "not found")
            s.delete(row)
        await _audit("task_deleted", actor=_actor(request), detail=task_id)
        return {"id": task_id, "deleted": True}
