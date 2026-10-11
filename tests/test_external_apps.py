"""External apps monitoring (unmanaged GPU processes: list, manual kill, and
the auto-unload pass).

Unit tests drive the ExternalAppsMonitor against a FakeGpuBackend with
simulated external processes + real ``sleep`` processes for the kill paths
(the clock is injected per instance). The E2E test drives a full server to
verify the list appears in /api/status and the kill route works end to end.
"""

from __future__ import annotations

import subprocess
import time

import httpx
import pytest
from conftest import ADMIN_PORT

from gpusybio.config.models import ExternalAppsSpec
from gpusybio.events.bus import EXTERNAL_APP_KILLED
from gpusybio.resources.gpu import FakeGpuBackend
from gpusybio.scheduler.external import ExternalAppsMonitor


# -- stubs ---------------------------------------------------------------------
class _StubState:
    def __init__(self, pid):
        self.pid = pid


class _StubStates:
    def __init__(self, pids=None):
        self._pids = pids or {}  # app_id -> pid

    def all(self):
        return {aid: _StubState(pid) for aid, pid in self._pids.items()}


class _StubQueue:
    def __init__(self, lengths=None):
        self._l = lengths or {}

    def length(self, gid):
        return self._l.get(gid, 0)


class _StubRM:
    def __init__(self, lengths=None, gws=None):
        self._l = lengths or {}
        self._gws = gws or {"gw1": {"enabled": True, "paused": False}}

    def queue(self):
        return _StubQueue(self._l)

    def gateway_configs(self):
        return self._gws


class _StubBackend:
    """Wraps the fake backend for the monitor (only ``external_processes``)."""

    def __init__(self):
        self.backend = FakeGpuBackend(24576)


class _StubResources:
    def __init__(self, backend):
        self.backend = backend
        self.gpu_activity_threshold_pct = 5.0  # the "not using GPU" bar


class _StubBus:
    def __init__(self):
        self.events = []

    async def publish_raw(self, type_, **data):
        self.events.append((type_, data))


def _spec(**kw) -> ExternalAppsSpec:
    defaults = dict(gpu_threshold=20.0, vram_threshold=100.0)
    defaults.update(kw)
    return ExternalAppsSpec(**defaults)


def _mon(spec=None, states=None, rm=None, backend=None, bus=None):
    backend = backend or FakeGpuBackend(24576)
    bus = bus or _StubBus()
    mon = ExternalAppsMonitor(
        _StubResources(backend), states or _StubStates(), rm or _StubRM(), bus,
        spec or _spec(), ["a1"],
    )
    return mon, backend, bus


def _sleep() -> subprocess.Popen:
    return subprocess.Popen(["sleep", "300"])


def _kill(proc):
    try:
        proc.kill()
        proc.wait(timeout=5)
    except Exception:  # noqa: BLE001
        pass


# -- shown list -----------------------------------------------------------------
async def test_shown_by_gpu_average_above_threshold():
    mon, backend, _ = _mon()
    backend.add_external(99001, 0.0, 30.0)  # 30% avg, no VRAM
    await mon.tick()
    rows = mon.list()
    assert len(rows) == 1
    assert rows[0]["pid"] == 99001
    assert rows[0]["gpu_pct"] == pytest.approx(30.0)
    assert rows[0]["vram_mb"] == 0.0
    assert rows[0]["auto_unload_ok"] is True


async def test_shown_by_vram_above_threshold():
    mon, backend, _ = _mon()
    backend.add_external(99002, 512.0, 0.0)  # idle, 512 MB
    await mon.tick()
    rows = mon.list()
    assert len(rows) == 1
    assert rows[0]["vram_mb"] == pytest.approx(512.0)
    assert rows[0]["gpu_pct"] == 0.0


async def test_hidden_when_below_both_thresholds():
    mon, backend, _ = _mon()
    backend.add_external(99003, 64.0, 10.0)
    await mon.tick()
    assert mon.list() == []


