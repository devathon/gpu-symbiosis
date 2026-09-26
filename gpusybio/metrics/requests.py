"""Request history (spec 08 §2/§4): rolling per-application and per-gateway
request metrics — counts, latency percentiles (wait + service tracked
separately), arrival/service rates, and 1m/5m/15m request counts.

Fed by the event bus (decoupled from the request path): REQUEST_RECEIVE records
arrivals; REQUEST_COMPLETE (every terminal state) records the outcome.
"""

from __future__ import annotations

import time
from collections import deque

from gpusybio.events.bus import REQUEST_COMPLETE, REQUEST_RECEIVE

# Retention windows (spec 08 §2).
RECEIVE_WINDOW_SEC = 3600  # keep up to 1h of request events for 15m counts
SAMPLE_CAP = 4000


class _Bucket:
    __slots__ = ("received", "completed", "failed", "cancelled", "expired",
                 "waits", "services", "arrivals", "completions",
                 "input_tokens", "output_tokens")

    def __init__(self) -> None:
        self.received = 0
        self.completed = 0
        self.failed = 0
        self.cancelled = 0
        self.expired = 0
        self.waits: deque = deque()
        self.services: deque = deque()
        self.arrivals: deque = deque()
        self.completions: deque = deque()
        self.input_tokens = 0
        self.output_tokens = 0

    def snapshot(self, now: float) -> dict:
        wait = _latency(self.waits)
        service = _latency(self.services)
        return {
            "requests_received": self.received,
            "requests_completed": self.completed,
            "requests_failed": self.failed,
            "requests_cancelled": self.cancelled,
            "requests_expired": self.expired,
            "active_requests": 0,  # filled by caller from live state
            "queued_requests": 0,
            "average_wait_sec": wait["avg"],
            "max_wait_sec": wait["max"],
            "p50_wait_sec": wait["p50"],
            "p95_wait_sec": wait["p95"],
            "p99_wait_sec": wait["p99"],
            "average_service_sec": service["avg"],
            "p50_service_sec": service["p50"],
            "p95_service_sec": service["p95"],
            "p99_service_sec": service["p99"],
            "arrival_rate": _rate(self.arrivals, now, 60.0),
            "service_rate": _rate(self.completions, now, 60.0),
            "request_count_last_1m": _count_last(self.arrivals, now, 60.0),
            "request_count_last_5m": _count_last(self.arrivals, now, 300.0),
            "request_count_last_15m": _count_last(self.arrivals, now, 900.0),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.input_tokens + self.output_tokens,
        }


