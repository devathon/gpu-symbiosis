"""Shared API context.

``make_admin_api`` builds a single :class:`ApiContext` and hands it to the
route-group modules (``auth``, ``applications``, ``gateways``, ``system``,
``metrics``, ``events``). The context holds every dependency a route needs
plus the cross-cutting helpers (audit, acting-identity, admin session/CSRF
lookup, cookie params, the full resource view) so no group module has to
reach into a shared closure.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import FastAPI, Request

from gpusybio.auth.admin import validate_session


@dataclass
class ApiContext:
    # Core dependencies (all route groups need most of these).
    db: object                      # session factory: ``with ctx.db() as s:``
    states: object                  # StateManager
    resources: object               # ResourceMonitor
    request_manager: object         # RequestManager
    apps: object                    # ApplicationManager
    scheduler: object               # Scheduler
    metrics: object                 # MetricsCollector
    hub: object                     # SSEHub

    # Optional wiring (None when the corresponding subsystem is disabled).
    bus: object = None
    idle_unloader: object = None
    on_gateway_created: object = None
    on_gateway_removed: object = None

    # Set after construction; the route groups register themselves on it.
    api: FastAPI | None = field(default=None, repr=False)

    # -- cross-cutting helpers (spec 12 §5 audit + spec 12 §3 auth) ----------
    async def bus_publish(self, action: str, actor: str | None, detail: str = ""):
        """Publish an audit event to the bus + SSE (spec 12 §5)."""
        if self.bus is not None:
            await self.bus.publish_raw("audit", action=action, actor=actor, detail=detail)
        try:
            await self.hub.push({"type": "audit", "action": action, "actor": actor, "detail": detail})
        except Exception:  # noqa: BLE001
            pass

    async def audit(self, action: str, actor: str | None, detail: str = ""):
        """Audit a state-changing action with the acting identity (spec 12 §5)."""
        try:
            await self.bus_publish(action, actor, detail=detail)
        except Exception:  # noqa: BLE001
            pass

    def session_from_scope(self, scope) -> dict | None:
        token = None
        for k, v in scope.get("headers", []):
            if k == b"cookie":
                for part in v.decode("latin-1").split(";"):
                    part = part.strip()
                    if part.startswith("gslb_session="):
                        token = part[len("gslb_session="):]
                break
        return validate_session(token)

    def session_from_request(self, request: Request) -> dict | None:
        return validate_session(request.cookies.get("gslb_session"))

    def csrf_from_request(self, request: Request) -> str | None:
        return request.headers.get("x-csrf-token")

    def actor(self, request: Request) -> str | None:
        """Acting identity for audit (spec 12 §5); None in development mode."""
        session = self.session_from_request(request)
        return session["username"] if session else None

    def cookie_params(self, token: str | None, delete: bool = False) -> dict:
        return {
            "key": "gslb_session",
            "value": "" if delete else token,
            "max_age": 0 if delete else 12 * 3600,
            "httponly": True,
            "samesite": "strict",
            "path": "/",
        }

    def resource_view(self) -> dict:
        """Full resource view (spec 10 §3): GPU identity + utilisation + system."""
        s = self.resources.last_sample or self.resources.sample()
        sys_ = self.resources.system_sample()
        return {
            "total_mb": self.resources.total_mb,
            "usable_mb": self.resources.usable_mb,
            "used_mb": s.used_mb,
            "available_mb": self.resources.available_mb(),
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