async def test_average_over_rolling_window_not_instant_spike():
    # Busy for a while, then idle: the shown value is the WINDOW AVERAGE
    # (spike-proof), and once the busy samples age out of the window it drops.
    mon, backend, _ = _mon()
    backend.add_external(99004, 0.0, 30.0)
    t = 0.0
    mon._now = lambda: t
    await mon.tick()  # busy sample @ t=0
    t = 30.0
    backend.set_external(99004, utilization_pct=0.0)
    await mon.tick()  # idle sample @ t=30
    # average (30 + 0)/2 = 15 < 20 -> hidden even though it was busy recently
    assert mon.list() == []
    t = 61.0
    await mon.tick()  # the t=0 busy sample aged out (window 60 s)
    assert mon._avg_util(99004) == pytest.approx(0.0)


async def test_vanished_pid_dropped():
    p = _sleep()
    mon, backend, _ = _mon()
    backend.add_external(p.pid, 0.0, 30.0)
    await mon.tick()
    assert len(mon.list()) == 1
    _kill(p)
    backend.remove_external(p.pid)  # it is gone from the device now
    await mon.tick()
    assert mon.list() == []
    assert mon._hist == {} and mon._vram == {}


# -- managed-process exclusion --------------------------------------------------
async def test_managed_pid_and_its_children_are_excluded():
    # The app's own process tree (root + real child) is never "external".
    parent = subprocess.Popen(["bash", "-c", "sleep 300 & wait"])
    try:
        child = parent.poll()
        assert child is None
        # find the sleep child under the bash parent
        deadline = time.time() + 3.0
        child_pid = None
        while time.time() < deadline:
            out = subprocess.run(["pgrep", "-P", str(parent.pid)], capture_output=True, text=True).stdout.split()
            if out:
                child_pid = int(out[0])
                break
            time.sleep(0.05)
        assert child_pid, "sleep child not found"
        backend = FakeGpuBackend(24576)
        backend.add_external(parent.pid, 0.0, 30.0)   # the app root
        backend.add_external(child_pid, 0.0, 30.0)   # its child (already attributed)
        mon, _, _ = _mon(states=_StubStates({"a1": parent.pid}), backend=backend)
        await mon.tick()
        assert mon.list() == []
    finally:
        _kill(parent)


# -- manual kill ----------------------------------------------------------------
async def test_manual_kill_terminates_process_and_publishes_event():
    p = _sleep()
    mon, backend, bus = _mon()
    backend.add_external(p.pid, 256.0, 0.0)
    await mon.tick()
    result = await mon.kill(p.pid)
    assert result["killed"] is True
    p.wait(timeout=5)
    assert p.returncode != 0  # killed
    assert bus.events[0][0] == EXTERNAL_APP_KILLED
    data = bus.events[0][1]
    assert data["pid"] == p.pid
    assert data["reason"] == "manual"
    assert mon.list() == []  # forgotten immediately


async def test_kill_already_gone_is_a_success():
    p = _sleep()
    pid = p.pid
    _kill(p)
    mon, backend, _ = _mon()
    backend.add_external(pid, 256.0, 0.0)
    result = await mon.kill(pid)
    assert result == {"killed": False, "detail": "process already gone"}
    assert mon._hist == {}  # state cleaned up


# -- auto-unload pass -------------------------------------------------------------
async def test_auto_kill_when_idle_vram_and_gateway_waiting():
    p = _sleep()
    try:
        mon, backend, bus = _mon(rm=_StubRM(lengths={"gw1": 3}))  # 3 waiting
        backend.add_external(p.pid, 512.0, 0.0)  # idle (avg 0 < 5), VRAM 512
        await mon.tick()
        assert p.poll() is not None, "should have been auto-killed"
        kinds = [t for t, _ in bus.events]
        assert kinds == [EXTERNAL_APP_KILLED]
        assert bus.events[0][1]["reason"] == "auto"
    finally:
        _kill(p)


async def test_no_auto_kill_without_a_waiting_gateway():
    p = _sleep()
    try:
        mon, backend, bus = _mon(rm=_StubRM(lengths={}))  # nobody waiting
        backend.add_external(p.pid, 512.0, 0.0)
        await mon.tick()
        assert p.poll() is None, "must survive with no gateway waiting"
        assert bus.events == []
    finally:
        _kill(p)


