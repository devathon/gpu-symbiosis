"""Per-process GPU utilization (spec 04 §3 / spec 08): ``app_utilization``
returns the per-process compute rate when the backend supports it — a value of
0.0 means the app is idle, so there is NO device-level fallback (which would
mis-attribute a co-resident app's load). It falls back to device-level only
when the backend has no per-process support.
"""

from __future__ import annotations

import time

from gpusybio.events.bus import EventBus
from gpusybio.resources.gpu import GpuSample
from gpusybio.resources.manager import ResourceManager


class StubBackend:
    def __init__(self, supports: bool = True, device_util: float = 50.0):
        self.supports_process_utilization = supports
        self._device_util = device_util
        self._proc: dict[int, float] = {}

    def sample(self) -> GpuSample:
        return GpuSample(
            total_mb=24576.0, used_mb=4096.0, utilization_pct=self._device_util,
            temperature_c=45.0, power_w=60.0, name="T",
        )

    def process_memory_mb(self, pid) -> float:
        return 4096.0

    def process_utilization_pct(self, pid) -> float:
        return self._proc.get(pid, 0.0)

    def set_pid(self, app_id, pid):
        pass


def _res(supports: bool, device_util: float = 50.0) -> tuple[ResourceManager, StubBackend]:
    b = StubBackend(supports=supports, device_util=device_util)
    return ResourceManager(backend=b, bus=EventBus()), b


def test_app_utilization_per_process_active():
    res, b = _res(supports=True)
    res.set_pid("a1", 111)
    b._proc[111] = 10.0
    # The per-process value — not the device-level 50.
    assert res.app_utilization("a1") == 10.0


def test_app_utilization_per_process_idle_no_fallback():
    """The crux: per-process 0.0 means idle, so it must NOT fall back to the
    device-level rate (a co-resident app's load would be mis-attributed)."""
    res, b = _res(supports=True, device_util=50.0)
    res.set_pid("a1", 111)
    assert res.app_utilization("a1") == 0.0  # not 50.0


def test_app_utilization_fallback_when_unsupported():
    """No per-process support → the device-level rate is the only proxy."""
    res, b = _res(supports=False, device_util=50.0)
    res.set_pid("a1", 111)
    assert res.app_utilization("a1") == 50.0


def test_app_utilization_no_pid_returns_zero():
    """No PID (stopped app, no process) → 0.0, never a device-level proxy.
    This is the multi-app mis-attribution guard: a stopped app must not inherit
    a co-resident app's device-level load."""
    res, b = _res(supports=True, device_util=50.0)
    assert res.app_utilization("a1") == 0.0  # not 50.0


def test_app_utilization_no_pid_no_support_returns_zero():
    """No PID + no per-process support → still 0.0 (no process to attribute)."""
    res, b = _res(supports=False, device_util=50.0)
    assert res.app_utilization("a1") == 0.0


def test_fake_backend_reports_per_process():
    """The fake backend models per-process utilization, so it reports support
    and app_utilization returns the per-process value (not device-level)."""
    from gpusybio.resources.gpu import FakeGpuBackend

    b = FakeGpuBackend(total_mb=24576.0)
    assert b.supports_process_utilization is True
    res = ResourceManager(backend=b, bus=EventBus())
    res.set_pid("a1", 222)
    b.mark_active("a1", 12.0)
    assert res.app_utilization("a1") == 12.0
    b.mark_idle("a1")
    assert res.app_utilization("a1") == 0.0  # idle, no device-level fallback


# -- rolling window (spec 04 §3): robust to a momentary 0% dip -------------
def test_app_utilization_rolling_average_robust_to_momentary_dip():
    """A running app that dips to 0% for one sample is still 'active' — the
    rolling average over the window keeps the signal high (spec 04 §3)."""
    res, b = _res(supports=True)
    res.set_pid("a1", 111)
    b._proc[111] = 99.0
    for _ in range(5):
        res.refresh_attribution()
    assert abs(res.app_utilization("a1") - 99.0) < 1e-6
    # Now the app dips to 0% for ONE sample (a momentary gap between bursts).
    b._proc[111] = 0.0
    res.refresh_attribution()
    # Rolling avg = (4×99 + 0) / 5 = 79.2 — robust to the dip.
    assert res.app_utilization("a1") > 5.0  # still above the activity threshold


def test_app_utilization_rolling_average_is_mean():
    """The reading is the mean of the samples in the window."""
    res, b = _res(supports=True)
    res.set_pid("a1", 111)
    for v in (10.0, 30.0, 50.0):
        b._proc[111] = v
        res.refresh_attribution()
    assert abs(res.app_utilization("a1") - 30.0) < 1e-6  # mean of 10, 30, 50


def test_app_utilization_window_prunes_old_samples():
    """Samples older than the window are pruned (a long-ago burst does not keep
    the app 'active' forever)."""
    res, b = _res(supports=True)
    res.set_pid("a1", 111)
    b._proc[111] = 99.0
    res.refresh_attribution()
    # Age the sample past the 5s window.
    res._util_history["a1"][0] = (time.time() - 10.0, 99.0)
    b._proc[111] = 0.0
    res.refresh_attribution()
    # The old 99% is pruned; the average is just the fresh 0%.
    assert res.app_utilization("a1") == 0.0


def test_app_utilization_configurable_window():
    """The window is configurable (a shorter window reacts faster)."""
    res, b = _res(supports=True)
    res._util_window_sec = 1.0
    res.set_pid("a1", 111)
    b._proc[111] = 99.0
    res.refresh_attribution()
    # Age the sample 2s (past a 1s window).
    res._util_history["a1"][0] = (time.time() - 2.0, 99.0)
    b._proc[111] = 0.0
    res.refresh_attribution()
    assert res.app_utilization("a1") == 0.0  # the 99% fell out of the short window
