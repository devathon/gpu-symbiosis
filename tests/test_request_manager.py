"""Request lifecycle robustness: terminal-state resolution must not depend on
the DB. begin_forward removes the slot from the queue, so complete/cancel/
expire used to fall back to a DB lookup; when the initial persist failed
(_persist swallows its errors), the lookup returned None, app_id was empty,
and _finish skipped releasing the concurrency slot — the app clogged forever
(default limit 1: every later request stayed queued, no forward possible).
"""

from __future__ import annotations

from gpusybio.applications.lifecycle import ProcessState, ResidencyState
from gpusybio.events.bus import EventBus
from gpusybio.gateway.request_manager import RequestManager
from gpusybio.state import StateManager


class _BrokenDB:
    """A session factory whose every call raises — persist/status-update
    failures are swallowed by the manager (they must never break the request
    path), so the manager runs on a dead database."""

    def __call__(self):
        raise RuntimeError("db down")


class _Settings:
    requests = type("R", (), {"default_request_timeout_sec": 600})()


def _make_rm() -> RequestManager:
    app_state = StateManager()
    app_state.set_process("app1", ProcessState.STARTED)
    app_state.set_residency("app1", ResidencyState.LOADED)
    return RequestManager(
        app_state, _BrokenDB(), EventBus(), _Settings(),
        {"gw1": {"id": "gw1", "application_id": "app1", "max_waiting_queue": 50,
                 "priority": 1, "max_waiting_time_sec": 300,
                 "wait_timeout_policy": "ESCALATE_PRIORITY"}},
        {"app1": {"max_concurrent_requests": 1}},
    )


def test_complete_releases_slot_when_db_is_unavailable():
    """A request that was persisted to neither the queue nor the DB (persist
    failure) still releases its concurrency slot on complete — the app does
    not clog."""
    import asyncio

    rm = _make_rm()

    async def run():
        slot = await rm.receive("gw1")  # _persist raises, swallowed
        assert slot is not None
        app_id = await rm.begin_forward(slot.request_id)
        assert app_id == "app1"
        assert rm.active_count("app1") == 1

        await rm.complete(slot.request_id)
        # The slot is released even though the DB has no row for it.
        assert rm.active_count("app1") == 0

        # The app is forwardable again: the next request gets a slot.
        slot2 = await rm.receive("gw1")
        assert await rm.begin_forward(slot2.request_id) == "app1"
        assert rm.active_count("app1") == 1
        await rm.complete(slot2.request_id)
        assert rm.active_count("app1") == 0

    asyncio.run(run())


def test_cancel_inflight_releases_slot_when_db_is_unavailable():
    """Client disconnect on an in-flight request releases the slot even with
    a dead DB (cancel is the queue-full / disconnect path)."""
    import asyncio

    rm = _make_rm()

    async def run():
        slot = await rm.receive("gw1")
        assert slot is not None
        assert await rm.begin_forward(slot.request_id) == "app1"
        assert rm.active_count("app1") == 1

        await rm.cancel(slot.request_id)
        assert rm.active_count("app1") == 0
        assert rm.can_forward_now("gw1") is True

    asyncio.run(run())


def _make_rm_gw(gw_limit: int | None, app_limit: int) -> RequestManager:
    app_state = StateManager()
    app_state.set_process("app1", ProcessState.STARTED)
    app_state.set_residency("app1", ResidencyState.LOADED)
    gw = {"id": "gw1", "application_id": "app1", "max_waiting_queue": 50,
          "priority": 1, "max_waiting_time_sec": 300,
          "wait_timeout_policy": "ESCALATE_PRIORITY"}
    if gw_limit is not None:
        gw["max_concurrent_requests"] = gw_limit
    return RequestManager(
        app_state, _BrokenDB(), EventBus(), _Settings(),
        {"gw1": gw},
        {"app1": {"max_concurrent_requests": app_limit}},
    )


