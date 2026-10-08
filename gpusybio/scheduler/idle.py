"""Phase 4 — automatic idle detection & unloading (spec 06 §10).

Idle is detected per application:

- **API apps** (have gateways): active while ``active_requests > 0``.
- **Non-API apps** (e.g. ComfyUI): active while their per-app GPU utilization
  (``resources.backend.process_utilization_pct``) is >= the activity threshold
  (``gpu_activity_threshold_pct``, default 5%, spec 06 §10).

An app that has been idle for its ``idle_timeout`` and is unloadable is
**proactively unloaded** to free VRAM. Hard exclusions (spec 06 §3.1) — pinned
and ``allow_automatic_unload = false`` — are never auto-unloaded; minimum
residency and pending demand (queued requests / recent arrival) also suppress
the unload. This is the "idle non-API apps become unload candidates" path of
spec 06 §10, generalized to idle API apps with no pending demand.
"""

from __future__ import annotations

import asyncio
import time

from gpusybio.applications.lifecycle import ProcessState, ResidencyState
from gpusybio.events.bus import EventBus, IDLE_TIMEOUT
from gpusybio.events.log import get_logger

log = get_logger("idle")

# An API app with this (or higher) recent arrival rate/min is "pending demand"
# and is not auto-unloaded, to avoid flapping unload/reload.
_PENDING_DEMAND_MIN_PER_MIN = 0.1


class IdleUnloader:
    def __init__(
        self,
        resources,
        states,
        apps,
        rm,
        bus: EventBus,
        app_ids: list[str],
        gateway_ids: list[str],
        min_residency_sec: float = 30.0,
        default_idle_timeout_sec: float = 300.0,
        interval: float = 5.0,
    ) -> None:
        self._resources = resources
        self._states = states
        self._apps = apps
        self._rm = rm
        self._bus = bus
        self._app_ids = list(app_ids)
        self._gateway_ids = list(gateway_ids)
        self._min_residency = min_residency_sec
        self._default_idle_timeout = default_idle_timeout_sec
        self._interval = interval
        # app_id -> last timestamp the app was observed active.
        self._last_active: dict[str, float] = {}
        self._task: asyncio.Task | None = None

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("idle tick failed")
            await asyncio.sleep(self._interval)

    # -- detection ----------------------------------------------------------
    def _has_gateway(self, app_id: str) -> bool:
        return any(self._rm.gateway(g)["application_id"] == app_id for g in self._gateway_ids)

    def _app_gpu_util(self, app_id: str) -> float:
        s = self._states.get(app_id)
        if s.pid is None:
            return 0.0
        try:
            return float(self._resources.backend.process_utilization_pct(s.pid) or 0.0)
        except Exception:
            return 0.0

    def _threshold(self, app_id: str) -> float:
        try:
            t = self._apps.config(app_id).idle_threshold_gpu_pct
            return float(t) if t else self._resources.gpu_activity_threshold_pct
        except Exception:
            return self._resources.gpu_activity_threshold_pct

    def is_active(self, app_id: str) -> bool:
        """True if the app is active: serving a request OR using the GPU
        (spec 04 §3) — the single ``active`` definition, shared with the
        display and the scheduler."""
        from gpusybio.scheduler.activity import is_active

        return is_active(self._states.get(app_id), self._resources, self._threshold(app_id))

    def last_active(self, app_id: str) -> float | None:
        """Last timestamp the app was observed active (None if never seen active)."""
        s = self._states.get(app_id)
        return self._last_active.get(app_id, s.loaded_at if s.residency is ResidencyState.LOADED else None)

    def _idle_timeout(self, app_id: str) -> float:
        cfg = self._apps.config(app_id)
        return cfg.idle_unload_timeout_sec or self._default_idle_timeout

    def _pending_demand(self, app_id: str) -> bool:
        if not self._has_gateway(app_id):
            return False
        if self._rm.queued_count(app_id) > 0:
            return True
        d = self._rm.demand()
        gws = [g for g in self._gateway_ids if self._rm.gateway(g)["application_id"] == app_id]
        return any(d.arrival_rate(g) > _PENDING_DEMAND_MIN_PER_MIN for g in gws)

    def _can_auto_unload(self, app_id: str) -> tuple[bool, str]:
        cfg = self._apps.config(app_id)
        s = self._states.get(app_id)
        if s.active_requests > 0:
            return False, "active_requests>0"
        if s.draining:
            return False, "draining"
        if cfg.pinned:
            return False, "pinned"
        if not cfg.allow_automatic_unload:
            return False, "allow_automatic_unload=false"
        if s.loaded_at and (time.time() - s.loaded_at) < self._min_residency:
            return False, "minimum residency"
        if self._pending_demand(app_id):
            return False, "pending demand"
        return True, "ok"

    # -- tick ---------------------------------------------------------------
    async def tick(self) -> list[str]:
        """One detection pass. Returns the ids auto-unloaded this tick."""
        now = time.time()
        unloaded: list[str] = []
        for app_id in self._app_ids:
            s = self._states.get(app_id)
            if not (s.process is ProcessState.STARTED and s.residency is ResidencyState.LOADED):
                self._last_active.pop(app_id, None)
                continue
            if s.draining:
                continue
            if self.is_active(app_id):
                self._last_active[app_id] = now
                continue
            # idle: how long?
            last = self._last_active.get(app_id, s.loaded_at or now)
            idle_for = now - last
            if idle_for < self._idle_timeout(app_id):
                continue
            ok, reason = self._can_auto_unload(app_id)
            if not ok:
                log.debug("skip auto-unload", app_id=app_id, reason=reason)
                continue
            log.info("auto-unloading idle app", app_id=app_id, idle_for_sec=round(idle_for, 1))
            await self._bus.publish_raw(
                IDLE_TIMEOUT,
                application_id=app_id,
                idle_for_sec=round(idle_for, 1),
                method="requests" if self._has_gateway(app_id) else "gpu_usage",
            )
            try:
                # release(): unload (keep process) if the app supports
                # load/unload, else stop it directly — an app without load/unload
                # scripts cannot be unloaded, only stopped (frees the same VRAM).
                await self._apps.release(app_id)
                unloaded.append(app_id)
                self._last_active.pop(app_id, None)
            except Exception:
                log.exception("auto-unload failed", app_id=app_id)
        return unloaded
