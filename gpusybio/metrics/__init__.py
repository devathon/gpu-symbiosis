"""Metrics (spec 08): 1 s sampling, rolling history, request/latency metrics,
type-metrics contract, active-time tracking, learning hooks, and the
aggregation/retention pipeline.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque

from aiohttp import ClientSession

from gpusybio.applications.lifecycle import ProcessState, ResidencyState
from gpusybio.database.db import SessionFactory
from gpusybio.database.models import Application, MetricSample, Request, SchedulerEvent
from gpusybio.events.bus import STOPPED
from gpusybio.events.log import get_logger
from gpusybio.gateway.request_manager import RequestManager
from gpusybio.metrics.aggregate import MetricsAggregator
from gpusybio.metrics.type_registry import TypePluginRegistry, make_type_registry
from gpusybio.metrics.lifetime import LifetimeStore
from gpusybio.metrics.requests import RequestMetrics
from gpusybio.metrics.typespec import COMMON_METRICS
from gpusybio.resources.manager import ResourceManager
from gpusybio.state import StateManager

log = get_logger("metrics")

HISTORY_SEC = 600  # 10 min rolling window (spec 08 §1)


class MetricsCollector:
    def __init__(
        self,
        resources: ResourceManager,
        states: StateManager,
        request_manager: RequestManager,
        db: SessionFactory,
        app_ids: list[str],
        app_types: dict[str, str] | None = None,
        base_urls: dict[str, str] | None = None,
        log_paths: dict[str, str] | None = None,
        type_registry: TypePluginRegistry | None = None,
    ):
        self._resources = resources
        self._states = states
        self._rm = request_manager
        self._db = db
        self._app_ids = app_ids
        self._app_types = app_types or {}
        self._base_urls = base_urls or {}
        self._log_paths = log_paths or {}
        self._history: deque = deque()
        self._task: asyncio.Task | None = None
        self._running = False

        # Request/latency metrics (spec 08 §2/§4) — subscribed to the bus.
        self.request_metrics = RequestMetrics(db=db, bus=request_manager.bus())
        # Type-metrics contract (spec 08 §9); pluggable application types
        # (spec 04 §8) — the registry resolves type → (spec, handler factory).
        self.type_registry = type_registry or make_type_registry()
        self._handlers = {
            app_id: self.type_registry.handler_for(
                self._app_types.get(app_id, "generic"),
                resources, states, self.request_metrics, self._base_urls, None,
                log_paths=self._log_paths,
            )
            for app_id in app_ids
        }
        # Active-time tracking (spec 08 §7).
        self._active_time: dict[str, dict] = {
            a: {"total_active_time": 0.0, "loaded_time": 0.0,
                "processing_time": 0.0, "idle_loaded_time": 0.0}
            for a in app_ids
        }
        self._last_sample_ts = 0.0
        # Aggregation + retention (spec 08 §6).
        self.aggregator = MetricsAggregator(db, app_ids)
        self._session: ClientSession | None = None
        # Lifetime token-metric persistence (spec 08 §5.1): seed on start, persist
        # on a 60 s timer for active apps and on app stop.
        self.lifetime_store = LifetimeStore(db)
        self._lifetime_persist_at = 0.0
        request_manager.bus().subscribe(STOPPED, self._on_app_stopped)

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._session = ClientSession()
        # Re-wire handlers with the live session (token/queue polling).
        for app_id in self._app_ids:
            self._handlers[app_id] = self.type_registry.handler_for(
                self._app_types.get(app_id, "generic"),
                self._resources, self._states, self.request_metrics, self._base_urls,
                self._session, log_paths=self._log_paths,
            )
        self.aggregator.start()
        self._seed_lifetime()
        self._task = asyncio.get_event_loop().create_task(self._loop(), name="metrics")

    async def stop(self) -> None:
        self._running = False
        await self.aggregator.stop()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if self._session is not None:
            await self._session.close()

    # -- lifetime persistence (spec 08 §5.1) -----------------------------------
    def _tailer(self, app_id: str):
        h = self._handlers.get(app_id)
        return h.tailer(app_id) if hasattr(h, "tailer") else None

    def _seed_lifetime(self) -> None:
        """Seed each tailer's lifetime + offset from the persisted state."""
        try:
            states = self.lifetime_store.load_many(self._app_ids)
        except Exception:  # noqa: BLE001 (e.g. a not-yet-migrated schema)
            log.exception("lifetime seed failed — starting with a fresh lifetime")
            return
        for app_id, state in states.items():
            t = self._tailer(app_id)
            if t is not None:
                t.seed(state)
                log.debug("lifetime seeded", app=app_id,
                          input_tokens=state.get("input_tokens"),
                          requests=state.get("requests"),
                          log_offset=state.get("log_offset"))

    def _persist_lifetime(self, app_id: str) -> None:
        t = self._tailer(app_id)
        if t is None:
            return
        try:
            self.lifetime_store.save(app_id, t.state())
        except Exception:  # noqa: BLE001
            log.exception("lifetime persist failed", app=app_id)

    def _persist_lifetime_for_active(self) -> None:
        """Persist the lifetime for active (running) apps — 60 s timer."""
        for app_id in self._app_ids:
            st = self._states.get(app_id)
            if st.process is ProcessState.STARTED:
                self._persist_lifetime(app_id)

    async def _on_app_stopped(self, ev) -> None:
        app_id = ev.data.get("application_id")
        if app_id:
            self._persist_lifetime(app_id)

    async def _loop(self, interval: float = 1.0) -> None:
        while self._running:
            t0 = time.time()
            try:
                await self._sample(interval)
                now = time.time()
                if now - self._lifetime_persist_at >= 60.0:
                    self._lifetime_persist_at = now
                    self._persist_lifetime_for_active()
            except Exception:  # noqa: BLE001 - sampling must never die
                log.exception("metrics sample failed")
            await asyncio.sleep(interval)

    async def _sample(self, interval: float) -> dict:
        s = self._resources.sample()
        sys_ = self._resources.system_sample()
        now = time.time()
        rec = {
            "at": now,
            "gpu_total_mb": s.total_mb,
            "gpu_used_mb": s.used_mb,
            "gpu_free_mb": s.free_mb,
            "gpu_available_mb": self._resources.available_mb(),
            "gpu_utilization_pct": s.utilization_pct,
            "vram_pct": round(s.used_pct, 1),
            "gpu_temperature_c": s.temperature_c,
            "gpu_power_w": s.power_w,
            "gpu_name": s.name,
            "gpu_pcie": s.pcie,
            "gpu_clock_mhz": s.clock_mhz,
            "gpu_rx_mbps": s.rx_mbps,
            "gpu_tx_mbps": s.tx_mbps,
            "gpu_power_limit_w": s.power_limit_w,
            "cpu_pct": sys_.cpu_pct,
            "cpu_cores": sys_.cpu_cores,
            "ram_total_mb": sys_.ram_total_mb,
            "ram_used_mb": sys_.ram_used_mb,
            "applications": {},
            "gateways": {},
        }
        for app_id in self._app_ids:
            st = self._states.get(app_id)
            mem = self._resources.observed_mb(app_id)
            util = self._resources.backend.process_utilization_pct(st.pid)
            rec["applications"][app_id] = {
                "gpu_memory_mb": round(mem, 1),
                "gpu_utilization_pct": util,
                "active_requests": st.active_requests,
                "process_state": st.process.value,
                "residency_state": st.residency.value,
                # Type-specific metrics (spec 08 §5/§9) via the handler.
                **await _safe_collect(self._handlers.get(app_id), app_id),
                # Active time (spec 08 §7).
                **self._active_time.get(app_id, {}),
            }
            self._record_active_time(app_id, st, interval)
            self._record_learning(app_id, mem, util)
        for gw_id, gw in self._rm.gateway_configs().items():
            rec["gateways"][gw_id] = {
                "queued": self._rm.queue().length(gw_id),
                "active": self._rm.active_count(gw["application_id"]),
                "oldest_wait_sec": round(self._rm.queue().oldest_wait_sec(gw_id), 1),
                "priority": gw["priority"],
                "enabled": gw.get("enabled", True),
            }

        self._history.append(rec)
        cutoff = now - HISTORY_SEC
        while self._history and self._history[0]["at"] < cutoff:
            self._history.popleft()

        # Persist (spec 08 §6: 1 s raw samples).
        try:
            with self._db() as session:
                session.add(MetricSample(gpu_pct=s.utilization_pct, gpu_mem_mb=s.used_mb,
                                         gpu_temp_c=s.temperature_c, gpu_power_w=s.power_w,
                                         cpu_pct=sys_.cpu_pct))
                for app_id in self._app_ids:
                    st = self._states.get(app_id)
                    session.add(
                        MetricSample(
                            application_id=app_id,
                            gpu_pct=self._resources.backend.process_utilization_pct(st.pid),
                            gpu_mem_mb=self._resources.observed_mb(app_id),
                            active_requests=st.active_requests,
                        )
                    )
        except Exception:  # noqa: BLE001
            log.exception("metrics persist failed")
        self._last_sample_ts = now
        return rec

    # -- active time (spec 08 §7) -------------------------------------------
    def _record_active_time(self, app_id: str, st, interval: float) -> None:
        at = self._active_time.setdefault(
            app_id, {"total_active_time": 0.0, "loaded_time": 0.0,
                     "processing_time": 0.0, "idle_loaded_time": 0.0}
        )
        if st.residency is ResidencyState.LOADED:
            at["total_active_time"] += interval
            at["loaded_time"] += interval
            if st.active_requests > 0:
                at["processing_time"] += interval
            else:
                at["idle_loaded_time"] += interval

    # -- learning hooks (spec 08 §10) ---------------------------------------
    def _record_learning(self, app_id: str, mem: float, util: float) -> None:
        try:
            with self._db() as s:
                row = s.get(Application, app_id)
                if row is None:
                    return
                if mem > 0 and (row.observed_max_gpu_memory_mb is None or mem > row.observed_max_gpu_memory_mb):
                    row.observed_max_gpu_memory_mb = int(mem)
                if util > 0 and (row.observed_max_gpu_usage_pct is None or util > row.observed_max_gpu_usage_pct):
                    row.observed_max_gpu_usage_pct = round(util, 1)
        except Exception:  # noqa: BLE001
            log.exception("learning hook failed", application_id=app_id)

    # -- views ---------------------------------------------------------------
    def history(self) -> list[dict]:
        return list(self._history)

    def current(self) -> dict | None:
        return self._history[-1] if self._history else None

    async def app_metrics(self, app_id: str, window: str = "1h") -> dict:
        """Combined application metrics for the API + UI (spec 08 §4/§5/§9).

        Merges the live sample, the type handler's metrics, request/latency
        metrics, active time, and the type spec (for auto-rendering).
        """
        st = self._states.get(app_id)
        handler = self._handlers.get(app_id)
        sample = self.current() or {}
        app_sample = sample.get("applications", {}).get(app_id, {})

        request = handler.get_request_metrics(
            app_id, st.active_requests, self._rm.queued_count(app_id)
        ) if handler is not None else {}
        type_metrics = await _safe_collect(handler, app_id)
        active_time = self._active_time.get(app_id, {})

        # Lifetime fallback (spec 08 §5.1): if the tailer's in-memory lifetime is
        # empty (not yet seeded or lost) but the DB has a persisted lifetime, use
        # the persisted totals so the card never shows zeroed tokens.
        if not type_metrics.get("input_tokens") and not type_metrics.get("output_tokens"):
            lifetime = self.lifetime_store.load(app_id)
            if lifetime and (lifetime.get("input_tokens") or lifetime.get("output_tokens")):
                total = lifetime.get("total_tokens") or (
                    lifetime.get("input_tokens", 0) + lifetime.get("output_tokens", 0)
                )
                tms = float(lifetime.get("total_time_ms", 0.0))
                pms = float(lifetime.get("prompt_time_ms", 0.0))
                ems = float(lifetime.get("eval_time_ms", 0.0))
                for key, v in (
                    ("input_tokens", lifetime.get("input_tokens")),
                    ("cached_tokens", lifetime.get("cached_tokens")),
                    ("output_tokens", lifetime.get("output_tokens")),
                    ("total_tokens", total),
                    ("prompt_tokens_per_sec",
                     lifetime.get("input_tokens", 0) / (pms / 1000.0) if pms else None),
                    ("generation_tokens_per_sec",
                     lifetime.get("output_tokens", 0) / (ems / 1000.0) if ems else None),
                    ("tokens_per_sec", total / (tms / 1000.0) if tms else None),
                ):
                    if v and not type_metrics.get(key):
                        type_metrics[key] = v

        # Type spec for auto-rendering (spec 08 §9).
        row = None
        with self._db() as s:
            row = s.get(Application, app_id)
        app_type = (row.application_type if row else None) or self._app_types.get(app_id, "generic")
        spec = self.type_registry.spec_for(app_type, row)

        values = _merge_metrics(app_sample, type_metrics, request, active_time)
        metrics_view = _metrics_view(spec, values)
        # The UI shows a STARTED app as RUNNING when active (spec 04 §3); the
        # metrics card needs the same `active` + load/unload support as the
        # application view to render its state badges.
        from gpusybio.scheduler.activity import is_active

        active = is_active(st, self._resources, row.idle_threshold_gpu_pct if row else None)
        return {
            "application_id": app_id,
            "type": app_type,
            "window": window,
            "state": {"process_state": st.process.value, "residency_state": st.residency.value,
                      "active": active,
                      "supports_load_unload": bool(row.unload_script) if row else False,
                      "active_requests": st.active_requests,
                      "queued_requests": self._rm.queued_count(app_id)},
            "metrics": metrics_view,
            "raw": values,
            "active_time": active_time,
            "spec": [{"id": m.id, "name": m.name, "unit": m.unit, "kind": m.kind,
                      "display": m.display, "group": m.group} for m in spec.metrics],
        }

    def gateway_metrics(self, gw_id: str) -> dict:
        return self.request_metrics.gateway(gw_id)

    def app_request_metrics(self) -> dict:
        out = {}
        for app_id in self._app_ids:
            st = self._states.get(app_id)
            out[app_id] = self.request_metrics.app(
                app_id, st.active_requests, self._rm.queued_count(app_id)
            )
        return out

    def gateway_request_metrics(self) -> dict:
        return self.request_metrics.all_gateways()

    def active_time(self, app_id: str) -> dict:
        return dict(self._active_time.get(app_id, {}))

    # -- Prometheus (spec 13 §3, Phase 5) -----------------------------------
    def prometheus(self) -> str:
        """Prometheus text exposition (spec 13 §3)."""
        from collections import Counter

        s = self.current() or {}
        # Reserved = active reservations; external = used not attributed to apps.
        reserved = sum(r.required_mb for r in self._resources.reservations().values())
        committed = sum(self._resources.observed_mb(a) for a in self._app_ids)
        external = max(0.0, s.get("gpu_used_mb", 0) - committed)
        lines = [
            "# GPUSymbiosis metrics (spec 13 §3)",
            _gauge("gpu_load_balancer_gpu_memory_bytes", s.get("gpu_total_mb", 0) * 1024, state="total"),
            _gauge("gpu_load_balancer_gpu_memory_bytes", s.get("gpu_used_mb", 0) * 1024, state="used"),
            _gauge("gpu_load_balancer_gpu_memory_bytes", s.get("gpu_free_mb", 0) * 1024, state="free"),
            _gauge("gpu_load_balancer_gpu_memory_bytes", reserved * 1024, state="reserved"),
            _gauge("gpu_load_balancer_gpu_memory_bytes", external * 1024, state="external"),
            _gauge("gpu_load_balancer_gpu_utilization", s.get("gpu_utilization_pct", 0)),
            _gauge("gpu_load_balancer_gpu_temperature_celsius", s.get("gpu_temperature_c", 0)),
            _gauge("gpu_load_balancer_gpu_power_watts", s.get("gpu_power_w", 0)),
        ]
        # Measured load/unload timings (spec 11 §2.3 / 13 §3) from the app rows.
        timings: dict[str, dict] = {}
        try:
            with self._db() as sess:
                for row in sess.query(Application).all():
                    timings[row.id] = row
        except Exception:  # noqa: BLE001
            pass
        for app_id, m in s.get("applications", {}).items():
            lines.append(_gauge("gpu_load_balancer_application_memory_bytes",
                                m.get("gpu_memory_mb", 0) * 1024, application=app_id))
            lines.append(_gauge("gpu_load_balancer_application_gpu_utilization",
                                m.get("gpu_utilization_pct", 0), application=app_id))
            lines.append(_gauge("gpu_load_balancer_application_state",
                                1 if m.get("residency_state") == "LOADED" else 0,
                                application=app_id, state=m.get("residency_state", "")))
            row = timings.get(app_id)
            if row is not None:
                if row.load_time_sec is not None:
                    lines.append(_gauge("gpu_load_balancer_application_load_seconds",
                                        row.load_time_sec, application=app_id))
                if row.unload_time_sec is not None:
                    lines.append(_gauge("gpu_load_balancer_application_unload_seconds",
                                        row.unload_time_sec, application=app_id))
        for gw_id, m in s.get("gateways", {}).items():
            lines.append(_gauge("gpu_load_balancer_gateway_queue_length",
                                m.get("queued", 0), gateway=gw_id))
            lines.append(_gauge("gpu_load_balancer_gateway_wait_seconds",
                                m.get("oldest_wait_sec", 0), gateway=gw_id))
        # Request totals + duration (spec 13 §3), per gateway.
        for gw_id, m in self.gateway_request_metrics().items():
            lines.append(_counter("gpu_load_balancer_request_total",
                                  m.get("requests_completed", 0), gateway=gw_id, status="completed"))
            lines.append(_counter("gpu_load_balancer_request_total",
                                  m.get("requests_failed", 0), gateway=gw_id, status="failed"))
            lines.append(_counter("gpu_load_balancer_request_total",
                                  m.get("requests_cancelled", 0), gateway=gw_id, status="cancelled"))
            lines.append(_counter("gpu_load_balancer_request_total",
                                  m.get("requests_expired", 0), gateway=gw_id, status="expired"))
            if m.get("average_wait_sec") is not None:
                lines.append(_gauge("gpu_load_balancer_request_duration_seconds",
                                    m.get("average_wait_sec", 0), gateway=gw_id, phase="wait"))
            if m.get("average_service_sec") is not None:
                lines.append(_gauge("gpu_load_balancer_request_duration_seconds",
                                    m.get("average_service_sec", 0), gateway=gw_id, phase="service"))
        # Scheduler decisions + switches (spec 13 §3), from the decision log.
        try:
            with self._db() as sess:
                decisions = Counter((r.decision,) for r in sess.query(SchedulerEvent).all())
            for dec, n in decisions.items():
                lines.append(_counter("gpu_load_balancer_scheduler_decisions_total",
                                      n, decision=dec))
            switches = sum(n for (d,), n in decisions.items() if d in ("switch", "activate_concurrent"))
            if switches:
                lines.append(_counter("gpu_load_balancer_switch_total", switches))
        except Exception:  # noqa: BLE001
            log.debug("prometheus decision series skipped")
        return "\n".join(lines) + "\n"


