"""Phase 4 — automatic idle detection & unloading (spec 06 §10).

Unit tests exercise the IdleUnloader's detection logic (API vs non-API
activity, hard exclusions, minimum residency, pending demand) against stubs;
the E2E tests drive a full server to verify an idle resident is auto-unloaded
and re-served on the next request, plus the idle-status API.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest
from conftest import A1_GW, ADMIN_PORT, _app_state, _req_done, _stream

from gpusybio.applications.lifecycle import ProcessState, ResidencyState
from gpusybio.events.bus import EventBus, IDLE_TIMEOUT
from gpusybio.scheduler.idle import IdleUnloader
from gpusybio.scoring import DemandTracker
from gpusybio.state import StateManager


# -- stubs -------------------------------------------------------------------
class StubApp:
    def __init__(self, pinned=False, allow_automatic_unload=True, idle_timeout=None):
        self.pinned = pinned
        self.allow_automatic_unload = allow_automatic_unload
        self.idle_unload_timeout_sec = idle_timeout
        self.idle_threshold_gpu_pct = 5.0
        self.unloaded = []

    def unload(self, app_id):
        self.unloaded.append(app_id)


class StubApps:
    def __init__(self, cfgs):
        self._cfgs = cfgs

    def config(self, app_id):
        return self._cfgs[app_id]

    async def unload(self, app_id):
        self._cfgs[app_id].unload(app_id)


class StubResources:
    def __init__(self, util=0.0):
        self.gpu_activity_threshold_pct = 5.0
        self.util = util
        outer = self

        class _B:
            def process_utilization_pct(self, pid):
                return outer.util

        self.backend = _B()


class StubRM:
    def __init__(self, gw_app=None, queued=0):
        self._gw_app = gw_app or {}
        self._queued = queued
        self._demand = DemandTracker()

    def gateway(self, gid):
        return {"application_id": self._gw_app.get(gid), "priority": 10}

    def queued_count(self, app_id):
        return self._queued

    def demand(self):
        return self._demand


class StubBus:
    def __init__(self):
        self.events = []

    async def publish_raw(self, type_, **data):
        self.events.append((type_, data))

    async def publish(self, event):
        self.events.append((event.type, event.data))


def make_unloader(apps, states, rm, resources, bus, app_ids, gateway_ids,
                  min_residency=30.0, idle_timeout=300.0, interval=5.0):
    return IdleUnloader(
        resources, states, apps, rm, bus, app_ids, gateway_ids,
        min_residency_sec=min_residency, default_idle_timeout_sec=idle_timeout,
        interval=interval,
    )


def load_app(states, app_id, pid=123, loaded_ago=600.0, active=0):
    states.set_process(app_id, ProcessState.RUNNING)
    states.set_residency(app_id, ResidencyState.LOADED)
    s = states.get(app_id)
    s.pid = pid
    s.loaded_at = time.time() - loaded_ago
    s.active_requests = active


# -- unit: activity detection --------------------------------------------------
def test_api_app_active_by_requests():
    states = StateManager()
    load_app(states, "a1", active=0)
    apps = StubApps({"a1": StubApp()})
    rm = StubRM(gw_app={"gw1": "a1"})
    u = make_unloader(apps, states, rm, StubResources(0.0), StubBus(), ["a1"], ["gw1"])
    assert u.is_active("a1") is False
    states.get("a1").active_requests = 1
    assert u.is_active("a1") is True


def test_non_api_app_active_by_gpu():
    states = StateManager()
    load_app(states, "c1")  # no gateway -> non-API
    apps = StubApps({"c1": StubApp()})
    rm = StubRM()  # no gateways
    u = make_unloader(apps, states, rm, StubResources(util=0.0), StubBus(), ["c1"], [])
    assert u.is_active("c1") is False  # GPU 0% < 5% threshold
    u._resources.util = 80.0
    assert u.is_active("c1") is True  # GPU 80% >= threshold


# -- unit: hard exclusions (spec 06 §3.1) -------------------------------------
async def test_pinned_never_auto_unloaded():
    states = StateManager()
    load_app(states, "a1")
    apps = StubApps({"a1": StubApp(pinned=True)})
    rm = StubRM(gw_app={"gw1": "a1"})
    bus = StubBus()
    u = make_unloader(apps, states, rm, StubResources(0.0), bus, ["a1"], ["gw1"],
                      min_residency=0.0, idle_timeout=1.0)
    await asyncio.sleep(1.1)
    unloaded = await u.tick()
    assert unloaded == []
    assert apps.config("a1").unloaded == []


async def test_allow_automatic_unload_false_never_unloaded():
    states = StateManager()
    load_app(states, "a1")
    apps = StubApps({"a1": StubApp(allow_automatic_unload=False)})
    rm = StubRM(gw_app={"gw1": "a1"})
    u = make_unloader(apps, states, rm, StubResources(0.0), StubBus(), ["a1"], ["gw1"],
                      min_residency=0.0, idle_timeout=1.0)
    await asyncio.sleep(1.1)
    unloaded = await u.tick()
    assert unloaded == []


# -- unit: minimum residency ---------------------------------------------------
async def test_minimum_residency_blocks_unload():
    states = StateManager()
    load_app(states, "a1", loaded_ago=5.0)  # only 5s old
    apps = StubApps({"a1": StubApp()})
    rm = StubRM(gw_app={"gw1": "a1"})
    u = make_unloader(apps, states, rm, StubResources(0.0), StubBus(), ["a1"], ["gw1"],
                      min_residency=30.0, idle_timeout=1.0)
    await asyncio.sleep(1.1)
    unloaded = await u.tick()
    assert unloaded == []  # residency not yet elapsed


# -- unit: pending demand suppression ------------------------------------------
async def test_pending_demand_blocks_unload():
    states = StateManager()
    load_app(states, "a1")
    apps = StubApps({"a1": StubApp()})
    rm = StubRM(gw_app={"gw1": "a1"}, queued=2)  # queued requests
    u = make_unloader(apps, states, rm, StubResources(0.0), StubBus(), ["a1"], ["gw1"],
                      min_residency=0.0, idle_timeout=1.0)
    await asyncio.sleep(1.1)
    unloaded = await u.tick()
    assert unloaded == []


async def test_recent_arrival_blocks_unload():
    states = StateManager()
    load_app(states, "a1")
    apps = StubApps({"a1": StubApp()})
    rm = StubRM(gw_app={"gw1": "a1"})
    for _ in range(10):
        rm.demand().arrival("gw1")  # recent traffic
    u = make_unloader(apps, states, rm, StubResources(0.0), StubBus(), ["a1"], ["gw1"],
                      min_residency=0.0, idle_timeout=1.0)
    await asyncio.sleep(1.1)
    unloaded = await u.tick()
    assert unloaded == []  # recent demand suppresses the unload


# -- unit: idle app is auto-unloaded ------------------------------------------
async def test_idle_app_is_auto_unloaded():
    states = StateManager()
    load_app(states, "a1")
    apps = StubApps({"a1": StubApp()})
    rm = StubRM(gw_app={"gw1": "a1"})
    bus = StubBus()
    u = make_unloader(apps, states, rm, StubResources(0.0), bus, ["a1"], ["gw1"],
                      min_residency=0.0, idle_timeout=1.0)
    await asyncio.sleep(1.1)  # idle longer than the timeout
    unloaded = await u.tick()
    assert unloaded == ["a1"]
    assert apps.config("a1").unloaded == ["a1"]
    assert any(e[0] == IDLE_TIMEOUT for e in bus.events)


async def test_non_api_idle_app_is_auto_unloaded():
    states = StateManager()
    load_app(states, "c1")  # ComfyUI-like, no gateway
    apps = StubApps({"c1": StubApp()})
    rm = StubRM()
    bus = StubBus()
    u = make_unloader(apps, states, rm, StubResources(util=0.0), bus, ["c1"], [],
                      min_residency=0.0, idle_timeout=1.0)
    await asyncio.sleep(1.1)
    unloaded = await u.tick()
    assert unloaded == ["c1"]


# -- E2E: idle-status API ------------------------------------------------------
async def test_idle_status_api(server):
    c = httpx.AsyncClient()
    await asyncio.sleep(1.0)
    r = await c.get(f"http://127.0.0.1:{ADMIN_PORT}/api/idle/status")
    assert r.status_code == 200
    body = r.json()
    assert "applications" in body
    # a1 is resident (start-loaded) and idle (no requests)
    a1 = body["applications"].get("a1")
    assert a1 is not None
    assert "active" in a1 and "auto_unloadable" in a1
    await c.aclose()


# -- E2E: auto-unload an idle resident, then re-serve it ----------------------
@pytest.fixture
def cfg_overrides():
    from gpusybio.config.models import SchedulerSpec

    return {
        "scheduler": SchedulerSpec(
            min_residency_sec=2.0,
            idle_unload_timeout_sec=5.0,
            idle_unload_interval_sec=1.0,
        )
    }


async def test_auto_unload_idle_then_reserve(server):
    from conftest import _wait

    c = httpx.AsyncClient()
    admin = f"http://127.0.0.1:{ADMIN_PORT}"
    # 1. Start a1 (resident). The server boots all apps STOPPED/OFF; tests start them.
    await c.post(f"{admin}/api/applications/a1/start")

    async def a1_loaded():
        d = await _app_state(c, "a1")
        return d["process_state"] == "RUNNING" and d["residency_state"] == "LOADED"

    await _wait(a1_loaded, timeout=30)

    # 2. a1 is idle (no requests). With a short idle timeout (5s) and min
    #    residency (2s) it should be auto-unloaded. Poll until it goes OFF.
    t0 = time.monotonic()
    off = False
    while time.monotonic() - t0 < 25.0:
        st = await _app_state(c, "a1")
        if st["residency_state"] == "OFF" or st["process_state"] == "STOPPED":
            off = True
            break
        await asyncio.sleep(0.5)
    assert off, f"a1 was not auto-unloaded (state={st})"

    # 3. A request to gw-a1 should queue, re-load a1, and be served.
    text = await _stream(c, f"http://127.0.0.1:{A1_GW}/generate", {"chunks": 4, "delay": 0.02})
    assert text
    await _wait(lambda: _req_done(c, "COMPLETED", "gw-a1"), timeout=30)
    await c.aclose()
