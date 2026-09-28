"""GPU backend semantics (spec 07 §2).

Covers the PCIe throughput contract: ``nvmlDeviceGetPcieThroughput`` returns an
*instantaneous* rate in KB/s (NVML measures a byte counter over a fixed 20 ms
window — it is NOT a cumulative counter), so the backend must read it directly
and convert to MB/s, the same way nvtop does.
"""

from __future__ import annotations

import sys
from unittest import mock

from gpusybio.resources.gpu import MB, NvmlBackend


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
