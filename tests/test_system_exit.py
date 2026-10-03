"""System exit behaviors.

1. Ctrl-C (SIGINT) asks whether to stop all started applications — 5s
   timeout, default YES. Yes → clean shutdown (apps stopped); no → the
   gateway exits, leaving the started applications running.
2. On startup, an application whose recorded PID is alive AND whose port
   is in use is re-adopted as RUNNING (it survived a gateway restart);
   mismatched cases stay STOPPED.
"""

from __future__ import annotations

import asyncio
import io
import os
import socket
import subprocess
import sys
import time

import pytest

from gpusybio.applications.lifecycle import ProcessState, ResidencyState
from gpusybio.config.models import ApplicationSpec, Config, GpuSpec, SettingsSpec
from gpusybio.server import Server


def _make_server(tmp_path, port: int, app_id: str = "a1", pid_file: str | None = None,
                 unload_script: str | None = None) -> Server:
    cfg = Config(
        db_path=str(tmp_path / "sys.db"),
        data_path=str(tmp_path / "data"),
        admin_host="127.0.0.1",
        admin_port=9999,
        applications=[
            ApplicationSpec(
                id=app_id, name=app_id, start_script="echo a1",
                port=port, gpu_memory_mb=1024, pid_file=pid_file,
                unload_script=unload_script,
            ),
        ],
        settings=SettingsSpec(gpu=GpuSpec(backend="fake", fake_total_mb=24576)),
    )
    return Server(cfg)


def _spawn_sleep() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])


def _bind(port: int) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", port))
    s.listen(1)
    return s


def _write_default_pid(data_path: str, app_id: str, pid: int) -> None:
    pid_dir = os.path.join(data_path, "pids")
    os.makedirs(pid_dir, exist_ok=True)
    with open(os.path.join(pid_dir, f"{app_id}.pid"), "w") as f:
        f.write(str(pid))


# -- startup re-adoption --------------------------------------------------------

async def test_readopt_surviving_app_at_startup(tmp_path):
    """Recorded PID alive + port in use → the app is RUNNING again."""
    srv = _make_server(tmp_path, 9041)
    data = str(tmp_path / "data")
    proc = _spawn_sleep()
    srv_sock = _bind(9041)
    _write_default_pid(data, "a1", proc.pid)
    try:
        srv._reconcile()
        s = srv.states.get("a1")
        assert s.process is ProcessState.RUNNING
        assert s.pid == proc.pid
        assert srv.resources._pids.get("a1") == proc.pid
    finally:
        proc.kill()
        srv_sock.close()
        srv.engine.dispose()


async def test_readopt_via_configured_pid_file(tmp_path):
    """The app's configured ``pid_file`` (not just the default) is honored."""
    pid_path = tmp_path / "custom" / "a1.pid"
    srv = _make_server(tmp_path, 9042, pid_file=str(pid_path))
    assert srv.apps.pid_path("a1") == str(pid_path)
    proc = _spawn_sleep()
    srv_sock = _bind(9042)
    pid_path.parent.mkdir(parents=True)
    pid_path.write_text(str(proc.pid))
    try:
        srv._reconcile()
        s = srv.states.get("a1")
        assert s.process is ProcessState.RUNNING
        assert s.pid == proc.pid
    finally:
        proc.kill()
        srv_sock.close()
        srv.engine.dispose()


async def test_no_readopt_dead_pid_port_in_use(tmp_path):
    """Port held by someone without our recorded PID → stays STOPPED."""
    srv = _make_server(tmp_path, 9043)
    srv_sock = _bind(9043)
    try:
        srv._reconcile()  # no pid file at all
        s = srv.states.get("a1")
        assert s.process is ProcessState.STOPPED
        assert s.pid is None
    finally:
        srv_sock.close()
        srv.engine.dispose()


async def test_no_readopt_pid_alive_port_free(tmp_path):
    """PID alive but port not in use → stays STOPPED (both must hold)."""
    srv = _make_server(tmp_path, 9044)
    data = str(tmp_path / "data")
    proc = _spawn_sleep()
    _write_default_pid(data, "a1", proc.pid)
    try:
        srv._reconcile()
        s = srv.states.get("a1")
        assert s.process is ProcessState.STOPPED
        assert s.pid is None
    finally:
        proc.kill()
        srv.engine.dispose()


async def test_readopt_standalone_server_is_loaded(tmp_path):
    """A re-adopted standalone server (no load/unload — the process IS the
    model) is re-adopted LOADED. Its memory monitor is disabled (no threshold),
    so without this nothing would ever set residency and a request would sit
    queued waiting for a load that never comes (the scheduler needs LOADED to
    forward)."""
    srv = _make_server(tmp_path, 9052)
    data = str(tmp_path / "data")
    proc = _spawn_sleep()
    srv_sock = _bind(9052)
    _write_default_pid(data, "a1", proc.pid)
    try:
        srv._reconcile()
        s = srv.states.get("a1")
        assert s.process is ProcessState.RUNNING
        assert s.residency is ResidencyState.LOADED
        assert s.loaded_at is not None
    finally:
        proc.kill()
        srv_sock.close()
        srv.engine.dispose()


