"""Phase 5 — Prometheus metrics endpoint (spec 13 §3)."""

from __future__ import annotations

import asyncio

import httpx
from conftest import A1_GW, ADMIN_PORT, _app_state, _req_done, _stream, _wait


async def test_prometheus_endpoint(server):
    c = httpx.AsyncClient()
    admin = f"http://127.0.0.1:{ADMIN_PORT}"
    # Boot a1 so there is live data (memory, state, a completed request).
    await c.post(f"{admin}/api/applications/a1/start")

    async def a1_loaded():
        d = await _app_state(c, "a1")
        return d["process_state"] == "STARTED" and d["residency_state"] == "LOADED"

    await _wait(a1_loaded, timeout=30)
    text = await _stream(c, f"http://127.0.0.1:{A1_GW}/generate", {"chunks": 4, "delay": 0.02})
    assert text
    await _wait(lambda: _req_done(c, "COMPLETED", "gw-a1"), timeout=20)
    # Let the 1s sampler tick so current() has data.
    await asyncio.sleep(2.0)

    r = await c.get(f"{admin}/api/metrics/prometheus")
    assert r.status_code == 200
    assert "text/plain" in r.headers.get("content-type", "")
    body = r.text

    # GPU series (spec 13 §3).
    assert "gpu_load_balancer_gpu_memory_bytes{state=\"total\"}" in body
    assert "gpu_load_balancer_gpu_memory_bytes{state=\"used\"}" in body
    assert "gpu_load_balancer_gpu_memory_bytes{state=\"free\"}" in body
    assert "gpu_load_balancer_gpu_utilization " in body
    assert "gpu_load_balancer_gpu_temperature_celsius " in body
    assert "gpu_load_balancer_gpu_power_watts " in body
    # Application series.
    assert "gpu_load_balancer_application_memory_bytes{application=\"a1\"}" in body
    assert "gpu_load_balancer_application_state{application=\"a1\"" in body
    # Gateway series.
    assert "gpu_load_balancer_gateway_queue_length{gateway=\"gw-a1\"}" in body
    # Request totals (per gateway) + a completed request.
    assert "gpu_load_balancer_request_total{gateway=\"gw-a1\",status=\"completed\"} 1" in body
    # Scheduler decisions (a1 was activated).
    assert "gpu_load_balancer_scheduler_decisions_total{" in body
    await c.aclose()


async def test_prometheus_request_duration(server):
    c = httpx.AsyncClient()
    admin = f"http://127.0.0.1:{ADMIN_PORT}"
    await c.post(f"{admin}/api/applications/a1/start")

    async def a1_loaded():
        d = await _app_state(c, "a1")
        return d["process_state"] == "STARTED" and d["residency_state"] == "LOADED"

    await _wait(a1_loaded, timeout=30)
    text = await _stream(c, f"http://127.0.0.1:{A1_GW}/generate", {"chunks": 6, "delay": 0.03})
    assert text
    await _wait(lambda: _req_done(c, "COMPLETED", "gw-a1"), timeout=20)
    await asyncio.sleep(2.0)
    body = (await c.get(f"{admin}/api/metrics/prometheus")).text
    # Service duration recorded for the completed request.
    assert "gpu_load_balancer_request_duration_seconds{gateway=\"gw-a1\",phase=\"service\"}" in body
    await c.aclose()
