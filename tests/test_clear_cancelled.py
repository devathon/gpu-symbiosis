"""Clear Queue must mark the cancelled request CANCELLED in the request log.

Regression: the clear-queue endpoint called the async ``_update_status``
without ``await``-ing it, so the coroutine was dropped and the DB row never
moved to CANCELLED — the request stayed QUEUED forever. Clearing a gateway's
queue must persist CANCELLED for every queued request it releases.
"""

from __future__ import annotations

import asyncio

import httpx

from conftest import ADMIN_PORT, A1_GW, _app_state, _wait

ADMIN = f"http://127.0.0.1:{ADMIN_PORT}"


async def _has(c, status: str, gw: str) -> bool:
    r = await c.get(f"{ADMIN}/api/requests?status={status}&gateway_id={gw}")
    return len(r.json()) >= 1


async def _a1_loaded(c) -> bool:
    d = await _app_state(c, "a1")
    return d["process_state"] == "STARTED" and d["residency_state"] == "LOADED"


async def test_clear_marks_request_cancelled(server):
    c = httpx.AsyncClient()
    try:
        # Load a1 so requests forward to it (it becomes the resident app).
        await c.post(f"{ADMIN}/api/applications/a1/start")
        await _wait(lambda: _a1_loaded(c), timeout=20)

        # A long request holds a1's single slot (max_concurrent = 1).
        long_task = asyncio.create_task(
            c.post(f"http://127.0.0.1:{A1_GW}/generate",
                   json={"chunks": 40, "delay": 0.5}, timeout=60)
        )
        await _wait(lambda: _has(c, "PROCESSING", "gw-a1"), timeout=20)

        # A second request cannot be served while the first is active → it queues.
        queued_task = asyncio.create_task(
            c.post(f"http://127.0.0.1:{A1_GW}/generate",
                   json={"chunks": 3, "delay": 0.05}, timeout=60)
        )
        await _wait(lambda: _has(c, "QUEUED", "gw-a1"), timeout=20)

        # Clear the gateway queue.
        r = await c.post(f"{ADMIN}/api/gateways/gw-a1/clear-queue?confirm=true")
        assert r.status_code == 200
        assert r.json()["cancelled"] >= 1

        # The queued request is now CANCELLED in the request log.
        await _wait(lambda: _has(c, "CANCELLED", "gw-a1"), timeout=10)

        # The cleared request's client call resolves (never hangs).
        queued_task.cancel()
        long_task.cancel()
        for t in (queued_task, long_task):
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
    finally:
        await c.aclose()
