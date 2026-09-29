"""Script execution contract (spec 03 §2.5).

Controlled environment (GPUSYB_APP_ID / GPUSYB_PORT / GPUSYB_PID), working
directory, timeout, captured stdout/stderr, recorded exit code. Failures are
surfaced to the caller (which emits application events).
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass


@dataclass
class ScriptResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool


@dataclass
class StartResult:
    """Result of a start script (spec 03 §2.5).

    ``exit_code`` is ``None`` while the script is still running — an *attached*
    script (``exec app``) never exits, so it must not be killed on timeout.
    ``pid`` is the resolved application PID: the pid file's contents if
    non-empty, else the PID of the spawned script (``proc.pid``).
    """

    exit_code: int | None
    pid: int
    proc: asyncio.subprocess.Process | None
    error: str | None = None


async def run_script(
    script: str,
    *,
    app_id: str,
    port: int | None = None,
    pid: int | None = None,
    cwd: str | None = None,
    timeout: float = 60.0,
    extra_env: dict[str, str] | None = None,
    data_path: str | None = None,
    log_path: str | None = None,
    pid_path: str | None = None,
) -> ScriptResult:
    """Execute a lifecycle script (POSIX shell). Returns a ScriptResult.

    ``GPUSYB_LOG``/``GPUSYB_PID_FILE`` point at the effective paths: the
    explicit ``log_path``/``pid_path`` if given, else the id-based default
    derived from ``data_path`` (spec 03 §2.5). ``GPUSYB_DATA_PATH`` is set when
    ``data_path`` is given.
    """
    env = {
        **os.environ,
        "GPUSYB_APP_ID": app_id,
        "GPUSYB_PORT": str(port) if port is not None else "",
        "GPUSYB_PID": str(pid) if pid is not None else "",
    }
    if data_path:
        env["GPUSYB_DATA_PATH"] = data_path
    if log_path:
        env["GPUSYB_LOG"] = log_path
    elif data_path:
        env["GPUSYB_LOG"] = os.path.join(data_path, "logs", f"{app_id}.log")
    if pid_path:
        env["GPUSYB_PID_FILE"] = pid_path
    elif data_path:
        env["GPUSYB_PID_FILE"] = os.path.join(data_path, "pids", f"{app_id}.pid")
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


def _read_pid_file(path: str) -> int | None:
    """Read an application PID from a pid file (spec 03 §2.5).

    The first all-digit line wins (pid files are conventionally a single line);
    returns ``None`` when the file is missing, empty, or has no numeric line.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.isdigit():
                    return int(line)
    except OSError:
        return None
    return None


def _pid_file_has_content(path: str) -> bool:
    try:
        return bool(open(path, "r", encoding="utf-8").read().strip())
    except OSError:
        return False


async def run_start_script(
    script: str,
    *,
    app_id: str,
    port: int | None = None,
    log_path: str,
    pid_path: str,
    data_path: str | None = None,
    cwd: str | None = None,
    grace: float = 15.0,
    extra_env: dict[str, str] | None = None,
) -> StartResult:
    """Run a start script (spec 03 §2.5) and resolve the application PID.

    The script's stdout **and** stderr go to ``log_path`` (created, truncated —
    the app's log is fresh on every start, so the tailer's session fingerprint
    detects the restart). ``GPUSYB_LOG``/``GPUSYB_PID_FILE`` point at the
    effective paths. The PID is read from ``pid_path`` (the script's job),
    falling back to the PID of the spawned script.

    Unlike :func:`run_script`, the script is **never killed on timeout**: an
    *attached* script (``exec app``) becomes the application and never exits.
    We wait up to ``grace`` for the script to exit OR the pid file to be
    written, then resolve the PID. Detached scripts exit in milliseconds;
    attached scripts hit the grace and resolve to ``proc.pid``.
    """
    env = {
        **os.environ,
        "GPUSYB_APP_ID": app_id,
        "GPUSYB_PORT": str(port) if port is not None else "",
        "GPUSYB_LOG": log_path,
        "GPUSYB_PID_FILE": pid_path,
    }
    if data_path:
        env["GPUSYB_DATA_PATH"] = data_path
    if extra_env:
        env.update(extra_env)

    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        os.makedirs(os.path.dirname(pid_path), exist_ok=True)
        log_fh = open(log_path, "w", encoding="utf-8")
        proc = await asyncio.create_subprocess_shell(
            script,
            cwd=cwd,
            env=env,
            stdout=log_fh,
            stderr=log_fh,
        )
    except (OSError, ValueError) as e:
        return StartResult(127, 0, None, f"spawn failed: {e}")  # type: ignore[arg-type]

    t0 = time.monotonic()
    while time.monotonic() - t0 < grace:
        if _pid_file_has_content(pid_path):
            break
        if proc.returncode is not None:  # script exited (detached)
            break
        await asyncio.sleep(0.1)

    pid = _read_pid_file(pid_path) or proc.pid
    return StartResult(proc.returncode, pid, proc)


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
