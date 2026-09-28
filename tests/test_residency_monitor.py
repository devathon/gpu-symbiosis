"""Memory-based residency monitor (spec 06 §10.2).

The monitor reconciles LOADED/UNLOADED when the app loads/frees its model
outside GPUSymbiosis (e.g. ComfyUI): LOADED + VRAM < unload threshold →
UNLOADED; OFF + VRAM >= load threshold → LOADED.
"""

from __future__ import annotations

import asyncio

from gpusybio.applications.lifecycle import ProcessState, ResidencyState
from gpusybio.scheduler.residency_monitor import ResidencyMonitor
from gpusybio.state import StateManager


class StubApp:
    def __init__(self, supports_load_unload=True, configured=12288.0,
                 load_pct=80.0, unload_pct=20.0):
        self.supports_load_unload = supports_load_unload
        self.configured_gpu_memory_mb = configured
        self.memory_load_threshold_pct = load_pct
        self.memory_unload_threshold_pct = unload_pct


class StubApps:
    def __init__(self, cfgs):
        self._cfgs = cfgs

    def config(self, app_id):
        return self._cfgs[app_id]


class StubResources:
    """Exposes a configurable per-process memory (the VRAM-held signal)."""

    def __init__(self, mem_mb=0.0):
        self.mem_mb = mem_mb
        outer = self

        class _B:
            def process_memory_mb(self, pid):
                return outer.mem_mb

        self.backend = _B()


class StubBus:
    def __init__(self):
        self.events = []

    async def publish_raw(self, event_type, **data):
        self.events.append((event_type, data))


def _make(states, app_id, bus, app_ids, supports=True, configured=12288.0):
    apps = StubApps({app_id: StubApp(supports_load_unload=supports,
                                     configured=configured)})
    res = StubResources()
    mon = ResidencyMonitor(
        res, states, apps, bus, app_ids, min_flip_sec=0.0,
    )
    return mon, apps, res


async def test_loaded_vram_low_marks_unloaded():
    states = StateManager()
    states.set_process("c1", ProcessState.RUNNING)
    states.set_residency("c1", ResidencyState.LOADED)
    states.get("c1").pid = 123
    bus = StubBus()
    mon, apps, res = _make(states, "c1", bus, ["c1"], configured=12288.0)
    # configured 12288 MB; unload threshold 20% = 2457.6 MB. VRAM 1000 MB < 2457.
    res.mem_mb = 1000.0
    changed = await mon.tick()
    assert changed == ["c1"]
    assert states.get("c1").residency is ResidencyState.OFF
    assert bus.events[0][0] == "UNLOADED"


async def test_off_vram_high_marks_loaded():
    states = StateManager()
    states.set_process("c1", ProcessState.RUNNING)
    states.set_residency("c1", ResidencyState.OFF)
    states.get("c1").pid = 123
    bus = StubBus()
    mon, apps, res = _make(states, "c1", bus, ["c1"], configured=12288.0)
    # load threshold 80% of 12288 = 9830.4 MB. VRAM 10000 MB >= 9830.4.
    res.mem_mb = 10000.0
    changed = await mon.tick()
    assert changed == ["c1"]
    assert states.get("c1").residency is ResidencyState.LOADED
    assert bus.events[0][0] == "LOAD_READY"


async def test_in_band_no_change():
    """VRAM between the thresholds → no state change (hysteresis band)."""
    states = StateManager()
    states.set_process("c1", ProcessState.RUNNING)
    states.set_residency("c1", ResidencyState.LOADED)
    states.get("c1").pid = 123
    bus = StubBus()
    mon, apps, res = _make(states, "c1", bus, ["c1"], configured=12288.0)
    # 2457.6 MB < VRAM 5000 MB < 9830.4 MB → in band, LOADED stays.
    res.mem_mb = 5000.0
    changed = await mon.tick()
    assert changed == []
    assert states.get("c1").residency is ResidencyState.LOADED


async def test_no_load_unload_skipped():
    """Apps without load/unload have nothing to reconcile."""
    states = StateManager()
    states.set_process("c1", ProcessState.RUNNING)
    states.set_residency("c1", ResidencyState.OFF)
    states.get("c1").pid = 123
    bus = StubBus()
    mon, apps, res = _make(states, "c1", bus, ["c1"], supports=False)
    res.mem_mb = 10000.0
    changed = await mon.tick()
    assert changed == []
    assert states.get("c1").residency is ResidencyState.OFF


async def test_stopped_skipped():
    """A stopped app (no pid) is not tracked."""
    states = StateManager()
    states.set_process("c1", ProcessState.STOPPED)
    states.set_residency("c1", ResidencyState.OFF)
    bus = StubBus()
    mon, apps, res = _make(states, "c1", bus, ["c1"])
    res.mem_mb = 10000.0
    changed = await mon.tick()
    assert changed == []
