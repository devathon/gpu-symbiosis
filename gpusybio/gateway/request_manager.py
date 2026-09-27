"""Request lifecycle (spec 05 §6): QUEUED → forward signal → COMPLETED/FAILED/
CANCELLED/EXPIRED.

Owns request rows (SQLite), the per-gateway queue, gateway-level waiting, and
the forward signals the scheduler emits. Enforces Invariants 5 (no lost queued
requests) and 6 (client disconnect cancels queued requests).
"""

from __future__ import annotations

import asyncio
import time

from gpusybio.config.seed import new_request_id
from gpusybio.database.db import SessionFactory
from gpusybio.database.models import Request
from gpusybio.events.bus import (
    NEW_REQUEST,
    REQUEST_COMPLETE,
    REQUEST_FAILED,
    REQUEST_RECEIVE,
    QUEUE_FULL,
    QUEUE_TIMEOUT,
)
from gpusybio.events.bus import Event
from gpusybio.events.log import get_logger
from gpusybio.gateway.queue import GatewayQueue, RequestSlot
from gpusybio.scoring import DemandTracker
from gpusybio.state import StateManager

log = get_logger("requestmanager")


class RequestManager:
    def __init__(
        self,
        states: StateManager,
        db: SessionFactory,
        bus,
        settings,
        gateway_configs: dict[str, dict],
        app_configs: dict[str, dict] | None = None,
    ):
        self._states = states
        self._db = db
        self._bus = bus
        self._settings = settings
        self._gateways = gateway_configs  # gw_id -> {app_id, max_waiting_queue, ...}
        self._app_configs = app_configs or {}  # app_id -> {max_concurrent_requests}
        self._queue = GatewayQueue()
        # request_id -> asyncio.Event (forward signal)
        self._forward: dict[str, asyncio.Event] = {}
        self._active: dict[str, set[str]] = {}  # app_id -> in-flight request ids
        self._lock = asyncio.Lock()
        # Demand tracking for the scheduler scoring model (spec 06 §6.3/§6.4).
        self._demand = DemandTracker()

    # -- config --------------------------------------------------------------
    def gateway(self, gateway_id: str) -> dict:
        return self._gateways[gateway_id]

    def gateway_configs(self) -> dict[str, dict]:
        return dict(self._gateways)

    def set_gateway(self, gateway_id: str, cfg: dict) -> None:
        """Install a gateway config live (API-created gateways, spec 11 §3)."""
        self._gateways[gateway_id] = cfg

    def remove_gateway(self, gateway_id: str) -> None:
        self._gateways.pop(gateway_id, None)

    def app_configs(self) -> dict[str, dict]:
        return self._app_configs

    def bus(self):
        return self._bus

    def queue(self) -> GatewayQueue:
        return self._queue

    def demand(self) -> DemandTracker:
        return self._demand

    # -- intake (spec 05 §3) --------------------------------------------------
    async def receive(self, gateway_id: str, metadata: dict | None = None) -> RequestSlot | None:
        """Register a new request. Returns the slot (queued) or None (429)."""
        gw = self._gateways.get(gateway_id)
        if gw is None:
            return None
        if self._queue.length(gateway_id) >= gw["max_waiting_queue"]:
            await self._bus.publish_raw(
                QUEUE_FULL, gateway_id=gateway_id, queue=str(self._queue.length(gateway_id))
            )
            log.warning("queue full", gateway_id=gateway_id)
            return None

        slot = RequestSlot(
            request_id=new_request_id(),
            gateway_id=gateway_id,
            application_id=gw["application_id"],
            priority=gw["priority"],
            metadata=metadata or {},
        )
        self._queue.enqueue(slot)
        self._forward[slot.request_id] = asyncio.Event()
        self._states.ensure(gw["application_id"]).queued_requests = (
            self._queue.length(gateway_id)
        )
        self._demand.arrival(gateway_id)  # spec 06 §6.3

        await self._persist(slot, status="QUEUED")
        await self._bus.publish_raw(
            REQUEST_RECEIVE,
            request_id=slot.request_id,
            gateway_id=gateway_id,
            application_id=slot.application_id,
        )
        await self._bus.publish_raw(
            NEW_REQUEST,
            request_id=slot.request_id,
            gateway_id=gateway_id,
            application_id=slot.application_id,
        )
        log.info(
            "request received",
            request_id=slot.request_id,
            gateway_id=gateway_id,
            queue=self._queue.length(gateway_id),
        )
        return slot

    # -- forward signal (spec 05 §6) ------------------------------------------
    def signal_forward(self, request_id: str) -> None:
        ev = self._forward.get(request_id)
        if ev is not None:
            ev.set()

    async def wait_forward(self, request_id: str, timeout: float | None) -> bool:
        """Wait for the scheduler to signal this request forward.

        Returns True if signaled, False on timeout (caller cancels the request).
        """
        ev = self._forward.get(request_id)
        if ev is None:
            return False
        try:
            await asyncio.wait_for(ev.wait(), timeout=timeout)
            return True
        except (asyncio.TimeoutError, TimeoutError):
            return False

    def is_terminal(self, request_id: str) -> bool:
        """True if the request reached a terminal state (not forwardable)."""
        try:
            with self._db() as s:
                row = s.get(Request, request_id)
            return row is not None and row.status in (
                "COMPLETED", "FAILED", "CANCELLED", "EXPIRED"
            )
        except Exception:  # noqa: BLE001
            return False

    def _app_limit(self, app_id: str) -> int:
        cfg = self._app_configs.get(app_id)
        if cfg is not None:
            return cfg.get("max_concurrent_requests", 1)
        return 1

    def can_forward_now(self, gateway_id: str) -> bool:
        """App ready + concurrency slot free → forward directly, no queueing."""
        gw = self._gateways.get(gateway_id)
        if gw is None:
            return False
        app_id = gw["application_id"]
        s = self._states.get(app_id)
        from gpusybio.applications.lifecycle import ProcessState, ResidencyState

        if s.process is not ProcessState.RUNNING or s.residency is not ResidencyState.LOADED:
            return False
        if s.draining:
            return False
        if len(self._active.get(app_id, ())) >= self._app_limit(app_id):
            return False
        return True

    # -- forward bookkeeping -----------------------------------------------------
    async def begin_forward(self, request_id: str) -> str | None:
        """Mark the request in-flight (PROCESSING). Removes it from the queue.

        Returns the app id, or None if the concurrency slot is taken (the slot
        stays/returns in the queue for the scheduler).
        """
        slot = self._queue.remove(request_id)
        if slot is None:
            slot = self._slot_from_db(request_id)
        if slot is None:
            return None
        app_id = slot.application_id
        if not self.can_forward_now(slot.gateway_id):
            self._queue.enqueue(slot)  # not ready — back in the queue for the scheduler
            return None
        async with self._lock:
            if len(self._active.get(app_id, ())) >= self._app_limit(app_id):
                self._queue.enqueue(slot)  # back to the queue
                return None
            self._active.setdefault(app_id, set()).add(request_id)
        self._states.request_started(app_id)
        self._states.ensure(app_id).queued_requests = self.queued_count(app_id)
        await self._update_status(request_id, "PROCESSING", started_at=True)
        await self._bus.publish_raw(
            "REQUEST_FORWARD", request_id=request_id, application_id=app_id
        )
        return app_id

    # -- terminal states (spec 05 §6) ---------------------------------------------
    async def complete(self, request_id: str, error: str | None = None) -> None:
        slot = self._queue.get(request_id) or self._slot_from_db(request_id)
        app_id = slot.application_id if slot else ""
        ok = error is None
        status = "COMPLETED" if ok else "FAILED"
        await self._finish(request_id, status, app_id, error)

    async def cancel(self, request_id: str) -> None:
        """Client disconnect / queue full / explicit clear (Invariant 6)."""
        slot = self._queue.remove(request_id)
        if slot is None:
            slot = self._slot_from_db(request_id)
        app_id = slot.application_id if slot else ""
        await self._finish(request_id, "CANCELLED", app_id, "client disconnected")

    async def expire(self, request_id: str) -> None:
        slot = self._queue.remove(request_id)
        app_id = slot.application_id if slot else ""
        await self._finish(request_id, "EXPIRED", app_id, "wait timeout")

    # -- housekeeping ---------------------------------------------------------------
    def active_count(self, app_id: str) -> int:
        return len(self._active.get(app_id, ()))

    def queued_count(self, app_id: str) -> int:
        return sum(1 for s in self._queue.all_slots() if s.application_id == app_id)

    # -- internals -----------------------------------------------------------------
    async def _finish(self, request_id: str, status: str, app_id: str, error: str | None) -> None:
        async with self._lock:
            if app_id:
                self._active.get(app_id, set()).discard(request_id)
            if app_id and app_id in self._active and not self._active[app_id]:
                del self._active[app_id]
            if app_id:
                self._states.request_finished(app_id)
        ev = self._forward.pop(request_id, None)
        if ev is not None:
            ev.set()  # wake the waiter: forward (scheduler) OR terminal (cancel/expire)
        if app_id and status == "COMPLETED":
            self._demand.service(app_id)  # spec 06 §6.4 (service rate = completions/min)
        await self._update_status(request_id, status, completed_at=True, error=error)
        if app_id:
            self._states.ensure(app_id).queued_requests = self.queued_count(app_id)
        await self._bus.publish_raw(
            REQUEST_COMPLETE, request_id=request_id, status=status, application_id=app_id
        )
        if status == "FAILED":
            await self._bus.publish_raw(REQUEST_FAILED, request_id=request_id, detail=error)
        log.info("request finished", request_id=request_id, status=status)

    async def _persist(self, slot: RequestSlot, status: str) -> None:
        try:
            with self._db() as s:
                s.add(
                    Request(
                        id=slot.request_id,
                        gateway_id=slot.gateway_id,
                        application_id=slot.application_id,
                        status=status,
                        priority=slot.priority,
                        queued_at=_dt(slot.queued_at),
                        received_at=_dt(slot.queued_at),
                    )
                )
        except Exception:  # noqa: BLE001 - persistence must never break the request path
            log.exception("request persist failed")

    async def _update_status(
        self,
        request_id: str,
        status: str,
        started_at: bool = False,
        completed_at: bool = False,
        error: str | None = None,
    ) -> None:
        now = _dt(time.time())
        try:
            with self._db() as s:
                row = s.get(Request, request_id)
                if row is None:
                    return
                row.status = status
                if started_at:
                    row.started_at = now
                if completed_at:
                    row.completed_at = now
                    if row.queued_at is not None:
                        row.queue_wait_sec = ((row.started_at or now) - row.queued_at).total_seconds()
                    if row.started_at is not None:
                        row.service_time_sec = (now - row.started_at).total_seconds()
                if error:
                    row.error = error
        except Exception:  # noqa: BLE001
            log.exception("request status update failed")

    def _slot_from_db(self, request_id: str) -> RequestSlot | None:
        try:
            with self._db() as s:
                row = s.get(Request, request_id)
            if row is None:
                return None
            return RequestSlot(
                request_id=row.id,
                gateway_id=row.gateway_id,
                application_id=row.application_id,
                priority=row.priority,
                queued_at=row.queued_at.timestamp() if row.queued_at else time.time(),
            )
        except Exception:  # noqa: BLE001
            return None


def _dt(ts: float):
    """Naive-UTC datetime (SQLite stores naive; keep subtraction consistent)."""
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)
