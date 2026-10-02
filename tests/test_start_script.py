"""Start-script contract (spec 03 §2.5, spec 04 §4.1):

- The PID comes from the pid file (not the script's stdout); a start script
  such as ``systemctl start`` cannot be expected to print the PID.
- If the pid file is empty/missing, the PID falls back to the spawned script's
  PID (``proc.pid``) — correct for an *attached* script (``exec app``).
- A *detached* script's ``proc.pid`` is a dead shell, so the pid file's PID is
  adopted whenever it names a *living* process (freshly written, or a daemon
  that outlived the script — e.g. a ``systemctl start`` service); a dead value
  is stale either way.
- The code never kills the start script on timeout, so both detached and
  attached scripts work.
- The script's stdout/stderr go to the effective log file.
- ``log_file``/``pid_file`` are optional per-app paths; the effective path is
  the configured value else the id-based default.
"""

from __future__ import annotations

import asyncio
import os
import subprocess

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


async def test_attached_stale_pidfile_overwritten(tmp_path):
    """Issue: an *attached* script (no pid-file write) starts while a pid file
    left over from a *previous* run holds a stale PID (content + old mtime).
    The code must NOT read that stale value — it resolves to the app's PID
    (``proc.pid``) and writes it over the stale one (spec 03 §2.5)."""
    import time as _time
    log = str(tmp_path / "q.log")
    pid = str(tmp_path / "q.pid")
    # Pre-seed a stale pid file (from a previous run): content + old mtime.
    with open(pid, "w") as f:
        f.write("99999\n")
    old = _time.time() - 120  # 2 minutes ago (well before this start)
    os.utime(pid, (old, old))
    # The attached script never writes the pid file.
    r = await run_start_script(
        "exec sleep 30",
        app_id="q1", log_path=log, pid_path=pid, grace=2.0,
    )
    assert r.exit_code is None
    assert _alive(r.pid)
    # The resolved PID is the CURRENT run's app, not the stale 99999.
    assert r.pid != 99999
    # The system overwrote the stale pid file with the current app's PID.
    with open(pid) as f:
        assert int(f.read().strip()) == r.pid
    _kill(r.pid)


async def test_detached_adopts_live_pid_from_preexisting_file(tmp_path):
    """Issue: a *detached* script (``systemctl start``) exits without writing
    a fresh pid file, while the pid file holds the PID of an already-running
    daemon — a service is a child of PID 1, never of the script's shell, so
    the file (written by a previous start or the operator) is the only record
    of it. The living PID is adopted; the dead shell's ``proc.pid`` must not
    be tracked (it would fire a false PROCESS_DIED)."""
    import time as _time
    log = str(tmp_path / "o.log")
    pid = str(tmp_path / "o.pid")
    daemon = subprocess.Popen(["sleep", "30"])  # the outliving "service"
    try:
        # Pre-seed the pid file with the live daemon's PID, old mtime
        # (recorded before this start — the script does not rewrite it).
        with open(pid, "w") as f:
            f.write(f"{daemon.pid}\n")
        old = _time.time() - 120
        os.utime(pid, (old, old))
        r = await run_start_script(
            "true",  # detached no-op: exits immediately, writes nothing
            app_id="o1", log_path=log, pid_path=pid, grace=2.0,
        )
        assert r.error is None
        assert r.exit_code == 0
        assert r.pid == daemon.pid  # the living daemon, not the dead shell
        assert _alive(r.pid)
    finally:
        daemon.terminate()
        daemon.wait()


async def test_detached_zero_pidfile_not_adopted(tmp_path):
    """Issue: a *detached* script wrote ``0`` into the pid file (a systemd
    ``MainPID`` captured before the unit finished activating — ``systemctl
    show`` right after ``systemctl start``). ``os.kill(0, 0)`` probes the
    caller's own process group, so a naive liveness check reads 0 as "alive";
    the resolution must treat 0 as missing and fall back to ``proc.pid``
    (the dead shell — honestly flagged by the watchdog, not tracked forever).
    """
    log = str(tmp_path / "z.log")
    pid = str(tmp_path / "z.pid")
    r = await run_start_script(
        f"echo 0 > {pid}",  # detached: writes 0, exits
        app_id="z1", log_path=log, pid_path=pid, grace=2.0,
    )
    assert r.error is None
    assert r.exit_code == 0
    assert r.pid != 0
    assert not _alive(r.pid)  # the dead shell, not a bogus "alive" 0


def test_read_pid_file_ignores_zero(tmp_path):
    """A pid file holding ``0`` (or only non-positive values) is empty for
    resolution purposes; the first *positive* PID wins."""
    from gpusybio.applications.scripts import _read_pid_file
    p = str(tmp_path / "z.pid")
    with open(p, "w") as f:
        f.write("0\n")
    assert _read_pid_file(p) is None
    with open(p, "w") as f:
        f.write("0\n1234\n")
    assert _read_pid_file(p) == 1234
    with open(p, "w") as f:
        f.write("1234\n")
    assert _read_pid_file(p) == 1234


def test_pid_zero_never_alive():
    """pid <= 0 is never a valid process: os.kill(0, 0) targets the caller's
    own process group, so it must read as not-alive (and stop must not send
    signals to it)."""
    from gpusybio.applications.scripts import _alive
    from gpusybio.applications.scripts import graceful_stop
    from gpusybio.resources.process_tree import pid_alive
    assert not _alive(0)
    assert not _alive(-1)
    assert not pid_alive(0)
    assert not pid_alive(-1)
    # A real PID still reads alive; graceful_stop refuses pid 0.
    assert _alive(os.getpid())
    assert pid_alive(os.getpid())
    res = asyncio.new_event_loop().run_until_complete(graceful_stop(0))
    assert res.exit_code == 1


async def test_attached_ignores_stale_live_pidfile(tmp_path):
    """Guard: an *attached* script (exec) starts while the pid file holds a
    still-living PID from a *previous* run (old mtime). The current app
    (``proc.pid``) wins — the file is overwritten, the stale value is not
    tracked (spec 03 §2.5)."""
    import time as _time
    log = str(tmp_path / "p.log")
    pid = str(tmp_path / "p.pid")
    other = subprocess.Popen(["sleep", "30"])  # a previous instance, still up
    try:
        with open(pid, "w") as f:
            f.write(f"{other.pid}\n")
        old = _time.time() - 120
        os.utime(pid, (old, old))
        r = await run_start_script(
            "exec sleep 30",
            app_id="p1", log_path=log, pid_path=pid, grace=2.0,
        )
        assert r.exit_code is None
        assert r.pid != other.pid  # the current app, not the previous one
        assert _alive(r.pid)
        # The system overwrote the stale pid file with the current app's PID.
        with open(pid) as f:
            assert int(f.read().strip()) == r.pid
        _kill(r.pid)
    finally:
        other.terminate()
        other.wait()


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
