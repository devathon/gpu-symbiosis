"""Management API (spec 10): FastAPI on 127.0.0.1:9130, admin-only.

All lifecycle actions are async transitions (202). The UI polls
/api/status or subscribes to /api/events/sse (spec 11 §1).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from gpusybio.config.seed import new_request_id
from gpusybio.database.models import Application, Gateway, Request
from gpusybio.events.bus import PERSISTENT_EVENTS
from gpusybio.events.sse import SSEHub
from gpusybio.gateway.request_manager import RequestManager
from gpusybio.metrics import MetricsCollector
from gpusybio.state import StateManager
from gpusybio.applications.lifecycle import combined_ui_state


class GatewayIn(BaseModel):
    name: str
    port: int
    application_id: str
    priority: int = 5
    max_waiting_queue: int = 20
    max_waiting_time_sec: int = 300
    wait_timeout_policy: str = "ESCALATE_PRIORITY"
    request_timeout_sec: float | None = None
    max_concurrent_requests: int = 1
    enabled: bool = True


def _app_view(row: Application, state, resources) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description,
        "process_state": state.process.value,
        "residency_state": state.residency.value,
        "state": combined_ui_state(state),
        "pid": state.pid,
        "draining": state.draining,
        "active_requests": state.active_requests,
        "queued_requests": state.queued_requests,
        "gpu_memory_used_mb": round(resources.observed_mb(row.id), 1),
        "gpu_memory_configured_mb": row.configured_gpu_memory_mb,
        "gpu_memory_effective_mb": row.effective_gpu_memory_mb,
        "startup_time_sec": row.startup_time_sec,
        "load_time_sec": row.load_time_sec,
        "unload_time_sec": row.unload_time_sec,
        "stop_time_sec": row.stop_time_sec,
        "supports_load_unload": row.supports_load_unload,
        "allow_automatic_unload": row.allow_automatic_unload,
        "pinned": row.pinned,
        "consecutive_load_failures": row.consecutive_load_failures,
        "enabled": row.enabled,
    }


def _gw_view(row: Gateway, rm: RequestManager, states: StateManager) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "port": row.port,
        "application_id": row.application_id,
        "priority": row.priority,
        "max_waiting_queue": row.max_waiting_queue,
        "max_waiting_time_sec": row.max_waiting_time_sec,
        "wait_timeout_policy": row.wait_timeout_policy,
        "request_timeout_sec": row.request_timeout_sec,
        "max_concurrent_requests": row.max_concurrent_requests,
        "enabled": row.enabled,
        "state": _gw_state(row, rm, states),
        "queue": {
            "length": rm.queue().length(row.id),
            "oldest_wait_sec": round(rm.queue().oldest_wait_sec(row.id), 1),
        },
    }


def _gw_state(row: Gateway, rm: RequestManager, states: StateManager) -> str:
    if not row.enabled:
        return "DISABLED"
    s = states.get(row.application_id)
    from gpusybio.applications.lifecycle import ProcessState, ResidencyState

    if s.process is ProcessState.ERROR:
        return "ERROR"
    if s.process is ProcessState.STARTING:
        return "LOADING"
    if s.process is ProcessState.STOPPING:
        return "UNLOADING"
    if rm.queue().length(row.id) > 0:
        return "WAITING" if s.active_requests == 0 else "PROCESSING"
    if s.active_requests > 0:
        return "PROCESSING"
    return "IDLE"


def make_admin_api(
    db,
    states: StateManager,
    resources,
    request_manager: RequestManager,
    apps,
    scheduler,
    metrics: MetricsCollector,
    hub: SSEHub,
) -> FastAPI:
    api = FastAPI(title="GPUSymbiosis", version="0.1.0")

    def _resource_view() -> dict:
        """Full resource view (spec 10 §3): GPU identity + utilisation + system."""
        s = resources.last_sample or resources.sample()
        sys_ = resources.system_sample()
        return {
            "total_mb": resources.total_mb,
            "usable_mb": resources.usable_mb,
            "used_mb": s.used_mb,
            "available_mb": resources.available_mb(),
            "vram_pct": round(s.used_pct, 1),
            "gpu_pct": round(s.utilization_pct, 1),
            "temperature_c": s.temperature_c,
            "clock_mhz": s.clock_mhz,
            "power_w": s.power_w,
            "power_limit_w": s.power_limit_w,
            "name": s.name,
            "pcie": s.pcie,
            "rx_mbps": s.rx_mbps,
            "tx_mbps": s.tx_mbps,
            "ram_total_mb": sys_.ram_total_mb,
            "ram_used_mb": sys_.ram_used_mb,
            "cpu_pct": sys_.cpu_pct,
            "cpu_cores": sys_.cpu_cores,
        }

    @api.get("/api/status")
    def status():
        with db() as s:
            apps_ = {a.id: _app_view(a, states.get(a.id), resources) for a in s.query(Application)}
            gws = {g.id: _gw_view(g, request_manager, states) for g in s.query(Gateway)}
        return {
            "applications": apps_,
            "gateways": gws,
            "resources": _resource_view(),
        }

    # -- applications (spec 10 §1) -------------------------------------------
    @api.post("/api/applications", status_code=201)
    def create_application(body: dict):
        try:
            row = Application(id=body["id"], **{k: v for k, v in body.items() if k != "id"})
            with db() as s:
                s.add(row)
            apps.load_config(row.id)
            request_manager.app_configs().setdefault(
                row.id, {"max_concurrent_requests": row.max_concurrent_requests}
            )
            return {"id": row.id, "transition": None}
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, str(e))

    @api.get("/api/applications")
    def list_applications():
        with db() as s:
            return [_app_view(a, states.get(a.id), resources) for a in s.query(Application)]

    @api.get("/api/applications/{app_id}")
    def get_application(app_id: str):
        with db() as s:
            row = s.get(Application, app_id)
        if row is None:
            raise HTTPException(404, "not found")
        return _app_view(row, states.get(app_id), resources)

    @api.put("/api/applications/{app_id}")
    def update_application(app_id: str, body: dict):
        with db() as s:
            row = s.get(Application, app_id)
            if row is None:
                raise HTTPException(404, "not found")
            for k, v in body.items():
                if hasattr(row, k) and k not in ("id",):
                    setattr(row, k, v)
        apps.load_config(app_id)
        request_manager.app_configs().update(
            {app_id: {"max_concurrent_requests": request_manager.app_configs()[app_id]["max_concurrent_requests"]}}
        )
        return {"id": app_id, "transition": None}

    @api.delete("/api/applications/{app_id}", status_code=202)
    def delete_application(app_id: str):
        s = states.get(app_id)
        if s.process.value != "STOPPED" or s.active_requests > 0:
            raise HTTPException(409, "application is not stopped")
        with db() as sess:
            row = sess.get(Application, app_id)
            if row is None:
                raise HTTPException(404, "not found")
            for g in row.gateways if hasattr(row, "gateways") else []:
                sess.delete(g)
            sess.delete(row)
        apps._configs.pop(app_id, None)
        request_manager.app_configs().pop(app_id, None)
        return {"id": app_id, "transition": "deleted"}

    async def _transition(app_id: str, op: str, force: bool = False):
        s = states.get(app_id)
        if s.transition is not None:
            raise HTTPException(409, f"transition '{s.transition}' in flight")
        if op in ("start", "load"):
            # Invariant 2 (spec 14): never exceed the VRAM budget — enforced for
            # manual actions too. An already-resident app passes (zero-cost path,
            # spec 06 §7); otherwise the required VRAM (incl. safety margin) must
            # fit alongside current residents (spec 07 §3).
            from gpusybio.applications.lifecycle import ProcessState, ResidencyState

            cfg = apps.config(app_id)
            required = resources.required_for(cfg.configured_gpu_memory_mb, cfg.safety_margin)
            already_resident = (
                s.process is ProcessState.RUNNING and s.residency is ResidencyState.LOADED
            )
            if not resources.fits(app_id, required, already_resident=already_resident):
                raise HTTPException(
                    409,
                    {
                        "detail": (
                            f"insufficient VRAM for {app_id}: would exceed the budget "
                            f"(Invariant 2). Required {required:.0f} MB "
                            f"(incl. safety margin), free {resources.available_mb():.0f} MB."
                        ),
                        "required_mb": round(required, 1),
                        "free_mb": round(resources.available_mb(), 1),
                        "residents": scheduler.residents(),
                    },
                )
        if op == "start":
            result = await apps.start(app_id)
            return {"id": app_id, "transition": "start"}
        if op == "stop":
            result = await apps.stop(app_id, force=force)
            return {"id": app_id, "transition": "stop"}
        if op == "load":
            result = await apps.load(app_id)
            return {"id": app_id, "transition": "load"}
        if op == "unload":
            result = await apps.unload(app_id)
            return {"id": app_id, "transition": "unload"}
        raise HTTPException(400, f"unknown op {op}")

    @api.post("/api/applications/{app_id}/start", status_code=202)
    async def start_application(app_id: str):
        return await _transition(app_id, "start")

    @api.post("/api/applications/{app_id}/stop", status_code=202)
    async def stop_application(app_id: str, force: bool = False):
        return await _transition(app_id, "stop", force)

    @api.post("/api/applications/{app_id}/load", status_code=202)
    async def load_application(app_id: str):
        return await _transition(app_id, "load")

    @api.post("/api/applications/{app_id}/unload", status_code=202)
    async def unload_application(app_id: str):
        return await _transition(app_id, "unload")

    # -- gateways (spec 10 §2) -------------------------------------------------
    @api.post("/api/gateways", status_code=201)
    def create_gateway(body: GatewayIn):
        row = Gateway(id=new_request_id().replace("req-", "gw-"), **body.model_dump())
        with db() as s:
            s.add(row)
        request_manager.gateway_configs().update(
            {row.id: {
                "id": row.id,
                "application_id": row.application_id,
                "priority": row.priority,
                "max_waiting_queue": row.max_waiting_queue,
                "max_waiting_time_sec": row.max_waiting_time_sec,
                "wait_timeout_policy": row.wait_timeout_policy,
                "request_timeout_sec": row.request_timeout_sec,
                "max_concurrent_requests": row.max_concurrent_requests,
                "enabled": row.enabled,
                "port": row.port,
            }}
        )
        return {"id": row.id, "port": row.port}

    @api.get("/api/gateways")
    def list_gateways():
        with db() as s:
            return [_gw_view(g, request_manager, states) for g in s.query(Gateway)]

    @api.put("/api/gateways/{gw_id}")
    def update_gateway(gw_id: str, body: dict):
        with db() as s:
            row = s.get(Gateway, gw_id)
            if row is None:
                raise HTTPException(404, "not found")
            for k, v in body.items():
                if hasattr(row, k) and k != "id":
                    setattr(row, k, v)
        gc = request_manager.gateway_configs().get(gw_id)
        if gc is not None:
            for k, v in body.items():
                if k in gc:
                    gc[k] = v
        return {"id": gw_id}

    @api.delete("/api/gateways/{gw_id}")
    def delete_gateway(gw_id: str):
        with db() as s:
            row = s.get(Gateway, gw_id)
            if row is None:
                raise HTTPException(404, "not found")
            s.delete(row)
        request_manager.gateway_configs().pop(gw_id, None)
        return {"id": gw_id}

    @api.post("/api/gateways/{gw_id}/enable")
    def enable_gateway(gw_id: str):
        with db() as s:
            row = s.get(Gateway, gw_id)
            if row is None:
                raise HTTPException(404, "not found")
            row.enabled = True
        gc = request_manager.gateway_configs().get(gw_id)
        if gc is not None:
            gc["enabled"] = True
        return {"id": gw_id, "enabled": True}

    @api.post("/api/gateways/{gw_id}/disable")
    def disable_gateway(gw_id: str):
        with db() as s:
            row = s.get(Gateway, gw_id)
            if row is None:
                raise HTTPException(404, "not found")
            row.enabled = False
        gc = request_manager.gateway_configs().get(gw_id)
        if gc is not None:
            gc["enabled"] = False
        return {"id": gw_id, "enabled": False}

    @api.post("/api/gateways/{gw_id}/process-now", status_code=202)
    async def process_now(gw_id: str):
        from gpusybio.events.bus import PROCESS_NOW

        await request_manager.bus().publish_raw(PROCESS_NOW, gateway_id=gw_id)
        await hub.push({"type": PROCESS_NOW, "gateway_id": gw_id})
        return {"id": gw_id}

    @api.post("/api/gateways/{gw_id}/clear-queue")
    async def clear_queue(gw_id: str):
        slots = request_manager.queue().clear(gw_id)
        for slot in slots:
            request_manager._forward.pop(slot.request_id, None)
            request_manager._update_status(slot.request_id, "CANCELLED", completed_at=True,
                                           error="queue cleared")
        return {"id": gw_id, "cancelled": len(slots)}

    # -- system / gpu / requests / events (spec 10 §3) ---------------------------
    @api.get("/api/system")
    def system():
        return {
            "version": "0.1.0",
            "gpu": _resource_view(),
            "metrics": metrics.current(),
        }

    @api.get("/api/gpu")
    def gpu():
        view = _resource_view()
        s = resources.last_sample or resources.sample()
        return {
            **view,
            "free_mb": s.free_mb,
            "reservations": {
                a: round(r.required_mb, 1) for a, r in resources.reservations().items()
            },
        }

    @api.get("/api/requests")
    def list_requests(status: str | None = None, gateway_id: str | None = None, limit: int = 100):
        with db() as s:
            q = s.query(Request)
            if status:
                q = q.filter(Request.status == status)
            if gateway_id:
                q = q.filter(Request.gateway_id == gateway_id)
            rows = q.order_by(Request.queued_at.desc()).limit(limit).all()
            return [
                {
                    "id": r.id,
                    "gateway_id": r.gateway_id,
                    "application_id": r.application_id,
                    "status": r.status,
                    "priority": r.priority,
                    "received_at": r.received_at.isoformat() if r.received_at else None,
                    "queued_at": r.queued_at.isoformat() if r.queued_at else None,
                    "started_at": r.started_at.isoformat() if r.started_at else None,
                    "completed_at": r.completed_at.isoformat() if r.completed_at else None,
                    "queue_wait_sec": r.queue_wait_sec,
                    "service_time_sec": r.service_time_sec,
                    "error": r.error,
                }
                for r in rows
            ]

    @api.get("/api/events")
    def list_events(limit: int = 200, application_id: str | None = None):
        from gpusybio.database.models import ApplicationEvent, GatewayEvent, SchedulerEvent

        out = []
        with db() as s:
            for e in s.query(ApplicationEvent).order_by(ApplicationEvent.at.desc()).limit(limit):
                out.append({"kind": "application", "application_id": e.application_id,
                           "event": e.event_type, "detail": e.detail, "at": e.at.isoformat() if e.at else None})
            for e in s.query(GatewayEvent).order_by(GatewayEvent.at.desc()).limit(limit):
                out.append({"kind": "gateway", "gateway_id": e.gateway_id,
                           "event": e.event_type, "detail": e.detail, "at": e.at.isoformat() if e.at else None})
            for e in s.query(SchedulerEvent).order_by(SchedulerEvent.at.desc()).limit(limit):
                out.append({"kind": "scheduler", "trigger": e.trigger,
                           "candidate": e.candidate_application_id, "decision": e.decision,
                           "detail": e.detail, "at": e.at.isoformat() if e.at else None})
        out.sort(key=lambda x: x["at"] or "", reverse=True)
        return out[:limit]

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

    # -- SSE (spec 11 §1) ----------------------------------------------------------
    @api.get("/api/events/sse")
    async def sse():
        async def stream():
            async with hub.subscribe() as s:
                async for line in s:
                    yield line

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    # -- event observer wiring -----------------------------------------------------
    async def _observe(event):
        if event.type in PERSISTENT_EVENTS:
            d = event.data
            try:
                from gpusybio.database.models import ApplicationEvent, GatewayEvent

                with db() as s:
                    if d.get("application_id"):
                        s.add(ApplicationEvent(
                            application_id=d["application_id"],
                            event_type=event.type,
                            detail=json.dumps({k: v for k, v in d.items() if k != "application_id"}) or None,
                        ))
                    elif d.get("gateway_id"):
                        s.add(GatewayEvent(
                            gateway_id=d["gateway_id"],
                            event_type=event.type,
                            detail=json.dumps({k: v for k, v in d.items() if k != "gateway_id"}) or None,
                        ))
            except Exception:  # noqa: BLE001
                pass
        await hub.push(
            {**event.data, "type": event.type, "at": datetime.now(timezone.utc).isoformat()}
        )

    # -- web UI (spec 14): serve the built bundle if present -------------------
    web_dist = _find_web_dist()
    if web_dist is not None:

        @api.get("/{path:path}")
        def web_file(path: str) -> FileResponse:
            if path.startswith("api/"):
                raise HTTPException(404)
            candidate = (web_dist / path.lstrip("/")).resolve()
            if path and candidate.is_file() and str(candidate).startswith(str(web_dist.resolve())):
                # Content-hashed assets are immutable; safe to cache aggressively.
                return FileResponse(str(candidate), headers={"Cache-Control": "public, max-age=31536000, immutable"})
            # index.html must always revalidate so a new build is picked up.
            return FileResponse(str(web_dist / "index.html"), headers={"Cache-Control": "no-cache"})

    # CORS for the Vite dev server (loopback only).
    api.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    api.state.observe = _observe
    return api


def _find_web_dist():
    import os
    from pathlib import Path

    env = os.environ.get("GPUSYBIO_WEB_DIST")
    if env:
        p = Path(env).resolve()
        return p if p.is_dir() else None
    # Default: <project_root>/web/dist (this file is gpusybio/api/__init__.py).
    cand = Path(__file__).resolve().parents[2] / "web" / "dist"
    return cand if cand.is_dir() else None
