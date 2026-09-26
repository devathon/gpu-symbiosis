"""Phase 5 — per-gateway API keys (spec 12 §2).

Keys are checked before queueing: invalid/missing → 401 and the request never
enters a queue. A gateway with no keys allows all requests (auth is optional);
once a key exists, a valid ``Authorization: Bearer <key>`` is required.
"""

from __future__ import annotations

import httpx
from conftest import ADMIN_PORT, _app_state, _req_done, _stream, _wait

GW_PORT = 9511


async def test_gateway_api_key_enforced(server):
    c = httpx.AsyncClient()
    admin = f"http://127.0.0.1:{ADMIN_PORT}"
    gw = "gw-a1"

    # Start a1 so a forwarded request (valid key) returns 200, not a queue hang.
    await c.post(f"{admin}/api/applications/a1/start")

    async def a1_loaded():
        d = await _app_state(c, "a1")
        return d["process_state"] == "RUNNING" and d["residency_state"] == "LOADED"

    await _wait(a1_loaded, timeout=30)

    # 1. Create a key (plaintext shown once).
    r = await c.post(f"{admin}/api/gateways/{gw}/keys", json={"name": "test"})
    assert r.status_code == 201
    body = r.json()
    assert body["key"].startswith("gslb_")
    key = body["key"]
    key_id = body["id"]

    # The key is stored hashed — the plaintext is not in the listing.
    listing = (await c.get(f"{admin}/api/gateways/{gw}/keys")).json()["keys"]
    assert any(k["id"] == key_id for k in listing)
    assert key not in str(listing)

    # 2. Without a key → 401 (checked before queueing, returns immediately).
    assert (await c.get(f"http://127.0.0.1:{GW_PORT}/health")).status_code == 401
    # 3. With a wrong key → 401.
    assert (await c.get(f"http://127.0.0.1:{GW_PORT}/health",
                       headers={"Authorization": "Bearer wrong"})).status_code == 401
    # 4. With the valid key → allowed (200).
    assert (await c.get(f"http://127.0.0.1:{GW_PORT}/health",
                       headers={"Authorization": f"Bearer {key}"})).status_code == 200

    # 5. A streaming request with the key is served.
    text = await _stream(c, f"http://127.0.0.1:{GW_PORT}/generate", {"chunks": 4, "delay": 0.02},
                        headers={"Authorization": f"Bearer {key}"})
    assert text
    await _wait(lambda: _req_done(c, "COMPLETED", "gw-a1"), timeout=20)

    # 6. Delete the key → auth no longer required (no keys configured).
    assert (await c.delete(f"{admin}/api/gateways/{gw}/keys/{key_id}")).status_code == 200
    assert (await c.get(f"{admin}/api/gateways/{gw}/keys")).json()["keys"] == []
    assert (await c.get(f"http://127.0.0.1:{GW_PORT}/health")).status_code == 200
    await c.aclose()


async def test_401_does_not_enter_queue(server):
    c = httpx.AsyncClient()
    admin = f"http://127.0.0.1:{ADMIN_PORT}"
    gw = "gw-a1"
    await c.post(f"{admin}/api/gateways/{gw}/keys", json={"name": "test"})

    # A rejected (401) request must never create a request row / queue entry.
    before = len((await c.get(f"{admin}/api/requests")).json())
    assert (await c.get(f"http://127.0.0.1:{GW_PORT}/health")).status_code == 401
    after = len((await c.get(f"{admin}/api/requests")).json())
    assert after == before
    await c.aclose()
