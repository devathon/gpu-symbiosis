"""GPU backends (spec 07 §2): one interface, NVML + fake implementations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from gpusybio.resources.process_tree import process_tree

MB = 1024 * 1024


@dataclass
class GpuSample:
    total_mb: float
    used_mb: float
    utilization_pct: float
    temperature_c: float | None
    power_w: float | None
    # Identity / extra telemetry (spec 07 §2.2): None where the backend
    # cannot provide it; the UI degrades gracefully.
    name: str = ""
    pcie: str = ""
    clock_mhz: float | None = None
    rx_mbps: float | None = None
    tx_mbps: float | None = None
    power_limit_w: float | None = None

    @property
    def free_mb(self) -> float:
        return max(0.0, self.total_mb - self.used_mb)

    @property
    def used_pct(self) -> float:
        return (self.used_mb / self.total_mb * 100.0) if self.total_mb else 0.0


@dataclass
class ExternalProcess:
    """One GPU process as seen by the backend (the external-apps monitor
    filters out the PIDs of managed applications, including their children)."""

    pid: int
    memory_mb: float
    utilization_pct: float


class GpuBackend(Protocol):
    """Hardware abstraction (spec 07 §2)."""

    def sample(self) -> GpuSample: ...

    def external_processes(self) -> list[ExternalProcess]:
        """Every GPU compute process on the device (pid, VRAM, SM
        utilization). Callers exclude managed applications; on the fake
        backend this returns only the simulated external set."""
        ...

    def process_memory_mb(self, pid: int | None) -> float:
        """GPU memory attributed to a PID *and all its descendant
        processes* (0.0 if unknown)."""
        ...

    def process_utilization_pct(self, pid: int | None) -> float:
        """Compute utilization attributed to a PID *and all its descendant
        processes* (0.0 if unknown)."""
        ...

    @property
    def supports_process_utilization(self) -> bool:
        """True if per-process *compute* utilization is available (NVML
        ``nvmlDeviceGetProcessUtilization``, or the fake). When True, a
        ``process_utilization_pct`` of 0.0 means the process is idle, so callers
        must NOT fall back to device-level. When False, it always returns 0.0
        and callers fall back to the device-level rate as a proxy.
        """
        ...


class FakeGpuBackend:
    """Config-driven simulation for tests and the demo.

    Per-application memory is driven by the Resource Manager (reservation /
    loaded state), with configurable ramp-up during load and ramp-down during
    unload so readiness/drain verification behaves like real hardware.
    """

    def __init__(self, total_mb: float, baseline_mb: float = 512.0):
        self._total_mb = total_mb
        self._baseline_mb = baseline_mb
        # app_id -> currently resident (loaded) memory
        self._app_mb: dict[str, float] = {}
        # app_id -> target memory (what loaded state means)
        self._targets: dict[str, float] = {}
        # app_id -> simulated GPU utilization while active
        self._active_util: dict[str, float] = {}
        # pid -> app_id for live attribution
        self._pid_app: dict[int, str] = {}
        # Simulated EXTERNAL (unmanaged) processes: pid -> (memory_mb, utilization_pct).
        # Independent of the app-driven memory above, so the external-apps monitor
        # and its tests see only these.
        self._external: dict[int, tuple[float, float]] = {}

    # -- simulation controls (used by ResourceManager) -------------------
    def set_target(self, app_id: str, total_mb: float) -> None:
        self._targets[app_id] = total_mb
        if app_id not in self._app_mb:
            self._app_mb[app_id] = 0.0

    def set_loaded(self, app_id: str, memory_mb: float) -> None:
        """Instant residency change (deterministic for tests)."""
        self._targets[app_id] = memory_mb
        self._app_mb[app_id] = memory_mb

    def mark_active(self, app_id: str, utilization_pct: float) -> None:
        self._active_util[app_id] = utilization_pct

    def mark_idle(self, app_id: str) -> None:
        self._active_util.pop(app_id, None)

    def set_pid(self, app_id: str, pid: int | None) -> None:
        """Register the app's PID for attribution (current memory is live)."""
        if pid is None:
            return
        self._pid_app[pid] = app_id

    # -- external-process simulation (external-apps monitor / tests) --------
    def add_external(self, pid: int, memory_mb: float, utilization_pct: float = 0.0) -> None:
        """Simulate an unmanaged process using the GPU / holding VRAM."""
        self._external[pid] = (memory_mb, utilization_pct)

    def set_external(self, pid: int, memory_mb: float | None = None, utilization_pct: float | None = None) -> None:
        """Adjust an existing simulated external process (missing fields kept)."""
        cur = self._external.get(pid, (0.0, 0.0))
        self._external[pid] = (
            memory_mb if memory_mb is not None else cur[0],
            utilization_pct if utilization_pct is not None else cur[1],
        )

    def remove_external(self, pid: int) -> None:
        self._external.pop(pid, None)

    def step(self) -> None:
        """No-op: transitions are instant in the fake backend."""
        for app_id in self._app_mb:
            self._app_mb[app_id] = self._targets.get(app_id, 0.0)

    # -- GpuBackend -------------------------------------------------------
    def sample(self) -> GpuSample:
        used = self._baseline_mb + sum(self._app_mb.values())
        util = max(self._active_util.values(), default=0.0)
        return GpuSample(
            total_mb=self._total_mb,
            used_mb=min(used, self._total_mb),
            utilization_pct=min(util, 100.0),
            temperature_c=45.0 + util * 0.4,
            power_w=60.0 + util * 1.4,
            name="Fake GPU",
            pcie="PCIe Gen 4 @ 16x",
            clock_mhz=1500.0 + util * 3.0,
            rx_mbps=round(util * 2.0, 1),
            tx_mbps=round(util * 2.0, 1),
            power_limit_w=250.0,
        )

    def process_memory_mb(self, pid: int | None) -> float:
        if pid is None:
            return 0.0
        app_id = self._pid_app.get(pid)
        if app_id is None:
            return 0.0
        cur = self._app_mb.get(app_id, 0.0)
        if cur > 0:
            return cur
        # Process alive but model unloaded: small resident slice.
        return self._baseline_mb * 0.3

    def process_utilization_pct(self, pid: int | None) -> float:
        if pid is None:
            return 0.0
        app_id = self._pid_app.get(pid)
        return self._active_util.get(app_id, 0.0) if app_id else 0.0

    @property
    def supports_process_utilization(self) -> bool:
        # The fake models per-process utilization via mark_active().
        return True

    def external_processes(self) -> list[ExternalProcess]:
        # Only the simulated external set (app-driven memory never appears).
        return [
            ExternalProcess(pid=pid, memory_mb=mem, utilization_pct=util)
            for pid, (mem, util) in sorted(self._external.items())
        ]


