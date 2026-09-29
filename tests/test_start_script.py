"""Start-script contract (spec 03 §2.5, spec 04 §4.1):

- The PID comes from the pid file (not the script's stdout); a start script
  such as ``systemctl start`` cannot be expected to print the PID.
- If the pid file is empty/missing, the PID falls back to the spawned script's
  PID (``proc.pid``) — correct for an *attached* script (``exec app``).
- The code never kills the start script on timeout, so both detached and
  attached scripts work.
- The script's stdout/stderr go to the effective log file.
- ``log_file``/``pid_file`` are optional per-app paths; the effective path is
  the configured value else the id-based default.
"""

from __future__ import annotations

import asyncio
import os

from gpusybio.config.models import Config
from gpusybio.applications.scripts import run_start_script


def _alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _kill(pid: int) -> None:
    if pid:
        try:
            os.kill(pid, 15)
        except OSError:
            pass


# -- PID resolution ----------------------------------------------------------

async def test_detached_pid_from_pid_file(tmp_path):
    """A detached script that writes the pid file: PID = the file's contents."""
    log = str(tmp_path / "a.log")
    pid = str(tmp_path / "a.pid")
    r = await run_start_script(
        f"sleep 30 >/dev/null 2>&1 & echo $! > {pid}",
        app_id="a1", log_path=log, pid_path=pid, grace=5.0,
    )
    assert r.error is None
    assert r.exit_code == 0
    with open(pid) as f:
        expected = int(f.read().strip())
    assert r.pid == expected
    assert _alive(r.pid)
    _kill(r.pid)


async def test_attached_pid_fallback_to_proc_pid(tmp_path):
    """An attached script (exec) with no pid file: PID = the spawned script."""
    log = str(tmp_path / "b.log")
    pid = str(tmp_path / "b.pid")  # never written
    r = await run_start_script(
        "exec sleep 30",
        app_id="b1", log_path=log, pid_path=pid, grace=2.0,
    )
    assert r.error is None
    assert r.exit_code is None  # still running (attached, not killed on timeout)
    assert _alive(r.pid)  # proc.pid is the app
    _kill(r.pid)


async def test_attached_blank_pidfile_is_written(tmp_path):
    """Issue: an attached script (exec) leaves the pid file blank — the system
    writes the resolved PID (``proc.pid``) so the file is the source of truth
    for stop/watchdog (spec 03 §2.5)."""
    log = str(tmp_path / "w.log")
    pid = str(tmp_path / "w.pid")  # never written by the script
    r = await run_start_script(
        "exec sleep 30",
        app_id="w1", log_path=log, pid_path=pid, grace=2.0,
    )
    assert r.exit_code is None
    assert _alive(r.pid)
    # The system wrote the resolved PID into the blank pid file.
    with open(pid) as f:
        assert int(f.read().strip()) == r.pid
    _kill(r.pid)


async def test_detached_exit_blank_pidfile_not_written(tmp_path):
    """A detached script that exits WITHOUT writing the pid file: ``proc.pid``
    is a dead shell, so the system must NOT write it (readiness/watchdog flag
    the dead pid). The PID still resolves to ``proc.pid`` for reporting."""
    log = str(tmp_path / "x.log")
    pid = str(tmp_path / "x.pid")  # never written by the script
    r = await run_start_script(
        "true",  # exits immediately, writes nothing
        app_id="x1", log_path=log, pid_path=pid, grace=2.0,
    )
    assert r.exit_code == 0
    # The dead-shell PID is NOT persisted into the pid file.
    assert not os.path.exists(pid) or not open(pid).read().strip()


async def test_stale_pidfile_not_read(tmp_path):
    """Issue: a pid file left over from a *previous* run holds a stale PID.
    The code must not read it — it waits for the current start script to
    (re)write the file (fresh mtime) (spec 03 §2.5)."""
    import time as _time
    log = str(tmp_path / "s.log")
    pid = str(tmp_path / "s.pid")
    # Pre-seed a stale pid file (from a previous run): content + old mtime.
    with open(pid, "w") as f:
        f.write("99999\n")
    old = _time.time() - 120  # 2 minutes ago (well before this start)
    os.utime(pid, (old, old))
    # The start script overwrites the pid file with the real app PID.
    r = await run_start_script(
        f"sleep 30 >/dev/null 2>&1 & echo $! > {pid}",
        app_id="s1", log_path=log, pid_path=pid, grace=5.0,
    )
    assert r.error is None
    with open(pid) as f:
        expected = int(f.read().strip())
    # The resolved PID is the CURRENT run's app, not the stale 99999.
    assert r.pid == expected
    assert r.pid != 99999
    assert _alive(r.pid)
    _kill(r.pid)


