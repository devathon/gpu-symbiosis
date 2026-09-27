"""Phase 5 — pinned applications, request cancellation, load-failure recovery.

- Pinned apps are hard-excluded from automatic unloading (spec 06 §3.1).
- Requests can be cancelled explicitly (spec 05 §6.3); cancellation never
  leaves an orphan (Invariant 6).
- Load failures are tracked; after N consecutive failures (default 3) the app
  is FAULTED, auto-loading is refused, and a successful load resets the
  counter (spec 04 §9).
"""

from __future__ import annotations

import asyncio
import time

import httpx
from conftest import A1_GW, ADMIN_PORT, _app_state, _req_done, _wait


async def test_pinned_app_excluded_from_unload(server):
    c = httpx.AsyncClient()
    admin = f"http://127.0.0.1:{ADMIN_PORT}"
    # Pin a1 and confirm the flag is persisted + reflected in status.
    assert (await c.post(f"{admin}/api/applications/a1/pin")).json()["pinned"] is True
    assert (await _app_state(c, "a1"))["pinned"] is True

    # With a1 resident, the scheduler must not select it as an unload victim.
    await c.post(f"{admin}/api/applications/a1/start")

    async def a1_loaded():
        d = await _app_state(c, "a1")
        return d["process_state"] == "RUNNING" and d["residency_state"] == "LOADED"

    await _wait(a1_loaded, timeout=30)
    victim, reason = server.scheduler._select_victim(exclude="a2")
    assert victim is None and "pinned" in reason

    # Unpin → a1 becomes a valid candidate again.
    assert (await c.post(f"{admin}/api/applications/a1/unpin")).json()["pinned"] is False
    assert (await _app_state(c, "a1"))["pinned"] is False
    victim, reason = server.scheduler._select_victim(exclude="a2")
    assert victim is not None and reason == "ok"
    await c.aclose()


async def test_cancel_queued_request(server):
    c = httpx.AsyncClient()
    admin = f"http://127.0.0.1:{ADMIN_PORT}"
    await c.post(f"{admin}/api/applications/a1/start")

    async def a1_loaded():
        d = await _app_state(c, "a1")
        return d["process_state"] == "RUNNING" and d["residency_state"] == "LOADED"

    await _wait(a1_loaded, timeout=30)
    # max_concurrent_requests=1: a long request occupies the slot, a second queues.
    task_a = asyncio.create_task(
        c.post(f"http://127.0.0.1:{A1_GW}/generate", json={"chunks": 20, "delay": 0.3})
    )
    # Wait until request A is active (occupying the slot, not completed/queued).
    async def a1_active():
        return (await _app_state(c, "a1"))["active_requests"] > 0

    await _wait(a1_active, timeout=10)
    # Request B now queues (slot busy).
    task_b = asyncio.create_task(
        c.post(f"http://127.0.0.1:{A1_GW}/generate", json={"chunks": 2, "delay": 0.1})
    )
    await _wait(lambda: _req_done(c, "QUEUED", "gw-a1"), timeout=10)
    reqs = (await c.get(f"{admin}/api/requests?gateway_id=gw-a1")).json()
    queued = [q for q in reqs if q["status"] == "QUEUED"]
    assert queued, "expected a queued request"
    req_id = queued[0]["id"]

    # Cancel the queued request → CANCELLED and removed from the queue.
    assert (await c.post(f"{admin}/api/requests/{req_id}/cancel")).json()["cancelled"] is True
    assert (await _req_done(c, "CANCELLED", "gw-a1"))
    # The pending client request B resolves (not hung).
    await asyncio.wait_for(task_b, timeout=10)
    # No orphan: nothing left queued for gw-a1.
    assert not [q for q in (await c.get(f"{admin}/api/requests?gateway_id=gw-a1")).json()
                if q["status"] == "QUEUED"]
    # Request A still completes normally.
    await asyncio.wait_for(task_a, timeout=30)
    await c.aclose()


async def test_cancel_processing_request(server):
    c = httpx.AsyncClient()
    admin = f"http://127.0.0.1:{ADMIN_PORT}"
    await c.post(f"{admin}/api/applications/a1/start")

    async def a1_loaded():
        d = await _app_state(c, "a1")
        return d["process_state"] == "RUNNING" and d["residency_state"] == "LOADED"

    await _wait(a1_loaded, timeout=30)
    # Start a long request in the background (it becomes ACTIVE/processing).
    task = asyncio.create_task(
        c.post(f"http://127.0.0.1:{A1_GW}/generate", json={"chunks": 20, "delay": 0.3})
    )
    # Wait until the request is active (occupying the slot).
    async def active_now():
        return (await _app_state(c, "a1"))["active_requests"] > 0

    await _wait(active_now, timeout=10)
    reqs = (await c.get(f"{admin}/api/requests?gateway_id=gw-a1")).json()
    active = [q for q in reqs if q["status"] not in ("QUEUED", "COMPLETED", "FAILED", "CANCELLED", "EXPIRED")]
    assert active, "expected an active request"
    req_id = active[0]["id"]
    # Cancel the processing request → CANCELLED.
    assert (await c.post(f"{admin}/api/requests/{req_id}/cancel")).json()["cancelled"] is True
    assert (await _req_done(c, "CANCELLED", "gw-a1"))
    await asyncio.wait_for(task, timeout=10)
    await c.aclose()


async def test_load_failure_recovery(server):
    c = httpx.AsyncClient()
    admin = f"http://127.0.0.1:{ADMIN_PORT}"
    mgr = server.apps
    # Start a1 (mock starts --start-loaded → RUNNING/LOADED), then unload → OFF
    # so that load() is eligible (it requires residency OFF).
    await c.post(f"{admin}/api/applications/a1/start")

    async def a1_loaded():
        d = await _app_state(c, "a1")
        return d["process_state"] == "RUNNING" and d["residency_state"] == "LOADED"

    await _wait(a1_loaded, timeout=30)
    await c.post(f"{admin}/api/applications/a1/unload")

    async def a1_off():
        d = await _app_state(c, "a1")
        return d["process_state"] == "RUNNING" and d["residency_state"] == "OFF"

    await _wait(a1_off, timeout=30)

    # Simulate 3 consecutive load failures → FAULTED (default N=3).
    for _ in range(3):
        await mgr._record_load_failure("a1")
    cfg = mgr.config("a1")
    assert cfg.consecutive_load_failures == 3
    assert cfg.faulted_until is not None and time.time() < cfg.faulted_until

    # Auto-loading is refused while faulted (spec 04 §9).
    assert await mgr.load("a1") is None
    # The fault is still visible in status.
    st = await _app_state(c, "a1")
    assert st.get("faulted_until") is not None or st.get("consecutive_load_failures", 0) > 0

    # Cooldown expiry re-allows loading; a successful load resets the counter.
    with server.db() as s:
        from gpusybio.database.models import Application
        row = s.get(Application, "a1")
        row.faulted_until = None
    mgr.load_config("a1")
    assert await mgr.load("a1") is not None  # load succeeds (mock app)
    cfg = mgr.config("a1")
    assert cfg.consecutive_load_failures == 0
    assert cfg.faulted_until is None
    await c.aclose()
