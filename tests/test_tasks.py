"""The Tasks feature (idle-time one-shot command scheduling).

Unit tests exercise the TaskScheduler's firing rules (device idle, cooldown,
VRAM + unload, ran gating, enabled) against stubs + a real in-memory DB, and the
pure window helper. The E2E test drives a full server to verify a task fires
when the GPU is idle and the cooldown blocks a second task.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from conftest import ADMIN_PORT

from gpusybio.config.models import TasksSpec
from gpusybio.database.db import create_engine, init_db, make_session_factory
from gpusybio.database.models import Task
from gpusybio.events.bus import TASK_RAN
from gpusybio.scheduler.tasks import TaskScheduler, _in_window


# -- pure window logic ---------------------------------------------------------
def _at(h: int, m: int) -> float:
    return time.mktime((2024, 1, 1, h, m, 0, 0, 0, -1))


def test_window_same_day():
    assert _in_window(_at(3, 0), "02:00", "06:00")
    assert _in_window(_at(2, 0), "02:00", "06:00")  # start inclusive
    assert not _in_window(_at(6, 0), "02:00", "06:00")  # end exclusive
    assert not _in_window(_at(7, 0), "02:00", "06:00")


def test_window_wraps_midnight():
    assert _in_window(_at(23, 30), "22:00", "02:00")
    assert _in_window(_at(0, 30), "22:00", "02:00")
    assert not _in_window(_at(3, 0), "22:00", "02:00")
    assert not _in_window(_at(2, 0), "22:00", "02:00")  # end exclusive


def test_window_malformed_never_fires():
    assert not _in_window(_at(3, 0), "garbage", "06:00")
    assert not _in_window(_at(3, 0), "02:00", "25:00")


# -- stubs ---------------------------------------------------------------------
class _Sample:
    def __init__(self, util=0.0):
        self.utilization_pct = util


class _StubResources:
    def __init__(self, util=0.0, available=10000.0, observed=None):
        self._util = util
        self._available = available
        self._observed = observed or {}

    def sample(self):
        return _Sample(self._util)

    @property
    def last_sample(self):
        return _Sample(self._util)

    def available_mb(self):
        return self._available

    def observed_mb(self, app_id):
        return self._observed.get(app_id, 0.0)


class _StubApps:
    def __init__(self):
        self.released = []

    async def release(self, app_id, force=False):
        self.released.append(app_id)


class _StubBus:
    def __init__(self):
        self.events = []

    async def publish_raw(self, type_, **data):
        self.events.append((type_, data))


class _StubIdleUnloader:
    def __init__(self, unloadable=None):
        self._u = unloadable or []

    def unloadable_app_ids(self):
        return list(self._u)


def _make(db, spec, res, apps, bus, idle):
    return TaskScheduler(
        spec, db, res, object(), apps, bus, idle, "/tmp/gpusybio-task-data", interval=5.0
    )


def _new_db():
    path = f"/tmp/gpusybio-task-unit-{os.getpid()}-{uuid.uuid4().hex[:8]}.db"
    engine = create_engine(path)
    init_db(engine)
    return make_session_factory(engine)


def _add(db, **kw) -> Task:
    kw.setdefault("id", uuid.uuid4().hex)
    kw.setdefault("name", "t")
    kw.setdefault("start_time", "00:00")
    kw.setdefault("end_time", "23:59")
    kw.setdefault("idle_time", 0)
    kw.setdefault("vram_requirement", 0)
    kw.setdefault("command", "true")
    kw.setdefault("ran", False)
    with db() as s:
        row = Task(**kw)
        s.add(row)
        s.flush()
        return row


def _marker() -> str:
    d = f"/tmp/gpusybio-task-markers-{os.getpid()}"
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, f"m-{uuid.uuid4().hex[:8]}")
    return p


async def _wait_file(path: str, timeout: float = 5.0) -> bool:
    """The task command runs detached (setsid) and asynchronously, so poll for
    its marker file instead of asserting immediately."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if os.path.exists(path):
            return True
        await asyncio.sleep(0.05)
    return os.path.exists(path)


