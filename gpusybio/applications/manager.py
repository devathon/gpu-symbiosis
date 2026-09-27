"""Application Manager (spec 04): lifecycle on behalf of the scheduler/UI.

Executes start/stop/load/unload, detects process state, measures timing,
protects against repeated load failures, and watches for dead processes.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import httpx

from gpusybio.applications.lifecycle import ProcessState, ResidencyState
from gpusybio.applications.scripts import ScriptResult, graceful_stop, run_script
from gpusybio.database.db import SessionFactory
from gpusybio.database.models import Application
from gpusybio.events.bus import EventBus
from gpusybio.events.bus import (
    FAULTED,
    LOAD_FAILED,
    LOAD_READY,
    PROCESS_DIED,
    START_TIMEOUT,
    UNLOAD_FAILED,
    UNLOADED,
)
from gpusybio.events.log import get_logger
from gpusybio.resources.manager import ResourceManager
from gpusybio.state import InvalidTransition, StateManager

log = get_logger("appmanager")

READY_POLL_SEC = 0.5


@dataclass
class AppConfig:
    """Runtime view of an application row (spec 03)."""

    id: str
    name: str
    start_script: str
    stop_script: str | None
    load_script: str | None
    unload_script: str | None
    port: int
    health_check_url: str | None
    health_check_method: str
    health_check_timeout: float
    ready_check_url: str | None
    configured_gpu_memory_mb: int
    effective_gpu_memory_mb: int
    safety_margin: float
    supports_load_unload: bool
    allow_automatic_unload: bool
    pinned: bool
    max_concurrent_requests: int
    enabled: bool
    consecutive_load_failures: int
    faulted_until: float | None
    process_name: str | None
    # Phase 4 (spec 06 §10): per-app idle-unload timeout (None → global default).
    idle_unload_timeout_sec: int | None = None
    activity_detection_method: str = "gpu_usage"
    idle_threshold_gpu_pct: float = 5.0
    # Measured timings (spec 11 §2.3: auto-calculated; feed switch cost, spec 06 §11).
    startup_time_sec: float | None = None
    load_time_sec: float | None = None
    unload_time_sec: float | None = None
    stop_time_sec: float | None = None

    @classmethod
    def from_row(cls, row: Application) -> "AppConfig":
        # SQLite returns naive datetimes; they were written as UTC (spec 04 §9).
        faulted_until = None
        if row.faulted_until is not None:
            from datetime import timezone

            fu = row.faulted_until
            if fu.tzinfo is None:
                fu = fu.replace(tzinfo=timezone.utc)
            faulted_until = fu.timestamp()
        return cls(
            id=row.id,
            name=row.name,
            start_script=row.start_script,
            stop_script=row.stop_script,
            load_script=row.load_script,
            unload_script=row.unload_script,
            port=row.port,
            health_check_url=row.health_check_url,
            health_check_method=row.health_check_method,
            health_check_timeout=row.health_check_timeout,
            ready_check_url=row.ready_check_url,
            configured_gpu_memory_mb=row.configured_gpu_memory_mb,
            effective_gpu_memory_mb=row.effective_gpu_memory_mb,
            safety_margin=row.gpu_memory_safety_margin,
            supports_load_unload=bool(row.unload_script),
            allow_automatic_unload=row.allow_automatic_unload,
            pinned=row.pinned,
            max_concurrent_requests=row.max_concurrent_requests,
            enabled=row.enabled,
            consecutive_load_failures=row.consecutive_load_failures,
            faulted_until=faulted_until,
            process_name=row.process_name,
            startup_time_sec=row.startup_time_sec,
            load_time_sec=row.load_time_sec,
            unload_time_sec=row.unload_time_sec,
            stop_time_sec=row.stop_time_sec,
            idle_unload_timeout_sec=row.idle_unload_timeout_sec,
            activity_detection_method=row.activity_detection_method or "gpu_usage",
            idle_threshold_gpu_pct=row.idle_threshold_gpu_pct if row.idle_threshold_gpu_pct is not None else 5.0,
        )


class ApplicationManager:
    def __init__(
        self,
        states: StateManager,
        resources: ResourceManager,
        bus: EventBus,
        db: SessionFactory,
        settings,
        data_path: str | None = None,
    ):
        self._states = states
        self._resources = resources
        self._bus = bus
        self._db = db
        self._settings = settings
        self._data_path = data_path
        self._configs: dict[str, AppConfig] = {}
        self._http = httpx.AsyncClient()
        self._watchdog_task: asyncio.Task | None = None

    # -- configuration -----------------------------------------------------
    def load_config(self, app_id: str) -> AppConfig:
        with self._db() as s:
            row = s.get(Application, app_id)
        if row is None:
            raise KeyError(app_id)
        cfg = AppConfig.from_row(row)
        self._configs[app_id] = cfg
        return cfg

    def config(self, app_id: str) -> AppConfig:
        # Lazy-load from the DB (source of truth) if not cached — covers
        # API-created apps that a startup pass didn't load.
        if app_id not in self._configs:
            self.load_config(app_id)
        return self._configs[app_id]

    def reload_all(self, ids: list[str]) -> None:
        for i in ids:
            self.load_config(i)

    def state(self, app_id: str):
        return self._states.get(app_id)

    # -- readiness (spec 04 §6) --------------------------------------------
    async def _probe_url(self, url: str, method: str, timeout: float) -> bool:
        try:
            req = self._http.build_request(method, url, timeout=timeout)
            resp = await self._http.send(req)
            ok = resp.status_code < 400
            await resp.aclose()
            return ok
        except httpx.HTTPError:
            return False

    async def _is_ready(self, cfg: AppConfig, pid: int | None) -> bool:
        """Readiness = health check OK (+ ready endpoint if configured).

        Never LOADED on script exit alone (spec 14 hard rule).
        """
        if cfg.health_check_url:
            if not await self._probe_url(
                cfg.health_check_url, cfg.health_check_method, cfg.health_check_timeout
            ):
                return False
        elif pid is None:
            return False
        if cfg.ready_check_url:
            if not await self._probe_url(
                cfg.ready_check_url, "GET", cfg.health_check_timeout
            ):
                return False
        return True

    async def _wait_ready(self, cfg: AppConfig, pid: int | None, timeout: float) -> bool:
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            if await self._is_ready(cfg, pid):
                return True
            await asyncio.sleep(READY_POLL_SEC)
        return False

    # -- start (spec 04 §4.1) ----------------------------------------------
    async def start(self, app_id: str) -> ScriptResult | None:
        cfg = self.config(app_id)
        try:
            self._states.begin_transition(app_id, "start")
        except InvalidTransition:
            return None
        s = self._states.get(app_id)
        if s.process not in (ProcessState.STOPPED, ProcessState.ERROR):
            s.clear_transition()
            return None
        t0 = time.monotonic()
        self._states.set_process(app_id, ProcessState.STARTING)
        await self._emit("START", app_id=app_id)

        result = await run_script(cfg.start_script, app_id=app_id, port=cfg.port, timeout=30.0, data_path=self._data_path)
        pid = _parse_pid(result)
        if result.exit_code != 0 or pid is None:
            self._states.set_process(app_id, ProcessState.ERROR)
            self._states.finish_transition(app_id)
            await self._emit(
                "START_FAILED", app_id=app_id, detail=result.stderr.strip()[:500] or "start failed"
            )
            return result
        self._states.set_pid(app_id, pid)
        s.pid = pid
        self._resources.set_pid(app_id, pid)
        self._resources.set_unloaded_baseline(app_id, max(64.0, cfg.configured_gpu_memory_mb * 0.05))

        timeout = self._settings.startup.max_detection_sec
        if await self._wait_ready(cfg, pid, timeout):
            elapsed = time.monotonic() - t0
            self._states.set_process(app_id, ProcessState.RUNNING)
            self._states.set_residency(app_id, ResidencyState.LOADED)
            self._states.get(app_id).loaded_at = time.time()
            self._resources.backend.set_loaded(app_id, cfg.configured_gpu_memory_mb)
            self._resources.refresh_attribution()
            self._record_timing(app_id, startup=elapsed)
            self._states.finish_transition(app_id)
            await self._emit("START", app_id=app_id, pid=pid, startup_sec=round(elapsed, 2))
            log.info("application started", application_id=app_id, pid=pid, startup_sec=round(elapsed, 2))
            return result

        # Startup timeout (spec 04 §4.1 step 6)
        self._states.set_process(app_id, ProcessState.ERROR)
        self._states.finish_transition(app_id)
        await self._emit(START_TIMEOUT, app_id=app_id, detail=f"{timeout}s")
        if pid is not None:
            await graceful_stop(pid)
            self._states.set_pid(app_id, None)
        return result

    # -- stop (spec 04 §4.2) ------------------------------------------------
    async def stop(self, app_id: str, force: bool = False) -> ScriptResult | None:
        cfg = self.config(app_id)
        try:
            self._states.begin_transition(app_id, "stop")
        except InvalidTransition:
            return None
        s = self._states.get(app_id)
        if s.process not in (ProcessState.RUNNING, ProcessState.ERROR):
            s.clear_transition()
            return None

        # Invariant 7: drain first unless explicitly forced.
        if not force and s.active_requests > 0:
            self._states.set_draining(app_id, True)
            await self._emit("DRAIN_START", app_id=app_id, active_requests=s.active_requests)
            await self._wait_drained(app_id)
            self._states.set_draining(app_id, False)

        t0 = time.monotonic()
        # Unload first: STOPPING is only valid with residency OFF (spec 04 §4.2).
        if s.residency in (ResidencyState.LOADED, ResidencyState.UNLOADING, ResidencyState.LOADING):
            self._states.set_residency(app_id, ResidencyState.OFF)
        self._states.set_process(app_id, ProcessState.STOPPING)
        await self._emit("STOP", app_id=app_id, forced=force)

        pid = s.pid
        if cfg.stop_script and pid is not None:
            result = await run_script(cfg.stop_script, app_id=app_id, pid=pid, timeout=30.0, data_path=self._data_path)
        elif pid is not None:
            result = await graceful_stop(pid)
        else:
            result = ScriptResult(0, "", "no process", False)

        elapsed = time.monotonic() - t0
        self._states.set_pid(app_id, None)
        self._states.set_process(app_id, ProcessState.STOPPED)
        self._states.set_residency(app_id, ResidencyState.OFF)
        self._resources.set_pid(app_id, None)
        self._resources.release(app_id)
        self._resources.backend.set_loaded(app_id, 0.0)
        self._record_timing(app_id, stop=elapsed)
        self._states.finish_transition(app_id)
        await self._emit("STOPPED", app_id=app_id, stop_sec=round(elapsed, 2), forced=force)
        log.info("application stopped", application_id=app_id, forced=force)
        return result

    # -- load (spec 04 §4.3) ------------------------------------------------
    async def load(self, app_id: str) -> ScriptResult | None:
        cfg = self.config(app_id)
        if not cfg.enabled:
            return None
        if cfg.faulted_until is not None and time.time() < cfg.faulted_until:
            await self._emit("LOAD_REFUSED", app_id=app_id, detail="faulted")
            return None
        try:
            self._states.begin_transition(app_id, "load")
        except InvalidTransition:
            return None
        s = self._states.get(app_id)
        if s.process is not ProcessState.RUNNING or s.residency is not ResidencyState.OFF:
            s.clear_transition()
            return None

        required = self._resources.required_for(cfg.configured_gpu_memory_mb, cfg.safety_margin)
        self._resources.reserve(app_id, required)  # spec 07 §6: reserve immediately
        t0 = time.monotonic()
        self._states.set_residency(app_id, ResidencyState.LOADING)
        await self._emit("LOAD", app_id=app_id, required_mb=round(required, 1))

        if cfg.load_script:
            result = await run_script(cfg.load_script, app_id=app_id, pid=s.pid, timeout=120.0, data_path=self._data_path)
        else:
            result = ScriptResult(0, "", "no load script", False)

        if result.exit_code == 0 and await self._wait_ready(cfg, s.pid, 60.0):
            elapsed = time.monotonic() - t0
            self._resources.release(app_id)
            self._resources.backend.set_loaded(app_id, cfg.configured_gpu_memory_mb)
            self._resources.refresh_attribution()
            self._states.set_residency(app_id, ResidencyState.LOADED)
            self._states.get(app_id).loaded_at = time.time()
            self._record_timing(app_id, load=elapsed)
            self._states.finish_transition(app_id)
            await self._record_load_success(app_id)
            await self._emit(LOAD_READY, app_id=app_id, load_sec=round(elapsed, 2))
            log.info("application loaded", application_id=app_id, load_sec=round(elapsed, 2))
            return result

        # Load failure (spec 04 §4.3 step 8, spec 14 §3)
        self._resources.release(app_id)
        self._states.set_residency(app_id, ResidencyState.OFF)
        self._states.set_process(app_id, ProcessState.ERROR)
        self._states.finish_transition(app_id)
        await self._record_load_failure(app_id)
        detail = (result.stderr.strip() or result.stdout.strip() or "readiness failed")[:500]
        await self._emit(LOAD_FAILED, app_id=app_id, detail=detail)
        log.error("application load failed", application_id=app_id, detail=detail)
        return result

    # -- release: unload vs stop (driven by the UNLOAD script) ---------------
    # An app that has an unload script can be *unloaded* (freed while the
    # process stays up — fast, re-loadable). An app without an unload script
    # cannot be unloaded (the process *is* the model), so it must be *stopped*
    # directly. The load script is optional: when absent, an unloaded app
    # resumes serving without a load step.
    async def release(self, app_id: str, force: bool = False) -> ScriptResult | None:
        """Free the app's VRAM. An app with an **unload script** is unloaded —
        the process stays up and the model is released (fast, re-loadable). An
        app without an unload script cannot be unloaded (there is no model to
        release — the process *is* the model), so it is **stopped** directly:
        the process is killed and its VRAM freed. Both paths free VRAM; the
        difference is whether the process survives."""
        if self.config(app_id).supports_load_unload:
            return await self.unload(app_id)
        return await self.stop(app_id, force=force)

    # -- unload (spec 04 §4.4) ----------------------------------------------
    async def unload(self, app_id: str) -> ScriptResult | None:
        cfg = self.config(app_id)
        if not cfg.supports_load_unload:
            # An app without an unload script cannot be unloaded — it must be
            # stopped directly (use release() / stop()). Refuse rather than
            # pretend to free VRAM the process still holds.
            log.warning("unload refused: no unload script", application_id=app_id)
            return None
        ok, why = self._states.can_unload(app_id)
        if not ok:
            log.warning("unload refused", application_id=app_id, reason=why)
            return None
        try:
            self._states.begin_transition(app_id, "unload")
        except InvalidTransition:
            return None
        t0 = time.monotonic()
        self._states.set_residency(app_id, ResidencyState.UNLOADING)
        await self._emit("UNLOAD", app_id=app_id)

        if cfg.unload_script:
            result = await run_script(cfg.unload_script, app_id=app_id, timeout=60.0, data_path=self._data_path)
        else:
            result = ScriptResult(0, "", "no unload script", False)

        # Signal memory release (in the fake world this starts the drain; on
        # real hardware the unload script did it), then wait for VRAM release
        # (spec 04 §4.4 step 6) — verify the baseline.
        self._resources.backend.set_loaded(app_id, 0.0)
        self._resources.refresh_attribution()
        deadline = time.monotonic() + 30.0
        released = False
        while time.monotonic() < deadline:
            if self._resources.verify_unloaded(app_id):
                released = True
                break
            await asyncio.sleep(0.5)

        if result.exit_code == 0 and released:
            elapsed = time.monotonic() - t0
            self._resources.release(app_id)
            self._states.set_residency(app_id, ResidencyState.OFF)
            self._record_timing(app_id, unload=elapsed)
            self._states.finish_transition(app_id)
            await self._emit(UNLOADED, app_id=app_id, unload_sec=round(elapsed, 2))
            log.info("application unloaded", application_id=app_id, unload_sec=round(elapsed, 2))
            return result

        # Unload failed: keep resident, surface the failure (spec 14 §5).
        self._states.set_residency(app_id, ResidencyState.LOADED)
        self._states.finish_transition(app_id)
        detail = (result.stderr.strip() or "verification failed")[:500]
        await self._emit(UNLOAD_FAILED, app_id=app_id, detail=detail)
        log.error("application unload failed", application_id=app_id, detail=detail)
        return result

    # -- draining --------------------------------------------------------------
    async def _wait_drained(self, app_id: str) -> None:
        while self._states.get(app_id).active_requests > 0:
            await asyncio.sleep(0.1)

    # -- watchdog (spec 04 §7, spec 14 §5) -----------------------------------
    async def watchdog_loop(self) -> None:
        while True:
            for app_id, cfg in list(self._configs.items()):
                s = self._states.get(app_id)
                if s.process is ProcessState.RUNNING and s.pid is not None:
                    if not _pid_alive(s.pid):
                        await self._process_died(app_id, s.pid)
            await asyncio.sleep(1.0)

    async def _process_died(self, app_id: str, pid: int | None) -> None:
        log.warning("process died", application_id=app_id, pid=pid)
        self._states.set_pid(app_id, None)
        self._states.set_process(app_id, ProcessState.ERROR)
        self._states.set_residency(app_id, ResidencyState.OFF)
        self._resources.set_pid(app_id, None)
        self._resources.release(app_id)
        self._resources.backend.set_loaded(app_id, 0.0)
        await self._emit(PROCESS_DIED, app_id=app_id, pid=pid)

    # -- failure protection (spec 04 §9) ---------------------------------------
    async def _record_load_failure(self, app_id: str) -> None:
        cfg = self.config(app_id)
        n = cfg.consecutive_load_failures + 1
        with self._db() as s:
            row = s.get(Application, app_id)
            row.consecutive_load_failures = n
        self._configs[app_id] = self.load_config(app_id)
        if n >= self._settings.load.max_consecutive_failures:
            faulted_until = time.time() + 300.0
            with self._db() as s:
                row = s.get(Application, app_id)
                row.faulted_until = _dt(faulted_until)
            self._configs[app_id] = self.load_config(app_id)
            await self._emit(FAULTED, app_id=app_id, detail=f"{n} consecutive failures")
            log.error("application faulted", application_id=app_id, failures=n)

    async def _record_load_success(self, app_id: str) -> None:
        cfg = self.config(app_id)
        if cfg.consecutive_load_failures > 0 or cfg.faulted_until is not None:
            with self._db() as s:
                row = s.get(Application, app_id)
                row.consecutive_load_failures = 0
                row.faulted_until = None
            self._configs[app_id] = self.load_config(app_id)
            await self._emit("FAULT_CLEARED", app_id=app_id)

    # -- timing (spec 04 §5) ---------------------------------------------------
    def _record_timing(self, app_id: str, startup=None, load=None, unload=None, stop=None) -> None:
        with self._db() as s:
            row = s.get(Application, app_id)
            if startup is not None:
                row.startup_time_sec = startup
            if load is not None:
                row.load_time_sec = load
            if unload is not None:
                row.unload_time_sec = unload
            if stop is not None:
                row.stop_time_sec = stop

    # -- events ----------------------------------------------------------------
    async def _emit(self, event_type: str, app_id: str = "", **data) -> None:
        data.setdefault("application_id", app_id)
        await self._bus.publish_raw(event_type, **data)
        # Persist to application_events (spec 04 §10).
        try:
            from gpusybio.database.models import ApplicationEvent

            with self._db() as s:
                s.add(ApplicationEvent(application_id=app_id, event_type=event_type,
                                       detail=str(data.get("detail", "")) or None))
        except Exception:  # noqa: BLE001 - persistence must never break lifecycle
            pass


def _parse_pid(result: ScriptResult) -> int | None:
    """Parse the PID the start script prints (last non-empty line, digits)."""
    import re

    lines = [ln.strip() for ln in result.stdout.strip().splitlines() if ln.strip()]
    for ln in reversed(lines):
        m = re.fullmatch(r"\s*(\d+)\s*", ln)
        if m:
            return int(m.group(1))
    return None


def _pid_alive(pid: int) -> bool:
    import os

    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _dt(ts: float):
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ts, tz=timezone.utc)
