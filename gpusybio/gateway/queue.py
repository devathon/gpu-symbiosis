"""Per-gateway request queue (spec 05 §3): ordered by (priority, queued_at)."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field


@dataclass
class RequestSlot:
    request_id: str
    gateway_id: str
    application_id: str
    priority: int = 5
    queued_at: float = field(default_factory=time.time)
    metadata: dict = field(default_factory=dict)

    @property
    def waiting_sec(self) -> float:
        return time.time() - self.queued_at


_ORDER = lambda s: (-s.priority, s.queued_at)  # noqa: E731


class GatewayQueue:
    """One queue per gateway; ordering = (priority desc, FIFO by queued_at).

    Per-gateway storage is a ``deque`` so the hot ``pop`` (serve oldest /
    highest-priority) is O(1) via ``popleft``; the re-sort on enqueue/boost is
    O(n log n) but the queue is bounded by ``max_waiting_queue`` (default 20).
    """

    def __init__(self) -> None:
        self._queues: dict[str, deque[RequestSlot]] = {}
        self._by_id: dict[str, RequestSlot] = {}

    def _resort(self, gateway_id: str) -> None:
        self._queues[gateway_id] = deque(sorted(self._queues[gateway_id], key=_ORDER))

    def enqueue(self, slot: RequestSlot) -> None:
        q = self._queues.setdefault(slot.gateway_id, deque())
        q.append(slot)
        self._resort(slot.gateway_id)
        self._by_id[slot.request_id] = slot

    def pop(self, gateway_id: str) -> RequestSlot | None:
        q = self._queues.get(gateway_id)
        if not q:
            return None
        slot = q.popleft()
        self._by_id.pop(slot.request_id, None)
        return slot

    def get(self, request_id: str) -> RequestSlot | None:
        return self._by_id.get(request_id)

    def remove(self, request_id: str) -> RequestSlot | None:
        slot = self._by_id.pop(request_id, None)
        if slot is not None:
            q = self._queues.get(slot.gateway_id)
            if q and slot in q:
                q.remove(slot)
        return slot

    def length(self, gateway_id: str) -> int:
        return len(self._queues.get(gateway_id, []))

    def oldest_wait_sec(self, gateway_id: str) -> float:
        """Longest wait of any queued slot — the true oldest request.

        The queue is sorted by (-priority, queued_at), so the *tail* is only
        the oldest within its own priority group; after boost() creates mixed
        priorities it is the *newest* slot in that group. The globally oldest
        wait is the max over all slots (each slot's waiting_sec is independent
        of sort order)."""
        q = self._queues.get(gateway_id)
        if not q:
            return 0.0
        return max(s.waiting_sec for s in q)

    def slots(self, gateway_id: str) -> list[RequestSlot]:
        """Queued slots in service order (priority desc, FIFO by queued_at)."""
        return list(self._queues.get(gateway_id, []))

    def boost(self, request_id: str) -> RequestSlot | None:
        """Raise a queued request to the top of its queue (spec 11 §4
        'force-process'). Priority is set above the current queue max."""
        slot = self._by_id.get(request_id)
        if slot is None:
            return None
        q = self._queues.get(slot.gateway_id)
        if q:
            slot.priority = max(s.priority for s in q) + 1
        self._resort(slot.gateway_id)
        return slot

    def clear(self, gateway_id: str) -> list[RequestSlot]:
        """Cancel all queued requests (never active ones — spec 05 §10)."""
        q = self._queues.pop(gateway_id, [])
        for slot in q:
            self._by_id.pop(slot.request_id, None)
        return q

    def all_slots(self) -> list[RequestSlot]:
        return list(self._by_id.values())