def _spec(**kw) -> TasksSpec:
    kw.setdefault("enabled", True)
    kw.setdefault("gpu_idle_threshold", 40.0)
    kw.setdefault("cooldown_time", 0.0)
    return TasksSpec(**kw)


# -- firing rules --------------------------------------------------------------
async def test_fires_when_idle_in_window_vram_ok():
    db = _new_db()
    marker = _marker()
    res = _StubResources(util=0.0, available=10000.0)
    apps, bus, idle = _StubApps(), _StubBus(), _StubIdleUnloader()
    sched = _make(db, _spec(), res, apps, bus, idle)
    t = _add(db, name="job", command=f"touch {marker}")
    await sched.tick()
    assert await _wait_file(marker)
    with db() as s:
        assert s.get(Task, t.id).ran is True
        assert s.get(Task, t.id).run_time is not None
    assert any(e[0] == TASK_RAN for e in bus.events)


async def test_disabled_does_not_fire():
    db = _new_db()
    marker = _marker()
    res = _StubResources(util=0.0)
    apps, bus, idle = _StubApps(), _StubBus(), _StubIdleUnloader()
    sched = _make(db, _spec(enabled=False), res, apps, bus, idle)
    t = _add(db, command=f"touch {marker}")
    await sched.tick()
    assert not os.path.exists(marker)
    with db() as s:
        assert s.get(Task, t.id).ran is False


async def test_gpu_busy_blocks_until_idle_time():
    db = _new_db()
    marker = _marker()
    # Device GPU% >= threshold on this tick → "busy" → idle clock resets to now.
    res = _StubResources(util=50.0)  # >= threshold 40
    apps, bus, idle = _StubApps(), _StubBus(), _StubIdleUnloader()
    sched = _make(db, _spec(), res, apps, bus, idle)
    t = _add(db, command=f"touch {marker}", idle_time=1)  # needs 1 min idle
    await sched.tick()
    assert not os.path.exists(marker)
    assert apps.released == []


async def test_ran_task_is_frozen_never_refires():
    db = _new_db()
    marker = _marker()
    res = _StubResources(util=0.0)
    apps, bus, idle = _StubApps(), _StubBus(), _StubIdleUnloader()
    sched = _make(db, _spec(), res, apps, bus, idle)
    t = _add(db, command=f"touch {marker}", ran=True)
    await sched.tick()
    assert not os.path.exists(marker)
    with db() as s:
        assert s.get(Task, t.id).ran is True


async def test_cooldown_blocks_second_task():
    db = _new_db()
    marker = _marker()
    res = _StubResources(util=0.0)
    apps, bus, idle = _StubApps(), _StubBus(), _StubIdleUnloader()
    sched = _make(db, _spec(cooldown_time=2.0), res, apps, bus, idle)
    # A task that just ran (within the 2-min cooldown).
    _add(db, command="true", ran=True, run_time=datetime.now(timezone.utc).replace(tzinfo=None))
    t = _add(db, command=f"touch {marker}")
    await sched.tick()
    assert not os.path.exists(marker)
    with db() as s:
        assert s.get(Task, t.id).ran is False


async def test_cooldown_elapsed_allows_run():
    db = _new_db()
    marker = _marker()
    res = _StubResources(util=0.0)
    apps, bus, idle = _StubApps(), _StubBus(), _StubIdleUnloader()
    sched = _make(db, _spec(cooldown_time=2.0), res, apps, bus, idle)
    # A task that ran 3 minutes ago (cooldown = 2 min has elapsed).
    _add(
        db,
        command="true",
        ran=True,
        run_time=(datetime.now(timezone.utc) - timedelta(minutes=3)).replace(tzinfo=None),
    )
    t = _add(db, command=f"touch {marker}")
    await sched.tick()
    assert await _wait_file(marker)
    with db() as s:
        assert s.get(Task, t.id).ran is True


