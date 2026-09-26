"""SPIKE 3 — SSE + long LLM stream through the proxy, client disconnect mid-stream
(spec 16 section 4).

Duration is compressed (2 s instead of minutes) but the pass criteria are the
ones that matter for multi-minute generations:

- NOT buffered: first chunk arrives well before the stream ends
- every token arrives (20/20)
- client disconnect mid-stream aborts the UPSTREAM request (app observes
  cancellation; active streams return to 0)
"""

import asyncio
import time

import aiohttp


async def test_sse_streams_without_buffering(stack):
    ports = stack["ports"]
    host = stack["host"]
    client = stack["client"]
    state = stack["state"]
    counters = stack["counters"]

    url = f"http://{host}:{ports['gw1']}/stream?tokens=20&interval_ms=100"
    tokens = []
    async with client.get(url) as resp:
        assert resp.status == 200
        assert resp.headers.get("content-type", "").startswith("text/event-stream")
        t0 = time.monotonic()
        first_at = None
        async for chunk in resp.content.iter_any():
            if first_at is None:
                first_at = time.monotonic()
            for line in chunk.decode().splitlines():
                if line.startswith("data: token-"):
                    tokens.append(line.removeprefix("data: "))
        total = time.monotonic() - t0

    assert len(tokens) == 20, f"expected 20 tokens, got {len(tokens)}: {tokens}"
    assert tokens[0] == "token-0" and tokens[-1] == "token-19"
    # Not buffered: the first chunk arrived in the first half of the stream.
    assert (first_at - t0) < total * 0.5, (
        f"first chunk at {first_at - t0:.2f}s of a {total:.2f}s stream — looks buffered"
    )
    assert state["streams"]["completed"] == 1
    assert counters["http_completed"] >= 1


async def test_long_chunked_stream(stack):
    ports = stack["ports"]
    host = stack["host"]
    client = stack["client"]
    state = stack["state"]

    url = f"http://{host}:{ports['gw2']}/long?chunks=10&interval_ms=80"
    received = []
    async with client.get(url) as resp:
        assert resp.status == 200
        async for chunk in resp.content.iter_any():
            received.append(chunk)
    payload = b"".join(received)
    for i in range(10):
        assert f"chunk-{i}:".encode() in payload
    assert state["long"]["completed"] == 1


async def test_client_disconnect_aborts_upstream(stack):
    ports = stack["ports"]
    host = stack["host"]
    client = stack["client"]
    state = stack["state"]
    counters = stack["counters"]

    # 50 tokens x 100 ms = ~5 s stream; the client leaves after the first chunk.
    url = f"http://{host}:{ports['gw1']}/stream?tokens=50&interval_ms=100"
    async with client.get(url) as resp:
        first = await resp.content.readany()
        assert b"token-0" in first
        # Exiting without consuming the rest closes the connection abruptly.
    resp.close()

    # Give the proxy + upstream time to notice.
    for _ in range(100):
        if state["streams"]["cancelled"] >= 1:
            break
        await asyncio.sleep(0.05)

    assert state["streams"]["cancelled"] >= 1, (
        "upstream request was NOT aborted after client disconnect "
        "(app kept generating — wasted GPU time)"
    )
    assert state["streams"]["active"] == 0
    assert counters["http_aborted"] >= 1
