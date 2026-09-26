"""Shared fixture: full stack (3 apps + 3 gateways + admin) on one loop."""

import asyncio

import pytest
from aiohttp import ClientSession

from spike.mock_app import PROXY_COUNTERS, STATE, create_app, reset_state
from spike.server import start_stack, stop_stack

HOST = "127.0.0.1"
PORTS = {
    "app1": 9231,   # qwen-a
    "app2": 9232,   # qwen-b
    "app3": 9233,   # comfy
    "gw1": 9321,    # gateway -> app1
    "gw2": 9322,    # gateway -> app2
    "gw3": 9323,    # gateway -> app3
    "admin": 9340,
}


@pytest.fixture()
async def stack():
    reset_state()
    apps = [create_app("qwen-a"), create_app("qwen-b"), create_app("comfy")]
    targets = [f"http://{HOST}:{PORTS[f'app{i}']}" for i in (1, 2, 3)]

    session = ClientSession()  # proxy's upstream client (shared, per 16 section 1)
    servers, runners, task = await start_stack(
        apps=apps,
        app_ports=[PORTS["app1"], PORTS["app2"], PORTS["app3"]],
        gateway_ports=[PORTS["gw1"], PORTS["gw2"], PORTS["gw3"]],
        targets=targets,
        admin_port=PORTS["admin"],
        session=session,
        counters=PROXY_COUNTERS,
        shared_state=STATE,
    )

    # Independent client session (simulates external clients).
    client = ClientSession()
    yield {
        "ports": PORTS,
        "host": HOST,
        "session": session,
        "client": client,
        "state": STATE,
        "counters": PROXY_COUNTERS,
        "servers": servers,
        "runners": runners,
    }

    await client.close()
    await stop_stack(servers, runners, task)
    await session.close()
