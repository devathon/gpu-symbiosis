"""Phase 5 — streaming hardening: WebSocket proxy + concurrent multi-app streaming.

Basic streaming works from Phase 1 (spec 05 §8). Phase 5 extends it with a
WebSocket bridge and concurrent multi-app streaming (spec 15 §Phase 5).
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
import websockets
from conftest import (
    A1_GW,
    A1_PORT,
    A2_GW,
    A2_PORT,
    ADMIN_PORT,
    _app_state,
    _req_done,
    _stream,
    _wait,
    build_app_spec,
)
from gpusybio.config.models import SchedulerSpec


async def test_websocket_proxy(server):
    """A WebSocket through the gateway is bridged to the resident app and back."""
    c = httpx.AsyncClient()
    admin = f"http://127.0.0.1:{ADMIN_PORT}"
    await c.post(f"{admin}/api/applications/a1/start")

    async def loaded():
        d = await _app_state(c, "a1")
        return d["process_state"] == "STARTED" and d["residency_state"] == "LOADED"

    await _wait(loaded, timeout=30)

    # Connect through the gateway port; the proxy bridges to the app's /ws.
    async with websockets.connect(f"ws://127.0.0.1:{A1_GW}/ws") as ws:
        await ws.send("hello")
        assert await ws.recv() == "ws-echo:hello"
        await ws.send("world")
        assert await ws.recv() == "ws-echo:world"

    await c.aclose()


@pytest.fixture
def cfg_overrides():
    return {
        "scheduler": SchedulerSpec(min_residency_sec=2.0),
        "applications": [
            build_app_spec("a1", "qwen38", A1_PORT, gpu_memory_mb=8192),
            build_app_spec("a2", "llama31", A2_PORT, gpu_memory_mb=8192),
        ],
    }


async def test_concurrent_multi_app_streaming(server):
    """Two resident apps stream concurrently, each from its own gateway (spec 02 §3)."""
    c = httpx.AsyncClient()
    admin = f"http://127.0.0.1:{ADMIN_PORT}"
    await c.post(f"{admin}/api/applications/a1/start")
    await c.post(f"{admin}/api/applications/a2/start")

    async def both_loaded():
        d1 = await _app_state(c, "a1")
        d2 = await _app_state(c, "a2")
        return (
            d1["process_state"] == "STARTED"
            and d1["residency_state"] == "LOADED"
            and d2["process_state"] == "STARTED"
            and d2["residency_state"] == "LOADED"
        )

    await _wait(both_loaded, timeout=30)

    # Stream from both gateways at the same time; both must complete and each
    # stream must come from its own app (proves per-gateway routing, spec 05).
    task_a = asyncio.create_task(
        _stream(c, f"http://127.0.0.1:{A1_GW}/generate", {"chunks": 8, "delay": 0.15})
    )
    task_b = asyncio.create_task(
        _stream(c, f"http://127.0.0.1:{A2_GW}/generate", {"chunks": 8, "delay": 0.15})
    )
    text_a = await asyncio.wait_for(task_a, timeout=30)
    text_b = await asyncio.wait_for(task_b, timeout=30)
    # Each gateway serves its own app (distinct names in the streamed chunks).
    assert "qwen38" in text_a and "llama31" not in text_a
    assert "llama31" in text_b and "qwen38" not in text_b

    # Both requests completed (not cancelled/expired).
    assert await _req_done(c, "COMPLETED", "gw-a1")
    assert await _req_done(c, "COMPLETED", "gw-a2")
    # Both apps remain resident (no eviction — coexistence, spec 02 §3).
    s1 = await _app_state(c, "a1")
    s2 = await _app_state(c, "a2")
    assert s1["residency_state"] == "LOADED"
    assert s2["residency_state"] == "LOADED"
    await c.aclose()
