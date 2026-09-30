"""GatewayQueue ordering and oldest-wait (spec 05 §3).

Regression: the queue is sorted by (-priority, queued_at), so its *tail* is
the newest slot in the lowest-priority group — not the globally oldest
request. oldest_wait_sec must be the max wait over all slots, or boosted/mixed
queues (and even plain FIFO queues with >1 slot) understate urgency.
"""

from __future__ import annotations

import time

from gpusybio.gateway.queue import GatewayQueue, RequestSlot


def _slot(rid: str, queued_at: float, priority: int = 5) -> RequestSlot:
    return RequestSlot(
        request_id=rid,
        gateway_id="gw",
        application_id="a1",
        priority=priority,
        queued_at=queued_at,
    )


def test_oldest_wait_fifo_same_priority():
    now = time.time()
    q = GatewayQueue()
    q.enqueue(_slot("r_old", now - 20))
    q.enqueue(_slot("r_new", now - 10))
    # service order is FIFO (oldest first); the tail is the *newest* slot
    assert [s.request_id for s in q.slots("gw")] == ["r_old", "r_new"]
    # oldest wait = the older slot's wait, not the tail's
    assert 19.0 <= q.oldest_wait_sec("gw") <= 21.5
    assert q.oldest_wait_sec("gw") > q.slots("gw")[-1].waiting_sec + 5.0


def test_oldest_wait_mixed_priorities_after_boost():
    now = time.time()
    q = GatewayQueue()
    q.enqueue(_slot("r_old", now - 20, priority=5))
    q.enqueue(_slot("r_new", now - 1, priority=5))
    # boost the *newest* request to the top (spec 11 §4 force-process)
    boosted = q.boost("r_new")
    assert boosted is not None
    assert q.slots("gw")[0].request_id == "r_new"
    # the globally oldest is still r_old, whatever the queue order
    assert 19.0 <= q.oldest_wait_sec("gw") <= 21.5


def test_oldest_wait_mixed_priorities_natural():
    now = time.time()
    q = GatewayQueue()
    q.enqueue(_slot("r_old", now - 20, priority=5))
    q.enqueue(_slot("r_new", now - 1, priority=9))  # naturally higher priority
    # sorted: r_new first (priority 9), r_old last (priority 5, tail)
    assert [s.request_id for s in q.slots("gw")] == ["r_new", "r_old"]
    # here the tail happens to be the oldest — but a third slot proves max():
    q.enqueue(_slot("r_mid", now - 5, priority=5))  # tail is now r_mid (newest)
    assert [s.request_id for s in q.slots("gw")] == ["r_new", "r_old", "r_mid"]
    assert 19.0 <= q.oldest_wait_sec("gw") <= 21.5


def test_oldest_wait_empty_and_removed():
    q = GatewayQueue()
    assert q.oldest_wait_sec("gw") == 0.0
    q.enqueue(_slot("r1", time.time() - 10))
    assert 9.0 <= q.oldest_wait_sec("gw") <= 11.5
    q.remove("r1")
    assert q.oldest_wait_sec("gw") == 0.0
