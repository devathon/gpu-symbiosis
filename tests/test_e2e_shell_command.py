"""E2E: the ``shell-command`` application type runs the full framework loop.

A stopped ``shell-command`` app (no start script - the framework launches the
built-in command server) is woken on demand by a POST request through its
gateway: the VRAM check fits, the command server starts, the request is
forwarded and the command runs, and the JSON result is returned. The app is then
STARTED (started with no operator script) and goes through the regular idle
lifecycle. (The command runs on POST only; other methods are 405.)
"""

from __future__ import annotations

import httpx
import pytest

from conftest import A1_GW, A1_PORT, A2_PORT, ADMIN_PORT, _app_state, _wait, build_app_spec
from gpusybio.config.models import ApplicationSpec


def _shell_command_spec() -> ApplicationSpec:
    return ApplicationSpec(
        id="a1",
        name="shellcmd",
        port=A1_PORT,
        application_type="shell-command",
        start_script="echo $name",  # start_script is the per-request template
        health_check_url=f"http://127.0.0.1:{A1_PORT}/health",
        gpu_memory_mb=1024,
    )


@pytest.fixture
def cfg_overrides():
    return {
        "applications": [
            _shell_command_spec(),
            build_app_spec("a2", "llama31", A2_PORT),  # stays STOPPED (unused)
        ]
    }


async def test_shell_command_woken_on_demand_and_runs_command(server):
    """A stopped shell-command app is started on demand (no operator script) and
    the request's command runs through its gateway."""
    admin = f"http://127.0.0.1:{ADMIN_PORT}"
    async with httpx.AsyncClient() as c:
        # The app starts stopped (nothing ran yet - no script was executed).
        d = await _app_state(c, "a1")
        assert d["process_state"] == "STOPPED"

        # A request through the gateway: the scheduler wakes the app (VRAM check
        # fits), the command server starts, the command runs, the result returns.
        r = await c.post(f"http://127.0.0.1:{A1_GW}/?name=hello", timeout=60.0)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["exit_code"] == 0
        assert data["stdout"].startswith("hello")
        assert data["timed_out"] is False

        # The app is now STARTED (started on demand, with no operator script).
        async def a1_running():
            st = await _app_state(c, "a1")
            return st["process_state"] == "STARTED"

        await _wait(a1_running, timeout=15)
        st = await _app_state(c, "a1")
        assert st["process_state"] == "STARTED"
        assert st["residency_state"] == "LOADED"

        # No unload script -> stop-based lifecycle (spec: supports_load_unload is
        # derived from unload_script). Its idle-unload is a plain stop (the
        # idle-unloader routing is unit-covered in test_idle.py), so the app is
        # subject to the regular idle/timeout lifecycle, not an unload.
        assert st["supports_load_unload"] is False


async def test_shell_command_second_request_reuses_running_app(server):
    """After the first request started the app, a second request is served by the
    same running command server (no re-start)."""
    admin = f"http://127.0.0.1:{ADMIN_PORT}"
    async with httpx.AsyncClient() as c:
        r1 = await c.post(f"http://127.0.0.1:{A1_GW}/?name=first", timeout=60.0)
        assert r1.status_code == 200
        assert r1.json()["stdout"].startswith("first")

        # The app is running now; the second request is served directly.
        r2 = await c.post(f"http://127.0.0.1:{A1_GW}/?name=second", timeout=60.0)
        assert r2.status_code == 200
        assert r2.json()["stdout"].startswith("second")

        st = await _app_state(c, "a1")
        assert st["process_state"] == "STARTED"


async def test_shell_command_missing_param_is_400_through_gateway(server):
    """The substitution contract holds end-to-end: a referenced param that is
    absent from the query is rejected with 400 (not a command run)."""
    admin = f"http://127.0.0.1:{ADMIN_PORT}"
    async with httpx.AsyncClient() as c:
        # Wake the app with a valid request first.
        r0 = await c.post(f"http://127.0.0.1:{A1_GW}/?name=wake", timeout=60.0)
        assert r0.status_code == 200
        # A request missing the required param is rejected.
        r = await c.post(f"http://127.0.0.1:{A1_GW}/", timeout=60.0)
        assert r.status_code == 400
        assert "missing param" in r.json()["error"]
