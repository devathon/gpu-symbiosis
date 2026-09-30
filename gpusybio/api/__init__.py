"""Management API (spec 10): FastAPI on 127.0.0.1:9130, admin-only.

All lifecycle actions are async transitions (202). The UI polls
/api/status or subscribes to /api/events/sse (spec 11 §1).

The route groups are split across sibling modules (``auth``, ``applications``,
``gateways``, ``system``, ``metrics``, ``events``); each exposes a
``register(api, ctx)`` that binds the shared names from an :class:`ApiContext`.
``make_admin_api`` builds one context and registers every group in the order
the routes were historically defined.
"""

from __future__ import annotations

from fastapi import FastAPI

from gpusybio.api.context import ApiContext
from gpusybio.api import auth, applications, gateways, system, metrics as metrics_routes, events
# Re-exported for direct import (tests + the app view pre-fill).
from gpusybio.api.views import _app_view  # noqa: F401


def make_admin_api(
    db,
    states,
    resources,
    request_manager,
    apps,
    scheduler,
    metrics,
    hub,
    idle_unloader=None,
    bus=None,
    on_gateway_created=None,
    on_gateway_removed=None,
) -> FastAPI:
    ctx = ApiContext(
        db=db,
        states=states,
        resources=resources,
        request_manager=request_manager,
        apps=apps,
        scheduler=scheduler,
        metrics=metrics,
        hub=hub,
        idle_unloader=idle_unloader,
        bus=bus,
        on_gateway_created=on_gateway_created,
        on_gateway_removed=on_gateway_removed,
    )
    api = FastAPI(title="GPUSymbiosis", version="0.1.0")
    ctx.api = api

    # Registration order preserved from the original single closure: admin auth
    # + middleware first, then status/applications, gateways, system, metrics,
    # and finally SSE + the event observer + web UI + CORS.
    auth.register(api, ctx)
    applications.register(api, ctx)
    gateways.register(api, ctx)
    system.register(api, ctx)
    metrics_routes.register(api, ctx)
    events.register(api, ctx)

    return api
