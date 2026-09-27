"""Unload semantics are driven by the UNLOAD script (not the load script).

- An app with an **unload script** is unloadable: ``supports_load_unload`` is
  True even when there is no load script, and ``POST /api/applications/{id}/
  unload`` is accepted. An unloaded app with no load script resumes serving
  immediately (no load step).
- An app with **no unload script** cannot be unloaded: the flag is False and the
  unload endpoint is rejected (400) — it must be stopped instead.

Unit coverage of the idle-unloader routing lives in ``test_idle.py``
(``test_idle_app_without_load_unload_is_stopped``).
"""

from __future__ import annotations

import asyncio
import os

import httpx
import pytest

from conftest import ADMIN_PORT, A1_PORT, A2_PORT, A1_GW, A2_GW, MOCK, PY, build_app_spec
from gpusybio.config.models import ApplicationSpec

A3_PORT = int(os.environ.get("GSYBIO_A3_PORT", 9503))
A4_PORT = int(os.environ.get("GSYBIO_A4_PORT", 9504))


def _a3_spec() -> ApplicationSpec:
    """A mock app with NO load/unload scripts — it cannot be unloaded, only stopped."""
    pidfile = "/tmp/gpusybio-a3.pid"
    log = "/tmp/gpusybio-a3.log"
    return ApplicationSpec(
        id="a3",
        name="plain38",
        start_script=(
            f"nohup {PY} {MOCK} --port {A3_PORT} --name plain38"
            f" >{log} 2>&1 & echo $! > {pidfile}; sleep 0.2; echo $(cat {pidfile})"
        ),
        stop_script=f"kill $(cat {pidfile}) 2>/dev/null; sleep 0.3; true",
        load_script=None,  # no load/unload scripts -> supports_load_unload = False
        unload_script=None,
        port=A3_PORT,
        health_check_url=f"http://127.0.0.1:{A3_PORT}/health",
        ready_check_url=f"http://127.0.0.1:{A3_PORT}/ready",
        gpu_memory_mb=4096,
    )


def _a4_spec() -> ApplicationSpec:
    """A mock app with an UNLOAD script but NO load script — unloadable, and a
    reload is a no-op that serves immediately (no load step)."""
    pidfile = "/tmp/gpusybio-a4.pid"
    log = "/tmp/gpusybio-a4.log"
    return ApplicationSpec(
        id="a4",
        name="unloadonly",
        start_script=(
            f"nohup {PY} {MOCK} --port {A4_PORT} --name unloadonly"
            f" >{log} 2>&1 & echo $! > {pidfile}; sleep 0.2; echo $(cat {pidfile})"
        ),
        stop_script=f"kill $(cat {pidfile}) 2>/dev/null; sleep 0.3; true",
        load_script=None,  # no load script, but ...
        unload_script=f"curl -s -X POST http://127.0.0.1:{A4_PORT}/unload",  # ... has an unload script
        port=A4_PORT,
        health_check_url=f"http://127.0.0.1:{A4_PORT}/health",
        # Point readiness at /health (always up), not /ready (503 while the
        # model is "unloaded"): a4 has no load script, so it must be able to
        # become ready/serving without a load step.
        ready_check_url=f"http://127.0.0.1:{A4_PORT}/health",
        gpu_memory_mb=4096,
    )


@pytest.fixture
def cfg_overrides():
    return {
        "applications": [
            build_app_spec("a1", "qwen38", A1_PORT, loaded=True),
            build_app_spec("a2", "llama31", A2_PORT, loaded=True),
            _a3_spec(),
            _a4_spec(),
        ]
    }


async def test_no_unload_script_cannot_be_unloaded(server):
    c = httpx.AsyncClient()
    await asyncio.sleep(1.0)
    # a3 has no unload script -> supports_load_unload is False.
    r = await c.get(f"http://127.0.0.1:{ADMIN_PORT}/api/applications/a3")
    assert r.status_code == 200
    assert r.json()["supports_load_unload"] is False
    # It cannot be unloaded — the endpoint refuses and tells us to stop instead.
    r = await c.post(f"http://127.0.0.1:{ADMIN_PORT}/api/applications/a3/unload")
    assert r.status_code == 400
    assert "unload" in r.json()["detail"].lower() or "stop" in r.json()["detail"].lower()
    await c.aclose()


async def test_unload_script_makes_app_unloadable(server):
    """An app with only an unload script (no load script) is unloadable, and a
    reload with no load script serves immediately."""
    # start/load/unload block until the transition completes; give them room.
    c = httpx.AsyncClient(timeout=30.0)
    await asyncio.sleep(1.0)
    # a4 has an unload script (no load script) -> supports_load_unload is True.
    r = await c.get(f"http://127.0.0.1:{ADMIN_PORT}/api/applications/a4")
    assert r.status_code == 200
    v = r.json()
    assert v["supports_load_unload"] is True
    assert v["load_script"] is None
    assert v["unload_script"] is not None

    # Start a4 (running, not loaded).
    r = await c.post(f"http://127.0.0.1:{ADMIN_PORT}/api/applications/a4/start")
    assert r.status_code == 202, r.text
    await asyncio.sleep(1.5)
    assert (await c.get(f"http://127.0.0.1:{ADMIN_PORT}/api/applications/a4")).json()["process_state"] == "RUNNING"

    # Load a4: no load script -> it becomes LOADED and serves immediately.
    r = await c.post(f"http://127.0.0.1:{ADMIN_PORT}/api/applications/a4/load")
    assert r.status_code == 202, r.text
    await asyncio.sleep(1.5)
    assert (await c.get(f"http://127.0.0.1:{ADMIN_PORT}/api/applications/a4")).json()["residency_state"] == "LOADED"

    # Unload a4: it has an unload script -> the endpoint accepts it (not 400).
    r = await c.post(f"http://127.0.0.1:{ADMIN_PORT}/api/applications/a4/unload")
    assert r.status_code == 202, r.text
    await asyncio.sleep(2.0)
    assert (await c.get(f"http://127.0.0.1:{ADMIN_PORT}/api/applications/a4")).json()["residency_state"] == "OFF"
    await c.aclose()
