"""Memory-based residency monitor (spec 06 §10).

Some applications (e.g. ComfyUI) load and free their model *outside*
GPUSymbiosis — directly, via their own API. This monitor watches each app's
per-process GPU memory and reconciles the residency state:

- ``LOADED`` while VRAM < ``memory_unload_threshold_pct`` (default 20% of the
  configured memory) → the model was freed externally → mark **UNLOADED**.
- ``OFF`` while VRAM >= ``memory_load_threshold_pct`` (default 80% of the
  configured memory) → the model was loaded externally → mark **LOADED**.

The wide hysteresis band (80% / 20%) prevents flapping near a single threshold.
Only apps that support load/unload (``supports_load_unload``) are tracked — an
app without a model to load/unload has nothing to reconcile.
"""

from __future__ import annotations

import asyncio
import time

from gpusybio.applications.lifecycle import ProcessState, ResidencyState
from gpusybio.events.bus import EventBus
from gpusybio.events.log import get_logger

log = get_logger("residency-monitor")


class ResidencyMonitor:
    def __init__(
        self,
        resources,
        states,
        apps,
        bus: EventBus,
        app_ids: list[str],
        db=None,
        interval: float = 5.0,
        min_flip_sec: float = 10.0,
    ) -> None:
        self._resources = resources
        self._states = states
        self._apps = apps
        self._bus = bus
        self._app_ids = list(app_ids)
        self._db = db
        self._interval = interval
        # Debounce (a safety net on top of the hysteresis band).
        self._min_flip = min_flip_sec
        self._last_flip: dict[str, float] = {}
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
                log.exception("residency monitor tick failed")
            await asyncio.sleep(self._interval)

    # -- detection ----------------------------------------------------------
    async def tick(self) -> list[str]:
        """One monitoring pass. Returns the app_ids whose residency changed."""
        changes: list[str] = []
        now = time.time()
        for app_id in self._app_ids:
            s = self._states.get(app_id)
            if s.process is not ProcessState.RUNNING or s.pid is None:
                continue
            if s.residency in (ResidencyState.LOADING, ResidencyState.UNLOADING):
                continue  # in a transition
            try:
                cfg = self._apps.config(app_id)
            except Exception:
                continue
            if not cfg.supports_load_unload:
                continue  # no model to load/unload
            if now - self._last_flip.get(app_id, 0.0) < self._min_flip:
                continue  # debounce
            configured = cfg.configured_gpu_memory_mb
            if configured <= 0:
                continue
            try:
                mem = float(self._resources.backend.process_memory_mb(s.pid) or 0.0)
            except Exception:
                continue
            loaded_mb = configured * cfg.memory_load_threshold_pct / 100.0
            unloaded_mb = configured * cfg.memory_unload_threshold_pct / 100.0
            if s.residency is ResidencyState.LOADED and mem < unloaded_mb:
                self._states.set_residency(app_id, ResidencyState.OFF)
                self._last_flip[app_id] = now
                log.info("memory-monitored unload", app_id=app_id,
                         mem_mb=round(mem, 1), below_mb=round(unloaded_mb, 1))
                await self._emit("UNLOADED", app_id,
                                detail=f"VRAM {mem:.0f} MB < {unloaded_mb:.0f} MB")
                changes.append(app_id)
            elif s.residency is ResidencyState.OFF and mem >= loaded_mb:
                self._states.set_residency(app_id, ResidencyState.LOADED)
                self._last_flip[app_id] = now
                log.info("memory-monitored load", app_id=app_id,
                         mem_mb=round(mem, 1), at_least_mb=round(loaded_mb, 1))
                await self._emit("LOAD_READY", app_id,
                                detail=f"VRAM {mem:.0f} MB >= {loaded_mb:.0f} MB")
                changes.append(app_id)
        return changes

    # -- events -------------------------------------------------------------
    async def _emit(self, event_type: str, app_id: str = "", **data) -> None:
        data.setdefault("application_id", app_id)
        await self._bus.publish_raw(event_type, **data)
        if self._db is not None:
            try:
                from gpusybio.database.models import ApplicationEvent

                with self._db() as s:
                    s.add(ApplicationEvent(
                        application_id=app_id, event_type=event_type,
                        detail=str(data.get("detail", "")) or None))
            except Exception:  # noqa: BLE001 - persistence must never break the monitor
                pass
