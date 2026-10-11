"""Composition root (spec 02 §5): one process, one event loop, many ports.

Startup (spec 09 §8 reconciliation): the DB is the source of truth; runtime
state starts fresh (all applications STOPPED/OFF in memory). The seed creates
missing application/gateway rows and updates existing ones only when a spec
sets ``update: true`` (spec 16 §2.6); reconciliation then verifies no
leftover processes hold app ports.
"""

from __future__ import annotations

import asyncio
import io
import os
import signal
import sys
import time

import uvicorn
from aiohttp import ClientSession

from gpusybio.api import make_admin_api
from gpusybio.metrics.type_registry import make_type_registry
from gpusybio.applications.lifecycle import ProcessState, ResidencyState
from gpusybio.applications.manager import ApplicationManager
from gpusybio.config.models import Config
from gpusybio.config.seed import seed_database
from gpusybio.database.db import create_engine, init_db, make_session_factory
from gpusybio.database.models import Application, Gateway
from gpusybio.events.bus import EventBus
from gpusybio.events.log import get_logger, setup_logging
from gpusybio.events.sse import SSEHub
from gpusybio.gateway.proxy import make_gateway_proxy
from gpusybio.gateway.request_manager import RequestManager
from gpusybio.metrics import MetricsCollector
from gpusybio.resources.gpu import make_backend
from gpusybio.resources.manager import ResourceManager
from gpusybio.resources.process_tree import pid_alive
from gpusybio.scheduler import Scheduler
from gpusybio.scheduler.external import ExternalAppsMonitor
from gpusybio.scheduler.idle import IdleUnloader
from gpusybio.scheduler.residency_monitor import ResidencyMonitor
from gpusybio.scheduler.tasks import TaskScheduler
from gpusybio.state import StateManager

log = get_logger("server")


