"""Evaluation / switching / decision methods for the Scheduler — the one
scheduler-transaction core (spec 06 §2/§4/§7/§11/§14/§17/§19).

Mixed into :class:`~gpusybio.scheduler.Scheduler`. ``_evaluate`` acquires the
scheduler lock itself (Invariant 4); the helpers it calls run under it.
"""

from __future__ import annotations

import time
import types

from gpusybio.applications.lifecycle import ProcessState, ResidencyState
from gpusybio.applications.manager import AppConfig
from gpusybio.database.models import SchedulerEvent
from gpusybio.events.bus import PROCESS_NOW
from gpusybio.events.log import get_logger
from gpusybio.scoring import is_emergency, switching_penalty

log = get_logger("scheduler")

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


class EvaluateMixin:
    # -- evaluation (one scheduler transaction) ------------------------------------
    async def _evaluate(self, ev) -> dict | None:
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

            # Drain completion (spec 06 §2: once active_requests == 0 the
            # drain completes). New requests for a draining app only ever
            # queue, so the drain is never cancelled or delayed by them: a
            # draining app that just went idle switches out now. If the
            # competing candidate's queue has emptied (requests cancelled),
            # the drain is released and the app serves its own queued work at
            # zero cost below (a drain only exists to serve a candidate).
            for res in self.residents():
                vs_res = self._states.get(res)
                if vs_res.residency is not ResidencyState.LOADED or not vs_res.draining:
                    continue
                if self._rm.active_count(res) > 0:
                    continue  # still draining (active request)
                # Never evict a running app: a draining victim is only switched
                # out once it is truly idle — no active request AND its GPU%
                # *average* below the activity threshold. A momentary 0% dip
                # (or a burst between requests) does not release it.
                if self._is_active(res):
                    continue  # still running (using the GPU) — wait for it to go idle
                other = self._pick_candidate(exclude=self._drain_exclude_apps(res))
                if other is None:
                    if not self._dry_run:
                        self._states.set_draining(res, False)
                    continue  # res's own queued work is served below (zero-cost)
                cand, csc = other
                victim = self._apps.config(res)
                ok = await self._do_switch(victim, cand["application_id"], trigger)
                if ok and not self._dry_run:
                    self._record_switch(res, cand["application_id"])
                return await self._decide(
                    trigger, cand, None, "switch" if ok else "wait",
                    csc["score"], 0.0, 0.0,
                    detail=(
                        f"drain of {res} complete → switched to {cand['application_id']}"
                        if ok
                        else f"drain of {res} complete but switch to {cand['application_id']} not ready yet"
                    ),
                    scores=csc, active=cand["application_id"] if ok else None,
                )

            # Resident-first (spec 06 §7 zero-cost path): a resident app with a
            # free slot and its own queued requests serves them at zero cost
            # BEFORE the scheduler considers evicting it for a competing
            # candidate. Without this, an idle resident's queued work can starve
            # behind a competitor whose score it cannot beat (its own completed
            # work raises its service rate and lowers its demand pressure).
            # An UNLOADING app takes no new work here (the candidate path can
            # switch back and re-load it). A still-loaded DRAINING app takes no
            # new work here either (spec 06 §2: new requests during drain
            # QUEUE, never cancel or delay the drain — otherwise a stream of
            # new requests would hold the drain open indefinitely and starve
            # the candidate it exists to serve). Its queued requests wait and
            # are served when the app is resident again.
            for res in self.residents():
                vs_res = self._states.get(res)
                if vs_res.residency is ResidencyState.UNLOADING:
                    continue
                if vs_res.draining:
                    continue
                if self._rm.queued_count(res) <= 0:
                    continue
                if self._rm.active_count(res) >= self._rm.app_limit(res):
                    continue
                free = self._rm.app_limit(res) - self._rm.active_count(res)
                fwd = self._forward(res, free)
                if fwd == 0:
                    # Only a paused gateway's requests are queued (spec 11) —
                    # nothing signalable; wait for the unpause (and let the
                    # candidate path consider other gateways this tick).
                    continue
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
                s.process is ProcessState.STARTED and s.residency is ResidencyState.LOADED
            )
            if resident:
                if s.draining:
                    # A draining app takes no new work (spec 06 §2): its queued
                    # requests wait. It is idle only in the instant the
                    # drain-completion check above just switched it out, so
                    # here it still has active work — wait for the drain.
                    return await self._decide(
                        trigger, cand, None, "drain", csc["score"], 0.0, 0.0,
                        detail=f"{app_id} draining ({self._rm.active_count(app_id)} active)",
                        scores=csc, active=app_id,
                    )
                limit = self._rm.app_limit(app_id)
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
                    st.process is ProcessState.STARTED
                    and st.residency is ResidencyState.LOADED
                )
                if ok:
                    fwd = self._forward(app_id, self._rm.app_limit(app_id))
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

            # Drain guard (spec 06 §2/§14): a draining victim waits for its
            # active work to finish; new requests only queue and never delay
            # the drain (the drain-completion check above completes the switch
            # once active reaches 0). A still-busy, not-yet-draining victim is
            # marked DRAINING here (spec 06 §2).
            if vs.draining or vs.residency is ResidencyState.UNLOADING:
                return await self._decide(
                    trigger, cand, None, "drain", csc["score"], 0.0, cur_sc,
                    detail=f"{victim.id} draining ({vs.active_requests} active)",
                    scores=csc, active=victim.id, guards=guards,
                )
            if self._is_active(victim.id):
                if not self._dry_run:
                    self._states.set_draining(victim.id, True)
                    await self._emit("DRAIN_START", application_id=victim.id)
                return await self._decide(
                    trigger, cand, None, "drain", csc["score"], 0.0, cur_sc,
                    detail=f"{victim.id} active ({vs.active_requests} requests)",
                    scores=csc, active=victim.id, guards=guards,
                )

            # -- victim idle: final margin comparison (spec 06 §13/§14) -------------
            cost = self._switch_cost(victim, app_id)
            # The cost is in seconds; the score is a normalized pressure whose
            # waiting term is oldest_wait / max_wait (§6.9). Normalize the cost
            # by the candidate gateway's max_waiting_time_sec — the same time
            # horizon — so the penalty lands on the score's scale instead of
            # raw seconds, which would dwarf it by ~max_wait× (spec 06 §11).
            time_scale = float(
                self._rm.gateway(cand["gateway_id"])["max_waiting_time_sec"]
            )
            penalty = switching_penalty(
                cost, time_scale, float(sched.switch_cost_weight)
            )
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
                    self._record_switch(victim.id, app_id)
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
    def _drain_exclude_apps(self, res: str) -> set[str]:
        """Apps whose gateways the drain-completion check must not pick:
        ``res``'s own gateways (its queued requests wait — spec 06 §2) and
        any other draining app (one drain active at a time in practice)."""
        excl = {res}
        for app_id in self._app_ids:
            if self._states.get(app_id).draining:
                excl.add(app_id)
        return excl

    def _clear_draining(self) -> None:
        """A drain only exists to serve a candidate; no candidate → release it."""
        if self._dry_run:
            return
        for app_id in self._app_ids:
            s = self._states.get(app_id)
            if s.draining and self._rm.queued_count(app_id) == 0:
                self._states.set_draining(app_id, False)

    def _record_switch(self, victim_id: str, app_id: str) -> None:
        """Anti-thrash bookkeeping (spec 06 §16 'Additionally tracked')."""
        t = self._tracking
        t.switch_count += 1
        t.last_switch_time = time.time()
        t.last_pair = (victim_id, app_id)
        t.previous_application = victim_id

    def _forward(self, app_id: str, limit: int) -> int:
        """Signal the next queued requests for this app (up to `limit`).

        Signal-only: the slot stays in the queue; the proxy's `begin_forward`
        removes it when it can actually forward and re-queues it if the app is
        not ready (so the next trigger re-evaluates — no slot is ever lost,
        Invariant 5). The scheduler never removes a slot or marks it in-flight
        itself: `begin_forward` is the single slot-lifecycle authority.

        spec 11: a paused gateway's queued slots are never signaled — the
        proxy's `begin_forward` would refuse the pause and the caller would
        cancel the signaled request. They wait for the unpause.
        """

        def _forwardable(slot) -> bool:
            gw = self._rm.gateway(slot.gateway_id)
            return gw is not None and not gw.get("paused", False)

        slots = sorted(
            (
                s
                for s in self._rm.queue().all_slots()
                if s.application_id == app_id and _forwardable(s)
            ),
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
            # STARTED/LOADED state without touching the real app (spec 15).
            return types.SimpleNamespace(
                process=ProcessState.STARTED, residency=ResidencyState.LOADED
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
        await self._release_victim(victim.id)
        if not await self._verify_victim_vram_freed(victim.id, app_id, trigger):
            return False
        # Victim VRAM freed — reset its failure counter before loading candidate.
        self._states.get(victim.id).drain_failures = 0
        return await self._load_and_forward_candidate(app_id, trigger)

    async def _release_victim(self, victim_id: str) -> None:
        """Release the victim's VRAM (unload or stop). Clears the draining flag."""
        await self._apps.release(victim_id)
        # Evicted → no longer a drain target (spec 06 §2). Without this the
        # victim's stale `draining` flag survives re-load and blocks
        # can_forward_now, starving its own queued requests.
        self._states.set_draining(victim_id, False)

    async def _verify_victim_vram_freed(
        self, victim_id: str, candidate_id: str, trigger: str
    ) -> bool:
        """Invariant 1 (spec 07 §8): verify victim VRAM is actually freed.
        Returns True if freed; False (and handles drain failure logic) if not.
        """
        victim_st = self._states.get(victim_id)
        if victim_st.residency is not ResidencyState.LOADED:
            return True  # victim successfully unloaded

        # Victim still resident (VRAM not freed) — must keep DRAINING flag.
        # But if drain failed 3 times, give up and clear the flag.
        victim_st.drain_failures += 1
        if victim_st.drain_failures >= 3:
            self._states.set_draining(victim_id, False)
            victim_st.drain_failures = 0
            log.warning(
                "drain gave up after 3 failures: clearing flag",
                victim=victim_id,
                candidate=candidate_id,
            )
        else:
            self._states.set_draining(victim_id, True)

        log.warning(
            "switch aborted: victim VRAM not freed",
            victim=victim_id,
            candidate=candidate_id,
            drain_failures=victim_st.drain_failures,
        )
        await self._emit(
            "SWITCH_ABORTED",
            application_id=candidate_id,
            victim=victim_id,
            trigger=trigger,
            reason="victim_vram_not_freed",
        )
        return False

    async def _load_and_forward_candidate(self, app_id: str, trigger: str) -> bool:
        """Load candidate, forward queued requests, emit SWITCH_IN."""
        st = await self._ensure_running_loaded(app_id)
        # (Re)loaded → no longer a drain target either.
        self._states.set_draining(app_id, False)
        ok = (
            st.process is ProcessState.STARTED and st.residency is ResidencyState.LOADED
        )
        if ok:
            self._forward(app_id, self._rm.app_limit(app_id))
            await self._emit("SWITCH_IN", application_id=app_id, trigger=trigger)
        return ok

    def _vram_accounting(self, allow_sample: bool = True) -> dict:
        """spec 11 §5: total/reserve/resident/reserved/external/free.

        ``allow_sample`` controls whether a missing cached sample triggers a fresh
        (potentially blocking NVML) ``sample()``. The API current-decision endpoint
        calls it with the default (True); ``_decide`` calls it under the scheduler
        lock with ``allow_sample=False`` so the event loop is never blocked by NVML
        (``observed_mb``/``reservations`` read cached state only).
        """
        s = self._resources.last_sample
        if s is None:
            if not allow_sample:
                # No cached sample and we must not block: report a degraded
                # zeroed accounting rather than triggering NVML under the lock.
                return {
                    "total_mb": 0.0,
                    "reserve_mb": round(float(self._settings.gpu.memory_reserve_mb), 1),
                    "usable_mb": 0.0,
                    "resident_mb": round(sum(self._resources.observed_mb(a) for a in self._app_ids), 1),
                    "reserved_mb": round(
                        sum(r.required_mb for r in self._resources.reservations().values()), 1
                    ),
                    "external_mb": 0.0,
                    "free_mb": 0.0,
                    "stale": True,
                }
            s = self._resources.sample()
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
        rec["vram"] = self._vram_accounting(allow_sample=False)
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
