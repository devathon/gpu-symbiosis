"""Gateway pause (spec 11): a paused gateway keeps accepting requests but stops
forwarding them to the app — they hold in the queue until unpause.

Unit level: the pause gate (can_forward_now), the scheduler candidate exclusion
(_gateway_score), and the proxy hold path (intake + wait, pass-through exempt).
E2E level: pause → request queued (not forwarded) → unpause → processed;
in-flight requests finish while paused; disconnect cancels; queue-full 429;
the paused state persists in the DB and shows in the gateway view.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from conftest import A1_GW, ADMIN_PORT, _app_state, _req_done, _wait
from gpusybio.applications.lifecycle import ProcessState, ResidencyState
from gpusybio.config.models import SettingsSpec
from gpusybio.events.bus import EventBus
from gpusybio.gateway.proxy import make_gateway_proxy
from gpusybio.gateway.request_manager import RequestManager
from gpusybio.scheduler.scoring import ScoringMixin
from gpusybio.state import StateManager

ADMIN = f"http://127.0.0.1:{ADMIN_PORT}"


# -- unit: the pause gate --------------------------------------------------------
class _BrokenDB:
    def __call__(self):
        raise RuntimeError("db down")


class _Settings:
    requests = type("R", (), {"default_request_timeout_sec": 600})()


def _make_rm(paused: bool) -> RequestManager:
    app_state = StateManager()
    app_state.set_process("app1", ProcessState.STARTED)
    app_state.set_residency("app1", ResidencyState.LOADED)
    gw = {"id": "gw1", "application_id": "app1", "max_waiting_queue": 50,
          "priority": 1, "max_waiting_time_sec": 300,
          "wait_timeout_policy": "ESCALATE_PRIORITY", "enabled": True}
    if paused:
        gw["paused"] = True
    return RequestManager(
        app_state, _BrokenDB(), EventBus(), _Settings(),
        {"gw1": gw}, {"app1": {"max_concurrent_requests": 1}},
    )


def test_can_forward_now_false_when_paused():
    """The pause gate: a paused gateway never forwards (immediate, post-signal,
    and WebSocket paths all share can_forward_now); unpause restores it."""
    assert _make_rm(paused=True).can_forward_now("gw1") is False
    assert _make_rm(paused=False).can_forward_now("gw1") is True


def test_gateway_score_none_when_paused():
    """A paused gateway is never a scheduler candidate: its queued requests must
    not trigger an app load (or a forward) until the gateway is unpaused."""
    for paused in (True, False):
        rm = _make_rm(paused=paused)
        asyncio.run(rm.receive("gw1"))  # a queued request
        stub = type("S", (ScoringMixin,), {"_rm": rm, "_boosts": {}, "_settings": SettingsSpec()})()
        sc = stub._gateway_score("gw1")
        if paused:
            assert sc is None
        else:
            assert sc is not None and sc["score"] > 0


# -- unit: the proxy hold path ---------------------------------------------------
def _drive(proxy, method: str = "GET", path: str = "/index.html") -> list:
    sent: list = []

    async def send(msg):
        sent.append(msg)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {"type": "http", "method": method, "path": path, "headers": [],
             "query_string": b""}
    asyncio.run(proxy(scope, receive, send))
    return sent


def test_paused_scheduled_request_held_until_signal():
    """spec 11: while paused the request is accepted (intake reached), the first
    forward is refused, and it waits for the scheduler — which signals after
    unpause. It is never rejected (no cancel) while held."""
    app_state = StateManager()
    app_state.set_process("app1", ProcessState.STARTED)
    app_state.set_residency("app1", ResidencyState.LOADED)

    class _Slot:
        request_id = "req-1"
        gateway_id = "gw1"
        application_id = "app1"
        queued_at = 0.0

    class _RM:
        def __init__(self):
            self.receive_called = False
            self.begin_forward_calls = 0
            self.cancel_called = False

        def gateway(self, gateway_id):
            return {"id": "gw1", "application_id": "app1", "enabled": True,
                    "paused": True, "max_waiting_queue": 50, "priority": 1,
                    "max_waiting_time_sec": 300, "wait_timeout_policy": "ESCALATE_PRIORITY",
                    "request_timeout_sec": 600}

        async def receive(self, gateway_id, metadata=None):
            self.receive_called = True
            return _Slot()

        async def begin_forward(self, request_id):
            self.begin_forward_calls += 1
            return None if self.begin_forward_calls == 1 else "app1"

        async def wait_forward(self, request_id, timeout):
            return True  # the scheduler signaled (after unpause)

        def is_terminal(self, request_id):
            return False

        async def cancel(self, request_id):
            self.cancel_called = True

        async def complete(self, *a, **k):
            return None

    rm = _RM()
    proxy = make_gateway_proxy(
        gateway={"id": "gw1", "application_id": "app1", "enabled": True, "paused": True},
        request_manager=rm, session=None, app_state=app_state,
        app_cfg={"id": "app1", "port": 7777, "max_concurrent_requests": 8},
        should_schedule=lambda m, p: p == "/api/prompt",
    )

    sent = _drive(proxy, method="POST", path="/api/prompt")
    assert rm.receive_called is True  # accepted — not a 503 rejection
    assert rm.begin_forward_calls == 2  # refused while paused, forwarded on signal
    assert rm.cancel_called is False
    assert sent[0]["status"] == 502  # forward attempted (no live upstream), not a hold rejection


def test_paused_passthrough_still_forwarded():
    """spec 11: the pause gates only scheduled (GPU) requests. Pass-through
    requests (no intake, no queue) are forwarded directly while paused — they
    don't consume the GPU. (502 = upstream unreachable, never the 503 hold.)"""
    app_state = StateManager()
    app_state.set_process("app1", ProcessState.STARTED)
    proxy = make_gateway_proxy(
        gateway={"id": "gw1", "application_id": "app1", "enabled": True, "paused": True},
        request_manager=None, session=None, app_state=app_state,
        app_cfg={"id": "app1", "port": 7777, "max_concurrent_requests": 8},
        should_schedule=lambda m, p: p.startswith("/api/"),
    )

    sent = _drive(proxy, method="GET", path="/index.html")
    assert sent[0]["status"] == 502


