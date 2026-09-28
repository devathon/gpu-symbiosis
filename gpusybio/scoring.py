"""Gateway scoring model (spec 06 §6), demand tracking (spec 06 §6.3/§6.4),
switching cost (spec 06 §11), emergency test (spec 06 §15).

Pure functions — unit-testable without a live scheduler.
"""

from __future__ import annotations

import math
import time

# spec 06 §6.5: small minimum value preventing division by zero.
EPS = 0.1


def backlog_pressure(queue_len: int, max_queue: int, oldest_wait: float, max_wait: float) -> float:
    """spec 06 §6.1. Allowed to exceed 1 — an overdue request becomes
    increasingly urgent (no clamping)."""
    q = queue_len / max(1, max_queue)
    a = oldest_wait / max(1.0, max_wait)
    return max(q, a)


def normalized_demand(demand_ratio: float) -> float:
    """spec 06 §6.6: ratio / (1 + ratio). 0→0, 1→0.5, 2→0.67, 10→0.91."""
    return demand_ratio / (1.0 + demand_ratio)


def gateway_score(
    priority: int,
    queue_len: int,
    max_queue: int,
    oldest_wait: float,
    max_wait: float,
    arrival_rate: float,
    service_rate: float,
) -> dict:
    """spec 06 §6.9 final formulation, with the full breakdown for the
    explainability surface (spec 06 §19, spec 11 §5).

    gateway_score = priority_pressure + waiting_pressure + recent_demand_pressure
    """
    bp = backlog_pressure(queue_len, max_queue, oldest_wait, max_wait)
    pp = priority * bp
    wp = oldest_wait / max(1.0, max_wait)
    dr = arrival_rate / max(service_rate, EPS)
    nd = normalized_demand(dr)
    rdp = priority * nd
    return {
        "score": round(pp + wp + rdp, 3),
        "backlog_pressure": round(bp, 4),
        "priority_pressure": round(pp, 3),
        "waiting_pressure": round(wp, 3),
        "arrival_rate": round(arrival_rate, 3),
        "service_rate": round(service_rate, 3),
        "demand_ratio": round(dr, 3),
        "normalized_demand": round(nd, 3),
        "recent_demand_pressure": round(rdp, 3),
    }


def switch_cost(
    unload_times: list[float],
    startup_time: float | None,
    load_time: float | None,
    needs_startup: bool,
    needs_load: bool,
) -> float:
    """spec 06 §11: Σ unload_time(removed) + candidate.startup (if process
    startup required) + candidate.load (if load required). Seconds.

    Zero-cost case (spec 06 §11): candidate already resident → no removal, no
    start, no load → 0.
    """
    cost = sum(t for t in unload_times if t)
    if needs_startup:
        cost += startup_time or 0.0
    if needs_load:
        cost += load_time or 0.0
    return cost


def switching_penalty(cost: float, time_scale: float, weight: float) -> float:
    """spec 06 §11: the raw switching cost (seconds) is normalized by the
    candidate gateway's time horizon (``max_waiting_time_sec``) — the same
    scale the score's waiting term already uses (``oldest_wait / max_wait``,
    §6.9) — then multiplied by the weight. This keeps the penalty on the
    score's scale instead of raw seconds, which would dwarf it by roughly
    ``max_wait``× (a 6 s startup against a ~1 score)."""
    return (cost / max(1.0, time_scale)) * weight


def is_emergency(
    oldest_wait: float,
    queue_len: int,
    emergency_wait_sec: float | None,
    emergency_queue_threshold: int | None,
) -> bool:
    """spec 06 §15: overrides MIN_RESIDENCY (never the drain)."""
    if emergency_wait_sec and oldest_wait >= emergency_wait_sec:
        return True
    if emergency_queue_threshold is not None and queue_len >= emergency_queue_threshold:
        return True
    return False


class DemandTracker:
    """Time-decayed EWMA of request arrival rate (per gateway) and service
    rate (per application), spec 06 §6.3 / §6.4. Rates in requests/minute.

    Model: every observed event contributes an exponentially decaying weight
    e^(-Δt/τ) with τ = 60 s, so the rate ≈ the number of events per minute.
    This is the EWMA of the per-minute rate: it prevents a single burst from
    permanently dominating scheduling (spec 06 §6.3) — a burst decays away
    instead of pinning the rate at its peak.
    """

    def __init__(self, tau: float = 60.0) -> None:
        self._tau = tau
        self._arrivals: dict[str, float] = {}  # gateway_id → decayed count
        self._services: dict[str, float] = {}  # application_id → decayed count
        self._t = time.time()

    def _decay(self) -> None:
        now = time.time()
        dt = now - self._t
        if dt <= 0:
            return
        f = math.exp(-dt / self._tau)
        self._arrivals = {k: v * f for k, v in self._arrivals.items() if v * f > 1e-6}
        self._services = {k: v * f for k, v in self._services.items() if v * f > 1e-6}
        self._t = now

    # -- observation -----------------------------------------------------
    def arrival(self, gateway_id: str) -> None:
        self._decay()
        self._arrivals[gateway_id] = self._arrivals.get(gateway_id, 0.0) + 1.0

    def service(self, application_id: str) -> None:
        """One completed (successfully served) request."""
        self._decay()
        self._services[application_id] = self._services.get(application_id, 0.0) + 1.0

    # -- rates (requests/minute) ------------------------------------------
    def arrival_rate(self, gateway_id: str) -> float:
        self._decay()
        return self._arrivals.get(gateway_id, 0.0)

    def service_rate(self, application_id: str) -> float:
        self._decay()
        return self._services.get(application_id, 0.0)
