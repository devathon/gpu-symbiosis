"""External apps monitoring: unmanaged system processes using the GPU or
holding VRAM.

An *external app* is a GPU process that does not belong to any managed
application — its whole process tree (root PID and descendants, the same
attribution span the per-app readings use) is excluded. The monitor keeps
per-pid state in memory only (no DB): a rolling ``window_sec`` (default 60 s)
of ``(t, util)`` samples for the average GPU utilization shown to the user,
plus the latest VRAM reading.

An external app is *shown* when its average GPU% is at/above
``gpu_threshold`` or its VRAM is at/above ``vram_threshold``.

Killing happens two ways:

- **manual** — the UI's kill button (``kill`` via the API);
- **auto** — the auto-unload pass, on each tick, when ALL of:
  ``auto_unload`` is on; some enabled, non-paused gateway has waiting
  requests (that is the moment memory is actually needed); the app is idle
  (average GPU% below the activity threshold — "not using GPU"); it holds
  VRAM at/above ``vram_threshold`` ("only using VRAM"); and its command does
  not match ``auto_unload_exclude`` (regexp, e.g. ``ffmpeg|vlc``).

Every kill is logged and published as ``external_app_killed`` (with the
reason) for the event feed / audit trail.
"""

from __future__ import annotations

import asyncio
import os
import re
import signal
import time
from collections import deque

from gpusybio.events.bus import EXTERNAL_APP_KILLED, EventBus
from gpusybio.events.log import get_logger
from gpusybio.resources.process_tree import process_tree

log = get_logger("external_apps")


def _compile_exclude(pattern: str) -> re.Pattern | None:
    """The auto-unload exclude regexp; empty or invalid = no exclusions."""
    pattern = (pattern or "").strip()
    if not pattern:
        return None
    try:
        return re.compile(pattern)
    except re.error:
        log.warning("invalid auto_unload_exclude pattern; ignoring", pattern=pattern)
        return None


