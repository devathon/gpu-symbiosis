"""Raw ASGI gateway proxy (spec 05 §6/§8): queuing + immediate-forward path.

Implements the spike-validated streaming pattern (spikes/phase1/spike/proxy.py):
- pump raced against client disconnect (http.disconnect),
- client disconnect during processing aborts the upstream,
- WebSocket bridging with clean-vs-error close accounting.

Flow per HTTP request (spec 05 §6):
  gateway enabled?  → 503 if not
  app ready + slot? → forward immediately
  else              → QUEUED, wait for the scheduler's forward signal
                      (client disconnect → CANCELLED; wait timeout → policy)
"""

from __future__ import annotations

import asyncio
import time

from gpusybio.events.log import get_logger
from gpusybio.gateway.request_manager import RequestManager

log = get_logger("gateway-proxy")


def _token_headers(resp) -> tuple[int | None, int | None]:
    """Per-request token counts from the upstream response headers (spec 08 §9.2)."""
    h = resp.headers

    def _int(name: str) -> int | None:
        v = h.get(name)
        if v is None:
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    return _int("X-Input-Tokens"), _int("X-Output-Tokens")


# Forwarded headers that must not be duplicated
_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
}


def build_path(scope: dict) -> str:
    """Request path + query from an ASGI scope (spike finding: bytes fields)."""
    path = scope["path"]
    if isinstance(path, bytes):
        path = path.decode()
    query = scope.get("query_string", b"")
    if isinstance(query, bytes):
        query = query.decode()
    if not query and isinstance(scope.get("query"), (str, bytes)):
        query = scope["query"].decode() if isinstance(scope["query"], bytes) else scope["query"]
    if query:
        return f"{path}?{query}"
    return path


def _client_headers(scope: dict) -> list[tuple[str, str]]:
    """Decode ASGI header pairs (bytes) to str pairs for aiohttp."""
    out = []
    for k, v in scope.get("headers", []):
        key = k.decode() if isinstance(k, bytes) else str(k)
        val = v.decode() if isinstance(v, bytes) else str(v)
        if key.lower() in _HOP_HEADERS:
            continue
        out.append((key, val))
    return out


