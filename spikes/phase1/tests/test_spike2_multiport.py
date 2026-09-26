"""SPIKE 2 — Multi-port ASGI listeners in one process/loop (spec 16 section 4).

Pass criteria:
- 3 gateway ports + 1 admin port all live simultaneously, one event loop, one process.
- Each gateway routes to its own target application.
- The admin API observes the same in-memory state the gateways mutate
  (shared state proves single control plane).
"""

import httpx


async def test_all_four_listeners_serve(stack):
    ports = stack["ports"]
    host = stack["host"]
    async with httpx.AsyncClient() as probe:
        # Each gateway routes to its own app.
        for gw, app_name in (("gw1", "qwen-a"), ("gw2", "qwen-b"), ("gw3", "comfy")):
            r = await probe.get(f"http://{host}:{ports[gw]}/health")
            assert r.status_code == 200, f"{gw} -> {r.status_code}"
            assert r.json() == {"app": app_name}

        # Shared state: the admin API (same process, same dict) sees what the
        # gateways did. Fetched AFTER the hits — it is a live snapshot.
        r = await probe.get(f"http://{host}:{ports['admin']}/api/system")
        assert r.status_code == 200
        system = r.json()
        assert system["shared_state"]["hits"]["qwen-a"] >= 1
        assert system["shared_state"]["hits"]["qwen-b"] >= 1
        assert system["shared_state"]["hits"]["comfy"] >= 1


async def test_gateways_are_independent(stack):
    """A request through gw1 must never reach app2/app3."""
    ports = stack["ports"]
    host = stack["host"]
    async with httpx.AsyncClient() as probe:
        for _ in range(3):
            r = await probe.get(f"http://{host}:{ports['gw1']}/health")
            assert r.json() == {"app": "qwen-a"}
        assert stack["state"]["hits"]["qwen-a"] == 3
        assert stack["state"]["hits"]["qwen-b"] == 0
        assert stack["state"]["hits"]["comfy"] == 0
