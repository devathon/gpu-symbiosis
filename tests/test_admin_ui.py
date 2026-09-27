"""Web management UI backend (spec 11 §2–§4): CRUD + queue inspector +
live gateway listeners + CSRF token endpoint."""

from __future__ import annotations

import asyncio
import httpx

import os

from conftest import _port_up, _wait, ADMIN_PORT, A1_GW


def _port_env(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


NEW_GW_PORT = _port_env("GSYBIO_NEW_GW_PORT", 9513)
NEW_GW_PORT2 = _port_env("GSYBIO_NEW_GW_PORT2", 9514)


async def test_queue_inspect_and_force(server):
    """Spec 11 §4: per-gateway queue list + per-request cancel + force-top."""
    c = httpx.AsyncClient()
    # Bring a1 up and hold its single slot with a long request.
    await c.post(f"http://127.0.0.1:{ADMIN_PORT}/api/applications/a1/start")
    await _wait(lambda: _req_running(c, "a1"))
    hold = asyncio.create_task(_long_request(c))
    # A second request must queue (a1's single slot is held).
    short = asyncio.create_task(_short_request(c))
    await _wait(lambda: _req_done(c, "QUEUED"))
    rows = (await c.get(f"http://127.0.0.1:{ADMIN_PORT}/api/requests?status=QUEUED")).json()
    queued = rows[0]["id"]

    # Inspector lists the queued request with wait time + priority.
    r = await c.get(f"http://127.0.0.1:{ADMIN_PORT}/api/gateways/gw-a1/queue")
    assert r.status_code == 200
    q = r.json()["queue"]
    assert len(q) == 1 and q[0]["request_id"] == queued
    assert q[0]["waiting_sec"] >= 0

    # Force raises it to the top (queue is single here, priority must bump).
    r = await c.post(f"http://127.0.0.1:{ADMIN_PORT}/api/gateways/gw-a1/queue/{queued}/force")
    assert r.status_code == 200 and r.json()["priority"] > 5
    # Force on an unqueued/terminal request → 404.
    r = await c.post(f"http://127.0.0.1:{ADMIN_PORT}/api/gateways/gw-a1/queue/nope/force")
    assert r.status_code == 404

    # Cancel it from the queue.
    r = await c.post(f"http://127.0.0.1:{ADMIN_PORT}/api/requests/{queued}/cancel")
    assert r.status_code == 202 and r.json()["cancelled"] is True
    assert (await c.get(f"http://127.0.0.1:{ADMIN_PORT}/api/gateways/gw-a1/queue")).json()["queue"] == []
    await c.aclose()


async def test_live_gateway_lifecycle(server):
    """Spec 11 §3.2: API-created gateways serve immediately; port change
    re-spawns; deletion closes the listener."""
    c = httpx.AsyncClient()

    # Validation first: unknown application → 400, port clash → 409.
    r = await c.post(f"http://127.0.0.1:{ADMIN_PORT}/api/gateways",
                     json={"name": "bad", "port": NEW_GW_PORT, "application_id": "nope", "priority": 5})
    assert r.status_code == 400
    r = await c.post(f"http://127.0.0.1:{ADMIN_PORT}/api/gateways",
                     json={"name": "clash", "port": A1_GW, "application_id": "a2", "priority": 5})
    assert r.status_code == 409  # port already used by gw-a1 (in this test DB)

    # Create: listener is live on the new port immediately.
    r = await c.post(f"http://127.0.0.1:{ADMIN_PORT}/api/gateways",
                     json={"name": "gw-live", "port": NEW_GW_PORT, "application_id": "a1", "priority": 7})
    assert r.status_code == 201, r.text
    gw = r.json()["id"]
    await _wait(lambda: _port_up(NEW_GW_PORT))

    # End-to-end through the fresh listener: a1 is stopped, the request is
    # intake'd + queued, the scheduler auto-starts a1, and the response streams.
    task = asyncio.create_task(
        c.post(f"http://127.0.0.1:{NEW_GW_PORT}/generate",
               json={"chunks": 4, "delay": 0.01}, timeout=45)
    )
    await _wait(lambda: _gw_requests(c, gw))  # request row exists for the new gateway
    r = await task
    assert r.status_code == 200

    # Port change re-spawns on the new port, old port dies.
    r = await c.put(f"http://127.0.0.1:{ADMIN_PORT}/api/gateways/{gw}", json={"port": NEW_GW_PORT2})
    assert r.status_code == 200
    await _wait(lambda: _port_up(NEW_GW_PORT2))
    await _wait(lambda: _port_down(NEW_GW_PORT), timeout=10)
    assert (await c.get(f"http://127.0.0.1:{ADMIN_PORT}/api/gateways")).json()
    assert any(g["port"] == NEW_GW_PORT2 and g["id"] == gw for g in
               (await c.get(f"http://127.0.0.1:{ADMIN_PORT}/api/gateways")).json())

    # Delete closes the listener.
    r = await c.delete(f"http://127.0.0.1:{ADMIN_PORT}/api/gateways/{gw}?confirm=true")
    assert r.status_code == 200
    await _wait(lambda: _port_down(NEW_GW_PORT2), timeout=10)
    assert not any(g["id"] == gw for g in
                   (await c.get(f"http://127.0.0.1:{ADMIN_PORT}/api/gateways")).json())
    await c.aclose()


async def test_app_crud_round_trip(server):
    """Spec 11 §2: create → update → delete an application via the API."""
    c = httpx.AsyncClient()
    body = {
        "id": "app-new",
        "name": "new model",
        "description": "test app",
        "start_script": "true",
        "stop_script": "true",
        "port": 9701,
        "configured_gpu_memory_mb": 8192,
    }
    r = await c.post(f"http://127.0.0.1:{ADMIN_PORT}/api/applications", json=body)
    assert r.status_code == 201, r.text
    got = (await c.get(f"http://127.0.0.1:{ADMIN_PORT}/api/applications/app-new")).json()
    assert got["name"] == "new model" and got["gpu_memory_configured_mb"] == 8192

    r = await c.put(f"http://127.0.0.1:{ADMIN_PORT}/api/applications/app-new",
                    json={"description": "edited", "gpu_memory_safety_margin": 0.2})
    assert r.status_code == 200
    got = (await c.get(f"http://127.0.0.1:{ADMIN_PORT}/api/applications/app-new")).json()
    assert got["description"] == "edited"

    # Delete without confirm → 409; with confirm → gone.
    r = await c.delete(f"http://127.0.0.1:{ADMIN_PORT}/api/applications/app-new")
    assert r.status_code == 409
    r = await c.delete(f"http://127.0.0.1:{ADMIN_PORT}/api/applications/app-new?confirm=true")
    assert r.status_code == 202
    assert (await c.get(f"http://127.0.0.1:{ADMIN_PORT}/api/applications/app-new")).status_code == 404
    await c.aclose()


async def test_auth_csrf_endpoint(server):
    """Spec 12 §3: the double-submit CSRF token is re-acquirable per session."""
    c = httpx.AsyncClient()
    # No admin configured (dev mode) → not authenticated.
    assert (await c.get(f"http://127.0.0.1:{ADMIN_PORT}/api/auth/csrf")).status_code == 401
    # Bootstrap an admin, log in, then fetch the token.
    r = await c.post(f"http://127.0.0.1:{ADMIN_PORT}/api/auth/admins",
                     json={"username": "ui", "password": "ui-pass-123"})
    assert r.status_code in (200, 201)
    r = await c.post(f"http://127.0.0.1:{ADMIN_PORT}/api/auth/login",
                     json={"username": "ui", "password": "ui-pass-123"})
    assert r.status_code == 200
    token = r.json()["csrf"]
    assert token
    r = await c.get(f"http://127.0.0.1:{ADMIN_PORT}/api/auth/csrf")
    assert r.status_code == 200 and r.json()["csrf"] == token
    await c.aclose()


# -- helpers ------------------------------------------------------------------
async def _port_down(port):
    return not await _port_up(port)


async def _gw_requests(c, gw_id):
    rows = (await c.get(f"http://127.0.0.1:{ADMIN_PORT}/api/requests?limit=50")).json()
    return any(r["gateway_id"] == gw_id for r in rows)


async def _req_running(c, app_id):
    v = (await c.get(f"http://127.0.0.1:{ADMIN_PORT}/api/applications/{app_id}")).json()
    return v["process_state"] == "RUNNING" and v["residency_state"] == "LOADED"


async def _long_request(c):
    # ~3s of streaming: holds a1's single concurrent slot.
    await c.post(f"http://127.0.0.1:{A1_GW}/generate",
                 json={"chunks": 60, "delay": 0.05}, timeout=60.0)


async def _short_request(c):
    # Status is not asserted: it completes normally or is cancelled (409) by the test.
    try:
        await c.post(f"http://127.0.0.1:{A1_GW}/generate",
                     json={"chunks": 2, "delay": 0.01}, timeout=30.0)
    except Exception:  # noqa: BLE001
        pass


async def _req_done(c, status):
    rows = (await c.get(f"http://127.0.0.1:{ADMIN_PORT}/api/requests?status={status}")).json()
    return len(rows) >= 1
