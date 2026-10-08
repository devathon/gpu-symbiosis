"""Memory-based residency monitor (spec 06 §10.2).

The monitor reconciles LOADED/UNLOADED when the app loads/frees its model
outside GPUSymbiosis (e.g. ComfyUI): OFF + VRAM >= loaded_gpu_memory_mb →
LOADED; LOADED + VRAM < threshold → UNLOADED. Threshold 0 (default) =
auto-detection disabled. A single threshold has no hysteresis band; the flip
debounce is the only flapping protection.
"""

from __future__ import annotations

import asyncio

from gpusybio.applications.lifecycle import ProcessState, ResidencyState
from gpusybio.scheduler.residency_monitor import ResidencyMonitor
from gpusybio.state import StateManager


class StubApp:
    def __init__(self, loaded_gpu_memory_mb=0.0):
        self.loaded_gpu_memory_mb = loaded_gpu_memory_mb


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


def _make(states, app_id, bus, app_ids, threshold_mb=0.0):
    apps = StubApps({app_id: StubApp(loaded_gpu_memory_mb=threshold_mb)})
    res = StubResources()
    mon = ResidencyMonitor(
        res, states, apps, bus, app_ids, min_flip_sec=0.0,
    )
    return mon, apps, res


async def test_loaded_vram_low_marks_unloaded():
    states = StateManager()
    states.set_process("c1", ProcessState.STARTED)
    states.set_residency("c1", ResidencyState.LOADED)
    states.get("c1").pid = 123
    bus = StubBus()
    mon, apps, res = _make(states, "c1", bus, ["c1"], threshold_mb=9000.0)
    # VRAM 1000 MB < 9000 MB threshold → UNLOADED.
    res.mem_mb = 1000.0
    changed = await mon.tick()
    assert changed == ["c1"]
    assert states.get("c1").residency is ResidencyState.OFF
    assert bus.events[0][0] == "UNLOADED"


async def test_off_vram_high_marks_loaded():
    states = StateManager()
    states.set_process("c1", ProcessState.STARTED)
    states.set_residency("c1", ResidencyState.OFF)
    states.get("c1").pid = 123
    bus = StubBus()
    mon, apps, res = _make(states, "c1", bus, ["c1"], threshold_mb=9000.0)
    # VRAM 10000 MB >= 9000 MB threshold → LOADED.
    res.mem_mb = 10000.0
    changed = await mon.tick()
    assert changed == ["c1"]
    assert states.get("c1").residency is ResidencyState.LOADED
    assert bus.events[0][0] == "LOAD_READY"


async def test_off_vram_at_threshold_marks_loaded():
    """The boundary is inclusive: VRAM exactly at the threshold is loaded."""
    states = StateManager()
    states.set_process("c1", ProcessState.STARTED)
    states.set_residency("c1", ResidencyState.OFF)
    states.get("c1").pid = 123
    bus = StubBus()
    mon, apps, res = _make(states, "c1", bus, ["c1"], threshold_mb=9000.0)
    res.mem_mb = 9000.0
    changed = await mon.tick()
    assert changed == ["c1"]
    assert states.get("c1").residency is ResidencyState.LOADED


async def test_loaded_vram_at_threshold_stays():
    """And LOADED at exactly the threshold stays loaded (< is strict)."""
    states = StateManager()
    states.set_process("c1", ProcessState.STARTED)
    states.set_residency("c1", ResidencyState.LOADED)
    states.get("c1").pid = 123
    bus = StubBus()
    mon, apps, res = _make(states, "c1", bus, ["c1"], threshold_mb=9000.0)
    res.mem_mb = 9000.0
    changed = await mon.tick()
    assert changed == []
    assert states.get("c1").residency is ResidencyState.LOADED


async def test_threshold_zero_skipped():
    """threshold 0 (the default) disables auto-detection for the app."""
    states = StateManager()
    states.set_process("c1", ProcessState.STARTED)
    states.set_residency("c1", ResidencyState.OFF)
    states.get("c1").pid = 123
    bus = StubBus()
    mon, apps, res = _make(states, "c1", bus, ["c1"])  # threshold 0
    res.mem_mb = 10000.0
    changed = await mon.tick()
    assert changed == []
    assert states.get("c1").residency is ResidencyState.OFF


async def test_unload_only_app_tracked():
    """The monitor no longer gates on load/unload support: an unload-only
    app (no load script) loads automatically from usage."""
    states = StateManager()
    states.set_process("c1", ProcessState.STARTED)
    states.set_residency("c1", ResidencyState.OFF)
    states.get("c1").pid = 123
    bus = StubBus()
    mon, apps, res = _make(states, "c1", bus, ["c1"], threshold_mb=9000.0)
    res.mem_mb = 10000.0
    changed = await mon.tick()
    assert changed == ["c1"]
    assert states.get("c1").residency is ResidencyState.LOADED


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
