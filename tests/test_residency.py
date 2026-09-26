"""Phase 4 — multi-application residency (coexistence, spec 02 §3).

When memory allows, two applications are resident simultaneously; a request to
either gateway is served from its resident app (zero-cost path, spec 06 §7).
Both apps here are 8 GB → 16 GB total, well under the 22.5 GB usable fake GPU.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from conftest import (
    ADMIN_PORT,
    A1_GW,
    A2_GW,
    _app_state,
    _req_done,
    _stream,
    _wait,
    build_app_spec,
)
from gpusybio.config.models import SchedulerSpec


@pytest.fixture
def cfg_overrides():
    return {
        "scheduler": SchedulerSpec(min_residency_sec=2.0),
        "applications": [
            build_app_spec("a1", "qwen38", 9501, gpu_memory_mb=8192),
            build_app_spec("a2", "llama31", 9502, gpu_memory_mb=8192),
        ],
    }


async def test_two_apps_coexist_and_both_served(server):
    c = httpx.AsyncClient()
    admin = f"http://127.0.0.1:{ADMIN_PORT}"
    # Start both apps.
    await c.post(f"{admin}/api/applications/a1/start")
    await c.post(f"{admin}/api/applications/a2/start")

    async def both_loaded():
        d1 = await _app_state(c, "a1")
        d2 = await _app_state(c, "a2")
        return (
            d1["process_state"] == "RUNNING"
            and d1["residency_state"] == "LOADED"
            and d2["process_state"] == "RUNNING"
            and d2["residency_state"] == "LOADED"
        )

    # Both resident simultaneously (coexistence — no eviction needed).
    await _wait(both_loaded, timeout=30)
    s1 = await _app_state(c, "a1")
    s2 = await _app_state(c, "a2")
    assert s1["residency_state"] == "LOADED"
    assert s2["residency_state"] == "LOADED"
    # Both hold live memory at the same time.
    assert s1["gpu_memory_used_mb"] > 0
    assert s2["gpu_memory_used_mb"] > 0

    # A request to each gateway is served from the resident app.
    t1 = await _stream(c, f"http://127.0.0.1:{A1_GW}/generate", {"chunks": 4, "delay": 0.02})
    assert t1
    t2 = await _stream(c, f"http://127.0.0.1:{A2_GW}/generate", {"chunks": 4, "delay": 0.02})
    assert t2

    # Both complete as served requests.
    await asyncio.sleep(1.0)
    assert await _req_done(c, "COMPLETED", "gw-a1")
    assert await _req_done(c, "COMPLETED", "gw-a2")
    # Still both resident after serving.
    assert (await _app_state(c, "a1"))["residency_state"] == "LOADED"
    assert (await _app_state(c, "a2"))["residency_state"] == "LOADED"
    await c.aclose()
