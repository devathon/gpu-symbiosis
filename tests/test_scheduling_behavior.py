"""Phase 2 behavior tests (spec 15): the scheduling model end-to-end.

Covers: minimum residency deferral (spec 06 §12), switch margin (spec 06 §13),
emergency override (spec 06 §15), Process Now boost (spec 06 §9), switch
cooldown anti-thrash (spec 06 §16), drain queueing (spec 06 §2: new requests
for a draining app queue — never dropped, never cancel or delay the drain),
and the admin over-allocation 409 (Invariant 2, spec 14).

Each test builds its own server with explicit scheduler knobs (fixture
setup runs before the test body, so knobs cannot be set in-test via a
shared fixture).
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from conftest import (
    A1_GW,
    A2_GW,
    ADMIN_PORT,
    _app_state,
    _decisions,
    _kill_mocks,
    _port_up,
    _stream,
    _wait,
    make_config,
)
from gpusybio.config.models import SchedulerSpec
from gpusybio.server import Server

ADMIN = f"http://127.0.0.1:{ADMIN_PORT}"


async def _start_server(spec: SchedulerSpec):
    srv = Server(make_config(spec))
    task = asyncio.create_task(srv.serve())
    await _wait(lambda: _port_up(ADMIN_PORT), timeout=10)
    return srv, task


async def _stop_server(srv, task) -> None:
    await srv.stop()
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
    await asyncio.sleep(0.3)
    _kill_mocks()


async def _a1_resident(c):
    await c.post(f"{ADMIN}/api/applications/a1/start")

    async def ready():
        d = await _app_state(c, "a1")
        return d["process_state"] == "RUNNING" and d["residency_state"] == "LOADED"

    await _wait(ready, timeout=30)


async def test_min_residency_defers_switch():
    """spec 06 §12: an idle candidate cannot evict a resident younger than
    MIN_RESIDENCY; the switch completes once the window elapses."""
    srv, task = await _start_server(SchedulerSpec(min_residency_sec=5, switch_margin=1))
    try:
        async with httpx.AsyncClient() as c:
            await _a1_resident(c)
            t0 = time.monotonic()

            # A1 busy (~3.2 s, inside the 5 s window) + A2 request queued.
            a1_task = asyncio.create_task(_stream(c, f"http://127.0.0.1:{A1_GW}/generate",
                                                  {"chunks": 12, "delay": 0.25}))
            await asyncio.sleep(0.3)
            r = await c.post(f"http://127.0.0.1:{A2_GW}/generate", json={"chunks": 3, "delay": 0.05},
                            timeout=60.0)
            assert r.status_code == 200, r.text
            assert "llama31" in r.text

            # A keep citing min residency happened, then a switch after the window.
            decisions = await _decisions(c)
            kept = [d for d in decisions if d["decision"] == "keep" and "min residency" in (d.get("detail") or "")]
            assert kept, decisions
            assert any(d["decision"] == "switch" for d in decisions), decisions
            assert time.monotonic() - t0 >= 4.0  # the switch beat nothing; it waited

            await a1_task
            st2 = await _app_state(c, "a2")
            assert st2["residency_state"] == "LOADED"
    finally:
        await _stop_server(srv, task)


async def test_switch_margin_blocks_switch():
    """spec 06 §13: effective score must exceed current + margin; a huge
    margin keeps the incumbent even when the candidate is far ahead."""
    srv, task = await _start_server(SchedulerSpec(min_residency_sec=0, switch_margin=1000))
    try:
        async with httpx.AsyncClient() as c:
            await _a1_resident(c)

            # A2 request: A1 idle → immediate comparison → margin blocks the switch.
            task = asyncio.create_task(c.post(f"http://127.0.0.1:{A2_GW}/generate",
                                              json={"chunks": 3, "delay": 0.05}, timeout=20))
            await _wait(lambda: _decisions(c), timeout=10)

            decisions = await _decisions(c)
            kept = [d for d in decisions
                    if d["decision"] == "keep" and d.get("candidate") == "a2" and "margin" in (d.get("detail") or "")]
            assert kept, decisions
            assert not any(d["decision"] == "switch" for d in decisions), decisions

            st1 = await _app_state(c, "a1")
            assert st1["residency_state"] == "LOADED"
            st2 = await _app_state(c, "a2")
            assert st2["residency_state"] != "LOADED"

            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
    finally:
        await _stop_server(srv, task)


async def test_emergency_overrides_min_residency():
    """spec 06 §15: waiting ≥ emergency_wait_sec overrides MIN_RESIDENCY
    (but still drains first — the switch happens only after A1 completes)."""
    srv, task = await _start_server(SchedulerSpec(min_residency_sec=5, switch_margin=1,
                                                 emergency_wait_sec=2.0))
    try:
        async with httpx.AsyncClient() as c:
            await _a1_resident(c)
            t0 = time.monotonic()

            # A1 busy (~4.0 s, inside the 5 s residency window); A2 waits past 2 s.
            # The drain decision needs a reconcile tick (1 s cadence) landing in the
            # window [a2 waited 2 s, a1 completes]; ~4 s keeps ≥2 ticks in that window
            # so the drain is robust to tick phase, while still beating the 5 s window.
            a1_task = asyncio.create_task(_stream(c, f"http://127.0.0.1:{A1_GW}/generate",
                                                  {"chunks": 16, "delay": 0.25}))
            await asyncio.sleep(0.3)
            r = await c.post(f"http://127.0.0.1:{A2_GW}/generate", json={"chunks": 3, "delay": 0.05},
                            timeout=60.0)
            assert r.status_code == 200, r.text
            assert "llama31" in r.text
            elapsed = time.monotonic() - t0
            assert elapsed < 5  # the switch beat the residency window (emergency)

            # A drain happened first (A1 had an active request), then the switch.
            decisions = await _decisions(c)
            assert any(d["decision"] == "drain" for d in decisions), decisions
            assert any(d["decision"] == "switch" for d in decisions), decisions
            await a1_task

            st2 = await _app_state(c, "a2")
            assert st2["residency_state"] == "LOADED"
    finally:
        await _stop_server(srv, task)


async def test_process_now_boost_changes_candidate():
    """spec 06 §9: Process Now applies a time-decaying priority boost; the
    boosted gateway becomes the candidate over a nominally higher-priority
    queued gateway. The boost never kills active work (the boosted request
    still waits for the drain)."""
    srv, task = await _start_server(SchedulerSpec(min_residency_sec=0, switch_margin=1))
    try:
        async with httpx.AsyncClient() as c:
            await _a1_resident(c)

            # A1 busy; one extra request to A1's gateway (priority 10) and
            # three to A2's (priority 5). Without the boost gw-a1 wins on
            # demand pressure; with it (5×2=10) gw-a2's larger backlog wins.
            a1_task = asyncio.create_task(_stream(c, f"http://127.0.0.1:{A1_GW}/generate",
                                                  {"chunks": 10, "delay": 0.25}))
            await asyncio.sleep(0.3)
            a1_extra = asyncio.create_task(c.post(f"http://127.0.0.1:{A1_GW}/generate",
                                                 json={"chunks": 2, "delay": 0.05}, timeout=30))
            await asyncio.sleep(0.2)
            a2_req = asyncio.create_task(c.post(f"http://127.0.0.1:{A2_GW}/generate",
                                               json={"chunks": 2, "delay": 0.05}, timeout=30))
            a2_req2 = asyncio.create_task(c.post(f"http://127.0.0.1:{A2_GW}/generate",
                                                json={"chunks": 2, "delay": 0.05}, timeout=30))
            a2_req3 = asyncio.create_task(c.post(f"http://127.0.0.1:{A2_GW}/generate",
                                                json={"chunks": 2, "delay": 0.05}, timeout=30))
            await asyncio.sleep(0.5)  # all queued

            # Without the boost the higher-priority queued gateway (gw-a1) is candidate.
            d = (await c.get(f"{ADMIN}/api/scheduler/current-decision")).json()
            assert d["candidate_application_id"] == "a1", d

            # Process Now on gw-a2 → boosted (5×2=10) beats gw-a1 (10×1, lower queue pressure).
            r = await c.post(f"{ADMIN}/api/gateways/gw-a2/process-now")
            assert r.status_code == 202, r.text

            async def boosted():
                d = (await c.get(f"{ADMIN}/api/scheduler/current-decision")).json()
                return d.get("candidate_application_id") == "a2"

            await _wait(boosted, timeout=10)

            d = (await c.get(f"{ADMIN}/api/scheduler/current-decision")).json()
            assert d["candidate_application_id"] == "a2", d
            assert d["boosts"], d

            # The boosted requests are served (after A1's drain); the other A1 request too.
            a2_resp = await a2_req
            assert "llama31" in a2_resp.text
            await a2_req2
            await a2_req3
            await a1_task
            a1_extra_resp = await a1_extra
            assert "qwen38" in a1_extra_resp.text
    finally:
        await _stop_server(srv, task)


async def test_switch_cooldown_prevents_reswitch():
    """spec 06 §16: re-switching the same pair within SWITCH_COOLDOWN is
    suppressed (anti-thrash); a different pair is not."""
    srv, task = await _start_server(SchedulerSpec(min_residency_sec=0, switch_margin=1,
                                                 switch_cooldown_sec=30.0))
    try:
        async with httpx.AsyncClient() as c:
            await _a1_resident(c)

            # First switch: a1 → a2 (A2 request completes).
            r = await c.post(f"http://127.0.0.1:{A2_GW}/generate", json={"chunks": 3, "delay": 0.05}, timeout=30)
            assert r.status_code == 200 and "llama31" in r.text

            async def a2_loaded():
                s = await _app_state(c, "a2")
                return s["residency_state"] == "LOADED"

            await _wait(a2_loaded, timeout=20)

            st = (await c.get(f"{ADMIN}/api/scheduler/current-decision")).json()
            assert st["switch_tracking"]["switch_count"] >= 1, st

            # Immediately re-request A1: the same pair (a1↔a2) is within cooldown.
            task = asyncio.create_task(c.post(f"http://127.0.0.1:{A1_GW}/generate",
                                              json={"chunks": 3, "delay": 0.05}, timeout=20))

            async def cooldown_kept():
                decisions = await _decisions(c)
                return any(d["decision"] == "keep" and "cooldown" in (d.get("detail") or "")
                           for d in decisions)

            await _wait(cooldown_kept, timeout=10)

            # A1 stays OFF; A2 stays resident.
            st1 = await _app_state(c, "a1")
            assert st1["residency_state"] == "OFF", st1
            st2 = await _app_state(c, "a2")
            assert st2["residency_state"] == "LOADED", st2

            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
    finally:
        await _stop_server(srv, task)


async def test_admin_start_rejects_overallocation():
    """Invariant 2 (spec 14): manual start/load never exceed the budget —
    rejected with 409 + required/free/residents (the Phase 1 loose end is
    closed)."""
    srv, task = await _start_server(SchedulerSpec())
    try:
        async with httpx.AsyncClient() as c:
            await _a1_resident(c)

            # A2 (12 GB × 1.1) cannot fit alongside the resident A1 (18 GB × 1.1).
            r = await c.post(f"{ADMIN}/api/applications/a2/start")
            assert r.status_code == 409, r.text
            body = r.json()["detail"] if isinstance(r.json(), dict) else r.json()
            if isinstance(body, dict):
                assert body["required_mb"] > 0
                assert body["free_mb"] < body["required_mb"]
                assert "a1" in body["residents"]
            else:
                assert "insufficient VRAM" in body

            # A2 untouched by the rejected transition.
            st2 = await _app_state(c, "a2")
            assert st2["process_state"] != "RUNNING"
    finally:
        await _stop_server(srv, task)


async def test_new_request_queues_during_drain():
    """spec 06 §2: new requests for a DRAINING (still resident) app QUEUE —
    never dropped, and they never cancel or delay the drain. Once A1's active
    work is done the drain completes (A1 unloads, A2 loads and is served); the
    queued A1 request is then served when the scheduler rotates back to A1.
    No starvation: the candidate the drain was made for always gets its turn."""
    srv, task = await _start_server(SchedulerSpec(min_residency_sec=0, switch_margin=1))
    try:
        async with httpx.AsyncClient() as c:
            await _a1_resident(c)

            # A1 busy (~2 s); an A2 request marks A1 as DRAINING.
            a1_task = asyncio.create_task(_stream(c, f"http://127.0.0.1:{A1_GW}/generate",
                                                  {"chunks": 8, "delay": 0.25}))
            await asyncio.sleep(0.3)
            a2_task = asyncio.create_task(c.post(f"http://127.0.0.1:{A2_GW}/generate",
                                                 json={"chunks": 3, "delay": 0.05}, timeout=60.0))

            async def draining():
                return (await _app_state(c, "a1"))["draining"] is True

            await _wait(draining, timeout=10)

            # A new request for the DRAINING app: accepted and queued — the
            # drain is NOT cancelled (A1 stays DRAINING while its work runs).
            new_a1 = asyncio.create_task(c.post(f"http://127.0.0.1:{A1_GW}/generate",
                                                json={"chunks": 3, "delay": 0.05}, timeout=60))
            await asyncio.sleep(0.5)  # let it queue (A1 is still busy)
            st1 = await _app_state(c, "a1")
            assert st1["draining"] is True, st1
            assert st1["queued_requests"] >= 1, st1

            # A1's in-flight work finishes → the drain completes (not
            # cancelled): A1 unloads, A2 loads and its request is served.
            async def a2_loaded():
                return (await _app_state(c, "a2"))["residency_state"] == "LOADED"

            await _wait(a2_loaded, timeout=20)
            st1 = await _app_state(c, "a1")
            assert st1["draining"] is False, st1
            assert st1["residency_state"] != "LOADED", st1
            r2 = await a2_task
            assert r2.status_code == 200 and "llama31" in r2.text

            # The queued A1 request was not cancelled: it is served when the
            # scheduler rotates back to A1 (its demand now wins).
            r1 = await new_a1
            assert r1.status_code == 200 and "qwen38" in r1.text
            await a1_task
    finally:
        await _stop_server(srv, task)
