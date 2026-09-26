"""Process-internal event bus (spec 06 §4 triggers, spec 04 §10 events).

Direct async dispatch: publishers await subscribers. Subscribers are few and
fast (the scheduler records a trigger; the logger persists; the SSE hub
pushes). A broken subscriber never breaks the bus.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

# Trigger events (scheduler wakes on these — spec 06 §4)
NEW_REQUEST = "new_request"
REQUEST_COMPLETE = "request_complete"
LOAD_COMPLETE = "load_complete"
UNLOAD_COMPLETE = "unload_complete"
QUEUE_TIMEOUT = "queue_timeout"
RESOURCE_CHANGE = "resource_change"
IDLE_TIMEOUT = "idle_timeout"
PROCESS_NOW = "process_now"
PROCESS_DIED = "process_died"
RECONCILE = "reconcile"

# Coarse classes: lifecycle events route to their trigger class so the
# scheduler can wake on e.g. LOAD_READY (class load_complete).
_ROUTE_CLASSES = {
    "LOAD_READY": LOAD_COMPLETE,
    "LOAD_FAILED": LOAD_COMPLETE,
    "UNLOADED": UNLOAD_COMPLETE,
    "UNLOAD_FAILED": UNLOAD_COMPLETE,
    "PROCESS_DIED": PROCESS_DIED,
    "REQUEST_CANCELLED": REQUEST_COMPLETE,
    "REQUEST_EXPIRED": REQUEST_COMPLETE,
}

# Lifecycle event names (spec 04 §10)
START = "START"
START_TIMEOUT = "START_TIMEOUT"
START_FAILED = "START_FAILED"
STOP = "STOP"
STOPPED = "STOPPED"
DRAIN_START = "DRAIN_START"
LOAD = "LOAD"
LOAD_READY = "LOAD_READY"
LOAD_FAILED = "LOAD_FAILED"
LOAD_REFUSED = "LOAD_REFUSED"
UNLOAD = "UNLOAD"
UNLOADED = "UNLOADED"
UNLOAD_FAILED = "UNLOAD_FAILED"
PROCESS_DIED = "PROCESS_DIED"
FAULTED = "FAULTED"
FAULT_CLEARED = "FAULT_CLEARED"
REQUEST_RECEIVE = "REQUEST_RECEIVED"
REQUEST_QUEUED = "REQUEST_QUEUED"
REQUEST_FORWARD = "REQUEST_FORWARD"
REQUEST_COMPLETED = "REQUEST_COMPLETED"
REQUEST_FAILED = "REQUEST_FAILED"
REQUEST_CANCELLED = "REQUEST_CANCELLED"
REQUEST_EXPIRED = "REQUEST_EXPIRED"
QUEUE_FULL = "QUEUE_FULL"
PROCESS_NOW = "PROCESS_NOW"
ENABLED = "ENABLED"
DISABLED = "DISABLED"
SWITCH_IN = "SWITCH_IN"
SWITCH_OUT = "SWITCH_OUT"

# Persistence-worthy events (spec 04 §10 / 05 §10)
PERSISTENT_EVENTS = {
    "START",
    "START_TIMEOUT",
    "STOP",
    "STOPPED",
    "LOAD",
    "LOAD_READY",
    "LOAD_FAILED",
    "UNLOAD",
    "UNLOADED",
    "UNLOAD_FAILED",
    "PROCESS_DIED",
    "SWITCH_IN",
    "SWITCH_OUT",
    "FAULTED",
    "FAULT_CLEARED",
    "REQUEST_RECEIVED",
    "REQUEST_QUEUED",
    "REQUEST_FORWARD",
    "REQUEST_COMPLETED",
    "REQUEST_FAILED",
    "REQUEST_CANCELLED",
    "REQUEST_EXPIRED",
    "QUEUE_FULL",
    "PROCESS_NOW",
    "ENABLED",
    "DISABLED",
    "scheduler_decision",
}


@dataclass
class Event:
    type: str
    data: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "type": self.type, "at": self.at, **self.data}


Subscriber = Callable[[Event], Awaitable[None]]


class EventBus:
    def __init__(self) -> None:
        self._subscribers: dict[str, list[Subscriber]] = {}
        # Broad observers (event logger, SSE hub): every event.
        self._observers: list[Subscriber] = []

    async def publish(self, event: Event) -> None:
        for handler in self._observers:
            await _safe(handler, event)
        for key in {event.type, *_ROUTE_CLASSES.get(event.type, [event.type])}:
            for handler in self._subscribers.get(key, []):
                await _safe(handler, event)

    def subscribe(self, event_types: set[str] | str, handler: Subscriber) -> "EventBus":
        keys = {event_types} if isinstance(event_types, str) else set(event_types)
        for t in keys:
            self._subscribers.setdefault(t, []).append(handler)
        return self

    def observe(self, handler: Subscriber) -> "EventBus":
        self._observers.append(handler)
        return self

    async def publish_raw(self, type_: str, **data) -> Event:
        event = Event(type=type_, data=data)
        await self.publish(event)
        return event


async def _safe(handler: Subscriber, event: Event) -> None:
    try:
        await handler(event)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - a broken subscriber must not break the bus
        # Log it: a silently-swallowed subscriber bug is otherwise invisible
        # (the scheduler's decision simply never appears).
        import traceback

        from gpusybio.events.log import get_logger

        get_logger().error(
            "subscriber failed",
            event_type=event.type,
            handler=type(handler).__name__,
            traceback=traceback.format_exc(),
        )