# -- e2e ------------------------------------------------------------------------
async def _a1_loaded(c) -> bool:
    d = await _app_state(c, "a1")
    return d["process_state"] == "STARTED" and d["residency_state"] == "LOADED"


async def _gw_view(c, gw_id="gw-a1"):
    rows = (await c.get(f"{ADMIN}/api/gateways")).json()
    return next(g for g in rows if g["id"] == gw_id)


async def test_pause_accepts_holds_then_unpause_processes(server):
    """spec 11: paused → request accepted and queued (not forwarded); unpause →
    the scheduler forwards it and it completes. The paused flag persists in the
    DB and the view reports PAUSED."""
    async with httpx.AsyncClient() as c:
        await c.post(f"{ADMIN}/api/applications/a1/start")
        await _wait(lambda: _a1_loaded(c), timeout=20)

        r = await c.post(f"{ADMIN}/api/gateways/gw-a1/pause")
        assert r.status_code == 200 and r.json()["paused"] is True
        view = await _gw_view(c)
        assert view["paused"] is True
        assert view["state"] == "PAUSED"
        # Persisted (survives a restart; the seed never overwrites it).
        from gpusybio.database.models import Gateway

        with server.db() as s:
            assert s.get(Gateway, "gw-a1").paused is True

        # A request while paused: accepted, queued, NOT forwarded.
        req_task = asyncio.create_task(
            c.post(f"http://127.0.0.1:{A1_GW}/generate", json={"chunks": 3, "delay": 0.05}, timeout=60)
        )
        await _wait(lambda: _req_done(c, "QUEUED", "gw-a1"), timeout=20)
        view = await _gw_view(c)
        assert view["queue"]["length"] >= 1
        # No progress while held.
        assert len((await c.get(f"{ADMIN}/api/requests?status=COMPLETED&gateway_id=gw-a1")).json()) == 0

        # Unpause → the queued request is forwarded and completes.
        r = await c.post(f"{ADMIN}/api/gateways/gw-a1/unpause")
        assert r.status_code == 200 and r.json()["paused"] is False
        view = await _gw_view(c)
        assert view["paused"] is False
        assert view["state"] != "PAUSED"

        resp = await _wait_task(req_task)
        assert resp.status_code == 200
        await _wait(lambda: _req_done(c, "COMPLETED", "gw-a1"), timeout=20)


