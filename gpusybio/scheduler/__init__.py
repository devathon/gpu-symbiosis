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
"""

from __future__ import annotations

import asyncio
import time
import types
from dataclasses import dataclass

from gpusybio.applications.lifecycle import ProcessState, ResidencyState
from gpusybio.applications.manager import AppConfig, ApplicationManager
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
from gpusybio.scoring import EPS, gateway_score, is_emergency, normalized_demand, switch_cost
from gpusybio.state import StateManager

log = get_logger("scheduler")

TRIGGERS = {NEW_REQUEST, REQUEST_COMPLETE, UNLOAD_COMPLETE, PROCESS_DIED, PROCESS_NOW}

# Columns of the scheduler_events table (the rest of the decision payload is
# surfaced via SSE / current-decision, not persisted).
_DB_COLS = {
    "trigger",
    "candidate_gateway_id",
    "candidate_application_id",
    "candidate_score",
    "switching_penalty",
    "effective_score",
    "current_score",
    "decision",
    "detail",
}


@dataclass
class SwitchTracking:
    """Anti-thrash bookkeeping (spec 06 §16 'Additionally tracked')."""

    switch_count: int = 0
    last_switch_time: float | None = None
    last_pair: tuple[str, str] | None = None  # (evicted, loaded)
    previous_application: str | None = None


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
            if self._states.get(a).process is ProcessState.RUNNING
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

    # -- scoring (spec 06 §6) -----------------------------------------------------
    def _boost_multiplier(self, gateway_id: str) -> float:
        exp = self._boosts.get(gateway_id)
        if exp is not None and time.time() < exp:
            return float(self._settings.scheduler.process_now_multiplier)
        return 1.0

    def _gateway_score(self, gateway_id: str) -> dict | None:
        """Full spec 06 §6 score for one gateway; None when the queue is empty."""
        gw = self._rm.gateway(gateway_id)
        q = self._rm.queue()
        if not gw.get("enabled", True) or q.length(gateway_id) == 0:
            return None
        d = self._rm.demand()
        return gateway_score(
            int(gw["priority"]) * self._boost_multiplier(gateway_id),
            q.length(gateway_id),
            gw["max_waiting_queue"],
            q.oldest_wait_sec(gateway_id),
            gw["max_waiting_time_sec"],
            d.arrival_rate(gateway_id),
            d.service_rate(gw["application_id"]),
        )

    def _app_score(self, app_id: str) -> dict:
        """application_demand (spec 05 §9): the app's highest-scoring gateway;
        0 when idle (no queued requests)."""
        best: dict | None = None
        for gid in self._gateway_ids:
            if self._rm.gateway(gid)["application_id"] != app_id:
                continue
            sc = self._gateway_score(gid)
            if sc is not None and (best is None or sc["score"] > best["score"]):
                best = sc
        return best or {"score": 0.0}

    def _pick_candidate(self) -> tuple[dict, dict] | None:
        """Candidate selection (spec 05 §9): argmax of gateway scores."""
        best_gw: str | None = None
        best_sc: dict | None = None
        for gid in self._gateway_ids:
            sc = self._gateway_score(gid)
            if sc is not None and (best_sc is None or sc["score"] > best_sc["score"]):
                best_gw, best_sc = gid, sc
        if best_gw is None:
            return None
        gw = self._rm.gateway(best_gw)
        q = self._rm.queue()
        head = min(
            (s for s in q.all_slots() if s.gateway_id == best_gw),
            key=lambda s: (-s.priority, s.queued_at),
        )
        cand = {
            "gateway_id": best_gw,
            "application_id": gw["application_id"],
            "queue_len": q.length(best_gw),
            "oldest_wait": q.oldest_wait_sec(best_gw),
            "request_id": head.request_id,
            "queued_at": head.queued_at,
            "priority": int(gw["priority"]) * self._boost_multiplier(best_gw),
        }
        return cand, best_sc

    # -- evaluation (one scheduler transaction) ------------------------------------
    async def _evaluate(self, ev: Event) -> dict | None:
        async with self._lock:
            trigger = ev.type

            # Boost expiry FIRST (spec 06 §9: TTL elapsed or the gateway's queue
            # drained) so a boost set by this very PROCESS_NOW trigger is not
            # immediately expired in the same evaluation.
            now = time.time()
            if not self._dry_run:
                for gid in list(self._boosts):
                    if now >= self._boosts[gid] or self._rm.queue().length(gid) == 0:
                        self._boosts.pop(gid, None)

                # Process Now (spec 06 §9): time-decaying priority boost.
                if trigger == PROCESS_NOW:
                    gid = ev.data.get("gateway_id")
                    if gid in self._rm.gateway_configs():
                        self._boosts[gid] = (
                            time.time()
                            + float(self._settings.scheduler.process_now_boost_ttl_sec)
                        )

            # Resident-first (spec 06 §7 zero-cost path): a resident app with a
            # free slot and its own queued requests serves them at zero cost
            # BEFORE the scheduler considers evicting it for a competing
            # candidate. Without this, an idle resident's queued work can starve
            # behind a competitor whose score it cannot beat (its own completed
            # work raises its service rate and lowers its demand pressure).
            # Draining/unloading apps take no new work (spec 06 §2).
            for res in self.residents():
                vs_res = self._states.get(res)
                if (
                    vs_res.draining
                    or vs_res.residency is ResidencyState.UNLOADING
                    or self._rm.queued_count(res) <= 0
                    or self._rm.active_count(res) >= self._rm._app_limit(res)
                ):
                    continue
                free = self._rm._app_limit(res) - self._rm.active_count(res)
                fwd = self._forward(res, free)
                return await self._decide(
                    trigger, None, None, "activate_concurrent", 0.0, 0.0, 0.0,
                    detail=f"forwarded {fwd} queued request(s) for resident {res} (zero-cost)",
                    active=res,
                )

            picked = self._pick_candidate()
            if picked is None:
                self._clear_draining()
                return await self._decide(trigger, None, None, "no_candidate", 0.0, 0.0, 0.0)

            cand, csc = picked
            app_id = cand["application_id"]
            cfg = self._apps.config(app_id)
            s = self._states.get(app_id)
            now = time.time()

            if cfg.faulted_until is not None and now < cfg.faulted_until:
                return await self._decide(
                    trigger, cand, None, "wait", csc["score"], 0.0, 0.0,
                    detail=f"application {app_id} is faulted", scores=csc,
                )

            # -- zero-cost path (spec 06 §7/§11): candidate already resident -------
            resident = (
                s.process is ProcessState.RUNNING and s.residency is ResidencyState.LOADED
            )
            if resident:
                limit = self._rm._app_limit(app_id)
                free = limit - self._rm.active_count(app_id)
                if free > 0:
                    fwd = self._forward(app_id, min(free, self._rm.queued_count(app_id)))
                    return await self._decide(
                        trigger, cand, None, "activate_concurrent", csc["score"], 0.0, 0.0,
                        detail=f"forwarded {fwd} request(s) (zero-cost, resident)",
                        scores=csc, active=app_id,
                    )
                return await self._decide(
                    trigger, cand, None, "wait", csc["score"], 0.0, 0.0,
                    detail=f"application {app_id} busy ({limit - free}/{limit} slots)",
                    scores=csc, active=app_id,
                )

            # -- concurrent load (spec 06 §14: fits alongside residents) -----------
            required = self._resources.required_for(cfg.configured_gpu_memory_mb, cfg.safety_margin)
            if self._resources.fits(app_id, required):
                st = await self._ensure_running_loaded(app_id)
                ok = (
                    st.process is ProcessState.RUNNING
                    and st.residency is ResidencyState.LOADED
                )
                if ok:
                    fwd = self._forward(app_id, self._rm._app_limit(app_id))
                    return await self._decide(
                        trigger, cand, None, "activate_concurrent", csc["score"], 0.0, 0.0,
                        detail=f"loaded {app_id} concurrently alongside {self.residents()}",
                        scores=csc, active=app_id,
                    )
                return await self._decide(
                    trigger, cand, None, "wait", csc["score"], 0.0, 0.0,
                    detail=f"load of {app_id} not ready yet", scores=csc,
                )

            # -- eviction path (candidate does not fit alongside residents) --------
            victim, reason = self._select_victim(exclude=app_id)
            if victim is None:
                return await self._decide(
                    trigger, cand, None, "wait", csc["score"], 0.0, 0.0,
                    detail=reason, scores=csc,
                )

            vs = self._states.get(victim.id)
            sched = self._settings.scheduler
            emergency = is_emergency(
                cand["oldest_wait"],
                cand["queue_len"],
                sched.emergency_wait_sec,
                sched.emergency_queue_threshold,
            )
            # Guard bookkeeping (for the explainability payload).
            mr_left = 0.0
            if vs.loaded_at is not None:
                mr_left = max(0.0, sched.min_residency_sec - (now - vs.loaded_at))
            cd_left = self._cooldown_left(app_id, victim.id)
            cur_sc = self._app_score(victim.id)["score"]
            guards = {
                "min_residency_left": round(mr_left, 2),
                "cooldown_left": round(cd_left, 2),
                "emergency": emergency,
            }

            # spec 06 §14 order: residency guard → draining → drain → margin compare.
            if not emergency:
                if mr_left > 0:
                    return await self._decide(
                        trigger, cand, None, "keep", csc["score"], 0.0, cur_sc,
                        detail=f"min residency ({mr_left:.1f}s left)",
                        scores=csc, active=victim.id, guards=guards,
                    )
                if cd_left > 0:
                    return await self._decide(
                        trigger, cand, None, "keep", csc["score"], 0.0, cur_sc,
                        detail=f"switch cooldown ({cd_left:.1f}s left)",
                        scores=csc, active=victim.id, guards=guards,
                    )

            if vs.draining or vs.residency is ResidencyState.UNLOADING:
                if vs.active_requests > 0:
                    return await self._decide(
                        trigger, cand, None, "drain", csc["score"], 0.0, cur_sc,
                        detail=f"{victim.id} draining ({vs.active_requests} active)",
                        scores=csc, active=victim.id, guards=guards,
                    )
                # Draining finished (active = 0): complete the switch below.
            elif vs.active_requests > 0:
                # spec 06 §2/§14: mark DRAINING; new requests queue, existing continue.
                # The switch completes on request completion (spec 06 §17).
                if not self._dry_run:
                    self._states.set_draining(victim.id, True)
                    await self._emit("DRAIN_START", application_id=victim.id)
                return await self._decide(
                    trigger, cand, None, "drain", csc["score"], 0.0, cur_sc,
                    detail=f"{victim.id} draining ({vs.active_requests} active)",
                    scores=csc, active=victim.id, guards=guards,
                )

            # -- victim idle: final margin comparison (spec 06 §13/§14) -------------
            cost = self._switch_cost(victim, app_id)
            penalty = cost * float(sched.switch_cost_weight)
            effective = csc["score"] - penalty
            margin = float(sched.switch_margin)
            if effective > cur_sc + margin:
                ok = await self._do_switch(victim, app_id, trigger)
                if not ok:
                    return await self._decide(
                        trigger, cand, None, "wait", csc["score"], penalty, cur_sc,
                        detail=f"switch to {app_id} not ready yet (retried on next event)",
                        scores=csc, active=victim.id, guards=guards,
                    )
                if not self._dry_run:
                    t = self._tracking
                    t.switch_count += 1
                    t.last_switch_time = time.time()
                    t.last_pair = (victim.id, app_id)
                    t.previous_application = victim.id
                return await self._decide(
                    trigger, cand, None, "switch", csc["score"], penalty, cur_sc,
                    detail=f"switched {victim.id} → {app_id} (cost {cost:.2f}s)",
                    scores=csc, active=app_id, guards=guards,
                )
            return await self._decide(
                trigger, cand, None, "keep", csc["score"], penalty, cur_sc,
                detail=f"effective {effective:.1f} <= current {cur_sc:.1f} + margin {margin:.1f}",
                scores=csc, active=victim.id, guards=guards,
            )

    # -- helpers -----------------------------------------------------------------
    def _cooldown_left(self, candidate: str, victim: str) -> float:
        """spec 06 §16: minimum time between two switches of the same pair."""
        t = self._tracking
        cd = float(self._settings.scheduler.switch_cooldown_sec)
        if cd <= 0 or t.last_pair is None or t.last_switch_time is None:
            return 0.0
        if set(t.last_pair) != {candidate, victim}:
            return 0.0
        elapsed = time.time() - t.last_switch_time
        return max(0.0, cd - elapsed)

    def _switch_cost(self, victim: AppConfig, app_id: str) -> float:
        """spec 06 §11: measured timings of the removal + the candidate."""
        s = self._states.get(app_id)
        needs_startup = s.process in (ProcessState.STOPPED, ProcessState.ERROR)
        needs_load = s.residency is ResidencyState.OFF
        cfg = self._apps.config(app_id)
        return switch_cost(
            [victim.unload_time_sec or 0.0],
            cfg.startup_time_sec if needs_startup else None,
            cfg.load_time_sec if needs_load else None,
            needs_startup,
            needs_load,
        )

    def _select_victim(self, exclude: str) -> tuple[AppConfig | None, str]:
        """Unload candidate selection (spec 06 §3).

        Hard exclusions (spec 06 §3.1): pinned and allow_automatic_unload=false
        are never unloaded on the automatic path. A busy resident (active > 0)
        is NOT excluded here — it is drained first (spec 06 §2/§14: mark
        DRAINING, switch on completion).
        """
        eligible: list[AppConfig] = []
        reasons = []
        for app_id in self._app_ids:
            if app_id == exclude:
                continue
            s = self._states.get(app_id)
            if not (
                s.process is ProcessState.RUNNING and s.residency is ResidencyState.LOADED
            ):
                continue
            cfg = self._apps.config(app_id)
            if cfg.pinned:
                reasons.append(f"{app_id} is pinned")
            elif not cfg.allow_automatic_unload:
                reasons.append(f"{app_id} disallows automatic unload")
            else:
                eligible.append(cfg)
        if not eligible:
            return None, "no unload candidate (" + "; ".join(reasons) + ")"
        return max(eligible, key=self._unload_score), "ok"

    def _unload_score(self, cfg: AppConfig) -> float:
        """spec 06 §3: highest unload score = lowest retention value."""
        d = self._rm.demand()
        gws = [g for g in self._gateway_ids if self._rm.gateway(g)["application_id"] == cfg.id]
        nds = [
            normalized_demand(d.arrival_rate(g) / max(d.service_rate(cfg.id), EPS)) for g in gws
        ]
        low_demand = 1.0 - (sum(nds) / len(nds) if nds else 0.0)
        prio = max((int(self._rm.gateway(g)["priority"]) for g in gws), default=1)
        low_priority = 1.0 - min(prio / 20.0, 1.0)
        s = self._states.get(cfg.id)
        age = (time.time() - s.loaded_at) if s.loaded_at else 0.0
        residency = 1.0 - min(age / 600.0, 1.0)  # shorter residency → lower retention
        unload_cost = min((cfg.unload_time_sec or 0.0) / 10.0, 1.0)
        return 1.0 + low_demand + low_priority + residency - unload_cost

    def _clear_draining(self) -> None:
        """A drain only exists to serve a candidate; no candidate → release it."""
        if self._dry_run:
            return
        for app_id in self._app_ids:
            s = self._states.get(app_id)
            if s.draining and self._rm.queued_count(app_id) == 0:
                self._states.set_draining(app_id, False)

    def _forward(self, app_id: str, limit: int) -> int:
        """Signal the next queued requests for this app (up to `limit`).

        Signal-only: the slot stays in the queue; the proxy's `begin_forward`
        removes it when it can actually forward and re-queues it if the app is
        not ready (so the next trigger re-evaluates — no slot is ever lost,
        Invariant 5). The scheduler never removes a slot or marks it in-flight
        itself: `begin_forward` is the single slot-lifecycle authority.
        """
        slots = sorted(
            (s for s in self._rm.queue().all_slots() if s.application_id == app_id),
            key=lambda s: (-s.priority, s.queued_at),
        )
        count = min(len(slots), limit)
        if self._dry_run:
            return count  # what-if: report how many would be forwarded, don't signal
        for slot in slots[:limit]:
            self._rm.signal_forward(slot.request_id)
        return count

    async def _ensure_running_loaded(self, app_id: str):
        s = self._states.get(app_id)
        if self._dry_run:
            # what-if: the caller already verified `fits`; report a would-be
            # RUNNING/LOADED state without touching the real app (spec 15).
            return types.SimpleNamespace(
                process=ProcessState.RUNNING, residency=ResidencyState.LOADED
            )
        if s.process in (ProcessState.STOPPED, ProcessState.ERROR):
            await self._apps.start(app_id)
            s = self._states.get(app_id)
        elif s.residency is ResidencyState.OFF:
            await self._apps.load(app_id)
            s = self._states.get(app_id)
        return s

    async def _do_switch(self, victim: AppConfig, app_id: str, trigger: str) -> bool:
        """spec 06 §17 graceful switch: unload (drained) → load → ready → drain queue."""
        if self._dry_run:
            return True  # what-if: the switch would succeed, but don't act (spec 15)
        await self._apps.unload(victim.id)
        # Evicted → no longer a drain target (spec 06 §2). Without this the
        # victim's stale `draining` flag survives re-load and blocks
        # can_forward_now, starving its own queued requests.
        self._states.set_draining(victim.id, False)
        st = await self._ensure_running_loaded(app_id)
        # (Re)loaded → no longer a drain target either.
        self._states.set_draining(app_id, False)
        ok = (
            st.process is ProcessState.RUNNING and st.residency is ResidencyState.LOADED
        )
        if ok:
            self._forward(app_id, self._rm._app_limit(app_id))
            await self._emit("SWITCH_IN", application_id=app_id, trigger=trigger)
        return ok

    def _vram_accounting(self) -> dict:
        """spec 11 §5: total/reserve/resident/reserved/external/free."""
        s = self._resources.last_sample or self._resources.sample()
        total = s.total_mb
        reserve = float(self._settings.gpu.memory_reserve_mb)
        usable = max(0.0, total - reserve)
        committed = sum(
            self._resources.observed_mb(a) for a in self._app_ids
        )
        external = max(0.0, s.used_mb - committed)
        free = max(0.0, usable - committed - external)
        return {
            "total_mb": round(total, 1),
            "reserve_mb": round(reserve, 1),
            "usable_mb": round(usable, 1),
            "resident_mb": round(committed, 1),
            "reserved_mb": round(
                sum(r.required_mb for r in self._resources.reservations().values()), 1
            ),
            "external_mb": round(external, 1),
            "free_mb": round(free, 1),
        }

    # -- decision (spec 06 §19) ----------------------------------------------------
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
        scores: dict | None = None,
        active: str | None = None,
        guards: dict | None = None,
    ) -> dict:
        effective = candidate_score - switching_penalty
        rec: dict = {
            "trigger": trigger,
            "candidate_gateway_id": candidate["gateway_id"] if candidate else None,
            "candidate_application_id": candidate["application_id"] if candidate else None,
            "candidate_score": round(candidate_score, 3),
            "switching_penalty": round(switching_penalty, 3),
            "effective_score": round(effective, 3),
            "current_score": round(current_score, 3),
            "decision": decision,
            "detail": detail,
            "at": time.time(),
        }
        if scores:
            rec["scores"] = scores
        if active:
            rec["active_application_id"] = active
        if guards is not None:
            rec["guards"] = guards
        rec["residents"] = self.residents()
        rec["vram"] = self._vram_accounting()
        rec["switch_tracking"] = self.switch_tracking()
        rec["boosts"] = self.boosts()
        if self._dry_run:
            # what-if: no bookkeeping, no event, no persistence (spec 15).
            return rec
        self.last_decision = rec
        await self._bus.publish_raw("scheduler_decision", **rec)
        try:
            with self._db() as s:
                s.add(SchedulerEvent(**{k: v for k, v in rec.items() if k in _DB_COLS}))
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
