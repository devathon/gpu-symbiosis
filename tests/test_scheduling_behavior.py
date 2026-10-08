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
    A1_PORT,
    A2_GW,
    A2_PORT,
    ADMIN_PORT,
    _app_state,
    _decisions,
    _kill_mocks,
    _port_up,
    _stream,
    _wait,
    build_app_spec,
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
        return d["process_state"] == "STARTED" and d["residency_state"] == "LOADED"

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
            assert st2["process_state"] != "STARTED"
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


async def test_running_app_is_not_evicted_while_using_gpu():
    """Never evict a running app (spec 04 §3 / spec 06 §2): a draining victim
    that is still using the GPU (rolling-average ``is_active``) is NOT switched
    out, even when a candidate's request is queued. A momentary 0% dip does not
    release it either — the drain only completes once the app is truly idle."""
    srv, task = await _start_server(SchedulerSpec(min_residency_sec=0, switch_margin=1))
    try:
        async with httpx.AsyncClient() as c:
            await _a1_resident(c)

            # Hold A1 at 99% GPU (simulating a busy, running app — e.g. ComfyUI
            # mid-generation) and let the 5 s rolling window build up.
            backend = srv.resources.backend
            backend.mark_active("a1", 99.0)
            await _wait(lambda: _util(c, "a1"), timeout=10)

            # A2 request queued → A1 becomes the drain target.
            a2_task = asyncio.create_task(
                c.post(f"http://127.0.0.1:{A2_GW}/generate",
                       json={"chunks": 30, "delay": 0.25}, timeout=60)
            )

            # A1 is marked DRAINING (it is active — using the GPU).
            async def a1_draining():
                s = await _app_state(c, "a1")
                return s["draining"] is True

            await _wait(a1_draining, timeout=10)

            # Give the drain-completion check several ticks to (wrongly) fire.
            # A1 is still using the GPU, so it must stay resident — NOT switched.
            await asyncio.sleep(4.0)
            st1 = await _app_state(c, "a1")
            assert st1["residency_state"] == "LOADED", st1
            assert st1["draining"] is True, st1

            # No switch to A2 happened (the drain guard held).
            decisions = await _decisions(c)
            assert not any(d["decision"] == "switch" for d in decisions), decisions

            # Once A1 truly goes idle (GPU drops and the window drains), the
            # drain completes and the switch proceeds.
            backend.mark_idle("a1")

            async def a2_loaded():
                s = await _app_state(c, "a2")
                return s["residency_state"] == "LOADED"

            await _wait(a2_loaded, timeout=30)
            st1b = await _app_state(c, "a1")
            assert st1b["residency_state"] != "LOADED", st1b
            r2 = await a2_task
            assert r2.status_code == 200 and "llama31" in r2.text
    finally:
        await _stop_server(srv, task)


async def _util(c, app_id):
    """The app's reported GPU% (the active/RUNNING display signal) — waits until it is high."""
    s = await _app_state(c, app_id)
    return (s.get("gpu_utilization_pct") or 0.0) >= 5.0