def test_gateway_limit_caps_inflight_below_app_limit():
    """The gateway's own concurrency cap bounds THIS gateway's in-flight
    requests even while the app's shared pool has room (min-of-two
    semantics). A missing gateway key means no gateway-level cap."""
    import asyncio

    rm = _make_rm_gw(1, 3)  # app allows 3, this gateway only 1 at a time

    async def run():
        slot = await rm.receive("gw1")
        assert await rm.begin_forward(slot.request_id) == "app1"
        assert rm.active_count("app1") == 1
        # Second request through the same gateway is held, although the app
        # still has 2 free slots.
        slot2 = await rm.receive("gw1")
        assert slot2 is not None
        assert await rm.begin_forward(slot2.request_id) is None
        assert rm.active_count("app1") == 1
        assert rm.queued_count("app1") == 1
        # Freeing the gateway's slot frees the app slot.
        await rm.complete(slot.request_id)
        assert rm.active_count("app1") == 0
        assert rm.can_forward_now("gw1") is True
        assert await rm.begin_forward(slot2.request_id) == "app1"
        assert rm.active_count("app1") == 1

    asyncio.run(run())


def test_app_limit_still_caps_when_gateway_limit_is_higher():
    """The app-level cap (shared pool) still bounds in-flight requests when
    the gateway's own cap is looser."""
    import asyncio

    rm = _make_rm_gw(5, 1)  # app allows 1, gateway allows 5

    async def run():
        slot = await rm.receive("gw1")
        assert await rm.begin_forward(slot.request_id) == "app1"
        slot2 = await rm.receive("gw1")
        assert slot2 is not None
        assert await rm.begin_forward(slot2.request_id) is None
        assert rm.active_count("app1") == 1
        assert rm.queued_count("app1") == 1
        await rm.complete(slot.request_id)
        assert await rm.begin_forward(slot2.request_id) == "app1"

    asyncio.run(run())


def test_missing_gateway_key_means_no_gateway_cap():
    """A gateway config without the key imposes no gateway-level cap — the
    app limit alone governs (backward compatible with pre-existing configs)."""
    import asyncio

    rm = _make_rm_gw(None, 2)

    async def run():
        slots = [await rm.receive("gw1") for _ in range(2)]
        assert all(s is not None for s in slots)
        assert await rm.begin_forward(slots[0].request_id) == "app1"
        assert await rm.begin_forward(slots[1].request_id) == "app1"
        assert rm.active_count("app1") == 2
        slot3 = await rm.receive("gw1")
        assert await rm.begin_forward(slot3.request_id) is None

    asyncio.run(run())


def test_app_pool_capped_across_gateways():
    """Two gateways on the same app: the app's shared pool (not each
    gateway's cap) bounds TOTAL in-flight across both."""
    import asyncio

    app_state = StateManager()
    app_state.set_process("app1", ProcessState.STARTED)
    app_state.set_residency("app1", ResidencyState.LOADED)
    rm = RequestManager(
        app_state, _BrokenDB(), EventBus(), _Settings(),
        {
            "gw1": {"id": "gw1", "application_id": "app1", "max_waiting_queue": 50,
                    "priority": 1, "max_waiting_time_sec": 300,
                    "wait_timeout_policy": "ESCALATE_PRIORITY",
                    "max_concurrent_requests": 2},
            "gw2": {"id": "gw2", "application_id": "app1", "max_waiting_queue": 50,
                    "priority": 1, "max_waiting_time_sec": 300,
                    "wait_timeout_policy": "ESCALATE_PRIORITY",
                    "max_concurrent_requests": 2},
        },
        {"app1": {"max_concurrent_requests": 2}},
    )

    async def run():
        r1 = await rm.receive("gw1")
        r2 = await rm.receive("gw2")
        r3 = await rm.receive("gw1")
        assert all(s is not None for s in (r1, r2, r3))
        assert await rm.begin_forward(r1.request_id) == "app1"
        assert await rm.begin_forward(r2.request_id) == "app1"
        # App pool (2) is full — gw1's own cap (2) still has room, but the
        # shared pool does not.
        assert rm.active_count("app1") == 2
        assert await rm.begin_forward(r3.request_id) is None
        assert rm.queued_count("app1") == 1
        # Freeing one slot (on gw2) lets the queued request forward.
        await rm.complete(r2.request_id)
        assert await rm.begin_forward(r3.request_id) == "app1"
        assert rm.active_count("app1") == 2

    asyncio.run(run())
