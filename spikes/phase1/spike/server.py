"""Multi-port launcher (spec 16 section 2.1).

N mock applications (aiohttp TCPSite) + N raw-ASGI gateway apps + 1 FastAPI
admin app, all on ONE event loop in ONE process. This is the serving model
the spike must validate.
"""

from __future__ import annotations

import asyncio
import os

import httpx
import uvicorn
from aiohttp import web
from fastapi import FastAPI

from spike.proxy import make_proxy


def make_admin_app(shared_state: dict) -> FastAPI:
    app = FastAPI()

    @app.get("/api/system")
    def system() -> dict:
        return {"pid": os.getpid(), "shared_state": shared_state}

    return app


async def start_stack(
    apps: list[web.Application],
    app_ports: list[int],
    gateway_ports: list[int],
    targets: list[str],
    admin_port: int,
    session,
    counters: dict,
    shared_state: dict,
) -> tuple[list[uvicorn.Server], list[web.AppRunner], asyncio.Task]:
    """Start every listener on the current event loop."""
    # 1) mock applications
    runners: list[web.AppRunner] = []
    for app, port in zip(apps, app_ports):
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", port)
        await site.start()
        runners.append(runner)

    # 2) gateway proxies (raw ASGI) + admin API (FastAPI)
    servers: list[uvicorn.Server] = []
    for port, target in zip(gateway_ports, targets):
        config = uvicorn.Config(
            make_proxy(target, session, counters),
            host="127.0.0.1",
            port=port,
            log_level="warning",
            lifespan="off",
        )
        servers.append(uvicorn.Server(config))

    admin_config = uvicorn.Config(
        make_admin_app(shared_state),
        host="127.0.0.1",
        port=admin_port,
        log_level="warning",
    )
    servers.append(uvicorn.Server(admin_config))

    async def run_all() -> None:
        await asyncio.gather(*[s.serve() for s in servers])

    task = asyncio.create_task(run_all())

    # Wait for the admin listener (last started) to be ready.
    async with httpx.AsyncClient() as probe:
        for _ in range(100):
            try:
                r = await probe.get(f"http://127.0.0.1:{admin_port}/api/system")
                if r.status_code == 200:
                    return servers, runners, task
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.05)
    task.cancel()
    raise RuntimeError("admin listener did not become ready")


async def stop_stack(
    servers: list[uvicorn.Server],
    runners: list[web.AppRunner],
    task: asyncio.Task,
) -> None:
    for server in servers:
        server.should_exit = True
    try:
        await asyncio.wait_for(task, timeout=5)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        task.cancel()
    for runner in runners:
        await runner.cleanup()