def _cmd_of(pid: int) -> str:
    """The process command line (``/proc/<pid>/cmdline``, NUL-joined); the
    comm name as fallback; ``pid <n>`` for anything unreadable."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            parts = [p for p in f.read().decode(errors="replace").split("\0") if p]
        if parts:
            return " ".join(parts)
    except (FileNotFoundError, PermissionError, OSError):
        pass
    try:
        with open(f"/proc/{pid}/comm") as f:
            name = f.read().strip()
        if name:
            return name
    except (FileNotFoundError, PermissionError, OSError):
        pass
    return f"pid {pid}"


class ExternalAppsMonitor:
    """Tracks unmanaged GPU processes; lists them and kills them.

    Read-only over all core state (states, queue, backend); owns no locks on
    shared data and writes no DB rows. The only write-side action is
    ``os.kill`` on a PID outside every managed application's process tree.
    """

    def __init__(
        self,
        resources,
        states,
        request_manager,
        bus: EventBus | None,
        spec,
        app_ids: list[str],
        interval: float = 5.0,
        window_sec: float = 60.0,
    ) -> None:
        self._resources = resources
        self._states = states
        self._rm = request_manager
        self._bus = bus
        self._spec = spec
        self._app_ids = list(app_ids)
        self._interval = max(0.2, interval)
        self._window = max(1.0, window_sec)
        self._exclude = _compile_exclude(getattr(spec, "auto_unload_exclude", "") or "")
        # pid -> deque of (t, util) samples; latest VRAM per pid. In-memory only.
        self._hist: dict[int, deque] = {}
        self._vram: dict[int, float] = {}
        self._task: asyncio.Task | None = None
        # Injectable clock (tests monkeypatch the module's time instead).
        self._now = time.time

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("external-apps tick failed")
            await asyncio.sleep(self._interval)

    # -- tick ---------------------------------------------------------------
    async def tick(self) -> None:
        now = self._now()
        # Managed applications: the whole process tree of each live app PID
        # (children are already attributed to the app — never external).
        managed: set[int] = set()
        for s in self._states.all().values():
            if s.pid:
                managed |= process_tree(s.pid)
        alive: set[int] = set()
        for p in self._resources.backend.external_processes():
            if p.pid in managed:
                continue
            alive.add(p.pid)
            self._hist.setdefault(p.pid, deque()).append((now, float(p.utilization_pct)))
            self._vram[p.pid] = float(p.memory_mb)
        # Drop state for pids that vanished (process exited / killed).
        for pid in list(self._hist):
            if pid not in alive:
                self._forget(pid)
        # Trim the rolling window.
        cutoff = now - self._window
        for dq in self._hist.values():
            while dq and dq[0][0] < cutoff:
                dq.popleft()
        if getattr(self._spec, "auto_unload", False):
            await self._auto_kill_pass()

    # -- shown list (API / UI) ----------------------------------------------
    def list(self) -> list[dict]:
        """Shown external apps: average GPU% >= threshold OR VRAM >= threshold."""
        out: list[dict] = []
        for pid in sorted(self._hist):
            avg = self._avg_util(pid)
            vram = self._vram.get(pid, 0.0)
            if avg < self._spec.gpu_threshold and vram < self._spec.vram_threshold:
                continue
            cmd = _cmd_of(pid)
            out.append(
                {
                    "pid": pid,
                    "cmd": cmd,
                    "gpu_pct": round(avg, 1),
                    "vram_mb": round(vram, 1),
                    "auto_unload_ok": not (self._exclude and self._exclude.search(cmd)),
                }
            )
        return out

    def config(self) -> dict:
        return {
            "gpu_threshold": float(self._spec.gpu_threshold),
            "vram_threshold": float(self._spec.vram_threshold),
            "auto_unload": bool(self._spec.auto_unload),
            "auto_unload_exclude": self._spec.auto_unload_exclude or "",
        }

    def _avg_util(self, pid: int) -> float:
        dq = self._hist.get(pid)
        if not dq:
            return 0.0
        return sum(u for _, u in dq) / len(dq)

    def _forget(self, pid: int) -> None:
        self._hist.pop(pid, None)
        self._vram.pop(pid, None)

    # -- gateway-waiting gate (auto-kill precondition) -----------------------
    def _gateway_waiting(self) -> bool:
        """An enabled, non-paused gateway has waiting requests — the moment
        memory is actually needed. (The same flags the scheduler honors.)"""
        q = self._rm.queue()
        for gid, cfg in self._rm.gateway_configs().items():
            if cfg.get("enabled") and not cfg.get("paused") and q.length(gid) > 0:
                return True
        return False

    # -- auto-unload pass -----------------------------------------------------
    async def _auto_kill_pass(self) -> None:
        if not self._gateway_waiting():
            return
        idle_bar = float(self._resources.gpu_activity_threshold_pct)
        for row in self.list():
            if not row["auto_unload_ok"]:
                continue  # excluded (regexp) — never auto-killed
            if row["vram_mb"] < self._spec.vram_threshold:
                continue  # not holding meaningful VRAM
            if row["gpu_pct"] >= idle_bar:
                continue  # using the GPU — "only using VRAM" does not hold
            await self.kill(row["pid"], reason="auto")

    # -- kill -----------------------------------------------------------------
    async def kill(self, pid: int, reason: str = "manual") -> dict:
        """SIGKILL the process. ``ProcessLookupError`` (already gone) is a
        success; ``PermissionError`` is reported as a failure."""
        cmd = _cmd_of(pid)
        vram = self._vram.get(pid)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            self._forget(pid)
            return {"killed": False, "detail": "process already gone"}
        except PermissionError:
            return {"killed": False, "detail": "permission denied"}
        self._forget(pid)
        log.info("external app killed", pid=pid, cmd=cmd, reason=reason, vram_mb=vram)
        if self._bus is not None:
            await self._bus.publish_raw(
                EXTERNAL_APP_KILLED, pid=pid, cmd=cmd, reason=reason, vram_mb=vram
            )
        return {"killed": True}
