"""Composition root (spec 02 §5): one process, one event loop, many ports.

Startup (spec 09 §8 reconciliation): the DB is the source of truth; runtime
state starts fresh (all applications STOPPED/OFF in memory). The seed creates
missing application/gateway rows and updates existing ones only when a spec
sets ``update: true`` (spec 16 §2.6); reconciliation then verifies no
leftover processes hold app ports.
"""

from __future__ import annotations

import asyncio
import signal
import sys

import uvicorn
from aiohttp import ClientSession

from gpusybio.api import make_admin_api
from gpusybio.applications.lifecycle import ProcessState
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
from gpusybio.scheduler import Scheduler
from gpusybio.scheduler.idle import IdleUnloader
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
            log_paths={a.id: config.app_log_path(a.id) for a in app_rows},
        )
        # Phase 4 (spec 06 §10): automatic idle detection + unloading.
        self.idle_unloader = IdleUnloader(
            self.resources, self.states, self.apps, self.request_manager, self.bus,
            self.app_ids, self.gateway_ids,
            min_residency_sec=float(config.settings.scheduler.min_residency_sec),
            default_idle_timeout_sec=float(config.settings.scheduler.idle_unload_timeout_sec),
            interval=float(config.settings.scheduler.idle_unload_interval_sec),
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
            on_gateway_created=self._spawn_gateway,
            on_gateway_removed=self._remove_gateway,
        )
        self._tasks: list[asyncio.Task] = []
        self._servers: list[uvicorn.Server] = []
        self.session: ClientSession | None = None
        self._stopped = False

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
        return make_gateway_proxy(
            gw, self.request_manager, self.session, self.states, app_view,
            verify_key=lambda presented: _verify_key(db, gw_id, presented),
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
            probe.bind(("127.0.0.1", port))
        except OSError as e:
            raise RuntimeError(f"port {port} is not available") from e
        finally:
            probe.close()

        # Now stop the old listener (if any) and wait for its socket to actually
        # close before re-binding. The API only calls this on a genuine port
        # change (a different socket), so a same-port re-save is never a no-op
        # race here.
        await self._stop_gw_listener(gw_id)
        self._gw_live[gw_id] = await self._serve("127.0.0.1", port, self._gateway_app(gw_id))

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
        """Fresh start: runtime state is in-memory (all STOPPED/OFF). Verify
        that no stray process holds an app port (spec 09 §8)."""
        for app_id in self.app_ids:
            cfg = self.apps.config(app_id)
            if _port_in_use(cfg.port):
                log.warning(
                    "port already in use at startup",
                    application_id=app_id, port=cfg.port,
                    detail="a previous instance may still be running; start will fail readiness",
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
            await self._serve("127.0.0.1", gw["port"], self._gateway_app(gw_id))

        # Admin API.
        await self._serve(self.config.admin_host, self.config.admin_port, self.admin_api)

        # Event observer: persist + SSE push.
        self.bus.observe(self.admin_api.state.observe)

        # Long-running loops.
        loop = asyncio.get_running_loop()
        self._tasks.append(asyncio.create_task(self.apps.watchdog_loop(), name="watchdog"))
        self._tasks.append(asyncio.create_task(self.resources.sampling_loop(), name="resources"))
        self.scheduler.start()
        self.metrics.start()
        self.idle_unloader.start()

        # Wait for all listeners.
        try:
            await asyncio.gather(*self._tasks)
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
        except Exception:  # noqa: BLE001
            pass
        if self.session is not None:
            await self.session.close()
        self.engine.dispose()


def _port_in_use(port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


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
