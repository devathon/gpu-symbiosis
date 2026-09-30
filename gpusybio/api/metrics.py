"""Metrics routes (spec 08 §9.3) + idle state (spec 06 §10) + the scheduler
current-decision surface + what-if simulation (spec 15 §Phase 5).

``register`` binds the shared names from the :class:`ApiContext` at the top;
the route bodies below are unchanged from the original ``make_admin_api``.
"""

from __future__ import annotations

from fastapi import HTTPException

from gpusybio.database.models import Application


def register(api, ctx) -> None:
    db = ctx.db
    states = ctx.states
    scheduler = ctx.scheduler
    metrics = ctx.metrics
    idle_unloader = ctx.idle_unloader

    @api.get("/api/applications/{app_id}/metrics")
    async def application_metrics(app_id: str, window: str = "1h"):
        with db() as s:
            if s.get(Application, app_id) is None:
                raise HTTPException(404, "not found")
        return await metrics.app_metrics(app_id, window)

    @api.get("/api/metrics/summary")
    def metrics_summary():
        """Current / average / maximum triplets (spec 08 §6)."""
        return metrics.aggregator.summary()

    @api.get("/api/metrics/history")
    def metrics_history(scope: str = "gpu", metric: str = "gpu_mem_mb",
                        window: str = "1m", limit: int = 500):
        window_sec = 3600 if window == "1h" else 60
        return metrics.aggregator.history(scope, metric, window_sec, limit)

    @api.get("/api/metrics/request")
    def request_metrics():
        """Per-application and per-gateway request/latency metrics (spec 08 §2/§4)."""
        return {
            "applications": metrics.app_request_metrics(),
            "gateways": metrics.gateway_request_metrics(),
        }

    @api.get("/api/metrics/prometheus")
    def prometheus():
        """Prometheus text exposition (spec 13 §3)."""
        from fastapi.responses import PlainTextResponse

        return PlainTextResponse(
            metrics.prometheus(), media_type="text/plain; version=0.0.4; charset=utf-8"
        )

    @api.get("/api/idle/status")
    def idle_status():
        """Per-application idle state (spec 06 §10): active, idle-for, unloadable."""
        if idle_unloader is None:
            return {"applications": {}}
        import time as _time

        from gpusybio.applications.lifecycle import ProcessState, ResidencyState

        now = _time.time()
        out: dict[str, dict] = {}
        for app_id in states.all():
            s = states.get(app_id)
            active = idle_unloader.is_active(app_id)
            last = idle_unloader.last_active(app_id)
            idle_for = round(now - last, 1) if last is not None and not active else None
            ok, reason = idle_unloader._can_auto_unload(app_id) if (
                s.process is ProcessState.RUNNING and s.residency is ResidencyState.LOADED
            ) else (False, "not loaded")
            out[app_id] = {
                "active": active,
                "idle_for_sec": idle_for,
                "method": "requests" if idle_unloader._has_gateway(app_id) else "gpu_usage",
                "auto_unloadable": ok,
                "reason": reason,
            }
        return {"applications": out}

    @api.get("/api/scheduler/current-decision")
    def current_decision():
        d = dict(scheduler.last_decision or {})
        d.setdefault("decision", "no_candidate")
        sc = scheduler.settings().scheduler
        d.setdefault("config", {
            "min_residency_sec": sc.min_residency_sec,
            "switch_margin": sc.switch_margin,
            "switch_cost_weight": sc.switch_cost_weight,
            "emergency_wait_sec": sc.emergency_wait_sec,
            "emergency_queue_threshold": sc.emergency_queue_threshold,
            "switch_cooldown_sec": sc.switch_cooldown_sec,
            "process_now_multiplier": sc.process_now_multiplier,
            "process_now_boost_ttl_sec": sc.process_now_boost_ttl_sec,
        })
        d.setdefault("residents", scheduler.residents())
        d.setdefault("switch_tracking", scheduler.switch_tracking())
        d.setdefault("boosts", scheduler.boosts())
        d.setdefault("vram", scheduler._vram_accounting())
        return d

    @api.post("/api/scheduler/simulate")
    async def simulate(body: dict | None = None):
        """Evaluate the scoring model against current state WITHOUT acting.

        Runs the same decision logic as a live trigger in dry-run mode: no
        forwards, loads, switches, boosts or decision bookkeeping are applied.
        The response is the decision that *would* have been made, tagged
        ``simulated: true`` (spec 15: "what-if mode ... without acting").
        """
        trigger = (body or {}).get("trigger", "reconcile")
        return await scheduler.simulate(trigger)