def make_gateway_proxy(
    gateway: dict,
    request_manager: RequestManager,
    session,
    app_state,
    app_cfg,
    verify_key=None,
    should_schedule=None,
):
    """Build the ASGI app for one gateway port.

    ``verify_key`` (optional, spec 12 §2) is a callable ``presented_key -> bool``
    checked **before** queueing: an invalid key → 401 and the request never
    enters the queue.

    ``should_schedule`` (optional, spec 05 §6) is a callable
    ``(method, path) -> bool``: when it returns ``False`` the request is
    forwarded directly (no intake row, no queue, no demand) — even while the
    app is stopped. ``None`` (the default) keeps the existing behaviour (every
    request goes through the scheduler).
    """
    gateway_id = gateway["id"]
    app_id = gateway["application_id"]
    app_port = app_cfg["port"]
    upstream = f"http://127.0.0.1:{app_port}"

    def _bearer(scope: dict) -> str | None:
        for k, v in scope.get("headers", []):
            key = k.decode() if isinstance(k, bytes) else str(k)
            if key.lower() == "authorization":
                val = v.decode() if isinstance(v, bytes) else str(v)
                if val.lower().startswith("bearer "):
                    return val[7:].strip()
        return None

    async def _auth_ok(scope: dict, send) -> bool:
        """Return True if the request may proceed; otherwise send a 401."""
        if verify_key is None:
            return True
        if verify_key(_bearer(scope)):
            return True
        try:
            await _send_error(send, 401, "invalid or missing API key")
        except Exception:  # noqa: BLE001
            pass
        return False

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        elif scope["type"] == "http":
            await _handle_http(scope, receive, send)
        elif scope["type"] == "websocket":
            await _handle_ws(scope, receive, send)
        else:
            log.warning("unhandled scope type", type=scope.get("type"))

    # -- helpers ----------------------------------------------------------------
    async def _read_body(receive):
        body = b""
        more = True
        while more:
            message = await receive()
            if message["type"] == "http.request":
                body += message.get("body", b"")
                more = message.get("more_body", False)
            elif message["type"] == "http.disconnect":
                return None
        return body

    async def _send_response(send, status, headers, chunks):
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": headers,
            }
        )
        if chunks is None:
            await send({"type": "http.response.body", "body": b""})
            return True
        for c in chunks:
            await send({"type": "http.response.body", "body": c, "more_body": True})
        await send({"type": "http.response.body", "body": b""})
        return True

    async def _send_error(send, status, message, content_type="text/plain"):
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", content_type.encode()),
                            (b"content-length", str(len(message)).encode())],
            }
        )
        await send({"type": "http.response.body", "body": message.encode()})

    # -- HTTP ---------------------------------------------------------------------
    async def _handle_http(scope, receive, send):
        body = await _read_body(receive)
        if body is None:
            return  # client disconnected during body read

        # API-key auth (spec 12 §2) — checked before queueing.
        if not await _auth_ok(scope, send):
            return

        if not gateway.get("enabled", True):
            await _send_error(send, 503, "gateway disabled")
            return

        # Routing (spec 05 §6): a request the application type marks as
        # pass-through is forwarded directly — no intake row, no queue, no
        # demand — even while the app is stopped.
        path = scope.get("path")
        if isinstance(path, bytes):
            path = path.decode()
        if should_schedule is not None and not should_schedule(
            scope.get("method", "GET"), path
        ):
            await _forward_direct(scope, send, body, receive)
            return

        # 1. Intake: always create the request row (metrics + Invariant 5).
        slot = await request_manager.receive(gateway_id, metadata={
            "method": scope.get("method"),
            "path": scope.get("path").decode() if isinstance(scope.get("path"), bytes) else scope.get("path"),
        })
        if slot is None:
            await _send_error(send, 429, "queue full")
            return
        request_id = slot.request_id

        # 2. Immediate-forward path (spec 05 §6: app available → forward).
        app_id2 = await request_manager.begin_forward(request_id)
        if app_id2 is None:
            # 3. Queued: wait for the scheduler's forward signal.
            signaled = await _wait_for_signal(slot, receive, send)
            if signaled is False:
                return  # client disconnected or policy rejected — already handled
            app_id2 = await request_manager.begin_forward(request_id)
            if app_id2 is None:
                # Lost the race: re-enter the wait (slot still queued).
                signaled = await _wait_for_signal(slot, receive, send)
                if signaled is False:
                    return
                app_id2 = await request_manager.begin_forward(request_id)
                if app_id2 is None:
                    await request_manager.cancel(request_id)
                    await _send_error(send, 503, "could not allocate a slot")
                    return

        # 4. Forward to the upstream (spike-validated streaming pattern).
        try:
            status, resp = await _forward(scope, send, body, request_id)
            if resp is None:
                await request_manager.complete(request_id, error=None if status < 500 else f"upstream {status}")
            else:
                tokens = _token_headers(resp)  # per-request token counts (spec 08 §9.2)
                await _stream(send, resp, receive, request_id, status, tokens)
        except (asyncio.CancelledError,):
            raise
        except Exception as e:  # noqa: BLE001 - upstream unreachable etc.
            log.warning("forward failed", request_id=request_id, error=str(e))
            try:
                await _send_error(send, 502, "upstream unreachable")
            except Exception:  # noqa: BLE001
                pass
            await request_manager.complete(request_id, error=str(e)[:200])

    async def _stream(send, resp, receive, request_id: str, status: int,
                     tokens: tuple[int | None, int | None] | None = None):
        """Pump the upstream body to the client, racing client disconnect."""
        in_tok, out_tok = (tokens or (None, None))
        stream = resp.content

        async def pump():
            async for chunk in stream:
                await send(
                    {"type": "http.response.body", "body": chunk, "more_body": True}
                )

        async def watch_disconnect():
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return True
                # ignore stray http.request messages

        stream_task = asyncio.create_task(pump())
        disconnect_task = asyncio.create_task(watch_disconnect())
        done, _pending = await asyncio.wait(
            {stream_task, disconnect_task}, return_when=asyncio.FIRST_COMPLETED
        )
        try:
            if stream_task not in done:
                # Client went away first: abort the upstream (spike pattern).
                stream_task.cancel()
                disconnect_task.cancel()
                try:
                    await stream_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
                if request_id is not None:
                    await request_manager.cancel(request_id)
                return
            disconnect_task.cancel()
            exc = stream_task.exception()
            if exc is not None:
                log.warning("upstream stream error", request_id=request_id, error=str(exc))
                try:
                    await _send_error(send, 502, "upstream error")
                except Exception:  # noqa: BLE001
                    pass
                if request_id is not None:
                    await request_manager.complete(request_id, error=str(exc)[:200])
            else:
                await send({"type": "http.response.body", "body": b""})
                if request_id is not None:
                    await request_manager.complete(
                        request_id, error=None if status < 500 else f"upstream {status}",
                        input_tokens=in_tok, output_tokens=out_tok,
                    )
        finally:
            try:
                resp.close()  # sync in aiohttp 3.13
            except Exception:  # noqa: BLE001
                pass

    async def _forward_direct(scope, send, body: bytes, receive):
        """Direct forward (spec 05 §6): pass-through requests (reads, static,
        websocket) bypass the scheduler — no intake row, no queue, no demand."""
        try:
            status, resp = await _forward(scope, send, body, None)
            if resp is not None:
                tokens = _token_headers(resp)
                await _stream(send, resp, receive, None, status, tokens)
        except (asyncio.CancelledError,):
            raise
        except Exception as e:  # noqa: BLE001 - upstream unreachable etc.
            log.warning("direct forward failed", error=str(e))
            try:
                await _send_error(send, 502, "upstream unreachable")
            except Exception:  # noqa: BLE001
                pass

    async def _forward(scope, send, body: bytes, request_id: str):
        """Send to upstream (app port), stream the response headers+body.

        Returns (status, resp) with resp=None for small/empty bodies (already
        sent); the caller streams resp.content and closes resp.
        """
        target = f"{upstream}/{build_path(scope).lstrip('/')}"
        method = scope.get("method", "GET")
        headers = _client_headers(scope)
        headers.append(("host", upstream.split("//", 1)[1].split("/", 1)[0]))

        # NOTE: not `async with` — the response must stay open so the caller can
        # stream the body; the connector releases it after the stream completes.
        resp = await session.request(
            method, target, data=body if body else None, headers=headers
        )
        try:
            headers_out = [
                (k.encode(), v.encode() if isinstance(v, str) else str(v).encode())
                for k, v in resp.headers.items()
                if k.lower() not in _HOP_HEADERS and k.lower() not in ("content-length",)
            ]
            await send(
                {"type": "http.response.start", "status": resp.status, "headers": headers_out}
            )
            content_length = resp.headers.get("Content-Length")
            transfer_encoding = resp.headers.get("Transfer-Encoding", "").lower()
            if content_length == "0" or (
                content_length is None and transfer_encoding == "identity"
            ):
                # Small/empty body: read it fully and close the connection.
                data = await resp.read()
                await send({"type": "http.response.body", "body": data})
                resp.release()
                return resp.status, None
            # Streaming body: caller streams resp.content, then closes resp.
            return resp.status, resp
        except Exception:  # noqa: BLE001
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass
            raise

    async def _wait_for_signal(slot, receive, send) -> bool:
        """Wait for the forward signal, racing client disconnect + policies."""
        gw = request_manager.gateway(slot.gateway_id)
        max_wait = gw.get("max_waiting_time", 300)
        policy = gw.get("wait_timeout_policy", "ESCALATE_PRIORITY")
        deadline = time.monotonic() + max_wait
        escalated = False

        while True:
            remaining = max(0.05, deadline - time.monotonic())
            wait_signal = asyncio.create_task(request_manager.wait_forward(slot.request_id, remaining))
            watch = asyncio.create_task(receive())
            done, _ = await asyncio.wait({wait_signal, watch}, return_when=asyncio.FIRST_COMPLETED)

            if wait_signal in done and wait_signal.result():
                watch.cancel()
                # The event was set: either the scheduler signaled a forward, or the
                # request reached a terminal state (cancelled/expired). Distinguish.
                if request_manager.is_terminal(slot.request_id):
                    try:
                        await _send_error(send, 409, "request cancelled")
                    except Exception:  # noqa: BLE001
                        pass
                    return False
                return True
            if watch in done:
                msg = watch.result()
                wait_signal.cancel()
                if msg.get("type") == "http.disconnect":
                    # Invariant 6: client gone → cancel the queued request.
                    await request_manager.cancel(slot.request_id)
                    return False
                # stray message — keep waiting
            # Timeout without signal: apply the wait-timeout policy.
            if time.monotonic() >= deadline:
                if policy in ("REJECT", "EXPIRE"):
                    if policy == "EXPIRE":
                        await request_manager.expire(slot.request_id)
                    else:
                        await request_manager.cancel(slot.request_id)
                    try:
                        await _send_error(send, 503, "wait timeout")
                    except Exception:  # noqa: BLE001
                        pass
                    return False
                if policy == "PROCESS_ANYWAY":
                    request_manager.signal_forward(slot.request_id)
                    return True
                # ESCALATE_PRIORITY: bump and keep waiting (one escalation).
                if not escalated:
                    escalated = True
                    slot.priority += 5
                    deadline = time.monotonic() + max_wait
                    q = request_manager.queue()
                    q.remove(slot.request_id)
                    q.enqueue(slot)
                    log.info(
                        "queue priority escalated",
                        request_id=slot.request_id,
                        priority=slot.priority,
                    )
            if time.monotonic() >= deadline and escalated:
                # Second timeout after escalation: expire.
                await request_manager.expire(slot.request_id)
                try:
                    await _send_error(send, 503, "wait timeout")
                except Exception:  # noqa: BLE001
                    pass
                return False

    # -- WebSocket (Phase 1: direct bridge when ready; no WS queueing yet) ----
    async def _handle_ws(scope, receive, send):
        import aiohttp

        # API-key auth (spec 12 §2) — before bridging.
        if not await _auth_ok(scope, send):
            return

        if not gateway.get("enabled", True) or not request_manager.can_forward_now(gateway_id):
            try:
                await send({"type": "websocket.accept"})
                await send({"type": "websocket.close", "code": 1013})
            except Exception:  # noqa: BLE001
                pass
            return

        target = f"{upstream}/{build_path(scope).lstrip('/')}"
        try:
            async with session.ws_connect(
                target,
                protocols=[s.decode() if isinstance(s, bytes) else s for s in scope.get("subprotocols", [])],
            ) as ws:
                await send({"type": "websocket.accept", "subprotocols": []})

                async def client_to_upstream():
                    while True:
                        message = await receive()
                        if message["type"] == "websocket.receive":
                            if message.get("bytes") is not None:
                                await ws.send_bytes(message["bytes"])
                            else:
                                await ws.send_str(message.get("text", ""))
                        elif message["type"] == "websocket.disconnect":
                            await ws.close(code=message.get("code", 1000))
                            return

                async def upstream_to_client():
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await send({"type": "websocket.send", "text": msg.data})
                        elif msg.type == aiohttp.WSMsgType.BYTES:
                            await send({"type": "websocket.send", "bytes": msg.data})
                        elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
                            await send({"type": "websocket.close", "code": msg.code or 1000})
                            return

                t1 = asyncio.create_task(client_to_upstream())
                t2 = asyncio.create_task(upstream_to_client())
                done, _ = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
                for t in (t1, t2):
                    if t not in done:
                        t.cancel()
        except (asyncio.CancelledError,):
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("ws bridge failed", gateway_id=gateway_id, error=str(e))
            try:
                await send({"type": "websocket.close", "code": 1011})
            except Exception:  # noqa: BLE001
                pass

    return app
