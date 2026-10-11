"""Idle-time task scheduling (the Tasks feature).

A *task* is a one-shot command the system launches when the GPU has been idle.
The scheduler owns its own 5 s loop (mirroring :class:`~gpusybio.scheduler.idle.IdleUnloader`)
and, on each tick, fires **at most one** pending task when **all** of:

1. the current time is inside the task's ``[start_time, end_time]`` window
   (same-day; a ``start > end`` window wraps midnight);
2. the device GPU% has stayed ``< gpu_idle_threshold`` for at least the task's
   ``idle_time`` minutes (device-level idle, tracked via the last "busy" sample);
3. no task ran in the last ``cooldown_time`` minutes (lets an already-launched
   task start consuming the GPU before the next one is eligible); and
4. the task's ``vram_requirement`` MB is available — if not, the scheduler frees
   idle, safely-unloadable apps (reusing the idle unloader's predicate) and the
   task runs on the *next* tick (it never blocks waiting for the unload).

Firing a task launches its command detached (``setsid``), output to a log file,
and freezes it (``ran=True`` + ``run_time``) — a ran task cannot be edited.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from datetime import datetime, timezone

from gpusybio.database.db import SessionFactory
from gpusybio.database.models import Task
from gpusybio.events.bus import TASK_RAN
from gpusybio.events.log import get_logger
from gpusybio.resources.manager import ResourceManager
from gpusybio.scheduler.idle import IdleUnloader
from gpusybio.state import StateManager

log = get_logger("tasks")


def _hhmm_to_minutes(value: str) -> int | None:
    """Parse ``"HH:MM"`` to minutes-of-day, or ``None`` if malformed."""
    try:
        hh, mm = value.strip().split(":")
        h, m = int(hh), int(mm)
    except (ValueError, AttributeError):
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return h * 60 + m


def _in_window(now: float, start: str, end: str) -> bool:
    """True if the current time is inside the ``[start, end]`` window.

    Same-day windows (``start <= end``); a ``start > end`` window wraps midnight
    (``[start, 24:00) ∪ [00:00, end)``). Malformed bounds → False (never fire).

    The window is a *local* time-of-day (the user's daily schedule, e.g. their
    morning), so it is compared against the server's local time.
    """
    cur = datetime.fromtimestamp(now)  # local time-of-day
    cur_m = cur.hour * 60 + cur.minute
    s, e = _hhmm_to_minutes(start), _hhmm_to_minutes(end)
    if s is None or e is None:
        return False
    if s <= e:
        return s <= cur_m < e or (s == e and cur_m == s)  # zero-length window: that minute only
    # wrap: [s, 24:00) ∪ [00:00, e)
    return cur_m >= s or cur_m < e


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class TaskScheduler:
    """Fires idle-time tasks. See the module docstring for the firing rules."""

    def __init__(
        self,
        tasks_spec,
        db: SessionFactory,
        resources: ResourceManager,
        states: StateManager,
        apps,
        bus,
        idle_unloader: IdleUnloader,
        data_path: str,
        interval: float = 5.0,
    ):
        self._spec = tasks_spec
        self._db = db
        self._resources = resources
        self._states = states
        self._apps = apps
        self._bus = bus
        self._idle_unloader = idle_unloader
        self._data_path = data_path
        self._interval = max(0.2, interval)
        # Last time the device GPU% was >= threshold. Seeded to "now" so a fresh
        # scheduler requires ``idle_time`` minutes of *observed* idle before the
        # first task can fire (it cannot know the GPU was idle before startup).
        self._last_busy_ts: float = time.time()
        # Hold refs to launched (detached) children so they are not GC'd.
        self._procs: dict[str, asyncio.Process] = {}
        self._task: asyncio.Task | None = None

    # -- lifecycle ---------------------------------------------------------------
    def start(self) -> None:
        loop = asyncio.get_event_loop()
        self._task = loop.create_task(self._loop(), name="task-scheduler")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 - a tick must never kill the loop
                log.exception("task tick failed")

    # -- a single firing pass ----------------------------------------------------
    async def tick(self) -> None:
        if not getattr(self._spec, "enabled", False):
            return
        now = time.time()

        # 1. Refresh the device-idle tracker (device GPU% >= threshold = busy).
        sample = self._resources.last_sample or self._resources.sample()
        if sample.utilization_pct >= self._spec.gpu_idle_threshold:
            self._last_busy_ts = now

        # 2. Pick the first eligible pending task (by created_at).
        task = self._next_candidate(now)
        if task is None:
            return

        # 3. VRAM gate: free idle unloadable apps if the shortfall is coverable.
        required = task.vram_requirement or 0
        avail = self._resources.available_mb()
        shortfall = required - avail
        if shortfall > 0:
            candidates = self._idle_unloader.unloadable_app_ids()
            freed = {a: self._resources.observed_mb(a) for a in candidates}
            if sum(freed.values()) < shortfall:
                log.info(
                    "task not fired: not enough VRAM even after unloading",
                    task_id=task.id, required=required, available=avail,
                    unloadable_free=sum(freed.values()),
                )
                return
            # Cover the shortfall (largest first); run on the next tick.
            for app_id in sorted(candidates, key=lambda a: freed[a], reverse=True):
                if shortfall <= 0:
                    break
                await self._apps.release(app_id)
                shortfall -= freed.get(app_id, 0)
            log.info("task deferred: unloading apps to free VRAM", task_id=task.id, required=required)
            return

        # 4. Fire it.
        await self._run(task)

    # -- eligibility -------------------------------------------------------------
    def _next_candidate(self, now: float) -> Task | None:
        with self._db() as s:
            rows = (
                s.query(Task)
                .filter(Task.ran.is_(False))
                .order_by(Task.created_at.asc())
                .all()
            )
            for row in rows:
                if not _in_window(now, row.start_time, row.end_time):
                    continue
                if (now - self._last_busy_ts) < (row.idle_time or 0) * 60:
                    continue
                if not self._cooldown_ok(now):
                    continue
                return row
        return None

    def _cooldown_ok(self, now: float) -> bool:
        cooldown_min = getattr(self._spec, "cooldown_time", 0.0) or 0.0
        if cooldown_min <= 0:
            return True
        with self._db() as s:
            latest = s.query(Task.run_time).filter(Task.ran.is_(True)).all()
        if not latest:
            return True
        most_recent = max(r[0] for r in latest)
        if most_recent.tzinfo is None:
            most_recent = most_recent.replace(tzinfo=timezone.utc)
        return (now - most_recent.timestamp()) >= cooldown_min * 60

    # -- firing ------------------------------------------------------------------
    async def _run(self, task: Task) -> None:
        logs_dir = os.path.join(self._data_path, "logs")
        pids_dir = os.path.join(self._data_path, "pids")
        os.makedirs(logs_dir, exist_ok=True)
        os.makedirs(pids_dir, exist_ok=True)
        log_path = os.path.join(logs_dir, f"task-{task.id}.log")
        pid_path = os.path.join(pids_dir, f"task-{task.id}.pid")
        try:
            with open(log_path, "ab") as lf:
                proc = await asyncio.create_subprocess_shell(
                    task.command, stdout=lf, stderr=lf, start_new_session=True
                )
        except Exception as exc:  # noqa: BLE001 - leave pending so it can retry
            log.error("task launch failed", task_id=task.id, command=task.command, error=str(exc))
            return
        self._procs[task.id] = proc
        try:
            with open(pid_path, "w") as f:
                f.write(str(proc.pid))
        except OSError:  # noqa: BLE001 - pid file is best-effort
            log.warning("could not write task pid file", task_id=task.id)
        # Freeze it: mark ran + record the run time.
        with self._db() as s:
            row = s.get(Task, task.id)
            if row is None:  # deleted between selection and run
                return
            row.ran = True
            row.run_time = _utcnow()
            row.updated_at = _utcnow()
        await self._bus.publish_raw(TASK_RAN, task_id=task.id, name=task.name)
        log.info("task ran", task_id=task.id, name=task.name, command=task.command)

    # -- accessors (API / UI) ----------------------------------------------------
    def new_id(self) -> str:
        return uuid.uuid4().hex

    def pending_count(self) -> int:
        with self._db() as s:
            return s.query(Task).filter(Task.ran.is_(False)).count()
