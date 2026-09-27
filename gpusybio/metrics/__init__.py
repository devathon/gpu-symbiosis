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
from gpusybio.events.log import get_logger
from gpusybio.gateway.request_manager import RequestManager
from gpusybio.metrics.aggregate import MetricsAggregator
from gpusybio.metrics.handlers import GenericHandler, make_handler
from gpusybio.metrics.requests import RequestMetrics
from gpusybio.metrics.typespec import COMMON_METRICS, TypeRegistry
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
        # Type-metrics contract (spec 08 §9).
        self.type_registry = TypeRegistry()
        self._handlers = {
            app_id: make_handler(
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

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._session = ClientSession()
        # Re-wire handlers with the live session (token/queue polling).
        for app_id in self._app_ids:
            self._handlers[app_id] = make_handler(
                self._app_types.get(app_id, "generic"),
                self._resources, self._states, self.request_metrics, self._base_urls,
                self._session, log_paths=self._log_paths,
            )
        self.aggregator.start()
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

    async def _loop(self, interval: float = 1.0) -> None:
        while self._running:
            t0 = time.time()
            try:
                await self._sample(interval)
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

        # Type spec for auto-rendering (spec 08 §9).
        row = None
        with self._db() as s:
            row = s.get(Application, app_id)
        app_type = (row.application_type if row else None) or self._app_types.get(app_id, "generic")
        custom = _parse_custom_metrics(row) if (row and app_type == "custom") else None
        spec = self.type_registry.spec_for(app_type, custom)

        # Merge: request metrics win over sample for shared ids.
        values = {**app_sample, **type_metrics, **request, **active_time}
        metric_defs = list(spec.metrics) + COMMON_METRICS
        metrics_view = []
        for m in metric_defs:
            if m.id in values or m.source == "sample":
                metrics_view.append({
                    "id": m.id, "name": m.name, "unit": m.unit, "kind": m.kind,
                    "display": m.display, "value": _round(values.get(m.id)),
                })
        return {
            "application_id": app_id,
            "type": app_type,
            "window": window,
            "state": {"process_state": st.process.value, "residency_state": st.residency.value,
                      "active_requests": st.active_requests,
                      "queued_requests": self._rm.queued_count(app_id)},
            "metrics": metrics_view,
            "raw": values,
            "active_time": active_time,
            "spec": [{"id": m.id, "name": m.name, "unit": m.unit, "kind": m.kind,
                      "display": m.display} for m in spec.metrics],
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


def _parse_custom_metrics(row) -> list[dict] | None:
    """Read a custom type's metric list from its config (spec 08 §9)."""
    import json

    raw = getattr(row, "description", None)
    if raw and raw.startswith("{"):
        try:
            data = json.loads(raw)
            return data.get("metrics")
        except Exception:  # noqa: BLE001
            return None
    return None


def _gauge(name: str, value: float, **labels) -> str:
    return _line(name, value, labels)


def _counter(name: str, value: float, **labels) -> str:
    return _line(name, value, labels)


def _line(name: str, value: float, labels: dict) -> str:
    if labels:
        joined = ",".join(f'{k}="{v}"' for k, v in labels.items())
        return f"{name}{{{joined}}} {value}"
    return f"{name} {value}"