# -- helpers -------------------------------------------------------------------
def _merge_metrics(app_sample: dict, type_metrics: dict, request: dict, active_time: dict) -> dict:
    """Combine the four metric sources into the ``raw`` view.

    Log-derived (lifetime) values win. Request metrics fill every key they
    uniquely own (latency percentiles, request counts) and any key still
    missing/zero, but never override a non-zero log-derived value — the bucket
    snapshot carries zeroed input/output/total_tokens on a fresh process and
    would otherwise stomp the tailer's lifetime totals.
    """
    values = {**app_sample, **type_metrics, **active_time}
    for key, v in request.items():
        if key not in values or not values.get(key):
            values[key] = v
    return values


async def _safe_collect(handler, app_id: str) -> dict:
    if handler is None:
        return {}
    try:
        result = handler.collect_metrics(app_id)
        # LlamaHandler/ComfyUIHandler collect_metrics is async (HTTP polling).
        if asyncio.iscoroutine(result):
            result = await asyncio.wait_for(result, timeout=3.0)
        return result if isinstance(result, dict) else {}
    except Exception:  # noqa: BLE001
        log.exception("handler collect failed", application_id=app_id)
        return {}


def _round(v):
    if v is None:
        return None
    if isinstance(v, float):
        return round(v, 2)
    return v