async def test_vram_unmet_frees_idle_unloadable_app_and_defers():
    db = _new_db()
    res = _StubResources(util=0.0, available=10000.0, observed={"a2": 10000.0})
    apps, bus, idle = _StubApps(), _StubBus(), _StubIdleUnloader(unloadable=["a2"])
    sched = _make(db, _spec(), res, apps, bus, idle)
    t = _add(db, command="true", vram_requirement=20000)  # shortfall = 10000
    await sched.tick()
    # Not fired this tick, but the idle unloadable app is released to free VRAM.
    assert apps.released == ["a2"]
    with db() as s:
        assert s.get(Task, t.id).ran is False


async def test_vram_unmet_and_nothing_to_free_does_not_fire():
    db = _new_db()
    marker = _marker()
    res = _StubResources(util=0.0, available=10000.0)
    apps, bus, idle = _StubApps(), _StubBus(), _StubIdleUnloader(unloadable=[])
    sched = _make(db, _spec(), res, apps, bus, idle)
    t = _add(db, command=f"touch {marker}", vram_requirement=20000)
    await sched.tick()
    assert not os.path.exists(marker)
    assert apps.released == []
    with db() as s:
        assert s.get(Task, t.id).ran is False


# -- E2E: full server ----------------------------------------------------------
@pytest.fixture
def cfg_overrides():
    return {"tasks": TasksSpec(enabled=True, gpu_idle_threshold=40.0, cooldown_time=120.0)}


async def test_e2e_task_fires_when_idle_and_cooldown_blocks_next(server):
    base = f"http://127.0.0.1:{ADMIN_PORT}"
    marker_a = _marker()
    marker_b = _marker()
    async with httpx.AsyncClient() as c:
        ra = await c.post(
            f"{base}/api/tasks",
            json={
                "name": "job-a",
                "start_time": "00:00",
                "end_time": "23:59",
                "idle_time": 0,
                "vram_requirement": 0,
                "command": f"touch {marker_a}",
            },
        )
        assert ra.status_code == 201
        a = ra.json()
        assert a["ran"] is False and a["run_time"] is None
        rb = await c.post(
            f"{base}/api/tasks",
            json={
                "name": "job-b",
                "start_time": "00:00",
                "end_time": "23:59",
                "idle_time": 0,
                "vram_requirement": 0,
                "command": f"touch {marker_b}",
            },
        )
        assert rb.status_code == 201
        b = rb.json()

        # Stop the background loop so we drive ticks deterministically (the fake
        # GPU is idle — no active apps — so the device-util gate passes).
        server.task_scheduler._task.cancel()

        # First tick: the earliest pending task (a) fires.
        await server.task_scheduler.tick()
        assert await _wait_file(marker_a), "task a should have launched its command"
        with server.db() as s:
            assert s.get(Task, a["id"]).ran is True
            assert s.get(Task, a["id"]).run_time is not None
            assert s.get(Task, b["id"]).ran is False

        # Second tick: b is still pending but the cooldown (just ran a) blocks it.
        await server.task_scheduler.tick()
        assert not os.path.exists(marker_b), "cooldown must block the second task"
        with server.db() as s:
            assert s.get(Task, b["id"]).ran is False


async def test_e2e_ran_task_rejects_edit(server):
    base = f"http://127.0.0.1:{ADMIN_PORT}"
    async with httpx.AsyncClient() as c:
        a = (await c.post(
            f"{base}/api/tasks",
            json={"name": "x", "start_time": "00:00", "end_time": "23:59",
                  "idle_time": 0, "vram_requirement": 0, "command": "true"},
        )).json()
        # Simulate a ran task directly in the DB.
        with server.db() as s:
            row = s.get(Task, a["id"])
            row.ran = True
            row.run_time = datetime.now(timezone.utc).replace(tzinfo=None)
        r = await c.put(f"{base}/api/tasks/{a['id']}", json={"name": "renamed"})
        assert r.status_code == 409
