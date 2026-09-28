"""Management API (spec 10): FastAPI on 127.0.0.1:9130, admin-only.

All lifecycle actions are async transitions (202). The UI polls
/api/status or subscribes to /api/events/sse (spec 11 §1).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from gpusybio.auth.admin import (
    admin_configured,
    check_csrf,
    create_admin,
    create_session,
    current_user,
    delete_admin,
    list_admins,
    revoke_session,
    validate_session,
    verify_admin,
)
from gpusybio.config.seed import new_request_id
from gpusybio.database.models import Application, Gateway, Request as RequestModel
from gpusybio.events.bus import PERSISTENT_EVENTS
from gpusybio.events.sse import SSEHub
from gpusybio.gateway.request_manager import RequestManager
from gpusybio.metrics import MetricsCollector
from gpusybio.scoring import gateway_score
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


class AuthBody(BaseModel):
    username: str
    password: str


def _app_view(row: Application, state, resources) -> dict:
    from gpusybio.scheduler.activity import is_active

    util = round(resources.app_utilization(row.id), 1)
    active = is_active(state, resources, row.idle_threshold_gpu_pct)
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description,
        "process_state": state.process.value,
        "residency_state": state.residency.value,
        "state": combined_ui_state(state, active),
        "active": active,
        "pid": state.pid,
        "draining": state.draining,
        "active_requests": state.active_requests,
        "queued_requests": state.queued_requests,
        "gpu_utilization_pct": util,
        "gpu_memory_used_mb": round(resources.observed_mb(row.id), 1),
        "gpu_memory_configured_mb": row.configured_gpu_memory_mb,
        "gpu_memory_effective_mb": row.effective_gpu_memory_mb,
        "startup_time_sec": row.startup_time_sec,
        "load_time_sec": row.load_time_sec,
        "unload_time_sec": row.unload_time_sec,
        "stop_time_sec": row.stop_time_sec,
        "supports_load_unload": bool(row.unload_script),
        "allow_automatic_unload": row.allow_automatic_unload,
        "pinned": row.pinned,
        "consecutive_load_failures": row.consecutive_load_failures,
        "enabled": row.enabled,
        # Form fields (spec 11 §2.3) — the edit view pre-fills from these.
        "process_name": row.process_name,
        "application_type": row.application_type,
        "start_script": row.start_script,
        "stop_script": row.stop_script,
        "load_script": row.load_script,
        "unload_script": row.unload_script,
        "port": row.port,
        "protocol": row.protocol,
        "health_check_url": row.health_check_url,
        "ready_check_url": row.ready_check_url,
        "cpu_memory_required_mb": row.cpu_memory_required_mb,
        "gpu_memory_safety_margin": row.gpu_memory_safety_margin,
        "idle_unload_timeout_sec": row.idle_unload_timeout_sec,
        "activity_detection_method": row.activity_detection_method,
        "idle_threshold_gpu_pct": row.idle_threshold_gpu_pct,
        "memory_load_threshold_pct": row.memory_load_threshold_pct,
        "memory_unload_threshold_pct": row.memory_unload_threshold_pct,
    }


def _gw_score(rm: RequestManager, row: Gateway) -> dict | None:
    """Per-gateway scheduler score + full breakup (spec 06 §6.9) for the
    explainability surface — shown on the gateway card, hoverable. Computed for
    every enabled gateway (idle ones score 0); demand is time-decayed (spec 06
    §6.3/§6.4) so it is a live metric, not a cached candidate value."""
    if not row.enabled:
        return None
    d = rm.demand()
    return gateway_score(
        row.priority,
        rm.queue().length(row.id),
        row.max_waiting_queue,
        rm.queue().oldest_wait_sec(row.id),
        row.max_waiting_time_sec,
        d.arrival_rate(row.id),
        d.service_rate(row.application_id),
    )


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
        "score": _gw_score(rm, row),
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
    # Per-gateway in-flight count (spec 11 §3): a gateway shows PROCESSING only
    # while serving its OWN requests — two gateways bound to the same app must
    # not both light up just because the shared app is busy.
    if rm.active_count_gateway(row.id) > 0:
        return "PROCESSING"
    if rm.queue().length(row.id) > 0:
        return "WAITING"
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
    idle_unloader=None,
    bus=None,
    on_gateway_created=None,
    on_gateway_removed=None,
) -> FastAPI:
    api = FastAPI(title="GPUSymbiosis", version="0.1.0")

    async def bus_publish(action: str, actor: str | None, detail: str = ""):
        """Publish an audit event to the bus + SSE (spec 12 §5)."""
        if bus is not None:
            await bus.publish_raw("audit", action=action, actor=actor, detail=detail)
        try:
            await hub.push({"type": "audit", "action": action, "actor": actor, "detail": detail})
        except Exception:  # noqa: BLE001
            pass

    # -- admin auth (spec 12 §3) --------------------------------------------------
    # State-changing admin endpoints require a valid session (+ CSRF) once any
    # admin account exists; read-only GETs stay open (loopback default).
    _AUTH_EXEMPT = {"/api/auth/login", "/api/auth/logout", "/api/auth/admins"}
    _MUTATING = {"POST", "PUT", "DELETE", "PATCH"}

    def _session_from_scope(scope) -> dict | None:
        token = None
        for k, v in scope.get("headers", []):
            if k == b"cookie":
                for part in v.decode("latin-1").split(";"):
                    part = part.strip()
                    if part.startswith("gslb_session="):
                        token = part[len("gslb_session="):]
                break
        return validate_session(token)

    def _session_from_request(request) -> dict | None:
        return validate_session(request.cookies.get("gslb_session"))

    def _csrf_from_request(request) -> str | None:
        return request.headers.get("x-csrf-token")

    def _actor(request) -> str | None:
        """Acting identity for audit (spec 12 §5); None in development mode."""
        session = _session_from_request(request)
        return session["username"] if session else None

    @api.middleware("http")
    async def _admin_auth(request, call_next):
        if request.method in _MUTATING and request.url.path.startswith("/api/"):
            if request.url.path not in _AUTH_EXEMPT and admin_configured(db):
                session = _session_from_scope(request.scope)
                if session is None:
                    return JSONResponse({"error": "authentication required"}, status_code=401)
                if not check_csrf(session, _csrf_from_request(request)):
                    return JSONResponse({"error": "invalid or missing CSRF token"}, status_code=403)
        return await call_next(request)

    def _cookie_params(token: str | None, delete: bool = False) -> dict:
        return {
            "key": "gslb_session",
            "value": "" if delete else token,
            "max_age": 0 if delete else 12 * 3600,
            "httponly": True,
            "samesite": "strict",
            "path": "/",
        }

    async def _audit(action: str, actor: str | None, detail: str = ""):
        """Audit a state-changing action with the acting identity (spec 12 §5)."""
        try:
            await bus_publish(action, actor, detail=detail)
        except Exception:  # noqa: BLE001
            pass

    @api.post("/api/auth/login")
    async def login(body: AuthBody, response: Response):
        admin_id = verify_admin(db, body.username, body.password)
        if admin_id is None:
            raise HTTPException(401, "invalid credentials")
        token, csrf = create_session(body.username)
        response.set_cookie(**_cookie_params(token))
        await _audit("admin_login", actor=body.username)
        return {"username": body.username, "csrf": csrf, "token": token}

    @api.post("/api/auth/logout")
    async def logout(request: Request, response: Response):
        session = _session_from_request(request)
        if session is not None:
            revoke_session(request.cookies.get("gslb_session"))
        response.delete_cookie("gslb_session", path="/")
        return {"ok": True}

    @api.get("/api/auth/me")
    async def me(request: Request):
        session = _session_from_request(request)
        if session is None:
            raise HTTPException(401, "not authenticated")
        return {"username": session["username"]}

    @api.get("/api/auth/csrf")
    async def csrf_token(request: Request):
        # Returns the session's CSRF token so the UI can re-acquire it after a
        # page reload (the double-submit token is session-scoped, spec 12 §3).
        session = _session_from_request(request)
        if session is None:
            raise HTTPException(401, "not authenticated")
        return {"csrf": session["csrf"]}

    @api.post("/api/auth/admins")
    async def create_admin_endpoint(body: AuthBody, request: Request):
        # Bootstrap: open when no admin exists; otherwise requires a session.
        if admin_configured(db) and _session_from_request(request) is None:
            raise HTTPException(401, "authentication required")
        try:
            admin_id, username = create_admin(db, body.username, body.password)
        except ValueError as e:
            raise HTTPException(409, str(e))
        await _audit("admin_created", actor=body.username, detail=f"admin {username}")
        return {"id": admin_id, "username": username}

    @api.get("/api/auth/admins")
    async def list_admins_endpoint():
        # Open (usernames only): lets the UI detect whether to show the login
        # screen or the first-admin bootstrap screen. No passwords exposed.
        return {"admins": list_admins(db)}

    @api.delete("/api/auth/admins/{admin_id}")
    async def delete_admin_endpoint(admin_id: str, request: Request):
        session = _session_from_request(request)
        if session is None:
            raise HTTPException(401, "authentication required")
        if not delete_admin(db, admin_id):
            raise HTTPException(404, "not found")
        await _audit("admin_deleted", actor=session["username"], detail=admin_id)
        return {"ok": True}

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
    async def create_application(body: dict, request: Request):
        try:
            row = Application(id=body["id"], **{k: v for k, v in body.items() if k != "id"})
            # Kept in sync (a cache for direct DB readers); the source of truth
            # is ``unload_script`` — read sites derive the flag from it.
            row.supports_load_unload = bool(row.unload_script)
            with db() as s:
                s.add(row)
            apps.load_config(row.id)
            request_manager.app_configs().setdefault(
                row.id, {"max_concurrent_requests": row.max_concurrent_requests}
            )
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, str(e))
        await _audit("application_created", actor=_actor(request), detail=row.id)
        return {"id": row.id, "transition": None}

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
    async def update_application(app_id: str, body: dict, request: Request):
        with db() as s:
            row = s.get(Application, app_id)
            if row is None:
                raise HTTPException(404, "not found")
            for k, v in body.items():
                if hasattr(row, k) and k not in ("id",):
                    setattr(row, k, v)
            # Kept in sync (a cache for direct DB readers); the source of truth
            # is ``unload_script`` — read sites derive the flag from it.
            row.supports_load_unload = bool(row.unload_script)
        apps.load_config(app_id)
        request_manager.app_configs().update(
            {app_id: {"max_concurrent_requests": request_manager.app_configs()[app_id]["max_concurrent_requests"]}}
        )
        await _audit("application_updated", actor=_actor(request), detail=app_id)
        return {"id": app_id, "transition": None}

    @api.delete("/api/applications/{app_id}", status_code=202)
    async def delete_application(request: Request, app_id: str, confirm: bool = False):
        # Destructive action (spec 12 §3): Delete requires explicit confirmation.
        if not confirm:
            raise HTTPException(409, "delete requires confirm=true")
        s = states.get(app_id)
        if s.process.value != "STOPPED" or s.active_requests > 0:
            raise HTTPException(409, "application is not stopped")
        from gpusybio.database.models import (
            ApplicationEvent,
            GatewayApiKey,
            GatewayEvent,
            Request as ReqModel,
        )

        with db() as sess:
            row = sess.get(Application, app_id)
            if row is None:
                raise HTTPException(404, "not found")
            # Children first (PRAGMA foreign_keys=ON): gateways, their keys,
            # events and all requests referencing this app.
            gw_ids = [g.id for g in sess.query(Gateway).filter_by(application_id=app_id)]
            if gw_ids:
                for model, col in ((GatewayApiKey, "gateway_id"), (GatewayEvent, "gateway_id")):
                    sess.query(model).filter(getattr(model, col).in_(gw_ids)).delete(synchronize_session=False)
                sess.query(ReqModel).filter(ReqModel.gateway_id.in_(gw_ids)).delete(synchronize_session=False)
                sess.query(Gateway).filter(Gateway.id.in_(gw_ids)).delete(synchronize_session=False)
            sess.query(ApplicationEvent).filter_by(application_id=app_id).delete(synchronize_session=False)
            sess.query(ReqModel).filter_by(application_id=app_id).delete(synchronize_session=False)
            sess.delete(row)
        apps._configs.pop(app_id, None)
        request_manager.app_configs().pop(app_id, None)
        await _audit("application_deleted", actor=_actor(request), detail=app_id)
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
    async def stop_application(request: Request, app_id: str, force: bool = False, confirm: bool = False):
        # Destructive action (spec 12 §3): a Force Stop requires explicit confirmation.
        if force and not confirm:
            raise HTTPException(409, "force stop requires confirm=true")
        result = await _transition(app_id, "stop", force)
        if force:
            await _audit("force_stop", actor=_actor(request), detail=app_id)
        return result

    @api.post("/api/applications/{app_id}/load", status_code=202)
    async def load_application(app_id: str):
        return await _transition(app_id, "load")

    @api.post("/api/applications/{app_id}/unload", status_code=202)
    async def unload_application(app_id: str):
        if not apps.config(app_id).supports_load_unload:
            raise HTTPException(
                400, "app has no unload script — it cannot be unloaded; stop it instead"
            )
        return await _transition(app_id, "unload")

    # -- pinned applications (spec 06 §3.1 / Phase 5) --------------------------
    # Pinned apps are hard-excluded from automatic unloading (scheduler + idle
    # unloader). The flag is persisted and reflected in the app status.
    def _set_pinned(app_id: str, pinned: bool):
        with db() as s:
            row = s.get(Application, app_id)
            if row is None:
                raise HTTPException(404, "not found")
            row.pinned = pinned
        apps.load_config(app_id)
        return {"id": app_id, "pinned": pinned}

    @api.post("/api/applications/{app_id}/pin")
    async def pin_application(app_id: str, request: Request):
        result = _set_pinned(app_id, True)
        await _audit("app_pinned", actor=_actor(request), detail=app_id)
        return result

    @api.post("/api/applications/{app_id}/unpin")
    async def unpin_application(app_id: str, request: Request):
        result = _set_pinned(app_id, False)
        await _audit("app_unpinned", actor=_actor(request), detail=app_id)
        return result

    # -- request cancellation (spec 05 §6.3 / Phase 5) -------------------------
    @api.post("/api/requests/{request_id}/cancel", status_code=202)
    async def cancel_request(request_id: str, request: Request):
        from gpusybio.database.models import Request

        with db() as s:
            row = s.get(Request, request_id)
            if row is None:
                raise HTTPException(404, "not found")
            terminal = row.status in ("COMPLETED", "FAILED", "CANCELLED", "EXPIRED")
        if not terminal:
            await request_manager.cancel(request_id)
            await _audit("request_cancelled", actor=_actor(request), detail=request_id)
        return {"id": request_id, "cancelled": not terminal}

    # -- gateways (spec 10 §2) -------------------------------------------------
    @api.post("/api/gateways", status_code=201)
    async def create_gateway(body: GatewayIn, request: Request):
        with db() as s:
            if s.get(Application, body.application_id) is None:
                raise HTTPException(400, f"application '{body.application_id}' not found")
            if any(g.port == body.port for g in s.query(Gateway)):
                raise HTTPException(409, f"port {body.port} already in use")
        row = Gateway(id=new_request_id().replace("req-", "gw-"), **body.model_dump())
        with db() as s:
            s.add(row)
        request_manager.set_gateway(row.id, {
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
        })
        if on_gateway_created is not None:
            try:
                await on_gateway_created(row.id)
            except Exception as e:  # noqa: BLE001
                # Roll back: the listener couldn't start (port conflict, …).
                with db() as s:
                    s.delete(s.get(Gateway, row.id))
                request_manager.gateway_configs().pop(row.id, None)
                raise HTTPException(409, f"could not start gateway listener: {e}")
        await _audit("gateway_created", actor=_actor(request), detail=f"{row.id} :{row.port}")
        return {"id": row.id, "port": row.port}

    @api.get("/api/gateways")
    def list_gateways():
        with db() as s:
            return [_gw_view(g, request_manager, states) for g in s.query(Gateway)]

    @api.put("/api/gateways/{gw_id}")
    async def update_gateway(gw_id: str, body: dict, request: Request):
        with db() as s:
            row = s.get(Gateway, gw_id)
            if row is None:
                raise HTTPException(404, "not found")
            if "application_id" in body and s.get(Application, body["application_id"]) is None:
                raise HTTPException(400, f"application '{body['application_id']}' not found")
            if "port" in body and any(
                g.port == body["port"] and g.id != gw_id for g in s.query(Gateway)
            ):
                raise HTTPException(409, f"port {body['port']} already in use")
            old_port = row.port
            for k, v in body.items():
                if hasattr(row, k) and k != "id":
                    setattr(row, k, v)
        gc = request_manager.gateway(gw_id)
        for k, v in body.items():
            if k in gc:
                gc[k] = v
        # Only a port *change* re-spawns the listener live (spec 11 §3.2). The
        # form always echoes the current port, so re-spawning on every save would
        # re-bind the same port and race the shutdown ("address already in use").
        # A changed port is a different socket, so re-spawning it is safe.
        if "port" in body and body["port"] != old_port and on_gateway_created is not None:
            try:
                await on_gateway_created(gw_id)
            except Exception as e:  # noqa: BLE001
                # New port unusable: restore the old port. _spawn_gateway probes
                # before stopping the old listener, so the old listener is still
                # up and this leaves the gateway consistent (listener on old_port).
                with db() as s:
                    s.get(Gateway, gw_id).port = old_port
                gc["port"] = old_port
                raise HTTPException(409, f"could not start gateway on port {body['port']}: {e}")
        await _audit("gateway_updated", actor=_actor(request), detail=gw_id)
        return {"id": gw_id}

    @api.delete("/api/gateways/{gw_id}")
    async def delete_gateway(request: Request, gw_id: str, confirm: bool = False):
        # Destructive action (spec 12 §3): Delete requires explicit confirmation.
        if not confirm:
            raise HTTPException(409, "delete requires confirm=true")
        from gpusybio.database.models import (
            GatewayApiKey,
            GatewayEvent,
            Request as ReqModel,
        )

        with db() as s:
            row = s.get(Gateway, gw_id)
            if row is None:
                raise HTTPException(404, "not found")
            # Children first (PRAGMA foreign_keys=ON).
            s.query(GatewayApiKey).filter_by(gateway_id=gw_id).delete(synchronize_session=False)
            s.query(GatewayEvent).filter_by(gateway_id=gw_id).delete(synchronize_session=False)
            s.query(ReqModel).filter_by(gateway_id=gw_id).delete(synchronize_session=False)
            s.delete(row)
        request_manager.remove_gateway(gw_id)
        if on_gateway_removed is not None:
            await on_gateway_removed(gw_id)
        await _audit("gateway_deleted", actor=_actor(request), detail=gw_id)
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

    # -- per-gateway API keys (spec 12 §2) ------------------------------------
    @api.post("/api/gateways/{gw_id}/keys", status_code=201)
    def create_key(gw_id: str, name: str | None = None):
        with db() as s:
            if s.get(Gateway, gw_id) is None:
                raise HTTPException(404, "not found")
        from gpusybio.auth import create_key as _create_key

        key_id, plaintext = _create_key(db, gw_id, name)
        # The plaintext key is shown once at creation (spec 12 §2).
        return {"id": key_id, "name": name, "key": plaintext}

    @api.get("/api/gateways/{gw_id}/keys")
    def list_keys(gw_id: str):
        from gpusybio.auth import list_keys as _list_keys

        return {"keys": _list_keys(db, gw_id)}

    @api.delete("/api/gateways/{gw_id}/keys/{key_id}")
    def delete_key(gw_id: str, key_id: str):
        from gpusybio.auth import delete_key as _delete_key

        ok = _delete_key(db, gw_id, key_id)
        if not ok:
            raise HTTPException(404, "not found")
        return {"id": key_id, "deleted": True}

    @api.post("/api/gateways/{gw_id}/process-now", status_code=202)
    async def process_now(gw_id: str):
        from gpusybio.events.bus import PROCESS_NOW

        await request_manager.bus().publish_raw(PROCESS_NOW, gateway_id=gw_id)
        await hub.push({"type": PROCESS_NOW, "gateway_id": gw_id})
        return {"id": gw_id}

    @api.post("/api/gateways/{gw_id}/clear-queue")
    async def clear_queue(request: Request, gw_id: str, confirm: bool = False):
        # Destructive action (spec 12 §3): Clear Queue requires explicit confirmation.
        if not confirm:
            raise HTTPException(409, "clear queue requires confirm=true")
        slots = request_manager.queue().clear(gw_id)
        for slot in slots:
            request_manager._forward.pop(slot.request_id, None)
            request_manager._update_status(slot.request_id, "CANCELLED", completed_at=True,
                                           error="queue cleared")
        await _audit("clear_queue", actor=_actor(request), detail=gw_id)
        return {"id": gw_id, "cancelled": len(slots)}

    # -- queue request inspector (spec 11 §4) -------------------------------------
    @api.get("/api/gateways/{gw_id}/queue")
    def inspect_queue(gw_id: str):
        from gpusybio.database.models import Request as ReqModel

        slots = request_manager.queue().slots(gw_id)
        with db() as s:
            received = {r.id: r.received_at for r in s.query(ReqModel).all()
                        if r.status == "QUEUED"}
        return {
            "gateway_id": gw_id,
            "queue": [
                {
                    "request_id": slot.request_id,
                    "priority": slot.priority,
                    "queued_at": slot.queued_at,
                    "waiting_sec": round(slot.waiting_sec, 1),
                    "received_at": received.get(slot.request_id),
                    "metadata": slot.metadata or {},
                }
                for slot in slots
            ],
        }

    @api.post("/api/gateways/{gw_id}/queue/{request_id}/force")
    async def force_queue_request(gw_id: str, request_id: str, request: Request):
        # spec 11 §4: "force-process (raises to top of queue)". Queued only —
        # never an active request.
        slot = request_manager.queue().boost(request_id)
        if slot is None:
            raise HTTPException(404, "not queued")
        if slot.gateway_id != gw_id:
            raise HTTPException(400, "wrong gateway")
        await _audit("force_queue", actor=_actor(request), detail=f"{gw_id}:{request_id}")
        return {"id": request_id, "priority": slot.priority}

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
            q = s.query(RequestModel)
            if status:
                q = q.filter(RequestModel.status == status)
            if gateway_id:
                q = q.filter(RequestModel.gateway_id == gateway_id)
            rows = q.order_by(RequestModel.queued_at.desc()).limit(limit).all()
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
                    "input_tokens": r.input_tokens,
                    "output_tokens": r.output_tokens,
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

    # -- metrics (spec 08 §9.3) ----------------------------------------------
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

    # -- what-if simulation (spec 15 §Phase 5) ------------------------------------
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