class RequestMetrics:
    """In-memory rolling metrics, keyed per application and per gateway."""

    def __init__(self, db=None, bus=None) -> None:
        self._apps: dict[str, _Bucket] = {}
        self._gateways: dict[str, _Bucket] = {}
        self._db = db
        self._last_prune = 0.0

        if bus is not None:
            bus.subscribe(REQUEST_RECEIVE, self._on_receive)
            bus.subscribe(REQUEST_COMPLETE, self._on_complete)

    # -- bus hooks -----------------------------------------------------------
    async def _on_receive(self, ev) -> None:
        gw = ev.data.get("gateway_id")
        app = ev.data.get("application_id")
        now = ev.at
        if app:
            self._app_bucket(app).received += 1
            self._push(self._app_bucket(app).arrivals, now)
        if gw:
            self._gw_bucket(gw).received += 1
            self._push(self._gw_bucket(gw).arrivals, now)

    async def _on_complete(self, ev) -> None:
        app = ev.data.get("application_id")
        status = ev.data.get("status", "")
        rid = ev.data.get("request_id")
        now = ev.at
        wait = service = None
        in_tok = out_tok = None
        gw = None
        if self._db is not None and rid:
            row = self._row(rid)
            if row is not None:
                gw = row.gateway_id
                wait = row.queue_wait_sec
                service = row.service_time_sec
                in_tok = row.input_tokens
                out_tok = row.output_tokens
        if app:
            b = self._app_bucket(app)
            self._record_terminal(b, status, now, wait, service, in_tok, out_tok)
        if gw:
            self._record_terminal(self._gw_bucket(gw), status, now, wait, service,
                                  in_tok, out_tok)

    def _record_terminal(self, b: _Bucket, status: str, now: float,
                         wait: float | None, service: float | None,
                         in_tok: int | None, out_tok: int | None) -> None:
        b.received  # ensure present
        if wait is not None:
            b.waits.append(wait)
            if len(b.waits) > SAMPLE_CAP:
                b.waits.popleft()
        if service is not None:
            b.services.append(service)
            if len(b.services) > SAMPLE_CAP:
                b.services.popleft()
        if in_tok:
            b.input_tokens += in_tok
        if out_tok:
            b.output_tokens += out_tok
        if status == "COMPLETED":
            b.completed += 1
            self._push(b.completions, now)
        elif status == "FAILED":
            b.failed += 1
        elif status == "CANCELLED":
            b.cancelled += 1
        elif status == "EXPIRED":
            b.expired += 1

    # -- accessors -----------------------------------------------------------
    def app(self, app_id: str, active: int = 0, queued: int = 0) -> dict:
        b = self._app_bucket(app_id)
        snap = b.snapshot(time.time())
        snap["active_requests"] = active
        snap["queued_requests"] = queued
        return snap

    def gateway(self, gw_id: str) -> dict:
        return self._gw_bucket(gw_id).snapshot(time.time())

    def all_apps(self) -> dict[str, dict]:
        return {a: b.snapshot(time.time()) for a, b in self._apps.items()}

    def all_gateways(self) -> dict[str, dict]:
        return {g: b.snapshot(time.time()) for g, b in self._gateways.items()}

    # -- internals -----------------------------------------------------------
    def _app_bucket(self, app_id: str) -> _Bucket:
        return self._apps.setdefault(app_id, _Bucket())

    def _gw_bucket(self, gw_id: str) -> _Bucket:
        return self._gateways.setdefault(gw_id, _Bucket())

    def _row(self, request_id: str):
        try:
            from gpusybio.database.models import Request

            with self._db() as s:
                return s.get(Request, request_id)
        except Exception:  # noqa: BLE001 - metrics must never break the path
            return None

    @staticmethod
    def _push(dq: deque, now: float) -> None:
        dq.append(now)
        cutoff = now - RECEIVE_WINDOW_SEC
        while dq and dq[0] < cutoff:
            dq.popleft()


def _latency(samples: deque) -> dict:
    if not samples:
        return {"avg": None, "max": None, "p50": None, "p95": None, "p99": None}
    xs = sorted(samples)
    n = len(xs)
    return {
        "avg": round(sum(xs) / n, 3),
        "max": round(xs[-1], 3),
        "p50": round(_pct(xs, 50), 3),
        "p95": round(_pct(xs, 95), 3),
        "p99": round(_pct(xs, 99), 3),
    }


def _pct(sorted_xs: list, pct: float) -> float:
    """Linear-interpolation percentile (numpy default)."""
    if not sorted_xs:
        return 0.0
    n = len(sorted_xs)
    if n == 1:
        return float(sorted_xs[0])
    idx = (n - 1) * (pct / 100.0)
    lo = int(idx)
    hi = min(n - 1, lo + 1)
    frac = idx - lo
    return sorted_xs[lo] + frac * (sorted_xs[hi] - sorted_xs[lo])


def _rate(timestamps: deque, now: float, window_sec: float) -> float:
    """Per-minute rate over the given window."""
    if not timestamps:
        return 0.0
    recent = sum(1 for t in timestamps if t >= now - window_sec)
    return round(recent / (window_sec / 60.0), 3)


def _count_last(timestamps: deque, now: float, window_sec: float) -> int:
    return sum(1 for t in timestamps if t >= now - window_sec)
