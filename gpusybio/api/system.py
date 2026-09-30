"""System / GPU / request / event routes (spec 10 §3).

``register`` binds the shared names from the :class:`ApiContext` at the top;
the route bodies below are unchanged from the original ``make_admin_api``.
"""

from __future__ import annotations

from gpusybio.database.models import Request as RequestModel


def register(api, ctx) -> None:
    db = ctx.db
    resources = ctx.resources
    metrics = ctx.metrics
    _resource_view = ctx.resource_view

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
