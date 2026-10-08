"""E2E: a ``shell-command`` app's ``start_script`` is the per-request template.

The command lives in the ``start_script`` field: the framework does NOT execute
``start_script`` as a server — it launches the built-in command server (which
runs the template) and passes the start_script through as
``GPUSYB_COMMAND_TEMPLATE``. The command runs on POST only (other methods are
405).
"""

from __future__ import annotations

import httpx
import pytest

from conftest import A1_GW, A1_PORT, A2_PORT, _app_state, _wait, build_app_spec
from gpusybio.config.models import ApplicationSpec


def _shell_command_spec() -> ApplicationSpec:
    return ApplicationSpec(
        id="a1",
        name="shellcmd",
        port=A1_PORT,
        application_type="shell-command",
        # The start_script IS the per-request template.
        start_script="echo $name",
        health_check_url=f"http://127.0.0.1:{A1_PORT}/health",
        gpu_memory_mb=0,  # CPU-only command — no VRAM requirement
    )


@pytest.fixture
def cfg_overrides():
    return {
        "applications": [
            _shell_command_spec(),
            build_app_spec("a2", "llama31", A2_PORT),  # stays STOPPED (unused)
        ]
    }


async def test_shell_command_start_script_used_as_template(server):
    """A stopped shell-command app whose command lives in start_script is woken
    on demand by a POST request: the framework launches the built-in command
    server (the start script is NOT run as a server) and runs it per request."""
    async with httpx.AsyncClient() as c:
        # The app starts stopped (nothing ran yet).
        d = await _app_state(c, "a1")
        assert d["process_state"] == "STOPPED"

        # A request through the gateway: the scheduler wakes the app (the 0-VRAM
        # app is zero-cost, so it always fits), the command server starts, the
        # start_script template runs, and the JSON result returns.
        r = await c.post(f"http://127.0.0.1:{A1_GW}/?name=hello", timeout=60.0)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["exit_code"] == 0
        assert data["stdout"].startswith("hello")
        assert data["timed_out"] is False

        # The app is now STARTED (started on demand, with the built-in server).
        async def a1_running():
            st = await _app_state(c, "a1")
            return st["process_state"] == "STARTED"

        await _wait(a1_running, timeout=15)
        st = await _app_state(c, "a1")
        assert st["process_state"] == "STARTED"
        assert st["residency_state"] == "LOADED"


async def test_shell_command_start_script_missing_param_is_400(server):
    """The substitution contract holds: a referenced param absent from the query
    is rejected with 400 (not a command run) — same as the explicit template."""
    async with httpx.AsyncClient() as c:
        # Wake the app with a valid request first.
        r0 = await c.post(f"http://127.0.0.1:{A1_GW}/?name=wake", timeout=60.0)
        assert r0.status_code == 200
        # A request missing the required param is rejected.
        r = await c.post(f"http://127.0.0.1:{A1_GW}/", timeout=60.0)
        assert r.status_code == 400
        assert "missing param" in r.json()["error"]
