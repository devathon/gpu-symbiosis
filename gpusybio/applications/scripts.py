"""Script execution contract (spec 03 §2.5).

Controlled environment (GPUSYB_APP_ID / GPUSYB_PORT / GPUSYB_PID), working
directory, timeout, captured stdout/stderr, recorded exit code. Failures are
surfaced to the caller (which emits application events).
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass


@dataclass
class ScriptResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool


async def run_script(
    script: str,
    *,
    app_id: str,
    port: int | None = None,
    pid: int | None = None,
    cwd: str | None = None,
    timeout: float = 60.0,
    extra_env: dict[str, str] | None = None,
) -> ScriptResult:
    """Execute a lifecycle script (POSIX shell). Returns a ScriptResult."""
    env = {
        **os.environ,
        "GPUSYB_APP_ID": app_id,
        "GPUSYB_PORT": str(port) if port is not None else "",
        "GPUSYB_PID": str(pid) if pid is not None else "",
    }
    if extra_env:
        env.update(extra_env)

    try:
        proc = await asyncio.create_subprocess_shell(
            script,
            cwd=cwd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (OSError, ValueError) as e:
        return ScriptResult(127, "", f"spawn failed: {e}", False)

    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return ScriptResult(
            proc.returncode if proc.returncode is not None else -1,
            stdout_b.decode(errors="replace"),
            stderr_b.decode(errors="replace"),
            False,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return ScriptResult(124, "", f"timed out after {timeout}s", True)


async def graceful_stop(pid: int, timeout: float = 10.0) -> ScriptResult:
    """SIGTERM → wait → SIGKILL (spec 03 §2.2 default stop)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "kill", str(pid), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )
        await proc.wait()
    except ProcessLookupError:
        return ScriptResult(0, "", "process already gone", False)
    except OSError as e:
        return ScriptResult(1, "", str(e), False)

    for _ in range(int(timeout * 10)):
        if not _alive(pid):
            return ScriptResult(0, "", "stopped via SIGTERM", False)
        await asyncio.sleep(0.1)

    try:
        os.kill(pid, 9)
        return ScriptResult(0, "", "stopped via SIGKILL", False)
    except ProcessLookupError:
        return ScriptResult(0, "", "stopped via SIGTERM (late exit)", False)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
