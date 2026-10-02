"""GPU backend semantics (spec 07 §2).

Covers the PCIe throughput contract: ``nvmlDeviceGetPcieThroughput`` returns an
*instantaneous* rate in KB/s (NVML measures a byte counter over a fixed 20 ms
window — it is NOT a cumulative counter), so the backend must read it directly
and convert to MB/s, the same way nvtop does.
"""

from __future__ import annotations

import subprocess
import sys
import time
from unittest import mock

import pytest

from gpusybio.resources.gpu import MB, NvmlBackend
from gpusybio.resources.process_tree import process_tree


def _fake_pynvml(rx_kbs: float, tx_kbs: float) -> mock.MagicMock:
    fake = mock.MagicMock()
    fake.nvmlDeviceGetHandleByIndex.return_value = mock.MagicMock()
    fake.nvmlDeviceGetName.return_value = b"Test GPU"
    fake.nvmlDeviceGetMemoryInfo.return_value = mock.MagicMock(total=24 * MB, used=8 * MB)
    fake.nvmlDeviceGetUtilizationRates.return_value = mock.MagicMock(gpu=50)
    fake.nvmlDeviceGetTemperature.return_value = 50
    fake.nvmlDeviceGetPowerUsage.return_value = 60000  # mW
    fake.nvmlDeviceGetClockInfo.return_value = 1500
    fake.nvmlDeviceGetPcieMaxLinkGeneration.return_value = 4
    fake.nvmlDeviceGetPcieMaxLinkWidth.return_value = 16
    fake.nvmlDeviceGetPowerManagementLimit.return_value = 250000  # mW
    fake.nvmlDeviceGetProcessUtilization.return_value = []
    fake.nvmlDeviceGetComputeRunningProcesses.return_value = []
    # Identity constants referenced by the backend.
    fake.NVML_PCIE_UTIL_RX_BYTES = 1
    fake.NVML_PCIE_UTIL_TX_BYTES = 0
    fake.NVML_TEMPERATURE_GPU = 0
    fake.NVML_CLOCK_SM = 1
    # The contract under test: an instantaneous KB/s rate, not a counter.
    fake.nvmlDeviceGetPcieThroughput.side_effect = (
        lambda _h, c: rx_kbs if c == 1 else tx_kbs
    )
    return fake


def test_pcie_throughput_is_rate_not_cumulative(monkeypatch):
    # spec 07 §2: the value is read directly and converted KB/s -> MB/s.
    # 102400 KB/s == 100 MB/s, 51200 KB/s == 50 MB/s.
    monkeypatch.setitem(sys.modules, "pynvml", _fake_pynvml(102400.0, 51200.0))
    s = NvmlBackend(0).sample()
    assert s.rx_mbps == 100.0
    assert s.tx_mbps == 50.0


def test_pcie_throughput_idle_is_zero(monkeypatch):
    # An idle GPU reports 0 KB/s -> 0.0 MB/s (no diff, no negative clamp).
    monkeypatch.setitem(sys.modules, "pynvml", _fake_pynvml(0.0, 0.0))
    s = NvmlBackend(0).sample()
    assert s.rx_mbps == 0.0
    assert s.tx_mbps == 0.0


def test_pcie_throughput_unavailable_is_none(monkeypatch):
    # When the API is unsupported (old driver / vGPU), rx/tx degrade to None.
    fake = _fake_pynvml(0.0, 0.0)
    fake.nvmlDeviceGetPcieThroughput.side_effect = Exception("not supported")
    monkeypatch.setitem(sys.modules, "pynvml", fake)
    s = NvmlBackend(0).sample()
    assert s.rx_mbps is None
    assert s.tx_mbps is None


# ---------------------------------------------------------------------------
# Process-tree attribution (spec 07 §2.2): GPU memory/utilization span the
# recorded PID *and all its descendants*, since children usually do the work.
# ---------------------------------------------------------------------------


def _spawn_parent_with_child() -> tuple[subprocess.Popen, int]:
    """A shell parent with a sleeping child: a real two-level process tree."""
    parent = subprocess.Popen(["sh", "-c", "sleep 30 & wait"])
    deadline = time.time() + 2.0
    child: int | None = None
    while time.time() < deadline and child is None:
        try:
            with open(f"/proc/{parent.pid}/task/{parent.pid}/children") as f:
                kids = f.read().split()
        except (FileNotFoundError, ProcessLookupError):
            time.sleep(0.05)
            continue
        if kids:
            child = int(kids[0])
    if child is None:
        parent.kill()
        pytest.skip("cannot observe process children in this environment")
    return parent, child


def test_process_tree_includes_children():
    parent, child = _spawn_parent_with_child()
    try:
        tree = process_tree(parent.pid)
        assert parent.pid in tree
        assert child in tree
    finally:
        parent.kill()
        parent.wait()


def test_process_tree_edge_cases():
    assert process_tree(None) == set()
    # A dead pid degrades to the single-pid set (old behavior).
    p = subprocess.Popen(["true"])
    p.wait()
    assert process_tree(p.pid) == {p.pid}


def test_memory_includes_child_processes(monkeypatch):
    parent, child = _spawn_parent_with_child()
    try:
        fake = _fake_pynvml(0.0, 0.0)
        fake.nvmlDeviceGetComputeRunningProcesses.return_value = [
            mock.MagicMock(pid=parent.pid, usedGpuMemory=2 * MB),
            mock.MagicMock(pid=child, usedGpuMemory=3 * MB),
            mock.MagicMock(pid=424242, usedGpuMemory=9 * MB),  # unrelated
        ]
        monkeypatch.setitem(sys.modules, "pynvml", fake)
        b = NvmlBackend(0)
        assert b.process_memory_mb(parent.pid) == 5.0
        assert b.process_memory_mb(424242) == 9.0
    finally:
        parent.kill()
        parent.wait()


def test_utilization_includes_child_processes(monkeypatch):
    parent, child = _spawn_parent_with_child()
    try:
        fake = _fake_pynvml(0.0, 0.0)
        ts = 10**17  # must be > the initial _last_util_ts (0)
        fake.nvmlDeviceGetProcessUtilization.return_value = [
            mock.MagicMock(pid=parent.pid, smUtil=60, timeStamp=ts),
            mock.MagicMock(pid=child, smUtil=70, timeStamp=ts),
        ]
        monkeypatch.setitem(sys.modules, "pynvml", fake)
        b = NvmlBackend(0)
        b.sample()  # refreshes the per-process utilization map
        # Children can occupy disjoint SMs: 60 + 70, capped at 100.
        assert b.process_utilization_pct(parent.pid) == 100.0
        assert b.process_utilization_pct(424242) == 0.0
        assert b.supports_process_utilization
    finally:
        parent.kill()
        parent.wait()


def test_utilization_single_process_unchanged(monkeypatch):
    # A lone process keeps the old one-PID semantics.
    parent = subprocess.Popen(["sleep", "30"])
    try:
        fake = _fake_pynvml(0.0, 0.0)
        ts = 10**17
        fake.nvmlDeviceGetProcessUtilization.return_value = [
            mock.MagicMock(pid=parent.pid, smUtil=35, timeStamp=ts),
        ]
        monkeypatch.setitem(sys.modules, "pynvml", fake)
        b = NvmlBackend(0)
        b.sample()
        assert b.process_utilization_pct(parent.pid) == 35.0
    finally:
        parent.kill()
        parent.wait()
