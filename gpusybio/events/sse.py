"""SSE hub for the dashboard (spec 11 §1: live updates via SSE).

Subscribers are async iterators of JSON strings. The hub is an EventBus
observer: every event (and every state snapshot pushed by the server) reaches
all live dashboard connections.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator


class SSEHub:
    def __init__(self, maxsize: int = 500) -> None:
        self._clients: set[asyncio.Queue] = set()
        self._maxsize = maxsize

    async def push(self, payload: dict) -> None:
        if not self._clients:
            return
        line = "data: " + json.dumps(payload) + "\n\n"
        for q in list(self._clients):
            if q.full():
                q.get_nowait()
            q.put_nowait(line)

    def subscribe(self) -> "SSEStream":
        return SSEStream(self)

    async def _add(self, q: asyncio.Queue) -> None:
        self._clients.add(q)

    async def _remove(self, q: asyncio.Queue) -> None:
        self._clients.discard(q)


class SSEStream:
    def __init__(self, hub: SSEHub) -> None:
        self._hub = hub
        self._queue: asyncio.Queue | None = None

    async def __aenter__(self) -> "SSEStream":
        self._queue = asyncio.Queue(maxsize=self._hub._maxsize)
        await self._hub._add(self._queue)
        return self

    async def __aexit__(self, *exc):
        if self._queue is not None:
            await self._hub._remove(self._queue)
            self._queue = None

    async def __aiter__(self) -> AsyncIterator[str]:
        assert self._queue is not None
        while True:
            line = await self._queue.get()
            yield line
