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
    app_state.set_process("app1", ProcessState.RUNNING)
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