class Server:
    def __init__(self, config: Config):
        self.config = config
        self.engine = create_engine(config.db_path)
        init_db(self.engine)
        self.db = make_session_factory(self.engine)
        seed_database(self.db, config)

        self.bus = EventBus()
        self.hub = SSEHub()
        self.states = StateManager()
        self.backend = make_backend(config.settings.gpu)
        self.resources = ResourceManager(
            backend=self.backend,
            bus=self.bus,
            memory_reserve_mb=float(config.settings.gpu.memory_reserve_mb),
            memory_safety_margin=float(config.settings.gpu.memory_safety_margin),
        )
        self.data_path = config.effective_data_path()
        # Pluggable application types (spec 04 §8): the shipped type modules +
        # drop-in modules under <data_path>/plugins, loaded before any app
        # config is resolved.
        self.type_registry = make_type_registry(self.data_path)
        self.apps = ApplicationManager(
            self.states, self.resources, self.bus, self.db, config.settings, data_path=self.data_path
        )
        # DB is the source of truth (spec 16 §2.6): load config for ALL persisted
        # applications and gateways — not just the seed document. API-created
        # ones survive a restart and must be first-class (scheduler, metrics,
        # idle, gateway listeners, shutdown).
        with self.db() as s:
            app_rows = s.query(Application).all()
            gw_rows = s.query(Gateway).all()
        self.app_ids = [a.id for a in app_rows]
        self.gateway_ids = [g.id for g in gw_rows]
        for app_id in self.app_ids:
            self.apps.load_config(app_id)
        # Register every app's unload baseline up front (spec 07 §8) so
        # verify_unloaded works even for apps re-adopted at startup (whose
        # process survived a restart and never ran the start() path).
        self.apps.register_unload_baselines()

        gw_configs = {
            g.id: {
                "id": g.id,
                "name": g.name,
                "application_id": g.application_id,
                "port": g.port,
                "priority": g.priority,
                "max_waiting_queue": g.max_waiting_queue,
                "max_waiting_time_sec": g.max_waiting_time_sec,
                "wait_timeout_policy": g.wait_timeout_policy,
                "request_timeout_sec": g.request_timeout_sec,
                "max_concurrent_requests": g.max_concurrent_requests,
                "enabled": g.enabled,
                "paused": g.paused,
            }
            for g in gw_rows
        }
        app_configs = {a.id: {"max_concurrent_requests": a.max_concurrent_requests} for a in app_rows}
        self.request_manager = RequestManager(
            self.states, self.db, self.bus, config.settings, gw_configs, app_configs
        )
        self.scheduler = Scheduler(
            self.states, self.resources, self.request_manager, self.apps,
            config.settings, self.db, self.bus, self.app_ids, self.gateway_ids
        )
        self.metrics = MetricsCollector(
            self.resources, self.states, self.request_manager, self.db, self.app_ids,
            app_types={a.id: a.application_type for a in app_rows},
            base_urls={a.id: f"http://127.0.0.1:{a.port}" for a in app_rows},
            log_paths={a.id: config.effective_log_path(a.id, a.log_file) for a in app_rows},
            type_registry=self.type_registry,
        )
        # Phase 4 (spec 06 §10): automatic idle detection + unloading.
        self.idle_unloader = IdleUnloader(
            self.resources, self.states, self.apps, self.request_manager, self.bus,
            self.app_ids, self.gateway_ids,
            min_residency_sec=float(config.settings.scheduler.min_residency_sec),
            default_idle_timeout_sec=float(config.settings.scheduler.idle_unload_timeout_sec),
            interval=float(config.settings.scheduler.idle_unload_interval_sec),
        )
        # spec 06 §10: memory-based residency monitor — reconciles LOADED/UNLOADED
        # when the app loads/frees its model outside GPUSymbiosis (e.g. ComfyUI).
        self.residency_monitor = ResidencyMonitor(
            self.resources, self.states, self.apps, self.bus,
            self.app_ids, db=self.db,
        )
        # The Tasks feature: one-shot commands launched when the GPU is idle.
        # Paused when config.settings.tasks.enabled is False (the tick returns early).
        self.task_scheduler = TaskScheduler(
            config.settings.tasks, self.db, self.resources, self.states, self.apps,
            self.bus, self.idle_unloader, self.data_path,
        )
        # External apps (unmanaged GPU processes): listed under Applications with
        # a kill button; the auto-unload pass kills an idle, VRAM-holding app
        # (gated on a waiting gateway + the exclude regexp). In-memory only.
        self.external_apps = ExternalAppsMonitor(
            self.resources, self.states, self.request_manager, self.bus,
            config.settings.external_apps, self.app_ids,
        )
        # gw_id -> (uvicorn.Server, serve task). The task is awaited on
        # re-spawn so the old listening socket is really closed before we
        # re-bind (spec 11 §3: re-saving a gateway must not fail with
        # "address already in use").
        self._gw_live: dict[str, tuple[uvicorn.Server, asyncio.Task]] = {}
        self.admin_api = make_admin_api(
            self.db, self.states, self.resources, self.request_manager,
            self.apps, self.scheduler, self.metrics, self.hub,
            idle_unloader=self.idle_unloader,
            bus=self.bus,
            external_apps=self.external_apps,
            on_gateway_created=self._spawn_gateway,
            on_gateway_removed=self._remove_gateway,
            config=self.config,
        )
        self._tasks: list[asyncio.Task] = []
        self._servers: list[uvicorn.Server] = []
        self.session: ClientSession | None = None
        self._stopped = False
        self._sigint_pending = False

    # -- gateway ASGI apps ----------------------------------------------------
    def _gateway_app(self, gw_id: str):
        gw = self.request_manager.gateway(gw_id)
        app_cfg = self.apps.config(gw["application_id"])
        app_view = {
            "id": app_cfg.id,
            "port": app_cfg.port,
            "max_concurrent_requests": app_cfg.max_concurrent_requests,
        }
        from gpusybio.auth import verify_key as _verify_key

        db = self.db
        # spec 05 §6: routing decision from the application type — which requests
        # go through the scheduler vs pass through directly.
        spec = self.type_registry.spec_for(app_cfg.application_type)
        return make_gateway_proxy(
            gw, self.request_manager, self.session, self.states, app_view,
            verify_key=lambda presented: _verify_key(db, gw_id, presented),
            should_schedule=spec.should_schedule,
        )

    async def _spawn_gateway(self, gw_id: str) -> None:
        """Spawn (or re-spawn on port change) a live gateway listener
        (spec 11 §3.2 — API-created gateways serve immediately)."""
        gw = self.request_manager.gateway(gw_id)
        port = gw["port"]
        import socket

        # Probe the target port FIRST (SO_REUSEADDR, like uvicorn) so a conflict
        # fails cleanly *before* the old listener is stopped — otherwise a failed
        # re-bind would leave the gateway with no listener at all.
        probe = socket.socket()
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((self.config.host, port))
        except OSError as e:
            raise RuntimeError(f"port {port} is not available") from e
        finally:
            probe.close()

        # Now stop the old listener (if any) and wait for its socket to actually
        # close before re-binding. The API only calls this on a genuine port
        # change (a different socket), so a same-port re-save is never a no-op
        # race here.
        await self._stop_gw_listener(gw_id)
        self._gw_live[gw_id] = await self._serve(self.config.host, port, self._gateway_app(gw_id))

    async def _remove_gateway(self, gw_id: str) -> None:
        """Close a gateway's live listener on deletion."""
        await self._stop_gw_listener(gw_id)

    async def _stop_gw_listener(self, gw_id: str) -> None:
        """Stop a gateway listener and await its serve task so the listening
        socket is really closed (uvicorn only releases it in shutdown())."""
        rec = self._gw_live.pop(gw_id, None)
        if rec is None:
            return
        server, task = rec
        server.should_exit = True
        if not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
            except asyncio.TimeoutError:
                task.cancel()

    # -- startup (spec 09 §8) --------------------------------------------------
    def _reconcile(self) -> None:
        """Fresh start: runtime state is in-memory (all STOPPED/OFF).

        Re-adopt applications whose process survived a gateway restart
        (system exit / crash): the recorded PID (pid file) must be alive
        AND the app port in use — then the app is STARTED in our state
        again and the watchdog/metrics/scheduler track it (residency is
        reconciled by the residency monitor on its next tick). Mismatched
        cases are warned and the app stays STOPPED.
        """
        for app_id in self.app_ids:
            cfg = self.apps.config(app_id)
            port = _port_in_use(cfg.port)
            pid = _read_pid(self.apps.pid_path(app_id))
            alive = pid is not None and pid_alive(pid)
            if alive and port:
                self.states.set_pid(app_id, pid)
                self.states.set_process(app_id, ProcessState.STARTED)
                self.resources.set_pid(app_id, pid)
                # A standalone server (no load/unload scripts — the process IS
                # the model) is loaded the moment it is running, so re-adopt it
                # LOADED: its memory monitor is disabled (no threshold) and a
                # request would otherwise sit queued waiting for a load that
                # never comes. A lazy-loading app (with load/unload) may not be
                # loaded — leave it OFF and let the residency monitor reconcile.
                if not cfg.supports_load_unload:
                    self.states.set_residency(app_id, ResidencyState.LOADED)
                    self.states.get(app_id).loaded_at = time.time()
                log.info("re-adopted running application",
                         application_id=app_id, pid=pid, port=cfg.port,
                         residency=self.states.get(app_id).residency.value)
            elif alive and not port:
                log.warning("recorded process alive but app port not in use",
                            application_id=app_id, pid=pid, port=cfg.port)
            elif port:
                log.warning(
                    "port already in use at startup",
                    application_id=app_id, port=cfg.port,
                    detail="a previous or external instance may still be running; start will fail readiness",
                )

    async def _serve(self, host: str, port: int, app) -> tuple[uvicorn.Server, asyncio.Task]:
        config = uvicorn.Config(app, host=host, port=port, log_level="warning", lifespan="on")
        server = uvicorn.Server(config)
        self._servers.append(server)
        task = asyncio.create_task(server.serve(), name=f"uvicorn-{port}")
        self._tasks.append(task)
        return (server, task)

    async def serve(self) -> None:
        setup_logging(self.config.settings.log_level)
        log.info("starting GPUSymbiosis", admin_port=self.config.admin_port,
                 apps=self.app_ids, gateways=self.gateway_ids)
        self._reconcile()

        self.session = ClientSession()
        await self.session.__aenter__()

        # Gateway ports (spec 05: one port per gateway).
        for gw_id in self.gateway_ids:
            gw = self.request_manager.gateway(gw_id)
            await self._serve(self.config.host, gw["port"], self._gateway_app(gw_id))

        # Admin API (admin_host, when set, overrides the shared host).
        await self._serve(self.config.admin_host or self.config.host, self.config.admin_port, self.admin_api)

        # Event observer: persist + SSE push.
        self.bus.observe(self.admin_api.state.observe)

        # Long-running loops.
        loop = asyncio.get_running_loop()
        # Ctrl-C (system exit): confirm whether to stop started applications.
        # add_signal_handler needs a sync callback — schedule the coroutine.
        loop.add_signal_handler(signal.SIGINT, lambda: asyncio.ensure_future(self._on_sigint()))
        self._tasks.append(asyncio.create_task(self.apps.watchdog_loop(), name="watchdog"))
        self._tasks.append(asyncio.create_task(self.resources.sampling_loop(), name="resources"))
        self.scheduler.start()
        self.metrics.start()
        self.idle_unloader.start()
        self.residency_monitor.start()
        self.task_scheduler.start()
        self.external_apps.start()

        # Wait for all listeners.
        try:
            await asyncio.gather(*self._tasks)
        except asyncio.CancelledError:
            # Shutdown: stop() force-cancels the listener tasks once their
            # graceful shutdown times out. This gather (no return_exceptions)
            # would re-raise that CancelledError up to asyncio.run and print a
            # traceback — swallowing it so the main coroutine returns cleanly.
            # The _on_sigint path hard-exits (os._exit) right after.
            pass
        finally:
            await self.stop()

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        log.info("shutting down")
        # Stop managed applications first (we own their processes).
        for app_id in self.app_ids:
            try:
                s = self.states.get(app_id)
                if s.process is not ProcessState.STOPPED:
                    await self.apps.stop(app_id, force=True)
            except Exception:  # noqa: BLE001
                log.exception("stop app failed during shutdown", application_id=app_id)
        # Clean shutdown: signal the servers and let main_loop exit naturally —
        # uvicorn only closes its sockets in shutdown(), which a task
        # cancellation would skip (no try/finally around it).
        for s in self._servers:
            s.should_exit = True
        try:
            await asyncio.wait(self._tasks, timeout=5.0)
        except BaseException:  # noqa: BLE001
            pass
        # Force-cancel anything that survived (watchdog/resources/servers).
        for t in self._tasks:
            if not t.done():
                t.cancel()
        try:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        except BaseException:  # noqa: BLE001
            pass
        # Scheduler/metrics/idle keep-alive loops.
        try:
            await self.scheduler.stop()
            await self.metrics.stop()
            await self.idle_unloader.stop()
            await self.residency_monitor.stop()
            await self.task_scheduler.stop()
            await self.external_apps.stop()
        except Exception:  # noqa: BLE001
            pass
        if self.session is not None:
            await self.session.close()
        self.engine.dispose()

    # -- system exit (Ctrl-C) ---------------------------------------------------
    async def _on_sigint(self) -> None:
        """Ctrl-C: ask whether to stop all started applications (5s timeout,
        default YES).

        yes → full clean shutdown (applications first), then the gateway
             exits with code 0 (hard exit — the shutdown is already done,
             this avoids a CancelledError from the cancelled loop tasks).
        no  → exit the gateway, leaving the started applications running.
        No started applications → just shut down, no prompt.
        """
        if self._stopped or self._sigint_pending:
            return
        self._sigint_pending = True
        started = [
            aid for aid in self.app_ids
            if self.states.get(aid).process is not ProcessState.STOPPED
        ]
        stop_apps = await self._confirm_stop(started) if started else True
        if stop_apps:
            log.info("ctrl-c: stopping started applications", apps=started)
            await self.stop()
        else:
            log.info("ctrl-c: leaving started applications running", apps=started)
        os._exit(0)

    async def _confirm_stop(self, started: list[str], timeout_sec: float = 5.0) -> bool:
        """Terminal prompt: stop all started applications?

        Default YES — a single Y/y keypress stops them, N/n leaves them running.
        A timeout, EOF, or non-interactive stdin also stops them (the default).
        Any other key is rejected and the prompt is repeated.
        """
        loop = asyncio.get_running_loop()
        try:
            return await asyncio.wait_for(
                asyncio.shield(
                    loop.run_in_executor(None, _read_confirmation, started, timeout_sec)
                ),
                timeout=timeout_sec,
            )
        except (asyncio.TimeoutError, EOFError, OSError):
            sys.stdout.write(
                f"\n(no answer within {timeout_sec:g}s — stopping applications)\n"
            )
            sys.stdout.flush()
            return True


