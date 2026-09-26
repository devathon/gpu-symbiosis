"""Scheduling (spec 06): event-driven + 1 s reconciliation, basic selection.

Phase 1 candidate selection = gateway priority + FIFO (spec 15: the full
scoring model is Phase 2). The structure keeps the decision event shape
(spec 06 §19) and the zero-cost resident path (spec 06 §11/§17) so Phase 2
scoring slots in without redesign.
"""

from __future__ import annotations

import asyncio
import time

from gpusybio.applications.lifecycle import ProcessState, ResidencyState
from gpusybio.applications.manager import ApplicationManager
from gpusybio.database.db import SessionFactory
from gpusybio.database.models import SchedulerEvent
from gpusybio.events.bus import (
    NEW_REQUEST,
    PROCESS_DIED,
    PROCESS_NOW,
    RECONCILE,
    REQUEST_COMPLETE,
    UNLOAD_COMPLETE,
    Event,
)
from gpusybio.events.log import get_logger
from gpusybio.gateway.request_manager import RequestManager
from gpusybio.resources.manager import ResourceManager
from gpusybio.state import StateManager

log = get_logger("scheduler")

TRIGGERS = {NEW_REQUEST, REQUEST_COMPLETE, UNLOAD_COMPLETE, PROCESS_DIED, PROCESS_NOW}