async def test_no_auto_kill_when_only_paused_gateways_wait():
    p = _sleep()
    try:
        mon, backend, bus = _mon(
            rm=_StubRM(lengths={"gw1": 3}, gws={"gw1": {"enabled": True, "paused": True}})
        )
        backend.add_external(p.pid, 512.0, 0.0)
        await mon.tick()
        assert p.poll() is None
        assert bus.events == []
    finally:
        _kill(p)


async def test_no_auto_kill_when_disabled():
    p = _sleep()
    try:
        mon, backend, bus = _mon(spec=_spec(auto_unload=False), rm=_StubRM(lengths={"gw1": 3}))
        backend.add_external(p.pid, 512.0, 0.0)
        await mon.tick()
        assert p.poll() is None
        assert bus.events == []
    finally:
        _kill(p)


async def test_no_auto_kill_when_busy():
    p = _sleep()
    try:
        mon, backend, bus = _mon(rm=_StubRM(lengths={"gw1": 3}))
        backend.add_external(p.pid, 512.0, 30.0)  # using the GPU
        await mon.tick()
        assert p.poll() is None, "never kill a process that is using the GPU"
        assert bus.events == []
    finally:
        _kill(p)


async def test_no_auto_kill_when_excluded_by_regexp():
    # A real process whose argv[0] is "vlc" (the exclude regexp "ffmpeg|vlc"
    # matches the command line). ``exec -a`` rewrites argv[0].
    p = subprocess.Popen(["sh", "-c", "exec -a vlc sleep 300"])
    try:
        mon, backend, bus = _mon(
            spec=_spec(auto_unload_exclude="ffmpeg|vlc"), rm=_StubRM(lengths={"gw1": 3})
        )
        backend.add_external(p.pid, 512.0, 0.0)
        await mon.tick()
        rows = mon.list()
        assert rows and rows[0]["auto_unload_ok"] is False
        assert p.poll() is None, "excluded commands are never auto-killed"
        assert bus.events == []
    finally:
        _kill(p)


async def test_config_view():
    mon, _, _ = _mon(spec=_spec(auto_unload_exclude="ffmpeg|vlc"))
    assert mon.config() == {
        "gpu_threshold": 20.0,
        "vram_threshold": 100.0,
        "auto_unload": True,
        "auto_unload_exclude": "ffmpeg|vlc",
    }


# -- E2E: full server -------------------------------------------------------------
async def test_e2e_external_apps_list_status_and_kill(server):
    base = f"http://127.0.0.1:{ADMIN_PORT}"
    fake = server.backend
    p = _sleep()
    try:
        fake.add_external(p.pid, 512.0, 35.0)
        # Drive the monitor deterministically (busy so it is never auto-killed).
        server.external_apps._task.cancel()
        await server.external_apps.tick()
        assert any(row["pid"] == p.pid for row in server.external_apps.list())
        async with httpx.AsyncClient() as c:
            st = (await c.get(f"{base}/api/status")).json()
            rows = [e for e in st["external_apps"] if e["pid"] == p.pid]
            assert len(rows) == 1
            assert rows[0]["vram_mb"] == pytest.approx(512.0)
            assert rows[0]["gpu_pct"] == pytest.approx(35.0)
            # config endpoint
            cfg = (await c.get(f"{base}/api/external-apps/config")).json()
            assert cfg["gpu_threshold"] == 20.0 and cfg["vram_threshold"] == 100.0
            assert cfg["auto_unload"] is True
            # manual kill via the API
            r = await c.post(f"{base}/api/external-apps/{p.pid}/kill")
            assert r.status_code == 200
            assert r.json()["killed"] is True
        p.wait(timeout=5)
        assert p.returncode != 0
        # gone from the list afterwards (the device no longer reports the pid —
        # the fake models that by removing it from its simulated set, as NVML
        # would by dropping the dead process).
        fake.remove_external(p.pid)
        await server.external_apps.tick()
        assert all(row["pid"] != p.pid for row in server.external_apps.list())
    finally:
        _kill(p)