def _read_confirmation(started: list[str], timeout_sec: float) -> bool:
    """Blocking single-keypress confirmation (Y/y = stop, N/n = leave running).

    Runs in a thread executor. On a real terminal it reads a single character
    in raw mode (no Enter needed); any other key is rejected with a beep and
    the prompt repeats. A timeout applies to the first accepted keypress and
    defaults to YES (stop). Non-terminal stdin (no tty / no fileno / EOF)
    falls back to line-buffered input with the same timeout and default.
    """
    import select
    import termios
    import tty

    try:
        fd = sys.stdin.fileno()
        if not os.isatty(fd):
            return _read_confirmation_fallback(started, timeout_sec)
    except (AttributeError, OSError, io.UnsupportedOperation):
        return _read_confirmation_fallback(started, timeout_sec)

    def _timed_out() -> None:
        sys.stdout.write(f"\n(no answer within {timeout_sec:g}s — stopping applications)\n")
        sys.stdout.flush()

    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        prompt = f"stop all started applications ({', '.join(started)})? [Y/n] "
        sys.stdout.write(prompt)
        sys.stdout.flush()

        start = time.time()
        while True:
            remaining = timeout_sec - (time.time() - start)
            if remaining <= 0:
                _timed_out()
                return True
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                _timed_out()
                return True

            ch = os.read(fd, 1)
            if not ch:  # EOF
                sys.stdout.write("\n(no answer — stopping applications)\n")
                sys.stdout.flush()
                return True
            key = ch.decode(errors="replace").lower()
            if key == "y":
                sys.stdout.write("\nY — stopping applications\n")
                sys.stdout.flush()
                return True
            if key == "n":
                sys.stdout.write("\nN — leaving applications running\n")
                sys.stdout.flush()
                return False
            # Reject any other key: beep and wait for the next one.
            sys.stdout.write("\a")
            sys.stdout.flush()
    except (EOFError, OSError):
        sys.stdout.write("\n(no answer — stopping applications)\n")
        sys.stdout.flush()
        return True
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        except OSError:
            pass


def _read_confirmation_fallback(started: list[str], timeout_sec: float) -> bool:
    """Non-tty fallback: line-buffered ``input()`` on a daemon thread, with a timeout.

    A timeout (thread still blocked) or an empty line defaults to YES (stop);
    only an explicit ``n``/``no`` declines.
    """
    import threading

    prompt = f"stop all started applications ({', '.join(started)})? [Y/n] "
    answer: list[str | None] = [None]

    def read_input() -> None:
        try:
            answer[0] = input(prompt).strip().lower()
        except (EOFError, OSError):
            answer[0] = ""

    t = threading.Thread(target=read_input)
    t.daemon = True
    t.start()
    t.join(timeout=timeout_sec)
    if answer[0] is None:
        sys.stdout.write(f"\n(no answer within {timeout_sec:g}s — stopping applications)\n")
        sys.stdout.flush()
        return True
    return answer[0] not in ("n", "no")


def _port_in_use(port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def _read_pid(path: str) -> int | None:
    """The PID recorded in a pid file, or None if missing/unreadable."""
    try:
        with open(path) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser("gpusybio")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    from gpusybio.config.models import load_yaml

    config = load_yaml(args.config)
    server = Server(config)
    try:
        asyncio.run(server.serve())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
