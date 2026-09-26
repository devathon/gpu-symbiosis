"""Mock GPU applications for the Phase 1 spikes.

Each mock simulates one managed application (spec 01 section 3):

- qwen-a / qwen-b : LLM-style servers exposing /stream (SSE token stream)
- comfy           : ComfyUI-style server exposing /ws/progress (WebSocket)

All instances share STATE so the admin API can observe what the gateways did
(this also verifies the single-process / single-state control-plane model).
"""

from __future__ import annotations

import asyncio
from collections import Counter

from aiohttp import WSMsgType, web
from aiohttp.client_exceptions import ClientConnectionResetError

STATE: dict = {
    "streams": {"active": 0, "completed": 0, "cancelled": 0},
    "long": {"active": 0, "completed": 0, "cancelled": 0},
    "ws": {"sessions": 0, "echoes": 0, "closed": 0},
    "hits": Counter(),
}

PROXY_COUNTERS: dict = {
    "http_completed": 0,
    "http_aborted": 0,
    "ws_bridged": 0,
    "ws_aborted": 0,
}


def reset_state() -> None:
    for key in STATE["streams"]:
        STATE["streams"][key] = 0
    for key in STATE["long"]:
        STATE["long"][key] = 0
    for key in STATE["ws"]:
        STATE["ws"][key] = 0
    STATE["hits"].clear()
    for key in PROXY_COUNTERS:
        PROXY_COUNTERS[key] = 0


def create_app(name: str) -> web.Application:
    app = web.Application()

    async def health(request: web.Request) -> web.Response:
        STATE["hits"][name] += 1
        return web.json_response({"app": name})

    async def sse_stream(request: web.Request) -> web.StreamResponse:
        """SSE token stream: one token per interval. Cancellation of this
        handler means the client (or the proxy on its behalf) left."""
        tokens = int(request.query.get("tokens", 10))
        interval = float(request.query.get("interval_ms", 100)) / 1000.0
        resp = web.StreamResponse(status=200)
        resp.content_type = "text/event-stream"
        await resp.prepare(request)
        STATE["streams"]["active"] += 1
        try:
            for i in range(tokens):
                await asyncio.sleep(interval)
                await resp.write(f"data: token-{i}\n\n".encode())
            STATE["streams"]["completed"] += 1
        except (
            asyncio.CancelledError,
            ConnectionError,
            ClientConnectionResetError,
        ):
            STATE["streams"]["cancelled"] += 1
            raise
        finally:
            STATE["streams"]["active"] -= 1
        await resp.write_eof()
        return resp

    async def long_stream(request: web.Request) -> web.StreamResponse:
        """Chunked non-SSE long response."""
        chunks = int(request.query.get("chunks", 10))
        interval = float(request.query.get("interval_ms", 100)) / 1000.0
        resp = web.StreamResponse(status=200)
        resp.content_type = "application/octet-stream"
        await resp.prepare(request)
        STATE["long"]["active"] += 1
        try:
            for i in range(chunks):
                await asyncio.sleep(interval)
                await resp.write(f"chunk-{i}:".encode() + bytes(64))
            STATE["long"]["completed"] += 1
        except (
            asyncio.CancelledError,
            ConnectionError,
            ClientConnectionResetError,
        ):
            STATE["long"]["cancelled"] += 1
            raise
        finally:
            STATE["long"]["active"] -= 1
        await resp.write_eof()
        return resp

    async def ws_progress(request: web.Request) -> web.WebSocketResponse:
        """ComfyUI-style progress WebSocket: periodic server->client progress
        messages plus echo of client->server messages (both directions)."""
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        STATE["ws"]["sessions"] += 1
        interval = float(request.query.get("interval_ms", 100)) / 1000.0

        async def producer() -> None:
            i = 0
            while not ws.closed:
                await asyncio.sleep(interval)
                if ws.closed:
                    return
                try:
                    await ws.send_json({"type": "progress", "p": i})
                    i += 1
                except ConnectionError:
                    return

        async def consumer() -> None:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    STATE["ws"]["echoes"] += 1
                    if not ws.closed:
                        await ws.send_json({"type": "echo", "data": msg.data})
                elif msg.type == WSMsgType.CLOSE:
                    return

        prod = asyncio.create_task(producer())
        try:
            await consumer()
        finally:
            prod.cancel()
            try:
                await prod
            except asyncio.CancelledError:
                pass
        STATE["ws"]["closed"] += 1
        return ws

    app.router.add_get("/health", health)
    app.router.add_get("/stream", sse_stream)
    app.router.add_get("/long", long_stream)
    app.router.add_get("/ws/progress", ws_progress)
    app._app_name = name  # type: ignore[attr-defined]
    return app
