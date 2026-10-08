"""Scheduling (spec 06): event-driven + 1 s reconciliation.

Phase 2 (spec 15) — the full gateway scoring model:
- gateway score = priority_pressure + waiting_pressure + recent_demand_pressure
  (spec 06 §6), with EWMA arrival rate and service rate (spec 06 §6.3/§6.4);
- switching guards: minimum residency (§12), switch margin (§13), emergency
  override (§15), anti-thrash switch cooldown + tracking (§16);
- unload candidate selection (spec 06 §3, incl. hard exclusions §3.1);
- drain-then-switch: a busy current app is marked DRAINING (spec 06 §2) and the
  switch completes on request completion (spec 06 §17);
- Process Now = time-decaying priority boost, never kills active work (spec 06 §9);
- zero-cost resident path (spec 06 §7/§11): no drain, no unload, no reload.

Every decision emits the explainability payload (spec 06 §19) surfaced by the
Scheduler Explainer UI (spec 11 §5).

The ``Scheduler`` class is composed of two mixins: :class:`ScoringMixin`
(:mod:`~gpusybio.scheduler.scoring`) holds the pure scoring + victim-selection
readers, and :class:`EvaluateMixin`
(:mod:`~gpusybio.scheduler.evaluate`) holds the one-scheduler-transaction
evaluation / switching / decision core. ``__init__`` keeps the lifecycle, the
public accessors, and the trigger loops.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from gpusybio.applications.lifecycle import ProcessState, ResidencyState
from gpusybio.applications.manager import AppConfig, ApplicationManager
from gpusybio.database.db import SessionFactory
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

from gpusybio.scheduler.scoring import ScoringMixin
from gpusybio.scheduler.evaluate import EvaluateMixin

log = get_logger("scheduler")

TRIGGERS = {NEW_REQUEST, REQUEST_COMPLETE, UNLOAD_COMPLETE, PROCESS_DIED, PROCESS_NOW}


@dataclass
class SwitchTracking:
    """Anti-thrash bookkeeping (spec 06 §16 'Additionally tracked')."""

    switch_count: int = 0
    last_switch_time: float | None = None
    last_pair: tuple[str, str] | None = None  # (evicted, loaded)
    previous_application: str | None = None


class Scheduler(ScoringMixin, EvaluateMixin):
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
        self._tracking = SwitchTracking()
        # Process Now boosts: gateway_id → expiry timestamp (spec 06 §9).
        self._boosts: dict[str, float] = {}
        # What-if mode (spec 15 §Phase 5): evaluate the scoring model without
        # acting (no forwards/loads/switches/boosts, no decision bookkeeping).
        self._dry_run: bool = False

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

    # -- what-if simulation (spec 15 §Phase 5) ----------------------------------
    async def simulate(self, trigger: str = RECONCILE) -> dict:
        """Evaluate the scoring model against current state WITHOUT acting.

        Runs the same decision logic as a live trigger, but with the dry-run
        flag set: no forwards, loads, switches, boosts or decision bookkeeping
        are applied (spec 15: "what-if mode ... without acting"). Returns the
        decision that would have been made, tagged ``simulated``.

        Note: ``_evaluate`` acquires the scheduler lock itself (Invariant 4);
        ``asyncio.Lock`` is non-reentrant, so this must NOT hold it here.
        """
        self._dry_run = True
        try:
            rec = await self._evaluate(Event(type=trigger))
        finally:
            self._dry_run = False
        if rec is None:
            rec = {}
        rec["simulated"] = True
        return rec

    # -- accessors (API / UI) ---------------------------------------------------
    def settings(self):
        return self._settings

    def residents(self) -> list[str]:
        return [
            a
            for a in self._app_ids
            if self._states.get(a).process is ProcessState.STARTED
            and self._states.get(a).residency is ResidencyState.LOADED
        ]

    def switch_tracking(self) -> dict:
        t = self._tracking
        return {
            "switch_count": t.switch_count,
            "last_switch_at": t.last_switch_time,
            "last_pair": list(t.last_pair) if t.last_pair else None,
            "previous_application": t.previous_application,
        }

    def boosts(self) -> dict:
        now = time.time()
        return {
            g: round(max(0.0, exp - now), 1) for g, exp in self._boosts.items() if exp > now
        }

    # -- trigger handling --------------------------------------------------------
    async def _on_trigger(self, event: Event) -> None:
        await self._evaluate(event)

    async def _trigger_loop(self) -> None:
        # Triggers are handled inline via subscription; this task exists so the
        # scheduler lifecycle has a stable task handle.
        while True:
            await asyncio.sleep(3600)

    async def _reconcile_loop(self, interval: float) -> None:
        """1 s reconciliation (spec 06 §4): safety net for missed events."""
        while True:
            await asyncio.sleep(interval)
            try:
                await self._evaluate(Event(type=RECONCILE))
            except Exception:  # noqa: BLE001 - reconciliation must never die
                log.exception("reconciliation failed")


__all__ = ["Scheduler", "SwitchTracking"]