async def test_readopt_lazy_loading_app_stays_off(tmp_path):
    """A re-adopted lazy-loading app (has an unload script) is NOT assumed
    loaded — it may have been unloaded before the restart. Residency stays
    OFF and the residency monitor reconciles it on its next tick."""
    srv = _make_server(tmp_path, 9053, unload_script="echo unload")
    data = str(tmp_path / "data")
    proc = _spawn_sleep()
    srv_sock = _bind(9053)
    _write_default_pid(data, "a1", proc.pid)
    try:
        srv._reconcile()
        s = srv.states.get("a1")
        assert s.process is ProcessState.RUNNING
        assert s.residency is ResidencyState.OFF
    finally:
        proc.kill()
        srv_sock.close()
        srv.engine.dispose()


# -- Ctrl-C confirmation (5s timeout, default yes) ------------------------------

async def test_confirm_stop_default_yes_on_eof(tmp_path, monkeypatch):
    """Non-interactive stdin (EOF) → the default is YES."""
    srv = _make_server(tmp_path, 9045)
    try:
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))  # input() -> EOFError
        assert await srv._confirm_stop(["a1"], timeout_sec=1.0) is True
    finally:
        srv.engine.dispose()


async def test_confirm_stop_answers(tmp_path, monkeypatch):
    srv = _make_server(tmp_path, 9046)
    try:
        for answer, expected in (
            ("y", True), ("yes", True), ("", True),
            ("n", False), ("no", False),
        ):
            monkeypatch.setattr(sys, "stdin", io.StringIO(f"{answer}\n"))
            assert await srv._confirm_stop(["a1"]) is expected
    finally:
        srv.engine.dispose()


async def test_confirm_stop_timeout_defaults_yes(tmp_path, monkeypatch):
    """No answer within the timeout → the default is YES."""
    srv = _make_server(tmp_path, 9047)
    try:
        def slow_input(prompt: str = "") -> str:
            time.sleep(1.0)
            return "n"  # would decline — but it never arrives in time

        monkeypatch.setattr("builtins.input", slow_input)
        assert await srv._confirm_stop(["a1"], timeout_sec=0.2) is True
    finally:
        srv.engine.dispose()


# -- Ctrl-C end-to-end -----------------------------------------------------------

async def test_ctrl_c_no_started_apps_shuts_down_without_prompt(tmp_path, monkeypatch):
    """Nothing started → no prompt, clean shutdown, exit code 0."""
    srv = _make_server(tmp_path, 9048)
    try:
        def boom(*a, **k):
            raise AssertionError("prompt must not be asked when nothing is started")

        def hard_exit(code: int):
            assert code == 0
            raise SystemExit(code)

        monkeypatch.setattr(srv, "_confirm_stop", boom)
        monkeypatch.setattr(os, "_exit", hard_exit)
        with pytest.raises(SystemExit):
            await srv._on_sigint()
        assert srv._stopped is True
    finally:
        srv.engine.dispose()


async def test_ctrl_c_declined_leaves_apps_running(tmp_path, monkeypatch):
    """Answer 'n' → the gateway exits, the started app stays RUNNING."""
    srv = _make_server(tmp_path, 9049)
    try:
        srv.states.set_process("a1", ProcessState.RUNNING)
        monkeypatch.setattr(sys, "stdin", io.StringIO("n\n"))

        def hard_exit(code: int):
            raise SystemExit(code)

        monkeypatch.setattr(os, "_exit", hard_exit)
        with pytest.raises(SystemExit):
            await srv._on_sigint()
        assert srv._stopped is False  # no clean shutdown ran
        assert srv.states.get("a1").process is ProcessState.RUNNING
    finally:
        srv.engine.dispose()


async def test_ctrl_c_accepted_stops_apps(tmp_path, monkeypatch):
    """Answer 'y' → clean shutdown, the app is stopped, exit code 0."""
    srv = _make_server(tmp_path, 9050)
    try:
        srv.states.set_process("a1", ProcessState.RUNNING)
        monkeypatch.setattr(sys, "stdin", io.StringIO("y\n"))

        def hard_exit(code: int):
            assert code == 0
            raise SystemExit(code)

        monkeypatch.setattr(os, "_exit", hard_exit)
        with pytest.raises(SystemExit):
            await srv._on_sigint()
        assert srv._stopped is True
        assert srv.states.get("a1").process is ProcessState.STOPPED
    finally:
        srv.engine.dispose()


async def test_serve_gather_survives_concurrent_task_cancellation():
    """Regression: on the exit path, stop() force-cancels the listener tasks
    while serve() is still awaiting gather(*tasks). serve()'s gather (no
    return_exceptions) used to re-raise that CancelledError up to asyncio.run,
    printing a traceback. It must now swallow it and return cleanly.

    This reproduces the two concurrent drivers on the same task: serve()'s
    guarded gather and stop()'s cancel+reap.
    """
    async def hang() -> None:  # stands in for a uvicorn server task
        await asyncio.sleep(3600)

    task = asyncio.create_task(hang())

    async def serve_gather() -> None:
        # The exact pattern in Server.serve().
        try:
            await asyncio.gather(task)
        except asyncio.CancelledError:
            pass

    async def stop_cancel() -> None:
        # The tail of Server.stop().
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    serve_task = asyncio.create_task(serve_gather())
    await asyncio.sleep(0.02)  # let serve_gather reach its gather await
    await stop_cancel()
    await serve_task  # completes cleanly; raises CancelledError if the guard is missing
