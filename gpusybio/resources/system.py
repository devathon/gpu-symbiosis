"""System (CPU/RAM) sampling (spec 07 §2.2) — stdlib /proc based.

CPU % is computed from the /proc/stat aggregate-line delta; RAM from
/proc/meminfo. Safe fallbacks (0.0 / 0) on non-Linux systems or parse
errors — the UI degrades, nothing crashes.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass


@dataclass
class SystemSample:
    cpu_pct: float
    cpu_cores: int
    ram_total_mb: float
    ram_used_mb: float


def _cpu_times() -> tuple[float, float] | None:
    """(total, idle) jiffies from the /proc/stat aggregate line."""
    try:
        with open("/proc/stat") as f:
            parts = f.readline().split()
        if parts[0] != "cpu":
            return None
        vals = list(map(int, parts[1:]))
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)  # idle + iowait
        return float(sum(vals)), float(idle)
    except (OSError, ValueError, IndexError):
        return None


def _mem_mb() -> tuple[float, float] | None:
    """(total, used) MB from /proc/meminfo."""
    try:
        info: dict[str, int] = {}
        with open("/proc/meminfo") as f:
            for line in f:
                key, _, rest = line.partition(":")
                info[key] = int(rest.split()[0])  # kB
        total = info["MemTotal"]
        free = info.get("MemAvailable", info.get("MemFree", 0))
        return total / 1024.0, (total - free) / 1024.0
    except (OSError, ValueError, KeyError, IndexError):
        return None


class SystemMonitor:
    """Stateful CPU sampling (needs the previous /proc/stat reading)."""

    def __init__(self) -> None:
        self._prev: tuple[float, float, float] | None = None  # (total, idle, t)

    def sample(self) -> SystemSample:
        cpu = 0.0
        cur = _cpu_times()
        if cur is not None:
            total, idle = cur
            now = time.monotonic()
            if self._prev is not None:
                prev_total, prev_idle, _ = self._prev
                dt = total - prev_total
                if dt > 0:
                    cpu = 100.0 * (1.0 - (idle - prev_idle) / dt)
            self._prev = (total, idle, now)
        mem = _mem_mb()
        return SystemSample(
            cpu_pct=round(max(0.0, min(100.0, cpu)), 1),
            cpu_cores=os.cpu_count() or 0,
            ram_total_mb=round(mem[0], 1) if mem else 0.0,
            ram_used_mb=round(mem[1], 1) if mem else 0.0,
        )
