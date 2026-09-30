"""Scoring + unload-victim-selection methods for the Scheduler.

Mixed into :class:`~gpusybio.scheduler.Scheduler`. The methods are pure
readers: they read state through ``self`` (``_rm``, ``_states``, ``_apps``,
``_settings``, ``_tracking``, ``_boosts``, ``_app_ids``) and hold no state of
their own.
"""

from __future__ import annotations

import time

from gpusybio.applications.lifecycle import ProcessState, ResidencyState
from gpusybio.applications.manager import AppConfig
from gpusybio.scoring import EPS, gateway_score, normalized_demand, switch_cost


class ScoringMixin:
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
        for gid in self._rm.gateway_configs():
            if self._rm.gateway(gid)["application_id"] != app_id:
                continue
            sc = self._gateway_score(gid)
            if sc is not None and (best is None or sc["score"] > best["score"]):
                best = sc
        return best or {"score": 0.0}

    def _pick_candidate(self, exclude: set[str] | None = None) -> tuple[dict, dict] | None:
        """Candidate selection (spec 05 §9): argmax of gateway scores.
        Gateway set is read live so API-created gateways are schedulable.
        ``exclude``: application ids whose gateways are skipped (the drain
        completion check excludes the draining app's own gateways)."""
        best_gw: str | None = None
        best_sc: dict | None = None
        for gid in self._rm.gateway_configs():
            if exclude and self._rm.gateway(gid)["application_id"] in exclude:
                continue
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

    # -- victim selection + switching guards (spec 06 §3/§11/§16) -----------------
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

    def _is_active(self, app_id: str) -> bool:
        """spec 04 §3: the single ``active`` definition — serving a request OR
        using the GPU. Used by the eviction guard so a RUNNING no-gateway app
        (high GPU utilisation, no request rows) is never evicted mid-work."""
        from gpusybio.scheduler.activity import is_active

        s = self._states.get(app_id)
        if s is None:
            return False
        try:
            threshold = self._apps.config(app_id).idle_threshold_gpu_pct
        except Exception:  # noqa: BLE001
            threshold = None
        return is_active(s, self._resources, threshold)

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
        gws = [g for g in self._rm.gateway_configs() if self._rm.gateway(g)["application_id"] == cfg.id]
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
