"""E2E: the spec 01 §3 core loop (Phase 1 definition of done).

24 GB GPU (fake): A1 (18 GB, :9501) resident; a request to A2's gateway
queues while A1 is busy; when A1's request completes the scheduler drains +
unloads A1, loads A2, and streams A2's request back.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

import httpx
import pytest

from gpusybio.config.models import (
    ApplicationSpec,
    Config,
    GpuSpec,
    GatewaySpec,
    SettingsSpec,
)
from gpusybio.server import Server

HERE = os.path.dirname(__file__)
MOCK = os.path.join(HERE, "mock_gpu_app.py")
PY = sys.executable

A1_PORT, A2_PORT = 9501, 9502
A1_GW, A2_GW = 9511, 9512
ADMIN_PORT = 9530


def _cfg() -> Config:
    tmp = f"/tmp/gpusybio-e2e-{os.getpid()}.db"
    if os.path.exists(tmp):
        os.unlink(tmp)
    for f in ("/tmp/gpusybio-a1.log", "/tmp/gpusybio-a2.log", "/tmp/gpusybio-a1.pid", "/tmp/gpusybio-a2.pid"):
        if os.path.exists(f):
            os.unlink(f)

    def app_spec(app_id, name, port, pidfile, log, loaded):
        return ApplicationSpec(
            id=app_id,
            name=name,
            start_script=(
                f"nohup {PY} {MOCK} --port {port} --name {name}"
                + (" --start-loaded" if loaded else "")
                + f" >{log} 2>&1 & echo $! > {pidfile}; sleep 0.2; echo $(cat {pidfile})"
            ),
            stop_script=f"kill $(cat {pidfile}) 2>/dev/null; sleep 0.3; true",
            load_script=f"curl -s -X POST http://127.0.0.1:{port}/load",
            unload_script=f"curl -s -X POST http://127.0.0.1:{port}/unload",
            port=port,
            health_check_url=f"http://127.0.0.1:{port}/health",
            ready_check_url=f"http://127.0.0.1:{port}/ready",
            gpu_memory_mb=18432 if app_id == "a1" else 12288,
        )

    return Config(
        db_path=tmp,
        admin_host="127.0.0.1",
        admin_port=ADMIN_PORT,
        applications=[
            app_spec("a1", "qwen38", A1_PORT, "/tmp/gpusybio-a1.pid", "/tmp/gpusybio-a1.log", loaded=True),
            app_spec("a2", "llama31", A2_PORT, "/tmp/gpusybio-a2.pid", "/tmp/gpusybio-a2.log", loaded=True),
        ],
        # Both apps load their model at process start (llama-server style);
        # load/unload scripts remain available for the reload cycle.
        gateways=[
            GatewaySpec(id="gw-a1", name="gw-a1", port=A1_GW, application_id="a1", priority=10),
            GatewaySpec(id="gw-a2", name="gw-a2", port=A2_GW, application_id="a2", priority=5),
        ],
        settings=SettingsSpec(gpu=GpuSpec(backend="fake", fake_total_mb=24576,
                                          memory_reserve_mb=2048, memory_safety_margin=0.10)),
    )


async def _wait(fn, timeout=30.0, interval=0.2):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if await fn():
            return
        await asyncio.sleep(interval)
    raise AssertionError("timeout waiting for condition")


@pytest.fixture
async def server():
    srv = Server(_cfg())
    task = asyncio.create_task(srv.serve())
    await _wait(lambda: _port_up(ADMIN_PORT), timeout=10)
    yield srv
    await srv.stop()
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
    # Defensive: no mock app process may survive the test.
    await asyncio.sleep(0.3)
    _kill_mocks()


def _kill_mocks() -> None:
    import subprocess

    try:
        out = subprocess.run(
            ["pgrep", "-f", "mock_gpu_app.py"], capture_output=True, text=True
        ).stdout.split()
        for pid in out:
            try:
                os.kill(int(pid), 9)
            except (OSError, ValueError):
                pass
    except Exception:  # noqa: BLE001
        pass


async def _port_up(port: int) -> bool:
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.3)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


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

        # 5. Scheduler decision log shows a switch happened.
        events = (await c.get(f"{admin}/api/events?limit=200")).json()
        switches = [e for e in events if e.get("decision") == "switch"]
        assert switches, events


async def test_zero_cost_resident(server):
    """Request to a resident app's gateway forwards immediately (zero cost)."""
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


async def _app_state(c, app_id):
    r = await c.get(f"http://127.0.0.1:{ADMIN_PORT}/api/applications/{app_id}")
    d = r.json()
    return d


async def _stream(c, url, payload):
    text = ""
    async with c.stream("POST", url, json=payload, timeout=60.0) as r:
        assert r.status_code == 200
        async for chunk in r.aiter_text():
            text += chunk
    return text


async def _req_done(c, status, gw=None):
    url = f"http://127.0.0.1:{ADMIN_PORT}/api/requests?status={status}"
    if gw:
        url += f"&gateway_id={gw}"
    return len((await c.get(url)).json()) >= 1