async def test_attached_stdout_lands_in_log(tmp_path):
    """An attached script's stdout/stderr are captured in the effective log."""
    log = str(tmp_path / "c.log")
    pid = str(tmp_path / "c.pid")
    r = await run_start_script(
        'exec sh -c "echo STARTED-APP; echo err-line 1>&2; sleep 30"',
        app_id="c1", log_path=log, pid_path=pid, grace=2.0,
    )
    import time
    time.sleep(0.3)
    content = open(log).read()
    assert "STARTED-APP" in content
    assert "err-line" in content
    _kill(r.pid)


async def test_failing_detached_script_reports_exit_code(tmp_path):
    """A start script that exits non-zero is a failure (exit_code != 0)."""
    log = str(tmp_path / "d.log")
    pid = str(tmp_path / "d.pid")
    r = await run_start_script(
        "echo boom >/dev/null 2>&1; exit 3",
        app_id="d1", log_path=log, pid_path=pid, grace=5.0,
    )
    assert r.exit_code == 3


async def test_stdout_echo_alone_no_longer_resolves_pid(tmp_path):
    """The old contract (echo the PID to stdout, no pid file) no longer works:
    the PID falls back to the spawned shell, which is dead by the time we check.
    """
    log = str(tmp_path / "e.log")
    pid = str(tmp_path / "e.pid")
    r = await run_start_script(
        "sleep 0.1 & p=$!; sleep 0.2; kill $p 2>/dev/null; echo $p",
        app_id="e1", log_path=log, pid_path=pid, grace=5.0,
    )
    assert r.exit_code == 0
    # No pid file was written, so the PID is the spawned shell's — not the
    # backgrounded process's (which the script killed). It is not usable.
    assert not _alive(r.pid)


# -- backward compatibility --------------------------------------------------

async def test_old_style_script_still_works(tmp_path):
    """The pre-rework start script (redirect + pid file + redundant stdout
    echo) still starts correctly — the stdout echo is simply ignored.
    """
    log = str(tmp_path / "f.log")
    pid = str(tmp_path / "f.pid")
    r = await run_start_script(
        f"sleep 30 > {log} 2>&1 & echo $! > {pid}; sleep 0.2; echo $(cat {pid})",
        app_id="f1", log_path=log, pid_path=pid, grace=5.0,
    )
    # The PID is resolved from the pid file (the script may still be running —
    # exit_code None — which the readiness check handles, not the exit code).
    with open(pid) as f:
        assert r.pid == int(f.read().strip())
    assert _alive(r.pid)
    _kill(r.pid)


# -- effective path resolution ----------------------------------------------

def test_effective_path_helpers_default():
    cfg = Config(data_path="/tmp/d")
    assert cfg.effective_log_path("a1") == "/tmp/d/logs/a1.log"
    assert cfg.effective_pid_path("a1") == "/tmp/d/pids/a1.pid"


def test_effective_path_helpers_override():
    cfg = Config(data_path="/tmp/d")
    assert cfg.effective_log_path("a1", "/custom/app.log") == "/custom/app.log"
    assert cfg.effective_pid_path("a1", "/custom/app.pid") == "/custom/app.pid"


async def test_run_start_script_uses_explicit_paths(tmp_path):
    """The script sees the effective (custom) log/pid paths, not id-based ones."""
    log = str(tmp_path / "custom.log")
    pid = str(tmp_path / "custom.pid")
    r = await run_start_script(
        f"sleep 30 >/dev/null 2>&1 & echo $! > {pid}",
        app_id="z1", log_path=log, pid_path=pid, grace=5.0,
    )
    assert r.exit_code == 0
    assert os.path.exists(pid)  # written to the custom path
    with open(pid) as f:
        assert r.pid == int(f.read().strip())
    _kill(r.pid)