def _metrics_view(spec, values: dict) -> list[dict]:
    """Spec-driven card view (spec 08 §9): one entry per field, in spec order,
    tagged with the type's display group — the UI renders from this list, so a
    type's fields dictate its own card layout (spec 04 §8).

    Requests dedupe: when the type's spec declares its own "requests" field
    (log-derived) the gateway-tracked "requests_received" is shown through it
    (log count wins, gateway count fills in) — never twice. Apps without a log
    (mock/generic) show the gateway count as their "Requests" tile.
    """
    metric_defs = list(spec.metrics) + COMMON_METRICS
    suppress = {"requests_received"} if any(m.id == "requests" for m in metric_defs) else set()
    metrics_view = []
    seen: set[str] = set()
    for m in metric_defs:
        if m.id in seen or m.id in suppress:
            continue
        seen.add(m.id)
        if m.id in values or m.source == "sample":
            v = values.get(m.id)
            if m.id == "requests" and not v:
                v = values.get("requests_received")
            metrics_view.append({
                "id": m.id, "name": m.name, "unit": m.unit, "kind": m.kind,
                "display": m.display, "group": m.group,
                "value": _round(v),
            })
    return metrics_view


def _gauge(name: str, value: float, **labels) -> str:
    return _line(name, value, labels)


def _counter(name: str, value: float, **labels) -> str:
    return _line(name, value, labels)


def _line(name: str, value: float, labels: dict) -> str:
    if labels:
        joined = ",".join(f'{k}="{v}"' for k, v in labels.items())
        return f"{name}{{{joined}}} {value}"
    return f"{name} {value}"
