"""Raw-ASGI gateway proxy — spike implementation of spec 05 section 7 / 16 section 2.1.

One ASGI app per gateway port. Pips requests through to the target application:

- HTTP: body piped through (buffered in this spike; pipe-through is the Phase 1
  implementation), response streamed chunk-by-chunk WITHOUT buffering (SSE /
  chunked / long generations), client disconnect cancels the upstream request.
- WebSocket: bidirectional bridge; whichever side closes first tears the
  other down (spec 05 section 8).

No FastAPI request abstraction is involved — only ASGI scope/receive/send.
"""

from __future__ import annotations

import asyncio
import contextlib

from aiohttp import ClientSession, ClientTimeout, WSMsgType

# Stripped before forwarding to upstream (hop-by-hop / connection-specific).
HOP_BY_HOP = {
    b"connection",
    b"keep-alive",
    b"proxy-authenticate",
    b"proxy-authorization",
    b"te",
    b"trailer",
    b"transfer-encoding",
    b"upgrade",
    b"accept-encoding",  # keep upstream identity-encoded so we can pass bytes through
    b"host",
}

_NO_TIMEOUT = ClientTimeout(total=None, sock_read=60 * 60)


class ClientGone(Exception):
    """Client disconnected before the request was fully received."""


def make_proxy(target: str, session: ClientSession, counters: dict) -> "ASGIApp":
    """Build a raw ASGI app for one gateway port, bound to one target app."""

    async def watch_disconnect(receive) -> None:
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return

    def build_url(scope) -> str:
        # uvicorn h11 puts the query in scope["query_string"] (bytes); other ASGI
        # servers may use scope["query"] or embed it in scope["path"]. Handle all
        # three without double-appending.
        path = scope["path"]
        if isinstance(path, bytes):
            path = path.decode("ascii")
        query = scope.get("query_string") or scope.get("query") or b""
        if isinstance(query, str):
            query = query.encode()
        if query and "?" not in path:
            return target + path + f"?{query.decode()}"
        return target + path

    async def proxy_http(scope, receive, send) -> None:
        url = build_url(scope)

        # Spike: buffer the request body. Phase 1 implementation: pipe-through
        # async generator for large uploads (e.g. ComfyUI image uploads).
        body = b""
        more = True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                raise ClientGone()
            body += message.get("body", b"")
            more = message.get("more_body", False)

        headers = {
            k.decode(): v.decode()
            for k, v in scope.get("headers", [])
            if k not in HOP_BY_HOP
        }

        async with session.request(
            scope["method"],
            url,
            headers=headers,
            data=body or None,
            timeout=_NO_TIMEOUT,
        ) as resp:
            start_headers = [
                (k.encode(), v.encode())
                for k, v in resp.headers.items()
                if k.lower() not in {h.decode() for h in HOP_BY_HOP}
            ]
            await send(
                {
                    "type": "http.response.start",
                    "status": resp.status,
                    "headers": start_headers,
                }
            )

            async def pump() -> None:
                async for chunk in resp.content.iter_any():
                    await send({"type": "http.response.body", "body": chunk, "more_body": True})
                await send({"type": "http.response.body", "body": b""})

            stream_task = asyncio.create_task(pump())
            disconnect_task = asyncio.create_task(watch_disconnect(receive))
            done, _ = await asyncio.wait(
                {stream_task, disconnect_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if stream_task not in done:
                # Client left mid-stream: abort the upstream request.
                counters["http_aborted"] += 1
                stream_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stream_task
            else:
                # Stream finished (or errored) — even if the client's close landed
                # in the same event-loop batch, the full body was delivered.
                if stream_task.cancelled() or (exc := stream_task.exception()) is not None:
                    disconnect_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await disconnect_task
                    if exc is not None:
                        raise exc
                counters["http_completed"] += 1
                disconnect_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await disconnect_task

    async def proxy_ws(scope, receive, send) -> None:
        ws_url = build_url(scope).replace("http://", "ws://", 1)
        protocols = scope.get("subprotocols") or None
        async with session.ws_connect(ws_url, protocols=protocols) as ws:
            accept: dict = {"type": "websocket.accept"}
            if ws.protocol:
                accept["subprotocol"] = ws.protocol
            await send(accept)

            async def client_to_upstream() -> str:
                while True:
                    message = await receive()
                    if message["type"] == "websocket.disconnect":
                        with contextlib.suppress(Exception):
                            await ws.close(code=message.get("code", 1000))
                        return "client"
                    if message.get("text") is not None:
                        await ws.send_str(message["text"])
                    if message.get("bytes") is not None:
                        await ws.send_bytes(message["bytes"])

            async def upstream_to_client() -> str:
                async for msg in ws:
                    if msg.type == WSMsgType.TEXT:
                        await send({"type": "websocket.send", "text": msg.data})
                    elif msg.type == WSMsgType.BINARY:
                        await send({"type": "websocket.send", "bytes": msg.data})
                    elif msg.type == WSMsgType.CLOSE:
                        return "server"
                    elif msg.type == WSMsgType.ERROR:
                        return "error"

            c2u = asyncio.create_task(client_to_upstream())
            u2c = asyncio.create_task(upstream_to_client())
            order: list[tuple[str, str]] = []

            async def remember(task: asyncio.Task, name: str) -> None:
                # Record completion order so the initiator is deterministic even
                # when both pumps finish in the same event-loop batch.
                try:
                    result = await task
                except asyncio.CancelledError:
                    return
                except Exception:
                    order.append((name, "error"))
                    return
                order.append((name, result))

            w_c2u = asyncio.create_task(remember(c2u, "client"))
            w_u2c = asyncio.create_task(remember(u2c, "server"))
            await asyncio.wait({w_c2u, w_u2c}, return_when=asyncio.FIRST_COMPLETED)
            for task in (c2u, u2c, w_c2u, w_u2c):
                if not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
            try:
                await send({"type": "websocket.close", "code": 1000})
            except Exception:
                pass  # client already gone
            # Spike limitation: the proxy CANNOT tell who initiated a clean close
            # — in a bridged close handshake the upstream CLOSE frame always
            # precedes the client's websocket.disconnect (the close frame must be
            # echoed before the connection drops), so both sides' events fire in
            # the same order regardless of initiator. Count by cleanliness
            # instead: a clean teardown of a working bridge is "bridged"; an
            # error teardown is "aborted". (Phase 1 implementation can track the
            # first close frame per side if initiator stats are ever needed.)
            clean = bool(order) and all(result != "error" for _, result in order)
            counters["ws_bridged" if clean else "ws_aborted"] += 1

    async def app(scope, receive, send) -> None:
        if scope["type"] == "http":
            await proxy_http(scope, receive, send)
        elif scope["type"] == "websocket":
            await proxy_ws(scope, receive, send)
        # ASGI lifespan: not used in the spike (lifespan="off" on the servers)

    app.target = target  # type: ignore[attr-defined]
    return app