class Scheduler:
    def __init__(
        self,
        states: StateManager,
        resources: ResourceManager,
        request_manager: RequestManager,
        apps: ApplicationManager,
        settings,
        db: SessionFactory,
        bus,
        app_ids: list[str],
        gateway_ids: list[str],
    ):
        self._states = states
        self._resources = resources
        self._rm = request_manager
        self._apps = apps
        self._settings = settings
        self._db = db
        self._bus = bus
        self._app_ids = app_ids
        self._gateway_ids = gateway_ids
        self._lock = asyncio.Lock()  # one scheduler transaction (Invariant 4)
        self._task: asyncio.Task | None = None
        self._reconcile_task: asyncio.Task | None = None
        self.last_decision: dict | None = None

    def start(self) -> None:
        self._bus.subscribe(TRIGGERS, self._on_trigger)
        loop = asyncio.get_event_loop()
        self._task = loop.create_task(self._trigger_loop(), name="scheduler-triggers")
        interval = max(0.2, self._settings.scheduler.interval_ms / 1000.0)
        self._reconcile_task = loop.create_task(
            self._reconcile_loop(interval), name="scheduler-reconcile"
        )

    async def stop(self) -> None:
        for t in (self._task, self._reconcile_task):
            if t:
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

    # -- trigger handling --------------------------------------------------------
    async def _on_trigger(self, event: Event) -> None:
        await self._evaluate(event.type)

    async def _trigger_loop(self) -> None:
        # Triggers are handled inline via subscription; this task exists so
        # RECONCILE and manual PROCESS_NOW events also flow through evaluate.
        while True:
            await asyncio.sleep(3600)

    async def _reconcile_loop(self, interval: float) -> None:
        """1 s reconciliation (spec 06 §4): safety net for missed events."""
        while True:
            await asyncio.sleep(interval)
            try:
                await self._evaluate(RECONCILE)
            except Exception:  # noqa: BLE001 - reconciliation must never die
                log.exception("reconciliation failed")

    # -- evaluation (one scheduler transaction) ------------------------------------
    async def _evaluate(self, trigger: str) -> dict | None:
        async with self._lock:
            candidate = self._pick_candidate()
            if candidate is None:
                return await self._decide(trigger, None, None, "no_candidate", 0.0, 0.0, 0.0)

            app_id = candidate["application_id"]
            s = self._states.get(app_id)
            cfg = self._apps.config(app_id)

            if cfg.faulted_until is not None and time.time() < cfg.faulted_until:
                return await self._decide(
                    trigger, candidate, None, "wait",
                    candidate["priority"], 0.0, 0.0, detail=f"app {app_id} faulted",
                )

            resident = s.process is ProcessState.RUNNING and s.residency is ResidencyState.LOADED

            if resident:
                # Zero-cost resident path (spec 06 §11): never drain/unload/reload.
                free = self._rm._app_limit(app_id) - self._rm.active_count(app_id)
                if free > 0:
                    forwarded = self._forward(app_id, min(free, self._rm.queued_count(app_id)))
                    return await self._decide(
                        trigger, candidate, None, "activate_concurrent",
                        candidate["priority"], 0.0, 0.0,
                        detail=f"forwarded {forwarded} request(s), zero-cost",
                    )
                return await self._decide(
                    trigger, candidate, None, "wait",
                    candidate["priority"], 0.0, 0.0, detail="app busy",
                )

            # Candidate is not resident: does it fit alongside the current
            # resident (concurrency, spec 06 §7)? If not, evicting the current
            # app is the only way to load it — a switch (spec 06 §5).
            required = self._resources.required_for(
                cfg.configured_gpu_memory_mb, cfg.safety_margin
            )
            concurrent = self._resources.fits(app_id, required)
            current = self._current_resident(exclude=app_id)
            decision = "activate_concurrent" if concurrent else "switch"

            if current is not None and not concurrent:
                # Eviction required — only allowed and possible when idle.
                if current.pinned:
                    return await self._decide(
                        trigger, candidate, None, "wait",
                        candidate["priority"], required, 0.0,
                        detail=f"current app {current.id} is pinned",
                    )
                if not current.allow_automatic_unload:
                    return await self._decide(
                        trigger, candidate, None, "wait",
                        candidate["priority"], required, 0.0,
                        detail=f"current app {current.id} disallows automatic unload",
                    )
                if self._rm.active_count(current.id) > 0:
                    return await self._decide(
                        trigger, candidate, None, "wait",
                        candidate["priority"], required, 0.0,
                        detail=f"current app {current.id} busy; cannot evict",
                    )
                await self._drain(current.id)
                await self._apps.unload(current.id)
                await self._emit("SWITCH_OUT", application_id=current.id, to=app_id)

            if s.process in (ProcessState.STOPPED, ProcessState.ERROR):
                await self._apps.start(app_id)
                s = self._states.get(app_id)
                if s.process is not ProcessState.RUNNING:
                    return await self._decide(
                        trigger, candidate, None, "wait",
                        candidate["priority"], required, 0.0, detail="start failed",
                    )
            elif s.residency is ResidencyState.OFF:
                await self._apps.load(app_id)
                s = self._states.get(app_id)
                if s.residency is not ResidencyState.LOADED:
                    return await self._decide(
                        trigger, candidate, None, "wait",
                        candidate["priority"], required, 0.0, detail="load failed",
                    )

            forwarded = self._forward(app_id, self._rm._app_limit(app_id))
            await self._emit("SWITCH_IN", application_id=app_id, trigger=trigger)
            return await self._decide(
                trigger, candidate, None, decision,
                candidate["priority"], required, candidate["priority"],
                detail=f"switched to {app_id}, forwarded {forwarded} request(s)",
            )

    # -- candidate selection (spec 15 Phase 1: priority + FIFO) ---------------------
    def _pick_candidate(self) -> dict | None:
        best: dict | None = None
        for gw_id in self._gateway_ids:
            gw = self._rm.gateway(gw_id)
            if not gw.get("enabled", True):
                continue
            slots = [
                s
                for s in self._rm.queue().all_slots()
                if s.gateway_id == gw_id
            ]
            if not slots:
                continue
            head = min(slots, key=lambda s: (-s.priority, s.queued_at))
            if best is None or (-head.priority, head.queued_at) < (-best["priority"], best["queued_at"]):
                best = {
                    "gateway_id": gw_id,
                    "application_id": gw["application_id"],
                    "priority": head.priority,
                    "request_id": head.request_id,
                    "queued_at": head.queued_at,
                }
        return best

    def _current_resident(self, exclude: str | None = None):
        for app_id in self._app_ids:
            if app_id == exclude:
                continue
            s = self._states.get(app_id)
            if s.process is ProcessState.RUNNING and s.residency is ResidencyState.LOADED:
                return self._apps.config(app_id)
        return None

    def _forward(self, app_id: str, limit: int) -> int:
        """Signal the next queued requests for this app (up to `limit`)."""
        n = 0
        for _ in range(limit):
            slots = [s for s in self._rm.queue().all_slots() if s.application_id == app_id]
            if not slots:
                break
            head = min(slots, key=lambda s: (-s.priority, s.queued_at))
            slot = self._rm.queue().remove(head.request_id)
            if slot is None:
                break
            self._rm.signal_forward(slot.request_id)
            n += 1
        return n

    async def _drain(self, app_id: str) -> None:
        """Drain: no new requests, wait for active to reach 0 (spec 06 §2)."""
        self._states.set_draining(app_id, True)
        await self._emit("DRAIN_START", application_id=app_id)
        deadline = time.monotonic() + 60.0
        while self._states.get(app_id).active_requests > 0:
            if time.monotonic() > deadline:
                log.warning("drain timeout", application_id=app_id)
                break
            await asyncio.sleep(0.05)
        self._states.set_draining(app_id, False)

    # -- decision events (spec 06 §19) --------------------------------------------
    async def _decide(
        self,
        trigger: str,
        candidate: dict | None,
        current: dict | None,
        decision: str,
        candidate_score: float,
        switching_penalty: float,
        current_score: float,
        detail: str = "",
    ) -> dict:
        effective = candidate_score - switching_penalty
        rec = {
            "trigger": trigger,
            "candidate_gateway_id": candidate["gateway_id"] if candidate else None,
            "candidate_application_id": candidate["application_id"] if candidate else None,
            "candidate_score": candidate_score,
            "switching_penalty": switching_penalty,
            "effective_score": effective,
            "current_score": current_score,
            "decision": decision,
            "detail": detail,
            "at": time.time(),
        }
        self.last_decision = rec
        await self._bus.publish_raw("scheduler_decision", **rec)
        try:
            with self._db() as s:
                s.add(SchedulerEvent(**{k: v for k, v in rec.items() if k != "at"}))
        except Exception:  # noqa: BLE001
            log.exception("decision persist failed")
        log.info(
            "scheduler decision",
            trigger=trigger,
            decision=decision,
            candidate=candidate["application_id"] if candidate else None,
            detail=detail,
        )
        return rec

    async def _emit(self, event_type: str, **data) -> None:
        await self._bus.publish_raw(event_type, **data)
