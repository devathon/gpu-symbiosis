"""GPU backends (spec 07 §2): one interface, NVML + fake implementations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

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


class GpuBackend(Protocol):
    """Hardware abstraction (spec 07 §2)."""

    def sample(self) -> GpuSample: ...

    def process_memory_mb(self, pid: int | None) -> float:
        """GPU memory attributed to one PID (0.0 if unknown)."""
        ...

    def process_utilization_pct(self, pid: int | None) -> float: ...


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
        # PCIe throughput counters are cumulative; keep the last reading.
        self._pcie_prev: tuple[float, float, float] | None = None  # (rx, tx, t)

    def sample(self) -> GpuSample:
        import time

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
        rx = tx = None
        try:
            rx_c = float(pynvml.nvmlDeviceGetPcieThroughput(h, getattr(pynvml, "NVML_PCIE_UTIL_RX_BYTES", 0x10000)))
            tx_c = float(pynvml.nvmlDeviceGetPcieThroughput(h, getattr(pynvml, "NVML_PCIE_UTIL_TX_BYTES", 0x20000)))
            now = time.monotonic()
            if self._pcie_prev is not None:
                prev_rx, prev_tx, prev_t = self._pcie_prev
                dt = now - prev_t
                if dt > 0.05:
                    # The counters are cumulative 64-bit values; a driver reset
                    # or counter wrap makes a reading *decrease* — clamp to 0
                    # instead of reporting a negative rate.
                    rx = max(0.0, rx_c - prev_rx) / dt / MB
                    tx = max(0.0, tx_c - prev_tx) / dt / MB
            self._pcie_prev = (rx_c, tx_c, now)
        except Exception:  # noqa: BLE001
            self._pcie_prev = None
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
        if pid is None:
            return 0.0
        pynvml = self._pynvml
        try:
            infos = pynvml.nvmlDeviceGetComputeRunningProcesses(self._handle)
        except pynvml.NVMLError:
            return 0.0
        for info in infos:
            if info.pid == pid:
                return info.usedGpuMemory / MB
        return 0.0

    def process_utilization_pct(self, pid: int | None) -> float:
        # NVML does not expose per-process utilization on all drivers; 0.0.
        return 0.0

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