class NvmlBackend:
    """NVIDIA NVML backend (spec 16 §2.2: nvidia-ml-py)."""

    def __init__(self, device_index: int = 0):
        import pynvml  # nvidia-ml-py

        pynvml.nvmlInit()
        self._pynvml = pynvml
        self._handle = pynvml.nvmlDeviceGetHandleByIndex(device_index)
        # Identity / static limits (spec 07 §2.2).
        try:
            name = pynvml.nvmlDeviceGetName(self._handle)
            self._name = name.decode() if isinstance(name, bytes) else str(name)
        except Exception:  # noqa: BLE001
            self._name = ""
        try:
            gen = pynvml.nvmlDeviceGetPcieMaxLinkGeneration(self._handle)
            width = pynvml.nvmlDeviceGetPcieMaxLinkWidth(self._handle)
            self._pcie = f"PCIe Gen {gen} @ {width}x"
        except Exception:  # noqa: BLE001
            self._pcie = ""
        try:
            self._power_limit_w = pynvml.nvmlDeviceGetPowerManagementLimit(self._handle) / 1000.0
        except Exception:  # noqa: BLE001
            self._power_limit_w = None
        # Per-process utilization (nvmlDeviceGetProcessUtilization): a sliding
        # window. `last_seen_timestamp` filters samples newer than the previous
        # sample period; `proc_util` is the {pid: smUtil} map from the last
        # period. `supports_proc_util` is set once a call succeeds (driver 525+).
        self._last_util_ts: int = 0
        self._proc_util: dict[int, float] = {}
        self._supports_proc_util = False

    def sample(self) -> GpuSample:
        pynvml = self._pynvml
        h = self._handle
        mem = pynvml.nvmlDeviceGetMemoryInfo(h)
        util = pynvml.nvmlDeviceGetUtilizationRates(h)
        try:
            temp = pynvml.nvmlDeviceGetTemperature(h, pynvml.NVML_TEMPERATURE_GPU)
        except pynvml.NVMLError:
            temp = None
        try:
            power = pynvml.nvmlDeviceGetPowerUsage(h) / 1000.0
        except pynvml.NVMLError:
            power = None
        try:
            clock = float(pynvml.nvmlDeviceGetClockInfo(h, pynvml.NVML_CLOCK_SM))
        except Exception:  # noqa: BLE001
            clock = None
        # PCIe throughput: nvmlDeviceGetPcieThroughput returns an
        # *instantaneous* rate in KB/s — NVML measures a byte counter over a
        # fixed 20 ms window, it is NOT a cumulative counter (nvtop reads it
        # directly, the same way). Convert KB/s → MB/s to match the
        # rx_mbps/tx_mbps fields (spec 07 §2).
        rx = tx = None
        try:
            rx = float(pynvml.nvmlDeviceGetPcieThroughput(h, getattr(pynvml, "NVML_PCIE_UTIL_RX_BYTES", 0x10000))) / 1024.0
            tx = float(pynvml.nvmlDeviceGetPcieThroughput(h, getattr(pynvml, "NVML_PCIE_UTIL_TX_BYTES", 0x20000))) / 1024.0
        except Exception:  # noqa: BLE001
            rx = tx = None
        self._refresh_proc_util()
        return GpuSample(
            total_mb=mem.total / MB,
            used_mb=mem.used / MB,
            utilization_pct=float(util.gpu),
            temperature_c=temp,
            power_w=power,
            name=self._name,
            pcie=self._pcie,
            clock_mhz=clock,
            rx_mbps=round(rx, 1) if rx is not None else None,
            tx_mbps=round(tx, 1) if tx is not None else None,
            power_limit_w=self._power_limit_w,
        )

    def process_memory_mb(self, pid: int | None) -> float:
        """GPU memory attributed to the PID and all its descendants
        (spec 07 §2.2): every compute process in the tree is summed, since
        children are the ones that usually hold the VRAM."""
        if pid is None:
            return 0.0
        pynvml = self._pynvml
        try:
            infos = pynvml.nvmlDeviceGetComputeRunningProcesses(self._handle)
        except pynvml.NVMLError:
            return 0.0
        pids = process_tree(pid)
        total = 0.0
        for info in infos:
            if info.pid in pids:
                total += info.usedGpuMemory / MB
        return total

    @property
    def supports_process_utilization(self) -> bool:
        return self._supports_proc_util

    def _refresh_proc_util(self) -> None:
        """One ``nvmlDeviceGetProcessUtilization`` call per sample period,
        mirroring nvtop: it returns samples for every process that used the GPU
        since ``last_seen_timestamp``. Build the {pid: smUtil} map and advance
        the window. A process absent from the map is idle (0%).

        The API needs a recent driver (525+); on older drivers the call raises
        and we keep ``_supports_proc_util`` False so callers fall back to the
        device-level rate. Samples with util > 100 or a non-newer timestamp are
        dropped (NVML bug #110, the same filter nvtop applies).
        """
        pynvml = self._pynvml
        try:
            samples = pynvml.nvmlDeviceGetProcessUtilization(self._handle, self._last_util_ts)
        except Exception:  # noqa: BLE001 - API unavailable / transient
            return
        new_map: dict[int, float] = {}
        max_ts = self._last_util_ts
        for s in samples:
            if s.timeStamp > self._last_util_ts and s.smUtil <= 100:
                new_map[s.pid] = float(s.smUtil)
                if s.timeStamp > max_ts:
                    max_ts = s.timeStamp
        self._proc_util = new_map
        self._last_util_ts = max_ts
        self._supports_proc_util = True

    def process_utilization_pct(self, pid: int | None) -> float:
        """Per-process compute utilization (SM rate) across the PID's whole
        process tree; 0.0 when no process in the tree is busy.

        Children can occupy disjoint SMs simultaneously, so their rates are
        summed (capped at 100) rather than maxed."""
        if pid is None:
            return 0.0
        total = 0.0
        for p in process_tree(pid):
            u = self._proc_util.get(p)
            if u:
                total += u
        return min(100.0, total)

    def external_processes(self) -> list[ExternalProcess]:
        """Every compute process on the device (the caller excludes managed
        application trees). SM utilization comes from the last
        ``nvmlDeviceGetProcessUtilization`` window — absent means idle (0%)."""
        pynvml = self._pynvml
        try:
            infos = pynvml.nvmlDeviceGetComputeRunningProcesses(self._handle)
        except Exception:  # noqa: BLE001 - transient / API unavailable
            return []
        return [
            ExternalProcess(
                pid=info.pid,
                memory_mb=(info.usedGpuMemory or 0) / MB,
                utilization_pct=self._proc_util.get(info.pid, 0.0),
            )
            for info in infos
        ]

    def set_loaded(self, app_id: str, memory_mb: float) -> None:
        """No-op on real hardware.

        The app manager calls this to drive the *fake* backend's simulated
        VRAM (spec 07 §2); with NVML the memory usage is read live from the
        device, so there is nothing to set. Present here so the same
        application-manager code path runs on both backends.
        """

    def close(self) -> None:
        self._pynvml.nvmlShutdown()


def make_backend(spec) -> GpuBackend:
    """Build a backend from `GpuSpec` (auto: NVML if available, else fake)."""
    if spec.backend == "fake":
        return FakeGpuBackend(spec.fake_total_mb)
    if spec.backend == "nvml":
        return NvmlBackend()
    try:
        return NvmlBackend()
    except Exception:
        return FakeGpuBackend(spec.fake_total_mb)
