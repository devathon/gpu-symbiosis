"""External-apps routes: unmanaged GPU processes shown under Applications with
a kill button.

``register`` binds the shared names from the :class:`ApiContext`. The list +
config reads are open (loopback); the kill is gated by the auth middleware
(mutating route). The monitor lives in memory — nothing here touches the DB.
"""

from __future__ import annotations

from fastapi import HTTPException, Request


def register(api, ctx) -> None:
    _audit = ctx.audit
    _actor = ctx.actor
    monitor = getattr(ctx, "external_apps", None)

    @api.get("/api/external-apps")
    def list_external_apps():
        if monitor is None:
            return []
        return monitor.list()

    @api.get("/api/external-apps/config")
    def external_apps_config():
        if monitor is None:
            return {
                "gpu_threshold": 20.0,
                "vram_threshold": 100.0,
                "auto_unload": True,
                "auto_unload_exclude": "",
            }
        return monitor.config()

    @api.post("/api/external-apps/{pid}/kill")
    async def kill_external_app(pid: int, request: Request):
        if monitor is None:
            raise HTTPException(503, "external-apps monitoring is not enabled")
        if pid not in {row["pid"] for row in monitor.list()}:
            raise HTTPException(404, "no such external app")
        result = await monitor.kill(pid, reason="manual")
        await _audit("external_app_killed", actor=_actor(request), detail=str(pid))
        if not result.get("killed"):
            raise HTTPException(502, result.get("detail", "kill failed"))
        return result
