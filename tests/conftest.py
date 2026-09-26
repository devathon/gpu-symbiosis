"""Shared E2E harness (spec 01 §3 core loop).

Fake 24 GB GPU, two mock GPU apps (a1 "qwen38" 18 GB, a2 "llama31" 12 GB),
admin API on :9530. The ``cfg_overrides`` fixture lets a test module set
scheduler knobs (``cfg_overrides["scheduler"] = SchedulerSpec(...)``) without
touching the shared config.
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time

import httpx
import pytest

from gpusybio.config.models import (
    ApplicationSpec,
    Config,
    GpuSpec,
    GatewaySpec,
    SchedulerSpec,
    SettingsSpec,
)
from gpusybio.server import Server

HERE = os.path.dirname(__file__)
MOCK = os.path.join(HERE, "mock_gpu_app.py")
PY = sys.executable

A1_PORT, A2_PORT = 9501, 9502
A1_GW, A2_GW = 9511, 9512
ADMIN_PORT = 9530


def make_config(scheduler: SchedulerSpec | None = None) -> Config:
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
        settings=SettingsSpec(
            gpu=GpuSpec(
                backend="fake",
                fake_total_mb=24576,
                memory_reserve_mb=2048,
                memory_safety_margin=0.10,
            ),
            scheduler=scheduler or SchedulerSpec(),
        ),
    )


async def _wait(fn, timeout=30.0, interval=0.2):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if await fn():
            return
        await asyncio.sleep(interval)
    raise AssertionError("timeout waiting for condition")


def _kill_mocks() -> None:
    try:
        out = subprocess.run(["pgrep", "-f", "mock_gpu_app.py"], capture_output=True, text=True).stdout.split()
        for pid in out:
            try:
                os.kill(int(pid), 9)
            except (OSError, ValueError):
                pass
    except Exception:  # noqa: BLE001
        pass


async def _port_up(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.3)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


@pytest.fixture
def cfg_overrides():
    """Test modules override this fixture to set scheduler knobs."""
    return {}


@pytest.fixture
async def server(cfg_overrides):
    scheduler = cfg_overrides.get("scheduler")
    srv = Server(make_config(scheduler))
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


# -- HTTP helpers ---------------------------------------------------------------
async def _app_state(c, app_id):
    r = await c.get(f"http://127.0.0.1:{ADMIN_PORT}/api/applications/{app_id}")
    return r.json()


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


async def _decisions(c):
    """All scheduler decisions (from /api/events), oldest first."""
    rows = (await c.get(f"http://127.0.0.1:{ADMIN_PORT}/api/events?limit=500")).json()
    sched = [r for r in rows if r.get("kind") == "scheduler"]
    sched.reverse()
    return sched