async def _wait_task(task, timeout=30.0):
    done, _ = await asyncio.wait({task}, timeout=timeout)
    assert task in done, "task did not complete"
    return task.result()


async def test_inflight_continues_while_paused(server):
    """spec 11: an in-flight request finishes normally while the gateway is
    paused — the pause only gates new forwards, never running work."""
    async with httpx.AsyncClient() as c:
        await c.post(f"{ADMIN}/api/applications/a1/start")
        await _wait(lambda: _a1_loaded(c), timeout=20)

        # A long request holds a1's single slot while it streams.
        inflight = asyncio.create_task(
            c.post(f"http://127.0.0.1:{A1_GW}/generate", json={"chunks": 12, "delay": 0.15}, timeout=60)
        )
        await _wait(lambda: _req_done(c, "PROCESSING", "gw-a1"), timeout=20)

        # Pause mid-stream: the in-flight request is unaffected.
        r = await c.post(f"{ADMIN}/api/gateways/gw-a1/pause")
        assert r.status_code == 200
        resp = await _wait_task(inflight)
        assert resp.status_code == 200
        await _wait(lambda: _req_done(c, "COMPLETED", "gw-a1"), timeout=20)

        await c.post(f"{ADMIN}/api/gateways/gw-a1/unpause")


async def test_disconnect_while_paused_cancels(server):
    """Invariant 6 holds under pause: a client that disconnects while its
    request is held in the paused gateway's queue cancels it (queue cleaned)."""
    async with httpx.AsyncClient() as c:
        await c.post(f"{ADMIN}/api/applications/a1/start")
        await _wait(lambda: _a1_loaded(c), timeout=20)
        await c.post(f"{ADMIN}/api/gateways/gw-a1/pause")

        req_task = asyncio.create_task(
            c.post(f"http://127.0.0.1:{A1_GW}/generate", json={"chunks": 3, "delay": 0.05}, timeout=60)
        )
        await _wait(lambda: _req_done(c, "QUEUED", "gw-a1"), timeout=20)

        # The client goes away while held → the queued request is cancelled.
        req_task.cancel()
        try:
            await req_task
        except (asyncio.CancelledError, httpx.HTTPError):
            pass
        await _wait(lambda: _req_done(c, "CANCELLED", "gw-a1"), timeout=20)
        view = await _gw_view(c)
        assert view["queue"]["length"] == 0

        await c.post(f"{ADMIN}/api/gateways/gw-a1/unpause")


async def test_queue_full_while_paused_429(server):
    """spec 11: a paused gateway still enforces its waiting-queue cap — a
    request that would overflow the queue is rejected (429) immediately."""
    async with httpx.AsyncClient() as c:
        await c.post(f"{ADMIN}/api/applications/a1/start")
        await _wait(lambda: _a1_loaded(c), timeout=20)

        # Shrink the cap to 1, then pause.
        r = await c.put(f"{ADMIN}/api/gateways/gw-a1", json={"max_waiting_queue": 1})
        assert r.status_code == 200
        await c.post(f"{ADMIN}/api/gateways/gw-a1/pause")

        first = asyncio.create_task(
            c.post(f"http://127.0.0.1:{A1_GW}/generate", json={"chunks": 3, "delay": 0.05}, timeout=60)
        )
        await _wait(lambda: _req_done(c, "QUEUED", "gw-a1"), timeout=20)

        # The queue is full (cap 1) → the next request is rejected, not queued.
        r = await c.post(f"http://127.0.0.1:{A1_GW}/generate",
                        json={"chunks": 3, "delay": 0.05}, timeout=10)
        assert r.status_code == 429

        first.cancel()
        try:
            await first
        except (asyncio.CancelledError, httpx.HTTPError):
            pass
        await _wait(lambda: _req_done(c, "CANCELLED", "gw-a1"), timeout=20)

        await c.post(f"{ADMIN}/api/gateways/gw-a1/unpause")


@pytest.mark.parametrize("endpoint", ["pause", "unpause"])
async def test_pause_endpoints_404_for_unknown(server, endpoint):
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{ADMIN}/api/gateways/nope/{endpoint}")
        assert r.status_code == 404
