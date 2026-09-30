"""Pure API view helpers + request/response models.

These functions take their inputs explicitly (a row, a state, a resources
monitor) and hold no closure state, so they live here rather than inside
``make_admin_api``. Route groups import them from this module.
"""

from __future__ import annotations

import json

from fastapi import HTTPException
from pydantic import BaseModel

from gpusybio.database.models import Application, Gateway
from gpusybio.gateway.request_manager import RequestManager
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
        "log_file": row.log_file,
        "pid_file": row.pid_file,
        "port": row.port,
        "protocol": row.protocol,
        "health_check_url": row.health_check_url,
        "ready_check_url": row.ready_check_url,
        "cpu_memory_required_mb": row.cpu_memory_required_mb,
        "gpu_memory_safety_margin": row.gpu_memory_safety_margin,
        "idle_unload_timeout_sec": row.idle_unload_timeout_sec,
        "activity_detection_method": row.activity_detection_method,
        "idle_threshold_gpu_pct": row.idle_threshold_gpu_pct,
        "loaded_gpu_memory_mb": row.loaded_gpu_memory_mb,
        # The ``custom`` type's declared metrics (spec 08 §9.6) — the edit view
        # pre-fills its fields from this; None for types without declared metrics.
        "metrics": _app_metrics(row),
    }


def _app_metrics(row) -> list | None:
    """The custom type's metric list, parsed from its JSON document column."""
    if not row.metrics:
        return None
    try:
        data = json.loads(row.metrics)
    except Exception:  # noqa: BLE001
        return None
    return data if isinstance(data, list) else None


def _coerce_metrics(value) -> str | None:
    """Normalize a client-supplied ``metrics`` value (a list, the legacy
    ``{"metrics": [...]}`` object, or an already-serialized JSON string) into
    the stored JSON-document form (a string), or None when unset."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            data = json.loads(value)
        except Exception:  # noqa: BLE001
            raise HTTPException(400, "metrics must be a JSON array or object")
    else:
        data = value
    if not isinstance(data, (list, dict)):
        raise HTTPException(400, "metrics must be a JSON array or object")
    return json.dumps(data)


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


def _find_web_dist():
    import os
    from pathlib import Path

    env = os.environ.get("GPUSYBIO_WEB_DIST")
    if env:
        p = Path(env).resolve()
        return p if p.is_dir() else None
    # Default: <project_root>/web/dist (this file is gpusybio/api/views.py).
    cand = Path(__file__).resolve().parents[2] / "web" / "dist"
    return cand if cand.is_dir() else None
