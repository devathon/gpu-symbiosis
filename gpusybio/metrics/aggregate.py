"""Metrics aggregator + retention (spec 08 §6):

- raw 1 s samples      → retain 24 h
- 1-minute aggregates  → retain 30 days
- hourly aggregates    → retain indefinitely

Each roll-up computes min/max/avg/sum per (scope, metric, window); the "current /
average / maximum" triplets for the UI are the latest window's avg + max plus the
most recent raw sample (current).
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

from gpusybio.database.db import SessionFactory
from gpusybio.database.models import (
    ApplicationEvent,
    GatewayEvent,
    MetricAggregate,
    MetricSample,
    SchedulerEvent,
)
from gpusybio.events.log import get_logger

log = get_logger("metrics.aggregate")

RAW_RETAIN_SEC = 24 * 3600
MIN_RETAIN_SEC = 30 * 24 * 3600
# Event tables (scheduler/application/gateway events) are not metrics but they
# also grow without bound (the scheduler persists one row per reconcile tick,
# every second). Retain them for a week — enough to investigate recent
# behavior — consistent with the metrics retention model (spec 08 §6).
EVENT_RETAIN_SEC = 7 * 24 * 3600

# Metrics rolled up from raw samples (scope = "gpu" or an application id).
GPU_METRICS = ("gpu_pct", "gpu_mem_mb", "gpu_temp_c", "gpu_power_w", "cpu_pct")
APP_METRICS = ("gpu_pct", "gpu_mem_mb", "active_requests")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class MetricsAggregator:
    def __init__(self, db: SessionFactory, app_ids: list[str]) -> None:
        self._db = db
        self._app_ids = app_ids
        self._task: asyncio.Task | None = None
        self._running = False
        self._last_hourly = 0.0

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.get_event_loop().create_task(self._loop(), name="metrics-aggregator")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def _loop(self, interval: float = 60.0) -> None:
        while self._running:
            await asyncio.sleep(interval)
            try:
                self.roll_up()
            except Exception:  # noqa: BLE001
                log.exception("metrics roll-up failed")
            if time.time() - self._last_hourly >= 3600:
                self._last_hourly = time.time()
                try:
                    self.roll_up_hourly()
                except Exception:  # noqa: BLE001
                    log.exception("hourly roll-up failed")
            try:
                self.prune()
            except Exception:  # noqa: BLE001
                log.exception("metrics prune failed")

    # -- roll-ups ----------------------------------------------------------
    def roll_up(self, window_sec: int = 60) -> None:
        """Aggregate raw samples from the last `window_sec` into 1-minute rows."""
        now = _utcnow()
        start = now - timedelta(seconds=window_sec)
        with self._db() as s:
            rows = (
                s.query(MetricSample)
                .filter(MetricSample.at >= start, MetricSample.at <= now)
                .all()
            )
        if not rows:
            return
        buckets: dict[tuple[str, str], list[float]] = {}
        for r in rows:
            scope = r.application_id or "gpu"
            for m in (APP_METRICS if r.application_id else GPU_METRICS):
                v = getattr(r, m)
                if v is not None:
                    buckets.setdefault((scope, m), []).append(float(v))
        with self._db() as s:
            for (scope, metric), vals in buckets.items():
                # Skip if this window already aggregated (idempotency).
                existing = (
                    s.query(MetricAggregate)
                    .filter(
                        MetricAggregate.scope == scope,
                        MetricAggregate.metric == metric,
                        MetricAggregate.window_sec == window_sec,
                        MetricAggregate.at >= start,
                        MetricAggregate.at <= now,
                    )
                    .count()
                )
                if existing:
                    continue
                s.add(
                    MetricAggregate(
                        scope=scope,
                        metric=metric,
                        window_sec=window_sec,
                        min=min(vals),
                        max=max(vals),
                        avg=round(sum(vals) / len(vals), 3),
                        total=round(sum(vals), 3),
                        count=len(vals),
                    )
                )

    def roll_up_hourly(self) -> None:
        """Aggregate the last hour of 1-minute rows into hourly rows."""
        now = _utcnow()
        start = now - timedelta(hours=1)
        with self._db() as s:
            rows = (
                s.query(MetricAggregate)
                .filter(
                    MetricAggregate.window_sec == 60,
                    MetricAggregate.at >= start,
                    MetricAggregate.at <= now,
                )
                .all()
            )
        if not rows:
            return
        buckets: dict[tuple[str, str], list[float]] = {}
        for r in rows:
            buckets.setdefault((r.scope, r.metric), []).append(r.avg if r.avg is not None else 0.0)
        with self._db() as s:
            for (scope, metric), vals in buckets.items():
                existing = (
                    s.query(MetricAggregate)
                    .filter(
                        MetricAggregate.scope == scope,
                        MetricAggregate.metric == metric,
                        MetricAggregate.window_sec == 3600,
                        MetricAggregate.at >= start,
                        MetricAggregate.at <= now,
                    )
                    .count()
                )
                if existing:
                    continue
                s.add(
                    MetricAggregate(
                        scope=scope,
                        metric=metric,
                        window_sec=3600,
                        min=min(vals),
                        max=max(vals),
                        avg=round(sum(vals) / len(vals), 3),
                        total=None,
                        count=len(vals),
                    )
                )

    # -- retention (spec 08 §6) --------------------------------------------
    def prune(self) -> None:
        now = _utcnow()
        with self._db() as s:
            raw_cutoff = now - timedelta(seconds=RAW_RETAIN_SEC)
            s.query(MetricSample).filter(MetricSample.at < raw_cutoff).delete()
            min_cutoff = now - timedelta(seconds=MIN_RETAIN_SEC)
            s.query(MetricAggregate).filter(
                MetricAggregate.window_sec == 60, MetricAggregate.at < min_cutoff
            ).delete()
            # Hourly (window_sec=3600) is retained indefinitely — never pruned.
            # Event tables: retain for EVENT_RETAIN_SEC (spec 08 §6). Without
            # this they grow without bound (scheduler persists per tick).
            event_cutoff = now - timedelta(seconds=EVENT_RETAIN_SEC)
            s.query(ApplicationEvent).filter(ApplicationEvent.at < event_cutoff).delete()
            s.query(GatewayEvent).filter(GatewayEvent.at < event_cutoff).delete()
            s.query(SchedulerEvent).filter(SchedulerEvent.at < event_cutoff).delete()

    # -- UI triplets (spec 08 §6) ------------------------------------------
    def summary(self) -> dict:
        """Current / average / maximum triplets for the UI (latest windows).

        Only rows from the last 120 s matter (the two most recent 1-minute
        windows); filter by time in SQL so the 30-day retention column is
        never materialized on UI polling."""
        now = _utcnow()
        out: dict = {"gpu": {}, "applications": {}}
        window_start = now - timedelta(seconds=120)
        with self._db() as s:
            last_min = (
                s.query(MetricAggregate)
                .filter(
                    MetricAggregate.window_sec == 60,
                    MetricAggregate.scope == "gpu",
                    MetricAggregate.at >= window_start,
                )
                .order_by(MetricAggregate.at.desc())
                .limit(100)
                .all()
            )
            for r in last_min:
                out["gpu"].setdefault(r.metric, {})
                if r.at >= window_start:
                    out["gpu"][r.metric]["average"] = r.avg
                    out["gpu"][r.metric]["maximum"] = r.max
            # current = latest raw sample
            raw = (
                s.query(MetricSample)
                .filter(MetricSample.application_id.is_(None))
                .order_by(MetricSample.at.desc())
                .first()
            )
            if raw is not None:
                for m in GPU_METRICS:
                    v = getattr(raw, m)
                    if v is not None:
                        out["gpu"].setdefault(m, {})["current"] = v
            for app_id in self._app_ids:
                app_rows = (
                    s.query(MetricAggregate)
                    .filter(
                        MetricAggregate.window_sec == 60,
                        MetricAggregate.scope == app_id,
                        MetricAggregate.at >= window_start,
                    )
                    .order_by(MetricAggregate.at.desc())
                    .limit(100)
                    .all()
                )
                for r in app_rows:
                    out["applications"].setdefault(app_id, {}).setdefault(r.metric, {})
                    if r.at >= window_start:
                        out["applications"][app_id][r.metric]["average"] = r.avg
                        out["applications"][app_id][r.metric]["maximum"] = r.max
        return out

    def history(self, scope: str, metric: str, window_sec: int, limit: int = 500) -> list[dict]:
        """Time series for a scope+metric over the retention window."""
        now = _utcnow()
        with self._db() as s:
            rows = (
                s.query(MetricAggregate)
                .filter(
                    MetricAggregate.scope == scope,
                    MetricAggregate.metric == metric,
                    MetricAggregate.window_sec == window_sec,
                )
                .order_by(MetricAggregate.at.desc())
                .limit(limit)
                .all()
            )
        return [
            {"at": r.at.isoformat() if r.at else None, "min": r.min, "max": r.max,
             "avg": r.avg, "count": r.count}
            for r in reversed(rows)
        ]
