"""Phase 5 — scheduler simulation (what-if mode, spec 15 §Phase 5).

"Scheduler simulation = what-if mode that evaluates the scoring model against
historical data without acting." The endpoint runs the same decision logic as a
live trigger in dry-run mode and reports the decision that *would* have been
made, without applying any action (no forwards/loads/switches/boosts) or any
decision bookkeeping.
"""

from __future__ import annotations

import asyncio

import httpx
from conftest import A2_GW, ADMIN_PORT, _app_state, _req_done, _wait


async def test_simulate_does_not_act(server):
    c = httpx.AsyncClient()
    admin = f"http://127.0.0.1:{ADMIN_PORT}"
    # a1 (18 GB) resident; a2 (12 GB) does not fit alongside it → eviction path,
    # but the min-residency guard (30 s) blocks the switch → decision "keep".
    await c.post(f"{admin}/api/applications/a1/start")

    async def a1_loaded():
        d = await _app_state(c, "a1")
        return d["process_state"] == "STARTED" and d["residency_state"] == "LOADED"

    await _wait(a1_loaded, timeout=30)
    # Queue a request to a2's gateway → a2 becomes the candidate (queued, no fit).
    task = asyncio.create_task(
        c.post(f"http://127.0.0.1:{A2_GW}/generate", json={"chunks": 3, "delay": 0.1})
    )
    await _wait(lambda: _req_done(c, "QUEUED", "gw-a2"), timeout=10)
    queued = [r for r in (await c.get(f"{admin}/api/requests?gateway_id=gw-a2")).json()
              if r["status"] == "QUEUED"]
    assert queued, "expected a queued a2 request"
    req_id = queued[0]["id"]

    # Snapshot state BEFORE the simulation.
    s_before = await _app_state(c, "a1")

    # What-if: evaluate the scoring model without acting (a2 still the candidate).
    r = await c.post(f"{admin}/api/scheduler/simulate")
    assert r.status_code == 200
    sim = r.json()
    assert sim.get("simulated") is True
    assert sim.get("decision") in (
        "keep", "wait", "drain", "switch", "no_candidate", "activate_concurrent"
    )
    # The explainability payload is present (same shape as a live decision).
    assert "candidate_score" in sim and "effective_score" in sim
    assert "residents" in sim and "vram" in sim

    # No action was applied: a1 still resident, a2 not loaded.
    s_after = await _app_state(c, "a1")
    assert s_after["residency_state"] == s_before["residency_state"] == "LOADED"
    d2 = await _app_state(c, "a2")
    assert d2["residency_state"] == "OFF"

    # The request is still queued (the simulation did not forward it): we can
    # still cancel it, and it lands in CANCELLED (not PROCESSING/COMPLETED).
    assert (await c.post(f"{admin}/api/requests/{req_id}/cancel")).json()["cancelled"] is True
    await asyncio.wait_for(task, timeout=10)
    assert await _req_done(c, "CANCELLED", "gw-a2")

    # No decision bookkeeping: the live last_decision is NOT the simulated one.
    live = (await c.get(f"{admin}/api/scheduler/current-decision")).json()
    assert live.get("simulated") is not True
    await c.aclose()
