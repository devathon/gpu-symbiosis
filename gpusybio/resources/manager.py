"""Resource Manager (spec 07): single source of truth for capacity numbers.

- Reservations (§6): VRAM reserved the moment a load begins; released on load
  success/failure or unload completion.
- Attribution (§5): live per-app memory from the backend, refreshed each
  sampling tick and on demand (unload verification).
- Fit check (§3): available = usable - Σ resident - external.
- Verification (§8): after an unload, live attribution must drop below the
  app's unloaded baseline before the scheduler proceeds.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass

from gpusybio.events.bus import EventBus, Event, RESOURCE_CHANGE
from gpusybio.resources.gpu import GpuBackend, GpuSample
from gpusybio.resources.system import SystemMonitor, SystemSample


@dataclass
class Reservation:
    app_id: str
    required_mb: float
    at: float


class ResourceManager:
    def __init__(
        self,
        backend: GpuBackend,
        bus: EventBus,
        memory_reserve_mb: float = 2048.0,
        memory_safety_margin: float = 0.10,
        unload_baseline_mb: float = 128.0,
        system: SystemMonitor | None = None,
        utilization_window_sec: float = 5.0,
    ):
        self._backend = backend
        self._bus = bus
        self.system = system or SystemMonitor()
        self._reserve_mb = memory_reserve_mb
        self._safety_margin = memory_safety_margin
        self._unload_baseline_mb = unload_baseline_mb
        self._reservations: dict[str, Reservation] = {}
        # Live per-app attribution (from the backend, refreshed continuously).
        self._live: dict[str, float] = {}
        self._pids: dict[str, int | None] = {}
        self._unloaded_base: dict[str, float] = {}
        self._last_sample: GpuSample | None = None
        self._lock = asyncio.Lock()
        self._last_change = 0.0
        # GPU-activity threshold for idle detection (spec 06 §10, default 5%).
        self.gpu_activity_threshold_pct: float = 5.0
        # Rolling window for the per-app GPU-utilization reading (spec 04 §3).
        # A running app can dip to 0% for a moment between bursts; averaging the
        # last N seconds keeps the "active" signal stable instead of flapping.
        self._util_window_sec = float(utilization_window_sec)
        self._util_history: dict[str, deque] = {}

    # -- properties --------------------------------------------------------
    @property
    def backend(self) -> GpuBackend:
        return self._backend

    @property
    def lock(self) -> asyncio.Lock:
        return self._lock

    def sample(self) -> GpuSample:
        s = self._backend.sample()
        self._last_sample = s
        return s

    @property
    def last_sample(self) -> GpuSample | None:
        return self._last_sample

    def system_sample(self) -> SystemSample:
        return self.system.sample()

    @property
    def total_mb(self) -> float:
        return (self._last_sample or self.sample()).total_mb

    @property
    def usable_mb(self) -> float:
        """total - reserve (spec 07 §8); the scheduler never plans beyond this."""
        return max(0.0, self.total_mb - self._reserve_mb)

    # -- process attribution -----------------------------------------------
    def set_pid(self, app_id: str, pid: int | None) -> None:
        self._pids[app_id] = pid
        set_pid = getattr(self._backend, "set_pid", None)
        if set_pid is not None:
            set_pid(app_id, pid)
        if pid is None:
            self._live[app_id] = 0.0

    def set_unloaded_baseline(self, app_id: str, baseline_mb: float) -> None:
        self._unloaded_base[app_id] = baseline_mb

    def refresh_attribution(self) -> None:
        """Refresh live per-app memory from the backend (steps the fake)."""
        step = getattr(self._backend, "step", None)
        if step is not None:
            step()
        self.sample()
        now = time.time()
        for app_id, pid in self._pids.items():
            if pid is not None:
                self._live[app_id] = self._backend.process_memory_mb(pid)
                # Record a per-process GPU-utilization sample for the rolling
                # window (spec 04 §3) — only when the backend supports it.
                if getattr(self._backend, "supports_process_utilization", False):
                    try:
                        util = float(self._backend.process_utilization_pct(pid) or 0.0)
                    except Exception:  # noqa: BLE001
                        util = 0.0
                    else:
                        self._util_history.setdefault(app_id, deque()).append((now, util))
                        self._prune_util(app_id, now)
            else:
                self._live[app_id] = 0.0
                self._util_history.pop(app_id, None)

    def _prune_util(self, app_id: str, now: float) -> None:
        """Drop utilization samples older than the rolling window."""
        dq = self._util_history.get(app_id)
        if not dq:
            return
        cutoff = now - self._util_window_sec
        while dq and dq[0][0] < cutoff:
            dq.popleft()

    # -- reservations (spec 07 §6) -----------------------------------------
    def reserve(self, app_id: str, required_mb: float) -> None:
        self._reservations[app_id] = Reservation(app_id, required_mb, time.time())

    def release(self, app_id: str) -> None:
        self._reservations.pop(app_id, None)

    def is_reserved(self, app_id: str) -> bool:
        return app_id in self._reservations

    def reservations(self) -> dict[str, Reservation]:
        return dict(self._reservations)

    def observed_mb(self, app_id: str) -> float:
        """Memory attributed to this app: reservation while loading, live after."""
        if app_id in self._reservations:
            return self._reservations[app_id].required_mb
        return self._live.get(app_id, 0.0)

    def app_utilization(self, app_id: str) -> float:
        """Per-app GPU utilization (spec 04 §3: the active/RUNNING display signal).

        No PID means no process (stopped) → 0.0, never a device-level proxy
        (which would mis-attribute a co-resident app's load to a stopped one).
        With a PID and per-process support (NVML
        ``nvmlDeviceGetProcessUtilization``, or the fake): the *rolling
        average* over the last ``utilization_window_sec`` (default 5s) —
        robust to a momentary 0% dip on a still-running app. A value of 0.0
        means the app is *idle*, so there is deliberately no device-level
        fallback. Only when the backend has no per-process support is the
        device-level rate used as a proxy (no rolling window — it is shared).
        """
        pid = self._pids.get(app_id)
        if pid is None:
            return 0.0
        if getattr(self._backend, "supports_process_utilization", False):
            now = time.time()
            dq = self._util_history.get(app_id)
            if dq:
                samples = [u for (t, u) in dq if now - t <= self._util_window_sec]
                if samples:
                    return sum(samples) / len(samples)
            # No window yet (just started): take a fresh sample.
            try:
                return float(self._backend.process_utilization_pct(pid) or 0.0)
            except Exception:  # noqa: BLE001
                return 0.0
        # Running app, but no per-process support: device-level is the proxy.
        return float((self._last_sample or self.sample()).utilization_pct or 0.0)

    # -- fit check (spec 06 §7) --------------------------------------------
    def required_for(self, configured_mb: float, safety_margin: float | None = None) -> float:
        margin = self._safety_margin if safety_margin is None else safety_margin
        return configured_mb * (1.0 + margin)

    def fits(self, app_id: str, required_mb: float, already_resident: bool = False) -> bool:
        """Candidate fits alongside current residents/reservations.

        A candidate that is already resident needs no additional VRAM
        (zero-cost path, spec 06 §7). Residency is passed in explicitly by the
        caller — it is NOT inferred from live memory: a running-but-unloaded
        app still holds a live baseline (spec 07 §5), and treating that as
        "resident" would let it load concurrently on top of other residents
        (Invariant 2 violation).
        """
        if already_resident:
            return True  # already resident — nothing to add
        return required_mb <= self.available_mb()

    def available_mb(self) -> float:
        """usable - Σ resident (reserved or live) - external (spec 07 §3).

        External = total used minus what we attribute to managed apps; the
        result is the conservative min(usable - committed, usable - used).
        """
        committed = 0.0
        for res in self._reservations.values():
            committed += res.required_mb
        for app_id, mb in self._live.items():
            if app_id not in self._reservations and mb > 0:
                committed += mb
        s = self._last_sample or self.sample()
        external = max(0.0, s.used_mb - committed)
        return max(0.0, self.usable_mb - committed - external)

    # -- verification (spec 07 §8) ------------------------------------------
    def verify_unloaded(self, app_id: str) -> bool:
        """Live attribution dropped below the app's unloaded baseline."""
        self.refresh_attribution()
        baseline = self._unloaded_base.get(app_id, self._unload_baseline_mb)
        return self._live.get(app_id, 0.0) <= baseline

    # -- sampling loop -------------------------------------------------------
    async def sampling_loop(self, interval: float = 1.0) -> None:
        """Periodic sample; publishes RESOURCE_CHANGE when VRAM moves meaningfully."""
        while True:
            prev = self._last_sample
            self.refresh_attribution()
            s = self._last_sample
            if prev is not None and abs(s.used_mb - prev.used_mb) > 64.0:
                if time.time() - self._last_change > 2.0:
                    self._last_change = time.time()
                    await self._bus.publish(
                        Event(
                            type=RESOURCE_CHANGE,
                            data={
                                "used_mb": s.used_mb,
                                "free_mb": s.free_mb,
                                "utilization_pct": s.utilization_pct,
                            },
                        )
                    )
            await asyncio.sleep(interval)


def make_resource_manager(
    backend: GpuBackend,
    bus: EventBus,
    settings,
    unload_baseline_mb: float = 128.0,
) -> ResourceManager:
    return ResourceManager(
        backend=backend,
        bus=bus,
        memory_reserve_mb=float(settings.gpu.memory_reserve_mb),
        memory_safety_margin=float(settings.gpu.memory_safety_margin),
        unload_baseline_mb=unload_baseline_mb,
        utilization_window_sec=float(getattr(settings.gpu, "utilization_window_sec", 5.0)),
    )
