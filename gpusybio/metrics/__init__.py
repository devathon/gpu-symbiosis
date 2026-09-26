"""Metrics (spec 08): 1 s sampling, rolling history, event log."""

from __future__ import annotations

import asyncio
import time
from collections import deque

from gpusybio.applications.lifecycle import ProcessState, ResidencyState
from gpusybio.database.db import SessionFactory
from gpusybio.database.models import MetricSample
from gpusybio.events.log import get_logger
from gpusybio.gateway.request_manager import RequestManager
from gpusybio.resources.manager import ResourceManager
from gpusybio.state import StateManager

log = get_logger("metrics")

HISTORY_SEC = 600  # 10 min rolling window (spec 08 §1)


class MetricsCollector:
    def __init__(
        self,
        resources: ResourceManager,
        states: StateManager,
        request_manager: RequestManager,
        db: SessionFactory,
        app_ids: list[str],
    ):
        self._resources = resources
        self._states = states
        self._rm = request_manager
        self._db = db
        self._app_ids = app_ids
        self._history: deque = deque()
        self._task: asyncio.Task | None = None
        self._running = False

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.get_event_loop().create_task(self._loop(), name="metrics")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def _loop(self, interval: float = 1.0) -> None:
        while self._running:
            try:
                self._sample()
            except Exception:  # noqa: BLE001 - sampling must never die
                log.exception("metrics sample failed")
            await asyncio.sleep(interval)

    def _sample(self) -> dict:
        s = self._resources.sample()
        sys_ = self._resources.system_sample()
        now = time.time()
        rec = {
            "at": now,
            "gpu_total_mb": s.total_mb,
            "gpu_used_mb": s.used_mb,
            "gpu_free_mb": s.free_mb,
            "gpu_available_mb": self._resources.available_mb(),
            "gpu_utilization_pct": s.utilization_pct,
            "vram_pct": round(s.used_pct, 1),
            "gpu_temperature_c": s.temperature_c,
            "gpu_power_w": s.power_w,
            "gpu_name": s.name,
            "gpu_pcie": s.pcie,
            "gpu_clock_mhz": s.clock_mhz,
            "gpu_rx_mbps": s.rx_mbps,
            "gpu_tx_mbps": s.tx_mbps,
            "gpu_power_limit_w": s.power_limit_w,
            "cpu_pct": sys_.cpu_pct,
            "cpu_cores": sys_.cpu_cores,
            "ram_total_mb": sys_.ram_total_mb,
            "ram_used_mb": sys_.ram_used_mb,
            "applications": {},
            "gateways": {},
        }
        for app_id in self._app_ids:
            st = self._states.get(app_id)
            mem = self._resources.observed_mb(app_id)
            rec["applications"][app_id] = {
                "gpu_memory_mb": round(mem, 1),
                "gpu_utilization_pct": self._resources.backend.process_utilization_pct(st.pid),
                "active_requests": st.active_requests,
                "process_state": st.process.value,
                "residency_state": st.residency.value,
            }
        for gw_id, gw in self._rm.gateway_configs().items():
            rec["gateways"][gw_id] = {
                "queued": self._rm.queue().length(gw_id),
                "active": self._rm.active_count(gw["application_id"]),
                "oldest_wait_sec": round(self._rm.queue().oldest_wait_sec(gw_id), 1),
                "priority": gw["priority"],
                "enabled": gw.get("enabled", True),
            }

        self._history.append(rec)
        cutoff = now - HISTORY_SEC
        while self._history and self._history[0]["at"] < cutoff:
            self._history.popleft()

        # Persist (spec 08 §6: 1 s raw samples).
        try:
            with self._db() as session:
                session.add(MetricSample(gpu_pct=s.utilization_pct, gpu_mem_mb=s.used_mb,
                                         gpu_temp_c=s.temperature_c, gpu_power_w=s.power_w))
                for app_id in self._app_ids:
                    st = self._states.get(app_id)
                    session.add(
                        MetricSample(
                            application_id=app_id,
                            gpu_pct=self._resources.backend.process_utilization_pct(st.pid),
                            gpu_mem_mb=self._resources.observed_mb(app_id),
                            active_requests=st.active_requests,
                        )
                    )
        except Exception:  # noqa: BLE001
            log.exception("metrics persist failed")
        return rec

    def history(self) -> list[dict]:
        return list(self._history)

    def current(self) -> dict | None:
        return self._history[-1] if self._history else None