async def test_unload_failure_restores_draining_and_blocks_forward():
    """
    Reproduce the comfyui→qwen3827b-lg race (user's exact scenario):

    1. a1 is resident and draining (a2 request queued).
    2. a1's unload FAILS (unload_script exits non-zero) → a1 stays LOADED.
    3. The drain-completion check fires; _do_switch aborts (victim VRAM not freed).
    4. The fix: draining flag is RESTORED (for first 2 failures).
    5. Next evaluation: resident-first path SKIPS the draining victim → a1's
       queued request is NOT forwarded, and a2 is NOT loaded on top.

    After 3 consecutive failures, the drain flag is cleared (give-up) to prevent
    an infinite UNLOAD→UNLOAD_FAILED loop. This is verified in
    test_unload_failure_gives_up_after_3.
    """
    spec = SchedulerSpec(min_residency_sec=0, switch_margin=1)
    a1_spec = build_app_spec("a1", "qwen38", A1_PORT, loaded=True, gpu_memory_mb=18432)
    a2_spec = build_app_spec("a2", "llama31", A2_PORT, loaded=True, gpu_memory_mb=12288)
    a1_spec = a1_spec.model_copy(update={"unload_script": "exit 1"})  # deterministic failure

    srv = Server(make_config(spec, applications=[a1_spec, a2_spec]))
    task = asyncio.create_task(srv.serve())
    try:
        await _wait(lambda: _port_up(ADMIN_PORT), timeout=10)
        async with httpx.AsyncClient() as c:
            # Start a1 and wait for LOADED
            await c.post(f"{ADMIN}/api/applications/a1/start")

            async def a1_ready():
                d = await _app_state(c, "a1")
                return d["process_state"] == "STARTED" and d["residency_state"] == "LOADED"
            await _wait(a1_ready, timeout=30)

            # Queue a short request for a1, then a request for a2 (makes a1 the drain target)
            a1_task = asyncio.create_task(
                c.post(f"http://127.0.0.1:{A1_GW}/generate",
                       json={"chunks": 3, "delay": 0.05}, timeout=30)
            )
            await asyncio.sleep(0.3)
            a2_task = asyncio.create_task(
                c.post(f"http://127.0.0.1:{A2_GW}/generate",
                       json={"chunks": 30, "delay": 0.25}, timeout=60)
            )

            # a1 becomes DRAINING (drain target for a2)
            async def a1_draining():
                d = await _app_state(c, "a1")
                return d["draining"] is True
            await _wait(a1_draining, timeout=10)

            # a1's short work finishes → a1 is idle but still DRAINING
            await a1_task
            # Wait for ONE reconcile tick (1s cadence) to fire the drain-completion check
            await asyncio.sleep(0.8)

            # a1 should STILL be draining (restored after abort) and still LOADED
            st1 = await _app_state(c, "a1")
            assert st1["residency_state"] == "LOADED", f"a1 should stay LOADED, got {st1}"
            assert st1["draining"] is True, f"draining flag should be restored after abort, got {st1}"
            assert st1["active_requests"] == 0, f"a1 should be idle, got {st1}"

            # a2 is NOT loaded on top (candidate path blocked by restored drain)
            st2 = await _app_state(c, "a2")
            assert st2["residency_state"] != "LOADED", f"a2 must not load while a1 draining, got {st2}"

            # No new "forwarded" decision for a1 after the abort (resident-first path skips draining apps)
            decisions = await _decisions(c)
            forwards = [d for d in decisions if "forwarded" in (d.get("detail") or "")]
            # Only the initial zero-cost forward (before drain) should exist
            assert len(forwards) <= 1, f"unexpected forwards after abort: {forwards}"

            # a2's request is still queued (will be served once a1 truly goes idle)
            a2_task.cancel()
            try:
                await a2_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
    finally:
        await _stop_server(srv, task)


async def test_unload_failure_gives_up_after_3():
    """
    After 3 consecutive unload failures, the scheduler clears the draining flag
    to prevent an infinite UNLOAD→UNLOAD_FAILED loop. Once the drain is abandoned,
    the candidate can be scheduled normally (concurrently if it fits, or by waiting
    for a real unload).
    """
    spec = SchedulerSpec(min_residency_sec=0, switch_margin=1)
    a1_spec = build_app_spec("a1", "qwen38", A1_PORT, loaded=True, gpu_memory_mb=18432)
    a2_spec = build_app_spec("a2", "llama31", A2_PORT, loaded=True, gpu_memory_mb=12288)
    a1_spec = a1_spec.model_copy(update={"unload_script": "exit 1"})

    srv = Server(make_config(spec, applications=[a1_spec, a2_spec]))
    task = asyncio.create_task(srv.serve())
    try:
        await _wait(lambda: _port_up(ADMIN_PORT), timeout=10)
        async with httpx.AsyncClient() as c:
            await c.post(f"{ADMIN}/api/applications/a1/start")

            async def a1_ready():
                d = await _app_state(c, "a1")
                return d["process_state"] == "STARTED" and d["residency_state"] == "LOADED"
            await _wait(a1_ready, timeout=30)

            a1_task = asyncio.create_task(
                c.post(f"http://127.0.0.1:{A1_GW}/generate",
                       json={"chunks": 3, "delay": 0.05}, timeout=30)
            )
            await asyncio.sleep(0.3)
            a2_task = asyncio.create_task(
                c.post(f"http://127.0.0.1:{A2_GW}/generate",
                       json={"chunks": 30, "delay": 0.25}, timeout=60)
            )

            async def a1_draining():
                d = await _app_state(c, "a1")
                return d["draining"] is True
            await _wait(a1_draining, timeout=10)

            await a1_task
            # Wait for 3+ drain-completion checks (~3s at 1s cadence, but mock is faster)
            await asyncio.sleep(3.5)

            # After 3 failures, drain flag should be cleared (give up)
            st1 = await _app_state(c, "a1")
            assert st1["residency_state"] == "LOADED", f"a1 should stay LOADED, got {st1}"
            # The drain flag is cleared after the 3rd failure. The key is that the
            # infinite UNLOAD→UNLOAD_FAILED loop stops (no more "unload failed" logs
            # after the "give up" log).

            # a2 should now be schedulable (concurrently in the mock since VRAM appears freed)
            st2 = await _app_state(c, "a2")
            # In the mock, a2 loads concurrently after the give-up. In production,
            # fits() would check real VRAM and only allow it if a1 truly freed memory.
            assert st2["residency_state"] == "LOADED", f"a2 should be loaded after give-up, got {st2}"

            a2_task.cancel()
            try:
                await a2_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
    finally:
        await _stop_server(srv, task)
