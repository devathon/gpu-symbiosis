"""E2E: the spec 01 §3 core loop (Phase 1 definition of done).

24 GB GPU (fake): A1 (18 GB, :9501) resident; a request to A2's gateway
queues while A1 is busy; when A1's request completes the scheduler drains +
unloads A1, loads A2, and streams A2's request back.

Phase 2 runs the full scoring model (spec 06 §6), so the test pins the
anti-thrash knobs to fast values (min residency 0, margin 1) — a default
30 s minimum residency would legitimately defer the switch (spec 06 §12).
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from conftest import (
    A1_GW,
    A2_GW,
    _app_state,
    _decisions,
    _req_done,
    _stream,
    _wait,
)
from gpusybio.config.models import SchedulerSpec

ADMIN_PORT = 9530


@pytest.fixture
def cfg_overrides():
    return {"scheduler": SchedulerSpec(min_residency_sec=0, switch_margin=1)}


async def test_core_loop(server):
    """A1 resident → A2 request queues → A1 drains+unloads → A2 loads → streams."""
    admin = f"http://127.0.0.1:{ADMIN_PORT}"
    async with httpx.AsyncClient() as c:
        # 1. Start A1 (resident, 18 GB).
        r = await c.post(f"{admin}/api/applications/a1/start")
        assert r.status_code == 202, r.text

        async def a1_ready():
            d = await _app_state(c, "a1")
            return d["process_state"] == "RUNNING" and d["residency_state"] == "LOADED"

        await _wait(a1_ready, timeout=30)

        # A1 must be RUNNING/LOADED and holding ~18 GB.
        async def a1_resident():
            d = await _app_state(c, "a1")
            return d["gpu_memory_used_mb"] > 18000

        await _wait(a1_resident, timeout=10)
        st = await _app_state(c, "a1")
        assert st["process_state"] == "RUNNING"
        assert st["residency_state"] == "LOADED"
        assert st["gpu_memory_used_mb"] > 18000

        # 2. Long request on A1 (in-flight) + request to A2's gateway (queued).
        a1_task = asyncio.create_task(_stream(c, f"http://127.0.0.1:{A1_GW}/generate",
                                              {"chunks": 10, "delay": 0.2}))
        await asyncio.sleep(0.3)  # let the A1 request get in-flight
        r = await c.post(f"http://127.0.0.1:{A2_GW}/generate", json={"chunks": 4, "delay": 0.05},
                        timeout=60.0)
        # A2's request: queued while A1 busy, then switched to and served.
        assert r.status_code == 200, r.text
        assert "llama31" in r.text  # streamed from A2

        # A1's long request must also complete.
        a1_chunks = await a1_task
        assert "qwen38" in a1_chunks

        # 3. After the switch: A2 resident, A1 unloaded.
        st2 = await _app_state(c, "a2")
        assert st2["process_state"] == "RUNNING"
        assert st2["residency_state"] == "LOADED"
        st1 = await _app_state(c, "a1")
        assert st1["residency_state"] == "OFF", st1

        # 4. Request history shows the queue wait + completion.
        await _wait(lambda: _req_done(c, "COMPLETED", gw="gw-a2"), timeout=10)
        rows = (await c.get(f"{admin}/api/requests?status=COMPLETED")).json()
        a2_rows = [r for r in rows if r["gateway_id"] == "gw-a2"]
        assert len(a2_rows) == 1
        assert a2_rows[0]["queue_wait_sec"] is None or a2_rows[0]["queue_wait_sec"] > 0

        # 5. Scheduler decision log shows a drain then a switch (spec 06 §14/§17).
        decisions = await _decisions(c)
        assert any(d["decision"] == "drain" for d in decisions), decisions
        assert any(d["decision"] == "switch" for d in decisions), decisions


async def test_zero_cost_resident(server):
    """Request to a resident app's gateway forwards immediately (zero cost, spec 06 §7)."""
    admin = f"http://127.0.0.1:{ADMIN_PORT}"
    async with httpx.AsyncClient() as c:
        await c.post(f"{admin}/api/applications/a1/start")
        await _wait(lambda: _app_state(c, "a1"), timeout=20)
        t0 = time.monotonic()
        r = await c.post(f"http://127.0.0.1:{A1_GW}/generate", json={"chunks": 3, "delay": 0.05}, timeout=30)
        elapsed = time.monotonic() - t0
        assert r.status_code == 200
        assert "qwen38" in r.text
        assert elapsed < 5  # immediate, no queue


async def test_resource_view(server):
    """/api/status resources carries the full view (spec 10 §3): identity,
    utilisation in %, power + limit, and system (CPU/RAM) fields."""
    admin = f"http://127.0.0.1:{ADMIN_PORT}"
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{admin}/api/status")
        res = r.json()["resources"]
        # Identity / extra GPU telemetry (fake backend).
        assert res["name"]
        assert res["pcie"]
        assert res["clock_mhz"] > 0
        assert res["power_w"] is not None
        assert res["power_limit_w"] is not None
        # Utilisation in % + raw MB.
        assert 0 <= res["vram_pct"] <= 100
        assert 0 <= res["gpu_pct"] <= 100
        assert res["total_mb"] > 0
        assert res["used_mb"] > 0
        # System (CPU/RAM) — /proc based on Linux; fields must be present.
        assert "cpu_pct" in res and "cpu_cores" in res
        assert "ram_total_mb" in res and "ram_used_mb" in res

        # /api/gpu exposes the same view.
        g = (await c.get(f"{admin}/api/gpu")).json()
        assert g["name"] == res["name"]
        assert g["vram_pct"] == res["vram_pct"]
