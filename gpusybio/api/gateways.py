"""Gateway routes (spec 10 §2) + per-gateway API keys (spec 12 §2) + the queue
request inspector (spec 11 §4).

``register`` binds the shared names from the :class:`ApiContext` at the top;
the route bodies below are unchanged from the original ``make_admin_api``.
"""

from __future__ import annotations

from fastapi import HTTPException, Request

from gpusybio.database.models import Application, Gateway
from gpusybio.gateway.request_manager import new_request_id
from gpusybio.api.views import GatewayIn, _gw_view


def register(api, ctx) -> None:
    db = ctx.db
    states = ctx.states
    request_manager = ctx.request_manager
    hub = ctx.hub
    on_gateway_created = ctx.on_gateway_created
    on_gateway_removed = ctx.on_gateway_removed
    _audit = ctx.audit
    _actor = ctx.actor

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
