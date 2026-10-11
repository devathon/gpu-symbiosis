"""Application routes (spec 10 §1) + /api/status (spec 10 §3).

``register`` binds the shared names from the :class:`ApiContext` at the top;
the route bodies below are unchanged from the original ``make_admin_api``.
"""

from __future__ import annotations

from fastapi import HTTPException, Request

from gpusybio.database.models import Application, Gateway
from gpusybio.api.views import _app_view, _coerce_command_env, _coerce_metrics, _gw_view


def register(api, ctx) -> None:
    db = ctx.db
    states = ctx.states
    resources = ctx.resources
    request_manager = ctx.request_manager
    apps = ctx.apps
    scheduler = ctx.scheduler
    metrics = ctx.metrics
    _audit = ctx.audit
    _actor = ctx.actor
    _resource_view = ctx.resource_view
    external_apps = getattr(ctx, "external_apps", None)

    @api.get("/api/status")
    def status():
        with db() as s:
            apps_ = {a.id: _app_view(a, states.get(a.id), resources) for a in s.query(Application)}
            gws = {g.id: _gw_view(g, request_manager, states) for g in s.query(Gateway)}
        return {
            "applications": apps_,
            "gateways": gws,
            "resources": _resource_view(),
            # In-memory external (unmanaged) GPU processes above threshold; the
            # UI polls this 2 s status — no separate endpoint polling needed.
            "external_apps": external_apps.list() if external_apps is not None else [],
        }

    @api.post("/api/applications", status_code=201)
    async def create_application(body: dict, request: Request):
        try:
            body = dict(body)
            if "metrics" in body:
                body["metrics"] = _coerce_metrics(body["metrics"])
            if "command_env" in body:
                body["command_env"] = _coerce_command_env(body["command_env"])
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

    @api.get("/api/application-types")
    def list_application_types():
        """Registered application types (spec 04 §8): built-ins + drop-in
        modules under <data_path>/plugins. The app form pre-fills its options
        from this list."""
        return {"types": metrics.type_registry.known()}

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
                    if k == "metrics":
                        v = _coerce_metrics(v)
                    elif k == "command_env":
                        v = _coerce_command_env(v)
                    setattr(row, k, v)
            # Kept in sync (a cache for direct DB readers); the source of truth
            # is ``unload_script`` — read sites derive the flag from it.
            row.supports_load_unload = bool(row.unload_script)
        apps.load_config(app_id)
        # Live update (no restart): the running request manager must honor the
        # new limit from the row just saved. (Previously this copied the live
        # value into itself — a no-op — so the change only applied after a
        # restart.) Direct assignment also covers an app id missing from the
        # live config (old code would have raised KeyError).
        request_manager.app_configs()[app_id] = {
            "max_concurrent_requests": row.max_concurrent_requests
        }
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
                s.process is ProcessState.STARTED and s.residency is ResidencyState.LOADED
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

    # Request cancellation (spec 05 §6.3 / Phase 5).
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
