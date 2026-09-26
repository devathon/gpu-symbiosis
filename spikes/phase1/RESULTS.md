# Phase 1 Spike Results

Run: `uv run pytest -v` in this directory (3 consecutive green runs, 7/7 tests).
Criteria: `spec/16-tech-stack.md` section 4.

## Spike 1 — WebSocket proxy through raw ASGI + aiohttp: **PASS**

`tests/test_spike1_websocket.py`

- ComfyUI-style WS progress bridged client→proxy→upstream and back:
  progress frames `p=0,1,2` received, client text `hello-gpu` echoed by the app
  through the proxy.
- Client disconnect tears the upstream session down: app-side `closed >= 1`,
  proxy counted a clean bridge teardown.
- App-side close is tolerated by the client (clean close, subprotocol
  passthrough verified on a second gateway).
- Raw ASGI only: `scope/receive/send` for `websocket.*` messages; no FastAPI
  request abstraction anywhere in the proxy path.

## Spike 2 — multi-port ASGI listeners in one process/loop: **PASS**

`tests/test_spike2_multiport.py`

- 3 gateway ports (9321–9323) + 1 admin port (9340) + 3 app ports (9231–9233)
  serving simultaneously from **one event loop in one process** (admin
  `/api/system` reports a single PID; all listeners verified live).
  Separate processes per gateway were **not** used.
- Per-gateway routing: each gateway serves only its bound target app
  (`/health` → correct app name per gateway).
- Gateways are independent: a request on gw1 increments only its target's
  hits; shared state is visible through the same-process admin API.

## Spike 3 — SSE + long stream with client-disconnect abort: **PASS**

`tests/test_spike3_sse_stream.py`

- 20-token SSE stream through the proxy: all tokens received in order,
  **first chunk at < 50% of total stream time** (no buffering).
- Long chunked stream (`/long`, 10 chunks): full content intact.
- Mid-stream client disconnect aborts upstream: app observed the cancellation
  (`streams.cancelled >= 1`, `active == 0`), proxy counted `http_aborted >= 1`,
  and a normal stream still counts `http_completed`.

## Defects found during the spikes (inputs to Phase 1 implementation)

1. **uvicorn needs `uvicorn[standard]` for WebSocket support** — the base
   package has no WS protocol implementation ("No supported WebSocket library
   detected"). Locked: `uvicorn[standard]` in the Phase 1 dependency set.
2. **ASGI scope query-string handling is implementation-specific.** uvicorn
   (h11 *and* httptools) puts the query in `scope["query_string"]` as **bytes**;
   `scope["query"]` is absent/None, and `scope["path"]` may be **bytes** under
   the httptools implementation. A naive `scope.get("query")` silently drops
   the entire query string. The proxy's `build_url()` now normalizes all three
   shapes (`query_string` bytes, `query`, query embedded in `path`).
3. **Completion races in the pump/disconnect `asyncio.wait`.** On a finished
   stream, the client's close and the pump's completion land in the same
   event-loop batch — both tasks appear in `done`. The branch must therefore
   be "stream task not in done ⇒ aborted", not "disconnect in done ⇒
   aborted" (the latter miscounts every clean stream end).
4. **WS close-initiator is unobservable at the proxy.** In a bridged close
   handshake the upstream CLOSE frame always precedes the client's
   `websocket.disconnect` (the close frame must be echoed before the connection
   drops), so both event orders are identical regardless of who initiated.
   The spike counts clean vs error teardown (`ws_bridged` / `ws_aborted`);
   initiator tracking is deferred — see the comment in `spike/proxy.py`.
5. **aiohttp 3.13 API notes** (mock app + tests): `StreamResponse` has no
   `content_type` kwarg (set the attribute), `enable_chunked()` is removed
   (chunked is the default without content-length), `ClientResponse.close()`
   is synchronous, and a server-side write after the client drops raises
   `ClientConnectionResetError` (a `ClientPayloadError`, **not** a
   `ConnectionError` subclass) — handler cancellation handlers must catch it.
6. **`asyncio.create_task(asyncio.gather(...))` is not a coroutine** — wrap the
   gather in an `async def run_all()` (see `spike/server.py`).

## Intentional spike limitations (documented in code)

- Request bodies are **buffered** before forwarding; pipe-through async
  generator is Phase 1 implementation work (needed for large ComfyUI uploads).
- Ports are fixed (9231–9233 apps, 9321–9323 gateways, 9340 admin) rather than
  ephemeral.
