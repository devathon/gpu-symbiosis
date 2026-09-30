"""SSE event stream (spec 11 §1), the event-observer wiring, and the web-UI
static bundle (spec 14).

``register`` binds the shared names from the :class:`ApiContext` at the top;
the route bodies below are unchanged from the original ``make_admin_api``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse

from gpusybio.events.bus import PERSISTENT_EVENTS
from gpusybio.api.views import _find_web_dist


def register(api, ctx) -> None:
    db = ctx.db
    hub = ctx.hub

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

    # Web UI (spec 14): serve the built bundle if present.
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
