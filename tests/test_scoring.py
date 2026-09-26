"""Unit tests for the gateway scoring model (spec 06 §6) and guards.

Covers the worked example (spec 06 §6.10), the no-clamping rule (§6.1),
the normalized-demand curve (§6.6), the service-rate epsilon (§6.5),
switching cost (§11), the emergency test (§15), and EWMA demand decay (§6.3).
"""

from __future__ import annotations

import time

from gpusybio.scoring import (
    DemandTracker,
    gateway_score,
    is_emergency,
    normalized_demand,
    switch_cost,
)


def test_worked_example_06_10():
    """spec 06 §6.10: priority 10, queue 3/10, oldest 18s/60s,
    arrival 8/min, service 4/min → 3.0 + 0.30 + 6.67 ≈ 10.0."""
    s = gateway_score(10, 3, 10, 18, 60, 8, 4)
    assert abs(s["backlog_pressure"] - 0.30) < 1e-6
    assert abs(s["priority_pressure"] - 3.0) < 1e-6
    assert abs(s["waiting_pressure"] - 0.30) < 1e-6
    assert abs(s["demand_ratio"] - 2.0) < 1e-6
    assert abs(s["normalized_demand"] - 2 / 3) < 1e-3
    assert abs(s["recent_demand_pressure"] - 20 / 3) < 1e-3
    assert abs(s["score"] - 10.0) < 0.05  # 9.967


def test_backlog_no_clamping_06_01():
    """An overloaded queue stays > 1 — no clamping (spec 06 §6.1)."""
    s = gateway_score(10, 12, 10, 90, 60, 0, 0)
    # backlog = max(12/10, 90/60) = 1.5
    assert abs(s["backlog_pressure"] - 1.5) < 1e-6
    assert s["score"] > 10  # urgency grows past the nominal scale


def test_backlog_is_max_of_ratios():
    # queue ratio dominates
    s = gateway_score(5, 8, 10, 1, 60, 0, 0)
    assert abs(s["backlog_pressure"] - 0.8) < 1e-6
    # wait ratio dominates
    s = gateway_score(5, 1, 10, 30, 60, 0, 0)
    assert abs(s["backlog_pressure"] - 0.5) < 1e-6


def test_normalized_demand_table_06_06():
    assert normalized_demand(0) == 0
    assert abs(normalized_demand(1) - 0.5) < 1e-9
    assert abs(normalized_demand(2) - 2 / 3) < 1e-9
    assert abs(normalized_demand(10) - 10 / 11) < 1e-9


def test_service_floor_epsilon_06_05():
    """Zero service rate → ratio = arrival / epsilon (no division by zero)."""
    s = gateway_score(10, 0, 20, 0, 300, 1, 0)
    assert abs(s["demand_ratio"] - 10) < 1e-6  # 1 / 0.1


def test_empty_queue_zero_pressure():
    s = gateway_score(10, 0, 20, 0, 300, 0, 4)
    assert s["priority_pressure"] == 0
    assert s["waiting_pressure"] == 0
    assert s["recent_demand_pressure"] == 0
    assert s["score"] == 0


def test_switch_cost_06_11():
    # removal only
    assert switch_cost([2.0], None, None, False, False) == 2.0
    # multiple removals + candidate load
    assert switch_cost([2.0, 1.0], None, 0.5, False, True) == 3.5
    # full: removals + startup + load
    assert switch_cost([2.0, 1.0], 3.0, 0.5, True, True) == 6.5
    # zero-cost resident path (spec 06 §11)
    assert switch_cost([], None, None, False, False) == 0.0
    # unmeasured timings count as 0, not an error
    assert switch_cost([None, 0.0], None, None, False, True) == 0.0


def test_emergency_06_15():
    # wait threshold
    assert is_emergency(300, 0, 300, None)
    assert is_emergency(301, 0, 300, None)
    assert not is_emergency(299, 0, 300, None)
    # queue threshold
    assert is_emergency(0, 5, None, 5)
    assert is_emergency(0, 6, None, 5)
    assert not is_emergency(0, 4, None, 5)
    # unset thresholds never fire
    assert not is_emergency(9999, 999, None, None)


def test_ewma_rate_is_per_minute():
    """8 events observed now ≈ 8 requests/minute (spec 06 §6.3 model)."""
    t = DemandTracker()
    for _ in range(8):
        t.arrival("gw")
    assert abs(t.arrival_rate("gw") - 8) < 1e-3  # ~8/min (tiny inter-call decay)
    assert t.arrival_rate("other") == 0


def test_ewma_burst_decays_06_03():
    """A burst decays away instead of pinning the rate at its peak."""
    t = DemandTracker(tau=0.2)
    for _ in range(8):
        t.arrival("gw")
    assert abs(t.arrival_rate("gw") - 8) < 1e-3  # ~8/min (tiny inter-call decay)
    time.sleep(0.4)  # e^(-0.4/0.2) ≈ 0.135
    assert t.arrival_rate("gw") < 2  # ≈ 8 * 0.135 ≈ 1.08


def test_ewma_service_tracking():
    t = DemandTracker()
    for _ in range(5):
        t.service("a1")
    assert abs(t.service_rate("a1") - 5) < 1e-3  # ~5/min (tiny inter-call decay)
    assert t.service_rate("a2") == 0
    # arrivals and services are independent
    assert t.arrival_rate("a1") == 0
